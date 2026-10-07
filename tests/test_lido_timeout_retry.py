"""Offline bounded timeout recovery: real journal, replay, and worker gates.

All transports and clocks are injected; no source or remote operation is used.
"""
from copy import deepcopy
import io
import json
import threading
import unittest
from unittest.mock import patch
import urllib.error

import execution as ex
import refresh
from acquisition_errors import transport_read_timeout
from recovery import Corrupt, Stopped, encoded, sha
import test_lido_recovery_integration as integration
from rpc_clock import advancing_wait
from test_execution_hourly import FakeRpc, h
from test_lido_rpc_concurrency import Transport


def read_timeout():
    return TimeoutError('The read operation timed out')


class TimeoutClassificationTests(unittest.TestCase):
    def test_only_direct_observed_read_timeout_in_transport_phases_is_eligible(self):
        for phase in ('open', 'read'):
            self.assertTrue(transport_read_timeout(read_timeout(), phase))
        for phase in ('response', 'envelope', 'validation', 'anchor', 'cache'):
            self.assertFalse(transport_read_timeout(read_timeout(), phase))
        class SimilarTimeout(TimeoutError):
            pass
        errors = [TimeoutError('timed out'), TimeoutError('bounded RPC deadline exhausted'),
                  SimilarTimeout('The read operation timed out'),
                  RuntimeError('The read operation timed out'),
                  urllib.error.URLError(read_timeout()), ConnectionResetError('reset')]
        for error in errors:
            for phase in ('open', 'read'):
                self.assertFalse(transport_read_timeout(error, phase), repr(error))

    def test_old_timeout_causes_contexts_wrappers_and_statuses_never_grant_retry(self):
        for edge in ('__cause__', '__context__'):
            for error in (RuntimeError('different error'), urllib.error.URLError('different error')):
                setattr(error, edge, read_timeout())
                self.assertFalse(transport_read_timeout(error, 'open'))
        for cause in (read_timeout(), RuntimeError('old failure'),
                      urllib.error.HTTPError(refresh.RPC_URL, 403, 'denied', {}, None)):
            error = read_timeout()
            error.__cause__ = cause
            self.assertFalse(transport_read_timeout(error, 'open'))
        for code in (403, 429):
            error = read_timeout()
            error.__context__ = urllib.error.HTTPError(refresh.RPC_URL, code, 'source failed', {}, None)
            self.assertFalse(transport_read_timeout(error, 'open'))
        for code in (301, 302, 401, 403, 429, 500):
            error = urllib.error.HTTPError(refresh.RPC_URL, code, 'The read operation timed out', {}, None)
            error.__cause__ = read_timeout()
            result = refresh.classify_acquisition_error(error, None, 'open')
            self.assertNotEqual(result['category'], 'transport_read_timeout')
        first = read_timeout()
        self.assertEqual(refresh.classify_acquisition_error(first, first, 'read')['category'], 'cancelled')


class BoundedTimeoutRetryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = integration.RecoveryIntegrationTests('test_unapproved_method_never_reaches_transport_or_spends_budget')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.journal = self.fixture.create()
        self.rpc = self.new_rpc(self.journal)
        self.fake = FakeRpc(self.fixture.logs)

    def new_rpc(self, journal):
        return refresh.DurableRpc(journal, self.fixture.state, self.fixture.summary,
                                  admission_wait=advancing_wait(self.fixture.clock))

    def authenticate(self, rpc=None):
        rpc = rpc or self.rpc
        with patch('refresh.open_rpc', side_effect=self.fixture.transport()):
            for number in ('0x64', '0x69', '0x6a', 'finalized'):
                rpc('eth_getBlockByNumber', [number, False])

    def grant_count(self):
        return len(self.journal.diagnostics()['timeoutRetries'])

    def assert_pacing_and_seal(self):
        rows = [r for r in self.journal.records if r['kind'] == 'reserve']
        self.assertTrue(all(b['stamp']['monotonic'] - a['stamp']['monotonic'] >= 2
                            for a, b in zip(rows, rows[1:])))
        failures = {r['data']['id']: r for r in self.journal.records
                    if r['kind'] == 'failure' and 'timeoutRetry' in r['data']}
        retries = [r for r in rows if 'timeoutRetryOf' in r['data']]
        for row in retries:
            prior = failures[row['data']['timeoutRetryOf']]
            self.assertGreaterEqual(row['stamp']['monotonic'] - prior['stamp']['monotonic'], 5)
            old = self.journal.requests[prior['data']['id']]['request']
            self.assertEqual([old['method'], old['params']],
                             [row['data']['request']['method'], row['data']['request']['params']])
        self.assertEqual(json.loads(self.fixture.seal_path.read_text()), self.journal.seal)
        self.assertEqual(self.journal.seal['attempts'], self.journal.calls)
        self.assertLessEqual(len(retries), 3)

    def test_one_timeout_replays_identically_and_preserves_exact_fresh_suffix(self):
        failed = False
        def respond(request):
            nonlocal failed
            if request['method'] == 'eth_getBlockByNumber' and request['params'] == ['0x65', False] and not failed:
                failed = True
                raise read_timeout()
            return self.fake(request['method'], request['params'])
        transport = Transport(respond)
        with patch('refresh.open_rpc', side_effect=transport):
            result = ex.collect_at(self.fixture.state, 105, h(105), self.rpc)
        expected = ex.collect_at(self.fixture.state, 105, h(105), FakeRpc(self.fixture.logs))
        self.assertEqual(integration.comparable(result), integration.comparable(expected))
        self.fixture.assert_fresh_suffix(transport.requests)
        self.assertEqual(self.grant_count(), 1)
        self.assertEqual(len(self.journal.failures), 1)
        self.assertEqual(len(self.journal.responses) + 1, self.journal.calls)
        self.assertEqual(set(transport.urls), {refresh.RPC_URL})
        self.assertEqual(self.rpc._in_flight, 0)
        self.assertFalse(self.journal._active_callbacks)
        self.journal.complete()
        self.assertEqual(self.journal.status, 'complete')
        self.assert_pacing_and_seal()
        for name, content in self.fixture.base.items():
            self.assertEqual((self.fixture.root / name).read_bytes(), content)

    def test_read_phase_timeout_discards_partial_body_and_retries_fresh_envelope(self):
        self.authenticate()
        valid = self.fixture.transport()
        calls = []
        class PartialResponse(io.BytesIO):
            def read1(self, size=-1):
                if self.tell() == 0:
                    return super().read1(4)
                raise read_timeout()
        def wire(request, **kwargs):
            calls.append(json.loads(request.data))
            if len(calls) == 1:
                return PartialResponse(b'{"jsonrpc":"2.0"')
            return valid(request, **kwargs)
        with patch('refresh.open_rpc', side_effect=wire):
            answer = self.rpc.fetch_headers([101])
        self.assertEqual(answer[101]['number'], '0x65')
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.journal.failures[5]['phase'], 'read')
        self.assertNotIn(5, self.journal.responses)
        self.assert_pacing_and_seal()

    def test_second_timeout_for_identical_request_is_terminal_and_cannot_resume(self):
        self.authenticate()
        transport = Transport(lambda _: (_ for _ in ()).throw(read_timeout()))
        with patch('refresh.open_rpc', side_effect=transport), self.assertRaises(TimeoutError):
            self.rpc.fetch_headers([101])
        self.rpc.drain()
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(self.grant_count(), 1)
        self.assertEqual(self.journal.status, 'terminal')
        self.assertEqual(self.journal.calls, 6)
        self.journal.close()
        with self.assertRaises(Stopped), patch('refresh.open_rpc') as wire:
            self.fixture.reopen()
        wire.assert_not_called()

    def test_three_distinct_retries_succeed_and_fourth_timeout_is_terminal(self):
        self.authenticate()
        counts = {}
        def respond(request):
            key = request['params'][0]
            counts[key] = counts.get(key, 0) + 1
            if counts[key] == 1:
                raise read_timeout()
            return self.fake(request['method'], request['params'])
        transport = Transport(respond)
        with patch('refresh.open_rpc', side_effect=transport):
            for number in (101, 102, 103):
                self.rpc.fetch_headers([number])
            with self.assertRaises(TimeoutError):
                self.rpc.fetch_headers([104])
        self.rpc.drain()
        self.assertEqual(counts, {'0x65': 2, '0x66': 2, '0x67': 2, '0x68': 1})
        self.assertEqual(self.grant_count(), 3)
        self.assertEqual(self.journal.calls, 11)
        self.assertEqual(self.journal.status, 'terminal')
        self.assert_pacing_and_seal()

    def test_timeout_retries_do_not_apply_to_anchor_balance_terminal_or_logs(self):
        targets = [('anchor', 'eth_getBlockByNumber', ['0x64', False]),
                   ('terminal', 'eth_getBlockByNumber', ['0x64', False]),
                   ('balance', 'eth_getBalance', [ex.ADDRESSES['core'], '0x69']),
                   ('historical_logs', 'eth_getLogs', [{'address': self.journal.context['log_addresses'],
                                                       'fromBlock': '0x65', 'toBlock': '0x69'}])]
        for index, (role, method, params) in enumerate(targets):
            with self.subTest(role=role):
                if index:
                    self.fixture.directory = self.fixture.root / f'role-{index}'
                    self.fixture.seal_path = self.fixture.root / f'role-{index}.seal.json'
                    self.journal = self.fixture.create()
                    self.rpc = self.new_rpc(self.journal)
                if role != 'anchor':
                    self.authenticate()
                transport = Transport(lambda _: (_ for _ in ()).throw(read_timeout()))
                with patch('refresh.open_rpc', side_effect=transport), self.assertRaises(TimeoutError):
                    self.rpc(method, params)
                self.assertEqual(len(transport.requests), 1)
                self.assertEqual(self.grant_count(), 0)
                self.assertEqual(self.journal.status, 'terminal')

    def test_cooldown_uses_original_deadline_and_cannot_spend_completion_reserve(self):
        self.authenticate()
        with patch('refresh.open_rpc', side_effect=self.fixture.transport()):
            while self.journal.calls < 249:
                self.rpc('eth_getBlockByNumber', ['0x64', False])
        transport = Transport(lambda _: (_ for _ in ()).throw(read_timeout()))
        with patch('refresh.open_rpc', side_effect=transport), self.assertRaises(TimeoutError):
            self.rpc.fetch_headers([101])
        self.rpc.drain()
        self.assertEqual(self.journal.calls, 250)
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(self.grant_count(), 0)
        self.assertEqual(self.journal.status, 'terminal')

    def test_no_retry_grant_when_less_than_full_cooldown_remains(self):
        self.authenticate()
        def respond(_):
            self.fixture.clock.advance(self.journal.remaining_seconds - 4)
            raise read_timeout()
        transport = Transport(respond)
        with patch('refresh.open_rpc', side_effect=transport), self.assertRaises(TimeoutError):
            self.rpc.fetch_headers([101])
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(self.grant_count(), 0)
        self.assertEqual(self.journal.status, 'terminal')

    def test_deadline_expiring_during_cooldown_prevents_retry_admission(self):
        self.authenticate()
        auto_wait = advancing_wait(self.fixture.clock)
        deadline = self.journal.deadline
        def admission_wait(condition, seconds):
            if self.grant_count():
                self.fixture.clock.advance(self.journal.remaining_seconds + 1)
                condition.wait(0)
            else:
                auto_wait(condition, seconds)
        self.rpc._admission_wait = admission_wait
        transport = Transport(lambda _: (_ for _ in ()).throw(read_timeout()))
        with patch('refresh.open_rpc', side_effect=transport), self.assertRaises(TimeoutError):
            self.rpc.fetch_headers([101])
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(self.journal.calls, 5)
        self.assertEqual(self.journal.deadline, deadline)
        self.assertEqual(self.grant_count(), 1)
        self.journal.close()
        with self.assertRaises(Stopped):
            self.fixture.reopen()

    def test_statuses_wrappers_and_invalid_data_are_terminal_without_retry(self):
        faults = [(f'http-{code}', urllib.error.HTTPError(refresh.RPC_URL, code, 'source failed', {}, None))
                  for code in (301, 401, 403, 429, 500)]
        faults += [('wrapper', urllib.error.URLError(read_timeout())),
                   ('reset', ConnectionResetError('reset')), ('bad-json', b'not JSON'),
                   ('bad-hash', {'number': '0x65', 'timestamp': '0x1', 'hash': 'bad'}),
                   ('bad-number', {'number': '0x66', 'timestamp': '0x1', 'hash': h(102)})]
        for index, (name, fault) in enumerate(faults):
            with self.subTest(fault=name):
                if index:
                    self.fixture.directory = self.fixture.root / f'fault-{index}'
                    self.fixture.seal_path = self.fixture.root / f'fault-{index}.seal.json'
                    self.journal = self.fixture.create()
                    self.rpc = self.new_rpc(self.journal)
                self.authenticate()
                requests = []
                def wire(request, **kwargs):
                    payload = json.loads(request.data)
                    requests.append(payload)
                    if isinstance(fault, BaseException):
                        raise fault
                    raw = fault if isinstance(fault, bytes) else json.dumps(
                        {'jsonrpc': '2.0', 'id': payload['id'], 'result': fault}).encode()
                    return io.BytesIO(raw)
                with patch('refresh.open_rpc', side_effect=wire), self.assertRaises(Exception):
                    self.rpc.fetch_headers([101])
                self.rpc.drain()
                self.assertEqual(len(requests), 1)
                self.assertEqual(self.grant_count(), 0)
                self.assertEqual(self.journal.status, 'terminal')

    def test_valid_concurrent_peers_survive_timeout_and_slot_unwinds_before_retry(self):
        self.authenticate()
        entered, granted = threading.Barrier(4), threading.Event()
        original = self.journal.retry_timeout
        failed_id = []
        def grant(ident, error, phase):
            answer = original(ident, error, phase)
            if answer:
                failed_id.append(ident)
                granted.set()
            return answer
        seen = set()
        def respond(request):
            number = int(request['params'][0], 16)
            if number not in seen:
                seen.add(number)
                entered.wait(timeout=5)
                if number == 101:
                    raise read_timeout()
                self.assertTrue(granted.wait(5))
            else:
                self.assertNotIn(failed_id[0], self.journal._active_callbacks)
                self.assertNotIn(failed_id[0], self.rpc._callbacks)
                self.assertLessEqual(self.rpc._in_flight, 4)
            return self.fake(request['method'], request['params'])
        transport = Transport(respond)
        with patch.object(self.journal, 'retry_timeout', side_effect=grant), patch('refresh.open_rpc', side_effect=transport):
            answer = self.rpc.fetch_headers([101, 102, 103, 104])
        self.assertEqual(set(answer), {101, 102, 103, 104})
        self.assertEqual(len(transport.requests), 5)
        self.assertEqual(self.journal.status, 'collecting')
        self.assertIsNone(self.rpc._error)
        self.assertLessEqual(transport.maximum, 4)
        self.assert_pacing_and_seal()

    def test_denial_during_cooldown_stops_retry_admission(self):
        self.authenticate()
        entered, cooling = threading.Barrier(2), threading.Event()
        auto_wait = advancing_wait(self.fixture.clock)
        def admission_wait(condition, seconds):
            if self.grant_count():
                cooling.set()
                if not condition.wait(5):
                    raise AssertionError('denial never woke retry cooldown')
            else:
                auto_wait(condition, seconds)
        self.rpc._admission_wait = admission_wait
        def respond(request):
            entered.wait(timeout=5)
            if request['params'][0] == '0x65':
                raise read_timeout()
            self.assertTrue(cooling.wait(5))
            raise urllib.error.HTTPError(refresh.RPC_URL, 403, 'denied', {}, None)
        transport = Transport(respond)
        with patch('refresh.open_rpc', side_effect=transport), self.assertRaises(RuntimeError):
            try:
                self.rpc.fetch_headers([101, 102])
            finally:
                self.rpc.drain()
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(self.journal.calls, 6)
        self.assertTrue(self.journal.denied)
        self.assertEqual(self.journal.status, 'terminal')
        self.assertEqual(self.grant_count(), 1)
        self.assertIsNone(next(iter(self.journal.diagnostics()['timeoutRetries'].values()))['retry_id'])

    def test_late_timeout_after_denial_cannot_claim_retry(self):
        self.authenticate()
        entered, denied = threading.Barrier(2), threading.Event()
        original = self.rpc.cancel
        def cancel(error=None):
            answer = original(error)
            if getattr(answer, 'denied', False):
                denied.set()
            return answer
        def respond(request):
            entered.wait(timeout=5)
            if request['params'][0] == '0x65':
                self.assertTrue(denied.wait(5))
                raise read_timeout()
            raise urllib.error.HTTPError(refresh.RPC_URL, 403, 'denied', {}, None)
        transport = Transport(respond)
        with patch.object(self.rpc, 'cancel', side_effect=cancel), patch('refresh.open_rpc', side_effect=transport):
            with self.assertRaises(RuntimeError):
                try:
                    self.rpc.fetch_headers([101, 102])
                finally:
                    self.rpc.drain()
        self.assertTrue(self.journal.denied)
        self.assertEqual(self.grant_count(), 0)
        self.assertEqual(len(transport.requests), 2)

    def test_interruption_during_cooldown_restores_pending_allowance_and_deadline(self):
        self.authenticate()
        deadline = self.journal.deadline
        auto_wait = advancing_wait(self.fixture.clock)
        def interrupt(condition, seconds):
            if self.grant_count():
                self.journal.pause('offline controller interruption during cooldown')
                raise refresh.RecoveryPaused('offline controller interruption')
            auto_wait(condition, seconds)
        self.rpc._admission_wait = interrupt
        transport = Transport(lambda _: (_ for _ in ()).throw(read_timeout()))
        with patch('refresh.open_rpc', side_effect=transport), self.assertRaises(refresh.RecoveryPaused):
            self.rpc.fetch_headers([101])
        self.rpc.drain()
        self.assertEqual(self.journal.calls, 5)
        original = deepcopy(self.journal.diagnostics()['timeoutRetries'])
        self.journal.close()
        self.journal = self.fixture.reopen()
        self.rpc = self.new_rpc(self.journal)
        self.assertEqual(self.journal.diagnostics()['timeoutRetries'], original)
        self.assertEqual(self.journal.deadline, deadline)
        self.authenticate()
        with patch('refresh.open_rpc', side_effect=self.fixture.transport()):
            self.rpc.fetch_headers([101])
        self.assertEqual(self.journal.calls, 10)
        retry = next(iter(self.journal.diagnostics()['timeoutRetries'].values()))
        self.assertEqual((retry['id'], retry['retry_id']), (5, 10))
        self.assert_pacing_and_seal()

    def test_consumed_retry_cannot_reset_after_reviewed_same_run_resume(self):
        self.authenticate()
        count = 0
        def respond(_):
            nonlocal count
            count += 1
            if count == 1:
                raise read_timeout()
            raise urllib.error.URLError(OSError('Tunnel connection failed: 403 Forbidden'))
        with patch('refresh.open_rpc', side_effect=Transport(respond)), self.assertRaises(refresh.RecoveryPaused):
            self.rpc.fetch_headers([101])
        self.rpc.drain()
        self.assertEqual(self.journal.status, 'paused')
        self.journal.close()
        self.journal = self.fixture.reopen()
        self.rpc = self.new_rpc(self.journal)
        self.authenticate()
        transport = Transport(lambda _: (_ for _ in ()).throw(read_timeout()))
        with patch('refresh.open_rpc', side_effect=transport), self.assertRaises(Stopped):
            self.rpc.fetch_headers([101])
        self.rpc.drain()
        self.assertEqual(len(transport.requests), 0)
        self.assertEqual(self.grant_count(), 1)
        self.assertEqual(self.journal.calls, 10)
        self.assertIsInstance(self.rpc._error, Stopped)

    def test_unknown_admitted_retry_cannot_turn_into_a_third_request_after_resume(self):
        self.authenticate()
        ident, _ = self.journal.reserve('event_header', 'eth_getBlockByNumber', ['0x65', False])
        self.journal.start_callback(ident)
        self.assertTrue(self.journal.retry_timeout(ident, read_timeout(), 'open'))
        self.journal.finish_callback(ident)
        self.fixture.clock.advance(5)
        retry_id, _ = self.journal.reserve('event_header', 'eth_getBlockByNumber', ['0x65', False])
        self.assertEqual((ident, retry_id), (5, 6))
        # Simulate termination after durable admission but before a known result.
        self.journal.close()
        self.journal = self.fixture.reopen()
        self.rpc = self.new_rpc(self.journal)
        self.authenticate()
        with patch('refresh.open_rpc') as wire, self.assertRaises(Stopped):
            self.rpc.fetch_headers([101])
        wire.assert_not_called()
        self.assertEqual(self.journal.calls, 10)
        self.assertEqual(self.journal.diagnostics()['inFlightIds'], [6])
        forged = deepcopy(next(row for row in self.journal.records
                               if row['kind'] == 'reserve' and row['data']['request']['id'] == 6))
        forged['data']['request']['id'] = 11
        forged['data']['requestSha256'] = sha(encoded(forged['data']['request']))
        forged['data']['epoch'] = self.journal.epoch
        forged['data'].pop('timeoutRetryOf')
        forged['stamp'] = self.fixture.clock()
        with self.assertRaisesRegex(Corrupt, 'already admitted'):
            self.journal._apply(forged)

    def test_run_retry_cap_survives_reviewed_same_run_resume(self):
        self.authenticate()
        counts = {}
        def respond(request):
            key = request['params'][0]
            counts[key] = counts.get(key, 0) + 1
            if counts[key] == 1:
                raise read_timeout()
            return self.fake(request['method'], request['params'])
        transport = Transport(respond)
        with patch('refresh.open_rpc', side_effect=transport):
            for number in (101, 102, 103):
                self.rpc.fetch_headers([number])
        self.journal.pause('offline interruption after three completed retries')
        self.journal.close()
        self.journal = self.fixture.reopen()
        self.rpc = self.new_rpc(self.journal)
        self.authenticate()
        self.assertEqual(self.grant_count(), 3)
        with patch('refresh.open_rpc', side_effect=transport):
            # Successful retry bytes may be revalidated/reused without a third read.
            self.rpc.fetch_headers([101, 102, 103])
            with self.assertRaises(TimeoutError):
                self.rpc.fetch_headers([104])
        self.assertEqual(counts, {'0x65': 2, '0x66': 2, '0x67': 2, '0x68': 1})
        self.assertEqual(self.journal.status, 'terminal')

    def test_unsealed_allowance_never_dispatches_retry(self):
        self.authenticate()
        original = self.journal._store.write
        def seal(value):
            if self.journal.records[-1]['data'].get('timeoutRetry'):
                raise OSError('offline independent seal write failed')
            original(value)
        transport = Transport(lambda _: (_ for _ in ()).throw(read_timeout()))
        with patch.object(self.journal._store, 'write', side_effect=seal), patch('refresh.open_rpc', side_effect=transport):
            with self.assertRaises(OSError):
                self.rpc.fetch_headers([101])
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(self.journal.calls, 5)
        with self.assertRaises(Stopped):
            self.journal.check()
        self.journal.close()
        with self.assertRaises(Corrupt):
            self.fixture.reopen()


if __name__ == '__main__':
    unittest.main()
