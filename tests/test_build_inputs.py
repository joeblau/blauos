"""Input-integrity and preservation tests; none require network access."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import blauos
import patches

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

if __name__ == '__main__':
    unittest.main()
