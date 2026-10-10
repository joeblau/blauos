#!/usr/bin/env python3
"""Compile the real early diagnostic object from an existing native recipe.

This is a source/object experiment, not a full fresh kernel build or an M1
execution test. The recipe's generated files are inputs and are recorded.
"""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts/build"))
from headers import locked_source
from xnu import clean_build_environment
from blauos import clean_git_environment

REVISION = "1bbaeb2ee6aa41e6f792a0b1e2943cc295b3b290"
RELATIVE = Path("src/Kernel/xnu/osfmk/arm/arm_init.c")
PATCH = ROOT / "patches/native/0006-t8103-early-diagnostics.patch"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(argv, cwd, environment):
    result = subprocess.run(argv, cwd=cwd, env=environment, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError(f"{argv[0]} exit {result.returncode}:\n{result.stdout}")
    return result.stdout


def nonce_tests(source, bootargs, output, environment, clang):
    # Test the actual pinned parser and the patched validator. Only the host
    # input seam PE_boot_args is supplied; no hardware operations are executed.
    original = bootargs.read_text()
    parser = original[original.index("typedef boolean_t"):original.index("boolean_t\nPE_imgsrc_mount_supported")]
    validator = re.search(r"static bool\nblauos_native_diag_nonce_valid\(.*?\n}\n", source.read_text(), re.S)
    if not validator:
        raise ValueError("Patched nonce validator not found")
    fixture = output / "nonce-fixture.c"
    fixture.write_text("#include <stdint.h>\n#include <stdbool.h>\n#include <string.h>\n"
                       "typedef int boolean_t;\n#define TRUE 1\n#define FALSE 0\n"
                       "static char *input;\nchar *PE_boot_args(void) { return input; }\n" +
                       parser + validator[0] +
                       "int test_nonce(char *args) {\n"
                       " struct { unsigned char before; char raw[70]; unsigned char after; } b = {0};\n"
                       " b.before = 0xa5; b.after = 0x5a; input = args;\n"
                       " int found = PE_parse_boot_argn(\"blauos_boot_nonce\", b.raw, sizeof(b.raw));\n"
                       " if (b.before != 0xa5 || b.after != 0x5a) return -1;\n"
                       " return found && blauos_native_diag_nonce_valid(b.raw);\n}\n")
    library = output / "nonce-fixture.dylib"
    sdk = run(["xcrun", "--sdk", "macosx", "--show-sdk-path"], ROOT, environment).strip()
    command = [clang, "-isysroot", sdk, "-dynamiclib", "-O2", "-Wall", "-Wextra", "-Werror", "-Wno-unused-function",
               str(fixture), "-o", str(library)]
    run(command, output, environment)
    test = ctypes.CDLL(str(library)).test_nonce
    test.argtypes = [ctypes.c_char_p]
    test.restype = ctypes.c_int
    cases = [("missing", b"", 0), ("valid_mixed", b"hex:" + b"0123456789abcdef" * 4, 1),
             ("valid_all_digits", b"hex:" + b"1" * 64, 1),
             ("valid_all_letters", b"hex:" + b"f" * 64, 1),
             ("numeric_without_prefix", b"1" * 64, 0),
             ("uppercase", b"hex:" + b"F" * 64, 0),
             ("nonhex", b"hex:" + b"a" * 63 + b"g", 0),
             ("empty", b"hex:", 0), ("short", b"hex:" + b"a" * 63, 0),
             ("long", b"hex:" + b"a" * 65, 0),
             ("truncated_very_long", b"hex:" + b"a" * 1000, 0),
             ("wrong_prefix", b"HEX:" + b"a" * 64, 0),
             ("embedded_nul", b"hex:" + b"a" * 32 + b"\0" + b"a" * 31, 0)]
    results = []
    for name, value, expected in cases:
        args = b"" if name == "missing" else b"blauos_boot_nonce=" + value
        actual = test(args)
        results.append({"name": name, "expected_valid": bool(expected),
                        "actual_valid": bool(actual), "canary_intact": actual != -1})
        if actual != expected:
            raise ValueError(f"Actual parser/validator failed {name}: {actual}")
    return {"scope": "synthetic host input tests of actual pinned parser and patched validator",
            "hardware_execution": False, "cases": results, "fixture_sha256": sha(fixture),
            "bootargs_source_sha256": sha(bootargs), "compile_argv": command}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configured-build", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("Use a new empty object experiment directory")
    output.mkdir(parents=True, exist_ok=True)
    base = args.configured_build.resolve()
    source_root = base / "source-overlay/src/Kernel/xnu"
    objroot = base / "src/Kernel/xnu/xnu_build/src/xnu-build"
    cwd = objroot / "DEVELOPMENT_ARM64_T8103/osfmk/DEVELOPMENT"
    environment = clean_git_environment(clean_build_environment())
    environment["DEVELOPER_DIR"] = run(["xcode-select", "-p"], ROOT, environment).strip()
    clang = run(["xcrun", "--find", "clang"], ROOT, environment).strip()
    pinned = locked_source(ROOT / "sources.lock.json", "puredarwin-vm-next", ROOT / ".blauos/sources")
    actual_revision = run(["git", "rev-parse", "HEAD"], pinned, environment).strip()
    if actual_revision != REVISION:
        raise ValueError("This patch is bound to its documented PureDarwin revision")
    workspace = output / "source-overlay"
    target = workspace / RELATIVE
    target.parent.mkdir(parents=True)
    target.write_bytes((pinned / RELATIVE).read_bytes())
    patch_snapshot = output / "0006.patch"
    patch_snapshot.write_bytes(PATCH.read_bytes())
    run(["git", "init", "--quiet", str(workspace)], ROOT, environment)
    run(["git", "apply", "--check", str(patch_snapshot)], workspace, environment)
    run(["git", "apply", str(patch_snapshot)], workspace, environment)
    dry_command = ["make", "-n", f"SRCROOT={source_root}", f"OBJROOT={objroot}",
                   f"SOURCE={source_root}/osfmk/", "RELATIVE_SOURCE_PATH=./osfmk/DEVELOPMENT",
                   "CURRENT_BUILD_CONFIG=DEVELOPMENT^ARM64^T8103", "CURRENT_KERNEL_CONFIG=DEVELOPMENT",
                   "CURRENT_ARCH_CONFIG=ARM64", "CURRENT_MACHINE_CONFIG=T8103", f"TARGET={cwd}/",
                   f"OBJPATH={objroot}/DEVELOPMENT_ARM64_T8103", "BUILD_WERROR=0", "BUILD_LTO=0",
                   "DO_CTFMERGE=0", "PLATFORM=MacOSX", "WARNFLAGS_STD=-Wno-implicit-int",
                   "-W", str(source_root / "osfmk/arm/arm_init.c"), "arm_init.o"]
    dry = run(dry_command, cwd, environment)
    (output / "dry-run.log").write_text(dry)
    commands = [shlex.split(line) for line in dry.splitlines()]
    command = next(command for command in commands if command and Path(command[0]).name == "clang"
                   and "-c" in command and str(source_root / "osfmk/arm/arm_init.c") in command)
    original_command = command.copy()
    command[0] = clang
    command[command.index(str(source_root / "osfmk/arm/arm_init.c"))] = str(target)
    command[command.index("-MF") + 1] = str(output / "arm_init.d")
    command += ["-o", str(output / "arm_init.o")]
    (output / "compile-command.json").write_text(json.dumps({"argv": command, "cwd": str(cwd)}, indent=2) + "\n")
    compile_log = run(command, cwd, environment)
    (output / "compile.log").write_text(compile_log)
    artifact = output / "arm_init.o"
    magic, cpu, subtype, filetype = struct.unpack_from("<IIII", artifact.read_bytes())
    if (magic, cpu, subtype, filetype) != (0xfeedfacf, 0x100000c, 0xc0000002, 1):
        raise ValueError("Object lacks real ARM64e kernel Mach-O metadata")
    disassembly = run(["xcrun", "otool", "-tvV", str(artifact)], ROOT, environment)
    (output / "disassembly.log").write_text(disassembly)
    helper = disassembly.split("_blauos_native_early_diagnostics:\n", 1)[1].split("\n_", 1)[0]
    registers = ["CurrentEL", "SCTLR_EL1", "TCR_EL1", "TTBR0_EL1", "TTBR1_EL1", "MAIR_EL1",
                 "CNTFRQ_EL0", "CNTVCT_EL0", "CNTV_CTL_EL0", "CNTV_CVAL_EL0"]
    for register in registers:
        if not re.search(r"\bmrs\s+[^,]+, " + register + r"\b", helper):
            raise ValueError(f"Missing actual register-read instruction {register}")
    if re.search(r"\bmsr\b", helper) or not re.search(r"\badr\s", helper):
        raise ValueError("Diagnostic must read the PC and must not write system registers")
    scoped_profiles = []
    for profile in ("T8103", "VIRT", "T8010"):
        preprocessor = []
        skip = False
        for item in command:
            if skip:
                skip = False
                continue
            if item in ("-o", "-MF"):
                skip = True
                continue
            if item in ("-c", "-MD", "-MP", "-DARM64_BOARD_CONFIG_T8103"):
                continue
            preprocessor.append(item)
        preprocessor += ["-E", "-DARM64_BOARD_CONFIG_" + profile]
        text = run(preprocessor, cwd, environment)
        present = "blauos_native_early_diagnostics" in text
        if present != (profile == "T8103"):
            raise ValueError(f"Unexpected diagnostic preprocessing in {profile}")
        scoped_profiles.append({"profile": profile, "emitter_present": present,
                                "scope": "actual preprocessing; not full alternate-profile builds"})
    dependency_rule = (output / "arm_init.d").read_text().replace("\\\n", " ").splitlines()[0]
    dependencies = shlex.split(dependency_rule.split(":", 1)[1])
    inputs = []
    for name in sorted(set(dependencies)):
        path = Path(name)
        if not path.is_absolute():
            path = cwd / path
        inputs.append({"path": str(path), "sha256": sha(path), "bytes": path.stat().st_size})
    (output / "dependency-inputs.json").write_text(json.dumps(inputs, indent=2) + "\n")
    tests = nonce_tests(target, pinned / "src/Kernel/xnu/pexpert/gen/bootargs.c", output, environment, clang)
    receipt = {"schema_version": 1, "status": "source_object_and_nonce_tests_pass",
               "physical_execution_verified": False, "native_boot_verified": False,
               "full_kernel_link_verified": False, "mmu_validation_verified": False,
               "interrupt_delivery_verified": False, "timer_callback_verified": False,
               "secondary_cpu_startup_verified": False, "source_revision": actual_revision,
               "source_file_sha256": sha(pinned / RELATIVE), "patched_source_sha256": sha(target),
               "patch_sha256": sha(patch_snapshot), "source_lock_sha256": sha(ROOT / "sources.lock.json"),
               "replay_script_sha256": sha(Path(__file__)), "configured_build": str(base),
               "recipe_scope": "existing source-generated configuration; not a fresh full build",
               "dry_run_argv": dry_command, "original_compiler_argv": original_command,
               "actual_compile_argv": command, "compiler_cwd": str(cwd), "compiler_sha256": sha(Path(clang)),
               "recipe_makefile_sha256": sha(cwd / "Makefile"),
               "makeinc_def_sha256": sha(objroot / "makedefs/MakeInc.def"),
               "makeinc_cmd_sha256": sha(objroot / "makedefs/MakeInc.cmd"),
               "artifact": {"path": str(artifact), "sha256": sha(artifact), "bytes": artifact.stat().st_size,
                            "cputype": cpu, "cpusubtype": subtype, "filetype": filetype},
               "register_reads_disassembled": registers, "diagnostic_system_register_writes": 0,
               "profile_scope_checks": scoped_profiles,
               "dependency_inputs": {"path": str(output / "dependency-inputs.json"), "count": len(inputs),
                                     "sha256": sha(output / "dependency-inputs.json")}, "nonce_tests": tests,
               "raw_outputs": [{"path": str(output / name), "sha256": sha(output / name),
                                "bytes": (output / name).stat().st_size}
                               for name in ["dry-run.log", "compile.log", "disassembly.log"]]}
    (output / "result.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"status": receipt["status"], "artifact_sha256": sha(artifact),
                      "physical_execution_verified": False, "nonce_test_count": len(tests["cases"])}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, StopIteration) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
