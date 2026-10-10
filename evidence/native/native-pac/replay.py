#!/usr/bin/env python3
"""Compile real PAC objects using an inactive source-generated T8103 recipe.

This records an object experiment. It never executes the native kernel, and it
does not replace a fresh full build or physical key-transition measurements.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts/build"))
from headers import locked_source
from xnu import clean_build_environment
from blauos import clean_git_environment

PATCH = ROOT / "patches/native/0011-t8103-public-native-pac.patch"
TARGETS = ["arm64/start.s", "arm64/locore.s", "arm64/cswitch.s",
           "arm64/machine_routines_asm.s", "arm/machine_routines_apple.c",
           "arm/arm_init.c"]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(argv, cwd, env):
    result = subprocess.run(argv, cwd=cwd, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError(f"{argv[0]} exit {result.returncode}:\n{result.stdout}")
    return result.stdout


def without_compile_outputs(argv):
    result = []
    skip = False
    for item in argv:
        if skip:
            skip = False
        elif item in ("-o", "-MF"):
            skip = True
        elif item not in ("-c", "-MD", "-MP", "-DARM64_BOARD_CONFIG_T8103"):
            result.append(item)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configured-build", type=Path, required=True)
    parser.add_argument("--base-receipt", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("Object experiment needs a fresh empty directory")
    output.mkdir(parents=True, exist_ok=True)
    base = args.configured_build.resolve()
    receipt = json.loads(args.base_receipt.read_text())
    if receipt.get("machine") != "T8103" or receipt.get("exit_code") != 0:
        raise ValueError("Base receipt must identify an actual successful T8103 build")
    env = clean_git_environment(clean_build_environment())
    env["DEVELOPER_DIR"] = run(["xcode-select", "-p"], ROOT, env).strip()
    clang = run(["xcrun", "--find", "clang"], ROOT, env).strip()
    pinned = locked_source(ROOT / "sources.lock.json", "puredarwin-vm-next", ROOT / ".blauos/sources")
    revision = run(["git", "rev-parse", "HEAD"], pinned, env).strip()
    if revision != receipt.get("source_revision"):
        raise ValueError("Recipe and verified source revisions differ")
    workspace = output / "source-overlay"
    workspace.mkdir()
    snapshots = []
    for amendment in receipt.get("source_amendments", []):
        if "snapshot_path" not in amendment:
            continue
        source = Path(amendment["snapshot_path"])
        if sha(source) != amendment["sha256"]:
            raise ValueError("Base patch snapshot hash mismatch")
        target = output / source.name
        target.write_bytes(source.read_bytes())
        snapshots.append(target)
    snapshot = output / "0011.patch"
    snapshot.write_bytes(PATCH.read_bytes())
    snapshots.append(snapshot)
    paths = set()
    for patch in snapshots:
        for path in re.findall(r"^(?:--- a/|\+\+\+ b/)([^\n]+)$", patch.read_text(), re.M):
            if Path(path).is_absolute() or ".." in Path(path).parts:
                raise ValueError("Patch source escapes isolated workspace")
            paths.add(path)
    for target in TARGETS:
        paths.add("src/Kernel/xnu/osfmk/" + target)
    for path in sorted(paths):
        source = pinned / path
        if source.is_file():
            target = workspace / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
    run(["git", "init", "--quiet", str(workspace)], ROOT, env)
    for patch in snapshots:
        run(["git", "apply", "--check", str(patch)], workspace, env)
        run(["git", "apply", str(patch)], workspace, env)
    source_root = base / "source-overlay/src/Kernel/xnu"
    new_source = workspace / "src/Kernel/xnu"
    objroot = base / "src/Kernel/xnu/xnu_build/src/xnu-build"
    cwd = objroot / "DEVELOPMENT_ARM64_T8103/osfmk/DEVELOPMENT"
    exports = output / "EXPORT_HDRS"
    shutil.copytree(objroot / "EXPORT_HDRS", exports)
    # Replace only changed exported headers with the same real source content.
    for path in paths:
        relative = Path(path).relative_to("src/Kernel/xnu") if path.startswith("src/Kernel/xnu/") else None
        if relative is None:
            continue
        if relative.parts[:2] == ("pexpert", "pexpert"):
            destination = exports / "pexpert" / Path(*relative.parts[1:])
        else:
            destination = exports / relative
        if destination.exists() and (workspace / path).is_file():
            destination.chmod(destination.stat().st_mode | 0o200)
            destination.write_bytes((workspace / path).read_bytes())
    records = []
    checks = []
    for target in TARGETS:
        name = Path(target).stem
        original_source = source_root / "osfmk" / target
        actual_source = new_source / "osfmk" / target
        dry = ["make", "-n", f"SRCROOT={source_root}", f"OBJROOT={objroot}",
               f"SOURCE={source_root}/osfmk/", "RELATIVE_SOURCE_PATH=./osfmk/DEVELOPMENT",
               "CURRENT_BUILD_CONFIG=DEVELOPMENT^ARM64^T8103", "CURRENT_KERNEL_CONFIG=DEVELOPMENT",
               "CURRENT_ARCH_CONFIG=ARM64", "CURRENT_MACHINE_CONFIG=T8103", f"TARGET={cwd}/",
               f"OBJPATH={objroot}/DEVELOPMENT_ARM64_T8103", "BUILD_WERROR=0", "BUILD_LTO=0",
               "DO_CTFMERGE=0", "PLATFORM=MacOSX", "WARNFLAGS_STD=-Wno-implicit-int",
               "-W", str(original_source), name + ".o"]
        dry_text = run(dry, cwd, env)
        (output / (name + "-dry.log")).write_text(dry_text)
        commands = [shlex.split(line) for line in dry_text.splitlines()]
        original = next(command for command in commands if command and Path(command[0]).name == "clang"
                        and "-c" in command and str(original_source) in command)
        actual = [item.replace(str(objroot / "EXPORT_HDRS"), str(exports)) for item in original]
        actual[0] = clang
        actual[actual.index(str(original_source))] = str(actual_source)
        actual[actual.index("-MF") + 1] = str(output / (name + ".d"))
        # Changed private headers precede original private headers; generated
        # configuration and all untouched headers remain actual recipe inputs.
        actual.insert(1, "-I" + str(new_source / "osfmk"))
        actual[1:1] = ["-iquote", str(original_source.parent)]
        artifact = output / (name + ".o")
        actual += ["-o", str(artifact)]
        (output / (name + "-compile-command.json")).write_text(json.dumps({"argv": actual, "cwd": str(cwd)}, indent=2) + "\n")
        (output / (name + "-compile.log")).write_text(run(actual, cwd, env))
        metadata = struct.unpack_from("<IIII", artifact.read_bytes())
        if metadata != (0xfeedfacf, 0x100000c, 0xc0000002, 1):
            raise ValueError("Expected actual ARM64e kernel object")
        disassembly = output / (name + "-disassembly.log")
        disassembly.write_text(run(["xcrun", "otool", "-tvV", str(artifact)], ROOT, env))
        rule = (output / (name + ".d")).read_text().replace("\\\n", " ").splitlines()[0]
        dependencies = []
        for item in sorted(set(shlex.split(rule.split(":", 1)[1]))):
            file = Path(item)
            file = file if file.is_absolute() else cwd / file
            dependencies.append({"path": str(file), "sha256": sha(file), "bytes": file.stat().st_size})
        inputs = output / (name + "-dependency-inputs.json")
        inputs.write_text(json.dumps(dependencies, indent=2) + "\n")
        records.append({"target": target, "original_compiler_argv": original,
                        "actual_compiler_argv": actual, "dry_run_argv": dry,
                        "artifact": {"path": str(artifact), "sha256": sha(artifact), "bytes": artifact.stat().st_size},
                        "dependency_inputs": {"path": str(inputs), "sha256": sha(inputs), "count": len(dependencies)},
                        "disassembly": {"path": str(disassembly), "sha256": sha(disassembly), "bytes": disassembly.stat().st_size}})
        # Compare actual preprocessed non-T8103 code, including header expansion,
        # rather than a hand-authored symbolic count test.
        if target.endswith(".s"):
            for profile in ("VIRT", "T8010"):
                old_command = without_compile_outputs(original)
                old_command[0] = clang
                old_command += ["-E", "-P", "-DARM64_BOARD_CONFIG_" + profile]
                new_command = without_compile_outputs(actual) + ["-E", "-P", "-DARM64_BOARD_CONFIG_" + profile]
                before = run(old_command, cwd, env)
                after = run(new_command, cwd, env)
                # Inactive conditional blocks can add or remove empty lines.
                # Compare every actual nonempty preprocessed assembly line.
                before_code = "\n".join(line for line in before.splitlines() if line.strip())
                after_code = "\n".join(line for line in after.splitlines() if line.strip())
                if before_code != after_code:
                    raise ValueError(f"Non-T8103 preprocessed behavior changed: {target}/{profile}")
                checks.append({"target": target, "profile": profile,
                               "before_preprocessed_sha256": hashlib.sha256(before.encode()).hexdigest(),
                               "after_preprocessed_sha256": hashlib.sha256(after.encode()).hexdigest(),
                               "nonempty_lines_sha256": hashlib.sha256(before_code.encode()).hexdigest(), "unchanged": True,
                               "scope": "actual preprocessing, not a full alternate-profile build"})
        print(name + " real object compiled", flush=True)
    result = {"schema_version": 1, "status": "real_native_pac_objects_compiled",
              "physical_execution_verified": False, "native_boot_verified": False,
              "full_kernel_link_verified": False, "pac_hardware_transition_verified": False,
              "source_revision": revision, "source_lock_sha256": sha(ROOT / "sources.lock.json"),
              "patch_sha256": sha(snapshot), "replay_script_sha256": sha(Path(__file__)),
              "base_receipt": {"path": str(args.base_receipt.resolve()), "sha256": sha(args.base_receipt)},
              "patch_snapshots": [{"path": str(path), "sha256": sha(path)} for path in snapshots],
              "recipe_scope": "inactive existing generated T8103 recipe with isolated changed source and exported headers",
              "compiler_cwd": str(cwd), "compiler_sha256": sha(Path(clang)),
              "recipe_makefile_sha256": sha(cwd / "Makefile"),
              "makeinc_def_sha256": sha(objroot / "makedefs/MakeInc.def"),
              "objects": records, "non_native_scope_checks": checks}
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"status": result["status"], "objects": len(records), "physical_execution_verified": False}))


if __name__ == "__main__":
    main()
