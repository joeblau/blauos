"""Reject malformed metadata and missing native provenance; no boot simulation."""

import importlib.util
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

BUILD_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts/build"
sys.path.insert(0, str(BUILD_SCRIPTS))
spec = importlib.util.spec_from_file_location("native_m1", BUILD_SCRIPTS / "native_m1.py")
native_m1 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native_m1)


def metadata_fixture(*commands, subtype=2, filetype=2):
    body = b"".join(commands)
    return struct.pack("<8I", 0xFEEDFACF, 0x0100000C, subtype, filetype,
                       len(commands), len(body), 0, 0) + body


def synthetic_fileset(filesize=0):
    """Metadata fixture, never an executable project or physical boot artifact."""
    segment = struct.pack("<II16sQQQQiiII", 0x19, 72, b"__TEXT", 0x1000,
                          0x4000, 128, filesize, 5, 5, 0, 0)
    state = bytearray(272)
    struct.pack_into("<Q", state, 32 * 8, 0x1000)
    thread = struct.pack("<IIII", 5, 288, 6, 68) + state
    unique = struct.pack("<II", 0x1B, 24) + bytes(16)
    kernel = metadata_fixture(segment, thread, unique)
    entry = struct.pack("<IIQQII", 0x80000035, 56, 0x1000, 128, 32, 0)
    entry += b"com.apple.kernel\0".ljust(24, b"\0")
    outer = metadata_fixture(entry, filetype=12)
    return outer.ljust(128, b"\0") + kernel


