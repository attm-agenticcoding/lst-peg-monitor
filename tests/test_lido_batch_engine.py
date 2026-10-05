import json
import os
from pathlib import Path
import unittest

from batch_engine import BatchState, E27, Queue, Request, ReportScenario, simulate_reports

ROOT = Path(os.environ.get('LIDO_EXECUTION_REGRESSION_DIR',
                          str(Path(__file__).resolve().parent / 'fixtures')))


def r(i, steth=100, shares=None, timestamp=100, report_timestamp=0):
    return Request(i, steth, steth if shares is None else shares, timestamp, report_timestamp)


class ExactBatchTests(unittest.TestCase):
    def test_1500_tier_same_report_full_completion(self):
        requests = [r(1, 1000), r(2, 500)]
        result = simulate_reports(requests, [ReportScenario('one', 1500, 100, E27)],
                                  tiers={'1500': [1, 2]})
        self.assertEqual(result['reports'][0]['batches'], [2])
        self.assertEqual(result['reports'][0]['eth_to_lock_wei'], 1500)
        self.assertEqual(result['tiers']['1500']['full_completion_report_label'], 'one')

    def test_1500_tier_split_requires_last_request(self):
        requests = [r(1, 1000), r(2, 500)]
        result = simulate_reports(requests, [ReportScenario('first', 1000, 100, E27),
                                            ReportScenario('second', 500, 100, E27)],
                                  tiers={'1500': [1, 2]})
        self.assertEqual([x['last_finalized_id'] for x in result['reports']], [1, 2])
        self.assertEqual(result['tiers']['1500']['first_finalization_report_index'], 0)
        self.assertEqual(result['tiers']['1500']['full_completion_report_index'], 1)
        self.assertEqual(result['tiers']['1500']['full_completion_report_label'], 'second')

    def test_no_skip_no_partial(self):
        result = Queue([r(1, 1000), r(2, 10)]).calculate_batches(999, 100, E27)
        self.assertEqual(result['batches'], [])
        self.assertEqual(result['eth_to_lock_wei'], 0)
        self.assertEqual(result['stop_reason'], 'budget')

    def test_timestamp_equality_is_eligible(self):
        result = Queue([r(1, timestamp=100), r(2, timestamp=101)]).calculate_batches(1000, 100, E27)
        self.assertEqual(result['batches'], [1])
        self.assertEqual(result['stop_reason'], 'timestamp')

    def test_more_than_36_requests_fit_one_batch(self):
        result = Queue([r(i) for i in range(1, 101)]).calculate_batches(10000, 100, E27)
        self.assertEqual(result['batches'], [100])
        self.assertEqual(result['eth_to_lock_wei'], 10000)

    def test_37th_segment_debit_does_not_equal_prefinalize(self):
        requests = [r(i, 100 if i % 2 else 200, 100, timestamp=100,
                      report_timestamp=i) for i in range(1, 38)]
        result = Queue(requests).calculate_batches(5000, 100, E27)
        self.assertEqual(result['batches'], list(range(1, 37)))
        self.assertEqual(result['candidate_last_request_id'], 36)
        self.assertEqual(result['remaining_batch_state_budget_wei'], 1300)
        self.assertEqual(result['eth_to_lock_wei'], 3600)
        self.assertEqual(result['actual_budget_remainder_wei'], 1400)
        self.assertEqual(result['stop_reason'], 'max_batches')

    def test_nominal_vs_min_dust_branch(self):
        request = r(1, 1, E27 - 1)
        self.assertEqual(request.rate, 1)
        self.assertEqual(min(request.amount_steth_wei, request.amount_shares_wei // E27), 0)
        self.assertEqual(request.cost(1), 1)
        result = Queue([request]).calculate_batches(1, 100, 1)
        self.assertEqual(result['eth_to_lock_wei'], 1)

    def test_aggregate_prefinalize_rounding(self):
        queue = Queue([r(1, 3, 3), r(2, 3, 3)])
        result = queue.calculate_batches(2, 100, E27 // 2)
        self.assertEqual(result['batches'], [2])
        self.assertEqual(result['remaining_batch_state_budget_wei'], 0)
        self.assertEqual(result['eth_to_lock_wei'], 3)
        self.assertFalse(result['prefinalize_fits_budget'])
        scenario = simulate_reports(queue.requests, [ReportScenario('too tight', 2, 100, E27 // 2)])
        self.assertEqual(scenario['last_finalized_id'], 0)
        self.assertEqual(scenario['reports'][0]['guard_failure'], 'aggregate_prefinalize_exceeds_eligible_budget')

    def test_same_report_timestamp_merges_across_share_threshold(self):
        queue = Queue([r(1, 100, 100), r(2, 200, 100)])
        self.assertEqual(queue.calculate_batches(1000, 100, E27)['batches'], [2])
        different = Queue([r(1, 100, 100, report_timestamp=1),
                           r(2, 200, 100, report_timestamp=2)])
        self.assertEqual(different.calculate_batches(1000, 100, E27)['batches'], [1, 2])

    def test_chunk_semantics_and_zero_budget_guard(self):
        queue = Queue([r(1), r(2)])
        state = queue.calculate_finalization_batches(E27, 100, 1, BatchState(100))
        self.assertFalse(state.finished)
        self.assertEqual(state.remaining_eth_budget, 0)
        with self.assertRaisesRegex(ValueError, 'InvalidState'):
            queue.calculate_finalization_batches(E27, 100, 1, state)
        result = queue.calculate_batches(100, 100, E27, max_requests_per_call=1)
        self.assertEqual(result['batches'], [1])
        self.assertEqual(result['view_calls'], 1)
        self.assertFalse(result['canonical_state_finished'])
        self.assertTrue(result['stopped_with_zero_budget'])

    def test_chunk_state_continues_same_batch(self):
        queue = Queue([r(i) for i in range(1, 6)])
        result = queue.calculate_batches(1000, 100, E27, max_requests_per_call=2)
        self.assertEqual(result['batches'], [5])
        self.assertEqual(result['view_calls'], 3)
        self.assertTrue(result['canonical_state_finished'])

    def test_paused_and_zero_budget_no_calls(self):
        queue = Queue([r(1)])
        for result in (queue.calculate_batches(0, 100, E27),
                       queue.calculate_batches(100, 100, E27, paused=True)):
            self.assertEqual(result['batches'], [])
            self.assertEqual(result['view_calls'], 0)

    def test_no_automatic_budget_carryover(self):
        result = simulate_reports([r(1, 1000)], [ReportScenario('first', 700, 100, E27),
                                                ReportScenario('second', 700, 100, E27)])
        self.assertEqual(result['last_finalized_id'], 0)

    def test_reference_visibility_blocks_new_requests(self):
        result = simulate_reports([r(1), r(2)], [ReportScenario('first', 1000, 100, E27,
                                                               visible_last_request_id=1),
                                                ReportScenario('second', 1000, 100, E27,
                                                               visible_last_request_id=2)])
        self.assertEqual([x['last_finalized_id'] for x in result['reports']], [1, 2])

    def test_invalid_inputs_fail_closed(self):
        with self.assertRaises(ValueError):
            Queue([r(1), r(3)])
        with self.assertRaises(ValueError):
            Queue([r(1)]).calculate_batches(100.0, 100, E27)
        with self.assertRaises(ValueError):
            Queue([r(1)]).calculate_batches(100, 100, 0)
        with self.assertRaises(ValueError):
            Queue([r(1)]).calculate_batches(100, 100, E27, max_requests_per_call=0)
        with self.assertRaises(ValueError):
            Queue([r(1), r(2)]).prefinalize([2, 1], E27)

    def test_mapping_rejects_float_bool_and_missing_report_group(self):
        row = dict(request_id=1, amount_steth_wei='100', amount_shares_wei='100',
                   timestamp=100, report_timestamp=0)
        self.assertEqual(Request.from_mapping(row).amount_steth_wei, 100)
        for value in (100.9, True, None, '100.0'):
            with self.assertRaises(ValueError):
                Request.from_mapping(dict(row, amount_steth_wei=value))
        with self.assertRaises(ValueError):
            Request.from_mapping(dict(row, report_timestamp=None))

    def test_uint256_overflow_rejected(self):
        with self.assertRaises(ValueError):
            Request(1, 2**128, 1, 100, 0)
        with self.assertRaises(ValueError):
            Queue([r(1, 2**127, 1), r(2, 2**127, 1)])

    def test_latest_delivered_report_retrospective_consistency(self):
        """Retrospective diagnostic only: observed rate/transfers are post-publication facts."""
        required = [ROOT / 'all_requests_with_report_groups.json',
                    ROOT / 'latest_report_budget_reconstruction.json']
        if not all(p.exists() for p in required):
            self.skipTest('delivered-report local fixtures absent')
        reconstruction = json.loads(required[1].read_text())
        ref_timestamp = reconstruction['report_reference_timestamp']
        rows = json.loads(required[0].read_text())
        requests = [Request.from_mapping(row) for row in rows
                    if row['request_id'] >= reconstruction['actual_finalized_from_id']
                    and row['timestamp'] <= ref_timestamp]
        last_finalized = reconstruction['actual_finalized_from_id'] - 1
        result = Queue(requests, last_finalized).calculate_batches(
            int(reconstruction['reference_budget_ex_post_reconstruction_wei']),
            ref_timestamp - 8052,  # q=32,s=12,M=7680 scenario; parent recovers current config
            int(reconstruction['report_share_rate']))
        self.assertEqual(result['batches'], [137257])
        self.assertEqual(result['eth_to_lock_wei'], int(reconstruction['actual_eth_locked_wei']))
        self.assertEqual(result['new_queue_shares_wei'], 1606102472032082652124)
        self.assertEqual(result['stop_reason'], 'budget')
        self.assertEqual(result['actual_budget_remainder_wei'], 873989210154551093989)
        report = {'test': 'latest_delivered_report_retrospective_consistency',
                  'warning': reconstruction['warning'],
                  'age_margin_assumption_seconds': 7680,
                  'safe_timestamp': ref_timestamp - 8052,
                  'expected_endpoints': [137257], 'result': result}
        self.assertTrue(result['prefinalize_fits_budget'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
