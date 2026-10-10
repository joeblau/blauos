#!/usr/bin/env python3
"""Collect read-only, sanitized observations for the M1 hardware lab."""
from __future__ import annotations

import argparse
import datetime
import glob
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import subprocess
import sys


EXPECTED_MODEL = "Macmini9,1"
EXPECTED_CHIP = "Apple M1"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def text_property(value):
    if isinstance(value, bytes):
        return value.rstrip(b"\0").decode("utf-8", errors="replace")
    return value if isinstance(value, str) else None


def public_hardware(profile, expert, architecture):
    """Whitelist observations; serials, UUIDs, addresses and accounts stay out."""
    entries = profile.get("SPHardwareDataType", [])
    if len(entries) != 1:
        raise ValueError("Expected one system_profiler hardware record")
    item = entries[0]
    memory = item.get("physical_memory", "")
    match = re.fullmatch(r"(\d+) (GB|TB)", memory)
    processors = re.fullmatch(r"proc (\d+):\d+:(\d+):(\d+)", item.get("number_processors", ""))
    return {
        "model": item.get("machine_model"),
        "chip": item.get("chip_type"),
        "architecture": architecture,
        "memory_bytes": int(match[1]) * (1024 ** (3 if match[2] == "GB" else 4)) if match else None,
        "logical_cpus": int(processors[1]) if processors else None,
        "performance_cores": int(processors[2]) if processors else None,
        "efficiency_cores": int(processors[3]) if processors else None,
        "firmware_version": item.get("boot_rom_version"),
        "os_loader_version": item.get("os_loader_version"),
        "board_compatible": text_property(expert.get("compatible")),
        "target_type": text_property(expert.get("target-type")),
    }


def matches_target(hardware):
    return (hardware.get("model") == EXPECTED_MODEL
            and hardware.get("chip") == EXPECTED_CHIP
            and hardware.get("architecture") == "arm64"
            and text_property(hardware.get("target_type")) in ("J274", "J274AP")
            and EXPECTED_MODEL in (hardware.get("board_compatible") or "").split("\0"))


def identity_fingerprint(expert, nonce):
    if not re.fullmatch(r"[0-9a-f]{64}", nonce):
        raise ValueError("Identity nonce must contain 64 lowercase hexadecimal characters")
    identity = {name: text_property(expert.get(name))
                for name in ("model", "IOPlatformUUID", "IOPlatformSerialNumber")}
    if not all(identity.values()):
        raise ValueError("Private identity requires model, platform UUID and serial observations")
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(bytes.fromhex(nonce) + encoded).hexdigest()


def public_serial_devices(paths):
    """Device names can contain adapter serial numbers; disclose only classes."""
    kinds = []
    for path in paths:
        name = Path(path).name.lower()
        kind = next((kind for token, kind in (("usbmodem", "usb-modem"), ("usbserial", "usb-serial"),
                                              ("debug-console", "debug-console"), ("bluetooth", "bluetooth"))
                     if token in name), "other")
        kinds.append({"kind": kind, "path_redacted": True})
    return sorted(kinds, key=lambda item: item["kind"])


def private_path_allowed(path):
    resolved = path.resolve()
    for parent in resolved.parents:
        if (parent / ".git").exists():
            return resolved.is_relative_to(parent / ".blauos/private")
    # A standalone collector copied to the lab can use a local private directory.
    return True


def write_private(path, data):
    if not private_path_allowed(path):
        raise ValueError("Private platform data inside a repository must use .blauos/private")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)


