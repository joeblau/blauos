"""Reject unsafe command manifests and SFTP paths before touching a guest."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/vm"))
from install_commands import command_artifacts, digest, sftp_quote


class CommandInstallTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "root"
        self.root.mkdir()
        self.receipt = Path(self.directory.name) / "provenance.json"

    def manifest(self, entries):
        self.receipt.write_text(json.dumps({"schema_version": 1, "artifacts": entries}))

    def file(self, path="bin/ls"):
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"test fixture, never executed")
        return {"path": "/" + path, "sha256": digest(file)}

    def test_corrupt_transfer_input_is_rejected(self):
        entry = self.file()
        self.manifest([entry])
        (self.root / "bin/ls").write_bytes(b"changed after build")
        with self.assertRaisesRegex(ValueError, "mismatched"):
            command_artifacts(self.root, self.receipt)

    def test_basename_collision_cannot_shadow_another_layer(self):
        self.manifest([self.file(), self.file("usr/bin/ls")])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            command_artifacts(self.root, self.receipt)

    def test_noncommand_and_parent_escape_paths_are_rejected(self):
        for path in ("/usr/lib/libSystem.B.dylib", "/bin/../ls", "/bin/ls\nrm", "/bin/.profile"):
            with self.subTest(path=path):
                self.manifest([{"path": path, "sha256": "a" * 64}])
                with self.assertRaisesRegex(ValueError, "Only command"):
                    command_artifacts(self.root, self.receipt)

    def test_symlink_parent_cannot_import_a_host_executable(self):
        outside = Path(self.directory.name) / "outside"
        outside.mkdir()
        (outside / "ls").write_bytes(b"outside executable")
        (self.root / "bin").symlink_to(outside)
        self.manifest([{"path": "/bin/ls", "sha256": digest(outside / "ls")}])
        with self.assertRaisesRegex(ValueError, "escaping"):
            command_artifacts(self.root, self.receipt)

    def test_sftp_paths_reject_batch_command_injection(self):
        for path in ("file\nput secret", "file\rbye", "file\x00", "file\x7f"):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "Control"):
                sftp_quote(path)
        self.assertEqual(sftp_quote('/a "quoted"\\path'), '"/a \\"quoted\\"\\\\path"')


if __name__ == "__main__":
    unittest.main()
