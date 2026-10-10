"""Input-integrity and preservation tests; none require network access."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import blauos
import patches
sys.path.insert(0, str(ROOT / 'scripts/build'))
import headers

class InputIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / 'source'
        self.repo.mkdir()
        self.git('init', '--quiet')
        self.git('config', 'user.name', 'Build test')
        self.git('config', 'user.email', 'build-test@example.invalid')
        self.git('remote', 'add', 'origin', 'https://github.com/fixture/source.git')
        (self.repo / 'input.h').write_text('original\n')
        self.git('add', 'input.h')
        self.git('commit', '--quiet', '-m', 'fixture')
        self.source = {'name':'fixture','url':'https://github.com/fixture/source.git',
                       'revision':self.git('rev-parse','HEAD'),'tree':self.git('rev-parse','HEAD^{tree}'),
                       'license':'MIT','role':'test fixture'}
        self.manifest = self.root / 'sources.lock.json'

    def tearDown(self):
        self.temporary.cleanup()

    def git(self, *args):
        return blauos.run(['git', *args], cwd=self.repo)

    def write_manifest(self, source=None, **extra):
        self.manifest.write_text(json.dumps({'schema_version':1,'sources':[source or self.source],**extra}))

    def test_floating_ref_is_rejected(self):
        source = dict(self.source, revision='main')
        self.write_manifest(source)
        with self.assertRaisesRegex(ValueError,'immutable'):
            blauos.load_manifest(self.manifest)

    def test_source_name_cannot_escape_cache(self):
        self.write_manifest(dict(self.source,name='../outside'))
        with self.assertRaisesRegex(ValueError,'Unsafe'):
            blauos.load_manifest(self.manifest)

    def test_modified_transitive_lock_is_rejected(self):
        lock = self.root / 'flake.lock'
        lock.write_text('original')
        self.write_manifest(transitive_locks=[{'path':'flake.lock','sha256':blauos.digest(lock)}])
        blauos.load_manifest(self.manifest)
        lock.write_text('modified')
        with self.assertRaisesRegex(ValueError,'hash differs'):
            blauos.load_manifest(self.manifest)

    def test_modified_checkout_is_preserved(self):
        header = self.repo / 'input.h'
        header.write_text('user change\n')
        with self.assertRaisesRegex(ValueError,'preserved'):
            blauos.verify_source(self.source,self.repo)
        self.assertEqual(header.read_text(),'user change\n')

    def test_wrong_revision_is_preserved(self):
        (self.repo/'input.h').write_text('new commit\n')
        self.git('commit','-am','new','--quiet')
        actual = self.git('rev-parse','HEAD')
        with self.assertRaisesRegex(ValueError,'existing checkout preserved'):
            blauos.verify_source(self.source,self.repo)
        self.assertEqual(self.git('rev-parse','HEAD'),actual)

    def test_git_environment_cannot_verify_another_worktree(self):
        foreign = self.root / 'sources' / 'fixture'
        foreign.parent.mkdir()
        blauos.run(['git', 'clone', '--quiet', str(self.repo), str(foreign)])
        blauos.run(['git', 'remote', 'set-url', 'origin', self.source['url']], cwd=foreign)
        (foreign / 'input.h').write_text('different actual build input\n')
        self.write_manifest()
        redirect = {'GIT_DIR': str(self.repo / '.git'), 'GIT_WORK_TREE': str(self.repo),
                    'GIT_INDEX_FILE': str(self.repo / '.git/index')}
        with patch.dict(os.environ, redirect):
            with self.assertRaisesRegex(ValueError, 'local changes'):
                blauos.verify_source(self.source, foreign)
            with self.assertRaisesRegex(ValueError, 'local changes'):
                headers.locked_source(self.manifest, 'fixture', foreign.parent)
            # A valid input still verifies while invalid inherited Git config
            # cannot redirect the operation or make its parsing fail.
            with patch.dict(os.environ, {'GIT_CONFIG_COUNT': 'invalid'}):
                self.assertEqual(blauos.verify_source(self.source, self.repo)['revision'],
                                 self.source['revision'])
        self.assertEqual((foreign / 'input.h').read_text(), 'different actual build input\n')
        self.assertEqual((self.repo / 'input.h').read_text(), 'original\n')

    def test_source_directory_must_be_the_actual_worktree(self):
        other = self.root / 'other-worktree'
        other.mkdir()
        self.git('config', 'core.worktree', str(other))
        self.write_manifest(dict(self.source, name='source'))
        with self.assertRaisesRegex(ValueError, 'actual Git worktree'):
            blauos.verify_source(self.source, self.repo)
        with self.assertRaisesRegex(ValueError, 'actual Git worktree'):
            headers.locked_source(self.manifest, 'source', self.root)

    def test_hidden_modified_tracked_files_are_rejected_and_preserved(self):
        self.write_manifest(dict(self.source, name='source'))
        for flag, reset in (('--assume-unchanged', '--no-assume-unchanged'),
                            ('--skip-worktree', '--no-skip-worktree')):
            with self.subTest(flag=flag):
                self.git('update-index', flag, 'input.h')
                (self.repo / 'input.h').write_text('hidden actual source edit\n')
                self.assertEqual(self.git('status', '--porcelain'), '')
                with self.assertRaisesRegex(ValueError, 'assumed-unchanged|skip-worktree'):
                    blauos.verify_source(self.source, self.repo)
                with self.assertRaisesRegex(ValueError, 'assumed-unchanged|skip-worktree'):
                    headers.locked_source(self.manifest, 'source', self.root)
                self.assertEqual((self.repo / 'input.h').read_text(), 'hidden actual source edit\n')
                self.git('update-index', reset, 'input.h')
                (self.repo / 'input.h').write_text('original\n')

    def test_locked_absent_sparse_inputs_pass_but_present_masked_files_fail(self):
        for name in ('selected/keep.h', 'excluded/missing.h'):
            path = self.repo / name
            path.parent.mkdir()
            path.write_text('pinned sparse input\n')
        self.git('add', '.')
        self.git('commit', '--quiet', '-m', 'sparse inputs')
        source = dict(self.source, revision=self.git('rev-parse', 'HEAD'),
                      tree=self.git('rev-parse', 'HEAD^{tree}'), sparse_paths=['selected'])
        self.git('sparse-checkout', 'init', '--cone')
        self.git('sparse-checkout', 'set', 'selected')
        self.write_manifest(dict(source, name='source'))
        self.assertFalse((self.repo / 'excluded/missing.h').exists())
        self.assertEqual(blauos.verify_source(source, self.repo)['revision'], source['revision'])
        self.assertEqual(headers.locked_source(self.manifest, 'source', self.root), self.repo)
        (self.repo / 'excluded').mkdir()
        hidden = self.repo / 'excluded/missing.h'
        hidden.write_text('present file concealed by skip-worktree\n')
        # Git may vivify a newly present sparse file and clear its skip bit;
        # either the index guard or the ordinary content guard must reject it.
        with self.assertRaisesRegex(ValueError, 'skip-worktree|local changes'):
            blauos.verify_source(source, self.repo)
        with self.assertRaisesRegex(ValueError, 'skip-worktree|local changes'):
            headers.locked_source(self.manifest, 'source', self.root)
        self.assertEqual(hidden.read_text(), 'present file concealed by skip-worktree\n')

    def test_deleted_included_sparse_inputs_are_rejected_and_preserved(self):
        included = ('input.h', 'parent/ancestor.h', 'parent/selected/keep.h',
                    'parent/selected/nested/deep.h')
        for name in (*included[1:], 'excluded/missing.h'):
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('pinned sparse input\n')
        self.git('add', '.')
        self.git('commit', '--quiet', '-m', 'nested sparse inputs')
        source = dict(self.source, name='source', revision=self.git('rev-parse', 'HEAD'),
                      tree=self.git('rev-parse', 'HEAD^{tree}'), sparse_paths=['parent/selected'])
        self.git('sparse-checkout', 'init', '--cone')
        self.git('sparse-checkout', 'set', 'parent/selected')
        self.write_manifest(source)
        self.assertFalse((self.repo / 'excluded/missing.h').exists())
        self.assertEqual(headers.locked_source(self.manifest, 'source', self.root), self.repo)
        for name in included:
            with self.subTest(name=name):
                self.git('update-index', '--skip-worktree', name)
                (self.repo / name).unlink()
                self.assertEqual(self.git('status', '--porcelain'), '')
                with self.assertRaisesRegex(ValueError, 'missing included skip-worktree'):
                    blauos.verify_source(source, self.repo)
                with self.assertRaisesRegex(ValueError, 'missing included skip-worktree'):
                    headers.locked_source(self.manifest, 'source', self.root)
                self.assertFalse((self.repo / name).exists())
                self.assertEqual(self.git('ls-files', '-v', '--', name), f'S {name}')
                self.git('update-index', '--no-skip-worktree', name)
                self.git('restore', '--', name)

    def test_locked_sparse_checkout_requires_cone_mode(self):
        self.git('sparse-checkout', 'set', '--no-cone', 'input.h')
        source = dict(self.source, name='source', sparse_paths=['input.h'])
        self.write_manifest(source)
        with self.assertRaisesRegex(ValueError, 'requires cone mode'):
            blauos.verify_source(source, self.repo)
        with self.assertRaisesRegex(ValueError, 'requires cone mode'):
            headers.locked_source(self.manifest, 'source', self.root)

    def test_non_git_commands_keep_their_original_environment(self):
        with patch.dict(os.environ, {'GIT_DIR': 'caller-value'}):
            value = blauos.run([sys.executable, '-c',
                               "import os; print(os.environ['GIT_DIR'])"])
        self.assertEqual(value, 'caller-value')

    def test_header_build_executes_despite_inherited_make_dry_run(self):
        build = self.root / 'header-build'
        build.mkdir()
        (build / 'Makefile').write_text('installhdrs:\n\t@touch actually-generated.h\n')
        with patch.dict(os.environ, {'MAKEFLAGS': '-n', 'MFLAGS': '-n',
                                     'CMAKE_TOOLCHAIN_FILE': '/unreviewed/toolchain.cmake',
                                     'SDKROOT': '/unreviewed/sdk', 'CPATH': '/unreviewed/headers'}):
            headers.run(['make', 'installhdrs'], build / 'run.log', build)
            environment = headers.clean_header_environment()
        self.assertTrue((build / 'actually-generated.h').is_file())
        self.assertFalse(set(environment) & {'MAKEFLAGS', 'MFLAGS', 'CMAKE_TOOLCHAIN_FILE',
                                             'SDKROOT', 'CPATH'})

    def test_header_cli_rejects_stale_outputs_before_source_or_tool_discovery(self):
        build = self.root / 'stale-header-build'
        build.mkdir()
        old_header = build / 'generated.h'
        old_header.write_text('previous generated header\n')
        cache = build / 'CMakeCache.txt'
        cache.write_text('previous build cache\n')
        evidence = self.root / 'not-created-evidence'
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/build/headers.py'),
                                 '--build-root', str(build), '--evidence', str(evidence),
                                 '--manifest', str(self.root / 'missing-source-manifest.json')],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('fresh build root; existing contents preserved', result.stderr)
        self.assertEqual(old_header.read_text(), 'previous generated header\n')
        self.assertEqual(cache.read_text(), 'previous build cache\n')
        self.assertFalse(evidence.exists())

    def test_sdk_hash_tracks_headers_and_symlink_identity(self):
        sdk = self.root/'sdk'; sdk.mkdir()
        header = sdk/'header.h'; header.write_text('first')
        alias = sdk/'alias.h'; alias.symlink_to('header.h')
        original = blauos.fingerprint_sdk(sdk)['content_sha256']
        header.write_text('second')
        changed = blauos.fingerprint_sdk(sdk)['content_sha256']
        self.assertNotEqual(original,changed)
        alias.unlink(); alias.symlink_to('../outside.h')
        self.assertNotEqual(changed,blauos.fingerprint_sdk(sdk)['content_sha256'])

    def test_patch_series_rejects_unlisted_changes(self):
        directory = self.root/'patches'; directory.mkdir()
        (self.repo/'input.h').write_text('approved\n')
        patch = subprocess.check_output(['git','diff','--binary'],cwd=self.repo)
        (directory/'0001-approved.patch').write_bytes(patch)
        (directory/'series').write_text('0001-approved.patch\n')
        self.git('restore','input.h')
        self.write_manifest()
        worktree = self.root/'worktree'
        patches.prepare(self.repo,self.source['revision'],worktree,patches.series(directory))
        base = [sys.executable,str(ROOT/'scripts/patches.py'),'verify','--manifest',str(self.manifest),
                '--source',str(self.repo),'--source-name','fixture','--series-dir',str(directory),'--worktree',str(worktree)]
        self.assertEqual(subprocess.run(base,capture_output=True).returncode,0)
        (worktree/'input.h').write_text('unreviewed\n')
        failed = subprocess.run(base,capture_output=True,text=True)
        self.assertNotEqual(failed.returncode,0)
        self.assertIn('outside the ordered patch series',failed.stderr)

    def test_patch_path_cannot_escape_series(self):
        directory=self.root/'patches'; directory.mkdir()
        (self.root/'outside.patch').write_text('not a patch')
        (directory/'series').write_text('../outside.patch\n')
        with self.assertRaisesRegex(ValueError,'Invalid patch entry'):
            patches.series(directory)

    def test_git_environment_cannot_conceal_unreviewed_patch_worktree(self):
        directory = self.root / 'patches'
        directory.mkdir()
        header = self.repo / 'input.h'
        header.write_text('approved patch input\n')
        (directory / '0001.patch').write_bytes(patches.git_diff(self.repo))
        (directory / 'series').write_text('0001.patch\n')
        self.git('restore', 'input.h')
        self.write_manifest()
        worktree = patches.prepare(self.repo, self.source['revision'],
                                   self.root / 'worktree', patches.series(directory))
        (worktree / 'input.h').write_text('unreviewed real worktree input\n')
        command = [sys.executable, str(ROOT / 'scripts/patches.py'), 'verify',
                   '--manifest', str(self.manifest), '--source', str(self.repo),
                   '--source-name', 'fixture', '--series-dir', str(directory),
                   '--worktree', str(worktree)]
        environment = dict(os.environ, GIT_DIR=str(self.repo / '.git'),
                           GIT_WORK_TREE=str(self.repo), GIT_INDEX_FILE=str(self.repo / '.git/index'))
        result = subprocess.run(command, env=environment, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('outside the ordered patch series', result.stderr)
        self.assertEqual((worktree / 'input.h').read_text(), 'unreviewed real worktree input\n')
        self.assertEqual(header.read_text(), 'original\n')

    def test_patch_prepare_cli_rejects_modified_locked_source(self):
        directory = self.root / 'patches'
        directory.mkdir()
        (directory / 'series').write_text('')
        self.write_manifest()
        (self.repo / 'input.h').write_text('modified pristine source\n')
        worktree = self.root / 'worktree'
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/patches.py'), 'apply',
                                 '--manifest', str(self.manifest), '--source', str(self.repo),
                                 '--source-name', 'fixture', '--series-dir', str(directory),
                                 '--worktree', str(worktree)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('source has local changes', result.stderr)
        self.assertFalse(worktree.exists())
        self.assertEqual((self.repo / 'input.h').read_text(), 'modified pristine source\n')

    def test_archive_requires_an_immutable_hash(self):
        self.write_manifest(archives=[{'name':'icu','filename':'icu.tgz','url':'https://github.com/fixture/source/archive.tar.gz',
                                      'sha256':'latest','license':'MIT','role':'fixture'}])
        with self.assertRaisesRegex(ValueError,'immutable archive'):
            blauos.load_manifest(self.manifest)

    def test_mismatched_archive_is_preserved(self):
        directory = self.root/'inputs'; directory.mkdir()
        archive = directory/'icu.tgz'; archive.write_bytes(b'user-owned input')
        self.write_manifest(archives=[{'name':'icu','filename':'icu.tgz','url':'https://github.com/fixture/source/archive.tar.gz',
                                      'sha256':'a'*64,'license':'MIT','role':'fixture'}])
        args = SimpleNamespace(manifest=self.manifest,names=['icu'],inputs_dir=directory,report=self.root/'report.json')
        with self.assertRaisesRegex(ValueError,'preserved'):
            blauos.fetch_archives(args)
        self.assertEqual(archive.read_bytes(),b'user-owned input')

if __name__ == '__main__':
    unittest.main()
