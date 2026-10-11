#!/usr/bin/env python3
"""Build pinned BSD text and shell utilities for the independent ARM64 guest.

Only compiler tools and SDK headers come from the build host. The executables
link exclusively to the supplied source runtime; this produces an additive
root and never changes that runtime or an existing output directory.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys

REPO = Path(__file__).resolve().parents[2]
SOURCE_NAME = "puredarwin-vm-next"
APPLE_NAME = "apple-text-commands"
TEXT = "src/Userspace/text_cmds/"
SHELL = "src/Userspace/shell_cmds/"
PROGRAMS = {
    "bin/cat": (TEXT + "cat.c",),
    "usr/bin/head": (TEXT + "head.c",),
    "usr/bin/tail": tuple(TEXT + "tail/" + name + ".c" for name in
                          ("tail", "forward", "misc", "read", "reverse")),
    "usr/bin/wc": (TEXT + "wc.c",),
    "usr/bin/cut": (TEXT + "cut.c",),
    "usr/bin/tr": tuple(TEXT + "tr/" + name + ".c" for name in
                        ("tr", "cmap", "cset", "str")),
    "usr/bin/uniq": (TEXT + "uniq.c",),
    "usr/bin/tee": (SHELL + "tee.c",),
    "usr/bin/printenv": (SHELL + "printenv.c",),
    "bin/pwd": (SHELL + "pwd.c",),
    "usr/bin/basename": (SHELL + "basename.c",),
    "usr/bin/dirname": (SHELL + "dirname.c",),
    "usr/bin/mktemp": (SHELL + "mktemp.c",),
    "bin/hostname": (SHELL + "hostname.c",),
    "usr/bin/comm": (TEXT + "comm.c",),
    "usr/bin/paste": (TEXT + "paste.c",),
    "usr/bin/rev": (TEXT + "rev.c",),
    "usr/bin/fold": (TEXT + "fold.c",),
    "usr/bin/expand": (TEXT + "expand.c",),
    "usr/bin/unexpand": (TEXT + "unexpand.c",),
}
LOCAL_HEADERS = {
    "usr/bin/tail": (TEXT + "tail/extern.h",),
    "usr/bin/tr": tuple(TEXT + "tr/" + name for name in ("extern.h", "cmap.h", "cset.h")),
}
APPLE_PROGRAMS = {
    "usr/bin/sed": tuple("sed/" + name + ".c" for name in ("main", "compile", "process", "misc")),
    "usr/bin/grep": tuple("grep/" + name + ".c" for name in ("grep", "file", "queue", "util")),
}
APPLE_HEADERS = {
    "usr/bin/sed": ("sed/defs.h", "sed/extern.h"),
    "usr/bin/grep": ("grep/grep.h",),
}
FTS_SOURCE = "src/Libraries/libSystem/libc/gen/fts.c"
FTS_HEADER = "src/Libraries/libSystem/libc/include/fts.h"
GREP_AMENDMENTS = ("vm/commands/text/compat/grep-plaintext.patch",
                   "vm/commands/file/libc-support.patch", "vm/commands/file/syscalls.c",
                   "vm/commands/file/compat.h")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def source_verifier():
    spec = importlib.util.spec_from_file_location("blauos_text_source_verify", REPO / "scripts/blauos.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build(args: argparse.Namespace) -> dict:
    if platform.system() != "Darwin":
        raise ValueError("this compiler profile requires a macOS build host")
    # Reject even dangling symlinks without writing anything into them.
    if args.output.exists() or args.output.is_symlink():
        raise ValueError(f"output already exists; use a fresh directory: {args.output}")
    # Preserve the source paths long enough for the canonical verifier to reject
    # symlinks and index flags that can hide modified build inputs.
    source, apple_source, runtime = args.source.absolute(), args.apple_source.absolute(), args.runtime_root.resolve()
    target_output = args.output.resolve()
    for protected in (source.resolve(), apple_source.resolve(), runtime):
        if target_output == protected or protected in target_output.parents:
            raise ValueError(f"output must be outside input trees: {protected}")
    manifest = REPO / "sources.lock.json"
    pins = {item["name"]: item for item in json.loads(manifest.read_text())["sources"]}
    verifier = source_verifier()
    source_state = verifier.verify_source(pins[SOURCE_NAME], source)
    apple_state = verifier.verify_source(pins[APPLE_NAME], apple_source)
    revision, tree = source_state["revision"], source_state["tree"]
    apple_revision, apple_tree = apple_state["revision"], apple_state["tree"]
    git_environment = verifier.clean_git_environment()
    runtime_inputs = {name: digest(runtime / name) for name in
                      ("usr/lib/libSystem.B.dylib", "usr/lib/system/libdyld.dylib", "usr/lib/dyld")}
    sdk = Path(subprocess.check_output(["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True).strip())
    clang = subprocess.check_output(["xcrun", "--find", "clang"], text=True).strip()
    bridge = REPO / "runtime/compat/sdk27_compile.h"
    amendments = [{"path": name, "sha256": digest(REPO / name)} for name in
                  ("scripts/vm/build_text_commands.py", "scripts/blauos.py", "runtime/compat/sdk27_compile.h")]
    args.output.mkdir(parents=True)
    args.created_output = True
    output = args.output.resolve()
    root = output / "root"
    root.mkdir()
    overlay = output / "apple-source-overlay"
    for directory in ("sed", "grep"):
        shutil.copytree(apple_source / directory, overlay / directory)
    patch = REPO / "vm/commands/text/compat/grep-plaintext.patch"
    subprocess.run(["git", "init", "-q", str(overlay)], check=True, capture_output=True, env=git_environment)
    subprocess.run(["git", "apply", "--check", str(patch)], cwd=overlay, check=True, capture_output=True, env=git_environment)
    subprocess.run(["git", "apply", str(patch)], cwd=overlay, check=True, capture_output=True, env=git_environment)
    libc_overlay = output / "libc-source-overlay"
    fts_source = libc_overlay / FTS_SOURCE
    fts_source.parent.mkdir(parents=True)
    shutil.copy2(source / FTS_SOURCE, fts_source)
    includes = output / "include"
    includes.mkdir()
    shutil.copy2(source / FTS_HEADER, includes / "fts.h")
    libc_patch = REPO / "vm/commands/file/libc-support.patch"
    subprocess.run(["git", "init", "-q", str(libc_overlay)], check=True, capture_output=True, env=git_environment)
    subprocess.run(["git", "apply", "--check", str(libc_patch)], cwd=libc_overlay, check=True, capture_output=True, env=git_environment)
    subprocess.run(["git", "apply", str(libc_patch)], cwd=libc_overlay, check=True, capture_output=True, env=git_environment)
    grep_amendments = [{"path": name, "sha256": digest(REPO / name)} for name in GREP_AMENDMENTS]
    entries, commands, errors = [], [], []
    common = ["-O2", "-Wall", "-Wextra", "-Wno-nullability-completeness",
              "-include", str(bridge), "-arch", "arm64", "-isysroot", str(sdk),
              "-mmacosx-version-min=11.0", "-fno-builtin", "-fno-stack-protector",
              "-Z", "-nostdlib", "-L" + str(runtime / "usr/lib"),
              "-Wl,-dylib_file,/usr/lib/system/libdyld.dylib:" + str(runtime / "usr/lib/system/libdyld.dylib"),
              "-Wl,-platform_version,macos,11.0,11.5", "-Wl,-fixup_chains", "-lSystem",
              "-Wl,-dylinker_install_name,/usr/lib/dyld"]
    with (output / "build.log").open("w") as log:
        for name, relatives in {**PROGRAMS, **APPLE_PROGRAMS}.items():
            destination = root / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            original = apple_source if name in APPLE_PROGRAMS else source
            compile_root = overlay if name in APPLE_PROGRAMS else source
            inputs = [compile_root / relative for relative in relatives]
            options = []
            if name == "usr/bin/grep":
                options = ["-DBLAUOS_GREP_NO_COMPRESSION", "-I" + str(includes),
                           "-include", str(REPO / "vm/commands/file/compat.h")]
                inputs += [fts_source, REPO / "vm/commands/file/syscalls.c"]
            command = [clang, *common, *options, *map(str, inputs), "-o", str(destination)]
            commands.append(command)
            log.write(json.dumps(command) + "\n")
            log.flush()
            process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
            if process.returncode:
                destination.unlink(missing_ok=True)
                errors.append({"path": "/" + name, "exit_code": process.returncode})
                continue
            destination.chmod(0o755)
            headers = APPLE_HEADERS.get(name, ()) if name in APPLE_PROGRAMS else LOCAL_HEADERS.get(name, ())
            sources = [{"path": relative, "sha256": digest(original / relative)} for relative in
                       (*relatives, *headers)]
            entry = {"path": "/" + name, "sha256": digest(destination),
                            "source": APPLE_NAME if name in APPLE_PROGRAMS else SOURCE_NAME,
                            "revision": apple_revision if name in APPLE_PROGRAMS else revision, "source_path": relatives[0],
                            "source_file_sha256": digest(inputs[0]), "upstream_source_files": sources,
                            "build_amendments": amendments}
            if name == "usr/bin/grep":
                entry["build_amendments"] = [*amendments, *grep_amendments]
                entry["upstream_source_file_sha256"] = digest(original / relatives[0])
                entry["disabled_features"] = ["compressed-input options J/M/X/Z and compressed-input aliases"]
                entry["additional_sources"] = [{"source": SOURCE_NAME, "revision": revision}]
                entry["additional_source_files"] = [
                    {"source": SOURCE_NAME, "path": FTS_SOURCE, "sha256": digest(source / FTS_SOURCE),
                     "compiled_sha256": digest(fts_source)},
                    {"source": SOURCE_NAME, "path": FTS_HEADER, "sha256": digest(source / FTS_HEADER)},
                ]
            entries.append(entry)
    changed_inputs = [name for name, sha in runtime_inputs.items() if digest(runtime / name) != sha]
    changed_amendments = [item["path"] for item in [*amendments, *grep_amendments]
                          if digest(REPO / item["path"]) != item["sha256"]]
    if changed_inputs or changed_amendments:
        errors.append({"error": "build inputs changed during compilation", "paths": changed_inputs + changed_amendments})
    verifier.verify_source(pins[SOURCE_NAME], source)
    verifier.verify_source(pins[APPLE_NAME], apple_source)
    result = {"schema_version": 1, "built": not errors, "artifacts": entries, "errors": errors,
              "source_name": SOURCE_NAME, "source_revision": revision, "source_tree": tree,
              "additional_sources": [{"source": APPLE_NAME, "revision": apple_revision, "tree": apple_tree}],
              "source_lock_sha256": digest(manifest), "sdk": str(sdk),
              "sdk_settings_sha256": digest(sdk / "SDKSettings.json"),
              "compiler": clang, "compiler_sha256": digest(Path(clang)),
              "compiler_version": subprocess.check_output([clang, "--version"], text=True).splitlines()[0],
              "runtime_root": str(runtime), "runtime_link_inputs": runtime_inputs,
              "stock_runtime_used": False, "guest_execution_verified": False}
    write(output / "provenance.json", result)
    write(output / "commands.json", commands)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=REPO / ".blauos/sources/puredarwin-vm-next")
    parser.add_argument("--apple-source", type=Path, default=REPO / ".blauos/sources/apple-text-commands")
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.created_output = False
    try:
        result = build(args)
        print(json.dumps({"built": result["built"], "output": str(args.output.resolve()),
                          "commands": [entry["path"] for entry in result["artifacts"]],
                          "errors": result["errors"]}, indent=2))
        return 0 if result["built"] else 1
    except (OSError, ValueError, RuntimeError, KeyError, StopIteration, subprocess.SubprocessError) as error:
        if args.created_output:
            write(args.output / "failure.json", {"built": False, "error": str(error)})
        print(f"text command build: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
