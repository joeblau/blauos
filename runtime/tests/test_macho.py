"""Malformed-artifact and provenance tests; these are not runtime evidence."""
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("blauos_runtime", ROOT / "scripts/build/runtime.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class MachOValidationTests(unittest.TestCase):
    def header(self, cpu=runtime.ARM64, commands=b"", count=0, kind=6):
        return struct.pack("<8I", 0xFEEDFACF, cpu, 0, kind, count, len(commands), 0, 0) + commands

    def write(self, contents):
        path = Path(self.directory.name) / "input"
        path.write_bytes(contents)
        return path

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def test_rejects_non_arm64_artifacts(self):
        with self.assertRaisesRegex(ValueError, "expected ARM64"):
            runtime.macho(self.write(self.header(cpu=0x01000007)))

    def test_rejects_command_outside_file(self):
        malformed = struct.pack("<8I", 0xFEEDFACF, runtime.ARM64, 0, 6, 1, 128, 0, 0)
        with self.assertRaisesRegex(ValueError, "outside"):
            runtime.macho(self.write(malformed))

    def test_rejects_unterminated_dependency_name(self):
        command = struct.pack("<6I", 0xC, 28, 24, 0, 0, 0) + b"abcd"
        with self.assertRaisesRegex(ValueError, "unterminated"):
            runtime.macho(self.write(self.header(commands=command, count=1)))

    def test_rejects_fat_slice_outside_file(self):
        fat = struct.pack(">7I", 0xCAFEBABE, 1, runtime.ARM64, 0, 4096, 32, 0)
        with self.assertRaisesRegex(ValueError, "slice outside"):
            runtime.macho(self.write(fat))

    def test_selects_arm64_fat_slice(self):
        thin = self.header()
        fat = struct.pack(">7I", 0xCAFEBABE, 1, runtime.ARM64, 0, 28, len(thin), 0) + thin
        self.assertEqual(runtime.macho(self.write(fat))["architecture"], "arm64")

    def test_fails_closed_without_runtime_and_provenance(self):
        root = Path(self.directory.name) / "root"
        root.mkdir()
        lock = Path(self.directory.name) / "lock.json"
        lock.write_text(json.dumps({"sources": []}))
        report = runtime.inspect(root, None, lock)
        self.assertEqual(report["status"], "blocked")
        self.assertFalse(report["offline_dependency_closure"])
        self.assertFalse(report["execution_verified"])

    def test_rejects_symlink_to_host_library(self):
        root = Path(self.directory.name) / "root"
        root.mkdir()
        outside = self.write(self.header())
        (root / "escape").symlink_to(outside)
        lock = Path(self.directory.name) / "lock.json"
        lock.write_text(json.dumps({"sources": []}))
        report = runtime.inspect(root, None, lock)
        self.assertTrue(any("escapes staged root" in e for e in report["errors"]))

    def test_rejects_directory_symlink_to_host_tree(self):
        root = Path(self.directory.name) / "root"
        root.mkdir()
        outside = Path(self.directory.name) / "host"
        outside.mkdir()
        (root / "escape").symlink_to(outside, target_is_directory=True)
        lock = Path(self.directory.name) / "lock.json"
        lock.write_text(json.dumps({"sources": []}))
        report = runtime.inspect(root, None, lock)
        self.assertTrue(any("directory symlink escapes" in e for e in report["errors"]))

    def staged(self, source="upstream", revision="a" * 40, hash_override=None):
        root = Path(self.directory.name) / "root"
        root.mkdir()
        artifact = root / "library"
        artifact.write_bytes(self.header())
        lock = Path(self.directory.name) / "lock.json"
        lock.write_text(json.dumps({"sources": [{"name": "upstream", "revision": "a" * 40}]}))
        provenance = Path(self.directory.name) / "provenance.json"
        provenance.write_text(json.dumps({"artifacts": [{"path": "/library", "source": source,
            "revision": revision, "sha256": hash_override or runtime.digest(artifact)}]}))
        return root, provenance, lock

    def test_rejects_unpinned_source_revision(self):
        report = runtime.inspect(*self.staged(revision="b" * 40))
        self.assertTrue(any("unpinned source revision" in e for e in report["errors"]))

    def test_rejects_artifact_hash_mismatch(self):
        report = runtime.inspect(*self.staged(hash_override="0" * 64))
        self.assertTrue(any("mismatched source hash" in e for e in report["errors"]))

    def test_rejects_missing_local_source_snapshot(self):
        report = runtime.inspect(*self.staged(source="blauos", revision="workspace-sha256:invalid"))
        self.assertTrue(any("local project source snapshot" in e for e in report["errors"]))

    def test_kernel_has_no_userspace_loader_requirement(self):
        root, provenance, lock = self.staged()
        kernel = root / "System/Library/Kernels/kernel"
        kernel.parent.mkdir(parents=True)
        kernel.write_bytes(self.header(kind=2))
        data = json.loads(provenance.read_text())
        data["artifacts"].append({"path": "/System/Library/Kernels/kernel", "source": "upstream",
            "revision": "a" * 40, "sha256": runtime.digest(kernel)})
        provenance.write_text(json.dumps(data))
        report = runtime.inspect(root, provenance, lock)
        self.assertFalse(any("unexpected executable dynamic linker: /System/Library/Kernels" in e for e in report["errors"]))
        self.assertEqual(next(a for a in report["artifacts"] if a["path"].endswith("/kernel"))["artifact_kind"], "kernel")


if __name__ == "__main__":
    unittest.main()
