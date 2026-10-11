"""Release signing (#78) tests: RFC 8032 vectors, tamper/old-key rejection."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts' / 'release'))
sys.path.insert(0, str(ROOT / 'scripts'))
import release
import signing

RFC8032_TEST_1 = {
    'secret': '9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60',
    'public': 'd75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a',
    'message': '',
    'signature': 'e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155'
                 '5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b',
}
RFC8032_TEST_2 = {
    'secret': '4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb',
    'public': '3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c',
    'message': '72',
    'signature': '92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da'
                 '085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00',
}


class Ed25519PrimitiveTests(unittest.TestCase):
    def test_rfc8032_vectors(self):
        for vector in (RFC8032_TEST_1, RFC8032_TEST_2):
            secret = bytes.fromhex(vector['secret'])
            message = bytes.fromhex(vector['message'])
            public = signing.ed25519_public_key(secret)
            self.assertEqual(public.hex(), vector['public'])
            signature = signing.ed25519_sign(secret, message)
            self.assertEqual(signature.hex(), vector['signature'])
            self.assertTrue(signing.ed25519_verify(public, message, signature))

    def test_wrong_message_or_wrong_key_fails(self):
        secret = bytes.fromhex(RFC8032_TEST_1['secret'])
        public = bytes.fromhex(RFC8032_TEST_1['public'])
        signature = signing.ed25519_sign(secret, b'release')
        self.assertFalse(signing.ed25519_verify(public, b'altered', signature))
        other = signing.ed25519_public_key(b'\x01' * 32)
        self.assertFalse(signing.ed25519_verify(other, b'release', signature))
        self.assertFalse(signing.ed25519_verify(public, b'release', signature[:-1] + b'\x00'))


class SigningPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.work = Path(self.temporary.name)
        self.key = signing.generate_key()
        self.key_path = self.work / 'key.json'
        release.write_json(self.key_path, self.key)
        self.root = signing.empty_trust_root()
        self.trust_path = self.work / 'trust-root.json'
        self.manifest = self.work / 'manifest.json'
        release.write_json(self.manifest, {'schema_version': 1, 'manifest_kind': 'blauos-release',
                                           'release_id': 'fixture'})

    def tearDown(self):
        self.temporary.cleanup()

    def add_key(self, key=None, **fields):
        key = key or self.key
        entry = signing.add_key_to_root(
            self.root, {'key_id': key['key_id'], 'public': bytes.fromhex(key['public_key'])})
        entry.update(fields)
        release.write_json(self.trust_path, self.root)
        return entry

    def sign(self, key_path=None, with_root=True):
        return signing.sign_manifest(key_path or self.key_path, self.manifest,
                                     self.trust_path if with_root else None)

    def verify(self, signature, **kwargs):
        signature_path = self.work / 'manifest.sig.json'
        release.write_json(signature_path, signature)
        return signing.verify_manifest(self.trust_path, self.manifest, signature_path, **kwargs)

    def test_sign_verify_roundtrip(self):
        self.add_key()
        signature = self.sign()
        result = self.verify(signature)
        self.assertTrue(result['verified'])
        self.assertEqual(result['key_id'], self.key['key_id'])
        self.assertFalse(result['boot_authorization_performed'])
        self.assertTrue(signature['distinct_from_boot_authorization'])

    def test_tampered_manifest_is_rejected(self):
        self.add_key()
        signature = self.sign()
        self.manifest.write_text(self.manifest.read_text() + ' ')
        with self.assertRaisesRegex(release.ReleaseError, 'tampered'):
            self.verify(signature)

    def test_forged_signature_is_rejected(self):
        self.add_key()
        signature = dict(self.sign())
        signature['signature'] = '00' * 64
        with self.assertRaisesRegex(release.ReleaseError, 'Ed25519 verification failed'):
            self.verify(signature)

    def test_unknown_key_is_rejected(self):
        self.add_key()  # trust root holds only self.key
        stranger = signing.generate_key()
        stranger_path = self.work / 'stranger.json'
        release.write_json(stranger_path, stranger)
        signature = signing.sign_manifest(stranger_path, self.manifest, None)
        with self.assertRaisesRegex(release.ReleaseError, 'unknown key_id'):
            self.verify(signature)

    def test_revoked_key_is_rejected_for_verify_and_sign(self):
        self.add_key()
        signature = self.sign()
        signing.revoke_key(self.root, self.key['key_id'], 'compromise drill')
        release.write_json(self.trust_path, self.root)
        with self.assertRaisesRegex(release.ReleaseError, 'revoked'):
            self.verify(signature)
        with self.assertRaisesRegex(release.ReleaseError, 'only active keys may sign'):
            self.sign()

    def test_rotation_retires_old_key_and_old_signatures_still_verify(self):
        self.add_key()
        old_signature = self.sign()
        successor = signing.generate_key()
        signing.rotate_key(self.root, self.key['key_id'],
                           {'key_id': successor['key_id'],
                            'public': bytes.fromhex(successor['public_key'])})
        release.write_json(self.trust_path, self.root)
        # Old signatures remain verifiable: signed_at fell inside the key's window.
        result = self.verify(old_signature)
        self.assertEqual(result['key_status'], 'retired')
        # But the retired key must not produce new signatures.
        with self.assertRaisesRegex(release.ReleaseError, 'only active keys may sign'):
            self.sign()
        successor_path = self.work / 'successor.json'
        release.write_json(successor_path, successor)
        new_signature = self.sign(key_path=successor_path)
        self.assertEqual(self.verify(new_signature)['key_id'], successor['key_id'])
        old_entry = signing.find_key(self.root, self.key['key_id'])
        self.assertEqual(old_entry['superseded_by'], successor['key_id'])

    def test_expired_key_window_is_rejected(self):
        self.add_key(not_after='2020-01-01T00:00:00+00:00')
        # Signing is allowed only for active keys; the window gates verification.
        signature = signing.sign_manifest(self.key_path, self.manifest, None)
        with self.assertRaisesRegex(release.ReleaseError, 'validity window'):
            self.verify(signature)

    def test_not_yet_valid_key_is_rejected(self):
        self.add_key(not_before='2999-01-01T00:00:00+00:00')
        signature = signing.sign_manifest(self.key_path, self.manifest, None)
        with self.assertRaisesRegex(release.ReleaseError, 'validity window'):
            self.verify(signature)

    def test_signature_outside_key_window_is_rejected(self):
        entry = self.add_key()
        signature = self.sign()
        entry['not_before'] = '2999-01-01T00:00:00+00:00'  # signed_at now precedes the window
        release.write_json(self.trust_path, self.root)
        with self.assertRaisesRegex(release.ReleaseError, 'signed_at outside'):
            self.verify(signature)

    def test_trust_root_rejects_duplicates_and_bad_ids(self):
        self.add_key()
        duplicate = json.loads(json.dumps(self.root))
        duplicate['keys'].append(dict(duplicate['keys'][0]))
        release.write_json(self.trust_path, duplicate)
        with self.assertRaisesRegex(release.ReleaseError, 'duplicate key_id'):
            signing.load_trust_root(self.trust_path)
        broken = json.loads(json.dumps(self.root))
        broken['keys'][0]['key_id'] = 'f' * 32
        release.write_json(self.trust_path, broken)
        with self.assertRaisesRegex(release.ReleaseError, 'key_id does not match'):
            signing.load_trust_root(self.trust_path)

    def test_private_key_file_rejects_mismatched_public(self):
        broken = dict(self.key, public_key='ab' * 32)
        release.write_json(self.work / 'broken.json', broken)
        with self.assertRaisesRegex(release.ReleaseError, 'secret/public mismatch'):
            signing.load_private_key(self.work / 'broken.json')


class SigningCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.work = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def run_tool(self, *args):
        return subprocess.run(
            [sys.executable, str(ROOT / 'scripts' / 'release' / 'signing.py'), *args],
            capture_output=True, text=True)

    def test_keygen_sign_rotate_revoke_verify_flow(self):
        trust = self.work / 'trust.json'
        key = self.work / 'key.json'
        result = self.run_tool('keygen', '--output', str(key), '--trust-root', str(trust))
        self.assertEqual(result.returncode, 0, result.stderr)
        key_id = json.loads(result.stdout)['key_id']
        self.assertEqual(os.stat(key).st_mode & 0o777, 0o600)
        manifest_path = self.work / 'release.json'
        manifest_path.write_text(json.dumps({'schema_version': 1, 'manifest_kind': 'blauos-release'}))
        signature = self.work / 'release.sig.json'
        sign_receipt = self.work / 'sign-receipt.json'
        result = self.run_tool('sign', '--key', str(key), '--manifest', str(manifest_path),
                               '--trust-root', str(trust), '--output', str(signature),
                               '--receipt', str(sign_receipt))
        self.assertEqual(result.returncode, 0, result.stderr)
        recorded = json.loads(sign_receipt.read_text())
        self.assertTrue(recorded['result']['offline'])
        self.assertFalse(recorded['result']['boot_authorization_performed'])
        self.assertIn('manifest_sha256', recorded['inputs'])
        verify_receipt = self.work / 'verify-receipt.json'
        result = self.run_tool('verify', '--trust-root', str(trust), '--manifest', str(manifest_path),
                               '--signature', str(signature), '--receipt', str(verify_receipt))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['verified'])
        successor = self.work / 'successor.json'
        result = self.run_tool('keygen', '--output', str(successor))
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_tool('rotate', '--trust-root', str(trust),
                               '--old-key-id', key_id, '--new-key', str(successor))
        self.assertEqual(result.returncode, 0, result.stderr)
        successor_id = json.loads(result.stdout)['active']
        result = self.run_tool('revoke', '--trust-root', str(trust),
                               '--key-id', successor_id, '--reason', 'rotation drill')
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_tool('verify', '--trust-root', str(trust),
                               '--manifest', str(manifest_path), '--signature', str(signature))
        self.assertEqual(result.returncode, 0, result.stderr)  # retired, not revoked
        result = self.run_tool('sign', '--key', str(successor), '--manifest', str(manifest_path),
                               '--trust-root', str(trust), '--output', str(self.work / 'x.sig.json'))
        self.assertEqual(result.returncode, 1)
        self.assertIn('revoked', result.stderr)


if __name__ == '__main__':
    unittest.main()
