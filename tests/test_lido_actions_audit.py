"""Offline bounded diagnostic extraction and corruption/secret rejection."""
import base64
import gzip
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/lido'))
import actions_audit as audit
import test_lido_recovery_journal as journal_fixtures


class AuditTests(unittest.TestCase):
    def setUp(self):
        # Reuse the real production journal fixture, not a made-up ledger seal.
        self.fixture = journal_fixtures.DurableJournalTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.fixture.directory = self.root / 'work/recovery'
        self.fixture.seal = self.root / 'work/recovery.seal.json'
        journal = self.fixture.create()
        self.fixture.authenticate(journal)
        self.fixture.log(journal)
        journal.stop('offline diagnostic fixture')
        journal.close()
        (self.root / 'result-run.json').write_text('{"stage":"failed","localVerificationOnly":false}')
        (self.root / 'work/audit.json').write_text('{"model":{"sixTiers":true}}')
        (self.root / 'work/rpc-audit.json').write_text('{"calls":5}')
        self.metadata = {'repository': 'attm-agenticcoding/lst-peg-monitor', 'runId': 'offline-test',
                         'repositoryPublished': True, 'pagesVerified': True}

    def lines(self):
        lines = []
        audit.emit_bundle(self.root, self.metadata, emit=lines.append)
        return lines

    def test_complete_byte_round_trip_in_timestamped_logs_without_ssz_or_environment(self):
        (self.root / 'environment.json').write_text('{"GH_TOKEN":"unrelated-secret"}')
        (self.root / 'work/state.ssz').write_bytes(b'RAW SSZ MUST NEVER BE EXPORTED')
        metadata, files = audit.decode_bundle('2026-10-07T00:00:00Z ' + line for line in self.lines())
        self.assertEqual(metadata, self.metadata)
        self.assertEqual(len([p for p in files if audit.RESPONSE.fullmatch(p)]), 5)
        self.assertNotIn('work/state.ssz', files)
        self.assertNotIn('environment.json', files)
        for path, content in files.items():
            self.assertEqual(content, (self.root / path).read_bytes())

    def test_missing_duplicate_reordered_corrupt_and_truncated_chunks_are_rejected(self):
        with patch.object(audit, 'CHUNK_BYTES', 80):
            lines = self.lines()
        self.assertGreater(len(lines), 5)
        broken = [lines[1:], lines[:-1], lines[:1] + lines[2:],
                  lines[:2] + lines[1:], [lines[0], lines[2], lines[1], *lines[3:]]]
        changed = list(lines)
        row = json.loads(changed[1]); row['data'] = 'A' + row['data'][1:]
        changed[1] = json.dumps(row); broken.append(changed)
        for value in broken:
            with self.subTest(count=len(value)), self.assertRaises((audit.AuditError, ValueError)):
                audit.decode_bundle(value)

    def test_sealed_response_corruption_and_orphans_are_rejected(self):
        path = next((self.root / 'work/recovery/responses').glob('*.json'))
        old = path.read_bytes()
        path.write_bytes(b'{}')
        with self.assertRaisesRegex(audit.AuditError, 'digest or size'):
            self.lines()
        path.write_bytes(old)
        (path.parent / ('99-' + '1' * 64 + '.json')).write_text('{}')
        with self.assertRaisesRegex(audit.AuditError, 'unsealed'):
            self.lines()

    def test_symlink_and_unsafe_response_path_are_rejected(self):
        path = self.root / 'work/audit.json'
        path.unlink()
        path.symlink_to(self.root / 'work/rpc-audit.json')
        with self.assertRaisesRegex(audit.AuditError, 'unsafe'):
            self.lines()
        self.assertFalse(audit.allowed('../outside.json'))
        self.assertFalse(audit.allowed('work/recovery/responses/../../secret.json'))

    def test_credential_bytes_patterns_and_sensitive_fields_never_emit_partial_bundle(self):
        for value, secrets in [({'source': 'a-secret-token-value'}, ('a-secret-token-value',)),
                               ({'nested': {'Authorization': 'Bearer private'}}, ()),
                               ({'value': 'ghs_' + 'x' * 30}, ())]:
            with self.subTest(value=value):
                (self.root / 'work/audit.json').write_text(json.dumps(value))
                lines = []
                with self.assertRaises(audit.AuditError):
                    audit.emit_bundle(self.root, self.metadata, secrets, emit=lines.append)
                self.assertEqual(lines, [])

    def test_missing_or_corrupt_independent_seal_is_rejected(self):
        path = self.root / 'work/recovery.seal.json'
        old = path.read_bytes()
        path.unlink()
        with self.assertRaises(audit.AuditError):
            self.lines()
        seal = json.loads(old); seal['head'] = '0' * 64
        path.write_text(json.dumps(seal))
        with self.assertRaisesRegex(Exception, 'seal mismatch'):
            self.lines()

    def test_success_needs_full_audit_and_limits_never_truncate(self):
        (self.root / 'result-run.json').write_text('{"stage":"complete"}')
        path = self.root / 'work/audit.json'
        old = path.read_bytes(); path.unlink()
        with self.assertRaisesRegex(audit.AuditError, 'lacks complete'):
            self.lines()
        path.write_bytes(old)
        for bound in ('MAX_RAW', 'MAX_GZIP'):
            with self.subTest(bound=bound), patch.object(audit, bound, 20):
                lines = []
                with self.assertRaises(audit.AuditError):
                    audit.emit_bundle(self.root, self.metadata, emit=lines.append)
                self.assertEqual(lines, [])

    def test_extraction_limits_decompression_before_allocating_full_payload(self):
        archive = b'x' * 4096
        compressed = gzip.compress(archive)
        header = {'schema': 1, 'chunks': 1, 'files': 0, 'rawBytes': len(archive),
                  'gzipBytes': len(compressed), 'rawSha256': audit.digest(archive),
                  'gzipSha256': audit.digest(compressed)}
        lines = [json.dumps({'lidoAudit': 'begin', **header}),
                 json.dumps({'lidoAudit': 'chunk', 'index': 0, 'data': base64.b64encode(compressed).decode()}),
                 json.dumps({'lidoAudit': 'end', **header})]
        with patch.object(audit, 'MAX_RAW', 100), self.assertRaisesRegex(audit.AuditError, 'raw diagnostic'):
            audit.decode_bundle(lines)


if __name__ == '__main__':
    unittest.main()
