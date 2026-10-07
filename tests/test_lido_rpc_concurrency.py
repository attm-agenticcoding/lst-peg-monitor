"""Offline transport, scheduling, and rollback checks for bounded RPC replay.

All responses come from memory. Events/barriers control overlap so these tests
do not depend on a public provider, scheduling sleeps, or wall-clock deadlines.
"""
from concurrent.futures import ALL_COMPLETED, ThreadPoolExecutor, wait as futures_wait
from copy import deepcopy
from email.message import Message
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lido"))
import execution as ex
import refresh
from publication import validate_result
from test_execution_hourly import FakeRpc, event, h, seed
from rpc_clock import RpcClock, advancing_wait


def header(number):
    return {"number": hex(number), "hash": h(number),
            "timestamp": hex(1000 + number * 12), "parentHash": h(number - 1)}


class Transport:
    """Capture real outgoing JSON and serve a controllable response in memory."""

    def __init__(self, respond=None):
        self.respond = respond or (lambda req: header(int(req["params"][0], 16)))
        self.requests, self.raw, self.urls, self.timeouts = [], {}, [], []
        self.active = self.maximum = 0
        self.condition = threading.Condition()

    def __call__(self, request, *, timeout):
        payload = json.loads(request.data)
        with self.condition:
            self.requests.append(payload)
            self.urls.append(request.full_url)
            self.timeouts.append(timeout)
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            self.condition.notify_all()
        try:
            value = self.respond(payload)
            # Explicit envelopes let callers inject errors or a mismatched ID.
            envelope = value if isinstance(value, Envelope) else {
                "jsonrpc": "2.0", "id": payload["id"], "result": value}
            raw = json.dumps(envelope).encode()
            with self.condition:
                self.raw[payload["id"]] = raw
            return io.BytesIO(raw)
        finally:
            with self.condition:
                self.active -= 1
                self.condition.notify_all()

    def wait_for_requests(self, count):
        with self.condition:
            if not self.condition.wait_for(lambda: len(self.requests) >= count, timeout=5):
                raise AssertionError(f"Expected {count} admitted requests; got {len(self.requests)}")

    def wait_until_idle(self):
        with self.condition:
            if not self.condition.wait_for(lambda: self.active == 0, timeout=5):
                raise AssertionError("Mock responses did not finish")


class Envelope(dict):
    pass


