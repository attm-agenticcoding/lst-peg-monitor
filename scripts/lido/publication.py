"""Validate a result against freshly fetched durable files before GitHub writes.

Pure local gate; no credentials/network/writes. The caller must still use an
atomic non-force Git update based on this exact fresh main commit, or the
contents-SHA compare-and-swap for a failure. Never publish local `once` results
through the recurring-task route.
"""
import argparse
import hashlib
import json
from pathlib import Path
import time
import re
from refresh import (SNAPSHOT_PATH, STATE_PATH, blob_sha, validate_lease, second,
                     EXPECTED_INTERVAL_SECONDS, SCHEDULE_UTC, MAX_SOURCE_AGE, MAX_OPERATIONAL_AGE)


def validate_scenario(output):
    if (output.get('schema') != 1 or output.get('live') is not False
            or output.get('calibrated') is not False or output.get('maxEconomicAgeSeconds') != 300):
        raise ValueError('unsafe public snapshot flags')
    source = second(output['asOf'])
    if [r['steth'] for r in output['tiers']] != [100, 200, 300, 500, 1000, 1500]:
        raise ValueError('six required independent tiers are missing')
    for tier in output['tiers']:
        if tier['split'] != ([tier['steth']] if tier['steth'] <= 1000 else [1000, 500]):
            raise ValueError('invalid request split')
        for name in ('main', 'stress'):
            completion = tier[name]
            if completion is None:
                if second(output['horizonEnd']) <= source:
                    raise ValueError('missing unknown-completion horizon')
            elif (type(completion['eligible_report_index']) is not int
                    or completion['eligible_report_index'] <= 0
                    or completion['elapsed_to_reference_seconds'] <= 0
                    or second(completion['reference_time_utc']) - source
                    != completion['elapsed_to_reference_seconds']):
                raise ValueError('inconsistent completion time')
    refs = [second(row['referenceTime']) for row in output['dailyCutoffs']]
    if not refs or refs != sorted(set(refs)) or refs[0] <= source:
        raise ValueError('invalid daily cutoff timestamps')
    for row in output['dailyCutoffs']:
        for name in ('mainSteth', 'stressSteth'):
            if not re.fullmatch(r'\d+\.\d{18}', row[name]):
                raise ValueError('inexact daily cutoff amount')


def validate_result(result, current, now):
    if result.get('schema') != 1 or result.get('stage') not in ('complete', 'failed'):
        raise ValueError('manifest is not a publishable final result')
    if result.get('localVerificationOnly') is not False:
        raise ValueError('local verification does not authorize a scheduled publication')
    if result.get('repository') != 'attm-agenticcoding/lst-peg-monitor':
        raise ValueError('unexpected repository')
    existing = json.loads(current[SNAPSHOT_PATH])
    validate_lease(existing, now, result['runId'])
    expected_paths = {SNAPSHOT_PATH, STATE_PATH} if result['stage'] == 'complete' else {SNAPSHOT_PATH}
    files = result.get('files', [])
    if len(files) != len(expected_paths) or {f['path'] for f in files} != expected_paths:
        raise ValueError('unexpected or duplicate publication paths')
    parsed = {}
    for file in files:
        path, content = file['path'], file['content']
        if not isinstance(content, str) or len(content.encode()) > 5 * 1024 * 1024:
            raise ValueError('unbounded or non-text publication content')
        if file.get('expectedGitBlobSha') != blob_sha(current[path]):
            raise ValueError('durable input changed during calculation: ' + path)
        if file.get('sha256') != hashlib.sha256(content.encode()).hexdigest():
            raise ValueError('manifest content digest does not match')
        parsed[path] = json.loads(content)
    output = parsed[SNAPSHOT_PATH]
    refresh = output.get('refresh', {})
    existing_refresh = existing.get('refresh', {})
    if (refresh.get('mode') != 'twice-daily' or refresh.get('expectedIntervalSeconds') != EXPECTED_INTERVAL_SECONDS
            or refresh.get('scheduleUtc') != SCHEDULE_UTC
            or refresh.get('maxSourceAgeSeconds') != MAX_SOURCE_AGE
            or refresh.get('maxOperationalAgeSeconds') != MAX_OPERATIONAL_AGE):
        raise ValueError('refresh cadence or freshness policy changed')
    # A manual result must not consume a scheduled day or discard another manual
    # idempotency key. Acquisition determines the trigger; publication preserves it.
    for key in ('trigger', 'attemptAt', 'attemptDay', 'attemptSlot', 'manualAttemptIds', 'lastManualRequestId'):
        if refresh.get(key) != existing_refresh.get(key):
            raise ValueError('refresh attempt identity changed: ' + key)
    if output.get('refresh', {}).get('lease') is not None:
        raise ValueError('finished output must release the lease')
    if result['stage'] == 'failed':
        if output.get('refresh', {}).get('state') != 'error':
            raise ValueError('failure manifest lacks explicit error status')
        before = {k: v for k, v in existing.items() if k != 'refresh'}
        after = {k: v for k, v in output.items() if k != 'refresh'}
        if after != before:
            raise ValueError('failed attempt modified successful scenario data')
    else:
        state = parsed[STATE_PATH]
        if output.get('refresh', {}).get('state') != 'ok':
            raise ValueError('success manifest lacks complete status')
        if output.get('refresh', {}).get('runId') != result['runId']:
            raise ValueError('output run identity mismatch')
        if second(output['asOf']) <= second(existing['asOf']) or second(output['asOf']) > now:
            raise ValueError('source did not advance or is future-dated')
        if now - second(output['asOf']) > MAX_SOURCE_AGE:
            raise ValueError('source became stale before publication')
        if (state['block'] != output['blockNumber'] or state['hash'] != output['blockHash']
                or state['timestamp'] != second(output['asOf'])):
            raise ValueError('public snapshot and durable checkpoint are not aligned')
        validate_scenario(output)
    return [{'path': f['path'], 'mode': '100644', 'type': 'blob', 'content': f['content']}
            for f in files]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--current-root', type=Path, required=True)
    parser.add_argument('--head', required=True, help='freshly fetched main commit SHA')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if len(args.head) != 40 or any(c not in '0123456789abcdef' for c in args.head):
        raise SystemExit('invalid fresh main commit SHA')
    result = json.loads(args.manifest.read_text())
    current = {p: (args.current_root / p).read_bytes() for p in (SNAPSHOT_PATH, STATE_PATH)}
    tree = validate_result(result, current, int(time.time()))
    args.output.write_text(json.dumps({'validated': True, 'baseCommit': args.head,
                                      'runId': result['runId'], 'treeElements': tree}, indent=2) + '\n')
    print(json.dumps({'validated': True, 'baseCommit': args.head, 'files': [e['path'] for e in tree]}))
