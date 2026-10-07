"""Offline clock/concurrency checks for the one shared production RPC pace."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import io
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/lido'))
import execution as ex
import refresh
import test_lido_recovery_integration as integration
from recovery import Stopped


class Clock:
    def __init__(self):
        self.now = 100.0
        self.waits = []
        self.waiters = set()
        self.changed = threading.Condition()

    def __call__(self):
        return self.now

    def auto_wait(self, condition, seconds):
        self.waits.append(seconds)
        self.now += seconds
        condition.wait(0)  # Exercise the same release/reacquire boundary.

    def manual_wait(self, condition, seconds):
        ident = threading.get_ident()
        with self.changed:
            self.waits.append(seconds)
            self.waiters.add(ident)
            self.changed.notify_all()
        try:
            if not condition.wait(5):
                raise AssertionError('offline admission was never woken')
        finally:
            with self.changed:
                self.waiters.discard(ident)
                self.changed.notify_all()

    def wait_for_waiters(self, count):
        with self.changed:
            if not self.changed.wait_for(lambda: len(self.waiters) >= count, 3):
                raise AssertionError(f'expected {count} pacing waiters; got {len(self.waiters)}')

    def advance(self, rpc, seconds):
        with rpc._admission:
            self.now += seconds
            rpc._admission.notify_all()


class Wire:
    def __init__(self, clock, respond=None):
        self.clock, self.respond = clock, respond
        self.starts, self.requests, self.timeouts = [], [], []
        self.changed = threading.Condition()
        self.active = self.maximum = 0

    def __call__(self, request, *, timeout):
        payload = json.loads(request.data)
        with self.changed:
            self.starts.append(self.clock())
            self.requests.append(payload)
            self.timeouts.append(timeout)
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            self.changed.notify_all()
        try:
            value = self.respond(payload) if self.respond else '0x1'
            if not isinstance(value, dict) or 'jsonrpc' not in value:
                value = {'jsonrpc': '2.0', 'id': payload['id'], 'result': value}
            return io.BytesIO(json.dumps(value).encode())
        finally:
            with self.changed:
                self.active -= 1
                self.changed.notify_all()

    def wait_for_calls(self, count):
        with self.changed:
            if not self.changed.wait_for(lambda: len(self.requests) >= count, 3):
                raise AssertionError(f'expected {count} wire calls; got {len(self.requests)}')


def call(rpc):
    return rpc('eth_getBalance', [ex.ADDRESSES['core'], '0x65'])


class RpcPacingTests(unittest.TestCase):
    def test_all_methods_share_spacing_and_idle_time_earns_no_burst_credit(self):
        clock = Clock()
        rpc = refresh.Rpc(1600, clock=clock, admission_wait=clock.auto_wait)
        wire = Wire(clock)
        with patch('refresh.open_rpc', side_effect=wire):
            for method in ('eth_getBlockByNumber', 'eth_getLogs', 'eth_getBalance'):
                rpc(method, [])
            clock.now += 40
            call(rpc)
            call(rpc)
        self.assertEqual(wire.starts, [100, 102, 104, 144, 146])
        self.assertEqual(clock.waits, [2, 2, 2])
        self.assertEqual(rpc.calls, 5)
        self.assertEqual(rpc._in_flight, 0)

    def test_spurious_wake_cannot_admit_early(self):
        clock = Clock()
        wakes = []

        def wake(condition, seconds):
            wakes.append(seconds)
            if len(wakes) > 1:
                clock.auto_wait(condition, seconds)

        rpc = refresh.Rpc(1600, clock=clock, admission_wait=wake)
        wire = Wire(clock)
        with patch('refresh.open_rpc', side_effect=wire):
            call(rpc)
            call(rpc)
        self.assertEqual(wakes, [2, 2])
        self.assertEqual(wire.starts, [100, 102])

    def test_competing_workers_admit_only_one_after_idle_or_wake(self):
        clock = Clock()
        rpc = refresh.Rpc(1600, clock=clock, admission_wait=clock.manual_wait)
        wire = Wire(clock)
        with patch('refresh.open_rpc', side_effect=wire), ThreadPoolExecutor(max_workers=8) as pool:
            call(rpc)
            futures = [pool.submit(call, rpc) for _ in range(8)]
            try:
                clock.wait_for_waiters(8)
                clock.advance(rpc, 40)
                wire.wait_for_calls(2)
                clock.wait_for_waiters(7)
                self.assertEqual(rpc.calls, 2)
                clock.advance(rpc, 2)
                wire.wait_for_calls(3)
                clock.wait_for_waiters(6)
                self.assertEqual(rpc.calls, 3)
            finally:
                rpc.cancel()
            for future in futures:
                try:
                    future.result(2)
                except RuntimeError:
                    pass
        self.assertEqual(wire.starts, [100, 140, 142])
        self.assertEqual(rpc.calls, 3)

    def test_four_actual_calls_remain_the_global_in_flight_limit(self):
        clock, release = Clock(), threading.Event()
        rpc = refresh.Rpc(1600, clock=clock, admission_wait=clock.manual_wait)

        def respond(_request):
            if not release.wait(5):
                raise AssertionError('wire gate was not released')
            return '0x1'

        wire = Wire(clock, respond)
        with patch('refresh.open_rpc', side_effect=wire), ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(call, rpc) for _ in range(8)]
            try:
                wire.wait_for_calls(1)
                for count in range(2, 5):
                    clock.advance(rpc, 2)
                    wire.wait_for_calls(count)
                clock.advance(rpc, 40)
                with self.assertRaises(FutureTimeout):
                    futures[-1].result(.05)
                self.assertEqual(rpc.calls, 4)
                self.assertEqual(wire.maximum, 4)
            finally:
                rpc.cancel()
                release.set()
            for future in futures:
                with self.assertRaises(RuntimeError):
                    future.result(2)
        self.assertEqual(len(wire.requests), 4)
        self.assertEqual(rpc._in_flight, 0)

    def test_cancel_403_and_429_wake_waiters_without_new_admission_or_retry(self):
        for stop in ('cancel', 403, 429):
            with self.subTest(stop=stop):
                clock, release = Clock(), threading.Event()
                rpc = refresh.Rpc(1600, clock=clock, admission_wait=clock.manual_wait)

                def respond(_request):
                    if not release.wait(5):
                        raise AssertionError('wire gate was not released')
                    if stop != 'cancel':
                        raise urllib.error.HTTPError(refresh.RPC_URL, stop, 'stop', {}, None)
                    return '0x1'

                wire = Wire(clock, respond)
                with patch('refresh.open_rpc', side_effect=wire), ThreadPoolExecutor(max_workers=4) as pool:
                    first = pool.submit(call, rpc)
                    wire.wait_for_calls(1)
                    pending = [pool.submit(call, rpc) for _ in range(3)]
                    try:
                        clock.wait_for_waiters(3)
                        if stop == 'cancel':
                            rpc.cancel()
                        else:
                            release.set()
                        for future in pending:
                            with self.assertRaises(RuntimeError):
                                future.result(2)
                    finally:
                        release.set()
                    with self.assertRaises(RuntimeError):
                        first.result(2)
                    with self.assertRaises(RuntimeError):
                        call(rpc)
                self.assertEqual(clock.now, 100)
                self.assertEqual(rpc.calls, 1)
                self.assertEqual(len(wire.requests), 1)
                self.assertEqual(len(rpc.failures), 1)
                if stop != 'cancel':
                    self.assertEqual(rpc.failures[0]['code'], stop)

    def test_pacing_wait_spends_original_deadline_without_charging_waiters(self):
        clock = Clock()
        rpc = refresh.Rpc(101, clock=clock, admission_wait=clock.auto_wait)
        wire = Wire(clock)
        with patch('refresh.open_rpc', side_effect=wire):
            call(rpc)
            with self.assertRaises(TimeoutError):
                call(rpc)
        self.assertEqual(clock.waits, [1])
        self.assertEqual(clock.now, rpc.deadline)
        self.assertEqual(wire.timeouts, [1])
        self.assertEqual(rpc.calls, 1)
        self.assertEqual(len(wire.requests), 1)

    def test_socket_timeout_is_recomputed_after_request_preparation(self):
        clock = Clock()
        rpc = refresh.Rpc(110, clock=clock, admission_wait=clock.auto_wait)
        wire = Wire(clock)
        request_class = refresh.urllib.request.Request

        def prepare(*args, **kwargs):
            clock.now += 3
            return request_class(*args, **kwargs)

        with patch('refresh.urllib.request.Request', side_effect=prepare), \
                patch('refresh.open_rpc', side_effect=wire):
            call(rpc)
        self.assertEqual(wire.timeouts, [7])
        self.assertEqual(rpc.calls, 1)

    def test_failed_log_attempt_is_charged_and_next_split_still_waits(self):
        clock = Clock()
        rpc = refresh.Rpc(1600, clock=clock, admission_wait=clock.auto_wait)

        def respond(request):
            if request['id'] == 1:
                return {'jsonrpc': '2.0', 'id': 1, 'error': {'code': -32005}}
            return []

        wire = Wire(clock, respond)
        with patch('refresh.open_rpc', side_effect=wire):
            with self.assertRaises(ex.RpcError):
                rpc('eth_getLogs', [])
            self.assertEqual(rpc('eth_getLogs', []), [])
        self.assertEqual(wire.starts, [100, 102])
        self.assertEqual(rpc.calls, 2)
        self.assertEqual(len(rpc.failures), 1)

    def test_original_default_budget_still_counts_every_admission(self):
        clock = Clock()
        rpc = refresh.Rpc(1600, clock=clock, admission_wait=clock.auto_wait)
        wire = Wire(clock)
        with patch('refresh.open_rpc', side_effect=wire):
            for _ in range(256):
                call(rpc)
            with self.assertRaisesRegex(RuntimeError, 'budget'):
                call(rpc)
        self.assertEqual(wire.starts, list(range(100, 612, 2)))
        self.assertEqual(rpc.calls, 256)
        self.assertEqual(len(clock.waits), 255)


class DurablePacingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = integration.RecoveryIntegrationTests(
            'test_unapproved_method_never_reaches_transport_or_spends_budget')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.clock = self.fixture.clock
        self.waits = []

    def auto_wait(self, condition, seconds):
        self.waits.append(seconds)
        self.clock.advance(seconds)
        condition.wait(0)

    def rpc(self, journal):
        return refresh.DurableRpc(journal, self.fixture.state, self.fixture.summary,
                                  admission_wait=self.auto_wait)

    def test_replay_and_final_fresh_reads_share_one_durable_pace(self):
        journal = self.fixture.create()
        rpc = self.rpc(journal)
        transport = self.fixture.transport(limit_ranges=True)
        with patch('refresh.open_rpc', side_effect=transport):
            ex.collect_at(self.fixture.state, 105, integration.h(105), rpc)
        stamps = [row['stamp']['monotonic'] for row in journal.records if row['kind'] == 'reserve']
        self.assertEqual(len(stamps), journal.calls)
        self.assertTrue(all(b - a >= 2 for a, b in zip(stamps, stamps[1:])))
        self.assertEqual(len(self.waits), journal.calls - 1)
        self.assertEqual(journal.last_admission_monotonic, stamps[-1])
        self.fixture.assert_fresh_suffix(transport.requests)
        self.assertEqual(journal.deadline, journal.context['start_monotonic'] + 1500)

    def test_resume_uses_latest_unknown_admission_and_original_deadline(self):
        journal = self.fixture.create()
        rpc = self.rpc(journal)
        with patch('refresh.open_rpc', side_effect=self.fixture.transport()):
            rpc('eth_getBlockByNumber', ['0x64', False])
        self.clock.advance(2)
        unknown, _ = journal.reserve('anchor', 'eth_getBlockByNumber', ['0x69', False])
        latest = journal.last_admission_monotonic
        deadline = journal.deadline
        journal.pause('offline interruption after charged unknown admission')
        journal.close()
        self.clock.advance(.5)
        journal = self.fixture.reopen()
        resumed = self.rpc(journal)
        with patch('refresh.open_rpc', side_effect=self.fixture.transport()):
            resumed('eth_getBlockByNumber', ['0x64', False])
        self.assertEqual(self.waits, [1.5])
        self.assertEqual(journal.last_admission_monotonic, latest + 2)
        self.assertEqual(journal.calls, 3)
        self.assertEqual(journal.deadline, deadline)
        self.assertIn(unknown, journal.diagnostics()['inFlightIds'])
        self.assertEqual(journal.epoch, 1)

    def test_resume_after_idle_grants_only_one_immediate_admission(self):
        journal = self.fixture.create()
        rpc = self.rpc(journal)
        with patch('refresh.open_rpc', side_effect=self.fixture.transport()):
            rpc('eth_getBlockByNumber', ['0x64', False])
        journal.pause('offline interruption')
        journal.close()
        self.clock.advance(40)
        journal = self.fixture.reopen()
        resumed = self.rpc(journal)
        with patch('refresh.open_rpc', side_effect=self.fixture.transport()):
            resumed('eth_getBlockByNumber', ['0x64', False])
            resumed('eth_getBlockByNumber', ['0x69', False])
        self.assertEqual(self.waits, [2])
        stamps = [row['stamp']['monotonic'] for row in journal.records if row['kind'] == 'reserve']
        self.assertEqual([b - a for a, b in zip(stamps, stamps[1:])], [40, 2])

    def test_durable_deadline_rejects_waiting_admission_without_new_reservation(self):
        journal = self.fixture.create()
        self.clock.advance(1499)
        rpc = self.rpc(journal)
        with patch('refresh.open_rpc', side_effect=self.fixture.transport()) as wire:
            rpc('eth_getBlockByNumber', ['0x64', False])
            with self.assertRaises((TimeoutError, Stopped)):
                rpc('eth_getBlockByNumber', ['0x69', False])
        self.assertEqual(self.waits, [1])
        self.assertEqual(journal.calls, 1)
        self.assertEqual(wire.call_count, 1)

    def test_durable_429_wakes_all_pacing_waiters_and_is_terminal(self):
        journal = self.fixture.create()
        gate, release = Clock(), threading.Event()
        rpc = refresh.DurableRpc(journal, self.fixture.state, self.fixture.summary,
                                  admission_wait=gate.manual_wait)

        def respond(_request):
            if not release.wait(5):
                raise AssertionError('wire gate was not released')
            raise urllib.error.HTTPError(refresh.RPC_URL, 429, 'rate limited', {}, None)

        wire = Wire(lambda: self.clock.monotonic, respond)
        with patch('refresh.open_rpc', side_effect=wire), ThreadPoolExecutor(max_workers=4) as pool:
            first = pool.submit(rpc, 'eth_getBlockByNumber', ['0x64', False])
            wire.wait_for_calls(1)
            pending = [pool.submit(rpc, 'eth_getBlockByNumber', ['0x69', False]) for _ in range(3)]
            try:
                gate.wait_for_waiters(3)
                release.set()
                for future in [first] + pending:
                    with self.assertRaises(RuntimeError):
                        future.result(2)
            finally:
                release.set()
                rpc.drain()
            with self.assertRaises(RuntimeError):
                rpc('eth_getBlockByNumber', ['0x69', False])
        self.assertEqual(journal.calls, 1)
        self.assertEqual(len(wire.requests), 1)
        self.assertEqual(journal.failures[1]['code'], 429)
        self.assertEqual(journal.status, 'terminal')
        self.assertEqual(rpc._in_flight, 0)
        journal.close()
        with self.assertRaises(Stopped):
            self.fixture.reopen()

    def test_paused_failed_attempt_keeps_its_spacing_on_resume(self):
        journal = self.fixture.create()
        rpc = self.rpc(journal)
        with patch('refresh.open_rpc', side_effect=self.fixture.transport()):
            rpc('eth_getBlockByNumber', ['0x64', False])
        self.clock.advance(2)
        failed, _ = journal.reserve('anchor', 'eth_getBlockByNumber', ['0x69', False])
        latest = journal.last_admission_monotonic
        journal.fail(failed, category='controller_interruption')
        journal.close()
        self.clock.advance(.5)
        journal = self.fixture.reopen()
        resumed = self.rpc(journal)
        with patch('refresh.open_rpc', side_effect=self.fixture.transport()):
            resumed('eth_getBlockByNumber', ['0x64', False])
        self.assertEqual(self.waits, [1.5])
        self.assertEqual(journal.last_admission_monotonic, latest + 2)
        self.assertEqual(journal.calls, 3)
        self.assertIn(failed, journal.failures)

    def assert_source_error_precedes_terminal_waiter(self, code):
        journal = self.fixture.create()
        rpc = self.rpc(journal)
        with patch('refresh.open_rpc', side_effect=self.fixture.transport()):
            for number in ('0x64', '0x69', '0x6a', 'finalized'):
                rpc('eth_getBlockByNumber', [number, False])
        self.clock.advance(2)
        gate = Clock()
        rpc = refresh.DurableRpc(journal, self.fixture.state, self.fixture.summary,
                                  admission_wait=gate.manual_wait)
        source_reply, sealed, finish_failure = (threading.Event() for _ in range(3))
        original_fail = journal.fail

        def fail(*args, **kwargs):
            original_fail(*args, **kwargs)
            sealed.set()
            # Reproduce a waiter waking after the journal is terminal while
            # the source worker has not yet returned from error persistence.
            with rpc._admission:
                rpc._admission.notify_all()
            if not finish_failure.wait(5):
                raise AssertionError('failure persistence gate was not released')

        def respond(_request):
            if not source_reply.wait(5):
                raise AssertionError('source error gate was not released')
            raise urllib.error.HTTPError(refresh.RPC_URL, code, 'stop', {}, None)

        wire = Wire(lambda: self.clock.monotonic, respond)
        with patch.object(journal, 'fail', side_effect=fail), \
                patch('refresh.open_rpc', side_effect=wire), ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(rpc.fetch_headers, range(101, 105))
            try:
                wire.wait_for_calls(1)
                gate.wait_for_waiters(3)
                source_reply.set()
                self.assertTrue(sealed.wait(2))
                with self.assertRaises(ex.RpcError) as captured:
                    result.result(2)
                self.assertEqual(captured.exception.code, code)
                self.assertEqual(captured.exception.denied, code == 403)
                self.assertIs(rpc._error, captured.exception)
                self.assertEqual(journal.calls, 5)
                self.assertEqual(journal.failures[5]['code'], code)
                self.assertEqual(len(wire.requests), 1)
            finally:
                source_reply.set()
                finish_failure.set()
                rpc.drain()
        self.assertEqual(journal.status, 'terminal')

    def test_403_source_error_cannot_be_replaced_by_a_terminal_pacing_waiter(self):
        self.assert_source_error_precedes_terminal_waiter(403)

    def test_429_source_error_cannot_be_replaced_by_a_terminal_pacing_waiter(self):
        self.assert_source_error_precedes_terminal_waiter(429)
