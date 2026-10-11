#!/usr/bin/env python3
"""Compile the bpm package manager against independent libSystem."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

from build_userspace import digest, flags, local_revision

ROOT = Path(__file__).resolve().parents[2]
PROGRAMS = {"usr/bin/bpm": "vm/commands/bpm/bpm.c"}


def build(args):
    if sys.platform != "darwin":
        raise ValueError("The ARM64 compiler profile requires macOS")
    if args.output.exists():
        raise ValueError("Use a fresh output directory; preserve previous build evidence")
    inputs = ("usr/lib/libSystem.B.dylib", "usr/lib/system/libdyld.dylib")
    for name in inputs:
        if not (args.runtime_root / name).is_file():
            raise ValueError("Missing independent runtime link input: " + name)
    sdk = subprocess.check_output(["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True).strip()
    clang = subprocess.check_output(["xcrun", "--find", "clang"], text=True).strip()
    supporting = ("scripts/vm/build_bpm_commands.py", "scripts/vm/build_userspace.py",
                  "runtime/compat/sdk27_compile.h")
    source_files = [{"path": name, "sha256": digest(ROOT / name)} for name in (*PROGRAMS.values(), *supporting)]
    workspace_revision = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    args.output.mkdir(parents=True)
    commands = []
    for installed, source in PROGRAMS.items():
        destination = args.output / "root" / installed
        destination.parent.mkdir(parents=True, exist_ok=True)
        commands.append([clang, "-O2", "-Wall", "-Wextra", "-Werror", "-Wno-nullability-completeness",
                         "-include", str(ROOT / "runtime/compat/sdk27_compile.h"), str(ROOT / source),
                         "-o", str(destination), *flags(args.runtime_root, sdk)])
    metadata = {"schema_version": 1, "commands": commands, "source_files": source_files,
                "compiler_version": subprocess.check_output([clang, "--version"], text=True).splitlines()[0],
                "compiler_sha256": digest(Path(clang)), "sdk": sdk,
                "runtime_link_inputs": {name: digest(args.runtime_root / name) for name in inputs},
                "stock_runtime_used": False, "guest_execution_verified": False}
    (args.output / "inputs.json").write_text(json.dumps(metadata, indent=2) + "\n")
    with (args.output / "build.log").open("w") as log:
        for command in commands:
            subprocess.run(command, check=True, stdout=log, stderr=subprocess.STDOUT)
    artifacts = []
    for installed, source in PROGRAMS.items():
        destination = args.output / "root" / installed
        dependencies = subprocess.check_output(["xcrun", "otool", "-L", str(destination)], text=True)
        (args.output / (Path(source).stem + "-linked-dylibs.txt")).write_text(dependencies)
        linked = [line.strip().split(" (", 1)[0] for line in dependencies.splitlines()[1:] if line.strip()]
        if linked != ["/usr/lib/libSystem.B.dylib"]:
            raise ValueError("Unexpected runtime dependency: " + repr(linked))
        used_sources = [entry for entry in source_files if entry["path"] == source or entry["path"] in supporting]
        artifacts.append({"path": "/" + installed, "sha256": digest(destination), "source": "blauos",
                          "source_files": used_sources, "revision": local_revision(used_sources),
                          "workspace_revision": workspace_revision, "source_path": source,
                          "source_file_sha256": digest(ROOT / source), "linked_dylibs": linked})
    result = {"schema_version": 1, "built": True, "artifacts": artifacts,
              "stock_runtime_used": False, "guest_execution_verified": False}
    (args.output / "provenance.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.runtime_root = args.runtime_root.resolve()
    args.output = args.output.resolve()
    output_preexisting = args.output.exists()
    try:
        print(json.dumps(build(args), indent=2))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        if not output_preexisting and args.output.is_dir():
            (args.output / "failure.json").write_text(json.dumps({"built": False, "error": str(error)}, indent=2) + "\n")
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