class RpcConcurrencyTests(unittest.TestCase):
    def rpc(self):
        clock = RpcClock()
        return refresh.Rpc(clock() + 1500, clock=clock, admission_wait=advancing_wait(clock))

    def assert_evidence(self, rpc, transport):
        evidence = rpc.evidence
        request_ids = [r["id"] for r in transport.requests]
        ids = [row["request"]["id"] for row in evidence]
        self.assertEqual(ids, sorted(request_ids))
        self.assertEqual(len(set(ids)), len(request_ids))
        requests = {row["id"]: row for row in transport.requests}
        for row in evidence:
            identifier = row["request"]["id"]
            self.assertEqual(row["request"], requests[identifier])
            self.assertEqual(row["responseSha256"], hashlib.sha256(transport.raw[identifier]).hexdigest())
        self.assertEqual(set(transport.urls), {refresh.RPC_URL})
        self.assertTrue(all(0 < timeout <= 40 for timeout in transport.timeouts))

    def test_concurrent_calls_validate_their_own_id_and_sort_complete_evidence(self):
        rpc = self.rpc()
        release = {number: threading.Event() for number in range(101, 105)}

        def respond(req):
            number = int(req["params"][0], 16)
            if not release[number].wait(5):
                raise AssertionError("Response gate timed out")
            return header(number)

        transport = Transport(respond)
        with patch("refresh.open_rpc", side_effect=transport), ThreadPoolExecutor(max_workers=4) as pool:
            futures = {number: pool.submit(rpc, "eth_getBlockByNumber", [hex(number), False])
                       for number in release}
            try:
                transport.wait_for_requests(4)
                # The last request ID is already allocated when earlier replies
                # arrive. Comparing a response to the global counter is wrong.
                for number in (103, 101, 104, 102):
                    release[number].set()
                    self.assertEqual(futures[number].result(timeout=5), header(number))
            finally:
                for gate in release.values():
                    gate.set()
        self.assertEqual(rpc.calls, 4)
        self.assert_evidence(rpc, transport)

    def test_fetch_headers_deduplicates_and_never_exceeds_four_active_calls(self):
        rpc, release = self.rpc(), threading.Event()

        def respond(req):
            if not release.wait(5):
                raise AssertionError("Header gate timed out")
            return header(int(req["params"][0], 16))

        transport = Transport(respond)
        numbers = list(range(101, 114)) + [102, 101, 113]
        with patch("refresh.open_rpc", side_effect=transport), ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(rpc.fetch_headers, numbers)
            try:
                transport.wait_for_requests(4)
                with transport.condition:
                    self.assertEqual(len(transport.requests), 4)
                    self.assertEqual(transport.active, 4)
            finally:
                release.set()
            headers = result.result(timeout=5)
        self.assertEqual(headers, {number: header(number) for number in set(numbers)})
        self.assertTrue(all(type(number) is int for number in headers))
        self.assertEqual(len(transport.requests), 13)
        self.assertEqual(rpc.calls, 13)
        self.assertLessEqual(transport.maximum, 4)
        self.assertTrue(all(req["method"] == "eth_getBlockByNumber" and req["params"][1] is False
                            for req in transport.requests))
        self.assert_evidence(rpc, transport)

    def test_global_budget_is_atomic_under_competing_admissions(self):
        rpc = self.rpc()
        transport = Transport(lambda req: "0x1")
        start = threading.Barrier(17)

        def compete():
            start.wait(timeout=5)
            try:
                return rpc("eth_getBalance", [ex.ADDRESSES["core"], "0x65"])
            except RuntimeError:
                return None

        with patch("refresh.open_rpc", side_effect=transport):
            for _ in range(252):
                rpc("eth_getBalance", [ex.ADDRESSES["core"], "0x65"])
            with ThreadPoolExecutor(max_workers=16) as pool:
                futures = [pool.submit(compete) for _ in range(16)]
                start.wait(timeout=5)
                outcomes = [future.result(timeout=5) for future in futures]
            self.assertGreaterEqual(outcomes.count(None), 12)
            self.assertEqual(len(transport.requests), 256)
            self.assertEqual(rpc.calls, 256)
            self.assertEqual(sorted(req["id"] for req in transport.requests), list(range(1, 257)))
            with self.assertRaises(RuntimeError):
                rpc("eth_getBalance", [ex.ADDRESSES["core"], "0x65"])
            self.assertEqual(len(transport.requests), 256)

    def test_header_preflight_preserves_six_balance_and_recheck_calls(self):
        rpc = self.rpc()
        transport = Transport(lambda req: "0x1" if req["method"] == "eth_getBalance"
                              else header(int(req["params"][0], 16)))
        with patch("refresh.open_rpc", side_effect=transport):
            for _ in range(248):
                rpc("eth_getBalance", [ex.ADDRESSES["core"], "0x65"])
            self.assertEqual(rpc.fetch_headers([102, 101, 102]), {101: header(101), 102: header(102)})
            for _ in range(3):
                rpc("eth_getBalance", [ex.ADDRESSES["core"], "0x65"])
            for number in (100, 101, 102):
                self.assertEqual(rpc("eth_getBlockByNumber", [hex(number), False]), header(number))
        self.assertEqual(rpc.calls, 256)
        self.assert_evidence(rpc, transport)

    def test_oversized_header_batch_fails_before_any_partial_admission(self):
        rpc, transport = self.rpc(), Transport(lambda req: "0x1")
        with patch("refresh.open_rpc", side_effect=transport):
            for _ in range(249):
                rpc("eth_getBalance", [ex.ADDRESSES["core"], "0x65"])
            before = len(transport.requests)
            with self.assertRaises(RuntimeError):
                rpc.fetch_headers([101, 102])
            self.assertEqual(len(transport.requests), before)
            self.assertEqual(rpc.calls, 249)

    def test_denial_stops_queued_headers_and_all_later_calls(self):
        for kind in ("http401", "http403", "rpc401", "rpc403"):
            with self.subTest(kind=kind):
                rpc = self.rpc()
                peers = threading.Barrier(4)
                denied_returned = threading.Event()
                release_peers = threading.Event()

                def respond(req):
                    peers.wait(timeout=5)
                    if int(req["params"][0], 16) == 101:
                        denied_returned.set()
                        code = int(kind[-3:])
                        if kind.startswith("http"):
                            raise urllib.error.HTTPError(refresh.RPC_URL, code, "denied", {}, None)
                        return Envelope(jsonrpc="2.0", id=req["id"], error={"code": code, "message": "denied"})
                    if not release_peers.wait(5):
                        raise AssertionError("Peer release timed out")
                    return header(int(req["params"][0], 16))

                transport = Transport(respond)
                with patch("refresh.open_rpc", side_effect=transport), ThreadPoolExecutor(max_workers=1) as pool:
                    result = pool.submit(rpc.fetch_headers, range(101, 121))
                    try:
                        self.assertTrue(denied_returned.wait(5))
                        with self.assertRaises(ex.RpcError) as captured:
                            result.result(timeout=5)
                        self.assertTrue(captured.exception.denied)
                    finally:
                        release_peers.set()
                        transport.wait_until_idle()
                    self.assertEqual(len(transport.requests), 4)
                    with self.assertRaises(RuntimeError):
                        rpc("eth_getBalance", [ex.ADDRESSES["core"], "0x65"])
                    self.assertEqual(len(transport.requests), 4)

    def test_explicit_cancellation_rejects_late_results_and_new_work(self):
        rpc, release = self.rpc(), threading.Event()

        def respond(req):
            if not release.wait(5):
                raise AssertionError("Cancellation gate timed out")
            return header(int(req["params"][0], 16))

        transport = Transport(respond)
        with patch("refresh.open_rpc", side_effect=transport), ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(rpc.fetch_headers, range(101, 121))
            try:
                transport.wait_for_requests(4)
                rpc.cancel()
            finally:
                release.set()
                transport.wait_until_idle()
            with self.assertRaises(RuntimeError):
                result.result(timeout=5)
            self.assertEqual(len(transport.requests), 4)
            with self.assertRaises(RuntimeError):
                rpc.fetch_headers([200])
            self.assertEqual(len(transport.requests), 4)

    def test_first_denial_survives_later_server_error_consumed_first(self):
        rpc = self.rpc()
        wait_entered, first_denial = threading.Event(), threading.Event()
        peers = threading.Barrier(4)
        real_cancel = rpc.cancel
        consumed = []

        def cancel(error=None):
            first_error = real_cancel(error)
            if getattr(error, "code", None) == 403:
                first_denial.set()
            return first_error

        def respond(req):
            number = int(req["params"][0], 16)
            peers.wait(timeout=5)
            if number == 101:
                if not wait_entered.wait(5):
                    raise AssertionError("Batch never entered its completion wait")
                raise urllib.error.HTTPError(refresh.RPC_URL, 403, "denied", {}, None)
            if not first_denial.wait(5):
                raise AssertionError("First denial was not recorded")
            if number == 102:
                raise urllib.error.HTTPError(refresh.RPC_URL, 500, "later server error", {}, None)
            return header(number)

        def later_failure_first(pending, **kwargs):
            wait_entered.set()
            done, unfinished = futures_wait(pending, timeout=5, return_when=ALL_COMPLETED)
            if unfinished:
                raise AssertionError("Controlled in-flight responses did not complete")
            ordered = sorted(done, key=lambda future: (pending[future] != 102, pending[future]))
            consumed.extend(pending[future] for future in ordered)
            return ordered, set()

        transport = Transport(respond)
        with patch("refresh.open_rpc", side_effect=transport), \
                patch.object(rpc, "cancel", side_effect=cancel), \
                patch("refresh.wait", side_effect=later_failure_first):
            with self.assertRaises(ex.RpcError) as captured:
                rpc.fetch_headers(range(101, 121))
            self.assertEqual(captured.exception.code, 403)
            self.assertTrue(captured.exception.denied)
            self.assertEqual(consumed[0], 102)
            with self.assertRaises(ex.RpcError) as again:
                rpc("eth_getBalance", [ex.ADDRESSES["core"], "0x65"])
            self.assertEqual(again.exception.code, 403)
            self.assertTrue(again.exception.denied)
        self.assertEqual(len(transport.requests), 4)
        failures = rpc.failures
        self.assertEqual([row["request"]["id"] for row in failures],
                         sorted(row["request"]["id"] for row in failures))
        failures_by_height = {int(row["request"]["params"][0], 16): row for row in failures}
        self.assertEqual(failures_by_height[101]["code"], 403)
        self.assertTrue(failures_by_height[101]["denied"])
        self.assertEqual(failures_by_height[102]["code"], 500)
        self.assertFalse(failures_by_height[102]["denied"])

    def test_expired_deadline_never_opens_the_transport(self):
        clock = RpcClock(100)
        rpc = refresh.Rpc(99, clock=clock, admission_wait=advancing_wait(clock))
        with patch("refresh.open_rpc") as open_rpc:
            with self.assertRaises(TimeoutError):
                rpc.fetch_headers([101])
            open_rpc.assert_not_called()
        self.assertEqual(rpc.calls, 0)

    def test_response_arriving_after_deadline_is_rejected(self):
        clock = RpcClock(100)
        rpc = refresh.Rpc(110, clock=clock, admission_wait=advancing_wait(clock))

        def respond(req):
            clock.advance(11)
            return header(101)

        transport = Transport(respond)
        with patch("refresh.open_rpc", side_effect=transport):
            with self.assertRaises(TimeoutError):
                rpc("eth_getBlockByNumber", ["0x65", False])
            with self.assertRaises(TimeoutError):
                rpc("eth_getBlockByNumber", ["0x66", False])
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(transport.timeouts, [10])

    def test_malformed_or_missing_headers_fail_without_retry(self):
        valid = header(101)
        bad_headers = (None, [], {}, {**valid, "number": "0x66"},
                       {**valid, "hash": "0xshort"}, {**valid, "timestamp": None},
                       {**valid, "timestamp": True}, {**valid, "timestamp": -1})
        for value in bad_headers:
            with self.subTest(value=value):
                rpc, transport = self.rpc(), Transport(lambda req, value=value: value)
                with patch("refresh.open_rpc", side_effect=transport):
                    with self.assertRaises(RuntimeError):
                        rpc.fetch_headers([101])
                    with self.assertRaises(RuntimeError):
                        rpc.fetch_headers([102])
                self.assertEqual(len(transport.requests), 1)

    def test_foreign_response_id_cancels_future_admissions(self):
        rpc = self.rpc()
        transport = Transport(lambda req: Envelope(jsonrpc="2.0", id=req["id"] + 1, result=header(101)))
        with patch("refresh.open_rpc", side_effect=transport):
            with self.assertRaisesRegex(ex.RpcError, "envelope"):
                rpc.fetch_headers([101])
            with self.assertRaises(RuntimeError):
                rpc.fetch_headers([102])
        self.assertEqual(len(transport.requests), 1)

    def test_log_limit_can_split_on_same_provider_without_poisoning_rpc(self):
        rpc = self.rpc()

        def respond(req):
            query = req["params"][0]
            if query["fromBlock"] != query["toBlock"]:
                return Envelope(jsonrpc="2.0", id=req["id"], error={"code": -32005, "message": "limit"})
            return []

        transport = Transport(respond)
        with patch("refresh.open_rpc", side_effect=transport):
            logs, coverage = ex.fetch_logs(rpc, 101, 102)
        self.assertEqual(logs, [])
        self.assertEqual([(row["from_block"], row["to_block"]) for row in coverage], [(101, 101), (102, 102)])
        self.assertEqual(len(transport.requests), 3)
        self.assertEqual(set(transport.urls), {refresh.RPC_URL})
        self.assertEqual([req["method"] for req in transport.requests], ["eth_getLogs"] * 3)

    def test_header_limit_error_is_terminal_and_never_split_or_retried(self):
        rpc = self.rpc()
        transport = Transport(lambda req: Envelope(jsonrpc="2.0", id=req["id"],
                                                   error={"code": -32005, "message": "limit"}))
        with patch("refresh.open_rpc", side_effect=transport):
            with self.assertRaises(ex.RpcError) as captured:
                rpc.fetch_headers([101])
            self.assertEqual(captured.exception.code, -32005)
            with self.assertRaises(RuntimeError):
                rpc.fetch_headers([102])
        self.assertEqual(len(transport.requests), 1)

    def test_redirects_are_denied_before_following_another_url(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                response = io.BytesIO(b"")
                response.code = response.status = status
                response.msg = "Redirect"
                headers = Message()
                headers["Location"] = "https://unapproved.invalid/rpc"
                response.info = lambda: headers
                response.geturl = lambda: refresh.RPC_URL
                rpc = self.rpc()
                # Exercise the real urllib opener and redirect policy, replacing
                # only its socket-bearing HTTP/HTTPS operation.
                with patch("urllib.request.AbstractHTTPHandler.do_open", return_value=response) as wire:
                    with self.assertRaises(ex.RpcError) as captured:
                        rpc("eth_getBlockByNumber", ["0x65", False])
                    self.assertTrue(captured.exception.denied)
                    self.assertEqual(wire.call_count, 1)
                    with self.assertRaises(RuntimeError):
                        rpc("eth_getBlockByNumber", ["0x66", False])
                    self.assertEqual(wire.call_count, 1)

    def test_bounded_diagnostics_distinguish_denial_from_cancelled_peers(self):
        rpc = self.rpc()
        release_denial, release_peers = threading.Event(), threading.Event()

        def respond(req):
            if req["method"] == "eth_getBalance":
                return "0x1"
            number = int(req["params"][0], 16)
            gate = release_denial if number == 101 else release_peers
            if not gate.wait(5):
                raise AssertionError("Diagnostic response gate timed out")
            if number == 101:
                raise urllib.error.HTTPError(refresh.RPC_URL, 403, "denied", {}, None)
            return header(number)

        transport = Transport(respond)
        with patch("refresh.open_rpc", side_effect=transport):
            for _ in range(252):
                rpc("eth_getBalance", [ex.ADDRESSES["core"], "0x65"])
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = {number: pool.submit(rpc, "eth_getBlockByNumber", [hex(number), False])
                           for number in range(101, 105)}
                try:
                    transport.wait_for_requests(256)
                    pending = rpc.diagnostics()
                    self.assertEqual(pending["source"], refresh.RPC_URL)
                    self.assertIsInstance(refresh.second(pending["capturedAt"]), int)
                    self.assertEqual(pending["calls"], 256)
                    self.assertEqual(len(pending["requests"]), 256)
                    self.assertEqual(len(pending["responses"]), 252)
                    self.assertEqual(pending["failures"], [])
                    self.assertEqual(pending["inFlightIds"], [253, 254, 255, 256])
                    # A caller may annotate its diagnostic snapshot without
                    # changing the collector's retained request evidence.
                    pending["requests"][0]["params"][0] = "changed by audit consumer"
                    self.assertEqual(rpc.diagnostics()["requests"][0]["params"][0], ex.ADDRESSES["core"])
                    release_denial.set()
                    with self.assertRaises(ex.RpcError) as denied:
                        futures[101].result(timeout=5)
                    self.assertTrue(denied.exception.denied)
                    after_denial = rpc.diagnostics()
                    self.assertEqual(len(after_denial["failures"]), 1)
                    self.assertEqual(len(after_denial["inFlightIds"]), 3)
                finally:
                    release_denial.set()
                    release_peers.set()
                for number in (102, 103, 104):
                    with self.assertRaises(ex.RpcError):
                        futures[number].result(timeout=5)
            with self.assertRaises(ex.RpcError):
                rpc("eth_getBalance", [ex.ADDRESSES["core"], "0x65"])
        diagnostic = rpc.diagnostics()
        self.assertEqual(diagnostic["calls"], 256)
        self.assertEqual(len(diagnostic["requests"]), 256)
        self.assertEqual(len(diagnostic["responses"]), 252)
        self.assertEqual(len(diagnostic["failures"]), 4)
        self.assertEqual(diagnostic["inFlightIds"], [])
        completed = diagnostic["responses"] + diagnostic["failures"]
        self.assertEqual(sorted(row["request"]["id"] for row in completed), list(range(1, 257)))
        self.assertEqual([req["id"] for req in diagnostic["requests"]], list(range(1, 257)))
        self.assertEqual([row["request"]["id"] for row in diagnostic["failures"]], [253, 254, 255, 256])
        failures = {int(row["request"]["params"][0], 16): row for row in diagnostic["failures"]}
        self.assertEqual(failures[101]["kind"], "source")
        self.assertEqual(failures[101]["code"], 403)
        self.assertTrue(failures[101]["denied"])
        for number in (102, 103, 104):
            self.assertEqual(failures[number]["kind"], "cancelled")
            self.assertIsNone(failures[number]["code"])
            self.assertFalse(failures[number]["denied"])
            self.assertLessEqual(len(failures[number]["error"]), 500)
        self.assertEqual(len(transport.requests), 256)


class HeaderBatchIntegrationTests(unittest.TestCase):
    def logs(self):
        return [event("WithdrawalRequested", (25, 20), (2, 1, 1), source="queue", block=101, index=0),
                event("WithdrawalRequested", (25, 20), (3, 1, 1), source="queue", block=101, tx=1, index=1),
                event("Resumed", source="queue", block=102, timestamp=1112),
                event("Transfer", (1,), (1, 2), block=103, timestamp=1124),
                event("Resumed", source="queue", block=105, timestamp=1148)]

    def test_collect_batches_only_unique_timestamp_heights_and_matches_fallback(self):
        class BatchRpc(FakeRpc):
            def __init__(self, logs):
                super().__init__(logs)
                self.batches = []

            def fetch_headers(self, numbers):
                self.batches.append(list(numbers))
                return {number: ex._header(self, number) for number in numbers}

        original = seed()
        rpc = BatchRpc(self.logs())
        batched = ex.collect_at(original, 105, h(105), rpc)
        sequential = ex.collect_at(original, 105, h(105), FakeRpc(self.logs()))
        self.assertEqual(len(rpc.batches), 1)
        self.assertEqual(sorted(rpc.batches[0]), [101, 102])
        self.assertEqual(original, seed())
        # Elapsed timings can legitimately differ between two runs.
        for result in (batched, sequential):
            result["validation"]["finality_wait"].pop("wait_seconds")
        self.assertEqual(batched, sequential)

    def test_real_rpc_header_failure_rolls_back_checkpoint_and_run_manifest(self):
        original = seed()
        before = deepcopy(original)
        fake = FakeRpc(self.logs())

        def respond(req):
            if req["method"] == "eth_getBlockByNumber" and req["params"][0] == "0x66":
                raise urllib.error.HTTPError(refresh.RPC_URL, 403, "denied", {}, None)
            return fake(req["method"], req["params"])

        transport = Transport(respond)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "data").mkdir()
            old = json.loads((ROOT / "tests/fixtures/lido-cutoff-20261004.json").read_text())
            now = int(time.time())
            leased, reason = refresh.propose_lease(old, now, "concurrency-rollback",
                                                 trigger="manual", manual_request_id="offline-concurrency-test")
            self.assertIsNone(reason)
            snapshot_bytes = refresh.canonical(leased).encode()
            state_bytes = refresh.canonical(original).encode()
            (root / refresh.SNAPSHOT_PATH).write_bytes(snapshot_bytes)
            (root / refresh.STATE_PATH).write_bytes(state_bytes)
            output, workdir = root / "result.json", root / "work"

            def collect(*args, **kwargs):
                clock = RpcClock()
                rpc = refresh.Rpc(clock() + 1500, clock=clock, admission_wait=advancing_wait(clock))
                return ex.collect_at(original, 105, h(105), rpc)

            args = ["refresh.py", "run", "--root", str(root), "--output", str(output),
                    "--workdir", str(workdir), "--run-id", "concurrency-rollback"]
            with patch.object(sys, "argv", args), patch("refresh.open_rpc", side_effect=transport), \
                    patch("refresh.collect", side_effect=collect), patch("refresh.signal.signal"), \
                    patch("refresh.signal.alarm"), patch("builtins.print"):
                self.assertEqual(refresh.main(), 1)
            manifest = json.loads(output.read_text())
            self.assertEqual(original, before)
            self.assertEqual((root / refresh.STATE_PATH).read_bytes(), state_bytes)
            self.assertEqual((root / refresh.SNAPSHOT_PATH).read_bytes(), snapshot_bytes)
            self.assertEqual(manifest["stage"], "failed")
            self.assertEqual([row["path"] for row in manifest["files"]], [refresh.SNAPSHOT_PATH])
            failed = json.loads(manifest["files"][0]["content"])
            self.assertTrue(failed["refresh"]["accessBlocked"])
            self.assertEqual({k: v for k, v in failed.items() if k != "refresh"},
                             {k: v for k, v in leased.items() if k != "refresh"})
            changes = validate_result(manifest, {refresh.SNAPSHOT_PATH: snapshot_bytes,
                                                 refresh.STATE_PATH: state_bytes}, now)
            self.assertEqual([row["path"] for row in changes], [refresh.SNAPSHOT_PATH])
            self.assertFalse((workdir / "audit.json").exists())
            self.assertFalse(any(req["method"] == "eth_getBalance" for req in transport.requests))

    def test_collect_persists_rpc_audit_on_execution_success_or_denial(self):
        old = json.loads((ROOT / "tests/fixtures/lido-cutoff-20261004.json").read_text())
        timestamp = refresh.second(old["asOf"]) + 60
        query = [{"address": [ex.ADDRESSES["queue"]], "fromBlock": "0x65", "toBlock": "0x69"}]
        summary = {"timestamp": timestamp, "block_number": 105, "block_hash": h(105)}
        original = seed()
        expected_request = {"jsonrpc": "2.0", "id": 1, "method": "eth_getLogs", "params": query}

        for denied in (False, True):
            with self.subTest(denied=denied), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "data").mkdir()
                workdir = root / "work"
                workdir.mkdir()
                input_bytes = {refresh.SNAPSHOT_PATH: refresh.canonical(old).encode(),
                               refresh.STATE_PATH: refresh.canonical(original).encode()}
                for path, data in input_bytes.items():
                    (root / path).write_bytes(data)

                def respond(req):
                    if denied:
                        raise urllib.error.HTTPError(refresh.RPC_URL, 403, "source denied", {}, None)
                    return []

                def collect_at(state, block, block_hash, rpc):
                    self.assertEqual(state, original)
                    self.assertEqual((block, block_hash), (105, h(105)))
                    rpc("eth_getLogs", query)
                    return {"validation": {"complete": True}, "state": {**state, "timestamp": timestamp},
                            "report_timing": {}, "snapshot": {}, "execution_anchor": {}}

                transport = Transport(respond)
                output = {"asOf": refresh.utc(timestamp), "blockNumber": 105}
                with patch("beacon_hourly.download_state", return_value={"bytes": 0}), \
                        patch("beacon_hourly.inspect_state", return_value=summary), \
                        patch("execution.collect_at", side_effect=collect_at), \
                        patch("beacon_hourly.simulate_reports", return_value={}), \
                        patch("refresh.report_references", return_value=[]), \
                        patch("refresh.build_scenarios", return_value={}), \
                        patch("refresh.public_snapshot", return_value=output), \
                        patch("refresh.open_rpc", side_effect=transport), patch("builtins.print"):
                    if denied:
                        with self.assertRaises(ex.RpcError) as captured:
                            refresh.collect(root, old, "diagnostic-test", workdir, now=timestamp + 1)
                        self.assertEqual(captured.exception.code, 403)
                        self.assertTrue(captured.exception.denied)
                    else:
                        result, state = refresh.collect(root, old, "diagnostic-test", workdir, now=timestamp + 1)
                        self.assertEqual(result, output)
                        self.assertEqual(state["timestamp"], timestamp)
                diagnostic = json.loads((workdir / "rpc-audit.json").read_text())
                self.assertEqual(diagnostic["source"], refresh.RPC_URL)
                self.assertEqual(diagnostic["calls"], 1)
                self.assertEqual(diagnostic["requests"], [expected_request])
                self.assertEqual(diagnostic["inFlightIds"], [])
                self.assertEqual(len(transport.requests), 1)
                if denied:
                    self.assertEqual(diagnostic["responses"], [])
                    self.assertEqual(len(diagnostic["failures"]), 1)
                    failure = diagnostic["failures"][0]
                    self.assertEqual(failure["request"], expected_request)
                    self.assertEqual(failure["kind"], "source")
                    self.assertEqual(failure["code"], 403)
                    self.assertTrue(failure["denied"])
                    self.assertIn("403", failure["error"])
                    self.assertFalse((workdir / "audit.json").exists())
                else:
                    self.assertEqual(diagnostic["failures"], [])
                    self.assertEqual(len(diagnostic["responses"]), 1)
                    self.assertEqual(diagnostic["responses"][0]["request"], expected_request)
                    self.assertEqual(diagnostic["responses"][0]["responseSha256"],
                                     hashlib.sha256(transport.raw[1]).hexdigest())
                    self.assertTrue((workdir / "audit.json").is_file())
                for path, data in input_bytes.items():
                    self.assertEqual((root / path).read_bytes(), data)
                self.assertEqual(old, json.loads(input_bytes[refresh.SNAPSHOT_PATH]))


if __name__ == "__main__":
    unittest.main()
