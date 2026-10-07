"""Offline actual-urllib wrappers, cancellation provenance and failure evidence."""
import json
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

import refresh
from recovery import Corrupt
import test_lido_recovery_integration as integration
from rpc_clock import advancing_wait


def urllib_wrapped(error, request):
    """Exercise urllib's real OSError -> URLError wrapper without any socket."""
    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def set_debuglevel(self, *args):
            pass

        def request(self, *args, **kwargs):
            raise urllib.error.URLError(error)

        def close(self):
            pass

    request.timeout = 1
    return urllib.request.AbstractHTTPHandler().do_open(Connection, request)


class TransportFailureTests(unittest.TestCase):
    def setUp(self):
        self.fixture = integration.RecoveryIntegrationTests('test_unapproved_method_never_reaches_transport_or_spends_budget')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.journal = self.fixture.create()
        self.rpc = refresh.DurableRpc(self.journal, self.fixture.state, self.fixture.summary,
                                      admission_wait=advancing_wait(self.fixture.clock))
        with patch('refresh.open_rpc', side_effect=self.fixture.transport()):
            for number in ('0x64', '0x69', '0x6a', 'finalized'):
                self.rpc('eth_getBlockByNumber', [number, False])

    def concurrent_failures(self, second_error):
        entered, first_stored = threading.Barrier(2), threading.Event()
        original_cancel = self.rpc.cancel

        def cancel(error=None):
            result = original_cancel(error)
            if isinstance(result, refresh.RecoveryPaused):
                first_stored.set()
            return result

        def wire(request, **kwargs):
            number = json.loads(request.data)['params'][0]
            entered.wait(timeout=5)
            if number == '0x65':
                return urllib_wrapped(OSError('Tunnel connection failed: 403 Forbidden'), request)
            self.assertEqual(number, '0x66')
            self.assertTrue(first_stored.wait(5))
            return urllib_wrapped(second_error(self.rpc._error), request)

        with patch.object(self.rpc, 'cancel', side_effect=cancel), patch('refresh.open_rpc', side_effect=wire):
            try:
                with self.assertRaises(RuntimeError):
                    self.rpc.fetch_headers([101, 102])
            finally:
                self.rpc.drain()
        return self.journal.diagnostics()['failures']

    def test_real_nested_urllib_wrapper_of_known_peer_is_cancelled_and_stays_paused(self):
        failures = self.concurrent_failures(lambda first: first)
        self.assertEqual(self.journal.status, 'paused')
        self.assertEqual([f['kind'] for f in failures], ['unknown_tunnel_rejection', 'cancelled'])
        self.assertIs(failures[1]['knownCancellation'], True)
        self.assertEqual(failures[1]['phase'], 'open')
        self.assertEqual(failures[1]['exception']['type'], 'urllib.error.URLError')
        types = [r['type'] for r in failures[1]['exception']['chain']]
        self.assertIn('urllib.error.URLError', types)
        self.assertIn('refresh.RecoveryPaused', types)
        self.assertEqual(self.journal.calls, 6)
        self.assertFalse(self.journal.denied)

    def test_independent_timeout_after_pause_is_terminal_with_its_exception_chain(self):
        failures = self.concurrent_failures(lambda first: TimeoutError('independent socket timeout'))
        self.assertEqual(self.journal.status, 'terminal')
        self.assertEqual(failures[1]['kind'], 'source_error')
        self.assertIs(failures[1]['knownCancellation'], False)
        self.assertIn('builtins.TimeoutError', [r['type'] for r in failures[1]['exception']['chain']])

    def test_independent_reset_after_pause_is_terminal(self):
        failures = self.concurrent_failures(lambda first: ConnectionResetError(104, 'independent connection reset'))
        self.assertEqual(self.journal.status, 'terminal')
        self.assertEqual(failures[1]['kind'], 'source_error')
        self.assertIn('builtins.ConnectionResetError', [r['type'] for r in failures[1]['exception']['chain']])

    def test_wrapped_real_source_403_after_proxy_pause_is_terminal_and_redacted(self):
        secret = 'NEVER_PERSIST_AUTH_MATERIAL'
        error = urllib.error.HTTPError('https://user:' + secret + '@offline.invalid/?api_key=' + secret,
                                       403, 'Authorization: Bearer ' + secret,
                                       {'Proxy-Authorization': secret}, None)
        failures = self.concurrent_failures(lambda first: error)
        self.assertEqual(self.journal.status, 'terminal')
        self.assertTrue(self.journal.denied)
        self.assertEqual((failures[1]['kind'], failures[1]['code'], failures[1]['denied']),
                         ('http_error', 403, True))
        ledger = (self.journal.directory / 'ledger.jsonl').read_text()
        self.assertNotIn(secret, ledger)
        self.assertNotIn('Proxy-Authorization', json.dumps(failures))

    def test_fresh_http_status_wins_over_cancellation_object_elsewhere_in_wrapper(self):
        first = refresh.RecoveryPaused('known first error')
        for code in (401, 403, 302, 500):
            with self.subTest(code=code):
                outer = urllib.error.URLError(urllib.error.HTTPError(refresh.RPC_URL, code, 'source status', {}, None))
                outer.__cause__ = first
                classification = refresh.classify_acquisition_error(outer, first, 'open')
                self.assertNotEqual(classification['category'], 'cancelled')
                self.assertEqual(classification['code'], code)
                self.assertEqual(classification['denied'], code in (401, 403, 302))

    def test_same_text_or_incidental_context_is_not_cancellation_identity(self):
        first = refresh.RecoveryPaused('same text')
        for error in (urllib.error.URLError(RuntimeError('same text')),
                      urllib.error.URLError(TimeoutError('independent failure'))):
            error.__context__ = first
            classification = refresh.classify_acquisition_error(error, first, 'open')
            self.assertEqual(classification['category'], 'source_error')
            self.assertFalse(classification['knownCancellation'])

    def test_textual_reason_plus_known_cause_is_not_a_transparent_peer_wrapper(self):
        first = refresh.RecoveryPaused('known first error')
        error = urllib.error.URLError('independent textual transport failure')
        error.__cause__ = first
        result = refresh.classify_acquisition_error(error, first, 'open')
        self.assertEqual(result['category'], 'source_error')
        self.assertFalse(result['knownCancellation'])

    def test_mixed_new_socket_error_branch_is_not_a_pure_cancellation_wrapper(self):
        first = refresh.RecoveryPaused('known first error')
        first.__cause__ = urllib.error.URLError(OSError('Tunnel connection failed: 403 Forbidden'))
        for independent in (TimeoutError('new timeout'), ConnectionResetError(104, 'new reset')):
            with self.subTest(kind=type(independent).__name__):
                outer = urllib.error.URLError(independent)
                outer.__cause__ = first
                result = refresh.classify_acquisition_error(outer, first, 'open')
                self.assertEqual(result['category'], 'source_error')
                self.assertFalse(result['knownCancellation'])

    def test_independent_timeout_with_old_tunnel_cause_cannot_borrow_its_pause(self):
        def independent(first):
            error = urllib.error.URLError(TimeoutError('new independent timeout'))
            error.__cause__ = first
            return error

        failures = self.concurrent_failures(independent)
        self.assertEqual(self.journal.status, 'terminal')
        self.assertEqual(failures[1]['kind'], 'source_error')
        self.assertIs(failures[1]['knownCancellation'], False)

    def test_new_tunnel_branch_mixed_with_independent_timeout_is_terminal(self):
        error = urllib.error.URLError(OSError('Tunnel connection failed: 403 Forbidden'))
        error.__cause__ = TimeoutError('independent timeout')
        self.assertEqual(refresh.classify_acquisition_error(error, None, 'open')['category'], 'source_error')

    def test_wrapper_identity_is_only_transparent_during_transport(self):
        first = refresh.RecoveryPaused('known first error')
        wrapped = urllib.error.URLError(urllib.error.URLError(first))
        for phase in ('response', 'anchor', 'validation'):
            self.assertEqual(refresh.classify_acquisition_error(wrapped, first, phase)['category'], 'source_error')

    def test_malformed_bytes_received_before_peer_pause_remain_terminal(self):
        ident, _ = self.journal.reserve('event_header', 'eth_getBlockByNumber', ['0x65', False])
        self.journal.pause('peer interruption after bytes arrived')
        with self.assertRaises(Corrupt):
            self.journal.finish(ident, b'not JSON', lambda _: True)
        failure = self.journal.diagnostics()['failures'][-1]
        self.assertEqual(self.journal.status, 'terminal')
        self.assertEqual(failure['phase'], 'envelope')
        self.assertIn('exception', failure)

    def test_exception_evidence_is_durable_and_not_only_an_in_memory_diagnostic(self):
        self.concurrent_failures(lambda first: first)
        self.journal.close()
        reopened = self.fixture.reopen()
        failures = reopened.diagnostics()['failures']
        self.assertEqual(failures[-1]['kind'], 'cancelled')
        self.assertIs(failures[-1]['knownCancellation'], True)
        self.assertEqual(failures[-1]['exception']['type'], 'urllib.error.URLError')
        self.assertEqual(reopened.calls, 6)

    def test_terminal_controller_reason_uses_the_same_redaction(self):
        self.journal.stop('Authorization: Bearer NEVER_PERSIST_THIS_SECRET')
        ledger = (self.journal.directory / 'ledger.jsonl').read_text()
        self.assertNotIn('NEVER_PERSIST_THIS_SECRET', ledger)
        self.assertEqual(self.journal.status, 'terminal')


if __name__ == '__main__':
    unittest.main()
