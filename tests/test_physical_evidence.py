"""Synthetic protocol fixtures test rejection guards, never physical boot proof."""
import copy
import importlib.util
import json
from pathlib import Path
import plistlib
import struct
import tempfile
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("physical_evidence", ROOT / "scripts/platform/physical_evidence.py")
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)


class PhysicalEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest_path = self.root / "manifest.json"
        self.revision = "ac9718fb1af618d5ce8678d0dc6e8a58f252216f"
        self.profile = dict(evidence.PROFILE, revision=self.revision)
        self.lock = self.root / "trusted-lock.json"
        self.lock.write_text(json.dumps({"schema_version": 1, "sources": [{"name": "xnu", "revision": self.revision}]}))
        self.identity_nonce = "01" * 32
        self.boot_nonce = "02" * 32
        self.private = [{"model": b"Macmini9,1\0", "IOPlatformUUID": "SYNTHETIC-PRIVATE-UUID",
                         "IOPlatformSerialNumber": "SYNTHETIC-PRIVATE-SERIAL"}]
        lab = evidence.module(ROOT / "scripts/platform/lab_inventory.py", "physical_test_lab")
        self.fingerprint = lab.identity_fingerprint(self.private[0], self.identity_nonce)
        private_bytes = plistlib.dumps(self.private)
        self.private_path = self.root / "private.plist"
        self.private_path.write_bytes(private_bytes)
        inventory = {"schema_version": 1, "role": "test-machine", "read_only": True,
                     "hardware": {"model": "Macmini9,1", "chip": "Apple M1", "architecture": "arm64",
                                  "memory_bytes": 16 * 1024 ** 3, "logical_cpus": 8,
                                  "target_type": "J274", "board_compatible": "J274AP\0Macmini9,1\0AppleARM",
                                  "firmware_version": "SYNTHETIC-UNTESTED"},
                     "identity": {"scheme": evidence.IDENTITY_SCHEME, "nonce": self.identity_nonce,
                                  "fingerprint_sha256": self.fingerprint,
                                  "private_capture_sha256": evidence.sha(private_bytes)}}
        inputs = [dict(self.write("checkpoint.c", b"synthetic fixture; not compiled native instrumentation"),
                       role="instrumentation", label="checkpoint.c"),
                  dict(self.write("recipe.py", b"synthetic fixture; not executed build recipe"),
                       role="recipe", label="recipe.py")]
        identities = [{k: item[k] for k in ("label", "role", "sha256")} for item in inputs]
        self.build_id = evidence.sha(evidence.canonical({"source_profile": self.profile,
                                                        "build_inputs": sorted(identities, key=lambda item: item["label"])}))
        self.kernel_uuid = "a03c1b28-aaf7-4cbd-8e50-3ed00a996d70"
        self.vm = 0xFFFFFE0007000000
        self.kernel = self.macho()
        self.entry = {"build_id": self.build_id, "kernel_uuid": self.kernel_uuid,
                      "model": "Macmini9,1", "soc": "t8103", "identity_fingerprint_sha256": self.fingerprint,
                      "execution_environment": "native-iboot", "slide": "0x4000", "pc": hex(self.vm + 0x4300)}
        self.manifest = {
            "schema_version": 1, "evidence_kind": "physical-xnu-checkpoint", "source_profile": self.profile,
            "source_lock": self.write("lock.json", self.lock.read_bytes()),
            "inventory": self.write_json("inventory.json", inventory), "build_id": self.build_id,
            "build_inputs": inputs, "kernel": self.write("kernel", self.kernel),
            "build_receipt": self.write_json("build.json", {"status": "passed", "exit_code": 0,
                               "source_profile": self.profile, "build_id": self.build_id,
                               "kernel_sha256": evidence.sha(self.kernel)}),
            "boot_object": self.write("boot-object", b"SYNTHETIC-BOOT-OBJECT"),
            "package_receipt": self.write_json("package.json", {"status": "passed",
                                  "boot_object_sha256": evidence.sha(b"SYNTHETIC-BOOT-OBJECT"),
                                  "included_kernel_sha256": evidence.sha(self.kernel), "kernel_uuid": self.kernel_uuid}),
            "boots": [{"nonce": self.boot_nonce, "mode": "cold",
                       "serial": self.serial(self.entry),
                       "authorization_receipt": self.write_json("authorization.json", {
                           "schema_version": 1, "environment": "paired-1tr", "security_mode": "permissive",
                           "exit_code": 0, "boot_object_sha256": evidence.sha(b"SYNTHETIC-BOOT-OBJECT"),
                           "policy_after": {"coih_sha384": "03" * 48},
                           "transcript": self.write("authorization.txt", b"synthetic authorization fixture"),
                           "policy_capture": self.write("policy.json", b'{"synthetic_fixture":true}')})}]}
        self.capture()

    def write(self, name, data):
        path = self.root / name
        path.write_bytes(data)
        return {"path": name, "sha256": evidence.sha(data)}

    def write_json(self, name, data):
        return self.write(name, evidence.canonical(data))

    def serial(self, entry, nonce=None):
        line = "BLAUOS_PHYSICAL " + (nonce or self.boot_nonce) + " entry " + json.dumps(entry) + "\n"
        record = self.write("serial.log", line.encode())
        if hasattr(self, "manifest"):
            self.capture()
        return record

    def capture(self):
        data = (self.root / "serial.log").read_bytes()
        record = self.capture_record(data, "capture.json")
        self.manifest["boots"][0]["capture_receipt"] = record
        return record

    def capture_record(self, data, filename):
        receipt = {"schema_version": 1, "transport": "native-uart", "device": "/dev/cu.synthetic-fixture",
                   "baud": 115200, "format": "8N1", "application_bytes_sent": 0, "reset_commands_sent": 0,
                   "error": None, "physical_boot_validated": False, "stop_reason": "duration-limit",
                   "collector_sha256": evidence.sha((ROOT / "scripts/platform/capture_serial.py").read_bytes()),
                   "serial": {"path": "serial.log", "bytes": len(data), "sha256": evidence.sha(data)},
                   "chunks": [{"offset": 0, "length": len(data), "received_at": "2026-10-10T12:00:00+00:00",
                               "monotonic_ns": 123456, "sha256": evidence.sha(data)}]}
        return self.write_json(filename, receipt)

    def checkpoint_bundle(self, event_change=None):
        """Four fully synthetic sessions exercise the proposed measured protocol."""
        def node(properties, children=()):
            result = struct.pack("<II", len(properties), len(children))
            for name, value in properties.items():
                result += struct.pack("<32sI", name.encode(), len(value)) + value + b"\0" * ((-len(value)) & 3)
            return result + b"".join(children)
        physical = 0x800000000
        tree = node({"name": b"device-tree\0", "model": b"Macmini9,1\0",
                     "compatible": b"J274AP\0Macmini9,1\0AppleARM\0"}, [
            node({"name": b"chosen\0", "chip-id": struct.pack("<I", 0x8103)}, [
                node({"name": b"memory-map\0", "Kernel": struct.pack("<QQ", physical + 0x40000, 0x4000)})])])
        plan = {"schema_version": 1, "page_size": 16384,
                "ram": [{"name": "dram", "base": physical, "size": 1024 ** 3}],
                "reservations": [{"name": "firmware", "base": physical + 0x50000, "size": 0x4000}],
                "boot_args_phys": physical + 0x10000, "device_tree_pointer_space": "physical"}
        manifest = copy.deepcopy(self.manifest)
        manifest["boots"] = []
        for index, mode in enumerate(("cold", "cold", "warm", "warm")):
            nonce = f"{index + 16:02x}" * 32
            boot_args = bytearray(1152)
            struct.pack_into("<HH", boot_args, 0, 2, 2)
            struct.pack_into("<4Q", boot_args, 8, 0xFFFFFE0000000000, physical, 1024 ** 3, physical + 0x10000)
            struct.pack_into("<Q", boot_args, 96, physical + 0x20000)
            struct.pack_into("<I", boot_args, 104, len(tree))
            command = ("blauos-checkpoint-nonce=" + nonce).encode()
            boot_args[108:108 + len(command)] = command
            struct.pack_into("<QQ", boot_args, 1136, 0, 16 * 1024 ** 3)
            boot = {"nonce": nonce, "mode": mode,
                    "authorization_receipt": self.manifest["boots"][0]["authorization_receipt"],
                    "boot_args": self.write(f"args-{index}.bin", bytes(boot_args)),
                    "device_tree": self.write(f"adt-{index}.bin", tree),
                    "memory_plan": self.write_json(f"memory-{index}.json", plan)}
            events = {"entry": self.entry,
                      "handoff": {"profile": "apple-xnu-12377", "boot_args_sha256": boot["boot_args"]["sha256"],
                                  "device_tree_sha256": boot["device_tree"]["sha256"],
                                  "memory_plan_sha256": boot["memory_plan"]["sha256"]},
                      "mmu": {"current_el": 1, "page_size": 16384, "sctlr_el1": "0x1", "tcr_el1": "0x40008000",
                              "ttbr0_el1": "0x800030000", "ttbr1_el1": "0x800034000", "translation_readback": "matched"},
                      "exception": {"kind": "controlled-brk", "esr_el1": "0xf0000000", "elr_el1": "0xfffffe0007000300",
                                    "far_el1": "0x0", "recovered": True},
                      "timer": {"frequency_hz": 24000000, "counter_before": 100, "counter_after": 101, "delivered": 1},
                      "interrupt": {"controller": "apple-aic", "irq": 1, "handled_count": 1},
                      "cpus": {"boot_cpu": 0, "online": list(range(8))}, "checkpoint": {"status": "controlled-stop"}}
            if event_change:
                event, changes = event_change
                events[event] = dict(events[event], **changes)
            serial = "".join("BLAUOS_PHYSICAL " + nonce + " " + event + " " + json.dumps(value) + "\n"
                             for event, value in events.items()).encode()
            boot["serial"] = self.write(f"serial-{index}.log", serial)
            boot["capture_receipt"] = self.capture_record(serial, f"capture-{index}.json")
            manifest["boots"].append(boot)
        return manifest

    def macho(self, subtype=2, marker=True, symbols=True):
        data = bytearray(4096)
        struct.pack_into("<8I", data, 0, 0xFEEDFACF, 0x100000C, subtype, 2, 3, 200, 0, 0)
        struct.pack_into("<II16s4Q4I", data, 32, 0x19, 152, b"__TEXT", self.vm, 4096, 0, 4096, 5, 5, 1, 0)
        struct.pack_into("<16s16s2Q8I", data, 104, b"__text", b"__TEXT", self.vm + 0x300, 0x100,
                         0x300, 2, 0, 0, 0x80000400, 0, 0, 0)
        struct.pack_into("<II16s", data, 184, 0x1B, 24, uuid.UUID(self.kernel_uuid).bytes)
        names = ("\0" + evidence.CHECKPOINT_START + "\0" + evidence.CHECKPOINT_END + "\0").encode()
        struct.pack_into("<6I", data, 208, 2, 24, 0x600, 2 if symbols else 0, 0x700, len(names))
        if symbols:
            struct.pack_into("<IBBHQ", data, 0x600, 1, 0xF, 1, 0, self.vm + 0x300)
            struct.pack_into("<IBBHQ", data, 0x610, len(evidence.CHECKPOINT_START) + 2, 0xF, 1, 0, self.vm + 0x310)
        data[0x700:0x700 + len(names)] = names
        if marker:
            value = b"BLAUOS_PHYSICAL_BUILD_ID=" + self.build_id.encode() + b"\0"
            data[0x500:0x500 + len(value)] = value
        return bytes(data)

    def validate(self, manifest=None, **kwargs):
        self.manifest_path.write_text(json.dumps(manifest or self.manifest))
        return evidence.validate(self.manifest_path, source_lock=self.lock, **kwargs)

    def assert_rejected(self, report, text):
        self.assertFalse(report["evidence_consistency_passed"])
        self.assertFalse(report["physical_gate_passed"])
        self.assertIn(text, " ".join(report["errors"]))

    def test_complete_entry_format_is_consistent_but_never_physical_attestation(self):
        report = self.validate(private_plist=self.private_path)
        self.assertEqual(report["errors"], [])
        self.assertTrue(report["kernel_entry_observed"])
        self.assertTrue(report["private_identity_consistency_verified"])
        self.assertFalse(report["hardware_attestation_verified"])
        self.assertFalse(report["physical_gate_passed"])
        self.assertTrue(report["review_required"])
        self.assertNotIn("SYNTHETIC-PRIVATE", json.dumps(report))

    def test_private_identity_is_never_read_from_a_public_manifest_path(self):
        self.manifest["private_platform_plist"] = "/path/that/does/not/exist"
        report = self.validate()
        self.assertTrue(report["evidence_consistency_passed"])
        self.assertFalse(report["private_identity_consistency_verified"])
        self.private_path.write_bytes(plistlib.dumps([dict(self.private[0], IOPlatformUUID="OTHER")]))
        self.assert_rejected(self.validate(private_plist=self.private_path), "capture hash mismatch")

    def test_stock_kernel_and_qemu_artifact_cannot_substitute(self):
        for blob, message in ((self.macho(symbols=False), "dedicated checkpoint"),
                              (self.macho(marker=False), "build identity"),
                              (self.macho(subtype=0), "native ARM64e")):
            with self.subTest(message=message):
                changed = copy.deepcopy(self.manifest)
                changed["kernel"] = self.write("kernel", blob)
                self.assert_rejected(self.validate(changed), message)

    def test_wrong_hardware_and_build_host_fail(self):
        for changes in ({"role": "build-host"}, {"hardware": {"model": "Mac15,8", "chip": "Apple M3 Max"}}):
            changed = copy.deepcopy(self.manifest)
            inventory = json.loads((self.root / "inventory.json").read_bytes())
            inventory.update(changes)
            changed["inventory"] = self.write_json("inventory-changed.json", inventory)
            self.assert_rejected(self.validate(changed), "dedicated physical")

    def test_generic_console_strings_and_shell_echo_do_not_pass(self):
        for text in (b"Darwin Kernel Version: XNU booted on M1\n", b"echo BLAUOS_PHYSICAL proof\n"):
            self.write("serial.log", text)
            self.capture()
            changed = copy.deepcopy(self.manifest)
            changed["boots"][0]["serial"] = self.write("serial.log", text)
            self.assert_rejected(self.validate(changed), "no dedicated")

    def test_entry_requires_correct_nonce_symbol_pc_and_native_environment(self):
        cases = [({"pc": "0x1234"}, None, "PC outside"),
                 ({"kernel_uuid": str(uuid.uuid4())}, None, "UUID mismatch"),
                 ({"execution_environment": "qemu"}, None, "not Path A"),
                 ({"identity_fingerprint_sha256": "ff" * 32}, None, "identity mismatch"),
                 ({}, "04" * 32, "stale physical")]
        for changes, nonce, message in cases:
            serial = self.serial(dict(self.entry, **changes), nonce)
            changed = copy.deepcopy(self.manifest)
            changed["boots"][0]["serial"] = serial
            self.assert_rejected(self.validate(changed), message)

    def test_virtual_uart_and_replayed_boots_are_rejected(self):
        changed = copy.deepcopy(self.manifest)
        receipt = json.loads((self.root / "capture.json").read_bytes())
        receipt["transport"] = "m1n1-usb"
        changed["boots"][0]["capture_receipt"] = self.write_json("virtual-capture.json", receipt)
        self.assert_rejected(self.validate(changed), "native UART")
        changed = copy.deepcopy(self.manifest)
        changed["boots"].append(copy.deepcopy(changed["boots"][0]))
        self.assert_rejected(self.validate(changed), "reused boot")

    def test_incomplete_checkpoint_never_promotes_entry_to_hardware_bringup(self):
        self.assert_rejected(self.validate(level="checkpoint"), "measured handoff")

    def test_full_synthetic_protocol_still_requires_physical_review(self):
        report = self.validate(self.checkpoint_bundle(), level="checkpoint")
        self.assertEqual(report["errors"], [])
        self.assertTrue(report["checkpoint_observations_complete"])
        self.assertEqual(len(report["boots"]), 4)
        self.assertFalse(report["hardware_attestation_verified"])
        self.assertFalse(report["physical_gate_passed"])
        for observation in report["boots"]:
            self.assertEqual(observation["checkpoint"]["online_cpus"], 8)

    def test_checkpoint_requires_progress_recovery_hardware_and_cpu_observations(self):
        cases = [("mmu", {"sctlr_el1": "0x0"}, "translation enabled"),
                 ("exception", {"recovered": False}, "controlled BRK"),
                 ("timer", {"counter_after": 99}, "counter progress"),
                 ("interrupt", {"controller": "qemu-gic"}, "native Apple AIC"),
                 ("cpus", {"online": [0]}, "secondary CPUs")]
        for event, change, message in cases:
            with self.subTest(event=event):
                self.assert_rejected(self.validate(self.checkpoint_bundle((event, change)), level="checkpoint"), message)
        manifest = self.checkpoint_bundle()
        manifest["boots"] = manifest["boots"][:3]
        self.assert_rejected(self.validate(manifest, level="checkpoint"), "two warm boots")

    def test_capture_chunk_corruption_and_wrong_collector_fail(self):
        for change, message in (({"sha256": "00" * 32}, "chunk hash"), ({"offset": 1}, "noncontiguous")):
            receipt = json.loads((self.root / "capture.json").read_bytes())
            receipt["chunks"][0].update(change)
            manifest = copy.deepcopy(self.manifest)
            manifest["boots"][0]["capture_receipt"] = self.write_json("bad-capture.json", receipt)
            self.assert_rejected(self.validate(manifest), message)
        receipt["collector_sha256"] = "00" * 32
        manifest["boots"][0]["capture_receipt"] = self.write_json("bad-capture.json", receipt)
        self.assert_rejected(self.validate(manifest), "reviewed collector")

    def test_malformed_json_record_shapes_return_structured_rejection(self):
        self.manifest_path.write_text("[]")
        self.assert_rejected(evidence.validate(self.manifest_path), "manifest: JSON object")
        for field, replacement in (("inventory", []), ("build_receipt", []), ("package_receipt", [])):
            manifest = copy.deepcopy(self.manifest)
            manifest[field] = self.write_json("bad-record.json", replacement)
            self.assert_rejected(self.validate(manifest), "JSON object")
        for field in ("hardware", "identity"):
            manifest = copy.deepcopy(self.manifest)
            inventory = json.loads((self.root / "inventory.json").read_bytes())
            inventory[field] = []
            manifest["inventory"] = self.write_json("bad-inventory.json", inventory)
            self.assert_rejected(self.validate(manifest), "JSON object")
        for changed in ({"serial": []}, {"chunks": [None]}):
            manifest = copy.deepcopy(self.manifest)
            receipt = json.loads((self.root / "capture.json").read_bytes())
            receipt.update(changed)
            manifest["boots"][0]["capture_receipt"] = self.write_json("bad-capture.json", receipt)
            self.assert_rejected(self.validate(manifest), "JSON object")
        manifest = copy.deepcopy(self.manifest)
        authorization = json.loads((self.root / "authorization.json").read_bytes())
        authorization["policy_after"] = []
        manifest["boots"][0]["authorization_receipt"] = self.write_json("bad-authorization.json", authorization)
        self.assert_rejected(self.validate(manifest), "JSON object")

    def test_source_profile_and_receipt_bindings_are_required(self):
        changed = copy.deepcopy(self.manifest)
        changed["source_profile"]["board_config"] = "VIRT"
        self.assert_rejected(self.validate(changed), "native T8103")
        (self.root / "checkpoint.c").write_bytes(b"changed source after build")
        self.assert_rejected(self.validate(), "file hash mismatch")

    def test_path_traversal_symlink_escape_and_duplicate_json_keys_fail(self):
        changed = copy.deepcopy(self.manifest)
        changed["inventory"]["path"] = "../inventory.json"
        self.assert_rejected(self.validate(changed), "relative bundle")
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "inventory.json"
            target.write_bytes((self.root / "inventory.json").read_bytes())
            (self.root / "escape.json").symlink_to(target)
            changed["inventory"]["path"] = "escape.json"
            self.assert_rejected(self.validate(changed), "escapes evidence")
        with self.assertRaisesRegex(evidence.EvidenceError, "duplicate"):
            evidence.load_json('{"pc":1,"pc":2}')


if __name__ == "__main__":
    unittest.main()
