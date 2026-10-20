"""Exercise real Git hooks, the pinned scanner, and the release gate."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

FAKE_TOKEN = 'ghp_' + 'aB3dE5fG7hI9jK1mN3pQ5rS7tU9vW1xY3zA5'
# Split home roots so this file does not contain the personal paths it tests.
HOME, USERS = '/home/', '/Users/'
HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('repo_guard', HERE / 'repo_guard.py')
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        # Tests use the fictional example policy, not the user's policy.
        config = self.root / 'config'
        shutil.copytree(HERE / 'examples', config)
        self.env = dict(os.environ, REPO_GUARD_STATE=str(self.root / 'state'), REPO_GUARD_CONFIG=str(config),
                        GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_SYSTEM='/dev/null')
        self.environment = patch.dict(os.environ, self.env, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.git('init', '-q')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'commit.gpgsign', 'false')
        self.git('config', 'repoGuard.profile', 'candidate')
        self.git('commit', '--allow-empty', '-qm', 'Base')

    def git(self, *args, expected=0, input=None):
        result = subprocess.run(['git', *args], cwd=self.repo, env=os.environ,
                                input=input, capture_output=True)
        self.assertEqual(result.returncode, expected, result.stderr.decode(errors='replace'))
        return result.stdout.decode().strip()

    def stage(self, name, content):
        item = self.repo / name
        item.parent.mkdir(parents=True, exist_ok=True)
        item.write_text(content)
        self.git('add', '--', name)

    def test_private_name_is_detected_and_redacted(self):
        self.stage('README.md', 'Internal Northwind tool.\n')
        findings = guard.check(self.repo, 'staged')
        self.assertIn('private-name', {item['rule'] for item in findings})
        self.assertNotIn('Northwind', json.dumps(findings))

    def test_private_word_does_not_match_inside_a_longer_word(self):
        # A common private word must not flag longer words in a dictionary.
        for value in ('Falcon', 'FalconBot', 'falcon_api', 'See falcon.'):
            with self.subTest(value=value):
                self.assertIn('private-name', {item['rule'] for item in guard.privacy(value, 'sample')})
        for value in ('falconry', 'Gyrfalcon', 'falcons'):
            with self.subTest(value=value):
                self.assertEqual(guard.privacy(value, 'sample'), [])
        # Terms still match inside longer names.
        self.assertIn('private-name', {item['rule'] for item in guard.privacy('northwindcloud', 'sample')})

    def test_unicode_text_keeps_case_insensitive_private_name_checks(self):
        self.stage('sample.txt', 'Résumé: Northwİnd\n')
        self.assertIn('private-name', {item['rule'] for item in guard.check(self.repo, 'staged')})

    def test_empty_environment_file_is_rejected(self):
        self.stage('.env.prod-readonly', '')
        self.assertIn('sensitive-filename', {item['rule'] for item in guard.check(self.repo, 'staged')})

    def test_example_environment_file_is_allowed_but_credentials_are_not(self):
        self.stage('.env.example', 'API_TOKEN=replace-me\n')
        self.assertEqual(guard.check(self.repo, 'staged'), [])
        self.stage('.env.example', 'API_TOKEN=' + FAKE_TOKEN + '\n')
        findings = guard.check(self.repo, 'staged')
        self.assertIn('github-pat', {item['rule'] for item in findings})
        self.assertNotIn('ghp_', json.dumps(findings))

    def test_scan_uses_index_and_ignores_unstaged_replacement(self):
        self.stage('sample.txt', 'Northwind\n')
        (self.repo / 'sample.txt').write_text('Public sample\n')
        self.assertIn('private-name', {item['rule'] for item in guard.check(self.repo, 'staged')})

    def test_staged_paths_are_literal_with_spaces_and_glob_characters(self):
        self.stage('a [sample] file.txt', 'Northwind\n')
        findings = guard.check(self.repo, 'staged')
        self.assertTrue(any(item['path'] == 'a [sample] file.txt' and item['rule'] == 'private-name' for item in findings))

    def test_home_path_and_placeholder(self):
        self.stage('sample.txt', HOME + 'example/reports/\n')
        self.assertEqual(guard.check(self.repo, 'staged'), [])
        self.stage('sample.txt', HOME + 'realperson/private-project/\n')
        self.assertIn('personal-path', {item['rule'] for item in guard.check(self.repo, 'staged')})
        self.stage('sample.txt', 'Reports under ' + HOME + 'realperson are local.\n')
        self.assertIn('personal-path', {item['rule'] for item in guard.check(self.repo, 'staged')})

    def test_web_user_urls_are_not_local_home_paths(self):
        for value in ('https://api.github.com/users/example-person/repos',
                      'https://example.org/Users/ExamplePerson/profile',
                      'https://example.org/home/example-person/page'):
            with self.subTest(value=value):
                self.assertEqual(guard.privacy(value, 'GitHub metadata'), [])

    def test_real_home_paths_still_match_in_config_and_file_urls(self):
        for value in ('ROOT="' + HOME + 'real-person"', "ROOT='" + USERS + "RealPerson/Documents'",
                      'PATH=/bin:' + HOME + 'real-person/bin', 'file://' + USERS + 'RealPerson/Documents',
                      'C:\\Users\\' + 'RealPerson\\Documents', '/mnt/c' + USERS + 'RealPerson/Documents'):
            with self.subTest(value=value):
                self.assertIn('personal-path', {item['rule'] for item in guard.privacy(value, 'config')})

    def test_github_runner_home_is_not_a_personal_path(self):
        # Every Actions log contains the hosted runner's home folder.
        for value in (HOME + 'runner/work/project/project', USERS + 'runner/work',
                      'C:\\Users\\' + 'runneradmin\\work'):
            with self.subTest(value=value):
                self.assertEqual(guard.privacy(value, 'Actions run 1'), [])
        self.assertIn('personal-path', {item['rule'] for item in guard.privacy(HOME + 'runnerbob/x', 'sample')})

    def test_quoted_placeholder_home_roots_are_allowed(self):
        for value in ("'" + HOME + "example'", '"' + HOME + 'user"', HOME + 'example/reports/'):
            self.assertEqual(guard.privacy(value, 'documentation'), [])

    def test_concrete_worktree_file_paths_need_no_private_name_policy(self):
        prefix = '.worktrees' + '/'
        config = {'personal_path_patterns': []}
        for value in (prefix + 'release-queue/pr-42/ui/index.html',
                      '../project/' + prefix + 'replay-task/control/panel.html',
                      'file:///tmp/project/' + prefix + 'checkout/report.html',
                      r'C:\project\.worktrees' + r'\release-task\ui\index.html',
                      r'.worktrees' + r'\\release-task\\ui\\index.html'):
            with self.subTest(value=value):
                findings = guard.privacy('First line\n' + value, 'sample', config)
                self.assertEqual(findings, [guard.finding('worktree-path', 'sample', 2)])
                self.assertNotIn(value, json.dumps(findings))

    def test_worktree_placeholders_and_directory_arguments_are_allowed(self):
        prefix = '.worktrees' + '/'
        for value in (prefix + 'example-task/ui/index.html',
                      prefix + 'sample-project/report.html',
                      prefix + 'demo/ui/index.html',
                      prefix + 'feature/report.html',
                      prefix + 'feature-a/ui/index.html',
                      prefix + '<checkout>/ui/index.html',
                      prefix + '${CHECKOUT}/ui/index.html',
                      prefix + '{checkout}/ui/index.html',
                      prefix + '*/ui/index.html',
                      prefix + 'public-source', '.worktrees/',
                      '.worktrees-backup/release-task/ui/index.html',
                      'other.worktrees/release-task/ui/index.html'):
            with self.subTest(value=value):
                self.assertEqual(guard.privacy(value, 'documentation'), [])
        for name in ('examplecorp', 'samplecustomer', 'democlient', 'feature-payment'):
            with self.subTest(name=name):
                self.assertIn('worktree-path', {item['rule'] for item in
                              guard.privacy(prefix + name + '/ui/index.html', 'sample')})

    def test_worktree_path_hooks_block_renamed_labels_and_outgoing_deleted_paths(self):
        if tuple(map(int, self.git('--version').split()[2].split('.')[:2])) < (2, 54):
            self.skipTest('Hook installation requires Git 2.54+.')
        concrete = '.worktrees' + '/release-task/ui/index.html'
        self.stage('README.md', 'Application preview: ' + concrete + '\n')
        guard.enable(self.repo, 'candidate')
        self.git('commit', '-qm', 'Add preview', expected=1)
        self.assertIn('worktree-path', {item['rule'] for item in guard.hook(
            self.repo, 'pre-commit', [], b'')})
        message = self.root / 'message'
        message.write_text('Preview at ' + concrete)
        self.assertIn('worktree-path', {item['rule'] for item in guard.hook(
            self.repo, 'commit-msg', [str(message)], b'')})
        # Import an unguarded history, then check it at the push boundary.
        self.git('config', '--remove-section', 'hook.repo-guard-pre-commit')
        self.git('commit', '-qm', 'Add preview')
        self.stage('README.md', 'Application preview\n')
        self.git('commit', '-qm', 'Remove local path')
        local = self.git('rev-parse', 'HEAD')
        updates = f'refs/heads/main {local} refs/heads/main {"0" * 40}\n'
        findings = guard.hook(self.repo, 'pre-push', ['origin', 'example'], updates.encode())
        self.assertIn('worktree-path', {item['rule'] for item in findings})
        self.assertNotIn(concrete, json.dumps(findings))

    def test_success_output_states_the_scan_scope(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertFalse(guard.emit([]))
        self.assertIn('active credential and repository policy checks', output.getvalue())
        self.assertIn('Review project-specific content', output.getvalue())

    def test_candidate_profile_checks_names_in_a_repository_with_a_private_github_remote(self):
        self.git('remote', 'add', 'origin', 'https://github.com/owner/project.git')
        guard.enable(self.repo, 'candidate')
        self.stage('sample.txt', 'Northwind\n')
        self.assertEqual([item['rule'] for item in guard.check(self.repo, 'staged')], ['private-name'])

    def test_enable_keeps_the_candidate_profile_unless_private_is_allowed(self):
        guard.enable(self.repo, 'candidate')
        with self.assertRaises(guard.GuardError):
            guard.enable(self.repo, 'private')
        self.assertEqual(self.git('config', 'repoGuard.profile'), 'candidate')
        self.assertEqual(guard.main(['enable', '--repo', str(self.repo), '--profile', 'private']), 2)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(guard.main(['enable', '--repo', str(self.repo), '--profile', 'private', '--allow-private']), 0)
        self.assertEqual(self.git('config', 'repoGuard.profile'), 'private')

    def test_private_profile_allows_brand_but_rejects_credentials(self):
        self.git('config', 'repoGuard.profile', 'private')
        self.stage('sample.txt', 'Northwind\n')
        self.assertEqual(guard.check(self.repo, 'staged'), [])

    def test_own_repository_link_is_allowed_without_allowing_another_repository(self):
        self.git('remote', 'add', 'origin', 'https://github.com/example-owner/example-public.git')
        self.stage('sample.txt', 'https://github.com/example-owner/example-public/issues/1\ngit@github.com:example-owner/example-public.git\n')
        self.assertEqual(guard.check(self.repo, 'staged'), [])
        self.stage('sample.txt', 'https://github.com/example-owner/example-public https://github.com/example-owner/private-project\n')
        self.assertIn('private-repo-url', {item['rule'] for item in guard.check(self.repo, 'staged')})

    def test_gist_links_are_not_repository_links(self):
        # The gist content gets its own check; its link names no repository.
        for value in ('https://gist.github.com/example-owner/' + 'b' * 32,
                      'https://gist.githubusercontent.com/example-owner/' + 'b' * 32 + '/raw/run.log'):
            with self.subTest(value=value):
                self.assertEqual(guard.privacy(value, 'sample'), [])
        for value in ('https://github.com/example-owner/other', 'https://www.github.com/example-owner/other',
                      'git@github.com:example-owner/other.git'):
            with self.subTest(value=value):
                self.assertIn('private-repo-url', {item['rule'] for item in guard.privacy(value, 'sample')})

    def test_private_profile_still_rejects_credentials(self):
        self.git('config', 'repoGuard.profile', 'private')
        self.stage('sample.txt', 'API_TOKEN=' + FAKE_TOKEN + '\n')
        self.assertIn('github-pat', {item['rule'] for item in guard.check(self.repo, 'staged')})

    def test_secret_in_commit_message_is_found_in_history(self):
        self.git('commit', '--allow-empty', '-qm', 'API_TOKEN=' + FAKE_TOKEN)
        self.assertIn('github-pat', {item['rule'] for item in guard.check(self.repo, 'history')})

    def test_commit_metadata_findings_name_their_commit(self):
        self.git('commit', '--allow-empty', '-qm', 'Internal Northwind change')
        named = self.git('rev-parse', 'HEAD')
        self.git('commit', '--allow-empty', '-qm', 'API_TOKEN=' + FAKE_TOKEN)
        token = self.git('rev-parse', 'HEAD')
        self.git('-c', 'user.email=owner@northwind.example', 'commit', '--allow-empty', '-qm', 'Public change')
        author = self.git('rev-parse', 'HEAD')
        self.git('branch', 'northwind-fix')
        self.stage('.env.prod', 'LOCAL=1\n')
        self.git('commit', '-qm', 'Add settings')
        settings = self.git('rev-parse', 'HEAD')
        found = {(item['rule'], item['path'], item['commit'], item['line'])
                 for item in guard.check(self.repo, 'history')}
        self.assertIn(('private-name', 'commit metadata', named, 1), found)
        self.assertIn(('github-pat', 'commit metadata', token, 1), found)
        self.assertIn(('private-email', 'commit metadata', author, 3), found)
        self.assertIn(('private-name', 'ref and tag metadata', '', 2), found)
        self.assertIn(('sensitive-filename-history', '.env.prod', settings, 5), found)

    def test_annotated_tag_message_is_checked_before_publication(self):
        self.git('tag', '-a', 'sample', '-m', 'API_TOKEN=' + FAKE_TOKEN)
        self.assertIn('github-pat', {item['rule'] for item in guard.check(self.repo, 'history')})

    def test_annotated_tag_message_is_checked_before_push(self):
        self.git('tag', '-a', 'sample', '-m', 'API_TOKEN=' + FAKE_TOKEN)
        object_id = self.git('rev-parse', 'refs/tags/sample')
        updates = f'refs/tags/sample {object_id} refs/tags/sample {"0" * 40}\n'
        findings = guard.hook(self.repo, 'pre-push', ['origin', 'example'], updates.encode())
        self.assertIn('github-pat', {item['rule'] for item in findings})

    def test_archive_paths_are_not_extracted(self):
        import io
        import zipfile
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as archive:
            archive.writestr('../../outside.txt', 'Private Northwind text')
        self.assertIn(b'Private Northwind text', guard.archive_text(data.getvalue()))
        self.assertFalse((self.root / 'outside.txt').exists())

    def test_pre_push_scans_each_update_including_new_refs(self):
        local = self.git('rev-parse', 'HEAD')
        updates = f'refs/heads/main {local} refs/heads/main {"0" * 40}\n'
        with patch.object(guard, 'check', return_value=[]) as scan:
            guard.hook(self.repo, 'pre-push', ['origin', 'example'], updates.encode())
        scan.assert_called_once_with(self.repo, 'history', revisions=[local, '--not', '--remotes=origin'])

    def test_real_pre_push_blocks_deleted_private_content(self):
        if tuple(map(int, self.git('--version').split()[2].split('.')[:2])) < (2, 54):
            self.skipTest('Hook installation requires Git 2.54+.')
        remote = self.root / 'remote.git'
        subprocess.run(['git', 'init', '--bare', '-q', str(remote)], check=True)
        self.git('remote', 'add', 'origin', str(remote))
        self.stage('sample.txt', 'Northwind\n')
        self.git('commit', '-qm', 'Add sample')
        self.stage('sample.txt', 'Public sample\n')
        self.git('commit', '-qm', 'Replace sample')
        guard.enable(self.repo, 'candidate')
        self.git('push', 'origin', 'HEAD:refs/heads/main', expected=1)
        result = subprocess.run(['git', '--git-dir', str(remote), 'show-ref', '--verify', 'refs/heads/main'], capture_output=True)
        self.assertNotEqual(result.returncode, 0)

    def test_missing_scanner_is_an_error(self):
        with patch.dict(os.environ, {'REPO_GUARD_BINARY': str(self.root / 'absent')}):
            with self.assertRaises(guard.GuardError):
                guard.check(self.repo, 'staged')

    def test_inline_allow_comment_does_not_skip_credentials(self):
        self.stage('sample.txt', 'API_TOKEN=' + FAKE_TOKEN + ' # betterleaks:allow\n')
        self.assertIn('github-pat', {item['rule'] for item in guard.check(self.repo, 'staged')})

    def test_scanner_exception_does_not_allow_a_changed_secret_or_file(self):
        token = FAKE_TOKEN
        self.stage('sample.txt', 'API_TOKEN=' + token + '\n')
        match = next(item for item in guard.check(self.repo, 'staged') if item['rule'] == 'github-pat')
        state = guard.state_dir(self.repo)
        guard.save(state / 'audit.json', {'findings': [match]})
        guard.approve_finding(self.repo, match['fingerprint'], 'Deliberate non-live test value')
        self.assertEqual(guard.check(self.repo, 'staged'), [])
        self.stage('sample.txt', 'API_TOKEN=' + token[:-1] + 'B\n')
        self.assertIn('github-pat', {item['rule'] for item in guard.check(self.repo, 'staged')})
        self.stage('other.txt', 'API_TOKEN=' + token + '\n')
        self.assertTrue(any(item['path'] == 'other.txt' for item in guard.check(self.repo, 'staged')))

    def test_changed_github_content_invalidates_snapshot(self):
        first = guard.snapshot(self.repo, {'description': 'Original'})
        second = guard.snapshot(self.repo, {'description': 'Updated'})
        self.assertNotEqual(first, second)

    def test_deleted_intermediate_secret_is_found_in_outgoing_history(self):
        base = self.git('rev-parse', 'HEAD')
        self.stage('sample.txt', 'Northwind\n')
        self.git('commit', '-qm', 'Add sample')
        self.stage('sample.txt', 'Public sample\n')
        self.git('commit', '-qm', 'Replace sample')
        findings = guard.check(self.repo, 'history', revisions=[f'{base}..HEAD'])
        self.assertIn('private-name', {item['rule'] for item in findings})
        self.assertTrue(any(item['rule'] == 'private-name' and item['path'] == 'sample.txt'
                            and item['commit'] for item in findings))

    def test_changed_binary_requires_review(self):
        (self.repo / 'image.png').write_bytes(b'\x89PNG\0private image')
        self.git('add', 'image.png')
        self.assertIn('binary-review', {item['rule'] for item in guard.check(self.repo, 'staged')})

    def test_hooks_preserve_existing_hook_and_fail_closed(self):
        if tuple(map(int, self.git('--version').split()[2].split('.')[:2])) < (2, 54):
            self.skipTest('Hook installation requires Git 2.54+.')
        hook = self.repo / '.git/hooks/pre-commit'
        hook.write_text('#!/bin/sh\nprintf ran > original-ran\n')
        hook.chmod(0o755)
        guard.enable(self.repo, 'candidate')
        self.stage('sample.txt', 'Public sample\n')
        self.git('commit', '-qm', 'Clean')
        self.assertTrue((self.repo / 'original-ran').exists())
        self.stage('sample.txt', 'Northwind\n')
        self.git('commit', '-qm', 'Blocked', expected=1)

    def test_commit_message_is_checked(self):
        if tuple(map(int, self.git('--version').split()[2].split('.')[:2])) < (2, 54):
            self.skipTest('Hook installation requires Git 2.54+.')
        guard.enable(self.repo, 'candidate')
        self.stage('sample.txt', 'Public sample\n')
        self.git('commit', '-qm', 'Internal Northwind change', expected=1)

    def test_publish_needs_explicit_target_and_fresh_ready_review(self):
        with patch.object(guard, 'prepare', return_value={'snapshot': 'current', 'findings': [], 'github': {'full_name': 'owner/project'}}):
            with self.assertRaises(guard.GuardError):
                guard.publish(self.repo, None)
            with self.assertRaises(guard.GuardError):
                guard.publish(self.repo, 'owner/project')
            state = guard.state_dir(self.repo)
            (state / 'review.json').write_text(json.dumps({'snapshot': 'old', 'decision': 'ready'}))
            with self.assertRaises(guard.GuardError):
                guard.publish(self.repo, 'owner/project')

    def test_policy_change_invalidates_snapshot(self):
        with patch.object(guard, 'policy_hash', return_value='one'):
            first = guard.snapshot(self.repo, {})
        with patch.object(guard, 'policy_hash', return_value='two'):
            second = guard.snapshot(self.repo, {})
        self.assertNotEqual(first, second)

    def test_planned_repository_change_invalidates_snapshot(self):
        self.git('config', 'repoGuard.publicRepository', 'owner/first')
        first = guard.snapshot(self.repo, {})
        self.git('config', 'repoGuard.publicRepository', 'owner/second')
        self.assertNotEqual(first, guard.snapshot(self.repo, {}))

    def test_private_clone_tokens_are_not_saved_or_bound_to_release_review(self):
        repositories = iter([
            {'full_name': 'owner/project', 'private': True, 'description': 'Example project',
             'temp_clone_token': 'temporary-read-token-a'},
            {'full_name': 'owner/project', 'private': True, 'description': 'Example project',
             'temp_clone_token': 'temporary-read-token-b'},
        ])
        def read_surface(repo, endpoint, pages=False):
            return next(repositories) if endpoint == 'repos/owner/project' else [[]]
        with patch.object(guard, 'github_name', return_value='owner/project'), \
             patch.object(guard, 'gh_json', side_effect=read_surface):
            first = guard.github_surface(self.repo)
            second = guard.github_surface(self.repo)
        self.assertNotIn('temp_clone_token', first['repository'])
        self.assertNotIn('temporary-read-token', guard.surface_text(first))
        self.assertEqual(guard.snapshot(self.repo, first), guard.snapshot(self.repo, second))
        second['repository']['description'] = 'Changed public description'
        self.assertNotEqual(guard.snapshot(self.repo, first), guard.snapshot(self.repo, second))

    def test_github_audit_reads_review_summaries_and_commit_comments(self):
        def read(repo, endpoint, pages=False):
            if endpoint == 'repos/owner/project':
                return {'full_name': 'owner/project', 'private': True}
            if endpoint.startswith('repos/owner/project/issues?'):
                return [[{'number': 7, 'title': 'Change', 'pull_request': {}}, {'number': 8, 'title': 'Bug'}]]
            if endpoint.startswith('repos/owner/project/pulls/7/reviews'):
                return [[{'body': 'Review summary names Northwind'}]]
            if endpoint.startswith('repos/owner/project/comments'):
                return [[{'body': 'Commit comment with ' + HOME + 'realperson/notes'}]]
            if endpoint.startswith(('repos/owner/project/pulls/8', 'repos/owner/project/issues/8')):
                self.fail('An issue has no pull request reviews.')
            return [[]]
        with patch.object(guard, 'github_name', return_value='owner/project'), \
             patch.object(guard, 'gh_json', side_effect=read):
            surface = guard.github_surface(self.repo)
        rules = {item['rule'] for item in guard.privacy(guard.surface_text(surface), 'GitHub metadata')}
        self.assertEqual(rules, {'private-name', 'personal-path'})

    def test_review_criteria_change_invalidates_snapshot(self):
        tools = self.root / 'tools'
        tools.mkdir()
        for name in ('repo_guard.py', 'report_html.py'):
            (tools / name).write_bytes((guard.ROOT / name).read_bytes())
        criteria = tools / 'skills/public-repo/references/release-review.md'
        criteria.parent.mkdir(parents=True)
        criteria.write_text('Check setup.\n')
        with patch.object(guard, 'ROOT', tools), patch.object(guard, 'SKILL', tools / 'skills/public-repo'):
            first = guard.snapshot(self.repo, {})
            criteria.write_text('Check setup and public sample images.\n')
            second = guard.snapshot(self.repo, {})
        self.assertNotEqual(first, second)

    def publish_with(self, security):
        """Publish against a mocked GitHub that reports the given security state."""
        state = guard.state_dir(self.repo)
        report = b'<!doctype html><html><body>Inspected code, documentation, rights, tests, history and GitHub content.</body></html>'
        (state / 'review.html').write_bytes(report)
        import hashlib
        guard.save(state / 'review.json', {'snapshot': 'current', 'decision': 'ready',
                   'report_sha256': hashlib.sha256(report).hexdigest()})
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.git('config', 'repoGuard.profile', 'private')
        audit = {'snapshot': 'current', 'findings': [], 'github': {'full_name': 'owner/project'}}
        original = guard.run
        requests = []
        def dispatch(args, repo=None, **kwargs):
            if args[:2] == ['gh', 'api']:
                requests.append(args + ([kwargs['data'].decode()] if kwargs.get('data') else []))
                return subprocess.CompletedProcess(args, 0, b'', b'')
            return original(args, repo, **kwargs)
        replies = [{'default_branch': 'main'}, {'private': False}, {'private': False, 'security_and_analysis': security}]
        with patch.object(guard, 'prepare', return_value=audit), \
             patch.object(guard, 'gh_json', side_effect=replies), \
             patch.object(guard, 'run', side_effect=dispatch):
            try:
                guard.publish(self.repo, 'owner/project')
            except guard.GuardError as error:
                return requests, error
        return requests, None

    def test_publication_turns_on_secret_scanning_and_confirms_it(self):
        enabled = {'secret_scanning': {'status': 'enabled'}, 'secret_scanning_push_protection': {'status': 'enabled'}}
        requests, error = self.publish_with(enabled)
        self.assertIsNone(error)
        self.assertEqual(requests[0], ['gh', 'api', '--method', 'PATCH', 'repos/owner/project', '-f', 'visibility=public'])
        self.assertEqual(requests[1][:6], ['gh', 'api', '--method', 'PATCH', 'repos/owner/project', '--input'])
        self.assertEqual(json.loads(requests[1][-1]), {'security_and_analysis': enabled})
        self.assertEqual(len(requests), 2)
        # Future work in this checkout gets the full public checks.
        self.assertEqual(self.git('config', 'repoGuard.profile'), 'candidate')

    def test_publication_returns_to_private_without_confirmed_scanning(self):
        requests, error = self.publish_with({'secret_scanning': {'status': 'enabled'},
                                             'secret_scanning_push_protection': {'status': 'disabled'}})
        self.assertIsInstance(error, guard.GuardError)
        self.assertIn('private again', str(error))
        self.assertEqual(requests[-1], ['gh', 'api', '--method', 'PATCH', 'repos/owner/project', '-f', 'visibility=private'])

    def test_audit_of_a_public_repository_needs_secret_scanning(self):
        self.stage('README.md', 'Example project\n')
        self.stage('LICENSE', 'Example license\n')
        self.git('commit', '-qm', 'Documentation')
        self.git('remote', 'add', 'origin', 'https://github.com/owner/project.git')
        def surface(security):
            return {'repository': {'full_name': 'owner/project', 'private': False, 'security_and_analysis': security},
                    'resources': {}}
        original = guard.run
        def local(args, repo=None, **kwargs):
            if args[:2] == ['git', 'fetch']:
                return subprocess.CompletedProcess(args, 0, b'', b'')
            return original(args, repo, **kwargs)
        for security, expected in (({'secret_scanning': {'status': 'disabled'},
                                     'secret_scanning_push_protection': {'status': 'enabled'}}, True),
                                   ({'secret_scanning': {'status': 'enabled'},
                                     'secret_scanning_push_protection': {'status': 'enabled'}}, False)):
            with self.subTest(security=security), patch.object(guard, 'github_surface', return_value=surface(security)), \
                 patch.object(guard, 'run', side_effect=local):
                rules = {item['rule'] for item in guard.prepare(self.repo)['findings']}
            self.assertEqual('secret-scanning-off' in rules, expected)

    def test_binary_approval_applies_to_exact_content(self):
        self.stage('image.png', 'Placeholder')
        item = self.repo / 'image.png'
        item.write_bytes(b'\x89PNG\0reviewed')
        self.git('add', 'image.png')
        import hashlib
        digest = hashlib.sha256(item.read_bytes()).hexdigest()
        guard.save(guard.state_dir(self.repo) / 'binary-reviews.json', {digest: {'note': 'Inspected image'}})
        self.assertEqual(guard.check(self.repo, 'staged'), [])
        item.write_bytes(b'\x89PNG\0changed')
        self.git('add', 'image.png')
        self.assertIn('binary-review', {item['rule'] for item in guard.check(self.repo, 'staged')})

    def test_dirty_tree_cannot_be_published(self):
        self.stage('new.txt', 'Change\n')
        with self.assertRaises(guard.GuardError):
            guard.require_clean(self.repo)

    def audit(self):
        surface = {'description': 'Test'}
        audit = {'snapshot': guard.snapshot(self.repo, surface), 'head': self.git('rev-parse', 'HEAD'),
                 'policy_hash': guard.policy_hash(), 'github': {'full_name': 'owner/project', 'private': True},
                 'findings': [], 'media_to_review': [], 'manual_surfaces': ['Tests']}
        guard.save(guard.state_dir(self.repo) / 'audit.json', audit)
        guard.save(guard.state_dir(self.repo) / 'github-surface.json', surface)
        return audit

    def test_html_report_is_ignored_and_escapes_repository_content(self):
        audit = self.audit()
        audit['findings'] = [guard.finding('private-name', '<script>alert(1)</script>', 2)]
        path = Path(guard.write_report(self.repo, audit))
        text = path.read_text()
        self.assertIn('&lt;script&gt;', text)
        self.assertNotIn('<script>', text)
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertTrue(text.startswith('<!doctype html>'))

    def test_review_requires_html_and_preserves_it_in_the_report(self):
        self.audit()
        source = self.root / 'analysis.html'
        source.write_text('# Markdown review')
        with self.assertRaises(ValueError):
            guard.review(self.repo, 'blocked', source)
        source.write_text('<!doctype html><html><body><p>Checked architecture. License is missing.</p></body></html>')
        record = guard.review(self.repo, 'blocked', source)
        self.assertIn('License is missing.', Path(record['report_path']).read_text())
        self.assertIn("release decision is a separate step", Path(record['report_path']).read_text())
        self.assertEqual(record['next_step'], "Wait for the user's first-release decision.")
        self.assertTrue((guard.state_dir(self.repo) / 'review.html').is_file())
        self.assertFalse((guard.state_dir(self.repo) / 'review.md').exists())

    def test_report_shows_github_metadata_and_escapes_its_values(self):
        audit = self.audit()
        metadata = {'description': '<script>example</script>',
                    'homepage': 'https://example.invalid/?a=1&b=2',
                    'topics': ['html', 'report-viewer']}
        audit['github'].update(metadata)
        text = Path(guard.write_report(self.repo, audit)).read_text()
        self.assertIn('&lt;script&gt;example&lt;/script&gt;', text)
        self.assertNotIn('<script>', text)
        self.assertIn('https://example.invalid/?a=1&amp;b=2', text)
        self.assertIn('html, report-viewer', text)
        audit['github'].update(description=None, homepage=None, topics=[])
        text = Path(guard.write_report(self.repo, audit)).read_text()
        self.assertIn('Missing description', text)
        self.assertIn('No topics', text)
        self.assertIn('Not set (optional)', text)
        self.assertEqual(audit['findings'], [])

    def test_preparation_stops_at_local_report_without_creation_push_or_publication(self):
        self.stage('README.md', 'Example project\n')
        self.stage('LICENSE', 'Example license\n')
        self.git('commit', '-qm', 'Documentation')
        self.git('remote', 'add', 'origin', 'https://github.com/owner/project.git')
        head = self.git('rev-parse', 'HEAD')
        remotes = self.git('remote', '-v')
        surface = {'repository': {'full_name': 'owner/project', 'private': True,
                                  'description': 'Browse project reports', 'homepage': None,
                                  'topics': ['html', 'reports']},
                   'resources': {'issues?state=all': [[]], 'issues/comments': [[]],
                                 'pulls/comments': [[]], 'releases': [[]],
                                 'actions/runs': [{'workflow_runs': []}],
                                 'actions/artifacts': [{'artifacts': []}]}}
        commands = []
        original = guard.run

        def local_only(args, repo=None, **kwargs):
            commands.append(args)
            if args[:2] == ['git', 'fetch']:
                return subprocess.CompletedProcess(args, 0, b'', b'')
            if args[0] == 'gh' or args[:2] in (['git', 'push'], ['git', 'init']):
                self.fail(f'Preparation attempted a release operation: {args}')
            return original(args, repo, **kwargs)

        with patch.object(guard, 'github_surface', return_value=surface), \
             patch.object(guard, 'run', side_effect=local_only):
            audit = guard.prepare(self.repo)
        self.assertTrue(Path(audit['report_path']).is_file())
        guide = Path(audit['review_guide'])
        self.assertTrue(guide.is_file())
        self.assertIn(f'href="{guide}"', Path(audit['report_path']).read_text())
        self.assertEqual(audit['github']['description'], 'Browse project reports')
        self.assertIsNone(audit['github']['homepage'])
        self.assertEqual(audit['github']['topics'], ['html', 'reports'])
        self.assertTrue(any(args[:2] == ['git', 'fetch'] for args in commands))
        self.assertEqual(self.git('rev-parse', 'HEAD'), head)
        self.assertEqual(self.git('remote', '-v'), remotes)
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertFalse((guard.state_dir(self.repo) / 'review.json').exists())

    def test_configuration_review_lists_values_a_new_user_can_need_to_change(self):
        self.stage('tool.py', "DATA_ROOT = '/opt/acme/data'\n"
                              "CACHE = Path.home() / '.cache/acme'\n"
                              "run(['ws', 'adopt', path])\n"
                              "fetch('https://ci:" + "pass@build.internal:8080/api?token=x', timeout=45)\n"
                              "LIMIT = 64 * 1024 * 1024\n"
                              "OWNER_EMAIL = 'owner@example.org'\n"
                              "PROXY = '192.168.4.20'\n")
        self.stage('README.md', 'Settings are in `~/.config/acme`. Mail owner@example.org.\n')
        self.stage('tests/test_tool.py', "ROOT = '/opt/fixture'\n")
        self.stage('tool_test.go', 'const root = "/opt/go-fixture"\n')
        self.stage('logo.png', '\0/opt/hidden')
        self.git('commit', '-qm', 'Tool')
        items = guard.configuration(self.repo)
        found = {(item['kind'], item['value']) for item in items}
        for expected in [('path', '/opt/acme/data'), ('home-path', "Path.home() / '.cache/acme'"),
                         ('home-path', '~/.config/acme'), ('command', 'ws'),
                         ('url', 'https://build.internal:8080/api'), ('host', 'build.internal'),
                         ('host', '192.168.4.20'), ('number', 'timeout=45'),
                         ('number', '64 * 1024 * 1024'), ('email', 'owner@example.org'),
                         ('constant', 'DATA_ROOT'), ('constant', 'OWNER_EMAIL')]:
            self.assertIn(expected, found)
        values = json.dumps(items)
        # Test fixtures and binary files are not user configuration.
        for absent in ('/opt/fixture', '/opt/go-fixture', '/opt/hidden', 'pass', 'token=x'):
            self.assertNotIn(absent, values)
        # Documentation shows defaults; its prose email is not a code setting.
        self.assertEqual([item['path'] for item in items if item['kind'] == 'email'], ['tool.py'])
        first = next(item for item in items if item['value'] == '/opt/acme/data')
        self.assertEqual((first['path'], first['line'], first['count']), ('tool.py', 1, 1))

    def test_report_lists_configuration_values_for_review_and_escapes_them(self):
        audit = self.audit()
        audit['configuration'] = [{'kind': 'path', 'value': '/opt/<b>acme</b>', 'path': 'tool.py',
                                   'line': 3, 'count': 2}]
        text = Path(guard.write_report(self.repo, audit)).read_text()
        self.assertIn('Configuration review · 1 values', text)
        self.assertIn('/opt/&lt;b&gt;acme&lt;/b&gt;', text)
        self.assertIn('tool.py:3', text)
        self.assertNotIn('<b>acme', text)
        self.assertIn('Configuration and portability', ' '.join(guard.MANUAL_SURFACES))
        guide = (HERE / 'skills/public-repo/references/release-review.md').read_text()
        self.assertIn('## Configuration and portability', guide)

    def test_audit_output_points_to_its_report_for_more_findings(self):
        import contextlib
        import io
        result = {'findings': [guard.finding('private-name', f'file{number}.txt', 1) for number in range(101)],
                  'report_path': '/private/report.html'}
        output = io.StringIO()
        with patch.object(guard, 'prepare', return_value=result), contextlib.redirect_stdout(output):
            guard.main(['prepare-public', '--repo', str(self.repo)])
        self.assertIn('1 more findings. See /private/report.html', output.getvalue())
        self.assertNotIn('Use prepare-public', output.getvalue())

    def test_check_text_applies_the_selected_profile(self):
        import contextlib
        import io
        sample = self.root / 'post.md'
        sample.write_text('Northwind notes. See https://github.com/example-owner/project\n')
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(guard.main(['check-text', str(sample)]), 1)
        self.assertIn('private-name', output.getvalue())
        self.assertIn('private-repo-url', output.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(guard.main(['check-text', str(sample), '--profile', 'private']), 0)
        sample.write_text('See https://github.com/example-owner/project\n')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(guard.main(['check-text', str(sample), '--target', 'example-owner/project']), 0)
        sample.write_text('API_TOKEN=' + FAKE_TOKEN + '\n')
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(guard.main(['check-text', str(sample), '--profile', 'private']), 1)
        self.assertNotIn(FAKE_TOKEN, output.getvalue())

    def gh(self, *args, private=True, cwd=None, data=b'', environment=None, gist=''):
        """Run the gh wrapper in front of a fake gh that records each call."""
        log = self.root / 'gh-calls.jsonl'
        fake = self.root / 'fake-gh'
        answer = 'true' if private else 'false'
        fake.write_text('#!/usr/bin/env python3\nimport json, sys\n'
                        'data = sys.stdin.read()\n'
                        f'with open({str(log)!r}, "a") as target:\n'
                        '    target.write(json.dumps({"args": sys.argv[1:], "stdin": data}) + "\\n")\n'
                        f'answers = {{"api": {answer!r}, "gist view": {gist!r}}}\n'
                        'print(answers.get(" ".join(sys.argv[1:3]), answers.get(sys.argv[1], "posted")))\n')
        fake.chmod(0o755)
        env = dict(os.environ, REPO_GUARD_GH=str(fake), **(environment or {}))
        result = subprocess.run([str(HERE / 'shims/gh'), *args], cwd=cwd or self.root, input=data,
                                env=env, capture_output=True)
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        log.unlink(missing_ok=True)
        return result, calls

    def test_gh_wrapper_checks_text_before_a_public_post(self):
        result, calls = self.gh('pr', 'create', '-R', 'example-owner/public', '--title', 'Sync',
                                '--body', 'Northwind sync notes', private=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn(b'private-name: --body:1', result.stdout)
        self.assertEqual([call['args'][0] for call in calls], ['api'])
        # A private repository can name its own private projects.
        result, calls = self.gh('pr', 'create', '-R', 'example-owner/internal', '--title', 'Sync',
                                '--body', 'Northwind sync notes')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls[-1]['args'], ['pr', 'create', '-R', 'example-owner/internal', '--title', 'Sync',
                                             '--body', 'Northwind sync notes'])
        # Credentials are refused everywhere.
        result, calls = self.gh('issue', 'comment', '3', '-R', 'example-owner/internal',
                                '--body=API_TOKEN=' + FAKE_TOKEN)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(FAKE_TOKEN.encode(), result.stdout + result.stderr)
        self.assertFalse(any(call['args'][0] == 'issue' for call in calls))

    def test_gh_wrapper_checks_body_files_and_forwards_the_same_input(self):
        result, calls = self.gh('pr', 'comment', '9', '-R', 'example-owner/internal', '--body-file', '-',
                                data=b'Release notes\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls[-1]['stdin'], 'Release notes\n')
        notes = self.root / 'notes.md'
        notes.write_text('Data lives in ' + HOME + 'realperson/data\n')
        result, calls = self.gh('release', 'create', 'v1', '-R', 'example-owner/public', '-F', str(notes),
                                private=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn(b'personal-path', result.stdout)

    def test_gh_wrapper_checks_a_gist_by_its_visibility(self):
        proof = self.root / 'proof.log'
        # A secret gist reaches only the people who get its link.
        proof.write_text('Northwind test run passed\n')
        result, calls = self.gh('gist', 'create', str(proof), '--desc', 'Proof')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls[-1]['args'][:2], ['gist', 'create'])
        result, calls = self.gh('gist', 'create', str(proof), '--public')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(calls, [])
        proof.write_text('API_TOKEN=' + FAKE_TOKEN + '\n')
        result, calls = self.gh('gist', 'create', str(proof))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(calls, [])

    def test_gh_wrapper_checks_a_linked_gist_with_the_post(self):
        body = 'Proof: https://gist.githubusercontent.com/someone/' + 'a' * 32 + '/raw/run.log'
        result, calls = self.gh('pr', 'create', '-R', 'example-owner/public', '--title', 'Fix', '--body', body,
                                private=False, gist='Run on ' + HOME + 'realperson/project\n')
        self.assertEqual(result.returncode, 1)
        self.assertIn(b'personal-path: gist ' + b'a' * 32, result.stdout)
        self.assertIn(['gist', 'view', 'a' * 32, '--raw'], [call['args'] for call in calls])
        self.assertFalse(any(call['args'][:2] == ['pr', 'create'] for call in calls))
        result, calls = self.gh('pr', 'create', '-R', 'example-owner/internal', '--title', 'Fix', '--body', body,
                                gist='Run on ' + HOME + 'realperson/project\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        # check-text reads linked gists for a merge gate.
        import contextlib
        import io
        sample = self.root / 'body.md'
        sample.write_text(body + '\n')
        with patch.dict(os.environ, {'REPO_GUARD_GH': str(self.root / 'fake-gh')}), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(guard.main(['check-text', str(sample), '--linked-gists']), 1)
        self.assertIn('personal-path: gist ' + 'a' * 32, output.getvalue())

    def test_gh_wrapper_uses_the_local_candidate_profile_and_passes_other_commands(self):
        self.git('remote', 'add', 'origin', 'https://github.com/example-owner/project.git')
        result, calls = self.gh('pr', 'edit', '4', '--body', 'Northwind', cwd=self.repo)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(calls, [])
        result, calls = self.gh('pr', 'list', cwd=self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [{'args': ['pr', 'list'], 'stdin': ''}])

    def test_gh_wrapper_checks_merge_messages_and_repository_metadata(self):
        result, calls = self.gh('pr', 'merge', '5', '-R', 'example-owner/public', '--squash',
                                '--body', 'Northwind fix', private=False)
        self.assertEqual(result.returncode, 1)
        result, calls = self.gh('repo', 'edit', 'example-owner/public', '--add-topic', 'northwind',
                                private=False)
        self.assertEqual(result.returncode, 1)
        self.assertEqual([call['args'][:2] for call in calls], [['api', 'repos/example-owner/public']])
        # A private repository's description stays private.
        result, calls = self.gh('repo', 'create', 'example-owner/internal-tool', '--private',
                                '--description', 'Northwind tool')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_gh_wrapper_sends_public_visibility_to_publish(self):
        for args in (['repo', 'create', 'example-owner/new-tool', '--public'],
                     ['repo', 'create', 'example-owner/new-tool'],
                     ['repo', 'edit', 'example-owner/tool', '--visibility', 'public', '--accept-visibility-change-consequences'],
                     ['repo', 'edit', 'example-owner/tool', '--visibility=public']):
            with self.subTest(args=args):
                result, calls = self.gh(*args)
                self.assertEqual(result.returncode, 1)
                self.assertIn(b'repo-guard publish', result.stderr)
                self.assertEqual(calls, [])
        for args in (['repo', 'create', 'example-owner/new-tool', '--private'],
                     ['repo', 'edit', 'example-owner/tool', '--visibility', 'private', '--accept-visibility-change-consequences']):
            with self.subTest(args=args):
                result, calls = self.gh(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(calls[-1]['args'], args)

    def test_gh_wrapper_refuses_a_post_when_the_check_cannot_run(self):
        result, calls = self.gh('pr', 'comment', '1', '-R', 'example-owner/internal', '--body', 'Notes',
                                environment={'REPO_GUARD_BINARY': str(self.root / 'absent')})
        self.assertEqual(result.returncode, 2)
        self.assertFalse(any(call['args'][0] == 'pr' for call in calls))

    def test_audit_keeps_a_private_profile_during_cleanup(self):
        self.stage('README.md', 'Example project\n')
        self.stage('LICENSE', 'Example license\n')
        self.git('commit', '-qm', 'Documentation')
        self.git('remote', 'add', 'origin', 'https://github.com/owner/project.git')
        surface = {'repository': {'full_name': 'owner/project', 'private': True}, 'resources': {}}
        original = guard.run
        def local(args, repo=None, **kwargs):
            if args[:2] == ['git', 'fetch']:
                return subprocess.CompletedProcess(args, 0, b'', b'')
            return original(args, repo, **kwargs)
        for configured, expected in (('private', 'private'), (None, 'candidate')):
            if configured:
                self.git('config', 'repoGuard.profile', configured)
            else:
                self.git('config', '--unset', 'repoGuard.profile')
            with self.subTest(configured=configured), patch.object(guard, 'github_surface', return_value=surface), \
                 patch.object(guard, 'run', side_effect=local):
                guard.prepare(self.repo)
            self.assertEqual(self.git('config', 'repoGuard.profile'), expected)
            self.assertIn('repo-guard', self.git('config', '--get', 'hook.repo-guard-pre-commit.command'))

    def test_clean_history_keeps_the_source_local_excludes(self):
        self.git('remote', 'add', 'origin', 'https://github.com/owner/project.git')
        with (self.repo / '.git/info/exclude').open('a') as target:
            target.write('/.ws/\n')
        destination = self.root / 'clean'
        self.clean_copy(destination)
        (destination / '.ws').mkdir()
        (destination / '.ws/env.json').write_text('{}')
        self.assertEqual(guard.git(destination, 'status', '--porcelain').strip(), b'')

    def test_generated_report_does_not_replace_a_tracked_report(self):
        self.stage('reports/report.html', '<html><body>Existing report</body></html>')
        self.git('commit', '-qm', 'Existing report')
        audit = self.audit()
        destination = Path(guard.write_report(self.repo, audit))
        self.assertEqual(destination, guard.state_dir(self.repo) / 'report.html')
        self.assertEqual((self.repo / 'reports/report.html').read_text(), '<html><body>Existing report</body></html>')

    def test_changed_audit_removes_an_outdated_ready_decision(self):
        audit = self.audit()
        source = self.root / 'analysis.html'
        source.write_text('<html><body><p>Everything reviewed.</p></body></html>')
        record = guard.review(self.repo, 'ready', source)
        self.assertIn('>Ready<', Path(record['report_path']).read_text())
        audit['snapshot'] = 'changed'
        text = Path(guard.write_report(self.repo, audit)).read_text()
        self.assertIn('Code review required', text)
        self.assertNotIn('Everything reviewed.', text)

    def clean_copy(self, destination):
        with patch.object(guard, 'gh_json', side_effect=[{'private': True}, {'login': 'example-user', 'id': 123}]):
            return guard.clean_history(self.repo, destination, 'owner/project')

    def test_clean_history_keeps_only_current_tree_and_one_public_author_commit(self):
        self.git('remote', 'add', 'origin', 'https://github.com/owner/project.git')
        self.stage('.gitignore', '.env\nreports/\n')
        self.stage('old.txt', 'Removed private Northwind example\n')
        self.git('commit', '-qm', 'Historical private content')
        old_commit = self.git('rev-parse', 'HEAD')
        self.git('rm', 'old.txt')
        self.stage('run.sh', '#!/bin/sh\nexit 0\n')
        (self.repo / 'run.sh').chmod(0o755)
        self.git('add', 'run.sh')
        (self.repo / 'reports').mkdir()
        (self.repo / 'reports/tracked.html').write_text('<html><body>Current example</body></html>')
        self.git('add', '-f', 'reports/tracked.html')
        (self.repo / 'link').symlink_to('run.sh')
        self.git('add', 'link')
        self.git('commit', '-qm', 'Current source')
        (self.repo / '.env').write_text('LOCAL_ONLY=private\n')
        source_head = self.git('rev-parse', 'HEAD')
        destination = self.root / 'clean'
        with patch.dict(os.environ, {'GIT_AUTHOR_EMAIL': 'old@private.invalid',
                                     'GIT_COMMITTER_EMAIL': 'old@private.invalid'}):
            record = self.clean_copy(destination)
        def fresh(*args):
            return subprocess.run(['git', *args], cwd=destination, capture_output=True, text=True)
        self.assertEqual(fresh('rev-list', '--count', '--all').stdout.strip(), '1')
        self.assertNotEqual(fresh('cat-file', '-e', old_commit).returncode, 0)
        self.assertEqual(fresh('rev-parse', 'HEAD^{tree}').stdout.strip(), self.git('rev-parse', 'HEAD^{tree}'))
        self.assertEqual(fresh('log', '-1', '--format=%ae %ce').stdout.strip(),
                         '123+example-user@users.noreply.github.com 123+example-user@users.noreply.github.com')
        self.assertEqual(fresh('remote').stdout.strip(), '')
        self.assertEqual(fresh('status', '--porcelain').stdout.strip(), '')
        self.assertFalse((destination / '.env').exists())
        self.assertFalse((destination / 'old.txt').exists())
        self.assertEqual((destination / 'link').readlink(), Path('run.sh'))
        self.assertTrue((destination / 'run.sh').stat().st_mode & 0o111)
        self.assertEqual(self.git('rev-parse', 'HEAD'), source_head)
        self.assertTrue(record['needs_release_review'])

    def test_clean_history_needs_matching_confirmation_and_an_unused_destination(self):
        self.git('remote', 'add', 'origin', 'https://github.com/owner/project.git')
        with self.assertRaises(guard.GuardError), patch.object(guard, 'gh_json') as api:
            guard.clean_history(self.repo, self.root / 'unused', 'owner/wrong-project')
        api.assert_not_called()
        destination = self.root / 'existing'
        destination.mkdir()
        (destination / 'keep.txt').write_text('Keep this file')
        with patch.object(guard, 'gh_json', return_value={'private': True}):
            with self.assertRaises(guard.GuardError):
                guard.clean_history(self.repo, destination, 'owner/project')
        self.assertEqual((destination / 'keep.txt').read_text(), 'Keep this file')

    def test_clean_history_accepts_an_ignored_workspace_inside_the_source(self):
        self.git('remote', 'add', 'origin', 'https://github.com/owner/project.git')
        with self.assertRaises(guard.GuardError):
            self.clean_copy(self.repo / '.worktrees/unignored')
        self.assertFalse((self.repo / '.worktrees/unignored').exists())
        self.stage('.gitignore', '.worktrees/\n')
        self.stage('README.md', 'Example project\n')
        self.stage('LICENSE', 'Example license\n')
        self.git('commit', '-qm', 'Public source')
        with self.assertRaises(guard.GuardError):
            self.clean_copy(self.repo / 'visible-copy')
        destination = self.repo / '.worktrees/public-source'
        self.clean_copy(destination)
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertEqual(guard.git(destination, 'rev-list', '--count', '--all').strip(), b'1')

    def test_local_audit_of_clean_source_needs_no_remote_or_github_access(self):
        self.git('remote', 'add', 'origin', 'https://github.com/owner/project.git')
        self.stage('old.txt', 'Internal Northwind content\n')
        self.git('commit', '-qm', 'Old private material')
        old = self.git('rev-parse', 'HEAD')
        self.git('rm', 'old.txt')
        target = guard.policy()['private_repo_owner'] + '/public-project'
        self.stage('README.md', 'https://github.com/' + target + '\n')
        self.stage('LICENSE', 'Example license\n')
        self.git('commit', '-qm', 'Public source')
        destination = self.root / 'clean'
        self.clean_copy(destination)
        original = guard.run
        def local_only(args, repo=None, **kwargs):
            if args[0] == 'gh' or args[:2] in (['git', 'fetch'], ['git', 'push']):
                self.fail(f'Local audit used a remote: {args}')
            return original(args, repo, **kwargs)
        with patch.object(guard, 'run', side_effect=local_only):
            audit = guard.prepare(destination, local_only=True, target=target)
        self.assertEqual(audit['scope'], 'local')
        self.assertEqual(audit['findings'], [])
        self.assertEqual(guard.check(destination, 'history'), [])
        self.assertIn('GitHub audit required', Path(audit['report_path']).read_text())
        self.assertEqual(guard.git(destination, 'remote').strip(), b'')
        self.assertNotEqual(guard.run(['git', 'cat-file', '-e', old], destination, allowed=(0, 1, 128)).returncode, 0)
        analysis = self.root / 'review.html'
        analysis.write_text('<html><body><p>Local source and media reviewed.</p></body></html>')
        record = guard.review(destination, 'ready', analysis)
        self.assertEqual(record['scope'], 'local')
        with patch.object(guard, 'prepare') as prepare:
            with self.assertRaisesRegex(guard.GuardError, 'GitHub audit'):
                guard.publish(destination, 'owner/project')
            prepare.assert_not_called()

    def test_clean_history_rejects_dirty_source_and_submodules(self):
        self.git('remote', 'add', 'origin', 'https://github.com/owner/project.git')
        self.stage('pending.txt', 'Pending change')
        with self.assertRaises(guard.GuardError):
            guard.clean_history(self.repo, self.root / 'unused', 'owner/project')
        self.git('commit', '-qm', 'Pending change')
        head = self.git('rev-parse', 'HEAD')
        self.git('update-index', '--add', '--cacheinfo', f'160000,{head},submodule')
        self.git('commit', '-qm', 'Submodule needs review')
        with patch.object(guard, 'gh_json', return_value={'private': True}):
            with self.assertRaises(guard.GuardError):
                guard.clean_history(self.repo, self.root / 'unused', 'owner/project')
        self.assertFalse((self.root / 'unused').exists())


if __name__ == '__main__':
    unittest.main()
