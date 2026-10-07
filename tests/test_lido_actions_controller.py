"""Offline controller/API fault tests. Never contact GitHub, RPC or Beacon sources."""
import base64
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/lido'))
import actions_controller as ac
from refresh import second, utc


def raw(value):
    return ac.canonical(value).encode()


def revision(files, number=1):
    return ac.Revision(format(number, '040x'),
                       {p: (ac.blob_sha(v), '100644', 'blob') for p, v in files.items()}, dict(files))


class FakeGitHub:
    def __init__(self, files):
        self.current = revision(files)
        self.revisions = {self.current.head: self.current}
        self.parents = {}
        self.commits = []
        self.pages = []
        self.number = 1
        self.race = None
        self.unknown_after_commit = False
        self.pages_error = None

    def revision(self, head=None):
        return self.revisions[head] if head else self.current

    def change(self, files):
        base = self.current
        self.number += 1
        self.current = revision({**base.files, **files}, self.number)
        self.revisions[self.current.head] = self.current
        self.parents[self.current.head] = base.head

    def commit(self, base, files, title):
        if self.race:
            race, self.race = self.race, None
            race(self)
        if base.head != self.current.head:
            raise ac.HeadMoved('exact-head compare-and-swap rejected')
        self.change(files)
        self.commits.append((base.head, dict(files)))
        if self.unknown_after_commit:
            self.unknown_after_commit = False
            raise ac.ApiFailure('HTTP transport failed; outcome may be unknown')
        return self.current.head

    def verify_commit(self, head, base, files):
        if self.parents[head] != base.head:
            raise ac.Stopped('incorrect parent')
        expected = revision({**base.files, **files})
        if self.revisions[head].tree != expected.tree:
            raise ac.Stopped('incorrect tree')
        return self.revisions[head]

    def pages_config(self):
        pass

    def deploy_and_verify(self, expected):
        self.pages.append(expected)
        if self.pages_error:
            raise self.pages_error


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.scratch = Path(self.temp.name)
        self.request = raw({'schema': 1, 'requestId': 'explicit-offline-1', 'rpcCallBudget': 256})
        self.old = json.loads((ROOT / 'tests/fixtures/lido-cutoff-20261004.json').read_bytes())
        self.now = 1791135600
        files = {p.relative_to(ROOT).as_posix(): p.read_bytes()
                 for p in (ROOT / 'scripts/lido').rglob('*')
                 if p.is_file() and ac.is_code(p.relative_to(ROOT).as_posix())}
        files.update({ac.SNAPSHOT_PATH: raw(self.old), ac.STATE_PATH: b'{"checkpoint":"retained"}\n',
                      ac.REQUEST_PATH: self.request, ac.WORKFLOW_PATH: (ROOT / ac.WORKFLOW_PATH).read_bytes()})
        self.api = FakeGitHub(files)
        self.child = Mock(side_effect=self.collect_failed)
        self.controller = ac.Controller(self.api, ROOT, self.scratch, now=lambda: self.now,
                                        child=self.child, environment={'PATH': '/usr/bin', 'GH_TOKEN': 'secret'})
        self.stdout = patch('sys.stdout', new=io.StringIO())
        self.stdout.start()
        self.addCleanup(self.stdout.stop)

    def write_result(self, command, result):
        Path(command[command.index('--output') + 1]).write_bytes(raw(result))

    def result(self, success=False):
        current = self.api.current.files
        old = json.loads(current[ac.SNAPSHOT_PATH])
        run_id = old['refresh']['lease']['runId']
        if success:
            value = deepcopy(old)
            at = self.now - 60
            value['asOf'] = utc(at)
            for row in value['tiers']:
                for scenario in ('main', 'stress'):
                    row[scenario]['elapsed_to_reference_seconds'] = second(row[scenario]['reference_time_utc']) - at
            value['refresh'].update(state='ok', lease=None, runId=run_id)
            files = {ac.SNAPSHOT_PATH: value,
                     ac.STATE_PATH: {'block': value['blockNumber'], 'hash': value['blockHash'], 'timestamp': at}}
        else:
            files = {ac.SNAPSHOT_PATH: ac.failure_snapshot(old, self.now, 'offline synthetic failure')}
        result = ac.manifest('complete' if success else 'failed', run_id, current, files)
        result['localVerificationOnly'] = False
        return result

    def collect_failed(self, command, **kwargs):
        self.write_result(command, self.result())
        return 1

    def test_inert_request_has_no_api_source_or_publication(self):
        self.controller.api = Mock()
        answer = self.controller.execute(raw({'schema': 1, 'requestId': None, 'rpcCallBudget': 256}))
        self.assertEqual(answer['stage'], 'skipped')
        self.controller.api.assert_not_called()
        self.assertEqual(self.controller.api.method_calls, [])
        self.child.assert_not_called()

    def test_failure_preserves_old_snapshot_and_checkpoint_and_consumes_id(self):
        state = self.api.current.files[ac.STATE_PATH]
        answer = self.controller.execute(self.request)
        self.assertEqual(answer['stage'], 'failed')
        current = json.loads(self.api.current.files[ac.SNAPSHOT_PATH])
        self.assertEqual({k: v for k, v in current.items() if k != 'refresh'}, self.old)
        self.assertEqual(self.api.current.files[ac.STATE_PATH], state)
        self.assertIsNone(current['refresh']['lease'])
        self.assertEqual(current['refresh']['manualAttemptIds'], ['explicit-offline-1'])
        self.assertEqual([set(files) for _, files in self.api.commits], [{ac.SNAPSHOT_PATH}, {ac.SNAPSHOT_PATH}])
        self.assertEqual(self.controller.execute(self.request)['stage'], 'skipped')
        self.assertEqual(self.child.call_count, 1)
        self.assertEqual(len(self.api.pages), 1)

    def test_success_is_exactly_two_atomic_files_and_actual_pages_verification(self):
        def complete(command, **kwargs):
            self.write_result(command, self.result(success=True))
            return 0
        self.child.side_effect = complete
        answer = self.controller.execute(self.request)
        self.assertEqual(answer['stage'], 'complete')
        self.assertTrue(answer['pagesVerified'])
        self.assertEqual(set(self.api.commits[-1][1]), set(ac.DATA_PATHS))
        self.assertEqual(self.api.pages, [{p: self.api.current.files[p] for p in ac.DATA_PATHS}])

    def test_shared_active_scheduled_lease_blocks_manual_without_source(self):
        owned, _ = ac.propose_lease(self.old, self.now, 'scheduled-owner')
        self.api.change({ac.SNAPSHOT_PATH: raw(owned)})
        self.assertEqual(self.controller.execute(self.request)['stage'], 'skipped')
        self.child.assert_not_called()
        self.assertEqual(self.api.commits, [])

    def test_manual_320_is_lease_bound_and_next_default_is_256(self):
        for budget in (256, 320):
            with self.subTest(budget=budget):
                request = {'schema': 1, 'requestId': 'budget-' + str(budget), 'rpcCallBudget': budget}
                base = revision({**self.api.current.files, ac.REQUEST_PATH: raw(request)})
                self.api = FakeGitHub(base.files)
                self.controller.api = self.api
                self.controller.acquire(self.api.current, request)
                lease = json.loads(self.api.current.files[ac.SNAPSHOT_PATH])['refresh']['lease']
                self.assertEqual(lease['rpcCallBudget'], budget)
                self.api.change({ac.SNAPSHOT_PATH: raw(ac.failure_snapshot(json.loads(self.api.current.files[ac.SNAPSHOT_PATH]), self.now, 'done'))})
                ordinary, _ = ac.propose_lease(json.loads(self.api.current.files[ac.SNAPSHOT_PATH]), self.now, 'next',
                                              trigger='manual', manual_request_id='ordinary-' + str(budget))
                self.assertEqual(ordinary['refresh']['lease']['rpcCallBudget'], 256)

    def test_price_only_head_race_retries_cas_without_repeat_collection(self):
        self.api.race = lambda api: api.change({'data/history.json': b'new independent price bytes'})
        self.controller.execute(self.request)
        self.assertEqual(self.child.call_count, 1)
        self.assertEqual(self.api.current.files['data/history.json'], b'new independent price bytes')

    def test_lease_checkpoint_code_workflow_request_and_unrelated_races_stop(self):
        for path in (ac.SNAPSHOT_PATH, ac.STATE_PATH, 'scripts/lido/refresh.py', ac.WORKFLOW_PATH,
                     ac.REQUEST_PATH, 'index.html'):
            with self.subTest(path=path):
                api = FakeGitHub(self.api.current.files)
                api.race = lambda target, p=path: target.change({p: b'changed competing bytes'})
                controller = ac.Controller(api, ROOT, self.scratch, now=lambda: self.now, child=self.child)
                with self.assertRaises(ac.Stopped):
                    controller.execute(self.request)
                self.assertEqual(api.commits, [])
        self.child.assert_not_called()

    def test_changed_code_before_acquisition_has_no_write_or_source(self):
        self.api.change({'scripts/lido/refresh.py': b'changed code'})
        with self.assertRaisesRegex(ac.Stopped, 'differs'):
            self.controller.execute(self.request)
        self.assertEqual(self.api.commits, [])
        self.child.assert_not_called()

    def test_expired_lease_cannot_publish_or_release(self):
        self.controller.acquire(self.api.current, ac.read_request(self.request))
        result = self.result()
        self.now += 2100
        with self.assertRaisesRegex(RuntimeError, 'expired'):
            self.controller.publish(result)
        with self.assertRaisesRegex(RuntimeError, 'expired'):
            self.controller.abort('expired')
        self.assertEqual(len(self.api.commits), 1)

    def test_publication_price_race_preserves_result_without_collection_retry(self):
        self.controller.acquire(self.api.current, ac.read_request(self.request))
        result = self.result(success=True)
        self.api.race = lambda api: api.change({'data/archive/2026-10-04.jsonl': b'price sample'})
        self.assertEqual(self.controller.publish(result)['stage'], 'complete')
        self.child.assert_not_called()
        self.assertEqual(len(self.api.commits), 2)

    def test_publication_rejects_changed_checkpoint_even_for_status_only_failure(self):
        self.controller.acquire(self.api.current, ac.read_request(self.request))
        result = self.result()
        self.api.change({ac.STATE_PATH: b'changed checkpoint'})
        with self.assertRaisesRegex(ac.Stopped, 'outside'):
            self.controller.publish(result)
        self.assertEqual(len(self.api.commits), 1)

    def test_publication_403_stops_without_second_write(self):
        self.controller.acquire(self.api.current, ac.read_request(self.request))
        self.api.commit = Mock(side_effect=ac.ApiFailure('HTTP status 403', 403))
        with self.assertRaises(ac.ApiFailure):
            self.controller.publish(self.result())
        self.assertEqual(self.api.commit.call_count, 1)

    def test_price_cas_races_are_bounded(self):
        original = self.api.commit
        attempts = [0]
        def raced(base, files, title):
            attempts[0] += 1
            self.api.change({'data/history.json': str(attempts[0]).encode()})
            return original(base, files, title)
        self.api.commit = raced
        with self.assertRaisesRegex(ac.Stopped, 'bounded'):
            self.controller.execute(self.request)
        self.assertEqual(attempts[0], 3)
        self.child.assert_not_called()

    def test_unknown_committed_write_is_read_back_without_duplicate_mutation(self):
        self.api.unknown_after_commit = True
        self.controller.execute(self.request)
        self.assertEqual(len(self.api.commits), 2)
        self.assertEqual(self.child.call_count, 1)

    def test_paused_manifest_aborts_same_job_never_resumes(self):
        calls = []
        def paused(command, **kwargs):
            calls.append(command[2])
            if command[2] == 'run':
                (self.scratch / 'work/recovery').mkdir(parents=True)
                self.write_result(command, {'stage': 'paused', 'runId': self.controller.run_id, 'files': []})
                return 2
            self.assertIn('--attest-current-main', command)
            self.assertNotIn('--reviewed-same-route', command)
            return self.collect_failed(command, **kwargs)
        self.child.side_effect = paused
        self.assertEqual(self.controller.execute(self.request)['stage'], 'failed')
        self.assertEqual(calls, ['run', 'abort'])

    def test_pages_failure_does_not_rewrite_success_or_restart_source(self):
        self.child.side_effect = lambda command, **kwargs: self.write_result(command, self.result(success=True))
        self.api.pages_error = ac.Stopped('Pages mismatch')
        with self.assertRaisesRegex(ac.Stopped, 'Pages mismatch'):
            self.controller.execute(self.request)
        self.assertTrue(self.controller.published)
        self.assertEqual(len(self.api.commits), 2)
        self.assertEqual(self.child.call_count, 1)
        self.assertEqual(json.loads(self.api.current.files[ac.SNAPSHOT_PATH])['refresh']['state'], 'ok')

    def test_missing_manifest_releases_only_old_status_after_child_stops(self):
        self.child.side_effect = lambda command, **kwargs: 1
        with self.assertRaises(ac.Stopped):
            self.controller.execute(self.request)
        self.assertEqual(len(self.api.commits), 2)
        self.assertEqual(json.loads(self.api.current.files[ac.SNAPSHOT_PATH])['refresh']['state'], 'error')

    def test_uncertain_live_child_retains_lease_and_never_aborts_or_publishes(self):
        self.child.side_effect = ac.ChildStillRunning('not confirmed stopped')
        with self.assertRaises(ac.ChildStillRunning):
            self.controller.execute(self.request)
        self.assertEqual(len(self.api.commits), 1)
        self.assertEqual(self.child.call_count, 1)
        self.assertEqual(self.api.pages, [])
        self.assertIsNotNone(json.loads(self.api.current.files[ac.SNAPSHOT_PATH])['refresh']['lease'])
        self.assertFalse(self.controller.child_stopped)


