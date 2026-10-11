"""Release manifest (#65) tests; software-level only, no network or hardware."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts' / 'release'))
sys.path.insert(0, str(ROOT / 'scripts'))
import manifest
import release


class ManifestGenerationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.work = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def generate(self, artifacts=None, release_id='test-release'):
        return manifest.generate(release_id, ROOT / 'sources.lock.json',
                                 ROOT / 'toolchain.lock.json', artifacts)

    def test_generate_binds_repo_locks_patches_and_sbom(self):
        result = self.generate()
        self.assertEqual(result['schema_version'], 1)
        self.assertEqual(result['manifest_kind'], 'blauos-release')
        self.assertEqual(result['source_pins']['lock_sha256'],
                         release.file_sha256(ROOT / 'sources.lock.json'))
        self.assertEqual(result['toolchain']['lock_sha256'],
                         release.file_sha256(ROOT / 'toolchain.lock.json'))
        lock = json.loads((ROOT / 'sources.lock.json').read_text())
        expected_components = (len(lock['sources']) + len(lock['archives'])
                               + len(lock['transitive_locks']))
        self.assertEqual(len(result['sbom']['components']), expected_components)
        names = {component['name'] for component in result['sbom']['components']}
        self.assertIn('xnu', names)
        self.assertIn('icu4c-76.1', names)
        licenses = {component.get('license') for component in result['sbom']['components']}
        self.assertTrue(any('APSL' in str(value) for value in licenses))
        patch_paths = {entry['path'] for entry in result['patches']}
        self.assertTrue(any(path.startswith('patches/native/') for path in patch_paths))
        self.assertTrue(any(path.startswith('vm/patches/') for path in patch_paths))
        self.assertEqual(result['unavailable_inputs'] if 'unavailable_inputs' in result
                         else result['source_pins']['unavailable_inputs'],
                         lock['unavailable_inputs'])

    def test_compatibility_metadata_and_physical_honesty(self):
        result = self.generate()
        machines = {entry['model']: entry for entry in result['compatibility']['machines']}
        self.assertEqual(machines['Macmini9,1']['soc'], 't8103')
        self.assertIn('hardware-gated', machines['Macmini9,1']['status'])
        self.assertIn('NOT RUN', result['physical_acceptance']['gate_2_status'])
        self.assertIn('image_format', result['compatibility'])
        self.assertIn('data_migration_constraints', result['compatibility'])
        self.assertTrue(result['third_party_verification'])

    def test_content_digest_is_self_consistent(self):
        result = self.generate()
        regenerated = dict(result)
        digest = regenerated.pop('manifest_content_sha256')
        self.assertEqual(release.sha256(release.canonical(regenerated)), digest)

    def test_release_id_rejects_path_components(self):
        with self.assertRaises(release.ReleaseError):
            self.generate(release_id='../escape')

    def test_verify_roundtrip_matches_repo_inputs(self):
        result = self.generate()
        path = self.work / 'manifest.json'
        release.write_json(path, result)
        verification = manifest.verify(path, ROOT / 'sources.lock.json',
                                       ROOT / 'toolchain.lock.json', None)
        self.assertTrue(verification['verified'],
                        [c for c in verification['checks'] if not c.get('match')])

    def test_verify_detects_modified_source_lock(self):
        result = self.generate()
        path = self.work / 'manifest.json'
        release.write_json(path, result)
        lock = json.loads((ROOT / 'sources.lock.json').read_text())
        lock['sources'][0]['role'] = 'altered after release'
        modified = self.work / 'sources.lock.json'
        modified.write_text(json.dumps(lock, indent=2))
        verification = manifest.verify(path, modified, ROOT / 'toolchain.lock.json', None)
        self.assertFalse(verification['verified'])
        mismatches = {c['name'] for c in verification['checks'] if not c.get('match')}
        self.assertIn('sources_lock_sha256', mismatches)

    def test_artifacts_are_hashed_and_corruption_is_detected(self):
        artifacts = self.work / 'artifacts'
        artifacts.mkdir()
        (artifacts / 'kernel.development.t8103').write_bytes(b'fixture-kernel-bytes')
        (artifacts / 'disk.img').write_bytes(b'fixture-disk-bytes' * 64)
        result = self.generate(artifacts=artifacts)
        self.assertEqual(len(result['artifacts']), 2)
        path = self.work / 'manifest.json'
        release.write_json(path, result)
        verification = manifest.verify(path, ROOT / 'sources.lock.json',
                                       ROOT / 'toolchain.lock.json', artifacts)
        self.assertTrue(verification['verified'])
        (artifacts / 'disk.img').write_bytes(b'corrupted-disk-bytes')
        verification = manifest.verify(path, ROOT / 'sources.lock.json',
                                       ROOT / 'toolchain.lock.json', artifacts)
        self.assertFalse(verification['verified'])
        mismatches = {c['name'] for c in verification['checks'] if not c.get('match')}
        self.assertIn('artifact:disk.img', mismatches)

    def test_verify_requires_artifact_dir_when_manifest_has_artifacts(self):
        artifacts = self.work / 'artifacts'
        artifacts.mkdir()
        (artifacts / 'kernel').write_bytes(b'fixture')
        result = self.generate(artifacts=artifacts)
        path = self.work / 'manifest.json'
        release.write_json(path, result)
        with self.assertRaisesRegex(release.ReleaseError, '--artifacts'):
            manifest.verify(path, ROOT / 'sources.lock.json', ROOT / 'toolchain.lock.json', None)


class ManifestCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.work = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def run_tool(self, *args):
        return subprocess.run(
            [sys.executable, str(ROOT / 'scripts' / 'release' / 'manifest.py'), *args],
            capture_output=True, text=True)

    def test_generate_and_verify_cli_with_receipts(self):
        output = self.work / 'release.json'
        receipt_path = self.work / 'generate-receipt.json'
        result = self.run_tool('generate', '--release-id', 'cli-test',
                               '--output', str(output), '--receipt', str(receipt_path))
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(receipt['argv'][1], 'generate')
        self.assertIn('source_lock_sha256', receipt['inputs'])
        self.assertEqual(receipt['result']['status'], 'generated')
        self.assertIn('NOT RUN', receipt['result']['physical_acceptance'])
        verify_receipt = self.work / 'verify-receipt.json'
        result = self.run_tool('verify', str(output), '--receipt', str(verify_receipt))
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertTrue(summary['verified'])
        self.assertEqual(summary['mismatches'], [])

    def test_cli_verify_exits_nonzero_on_mismatch(self):
        output = self.work / 'release.json'
        result = self.run_tool('generate', '--release-id', 'cli-test', '--output', str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        lock = json.loads((ROOT / 'toolchain.lock.json').read_text())
        lock['profile'] = 'altered'
        modified = self.work / 'toolchain.lock.json'
        modified.write_text(json.dumps(lock))
        result = self.run_tool('verify', str(output), '--toolchain-lock', str(modified))
        self.assertEqual(result.returncode, 1)
        self.assertFalse(json.loads(result.stdout)['verified'])


if __name__ == '__main__':
    unittest.main()
