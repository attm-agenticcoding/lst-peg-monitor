"""Offline adversarial tests for block-pinned Lido execution replay."""
import copy
import json
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "lido"))
import execution as ex


def h(value):
    return "0x" + format(value, "064x")


def event(name, data=(), indexed=(), *, source="core", block=101, tx=0, index=0, timestamp=1100):
    return {"address": ex.ADDRESSES[source], "topics": [ex.TOPICS[name]] + [h(x) for x in indexed],
            "data": "0x" + "".join(format(x, "064x") for x in data), "blockNumber": hex(block),
            "blockHash": h(block), "blockTimestamp": hex(timestamp), "transactionIndex": hex(tx),
            "transactionHash": h(block * 1000 + tx), "logIndex": hex(index), "removed": False}


def seed():
    return {"schema": 1, "block": 100, "hash": h(100), "timestamp": 1000,
            "pending_requests": [{"request_id": 1, "amount_steth_wei": "100", "amount_shares_wei": "80",
                                  "timestamp": 900, "report_timestamp": 800}],
            "last_request_id": 1, "last_finalized_id": 0, "last_report_timestamp": 800,
            "core_buffer_wei": "500", "cl_balance_wei": "500", "deposited_post_report_wei": "0",
            "total_shares_wei": "800", "external_shares_wei": "0", "reserve_stored_wei": "100",
            "reserve_target_wei": "100", "queue_resume_since": 0, "queue_bunker_since": ex.UINT256_MAX,
            "config": {"genesis_time": 0, "seconds_per_slot": 12, "slots_per_epoch": 32,
                       "epochs_per_frame": 2, "initial_epoch": 0, "fast_lane_length_slots": 1,
                       "request_timestamp_margin": 7680, "quality": "conditional_unpinned_seed"}}


class FakeRpc:
    def __init__(self, logs=(), balances=None, timestamp=1100):
        self.logs = list(logs)
        self.balances = balances or {"core": 500, "el_vault": 23, "withdrawal_vault": 47}
        self.timestamp, self.calls, self.reorg_after = timestamp, [], None

    def __call__(self, method, params):
        self.calls.append((method, params))
        if method == "eth_getBlockByNumber":
            number = 200 if params[0] == "finalized" else int(params[0], 16)
            changed = self.reorg_after is not None and len(self.calls) >= self.reorg_after
            return {"number": hex(number), "hash": h(number + (1 if changed else 0)),
                    "timestamp": hex(1000 if number == 100 else self.timestamp + max(0, number - 101) * 12),
                    "parentHash": h(number - 1), "parentBeaconBlockRoot": h(number * 2)}
        if method == "eth_getLogs":
            query = params[0]
            return copy.deepcopy([l for l in self.logs if int(query["fromBlock"], 16) <= int(l["blockNumber"], 16) <= int(query["toBlock"], 16)])
        if method == "eth_getBalance":
            self.assert_block = params[1]
            return hex(self.balances[next(k for k in self.balances if ex.ADDRESSES[k] == params[0])])
        raise AssertionError("Forbidden/unexpected RPC method " + method)


