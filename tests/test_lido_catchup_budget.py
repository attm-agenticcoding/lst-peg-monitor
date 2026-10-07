"""Offline checks for the one-lease manual catch-up budget; no source access.

Fixture classes are referenced through modules, avoiding duplicate discovery.
The journal source uses the existing explicitly synthetic fixture.
"""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/lido'))
import refresh
import recovery
from rpc_budget import lease_rpc_budget
from publication import validate_result
import test_lido_refresh as refresh_fixtures
import test_lido_recovery_journal as journal_fixtures
import test_lido_rpc_concurrency as rpc_fixtures
from rpc_clock import RpcClock, advancing_wait


class LeaseBudgetTests(unittest.TestCase):
    setUp = refresh_fixtures.RefreshTests.setUp

    def manual(self, budget=320, *, snapshot=None):
        value, reason = refresh.propose_lease(
            self.old if snapshot is None else snapshot, self.now, 'catchup',
            trigger='manual', manual_request_id='request-320', rpc_call_budget=budget)
        self.assertIsNone(reason)
        return value

    def test_scheduled_and_manual_defaults_remain_256(self):
        for kwargs in ({}, {'trigger': 'manual', 'manual_request_id': 'default-manual'}):
            with self.subTest(kwargs=kwargs):
                value, reason = refresh.propose_lease(self.old, self.now, 'default', **kwargs)
                self.assertIsNone(reason)
                self.assertEqual(value['refresh']['lease']['rpcCallBudget'], 256)
                self.assertEqual(lease_rpc_budget(value), 256)
        clock = RpcClock()
        rpc = refresh.Rpc(clock() + 1500, clock=clock, admission_wait=advancing_wait(clock))
        self.assertEqual(rpc.max_calls, 256)
        self.assertEqual(refresh.MAX_RPC_CALLS, 256)
        self.assertEqual(recovery.MAX_CALLS, 256)

    def test_budget_accepts_only_the_two_exact_integer_values(self):
        for value in (None, True, False, 0, -1, 255, 257, 319, 321,
                      256.0, 320.0, '256', '320', [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.manual(value)
        for value in (256, 320):
            self.assertEqual(lease_rpc_budget(self.manual(value)), value)

    def test_scheduled_cannot_request_320(self):
        with self.assertRaises(ValueError):
            refresh.propose_lease(self.old, self.now, 'scheduled', rpc_call_budget=320)

    def test_320_lease_is_bound_to_its_manual_request_and_run(self):
        value = self.manual()
        current, lease = value['refresh'], value['refresh']['lease']
        self.assertEqual(lease['rpcCallBudget'], 320)
        self.assertEqual(lease['trigger'], 'manual')
        self.assertEqual(current['trigger'], 'manual')
        self.assertEqual(lease['runId'], 'catchup')
        self.assertEqual(lease['requestKey'], 'request-320')
        self.assertEqual(current['lastManualRequestId'], 'request-320')
        self.assertIn('request-320', current['manualAttemptIds'])
        self.assertEqual(lease['expiresAtEpoch'] - lease['acquiredAtEpoch'], 2100)
        self.assertEqual(current['maxSourceAgeSeconds'], 5400)
        self.assertEqual(current['expectedIntervalSeconds'], 86400)
        self.assertEqual(current['maxOperationalAgeSeconds'], 91800)
        self.assertEqual(refresh.MAX_RUN_SECONDS, 1500)
        self.assertEqual(recovery.MAX_SECONDS, 1500)
        self.assertEqual(refresh.HEADER_WORKERS, 4)
        self.assertEqual(recovery.MAX_IN_FLIGHT, 4)

    def test_320_budget_rejects_unbound_or_malformed_persisted_identity(self):
        mutations = [
            ('lease', 'trigger', 'scheduled'), ('lease', 'trigger', None),
            ('refresh', 'trigger', 'scheduled'), ('refresh', 'trigger', None),
            ('lease', 'runId', ''), ('lease', 'runId', None),
            ('lease', 'requestKey', 'another-request'),
            ('lease', 'requestKey', 'bad key'), ('lease', 'requestKey', None),
            ('refresh', 'lastManualRequestId', 'another-request'),
            ('refresh', 'lastManualRequestId', None),
            ('refresh', 'manualAttemptIds', []),
            ('refresh', 'manualAttemptIds', 'request-320'),
        ]
        for owner, key, replacement in mutations:
            with self.subTest(owner=owner, key=key, replacement=replacement):
                value = self.manual()
                target = value['refresh']['lease'] if owner == 'lease' else value['refresh']
                target[key] = replacement
                with self.assertRaises(ValueError):
                    lease_rpc_budget(value)
        for invalid in (True, '320', 320.0, 321):
            value = self.manual()
            value['refresh']['lease']['rpcCallBudget'] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                lease_rpc_budget(value)

    def test_legacy_missing_budget_defaults_to_256(self):
        self.assertEqual(lease_rpc_budget({}), 256)
        self.assertEqual(lease_rpc_budget({'refresh': {'lease': None}}), 256)
        value = self.manual()
        del value['refresh']['lease']['rpcCallBudget']
        self.assertEqual(lease_rpc_budget(value), 256)

    def test_existing_256_lease_cannot_be_reproposed_as_320(self):
        value = self.manual(256)
        for request in ('request-320', 'different-request'):
            proposed, reason = refresh.propose_lease(
                value, self.now + 1, 'replacement', trigger='manual',
                manual_request_id=request, rpc_call_budget=320)
            self.assertIsNone(proposed)
            self.assertIn('lease', reason)
        self.assertEqual(lease_rpc_budget(value), 256)

    def test_release_never_changes_the_next_run_default(self):
        for result_state in ('error', 'ok'):
            completed = refresh.failure_snapshot(self.manual(), self.now + 1, 'offline fixture')
            completed['refresh']['state'] = result_state
            for kwargs in ({}, {'trigger': 'manual', 'manual_request_id': 'next-request'}):
                with self.subTest(result_state=result_state, kwargs=kwargs):
                    next_run, reason = refresh.propose_lease(completed, self.now + 2, 'next', **kwargs)
                    self.assertIsNone(reason)
                    self.assertEqual(lease_rpc_budget(next_run), 256)
            retried, reason = refresh.propose_lease(
                completed, self.now + 3, 'retry', trigger='manual',
                manual_request_id='request-320', rpc_call_budget=320)
            self.assertIsNone(retried)
            self.assertIn('already', reason)

    @staticmethod
    def replace_file(entry, value):
        entry['content'] = refresh.canonical(value)
        entry['sha256'] = hashlib.sha256(entry['content'].encode()).hexdigest()

    def test_320_failure_publication_preserves_all_last_good_evidence(self):
        leased = self.manual()
        current = {refresh.SNAPSHOT_PATH: refresh.canonical(leased).encode(),
                   refresh.STATE_PATH: b'{}'}
        failed = refresh.failure_snapshot(leased, self.now + 1, 'offline source failure')
        result = refresh.manifest('failed', 'catchup', current, {refresh.SNAPSHOT_PATH: failed})
        result['localVerificationOnly'] = False
        self.assertEqual(len(validate_result(result, current, self.now + 2)), 1)
        self.assertEqual({k: v for k, v in failed.items() if k != 'refresh'},
                         {k: v for k, v in leased.items() if k != 'refresh'})
        self.assertIsNone(failed['refresh']['lease'])
        self.assertEqual(lease_rpc_budget(failed), 256)
        tampered = deepcopy(result)
        value = json.loads(tampered['files'][0]['content'])
        value['asOf'] = refresh.utc(self.now)
        self.replace_file(tampered['files'][0], value)
        with self.assertRaisesRegex(ValueError, 'modified successful'):
            validate_result(tampered, current, self.now + 2)

    def test_320_success_retains_the_two_file_atomic_publication_gate(self):
        result, current = refresh_fixtures.RefreshTests.success_fixture(self)
        previous = json.loads(current[refresh.SNAPSHOT_PATH])
        previous['refresh']['lease'] = None
        leased = self.manual(snapshot=previous)
        current[refresh.SNAPSHOT_PATH] = refresh.canonical(leased).encode()
        output_files = {entry['path']: json.loads(entry['content']) for entry in result['files']}
        output = output_files[refresh.SNAPSHOT_PATH]
        output['refresh'] = {**leased['refresh'], 'state': 'ok', 'runId': 'catchup', 'lease': None}
        result = refresh.manifest('complete', 'catchup', current, output_files)
        result['localVerificationOnly'] = False
        entries = validate_result(result, current, self.now)
        self.assertIs(result['atomicPublicationRequired'], True)
        self.assertEqual({entry['path'] for entry in entries}, set(current))
        self.assertEqual(len(entries), 2)
        self.assertEqual(output['refresh']['attemptDay'], previous['refresh']['attemptDay'])
        self.assertIsNone(output['refresh']['lease'])
        self.assertEqual(lease_rpc_budget(output), 256)
        incomplete = deepcopy(result)
        incomplete['files'] = [entry for entry in incomplete['files']
                               if entry['path'] == refresh.SNAPSHOT_PATH]
        with self.assertRaisesRegex(ValueError, 'publication paths'):
            validate_result(incomplete, current, self.now)

    def test_cli_can_only_choose_budget_while_preparing_a_lease(self):
        for stage in ('run', 'resume', 'abort', 'once'):
            for budget in (256, 320):
                args = ['refresh.py', stage, '--output', '/not-written.json',
                        '--rpc-call-budget', str(budget)]
                with self.subTest(stage=stage, budget=budget), \
                        patch.object(sys, 'argv', args), patch('sys.stderr', new_callable=io.StringIO), \
                        patch('refresh.collect') as collector, self.assertRaises(SystemExit) as caught:
                    refresh.main()
                self.assertEqual(caught.exception.code, 2)
                collector.assert_not_called()

    def test_prepare_lease_cli_emits_explicit_manual_cap_without_changing_inputs(self):
        with tempfile.TemporaryDirectory(prefix='lido-budget-cli-') as name:
            root = Path(name)
            (root / 'data').mkdir()
            original = refresh.canonical(self.old)
            (root / refresh.SNAPSHOT_PATH).write_text(original)
            output = root / 'result.json'
            args = ['refresh.py', 'prepare-lease', '--root', str(root), '--output', str(output),
                    '--run-id', 'catchup', '--trigger', 'manual',
                    '--manual-request-id', 'cli-request', '--rpc-call-budget', '320']
            with patch.object(sys, 'argv', args), patch('refresh.time.time', return_value=self.now), \
                    patch('builtins.print'), patch('refresh.collect') as collector:
                self.assertEqual(refresh.main(), 0)
            collector.assert_not_called()
            result = json.loads(output.read_text())
            self.assertEqual(result['stage'], 'acquire')
            self.assertEqual(len(result['files']), 1)
            self.assertEqual(lease_rpc_budget(json.loads(result['files'][0]['content'])), 320)
            self.assertEqual((root / refresh.SNAPSHOT_PATH).read_text(), original)

    def test_prepare_lease_cli_refuses_scheduled_320_and_nonchoices(self):
        for budget in ('320', '257', '321', '320.0'):
            args = ['refresh.py', 'prepare-lease', '--output', '/not-written.json',
                    '--rpc-call-budget', budget]
            with self.subTest(budget=budget), patch.object(sys, 'argv', args), \
                    patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit) as caught:
                refresh.main()
            self.assertEqual(caught.exception.code, 2)


class JournalBudgetTests(unittest.TestCase):
    setUp = journal_fixtures.DurableJournalTests.setUp
    close_all = journal_fixtures.DurableJournalTests.close_all
    options = journal_fixtures.DurableJournalTests.options
    create = journal_fixtures.DurableJournalTests.create
    reopen = journal_fixtures.DurableJournalTests.reopen
    response = journal_fixtures.DurableJournalTests.response
    query = journal_fixtures.DurableJournalTests.query

    def set_budget(self, budget, *, suffix='run'):
        snapshot, reason = refresh.propose_lease(
            {}, int(self.clock.wall), 'offline-test', trigger='manual',
            manual_request_id='offline-request', rpc_call_budget=budget)
        self.assertIsNone(reason)
        self.base[recovery.SNAPSHOT_PATH] = recovery.encoded(snapshot)
        (self.root / recovery.SNAPSHOT_PATH).write_bytes(self.base[recovery.SNAPSHOT_PATH])
        self.directory = self.root / 'worker' / suffix
        self.seal = self.root / 'controller' / (suffix + '.json')

    def authenticate(self, journal):
        target = self.summary['block_number']
        for number in (100, target, target + 1, 'finalized'):
            params = [number if number == 'finalized' else hex(number), False]
            self.response(journal, 'anchor', 'eth_getBlockByNumber', params, {})
        journal.mark_authenticated()

    def consume_anchor(self, journal):
        return self.response(journal, 'anchor', 'eth_getBlockByNumber', ['0x64', False], {})

    def test_lease_cap_is_copied_into_immutable_context_and_rpc(self):
        for budget in (256, 320):
            with self.subTest(budget=budget):
                self.set_budget(budget, suffix=str(budget))
                journal = self.create()
                self.assertEqual(journal.context['rpc_call_budget'], budget)
                self.assertEqual(journal.max_calls, budget)
                rpc = refresh.DurableRpc(journal, json.loads(self.base[recovery.STATE_PATH]), self.summary,
                                         admission_wait=advancing_wait(self.clock))
                self.assertEqual(rpc.max_calls, budget)
                context = journal.context
                context['rpc_call_budget'] = 999
                context['lease']['rpcCallBudget'] = 999
                self.assertEqual(journal.max_calls, budget)
                journal.check()
                with self.assertRaises(AttributeError):
                    journal.max_calls = 999
                with self.assertRaises(AttributeError):
                    rpc.max_calls = 999
                journal.close()

    def test_mutating_internal_context_cannot_upgrade_a_run(self):
        self.set_budget(256)
        journal = self.create()
        journal._context['rpc_call_budget'] = 320
        with self.assertRaises(recovery.Corrupt):
            journal.check()
        self.assertEqual(journal.calls, 0)

    def test_context_cap_must_match_original_lease_even_when_bytes_are_reencoded(self):
        for lease_cap, context_cap in ((256, 320), (320, 256), (320, None)):
            with self.subTest(lease_cap=lease_cap, context_cap=context_cap):
                self.set_budget(lease_cap, suffix=f'binding-{lease_cap}-{context_cap}')
                journal = self.create()
                if context_cap is None:
                    journal._context.pop('rpc_call_budget')
                else:
                    journal._context['rpc_call_budget'] = context_cap
                journal._context_bytes = recovery.encoded(journal._context)
                with self.assertRaises(recovery.Corrupt):
                    journal.check()
                journal.close()

    def test_fresh_checkout_cannot_upgrade_an_original_256_lease(self):
        self.set_budget(256)
        journal = self.create()
        journal.pause('offline interruption')
        journal.close()
        changed = json.loads(self.base[recovery.SNAPSHOT_PATH])
        changed['refresh']['lease']['rpcCallBudget'] = 320
        self.base[recovery.SNAPSHOT_PATH] = recovery.encoded(changed)
        (self.root / recovery.SNAPSHOT_PATH).write_bytes(self.base[recovery.SNAPSHOT_PATH])
        with patch('refresh.open_rpc') as wire, self.assertRaises(recovery.Corrupt):
            self.reopen()
        wire.assert_not_called()

    def test_legacy_lease_and_legacy_context_both_default_to_256(self):
        # Construct a legacy-shaped begin record in this synthetic fixture only.
        append = recovery.RunJournal._append

        def legacy_begin(journal, kind, **data):
            if kind == 'begin':
                journal._context.pop('rpc_call_budget')
                journal._context_bytes = recovery.encoded(journal._context)
                data['context'] = journal.context
            return append(journal, kind, **data)

        with patch.object(recovery.RunJournal, '_append', legacy_begin):
            journal = self.create()
        self.assertNotIn('rpc_call_budget', journal.context)
        self.assertNotIn('rpcCallBudget', journal.context['lease'])
        self.assertEqual(journal.max_calls, 256)
        journal.pause('offline interruption')
        journal.close()
        reopened = self.reopen()
        self.assertEqual(reopened.max_calls, 256)
        self.assertEqual(reopened.calls, 0)

    def test_failed_and_unknown_attempts_stay_spent_across_restart_at_both_caps(self):
        for budget in (256, 320):
            with self.subTest(budget=budget):
                self.set_budget(budget, suffix=f'spent-{budget}')
                journal = self.create()
                self.authenticate(journal)
                failed, _ = journal.reserve('historical_logs', 'eth_getLogs', self.query(journal))
                journal.fail(failed, category='rpc_limit', code=-32005)
                unknown, _ = journal.reserve('historical_logs', 'eth_getLogs', self.query(journal))
                journal.pause('offline interruption with an unknown response')
                self.assertEqual(journal.calls, 6)
                deadline = journal.deadline
                journal.close()
                journal = self.reopen()
                self.assertEqual(journal.calls, 6)
                self.assertEqual(journal.max_calls, budget)
                self.assertEqual(journal.deadline, deadline)
                self.assertIn(failed, journal.failures)
                self.assertIn(unknown, journal.diagnostics()['inFlightIds'])
                self.authenticate(journal)
                while journal.calls < budget:
                    self.consume_anchor(journal)
                with self.assertRaisesRegex(recovery.Stopped, 'budget'):
                    journal.reserve('anchor', 'eth_getBlockByNumber', ['0x64', False])
                self.assertEqual(journal.calls, budget)
                self.assertEqual(sorted(journal.requests), list(range(1, budget + 1)))
                self.assertEqual(json.loads(self.seal.read_text())['attempts'], budget)
                journal.close()
                # Read/replay an exhausted 320 ledger, but never admit a normal
                # resume without the same ten fresh authentication/finish calls.
                with self.assertRaisesRegex(recovery.Stopped, 'budget'):
                    self.reopen()
                aborted = self.reopen(abort_only=True, reviewed_same_route=None, inspect_ssz=None)
                self.assertEqual(aborted.calls, budget)
                self.assertEqual(aborted.max_calls, budget)
                aborted.close()

    def test_restart_preflight_keeps_ten_fresh_calls_reserved(self):
        for budget in (256, 320):
            for remaining in (9, 10):
                with self.subTest(budget=budget, remaining=remaining):
                    self.set_budget(budget, suffix=f'restart-{budget}-{remaining}')
                    journal = self.create()
                    while journal.calls < budget - remaining:
                        self.consume_anchor(journal)
                    journal.pause('offline interruption')
                    journal.close()
                    if remaining == 9:
                        with self.assertRaisesRegex(recovery.Stopped, 'budget'):
                            self.reopen()
                    else:
                        reopened = self.reopen()
                        self.assertEqual(reopened.calls, budget - 10)
                        reopened.close()

    def test_320_does_not_extend_the_original_deadline(self):
        self.set_budget(320)
        journal = self.create()
        self.assertEqual(journal.deadline, self.clock.monotonic + 1500)
        journal.pause('offline interruption')
        journal.close()
        self.clock.advance(1500)
        with self.assertRaisesRegex(recovery.Stopped, 'deadline'):
            self.reopen()

    def test_32_plus_227_headers_plus_six_finishing_reads_only_fits_320(self):
        self.summary.update(block_number=400, block_hash=journal_fixtures.h(400))
        for budget in (256, 320):
            with self.subTest(budget=budget):
                self.set_budget(budget, suffix=f'headers-{budget}')
                journal = self.create()
                self.authenticate(journal)
                while journal.calls < 32:
                    self.consume_anchor(journal)
                rpc = refresh.DurableRpc(journal, json.loads(self.base[recovery.STATE_PATH]), self.summary,
                                         admission_wait=advancing_wait(self.clock))
                transport = rpc_fixtures.Transport()
                numbers = list(range(101, 328))
                self.assertEqual(len(numbers), 227)
                with patch('refresh.time.monotonic', side_effect=lambda: self.clock.monotonic), \
                        patch('refresh.open_rpc', side_effect=transport):
                    if budget == 256:
                        with self.assertRaisesRegex(RuntimeError, 'budget'):
                            rpc.fetch_headers(numbers)
                        self.assertEqual(journal.calls, 32)
                        self.assertEqual(transport.requests, [])
                    else:
                        headers = rpc.fetch_headers(numbers)
                        rpc.drain()
                        self.assertEqual(set(headers), set(numbers))
                        self.assertEqual(journal.calls, 259)
                        self.assertEqual(len(transport.requests), 227)
                        self.assertLessEqual(transport.maximum, 4)
                        for address in journal.context['balance_addresses']:
                            self.response(journal, 'balance', 'eth_getBalance', [address, hex(400)], '0x1')
                        for number in (100, 400, 401):
                            self.response(journal, 'terminal', 'eth_getBlockByNumber', [hex(number), False], {})
                        self.assertEqual(journal.calls, 265)
                journal.close()


if __name__ == '__main__':
    unittest.main()
