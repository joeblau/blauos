"""Hardware and private-evidence guards; fixtures here are synthetic."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("lab_inventory", Path(__file__).resolve().parents[1] / "scripts/platform/lab_inventory.py")
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)


class LabInventoryTests(unittest.TestCase):
    def fixture(self):
        return {"SPHardwareDataType": [{"machine_model": "Macmini9,1", "chip_type": "Apple M1",
                                      "physical_memory": "16 GB", "number_processors": "proc 8:0:4:4",
                                      "serial_number": "PRIVATE-SERIAL", "platform_UUID": "PRIVATE-UUID"}]}, {
            "model": b"Macmini9,1\0", "target-type": b"J274\0",
            "compatible": b"J274AP\0Macmini9,1\0AppleARM\0",
            "IOPlatformUUID": "PRIVATE-UUID", "IOPlatformSerialNumber": "PRIVATE-SERIAL"}

    def test_public_hardware_omits_private_identifiers(self):
        profile, expert = self.fixture()
        public = lab.public_hardware(profile, expert, "arm64")
        serialized = json.dumps(public)
        self.assertNotIn("PRIVATE-SERIAL", serialized)
        self.assertNotIn("PRIVATE-UUID", serialized)
        self.assertTrue(lab.matches_target(public))

    def test_other_apple_silicon_and_wrong_board_are_rejected(self):
        profile, expert = self.fixture()
        public = lab.public_hardware(profile, expert, "arm64")
        for changed in ({"model": "Mac15,8"}, {"chip": "Apple M1 Pro"}, {"architecture": "x86_64"},
                        {"target_type": "J514c"}, {"board_compatible": "J514cAP\0Mac15,8"}):
            with self.subTest(changed=changed):
                self.assertFalse(lab.matches_target(dict(public, **changed)))

    def test_public_device_names_do_not_publish_adapter_identifiers(self):
        devices = lab.public_serial_devices(["/dev/cu.usbserial.PRIVATE-SERIAL", "/dev/cu.debug-console"])
        self.assertNotIn("PRIVATE-SERIAL", json.dumps(devices))
        self.assertEqual({item["kind"] for item in devices}, {"usb-serial", "debug-console"})

    def test_public_and_private_outputs_cannot_be_same_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "capture.plist"
            with patch("sys.argv", ["lab_inventory.py", "--role", "test-machine", "--output", str(path),
                                    "--private-platform-output", str(path), "--identity-nonce", "01" * 32]):
                with self.assertRaises(SystemExit) as failure:
                    lab.main()
            self.assertEqual(failure.exception.code, 2)
            self.assertFalse(path.exists())

    def test_ambiguous_hardware_record_is_rejected(self):
        profile, expert = self.fixture()
        profile["SPHardwareDataType"] *= 2
        with self.assertRaises(ValueError):
            lab.public_hardware(profile, expert, "arm64")

    def test_identity_is_bound_to_nonce_and_machine(self):
        _, expert = self.fixture()
        first = lab.identity_fingerprint(expert, "01" * 32)
        self.assertNotEqual(first, lab.identity_fingerprint(expert, "02" * 32))
        self.assertNotEqual(first, lab.identity_fingerprint(dict(expert, IOPlatformUUID="OTHER"), "01" * 32))
        for nonce in ("", "0" * 63, "G" * 64):
            with self.assertRaises(ValueError):
                lab.identity_fingerprint(expert, nonce)
        with self.assertRaises(ValueError):
            lab.identity_fingerprint({"model": "Macmini9,1"}, "01" * 32)

    def test_private_evidence_cannot_enter_public_repo_or_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".git").mkdir()
            with self.assertRaises(ValueError):
                lab.write_private(root / "evidence/platform.plist", b"private identity")
            path = root / ".blauos/private/platform.plist"
            lab.write_private(path, b"private identity")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                lab.write_private(path, b"replacement")
            self.assertEqual(path.read_bytes(), b"private identity")


if __name__ == "__main__":
    unittest.main()