class ReplayTests(unittest.TestCase):
    def test_same_block_before_after_report_groups(self):
        logs = [event("WithdrawalRequested", (25, 20), (2, 1, 1), source="queue", tx=0, index=0),
                event("ETHDistributed", (500, 500, 0, 0, 500), (1000,), tx=1, index=1),
                event("WithdrawalRequested", (25, 20), (3, 1, 1), source="queue", tx=2, index=2)]
        got = ex.collect_at(seed(), 101, h(101), FakeRpc(logs))
        self.assertEqual([r["report_timestamp"] for r in got["state"]["pending_requests"]], [800, 800, 1000])
        self.assertEqual(got["snapshot"]["pending_request_count"], 3)

    def test_same_report_transaction_request_fails_regardless_log_order(self):
        for request_index in (0, 2):
            logs = [event("ETHDistributed", (500, 500, 0, 0, 500), (1000,), tx=1, index=1),
                    event("WithdrawalRequested", (25, 20), (2, 1, 1), source="queue", tx=1, index=request_index)]
            with self.assertRaisesRegex(ex.ExecutionError, "unobservable"):
                ex.collect_at(seed(), 101, h(101), FakeRpc(logs))

    def test_full_report_finalizes_burns_and_resets_cash_once(self):
        logs = [event("WithdrawalsFinalized", (100, 80, 1100), (1, 1), source="queue", index=0),
                event("SharesBurnt", (100, 100, 80), (7,), index=1),
                event("ETHDistributed", (500, 500, 20, 5, 425), (1000,), index=2),
                event("TokenRebased", (200, 800, 1000, 720, 925, 0), (1000,), index=3),
                event("InternalShareRateUpdated", (720, 925, 0), (1000,), index=4),
                event("BatchMetadataUpdate", (1, 1), source="queue", index=5)]
        got = ex.collect_at(seed(), 101, h(101), FakeRpc(logs, {"core": 425, "el_vault": 0, "withdrawal_vault": 0}))
        self.assertEqual(got["state"]["pending_requests"], [])
        self.assertEqual(got["snapshot"]["last_request_id"], 1)
        self.assertEqual(got["snapshot"]["last_finalized_id"], 1)
        self.assertEqual(got["snapshot"]["total_pooled_ether_wei"], "925")
        self.assertEqual(got["snapshot"]["scenario_available_cash_wei"], "325")

    def test_external_rebalance_does_not_burn_total_shares(self):
        s = seed()
        s["external_shares_wei"] = "80"
        logs = [event("ExternalEtherTransferredToBuffer", (100,), index=0),
                event("ExternalSharesBurnt", (72,), index=1)]
        got = ex.collect_at(s, 101, h(101), FakeRpc(logs, {"core": 600, "el_vault": 0, "withdrawal_vault": 0}))
        self.assertEqual(got["snapshot"]["total_shares_wei"], "800")
        self.assertEqual(got["snapshot"]["external_shares_wei"], "8")
        self.assertEqual(got["snapshot"]["total_pooled_ether_wei"], str(1100 + 8 * 1100 // 792))

    def test_external_burn_is_not_double_counted(self):
        s = seed()
        s["external_shares_wei"] = "80"
        logs = [event("SharesBurnt", (10, 10, 8), (7,), index=0), event("ExternalSharesBurnt", (8,), index=1)]
        got = ex.collect_at(s, 101, h(101), FakeRpc(logs))
        self.assertEqual(got["snapshot"]["total_shares_wei"], "792")
        self.assertEqual(got["snapshot"]["internal_shares_wei"], "720")

    def test_deposit_to_cl_preserves_pooled_cash_not_buffer(self):
        logs = [event("DepositedPostReportUpdated", (50,), index=0), event("Unbuffered", (50,), index=1),
                event("DepositsReserveSet", (50,), index=2)]
        got = ex.collect_at(seed(), 101, h(101), FakeRpc(logs, {"core": 450, "el_vault": 23, "withdrawal_vault": 47}))
        self.assertEqual(got["snapshot"]["total_pooled_ether_wei"], "1000")
        self.assertEqual(got["snapshot"]["core_buffer_replayed_wei"], "450")
        self.assertEqual(got["snapshot"]["effective_deposit_reserve_wei"], "50")

    def test_reserve_target_increase_does_not_immediately_increase_reserve(self):
        got = ex.collect_at(seed(), 101, h(101), FakeRpc([event("DepositsReserveTargetSet", (700,))]))
        self.assertEqual(got["snapshot"]["effective_deposit_reserve_wei"], "100")
        got = ex.collect_at(seed(), 101, h(101), FakeRpc([event("DepositsReserveSet", (700,))]))
        self.assertEqual(got["snapshot"]["effective_deposit_reserve_wei"], "500")

    def test_integer_values_above_javascript_safe_limit(self):
        amount = 10 ** 25 + 123
        logs = [event("Submitted", (amount, 0), (1,), index=0),
                event("TransferShares", (amount * 4 // 5,), (0, 1), index=1)]
        got = ex.collect_at(seed(), 101, h(101), FakeRpc(logs, {"core": 500 + amount, "el_vault": 23, "withdrawal_vault": 47}))
        self.assertEqual(got["snapshot"]["total_shares_wei"], str(800 + amount * 4 // 5))
        self.assertEqual(got["snapshot"]["total_pooled_ether_wei"], str(1000 + amount))
        self.assertIsInstance(got["snapshot"]["total_shares_wei"], str)

    def test_forced_eth_not_added_to_available_cash(self):
        got = ex.collect_at(seed(), 101, h(101), FakeRpc(balances={"core": 550, "el_vault": 23, "withdrawal_vault": 47}))
        self.assertEqual(got["snapshot"]["unaccounted_physical_core_wei"], "50")
        self.assertEqual(got["snapshot"]["scenario_available_cash_wei"], "470")
        self.assertFalse(got["validation"]["core_buffer_reconciles"])

    def test_pause_can_expire_without_event(self):
        s = seed()
        s["queue_resume_since"] = 1100
        self.assertFalse(ex.collect_at(s, 101, h(101), FakeRpc())["snapshot"]["queue_paused"])
        logs = [event("Paused", (ex.UINT256_MAX,), source="queue")]
        with self.assertRaisesRegex(ex.ExecutionError, "paused"):
            ex.collect_at(s, 101, h(101), FakeRpc(logs))

    def test_bunker_transition_and_conditional_config_persist(self):
        logs = [event("BunkerModeEnabled", (900,), source="queue")]
        changed = ex.replay_events(seed(), logs)
        self.assertEqual(changed["queue_bunker_since"], 900)
        with self.assertRaisesRegex(ex.ExecutionError, "bunker mode"):
            ex.collect_at(seed(), 101, h(101), FakeRpc(logs))
        got = ex.collect_at(seed(), 101, h(101), FakeRpc())
        self.assertTrue(got["validation"]["conditional"])
        self.assertEqual(got["report_timing"]["quality"], "conditional_unpinned_seed")

    def test_configuration_event_updates_are_exact(self):
        logs = [event("FrameConfigSet", (0, 3), source="consensus", index=0),
                event("RequestTimestampMarginSet", (1234,), source="sanity", index=1)]
        got = ex.collect_at(seed(), 101, h(101), FakeRpc(logs))
        self.assertEqual(got["report_timing"]["frame_duration_seconds"], 1152)
        self.assertEqual(got["report_timing"]["request_timestamp_margin"], 1234)
        self.assertEqual(got["report_timing"]["frame_evidence"]["quality"], "event_replayed")
        self.assertTrue(got["validation"]["conditional"])

    def test_current_report_can_be_delayed_after_reference(self):
        cfg = {"genesis_time": 1606824023, "seconds_per_slot": 12, "slots_per_epoch": 32,
               "epochs_per_frame": 225, "initial_epoch": 201600, "fast_lane_length_slots": 100,
               "request_timestamp_margin": 7680, "quality": "conditional_unpinned_seed"}
        timing = ex.report_timing(cfg, 1791130583, 1791115211)
        self.assertEqual(timing["current_ref_timestamp"], 1791115211)
        self.assertEqual(timing["frame_duration_seconds"], 86400)
        self.assertEqual(timing["next_ref_timestamp"], 1791201611)
        delayed = ex.report_timing(cfg, 1791130583, 1791028811)
        self.assertEqual(delayed["next_ref_timestamp"], 1791115211)


class FailClosedTests(unittest.TestCase):
    def test_failure_does_not_mutate_input(self):
        s, before = seed(), seed()
        logs = [event("WithdrawalRequested", (25, 20), (3, 1, 1), source="queue")]
        with self.assertRaisesRegex(ex.ExecutionError, "Noncontiguous"):
            ex.collect_at(s, 101, h(101), FakeRpc(logs))
        self.assertEqual(s, before)

    def test_missing_seed_row(self):
        s = seed()
        s["pending_requests"] = []
        with self.assertRaisesRegex(ex.ExecutionError, "missing tail"):
            ex.collect_at(s, 101, h(101), FakeRpc())

    def test_finalization_shares_must_match(self):
        l = event("WithdrawalsFinalized", (100, 81, 1100), (1, 1), source="queue")
        with self.assertRaisesRegex(ex.ExecutionError, "Finalization amount"):
            ex.collect_at(seed(), 101, h(101), FakeRpc([l]))

    def test_duplicate_and_removed_logs(self):
        l = event("Transfer", (10,), (1, 2))
        with self.assertRaisesRegex(ex.ExecutionError, "Duplicate"):
            ex.collect_at(seed(), 101, h(101), FakeRpc([l, l]))
        l["removed"] = True
        with self.assertRaisesRegex(ex.ExecutionError, "Removed"):
            ex.collect_at(seed(), 101, h(101), FakeRpc([l]))

    def test_stale_seed_wrong_target_reorg_and_log_hash(self):
        with self.assertRaisesRegex(ex.ExecutionError, "older"):
            ex.collect_at(seed(), 99, h(99), FakeRpc())
        with self.assertRaisesRegex(ex.ExecutionError, "does not match"):
            ex.collect_at(seed(), 101, h(999), FakeRpc())
        rpc = FakeRpc()
        rpc.reorg_after = 7
        with self.assertRaisesRegex(ex.ExecutionError, "changed"):
            ex.collect_at(seed(), 101, h(101), rpc)
        l = event("Transfer", (1,), (1, 2))
        l["blockHash"] = h(999)
        with self.assertRaisesRegex(ex.ExecutionError, "Log block hash"):
            ex.collect_at(seed(), 101, h(101), FakeRpc([l]))

    def test_rpc_log_timestamp_is_checked(self):
        l = event("Transfer", (1,), (1, 2), timestamp=1101)
        with self.assertRaisesRegex(ex.ExecutionError, "timestamp mismatches"):
            ex.collect_at(seed(), 101, h(101), FakeRpc([l]))

    def test_header_supplies_missing_log_timestamp(self):
        l = event("Transfer", (1,), (1, 2))
        del l["blockTimestamp"]
        self.assertTrue(ex.collect_at(seed(), 101, h(101), FakeRpc([l]))["validation"]["complete"])

    def test_negative_buffer_and_underfunded_physical_core(self):
        with self.assertRaisesRegex(ex.ExecutionError, "Negative"):
            ex.collect_at(seed(), 101, h(101), FakeRpc([event("Unbuffered", (600,))]))
        with self.assertRaisesRegex(ex.ExecutionError, "below"):
            ex.collect_at(seed(), 101, h(101), FakeRpc(balances={"core": 499, "el_vault": 23, "withdrawal_vault": 47}))

    def test_new_implementation_is_terminal(self):
        for source in ("queue", "oracle", "locator"):
            with self.assertRaisesRegex(ex.ExecutionError, "topology/version"):
                ex.collect_at(seed(), 101, h(101), FakeRpc([event("Upgraded", (), (1,), source=source)]))

    def test_stopped_core_and_role_changes_are_terminal(self):
        for log in (event("Stopped"), event("RoleGranted", (1,), (1, 1), source="queue")):
            with self.assertRaises(ex.ExecutionError):
                ex.collect_at(seed(), 101, h(101), FakeRpc([log]))

    def test_queue_nft_metadata_events_do_not_change_accounting(self):
        logs = [event("BatchMetadataUpdate", (1, 1), source="queue", index=0),
                event("BaseURISet", (), source="queue", index=1),
                event("NftDescriptorAddressSet", (1,), source="queue", index=2)]
        got = ex.collect_at(seed(), 101, h(101), FakeRpc(logs))
        self.assertEqual(got["snapshot"]["total_pooled_ether_wei"], "1000")
        self.assertEqual(got["state"]["last_request_id"], 1)

    def test_unknown_core_or_config_event_is_terminal(self):
        for source in ("core", "sanity"):
            l = event("Transfer", (1,), (1, 2), source=source)
            l["topics"][0] = h(999)
            with self.assertRaises(ex.ExecutionError):
                ex.collect_at(seed(), 101, h(101), FakeRpc([l]))

    def test_float_or_bool_never_enters_exact_integer_state(self):
        for value in (1.0, True, -1, "-1"):
            with self.assertRaises(ex.ExecutionError):
                ex.integer(value)

    def test_only_permitted_methods_and_pinned_balances(self):
        rpc = FakeRpc()
        ex.collect_at(seed(), 101, h(101), rpc)
        self.assertEqual({method for method, _ in rpc.calls}, ex.HttpRpc.METHODS)
        self.assertEqual([p[1] for m, p in rpc.calls if m == "eth_getBalance"], ["0x65"] * 3)
        with self.assertRaisesRegex(ex.ExecutionError, "not authorized"):
            ex.HttpRpc()("eth_call", [])

    def test_getlogs_split_limit_but_never_retry_403(self):
        calls = []
        def limited(method, params):
            lo, hi = (int(params[0][f], 16) for f in ("fromBlock", "toBlock"))
            calls.append((lo, hi))
            if hi - lo > 1:
                raise ex.RpcError("limit", code=-32005)
            return []
        _, ranges = ex.fetch_logs(limited, 1, 8, chunk_blocks=8)
        self.assertEqual([(r["from_block"], r["to_block"]) for r in ranges], [(1, 2), (3, 4), (5, 6), (7, 8)])
        calls.clear()
        def denied(method, params):
            calls.append(method)
            raise ex.RpcError("HTTP 403", code=403, denied=True)
        with self.assertRaises(ex.RpcError):
            ex.fetch_logs(denied, 1, 8)
        self.assertEqual(calls, ["eth_getLogs"])

    def test_log_fetch_bound_and_ambiguous_single_block(self):
        with self.assertRaisesRegex(ex.ExecutionError, "bounded"):
            ex.fetch_logs(FakeRpc(), 1, 10000, chunk_blocks=1, max_calls=2)
        l = event("Transfer", (1,), (1, 2))
        with self.assertRaisesRegex(ex.ExecutionError, "single block"):
            ex.fetch_logs(FakeRpc([l]), 101, 101, result_cap=1)

    def test_audited_seed_reproduces_exact_rates_and_omits_owners(self):
        directory = os.environ.get("LIDO_EXECUTION_REGRESSION_DIR")
        if not directory:
            self.skipTest("Set LIDO_EXECUTION_REGRESSION_DIR for optional full audit reproduction")
        path = Path(directory)
        s = ex.build_seed(path)
        self.assertEqual(s["total_shares_wei"], "7902355921997249417348115")
        self.assertEqual(ex._totals(ex._numeric_state(s))[2], 9842785314581241588367198)
        self.assertEqual(len(s["pending_requests"]), 479)
        self.assertFalse(any("owner" in row or "requestor" in row for row in s["pending_requests"]))
        self.assertEqual(s["config"]["quality"], "conditional_unpinned_seed")


class FinalityWaitTests(unittest.TestCase):
    class Clock:
        def __init__(self):
            self.now, self.sleeps = 0, []
        def monotonic(self):
            return self.now
        def sleep(self, seconds):
            self.sleeps.append(seconds)
            self.now += seconds

    class LagRpc(FakeRpc):
        def __init__(self, heights):
            super().__init__()
            self.heights = list(heights)
            self.finalized_calls = 0
        def __call__(self, method, params):
            if method == "eth_getBlockByNumber" and params[0] == "finalized":
                self.calls.append((method, params))
                value = self.heights[min(self.finalized_calls, len(self.heights)-1)]
                self.finalized_calls += 1
                if isinstance(value, Exception):
                    raise value
                return {"number": hex(value), "hash": h(value), "timestamp": hex(1100 + (value-101)*12)}
            return super().__call__(method, params)

    def test_lag_waits_then_authenticates_before_replay(self):
        clock, rpc = self.Clock(), self.LagRpc([101, 101, 102])
        got = ex.collect_at(seed(), 101, h(101), rpc, sleep=clock.sleep, monotonic=clock.monotonic)
        self.assertEqual(clock.sleeps, [30, 30])
        self.assertEqual(got["validation"]["finality_wait"], {"polls": 3, "wait_seconds": 60})
        log_index = next(i for i, call in enumerate(rpc.calls) if call[0] == "eth_getLogs")
        self.assertEqual(rpc.calls[log_index-2:log_index],
                         [("eth_getBlockByNumber", ["0x65", False]), ("eth_getBlockByNumber", ["0x66", False])])
        self.assertEqual(got["execution_anchor"]["finalized_block"]["hash"], h(102))

    def test_wait_budget_preserves_downstream_deadline(self):
        clock, rpc = self.Clock(), self.LagRpc([101])
        rpc.deadline = 140
        with self.assertRaisesRegex(ex.ExecutionError, "bounded same-source wait"):
            ex.collect_at(seed(), 101, h(101), rpc, sleep=clock.sleep, monotonic=clock.monotonic)
        self.assertEqual(clock.sleeps, [20])
        self.assertFalse(any(m == "eth_getLogs" for m, _ in rpc.calls))

    def test_two_epoch_wait_cap(self):
        clock, rpc = self.Clock(), self.LagRpc([101])
        with self.assertRaisesRegex(ex.ExecutionError, "bounded same-source wait"):
            ex.collect_at(seed(), 101, h(101), rpc, sleep=clock.sleep, monotonic=clock.monotonic)
        self.assertEqual(clock.now, 768)
        self.assertEqual(rpc.finalized_calls, 27)

    def test_denial_during_wait_is_not_retried(self):
        clock = self.Clock()
        rpc = self.LagRpc([101, ex.RpcError("denied", code=403, denied=True), 102])
        with self.assertRaises(ex.RpcError):
            ex.collect_at(seed(), 101, h(101), rpc, sleep=clock.sleep, monotonic=clock.monotonic)
        self.assertEqual(rpc.finalized_calls, 2)
        self.assertEqual(clock.sleeps, [30])
        self.assertFalse(any(m == "eth_getLogs" for m, _ in rpc.calls))



class HistoricalReportTests(unittest.TestCase):
    def test_portable_real_report_trace_including_nft_metadata(self):
        path = Path(__file__).resolve().parent / "fixtures" / "lido-report-boundary-20261004.json"
        fixture = json.loads(path.read_text())
        self.assertNotIn("/workspace/", path.read_text())
        self.assertEqual(len(fixture["logs"]), 29)
        self.assertTrue(any(l["topics"][0] == ex.TOPICS["BatchMetadataUpdate"] for l in fixture["logs"]))
        for log in fixture["logs"]:
            self.assertEqual(log["transactionHash"], fixture["source"]["report_transaction"])
            self.assertEqual(log["blockHash"], fixture["source"]["report_block_hash"])
        for row in fixture["initial_state"]["pending_requests"]:
            self.assertNotIn("owner", row)
            self.assertNotIn("requestor", row)
        ex._validate(ex._numeric_state(fixture["initial_state"]))
        got = ex.replay_events(fixture["initial_state"], fixture["logs"])
        for field, expected in fixture["expected"].items():
            self.assertEqual(got[field], expected, field)
        self.assertEqual(got["total_shares_wei"], "7901094207520922607064259")
        self.assertEqual(got["core_buffer_wei"], "879519355394551093989")
        self.assertEqual(got["last_finalized_id"], 137257)
        self.assertEqual([r["request_id"] for r in got["pending_requests"]], fixture["expected_pending_ids"])

    def test_optional_full_history_reproduces_audited_seed(self):
        directory = os.environ.get("LIDO_EXECUTION_REGRESSION_DIR")
        if not directory:
            self.skipTest("Set LIDO_EXECUTION_REGRESSION_DIR for optional full history reproduction")
        root = Path(directory)
        read = lambda name: json.loads((root / name).read_text())
        logs = []
        for name in ("core", "queue", "oracle"):
            logs += read("raw/rpc/" + ex.ADDRESSES[name] + "_26109778_26119777.json")["response"]["result"]
        logs += read("raw/rpc/fresh_core_logs.json")["response"]["result"]
        logs += read("raw/rpc/fresh_queue_logs.json")["response"]["result"]
        anchor = [l for l in logs if l["address"] == ex.ADDRESSES["core"] and
                  l["topics"][0] == ex.TOPICS["InternalShareRateUpdated"]][0]
        anchor_key = ex.key(anchor)
        txlogs = [l for l in logs if l["transactionHash"] == anchor["transactionHash"] and
                  l["address"] == ex.ADDRESSES["core"]]
        def data(name):
            return ex.words(next(l for l in txlogs if l["topics"][0] == ex.TOPICS[name]))
        final_seed = ex.build_seed(root)
        start = copy.deepcopy(final_seed)
        start.update(block=anchor_key[0], hash=anchor["blockHash"], timestamp=int(anchor["blockTimestamp"], 16),
                     last_finalized_id=137255, last_report_timestamp=int(anchor["topics"][1], 16),
                     core_buffer_wei=str(data("ETHDistributed")[4]), total_shares_wei=str(data("TokenRebased")[3]),
                     external_shares_wei=str(data("TokenRebased")[3] - data("InternalShareRateUpdated")[0]),
                     cl_balance_wei=str(sum(data("CLBalancesUpdated"))),
                     deposited_post_report_wei=str(data("DepositedPostReportUpdated")[0]))
        rows = read("all_requests_with_report_groups.json")
        start["pending_requests"] = [r for r in rows if r["request_id"] > 137255 and
                                     (r["block"], r["transaction_index"], r["log_index"]) <= anchor_key]
        start["last_request_id"] = start["pending_requests"][-1]["request_id"]
        got = ex.replay_events(start, [l for l in logs if ex.key(l) > anchor_key])
        for field in ("last_finalized_id", "last_request_id", "last_report_timestamp", "total_shares_wei",
                      "external_shares_wei", "core_buffer_wei", "cl_balance_wei", "deposited_post_report_wei"):
            self.assertEqual(got[field], final_seed[field], field)
        self.assertEqual([r["request_id"] for r in got["pending_requests"]],
                         [r["request_id"] for r in final_seed["pending_requests"]])
        self.assertEqual([r["report_timestamp"] for r in got["pending_requests"]],
                         [r["report_timestamp"] for r in final_seed["pending_requests"]])


if __name__ == "__main__":
    unittest.main()
