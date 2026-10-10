#!/usr/bin/env python3
"""Compile essential guest commands against the independently built Darwin runtime."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
PROGRAMS = {
    "bin/netsetup": "src/Userspace/netsetup/netsetup.c",
    "bin/uname": "src/Userspace/shell_cmds/uname.c",
    "bin/sleep": "src/Userspace/shell_cmds/sleep.c",
    "bin/test": "src/Userspace/shell_cmds/test.c",
    "bin/true": "src/Userspace/shell_cmds/true.c",
    "bin/false": "src/Userspace/shell_cmds/false.c",
    "bin/echo": "src/Userspace/shell_cmds/echo.c",
    "sbin/reboot": "src/Userspace/system_cmds/pd_shutdown.c",
}
PROBES = {
    "bin/blauos_guest_probe": "vm/guest_probe.c",
    "bin/blauos_runtime_probe": "runtime/probes/runtime_probe.c",
    "usr/libexec/blauos-bootstrap-probe": "runtime/probes/bootstrap_probe.c",
    "usr/libexec/blauos-shutdown-probe": "runtime/probes/shutdown_probe.c",
    "usr/lib/libblauos-loader-fixture.dylib": "runtime/probes/loader_fixture.c",
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def local_revision(files):
    encoded = json.dumps(sorted(files, key=lambda entry: entry["path"]), sort_keys=True, separators=(",", ":")).encode()
    return "workspace-sha256:" + hashlib.sha256(encoded).hexdigest()


def flags(root, sdk, library=False):
    result = ["-arch", "arm64", "-isysroot", sdk, "-mmacosx-version-min=11.0", "-fno-builtin",
            "-fno-stack-protector", "-nostdlib", "-L" + str(root / "usr/lib"),
            "-Wl,-dylib_file,/usr/lib/system/libdyld.dylib:" + str(root / "usr/lib/system/libdyld.dylib"),
            "-Wl,-platform_version,macos,11.0,11.5",
            "-Wl,-fixup_chains", "-lSystem"]
    if not library:
        result.append("-Wl,-dylinker_install_name,/usr/lib/dyld")
    return result


def build(args):
    if sys.platform != "darwin":
        raise ValueError("This native compiler profile requires macOS; disk formatting remains isolated in Linux")
    if args.output.exists():
        raise ValueError("Use a fresh output directory")
    pins = {item["name"]: item["revision"] for item in json.loads((ROOT / "sources.lock.json").read_text())["sources"]}
    revision = subprocess.check_output(["git", "-C", str(args.source), "rev-parse", "HEAD"], text=True).strip()
    if revision != pins["puredarwin-vm-next"]:
        raise ValueError("Source does not match locked PureDarwin revision")
    if subprocess.check_output(["git", "-C", str(args.source), "status", "--porcelain"]):
        raise ValueError("Compile pristine upstream command sources; compatibility overlays must be explicit")
    sdk = subprocess.check_output(["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True).strip()
    clang = subprocess.check_output(["xcrun", "--find", "clang"], text=True).strip()
    args.output.mkdir(parents=True)
    staged = args.output / "root"
    entries = []
    commands = []
    errors = []
    workspace_revision = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    with (args.output / "build.log").open("w") as log:
        overlay = args.output / "puredarwin-source-overlay"
        netsetup_relative = PROGRAMS["bin/netsetup"]
        netsetup_source = overlay / netsetup_relative
        netsetup_source.parent.mkdir(parents=True)
        shutil.copyfile(args.source / netsetup_relative, netsetup_source)
        network_patch = ROOT / "vm/patches/netsetup-interface-registration.patch"
        subprocess.run(["git", "init", "-q", str(overlay)], check=True, stdout=log, stderr=subprocess.STDOUT)
        for options in (["--check"], []):
            subprocess.run(["git", "-C", str(overlay), "apply", *options, str(network_patch)],
                           check=True, stdout=log, stderr=subprocess.STDOUT)
        for name, relative in {**PROGRAMS, **PROBES}.items():
            own = name in PROBES
            source = (ROOT if own else args.source) / relative
            if name == "bin/netsetup":
                source = netsetup_source
            destination = staged / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            command = [clang, "-O2", "-Wall", "-Wextra", "-Wno-nullability-completeness",
                       "-include", str(ROOT / "runtime/compat/sdk27_compile.h"), str(source), "-o", str(destination)]
            if name == "bin/blauos_runtime_probe":
                command.append("-fblocks")
            if name.endswith(".dylib"):
                command += ["-dynamiclib", "-Z", "-Wl,-install_name,/" + name]
            if name == "bin/netsetup":
                command += ["-Z", "-lIOKitCF", "-lCoreFoundation"]
            command += flags(args.runtime_root, sdk, library=name.endswith(".dylib"))
            commands.append(command)
            log.write("$ " + repr(command) + "\n")
            log.flush()
            process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
            if process.returncode:
                errors.append({"path": "/" + name, "exit_code": process.returncode})
                continue
            entry = {"path": "/" + name, "sha256": digest(destination), "source": "puredarwin-vm-next",
                     "revision": revision, "source_path": relative, "source_file_sha256": digest(source)}
            if name == "bin/netsetup":
                amendments = ("vm/patches/netsetup-interface-registration.patch", "scripts/vm/build_userspace.py")
                entry.update(upstream_source_file_sha256=digest(args.source / relative),
                             build_amendments=[{"path": item, "sha256": digest(ROOT / item)} for item in amendments],
                             framework_link_inputs={item: digest(args.runtime_root / item) for item in
                                                    ("usr/lib/libIOKitCF.dylib", "usr/lib/libCoreFoundation.dylib")})
            if own:
                files = [{"path": relative, "sha256": digest(source)},
                         {"path": "runtime/compat/sdk27_compile.h", "sha256": digest(ROOT / "runtime/compat/sdk27_compile.h")}]
                entry.update(source="blauos", source_files=files, revision=local_revision(files), workspace_revision=workspace_revision)
            entries.append(entry)
    for name in ("halt", "poweroff", "shutdown"):
        (staged / "sbin" / name).symlink_to("reboot")
    (staged / "bin/[").symlink_to("test")
    result = {"schema_version": 1, "artifacts": entries, "sdk": sdk, "built": not errors, "errors": errors,
              "compiler_version": subprocess.check_output([clang, "--version"], text=True).splitlines()[0],
              "runtime_link_inputs": {name: digest(args.runtime_root / name) for name in ("usr/lib/libSystem.B.dylib", "usr/lib/system/libdyld.dylib")},
              "stock_runtime_used": False, "guest_execution_verified": False}
    (args.output / "provenance.json").write_text(json.dumps(result, indent=2) + "\n")
    (args.output / "commands.json").write_text(json.dumps(commands, indent=2) + "\n")
    if errors:
        raise ValueError("Guest commands failed actual source compilation/link: " + ", ".join(item["path"] for item in errors))
    if args.stage:
        for entry in entries:
            path = entry["path"].lstrip("/")
            destination = args.runtime_root / path
            if destination.exists():
                raise ValueError(f"Refusing to overwrite previously staged artifact: {destination}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(staged / path, destination)
        for source in (staged / "sbin").iterdir():
            if source.is_symlink():
                (args.runtime_root / "sbin" / source.name).symlink_to(os.readlink(source))
        (args.runtime_root / "bin/[").symlink_to("test")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / ".blauos/sources/puredarwin-vm-next")
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stage", action="store_true", help="Add new commands to the runtime root; provenance stays in a separate additive manifest")
    args = parser.parse_args()
    for name in ("source", "runtime_root", "output"):
        setattr(args, name, getattr(args, name).resolve())
    try:
        print(json.dumps(build(args), indent=2))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        if args.output.is_dir():
            (args.output / "failure.json").write_text(json.dumps({"built": False, "error": str(error)}, indent=2) + "\n")
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
