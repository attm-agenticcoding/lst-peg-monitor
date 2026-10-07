#!/usr/bin/env python3
"""Prepare a daily or explicitly requested manual lease, then collect a snapshot.

Only the small, explicit manifest is eligible for publication. External writers
must acquire the lease with a GitHub contents-SHA compare-and-swap, and publish
the final files in one non-force, fast-forward commit after checking lease owner.
This command never uses a wallet, credentials, eth_call, or private user data.
"""
import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
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
import threading
import time
import urllib.request
import urllib.error
import uuid

from scenarios import build_scenarios, report_references, utc, eth
from acquisition_errors import (MAX_EXCEPTION_NODES, exception_nodes, safe_exception,
                                sanitized_text, transport_read_timeout)
from rpc_budget import DEFAULT_RPC_CALLS, lease_rpc_budget, validate_budget

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_PATH = 'data/lido-cutoff-snapshot.json'
STATE_PATH = 'data/lido-collector-state.json'
MAX_RUN_SECONDS = 1500
LEASE_SECONDS = 2100
MAX_SOURCE_AGE = 5400
EXPECTED_INTERVAL_SECONDS = 86400
MAX_OPERATIONAL_AGE = EXPECTED_INTERVAL_SECONDS + MAX_SOURCE_AGE
MAX_MANUAL_ATTEMPTS = 128
RPC_URL = 'https://rpc.mevblocker.io'
ALLOWED_RPC = {'eth_getLogs', 'eth_getBlockByNumber', 'eth_getBalance'}
MAX_RPC_CALLS = DEFAULT_RPC_CALLS
HEADER_WORKERS = 4
RPC_ADMISSION_SECONDS = 2.0


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


def propose_lease(snapshot, now, run_id, *, trigger='scheduled', manual_request_id=None,
                  rpc_call_budget=DEFAULT_RPC_CALLS):
    if trigger not in ('scheduled', 'manual'):
        raise ValueError('invalid refresh trigger')
    if trigger == 'manual':
        if not isinstance(manual_request_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,159}', manual_request_id):
            raise ValueError('manual refresh requires a stable explicit manual request ID')
    elif manual_request_id is not None:
        raise ValueError('manual request ID is only valid for an explicit manual trigger')
    validate_budget(rpc_call_budget, trigger)
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
                  'rpcCallBudget': rpc_call_budget,
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
    lease_rpc_budget(snapshot)


