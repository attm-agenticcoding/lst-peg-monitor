"""Bounded, same-run durable acquisition journal for the Lido collector.

The controller supplies fresh main inputs and a reference to its same-route
review on reopen. These are explicit controller attestations, not cryptographic
permission grants. The local filesystem and independent seal are trusted;
coordinated rollback of both is outside this module's integrity boundary.
There is no transport, alternate source, checkpoint, or publication operation.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from urllib.parse import urlsplit
from acquisition_errors import safe_exception, sanitized_text

MAX_CALLS = 256
MAX_SECONDS = 1500
LEASE_SECONDS = 2100
MAX_SOURCE_AGE = 5400
MAX_IN_FLIGHT = 4
MAX_RAW = 20 * 1024 * 1024
MAX_RECORD = 64 * 1024 * 1024
MAX_RECORDS = MAX_CALLS * 5 + 64
SNAPSHOT_PATH = 'data/lido-cutoff-snapshot.json'
STATE_PATH = 'data/lido-collector-state.json'
CACHE_ROLES = frozenset(('historical_logs', 'event_header'))
ROLES = CACHE_ROLES | {'anchor', 'balance', 'terminal'}
PAUSABLE = frozenset(('unknown_tunnel_rejection', 'tool_interruption',
                      'controller_interruption'))


class Stopped(RuntimeError):
    """This run cannot admit or accept further acquisition work."""


class Corrupt(Stopped):
    """The durable run identity, content, or high-water mark disagrees."""


class Busy(Stopped):
    """Another callback/owner may still run; do not release its durable lease."""

    busy = True


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def json_value(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise Corrupt('duplicate JSON key')
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(
                              Corrupt('nonfinite JSON number')))
    except (ValueError, TypeError, UnicodeError) as error:
        raise Corrupt('malformed JSON') from error


def file_sha(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def _blob(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_RAW:
        raise Stopped('base input must be bounded original bytes')
    return {'bytes': base64.b64encode(raw).decode('ascii'), 'sha256': sha(raw),
            'git_blob_sha': hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()}


def _unblob(value):
    try:
        raw = base64.b64decode(value['bytes'], validate=True)
        if _blob(raw) != value:
            raise Corrupt('base input bytes/digest mismatch')
        return raw
    except (KeyError, ValueError, TypeError) as error:
        raise Corrupt('malformed base input') from error


def _number(value):
    if not isinstance(value, str) or not re.fullmatch(r'0x(?:0|[1-9a-f][0-9a-f]*)', value):
        raise Stopped('an explicit canonical block quantity is required')
    return int(value, 16)


def _hash(value, size=64):
    return isinstance(value, str) and re.fullmatch(r'0x[0-9a-f]{' + str(size) + '}', value) is not None


def _stamp_valid(stamp):
    return (isinstance(stamp, dict) and set(stamp) == {'wall', 'monotonic', 'identity'}
            and isinstance(stamp['identity'], str) and bool(stamp['identity'])
            and all(type(stamp[k]) in (float, int) and math.isfinite(stamp[k])
                    and stamp[k] >= 0 for k in ('wall', 'monotonic')))


def system_clock():
    """CLOCK_MONOTONIC survives process restarts only within the same boot."""
    try:
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError as error:
        raise Stopped('stable operating-system boot identity unavailable') from error
    if not boot:
        raise Stopped('stable operating-system boot identity is empty')
    # Linux time namespaces can offset monotonic time without changing boot_id.
    try:
        namespace = os.readlink('/proc/self/ns/time')
    except FileNotFoundError:
        # Kernels without time namespaces expose only the boot's clock. Do not
        # turn a permission/read failure into this compatibility fallback.
        namespace = 'no-time-namespace'
    except OSError as error:
        raise Stopped('stable monotonic clock namespace unavailable') from error
    return {'wall': time.time(), 'monotonic': time.monotonic(),
            'identity': boot + ':' + namespace}


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic(path, raw):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.journal-', dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        _fsync_dir(path.parent)
    finally:
        temp.unlink(missing_ok=True)


atomic_write = _atomic


def _code_directory(root, *, creating=False):
    root = Path(root).resolve()
    if (root / 'scripts/lido').is_dir():
        return root / 'scripts/lido'
    if (root / 'refresh.py').is_file():
        return root
    if creating:
        return Path(__file__).resolve().parent
    raise Corrupt('fresh current root has no collector source directory')


def code_digests(root):
    """Hash the actual collector/model source bytes, never just a commit label."""
    root = Path(root).resolve()
    paths = sorted(p for p in root.rglob('*')
                   if p.is_file() and p.suffix in ('.py', '.cpp')
                   and '__pycache__' not in p.parts)
    if not paths or not (root / 'refresh.py').is_file():
        raise Corrupt('collector code files are missing')
    return {str(p.relative_to(root)): file_sha(p) for p in paths}


class LocalSealStore:
    """Atomic/fsynced high-water mark outside the replaceable journal tree."""

    def __init__(self, path, directory=None):
        self.path = Path(path).resolve()
        if directory is not None and self.path.is_relative_to(Path(directory).resolve()):
            raise Stopped('controller seal must be outside the journal directory')

    def read(self):
        try:
            if self.path.stat().st_size > 4096:
                raise Corrupt('oversize controller seal')
            value = json_value(self.path.read_bytes())
        except OSError as error:
            raise Corrupt('independent controller seal is missing') from error
        if (not isinstance(value, dict) or set(value) != {'run_id', 'records', 'head', 'attempts'}
                or not isinstance(value['run_id'], str)
                or type(value['records']) is not int or not 0 < value['records'] <= MAX_RECORDS
                or type(value['attempts']) is not int or not 0 <= value['attempts'] <= MAX_CALLS
                or not isinstance(value['head'], str)
                or not re.fullmatch('[0-9a-f]{64}', value['head'])):
            raise Corrupt('invalid independent controller seal')
        return value

    def write(self, value):
        if self.path.exists():
            old = self.read()
            if (old['run_id'] != value['run_id'] or value['records'] != old['records'] + 1
                    or not old['attempts'] <= value['attempts'] <= old['attempts'] + 1):
                raise Corrupt('independent seal must advance the same run once')
        elif value['records'] != 1 or value['attempts'] != 0:
            raise Corrupt('independent seal disappeared')
        _atomic(self.path, encoded(value) + b'\n')

    __call__ = write


def _seal_path(directory, seal_path):
    directory = Path(directory).resolve()
    return Path(seal_path) if seal_path is not None else directory.with_name(directory.name + '.seal.json')


def _read_ledger(directory, seal):
    """Validate the complete sealed chain, including full-record truncation."""
    rows, previous, attempts, run_id = [], '0' * 64, 0, None
    try:
        with (Path(directory) / 'ledger.jsonl').open('rb') as ledger:
            while True:
                raw = ledger.readline(MAX_RECORD + 1)
                if not raw:
                    break
                if len(raw) > MAX_RECORD or not raw.endswith(b'\n') or len(rows) >= MAX_RECORDS:
                    raise Corrupt('truncated or excessive journal record')
                row = json_value(raw)
                if (not isinstance(row, dict)
                        or set(row) != {'seq', 'previous', 'kind', 'data', 'stamp', 'hash'}
                        or type(row['seq']) is not int or row['seq'] != len(rows) + 1
                        or row['previous'] != previous
                        or row['hash'] != sha(encoded({k: v for k, v in row.items() if k != 'hash'}))
                        or not _stamp_valid(row['stamp']) or not isinstance(row['data'], dict)):
                    raise Corrupt('journal sequence, record, clock or hash mismatch')
                if not rows:
                    if row['kind'] != 'begin':
                        raise Corrupt('missing original begin record')
                    run_id = row['data'].get('context', {}).get('run_id')
                else:
                    last = rows[-1]['stamp']
                    if (row['stamp']['identity'] != last['identity']
                            or any(row['stamp'][k] < last[k] for k in ('wall', 'monotonic'))):
                        raise Corrupt('journal clock identity changed or regressed')
                attempts += row['kind'] == 'reserve'
                rows.append(row)
                previous = row['hash']
    except OSError as error:
        raise Corrupt('journal ledger is missing or unreadable') from error
    if not rows or seal != {'run_id': run_id, 'records': len(rows), 'head': previous, 'attempts': attempts}:
        raise Corrupt('independent journal seal mismatch')
    return rows


def read_context(directory, seal_path=None):
    """Read immutable originals only from a complete independently sealed chain."""
    store = LocalSealStore(_seal_path(directory, seal_path), directory)
    rows = _read_ledger(directory, store.read())
    return deepcopy(rows[0]['data']['context'])


class RunJournal:
    read_context = staticmethod(read_context)

    @classmethod
    def create(cls, directory, *, root, run_id, base_blobs, endpoint, route_id,
               head=None, seal_path=None, code_root=None, clock=system_clock):
        directory = Path(directory).resolve()
        store = LocalSealStore(_seal_path(directory, seal_path), directory)
        if store.path.exists():
            raise Stopped('independent seal already exists; a run cannot be recreated')
        directory.mkdir(parents=True, exist_ok=False)
        self = cls(directory, store, clock)
        try:
            now = self._stamp()
            if set(base_blobs) != {SNAPSHOT_PATH, STATE_PATH}:
                raise Stopped('exactly the original snapshot and collector-state bytes are required')
            seed = json_value(base_blobs[STATE_PATH])
            lease = json_value(base_blobs[SNAPSHOT_PATH]).get('refresh', {}).get('lease')
            from execution import ADDRESSES, MONITORED
            code_root = Path(code_root).resolve() if code_root is not None else _code_directory(root, creating=True)
            context = {'schema': 1, 'run_id': run_id, 'head': head,
                       'endpoint': endpoint, 'route_id': route_id,
                       'base_blobs': {name: _blob(raw) for name, raw in base_blobs.items()},
                       'code_digests': code_digests(code_root), 'lease': deepcopy(lease),
                       'start_wall': now['wall'], 'start_monotonic': now['monotonic'],
                       'deadline_wall': now['wall'] + MAX_SECONDS,
                       'deadline_monotonic': now['monotonic'] + MAX_SECONDS,
                       'clock_identity': now['identity'],
                       'seed': {'number': seed['block'], 'hash': seed['hash'], 'timestamp': seed['timestamp']},
                       'log_addresses': [ADDRESSES[k] for k in MONITORED],
                       'balance_addresses': [ADDRESSES[k] for k in ('core', 'el_vault', 'withdrawal_vault')]}
            self._context, self._context_bytes = context, encoded(context)
            self._current_code_root = code_root
            self._validate_context()
            self.validate_running_code()
            self._append('begin', context=deepcopy(context))
            self.check()
            return self
        except BaseException:
            self.close()
            raise

    @classmethod
    def open(cls, directory, *, root, head, run_id, base_blobs, endpoint, route_id,
             fresh_main=False, reviewed_same_route=None, inspect_ssz=None, seal_path=None,
             code_root=None, clock=system_clock, abort_only=False):
        self = cls(Path(directory).resolve(),
                   LocalSealStore(_seal_path(directory, seal_path), directory), clock)
        ledger_denied = False
        try:
            rows = _read_ledger(self.directory, self._store.read())
            # A later corrupt/missing raw file must not conceal a denial already
            # present in this independently verified ledger.
            ledger_denied = any(
                row['kind'] in ('failure', 'stop') and
                (row['data'].get('denied') is True
                 or row['data'].get('code') in (401, 403)
                 or 'redirect' in str(row['data'].get('category', '')))
                for row in rows)
            self._context = deepcopy(rows[0]['data']['context'])
            self._context_bytes = encoded(self._context)
            self._validate_context()
            for row in rows:
                self._apply(row)
                self.records.append(row)
            if (fresh_main is not True or (not abort_only and (
                    not isinstance(reviewed_same_route, str)
                    or not reviewed_same_route.strip() or len(reviewed_same_route) > 2000))):
                raise Stopped('fresh-main and explicit reviewed same-route controller attestations required')
            if not isinstance(head, str) or not re.fullmatch('[0-9a-f]{40}', head):
                raise Stopped('fresh main commit SHA required on resume')
            if (run_id != self._context['run_id'] or endpoint != self._context['endpoint']
                    or route_id != self._context['route_id']):
                raise Corrupt('run identity, source endpoint or route changed')
            if set(base_blobs) != {SNAPSHOT_PATH, STATE_PATH}:
                raise Corrupt('both fresh current data blobs are required')
            root = Path(root).resolve()
            for name, original in self._context['base_blobs'].items():
                if _blob(base_blobs[name]) != original or (root / name).read_bytes() != base_blobs[name]:
                    raise Corrupt('fresh main snapshot/state bytes or original lease changed')
            current_code = _code_directory(root)
            if (code_digests(current_code) != self._context['code_digests']
                    or (code_root is not None
                        and code_digests(code_root) != self._context['code_digests'])):
                raise Corrupt('actual current collector/model code bytes changed')
            self._current_code_root = current_code
            self.validate_running_code()
            if abort_only:
                self._abort_only = True
                return self
            self.check(allow_paused=True)
            if self._source is None:
                raise Stopped('no sealed original SSZ source; this run cannot resume')
            if not callable(inspect_ssz):
                raise Stopped('complete SSZ reinspection is required on resume')
            self._check_source(inspect_ssz)
            if self.calls + 4 + 6 > MAX_CALLS:
                raise Stopped('remaining shared budget cannot cover fresh anchors and completion')
            self._append('resume', head=head, fresh_main=True,
                         reviewed_same_route=reviewed_same_route.strip(),
                         previous_epoch=self.epoch)
            self.check()
            return self
        except BaseException as error:
            if self.denied or ledger_denied:
                error.denied = True
            self.close()
            raise

    def __init__(self, directory, store, clock):
        self.directory, self._store, self.clock = directory, store, clock
        self._mutex = threading.RLock()
        try:
            self._lock = (directory / 'owner.lock').open('a+b')
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if hasattr(self, '_lock'):
                self._lock.close()
            if isinstance(error, BlockingIOError):
                raise Busy('another process still owns the journal') from error
            raise Stopped('journal unavailable') from error
        self.records, self.requests, self.responses, self.failures = [], {}, {}, {}
        self._context, self._context_bytes, self._source = None, None, None
        self._active_callbacks, self._local_live, self._accepted = set(), set(), set()
        self.status, self.epoch, self.denied, self._poisoned = 'new', 0, False, False
        self._cache_reuses = []
        self._abort_only = False
        self._current_code_root = None

    @property
    def context(self):
        return deepcopy(self._context)

    @property
    def source(self):
        return deepcopy(self._source)

    @property
    def calls(self):
        return len(self.requests)

    @property
    def deadline(self):
        return self._context['deadline_monotonic']

    @property
    def remaining_seconds(self):
        with self._mutex:
            now = self.check()
            return min(self.deadline - now['monotonic'],
                       self._context['deadline_wall'] - now['wall'],
                       self._context['lease']['expiresAtEpoch'] - now['wall'])

    @property
    def seal(self):
        return {'run_id': self._context['run_id'], 'records': len(self.records),
                'head': self.records[-1]['hash'] if self.records else '0' * 64,
                'attempts': self.calls}

    def _stamp(self):
        now = self.clock()
        if not _stamp_valid(now):
            raise Stopped('invalid stable clock snapshot')
        return now

    def _validate_context(self):
        c = self._context
        try:
            endpoint = urlsplit(c['endpoint'])
            if (encoded(c) != self._context_bytes or c['schema'] != 1
                    or not isinstance(c['run_id'], str) or not c['run_id']
                    or not isinstance(c['route_id'], str) or not c['route_id']
                    or endpoint.scheme != 'https' or not endpoint.hostname
                    or endpoint.username or endpoint.password or endpoint.fragment
                    or (c['head'] is not None and not re.fullmatch('[0-9a-f]{40}', c['head']))
                    or c['deadline_wall'] != c['start_wall'] + MAX_SECONDS
                    or c['deadline_monotonic'] != c['start_monotonic'] + MAX_SECONDS
                    or not isinstance(c['clock_identity'], str) or not c['clock_identity']
                    or set(c['base_blobs']) != {SNAPSHOT_PATH, STATE_PATH}
                    or not c['code_digests']):
                raise Corrupt('invalid immutable original run context')
            for blob in c['base_blobs'].values():
                _unblob(blob)
            lease = c['lease']
            if (not isinstance(lease, dict) or lease.get('runId') != c['run_id']
                    or type(lease.get('acquiredAtEpoch')) is not int
                    or type(lease.get('expiresAtEpoch')) is not int
                    or lease['expiresAtEpoch'] != lease['acquiredAtEpoch'] + LEASE_SECONDS
                    or lease['acquiredAtEpoch'] > c['start_wall']
                    or lease != json_value(_unblob(c['base_blobs'][SNAPSHOT_PATH])).get('refresh', {}).get('lease')):
                raise Corrupt('original durable 35-minute lease is missing or changed')
            seed = json_value(_unblob(c['base_blobs'][STATE_PATH]))
            if (c['seed'] != {'number': seed['block'], 'hash': seed['hash'], 'timestamp': seed['timestamp']}
                    or type(seed['block']) is not int or seed['block'] < 0
                    or type(seed['timestamp']) is not int or not _hash(seed['hash'])):
                raise Corrupt('invalid original seed identity')
        except (KeyError, TypeError, ValueError) as error:
            raise Corrupt('malformed immutable run context') from error

    def check(self, *, allow_paused=False):
        with self._mutex:
            if self._abort_only:
                raise Stopped('journal was opened for controller abort only')
            if self._lock.closed or self._poisoned:
                raise Stopped('journal closed or durable outcome uncertain')
            self._validate_context()
            now, c = self._stamp(), self._context
            if (now['identity'] != c['clock_identity']
                    or now['wall'] < c['start_wall'] or now['monotonic'] < c['start_monotonic']
                    or abs((now['wall'] - c['start_wall']) - (now['monotonic'] - c['start_monotonic'])) > 5
                    or (self.records and any(now[k] < self.records[-1]['stamp'][k]
                                             for k in ('wall', 'monotonic')))):
                raise Stopped('original boot/monotonic/wall clock changed or regressed')
            if (now['wall'] >= min(c['deadline_wall'], c['lease']['expiresAtEpoch'])
                    or now['monotonic'] >= c['deadline_monotonic']):
                raise Stopped('original computation deadline or durable lease expired')
            if self._source is not None and not 0 <= now['wall'] - self._source['inspection']['timestamp'] <= MAX_SOURCE_AGE:
                raise Stopped('original SSZ target is future-dated or older than 90 minutes')
            if self.status in ('terminal', 'complete') or (self.status == 'paused' and not allow_paused):
                error = Stopped('run is ' + self.status)
                error.denied = self.denied
                error.pausable = self.status == 'paused' and not self.denied
                raise error
            return now

    def _append(self, kind, **data):
        if self._lock.closed or self._poisoned or len(self.records) >= MAX_RECORDS:
            raise Stopped('journal closed, exhausted, or durability uncertain')
        stamp = self._stamp()
        if stamp['identity'] != self._context['clock_identity'] or (self.records and any(
                stamp[k] < self.records[-1]['stamp'][k] for k in ('wall', 'monotonic'))):
            raise Stopped('journal clock changed or regressed before append')
        row = {'seq': len(self.records) + 1, 'previous': self.seal['head'],
               'kind': kind, 'data': data, 'stamp': stamp}
        row['hash'] = sha(encoded(row))
        raw = encoded(row) + b'\n'
        if len(raw) > MAX_RECORD:
            raise Stopped('journal record exceeds bound')
        try:
            with (self.directory / 'ledger.jsonl').open('ab', buffering=0) as out:
                if out.write(raw) != len(raw):
                    raise OSError('short ledger write')
                os.fsync(out.fileno())
            _fsync_dir(self.directory)
            self._apply(row)
            self.records.append(row)
            self._store.write(self.seal)
        except BaseException:
            self._poisoned = True
            raise

    def _apply(self, row):
        kind, data = row['kind'], row['data']
        if kind == 'begin':
            if self.records or encoded(data['context']) != self._context_bytes:
                raise Corrupt('run context changed')
            self.status = 'acquiring'
        elif kind == 'source':
            if self.status != 'acquiring' or self._source is not None or self.requests:
                raise Corrupt('original source was replaced')
            self._validate_source(data['source'])
            self._source, self.status = deepcopy(data['source']), 'anchors'
        elif kind == 'reserve':
            req = data['request']
            ident = req['id']
            if (type(ident) is not int or ident != self.calls + 1 or ident > MAX_CALLS
                    or req.get('jsonrpc') != '2.0' or data['epoch'] != self.epoch
                    or sha(encoded(req)) != data['requestSha256']):
                raise Corrupt('invalid durable attempt reservation')
            self._scope(data['role'], req['method'], req['params'])
            self.requests[ident] = deepcopy(data)
        elif kind == 'response':
            ident = data['id']
            if ident not in self.requests or ident in self.responses or ident in self.failures:
                raise Corrupt('orphan or duplicate complete response')
            if type(data.get('cacheable')) is not bool:
                raise Corrupt('invalid complete-response cache classification')
            if data['cacheable'] and self.requests[ident]['role'] not in CACHE_ROLES:
                raise Corrupt('fresh-only response incorrectly marked reusable')
            self._read_raw(data)
            self.responses[ident] = deepcopy(data)
            self._local_live.discard(ident)
        elif kind == 'failure':
            ident = data['id']
            if ident not in self.requests or ident in self.responses or ident in self.failures:
                raise Corrupt('orphan or duplicate failed attempt')
            self.failures[ident] = deepcopy(data)
            self._local_live.discard(ident)
            category, code = data['category'], data.get('code')
            if category in ('limit_exceeded', 'rpc_limit'):
                if self.requests[ident]['request']['method'] != 'eth_getLogs' or code != -32005:
                    raise Corrupt('only log -32005 permits bounded split')
            elif category != 'cancelled':
                self.denied |= bool(data.get('denied')) or code in (401, 403) or 'redirect' in category
                if self.status != 'terminal':
                    self.status = 'paused' if category in PAUSABLE and not self.denied else 'terminal'
                self._accepted.clear()
        elif kind in ('pause', 'stop'):
            if self.status == 'complete':
                raise Corrupt('completed run cannot change state')
            self.denied |= bool(data.get('denied'))
            if self.status != 'terminal':
                self.status = 'terminal' if kind == 'stop' or self.denied else 'paused'
            self._accepted.clear()
        elif kind == 'resume':
            if self.status in ('terminal', 'complete') or self._source is None or data['previous_epoch'] != self.epoch:
                raise Corrupt('invalid same-run resume')
            self.epoch += 1
            self.status = 'anchors'
            self._accepted.clear()
        elif kind == 'authenticated':
            if self.status != 'anchors' or data['epoch'] != self.epoch:
                raise Corrupt('invalid fresh authentication transition')
            self.status = 'collecting'
        elif kind == 'complete':
            if self.status != 'collecting' or data['epoch'] != self.epoch:
                raise Corrupt('invalid completion transition')
            self.status = 'complete'
        else:
            raise Corrupt('unknown durable journal record')

    def _validate_source(self, source):
        try:
            summary, acquisition = source['inspection'], source['acquisition']
            c = self._context
            if (not _hash(summary['state_root']) or not _hash(summary['derived_beacon_block_header_root'])
                    or not _hash(summary['block_hash'])
                    or type(summary['block_number']) is not int
                    or summary['block_number'] <= c['seed']['number']
                    or type(summary['timestamp']) is not int
                    or summary['timestamp'] <= c['seed']['timestamp']
                    or source['sha256'] != summary['state_sha256']
                    or not re.fullmatch('[0-9a-f]{64}', source['sha256'])
                    or type(source['bytes']) is not int or not 0 < source['bytes'] <= 450 * 1024 ** 2
                    or source['bytes'] != summary['state_bytes'] or source['bytes'] != acquisition['bytes']
                    or not isinstance(source['path'], str) or not Path(source['path']).is_absolute()
                    or not acquisition['source_url'].startswith('https://')
                    or not math.floor(c['start_wall']) <= acquisition['retrieval_started_timestamp']
                    <= acquisition['retrieved_timestamp'] <= source['pinned_wall']
                    or source['pinned_wall'] < c['start_wall']
                    or not 0 <= source['pinned_wall'] - summary['timestamp'] <= MAX_SOURCE_AGE):
                raise Corrupt('SSZ bytes, acquisition, root or target binding changed')
        except (KeyError, TypeError, ValueError) as error:
            raise Corrupt('malformed pinned SSZ source') from error

    def _check_source(self, inspect_ssz=None):
        if self._source is None:
            raise Corrupt('original sealed SSZ source missing')
        self._validate_source(self._source)
        path = Path(self._source['path'])
        try:
            if path.stat().st_size != self._source['bytes'] or file_sha(path) != self._source['sha256']:
                raise Corrupt('original SSZ bytes changed')
            if inspect_ssz is not None and encoded(inspect_ssz(path)) != encoded(self._source['inspection']):
                raise Corrupt('original SSZ root or target inspection changed')
        except OSError as error:
            raise Corrupt('original SSZ bytes missing') from error

    def pin_source(self, path, acquisition, inspection):
        with self._mutex:
            now = self.check()
            if self.status != 'acquiring' or self._source is not None:
                raise Stopped('this run already pinned its only SSZ source')
            path = Path(path).resolve()
            source = {'path': str(path), 'sha256': file_sha(path), 'bytes': path.stat().st_size,
                      'pinned_wall': now['wall'], 'acquisition': deepcopy(acquisition),
                      'inspection': deepcopy(inspection)}
            self._validate_source(source)
            source = json_value(encoded(source))
            # The source must itself be durable before committing its identity.
            with path.open('rb') as stream:
                os.fsync(stream.fileno())
            _fsync_dir(path.parent)
            self.check()
            self._append('source', source=source)

    def _scope(self, role, method, params):
        if role not in ROLES or self._source is None:
            raise Stopped('pinned source and explicit acquisition role required')
        seed, target = self._context['seed']['number'], self._source['inspection']['block_number']
        if method == 'eth_getLogs' and role == 'historical_logs':
            if (not isinstance(params, list) or len(params) != 1 or not isinstance(params[0], dict)
                    or set(params[0]) != {'address', 'fromBlock', 'toBlock'}
                    or params[0]['address'] != self._context['log_addresses']
                    or not seed < _number(params[0]['fromBlock']) <= _number(params[0]['toBlock']) <= target):
                raise Stopped('historical range or addresses outside pinned run')
        elif method == 'eth_getBlockByNumber' and role in ('anchor', 'terminal', 'event_header'):
            if not isinstance(params, list) or len(params) != 2 or params[1] is not False:
                raise Stopped('invalid fixed header request')
            if params[0] == 'finalized' and role == 'anchor':
                return
            number = _number(params[0])
            if role == 'event_header':
                if not seed < number < target:
                    raise Stopped('event header outside historical interior')
            elif number not in (seed, target, target + 1):
                raise Stopped('fresh header outside seed/target/child identity')
        elif method == 'eth_getBalance' and role == 'balance':
            if (not isinstance(params, list) or len(params) != 2
                    or params[0] not in self._context['balance_addresses'] or _number(params[1]) != target):
                raise Stopped('balance outside pinned target identity')
        else:
            raise Stopped('method/role combination outside approved collector scope')

    def reserve(self, role, method, params, *, remaining_required=0):
        with self._mutex:
            self.check()
            self._scope(role, method, params)
            if self.status != 'collecting' and not (role == 'anchor' and self.status == 'anchors'):
                raise Stopped('fresh anchor authentication required')
            if type(remaining_required) is not int or remaining_required < 0:
                raise Stopped('invalid remaining completion budget')
            reserve_after = max(remaining_required, 3 if role in CACHE_ROLES | {'balance'} else 0)
            if self.calls + 1 + reserve_after > MAX_CALLS:
                raise Stopped('shared attempt budget cannot cover request and completion')
            if len(self._local_live) >= MAX_IN_FLIGHT:
                raise Stopped('four current-process RPC reservations already live')
            request = {'jsonrpc': '2.0', 'id': self.calls + 1, 'method': method, 'params': deepcopy(params)}
            raw = encoded(request)
            self._append('reserve', role=role, request=request, requestSha256=sha(raw), epoch=self.epoch)
            self._local_live.add(request['id'])
            return request['id'], raw

    def start_callback(self, ident):
        with self._mutex:
            self.check()
            if (ident not in self._local_live or ident in self._active_callbacks
                    or self.requests[ident]['epoch'] != self.epoch
                    or len(self._active_callbacks) >= MAX_IN_FLIGHT):
                raise Stopped('callback has no live reservation or four callbacks remain active')
            self._active_callbacks.add(ident)

    def finish_callback(self, ident):
        with self._mutex:
            self._active_callbacks.discard(ident)

    def _read_raw(self, data):
        try:
            expected = f"responses/{data['id']}-{data['responseSha256']}.json"
            if (data['file'] != expected or type(data['bytes']) is not int
                    or not 0 < data['bytes'] <= MAX_RAW
                    or not re.fullmatch('[0-9a-f]{64}', data['responseSha256'])):
                raise Corrupt('invalid bounded raw-response reference')
            path = self.directory / expected
            if path.stat().st_size != data['bytes']:
                raise Corrupt('sealed response byte length changed')
            raw = path.read_bytes()
            if len(raw) != data['bytes'] or sha(raw) != data['responseSha256']:
                raise Corrupt('sealed complete-response digest changed')
            return raw
        except (KeyError, OSError, TypeError) as error:
            raise Corrupt('sealed complete response missing or malformed') from error

    def _result(self, ident, raw):
        value = json_value(raw)
        if (not isinstance(value, dict) or value.get('jsonrpc') != '2.0'
                or type(value.get('id')) is not int or value['id'] != ident):
            raise Corrupt('mismatched JSON-RPC response envelope')
        if 'error' in value:
            from execution import RpcError
            error = value['error']
            code = error.get('code') if isinstance(error, dict) else None
            # An explicit denial remains terminal even inside a malformed
            # envelope. Only a valid, exclusive error may authorize log split.
            if code not in (401, 403) and ('result' in value or not isinstance(error, dict)
                    or type(code) is not int or not isinstance(error.get('message'), str)):
                raise Corrupt('malformed JSON-RPC error envelope')
            raise RpcError('RPC source failed: ' + str(error)[:500], code=code,
                           denied=code in (401, 403))
        if 'result' not in value:
            raise Corrupt('JSON-RPC response has no result')
        return value['result']

    def finish(self, ident, raw, validator, *, cacheable=True):
        """Seal a whole successful envelope; callback validates result semantics.

        A late success may be retained after a peer pauses, but cannot be returned
        as accepted work. A reopened epoch revalidates every reusable result.
        """
        with self._mutex:
            if ident not in self.requests or ident in self.responses or ident in self.failures:
                raise Stopped('request has no unfinished charged attempt')
            phase = 'envelope'
            try:
                if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_RAW:
                    raise Corrupt('response exceeds bounded complete-byte size')
                result = self._result(ident, raw)
                phase = 'validation'
                if validator(deepcopy(result)) is not True:
                    raise Corrupt('response semantic validation failed')
            except Exception as error:
                code = getattr(error, 'code', None)
                denial = bool(getattr(error, 'denied', False) or code in (401, 403))
                if denial:
                    # Never hide the observed denial behind an audit disk error.
                    self.denied, self.status = True, 'terminal'
                    self._accepted.clear()
                category = ('limit_exceeded' if self.requests[ident]['request']['method'] == 'eth_getLogs'
                            and code == -32005 and not getattr(error, 'denied', False) else 'invalid_response')
                try:
                    self.fail(ident, category=category, code=code, error=error, denied=denial, phase=phase)
                except Exception as persistence_error:
                    if not denial:
                        raise
                    error.add_note('Recording the terminal source denial also failed: ' + str(persistence_error)[:300])
                raise
            req = self.requests[ident]
            reusable = bool(cacheable and req['role'] in CACHE_ROLES)
            if req['role'] == 'historical_logs' and (not isinstance(result, list) or len(result) >= 10000):
                reusable = False
            digest = sha(raw)
            relative = f'responses/{ident}-{digest}.json'
            _atomic(self.directory / relative, raw)
            self._append('response', id=ident, file=relative, bytes=len(raw), responseSha256=digest,
                         cacheable=reusable, acquiredAt=self._stamp()['wall'])
            self.check()
            if req['epoch'] != self.epoch:
                raise Stopped('late prior-epoch response retained as raw only')
            self._accepted.add(ident)
            return deepcopy(result)

    def fail(self, ident, category, code=None, *, error=None, denied=False,
             phase=None, known_cancel=False):
        with self._mutex:
            if not isinstance(category, str) or not category:
                raise Stopped('failure category required')
            detail = safe_exception(error) if isinstance(error, BaseException) else None
            message = detail['message'] if detail is not None else sanitized_text(error or category)
            metadata = ({'exception': detail} if detail is not None else {})
            if phase is not None:
                if phase not in ('open', 'read', 'response', 'envelope', 'validation', 'anchor', 'cache'):
                    raise Stopped('unsupported acquisition failure phase')
                metadata['phase'] = phase
            if detail is not None or phase is not None:
                metadata['knownCancellation'] = known_cancel is True
            actual_denial = bool(denied or code in (401, 403) or 'redirect' in category)
            if actual_denial:
                self.denied, self.status = True, 'terminal'
                self._accepted.clear()
            if ident in self.failures:
                if actual_denial and not self.failures[ident].get('denied'):
                    self._append('stop', reason=message, denied=True, **metadata)
                return
            if ident in self.responses:
                # The response may have been sealed before a peer's stop check.
                # It remains a response; cancellation cannot rewrite history.
                if actual_denial:
                    self._append('stop', reason=message, denied=True, **metadata)
                elif category != 'cancelled' and self.status not in ('paused', 'terminal'):
                    self._append('stop', reason=message, denied=actual_denial, **metadata)
                return
            if ident not in self.requests:
                raise Stopped('failed attempt was not durably reserved')
            if category in ('limit_exceeded', 'rpc_limit') and (
                    self.requests[ident]['request']['method'] != 'eth_getLogs' or code != -32005):
                raise Stopped('only log -32005 permits same-source bounded splitting')
            self._append('failure', id=ident, category=category, code=code,
                         error=message, denied=actual_denial, **metadata)

    def cached(self, role, method, params, validator):
        with self._mutex:
            self.check()
            self._scope(role, method, params)
            if role not in CACHE_ROLES:
                return False, None
            if self.status != 'collecting':
                raise Stopped('retained source bytes require fresh anchor authentication')
            for ident, req in self.requests.items():
                response = self.responses.get(ident)
                if (req['role'] != role or req['request']['method'] != method
                        or req['request']['params'] != params or not response or not response['cacheable']):
                    continue
                try:
                    result = self._result(ident, self._read_raw(response))
                    if validator(deepcopy(result)) is not True:
                        raise Corrupt('cached result failed fresh semantic validation')
                except Exception as error:
                    self._append('stop', reason='cached_response_validation_failed', denied=False,
                                 phase='cache', exception=safe_exception(error))
                    raise
                self.check()
                self._accepted.add(ident)
                self._cache_reuses.append({'id': ident, 'epoch': self.epoch})
                return True, deepcopy(result)
            return False, None

    def mark_authenticated(self):
        with self._mutex:
            self.check()
            if self.status != 'anchors' or self._local_live:
                raise Stopped('fresh authentication must finish before historical acquisition')
            seed, target = self._context['seed']['number'], self._source['inspection']['block_number']
            required = {hex(seed), hex(target), hex(target + 1), 'finalized'}
            accepted = [ident for ident in self._accepted
                        if self.requests[ident]['epoch'] == self.epoch
                        and self.requests[ident]['role'] == 'anchor']
            actual = {self.requests[ident]['request']['params'][0] for ident in accepted}
            if not required <= actual:
                raise Stopped('four fresh accepted seed/target/child/finalized anchors are required')
            # Exact EIP-4788 root, timestamps and finality are checked by caller.
            self._append('authenticated', epoch=self.epoch, anchor_ids=sorted(accepted))

    def pause(self, reason):
        with self._mutex:
            if self.status in ('paused', 'terminal'):
                return
            self._append('pause', reason=sanitized_text(reason))

    def stop(self, reason, *, denied=False):
        with self._mutex:
            if self.status == 'terminal' and (self.denied or not denied):
                return
            self._append('stop', reason=sanitized_text(reason), denied=bool(denied))

    def complete(self):
        """Caller has replayed/validated the full model; no checkpoint is stored."""
        with self._mutex:
            self.check()
            if self.status != 'collecting' or self._active_callbacks or self._local_live:
                raise Stopped('cannot complete while acquisition is incomplete or active')
            terminal = list(range(self.calls - 2, self.calls + 1))
            seed, target = self._context['seed']['number'], self._source['inspection']['block_number']
            for ident, number in zip(terminal, (seed, target, target + 1)):
                req = self.requests.get(ident, {})
                if (ident not in self._accepted or req.get('epoch') != self.epoch
                        or req.get('role') != 'terminal'
                        or req['request']['params'] != [hex(number), False]):
                    raise Stopped('completion requires the three fresh terminal rereads')
            balances = {req['request']['params'][0] for ident, req in self.requests.items()
                        if ident in self._accepted and req['epoch'] == self.epoch and req['role'] == 'balance'}
            if balances != set(self._context['balance_addresses']):
                raise Stopped('completion requires all three fresh target balances')
            self.validate_running_code()
            self._check_source()
            self.check()
            self._append('complete', epoch=self.epoch)

    def validate_running_code(self):
        """Check both the fresh materialization and the executing module tree."""
        expected = self._context['code_digests']
        if (code_digests(Path(__file__).resolve().parent) != expected
                or (self._current_code_root is not None
                    and code_digests(self._current_code_root) != expected)):
            raise Corrupt('actual executing or current collector/model code changed')

    def diagnostics(self):
        with self._mutex:
            responses = [{'request': deepcopy(self.requests[ident]['request']),
                          'responseSha256': data['responseSha256'],
                          'role': self.requests[ident]['role'], 'epoch': self.requests[ident]['epoch'],
                          'acquiredAt': data['acquiredAt']}
                         for ident, data in sorted(self.responses.items())]
            failures = [{'request': deepcopy(self.requests[ident]['request']),
                         'error': data['error'], 'kind': data['category'],
                         'code': data['code'], 'denied': data['denied'],
                         **{k: deepcopy(data[k]) for k in ('exception', 'phase', 'knownCancellation') if k in data}}
                        for ident, data in sorted(self.failures.items())]
            completed = set(self.responses) | set(self.failures)
            return {'source': self._context['endpoint'],
                    'capturedAt': datetime.fromtimestamp(self._stamp()['wall'], timezone.utc).isoformat().replace('+00:00', 'Z'),
                    'calls': self.calls,
                    'requests': [deepcopy(data['request']) for _, data in sorted(self.requests.items())],
                    'responses': responses, 'failures': failures,
                    'inFlightIds': sorted(set(self.requests) - completed),
                    'status': self.status, 'epoch': self.epoch, 'denied': self.denied,
                    'seal': deepcopy(self.seal), 'cacheReuses': deepcopy(self._cache_reuses)}

    def close(self):
        """Keep flock until every real callback has quiesced; never force-close."""
        with self._mutex:
            if self._active_callbacks:
                raise Busy('cannot close while acquisition callbacks are active; ownership retained')
            if not self._lock.closed:
                fcntl.flock(self._lock, fcntl.LOCK_UN)
                self._lock.close()
