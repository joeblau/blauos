"""Composition and evidence safeguards; real login is verified by the VM harness."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/vm"))
import ssh_workflow as workflow


class SSHWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)

    def layer(self, name, artifact, content):
        layer = self.root / name
        file = layer / artifact.lstrip("/")
        file.parent.mkdir(parents=True)
        file.write_text(content)
        receipt = self.root / (name + ".json")
        receipt.write_text(json.dumps({"schema_version": 1, "artifacts": [
            {"path": artifact, "sha256": workflow.digest(file), "source": name}]}))
        return layer, receipt

    def test_composition_preserves_baseline_provenance_and_adds_exact_source_artifacts(self):
        base, original = self.layer("base", "/bin/sh", "existing independent shell")
        added, receipt = self.layer("ssh", "/usr/sbin/sshd", "source-built daemon")
        previous = original.read_bytes()
        root, provenance = workflow.compose(base, original, [(added, receipt)], self.root / "composed")
        self.assertEqual(original.read_bytes(), previous)
        self.assertEqual((base / "bin/sh").read_text(), "existing independent shell")
        self.assertEqual((root / "usr/sbin/sshd").read_text(), "source-built daemon")
        self.assertEqual(json.loads(provenance.read_text())["artifacts"][0], json.loads(previous)["artifacts"][0])

    def test_baseline_file_and_dangling_symlink_are_never_overwritten(self):
        base, original = self.layer("base", "/bin/sh", "shell")
        added, receipt = self.layer("overwrite", "/bin/sh", "replacement")
        with self.assertRaisesRegex(ValueError, "replace baseline"):
            workflow.compose(base, original, [(added, receipt)], self.root / "bad")
        self.assertFalse((self.root / "bad").exists())
        (base / "usr").mkdir()
        (base / "usr/new").symlink_to(self.root / "outside")
        added, receipt = self.layer("escape", "/usr/new", "payload")
        with self.assertRaisesRegex(ValueError, "replace baseline"):
            workflow.compose(base, original, [(added, receipt)], self.root / "bad")
        self.assertFalse((self.root / "outside").exists())

    def test_entropy_receipt_binds_binary_lock_and_source_overlays_without_claiming_guest_pass(self):
        data = {"schema_version": 1, "kernel_sha256": "a" * 64, "loader_sha256": "b" * 64,
                "source_lock_sha256": "c" * 64, "loader_require_efi_rng": True,
                "kernel_prng_provider": "yarrow-fixed-v1", "virtio_entropy_device": True,
                "patches": [{"path": p, "sha256": "c" * 64} for p in workflow.ENTROPY_PATCHES]}
        with mock.patch.object(workflow, "digest", return_value="c" * 64):
            verified = workflow.verify_entropy_receipt(data, "a" * 64, "b" * 64)
            self.assertFalse(verified["guest_entropy_admission_verified"])
            for field, bad in (("kernel_sha256", "d" * 64), ("loader_sha256", "d" * 64),
                               ("source_lock_sha256", "d" * 64), ("loader_require_efi_rng", False),
                               ("kernel_prng_provider", "old-unseeded-provider"), ("virtio_entropy_device", False)):
                changed = copy.deepcopy(data)
                changed[field] = bad
                with self.subTest(field=field), self.assertRaises(ValueError):
                    workflow.verify_entropy_receipt(changed, "a" * 64, "b" * 64)
            changed = copy.deepcopy(data)
            changed["patches"][0]["sha256"] = "d" * 64
            with self.assertRaisesRegex(ValueError, "overlays differ"):
                workflow.verify_entropy_receipt(changed, "a" * 64, "b" * 64)

    def test_symlink_parent_cannot_escape_or_modify_input_root(self):
        base, original = self.layer("base", "/bin/sh", "shell")
        outside = self.root / "outside"
        outside.mkdir()
        (base / "usr").symlink_to(outside, target_is_directory=True)
        added, receipt = self.layer("escape", "/usr/new", "payload")
        before = original.read_bytes()
        with self.assertRaisesRegex(ValueError, "Symlink in additive artifact parent"):
            workflow.compose(base, original, [(added, receipt)], self.root / "bad")
        self.assertEqual(original.read_bytes(), before)
        self.assertTrue((base / "usr").is_symlink())
        self.assertEqual(list(outside.iterdir()), [])

    def test_evidence_allowlist_excludes_disks_and_keys_and_redacts_authorized_public_text(self):
        output = self.root / "run"
        (output / "logs").mkdir(parents=True)
        (output / "private").mkdir()
        (output / "smoke").mkdir()
        (output / "result.json").write_text('{"status":"failed"}')
        (output / "logs/start.log").write_text("client ssh-ed25519 CLIENT; host ssh-ed25519 HOST\n")
        (output / "private/test_identity").write_text("never export this key")
        (output / "smoke/wrong-key").write_text("never export this key")
        (output / "smoke/writable.img").write_bytes(b"never export this disk")
        workflow.export_evidence(output, ["ssh-ed25519 CLIENT"])
        exported = output / "evidence"
        self.assertFalse((exported / "private").exists())
        self.assertFalse((exported / "smoke/writable.img").exists())
        self.assertNotIn("CLIENT", (exported / "logs/start.log").read_text())
        self.assertIn("ssh-ed25519 HOST", (exported / "logs/start.log").read_text())

    def test_private_key_content_in_log_refuses_export(self):
        output = self.root / "run"
        (output / "logs").mkdir(parents=True)
        (output / "logs/bad.log").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n")
        with self.assertRaisesRegex(ValueError, "Private-key content"):
            workflow.export_evidence(output, [])
        self.assertFalse((output / "evidence/logs/bad.log").exists())


if __name__ == "__main__":
    unittest.main()
