import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/lido'))
from refresh import (propose_lease, validate_lease, failure_snapshot, blob_sha,
                     hour_key, day_key, manifest, SNAPSHOT_PATH, STATE_PATH, Rpc,
                     MAX_SOURCE_AGE, MAX_OPERATIONAL_AGE)
from scenarios import report_references, build_scenarios
from publication import validate_result
from rpc_clock import RpcClock, advancing_wait


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.old = json.loads((ROOT / 'tests/fixtures/lido-cutoff-20261004.json').read_text())
        self.inputs = json.loads((ROOT / 'tests/fixtures/lido-scenario-inputs-20261004.json').read_text())
        self.now = 1791135600  # 2026-10-04 17:40 UTC

    def test_existing_results_reproduce_exactly(self):
        i = self.inputs
        refs = report_references(i['state']['timestamp'], i['timing'], 5)
        result = build_scenarios(i['state'], i['snapshot'], refs, i['flows'])
        self.assertEqual(result['tiers'], self.old['tiers'])
        self.assertEqual(result['dailyCutoffs'], self.old['dailyCutoffs'])

    def test_dynamic_calendar_and_age_cutoff(self):
        i = self.inputs
        boundary = report_references(i['state']['timestamp'], i['timing'], 1)[0]['timestamp']
        refs = report_references(boundary, i['timing'], 2)
        self.assertEqual(refs[0]['timestamp'], boundary + 86400)
        # Within the request margin, cash can exist but a new request is ineligible.
        i['state']['timestamp'] = boundary - 100
        refs = report_references(i['state']['timestamp'], i['timing'], 5)
        self.assertEqual(refs[0]['safe_timestamp'], boundary - 8052)
        i['snapshot']['scenario_available_cash_wei'] = str(10**25)
        result = build_scenarios(i['state'], i['snapshot'], refs, i['flows'])
        self.assertEqual(result['dailyCutoffs'][0]['mainSteth'], '0.000000000000000000')
        self.assertGreater(result['tiers'][0]['main']['eligible_report_index'], 1)

    def test_uncovered_is_null(self):
        i = self.inputs
        i['snapshot']['scenario_available_cash_wei'] = '0'
        result = build_scenarios(i['state'], i['snapshot'],
                                 report_references(i['state']['timestamp'], i['timing'], 5),
                                 {'main': [0] * 5, 'stress': [0] * 5})
        self.assertTrue(all(t['main'] is None and t['stress'] is None for t in result['tiers']))

    def test_inexact_or_incomplete_flow_rejected(self):
        i = self.inputs
        refs = report_references(i['state']['timestamp'], i['timing'], 5)
        for invalid in ([0] * 4, [1, 0, 0, 0, 0], [0.1] * 5, [-1] * 5):
            with self.assertRaises(ValueError):
                build_scenarios(i['state'], i['snapshot'], refs, {'main': invalid, 'stress': [0] * 5})

    def test_lease_does_not_refresh_scenario(self):
        leased, reason = propose_lease(self.old, self.now, 'A')
        self.assertIsNone(reason)
        self.assertEqual(leased['asOf'], self.old['asOf'])
        self.assertEqual(leased['tiers'], self.old['tiers'])
        validate_lease(leased, self.now + 100, 'A')
        for who, at in [('B', self.now), ('A', self.now + 2101)]:
            with self.assertRaises(RuntimeError):
                validate_lease(leased, at, who)

    def test_overlap_and_same_day_are_idempotent(self):
        leased, _ = propose_lease(self.old, self.now, 'A')
        self.assertIsNone(propose_lease(leased, self.now + 1, 'B')[0])
        failed = failure_snapshot(leased, self.now + 10, 'source unavailable')
        self.assertIsNone(propose_lease(failed, self.now + 100, 'B')[0])
        self.assertIsNone(propose_lease(failed, self.now + 3600, 'B')[0])
        self.assertIsNotNone(propose_lease(failed, self.now + 86400, 'B')[0])

    def test_next_utc_midnight_is_a_new_scheduled_day(self):
        from refresh import second
        at = second('2026-10-04T23:59:59Z')
        leased, _ = propose_lease(self.old, at, 'A')
        failed = failure_snapshot(leased, at, 'source unavailable')
        new, reason = propose_lease(failed, at + 1, 'B')
        self.assertIsNone(reason)
        self.assertEqual(new['refresh']['attemptDay'], '2026-10-05')

    def test_daily_metadata_keeps_acquisition_and_operational_freshness_separate(self):
        leased, _ = propose_lease(self.old, self.now, 'A')
        refresh = leased['refresh']
        self.assertEqual(refresh['mode'], 'daily')
        self.assertEqual(refresh['expectedIntervalSeconds'], 86400)
        self.assertEqual(refresh['scheduleUtc'], '00:00')
        self.assertEqual(refresh['maxSourceAgeSeconds'], 5400)
        self.assertEqual(refresh['maxOperationalAgeSeconds'], 91800)

    def test_historical_hourly_metadata_does_not_consume_daily_attempt(self):
        old = copy.deepcopy(self.old)
        old['refresh'] = {'mode': 'hourly', 'attemptHour': hour_key(self.now)}
        leased, reason = propose_lease(old, self.now, 'A')
        self.assertIsNone(reason)
        self.assertEqual(leased['refresh']['attemptDay'], day_key(self.now))
        self.assertEqual(leased['refresh']['attemptHour'], old['refresh']['attemptHour'])

    def test_manual_then_daily_same_day_has_independent_keys(self):
        manual, _ = propose_lease(self.old, self.now, 'M', trigger='manual', manual_request_id='request-1')
        self.assertNotIn('attemptDay', manual['refresh'])
        finished = failure_snapshot(manual, self.now + 10, 'source unavailable')
        daily, reason = propose_lease(finished, self.now + 11, 'D')
        self.assertIsNone(reason)
        self.assertEqual(daily['refresh']['attemptDay'], day_key(self.now))
        self.assertEqual(daily['refresh']['manualAttemptIds'], ['request-1'])

    def test_daily_then_manual_does_not_consume_next_day(self):
        daily, _ = propose_lease(self.old, self.now, 'D')
        done = failure_snapshot(daily, self.now + 1, 'source unavailable')
        manual, reason = propose_lease(done, self.now + 2, 'M', trigger='manual', manual_request_id='request-2')
        self.assertIsNone(reason)
        self.assertEqual(manual['refresh']['attemptDay'], daily['refresh']['attemptDay'])
        finished = failure_snapshot(manual, self.now + 3, 'source unavailable')
        self.assertIsNone(propose_lease(finished, self.now + 4, 'D2')[0])
        self.assertIsNotNone(propose_lease(finished, self.now + 86400, 'D3')[0])

    def test_manual_collision_and_retried_request_are_idempotent(self):
        manual, _ = propose_lease(self.old, self.now, 'M1', trigger='manual', manual_request_id='request-1')
        for trigger, key in [('manual', 'request-1'), ('manual', 'request-2'), ('scheduled', None)]:
            self.assertIsNone(propose_lease(manual, self.now + 1, 'M2', trigger=trigger, manual_request_id=key)[0])
        finished = failure_snapshot(manual, self.now + 10, 'source unavailable')
        self.assertIsNone(propose_lease(finished, self.now + 11, 'M2', trigger='manual', manual_request_id='request-1')[0])
        second_manual, _ = propose_lease(finished, self.now + 12, 'M2', trigger='manual', manual_request_id='request-2')
        finished_second = failure_snapshot(second_manual, self.now + 13, 'source unavailable')
        self.assertIsNone(propose_lease(finished_second, self.now + 14, 'M3', trigger='manual', manual_request_id='request-1')[0])

    def test_manual_requires_explicit_stable_key(self):
        for key in (None, '', 'bad key', 'x' * 161):
            with self.assertRaises(ValueError):
                propose_lease(self.old, self.now, 'M', trigger='manual', manual_request_id=key)
        with self.assertRaises(ValueError):
            propose_lease(self.old, self.now, 'D', manual_request_id='request-1')

    def test_failure_preserves_last_good_evidence(self):
        leased, _ = propose_lease(self.old, self.now, 'A')
        failed = failure_snapshot(leased, self.now + 10, '403 denied')
        for key in self.old:
            self.assertEqual(failed[key], self.old[key])
        self.assertEqual(failed['refresh']['state'], 'error')
        self.assertIsNone(failed['refresh']['lease'])
        self.assertNotIn('lastSuccessAt', failed['refresh'])

    def test_access_denial_blocks_daily_and_manual_retries(self):
        from execution import RpcError
        leased, _ = propose_lease(self.old, self.now, 'A')
        blocked = failure_snapshot(leased, self.now + 10, RpcError('HTTP 403', code=403, denied=True))
        self.assertTrue(blocked['refresh']['accessBlocked'])
        value, reason = propose_lease(blocked, self.now + 86400, 'B')
        self.assertIsNone(value)
        self.assertIn('authorization', reason)
        self.assertIsNone(propose_lease(blocked, self.now + 1, 'M', trigger='manual', manual_request_id='request-1')[0])

    def test_manifest_explicit_files_and_expected_blob(self):
        text = json.dumps(self.old)
        output = manifest('failed', 'A', {SNAPSHOT_PATH: text}, {SNAPSHOT_PATH: self.old})
        self.assertEqual([f['path'] for f in output['files']], [SNAPSHOT_PATH])
        self.assertEqual(output['files'][0]['expectedGitBlobSha'], blob_sha(text))
        self.assertEqual(blob_sha('test\n'), '9daeafb9864cf43055ae93beb0afd6c7d144bfa4')

    def test_rpc_cannot_expand_to_denied_call_method(self):
        clock = RpcClock()
        rpc = Rpc(clock() + 20, clock=clock, admission_wait=advancing_wait(clock))
        with self.assertRaisesRegex(RuntimeError, 'outside approved'):
            rpc('eth_call', [])
        self.assertEqual(rpc.calls, 0)

    def test_rpc_limit_error_preserves_bounded_split_semantics(self):
        import io
        from execution import RpcError
        raw = json.dumps({'jsonrpc': '2.0', 'id': 1, 'error': {'code': -32005, 'message': 'limit'}}).encode()
        clock = RpcClock()
        with patch('refresh.open_rpc', return_value=io.BytesIO(raw)):
            with self.assertRaises(RpcError) as captured:
                Rpc(clock() + 30, clock=clock, admission_wait=advancing_wait(clock))('eth_getLogs', [])
        self.assertEqual(captured.exception.code, -32005)
        self.assertFalse(captured.exception.denied)

    def test_rpc_mismatched_envelope_rejected(self):
        import io
        from execution import RpcError
        raw = json.dumps({'jsonrpc': '2.0', 'id': 99, 'result': []}).encode()
        clock = RpcClock()
        with patch('refresh.open_rpc', return_value=io.BytesIO(raw)):
            with self.assertRaisesRegex(RpcError, 'envelope'):
                Rpc(clock() + 30, clock=clock, admission_wait=advancing_wait(clock))('eth_getLogs', [])

    def publication_fixture(self):
        leased, _ = propose_lease(self.old, self.now, 'A')
        raw = json.dumps(leased).encode()
        result = manifest('failed', 'A', {SNAPSHOT_PATH: raw},
                          {SNAPSHOT_PATH: failure_snapshot(leased, self.now + 5, 'source down')})
        result['localVerificationOnly'] = False
        return result, {SNAPSHOT_PATH: raw, STATE_PATH: b'{}'}

    def test_publication_failure_preserves_snapshot(self):
        result, current = self.publication_fixture()
        self.assertEqual(len(validate_result(result, current, self.now + 10)), 1)

    def test_publication_rejects_changed_inputs_and_lease(self):
        result, current = self.publication_fixture()
        current[SNAPSHOT_PATH] += b' '
        with self.assertRaisesRegex(ValueError, 'input changed'):
            validate_result(result, current, self.now + 10)
        result, current = self.publication_fixture()
        with self.assertRaisesRegex(RuntimeError, 'expired'):
            validate_result(result, current, self.now + 2200)
        result['runId'] = 'B'
        with self.assertRaisesRegex(RuntimeError, 'foreign'):
            validate_result(result, current, self.now + 10)

    def test_publication_rejects_tampered_failure_or_extra_path(self):
        result, current = self.publication_fixture()
        file = result['files'][0]
        value = json.loads(file['content']); value['asOf'] = '2026-10-04T18:00:00Z'
        file['content'] = json.dumps(value)
        file['sha256'] = hashlib.sha256(file['content'].encode()).hexdigest()
        with self.assertRaisesRegex(ValueError, 'modified successful'):
            validate_result(result, current, self.now + 10)
        result, current = self.publication_fixture(); result['files'].append(copy.deepcopy(result['files'][0]))
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            validate_result(result, current, self.now + 10)

    def test_publication_rejects_local_verification(self):
        result, current = self.publication_fixture(); result['localVerificationOnly'] = True
        with self.assertRaisesRegex(ValueError, 'local verification'):
            validate_result(result, current, self.now + 10)

    def success_fixture(self):
        from refresh import canonical
        from scenarios import utc
        leased, _ = propose_lease(self.old, self.now, 'A')
        current = {SNAPSHOT_PATH: canonical(leased).encode(), STATE_PATH: b'{}'}
        value = copy.deepcopy(leased)
        at = self.now - 60
        value['asOf'] = utc(at)
        for row in value['tiers']:
            for name in ('main', 'stress'):
                from refresh import second
                row[name]['elapsed_to_reference_seconds'] = second(row[name]['reference_time_utc']) - at
        value['refresh'].update(state='ok', lease=None, runId='A')
        state = {'block': value['blockNumber'], 'hash': value['blockHash'], 'timestamp': at}
        result = manifest('complete', 'A', current, {SNAPSHOT_PATH: value, STATE_PATH: state})
        result['localVerificationOnly'] = False
        return result, current

    def test_success_publication_is_two_atomic_files(self):
        result, current = self.success_fixture()
        entries = validate_result(result, current, self.now)
        self.assertEqual({e['path'] for e in entries}, {SNAPSHOT_PATH, STATE_PATH})
        self.assertEqual(len(entries), 2)

    def test_publication_rejects_changed_attempt_identity_and_freshness(self):
        for field, changed in [('trigger', 'manual'), ('attemptDay', '2026-10-05'),
                               ('manualAttemptIds', ['unrequested']), ('maxSourceAgeSeconds', 86400),
                               ('expectedIntervalSeconds', 3600)]:
            result, current = self.success_fixture()
            file = next(f for f in result['files'] if f['path'] == SNAPSHOT_PATH)
            value = json.loads(file['content']); value['refresh'][field] = changed
            file['content'] = json.dumps(value)
            file['sha256'] = hashlib.sha256(file['content'].encode()).hexdigest()
            with self.assertRaisesRegex(ValueError, 'identity changed|policy changed'):
                validate_result(result, current, self.now)

    def test_manual_publication_preserves_scheduled_key(self):
        from refresh import canonical
        daily, _ = propose_lease(self.old, self.now - 60, 'D')
        done = failure_snapshot(daily, self.now - 59, 'source unavailable')
        manual, _ = propose_lease(done, self.now, 'M', trigger='manual', manual_request_id='request-1')
        current = {SNAPSHOT_PATH: canonical(manual).encode(), STATE_PATH: b'{}'}
        failed = failure_snapshot(manual, self.now + 5, 'source unavailable')
        result = manifest('failed', 'M', current, {SNAPSHOT_PATH: failed})
        result['localVerificationOnly'] = False
        self.assertEqual(len(validate_result(result, current, self.now + 10)), 1)
        self.assertEqual(failed['refresh']['attemptDay'], daily['refresh']['attemptDay'])

    def test_manual_success_uses_same_atomic_publication_gate(self):
        from refresh import canonical
        result, current = self.success_fixture()
        previous = json.loads(current[SNAPSHOT_PATH])
        previous['refresh']['lease'] = None
        manual, _ = propose_lease(previous, self.now, 'M', trigger='manual', manual_request_id='request-1')
        current[SNAPSHOT_PATH] = canonical(manual).encode()
        files = {f['path']: json.loads(f['content']) for f in result['files']}
        output = files[SNAPSHOT_PATH]
        output['refresh'] = {**manual['refresh'], 'state': 'ok', 'runId': 'M', 'lease': None}
        result = manifest('complete', 'M', current, files)
        result['localVerificationOnly'] = False
        self.assertEqual(len(validate_result(result, current, self.now)), 2)
        self.assertEqual(output['refresh']['attemptDay'], previous['refresh']['attemptDay'])

    def test_success_rejects_changed_checkpoint_and_misalignment(self):
        result, current = self.success_fixture(); current[STATE_PATH] = b'{ }'
        with self.assertRaisesRegex(ValueError, 'input changed'):
            validate_result(result, current, self.now)
        result, current = self.success_fixture()
        f = next(f for f in result['files'] if f['path'] == STATE_PATH)
        state = json.loads(f['content']); state['block'] += 1
        f['content'] = json.dumps(state); f['sha256'] = hashlib.sha256(f['content'].encode()).hexdigest()
        with self.assertRaisesRegex(ValueError, 'not aligned'):
            validate_result(result, current, self.now)

    def test_success_rejects_unchanged_future_and_stale_source(self):
        from scenarios import utc
        for asof, error in [(self.old['asOf'], 'did not advance'),
                            (utc(self.now + 1), 'future'), (utc(self.now - 5401), 'stale')]:
            result, current = self.success_fixture()
            f = next(f for f in result['files'] if f['path'] == SNAPSHOT_PATH)
            value = json.loads(f['content']); value['asOf'] = asof
            # Make the durable old fixture sufficiently old for the stale branch.
            if error == 'stale':
                existing = json.loads(current[SNAPSHOT_PATH]); existing['asOf'] = utc(self.now - 8000)
                current[SNAPSHOT_PATH] = json.dumps(existing).encode()
                f['expectedGitBlobSha'] = blob_sha(current[SNAPSHOT_PATH])
            f['content'] = json.dumps(value); f['sha256'] = hashlib.sha256(f['content'].encode()).hexdigest()
            with self.assertRaisesRegex(ValueError, error):
                validate_result(result, current, self.now)

    def test_lease_expires_at_exact_boundary(self):
        result, current = self.success_fixture()
        with self.assertRaisesRegex(RuntimeError, 'expired'):
            validate_result(result, current, self.now + 2100)


if __name__ == '__main__':
    unittest.main()
