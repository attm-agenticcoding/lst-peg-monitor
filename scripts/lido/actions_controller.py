#!/usr/bin/env python3
"""One daily or explicit manual attempt, one collector, exact-head publication.

Only this controller receives the ephemeral GitHub token. No replacement run,
resume, self-dispatch, credentials on disk, or cross-runner artifacts.
"""
import argparse
import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

from publication import validate_result
from refresh import (SNAPSHOT_PATH, STATE_PATH, blob_sha, canonical, day_key, second,
                     failure_snapshot, manifest, propose_lease, validate_lease)
from rpc_budget import DEFAULT_RPC_CALLS

REPOSITORY = 'attm-agenticcoding/lst-peg-monitor'
REQUEST_PATH = '.github/lido-manual-request.json'
WORKFLOW_PATH = '.github/workflows/lido-manual.yml'
DATA_PATHS = (SNAPSHOT_PATH, STATE_PATH)
API = 'https://api.github.com'
PAGES = 'https://attm-agenticcoding.github.io/lst-peg-monitor/'
SHA = re.compile(r'[0-9a-f]{40}')
MAX_BYTES = 8 * 1024 * 1024
CAS_ATTEMPTS = 3
DAILY_CRON = '0 0 * * *'
RELAY_WORKFLOW_PATH = '.github/workflows/snapshot.yml'


class Stopped(RuntimeError):
    pass


class HeadMoved(Stopped):
    pass


class ChildStillRunning(Stopped):
    """No lease-release publication is permitted without proven quiescence."""


class ApiFailure(Stopped):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ApiFailure('HTTP redirect refused')


def checked_sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise Stopped('invalid Git object identity')
    return value


def child_environment(environment):
    # Allowlist rather than stripping familiar token names. Exclude the Actions
    # runtime token, OIDC URLs, credentials, proxies, Git config and Python hooks.
    allowed = ('PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL', 'TZ', 'LD_LIBRARY_PATH', 'RUNNER_TRACKING_ID')
    return {key: environment[key] for key in allowed if key in environment}


def read_request(raw):
    def unique_keys(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise Stopped('manual request contains a duplicate key')
            value[key] = item
        return value
    value = json.loads(raw, object_pairs_hook=unique_keys)
    if not isinstance(value, dict) or set(value) != {'schema', 'requestId', 'rpcCallBudget'}:
        raise Stopped('manual request must contain only schema, requestId and rpcCallBudget')
    if type(value['schema']) is not int or value['schema'] != 1:
        raise Stopped('unknown manual request schema')
    if type(value['rpcCallBudget']) is not int or value['rpcCallBudget'] not in (256, 320):
        raise Stopped('manual request budget must be 256 or explicitly approved 320')
    request_id = value['requestId']
    if request_id is None:
        if value['rpcCallBudget'] != 256:
            raise Stopped('inert request must retain the ordinary 256 budget')
        return None
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,159}', request_id):
        raise Stopped('manual request requires a stable explicit requestId')
    return value


def validate_invocation(env):
    if (env.get('GITHUB_ACTIONS') != 'true' or env.get('GITHUB_REPOSITORY') != REPOSITORY
            or env.get('GITHUB_REF') != 'refs/heads/main'
            or env.get('GITHUB_EVENT_NAME') not in ('push', 'workflow_dispatch', 'schedule')):
        raise Stopped('only this repository main daily/manual workflow is allowed')
    checked_sha(env.get('GITHUB_SHA'))
    if env.get('GITHUB_SERVER_URL') != 'https://github.com' or env.get('GITHUB_API_URL') != API:
        raise Stopped('unexpected GitHub server')


def validate_schedule_day(schedule, now):
    # Never turn an old queued run into today's attempt or backdate a lease.
    if schedule['requestKey'] != day_key(now):
        raise Stopped('scheduled run crossed its original UTC creation day; no catch-up allowed')


