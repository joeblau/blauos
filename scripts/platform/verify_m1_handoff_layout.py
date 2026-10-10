#!/usr/bin/env python3
"""Compile-only ABI assertions against actual pinned ARM64 boot headers."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/build"))
from headers import locked_source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources-dir", type=Path, default=ROOT / ".blauos/sources")
    parser.add_argument("--output-dir", type=Path, default=ROOT / ".blauos/build/native-handoff-layout")
    parser.add_argument("--report", type=Path, default=ROOT / "evidence/native/handoff/abi-layout.json")
    args = parser.parse_args()
    contract_path = ROOT / "platform/apple-m1/handoff/contract.json"
    contract = json.loads(contract_path.read_text())
    environment = {name: value for name, value in os.environ.items() if name in
                   {"PATH", "HOME", "TMPDIR", "TMP", "TEMP", "USER", "LOGNAME",
                    "SHELL", "LANG", "LC_ALL", "DEVELOPER_DIR"}}
    compiler = Path(subprocess.check_output(
        ["/usr/bin/xcrun", "--sdk", "macosx", "--find", "clang"],
        env=environment, text=True).strip()).resolve()
    sdk = Path(subprocess.check_output(
        ["/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-path"],
        env=environment, text=True).strip()).resolve()
    pins = {source["name"]: source["revision"] for source in
            json.loads((ROOT / "sources.lock.json").read_text())["sources"]}
    result = {"schema_version": 1, "status": "failed", "compile_only": True,
              "physical_execution_verified": False, "steps": [], "errors": [],
              "verified_source_files": [],
              "source_contract_sha256": hashlib.sha256(contract_path.read_bytes()).hexdigest(),
              "verifier_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "compiler_path": str(compiler),
              "compiler_sha256": hashlib.sha256(compiler.read_bytes()).hexdigest(),
              "sdk_path": str(sdk),
              "compiler_version": subprocess.check_output([str(compiler), "--version"],
                                                          env=environment, text=True)}
    args.output_dir.mkdir(parents=True, exist_ok=True)
    shim = args.output_dir / "irrelevant-header-shims/pexpert/arm"
    shim.mkdir(parents=True, exist_ok=True)
    # These headers supply no types used by boot_args. Empty shims keep the
    # probe independent of generated kernel SDK/debug types; no runtime stub.
    for name in ("consistent_debug.h", "protos.h"):
        (shim / name).write_text("/* Compile-only irrelevant include shim; no runtime code. */\n")
    for profile, record in contract["profiles"].items():
        source = record["sources"][0]
        checkout = locked_source(ROOT / "sources.lock.json", source["source"], args.sources_dir)
        revision = pins[source["source"]]
        header = checkout / source["path"]
        mismatch = False
        for bound_source in record["sources"]:
            source_file = args.sources_dir / bound_source["source"] / bound_source["path"]
            actual_hash = hashlib.sha256(source_file.read_bytes()).hexdigest()
            if pins.get(bound_source["source"]) != bound_source["revision"] or revision != bound_source["revision"] or actual_hash != bound_source["sha256"]:
                result["errors"].append(profile + ": pinned source mismatch: " + bound_source["path"])
                mismatch = True
            else:
                result["verified_source_files"].append({"profile": profile, "source": bound_source["source"],
                    "revision": revision, "path": bound_source["path"], "sha256": actual_hash})
        if mismatch:
            continue
        code = ["#include <stdint.h>", "#include <stddef.h>", "#include " + json.dumps(str(header.resolve())),
                f'_Static_assert(sizeof(boot_args) == {record["boot_args_size"]}, "size");',
                f'_Static_assert(BOOT_LINE_LENGTH == {record["command_line_size"]}, "command line");',
                '_Static_assert(sizeof(void *) == 8 && sizeof(unsigned long) == 8, "LP64");']
        code += [f'_Static_assert(offsetof(boot_args, {field}) == {offset}, "{field}");'
                 for field, offset in record["field_offsets"].items()]
        probe = args.output_dir / (profile + ".c")
        probe.write_text("\n".join(code) + "\n")
        for architecture in ("arm64", "arm64e"):
            command = [str(compiler), "-isysroot", str(sdk), "-arch", architecture,
                       "-mmacosx-version-min=11.0", "-fsyntax-only",
                       "-Wall", "-Wextra", "-Werror", "-I" + str(shim.parents[1]), str(probe)]
            process = subprocess.run(command, env=environment, capture_output=True, text=True)
            result["steps"].append({"profile": profile, "architecture": architecture, "command": command,
                                    "exit_code": process.returncode, "diagnostics": process.stdout + process.stderr,
                                    "header_sha256": source["sha256"]})
            if process.returncode:
                result["errors"].append(profile + "/" + architecture + ": compiler ABI assertions failed")
    result["status"] = "source_abi_assertions_pass" if not result["errors"] else "failed"
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: result[key] for key in ("status", "errors")}))
    return 0 if not result["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
