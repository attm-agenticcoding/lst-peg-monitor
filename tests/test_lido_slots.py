"""12-hour UTC boundaries, conservative daily-state migration and atomic races."""
import json
from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

from test_lido_actions_controller import ControllerFixture, ROOT, raw
import actions_controller as ac
from refresh import second, slot_key, slot_attempted, utc


class SlotPolicyTests(unittest.TestCase):
    def test_shared_sender_receiver_migration_cases(self):
        for case in json.loads((ROOT / 'tests/fixtures/lido-slot-migration.json').read_text()):
            with self.subTest(case=case['name']):
                if case.get('error'):
                    with self.assertRaises(ValueError):
                        slot_attempted(case['refresh'], second(case['now']))
                else:
                    self.assertEqual(slot_attempted(case['refresh'], second(case['now'])), case['blocked'])


class SlotControllerTests(ControllerFixture, unittest.TestCase):
    def configure(self, at, cron, run_id=123456):
        self.now = second(at)
        event = self.scratch / 'event.json'
        event.write_bytes(raw({'schedule': cron}))
        env = {'GITHUB_EVENT_NAME': 'schedule', 'GITHUB_RUN_ID': str(run_id),
               'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_EVENT_PATH': str(event), 'GITHUB_SHA': '1' * 40}
        self.provenance = {'id': run_id, 'event': 'schedule', 'run_attempt': 1,
                           'path': ac.WORKFLOW_PATH, 'head_branch': 'main', 'head_sha': '1' * 40,
                           'created_at': utc(self.now), 'repository': {'full_name': ac.REPOSITORY}}
        self.api.workflow_run = Mock(return_value=self.provenance)
        self.controller = ac.Controller(self.api, ROOT, self.scratch, now=lambda: self.now,
                                        child=self.child, environment=env)

    def test_two_slots_each_consume_failure_once(self):
        for at, cron, ident in (('2026-10-08T00:00:00Z', '0 0 * * *', 100),
                                ('2026-10-08T12:00:00Z', '0 12 * * *', 101),
                                ('2026-10-09T00:00:00Z', '0 0 * * *', 102)):
            self.configure(at, cron, ident)
            self.assertEqual(self.controller.execute()['stage'], 'failed')
            self.assertEqual(self.controller.execute()['stage'], 'skipped')
            refresh = json.loads(self.api.current.files[ac.SNAPSHOT_PATH])['refresh']
            self.assertEqual(refresh['attemptSlot'], at)
        self.assertEqual(self.child.call_count, 3)

    def test_legacy_migration_preserves_history_and_manual_identity(self):
        self.configure('2026-10-08T12:00:00Z', '0 12 * * *')
        legacy = {'attemptDay': '2026-10-08', 'attemptAt': '2026-10-08T01:11:26Z',
                  'trigger': 'relay-dispatch', 'manualAttemptIds': ['consumed-manual-320']}
        old = {**self.old, 'refresh': legacy}
        self.api.change({ac.SNAPSHOT_PATH: raw(old)})
        self.assertEqual(self.controller.execute()['stage'], 'failed')
        result = json.loads(self.api.current.files[ac.SNAPSHOT_PATH])
        self.assertEqual(result['refresh']['attemptDay'], legacy['attemptDay'])
        self.assertEqual(result['refresh']['manualAttemptIds'], legacy['manualAttemptIds'])
        self.assertEqual(result['refresh']['attemptSlot'], '2026-10-08T12:00:00Z')
        self.assertEqual({k:v for k,v in result.items() if k != 'refresh'}, self.old)

    def test_no_backfill_from_wrong_cron_or_cross_slot_queue(self):
        for at, cron in (('2026-10-08T12:01:00Z', '0 0 * * *'),
                         ('2026-10-09T00:01:00Z', '0 12 * * *')):
            self.configure(at, cron)
            with self.assertRaises(ac.Stopped):
                self.controller.execute()
        self.configure('2026-10-08T11:59:59Z', '0 0 * * *')
        self.now += 1
        with self.assertRaises(ac.Stopped):
            self.controller.execute()
        self.child.assert_not_called()
        self.assertEqual(self.api.commits, [])

    def test_noon_at_preflight_cas_or_materialization_never_starts_source(self):
        for phase in ('preflight', 'cas', 'materialize'):
            with self.subTest(phase=phase):
                self.api.change({ac.SNAPSHOT_PATH: raw(self.old)})
                self.configure('2026-10-08T11:59:59Z', '0 0 * * *')
                self.api.pages_config = lambda: None
                def tick(*args):
                    self.now += 1
                if phase == 'preflight':
                    self.api.pages_config = tick
                elif phase == 'cas':
                    self.api.race = lambda api: (tick(), api.change({'data/history.json': b'price race'}))
                original = ac.materialize
                def materialize(current, directory):
                    original(current, directory)
                    if phase == 'materialize':
                        tick()
                with patch.object(ac, 'materialize', side_effect=materialize):
                    with self.assertRaises(ac.Stopped):
                        self.controller.execute()
        self.child.assert_not_called()

    def test_active_previous_slot_lease_still_blocks_new_slot(self):
        self.configure('2026-10-08T11:59:00Z', '0 0 * * *')
        owned, _ = ac.propose_lease(self.old, self.now, 'morning-owner')
        self.api.change({ac.SNAPSHOT_PATH: raw(owned)})
        self.configure('2026-10-08T12:00:00Z', '0 12 * * *')
        self.assertEqual(self.controller.execute()['stage'], 'skipped')
        self.child.assert_not_called()

    def test_same_slot_relay_wins_cron_cas_before_source(self):
        self.configure('2026-10-08T12:00:00Z', '0 12 * * *')
        def compete(api):
            owned, _ = ac.propose_lease(self.old, self.now, 'relay-owner', trigger='relay-dispatch')
            api.change({ac.SNAPSHOT_PATH: raw(owned)})
        self.api.race = compete
        with self.assertRaises(ac.Stopped):
            self.controller.execute()
        self.child.assert_not_called()
        self.assertEqual(self.api.commits, [])


if __name__ == '__main__':
    unittest.main()
