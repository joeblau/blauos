"""All inputs in this suite are deliberately synthetic, never M1 captures."""
import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location("m1_handoff_test_decoder", ROOT / "platform/apple-m1/handoff/decoder.py")
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)

PHYS = 0x10000000
VIRT = 0xffff000000000000


def node(name, properties=(), children=()):
    values = [("name", name.encode() + b"\0"), *properties]
    payload = struct.pack("<II", len(values), len(children))
    for key, value in values:
        payload += struct.pack("<32sI", key.encode() + b"\0", len(value)) + value + b"\0" * ((-len(value)) & 3)
    return payload + b"".join(children)


def tree(memory_properties=None):
    if memory_properties is None:
        memory_properties = [("BootKC", struct.pack("<QQ", PHYS + 0x4000, 0x8000))]
    return node("device-tree", children=[node("chosen", children=[node("memory-map", memory_properties)])])


def boot_bytes(profile="apple-xnu-12377", adt=None):
    adt = tree() if adt is None else adt
    count, size = (1024, 1152) if profile == "apple-xnu-12377" else (608, 736)
    raw = bytearray(size)
    struct.pack_into("<HH", raw, 0, 2, 2)
    struct.pack_into("<4Q", raw, 8, VIRT, PHYS, 0x200000, PHYS + 0x40000)
    struct.pack_into("<Q", raw, 96, VIRT + 0x20000)
    struct.pack_into("<I", raw, 104, len(adt))
    command = b"synthetic_fixture=1\0"
    raw[108:108 + len(command)] = command
    struct.pack_into("<QQ", raw, (108 + count + 7) & ~7, 1, 0x400000)
    return bytes(raw)


def plan():
    return {"schema_version": 1, "page_size": 16384,
            "ram": [{"name": "synthetic-ram", "base": PHYS, "size": 0x200000}],
            "reservations": [{"name": "synthetic-firmware", "base": PHYS + 0x80000, "size": 0x4000}],
            "boot_args_phys": PHYS + 0x18000, "device_tree_pointer_space": "kernel_virtual"}


