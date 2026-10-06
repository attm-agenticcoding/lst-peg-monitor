#!/usr/bin/env python3
"""Prepare a daily or explicitly requested manual lease, then collect a snapshot.

Only the small, explicit manifest is eligible for publication. External writers
must acquire the lease with a GitHub contents-SHA compare-and-swap, and publish
the final files in one non-force, fast-forward commit after checking lease owner.
This command never uses a wallet, credentials, eth_call, or private user data.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import signal
import tempfile
import time
import urllib.request
import urllib.error
import uuid

from scenarios import build_scenarios, report_references, utc, eth

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_PATH = 'data/lido-cutoff-snapshot.json'
STATE_PATH = 'data/lido-collector-state.json'
MAX_RUN_SECONDS = 1500
LEASE_SECONDS = 2100
MAX_SOURCE_AGE = 5400
EXPECTED_INTERVAL_SECONDS = 86400
MAX_OPERATIONAL_AGE = EXPECTED_INTERVAL_SECONDS + MAX_SOURCE_AGE
MAX_MANUAL_ATTEMPTS = 128
RPC_URL = 'https://rpc.flashbots.net/'
ALLOWED_RPC = {'eth_getLogs', 'eth_getBlockByNumber', 'eth_getBalance'}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def blob_sha(data):
    data = data.encode() if isinstance(data, str) else data
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def load_json(path):
    return json.loads(Path(path).read_text())


def second(value):
    return int(datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp())


def hour_key(now):
    """Historical metadata helper; never used for daily scheduling decisions."""
    return utc(now - now % 3600)


def day_key(now):
    return utc(now - now % EXPECTED_INTERVAL_SECONDS)[:10]


def propose_lease(snapshot, now, run_id, *, trigger='scheduled', manual_request_id=None):
    if trigger not in ('scheduled', 'manual'):
        raise ValueError('invalid refresh trigger')
    if trigger == 'manual':
        if not isinstance(manual_request_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,159}', manual_request_id):
            raise ValueError('manual refresh requires a stable explicit manual request ID')
    elif manual_request_id is not None:
        raise ValueError('manual request ID is only valid for an explicit manual trigger')
    refresh = snapshot.get('refresh', {})
    if refresh.get('accessBlocked') is True:
        return None, 'source access was denied; resolve authorization before another attempt'
    lease = refresh.get('lease', {})
    if lease and int(lease.get('expiresAtEpoch', 0)) > now:
        return None, 'another refresh owns an unexpired lease'
    if trigger == 'scheduled' and refresh.get('attemptDay') == day_key(now):
        return None, 'this UTC day has already been attempted'
    manual_attempts = list(refresh.get('manualAttemptIds', []))
    if trigger == 'manual' and manual_request_id in manual_attempts:
        return None, 'this explicit manual request has already been attempted'
    result = deepcopy(snapshot)
    result['refresh'] = {
        **refresh, 'mode': 'daily', 'state': 'running',
        'expectedIntervalSeconds': EXPECTED_INTERVAL_SECONDS, 'scheduleUtc': '00:00',
        'maxSourceAgeSeconds': MAX_SOURCE_AGE, 'maxOperationalAgeSeconds': MAX_OPERATIONAL_AGE,
        'attemptAt': utc(now), 'trigger': trigger, 'error': None,
        'lease': {'runId': run_id, 'acquiredAtEpoch': now,
                  'expiresAtEpoch': now + LEASE_SECONDS, 'trigger': trigger,
                  'requestKey': day_key(now) if trigger == 'scheduled' else manual_request_id},
    }
    if trigger == 'scheduled':
        result['refresh']['attemptDay'] = day_key(now)
    else:
        result['refresh']['manualAttemptIds'] = (manual_attempts + [manual_request_id])[-MAX_MANUAL_ATTEMPTS:]
        result['refresh']['lastManualRequestId'] = manual_request_id
    return result, None


def validate_lease(snapshot, now, run_id):
    lease = snapshot.get('refresh', {}).get('lease', {})
    if lease.get('runId') != run_id or int(lease.get('expiresAtEpoch', 0)) <= now:
        raise RuntimeError('missing, expired, or foreign durable refresh lease')


def failure_snapshot(snapshot, now, error):
    """Only status changes: original asOf, scenarios and evidence stay unchanged."""
    result = deepcopy(snapshot)
    refresh = result.setdefault('refresh', {})
    refresh.update(state='error', finishedAt=utc(now), lease=None,
                   error=str(error).replace('\n', ' ')[:500],
                   accessBlocked=(getattr(error, 'denied', False)
                                  or getattr(error, 'code', None) in (401, 403)))
    return result


def manifest(stage, run_id, base, files, result=None, error=None):
    return {
        'schema': 1, 'repository': 'attm-agenticcoding/lst-peg-monitor',
        'stage': stage, 'runId': run_id, 'result': result, 'error': error,
        'atomicPublicationRequired': stage == 'complete',
        'leaseOwnerMustMatch': run_id if stage != 'acquire' else None,
        'files': [{'path': name, 'content': canonical(value),
                   'sha256': digest(value),
                   'expectedGitBlobSha': blob_sha(base[name]) if name in base else None}
                  for name, value in files.items()],
    }


class Rpc:
    def __init__(self, deadline):
        self.deadline, self.calls, self.evidence = deadline, 0, []

    def __call__(self, method, params):
        from execution import RpcError
        if method not in ALLOWED_RPC:
            raise RuntimeError('RPC method outside approved collector scope: ' + method)
        self.calls += 1
        remaining = self.deadline - time.monotonic()
        if self.calls > 256 or remaining <= 0:
            raise RuntimeError('bounded RPC budget exhausted')
        request = {'jsonrpc': '2.0', 'id': self.calls, 'method': method, 'params': params}
        req = urllib.request.Request(RPC_URL, data=json.dumps(request).encode(),
                                     headers={'Content-Type': 'application/json'})
        # HTTP 401/403 and JSON-RPC errors stop the run. No provider/method bypass.
        try:
            with urllib.request.urlopen(req, timeout=min(40, remaining)) as response:
                raw = response.read(20 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as error:
            raise RpcError(f'{method}: HTTP {error.code}; source access failed',
                           code=error.code, denied=error.code in (401, 403)) from error
        if len(raw) > 20 * 1024 * 1024:
            raise RuntimeError('RPC result exceeds bounded response size')
        value = json.loads(raw)
        if value.get('id') != self.calls or value.get('jsonrpc') != '2.0':
            raise RpcError('mismatched JSON-RPC response envelope')
        if 'error' in value or 'result' not in value:
            error = value.get('error', {})
            code = error.get('code') if isinstance(error, dict) else None
            raise RpcError('RPC source failed: ' + str(error or 'missing result'),
                           code=code, denied=code in (401, 403))
        self.evidence.append({'request': request, 'responseSha256': hashlib.sha256(raw).hexdigest()})
        return value['result']


def public_snapshot(old, state, execution, beacon, scenarios, now, run_id, provenance):
    summary = beacon['summary']
    snapshot = execution['snapshot']
    result = {
        'schema': 1, 'id': f'lido-{state["block"]}-known-state-v2',
        'asOf': utc(int(state['timestamp'])), 'blockNumber': int(state['block']),
        'blockHash': state['hash'], 'beaconSlot': summary['state_slot'],
        'beaconStateRoot': summary['state_root'],
        'pendingRequests': len(state['pending_requests']),
        'pendingSteth': eth(sum(int(r['amount_steth_wei']) for r in state['pending_requests'])),
        'physicalCashEth': eth(int(snapshot['sum_current_physical_cash_wei'])),
        'scenarioAvailableCashEth': eth(int(snapshot['scenario_available_cash_wei'])),
        'maxEconomicAgeSeconds': 300, 'live': False, 'calibrated': False,
        **{k: scenarios[k] for k in ('tiers', 'dailyCutoffs', 'horizonEnd')},
        'assumptions': [
            'Known-state, nominal no-haircut scenarios; future cash is assumed fully report-admissible.',
            'No unknown future deposits, exits, rewards, losses, missed blocks or report interruptions.',
            'Main retains legacy workload; stress reserves eight total pending-partial positions per block.',
            'Reference reports are not publication or claim times, probability bounds or guarantees.',
        ],
        'configurationEvidence': state['config'],
        'consensusAssumptions': beacon['assumptions'],
        'validation': {'predictiveOutOfSample': False,
                       'execution': execution['validation'], 'beacon': beacon['validation'],
                       'fifoBudgetReconciliation': True,
                       'historicalRegressionFixture': '2026-10-04T16:16:23Z'},
        'sourceDigests': {'executionState': digest(state),
                          'beaconStateSsz': summary['state_sha256'],
                          'scenarioAudit': digest(scenarios['scenarioAudit']),
                          'acquisition': digest(provenance)},
        'refresh': {**old.get('refresh', {}), 'mode': 'daily', 'state': 'ok',
                    'expectedIntervalSeconds': EXPECTED_INTERVAL_SECONDS, 'scheduleUtc': '00:00',
                    'maxSourceAgeSeconds': MAX_SOURCE_AGE, 'maxOperationalAgeSeconds': MAX_OPERATIONAL_AGE,
                    'finishedAt': utc(now), 'lastSuccessAt': utc(now),
                    'lastSuccessSnapshotAsOf': utc(int(state['timestamp'])),
                    'runId': run_id, 'lease': None, 'error': None},
    }
    return result


def collect(root, old, run_id, workdir, now=None):
    from beacon_hourly import download_state, inspect_state, simulate_reports
    from execution import collect_at
    state = load_json(root / STATE_PATH)
    deadline = time.monotonic() + MAX_RUN_SECONDS - 30
    rpc = Rpc(deadline)
    state_file = workdir / 'state.ssz'
    acquisition = download_state(state_file)
    print(json.dumps({'step': 'state_downloaded', 'bytes': acquisition['bytes']}), flush=True)
    summary = inspect_state(state_file)
    print(json.dumps({'step': 'state_authenticated', 'blockNumber': summary['block_number'],
                      'asOf': utc(int(summary['timestamp']))}), flush=True)
    current = int(time.time()) if now is None else now
    timestamp = int(summary['timestamp'])
    if timestamp <= second(old['asOf']):
        raise RuntimeError('source has not advanced beyond the last successful snapshot')
    if timestamp > current or current - timestamp > MAX_SOURCE_AGE:
        raise RuntimeError('consensus source snapshot is future-dated or older than 90 minutes')
    execution = collect_at(state, summary['block_number'], summary['block_hash'], rpc)
    print(json.dumps({'step': 'execution_replayed', 'rpcCalls': rpc.calls}), flush=True)
    if not execution['validation'].get('complete', False):
        raise RuntimeError('execution reconstruction is incomplete')
    state = execution['state']
    if int(state['timestamp']) != timestamp:
        raise RuntimeError('consensus and execution timestamps do not match')
    refs = report_references(timestamp, execution['report_timing'])
    beacon = simulate_reports(state_file, workdir, [r['timestamp'] for r in refs],
                              execution['execution_anchor'], summary=summary)
    print(json.dumps({'step': 'scenarios_simulated', 'referenceReports': len(refs)}), flush=True)
    scenarios = build_scenarios(state, execution['snapshot'], refs, beacon)
    provenance = {'beaconAcquisition': acquisition, 'rpc': rpc.evidence}
    finished = int(time.time()) if now is None else now
    if finished - timestamp > MAX_SOURCE_AGE:
        raise RuntimeError('snapshot exceeded 90-minute age before calculation finished')
    output = public_snapshot(old, state, execution, beacon, scenarios, finished, run_id, provenance)
    (workdir / 'audit.json').write_text(canonical({'execution': execution, 'beacon': beacon,
                                                'scenarios': scenarios, 'provenance': provenance}))
    return output, state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=['prepare-lease', 'run', 'once'])
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--workdir', type=Path)
    parser.add_argument('--run-id', default=None)
    parser.add_argument('--trigger', choices=['scheduled', 'manual'], default='scheduled',
                        help='prepare-lease only: manual requires a current explicit user request')
    parser.add_argument('--manual-request-id', default=None,
                        help='prepare-lease only: stable idempotency key for the authorized manual request')
    parser.add_argument('--keep-source', action='store_true',
                        help='retain large raw SSZ for an explicitly requested local audit')
    args = parser.parse_args()
    if args.stage != 'prepare-lease' and (args.trigger != 'scheduled' or args.manual_request_id is not None):
        parser.error('trigger and manual request ID belong to prepare-lease; run uses its durable lease')
    run_id = args.run_id or str(uuid.uuid4())
    now = int(time.time())
    base = {name: (args.root / name).read_bytes() for name in (SNAPSHOT_PATH, STATE_PATH)
            if (args.root / name).is_file()}
    old = json.loads(base[SNAPSHOT_PATH])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.stage == 'prepare-lease':
        proposed, skipped = propose_lease(old, now, run_id, trigger=args.trigger,
                                         manual_request_id=args.manual_request_id)
        result = manifest('skipped' if skipped else 'acquire', run_id, base,
                          {} if skipped else {SNAPSHOT_PATH: proposed}, result=skipped)
        args.output.write_text(canonical(result))
        print(canonical({k: result[k] for k in ('stage', 'runId', 'result')}))
        return 0
    if args.stage == 'run':
        validate_lease(old, now, run_id)
    else:
        # A local verification run does not acquire or authorize a remote lease.
        old = deepcopy(old)
        old['refresh'] = {**old.get('refresh', {}), 'attemptAt': utc(now), 'trigger': 'verification'}
    workdir = args.workdir or Path(tempfile.mkdtemp(prefix='lido-refresh-'))
    workdir.mkdir(parents=True, exist_ok=True)
    lock = open(workdir / 'collector.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    def expired(_signum, _frame):
        raise TimeoutError('collector exceeded the 25-minute deadline')
    signal.signal(signal.SIGALRM, expired)
    signal.alarm(MAX_RUN_SECONDS)
    try:
        snapshot, state = collect(args.root, old, run_id, workdir)
        if args.stage == 'run':
            validate_lease(old, int(time.time()), run_id)
        result = manifest('complete', run_id, base, {SNAPSHOT_PATH: snapshot, STATE_PATH: state},
                          result={'asOf': snapshot['asOf'], 'blockNumber': snapshot['blockNumber']})
        code = 0
    except Exception as error:
        snapshot = failure_snapshot(old, int(time.time()), error)
        result = manifest('failed', run_id, base, {SNAPSHOT_PATH: snapshot}, error=str(error)[:500])
        code = 1
    finally:
        signal.alarm(0)
    result['localVerificationOnly'] = args.stage == 'once'
    args.output.write_text(canonical(result))
    # Source bytes are reproducible acquisition scratch, not a durable input.
    # The output manifest and small audit remain available for a blocked publish.
    # Do not let recurring or manual runs accumulate large raw SSZ files.
    if not args.keep_source:
        (workdir / 'state.ssz').unlink(missing_ok=True)
    print(canonical({k: result[k] for k in ('stage', 'runId', 'result', 'error')}))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