def invocation_mode(environment):
    if environment.get('GITHUB_EVENT_NAME') == 'schedule':
        return 'scheduled'
    if environment.get('GITHUB_EVENT_NAME') != 'workflow_dispatch':
        return 'manual'
    event = json.loads(Path(environment['GITHUB_EVENT_PATH']).read_bytes())
    inputs = event.get('inputs') or {}
    if not isinstance(inputs, dict) or set(inputs) - {'mode', 'attempt_day', 'relay_run_id'}:
        raise Stopped('unexpected dispatch inputs')
    mode = inputs.get('mode', 'manual')
    if mode == 'daily':
        return 'relay-dispatch'
    if mode != 'manual' or inputs.get('attempt_day') or inputs.get('relay_run_id'):
        raise Stopped('invalid manual dispatch inputs')
    return 'manual'


def scheduled_request(api, environment, now, *, relay=False):
    """Bind a first cron or relay daily attempt to GitHub's creation day.

    GitHub does not expose a documented nominal occurrence timestamp. The
    original run's UTC creation day is explicit audit evidence, not an inferred
    on-time cron occurrence. Queueing across midnight and reruns fail closed.
    """
    event = json.loads(Path(environment['GITHUB_EVENT_PATH']).read_bytes())
    run_id = environment.get('GITHUB_RUN_ID', '')
    expected_event = 'workflow_dispatch' if relay else 'schedule'
    inputs = event.get('inputs') or {}
    if (environment.get('GITHUB_EVENT_NAME') != expected_event
            or (not relay and event.get('schedule') != DAILY_CRON)
            or (relay and (invocation_mode(environment) != 'relay-dispatch'
                          or inputs.get('attempt_day') != day_key(now)
                          or not isinstance(inputs.get('relay_run_id'), str)
                          or not re.fullmatch(r'[1-9][0-9]*', inputs.get('relay_run_id', ''))))
            or not re.fullmatch(r'[1-9][0-9]*', run_id)
            or environment.get('GITHUB_RUN_ATTEMPT') != '1'):
        raise Stopped('only an original current-day daily event is allowed')
    run = api.workflow_run(run_id)
    if (type(run.get('id')) is not int or str(run['id']) != run_id
            or run.get('event') != expected_event or run.get('run_attempt') != 1
            or type(run.get('run_attempt')) is not int
            or run.get('path') != WORKFLOW_PATH or run.get('head_branch') != 'main'
            or run.get('head_sha') != environment.get('GITHUB_SHA')
            or run.get('repository', {}).get('full_name') != REPOSITORY):
        raise Stopped('GitHub run provenance does not match the original daily schedule')
    created_at = run.get('created_at')
    if not isinstance(created_at, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', created_at):
        raise Stopped('scheduled run lacks an exact UTC creation timestamp')
    try:
        created = second(created_at)
    except ValueError:
        raise Stopped('scheduled run has an invalid UTC creation timestamp') from None
    if created > now:
        raise Stopped('scheduled run creation timestamp is in the future')
    request = {'trigger': 'relay-dispatch' if relay else 'scheduled', 'requestKey': day_key(created),
               'rpcCallBudget': DEFAULT_RPC_CALLS,
               'githubRunId': run_id, 'githubRunAttempt': 1, 'createdAt': created_at,
               'githubHeadSha': run['head_sha'], 'utcDayBasis': 'github_run_created_at'}
    validate_schedule_day(request, now)
    if relay:
        if (run.get('actor', {}).get('login') != 'github-actions[bot]'
                or run.get('actor', {}).get('type') != 'Bot'):
            raise Stopped('daily fallback must be dispatched by the relay GitHub Actions token')
        parent = api.workflow_run(inputs['relay_run_id'])
        if (type(parent.get('id')) is not int or str(parent['id']) != inputs['relay_run_id']
                or parent.get('path') != RELAY_WORKFLOW_PATH or parent.get('head_branch') != 'main'
                or parent.get('repository', {}).get('full_name') != REPOSITORY
                or parent.get('event') not in ('workflow_dispatch', 'schedule')
                or not isinstance(parent.get('created_at'), str)
                or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', parent['created_at'])
                or second(parent['created_at']) > created):
            raise Stopped('daily dispatch lacks matching snapshot relay provenance')
        # The parent can finish its ordinary handoff while this run is queued.
        # A historical completed relay cannot serve as current sender evidence.
        if parent.get('status') != 'in_progress':
            updated = parent.get('updated_at')
            if (parent.get('status') != 'completed' or not isinstance(updated, str)
                    or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', updated)
                    or second(updated) < created):
                raise Stopped('referenced relay was not active for this daily dispatch')
        request.update(relayRunId=inputs['relay_run_id'], dispatchAttemptDay=inputs['attempt_day'],
                       relayHeadSha=checked_sha(parent.get('head_sha')))
    else:
        request['schedule'] = DAILY_CRON
    return request


def is_code(path):
    return path.startswith('scripts/lido/') and Path(path).suffix in ('.py', '.cpp')


def is_price(path):
    return (path in ('data/history.json', 'data/legacy/history-v1.json')
            or re.fullmatch(r'data/archive/\d{4}-\d{2}-\d{2}\.jsonl(?:\.gz)?', path) is not None)


@dataclass
class Revision:
    head: str
    tree: dict
    files: dict


def require_price_only(base, current):
    changed = {path for path in base.tree.keys() | current.tree.keys()
               if base.tree.get(path) != current.tree.get(path)}
    if any(not is_price(path) for path in changed):
        raise Stopped('main changed outside the independent price data')


class GitHub:
    def __init__(self, token, opener=None):
        if not token:
            raise Stopped('ephemeral repository token is missing')
        self._token = token
        self._opener = opener or urllib.request.build_opener(NoRedirect())
        self._blobs = {}

    def request(self, method, path, payload=None, *, public=False, public_run=False):
        # Do not accept arbitrary URLs or forward a repository token to Pages.
        if public_run:
            allowed_run = r'/repos/' + re.escape(REPOSITORY) + r'/actions/runs/[1-9][0-9]*'
            if public or method != 'GET' or payload is not None or not re.fullmatch(allowed_run, path):
                raise Stopped('unexpected public workflow-run read')
            url = API + path
        elif public:
            if path not in DATA_PATHS or method != 'GET' or payload is not None:
                raise Stopped('unexpected public read')
            url = PAGES + path
        else:
            if not (path == '/graphql' or path.startswith('/repos/' + REPOSITORY + '/')):
                raise Stopped('unexpected API destination')
            url = API + path
        headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'lido-manual-controller',
                   'X-GitHub-Api-Version': '2022-11-28', 'Cache-Control': 'no-cache'}
        if payload is not None:
            headers['Content-Type'] = 'application/json'
        if not public and not public_run:
            headers['Authorization'] = 'Bearer ' + self._token
        raw = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(url, data=raw, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=30) as response:
                body = response.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES:
                    raise ApiFailure('unbounded API response')
                if response.status < 200 or response.status >= 300:
                    raise ApiFailure('unexpected API status')
        except urllib.error.HTTPError as error:
            # Never log response bodies/headers, URL, request or exception args.
            raise ApiFailure('HTTP status ' + str(error.code), error.code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ApiFailure('HTTP transport failed; outcome may be unknown') from None
        return body if public else json.loads(body)

    def rest(self, path, method='GET', payload=None):
        return self.request(method, '/repos/' + REPOSITORY + path, payload)

    def workflow_run(self, run_id):
        # Public repository metadata requires no token or new Actions permission.
        return self.request('GET', '/repos/' + REPOSITORY + '/actions/runs/' + run_id,
                            public_run=True)

    def validate_push(self, before, after):
        before, after = checked_sha(before), checked_sha(after)
        if before == '0' * 40 or before == after:
            raise Stopped('push must change an existing main manual request file')
        identities = []
        for head in (before, after):
            response = self.rest('/git/trees/' + head + '?recursive=1')
            if response.get('truncated') is not False:
                raise Stopped('incomplete event tree')
            identities.append(next((row['sha'] for row in response['tree']
                                    if row['path'] == REQUEST_PATH and row['type'] == 'blob'), None))
        if not identities[1] or identities[0] == identities[1]:
            raise Stopped('this push did not change the explicit manual request')

    def revision(self, head=None):
        head = checked_sha(head or self.rest('/git/ref/heads/main')['object']['sha'])
        response = self.rest('/git/trees/' + head + '?recursive=1')
        if response.get('truncated') is not False:
            raise Stopped('incomplete repository tree')
        tree = {row['path']: (row['sha'], row['mode'], row['type']) for row in response['tree']
                if row['type'] != 'tree'}
        required = set(DATA_PATHS) | {REQUEST_PATH, WORKFLOW_PATH}
        files = {}
        for path in sorted(required | {p for p in tree if is_code(p)}):
            if path not in tree or tree[path][1:] != ('100644', 'blob'):
                raise Stopped('missing or nonregular controlled repository file')
            oid = checked_sha(tree[path][0])
            if oid not in self._blobs:
                value = self.rest('/git/blobs/' + oid)
                if value.get('encoding') != 'base64' or value.get('sha') != oid:
                    raise Stopped('invalid repository blob response')
                raw = base64.b64decode(value['content'])
                if len(raw) > MAX_BYTES or blob_sha(raw) != oid:
                    raise Stopped('repository blob hash mismatch')
                self._blobs[oid] = raw
            files[path] = self._blobs[oid]
        return Revision(head, tree, files)

    def commit(self, base, files, title):
        if not files or set(files) - set(DATA_PATHS):
            raise Stopped('publication path outside the two Lido JSON files')
        data = {'branch': {'repositoryNameWithOwner': REPOSITORY, 'branchName': 'main'},
                'expectedHeadOid': checked_sha(base.head), 'message': {'headline': title},
                'fileChanges': {'additions': [{'path': p, 'contents': base64.b64encode(raw).decode()}
                                               for p, raw in sorted(files.items())]}}
        response = self.request('POST', '/graphql', {
            'query': 'mutation($input:CreateCommitOnBranchInput!){createCommitOnBranch(input:$input){commit{oid}}}',
            'variables': {'input': data}})
        if response.get('errors'):
            errors = response['errors']
            if (not (response.get('data') or {}).get('createCommitOnBranch')
                    and all('Expected branch to point to' in e.get('message', '') for e in errors)):
                raise HeadMoved('exact-head compare-and-swap rejected')
            raise ApiFailure('GitHub rejected commit; inspect repository before another action')
        return checked_sha(response['data']['createCommitOnBranch']['commit']['oid'])

    def verify_commit(self, head, base, files):
        commit = self.rest('/git/commits/' + checked_sha(head))
        if [parent['sha'] for parent in commit['parents']] != [base.head]:
            raise Stopped('publication parent does not match exact expected head')
        actual = self.revision(head)
        expected_tree = dict(base.tree)
        for path, raw in files.items():
            expected_tree[path] = (blob_sha(raw), '100644', 'blob')
        if actual.tree != expected_tree or any(actual.files[p] != raw for p, raw in files.items()):
            raise Stopped('committed bytes or complete changed-path set do not match')
        return actual

    def pages_config(self):
        config = self.rest('/pages')
        if (config.get('build_type') != 'legacy'
                or config.get('source') != {'branch': 'main', 'path': '/'}
                or config.get('html_url') != PAGES or config.get('cname')):
            raise Stopped('existing Pages configuration is unsupported; no settings were changed')

    def deploy_and_verify(self, expected, *, clock=time.monotonic, sleep=time.sleep):
        self.pages_config()
        self.rest('/pages/builds', method='POST', payload={})
        deadline = clock() + 360
        while True:
            # Read both actual JSON files, even for a failure-status-only commit.
            if all(self.request('GET', path, public=True) == expected[path] for path in DATA_PATHS):
                return
            if clock() >= deadline:
                raise Stopped('committed Lido JSON does not match public Pages after six minutes')
            sleep(min(15, max(0, deadline - clock())))


def verify_local_code(root, current):
    local = {p.relative_to(root).as_posix(): blob_sha(p.read_bytes())
             for p in (root / 'scripts/lido').rglob('*') if p.is_file() and is_code(p.relative_to(root).as_posix())}
    remote = {p: row[0] for p, row in current.tree.items() if is_code(p)}
    if local != remote or blob_sha((root / WORKFLOW_PATH).read_bytes()) != current.tree[WORKFLOW_PATH][0]:
        raise Stopped('running collector/controller/workflow differs from current main')


def materialize(current, directory):
    directory.mkdir(parents=True, exist_ok=True)
    for path, raw in current.files.items():
        target = directory / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)


