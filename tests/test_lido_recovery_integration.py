"""Offline integration of durable acquisition with the unchanged EL replay.

The tiny source file is explicitly synthetic, not valid Beacon SSZ. Its saved
inspection is injected only at the SSZ inspection boundary. Real journal,
transport-envelope, scope, authentication, budget, and execution replay code
run against local fixtures; no network or publication is performed.
"""
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lido"))
import execution as ex
import refresh
from recovery import RunJournal
from publication import validate_result
from test_execution_hourly import FakeRpc, event, h, seed
from test_lido_rpc_concurrency import Envelope, Transport
from rpc_clock import advancing_wait


class FixtureClock:
    def __init__(self):
        self.wall = 1200.0
        self.monotonic = time.monotonic()

    def __call__(self):
        return {"wall": self.wall, "monotonic": self.monotonic,
                "identity": "offline-recovery-fixture/boot-1"}

    def advance(self, seconds):
        self.wall += seconds
        self.monotonic += seconds


def request_key(request):
    return json.dumps([request["method"], request["params"]], sort_keys=True)


def comparable(result):
    result = deepcopy(result)
    result["validation"]["finality_wait"].pop("wait_seconds")
    return result


class RecoveryIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="lido-recovery-integration-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "data").mkdir()
        shutil.copytree(ROOT / "scripts" / "lido", self.root / "scripts" / "lido",
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.clock = FixtureClock()
        self.state = seed()
        old = json.loads((ROOT / "tests/fixtures/lido-cutoff-20261004.json").read_text())
        old.update(asOf=refresh.utc(self.state["timestamp"]), blockNumber=100, blockHash=h(100))
        self.run_id = "offline-recovery-integration"
        self.old, reason = refresh.propose_lease(
            old, int(self.clock.wall), self.run_id, trigger="manual",
            manual_request_id="offline-recovery-request")
        self.assertIsNone(reason)
        self.base = {refresh.SNAPSHOT_PATH: refresh.canonical(self.old).encode(),
                     refresh.STATE_PATH: refresh.canonical(self.state).encode()}
        for name, content in self.base.items():
            (self.root / name).write_bytes(content)
        self.source_file = self.root / "fixture.synthetic.ssz"
        self.source_file.write_bytes(b"OFFLINE SYNTHETIC SSZ STAND-IN; NOT BEACON DATA\n")
        self.summary = {
            "state_bytes": self.source_file.stat().st_size,
            "state_sha256": hashlib.sha256(self.source_file.read_bytes()).hexdigest(),
            "state_root": h(900), "derived_beacon_block_header_root": h(212),
            "block_number": 105, "block_hash": h(105), "timestamp": 1148,
        }
        self.acquisition = {
            "source_url": "https://offline.invalid/fixture.synthetic.ssz",
            "bytes": self.summary["state_bytes"],
            "retrieval_started_timestamp": self.clock.wall,
            "retrieved_timestamp": self.clock.wall,
        }
        self.logs = [
            event("WithdrawalRequested", (25, 20), (2, 1, 1), source="queue", block=101),
            event("WithdrawalRequested", (25, 20), (3, 1, 1), source="queue", block=101,
                  tx=1, index=1),
            event("Resumed", source="queue", block=102, timestamp=1112),
            event("Transfer", (1,), (1, 2), block=103, timestamp=1124),
            event("Resumed", source="queue", block=105, timestamp=1148),
        ]
        self.directory = self.root / "work" / "recovery"
        self.seal_path = self.root / "controller" / "seal.json"
        self.head = "1" * 40

    def options(self):
        return dict(root=self.root, head=self.head, run_id=self.run_id,
                    base_blobs={name: (self.root / name).read_bytes() for name in self.base},
                    endpoint=refresh.RPC_URL, route_id="offline-same-route",
                    seal_path=self.seal_path, clock=self.clock)

    def create(self):
        journal = RunJournal.create(self.directory, **self.options())
        self.addCleanup(journal.close)
        acquisition = {**self.acquisition,
                       "retrieval_started_timestamp": self.clock.wall,
                       "retrieved_timestamp": self.clock.wall}
        journal.pin_source(self.source_file, acquisition, self.summary)
        return journal

    def reopen(self, **overrides):
        options = self.options()
        options.update(fresh_main=True, reviewed_same_route="offline reviewed route fixture",
                       inspect_ssz=lambda path: deepcopy(self.summary))
        options.update(overrides)
        journal = RunJournal.open(self.directory, **options)
        self.addCleanup(journal.close)
        return journal

    def transport(self, *, pause_balance=False, limit_ranges=False, error=None):
        fake = FakeRpc(self.logs)
        paused = False

        def respond(request):
            nonlocal paused
            if error is not None:
                raise error
            if pause_balance and not paused and request["method"] == "eth_getBalance":
                paused = True
                raise urllib.error.URLError(OSError("Tunnel connection failed: 403 Forbidden"))
            if limit_ranges and request["method"] == "eth_getLogs":
                query = request["params"][0]
                if query["fromBlock"] != query["toBlock"]:
                    return Envelope(jsonrpc="2.0", id=request["id"],
                                    error={"code": -32005, "message": "fixture log size limit"})
            return fake(request["method"], request["params"])

        return Transport(respond)

    def collect(self, journal, transport, **kwargs):
        rpc = refresh.DurableRpc(journal, self.state, self.summary,
                                 admission_wait=advancing_wait(self.clock))
        with patch("refresh.open_rpc", side_effect=transport):
            result = ex.collect_at(self.state, 105, h(105), rpc, **kwargs)
        return result, rpc

    def pause(self, journal, *, limit_ranges=False):
        transport = self.transport(pause_balance=True, limit_ranges=limit_ranges)
        with self.assertRaises(RuntimeError):
            self.collect(journal, transport)
        transport.wait_until_idle()
        self.assertEqual(journal.status, "paused")
        self.assertEqual(self.state, seed())
        for name, content in self.base.items():
            self.assertEqual((self.root / name).read_bytes(), content)
        journal.close()
        return transport

    def assert_fresh_suffix(self, requests):
        self.assertEqual([(row["method"], row["params"]) for row in requests[-6:]], [
            ("eth_getBalance", [ex.ADDRESSES["core"], "0x69"]),
            ("eth_getBalance", [ex.ADDRESSES["el_vault"], "0x69"]),
            ("eth_getBalance", [ex.ADDRESSES["withdrawal_vault"], "0x69"]),
            ("eth_getBlockByNumber", ["0x64", False]),
            ("eth_getBlockByNumber", ["0x69", False]),
            ("eth_getBlockByNumber", ["0x6a", False]),
        ])

    def test_uninterrupted_durable_replay_matches_unchanged_execution(self):
        journal = self.create()
        transport = self.transport()
        result, rpc = self.collect(journal, transport)
        reference = ex.collect_at(self.state, 105, h(105), FakeRpc(self.logs))
        self.assertEqual(comparable(result), comparable(reference))
        self.assertEqual(journal.calls, len(transport.requests))
        self.assertEqual(rpc.calls, journal.calls)
        self.assert_fresh_suffix(transport.requests)
        self.assertEqual(set(transport.urls), {refresh.RPC_URL})
        self.assertEqual(self.state, seed())

    def test_resume_replays_identically_without_refetching_successful_historical_data(self):
        journal = self.create()
        first = self.pause(journal)
        spent, original_deadline = journal.calls, journal.deadline
        self.clock.advance(30)
        journal = self.reopen()
        self.assertEqual(journal.calls, spent)
        self.assertEqual(journal.deadline, original_deadline)
        resumed = self.transport()
        result, _ = self.collect(journal, resumed)
        expected = ex.collect_at(self.state, 105, h(105), FakeRpc(self.logs))
        self.assertEqual(comparable(result), comparable(expected))
        self.assertEqual(len(resumed.requests), 10)
        self.assertEqual([row["params"] for row in resumed.requests[:4]],
                         [["0x64", False], ["0x69", False], ["0x6a", False], ["finalized", False]])
        self.assertFalse(any(row["method"] == "eth_getLogs" for row in resumed.requests))
        self.assertFalse(any(row["method"] == "eth_getBlockByNumber" and
                             row["params"][0] in ("0x65", "0x66") for row in resumed.requests))
        self.assert_fresh_suffix(resumed.requests)
        all_requests = first.requests + resumed.requests
        self.assertEqual(sorted(row["id"] for row in all_requests), list(range(1, journal.calls + 1)))
        self.assertEqual(journal.calls, spent + 10)

    def test_real_limit_errors_split_again_but_successful_leaves_are_reused(self):
        journal = self.create()
        first = self.pause(journal, limit_ranges=True)
        journal = self.reopen()
        resumed = self.transport(limit_ranges=True)
        result, _ = self.collect(journal, resumed)
        reference = ex.collect_at(self.state, 105, h(105), FakeRpc(self.logs))
        # Split coverage is different, but reconstruction must be identical.
        for name in ("state", "snapshot", "report_timing", "execution_anchor"):
            self.assertEqual(result[name], reference[name])
        first_logs = [row for row in first.requests if row["method"] == "eth_getLogs"]
        next_logs = [row for row in resumed.requests if row["method"] == "eth_getLogs"]
        self.assertTrue(first_logs)
        self.assertTrue(next_logs)
        self.assertTrue(all(row["params"][0]["fromBlock"] != row["params"][0]["toBlock"]
                            for row in next_logs))
        failed_ranges = Counter(request_key(row) for row in first_logs
                                if row["params"][0]["fromBlock"] != row["params"][0]["toBlock"])
        self.assertEqual(Counter(map(request_key, next_logs)), failed_ranges)
        self.assertEqual(journal.calls, len(first.requests) + len(resumed.requests))
        self.assert_fresh_suffix(resumed.requests)

    def test_latest_checkout_rejects_changed_checkpoint_before_transport(self):
        journal = self.create()
        self.pause(journal)
        changed = deepcopy(self.state)
        changed["last_report_timestamp"] += 1
        (self.root / refresh.STATE_PATH).write_text(refresh.canonical(changed))
        with patch("refresh.open_rpc") as wire, self.assertRaises(RuntimeError):
            self.reopen()
        wire.assert_not_called()

    def test_latest_checkout_rejects_changed_code_before_transport(self):
        journal = self.create()
        self.pause(journal)
        code = self.root / "scripts" / "lido" / "refresh.py"
        code.write_text(code.read_text() + "\n# offline code mutation\n")
        with patch("refresh.open_rpc") as wire, self.assertRaises(RuntimeError):
            self.reopen()
        wire.assert_not_called()

    def test_latest_checkout_rejects_changed_route_or_missing_review(self):
        journal = self.create()
        self.pause(journal)
        for changed in ({"route_id": "another-route"},
                        {"endpoint": "https://unapproved.invalid/rpc"},
                        {"fresh_main": False}, {"reviewed_same_route": ""}):
            with self.subTest(changed=changed), patch("refresh.open_rpc") as wire:
                with self.assertRaises(RuntimeError):
                    self.reopen(**changed)
                wire.assert_not_called()

    def test_unrelated_main_head_change_preserves_pinned_code_and_data(self):
        journal = self.create()
        self.pause(journal)
        spent = journal.calls
        journal = self.reopen(head="2" * 40)
        self.assertEqual(journal.calls, spent)
        result, _ = self.collect(journal, self.transport())
        self.assertTrue(result["validation"]["complete"])

    def test_true_source_denials_and_redirects_are_terminal_across_restart(self):
        for code in (401, 403, 302, 307):
            with self.subTest(code=code):
                self.directory = self.root / f"denial-{code}"
                self.seal_path = self.root / "controller" / f"denial-{code}.json"
                journal = self.create()
                kind = urllib.error.HTTPError if code in (401, 403) else refresh.RpcRedirectError
                error = kind(refresh.RPC_URL, code, "offline refusal", {}, None)
                transport = self.transport(error=error)
                with self.assertRaises(RuntimeError):
                    self.collect(journal, transport)
                self.assertEqual(len(transport.requests), 1)
                self.assertEqual(journal.calls, 1)
                self.assertEqual(journal.status, "terminal")
                journal.close()
                with patch("refresh.open_rpc") as wire, self.assertRaises(RuntimeError):
                    self.reopen()
                wire.assert_not_called()

    def test_unapproved_method_never_reaches_transport_or_spends_budget(self):
        journal = self.create()
        rpc = refresh.DurableRpc(journal, self.state, self.summary,
                                 admission_wait=advancing_wait(self.clock))
        with patch("refresh.open_rpc") as wire, self.assertRaises(RuntimeError):
            rpc("eth_call", [{"to": ex.ADDRESSES["core"], "data": "0x"}, "0x69"])
        wire.assert_not_called()
        self.assertEqual(journal.calls, 0)

    def test_resumed_anchor_mismatch_stops_before_retained_data_can_be_replayed(self):
        for changed_height, field, value, expected_calls in (
            (100, "timestamp", "0x3e9", 1),
            (105, "timestamp", "0x47d", 2),
            (106, "parentBeaconBlockRoot", h(999), 3),
        ):
            with self.subTest(field=field, height=changed_height):
                self.directory = self.root / f"anchor-{changed_height}"
                self.seal_path = self.root / "controller" / f"anchor-{changed_height}.json"
                journal = self.create()
                self.pause(journal)
                journal = self.reopen()
                fake = FakeRpc(self.logs)

                def respond(request):
                    result = fake(request["method"], request["params"])
                    if request["method"] == "eth_getBlockByNumber" and \
                            request["params"][0] == hex(changed_height):
                        result[field] = value
                    return result

                transport = Transport(respond)
                with self.assertRaises(RuntimeError):
                    self.collect(journal, transport)
                self.assertEqual(len(transport.requests), expected_calls)
                self.assertTrue(all(row["method"] == "eth_getBlockByNumber"
                                    for row in transport.requests))
                self.assertEqual(self.state, seed())
                journal.close()

    def test_budget_is_cumulative_across_resume_and_charged_before_transport(self):
        journal = self.create()
        first = self.pause(journal)
        spent = journal.calls
        journal = self.reopen()
        transport = self.transport()
        _, rpc = self.collect(journal, transport)
        self.assertEqual(journal.calls, spent + len(transport.requests))
        original_respond = transport.respond

        def respond(request):
            # The persisted counter is visible before the transport starts.
            self.assertEqual(journal.calls, request["id"])
            seal = json.loads(self.seal_path.read_text())
            self.assertEqual(seal["attempts"], request["id"])
            return original_respond(request)

        transport.respond = respond
        with patch("refresh.open_rpc", side_effect=transport):
            while journal.calls < refresh.MAX_RPC_CALLS - 3:
                rpc("eth_getBalance", [ex.ADDRESSES["core"], "0x69"])
            for number in (100, 105, 106):
                rpc("eth_getBlockByNumber", [hex(number), False])
            with self.assertRaises(RuntimeError):
                rpc("eth_getBlockByNumber", ["0x64", False])
        self.assertEqual(journal.calls, 256)
        requests = first.requests + transport.requests
        self.assertEqual(len(requests), 256)
        self.assertEqual(sorted(row["id"] for row in requests), list(range(1, 257)))

    def test_original_deadline_cannot_be_refreshed_by_reopening(self):
        journal = self.create()
        self.pause(journal)
        self.clock.advance(refresh.MAX_RUN_SECONDS + 1)
        with patch("refresh.open_rpc") as wire, self.assertRaises(RuntimeError):
            self.reopen()
        wire.assert_not_called()

    def test_request_scope_stays_pinned_after_authentication(self):
        invalid = [
            ("eth_getBalance", ["0x" + "0" * 40, "0x69"]),
            ("eth_getBalance", [ex.ADDRESSES["core"], "latest"]),
            ("eth_getBalance", [ex.ADDRESSES["core"], "0x68"]),
            ("eth_getLogs", [{"address": [ex.ADDRESSES[k] for k in ex.MONITORED],
                              "fromBlock": "0x64", "toBlock": "0x69"}]),
            ("eth_getLogs", [{"address": [ex.ADDRESSES[k] for k in ex.MONITORED],
                              "fromBlock": "0x65", "toBlock": "0x6a"}]),
            ("eth_getLogs", [{"address": [ex.ADDRESSES[k] for k in ex.MONITORED],
                              "fromBlock": "0x65", "toBlock": "0x69", "topics": []}]),
        ]
        for index, (method, params) in enumerate(invalid):
            with self.subTest(method=method, params=params), patch("refresh.open_rpc") as wire:
                self.directory = self.root / f"scope-{index}"
                self.seal_path = self.root / "controller" / f"scope-{index}.json"
                journal = self.create()
                _, rpc = self.collect(journal, self.transport())
                spent = journal.calls
                with self.assertRaises(RuntimeError):
                    rpc(method, params)
                wire.assert_not_called()
                self.assertEqual(journal.calls, spent)
                journal.close()

    def test_once_output_remains_local_and_cannot_pass_publication_validation(self):
        output = self.root / "local-result.json"
        snapshot = {**self.old, "asOf": refresh.utc(1148), "blockNumber": 105}
        args = ["refresh.py", "once", "--root", str(self.root), "--output", str(output),
                "--workdir", str(self.root / "local-work"), "--run-id", self.run_id]
        with patch.object(sys, "argv", args), patch("refresh.time.time", return_value=1200), \
                patch("refresh.collect", return_value=(snapshot, self.state)), \
                patch("refresh.signal.signal"), patch("refresh.signal.alarm"), patch("builtins.print"):
            self.assertEqual(refresh.main(), 0)
        result = json.loads(output.read_text())
        self.assertEqual(result["stage"], "complete")
        self.assertIs(result["localVerificationOnly"], True)
        with self.assertRaisesRegex(ValueError, "local verification"):
            validate_result(result, self.base, 1200)
        for name, content in self.base.items():
            self.assertEqual((self.root / name).read_bytes(), content)

    def cli(self, stage, transport):
        """Run the real CLI with only source inspection/model output stubbed."""
        output, workdir = self.root / f"{stage}-result.json", self.root / "cli-work"
        args = ["refresh.py", stage, "--root", str(self.root), "--output", str(output),
                "--workdir", str(workdir), "--run-id", self.run_id, "--head", self.head,
                "--seal-path", str(self.root / "controller" / "cli-seal.json")]
        if stage in ("resume", "abort"):
            args += ["--current-root", str(self.root), "--attest-current-main"]
        if stage == "resume":
            args += ["--reviewed-same-route", "offline explicit same-route review"]

        def collect(root, old, run_id, directory, *, journal):
            self.assertIsNotNone(journal)
            source = directory / "state.ssz"
            if journal.source is None:
                source.write_bytes(self.source_file.read_bytes())
                journal.pin_source(source, self.acquisition, self.summary)
            result, _ = self.collect(journal, transport)
            snapshot = {**old, "asOf": refresh.utc(result["state"]["timestamp"]),
                        "blockNumber": result["state"]["block"], "blockHash": result["state"]["hash"]}
            return snapshot, result["state"]

        with patch.object(sys, "argv", args), patch("refresh.time.time", side_effect=lambda: self.clock.wall), \
                patch("refresh.time.monotonic", side_effect=lambda: self.clock.monotonic), \
                patch("refresh.collect", side_effect=collect), \
                patch("beacon_hourly.inspect_state", return_value=deepcopy(self.summary)), \
                patch("refresh.signal.signal"), patch("refresh.signal.alarm"), patch("builtins.print"):
            code = refresh.main()
        return code, json.loads(output.read_text()), workdir

    def test_cli_pause_then_reviewed_resume_preserves_lease_and_finishes_same_run(self):
        first = self.transport(pause_balance=True)
        code, paused, workdir = self.cli("run", first)
        self.assertEqual(code, 2, paused.get("error"))
        self.assertEqual(paused["stage"], "paused")
        self.assertEqual(paused["files"], [])
        self.assertIs(paused["localVerificationOnly"], False)
        self.assertTrue(paused["recovery"]["sameRouteReviewRequired"])
        self.assertEqual(paused["recovery"]["calls"], len(first.requests))
        self.assertTrue((workdir / "state.ssz").is_file())
        with self.assertRaisesRegex(ValueError, "not a publishable final result"):
            validate_result(paused, self.base, int(self.clock.wall))
        for name, content in self.base.items():
            self.assertEqual((self.root / name).read_bytes(), content)
        self.clock.advance(30)
        resumed = self.transport()
        code, complete, same_workdir = self.cli("resume", resumed)
        self.assertEqual(code, 0)
        self.assertEqual(same_workdir, workdir)
        self.assertEqual(complete["stage"], "complete")
        self.assertEqual(complete["runId"], paused["runId"])
        self.assertIs(complete["localVerificationOnly"], False)
        self.assertEqual({row["path"] for row in complete["files"]}, set(self.base))
        self.assertEqual(len(resumed.requests), 10)
        self.assert_fresh_suffix(resumed.requests)
        self.assertFalse((workdir / "state.ssz").exists())
        self.assertEqual(sorted(row["id"] for row in first.requests + resumed.requests),
                         list(range(1, len(first.requests) + 11)))

    def test_trickling_body_rechecks_original_deadline_between_bounded_chunks(self):
        journal = self.create()
        rpc = refresh.DurableRpc(journal, self.state, self.summary,
                                 admission_wait=advancing_wait(self.clock))
        sizes = []

        class Response:
            def read1(inner, size):
                sizes.append(size)
                self.clock.advance(1501)
                return b'{'

            def read(inner, _size):
                raise AssertionError('whole-body read must not bypass deadline checks')

        with patch('refresh.time.monotonic', side_effect=lambda: self.clock.monotonic):
            with self.assertRaises(TimeoutError):
                rpc._read_response(Response())
        self.assertEqual(sizes, [64 * 1024])
        self.assertEqual(journal.calls, 0)

    def test_pause_between_header_check_and_cache_admission_stays_reviewable(self):
        from recovery import Busy
        journal = self.create()
        rpc = refresh.DurableRpc(journal, self.state, self.summary,
                                 admission_wait=advancing_wait(self.clock))
        with patch('refresh.open_rpc', side_effect=self.transport()):
            for block in ('0x64', '0x69', '0x6a', 'finalized'):
                rpc('eth_getBlockByNumber', [block, False])
        cached_window, paused, release_tunnel = threading.Event(), threading.Event(), threading.Event()
        transport_entered = threading.Event()
        original_cached, original_fail = journal.cached, journal.fail
        original_header = rpc._event_header

        def event_header(number):
            # Let the first worker pass its final pre-transport lock check
            # before the second deliberately holds that lock in cached().
            if number == 102:
                self.assertTrue(transport_entered.wait(5))
            return original_header(number)

        def cached(role, method, params, validator):
            # Only the actual worker, after its _check; not the cache pre-scan.
            if getattr(rpc._role, 'value', None) == 'event_header' and params[0] == '0x66':
                cached_window.set()
                self.assertTrue(paused.wait(5))
            return original_cached(role, method, params, validator)

        def fail(ident, category, **kwargs):
            original_fail(ident, category=category, **kwargs)
            if category == 'unknown_tunnel_rejection':
                paused.set()
                self.assertTrue(release_tunnel.wait(5))

        def respond(request):
            self.assertEqual(request['params'][0], '0x65')
            transport_entered.set()
            self.assertTrue(cached_window.wait(5))
            raise urllib.error.URLError(OSError('Tunnel connection failed: fixture interruption'))

        transport = Transport(respond)
        with patch.object(journal, 'cached', side_effect=cached), \
                patch.object(journal, 'fail', side_effect=fail), \
                patch.object(rpc, '_event_header', side_effect=event_header), \
                patch('refresh.open_rpc', side_effect=transport):
            try:
                with self.assertRaises(refresh.RecoveryPaused):
                    rpc.fetch_headers([101, 102])
                self.assertEqual(journal.status, 'paused')
                with self.assertRaises(Busy):
                    journal.close()
            finally:
                release_tunnel.set()
                rpc.drain()
        self.assertEqual(journal.status, 'paused')
        self.assertEqual(journal.calls, 5)
        self.assertEqual(len(transport.requests), 1)

    def test_slow_completion_seal_cannot_emit_success_after_original_deadline(self):
        complete = RunJournal.complete

        def slow_complete(journal):
            complete(journal)
            self.clock.advance(1501)

        with patch.object(RunJournal, 'complete', slow_complete):
            code, result, workdir = self.cli('run', self.transport())
        self.assertEqual((code, result['stage']), (1, 'failed'))
        self.assertIn('deadline while sealing completion', result['error'])
        self.assertEqual([row['path'] for row in result['files']], [refresh.SNAPSHOT_PATH])
        failure = json.loads(result['files'][0]['content'])
        self.assertEqual(failure['asOf'], self.old['asOf'])
        self.assertEqual(failure['tiers'], self.old['tiers'])
        self.assertFalse((workdir / 'state.ssz').exists())
        validate_result(result, self.base, int(self.clock.wall))

    def test_deadline_signal_during_drain_keeps_ownership_until_callback_exits(self):
        from recovery import Busy
        journal = self.create()
        rpc = refresh.DurableRpc(journal, self.state, self.summary,
                                 admission_wait=advancing_wait(self.clock))
        ident, _ = journal.reserve('anchor', 'eth_getBlockByNumber', ['0x64', False])
        journal.start_callback(ident)
        rpc._callbacks.add(ident)
        waits = []

        def wait_for_teardown(timeout):
            waits.append(timeout)
            if len(waits) == 1:
                self.clock.advance(1501)
                raise TimeoutError('original deadline expired during teardown')
            self.assertEqual(journal.status, 'terminal')
            with self.assertRaises(Busy):
                journal.close()
            journal.finish_callback(ident)
            rpc._callbacks.remove(ident)

        with patch.object(rpc._quiescent, 'wait', side_effect=wait_for_teardown), \
                patch('refresh.time.monotonic', side_effect=lambda: self.clock.monotonic):
            rpc.drain()
        self.assertEqual(len(waits), 2)
        self.assertEqual(journal.status, 'terminal')
        self.assertEqual(journal.calls, 1)  # An unknown in-flight attempt stays charged.
        journal.close()

    def test_cli_abort_paused_run_emits_only_recoverable_snapshot_status(self):
        code, paused, workdir = self.cli("run", self.transport(pause_balance=True))
        self.assertEqual((code, paused["stage"]), (2, "paused"), paused.get("error"))
        self.clock.advance(30)
        transport = self.transport()
        code, aborted, _ = self.cli("abort", transport)
        self.assertEqual(code, 1)
        self.assertEqual(aborted["stage"], "failed")
        self.assertEqual(transport.requests, [])
        self.assertEqual([row["path"] for row in aborted["files"]], [refresh.SNAPSHOT_PATH])
        failed = json.loads(aborted["files"][0]["content"])
        self.assertEqual({key: value for key, value in failed.items() if key != "refresh"},
                         {key: value for key, value in self.old.items() if key != "refresh"})
        self.assertIsNone(failed["refresh"]["lease"])
        self.assertFalse(failed["refresh"]["accessBlocked"])
        self.assertEqual(len(validate_result(aborted, self.base, int(self.clock.wall))), 1)
        self.assertFalse((workdir / "state.ssz").exists())
        for name, content in self.base.items():
            self.assertEqual((self.root / name).read_bytes(), content)

    def test_cli_resume_requires_explicit_fresh_main_and_route_review(self):
        output = self.root / "unreviewed-result.json"
        args = ["refresh.py", "resume", "--root", str(self.root), "--output", str(output),
                "--workdir", str(self.root / "cli-work"), "--run-id", self.run_id,
                "--head", self.head, "--current-root", str(self.root), "--attest-current-main"]
        with patch.object(sys, "argv", args), patch("refresh.open_rpc") as wire, \
                patch("sys.stderr"), self.assertRaises(SystemExit) as failure:
            refresh.main()
        self.assertEqual(failure.exception.code, 2)
        wire.assert_not_called()
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
