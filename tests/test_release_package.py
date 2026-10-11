"""Signed package format tests (#109 follow-up): build/inspect/index/stage-repo.

The format under test is the host side of the guest package manager: .bpm
ustar packages with a signed manifest.json (per-file SHA-256, recipe binding),
verified with the repository's Ed25519 trust-root tooling. All fixtures are
synthetic; nothing here boots a guest (live acceptance is recorded under
evidence/virtualization/package-manager).
"""
import importlib.util
import json
import os
from pathlib import Path
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


class PackageFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.key = signing.generate_key()
        self.key_path = self.work / "key.json"
        release.write_json(self.key_path, self.key)
        self.trust_root = signing.empty_trust_root()
        signing.add_key_to_root(self.trust_root, {"key_id": self.key["key_id"],
                                                  "public": bytes.fromhex(self.key["public_key"])})
        self.trust_path = self.work / "trust-root.json"
        release.write_json(self.trust_path, self.trust_root)
        self.layer = self.work / "layer"
        binary = self.layer / "usr/bin/tool"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"synthetic guest tool payload\n")
        binary.chmod(0o755)
        doc = self.layer / "usr/share/doc/tool/README"
        doc.parent.mkdir(parents=True)
        doc.write_text("fixture documentation\n")
        self.recipes = self.work / "packages"
        self.recipes.mkdir()
        # Recipes bind by repo-relative packages/*.json path; run the fixture
        # with the scratch root as cwd so "packages/tool.json" resolves there.
        self.previous_cwd = os.getcwd()
        os.chdir(self.work)
        self.addCleanup(os.chdir, self.previous_cwd)
        self.recipe = Path("packages/tool.json")
        release.write_json(self.recipe, {"schema_version": 1, "name": "tool",
                                         "depends": ["runtime"], "outputs": ["usr/bin/tool"],
                                         "summary": "fixture"})

    def build(self, name="tool", recipe=None):
        manifest = package.build_manifest(self.layer, recipe or self.recipe, name, "1.0")
        manifest_bytes = (json.dumps(manifest, indent=2) + "\n").encode()
        manifest_path = self.work / "manifest.json"
        manifest_path.write_bytes(manifest_bytes)
        signature = signing.sign_manifest(self.key_path, manifest_path, self.trust_path)
        output = self.work / f"{name}-1.0.bpm"
        package.write_package(manifest_bytes, signature, self.layer, output)
        return output

    def tampered(self, source: Path, member_prefix: str) -> Path:
        """Flip one byte inside the first matching member's data."""
        data = bytearray(source.read_bytes())
        with tarfile.open(source) as tarball:
            offsets = {member.name: (member.offset_data, member.size) for member in tarball}
        offset, size = next(value for name, value in offsets.items()
                            if name.startswith(member_prefix) and value[1] > 0)
        data[offset] ^= 0x01
        target = self.work / (source.stem + "-tampered.bpm")
        target.write_bytes(bytes(data))
        return target


class BuildTests(PackageFixture):
    def test_build_manifest_binds_recipe_and_hashes_every_file(self):
        manifest = package.build_manifest(self.layer, self.recipe, "tool", "1.0")
        self.assertEqual(manifest["manifest_kind"], "blauos-package")
        self.assertEqual(manifest["depends"], ["runtime"])
        self.assertEqual(manifest["recipe"]["path"], str(self.recipe))
        self.assertEqual(manifest["recipe"]["sha256"], release.file_sha256(self.recipe))
        paths = {entry["path"] for entry in manifest["files"]}
        self.assertEqual(paths, {"/usr/bin/tool", "/usr/share/doc/tool/README"})
        for entry in manifest["files"]:
            self.assertEqual(entry["sha256"],
                             release.file_sha256(self.layer / entry["path"].lstrip("/")))

    def test_recipe_name_mismatch_rejected(self):
        with self.assertRaisesRegex(release.ReleaseError, "does not match"):
            package.build_manifest(self.layer, self.recipe, "other", "1.0")

    def test_recipe_output_missing_from_layer_rejected(self):
        recipe = json.loads(self.recipe.read_text())
        recipe["outputs"] = ["usr/bin/not-built"]
        self.recipe.write_text(json.dumps(recipe))
        with self.assertRaisesRegex(release.ReleaseError, "recipe outputs missing"):
            package.build_manifest(self.layer, self.recipe, "tool", "1.0")

    def test_symlink_payload_rejected(self):
        (self.layer / "usr/bin/link").symlink_to("tool")
        with self.assertRaisesRegex(release.ReleaseError, "symlink"):
            package.build_manifest(self.layer, self.recipe, "tool", "1.0")

    def test_package_manager_state_paths_rejected(self):
        for path in ("/var/db/bpm/db/x.json", "/etc/blauos/trust-root.json", "/../escape",
                     "relative/path", "/usr//bin/tool"):
            with self.subTest(path=path):
                with self.assertRaises(release.ReleaseError):
                    package.valid_guest_path(path)

    def test_roundtrip_verify_passes(self):
        built = self.build()
        result = package.verify_package(built, self.trust_path)
        self.assertTrue(result["verified"])
        self.assertEqual(result["name"], "tool")
        self.assertEqual(result["file_count"], 2)
        self.assertEqual(result["key_id"], self.key["key_id"])