def group_alive(pid):
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False


def stop_child(process):
    # Repeated runner signals must not interrupt teardown then permit a lease
    # release while a callback/descendant is still live. Hard runner loss cannot
    # execute cleanup; the lease simply expires in that case.
    saved = {sig: signal.signal(sig, signal.SIG_IGN) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        if group_alive(process.pid):
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            if group_alive(process.pid):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=10)
            deadline = time.monotonic() + 5
            while group_alive(process.pid):
                if time.monotonic() >= deadline:
                    raise ChildStillRunning('source process group is not confirmed stopped; lease retained')
                time.sleep(0.05)
        # Reap the process even if its whole group vanished between checks.
        process.wait(timeout=1)
    except BaseException:
        raise ChildStillRunning('source process group is not confirmed stopped; lease retained') from None
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)


def run_child(command, *, timeout, environment):
    # No shell, no credential arguments, no persisted checkout credential helper.
    process = None
    try:
        process = subprocess.Popen(command, env=child_environment(environment), start_new_session=True)
        return process.wait(timeout=timeout)
    finally:
        if process is None:
            raise ChildStillRunning('source process startup is uncertain; lease retained') from None
        try:
            stop_child(process)
        except BaseException:
            # Include failures while installing/restoring signal handlers: no
            # teardown failure may escape as an ordinary recoverable exception.
            raise ChildStillRunning('source process group is not confirmed stopped; lease retained') from None


