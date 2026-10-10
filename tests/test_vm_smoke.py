"""Harness tests, never guest-boot evidence."""
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("vm_smoke", ROOT / "scripts/vm/smoke.py")
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


class VMReadinessTests(unittest.TestCase):
    def test_prior_boot_nonce_cannot_satisfy_readiness(self):
        old = "a" * 64
        current = "b" * 64
        log = "\n".join(f"BLAUOS_SMOKE {old} {stage}" for stage in smoke.REQUIRED)
        self.assertEqual(smoke.read_markers(log, current), set())

    def test_echoed_command_cannot_satisfy_readiness(self):
        nonce = "a" * 64
        self.assertEqual(smoke.read_markers(f"# echo BLAUOS_SMOKE {nonce} shell", nonce), set())

    def test_only_exact_guest_markers_count(self):
        nonce = "a" * 64
        self.assertEqual(smoke.read_markers(f"BLAUOS_SMOKE {nonce} launchd\r\n", nonce), {"launchd"})
        self.assertEqual(smoke.read_markers(f"BLAUOS_SMOKE {nonce} launchd trailing", nonce), set())

    def test_panic_and_missing_runtime_are_failure_signals(self):
        for message in ("panic(cpu 0 caller 0xffff): test", "Kernel panic", "BLAUOS_SMOKE_ERROR stage=dns errno=3",
                        "dyld: Library not found: /usr/lib/libSystem.B.dylib"):
            self.assertTrue(smoke.FAILURE.search(message))

    def test_runtime_audit_missing_rejects_image(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            (path / "manifest.json").write_text(json.dumps({"schema_version": 1, "architecture": "arm64", "runtime_audit_passed": True}))
            args = SimpleNamespace(manifest=path / "manifest.json")
            with self.assertRaisesRegex(ValueError, "audit is absent"):
                smoke.validate_artifacts(args)

    def test_unknown_runtime_closure_rejects_image(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            (path / "manifest.json").write_text(json.dumps({"schema_version": 1, "architecture": "arm64"}))
            with self.assertRaisesRegex(ValueError, "no passing"):
                smoke.validate_artifacts(SimpleNamespace(manifest=path / "manifest.json"))

    def test_target_disk_is_snapshot_and_network_binds_loopback(self):
        with tempfile.TemporaryDirectory() as folder:
            args = SimpleNamespace(qemu="qemu-system-aarch64", machine="virt-9.2", cpus=2, memory=1024,
                                   image=Path(folder) / "image", firmware=Path(folder) / "firmware", guest_port=18080)
            command = smoke.qemu_command(args, Path(folder) / "vars")
            self.assertIn("snapshot=on", " ".join(command))
            self.assertIn("hostfwd=tcp:127.0.0.1:18080-10.0.2.15:18080", " ".join(command))
            self.assertNotIn("mon:stdio", command)


if __name__ == "__main__":
    unittest.main()