class NativeMetadataTests(unittest.TestCase):
    def test_arm64_and_arm64e_are_distinct(self):
        self.assertEqual(native_m1.mach_header(metadata_fixture())["architecture"], "arm64e")
        self.assertEqual(native_m1.mach_header(metadata_fixture(subtype=0))["architecture"], "arm64")

    def test_truncated_header_and_command_are_rejected(self):
        with self.assertRaises(ValueError):
            native_m1.mach_header(b"\xcf\xfa\xed\xfe")
        with self.assertRaises(ValueError):
            native_m1.mach_header(metadata_fixture(struct.pack("<II", 0x1B, 24)))

    def test_zero_size_command_cannot_loop_or_pass(self):
        with self.assertRaises(ValueError):
            native_m1.mach_header(metadata_fixture(struct.pack("<II", 0x19, 0)))

    def test_uuid_decoded_from_actual_load_command_bytes(self):
        command = struct.pack("<II", 0x1B, 24) + bytes.fromhex("0123456789abcdef0123456789abcdef")
        self.assertEqual(native_m1.mach_header(metadata_fixture(command))["uuid"],
                         "01234567-89AB-CDEF-0123-456789ABCDEF")

    def test_duplicate_uuid_rejected(self):
        command = struct.pack("<II", 0x1B, 24) + bytes(16)
        with self.assertRaises(ValueError):
            native_m1.mach_header(metadata_fixture(command, command))

    def test_segment_file_range_cannot_escape_input(self):
        command = struct.pack("<II16sQQQQiiII", 0x19, 72, b"__TEXT", 0x1000,
                              0x4000, (1 << 64) - 32, 64, 5, 5, 0, 0)
        with self.assertRaises(ValueError):
            native_m1.mach_header(metadata_fixture(command))

    def test_virtual_segment_overflow_rejected(self):
        command = struct.pack("<II16sQQQQiiII", 0x19, 72, b"__TEXT", (1 << 64) - 16,
                              32, 0, 0, 5, 5, 0, 0)
        with self.assertRaises(ValueError):
            native_m1.mach_header(metadata_fixture(command))

    def test_command_count_mismatch_rejected(self):
        data = bytearray(metadata_fixture(struct.pack("<II", 0x99, 8)))
        struct.pack_into("<I", data, 16, 0)
        with self.assertRaises(ValueError):
            native_m1.mach_header(bytes(data))

    def test_out_of_file_fileset_entry_rejected(self):
        command = struct.pack("<IIQQII", 0x80000035, 40, 0, 4096, 32, 0) + b"kernel\0\0"
        with self.assertRaises(ValueError):
            native_m1.mach_header(metadata_fixture(command, filetype=12))

    def test_unixthread_state_cannot_read_outside_command(self):
        command = struct.pack("<IIII", 5, 16, 6, 68)
        with self.assertRaises(ValueError):
            native_m1.mach_header(metadata_fixture(command))

    def test_zero_file_size_segment_cannot_supply_entry_instructions(self):
        with tempfile.TemporaryDirectory() as name:
            artifact = Path(name) / "synthetic-fileset-metadata"
            artifact.write_bytes(synthetic_fileset())
            report = native_m1.inspect(artifact, None, Path(name) / "unused-lock")
            self.assertIn("Kernel entry PC is outside file-backed executable segments",
                          report["candidate_check_errors"])
            self.assertFalse(report["native_boot_assets_ready"])

    def test_asserted_t8103_receipt_without_effective_recipe_rejected(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            artifact = directory / "synthetic-fileset-metadata"
            artifact.write_bytes(synthetic_fileset(filesize=416))
            manifest = directory / "lock.json"
            manifest.write_text(json.dumps({"sources": [{"name": "xnu", "revision": "a" * 40,
                                                        "tree": "b" * 40}]}))
            receipt = directory / "receipt.json"
            receipt.write_text(json.dumps({"machine": "T8103", "source_revision": "a" * 40,
                "manifest_sha256": native_m1.digest(manifest), "exit_code": 0,
                "artifacts": [{"sha256": native_m1.digest(artifact)}]}))
            report = native_m1.inspect(artifact, receipt, manifest)
            self.assertFalse(report["native_source_provenance_verified"])
            self.assertIn("Effective build recipe is missing", report["candidate_check_errors"])

    def test_duplicate_make_override_rejected_by_effective_recipe(self):
        with tempfile.TemporaryDirectory() as name:
            manifest = Path(name) / "lock.json"
            source = {"name": "xnu", "revision": "a" * 40, "tree": "b" * 40}
            manifest.write_text(json.dumps({"sources": [source]}))
            recipe = {"schema_version": 1, "source_name": "xnu", "source_revision": "a" * 40,
                "source_tree": "b" * 40, "machine": "T8103", "architecture_config": "ARM64",
                "kernel_config": "DEVELOPMENT", "sdk": "/synthetic/sdk", "build_system": "apple_make",
                "manifest_sha256": native_m1.digest(manifest), "effective_source": "/synthetic/source",
                "tool_environment": {"CC": "/synthetic/clang", "CXX": "/synthetic/clang++",
                    "SDKROOT": "/synthetic/sdk", "DEVELOPER_DIR": "/synthetic/developer"},
                "commands": [{"cwd": "/synthetic/source", "exit_code": 0, "raw_log_sha256": "0" * 64,
                    "argv": ["make", "ARCH_CONFIGS=ARM64", "MACHINE_CONFIGS=T8103",
                    "KERNEL_CONFIGS=DEVELOPMENT", "MACHINE_CONFIGS=VIRT", "SRCROOT=/synthetic/source",
                    "SDKROOT=/synthetic/sdk", "SDKROOT_RESOLVED=/synthetic/sdk",
                    "CC=/synthetic/clang", "CXX=/synthetic/clang++"]}]}
            build = {"machine": "T8103", "source_revision": "a" * 40, "exit_code": 0,
                "status": "kernel-built-not-boot-validated",
                "manifest_sha256": native_m1.digest(manifest), "sdk": "/synthetic/sdk",
                "source": "/synthetic/source", "recipe": recipe,
                "recipe_sha256": hashlib.sha256(json.dumps(recipe, sort_keys=True,
                                            separators=(",", ":")).encode()).hexdigest()}
            errors = native_m1.native_recipe_errors(build, source, manifest, "xnu")
            self.assertIn("Effective make setting MACHINE_CONFIGS is not uniquely native", errors)
            # A correct board flag does not authorize alternate source/SDK paths.
            recipe["commands"][0]["argv"].remove("MACHINE_CONFIGS=VIRT")
            recipe["commands"][0]["argv"].append("SRCROOT=/synthetic/other-tree")
            recipe["commands"][0]["argv"].append("SDKROOT=/synthetic/other-sdk")
            recipe["tool_environment"]["MAKEFLAGS"] = "MACHINE_CONFIGS=VIRT"
            errors = native_m1.native_recipe_errors(build, source, manifest, "xnu")
            self.assertIn("Effective make setting SRCROOT is not uniquely native", errors)
            self.assertIn("Effective make setting SDKROOT is not uniquely native", errors)
            self.assertIn("Inherited compiler or make overrides remain in the effective environment", errors)
            self.assertIn("Effective recipe canonical hash does not match", errors)

    def test_metadata_alone_never_claims_native_readiness(self):
        with tempfile.TemporaryDirectory() as name:
            artifact = Path(name) / "metadata-only"
            artifact.write_bytes(metadata_fixture())
            report = native_m1.inspect(artifact, None, Path(name) / "unused-lock.json")
            self.assertFalse(report["native_source_provenance_verified"])
            self.assertFalse(report["native_boot_assets_ready"])
            self.assertFalse(report["physical_execution_verified"])
            self.assertIn("Successful pinned native T8103 build provenance is missing",
                          report["candidate_check_errors"])

    def test_virt_and_stock_receipts_cannot_prove_native_source(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            artifact = directory / "synthetic-metadata-not-a-kernel"
            artifact.write_bytes(metadata_fixture())
            manifest = directory / "lock.json"
            manifest.write_text(json.dumps({"sources": [{"name": "xnu", "revision": "a" * 40}]}))
            for machine, revision in [("VIRT", "a" * 40), ("T8103", "stock-kernel")]:
                receipt = directory / "receipt.json"
                receipt.write_text(json.dumps({"machine": machine, "source_revision": revision,
                    "manifest_sha256": native_m1.digest(manifest), "exit_code": 0,
                    "artifacts": [{"sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}]}))
                report = native_m1.inspect(artifact, receipt, manifest)
                self.assertFalse(report["native_source_provenance_verified"])
                self.assertFalse(report["native_boot_assets_ready"])
                self.assertFalse(report["physical_execution_verified"])


if __name__ == "__main__":
    unittest.main()
