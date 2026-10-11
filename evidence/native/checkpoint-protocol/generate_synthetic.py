#!/usr/bin/env python3
"""Generate a SYNTHETIC BlauOS physical-checkpoint evidence bundle.

Everything here is host-generated and clearly synthetic: no hardware booted,
no UART captured bytes, no authorization ran.  The serial payload format is
the one emitted by patches/native/0017-t8103-physical-checkpoint-protocol.patch,
bound to the ACTUAL built kernel (UUID, checkpoint symbol addresses, build-ID
marker) so that agreement with the offline validator is meaningful.  The
result can only ever yield evidence_consistency_passed with
hardware_attestation_verified=false and physical_gate_passed=false.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import struct
import sys

ROOT = Path(__file__).resolve().parents[3]


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    imported = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(imported)
    return imported


evidence = module(ROOT / "scripts/platform/physical_evidence.py", "synth_physical_evidence")
lab = module(ROOT / "scripts/platform/lab_inventory.py", "synth_lab_inventory")
decoder = module(ROOT / "platform/apple-m1/handoff/decoder.py", "synth_handoff_decoder")

PROFILE = dict(evidence.REVIEWED_PROFILES["puredarwin-xnu-7195"],
               revision="1bbaeb2ee6aa41e6f792a0b1e2943cc295b3b290")
PHYS = 0x800000000
SLIDE = 0x4000


def adt_fixture():
    """Synthetic Apple device tree in the shape the decoder expects."""
    def node(properties, children=()):
        result = struct.pack("<II", len(properties), len(children))
        for name, value in properties.items():
            result += struct.pack("<32sI", name.encode(), len(value)) + value + b"\0" * ((-len(value)) & 3)
        return result + b"".join(children)
    return node({"name": b"device-tree\0", "model": b"Macmini9,1\0",
                 "compatible": b"J274AP\0Macmini9,1\0AppleARM\0"}, [
        node({"name": b"chosen\0", "chip-id": struct.pack("<I", 0x8103)}, [
            node({"name": b"memory-map\0", "Kernel": struct.pack("<QQ", PHYS + 0x40000, 0x4000)})])])


def boot_args_fixture(nonce, adt_length):
    """Synthetic puredarwin-xnu-7195 Revision 2 / Version 2 boot_args (736 bytes)."""
    data = bytearray(decoder.PROFILES["puredarwin-xnu-7195"]["size"])
    struct.pack_into("<HH", data, 0, 2, 2)
    struct.pack_into("<4Q", data, 8, 0xFFFFFE0000000000, PHYS, 1024 ** 3, PHYS + 0x40000)
    struct.pack_into("<Q", data, 96, PHYS + 0x20000)
    struct.pack_into("<I", data, 104, adt_length)
    command = ("blauos-checkpoint-nonce=" + nonce).encode()
    data[108:108 + len(command)] = command
    struct.pack_into("<QQ", data, 720, 0, 1024 ** 3)
    return bytes(data)


def main():
    kernel_path = Path(sys.argv[1])
    bundle = Path(sys.argv[2])
    if bundle.exists():
        raise SystemExit(f"refusing to overwrite existing bundle: {bundle}")
    bundle.mkdir(parents=True)

    def write(name, data):
        (bundle / name).write_bytes(data)
        return {"path": name, "sha256": hashlib.sha256(data).hexdigest()}

    def write_json(name, value):
        return write(name, evidence.canonical(value))

    # Synthetic lab inventory of the dedicated target shape; identity
    # fingerprint uses the reviewed nonce-bound scheme with synthetic private
    # values that never leave this host-generated bundle.
    identity_nonce = "aa" * 32
    private = [{"model": b"Macmini9,1\0", "IOPlatformUUID": "SYNTHETIC-PRIVATE-UUID",
                "IOPlatformSerialNumber": "SYNTHETIC-PRIVATE-SERIAL"}]
    fingerprint = lab.identity_fingerprint(private[0], identity_nonce)
    inventory = {"schema_version": 1, "role": "test-machine", "read_only": True,
                 "hardware": {"model": "Macmini9,1", "chip": "Apple M1", "architecture": "arm64",
                              "memory_bytes": 16 * 1024 ** 3, "logical_cpus": 8,
                              "target_type": "J274", "board_compatible": "J274AP\0Macmini9,1\0AppleARM",
                              "firmware_version": "SYNTHETIC-UNTESTED"},
                 "identity": {"scheme": evidence.IDENTITY_SCHEME, "nonce": identity_nonce,
                              "fingerprint_sha256": fingerprint,
                              "private_capture_sha256": hashlib.sha256(b"synthetic-not-captured").hexdigest()}}

    inputs = []
    for label, role, source in (("0017-t8103-physical-checkpoint-protocol.patch", "instrumentation",
                                 ROOT / "patches/native/0017-t8103-physical-checkpoint-protocol.patch"),
                                ("scripts/build/xnu.py", "recipe", ROOT / "scripts/build/xnu.py")):
        record = write(label.replace("/", "_"), source.read_bytes())
        inputs.append(dict(record, role=role, label=label))
    identities = sorted(({"label": i["label"], "role": i["role"], "sha256": i["sha256"]} for i in inputs),
                        key=lambda i: i["label"])
    build_id = evidence.sha(evidence.canonical({"source_profile": PROFILE, "build_inputs": identities}))

    kernel_bytes = kernel_path.read_bytes()
    kernel = evidence.inspect_kernel(kernel_bytes, build_id)  # raises unless labels/marker are right
    kernel_ref = write("kernel.development.t8103.unstripped", kernel_bytes)

    receipt = {"status": "passed", "exit_code": 0, "source_profile": PROFILE, "build_id": build_id,
               "kernel_sha256": kernel_ref["sha256"],
               "note": "SYNTHETIC host-written receipt mirroring the real build result; see "
                       "evidence/native/kernel/puredarwin-t8103-native-17/result.json for the actual recipe receipt"}
    boot_object = b"SYNTHETIC-BOOT-OBJECT-NOT-A-REAL-KERNELCOLLECTION"
    package = {"status": "passed", "boot_object_sha256": evidence.sha(boot_object),
               "included_kernel_sha256": kernel_ref["sha256"], "kernel_uuid": kernel["uuid"],
               "note": "SYNTHETIC host-written receipt; no packaging tool ran"}

    pc = kernel["checkpoint_start"] + SLIDE
    entry = {"build_id": build_id, "kernel_uuid": kernel["uuid"], "model": "Macmini9,1",
             "soc": "t8103", "identity_fingerprint_sha256": fingerprint,
             "execution_environment": "native-iboot", "slide": hex(SLIDE), "pc": hex(pc)}

    def capture_record(serial_bytes, name):
        receipt = {"schema_version": 1, "transport": "native-uart", "device": "/dev/cu.synthetic-fixture",
                   "baud": 115200, "format": "8N1", "application_bytes_sent": 0, "reset_commands_sent": 0,
                   "error": None, "physical_boot_validated": False, "stop_reason": "duration-limit",
                   "collector_sha256": evidence.sha((ROOT / "scripts/platform/capture_serial.py").read_bytes()),
                   "serial": {"path": name, "bytes": len(serial_bytes), "sha256": evidence.sha(serial_bytes)},
                   "chunks": [{"offset": 0, "length": len(serial_bytes), "received_at": "2026-10-10T12:00:00+00:00",
                               "monotonic_ns": 123456, "sha256": evidence.sha(serial_bytes)}],
                   "note": "SYNTHETIC receipt in the passive-collector shape; nothing was captured"}
        return write_json(name.replace(".log", ".capture.json"), receipt)

    def authorization(name):
        record = {"schema_version": 1, "environment": "paired-1tr", "security_mode": "permissive",
                  "exit_code": 0, "boot_object_sha256": evidence.sha(boot_object),
                  "policy_after": {"coih_sha384": "03" * 48},
                  "transcript": write(name + ".transcript.txt", b"SYNTHETIC authorization fixture\n"),
                  "policy_capture": write_json(name + ".policy.json", {"synthetic_fixture": True}),
                  "note": "SYNTHETIC receipt; no target authorization tool ran"}
        return write_json(name + ".authorization.json", record)

    boots = []
    adt = adt_fixture()
    for index, mode in enumerate(("cold", "cold", "warm", "warm")):
        nonce = f"{index + 16:02x}" * 32
        name = f"boot-{index}"
        args = boot_args_fixture(nonce, len(adt))
        plan = {"schema_version": 1, "page_size": 16384,
                "ram": [{"name": "dram", "base": PHYS, "size": 1024 ** 3}],
                "reservations": [], "boot_args_phys": PHYS + 0x10000,
                "device_tree_pointer_space": "physical"}
        args_ref = write(name + ".boot_args.bin", args)
        adt_ref = write(name + ".adt.bin", adt)
        plan_ref = write_json(name + ".memory_plan.json", plan)
        events = {"entry": entry,
                  "handoff": {"profile": "puredarwin-xnu-7195",
                              "boot_args_sha256": args_ref["sha256"],
                              "device_tree_sha256": adt_ref["sha256"],
                              "memory_plan_sha256": plan_ref["sha256"]},
                  "mmu": {"current_el": 1, "page_size": 16384, "sctlr_el1": "0x30d51835",
                          "tcr_el1": "0x40008000", "ttbr0_el1": "0x800030000",
                          "ttbr1_el1": "0x800034000", "translation_readback": "matched"},
                  "exception": {"kind": "controlled-brk", "esr_el1": "0xf000b105",
                                "elr_el1": hex(pc), "far_el1": "0x0", "recovered": True},
                  "timer": {"frequency_hz": 24000000, "counter_before": 100,
                            "counter_after": 240100, "delivered": 1},
                  "interrupt": {"controller": "apple-aic", "irq": 37, "handled_count": 1},
                  "cpus": {"boot_cpu": 0, "online": list(range(8))},
                  "checkpoint": {"status": "controlled-stop"}}
        serial = "".join("BLAUOS_PHYSICAL " + nonce + " " + event + " " + json.dumps(payload) + "\n"
                         for event, payload in events.items()).encode()
        serial_ref = write(name + ".serial.log", serial)
        boots.append({"nonce": nonce, "mode": mode, "serial": serial_ref,
                      "capture_receipt": capture_record(serial, name + ".serial.log"),
                      "authorization_receipt": authorization(name),
                      "boot_args": args_ref, "device_tree": adt_ref, "memory_plan": plan_ref})

    manifest = {"schema_version": 1, "evidence_kind": "physical-xnu-checkpoint",
                "note": "SYNTHETIC host-generated bundle proving emitter/validator format "
                        "agreement only; no physical execution, capture or authorization occurred",
                "source_profile": PROFILE,
                "source_lock": write("sources.lock.json", (ROOT / "sources.lock.json").read_bytes()),
                "inventory": write_json("inventory.json", inventory),
                "build_inputs": inputs, "build_id": build_id,
                "kernel": kernel_ref,
                "build_receipt": write_json("build_receipt.json", receipt),
                "boot_object": write("boot_object.bin", boot_object),
                "package_receipt": write_json("package_receipt.json", package),
                "boots": boots}
    write_json("manifest.json", manifest)
    print(json.dumps({"bundle": str(bundle), "build_id": build_id,
                      "kernel_uuid": kernel["uuid"],
                      "checkpoint_start": hex(kernel["checkpoint_start"]),
                      "checkpoint_end": hex(kernel["checkpoint_end"]),
                      "synthetic": True}, indent=2))


if __name__ == "__main__":
    main()