def failure_snapshot(snapshot, now, error):
    """Only status changes: original asOf, scenarios and evidence stay unchanged."""
    result = deepcopy(snapshot)
    refresh = result.setdefault('refresh', {})
    refresh.update(state='error', finishedAt=utc(now), lease=None,
                   error=(safe_exception(error)['message'] if isinstance(error, BaseException)
                          else sanitized_text(str(error)))[:500],
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


class RpcRedirectError(urllib.error.HTTPError):
    """The configured source requested an unapproved destination."""


class NoRpcRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A new source needs explicit review, even if the original source points at it.
        raise RpcRedirectError(req.full_url, code, 'RPC redirect refused', headers, fp)


def open_rpc(request, *, timeout):
    return urllib.request.build_opener(NoRpcRedirect()).open(request, timeout=timeout)


class Rpc:
    def __init__(self, deadline, *, clock=None, admission_wait=None):
        self.deadline, self.calls, self.evidence = deadline, 0, []
        self.failures = []
        self._requests = []
        self._lock = threading.Lock()
        self._admission = threading.Condition(self._lock)
        self._clock = clock if clock is not None else lambda: time.monotonic()
        # Dependency injection is only for offline clocks. Production has no
        # CLI/environment option to change or disable the shared fixed pace.
        self._admission_wait = admission_wait or (lambda condition, seconds: condition.wait(seconds))
        self._last_admission = None
        self._in_flight = 0
        self._error = None

    @property
    def max_calls(self):
        return MAX_RPC_CALLS

    def cancel(self, error=None):
        with self._lock:
            if self._error is None:
                self._error = error if error is not None else RuntimeError('RPC collection cancelled')
            self._admission.notify_all()
            return self._error

    def _record_failure(self, request, error, kind='source'):
        with self._lock:
            self.failures.append({'request': request, 'error': str(error)[:500],
                                  'kind': kind,
                                  'code': None if kind == 'cancelled' else getattr(error, 'code', None),
                                  'denied': kind != 'cancelled' and bool(getattr(error, 'denied', False))})
            self.failures.sort(key=lambda row: row['request']['id'])

    def diagnostics(self):
        with self._lock:
            completed = {r['request']['id'] for r in self.evidence + self.failures}
            return deepcopy({'source': RPC_URL, 'capturedAt': utc(int(time.time())),
                             'calls': self.calls, 'requests': self._requests,
                             'responses': self.evidence, 'failures': self.failures,
                             'inFlightIds': [r['id'] for r in self._requests if r['id'] not in completed]})

    def _check(self):
        if self._error is not None:
            raise self._error
        if self._clock() >= self.deadline:
            raise TimeoutError('bounded RPC deadline exhausted')

    def _wait_admission(self, *, not_before=None):
        """Called under _lock; every wait releases it so a peer can stop us.

        A single next-admission time serves every method and worker. Idle time
        earns no burst credit, and waiting spends the original compute budget.
        """
        while True:
            self._check()
            if self.calls >= self.max_calls:
                raise RuntimeError('bounded RPC budget exhausted')
            remaining = self.deadline - self._clock()
            if self._in_flight >= HEADER_WORKERS:
                self._admission.wait(remaining)
                continue
            delay = (0 if self._last_admission is None else
                     self._last_admission + RPC_ADMISSION_SECONDS - self._clock())
            if not_before is not None:
                delay = max(delay, not_before - self._clock())
            if delay <= 0:
                return
            self._admission_wait(self._admission, min(delay, remaining))

    def _release_admission(self):
        with self._admission:
            self._in_flight -= 1
            self._admission.notify_all()

    def _event_header(self, number):
        from execution import _header
        return _header(self, number)

    def fetch_headers(self, numbers):
        """Fetch only the model's timestamp headers, never its final rechecks.

        Admit at most four reads at once. Every admitted request consumes the
        shared budget. A failed/cancelled batch is discarded in its entirety.
        """
        from execution import _header
        numbers = sorted(set(numbers))
        if any(type(n) is not int or n < 0 for n in numbers):
            raise ValueError('invalid event header number')
        with self._lock:
            self._check()
            # Three pinned balances and three final seed/target/child rechecks.
            if self.calls + len(numbers) + 6 > self.max_calls:
                raise RuntimeError('bounded RPC budget cannot cover event headers and final checks')
        if not numbers:
            return {}
        pool = ThreadPoolExecutor(max_workers=HEADER_WORKERS)
        pending, answer = {}, {}
        remaining = iter(numbers)
        try:
            for number in list(numbers)[:HEADER_WORKERS]:
                next(remaining)
                pending[pool.submit(self._event_header, number)] = number
            while pending:
                with self._lock:
                    self._check()
                done, _ = wait(pending, timeout=max(0, self.deadline - self._clock()),
                               return_when=FIRST_COMPLETED)
                if not done:
                    raise TimeoutError('bounded RPC deadline exhausted')
                # Check all completed futures before admitting replacement work.
                for future in done:
                    answer[pending.pop(future)] = future.result()
                with self._lock:
                    self._check()
                for _ in range(len(done)):
                    number = next(remaining, None)
                    if number is None:
                        break
                    pending[pool.submit(self._event_header, number)] = number
            with self._lock:
                self._check()
            return answer
        except BaseException as error:
            first_error = self.cancel(error)
            for future in pending:
                future.cancel()
            # Do not extend the calculation deadline waiting for network teardown.
            # In-flight calls cannot admit more work or return an accepted result.
            raise first_error
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def __call__(self, method, params):
        from execution import RpcError
        if method not in ALLOWED_RPC:
            raise RuntimeError('RPC method outside approved collector scope: ' + method)
        with self._lock:
            self._wait_admission()
            self.calls += 1
            ident = self.calls
            self._last_admission = self._clock()
            self._in_flight += 1
            request = {'jsonrpc': '2.0', 'id': ident, 'method': method, 'params': params}
            self._requests.append(deepcopy(request))
        # HTTP 401/403 and JSON-RPC errors stop the run. No provider/method bypass.
        try:
            req = urllib.request.Request(RPC_URL, data=json.dumps(request).encode(),
                                         headers={'Content-Type': 'application/json',
                                                  'User-Agent': 'lst-peg-monitor/1.0'})
            with self._lock:
                self._check()
                remaining = self.deadline - self._clock()
            with open_rpc(req, timeout=min(40, remaining)) as response:
                raw = response.read(20 * 1024 * 1024 + 1)
            with self._lock:
                self._check()
            if len(raw) > 20 * 1024 * 1024:
                raise RuntimeError('RPC result exceeds bounded response size')
            value = json.loads(raw)
            if not isinstance(value, dict) or value.get('id') != ident or value.get('jsonrpc') != '2.0':
                raise RpcError('mismatched JSON-RPC response envelope')
            if 'error' in value or 'result' not in value:
                error = value.get('error', {})
                code = error.get('code') if isinstance(error, dict) else None
                raise RpcError('RPC source failed: ' + str(error or 'missing result'),
                               code=code, denied=code in (401, 403))
            with self._lock:
                self._check()
                self.evidence.append({'request': request, 'responseSha256': hashlib.sha256(raw).hexdigest()})
                self.evidence.sort(key=lambda row: row['request']['id'])
            return value['result']
        except urllib.error.HTTPError as error:
            failed = RpcError(f'{method}: HTTP {error.code}; source access failed',
                              code=error.code, denied=(error.code in (401, 403)
                                                       or isinstance(error, RpcRedirectError)))
            first_error = self.cancel(failed)
            self._record_failure(request, failed)
            raise first_error from error
        except Exception as error:
            with self._lock:
                cancelled = error is self._error
            # Only the documented log-result size limit permits bounded splitting.
            splittable = (method == 'eth_getLogs' and isinstance(error, RpcError)
                          and error.code == -32005 and not error.denied)
            first_error = None if splittable else self.cancel(error)
            self._record_failure(request, error, kind='cancelled' if cancelled else 'source')
            if first_error is not None:
                raise first_error
            raise
        finally:
            self._release_admission()


class RecoveryPaused(RuntimeError):
    """Collection stopped and requires an explicit reviewed same-route resume."""


class _RetryReadTimeout(Exception):
    """Unwind the finished callback before using its sealed retry allowance."""


def classify_acquisition_error(error, first_error, phase):
    """Classify only evidenced transport wrappers, never timing or similar text.

    urllib can wrap a locally raised exception more than once. Only an exact
    stored cancellation object beneath otherwise pure URLError wrappers proves
    peer cancellation. New socket errors, statuses, validation errors and
    incidental Python exception contexts remain independent source failures.
    """
    from execution import RpcError
    nodes = list(exception_nodes(error, include_context=False))
    original = list(exception_nodes(first_error, include_context=False)) if first_error is not None else []
    original_ids = {id(node) for node in original}
    fresh = [node for node in nodes if id(node) not in original_ids]
    identity = first_error is not None and any(node is first_error for node in nodes)
    pure_wrappers = all(isinstance(node, urllib.error.URLError)
                        and not isinstance(node, urllib.error.HTTPError)
                        and isinstance(node.reason, BaseException) for node in fresh)
    cancelled = bool(identity and (error is first_error or
                                   (len(nodes) < MAX_EXCEPTION_NODES
                                    and phase in ('open', 'read') and pure_wrappers)))
    # A fresh actual status always wins over a cancellation object elsewhere in
    # the explicit wrapper graph. Never reinterpret origin 403 as proxy denial.
    for node in fresh:
        if isinstance(node, urllib.error.HTTPError):
            redirected = isinstance(node, RpcRedirectError) or 300 <= node.code < 400
            if node.code in (401, 403) or redirected:
                return {'category': 'redirect' if redirected else 'http_error',
                        'code': node.code, 'denied': True, 'knownCancellation': False}
        if isinstance(node, RpcError) and node.denied:
            return {'category': 'source_error', 'code': node.code,
                    'denied': True, 'knownCancellation': False}
    if cancelled:
        return {'category': 'cancelled', 'code': None, 'denied': False,
                'knownCancellation': True}
    # Guard/controller exceptions are generated locally, not inferred from a
    # generic source error merely because another request already paused.
    if isinstance(error, RecoveryPaused) or getattr(error, 'pausable', False):
        return {'category': 'controller_interruption', 'code': None,
                'denied': False, 'knownCancellation': False}
    for node in fresh:
        if isinstance(node, urllib.error.HTTPError):
            return {'category': 'http_error', 'code': node.code,
                    'denied': False, 'knownCancellation': False}
    def tunnel_leaf(node):
        plain_tunnel_error = type(node) is OSError and node.errno is None
        string_url_reason = isinstance(node, urllib.error.URLError) and isinstance(node.reason, str)
        return ((plain_tunnel_error or string_url_reason)
                and 'tunnel connection failed' in safe_exception(node)['message'].lower())

    def transparent_url_wrapper(node):
        return (isinstance(node, urllib.error.URLError)
                and not isinstance(node, urllib.error.HTTPError)
                and isinstance(node.reason, BaseException))

    # Only this attempt's new transport graph can identify a tunnel failure.
    # The stored first error may itself have an old tunnel cause; borrowing that
    # cause would incorrectly turn a new timeout/reset into another pause.
    tunnel = (len(nodes) < MAX_EXCEPTION_NODES and phase == 'open'
              and isinstance(error, urllib.error.URLError)
              and any(tunnel_leaf(node) for node in fresh)
              and all(transparent_url_wrapper(node) or tunnel_leaf(node) for node in fresh))
    if tunnel:
        return {'category': 'unknown_tunnel_rejection', 'code': None,
                'denied': False, 'knownCancellation': False}
    if transport_read_timeout(error, phase):
        return {'category': 'transport_read_timeout', 'code': None,
                'denied': False, 'knownCancellation': False}
    code = getattr(error, 'code', None)
    return {'category': 'source_error', 'code': code if type(code) is int else None,
            'denied': bool(getattr(error, 'denied', False)), 'knownCancellation': False}


class DurableRpc(Rpc):
    """Run-scoped raw evidence, with fresh authentication on every execution.

    The execution engine still performs its full replay and final sandwich.
    Only its explicit event-header batch and bounded historical log ranges can
    read sealed raw bytes. Initial/final headers and balances always go online.
    """

    def __init__(self, journal, state, summary, *, admission_wait=None):
        self.journal = journal
        super().__init__(journal.deadline, clock=lambda: journal.clock()['monotonic'],
                         admission_wait=admission_wait)
        # Includes failed and unknown attempts, in all prior same-run epochs.
        # The journal verifies boot identity, monotonic continuity and seal.
        self._last_admission = journal.last_admission_monotonic
        self.state, self.summary = deepcopy(state), deepcopy(summary)
        self._role = threading.local()
        self._anchors = {}
        self._authenticated = False
        self._callbacks = set()
        self._quiescent = threading.Condition()
        self._sync_evidence()

    @property
    def calls(self):
        return self.journal.calls

    @property
    def max_calls(self):
        return self.journal.max_calls

    @calls.setter
    def calls(self, _value):
        pass

    def _sync_evidence(self):
        diagnostic = self.journal.diagnostics()
        self.evidence = diagnostic['responses']
        self.failures = diagnostic['failures']

    def diagnostics(self):
        return self.journal.diagnostics()

    def cancel(self, error=None):
        # Admission itself can race a peer's durable pause, before transport's
        # try/except begins. Normalize at the shared first-error boundary too.
        if getattr(error, 'pausable', False) or (error is None and self.journal.status == 'paused'):
            error = RecoveryPaused('same-route acquisition interrupted; explicit review required before resume')
        return super().cancel(error)

    def _check(self):
        super()._check()
        try:
            self.journal.check()
        except Exception as error:
            if getattr(error, 'pausable', False):
                raise RecoveryPaused('same-route acquisition interrupted; explicit review required before resume') from error
            raise

    def _event_header(self, number):
        self._role.value = 'event_header'
        try:
            return super()._event_header(number)
        finally:
            self._role.value = None

    def fetch_headers(self, numbers):
        # Cached bytes are revalidated before sizing the outstanding source work.
        # The six finishing reads retain their original shared-budget reserve.
        answer, missing = {}, []
        for number in sorted(set(numbers)):
            if type(number) is not int or number < 0:
                raise ValueError('invalid event header number')
            params = [hex(number), False]
            hit, value = self.journal.cached('event_header', 'eth_getBlockByNumber', params,
                                             self._validator('event_header', params))
            if hit:
                answer[number] = value
            else:
                missing.append(number)
        answer.update(super().fetch_headers(missing))
        self._sync_evidence()
        return answer

    def drain(self):
        """Retain both local locks until dispatched network callbacks exit."""
        while True:
            try:
                with self._quiescent:
                    if not self._callbacks:
                        return
                    self._quiescent.wait(timeout=1)
                if self._clock() >= self.deadline:
                    self.journal.stop('original deadline expired during network teardown')
            except (TimeoutError, RecoveryPaused) as error:
                # A signal during teardown must not drop ownership. The reads
                # have bounded socket timeouts and recheck cancellation between
                # chunks; only their teardown may finish after the deadline.
                self.cancel(error)
                if isinstance(error, TimeoutError):
                    self.journal.stop(str(error))
                else:
                    self.journal.pause(str(error))

    def _read_response(self, response):
        cap, chunks, size = 20 * 1024 * 1024, [], 0
        reader = getattr(response, 'read1', response.read)
        while True:
            with self._lock:
                self._check()
            chunk = reader(min(64 * 1024, cap + 1 - size))
            with self._lock:
                self._check()
            if not chunk:
                return b''.join(chunks)
            chunks.append(chunk)
            size += len(chunk)
            if size > cap:
                raise RuntimeError('RPC result exceeds bounded response size')

    def _validator(self, role, params):
        from execution import ADDRESSES, MONITORED, _hash, integer, key

        def validate(value):
            if role == 'historical_logs':
                if not isinstance(value, list):
                    raise RuntimeError('eth_getLogs did not return a list')
                query = params[0]
                lo, hi = int(query['fromBlock'], 16), int(query['toBlock'], 16)
                allowed = {ADDRESSES[k] for k in MONITORED}
                for log in value:
                    if (not isinstance(log, dict) or not lo <= key(log)[0] <= hi
                            or log.get('address', '').lower() not in allowed
                            or log.get('removed', False)):
                        raise RuntimeError('invalid historical log scope or removed log')
                    _hash(log.get('blockHash'))
                    _hash(log.get('transactionHash'))
                return True
            if role == 'balance':
                integer(value)
                return True
            if not isinstance(value, dict):
                raise RuntimeError('missing execution header')
            height = integer(value.get('number'))
            _hash(value.get('hash'))
            integer(value.get('timestamp'))
            if params[0] != 'finalized' and height != int(params[0], 16):
                raise RuntimeError('mismatched execution header number')
            return True
        return validate

    def _observe_anchor(self, params, value):
        from execution import integer
        seed, target = int(self.state['block']), int(self.summary['block_number'])
        number = params[0] if params[0] == 'finalized' else int(params[0], 16)
        if number == seed:
            if (value['hash'].lower() != self.state['hash'].lower()
                    or integer(value['timestamp']) != int(self.state['timestamp'])):
                raise RuntimeError('durable seed header identity changed')
        if number == target:
            if (value['hash'].lower() != self.summary['block_hash'].lower()
                    or integer(value['timestamp']) != int(self.summary['timestamp'])):
                raise RuntimeError('SSZ execution target identity changed')
        if number == target + 1:
            if (value.get('parentHash', '').lower() != self.summary['block_hash'].lower()
                    or integer(value['timestamp']) <= int(self.summary['timestamp'])
                    or value.get('parentBeaconBlockRoot', '').lower()
                    != self.summary['derived_beacon_block_header_root'].lower()):
                raise RuntimeError('SSZ execution child authentication changed')
        self._anchors[number] = deepcopy(value)
        if not self._authenticated and all(n in self._anchors for n in (seed, target, target + 1, 'finalized')):
            finalized = self._anchors['finalized']
            height = integer(finalized['number'])
            if height < target + 1:
                return  # Preserve the engine's bounded successful finality polls.
            if height == target + 1 and finalized['hash'].lower() != self._anchors[target + 1]['hash'].lower():
                raise RuntimeError('finalized child hash changed')
            self.journal.mark_authenticated()
            self._authenticated = True

    def __call__(self, method, params):
        if method not in ALLOWED_RPC:
            raise RuntimeError('RPC method outside approved collector scope: ' + method)
        role = getattr(self._role, 'value', None)
        if role is None:
            role = ('historical_logs' if method == 'eth_getLogs' else
                    'balance' if method == 'eth_getBalance' else
                    'terminal' if self._authenticated else 'anchor')
        validator = self._validator(role, params)
        # Each attempt exits its callback and releases its in-flight slot before
        # cooldown/admission. No recursive transport or whole-run restart.
        while True:
            try:
                return self._call_once(role, method, params, validator)
            except _RetryReadTimeout:
                continue

    def _call_once(self, role, method, params, validator):
        from execution import RpcError
        with self._lock:
            self._check()
            hit, value = self.journal.cached(role, method, params, validator)
            if hit:
                return value
            self._wait_admission(not_before=self.journal.timeout_retry_not_before(role, method, params))
            ident, raw_request = self.journal.reserve(role, method, params,
                                                       remaining_required=6 if role in ('historical_logs', 'event_header') else 0)
            self._last_admission = self.journal.last_admission_monotonic
            self.journal.start_callback(ident)
            self._in_flight += 1
            with self._quiescent:
                self._callbacks.add(ident)
        phase = 'open'
        try:
            request = urllib.request.Request(RPC_URL, data=raw_request,
                                             headers={'Content-Type': 'application/json',
                                                      'User-Agent': 'lst-peg-monitor/1.0'})
            with self._lock:
                self._check()
            with open_rpc(request, timeout=min(40, self.journal.remaining_seconds)) as response:
                phase = 'read'
                raw = self._read_response(response)
            phase = 'response'
            value = self.journal.finish(ident, raw, validator)
            phase = 'anchor'
            if method == 'eth_getBlockByNumber' and role != 'event_header':
                self._observe_anchor(params, value)
            self._sync_evidence()
            return value
        except BaseException as error:
            if isinstance(error, RpcError) and method == 'eth_getLogs' and error.code == -32005 and not error.denied:
                raise  # The journal charged the valid error; the engine splits.
            # Read the immutable first-error reference without waiting for the
            # admission lock: a peer may hold it while observing journal pause.
            # cancel() remains the synchronized sole writer of this reference.
            first_error = self._error
            classification = classify_acquisition_error(error, first_error, phase)
            category = classification['category']
            if category == 'transport_read_timeout':
                with self._lock:
                    if self._error is None and self.journal.retry_timeout(ident, error, phase):
                        raise _RetryReadTimeout() from error
                # Exhausted/ineligible timeouts are fatal. Publish the original
                # error before a peer can observe the terminal journal guard.
                self.cancel(error)
            if category == 'controller_interruption' and self.journal.status == 'paused':
                category = 'cancelled'  # A local guard observed a peer's pause.
            access_error = None
            if classification['denied'] or category == 'http_error':
                # Publish the source error before the journal becomes terminal:
                # a pacing waiter must not replace it with a generic Stopped.
                access_error = self.cancel(RpcError(f'{method}: source access failed',
                                                   code=classification['code'],
                                                   denied=classification['denied']))
            try:
                self.journal.fail(ident, category=category, code=classification['code'],
                                  denied=classification['denied'], error=error,
                                  phase=phase, known_cancel=classification['knownCancellation'])
            except Exception as journal_error:
                if not classification['denied']:
                    raise
                # Persistence failure cannot conceal an actual access denial.
                print(json.dumps({'step': 'rpc_failure_journal_failed',
                                  'error': safe_exception(journal_error)['message']}), flush=True)
            if access_error is not None:
                raise access_error from error
            if category in ('unknown_tunnel_rejection', 'controller_interruption'):
                paused = RecoveryPaused('same-route acquisition interrupted; explicit review required before resume')
                raise self.cancel(paused) from error
            if category == 'cancelled':
                # Preserve the exact first error once established. A wrapper is
                # evidence of cancellation, not a second source failure.
                raise self.cancel(first_error if first_error is not None else error)
            raise self.cancel(error)
        finally:
            self.journal.finish_callback(ident)
            with self._quiescent:
                self._callbacks.discard(ident)
                self._quiescent.notify_all()
            self._release_admission()


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


def collect(root, old, run_id, workdir, now=None, *, journal=None):
    from beacon_hourly import download_state, inspect_state, simulate_reports
    from execution import collect_at
    state = load_json(root / STATE_PATH)
    state_file = workdir / 'state.ssz'
    if journal is not None and journal.source is not None:
        if state_file.resolve() != Path(journal.source['path']).resolve():
            raise RuntimeError('original SSZ path does not belong to this run work directory')
        acquisition = deepcopy(journal.source['acquisition'])
        summary = deepcopy(journal.source['inspection'])
    else:
        acquisition = download_state(state_file)
        print(json.dumps({'step': 'state_downloaded', 'bytes': acquisition['bytes']}), flush=True)
        summary = inspect_state(state_file)
        if journal is not None:
            journal.pin_source(state_file, acquisition, summary)
    deadline = journal.deadline if journal is not None else time.monotonic() + MAX_RUN_SECONDS - 30
    rpc = DurableRpc(journal, state, summary) if journal is not None else Rpc(deadline)
    print(json.dumps({'step': 'state_authenticated', 'blockNumber': summary['block_number'],
                      'asOf': utc(int(summary['timestamp']))}), flush=True)
    current = int(time.time()) if now is None else now
    timestamp = int(summary['timestamp'])
    if timestamp <= second(old['asOf']):
        raise RuntimeError('source has not advanced beyond the last successful snapshot')
    if timestamp > current or current - timestamp > MAX_SOURCE_AGE:
        raise RuntimeError('consensus source snapshot is future-dated or older than 90 minutes')
    try:
        execution = collect_at(state, summary['block_number'], summary['block_hash'], rpc)
    except BaseException:
        if journal is not None:
            rpc.cancel()
            rpc.drain()
        # Also preserve failed attempts; in-flight teardown is labelled explicitly.
        # A diagnostic disk error must not conceal a source-access denial.
        try:
            (workdir / 'rpc-audit.json').write_text(canonical(rpc.diagnostics()))
        except OSError as audit_error:
            print(json.dumps({'step': 'rpc_audit_write_failed', 'error': str(audit_error)[:300]}), flush=True)
        raise
    else:
        if journal is not None:
            rpc.drain()
        (workdir / 'rpc-audit.json').write_text(canonical(rpc.diagnostics()))
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
    provenance = {'beaconAcquisition': acquisition, 'rpcSource': RPC_URL,
                  'rpc': rpc.evidence, 'rpcFailures': rpc.failures}
    finished = int(time.time()) if now is None else now
    if finished - timestamp > MAX_SOURCE_AGE:
        raise RuntimeError('snapshot exceeded 90-minute age before calculation finished')
    if journal is not None:
        journal.check()
        rpc._sync_evidence()
    output = public_snapshot(old, state, execution, beacon, scenarios, finished, run_id, provenance)
    (workdir / 'audit.json').write_text(canonical({'execution': execution, 'beacon': beacon,
                                                'scenarios': scenarios, 'provenance': provenance}))
    return output, state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=['prepare-lease', 'run', 'resume', 'abort', 'once'])
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--workdir', type=Path)
    parser.add_argument('--run-id', default=None)
    parser.add_argument('--head', help='fresh main commit SHA supplied by the external controller')
    parser.add_argument('--current-root', type=Path,
                        help='freshly materialized current-main data and collector code; required for resume/abort')
    parser.add_argument('--attest-current-main', action='store_true',
                        help='controller attests current-root/head were freshly fetched for this invocation')
    parser.add_argument('--reviewed-same-route',
                        help='resume only: caller attestation identifying the explicit same-route command review')
    parser.add_argument('--seal-path', type=Path,
                        help='independent controller seal, outside the recovery journal directory')
    parser.add_argument('--abort-reason', default='controller deliberately aborted the original run')
    parser.add_argument('--trigger', choices=['scheduled', 'manual'], default='scheduled',
                        help='prepare-lease only: manual requires a current explicit user request')
    parser.add_argument('--manual-request-id', default=None,
                        help='prepare-lease only: stable idempotency key for the authorized manual request')
    parser.add_argument('--rpc-call-budget', type=int, choices=[256, 320], default=None,
                        help='prepare-lease only: 320 requires an explicitly approved manual catch-up run')
    parser.add_argument('--keep-source', action='store_true',
                        help='retain large raw SSZ for an explicitly requested local audit')
    args = parser.parse_args()
    if args.stage != 'prepare-lease' and (args.trigger != 'scheduled' or args.manual_request_id is not None):
        parser.error('trigger and manual request ID belong to prepare-lease; run uses its durable lease')
    if args.stage != 'prepare-lease' and args.rpc_call_budget is not None:
        parser.error('RPC budget belongs to prepare-lease; run/resume inherit the immutable original lease')
    if args.rpc_call_budget == 320 and args.trigger != 'manual':
        parser.error('320 calls require an explicitly approved manual catch-up lease')
    if args.stage in ('resume', 'abort'):
        if not (args.run_id and args.workdir and args.current_root and args.head and args.attest_current_main):
            parser.error('resume/abort require --run-id, --workdir, --current-root, --head and --attest-current-main')
        if args.stage == 'resume' and not args.reviewed_same_route:
            parser.error('resume requires --reviewed-same-route with the actual caller review reference')
        args.root = args.current_root
    elif args.reviewed_same_route or args.current_root or args.attest_current_main:
        parser.error('fresh-current and reviewed-resume arguments belong to resume/abort')
    if args.head is not None and not re.fullmatch(r'[0-9a-f]{40}', args.head):
        parser.error('--head must be an exact 40-character main commit SHA')
    run_id = args.run_id or str(uuid.uuid4())
    now = int(time.time())
    base = {name: (args.root / name).read_bytes() for name in (SNAPSHOT_PATH, STATE_PATH)
            if (args.root / name).is_file()}
    old = json.loads(base[SNAPSHOT_PATH])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.stage == 'prepare-lease':
        proposed, skipped = propose_lease(old, now, run_id, trigger=args.trigger,
                                         manual_request_id=args.manual_request_id,
                                         rpc_call_budget=args.rpc_call_budget or DEFAULT_RPC_CALLS)
        result = manifest('skipped' if skipped else 'acquire', run_id, base,
                          {} if skipped else {SNAPSHOT_PATH: proposed}, result=skipped)
        args.output.write_text(canonical(result))
        print(canonical({k: result[k] for k in ('stage', 'runId', 'result')}))
        return 0
    if args.stage == 'once':
        old = deepcopy(old)
        old['refresh'] = {**old.get('refresh', {}), 'attemptAt': utc(now), 'trigger': 'verification'}
    workdir = args.workdir or Path(tempfile.mkdtemp(prefix='lido-refresh-'))
    workdir.mkdir(parents=True, exist_ok=True)
    with open(workdir / 'collector.lock', 'a') as lock:
        # A competing live process must never emit a lease-release manifest.
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        journal = None
        hard_deadline = time.monotonic() + MAX_RUN_SECONDS
        interrupted = False

        def expired(_signum, _frame):
            raise TimeoutError('collector exceeded the original 25-minute deadline')

        def controller_interrupted(_signum, _frame):
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                raise RecoveryPaused('controller interrupted collection; explicit same-route review required')

        previous_signals = {}
        for signum, handler in ((signal.SIGALRM, expired), (signal.SIGINT, controller_interrupted),
                                (signal.SIGTERM, controller_interrupted)):
            previous_signals[signum] = signal.signal(signum, handler)
        try:
            if args.stage != 'once':
                from recovery import RunJournal
                validate_lease(old, int(time.time()), run_id)
                common = dict(root=args.root, head=args.head, run_id=run_id, base_blobs=base,
                              endpoint=RPC_URL, route_id='urllib-mevblocker-v1', seal_path=args.seal_path)
                if args.stage == 'run':
                    journal = RunJournal.create(workdir / 'recovery', code_root=Path(__file__).parent, **common)
                else:
                    from beacon_hourly import inspect_state
                    journal = RunJournal.open(workdir / 'recovery', fresh_main=args.attest_current_main,
                                              reviewed_same_route=args.reviewed_same_route,
                                              inspect_ssz=inspect_state, abort_only=args.stage == 'abort', **common)
                hard_deadline = journal.deadline
                if args.stage == 'abort':
                    raise RuntimeError(args.abort_reason[:500])
            remaining = hard_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('collector exceeded the original 25-minute deadline')
            signal.alarm(max(1, int(remaining)))
            snapshot, state = collect(args.root, old, run_id, workdir, journal=journal)
            if time.monotonic() >= hard_deadline:
                raise TimeoutError('collector exceeded the original 25-minute deadline')
            if args.stage != 'once':
                validate_lease(old, int(time.time()), run_id)
                journal.check()
            result = manifest('complete', run_id, base, {SNAPSHOT_PATH: snapshot, STATE_PATH: state},
                              result={'asOf': snapshot['asOf'], 'blockNumber': snapshot['blockNumber']})
            if time.monotonic() >= hard_deadline:
                raise TimeoutError('collector exceeded the original 25-minute deadline')
            if journal is not None:
                journal.complete()
            if time.monotonic() >= hard_deadline:
                raise TimeoutError('collector exceeded the original 25-minute deadline while sealing completion')
            code = 0
        except BaseException as error:
            if getattr(error, 'busy', False):
                raise  # Ownership cannot move while any old callback is active.
            if getattr(error, 'pausable', False):
                error = RecoveryPaused(str(error))
            if journal is not None and isinstance(error, RecoveryPaused) and journal.source is not None:
                try:
                    journal.check(allow_paused=True)
                    journal.pause(str(error))
                except Exception as guard_error:
                    error = guard_error
            pausable = (journal is not None and journal.source is not None
                        and journal.status == 'paused' and isinstance(error, RecoveryPaused)
                        and args.stage != 'abort')
            if pausable:
                # This has no publishable files and does not release the durable lease.
                result = manifest('paused', run_id, base, {}, error=(safe_exception(error)['message'])[:500])
                result['recovery'] = {'calls': journal.calls, 'seal': journal.seal,
                                      'rpcCallBudget': journal.max_calls,
                                      'sameRouteReviewRequired': True,
                                      'originalDeadlineMonotonic': journal.deadline}
                code = 2
            else:
                if journal is not None:
                    try:
                        journal.stop(str(error)[:500])
                    except Exception as stop_error:
                        # A corrupt/expired journal still needs an actual failure
                        # manifest; it can never be used as partial source state.
                        print(json.dumps({'step': 'journal_stop_failed',
                                          'error': str(stop_error)[:300]}), flush=True)
                    if getattr(journal, 'denied', False):
                        from execution import RpcError
                        error = RpcError(str(error), denied=True)
                snapshot = failure_snapshot(old, int(time.time()), error)
                result = manifest('failed', run_id, base, {SNAPSHOT_PATH: snapshot}, error=(safe_exception(error)['message'])[:500])
                code = 1
        finally:
            signal.alarm(0)
            for signum, handler in previous_signals.items():
                signal.signal(signum, handler)
        result['localVerificationOnly'] = args.stage == 'once'
        # Retain source bytes only for a pending same-run recovery or explicit audit.
        # Any output-write failure retains scratch, so it cannot erase the only
        # reviewable evidence before a failure manifest has been durably saved.
        from recovery import atomic_write
        if journal is not None:
            # Refusal here must precede even exposing a lease-release manifest.
            # collector.lock remains held through manifest write and cleanup.
            journal.close()
        atomic_write(args.output, canonical(result).encode())
        if result['stage'] != 'paused' and not args.keep_source and (journal is not None or args.stage == 'once'):
            (workdir / 'state.ssz').unlink(missing_ok=True)
        print(canonical({k: result[k] for k in ('stage', 'runId', 'result', 'error')}))
        return code


if __name__ == '__main__':
    raise SystemExit(main())
