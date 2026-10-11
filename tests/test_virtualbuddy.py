"""Bundle safety tests; these never establish guest execution."""
import hashlib
import importlib.util
from pathlib import Path
import plistlib
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("virtualbuddy_bundle", ROOT / "scripts/vm/virtualbuddy.py")
vb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vb)


class VirtualBuddyBundleTests(unittest.TestCase):
    def test_bad_input_hash_creates_no_bundle(self):
        with tempfile.TemporaryDirectory() as folder:
            image, output = Path(folder) / "source.img", Path(folder) / "new.vbvm"
            image.write_bytes(b"image")
            with self.assertRaisesRegex(ValueError, "hash differs"):
                vb.bundle(image, "0" * 64, output)
            self.assertFalse(output.exists())

    def test_existing_vm_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            image, output = Path(folder) / "source.img", Path(folder) / "existing.vbvm"
            image.write_bytes(b"image")
            output.mkdir()
            sentinel = output / "Disk.img"
            sentinel.write_bytes(b"user data")
            with self.assertRaisesRegex(ValueError, "existing VMs"):
                vb.bundle(image, vb.digest(image), output)
            self.assertEqual(sentinel.read_bytes(), b"user data")

    def test_copies_exact_image_with_distinct_identity_and_fresh_nvram(self):
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "source.img"
            image.write_bytes(b"source-built disk")
            receipts = [vb.bundle(image, hashlib.sha256(image.read_bytes()).hexdigest(),
                                  Path(folder) / (name + ".vbvm")) for name in ("first", "second")]
            self.assertNotEqual(receipts[0]["metadata"]["uuid"], receipts[1]["metadata"]["uuid"])
            self.assertNotEqual(receipts[0]["configuration"]["hardware"]["networkDevices"][0]["macAddress"],
                                receipts[1]["configuration"]["hardware"]["networkDevices"][0]["macAddress"])
            for receipt in receipts:
                bundle = Path(receipt["bundle"])
                self.assertEqual((bundle / "Disk.img").read_bytes(), image.read_bytes())
                config = plistlib.loads((bundle / ".vbdata/Config.plist").read_bytes())
                self.assertEqual(config["hardware"]["cpuCount"], 1)
                self.assertEqual(config["hardware"]["memorySize"], 1073741824)
                self.assertFalse((bundle / ".vbdata/nvram").exists())
                self.assertFalse(receipt["boot_verified"])