class DecoderTests(unittest.TestCase):
    def test_contract_and_decoder_profiles_match(self):
        contract = json.loads((ROOT / "platform/apple-m1/handoff/contract.json").read_text())
        self.assertEqual(set(contract["profiles"]), set(d.PROFILES))
        for name, layout in d.PROFILES.items():
            self.assertEqual(layout["size"], contract["profiles"][name]["boot_args_size"])
            self.assertEqual(layout["command_line_size"], contract["profiles"][name]["command_line_size"])

    def test_two_distinct_source_abis(self):
        for profile in d.PROFILES:
            decoded = d.parse_boot_args(boot_bytes(profile), profile)
            self.assertEqual(decoded["bootFlags"], 1)
            self.assertEqual(decoded["memSizeActual"], 0x400000)
            self.assertEqual(decoded["commandLine"], "synthetic_fixture=1")

    def test_wrong_profile_rejected(self):
        with self.assertRaises(d.HandoffError):
            d.parse_boot_args(boot_bytes("puredarwin-xnu-7195"), "apple-xnu-12377")

    def test_unknown_profile_and_extra_boot_args_rejected(self):
        for raw, profile in ((boot_bytes(), "unknown"), (boot_bytes() + b"\0", "apple-xnu-12377")):
            with self.assertRaises(d.HandoffError): d.parse_boot_args(raw, profile)

    def test_boot_args_every_truncation(self):
        raw = boot_bytes()
        for end in range(len(raw)):
            with self.assertRaises(d.HandoffError): d.parse_boot_args(raw[:end], "apple-xnu-12377")

    def test_version_and_revision_rejected(self):
        for offset in (0, 2):
            raw = bytearray(boot_bytes()); struct.pack_into("<H", raw, offset, 3)
            with self.assertRaises(d.HandoffError): d.parse_boot_args(raw, "apple-xnu-12377")

    def test_command_line_must_terminate(self):
        raw = bytearray(boot_bytes()); raw[108:1132] = b"X" * 1024
        with self.assertRaises(d.HandoffError): d.parse_boot_args(raw, "apple-xnu-12377")

    def test_boot_memory_overflow(self):
        raw = bytearray(boot_bytes()); struct.pack_into("<Q", raw, 16, (1 << 64) - 1)
        with self.assertRaises(d.HandoffError): d.parse_boot_args(raw, "apple-xnu-12377")

    def test_top_of_kernel_outside_ram(self):
        raw = bytearray(boot_bytes()); struct.pack_into("<Q", raw, 32, PHYS - 1)
        with self.assertRaises(d.HandoffError): d.parse_boot_args(raw, "apple-xnu-12377")

    def test_zero_and_oversized_device_tree(self):
        for size in (0, 0xffffffff):
            raw = bytearray(boot_bytes()); struct.pack_into("<I", raw, 104, size)
            with self.assertRaises(d.HandoffError): d.parse_boot_args(raw, "apple-xnu-12377")

    def test_adt_value_padding(self):
        decoded = d.parse_adt(node("device-tree", [("one-byte", b"X")]))
        self.assertEqual(decoded["properties"]["one-byte"], b"X")

    def test_adt_every_truncation(self):
        raw = tree()
        for end in range(len(raw)):
            with self.assertRaises(d.HandoffError): d.parse_adt(raw[:end])

    def test_impossible_property_count(self):
        raw = bytearray(tree()); struct.pack_into("<I", raw, 0, 0xffffffff)
        with self.assertRaises(d.HandoffError): d.parse_adt(raw)

    def test_declared_property_length_and_child_count(self):
        for offset in (4, 40):
            raw = bytearray(tree()); struct.pack_into("<I", raw, offset, 0xffffffff)
            with self.assertRaises(d.HandoffError): d.parse_adt(raw)

    def test_big_endian_fdt_header_rejected(self):
        raw = struct.pack(">10I", 0xd00dfeed, 40, 40, 40, 40, 17, 16, 0, 0, 0)
        with self.assertRaises(d.HandoffError): d.parse_adt(raw)

    def test_unterminated_property_name(self):
        raw = bytearray(tree()); raw[8:40] = b"X" * 32
        with self.assertRaises(d.HandoffError): d.parse_adt(raw)

    def test_duplicate_property_and_siblings(self):
        for raw in (node("device-tree", [("name", b"duplicate\0")]),
                    node("device-tree", children=[node("same"), node("same")])):
            with self.assertRaises(d.HandoffError): d.parse_adt(raw)

    def test_missing_name(self):
        raw = struct.pack("<II32sI", 1, 0, b"other\0", 0)
        with self.assertRaises(d.HandoffError): d.parse_adt(raw)

    def test_depth_limit(self):
        raw = node("leaf")
        for _ in range(66): raw = node("branch", children=[raw])
        with self.assertRaises(d.HandoffError): d.parse_adt(raw)

    def test_trailing_data_rejected(self):
        with self.assertRaises(d.HandoffError): d.parse_adt(tree() + b"\0\0\0\0")

    def check_memory(self, memory_plan=None, raw_tree=None, boot=None):
        raw_tree = tree() if raw_tree is None else raw_tree
        boot = d.parse_boot_args(boot_bytes(adt=raw_tree), "apple-xnu-12377") if boot is None else boot
        return d.validate_memory_plan(boot, d.parse_adt(raw_tree), plan() if memory_plan is None else memory_plan, len(raw_tree))

    def test_valid_plan_does_not_derive_free_memory(self):
        self.assertFalse(self.check_memory()["free_memory_derived"])

    def test_zero_overflow_and_boolean_ranges(self):
        for base, size in ((0, 0), ((1 << 64) - 1, 1), (True, 4096), (-1, 4096)):
            with self.assertRaises(d.HandoffError): d.interval(base, size, "synthetic")

    def test_missing_and_unsupported_memory_map(self):
        for raw in (node("device-tree"), tree([("BadEncoding", b"12345678")])):
            with self.assertRaises(d.HandoffError): self.check_memory(raw_tree=raw)

    def test_reservation_overlap(self):
        value = plan(); value["reservations"].append({"name": "overlap", "base": PHYS + 0x4000, "size": 0x4000})
        with self.assertRaises(d.HandoffError): self.check_memory(value)

    def test_explicit_containment_overlap(self):
        value = plan(); value["reservations"].append({"name": "container", "base": PHYS + 0x4000, "size": 0x10000})
        value["allowed_containment_overlaps"] = [["container", "adt:BootKC"]]
        self.check_memory(value)

    def test_partial_overlap_cannot_be_waived(self):
        value = plan(); value["reservations"].append({"name": "partial", "base": PHYS + 0x8000, "size": 0x10000})
        value["allowed_containment_overlaps"] = [["partial", "adt:BootKC"]]
        with self.assertRaises(d.HandoffError): self.check_memory(value)

    def test_ram_overlap(self):
        value = plan(); value["ram"].append({"name": "overlapping-ram", "base": PHYS, "size": 0x4000})
        with self.assertRaises(d.HandoffError): self.check_memory(value)

    def test_reservation_outside_ram(self):
        value = plan(); value["reservations"][0]["base"] = PHYS + 0x400000
        with self.assertRaises(d.HandoffError): self.check_memory(value)

    def test_pointer_space_is_explicit(self):
        value = plan(); del value["device_tree_pointer_space"]
        with self.assertRaises(d.HandoffError): self.check_memory(value)

    def test_virtual_pointer_outside_window(self):
        boot = d.parse_boot_args(boot_bytes(), "apple-xnu-12377"); boot["deviceTreeP"] = VIRT - 1
        with self.assertRaises(d.HandoffError): self.check_memory(boot=boot)

    def test_adt_length_mismatch(self):
        boot = d.parse_boot_args(boot_bytes(), "apple-xnu-12377"); boot["deviceTreeLength"] += 4
        with self.assertRaises(d.HandoffError): self.check_memory(boot=boot)

    def test_unknown_allowance_and_bad_schema(self):
        value = plan(); value["allowed_containment_overlaps"] = [["unknown", "adt:BootKC"]]
        with self.assertRaises(d.HandoffError): self.check_memory(value)
        value = plan(); value["schema_version"] = True
        with self.assertRaises(d.HandoffError): self.check_memory(value)

    def test_alignment(self):
        value = plan(); value["ram"][0]["size"] += 1
        with self.assertRaises(d.HandoffError): self.check_memory(value)

    def test_explicit_physical_pointer_space(self):
        value = plan(); value["device_tree_pointer_space"] = "physical"
        boot = d.parse_boot_args(boot_bytes(), "apple-xnu-12377"); boot["deviceTreeP"] = PHYS + 0x20000
        self.assertEqual(self.check_memory(value, boot=boot)["reservations"][2]["base"], PHYS + 0x20000)

    def test_memory_plan_count_limit(self):
        value = plan()
        value["reservations"] = [{"name": str(index), "base": PHYS + index * 16384, "size": 16384}
                                 for index in range(d.MAX_MEMORY_RANGES)]
        with self.assertRaisesRegex(d.HandoffError, "range limit"):
            self.check_memory(value)

    def test_cli_retains_hardware_false(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "args.bin").write_bytes(boot_bytes())
            (folder / "adt.bin").write_bytes(tree())
            (folder / "plan.json").write_text(json.dumps(plan()))
            command = [sys.executable, str(ROOT / "scripts/platform/inspect_m1_handoff.py"),
                       "--profile", "apple-xnu-12377", "--boot-args", str(folder / "args.bin"),
                       "--adt", str(folder / "adt.bin"), "--memory-plan", str(folder / "plan.json"),
                       "--report", str(folder / "report.json"), "--synthetic-fixture"]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads((folder / "report.json").read_text())
            self.assertEqual(report["status"], "offline_validation_pass")
            self.assertTrue(report["synthetic_fixture"])
            for name in ("physical_execution_verified", "mmu_verified", "exceptions_verified"):
                self.assertFalse(report[name])
            (folder / "adt.bin").write_bytes(tree()[:-1])
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 1)
            self.assertEqual(json.loads((folder / "report.json").read_text())["status"], "failed")


if __name__ == "__main__":
    unittest.main()
