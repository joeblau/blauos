#!/usr/bin/env python3
"""Validate captured physical-XNU evidence offline; never authorize or boot a Mac.

A passing consistency check is not hardware attestation. Log files and receipts
are operator-controlled inputs, so Gate 2 always needs an independent reviewer.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import plistlib
import re
import secrets
import stat
import struct
import sys
import uuid


PROJECT = Path(__file__).resolve().parents[2]
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
CHECKPOINT_START = "_blauos_physical_checkpoint_entry"
CHECKPOINT_END = CHECKPOINT_START + "_end"
EVENTS = {"entry", "handoff", "mmu", "exception", "timer", "interrupt", "cpus", "checkpoint"}
IDENTITY_SCHEME = "sha256(nonce-bytes + canonical-platform-json)-v1"
PROFILE = {"xnu_source": "xnu", "board_config": "T8103", "architecture": "arm64e",
           "boot_args_profile": "apple-xnu-12377"}
REVIEWED_PROFILES = {
    "apple-xnu-12377": PROFILE,
    "puredarwin-xnu-7195": dict(PROFILE, xnu_source="puredarwin-vm-next", boot_args_profile="puredarwin-xnu-7195"),
}


class EvidenceError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise EvidenceError(message)


def object_record(value, name):
    require(isinstance(value, dict), f"{name}: JSON object required")
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def hex64(value, name):
    require(isinstance(value, str) and HEX64.fullmatch(value), f"{name}: lowercase SHA256/nonce required")
    return value


def integer(value, name, minimum=0, maximum=(1 << 64) - 1):
    require(type(value) is int and minimum <= value <= maximum, f"{name}: integer outside supported bounds")
    return value


def address(value, name):
    require(isinstance(value, str) and re.fullmatch(r"0x[0-9a-f]{1,16}", value), f"{name}: hexadecimal address required")
    return int(value, 16)


def load_json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, f"duplicate JSON key: {key}")
            result[key] = value
        return result
    def reject_constant(value):
        raise EvidenceError(f"nonfinite JSON value: {value}")
    try:
        return json.loads(data, object_pairs_hook=unique, parse_constant=reject_constant)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise EvidenceError("invalid JSON evidence") from error


def referenced(root, record, label, max_bytes=512 * 1024 * 1024):
    require(isinstance(record, dict), f"{label}: missing hashed file reference")
    name = record.get("path")
    require(isinstance(name, str) and name and not Path(name).is_absolute()
            and ".." not in Path(name).parts, f"{label}: relative bundle path required")
    target = (root / name).resolve(strict=True)
    require(target.is_relative_to(root.resolve()), f"{label}: path escapes evidence bundle")
    require(stat.S_ISREG(target.stat().st_mode), f"{label}: regular file required")
    require(0 < target.stat().st_size <= max_bytes, f"{label}: empty or oversized evidence")
    data = target.read_bytes()
    require(sha(data) == hex64(record.get("sha256"), label + ".sha256"), f"{label}: file hash mismatch")
    return data


def inspect_kernel(data, build_id):
    """Inspect the actual unstripped ARM64e executable and checkpoint labels."""
    require(len(data) >= 32 and data[:4] == b"\xcf\xfa\xed\xfe", "kernel: thin little-endian Mach-O64 required")
    _, cpu, subtype, kind, count, total, _, _ = struct.unpack_from("<8I", data)
    require(cpu == 0x100000C and subtype & 0xFFFFFF == 2 and kind == 2,
            "kernel: native ARM64e MH_EXECUTE required; VIRT/userspace kernels cannot substitute")
    require(0 < count <= total // 8 and total <= len(data) - 32, "kernel: invalid load command table")
    cursor, ids, tables, segments, section_ranges = 32, [], [], [], []
    for _ in range(count):
        require(cursor + 8 <= 32 + total, "kernel: truncated load command")
        command, length = struct.unpack_from("<II", data, cursor)
        require(length >= 8 and length % 8 == 0 and cursor + length <= 32 + total,
                "kernel: malformed load command")
        if command == 0x1B:
            require(length == 24, "kernel: malformed UUID command")
            ids.append(str(uuid.UUID(bytes=data[cursor + 8:cursor + 24])))
        if command == 0x19:
            require(length >= 72, "kernel: malformed segment")
            vm, extent, offset, size, maximum, initial, sections, _ = struct.unpack_from("<4Q4I", data, cursor + 24)
            require(length == 72 + sections * 80 and offset + size <= len(data)
                    and vm + extent <= 1 << 64 and size <= extent, "kernel: segment outside executable")
            require(not (initial & 2 and initial & 4), "kernel: writable executable segment")
            segments.append((vm, extent, offset, size, initial))
            for index in range(sections):
                section_at = cursor + 72 + index * 80
                base, section_size, fileoff = struct.unpack_from("<QQI", data, section_at + 32)
                require(vm <= base <= base + section_size <= vm + extent, "kernel: section outside segment")
                section_ranges.append((base, section_size, initial))
        if command == 2:
            require(length == 24, "kernel: malformed symbol table")
            tables.append(struct.unpack_from("<4I", data, cursor + 8))
        cursor += length
    require(cursor == 32 + total and len(ids) == 1 and len(tables) == 1, "kernel: unique UUID and symbols required")
    symbols = {}
    symoff, count, stroff, strsize = tables[0]
    require(symoff + count * 16 <= len(data) and stroff + strsize <= len(data), "kernel: symbols outside file")
    strings = data[stroff:stroff + strsize]
    for index in range(count):
        pos, kind, section, _, value = struct.unpack_from("<IBBHQ", data, symoff + index * 16)
        if kind & 0xE == 0xE and not kind & 0xE0 and section:
            require(pos < len(strings) and b"\0" in strings[pos:], "kernel: unterminated symbol")
            name = strings[pos:strings.index(b"\0", pos)].decode("utf-8", errors="strict")
            if name in (CHECKPOINT_START, CHECKPOINT_END):
                require(name not in symbols, "kernel: duplicate checkpoint symbol")
                require(section <= len(section_ranges), "kernel: checkpoint symbol has no real section")
                base, extent, protection = section_ranges[section - 1]
                require(base <= value <= base + extent and protection & 4, "kernel: checkpoint label is not executable section code")
                symbols[name] = (value, section)
    start, start_section = symbols.get(CHECKPOINT_START, (0, 0))
    end, end_section = symbols.get(CHECKPOINT_END, (0, 0))
    require(0 < start < end <= start + 4096, "kernel: dedicated checkpoint start/end symbols missing")
    require(start_section == end_section, "kernel: checkpoint labels span different sections")
    require(any(initial & 4 and vm <= start < end <= vm + extent for vm, extent, _, _, initial in segments),
            "kernel: checkpoint is outside executable memory")
    marker = b"BLAUOS_PHYSICAL_BUILD_ID=" + build_id.encode() + b"\0"
    require(sum(data[offset:offset + size].count(marker) for _, _, offset, size, _ in segments) == 1,
            "kernel: unique loadable source/recipe build identity missing")
    return {"uuid": ids[0], "checkpoint_start": start, "checkpoint_end": end,
            "architecture": "arm64e", "file_type": "MH_EXECUTE"}


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    imported = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(imported)
    return imported


def source_identity(root, manifest, trusted_lock, contract):
    profile = manifest.get("source_profile")
    expected = REVIEWED_PROFILES.get(profile.get("boot_args_profile")) if isinstance(profile, dict) else None
    require(expected is not None and all(profile.get(k) == v for k, v in expected.items()),
            "source_profile: reviewed native T8103/ARM64e profile required")
    lock_bytes = referenced(root, manifest.get("source_lock"), "source_lock", 1024 * 1024)
    require(lock_bytes == trusted_lock.read_bytes(), "source_lock: differs from operator-selected project lock")
    lock = object_record(load_json(lock_bytes), "source_lock")
    sources = lock.get("sources")
    require(isinstance(sources, list), "source_lock: sources array required")
    sources = [object_record(item, "source_lock.source") for item in sources]
    pin = [source for source in sources if source.get("name") == profile["xnu_source"]]
    require(len(pin) == 1 and profile.get("revision") == pin[0].get("revision"), "source_profile: XNU revision is not pinned")
    reviewed = load_json(contract.read_bytes())["profiles"][profile["boot_args_profile"]]
    require(any(item.get("source") == profile["xnu_source"] and item.get("revision") == profile["revision"] for item in reviewed["sources"]),
            "source_profile: handoff ABI does not match selected XNU revision")
    inputs = manifest.get("build_inputs")
    require(isinstance(inputs, list) and 2 <= len(inputs) <= 128, "build_inputs: source and recipe evidence required")
    identities, labels = [], set()
    for item in inputs:
        require(isinstance(item, dict) and item.get("role") in ("instrumentation", "recipe", "patch", "toolchain"),
                "build_inputs: reviewed input role required")
        label = item.get("label")
        require(isinstance(label, str) and label and label not in labels, "build_inputs: distinct labels required")
        labels.add(label)
        referenced(root, item, "build_inputs." + label, 32 * 1024 * 1024)
        identities.append({"label": label, "role": item["role"], "sha256": item["sha256"]})
    require({"instrumentation", "recipe"} <= {item["role"] for item in inputs}, "build_inputs: instrumentation and recipe missing")
    return sha(canonical({"source_profile": profile, "build_inputs": sorted(identities, key=lambda item: item["label"])}))


def verify_inventory(root, manifest, private_plist):
    inventory = object_record(load_json(referenced(root, manifest.get("inventory"), "inventory", 1024 * 1024)), "inventory")
    lab = module(PROJECT / "scripts/platform/lab_inventory.py", "physical_lab_inventory")
    hardware = object_record(inventory.get("hardware"), "inventory.hardware")
    require(inventory.get("schema_version") == 1 and inventory.get("role") == "test-machine"
            and inventory.get("read_only") is True and lab.matches_target(inventory.get("hardware", {})),
            "inventory: dedicated physical Macmini9,1/Apple M1/J274 observations required")
    require(isinstance(hardware.get("firmware_version"), str) and hardware["firmware_version"], "inventory: firmware version missing")
    integer(hardware.get("memory_bytes"), "inventory.memory_bytes", 4 * 1024 ** 3)
    require(integer(hardware.get("logical_cpus"), "inventory.logical_cpus", 1, 8) == 8,
            "inventory: M1 Mini eight CPU observations required")
    identity = object_record(inventory.get("identity"), "inventory.identity")
    require(identity.get("scheme") == IDENTITY_SCHEME, "inventory: nonce-bound identity capture missing")
    hex64(identity.get("nonce"), "inventory.identity.nonce")
    hex64(identity.get("fingerprint_sha256"), "inventory.identity.fingerprint")
    private_verified = False
    if private_plist is not None:
        raw = private_plist.read_bytes()
        require(sha(raw) == identity.get("private_capture_sha256"), "private identity: capture hash mismatch")
        parsed = plistlib.loads(raw)
        require(isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict),
                "private identity: one platform record required")
        require(lab.text_property(parsed[0].get("model")) == hardware["model"], "private identity: model mismatch")
        require(lab.identity_fingerprint(parsed[0], identity["nonce"]) == identity["fingerprint_sha256"],
                "private identity: nonce/platform fingerprint mismatch")
        private_verified = True
    return inventory, private_verified


def parse_events(data, nonce):
    events = {}
    for line in data.decode("utf-8", errors="strict").splitlines():
        if not line.startswith("BLAUOS_PHYSICAL "):
            continue
        parts = line.split(" ", 3)
        require(len(parts) == 4 and parts[1] == nonce, "serial: malformed or stale physical marker")
        name = parts[2]
        require(name in EVENTS and name not in events, "serial: duplicate or unknown checkpoint event")
        value = load_json(parts[3])
        require(isinstance(value, dict), "serial: event payload must be an object")
        events[name] = value
    return events


def verify_entry(events, nonce, kernel, build_id, fingerprint):
    require("entry" in events, "serial: no dedicated source-kernel entry observation")
    event = events["entry"]
    require(event.get("build_id") == build_id and event.get("kernel_uuid") == kernel["uuid"], "entry: build identity/UUID mismatch")
    require(event.get("model") == "Macmini9,1" and event.get("soc") == "t8103"
            and event.get("identity_fingerprint_sha256") == fingerprint, "entry: physical identity mismatch")
    require(event.get("execution_environment") == "native-iboot", "entry: stock kernel, emulator or hypervisor is not Path A")
    slide = address(event.get("slide"), "entry.slide")
    pc = address(event.get("pc"), "entry.pc")
    require(slide % 16384 == 0 and kernel["checkpoint_start"] + slide <= pc < kernel["checkpoint_end"] + slide
            and kernel["checkpoint_end"] + slide < 1 << 64, "entry: PC outside dedicated checkpoint symbol/slide")


def verify_capture(root, boot, serial, collector):
    capture = object_record(load_json(referenced(root, boot.get("capture_receipt"), "boot.capture_receipt", 16 * 1024 * 1024)), "capture")
    require(capture.get("schema_version") == 1 and capture.get("transport") == "native-uart"
            and capture.get("baud") == 115200 and capture.get("format") == "8N1"
            and type(capture.get("application_bytes_sent")) is int and capture["application_bytes_sent"] == 0
            and type(capture.get("reset_commands_sent")) is int and capture["reset_commands_sent"] == 0
            and capture.get("error") is None, "capture: passive native UART receipt required")
    require(capture.get("collector_sha256") == sha(collector.read_bytes()), "capture: receipt does not match reviewed collector source")
    device = capture.get("device")
    require(isinstance(device, str) and device.startswith("/dev/")
            and not re.fullmatch(r"/dev/(?:cu|tty)\.usbmodemP_0[13]", device),
            "capture: m1n1 proxy/virtual UART device cannot substitute")
    record = object_record(capture.get("serial"), "capture.serial")
    require(record.get("sha256") == sha(serial) and record.get("bytes") == len(serial), "capture: raw serial length/hash mismatch")
    chunks = capture.get("chunks")
    require(isinstance(chunks, list) and 1 <= len(chunks) <= 262144, "capture: byte/timestamp receipts missing")
    offset, last_time = 0, -1
    for chunk in chunks:
        object_record(chunk, "capture.chunk")
        require(chunk.get("offset") == offset, "capture: noncontiguous chunk offsets")
        length = integer(chunk.get("length"), "capture.chunk.length", 1, len(serial))
        require(offset + length <= len(serial) and chunk.get("sha256") == sha(serial[offset:offset + length]),
                "capture: chunk hash/length mismatch")
        timestamp = integer(chunk.get("monotonic_ns"), "capture.chunk.monotonic_ns")
        require(timestamp >= last_time, "capture: timestamps move backward")
        require(isinstance(chunk.get("received_at"), str), "capture: UTC observation missing")
        offset, last_time = offset + length, timestamp
    require(offset == len(serial), "capture: serial bytes are not wholly covered by chunk receipts")
    require(capture.get("physical_boot_validated") is False, "capture: collector must not declare a physical boot gate")
    return {"transport": "native-uart", "chunks": len(chunks), "stop_reason": capture.get("stop_reason")}


def verify_checkpoint(root, boot, events, inventory, decoder, profile):
    require(EVENTS <= set(events), "checkpoint: measured handoff/MMU/exception/timer/interrupt/CPU evidence missing")
    boot_bytes = referenced(root, boot.get("boot_args"), "boot.boot_args", 4096)
    adt_bytes = referenced(root, boot.get("device_tree"), "boot.device_tree", decoder.MAX_ADT_BYTES)
    plan = load_json(referenced(root, boot.get("memory_plan"), "boot.memory_plan", 1024 * 1024))
    args = decoder.parse_boot_args(boot_bytes, profile["boot_args_profile"])
    tree = decoder.parse_adt(adt_bytes)
    memory = decoder.validate_memory_plan(args, tree, plan, len(adt_bytes))
    command_nonce = "blauos-checkpoint-nonce=" + boot["nonce"]
    require(args["commandLine"].split().count(command_nonce) == 1, "handoff: boot_args does not contain this boot's fresh nonce")
    handoff = events["handoff"]
    for key, record in (("boot_args_sha256", boot["boot_args"]), ("device_tree_sha256", boot["device_tree"]),
                        ("memory_plan_sha256", boot["memory_plan"])):
        require(handoff.get(key) == record["sha256"], "handoff: capture hash does not match kernel observation")
    require(handoff.get("profile") == profile["boot_args_profile"], "handoff: wrong source ABI profile")
    entries = dict(decoder.walk_adt(tree))
    props = entries["/"]["properties"]
    model = decoder.c_string(props.get("model", b""), "ADT.model")
    compatible = props.get("compatible", b"").split(b"\0")
    chosen = entries.get("/chosen", {}).get("properties", {})
    chip_id = chosen.get("chip-id", b"")
    require(model == "Macmini9,1" and b"J274AP" in compatible and b"Macmini9,1" in compatible
            and len(chip_id) == 4 and struct.unpack("<I", chip_id)[0] == 0x8103,
            "handoff: captured ADT does not identify native M1 Mini/T8103/J274AP")
    mmu = events["mmu"]
    require(mmu.get("current_el") == 1 and mmu.get("page_size") == memory["page_size_assumption"] == 16384,
            "mmu: measured EL1/16KiB native profile required")
    sctlr = address(mmu.get("sctlr_el1"), "mmu.sctlr_el1")
    tcr = address(mmu.get("tcr_el1"), "mmu.tcr_el1")
    address(mmu.get("ttbr0_el1"), "mmu.ttbr0_el1")
    address(mmu.get("ttbr1_el1"), "mmu.ttbr1_el1")
    require(sctlr & 1 and (tcr >> 14) & 3 == 2 and (tcr >> 30) & 3 == 1,
            "mmu: translation enabled and both TCR granules must match 16KiB")
    require(mmu.get("translation_readback") == "matched", "mmu: mapping readback observation missing")
    exception = events["exception"]
    esr = address(exception.get("esr_el1"), "exception.esr_el1")
    address(exception.get("elr_el1"), "exception.elr_el1")
    address(exception.get("far_el1"), "exception.far_el1")
    require(exception.get("kind") == "controlled-brk" and esr >> 26 == 0x3C
            and exception.get("recovered") is True, "exception: controlled BRK/recovery not observed")
    timer = events["timer"]
    integer(timer.get("frequency_hz"), "timer.frequency_hz", 1)
    start = integer(timer.get("counter_before"), "timer.counter_before")
    require(integer(timer.get("counter_after"), "timer.counter_after") > start
            and integer(timer.get("delivered"), "timer.delivered", 1) >= 1, "timer: counter progress and delivered callback required")
    interrupt = events["interrupt"]
    require(interrupt.get("controller") == "apple-aic", "interrupt: native Apple AIC observation required")
    integer(interrupt.get("irq"), "interrupt.irq", 0, 65535)
    integer(interrupt.get("handled_count"), "interrupt.handled_count", 1)
    cpus = events["cpus"]
    expected = inventory["hardware"]["logical_cpus"]
    require(cpus.get("boot_cpu") == 0 and cpus.get("online") == list(range(expected)),
            "cpus: observed boot and all supported secondary CPUs required")
    require(events["checkpoint"].get("status") == "controlled-stop", "checkpoint: controlled initialization stop missing")
    return {"memory_ranges": len(memory["ram"]), "reservations": len(memory["reservations"]),
            "online_cpus": expected, "device_tree_sha256": boot["device_tree"]["sha256"]}


def validate(manifest_path, bundle_root=None, private_plist=None, source_lock=None, handoff_contract=None,
             level="entry", capture_collector=None):
    root = (bundle_root or manifest_path.parent).resolve()
    result = {"schema_version": 1, "level": level, "evidence_consistency_passed": False,
              "kernel_entry_observed": False, "checkpoint_observations_complete": False,
              "private_identity_consistency_verified": False, "hardware_attestation_verified": False,
              "physical_gate_passed": False, "review_required": True, "boots": [], "errors": [],
              "limitations": ["Offline files cannot prove physical origin, physical power cycles, or reviewer identity.",
                             "Authorization receipts do not establish that a custom kernel executed."]}
    try:
        require(level in ("entry", "checkpoint"), "level: entry or checkpoint required")
        require(stat.S_ISREG(manifest_path.stat().st_mode) and manifest_path.stat().st_size <= 8 * 1024 * 1024,
                "manifest: bounded regular JSON file required")
        raw = manifest_path.read_bytes()
        result["manifest_sha256"] = sha(raw)
        manifest = object_record(load_json(raw), "manifest")
        require(manifest.get("schema_version") == 1 and manifest.get("evidence_kind") == "physical-xnu-checkpoint",
                "manifest: unsupported physical evidence schema")
        inventory, private_verified = verify_inventory(root, manifest, private_plist)
        result["private_identity_consistency_verified"] = private_verified
        build_id = source_identity(root, manifest, source_lock or PROJECT / "sources.lock.json",
                                   handoff_contract or PROJECT / "platform/apple-m1/handoff/contract.json")
        require(manifest.get("build_id") == build_id, "build_id: source/recipe hash mismatch")
        kernel_bytes = referenced(root, manifest.get("kernel"), "kernel")
        kernel = inspect_kernel(kernel_bytes, build_id)
        result["kernel"] = dict(kernel, sha256=sha(kernel_bytes), build_id=build_id)
        receipt = object_record(load_json(referenced(root, manifest.get("build_receipt"), "build_receipt", 8 * 1024 * 1024)), "build_receipt")
        require(receipt.get("status") == "passed" and receipt.get("exit_code") == 0
                and receipt.get("source_profile") == manifest["source_profile"]
                and receipt.get("build_id") == build_id and receipt.get("kernel_sha256") == sha(kernel_bytes),
                "build_receipt: genuine successful native source recipe evidence required")
        boot_object = referenced(root, manifest.get("boot_object"), "boot_object")
        package = object_record(load_json(referenced(root, manifest.get("package_receipt"), "package_receipt", 8 * 1024 * 1024)), "package_receipt")
        require(package.get("status") == "passed" and package.get("boot_object_sha256") == sha(boot_object)
                and package.get("included_kernel_sha256") == sha(kernel_bytes)
                and package.get("kernel_uuid") == kernel["uuid"], "package_receipt: boot object is not bound to source kernel")
        boots = manifest.get("boots")
        require(isinstance(boots, list) and 1 <= len(boots) <= 32, "boots: actual captured session evidence required")
        decoder = module(PROJECT / "platform/apple-m1/handoff/decoder.py", "physical_handoff_decoder")
        nonces, logs, modes = set(), set(), []
        for index, boot in enumerate(boots):
            require(isinstance(boot, dict), "boot: object required")
            nonce = hex64(boot.get("nonce"), "boot.nonce")
            require(nonce not in nonces and nonce != inventory["identity"]["nonce"], "boot: reused boot/identity nonce")
            nonces.add(nonce)
            require(boot.get("mode") in ("cold", "warm"), "boot: explicit witnessed cold/warm mode required")
            modes.append(boot["mode"])
            serial = referenced(root, boot.get("serial"), "boot.serial", 64 * 1024 * 1024)
            capture = verify_capture(root, boot, serial, capture_collector or PROJECT / "scripts/platform/capture_serial.py")
            require(sha(serial) not in logs, "boot: duplicated serial capture")
            logs.add(sha(serial))
            authorization = object_record(load_json(referenced(root, boot.get("authorization_receipt"), "boot.authorization_receipt", 8 * 1024 * 1024)), "authorization")
            policy = object_record(authorization.get("policy_after"), "authorization.policy_after")
            require(authorization.get("schema_version") == 1 and authorization.get("environment") == "paired-1tr"
                    and authorization.get("security_mode") == "permissive" and authorization.get("exit_code") == 0
                    and authorization.get("boot_object_sha256") == sha(boot_object)
                    and isinstance(policy.get("coih_sha384"), str),
                    "authorization: paired1TR/permissive/result/payload/LocalPolicy capture required")
            require(re.fullmatch(r"[0-9a-f]{96}", authorization["policy_after"]["coih_sha384"]),
                    "authorization: coih is SHA384 of Image4 manifest, not file SHA256")
            referenced(root, authorization.get("transcript"), "authorization.transcript", 8 * 1024 * 1024)
            referenced(root, authorization.get("policy_capture"), "authorization.policy_capture", 8 * 1024 * 1024)
            events = parse_events(serial, nonce)
            verify_entry(events, nonce, kernel, build_id, inventory["identity"]["fingerprint_sha256"])
            observation = {"index": index, "nonce": nonce, "mode_claim": boot["mode"],
                           "serial_sha256": sha(serial), "capture": capture,
                           "entry_observed": True, "checkpoint_complete": False}
            if level == "checkpoint":
                observation["checkpoint"] = verify_checkpoint(root, boot, events, inventory, decoder, manifest["source_profile"])
                observation["checkpoint_complete"] = True
            result["boots"].append(observation)
        if level == "checkpoint":
            require(modes.count("cold") >= 2 and modes.count("warm") >= 2,
                    "checkpoint: at least two independently captured cold and two warm boots required")
        result["evidence_consistency_passed"] = True
        result["kernel_entry_observed"] = True
        result["checkpoint_observations_complete"] = level == "checkpoint"
    except (EvidenceError, OSError, ValueError, KeyError, TypeError, AttributeError, struct.error) as error:
        result["errors"].append(str(error))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    challenge = commands.add_parser("challenge", help="Create a nonce before one physical boot; does not capture or boot anything")
    challenge.add_argument("--output", type=Path, required=True)
    check = commands.add_parser("validate", help="Check captured files offline; no gate is approved automatically")
    check.add_argument("manifest", type=Path)
    check.add_argument("--bundle-root", type=Path)
    check.add_argument("--source-lock", type=Path, default=PROJECT / "sources.lock.json")
    check.add_argument("--handoff-contract", type=Path, default=PROJECT / "platform/apple-m1/handoff/contract.json")
    check.add_argument("--capture-collector", type=Path, default=PROJECT / "scripts/platform/capture_serial.py",
                       help="Reviewed collector source snapshot used for these receipts")
    check.add_argument("--private-platform-plist", type=Path, help="Explicit private identity input; no identifiers appear in report")
    check.add_argument("--level", choices=("entry", "checkpoint"), default="entry")
    check.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Use a new output path so prior evidence is preserved")
    if args.command == "challenge":
        result = {"schema_version": 1, "evidence_kind": "physical-boot-challenge",
                  "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  "nonce": secrets.token_hex(32), "boot_observed": False,
                  "kernel_boot_arg": "blauos-checkpoint-nonce=<replace-with-nonce>",
                  "physical_gate_passed": False}
        result["kernel_boot_arg"] = "blauos-checkpoint-nonce=" + result["nonce"]
        status = 0
    else:
        result = validate(args.manifest, args.bundle_root, args.private_platform_plist,
                          args.source_lock, args.handoff_contract, args.level, args.capture_collector)
        status = 0 if result["evidence_consistency_passed"] else 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as stream:
        stream.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"output": str(args.output), "evidence_consistency_passed": result.get("evidence_consistency_passed", False),
                      "physical_gate_passed": False, "errors": result.get("errors", [])}))
    return status


if __name__ == "__main__":
    sys.exit(main())