class ApiTests(unittest.TestCase):
    def test_exact_head_mutation_two_files_without_ref_force_or_extra_scope(self):
        api = ac.GitHub('do-not-log')
        api.request = Mock(return_value={'data': {'createCommitOnBranch': {'commit': {'oid': '2' * 40}}}})
        api.commit(revision({}), {p: b'{}' for p in ac.DATA_PATHS}, 'test')
        method, endpoint, body = api.request.call_args.args
        self.assertEqual((method, endpoint), ('POST', '/graphql'))
        value = body['variables']['input']
        self.assertEqual(value['expectedHeadOid'], format(1, '040x'))
        self.assertEqual(value['branch'], {'repositoryNameWithOwner': ac.REPOSITORY, 'branchName': 'main'})
        self.assertEqual(set(value['fileChanges']), {'additions'})
        self.assertEqual({f['path'] for f in value['fileChanges']['additions']}, set(ac.DATA_PATHS))
        with self.assertRaises(ac.Stopped):
            api.commit(revision({}), {'index.html': b'bad'}, 'test')

    def test_head_rejection_and_unknown_graphql_errors_are_distinct(self):
        api = ac.GitHub('do-not-log')
        api.request = Mock(return_value={'data': None, 'errors': [{'message': 'Expected branch to point to old SHA'}]})
        with self.assertRaises(ac.HeadMoved):
            api.commit(revision({}), {ac.SNAPSHOT_PATH: b'{}'}, 'test')
        api.request.return_value = {'errors': [{'message': 'secret echoed by remote'}]}
        with self.assertRaises(ac.ApiFailure) as caught:
            api.commit(revision({}), {ac.SNAPSHOT_PATH: b'{}'}, 'test')
        self.assertNotIn('secret', str(caught.exception))

    def test_http_denial_and_redirect_do_not_leak_token_or_retry(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError('https://secret.invalid/token', 403,
                                                        'secret-token', {'Authorization': 'secret'}, io.BytesIO(b'secret'))
        api = ac.GitHub('secret-token', opener)
        with self.assertRaises(ac.ApiFailure) as caught:
            api.rest('/pages')
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(str(caught.exception), 'HTTP status 403')
        self.assertEqual(opener.open.call_count, 1)
        with self.assertRaises(ac.ApiFailure):
            ac.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.invalid')

    def test_public_pages_receives_no_authorization_header(self):
        response = Mock(status=200)
        response.read.return_value = b'{}'
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        api = ac.GitHub('secret-token', opener)
        api.request('GET', ac.SNAPSHOT_PATH, public=True)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, ac.PAGES + ac.SNAPSHOT_PATH)
        self.assertFalse(request.has_header('Authorization'))
        with self.assertRaises(ac.Stopped):
            api.request('GET', 'https://evil.invalid', public=True)
        with self.assertRaises(ac.Stopped):
            api.request('POST', '/repos/another/repository/pages')

    def test_pages_mismatch_is_bounded_and_new_build_requested_once(self):
        api = ac.GitHub('secret')
        api.pages_config = Mock()
        api.rest = Mock(return_value={})
        api.request = Mock(return_value=b'old')
        now = [0]
        def sleep(seconds):
            now[0] += seconds
        with self.assertRaisesRegex(ac.Stopped, 'does not match'):
            api.deploy_and_verify({p: b'new' for p in ac.DATA_PATHS}, clock=lambda: now[0], sleep=sleep)
        self.assertEqual(now[0], 360)
        api.rest.assert_called_once_with('/pages/builds', method='POST', payload={})

    def test_pages_configuration_is_read_only_and_explicit(self):
        api = ac.GitHub('secret')
        valid = {'build_type': 'legacy', 'source': {'branch': 'main', 'path': '/'}, 'html_url': ac.PAGES, 'cname': None}
        api.rest = Mock(return_value=valid)
        api.pages_config()
        for changes in ({'build_type': 'workflow'}, {'source': {'branch': 'other', 'path': '/'}},
                        {'html_url': 'https://another.invalid/'}, {'cname': 'custom.invalid'}):
            api.rest.return_value = {**valid, **changes}
            with self.assertRaisesRegex(ac.Stopped, 'unsupported'):
                api.pages_config()
        self.assertTrue(all(call.args == ('/pages',) for call in api.rest.call_args_list))

    def test_actual_committed_parent_and_full_tree_are_verified(self):
        api = ac.GitHub('secret')
        base = revision({ac.SNAPSHOT_PATH: b'old', ac.STATE_PATH: b'state'})
        desired = {ac.SNAPSHOT_PATH: b'new'}
        api.rest = Mock(return_value={'parents': [{'sha': base.head}]})
        api.revision = Mock(return_value=revision({**base.files, **desired}, 2))
        api.verify_commit(format(2, '040x'), base, desired)
        api.revision.return_value = revision({**base.files, **desired, 'extra': b'unauthorized'}, 2)
        with self.assertRaisesRegex(ac.Stopped, 'changed-path'):
            api.verify_commit(format(2, '040x'), base, desired)
        api.rest.return_value = {'parents': [{'sha': '9' * 40}]}
        with self.assertRaisesRegex(ac.Stopped, 'parent'):
            api.verify_commit(format(2, '040x'), base, desired)

    def test_bad_blob_readback_and_truncated_tree_fail_closed(self):
        api = ac.GitHub('secret')
        files = {path: b'{}' for path in (*ac.DATA_PATHS, ac.REQUEST_PATH, ac.WORKFLOW_PATH)}
        tree = [{'path': p, 'sha': ac.blob_sha(v), 'mode': '100644', 'type': 'blob'} for p, v in files.items()]
        api.rest = Mock(side_effect=[{'truncated': False, 'tree': tree},
                                    {'encoding': 'base64', 'sha': ac.blob_sha(b'{}'),
                                     'content': base64.b64encode(b'changed').decode()}])
        with self.assertRaisesRegex(ac.Stopped, 'hash mismatch'):
            api.revision('1' * 40)
        api.rest = Mock(return_value={'truncated': True, 'tree': tree})
        with self.assertRaisesRegex(ac.Stopped, 'incomplete'):
            api.revision('1' * 40)

    def test_push_path_gate_blocks_ordinary_price_push(self):
        api = ac.GitHub('secret')
        api.rest = Mock(return_value={'truncated': False, 'tree': [
            {'path': ac.REQUEST_PATH, 'type': 'blob', 'sha': '1' * 40}]})
        with self.assertRaisesRegex(ac.Stopped, 'did not change'):
            api.validate_push('1' * 40, '2' * 40)
        api.rest.side_effect = [{'truncated': False, 'tree': []}, {'truncated': False, 'tree': [
            {'path': ac.REQUEST_PATH, 'type': 'blob', 'sha': '3' * 40}]}]
        api.validate_push('1' * 40, '2' * 40)


