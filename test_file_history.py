"""File history evidence uses local Git data, separate from license terms."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import repo_guard as guard


class FileHistoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / 'source'
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Example')
        self.git('config', 'user.email', 'example@example.invalid')
        self.git('config', 'commit.gpgsign', 'false')
        self.write('LICENSE', 'Example inherited license\n')
        self.commit('Licensed source')
        self.write('shared.py', ''.join(f'value_{n} = {n}\n' for n in range(30)))
        self.commit('Add upstream module')
        self.git('tag', 'upstream')
        self.write('asset.py', 'def import_asset():\n    return "local asset"\n')
        self.commit('Add fork module')
        self.license = (self.repo / 'LICENSE').read_bytes()

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.repo,
                                       env=dict(os.environ, GIT_CONFIG_GLOBAL='/dev/null',
                                                GIT_CONFIG_SYSTEM='/dev/null')).decode().strip()

    def write(self, name, value):
        (self.repo / name).write_text(value)
        self.git('add', '--', name)

    def commit(self, message):
        self.git('-c', 'core.hooksPath=/dev/null', 'commit', '-qm', message)

    def evidence(self, path='asset.py', upstream='upstream'):
        result = guard.file_history(self.repo, 'HEAD', upstream, [path])
        self.assertEqual((self.repo / 'LICENSE').read_bytes(), self.license)
        self.assertEqual(self.git('status', '--porcelain'), '')
        return result['files'][0]

    def test_fork_addition_does_not_inherit_upstream_history_from_license(self):
        item = self.evidence()
        self.assertEqual(item['history_status'], 'recorded')
        self.assertFalse(item['first_addition_in_upstream_history'])
        self.assertFalse(item['first_blob_in_upstream_history'])
        self.assertEqual(item['first_addition']['path'], 'asset.py')

    def test_modified_renamed_upstream_file_keeps_upstream_history(self):
        self.git('mv', 'shared.py', 'renamed.py')
        with (self.repo / 'renamed.py').open('a') as target:
            target.write('extra = True\n')
        self.git('add', 'renamed.py')
        self.commit('Rename and modify upstream module')
        item = self.evidence('renamed.py')
        self.assertTrue(item['first_addition_in_upstream_history'])
        self.assertEqual(item['first_addition']['path'], 'shared.py')
        self.assertTrue(any(entry['status'].startswith('R') for entry in item['trail']))

    def test_deleted_upstream_path_still_has_upstream_history(self):
        self.git('branch', 'fork')
        self.git('checkout', '-qb', 'later-upstream', 'upstream')
        self.git('rm', 'shared.py')
        self.commit('Delete upstream module')
        self.git('tag', 'upstream-deleted')
        self.git('checkout', '-q', 'fork')
        self.assertTrue(self.evidence('shared.py', 'upstream-deleted')['first_addition_in_upstream_history'])

    def test_copy_at_new_path_reports_matching_upstream_blob(self):
        content = (self.repo / 'shared.py').read_text()
        self.git('rm', 'shared.py')
        self.commit('Remove local path before a later copy')
        self.write('copied.py', content)
        self.commit('Copy module to a new path')
        item = self.evidence('copied.py')
        self.assertFalse(item['first_addition_in_upstream_history'])
        self.assertTrue(item['first_blob_in_upstream_history'])

    def test_missing_ref_and_missing_file_are_unknown(self):
        for path, upstream in [('asset.py', 'absent'), ('absent.py', 'upstream')]:
            with self.subTest(path=path, upstream=upstream):
                item = self.evidence(path, upstream)
                self.assertEqual(item['history_status'], 'unknown')
                self.assertIsNone(item['first_addition_in_upstream_history'])

    def test_shallow_history_is_unknown(self):
        shallow = Path(self.temp.name) / 'shallow'
        subprocess.check_call(['git', 'clone', '-q', '--depth=1', self.repo.as_uri(), str(shallow)])
        result = guard.file_history(shallow, 'HEAD', 'HEAD', ['asset.py'])
        self.assertEqual(result['files'][0]['history_status'], 'unknown')
        self.assertIsNone(result['files'][0]['first_addition_in_upstream_history'])

    def test_root_addition_is_unknown_after_history_cleanup(self):
        item = self.evidence('LICENSE')
        self.assertEqual(item['history_status'], 'unknown')

    def test_unrelated_upstream_history_is_unknown(self):
        root = self.git('commit-tree', self.git('write-tree'), '-m', 'Unrelated imported tree')
        self.git('tag', 'unrelated', root)
        self.assertEqual(self.evidence(upstream='unrelated')['history_status'], 'unknown')

    def test_unknown_cli_returns_one_with_json_evidence(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = guard.main(['file-history', '--repo', str(self.repo), '--upstream', 'absent', 'asset.py'])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())['files'][0]['history_status'], 'unknown')

    def test_cli_emits_evidence_without_policy_or_notice_changes(self):
        self.write('NOTICE', 'The asset module is derived from upstream.\n')
        self.commit('Sanitized incident notice')
        before = (self.repo / 'NOTICE').read_bytes()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = guard.main(['file-history', '--repo', str(self.repo), '--upstream', 'upstream', 'asset.py'])
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(output.getvalue())['files'][0]['first_addition_in_upstream_history'])
        self.assertEqual((self.repo / 'NOTICE').read_bytes(), before)