class Controller:
    def __init__(self, api, root, scratch, *, now=time.time, child=run_child, environment=None):
        self.api, self.root, self.scratch = api, root, scratch
        self.now, self.child = now, child
        self.environment = environment if environment is not None else os.environ
        self.leased = None
        self.run_id = None
        self.published = False
        self.child_stopped = True
        self.trigger = 'manual'
        self.schedule = None
        self.audit = {'schema': 1, 'repository': REPOSITORY, 'revisions': [], 'commits': [],
                      'repositoryPublished': False, 'pagesVerified': False,
                      'githubEventName': self.environment.get('GITHUB_EVENT_NAME'),
                      'githubRunId': self.environment.get('GITHUB_RUN_ID'),
                      'githubRunAttempt': self.environment.get('GITHUB_RUN_ATTEMPT'),
                      'githubHeadSha': self.environment.get('GITHUB_SHA')}

    def observe(self, stage, revision):
        self.audit['revisions'].append({'stage': stage, 'head': revision.head,
            'data': {p: {'gitBlob': revision.tree[p][0], 'sha256': hashlib.sha256(revision.files[p]).hexdigest()}
                     for p in DATA_PATHS},
            'codeBlobs': {p: row[0] for p, row in revision.tree.items() if is_code(p)},
            'requestBlob': revision.tree[REQUEST_PATH][0], 'workflowBlob': revision.tree[WORKFLOW_PATH][0]})

    def stable_current(self, base):
        current = self.api.revision()
        require_price_only(base, current)
        return current

    def cas(self, base, files, title, gate):
        for attempt in range(CAS_ATTEMPTS):
            gate(base)
            try:
                head = self.api.commit(base, files, title)
            except HeadMoved:
                if attempt + 1 == CAS_ATTEMPTS:
                    raise Stopped('bounded price-only head race retries exhausted')
                base = self.stable_current(base)
                continue
            except ApiFailure as error:
                if error.status in (401, 403):
                    raise
                # A timed-out write might have succeeded. Reconcile one read,
                # never blindly repeat an uncertain mutation.
                current = self.api.revision()
                if current.head == base.head or any(current.files[p] != raw for p, raw in files.items()):
                    raise
                head = current.head
            committed = self.api.verify_commit(head, base, files)
            self.audit['commits'].append({'expectedHeadOid': base.head, 'verifiedCommit': head,
                'singleParentVerified': True, 'exactChangedPathsVerified': True,
                'files': {p: {'gitBlob': blob_sha(content), 'sha256': hashlib.sha256(content).hexdigest()}
                          for p, content in files.items()}})
            return self.stable_current(committed)
        raise AssertionError('unreachable CAS loop')

    def acquire(self, current, request, *, schedule=None):
        self.schedule = schedule
        self.trigger = schedule['trigger'] if schedule is not None else 'manual'
        now = int(self.now())
        if schedule is not None:
            validate_schedule_day(schedule, now)
        request_key = schedule['requestKey'] if schedule is not None else request['requestId']
        budget = DEFAULT_RPC_CALLS if schedule is not None else request['rpcCallBudget']
        self.run_id = str(uuid.uuid4())
        self.audit.update(runId=self.run_id, trigger=self.trigger, manualRequest=request,
                          scheduledRequest=schedule)
        self.observe('before_acquire', current)
        proposed, reason = propose_lease(json.loads(current.files[SNAPSHOT_PATH]), now,
                                        self.run_id, trigger=self.trigger,
                                        manual_request_id=request_key if self.trigger == 'manual' else None,
                                        rpc_call_budget=budget)
        if proposed is None:
            return reason
        files = {SNAPSHOT_PATH: canonical(proposed).encode()}
        def gate(base):
            # Original expected snapshot/checkpoint/code and request cannot move.
            require_price_only(current, base)
            if schedule is not None:
                validate_schedule_day(schedule, int(self.now()))
            validate_lease(proposed, int(self.now()), self.run_id)
        self.leased = self.cas(current, files, 'Lido: acquire ' + self.trigger + ' lease', gate)
        self.observe('lease_readback', self.leased)
        lease = json.loads(self.leased.files[SNAPSHOT_PATH])['refresh']['lease']
        if (lease['runId'] != self.run_id or lease['requestKey'] != request_key
                or lease['trigger'] != self.trigger or lease['rpcCallBudget'] != budget):
            raise Stopped('durable lease readback does not match the daily/manual request')
        print(json.dumps({'step': 'lease_verified', 'head': self.leased.head,
                          'lease': lease, 'inputBlobs': {p: self.leased.tree[p][0] for p in DATA_PATHS},
                          'codeBlobs': {p: row[0] for p, row in self.leased.tree.items() if is_code(p)}}), flush=True)
        return None

    def publish(self, result):
        current = self.stable_current(self.leased)
        self.observe('before_publication', current)
        def gate(base):
            require_price_only(self.leased, base)
            validate_result(result, {p: base.files[p] for p in DATA_PATHS}, int(self.now()))
        files = {f['path']: f['content'].encode() for f in result['files']}
        committed = self.cas(current, files, 'Lido: ' + result['stage'] + ' ' + self.trigger + ' refresh', gate)
        self.published = True
        self.audit.update(repositoryPublished=True, collectorStage=result['stage'])
        self.observe('committed_readback', committed)
        snapshot = json.loads(committed.files[SNAPSHOT_PATH])
        print(json.dumps({'step': 'repository_published', 'stage': result['stage'],
                          'runId': self.run_id, 'commit': committed.head,
                          'sha256': {p: hashlib.sha256(committed.files[p]).hexdigest() for p in DATA_PATHS},
                          'asOf': snapshot.get('asOf'), 'blockNumber': snapshot.get('blockNumber'),
                          'tiers': snapshot.get('tiers'), 'dailyCutoffs': snapshot.get('dailyCutoffs'),
                          'pagesVerified': False}), flush=True)
        self.api.deploy_and_verify({p: committed.files[p] for p in DATA_PATHS})
        self.audit['pagesVerified'] = True
        return {'stage': result['stage'], 'commit': committed.head, 'pagesVerified': True}

    def invoke(self, stage, current, *, reason=None):
        runtime = self.scratch / 'runtime'
        materialize(current, runtime)
        output = self.scratch / ('result-' + stage + '.json')
        command = [sys.executable, str(runtime / 'scripts/lido/refresh.py'), stage,
                   '--root', str(runtime), '--run-id', self.run_id, '--head', current.head,
                   '--workdir', str(self.scratch / 'work'), '--output', str(output)]
        if stage == 'abort':
            command += ['--current-root', str(runtime), '--attest-current-main', '--abort-reason', reason]
        if stage == 'run' and self.schedule is not None:
            validate_schedule_day(self.schedule, int(self.now()))
        self.child_stopped = False
        try:
            self.child(command, timeout=1530 if stage == 'run' else 60, environment=self.environment)
        except ChildStillRunning:
            raise
        except BaseException:
            self.child_stopped = True  # run_child verified the group in its finally.
            raise
        else:
            self.child_stopped = True
        if not output.is_file() or output.stat().st_size > MAX_BYTES:
            raise Stopped('collector did not leave a bounded terminal manifest')
        result = json.loads(output.read_bytes())
        if result.get('runId') != self.run_id:
            raise Stopped('collector result belongs to a different run')
        return result

    def abort(self, reason):
        current = self.stable_current(self.leased)
        validate_lease(json.loads(current.files[SNAPSHOT_PATH]), int(self.now()), self.run_id)
        if (self.scratch / 'work/recovery').exists():
            return self.invoke('abort', current, reason=reason)
        # No journal and the subprocess is already stopped: no source result is
        # eligible, but the existing failure gate can safely release this lease.
        old = json.loads(current.files[SNAPSHOT_PATH])
        result = manifest('failed', self.run_id, current.files,
                          {SNAPSHOT_PATH: failure_snapshot(old, int(self.now()), reason)})
        result['localVerificationOnly'] = False
        return result

    def execute(self, request_raw=None):
        mode = invocation_mode(self.environment)
        schedule = (scheduled_request(self.api, self.environment, int(self.now()))
                    if mode == 'scheduled' else
                    scheduled_request(self.api, self.environment, int(self.now()), relay=True)
                    if mode == 'relay-dispatch' else None)
        request = None if schedule is not None else read_request(request_raw)
        if schedule is None and request is None:
            return {'stage': 'skipped', 'reason': 'manual request is inert'}
        current = self.api.revision()
        verify_local_code(self.root, current)
        if schedule is None and current.files[REQUEST_PATH] != request_raw:
            raise Stopped('manual request changed after this workflow was triggered')
        # Configuration failures should happen before consuming a request/lease.
        self.api.pages_config()
        reason = self.acquire(current, request, schedule=schedule)
        if reason:
            return {'stage': 'skipped', 'reason': reason}
        try:
            # Require a fresh main read after lease readback and before any source
            # command; ordinary price pushes are the only acceptable changes.
            result = self.invoke('run', self.stable_current(self.leased))
            if result.get('stage') == 'paused':
                result = self.abort('Actions has no same-route review; original run deliberately aborted')
            return self.publish(result)
        except BaseException as error:
            if (not self.published and not isinstance(error, ChildStillRunning)
                    and not (isinstance(error, ApiFailure) and error.status in (401, 403))):
                try:
                    self.publish(self.abort('Actions controller stopped; original run cannot continue'))
                except BaseException:
                    # Never overwrite a changed/expired lease or claim cleanup
                    # after runner loss. Its recorded expiry remains authoritative.
                    try:
                        print(json.dumps({'stage': 'blocked', 'leaseReleaseVerified': False}), flush=True)
                    except BaseException:
                        pass
            raise