def collect(role, private_output=None, nonce=None):
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise ValueError("Run this collector on the ARM64 macOS build host or physical test Mac")
    receipts = []

    def run(command):
        completed = subprocess.run(command, capture_output=True, timeout=30, check=False)
        receipts.append({"command": command, "exit_code": completed.returncode})
        if completed.returncode:
            raise ValueError(f"Read-only inventory command failed: {command[0]} (exit {completed.returncode})")
        return completed.stdout

    profile = json.loads(run(["/usr/sbin/system_profiler", "SPHardwareDataType", "SPNVMeDataType", "SPEthernetDataType", "-json"]))
    private_expert = run(["/usr/sbin/ioreg", "-a", "-r", "-c", "IOPlatformExpertDevice"])
    experts = plistlib.loads(private_expert)
    if len(experts) != 1:
        raise ValueError("Expected one IOPlatformExpertDevice record")
    expert = experts[0]
    architecture = run(["/usr/bin/uname", "-m"]).decode().strip()
    os_version = plistlib.loads(Path("/System/Library/CoreServices/SystemVersion.plist").read_bytes())
    hardware = public_hardware(profile, expert, architecture)
    matched = matches_target(hardware)
    storage = []
    for controller in profile.get("SPNVMeDataType", []):
        for drive in controller.get("_items", []):
            storage.append({"controller": controller.get("_name"), "model": drive.get("device_model"),
                            "firmware_revision": drive.get("device_revision"),
                            "capacity_bytes": drive.get("size_in_bytes"), "protocol": "NVMe",
                            "removable": drive.get("removable_media"), "detachable": drive.get("detachable_drive")})
    ethernet = [{"controller": item.get("spethernet_product_name") or item.get("_name"),
                 "bus": item.get("spethernet_bus"), "driver": item.get("spethernet_driver"),
                 "vendor": item.get("spethernet_vendor_name"), "product_id": item.get("spethernet_product-id")}
                for item in profile.get("SPEthernetDataType", [])]
    tools = {}
    for name, filename in (("kmutil", "/usr/bin/kmutil"), ("bputil", "/usr/bin/bputil"),
                           ("diskutil", "/usr/sbin/diskutil"), ("bless", "/usr/sbin/bless")):
        path = Path(filename)
        tools[name] = {"path": filename, "sha256": digest(path)} if path.is_file() else {"available": False}
    result = {
        "schema_version": 1, "role": role, "read_only": True,
        "collector_sha256": digest(Path(__file__)),
        "captured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "hardware": hardware,
        "os": {"version": os_version["ProductVersion"], "build": os_version["ProductBuildVersion"],
               "kernel_release": run(["/usr/bin/uname", "-r"]).decode().strip()},
        "storage": storage, "ethernet": ethernet,
        "serial_devices": public_serial_devices(glob.glob("/dev/cu.*")), "tools": tools,
        "target": {"expected_model": EXPECTED_MODEL, "expected_soc": "t8103", "matches_required_hardware": matched},
        "readiness": {"status": "build-host-only" if role == "build-host" else ("awaiting-lab-verification" if matched else "wrong-hardware"),
                      "lab_setup_verified": False, "physical_boot_validated": False,
                      "required_external_evidence": ["dedicated target and recovery operator", "backup/restore verification",
                                                     "known-good boot and per-OS policy ownership", "physical serial/debug capture"]},
        "commands": receipts,
        "limitations": ["Stock macOS inventory is not evidence that project XNU ran.",
                        "An offline inventory and identity digest are consistency evidence, not hardware attestation."],
    }
    if private_output:
        fingerprint = identity_fingerprint(expert, nonce)
        write_private(private_output, private_expert)
        result["identity"] = {"nonce": nonce, "fingerprint_sha256": fingerprint,
                              "scheme": "sha256(nonce-bytes + canonical-platform-json)-v1",
                              "private_capture_sha256": hashlib.sha256(private_expert).hexdigest(),
                              "private_capture_published": False}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("build-host", "test-machine"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--private-platform-output", type=Path)
    parser.add_argument("--identity-nonce")
    args = parser.parse_args()
    if bool(args.private_platform_output) != bool(args.identity_nonce):
        parser.error("Private capture requires both --private-platform-output and --identity-nonce")
    if args.private_platform_output and args.output.resolve() == args.private_platform_output.resolve():
        parser.error("Public inventory and private platform capture require different paths")
    if args.output.exists():
        parser.error("Use a new output path so earlier inventory evidence is preserved")
    try:
        result = collect(args.role, args.private_platform_output, args.identity_nonce)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 1
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x") as stream:
            stream.write(json.dumps(result, indent=2) + "\n")
    except OSError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps({"output": str(args.output), "role": result["role"],
                      "model": result["hardware"]["model"], "matches_target": result["target"]["matches_required_hardware"],
                      "readiness": result["readiness"]["status"]}))
    return 2 if args.role == "test-machine" and not result["target"]["matches_required_hardware"] else 0


if __name__ == "__main__":
    sys.exit(main())
