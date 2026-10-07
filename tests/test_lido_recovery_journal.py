"""Offline fault injection for the production journal; no network or real SSZ."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/lido'))
from execution import RpcError
from recovery import (RunJournal, Busy, Corrupt, Stopped, MAX_CALLS,
                      SNAPSHOT_PATH, STATE_PATH, encoded, read_context, system_clock)


def h(number):
    return '0x' + f'{number:064x}'


class Clock:
    def __init__(self):
        self.wall, self.monotonic, self.identity = 10000.25, 100.25, 'fixture-boot/clock-1'

    def __call__(self):
        return dict(wall=self.wall, monotonic=self.monotonic, identity=self.identity)

    def advance(self, seconds):
        self.wall += seconds
        self.monotonic += seconds


class DurableJournalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='lido-journal-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'data').mkdir()
        shutil.copytree(ROOT / 'scripts/lido', self.root / 'scripts/lido',
                        ignore=shutil.ignore_patterns('__pycache__'))
        self.clock = Clock()
        snapshot = {'refresh': {'lease': {'runId': 'offline-test',
                    'acquiredAtEpoch': 10000, 'expiresAtEpoch': 12100}}}
        seed = {'block': 100, 'hash': h(100), 'timestamp': 9800}
        self.base = {SNAPSHOT_PATH: encoded(snapshot), STATE_PATH: encoded(seed)}
        for name, raw in self.base.items():
            (self.root / name).write_bytes(raw)
        self.directory = self.root / 'worker' / 'journal'
        self.seal = self.root / 'controller' / 'seal.json'
        self.path = self.root / 'original.ssz'
        self.path.write_bytes(b'SYNTHETIC ONLY: bounded local SSZ stand-in\n')
        self.summary = dict(state_root=h(300), derived_beacon_block_header_root=h(301),
                            block_number=105, block_hash=h(105), timestamp=9988,
                            state_bytes=self.path.stat().st_size,
                            state_sha256=hashlib.sha256(self.path.read_bytes()).hexdigest(),
                            ssz_field_ranges={'synthetic': (0, self.path.stat().st_size)})
        self.acquisition = dict(source_url='https://fixture.invalid/state.ssz',
                                bytes=self.summary['state_bytes'],
                                retrieval_started_timestamp=10000,
                                retrieved_timestamp=10000)
        self.journals = []
        self.addCleanup(self.close_all)

    def close_all(self):
        for journal in self.journals:
            for ident in list(journal._active_callbacks):
                journal.finish_callback(ident)
            journal.close()

    def options(self):
        return dict(root=self.root, head='1' * 40, run_id='offline-test',
                    base_blobs=self.base, endpoint='https://fixture.invalid/rpc',
                    route_id='original-controller-route', seal_path=self.seal, clock=self.clock)

    def create(self, *, source=True):
        journal = RunJournal.create(self.directory, **self.options())
        self.journals.append(journal)
        if source:
            journal.pin_source(self.path, self.acquisition, self.summary)
        return journal

    def reopen(self, **changes):
        options = self.options()
        options.update(fresh_main=True, reviewed_same_route='reviewed route fixture',
                       inspect_ssz=lambda _: deepcopy(self.summary))
        options.update(changes)
        journal = RunJournal.open(self.directory, **options)
        self.journals.append(journal)
        return journal

    def response(self, journal, role, method, params, result, *, cacheable=True):
        ident, _ = journal.reserve(role, method, params)
        journal.start_callback(ident)
        try:
            answer = journal.finish(ident, encoded({'jsonrpc': '2.0', 'id': ident, 'result': result}),
                                    lambda _: True, cacheable=cacheable)
        finally:
            journal.finish_callback(ident)
        return ident, answer

    def authenticate(self, journal):
        for number in (100, 105, 106, 'finalized'):
            params = [number if number == 'finalized' else hex(number), False]
            self.response(journal, 'anchor', 'eth_getBlockByNumber', params, {})
        journal.mark_authenticated()

    def query(self, journal):
        return [{'address': journal.context['log_addresses'], 'fromBlock': hex(101), 'toBlock': hex(105)}]

    def cached_log(self, journal):
        return journal.cached('historical_logs', 'eth_getLogs', self.query(journal), lambda value: isinstance(value, list))

    def log(self, journal, result=None, **kwargs):
        return self.response(journal, 'historical_logs', 'eth_getLogs', self.query(journal),
                             [] if result is None else result, **kwargs)

    def test_begin_is_sealed_before_download_and_missing_source_cannot_resume(self):
        journal = self.create(source=False)
        self.assertEqual(read_context(self.directory, self.seal)['run_id'], 'offline-test')
        self.assertEqual(journal.calls, 0)
        journal.pause('interrupted during SSZ download')
        journal.close()
        with self.assertRaisesRegex(Stopped, 'no sealed original SSZ'):
            self.reopen()

    def test_integer_download_timestamps_and_tuple_inspection_survive_resume(self):
        journal = self.create()
        journal.pause('controller interruption')
        journal.close()
        reopened = self.reopen()
        self.assertEqual(reopened.source['inspection']['ssz_field_ranges']['synthetic'],
                         [0, self.path.stat().st_size])
        self.assertEqual(reopened.context['start_wall'], 10000.25)

    def test_partial_record_truncation_rejected(self):
        journal = self.create()
        journal.close()
        path = self.directory / 'ledger.jsonl'
        path.write_bytes(path.read_bytes()[:-3])
        with self.assertRaises(Corrupt):
            self.reopen()
        with self.assertRaises(Corrupt):
            read_context(self.directory, self.seal)

    def test_complete_record_rollback_rejected_by_independent_seal(self):
        journal = self.create()
        journal.pause('controller interruption')
        journal.close()
        path = self.directory / 'ledger.jsonl'
        path.write_bytes(b''.join(path.read_bytes().splitlines(keepends=True)[:-1]))
        with self.assertRaisesRegex(Corrupt, 'seal mismatch'):
            self.reopen()

    def test_missing_independent_seal_rejected(self):
        journal = self.create()
        journal.close()
        self.seal.unlink()
        with self.assertRaises(Corrupt):
            self.reopen()

    def test_seal_must_be_outside_journal(self):
        options = self.options()
        options['seal_path'] = self.directory / 'unsafe-seal.json'
        with self.assertRaisesRegex(Stopped, 'outside'):
            RunJournal.create(self.directory, **options)

    def test_orphan_unsealed_raw_never_reused(self):
        journal = self.create()
        self.authenticate(journal)
        responses = self.directory / 'responses'
        responses.mkdir(exist_ok=True)
        (responses / 'orphan.json').write_bytes(encoded({'jsonrpc': '2.0', 'id': 500, 'result': []}))
        journal.pause('controller interruption')
        journal.close()
        reopened = self.reopen()
        self.authenticate(reopened)
        self.assertEqual(self.cached_log(reopened), (False, None))
        self.assertEqual(reopened.calls, 8)

    def test_sealed_raw_corruption_rejected(self):
        journal = self.create()
        self.authenticate(journal)
        ident, _ = self.log(journal)
        journal.close()
        raw = self.directory / journal.responses[ident]['file']
        raw.write_bytes(raw.read_bytes().replace(b'[]', b'{}'))
        with self.assertRaises(Corrupt):
            self.reopen()

    def test_original_ssz_corruption_rejected(self):
        journal = self.create()
        journal.close()
        self.path.write_bytes(b'changed original bytes')
        with self.assertRaisesRegex(Corrupt, 'SSZ bytes changed'):
            self.reopen()

    def test_full_ssz_inspection_must_match_on_resume(self):
        journal = self.create()
        journal.close()
        changed = {**self.summary, 'derived_beacon_block_header_root': h(302)}
        with self.assertRaisesRegex(Corrupt, 'inspection changed'):
            self.reopen(inspect_ssz=lambda _: changed)

    def test_original_clock_deadline_and_boot_never_reset(self):
        journal = self.create()
        original = journal.deadline
        journal.close()
        self.clock.advance(1499)
        reopened = self.reopen()
        self.assertEqual(reopened.deadline, original)
        reopened.close()
        self.clock.advance(1)
        with self.assertRaisesRegex(Stopped, 'deadline'):
            self.reopen()

    def test_boot_change_is_rejected(self):
        journal = self.create()
        journal.close()
        self.clock.identity = 'different-boot'
        with self.assertRaisesRegex(Stopped, 'clock'):
            self.reopen()

    def test_clock_regression_and_divergence_rejected(self):
        journal = self.create()
        for component, delta in (('wall', -1), ('monotonic', -1), ('wall', 6), ('monotonic', 6)):
            with self.subTest(component=component, delta=delta):
                setattr(self.clock, component, getattr(self.clock, component) + delta)
                with self.assertRaisesRegex(Stopped, 'clock'):
                    journal.check()
                setattr(self.clock, component, getattr(self.clock, component) - delta)

    def test_unknown_prior_process_reservation_remains_spent(self):
        journal = self.create()
        ident, _ = journal.reserve('anchor', 'eth_getBlockByNumber', [hex(100), False])
        journal.close()
        reopened = self.reopen()
        self.assertEqual(reopened.calls, 1)
        self.assertEqual(reopened.diagnostics()['inFlightIds'], [ident])
        self.authenticate(reopened)
        self.assertEqual(reopened.calls, 5)
        self.assertEqual(reopened.diagnostics()['inFlightIds'], [ident])

    def test_active_callbacks_keep_flock_and_prevent_fifth_admission(self):
        journal = self.create()
        ids = []
        for _ in range(4):
            ident, _ = journal.reserve('anchor', 'eth_getBlockByNumber', [hex(100), False])
            journal.start_callback(ident)
            ids.append(ident)
        with self.assertRaises(Stopped):
            journal.reserve('anchor', 'eth_getBlockByNumber', [hex(100), False])
        self.assertEqual(journal.calls, 4)
        with self.assertRaises(Busy):
            journal.close()
        with self.assertRaises(Busy):
            self.reopen()
        for ident in ids:
            journal.finish_callback(ident)
        journal.close()
        self.assertEqual(self.reopen().calls, 4)

    def test_source_denial_is_terminal_durable_and_no_result_cache(self):
        journal = self.create()
        ident, _ = journal.reserve('anchor', 'eth_getBlockByNumber', [hex(100), False])
        with self.assertRaises(RpcError) as caught:
            journal.finish(ident, encoded({'jsonrpc': '2.0', 'id': ident,
                                          'error': {'code': 403, 'message': 'denied'}}), lambda _: True)
        self.assertTrue(caught.exception.denied)
        self.assertTrue(journal.denied)
        self.assertEqual(journal.responses, {})
        journal.close()
        with self.assertRaises(Stopped) as caught:
            self.reopen()
        self.assertTrue(caught.exception.denied)

    def test_denial_survives_seal_write_failure_in_memory_and_exception(self):
        journal = self.create()
        ident, _ = journal.reserve('anchor', 'eth_getBlockByNumber', [hex(100), False])
        with patch.object(journal._store, 'write', side_effect=OSError('simulated disk failure')):
            with self.assertRaises(RpcError) as caught:
                journal.finish(ident, encoded({'jsonrpc': '2.0', 'id': ident,
                                              'error': {'code': 401, 'message': 'denied'}}), lambda _: True)
        self.assertTrue(caught.exception.denied)
        self.assertTrue(journal.denied)
        self.assertEqual(journal.status, 'terminal')
        journal.close()
        with self.assertRaises(Corrupt):
            self.reopen()

    def test_denial_overrides_paused_state(self):
        journal = self.create()
        ident, _ = journal.reserve('anchor', 'eth_getBlockByNumber', [hex(100), False])
        journal.pause('controller interruption')
        journal.fail(ident, category='http_error', code=403)
        self.assertTrue(journal.denied)
        self.assertEqual(journal.status, 'terminal')

    def test_corrupt_earlier_raw_cannot_hide_a_later_sealed_source_denial(self):
        journal = self.create()
        self.authenticate(journal)
        ident, _ = journal.reserve('historical_logs', 'eth_getLogs', self.query(journal))
        journal.fail(ident, category='http_error', code=403)
        raw = journal.directory / journal.responses[1]['file']
        raw.write_bytes(b'corrupt saved source response')
        journal.close()
        with self.assertRaises(Corrupt) as caught:
            self.reopen()
        self.assertTrue(caught.exception.denied)

    def test_late_complete_success_after_pause_is_retained_but_not_accepted(self):
        journal = self.create()
        self.authenticate(journal)
        ident, _ = journal.reserve('historical_logs', 'eth_getLogs', self.query(journal))
        journal.start_callback(ident)
        journal.pause('controller interruption')
        with self.assertRaisesRegex(Stopped, 'paused'):
            journal.finish(ident, encoded({'jsonrpc': '2.0', 'id': ident, 'result': []}), lambda _: True)
        journal.fail(ident, category='source_error')  # Adapter's stopped-result catch.
        self.assertEqual(journal.status, 'paused')
        journal.finish_callback(ident)
        journal.close()
        reopened = self.reopen()
        self.authenticate(reopened)
        self.assertEqual(self.cached_log(reopened), (True, []))

    def test_multiple_epochs_preserve_attempts_and_original_deadline(self):
        journal = self.create()
        deadline = journal.deadline
        for epoch in range(3):
            self.authenticate(journal)
            self.assertEqual(journal.calls, 4 * (epoch + 1))
            self.assertEqual(journal.epoch, epoch)
            journal.pause('controller interruption')
            journal.close()
            self.clock.advance(1)
            journal = self.reopen()
            self.assertEqual(journal.deadline, deadline)

    def test_unknown_transport_error_is_terminal(self):
        journal = self.create()
        ident, _ = journal.reserve('anchor', 'eth_getBlockByNumber', [hex(100), False])
        journal.fail(ident, category='unclassified_error')
        self.assertEqual(journal.status, 'terminal')

    def test_remaining_completion_budget_rejected_before_reserving(self):
        journal = self.create()
        self.authenticate(journal)
        with self.assertRaisesRegex(Stopped, 'budget'):
            journal.reserve('historical_logs', 'eth_getLogs', self.query(journal),
                            remaining_required=MAX_CALLS - journal.calls)
        self.assertEqual(journal.calls, 4)

    def test_capped_success_is_returned_but_not_reusable(self):
        journal = self.create()
        self.authenticate(journal)
        ident, answer = self.log(journal, [{}] * 10000)
        self.assertEqual(len(answer), 10000)
        self.assertFalse(journal.responses[ident]['cacheable'])
        self.assertEqual(self.cached_log(journal), (False, None))

    def test_duplicate_json_keys_and_malformed_limit_errors_fail_terminal(self):
        journal = self.create()
        self.authenticate(journal)
        ident, _ = journal.reserve('historical_logs', 'eth_getLogs', self.query(journal))
        raw = encoded({'jsonrpc': '2.0', 'id': ident, 'result': [],
                       'error': {'code': -32005, 'message': 'ambiguous invalid envelope'}})
        with self.assertRaises(Corrupt):
            journal.finish(ident, raw, lambda _: True)
        self.assertEqual(journal.status, 'terminal')
        self.assertEqual(journal.responses, {n: value for n, value in journal.responses.items() if n < ident})

    def test_duplicate_json_result_keys_are_never_success(self):
        journal = self.create()
        ident, _ = journal.reserve('anchor', 'eth_getBlockByNumber', [hex(100), False])
        raw = ('{"jsonrpc":"2.0","id":%d,"result":{},"result":{}}' % ident).encode()
        with self.assertRaises(Corrupt):
            journal.finish(ident, raw, lambda _: True)
        self.assertEqual(journal.status, 'terminal')
        self.assertEqual(journal.responses, {})

    def test_explicit_code_root_cannot_bypass_fresh_root_code_check(self):
        journal = self.create()
        journal.close()
        with (self.root / 'scripts/lido/refresh.py').open('ab') as out:
            out.write(b'\n# Changed current source\n')
        with self.assertRaisesRegex(Corrupt, 'code bytes changed'):
            self.reopen(code_root=ROOT / 'scripts/lido')

    def test_clock_namespace_compatibility_does_not_hide_access_denial(self):
        with patch('recovery.os.readlink', side_effect=FileNotFoundError('kernel lacks time namespaces')):
            self.assertTrue(system_clock()['identity'].endswith(':no-time-namespace'))
        with patch('recovery.os.readlink', side_effect=PermissionError('denied')):
            with self.assertRaisesRegex(Stopped, 'namespace unavailable'):
                system_clock()
        with patch('recovery.Path.read_text', return_value='\n'):
            with self.assertRaisesRegex(Stopped, 'boot identity is empty'):
                system_clock()

    def test_cached_response_is_semantically_revalidated(self):
        journal = self.create()
        self.authenticate(journal)
        self.log(journal)
        with self.assertRaises(Corrupt):
            journal.cached('historical_logs', 'eth_getLogs', self.query(journal), lambda _: False)
        self.assertEqual(journal.status, 'terminal')

    def test_abort_only_needs_no_review_or_source_and_cannot_acquire(self):
        journal = self.create(source=False)
        journal.close()
        self.clock.advance(1600)
        aborted = self.reopen(abort_only=True, reviewed_same_route=None, inspect_ssz=None)
        with self.assertRaisesRegex(Stopped, 'abort only'):
            aborted.check()
        aborted.stop('controller abort')
        self.assertEqual(aborted.status, 'terminal')

    def test_cannot_complete_without_fresh_balances_and_terminal_sandwich(self):
        journal = self.create()
        self.authenticate(journal)
        with self.assertRaisesRegex(Stopped, 'terminal rereads'):
            journal.complete()
        self.assertEqual(journal.status, 'collecting')


if __name__ == '__main__':
    unittest.main()
