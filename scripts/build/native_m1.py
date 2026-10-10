#!/usr/bin/env python3
"""Audit pinned M1 sources and inspect boot assets without installing them."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import sys
import uuid

from headers import ROOT, clean_git_environment, locked_source

TARGET = {"model": "Macmini9,1", "board": "J274AP", "soc": "T8103"}
REFERENCES = [
    "https://asahilinux.org/docs/hw/devices/device-list/",
    "https://support.apple.com/guide/security/sec7d92dc49f/web",
    "https://support.apple.com/guide/security/secac71d5623/web",
]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_record(source: Path, relative: str) -> dict:
    path = source / relative
    return {"path": relative, "sha256": digest(path)}


def machine_targets(text: str) -> list[str]:
    matches = re.findall(r"^SUPPORTED_ARM64_MACHINE_CONFIGS\s*[:?]?=\s*(.*)$",
                         text, re.MULTILINE)
    return sorted({word for line in matches for word in line.split() if word != "NONE"})


def audit(manifest: Path, source_root: Path, build_evidence: Path | None,
          source_profile: str = "xnu") -> dict:
    apple = locked_source(manifest, "xnu", source_root).resolve()
    pd = locked_source(manifest, "puredarwin-vm-next", source_root).resolve()
    entries = {s["name"]: s for s in json.loads(manifest.read_text())["sources"]}
    profiles = []
    for name, source, kernel, makefile in [
        ("xnu", apple, apple, "makedefs/MakeInc.def"),
        ("puredarwin-vm-next", pd, pd / "src/Kernel/xnu", "cmake/MakeInc.def.in"),
    ]:
        definition = (kernel / makefile).read_text()
        targets = machine_targets(definition)
        boot_header = kernel / "pexpert/pexpert/arm64/boot.h"
        boot_line = int(re.search(r"#define\s+BOOT_LINE_LENGTH\s+(\d+)",
                                 boot_header.read_text())[1])
        files = [makefile, "pexpert/pexpert/arm64/board_config.h",
                 "pexpert/pexpert/arm64/H13.h", "pexpert/pexpert/arm64/boot.h",
                 "osfmk/arm64/start.s", "osfmk/arm/arm_init.c", "pexpert/arm/pe_init.c"]
        profiles.append({
            "name": name, "revision": entries[name]["revision"],
            "tree": subprocess.check_output(["git", "-C", str(source), "rev-parse",
                                             "HEAD^{tree}"], text=True, env=clean_git_environment()).strip(),
            "source_tag": entries[name].get("tag"),
            "master_version": ((kernel / "config/MasterVersion").read_text().splitlines()[0].strip()
                               if (kernel / "config/MasterVersion").exists() else None),
            "supported_arm64_targets": targets,
            "t8103_build_target_declared": "T8103" in targets,
            "t8103_board_definition_present": "ARM64_BOARD_CONFIG_T8103" in
                (kernel / "pexpert/pexpert/arm64/board_config.h").read_text(),
            "t8103_flags": next((line for line in definition.splitlines()
                                  if line.startswith("MACHINE_FLAGS_ARM64_T8103")), None),
            "boot_args_source_profile": {"source_revision": entries[name]["revision"],
                "header_sha256": digest(boot_header), "command_line_bytes": boot_line,
                "compatibility_with_target_iboot_measured": False},
            "source_files": [source_record(kernel, file) for file in files],
        })
    native = next(p for p in profiles if p["name"] == source_profile)
    kdk_root = Path("/Library/Developer/KDKs")
    kdks = sorted(str(p) for p in kdk_root.glob("*.kdk")) if kdk_root.exists() else []
    report = {
        "schema_version": 1, "status": "source_audit_complete_native_assets_unproven",
        "target": TARGET, "source_lock_sha256": digest(manifest),
        "profiles": profiles, "selected_source_profile": native["name"],
        "installed_kdk_paths": kdks,
        "selected_collection_format": {"architecture": "arm64e", "filetype": "MH_FILESET",
            "kernel_entry_id": "com.apple.kernel", "kind": "boot",
            "format_is_source_documented_not_target_tested": True,
            "source": f"https://github.com/apple-oss-distributions/xnu/blob/{entries['xnu']['revision']}/README.md"},
        "required_inputs": [
            "Genuine linked DEVELOPMENT T8103 kernel and its UUID/symbols",
            "Matching Xcode/SDK and KDK; KDK KernelSupport/libT8103.os.DEVELOPMENT.a",
            "CoreEntitlements, TrustCache and CodeSignature build interfaces",
            "Target-machine firmware/OS and its actual required driver inventory",
            "Every included driver binary/plist, dependency, UUID, origin and hash",
            "Target-local BootKC generation and local authorization/signing evidence",
            "Source-profile-matched boot_args, Apple device tree and memory handoff",
        ],
        "proprietary_dependencies": [
            "Apple host SDK build headers",
            "KDK platform support archive and unpublished security interfaces",
            "Apple-paired firmware and native platform driver inventory, not yet captured",
        ],
        "platform_driver_findings": {
            "path": "src/Kernel/Extensions/PDArmPlatformExpert/PDArmPlatformExpert.cpp",
            "sha256": digest(pd / "src/Kernel/Extensions/PDArmPlatformExpert/PDArmPlatformExpert.cpp"),
            "apple_aic_dispatch_integrated": True,
            "observation": "PDAppleARMPlatformExpert overrides interrupt hooks and calls PDAppleAIC_init",
            "limits": [
                "Native controller register behavior has not been exercised on T8103",
                "PDArmCPU initializes one interrupt-controller CPU; startCPU returns KERN_FAILURE",
                "PDAppleAIC dispatch ignores non-external event types; timer/IPI paths need separate review",
                "AIC initialization failure only logs a warning in the Apple platform late hook",
            ],
            "related_sources": [source_record(pd, "src/Kernel/Extensions/PDArmPlatformExpert/" + file)
                                for file in ["PDAppleAIC.cpp", "PDArmCPU.cpp", "Info.plist", "CMakeLists.txt"]],
        },
        "local_signing_steps_executed": [], "packaged_native_assets": [],
        "native_boot_assets_ready": False, "physical_execution_verified": False,
        "path_a_result": "not_executed", "fallback_activated": False,
        "primary_references": REFERENCES,
    }
    if build_evidence:
        build = json.loads(build_evidence.read_text())
        if build.get("machine") != "T8103" or build.get("source_revision") != native["revision"]:
            raise ValueError("Build evidence does not belong to the selected native T8103 source profile")
        report["build_experiment"] = {
            "path": str(build_evidence), "sha256": digest(build_evidence),
            "status": build["status"], "exit_code": build["exit_code"],
            "artifact_count": len(build["artifacts"]), "source_revision": build["source_revision"],
            "machine": build["machine"], "sdk": build["sdk"],
            "source_amendments": build.get("source_amendments", []),
            "recipe": build.get("recipe"),
        }
    return report


def mach_header(data: bytes, offset: int = 0) -> dict:
    if offset < 0 or offset + 32 > len(data):
        raise ValueError("Truncated Mach-O header")
    magic, cpu, subtype, filetype, ncmds, size, flags, _ = struct.unpack_from("<8I", data, offset)
    if magic != 0xFEEDFACF or cpu != 0x0100000C:
        raise ValueError("Expected a thin little-endian ARM64 Mach-O")
    start, end = offset + 32, offset + 32 + size
    if end > len(data) or ncmds > size // 8:
        raise ValueError("Mach-O load-command table exceeds the file")
    record = {"header_offset": offset, "cpu_subtype": hex(subtype),
              "architecture": "arm64e" if subtype & 0xFFFFFF == 2 else "arm64",
              "filetype": {2: "MH_EXECUTE", 12: "MH_FILESET"}.get(filetype, str(filetype)),
              "flags": hex(flags), "uuid": None, "segments": [], "fileset_entries": [],
              "entry_pc": None, "load_commands": []}
    cursor = start
    for _ in range(ncmds):
        if cursor + 8 > end:
            raise ValueError("Truncated Mach-O load command")
        cmd, length = struct.unpack_from("<II", data, cursor)
        if length < 8 or length % 8 or cursor + length > end:
            raise ValueError("Invalid Mach-O load-command size")
        record["load_commands"].append(hex(cmd))
        if cmd == 0x19:
            if length < 72:
                raise ValueError("Truncated LC_SEGMENT_64")
            _, _, name, address, vmsize, fileoff, filesize, maxprot, initprot, nsects, _ = \
                struct.unpack_from("<II16sQQQQiiII", data, cursor)
            if length != 72 + 80 * nsects:
                raise ValueError("Invalid LC_SEGMENT_64 section table")
            if fileoff > len(data) or filesize > len(data) - fileoff:
                raise ValueError("Mach-O segment file range exceeds the file")
            if address + vmsize > 1 << 64:
                raise ValueError("Mach-O segment virtual range overflows ARM64")
            record["segments"].append({"name": name.rstrip(b"\0").decode("ascii"),
                "vmaddr": hex(address), "vmsize": vmsize, "fileoff": fileoff,
                "filesize": filesize, "initial_protection": initprot, "max_protection": maxprot})
        elif cmd == 0x1B:
            if length != 24:
                raise ValueError("Invalid LC_UUID")
            if record["uuid"] is not None:
                raise ValueError("Duplicate LC_UUID")
            record["uuid"] = str(uuid.UUID(bytes=data[cursor + 8:cursor + 24])).upper()
        elif cmd == 0x80000035:
            if length < 40:
                raise ValueError("Truncated LC_FILESET_ENTRY")
            address, fileoff, nameoff, _ = struct.unpack_from("<QQII", data, cursor + 8)
            if nameoff < 32 or nameoff >= length or fileoff + 32 > len(data):
                raise ValueError("Invalid fileset entry offset")
            raw = data[cursor + nameoff:cursor + length]
            if b"\0" not in raw:
                raise ValueError("Unterminated fileset entry ID")
            name = raw.split(b"\0", 1)[0].decode("utf-8")
            if not name or any(e["entry_id"] == name for e in record["fileset_entries"]):
                raise ValueError("Empty or duplicate fileset entry ID")
            record["fileset_entries"].append({"entry_id": name, "vmaddr": hex(address), "fileoff": fileoff})
        elif cmd == 5:
            state = cursor + 8
            while state < cursor + length:
                if state + 8 > cursor + length:
                    raise ValueError("Truncated LC_UNIXTHREAD state")
                flavor, count = struct.unpack_from("<II", data, state)
                if state + 8 + 4 * count > cursor + length:
                    raise ValueError("LC_UNIXTHREAD state exceeds its command")
                if flavor == 6:
                    if count != 68:
                        raise ValueError("Unexpected ARM_THREAD_STATE64 size")
                    record["entry_pc"] = hex(struct.unpack_from("<Q", data, state + 8 + 32 * 8)[0])
                state += 8 + count * 4
        cursor += length
    if cursor != end:
        raise ValueError("Mach-O command count does not cover sizeofcmds")
    return record


def native_recipe_errors(build: dict, source: dict, manifest: Path,
                         source_profile: str) -> list[str]:
    """Check the actual source/profile recipe instead of trusting a board label."""
    errors = []
    if (build.get("source_revision") != source["revision"] or
            build.get("manifest_sha256") != digest(manifest) or
            build.get("machine") != "T8103" or type(build.get("exit_code")) is not int or
            build.get("exit_code") != 0 or build.get("status") != "kernel-built-not-boot-validated"):
        errors.append("Build result is not a successful pinned T8103 experiment")
    recipe = build.get("recipe")
    if not isinstance(recipe, dict) or recipe.get("schema_version") != 1:
        return errors + ["Effective build recipe is missing"]
    canonical = hashlib.sha256(json.dumps(recipe, sort_keys=True,
                                          separators=(",", ":")).encode()).hexdigest()
    if build.get("recipe_sha256") != canonical:
        errors.append("Effective recipe canonical hash does not match")
    expected = {"source_name": source_profile, "source_revision": source["revision"],
                "source_tree": source.get("tree"), "machine": "T8103",
                "architecture_config": "ARM64", "kernel_config": "DEVELOPMENT",
                "sdk": build.get("sdk"), "effective_source": build.get("source"),
                "manifest_sha256": digest(manifest),
                "build_system": "apple_make" if source_profile == "xnu" else "puredarwin_cmake"}
    if any(value is None or recipe.get(key) != value for key, value in expected.items()):
        errors.append("Effective build recipe does not match the locked native source profile")
    environment = recipe.get("tool_environment")
    if not isinstance(environment, dict):
        errors.append("Sanitized build tool environment is missing")
        environment = {}
    permitted_environment = {"PATH", "HOME", "TMPDIR", "TMP", "TEMP", "USER", "LOGNAME",
                             "SHELL", "LANG", "LC_ALL", "DEVELOPER_DIR", "SDKROOT", "CC", "CXX"}
    if set(environment) - permitted_environment:
        errors.append("Inherited compiler or make overrides remain in the effective environment")
    if environment.get("SDKROOT") != recipe.get("sdk") or any(
            not isinstance(environment.get(name), str) or not environment[name]
            for name in ["DEVELOPER_DIR", "CC", "CXX"]):
        errors.append("Effective SDK and compiler environment is incomplete or inconsistent")
    commands = recipe.get("commands")
    if not isinstance(commands, list) or not commands:
        errors.append("Exact native build commands are missing")
    else:
        if not all(isinstance(c, dict) and isinstance(c.get("argv"), list) and
                   c["argv"] and all(isinstance(a, str) for a in c["argv"]) and
                   isinstance(c.get("cwd"), str) and type(c.get("exit_code")) is int and
                   c["exit_code"] == 0 and re.fullmatch(r"[0-9a-f]{64}", str(c.get("raw_log_sha256")))
                   for c in commands):
            errors.append("Native build command inventory is malformed")
        elif source_profile == "xnu":
            command = commands[-1]
            expected_settings = {"ARCH_CONFIGS": "ARM64", "MACHINE_CONFIGS": "T8103",
                                 "KERNEL_CONFIGS": "DEVELOPMENT", "SRCROOT": recipe.get("effective_source"),
                                 "SDKROOT": recipe.get("sdk"), "SDKROOT_RESOLVED": recipe.get("sdk"),
                                 "CC": environment.get("CC"), "CXX": environment.get("CXX")}
            for key, value in expected_settings.items():
                if not isinstance(value, str) or [a for a in command["argv"] if a.startswith(key + "=")] != [key + "=" + value]:
                    errors.append(f"Effective make setting {key} is not uniquely native")
            if command["argv"][0] != "make" or command["cwd"] != build.get("source"):
                errors.append("Native make source directory does not match the build input")
        elif source_profile == "puredarwin-vm-next":
            configure = commands[0]["argv"]
            for setting in ["-DPUREDARWIN_ARM64_MACHINE_CONFIG=T8103",
                            "-DPUREDARWIN_XNU_KERNEL_CONFIG=DEVELOPMENT",
                            "-DPUREDARWIN_ARCH=arm64", f"-DCMAKE_OSX_SYSROOT={recipe.get('sdk')}",
                            f"-DCMAKE_C_COMPILER={environment.get('CC')}",
                            f"-DCMAKE_CXX_COMPILER={environment.get('CXX')}"]:
                key = setting.split("=", 1)[0] + "="
                if [a for a in configure if a.startswith(key)] != [setting]:
                    errors.append(f"Effective CMake setting {key[:-1]} is not uniquely native")
            if (configure[0] != "cmake" or configure.count("-S") != 1 or
                    configure[configure.index("-S") + 1:][:1] != [build.get("source")]):
                errors.append("Native CMake source directory does not match the build input")
    return errors


def inspect(path: Path, build_evidence: Path | None, manifest: Path,
            source_profile: str = "xnu") -> dict:
    data = path.read_bytes()
    header = mach_header(data)
    errors = []
    kernel = header
    if header["filetype"] == "MH_FILESET":
        entries = [e for e in header["fileset_entries"] if e["entry_id"] == "com.apple.kernel"]
        if len(entries) != 1:
            errors.append("Fileset has no unique com.apple.kernel entry")
        else:
            kernel = mach_header(data, entries[0]["fileoff"])
    if kernel["architecture"] != "arm64e":
        errors.append("Kernel is not the documented native arm64e architecture")
    if kernel["filetype"] != "MH_EXECUTE":
        errors.append("Kernel entry is not MH_EXECUTE")
    if kernel["uuid"] is None:
        errors.append("Kernel UUID is absent")
    pc = kernel["entry_pc"] or header["entry_pc"]
    if pc is None:
        errors.append("Kernel entry PC is absent")
    elif not any(s["initial_protection"] & 4 and int(s["vmaddr"], 16) <= int(pc, 16) <
                 int(s["vmaddr"], 16) + min(s["vmsize"], s["filesize"])
                 for s in kernel["segments"]):
        errors.append("Kernel entry PC is outside file-backed executable segments")
    if header["filetype"] != "MH_FILESET":
        errors.append("Input is not the selected native MH_FILESET BootKC")
    provenance_verified = False
    if build_evidence:
        build = json.loads(build_evidence.read_text())
        source = next(s for s in json.loads(manifest.read_text())["sources"] if s["name"] == source_profile)
        direct_hash = digest(path)
        artifacts = build.get("artifacts", [])
        # A raw XNU result proves its kernel bytes only. A collection needs a
        # separate kernel-input binding; inspecting it cannot invent that proof.
        recipe_errors = native_recipe_errors(build, source, manifest, source_profile)
        errors.extend(recipe_errors)
        provenance_verified = not recipe_errors and any(a.get("sha256") == direct_hash for a in artifacts)
        if not provenance_verified:
            errors.append("Artifact is not hash-bound to a successful pinned native T8103 build")
    else:
        errors.append("Successful pinned native T8103 build provenance is missing")
    return {"schema_version": 1, "status": "inspected", "target": TARGET,
            "source_profile": source_profile,
            "path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
            "header": header, "kernel_header": kernel if kernel is not header else None,
            "native_source_provenance_verified": provenance_verified,
            "candidate_check_errors": errors,
            "native_boot_assets_ready": False, "physical_execution_verified": False,
            "local_authorization_verified": False,
            "limitation": "Mach-O metadata inspection does not validate local signing, firmware ABI or execution"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "sources.lock.json")
    parser.add_argument("--source-root", type=Path, default=ROOT / ".blauos/sources")
    parser.add_argument("--source-profile", choices=["xnu", "puredarwin-vm-next"], default="xnu")
    parser.add_argument("--output", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    audit_parser = commands.add_parser("audit")
    audit_parser.add_argument("--build-evidence", type=Path)
    inspect_parser = commands.add_parser("inspect")
    inspect_parser.add_argument("artifact", type=Path)
    inspect_parser.add_argument("--build-evidence", type=Path)
    inspect_parser.add_argument("--require-native", action="store_true")
    args = parser.parse_args()
    if args.command == "audit":
        report = audit(args.manifest, args.source_root, args.build_evidence, args.source_profile)
    else:
        report = inspect(args.artifact, args.build_evidence, args.manifest, args.source_profile)
    text = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
    print(text, end="")
    return 2 if args.command == "inspect" and args.require_native and report["candidate_check_errors"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError, struct.error) as error:
        print(f"Native M1 inspection failed: {error}", file=sys.stderr)
        sys.exit(1)
