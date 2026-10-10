#!/usr/bin/env python3
"""Cross-configure dash on a macOS build host against source-built Darwin libraries."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys

from build_userspace import flags

ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(args):
    if sys.platform != "darwin":
        raise ValueError("This cross-configuration profile requires a native macOS compiler")
    if args.output.exists():
        raise ValueError("Preserve previous evidence by using a fresh output directory")
    pins = {item["name"]: item["revision"] for item in json.loads((ROOT / "sources.lock.json").read_text())["sources"]}
    revision = subprocess.check_output(["git", "-C", str(args.source), "rev-parse", "HEAD"], text=True).strip()
    if revision != pins["dash"]:
        raise ValueError("dash does not match the locked source revision")
    tracked = subprocess.check_output(["git", "-C", str(args.source), "ls-files", "-z"]).decode().split("\0")
    for name in filter(None, tracked):
        if digest(args.source / name) != digest(args.prepared / name):
            raise ValueError(f"Prepared dash source differs from upstream tracked file: {name}")
    if not (args.prepared / "configure").is_file():
        raise ValueError("Generate configure using pinned autoreconf before this native build")
    preparation = json.loads((args.prepared.parent / "prepare.json").read_text())
    if preparation.get("revision") != revision or preparation.get("configure_sha256") != digest(args.prepared / "configure"):
        raise ValueError("Generated configure differs from its source/tool preparation evidence")
    args.output.mkdir(parents=True)
    copied = args.output / "source"
    shutil.copytree(args.prepared, copied, symlinks=True)
    # Preparation may include a prior failed build. Configure updates the build
    # files, and clean removes its objects before any target compilation.
    sdk = subprocess.check_output(["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True).strip()
    clang = subprocess.check_output(["xcrun", "--find", "clang"], text=True).strip()
    env = dict(os.environ)
    env.update(CC=clang, CC_FOR_BUILD=shlex.join([clang, "-isysroot", sdk]),
               CFLAGS=shlex.join(["-O2", "-arch", "arm64", "-isysroot", sdk, "-mmacosx-version-min=11.0",
                                "-fno-builtin", "-fno-stack-protector", "-include", str(ROOT / "runtime/compat/sdk27_compile.h")]),
               LDFLAGS=shlex.join(flags(args.runtime_root, sdk)),
               ac_cv_sizeof_intmax_t="8", ac_cv_sizeof_long_long_int="8")
    command = ["sh", str(copied / "configure"), "--host=aarch64-apple-darwin20",
               "--build=aarch64-apple-darwin" + platform.release().split(".")[0], "--without-libedit", "--prefix=/usr"]
    metadata = {"schema_version": 1, "source": "dash", "revision": revision,
                "configure_sha256": digest(copied / "configure"), "configure_command": command,
                "preparation": preparation,
                "annotation_header_sha256": digest(ROOT / "runtime/compat/sdk27_compile.h"),
                "compiler_version": subprocess.check_output([clang, "--version"], text=True).splitlines()[0],
                "runtime_link_inputs": {name: digest(args.runtime_root / name) for name in ("usr/lib/libSystem.B.dylib", "usr/lib/system/libdyld.dylib")},
                "environment": {key: env[key] for key in ("CC", "CC_FOR_BUILD", "CFLAGS", "LDFLAGS", "ac_cv_sizeof_intmax_t", "ac_cv_sizeof_long_long_int")},
                "target_execute_during_configure": False, "guest_execution_verified": False}
    (args.output / "inputs.json").write_text(json.dumps(metadata, indent=2) + "\n")
    with (args.output / "build.log").open("w") as log:
        subprocess.run(command, cwd=copied, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
        log.flush()
        if "checking whether we are cross compiling... yes" not in (args.output / "build.log").read_text():
            raise ValueError("Configure did not establish cross-compilation; refuse host target execution")
        subprocess.run(["make", "clean"], cwd=copied, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
        subprocess.run(["make", "-j2"], cwd=copied, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
    binary = copied / "src/dash"
    destination = args.output / "root/bin/sh"
    destination.parent.mkdir(parents=True)
    shutil.copy2(binary, destination)
    entry = {"path": "/bin/sh", "sha256": digest(destination), "source": "dash", "revision": revision,
             "build_inputs": metadata, "license": "BSD-3-Clause AND GPL-2.0-or-later (generated signal names)"}
    result = {"schema_version": 1, "artifacts": [entry], "built": True, "guest_execution_verified": False}
    (args.output / "provenance.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / ".blauos/sources/dash")
    parser.add_argument("--prepared", type=Path, required=True, help="Private source archive after pinned autoreconf")
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    for name in ("source", "prepared", "runtime_root", "output"):
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