class BoundaryTests(unittest.TestCase):
    def test_child_env_has_no_repository_runtime_oidc_or_git_credentials(self):
        environment = {'PATH': '/bin', 'HOME': '/home/runner', 'GH_TOKEN': 'secret', 'GITHUB_TOKEN': 'secret',
                       'ACTIONS_RUNTIME_TOKEN': 'secret', 'ACTIONS_ID_TOKEN_REQUEST_URL': 'secret',
                       'ACTIONS_ID_TOKEN_REQUEST_TOKEN': 'secret', 'GIT_CONFIG_COUNT': '1', 'PYTHONPATH': 'bad',
                       'HTTPS_PROXY': 'secret', 'OTHER_CREDENTIAL': 'secret'}
        self.assertEqual(ac.child_environment(environment), {'PATH': '/bin', 'HOME': '/home/runner'})

    def test_wrong_repo_ref_schedule_or_server_never_allowed(self):
        good = {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': ac.REPOSITORY, 'GITHUB_REF': 'refs/heads/main',
                'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_SHA': '1' * 40,
                'GITHUB_SERVER_URL': 'https://github.com', 'GITHUB_API_URL': ac.API}
        ac.validate_invocation(good)
        for key, value in [('GITHUB_REPOSITORY', 'other/repo'), ('GITHUB_REF', 'refs/heads/other'),
                           ('GITHUB_EVENT_NAME', 'schedule'), ('GITHUB_EVENT_NAME', 'pull_request'),
                           ('GITHUB_SERVER_URL', 'https://evil.invalid')]:
            with self.subTest(key=key, value=value), self.assertRaises(ac.Stopped):
                ac.validate_invocation({**good, key: value})

    def test_inert_and_budget_input_are_strict(self):
        for value in ({'schema': 1, 'requestId': None, 'rpcCallBudget': 320},
                      {'schema': 1, 'requestId': 'id', 'rpcCallBudget': 321},
                      {'schema': True, 'requestId': 'id', 'rpcCallBudget': 256},
                      {'schema': 1, 'requestId': 'id', 'rpcCallBudget': 256, 'resume': True}):
            with self.assertRaises(ac.Stopped):
                ac.read_request(raw(value))
        with self.assertRaisesRegex(ac.Stopped, 'duplicate'):
            ac.read_request('{"schema":1,"requestId":"id","rpcCallBudget":256,"rpcCallBudget":320}')

    def test_process_group_is_stopped_before_failure_can_be_published(self):
        process = Mock(pid=12345)
        process.wait.side_effect = [ac.subprocess.TimeoutExpired('child', 1), 0, 0]
        with (patch.object(ac.subprocess, 'Popen', return_value=process) as popen,
              patch.object(ac.os, 'killpg') as kill, patch.object(ac, 'group_alive', side_effect=[True, False, False])):
            with self.assertRaises(ac.subprocess.TimeoutExpired):
                ac.run_child(['python', 'collector'], timeout=1, environment={'PATH': '/bin', 'GH_TOKEN': 'secret'})
        self.assertEqual(popen.call_args.kwargs, {'env': {'PATH': '/bin'}, 'start_new_session': True})
        kill.assert_called_once_with(12345, ac.signal.SIGTERM)
        self.assertEqual(process.wait.call_count, 3)

    def test_repeated_interruption_never_claims_quiescence(self):
        process = Mock(pid=12345)
        process.wait.side_effect = [ac.subprocess.TimeoutExpired('child', 1), ac.Stopped('second interruption')]
        with (patch.object(ac.subprocess, 'Popen', return_value=process),
              patch.object(ac.os, 'killpg'), patch.object(ac, 'group_alive', return_value=True),
              patch.object(ac.signal, 'signal', return_value='previous') as handlers):
            with self.assertRaises(ac.ChildStillRunning):
                ac.run_child(['python', 'collector'], timeout=1, environment={})
        self.assertEqual(handlers.call_args_list[0].args, (ac.signal.SIGINT, ac.signal.SIG_IGN))
        self.assertEqual(handlers.call_args_list[1].args, (ac.signal.SIGTERM, ac.signal.SIG_IGN))
        self.assertEqual(handlers.call_args_list[-1].args, (ac.signal.SIGTERM, 'previous'))

    def test_failed_kill_does_not_permit_publication(self):
        process = Mock(pid=12345)
        process.wait.side_effect = ac.subprocess.TimeoutExpired('child', 1)
        with (patch.object(ac.os, 'killpg', side_effect=[None, PermissionError('cannot kill')]),
              patch.object(ac, 'group_alive', return_value=True)):
            with self.assertRaises(ac.ChildStillRunning):
                ac.stop_child(process)

    def test_signal_masking_or_restore_failure_is_always_nonpublishable(self):
        for handlers in ([None, ac.Stopped('signal during masking')],
                         [None, None, ac.Stopped('signal during restore')]):
            with self.subTest(handlers=len(handlers)):
                process = Mock(pid=12345)
                process.wait.return_value = 0
                with (patch.object(ac.subprocess, 'Popen', return_value=process),
                      patch.object(ac, 'group_alive', return_value=False),
                      patch.object(ac.signal, 'signal', side_effect=handlers)):
                    with self.assertRaises(ac.ChildStillRunning):
                        ac.run_child(['python', 'collector'], timeout=1, environment={})

    def test_interrupted_startup_is_never_assumed_stopped(self):
        with patch.object(ac.subprocess, 'Popen', side_effect=ac.Stopped('interrupted during creation')):
            with self.assertRaises(ac.ChildStillRunning):
                ac.run_child(['python', 'collector'], timeout=1, environment={})

    def test_diagnostics_and_cleanup_never_replace_primary_denial_or_live_child(self):
        for error, stopped in ((ac.ApiFailure('HTTP status 403', 403), True),
                               (ac.ChildStillRunning('unknown live child'), False)):
            with self.subTest(error=type(error).__name__):
                controller = Mock(child_stopped=stopped, audit={})
                controller.execute.side_effect = error
                api = Mock(_token='secret')
                with (patch.object(sys, 'argv', ['controller', '--root', str(ROOT)]),
                      patch.object(ac, 'validate_invocation'),
                      patch.object(ac, 'read_request', return_value={'requestId': 'unit'}),
                      patch.object(ac.subprocess, 'run'), patch.object(ac, 'GitHub', return_value=api),
                      patch.object(ac, 'Controller', return_value=controller),
                      patch.object(ac.tempfile, 'mkdtemp', return_value='/tmp/unused-test-directory'),
                      patch.object(ac.shutil, 'rmtree', side_effect=OSError('cleanup failed')),
                      patch('actions_audit.emit_bundle', side_effect=OSError('audit failed')),
                      patch('builtins.print', side_effect=BrokenPipeError('log closed')),
                      patch.dict(ac.os.environ, {'GITHUB_EVENT_NAME': 'workflow_dispatch'})):
                    with self.assertRaises(type(error)) as caught:
                        ac.main()
                    self.assertIs(caught.exception, error)

    def test_exited_parent_does_not_hide_live_descendant(self):
        process = Mock(pid=12345)
        process.wait.return_value = 0
        with (patch.object(ac.os, 'killpg') as kill,
              patch.object(ac, 'group_alive', side_effect=[True, True, False])):
            ac.stop_child(process)
        self.assertEqual([call.args for call in kill.call_args_list],
                         [(12345, ac.signal.SIGTERM), (12345, ac.signal.SIGKILL)])


if __name__ == '__main__':
    unittest.main()
