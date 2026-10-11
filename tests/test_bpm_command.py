"""Guest bpm tool tests: the C source is compiled for the host and run
end-to-end against synthetic signed packages.

The guest binary itself is built by scripts/vm/build_bpm_commands.py against
the independent runtime; these tests compile the same source with the host
compiler and exercise install/list/verify/remove/repo/recv against scratch
roots, plus every fail-closed rejection path. Live in-guest acceptance is
recorded separately under evidence/virtualization/package-manager.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "release"))
sys.path.insert(0, str(ROOT / "scripts"))
import release
import signing

SPEC = importlib.util.spec_from_file_location("release_package", ROOT / "scripts/release/package.py")
package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package)

SOURCE = ROOT / "vm/commands/bpm/bpm.c"


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("clang"),
                     "bpm command tests compile the C source with host clang")
class BpmCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binary_dir = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.binary_dir.name) / "bpm"
        subprocess.run(["clang", "-std=gnu17", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-o", str(cls.binary), str(SOURCE)], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.binary_dir.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.previous_cwd = os.getcwd()
        os.chdir(self.work)
        self.addCleanup(os.chdir, self.previous_cwd)
        self.key = signing.generate_key()
        self.key_path = self.work / "key.json"
        release.write_json(self.key_path, self.key)
        self.trust_root = signing.empty_trust_root()
        signing.add_key_to_root(self.trust_root, {"key_id": self.key["key_id"],
                                                  "public": bytes.fromhex(self.key["public_key"])})
        self.trust_path = self.work / "trust-root.json"
        release.write_json(self.trust_path, self.trust_root)
        self.root = self.work / "root"
        self.root.mkdir()
        self.db = self.work / "var/db/bpm/db"
        self.repo = self.work / "repo"
        self.repo.mkdir()
        self.built = {}

    def bpm(self, *args, stdin=None):
        return subprocess.run([str(self.binary), "--root", str(self.root), "--db", str(self.db),
                               "--trust-root", str(self.trust_path), "--repo", str(self.repo),
                               *args], capture_output=True, text=True, input=stdin)

    def make_layer(self, name, payload=b"synthetic payload\n", extra=()):
        layer = self.work / ("layer-" + name)
        binary = layer / "usr/bin" / name
        binary.parent.mkdir(parents=True)
        binary.write_bytes(payload)
        binary.chmod(0o755)
        for relative in extra:
            path = layer / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name + " " + relative + "\n")
        return layer

    def make_package(self, name, depends=("runtime",), payload=b"synthetic payload\n", extra=()):
        recipes = self.work / "packages"
        recipes.mkdir(exist_ok=True)
        recipe = Path("packages") / (name + ".json")  # relative: recipe binding path
        release.write_json(recipe, {"schema_version": 1, "name": name, "depends": list(depends),
                                    "outputs": ["usr/bin/" + name], "summary": "fixture"})
        layer = self.make_layer(name, payload, extra)
        manifest = package.build_manifest(layer, recipe, name, "1.0")
        manifest_bytes = (json.dumps(manifest, indent=2) + "\n").encode()
        manifest_path = self.work / (name + ".manifest.json")
        manifest_path.write_bytes(manifest_bytes)
        signature = signing.sign_manifest(self.key_path, manifest_path, self.trust_path)
        output = self.work / (name + "-1.0.bpm")
        package.write_package(manifest_bytes, signature, layer, output)
        self.built[name] = output
        return output

    def test_install_list_verify_remove_lifecycle(self):
        built = self.make_package("alpha", extra=("usr/share/alpha/data",))
        result = self.bpm("install", str(built))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("BLAUOS_BPM install name=alpha version=1.0 files=2 pass", result.stdout)
        self.assertEqual((self.root / "usr/bin/alpha").stat().st_mode & 0o777, 0o755)
        result = self.bpm("list")
        self.assertIn("alpha 1.0\n", result.stdout)
        result = self.bpm("verify", "alpha")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("BLAUOS_BPM verify name=alpha", result.stdout)
        result = self.bpm("remove", "alpha")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "usr/bin/alpha").exists())
        result = self.bpm("list")
        self.assertIn("BLAUOS_BPM list count=0", result.stdout)
        self.assertFalse((self.db / "alpha.json").exists())

    def test_tampered_payload_never_installs(self):
        built = self.make_package("alpha")
        data = bytearray(built.read_bytes())
        with tarfile.open(built) as tarball:
            offset = {m.name: m.offset_data for m in tarball}["payload/usr/bin/alpha"]
        data[offset] ^= 0x01
        tampered = self.work / "tampered.bpm"
        tampered.write_bytes(bytes(data))
        result = self.bpm("install", str(tampered))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("payload-verify", result.stderr)
        self.assertFalse((self.root / "usr/bin/alpha").exists())
        self.assertFalse((self.db / "alpha.json").exists())
        leftovers = list(self.db.parent.glob("tmp/bpm-*")) if self.db.parent.exists() else []
        self.assertEqual(leftovers, [])

    def test_tampered_manifest_rejected_before_writes(self):
        built = self.make_package("alpha")
        data = bytearray(built.read_bytes())
        with tarfile.open(built) as tarball:
            offset = {m.name: m.offset_data for m in tarball}["manifest.json"]
        at = bytes(data).index(b'"version"', offset)
        data[at + 12] ^= 0x01  # flip inside the version value; JSON stays valid
        tampered = self.work / "tampered-manifest.bpm"
        tampered.write_bytes(bytes(data))
        result = self.bpm("install", str(tampered))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("manifest-tampered", result.stderr)
        self.assertFalse((self.root / "usr/bin/alpha").exists())

    def test_signature_from_untrusted_key_rejected(self):
        other = signing.generate_key()
        other_path = self.work / "other-key.json"
        release.write_json(other_path, other)
        recipes = self.work / "packages"
        recipes.mkdir(exist_ok=True)
        release.write_json(Path("packages/alpha.json"), {"schema_version": 1, "name": "alpha",
                                                         "depends": ["runtime"],
                                                         "outputs": ["usr/bin/alpha"], "summary": "f"})
        layer = self.make_layer("alpha")
        manifest = package.build_manifest(layer, Path("packages/alpha.json"), "alpha", "1.0")
        manifest_bytes = (json.dumps(manifest, indent=2) + "\n").encode()
        manifest_path = self.work / "alpha.manifest.json"
        manifest_path.write_bytes(manifest_bytes)
        signature = signing.sign_manifest(other_path, manifest_path, None)
        built = self.work / "alpha-untrusted.bpm"
        package.write_package(manifest_bytes, signature, layer, built)
        result = self.bpm("install", str(built))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown-key", result.stderr)

    def test_revoked_key_rejected(self):
        built = self.make_package("alpha")
        signing.revoke_key(self.trust_root, self.key["key_id"], "test revocation")
        release.write_json(self.trust_path, self.trust_root)
        result = self.bpm("install", str(built))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("key-not-trusted", result.stderr)

    def test_corrupt_signature_bytes_rejected(self):
        built = self.make_package("alpha")
        import io
        members = []
        with tarfile.open(built) as tarball:
            for member in tarball:
                members.append((member, tarball.extractfile(member).read() if member.isfile() else None))
        forged = self.work / "forged.bpm"
        with tarfile.open(forged, "w", format=tarfile.USTAR_FORMAT) as tarball:
            for member, data in members:
                if member.name == "manifest.json.sig":
                    signature = json.loads(data)
                    sig = bytearray(bytes.fromhex(signature["signature"]))
                    sig[10] ^= 0x01
                    signature["signature"] = sig.hex()
                    data = (json.dumps(signature, indent=2) + "\n").encode()
                    member.size = len(data)
                tarball.addfile(member, io.BytesIO(data) if data is not None else None)
        result = self.bpm("install", str(forged))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ed25519", result.stderr)

    def test_install_refuses_to_overwrite_existing_file(self):
        (self.root / "usr/bin").mkdir(parents=True)
        (self.root / "usr/bin/alpha").write_text("preexisting\n")
        built = self.make_package("alpha")
        result = self.bpm("install", str(built))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("destination-exists", result.stderr)
        self.assertEqual((self.root / "usr/bin/alpha").read_text(), "preexisting\n")
        self.assertFalse((self.db / "alpha.json").exists())

    def test_double_install_refused(self):
        built = self.make_package("alpha")
        self.assertEqual(self.bpm("install", str(built)).returncode, 0)
        result = self.bpm("install", str(built))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already-installed", result.stderr)

    def test_remove_refuses_dependents_then_succeeds_in_order(self):
        self.built["vim"] = None
        base = self.make_package("alpha")
        dependent = self.make_package("beta", depends=("alpha",))
        self.assertEqual(self.bpm("install", str(base)).returncode, 0)
        self.assertEqual(self.bpm("install", str(dependent)).returncode, 0)
        result = self.bpm("remove", "alpha")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("required-by beta", result.stderr)
        self.assertEqual(self.bpm("remove", "beta").returncode, 0)
        self.assertEqual(self.bpm("remove", "alpha").returncode, 0)

    def test_missing_dependency_refused(self):
        dependent = self.make_package("beta", depends=("alpha",))
        result = self.bpm("install", str(dependent))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing-dependency alpha", result.stderr)

    def test_verify_detects_post_install_modification_and_remove_refuses_it(self):
        built = self.make_package("alpha")
        self.assertEqual(self.bpm("install", str(built)).returncode, 0)
        (self.root / "usr/bin/alpha").write_bytes(b"locally modified\n")
        result = self.bpm("verify", "alpha")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("hash /usr/bin/alpha", result.stderr)
        result = self.bpm("remove", "alpha")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("modified-recorded-file", result.stderr)

    def test_verify_all_reports_each_package(self):
        self.assertEqual(self.bpm("install", str(self.make_package("alpha"))).returncode, 0)
        self.assertEqual(self.bpm("install", str(self.make_package("beta"))).returncode, 0)
        result = self.bpm("verify")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("name=alpha", result.stdout)
        self.assertIn("name=beta", result.stdout)

    def test_repo_index_flow_and_repo_hash_enforcement(self):
        built = self.make_package("alpha")
        target = self.repo / built.name
        target.write_bytes(built.read_bytes())
        args = type("Args", (), {"repo_dir": self.repo, "key": self.key_path,
                                 "trust_root": self.trust_path})()
        package.cmd_index(args)
        result = self.bpm("repo")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("alpha 1.0 alpha-1.0.bpm", result.stdout)
        result = self.bpm("install", "alpha")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "usr/bin/alpha").exists())
        self.assertEqual(self.bpm("remove", "alpha").returncode, 0)
        # A swapped package file must fail the index hash before any write.
        other = self.make_package("beta")
        target.write_bytes(other.read_bytes())
        result = self.bpm("install", "alpha")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("repo-hash", result.stderr)
        self.assertFalse((self.root / "usr/bin/alpha").exists())

    def test_repo_rejects_tampered_index(self):
        built = self.make_package("alpha")
        (self.repo / built.name).write_bytes(built.read_bytes())
        args = type("Args", (), {"repo_dir": self.repo, "key": self.key_path,
                                 "trust_root": self.trust_path})()
        package.cmd_index(args)
        index = json.loads((self.repo / "index.json").read_text())
        index["packages"][0]["sha256"] = "0" * 64
        (self.repo / "index.json").write_text(json.dumps(index, indent=2) + "\n")
        result = self.bpm("repo")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("manifest-tampered", result.stderr)

    def test_recv_hex_stream_roundtrip_and_digest_enforcement(self):
        payload = bytes(range(256)) * 40
        digest = hashlib.sha256(payload).hexdigest()
        lines = [payload[i:i + 100].hex() for i in range(0, len(payload), 100)]
        stream = "\n".join(lines + ["END " + digest]) + "\n"
        out = self.work / "received.bin"
        result = self.bpm("recv", str(out), stdin=stream)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("BLAUOS_BPM recv pass sha256=" + digest, result.stdout)
        self.assertEqual(out.read_bytes(), payload)
        # A wrong terminator digest deletes the output and fails closed.
        stream_bad = "\n".join(lines + ["END " + "0" * 64]) + "\n"
        result = self.bpm("recv", str(out) + ".bad", stdin=stream_bad)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.work / "received.bin.bad").exists())
        # Non-hex frames are rejected.
        result = self.bpm("recv", str(out) + ".junk", stdin="zz\nEND " + digest + "\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.work / "received.bin.junk").exists())

    def test_manifest_schema_violations_rejected(self):
        import io
        for mutate, marker in (
                (lambda m: m.update(schema_version=2), "manifest-schema"),
                (lambda m: m["files"].append(dict(m["files"][0])), "manifest-schema"),
                (lambda m: m["files"][0].update(path="/var/db/bpm/db/evil.json"), "manifest-schema"),
                (lambda m: m.update(depends="runtime"), "manifest-schema")):
            with self.subTest(marker=marker):
                good = self.make_package("good" + str(len(self.built)))
                with tarfile.open(good) as tarball:
                    members = [(member, tarball.extractfile(member).read() if member.isfile() else None)
                               for member in tarball]
                manifest = None
                for member, data in members:
                    if member.name == "manifest.json":
                        manifest = json.loads(data)
                mutate(manifest)
                manifest_bytes = (json.dumps(manifest, indent=2) + "\n").encode()
                manifest_path = self.work / "mutated.manifest.json"
                manifest_path.write_bytes(manifest_bytes)
                signature = signing.sign_manifest(self.key_path, manifest_path, self.trust_path)
                signature_bytes = (json.dumps(signature, indent=2) + "\n").encode()
                built = self.work / ("mutated-%d.bpm" % len(self.built))
                with tarfile.open(built, "w", format=tarfile.USTAR_FORMAT) as tarball:
                    for member, data in members:
                        if member.name == "manifest.json":
                            data = manifest_bytes
                            member.size = len(data)
                        if member.name == "manifest.json.sig":
                            data = signature_bytes
                            member.size = len(data)
                        tarball.addfile(member, io.BytesIO(data) if data is not None else None)
                self.built[built.name] = built
                result = self.bpm("install", str(built))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(marker, result.stderr)


if __name__ == "__main__":
    unittest.main()