class RejectionTests(PackageFixture):
    def test_tampered_payload_rejected(self):
        built = self.build()
        with self.assertRaisesRegex(release.ReleaseError, "payload hash differs"):
            package.verify_package(self.tampered(built, "payload/"), self.trust_path)

    def test_tampered_manifest_rejected(self):
        built = self.build()
        with self.assertRaises(release.ReleaseError):
            package.verify_package(self.tampered(built, "manifest.json"), self.trust_path)

    def test_untrusted_key_rejected(self):
        built = self.build()
        other_root = signing.empty_trust_root()
        other_path = self.work / "other-trust.json"
        release.write_json(other_path, other_root)
        with self.assertRaisesRegex(release.ReleaseError, "unknown key_id"):
            package.verify_package(built, other_path)

    def test_revoked_key_rejected(self):
        built = self.build()
        signing.revoke_key(self.trust_root, self.key["key_id"], "test revocation")
        release.write_json(self.trust_path, self.trust_root)
        with self.assertRaisesRegex(release.ReleaseError, "revoked"):
            package.verify_package(built, self.trust_path)

    def test_missing_signature_member_rejected(self):
        built = self.build()
        members = []
        with tarfile.open(built) as tarball:
            for member in tarball:
                if member.name == "manifest.json.sig":
                    continue
                members.append((member, tarball.extractfile(member).read() if member.isfile() else None))
        unsigned = self.work / "unsigned.bpm"
        import io
        with tarfile.open(unsigned, "w", format=tarfile.USTAR_FORMAT) as tarball:
            for member, data in members:
                tarball.addfile(member, io.BytesIO(data) if data is not None else None)
        with self.assertRaisesRegex(release.ReleaseError, "manifest.json.sig"):
            package.verify_package(unsigned, self.trust_path)

    def test_manifest_not_under_signature_rejected(self):
        """A rebuilt manifest with changed metadata must fail the signed digest."""
        built = self.build()
        with tarfile.open(built) as tarball:
            manifest = json.loads(tarball.extractfile("manifest.json").read())
            sig = tarball.extractfile("manifest.json.sig").read()
            members = [(member, tarball.extractfile(member).read() if member.isfile() else None)
                       for member in tarball]
        manifest["version"] = "9.9"
        changed = (json.dumps(manifest, indent=2) + "\n").encode()
        forged = self.work / "forged.bpm"
        import io
        with tarfile.open(forged, "w", format=tarfile.USTAR_FORMAT) as tarball:
            for member, data in members:
                if member.name == "manifest.json":
                    data = changed
                    member.size = len(data)
                if member.name == "manifest.json.sig":
                    data = sig
                    member.size = len(sig)
                tarball.addfile(member, io.BytesIO(data) if data is not None else None)
        with self.assertRaisesRegex(release.ReleaseError, "tampered"):
            package.verify_package(forged, self.trust_path)


class IndexAndStageTests(PackageFixture):
    def test_index_signs_and_stage_repo_layers(self):
        repo = self.work / "repo"
        repo.mkdir()
        built = self.build()
        target = repo / built.name
        target.write_bytes(built.read_bytes())
        args = type("Args", (), {"repo_dir": repo, "key": self.key_path,
                                 "trust_root": self.trust_path})()
        result = package.cmd_index(args)
        self.assertEqual(result["packages"], 1)
        index = release.load_json_file(repo / "index.json")
        self.assertEqual(index["manifest_kind"], "blauos-package-index")
        self.assertEqual(index["packages"][0]["sha256"], release.file_sha256(target))
        signing.verify_manifest(self.trust_path, repo / "index.json", repo / "index.json.sig")
        stage = self.work / "staged"
        stage_args = type("Args", (), {"repo_dir": repo, "trust_root": self.trust_path,
                                       "output": stage})()
        package.cmd_stage_repo(stage_args)
        root = stage / "root"
        self.assertTrue((root / "etc/blauos/trust-root.json").is_file())
        self.assertTrue((root / "var/db/bpm/repo" / built.name).is_file())
        self.assertTrue((root / "var/db/bpm/repo/index.json").is_file())
        self.assertTrue((root / "var/db/bpm/db").is_dir())
        provenance = release.load_json_file(stage / "provenance.json")
        paths = {entry["path"] for entry in provenance["artifacts"]}
        self.assertIn("/etc/blauos/trust-root.json", paths)
        self.assertIn("/var/db/bpm/repo/" + built.name, paths)

    def test_index_rejects_duplicate_package_names(self):
        repo = self.work / "repo"
        repo.mkdir()
        built = self.build()
        (repo / "a.bpm").write_bytes(built.read_bytes())
        (repo / "b.bpm").write_bytes(built.read_bytes())
        args = type("Args", (), {"repo_dir": repo, "key": self.key_path,
                                 "trust_root": self.trust_path})()
        with self.assertRaisesRegex(release.ReleaseError, "duplicate package name"):
            package.cmd_index(args)


if __name__ == "__main__":
    unittest.main()
