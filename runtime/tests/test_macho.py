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

    def chained_import(self, weak=False):
        names = b"_required_objc\0"
        fixups = struct.pack("<7I", 0, 0, 28, 32, 1, 1, 0)
        fixups += struct.pack("<I", 0xFE | (0x100 if weak else 0)) + names
        command = struct.pack("<4I", 0x80000034, 16, 48, len(fixups))
        return self.header(commands=command, count=1) + fixups

    def test_parses_required_flat_import(self):
        info = runtime.macho(self.write(self.chained_import()))
        self.assertEqual(info["flat_namespace_imports"], [{"name": "_required_objc", "weak": False}])

    def test_distinguishes_weak_flat_import(self):
        info = runtime.macho(self.write(self.chained_import(weak=True)))
        self.assertTrue(info["flat_namespace_imports"][0]["weak"])

    def test_flat_import_requires_staged_provider(self):
        root, provenance, lock = self.staged()
        (root / "library").write_bytes(self.chained_import())
        data = json.loads(provenance.read_text())
        data["artifacts"][0]["sha256"] = runtime.digest(root / "library")
        provenance.write_text(json.dumps(data))
        report = runtime.inspect(root, provenance, lock)
        self.assertTrue(any("required flat namespace import unresolved" in e for e in report["errors"]))

    def provider_stage(self, dependency_kind=None):
        root, provenance, lock = self.staged()
        name = b"/provider\0"
        dependency = b""
        if dependency_kind:
            length = (24 + len(name) + 7) & ~7
            dependency = struct.pack("<6I", dependency_kind, length, 24, 0, 0, 0) + name
            dependency += bytes(length - len(dependency))
        names = b"_required_objc\0"
        fixups = struct.pack("<7I", 0, 0, 28, 32, 1, 1, 0) + struct.pack("<I", 0xFE) + names
        command = struct.pack("<4I", 0x80000034, 16, 48 + len(dependency), len(fixups))
        (root / "library").write_bytes(self.header(commands=dependency + command,
            count=2 if dependency else 1) + fixups)
        strings = b"\0_required_objc\0"
        symbols = struct.pack("<IBBHQ", 1, 0xF, 1, 0, 1)
        symtab = struct.pack("<6I", 2, 24, 56, 1, 72, len(strings))
        (root / "provider").write_bytes(self.header(commands=symtab, count=1) + symbols + strings)
        data = json.loads(provenance.read_text())
        data["flat_namespace_providers"] = ["/provider"]
        data["artifacts"][0]["sha256"] = runtime.digest(root / "library")
        data["artifacts"].append({"path": "/provider", "source": "upstream", "revision": "a" * 40,
                                  "sha256": runtime.digest(root / "provider")})
        provenance.write_text(json.dumps(data))
        return root, provenance, lock

    def test_declared_provider_without_load_dependency_fails(self):
        report = runtime.inspect(*self.provider_stage())
        self.assertTrue(any("provider is not loaded by dependency graph" in e for e in report["errors"]))

    def test_weak_dependency_cannot_supply_required_flat_provider(self):
        report = runtime.inspect(*self.provider_stage(dependency_kind=0x80000018))
        self.assertTrue(any("provider is not loaded by dependency graph" in e for e in report["errors"]))

    def test_strong_dependency_supplies_required_flat_provider(self):
        report = runtime.inspect(*self.provider_stage(dependency_kind=0xC))
        self.assertFalse(any("flat namespace" in e for e in report["errors"]))
        artifact = next(entry for entry in report["artifacts"] if entry["path"] == "/library")
        self.assertEqual(artifact["loaded_flat_namespace_providers"], ["/provider"])

    def archive_stage(self, hash_override=None):
        archive_hash = "b" * 64
        root, provenance, lock = self.staged(source="archive-source", revision=archive_hash)
        lock.write_text(json.dumps({"sources": [], "archives": [{"name": "archive-source", "sha256": archive_hash,
            "upstream_revision": "a" * 40}]}))
        data = json.loads(provenance.read_text())
        data["artifacts"][0]["archive_sha256"] = hash_override or archive_hash
        provenance.write_text(json.dumps(data))
        return root, provenance, lock

    def test_accepts_matching_source_archive_identity(self):
        report = runtime.inspect(*self.archive_stage())
        self.assertFalse(any("unpinned source revision" in e or "source archive hash" in e for e in report["errors"]))

    def test_rejects_mismatched_source_archive_hash(self):
        report = runtime.inspect(*self.archive_stage(hash_override="c" * 64))
        self.assertTrue(any("source archive hash missing or mismatched" in e for e in report["errors"]))


if __name__ == "__main__":
    unittest.main()
