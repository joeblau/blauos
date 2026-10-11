#!/usr/bin/env python3
"""Build locked Apple/BSD file commands against the independent ARM64 runtime."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
COMMANDS = {
    "bin/ls": ("ls/ls.c", "ls/cmp.c", "ls/print.c", "ls/util.c"),
    "bin/cp": ("cp/cp.c", "cp/utils.c"),
    "bin/mv": ("mv/mv.c",),
    "bin/rm": ("rm/rm.c",),
    "bin/mkdir": ("mkdir/mkdir.c",),
    "bin/rmdir": ("rmdir/rmdir.c",),
    "bin/ln": ("ln/ln.c",),
    "bin/chmod": ("chmod/chmod.c",),
    "usr/bin/touch": ("touch/touch.c",),
    "usr/bin/stat": ("stat/stat.c",),
    "bin/df": ("df/df.c", "df/vfslist.c"),
    "usr/bin/du": ("du/du.c",),
}
LIBC = "src/Libraries/libSystem/libc/"
LIBC_SOURCES = {
    "fts": LIBC + "gen/fts.c",
    "strmode": LIBC + "string/FreeBSD/strmode.c",
    "fflags": LIBC + "gen/strtofflags.c",
    "getbsize": LIBC + "gen/FreeBSD/getbsize.c",
    "lchmod": LIBC + "emulated/lchmod.c",
}
SUPPORT = {
    "ls": ("fts", "strmode", "fflags", "getbsize", "humanize"),
    "cp": ("fts", "lchmod"),
    "mv": ("strmode", "fflags"),
    "rm": ("fts", "strmode", "fflags"),
    "chmod": ("fts", "lchmod", "strmode"),
    "stat": ("strmode",),
    "df": ("getbsize", "humanize"),
    "du": ("fts", "getbsize"),
}
AMENDMENTS = (
    "scripts/vm/build_file_commands.py", "runtime/compat/sdk27_compile.h",
    "vm/commands/file/compat.h", "vm/commands/file/get_compat.h",
    "vm/commands/file/syscalls.c", "vm/commands/file/fortify.c", "vm/commands/file/file_cmds.patch",
    "vm/commands/file/libc-support.patch",
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_source(source, pin):
    revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    tree = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD^{tree}"], text=True).strip()
    if revision != pin["revision"] or tree != pin["tree"]:
        raise ValueError(f"{pin['name']} checkout does not match locked revision/tree")
    if subprocess.check_output(["git", "-C", str(source), "status", "--porcelain", "--untracked-files=all", "--ignored"]):
        raise ValueError(f"{pin['name']} checkout must be pristine; compatibility changes belong in explicit patches")
    return {"source": pin["name"], "revision": revision, "tree": tree}


def copy_files(source, destination, relatives):
    for relative in relatives:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, target)


def apply_patch(overlay, patch, log):
    for check in (["--check"], []):
        subprocess.run(["git", "-C", str(overlay), "apply", *check, str(patch)],
                       check=True, stdout=log, stderr=subprocess.STDOUT)


def build(args):
    if sys.platform != "darwin":
        raise ValueError("This compiler profile requires a macOS ARM64 build host")
    if args.output.exists() or args.output.is_symlink():
        raise ValueError("Use a fresh output directory")
    pins = {entry["name"]: entry for entry in json.loads((ROOT / "sources.lock.json").read_text())["sources"]}
    providers = [validate_source(args.source, pins["apple-file_cmds"]),
                 validate_source(args.darwin_source, pins["puredarwin-vm-next"]),
                 validate_source(args.libutil_source, pins["apple-libutil"])]
    runtime_paths = ("usr/lib/libSystem.B.dylib", "usr/lib/system/libdyld.dylib")
    runtime_inputs = {path: digest(args.runtime_root / path) for path in runtime_paths}
    clang = subprocess.check_output(["xcrun", "--find", "clang"], text=True).strip()
    sdk = subprocess.check_output(["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True).strip()
    args.output.mkdir(parents=True)
    overlay = args.output / "file-source-overlay"
    darwin_overlay = args.output / "darwin-source-overlay"
    includes = args.output / "include"
    includes.mkdir()
    primary_files = sorted({str(path.relative_to(args.source))
                            for sources in COMMANDS.values()
                            for path in (args.source / Path(sources[0]).parent).glob("*.[ch]")})
    darwin_files = sorted(set(LIBC_SOURCES.values()) | {LIBC + "include/fts.h"})
    copy_files(args.source, overlay, primary_files)
    copy_files(args.darwin_source, darwin_overlay, darwin_files)
    shutil.copy2(args.darwin_source / (LIBC + "include/fts.h"), includes / "fts.h")
    shutil.copy2(args.libutil_source / "libutil.h", includes / "libutil.h")
    entries, commands, errors = [], [], []
    amendments = [{"path": path, "sha256": digest(ROOT / path)} for path in AMENDMENTS]
    flags = ["-arch", "arm64", "-isysroot", sdk, "-mmacosx-version-min=11.0",
             "-O2", "-Wall", "-Wextra", "-Wno-nullability-completeness", "-Wno-deprecated-declarations",
             "-Wno-incompatible-pointer-types-discards-qualifiers", "-Wno-deprecated-non-prototype",
             "-fno-builtin", "-fno-stack-protector", "-nostdlib", "-Z", "-D_DARWIN_C_SOURCE",
             "-include", str(ROOT / "runtime/compat/sdk27_compile.h"),
             "-include", str(ROOT / "vm/commands/file/compat.h"),
             "-I" + str(includes), "-I" + str(ROOT / "vm/commands/file"),
             "-L" + str(args.runtime_root / "usr/lib"),
             "-Wl,-dylib_file,/usr/lib/system/libdyld.dylib:" + str(args.runtime_root / "usr/lib/system/libdyld.dylib"),
             "-Wl,-platform_version,macos,11.0,11.5", "-Wl,-fixup_chains",
             "-Wl,-dylinker_install_name,/usr/lib/dyld", "-lSystem"]
    with (args.output / "build.log").open("w") as log:
        apply_patch(overlay, ROOT / "vm/commands/file/file_cmds.patch", log)
        apply_patch(darwin_overlay, ROOT / "vm/commands/file/libc-support.patch", log)
        for target, relatives in COMMANDS.items():
            destination = args.output / "root" / target
            destination.parent.mkdir(parents=True, exist_ok=True)
            support = SUPPORT.get(Path(target).name, ())
            source_files = [overlay / relative for relative in relatives]
            source_files += [darwin_overlay / LIBC_SOURCES[name] for name in support if name != "humanize"]
            if "humanize" in support:
                source_files.append(args.libutil_source / "humanize_number.c")
            source_files.append(ROOT / "vm/commands/file/syscalls.c")
            if Path(target).name == "cp":
                source_files.append(ROOT / "vm/commands/file/fortify.c")
            command = [clang, *flags, *map(str, source_files), "-o", str(destination)]
            commands.append(command)
            log.write("$ " + repr(command) + "\n")
            log.flush()
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
            if result.returncode:
                errors.append({"path": "/" + target, "exit_code": result.returncode})
                continue
            linked = subprocess.check_output(["xcrun", "otool", "-L", str(destination)], text=True)
            dylibs = [line.strip().split(" (", 1)[0] for line in linked.splitlines()[1:]]
            if dylibs != ["/usr/lib/libSystem.B.dylib"]:
                raise ValueError(f"Unexpected target runtime provider for {target}: {dylibs}")
            metadata = importlib.util.spec_from_file_location("blauos_runtime_inspect", ROOT / "scripts/build/runtime.py")
            runtime_module = importlib.util.module_from_spec(metadata)
            metadata.loader.exec_module(runtime_module)
            inspected = runtime_module.macho(destination)
            if inspected["architecture"] != "arm64" or inspected["dylinker"] != "/usr/lib/dyld":
                raise ValueError(f"Incorrect guest ABI/loader for {target}")
            actual_primary = [path for path in primary_files if Path(path).parent == Path(relatives[0]).parent]
            external = [{"source": "apple-file_cmds", "path": path, "sha256": digest(args.source / path)}
                        for path in actual_primary]
            if support:
                external += [{"source": "puredarwin-vm-next", "path": path, "sha256": digest(args.darwin_source / path)}
                             for path in [LIBC_SOURCES[name] for name in support if name != "humanize"]]
                if "fts" in support:
                    external.append({"source": "puredarwin-vm-next", "path": LIBC + "include/fts.h",
                                     "sha256": digest(args.darwin_source / (LIBC + "include/fts.h"))})
            if "humanize" in support:
                external += [{"source": "apple-libutil", "path": path, "sha256": digest(args.libutil_source / path)}
                             for path in ("humanize_number.c", "libutil.h")]
            additional = [provider for provider in providers[1:] if any(f["source"] == provider["source"] for f in external)]
            entries.append({"path": "/" + target, "sha256": digest(destination),
                            "source": "apple-file_cmds", "revision": providers[0]["revision"],
                            "build_amendments": amendments, "additional_sources": additional,
                            "external_source_files": external, "linked_dylibs": dylibs})
    manifest = {"schema_version": 1, "artifacts": entries, "built": not errors, "errors": errors,
                "source_providers": providers, "sources_lock_sha256": digest(ROOT / "sources.lock.json"),
                "compiler": {"path": clang, "sha256": digest(Path(clang)),
                             "version": subprocess.check_output([clang, "--version"], text=True).splitlines()[0]},
                "sdk": sdk, "runtime_link_inputs": runtime_inputs,
                "stock_runtime_used": False, "guest_execution_verified": False,
                "profile": "headless-bsd-posix-file-commands-v1"}
    notices = []
    for source_name, base, paths in (
        ("apple-file_cmds", args.source, primary_files),
        ("puredarwin-vm-next", args.darwin_source, darwin_files),
        ("apple-libutil", args.libutil_source, ("humanize_number.c", "libutil.h")),
    ):
        for relative in paths:
            source_text = (base / relative).read_text()
            prefix = re.match(r"\s*(?:(?:/\*.*?\*/)|(?://[^\n]*\n)|\s)+", source_text, re.DOTALL)
            if prefix:
                notices.append(f"\n{source_name}: {relative}\n" + prefix.group().strip() + "\n")
    notices.insert(0, "This product includes software developed by the University of California, Berkeley and its contributors.\n")
    notice_path = args.output / "third-party-notices.txt"
    notice_path.write_text("\n".join(notices))
    manifest["third_party_notices"] = {"path": str(notice_path), "sha256": digest(notice_path)}
    (args.output / "provenance.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (args.output / "commands.json").write_text(json.dumps(commands, indent=2) + "\n")
    if errors:
        raise ValueError("Actual compile/link failed: " + ", ".join(entry["path"] for entry in errors))
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--darwin-source", type=Path, default=ROOT / ".blauos/sources/puredarwin-vm-next")
    parser.add_argument("--libutil-source", type=Path, default=ROOT / ".blauos/experiments/command-tools/libutil-source")
    args = parser.parse_args()
    if args.output.is_symlink():
        print("Use a fresh output directory; output symlinks are refused", file=sys.stderr)
        return 1
    for name in ("source", "runtime_root", "output", "darwin_source", "libutil_source"):
        setattr(args, name, getattr(args, name).resolve())
    preexisting = args.output.exists()
    try:
        result = build(args)
        print(json.dumps({"built": result["built"], "artifacts": len(result["artifacts"]),
                          "provenance": str(args.output / "provenance.json")}, indent=2))
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        if not preexisting and args.output.is_dir():
            (args.output / "failure.json").write_text(json.dumps({"built": False, "error": str(error)}, indent=2) + "\n")
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
