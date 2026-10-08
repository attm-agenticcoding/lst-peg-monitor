"""Offline relay receiver tests: source work must follow the shared durable CAS."""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock

from test_lido_actions_controller import ControllerFixture, ROOT, raw
import actions_controller as ac
from refresh import utc


class RelayControllerTests(ControllerFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.event = self.scratch / 'event.json'
        self.inputs = {'mode': 'daily', 'attempt_day': ac.day_key(self.now), 'relay_run_id': '98765'}
        self.event.write_bytes(raw({'inputs': self.inputs}))
        self.environment = {'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_RUN_ID': '123456',
                            'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_EVENT_PATH': str(self.event),
                            'GITHUB_SHA': '1' * 40}
        self.run = {'id': 123456, 'event': 'workflow_dispatch', 'run_attempt': 1,
                    'path': ac.WORKFLOW_PATH, 'head_branch': 'main', 'head_sha': '1' * 40,
                    'created_at': utc(self.now - 60), 'repository': {'full_name': ac.REPOSITORY}}
        self.parent = {**self.run, 'id': 98765, 'path': ac.RELAY_WORKFLOW_PATH,
                       'created_at': utc(self.now - 600)}
        self.api.workflow_run = Mock(side_effect=lambda run_id: self.run if run_id == '123456' else self.parent)
        self.controller = ac.Controller(self.api, ROOT, self.scratch, now=lambda: self.now,
                                        child=self.child, environment=self.environment)

    def test_daily_ignores_manual_320_consumes_day_and_retains_provenance(self):
        self.api.change({ac.REQUEST_PATH: raw({'schema': 1, 'requestId': 'consumed-manual', 'rpcCallBudget': 320})})
        def inspect(command, **kwargs):
            refresh = json.loads(self.api.current.files[ac.SNAPSHOT_PATH])['refresh']
            self.assertEqual(refresh['trigger'], 'relay-dispatch')
            lease = refresh['lease']
            self.assertEqual(lease['requestKey'], self.inputs['attempt_day'])
            self.assertEqual(lease['rpcCallBudget'], 256)
            self.assertEqual(lease['expiresAtEpoch'] - lease['acquiredAtEpoch'], 2100)
            self.assertEqual(kwargs['timeout'], 1530)
            return self.collect_failed(command, **kwargs)
        self.child.side_effect = inspect
        self.assertEqual(self.controller.execute(b'invalid manual JSON')['stage'], 'failed')
        self.assertEqual(self.controller.audit['scheduledRequest']['trigger'], 'relay-dispatch')
        self.assertEqual(self.controller.audit['scheduledRequest']['relayRunId'], '98765')
        self.assertIsNone(self.controller.audit['manualRequest'])
        self.assertEqual(self.controller.execute()['stage'], 'skipped')
        self.assertEqual(self.child.call_count, 1)
        final = json.loads(self.api.current.files[ac.SNAPSHOT_PATH])
        proposed, reason = ac.propose_lease(final, self.now, 'later-cron')
        self.assertIsNone(proposed)
        self.assertIn('already been attempted', reason)

    def test_success_publishes_exact_dual_json_and_verifies_pages(self):
        self.child.side_effect = lambda command, **kwargs: self.write_result(command, self.result(success=True))
        answer = self.controller.execute()
        self.assertEqual(answer['stage'], 'complete')
        self.assertTrue(answer['pagesVerified'])
        self.assertEqual(set(self.api.commits[-1][1]), set(ac.DATA_PATHS))

    def test_cron_consumption_or_active_manual_lease_blocks_relay(self):
        for trigger in ('scheduled', 'manual', 'relay-dispatch'):
            owned, _ = ac.propose_lease(self.old, self.now, 'owner', trigger=trigger,
                                        manual_request_id='explicit' if trigger == 'manual' else None)
            if trigger == 'scheduled':
                owned = ac.failure_snapshot(owned, self.now, 'consumed cron failure')
            self.api.change({ac.SNAPSHOT_PATH: raw(owned)})
            self.assertEqual(self.controller.execute()['stage'], 'skipped')
        self.child.assert_not_called()
        self.assertEqual(self.api.commits, [])

    def test_cron_wins_cas_race_before_any_relay_source_query(self):
        def competing_cron(api):
            owned, _ = ac.propose_lease(self.old, self.now, 'cron-owner')
            api.change({ac.SNAPSHOT_PATH: raw(owned)})
        self.api.race = competing_cron
        with self.assertRaises(ac.Stopped):
            self.controller.execute()
        self.child.assert_not_called()
        self.assertEqual(self.api.commits, [])

    def test_invalid_input_rerun_parent_or_day_cannot_write_or_collect(self):
        for changes in ({'mode': 'bad'}, {'attempt_day': '2026-01-01'}, {'relay_run_id': '../bad'},
                        {'rpcCallBudget': '320'}, {'mode': 'manual'}):
            self.event.write_bytes(raw({'inputs': {**self.inputs, **changes}}))
            with self.subTest(changes=changes), self.assertRaises(ac.Stopped):
                self.controller.execute()
        self.event.write_bytes(raw({'inputs': self.inputs}))
        for changes in ({'GITHUB_RUN_ATTEMPT': '2'}, {'GITHUB_RUN_ID': 'bad'}):
            self.controller.environment = {**self.environment, **changes}
            with self.assertRaises(ac.Stopped):
                self.controller.execute()
        self.controller.environment = self.environment
        original = deepcopy(self.parent)
        for changes in ({'path': ac.WORKFLOW_PATH}, {'head_branch': 'other'}, {'id': 123},
                        {'repository': {'full_name': 'other/repo'}}, {'created_at': utc(self.now + 1)}):
            self.parent = {**original, **changes}
            with self.assertRaises(ac.Stopped):
                self.controller.execute()
        self.child.assert_not_called()
        self.assertEqual(self.api.commits, [])

    def test_cross_midnight_dispatch_cannot_consume_new_day(self):
        self.now += 86400
        with self.assertRaises(ac.Stopped):
            self.controller.execute()
        self.child.assert_not_called()
        self.assertEqual(self.api.commits, [])

    def test_next_day_eligible_but_relay_can_never_select_320(self):
        self.controller.execute()
        self.now += 86400
        current = json.loads(self.api.current.files[ac.SNAPSHOT_PATH])
        owned, reason = ac.propose_lease(current, self.now, 'next-day', trigger='relay-dispatch')
        self.assertIsNone(reason)
        self.assertEqual(owned['refresh']['lease']['rpcCallBudget'], 256)
        with self.assertRaises(ValueError):
            ac.propose_lease(current, self.now, 'invalid', trigger='relay-dispatch', rpc_call_budget=320)


if __name__ == '__main__':
    unittest.main()
