import errno
import json
from pathlib import Path
import re
import sys
import unittest
from urllib.error import HTTPError, URLError


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/lido'))
from acquisition_errors import (MAX_DIAGNOSTIC_BYTES, MAX_EXCEPTION_NODES,
                                MAX_MESSAGE_BYTES, MAX_TYPE_BYTES,
                                exception_nodes, safe_exception, sanitized_text)


class ExceptionDiagnosticsTests(unittest.TestCase):
    def persisted(self, error):
        result = safe_exception(error)
        encoded = json.dumps(result)
        self.assertLessEqual(len(encoded.encode('ascii')), MAX_DIAGNOSTIC_BYTES)
        self.assertEqual(json.loads(encoded), result)
        for node in [result] + result['chain']:
            self.assertLessEqual(len(json.dumps(node['type']).encode('ascii')), MAX_TYPE_BYTES)
            self.assertLessEqual(len(json.dumps(node['message']).encode('ascii')), MAX_MESSAGE_BYTES)
            self.assertFalse(set(node) & {'args', 'headers', 'traceback', 'repr'})
        return result, encoded

    def test_type_text_and_errno_are_useful(self):
        result, _ = self.persisted(ConnectionResetError(errno.ECONNRESET, 'peer reset connection'))
        self.assertEqual(result['type'], 'builtins.ConnectionResetError')
        self.assertIn('peer reset connection', result['message'])
        self.assertEqual(result['errno'], errno.ECONNRESET)
        self.assertEqual(result['chain'], [])

    def test_url_credentials_query_fragment_and_path_are_redacted(self):
        result, encoded = self.persisted(OSError(
            'request failed https://USERSECRET:PASSSECRET@rpc.example/KEYSECRET?api_key=QUERYSECRET#FRAGMENTSECRET'))
        for secret in ('USERSECRET', 'PASSSECRET', 'KEYSECRET', 'QUERYSECRET', 'FRAGMENTSECRET'):
            self.assertNotIn(secret, encoded)
        self.assertIn('request failed', result['message'])
        self.assertIn('[redacted URL]', result['message'])

    def test_auth_headers_cookies_and_credential_assignments_are_not_persisted(self):
        messages = [
            'Authorization: Bearer SECRET_A', 'Proxy-Authorization: Basic SECRET_B',
            'Cookie: session=SECRET_C; other=SECRET_D',
            'Set-Cookie: id=SECRET_E\r\n folded=SECRET_F',
            "headers={'Authorization': 'SECRET_G', 'other': 'SECRET_H'}",
            'api_key=SECRET_I', 'api-key: SECRET_J', 'password="SECRET_K"',
            "{'access_token': 'SECRET_L', 'refresh_token': 'SECRET_M'}",
            'client_secret=SECRET_N', 'AWS_SECRET_ACCESS_KEY=SECRET_O',
            'token=SECRET_P', 'auth=SECRET_Q', 'pwd=SECRET_R',
            'Bearer SECRET_S', 'X-Api-Key: SECRET_T',
        ]
        for message in messages:
            with self.subTest(message=message):
                # Use opaque values: a sentinel containing "secret" could
                # itself trigger redaction and conceal a broken key matcher.
                message = re.sub(r'\bSECRET_([A-Z])\b', r'v4Lue9Z8_\1', message)
                result, encoded = self.persisted(RuntimeError(message))
                self.assertNotIn('v4Lue9Z8_', encoded)
                self.assertIn('redacted', result['message'])

    def test_http_error_records_status_without_formatting_reason_or_headers(self):
        class Unprintable:
            def __str__(self):
                raise AssertionError('do not format the reason')
            def __repr__(self):
                raise AssertionError('do not inspect a repr')

        error = HTTPError('https://USERSECRET:PASSSECRET@rpc.example/?SECRET_QUERY',
                          403, Unprintable(), {'Authorization': 'SECRET_HEADER'}, None)
        result, encoded = self.persisted(error)
        self.assertEqual(result['message'], 'HTTP status 403')
        self.assertEqual(result['code'], 403)
        self.assertNotIn('SECRET', encoded)
        self.assertEqual(list(exception_nodes(error)), [error])

    def test_chain_preserves_parent_and_edge_identity(self):
        root = RuntimeError('collector stopped')
        cause = URLError(ConnectionResetError(errno.ECONNRESET, 'peer reset'))
        context = ValueError('earlier failure')
        root.__cause__ = cause
        root.__context__ = context
        root.__suppress_context__ = False
        result, _ = self.persisted(root)
        self.assertEqual([(item['parent'], item['via']) for item in result['chain']],
                         [(0, '__cause__'), (0, '__context__'), (1, 'reason')])
        self.assertEqual(list(exception_nodes(root)), [root, cause, context, cause.reason])
        self.assertEqual(list(exception_nodes(root, include_context=False)), [root, cause, cause.reason])

    def test_suppressed_context_is_not_traversed(self):
        root = RuntimeError('top')
        root.__context__ = ValueError('SECRET_CONTEXT')
        root.__suppress_context__ = True
        result, encoded = self.persisted(root)
        self.assertEqual(result['chain'], [])
        self.assertNotIn('SECRET_CONTEXT', encoded)

    def test_nested_url_error_and_http_error(self):
        leaf = HTTPError('https://rpc.example', 502, 'SECRET_REASON',
                         {'Cookie': 'SECRET_COOKIE'}, None)
        middle = URLError(leaf)
        root = URLError(middle)
        result, encoded = self.persisted(root)
        self.assertEqual(list(exception_nodes(root)), [root, middle, leaf])
        self.assertEqual(result['chain'][-1]['code'], 502)
        self.assertNotIn('SECRET', encoded)

    def test_url_reason_is_traversed_only_when_it_is_an_exception(self):
        class Pretend:
            def __str__(self):
                raise AssertionError('must not format object')
            def __repr__(self):
                raise AssertionError('must not repr object')

        for reason in (Pretend(), {'password': 'SECRET_MAP'}, ['SECRET_LIST']):
            root = URLError(reason)
            result, encoded = self.persisted(root)
            self.assertEqual(list(exception_nodes(root)), [root])
            self.assertEqual(result['chain'], [])
            self.assertNotIn('SECRET', encoded)

    def test_cycles_and_shared_nodes_are_bounded_by_identity(self):
        root = URLError('root')
        other = RuntimeError('other')
        root.reason = other
        root.__cause__ = other
        other.__cause__ = root
        result, _ = self.persisted(root)
        self.assertEqual(list(exception_nodes(root)), [root, other])
        self.assertEqual(len(result['chain']), 1)
        self.assertTrue(result['truncated'])
        root.reason = root
        self.persisted(root)

    def test_all_text_and_chain_bytes_have_a_hard_limit(self):
        roots = []
        for _ in range(MAX_EXCEPTION_NODES * 3):
            kind = type('X' * 500, (RuntimeError,), {'__module__': 'm' * 500})
            roots.append(kind('\U0001f600' * 1000))
        for first, second in zip(roots, roots[1:]):
            first.__cause__ = second
        result, _ = self.persisted(roots[0])
        self.assertEqual(len(list(exception_nodes(roots[0]))), MAX_EXCEPTION_NODES)
        self.assertEqual(len(result['chain']), MAX_EXCEPTION_NODES - 1)
        self.assertTrue(result['truncated'])

    def test_oversized_text_is_suppressed_before_partial_redaction(self):
        result, encoded = self.persisted(RuntimeError('x' * 100000 + ' password=SECRET_END'))
        self.assertNotIn('SECRET_END', encoded)
        self.assertIn('exceeded diagnostic limit', result['message'])

    def test_broken_string_and_object_repr_do_not_escape(self):
        class Broken(RuntimeError):
            def __str__(self):
                raise ValueError('password=SECRET_FAILURE')
            def __repr__(self):
                raise AssertionError('never repr an exception')

        result, encoded = self.persisted(Broken())
        self.assertEqual(result['message'], '[exception text unavailable]')
        self.assertNotIn('SECRET_FAILURE', encoded)
        _, encoded = self.persisted(RuntimeError('failed <Client object at 0x1234abcd>'))
        self.assertNotIn('0x1234abcd', encoded)
        _, encoded = self.persisted(RuntimeError('Traceback (most recent call last):\n  v4Lue9Z8'))
        self.assertNotIn('v4Lue9Z8', encoded)

    def test_arbitrary_reason_and_code_attributes_are_not_followed(self):
        root = RuntimeError('failure')
        root.reason = ValueError('SECRET_REASON')
        root.code = 'SECRET_CODE'
        root.errno = 'SECRET_ERRNO'
        root.headers = {'Cookie': 'SECRET_COOKIE'}
        result, encoded = self.persisted(root)
        self.assertEqual(list(exception_nodes(root)), [root])
        self.assertEqual(set(result), {'type', 'message', 'chain', 'truncated'})
        self.assertNotIn('SECRET', encoded)

    def test_public_text_sanitizer_bounds_text_without_formatting_objects(self):
        class Unprintable:
            def __str__(self):
                raise AssertionError('must not format object')
            def __repr__(self):
                raise AssertionError('must not repr object')

        self.assertEqual(sanitized_text(Unprintable()), '[non-text diagnostic suppressed]')
        self.assertNotIn('SECRET', sanitized_text("{'Proxy-Authorization': 'Basic SECRET'}"))
        self.assertLessEqual(len(json.dumps(sanitized_text('x' * 1000, limit=80))), 80)
        self.assertNotIn('SECRET', sanitized_text('https://rpc.example?token=SECRET'))

    def test_string_url_reason_and_known_numeric_fields_are_sanitized(self):
        root = URLError('proxy failed: Authorization: Bearer SECRET')
        root.errno = 'SECRET_ERRNO'
        result, encoded = self.persisted(root)
        self.assertNotIn('SECRET', encoded)
        self.assertNotIn('errno', result)
        root = HTTPError('https://rpc.example', True, 'SECRET_REASON', {}, None)
        result, encoded = self.persisted(root)
        self.assertNotIn('code', result)
        self.assertEqual(result['message'], 'HTTP error')
        self.assertNotIn('SECRET', encoded)


if __name__ == '__main__':
    unittest.main()