def finish_diagnostics(controller, directory, token):
    """Best effort only: never replace the primary source/controller exception."""
    def report(value):
        try:
            print(json.dumps(value), flush=True)
        except BaseException:
            pass
    if not controller.child_stopped:
        report({'step': 'audit_skipped', 'reason': 'source process stop is unverified',
                'completeDiagnosticBundle': False})
        return False
    audit_ok = False
    try:
        from actions_audit import emit_bundle
        emit_bundle(directory, controller.audit, (token,), emit=lambda line: print(line, flush=True))
        audit_ok = True
    except BaseException:
        report({'step': 'audit_failed', 'completeDiagnosticBundle': False})
    try:
        shutil.rmtree(directory)
    except BaseException:
        report({'step': 'scratch_cleanup_failed'})
    return audit_ok


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    validate_invocation(os.environ)
    root = args.root.resolve()
    scheduled = invocation_mode(os.environ) != 'manual'
    request_raw = None if scheduled else (root / REQUEST_PATH).read_bytes()
    if not scheduled and read_request(request_raw) is None:
        print(json.dumps({'stage': 'skipped', 'reason': 'manual request is inert'}))
        return 0
    if sys.version_info < (3, 12) or not shutil.which('g++'):
        raise Stopped('Python 3.12 and g++ are required before lease acquisition')
    subprocess.run(['g++', '-std=c++17', '-x', 'c++', '-fsyntax-only', '-'],
                   input='int main() { return 0; }\n', text=True, check=True, timeout=20,
                   env=child_environment(os.environ), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # The token is never placed in argv, URLs, files, collector env, or logs.
    api = GitHub(os.environ.pop('GH_TOKEN', None))
    if os.environ['GITHUB_EVENT_NAME'] == 'push':
        event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_bytes())
        if (event.get('after') != os.environ['GITHUB_SHA'] or event.get('ref') != 'refs/heads/main'
                or event.get('deleted') or event.get('forced')
                or event.get('repository', {}).get('full_name') != REPOSITORY):
            raise Stopped('unexpected main push event')
        api.validate_push(event.get('before'), event['after'])
    def interrupted(signum, frame):
        raise Stopped('Actions runner interrupted the original daily/manual run')
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, interrupted)
    directory = Path(tempfile.mkdtemp(prefix='lido-actions-'))
    controller = Controller(api, root, directory)
    audit_ok = False
    try:
        answer = controller.execute(request_raw)
    finally:
        try:
            audit_ok = finish_diagnostics(controller, directory, api._token)
        except BaseException:
            audit_ok = False
    answer['auditVerified'] = audit_ok
    print(json.dumps(answer))
    if not audit_ok:
        return 1
    return 0 if answer['stage'] in ('complete', 'skipped') else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        # Deliberately omit arbitrary exception text. Small diagnostic types do
        # not reveal secrets, request bodies or endpoint response content.
        detail = str(error) if isinstance(error, Stopped) else 'unexpected local controller error'
        print(json.dumps({'stage': 'blocked', 'errorType': type(error).__name__, 'reason': detail}), file=sys.stderr)
        raise SystemExit(1)
