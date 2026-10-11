"""Release download (#79) tests with a local HTTP fixture server.

Covers truncation, corruption, full-disk simulation, offline hosts, resume
from recorded offsets, restart when a server ignores Range, cache eviction
under a disk bound, and rejection of tampered release metadata.
"""
import errno
import hashlib
import http.server
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts' / 'release'))
sys.path.insert(0, str(ROOT / 'scripts'))
import download
import release
import signing


def make_handler(directory, state):
    root = directory.resolve()

    class FixtureHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            name = self.path.lstrip('/')
            target = (root / name).resolve()
            if not str(target).startswith(str(root)) or not target.is_file():
                self.send_error(404)
                return
            data = state.get('corrupt', {}).get(name, target.read_bytes())
            truncate = state.get('truncate', {}).get(name)
            if truncate is not None:
                # Advertise the full length, deliver a prefix, then hang up.
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data[:truncate])
                self.close_connection = True
                return
            range_header = self.headers.get('Range')
            if range_header and not state.get('ignore_range'):
                state.setdefault('ranges', []).append((name, range_header))
                start = int(range_header.removeprefix('bytes=').split('-')[0])
                if start >= len(data):
                    self.send_error(416)
                    return
                self.send_response(206)
                self.send_header('Content-Range', f'bytes {start}-{len(data) - 1}/{len(data)}')
                self.send_header('Content-Length', str(len(data) - start))
                self.end_headers()
                self.wfile.write(data[start:])
                return
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return FixtureHandler


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.work = Path(self.temporary.name)
        self.serve_dir = self.work / 'served'
        self.serve_dir.mkdir()
        self.state = {}
        self.handler = make_handler(self.serve_dir, self.state)
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), self.handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f'http://127.0.0.1:{self.server.server_port}'
        self.key = signing.generate_key()
        key_path = self.work / 'key.json'
        release.write_json(key_path, self.key)
        self.key_path = key_path
        self.trust_root = signing.empty_trust_root()
        signing.add_key_to_root(self.trust_root, {'key_id': self.key['key_id'],
                                                  'public': bytes.fromhex(self.key['public_key'])})
        self.trust_path = self.work / 'trust-root.json'
        release.write_json(self.trust_path, self.trust_root)
        self.cache = download.Cache(self.work / 'cache', 1024 * 1024)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temporary.cleanup()

    def publish(self, artifacts: dict, release_id='fixture-release', manifest_name='release.json'):
        for name, data in artifacts.items():
            target = self.serve_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        entries = [{'path': name, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                   for name, data in sorted(artifacts.items())]
        manifest = {'schema_version': 1, 'manifest_kind': 'blauos-release',
                    'release_id': release_id, 'artifacts': entries}
        manifest_path = self.serve_dir / manifest_name
        release.write_json(manifest_path, manifest)
        signature = signing.sign_manifest(self.key_path, manifest_path, self.trust_path)
        release.write_json(self.serve_dir / (manifest_name + '.sig.json'), signature)
        return manifest

    def fetch(self, manifest_name='release.json', retries=0, cache=None):
        return download.fetch_release(self.base_url, cache or self.cache, self.trust_path,
                                      manifest_name, timeout=5, retries=retries, backoff=0)

    def test_happy_path_fetches_and_verifies(self):
        self.publish({'kernel.img': b'kernel-bytes' * 128, 'disk.img': b'disk' * 512})
        result = self.fetch()
        self.assertTrue(result['signature_verified'])
        self.assertEqual(result['release_id'], 'fixture-release')
        self.assertFalse(result['boot_authorization_performed'])
        statuses = {entry['path']: entry['status'] for entry in result['artifacts']}
        self.assertEqual(statuses, {'disk.img': 'fetched', 'kernel.img': 'fetched'})
        for name in statuses:
            self.assertTrue((self.cache.artifacts / name).is_file())
            self.assertFalse(list(self.cache.partial.glob('*')))
        # A second fetch must be served entirely from the verified cache.
        result = self.fetch()
        self.assertEqual({entry['status'] for entry in result['artifacts']}, {'cached'})
        self.assertEqual(sum(entry['bytes_fetched'] for entry in result['artifacts']), 0)

    def test_tampered_manifest_is_rejected(self):
        self.publish({'a.bin': b'aaa'})
        manifest_path = self.serve_dir / 'release.json'
        manifest_path.write_text(manifest_path.read_text().replace('fixture-release', 'forged-release'))
        with self.assertRaisesRegex(release.ReleaseError, 'tampered'):
            self.fetch()
        self.assertFalse(list(self.cache.artifacts.glob('*')))

    def test_offline_host_raises_offline_error(self):
        probe = socket.socket()
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
        probe.close()
        with self.assertRaises(download.OfflineError):
            download.fetch_release(f'http://127.0.0.1:{port}', self.cache, self.trust_path,
                                   'release.json', timeout=1, retries=0, backoff=0)

    def test_truncated_download_is_retained_and_resumed(self):
        payload = bytes(range(256)) * 16  # 4096 bytes
        self.publish({'big.bin': payload})
        self.state['truncate'] = {'big.bin': 1024}
        with self.assertRaises(download.TruncatedError):
            self.fetch()
        part = self.cache.partial / 'big.bin.part'
        # Incomplete bytes are distinguishable from verified artifacts.
        self.assertTrue(part.is_file())
        self.assertFalse((self.cache.artifacts / 'big.bin').exists())
        self.assertEqual(part.stat().st_size, 1024)
        self.state['truncate'] = {}
        result = self.fetch()
        entry = result['artifacts'][0]
        self.assertEqual(entry['status'], 'resumed')
        self.assertEqual(entry['resumed_from'], 1024)
        self.assertEqual(entry['bytes_fetched'], len(payload) - 1024)
        self.assertIn(('big.bin', 'bytes=1024-'), self.state['ranges'])
        self.assertEqual((self.cache.artifacts / 'big.bin').read_bytes(), payload)
        self.assertFalse(part.exists())

    def test_server_without_range_support_restarts_safely(self):
        payload = b'x' * 2048
        self.publish({'big.bin': payload})
        self.state['truncate'] = {'big.bin': 512}
        with self.assertRaises(download.TruncatedError):
            self.fetch()
        self.state['truncate'] = {}
        self.state['ignore_range'] = True
        result = self.fetch()
        entry = result['artifacts'][0]
        self.assertEqual(entry['status'], 'fetched')
        self.assertEqual((self.cache.artifacts / 'big.bin').read_bytes(), payload)

    def test_completed_partial_is_promoted_without_network(self):
        payload = b'z' * 512
        self.publish({'big.bin': payload})
        part = self.cache.partial / 'big.bin.part'
        part.write_bytes(payload)
        release.write_json(self.cache.partial / 'big.bin.state.json',
                           {'url': f'{self.base_url}/big.bin',
                            'sha256': hashlib.sha256(payload).hexdigest(),
                            'received': len(payload), 'updated_at': 'fixture'})
        result = self.fetch()
        self.assertEqual(result['artifacts'][0]['status'], 'completed-from-partial')
        self.assertEqual((self.cache.artifacts / 'big.bin').read_bytes(), payload)

    def test_corrupted_bytes_are_discarded(self):
        self.publish({'a.bin': b'authentic'})
        self.state['corrupt'] = {'a.bin': b'forged!!!'}  # same length; only bytes differ
        with self.assertRaisesRegex(download.IntegrityError, 'SHA-256 mismatch'):
            self.fetch()
        self.assertFalse(list(self.cache.artifacts.glob('*')))
        self.assertFalse(list(self.cache.partial.glob('*')))

    def test_full_disk_during_write_is_reported(self):
        self.publish({'a.bin': b'data' * 256})
        real_open = Path.open

        class NoSpaceFile:
            def __init__(self, wrapped):
                self.wrapped = wrapped
            def write(self, chunk):
                raise OSError(errno.ENOSPC, 'No space left on device')
            def __getattr__(self, name):
                return getattr(self.wrapped, name)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.wrapped.close()

        def fake_open(path, mode='r', *args, **kwargs):
            handle = real_open(path, mode, *args, **kwargs)
            if str(path).endswith('.part') and 'b' in mode and ('w' in mode or 'a' in mode):
                return NoSpaceFile(handle)
            return handle

        with mock.patch.object(Path, 'open', fake_open):
            with self.assertRaisesRegex(download.DiskBoundError, 'disk full'):
                self.fetch()
        self.assertFalse((self.cache.artifacts / 'a.bin').exists())

    def test_cache_bound_evicts_least_recently_used(self):
        small = download.Cache(self.work / 'small-cache', 3072)
        self.publish({'a.bin': b'a' * 2000}, release_id='release-a')
        download.fetch_release(self.base_url, small, self.trust_path, 'release.json',
                               timeout=5, retries=0, backoff=0)
        self.assertTrue((small.artifacts / 'a.bin').is_file())
        self.publish({'b.bin': b'b' * 2000}, release_id='release-b')
        download.fetch_release(self.base_url, small, self.trust_path, 'release.json',
                               timeout=5, retries=0, backoff=0)
        self.assertFalse((small.artifacts / 'a.bin').exists())
        self.assertTrue((small.artifacts / 'b.bin').is_file())
        self.assertNotIn('a.bin', small.index['entries'])

    def test_cache_bound_reports_when_nothing_is_evictable(self):
        tiny = download.Cache(self.work / 'tiny-cache', 128)
        self.publish({'a.bin': b'a' * 2000})
        with self.assertRaisesRegex(download.DiskBoundError, 'cannot fit'):
            download.fetch_release(self.base_url, tiny, self.trust_path, 'release.json',
                                   timeout=5, retries=0, backoff=0)

    def test_cached_file_with_altered_bytes_is_refetched(self):
        self.publish({'a.bin': b'authentic'})
        self.fetch()
        cached = self.cache.artifacts / 'a.bin'
        cached.write_bytes(b'modified on disk')
        result = self.fetch()
        self.assertEqual(result['artifacts'][0]['status'], 'fetched')
        self.assertEqual(cached.read_bytes(), b'authentic')

    def test_manifest_signature_from_untrusted_key_is_rejected(self):
        self.publish({'a.bin': b'aaa'})
        stranger_root = signing.empty_trust_root()
        release.write_json(self.work / 'stranger-root.json', stranger_root)
        with self.assertRaisesRegex(release.ReleaseError, 'unknown key_id'):
            download.fetch_release(self.base_url, self.cache, self.work / 'stranger-root.json',
                                   'release.json', timeout=5, retries=0, backoff=0)

    def test_cli_fetch_with_receipt(self):
        self.publish({'a.bin': b'cli-bytes'})
        cache_dir = self.work / 'cli-cache'
        receipt_path = self.work / 'fetch-receipt.json'
        result = subprocess.run(
            [sys.executable, str(ROOT / 'scripts' / 'release' / 'download.py'), 'fetch',
             '--base-url', self.base_url, '--manifest-name', 'release.json',
             '--trust-root', str(self.trust_path), '--cache', str(cache_dir),
             '--max-cache-bytes', '1048576', '--timeout', '5', '--retries', '0',
             '--receipt', str(receipt_path)],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertTrue(summary['signature_verified'])
        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(receipt['argv'][1], 'fetch')
        self.assertIn('trust_root_sha256', receipt['inputs'])
        self.assertTrue((cache_dir / 'artifacts' / 'a.bin').is_file())


if __name__ == '__main__':
    unittest.main()
