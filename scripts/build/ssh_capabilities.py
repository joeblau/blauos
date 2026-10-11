#!/usr/bin/env python3
"""Build the guest SSH probe and reproduce the pinned kernel RNG adapter defect."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys

REPO = Path(__file__).resolve().parents[2]
PUREDARWIN_PIN = "1bbaeb2ee6aa41e6f792a0b1e2943cc295b3b290"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def run(argv: list[str], log: Path) -> int:
    with log.open("w") as stream:
        stream.write(json.dumps(argv) + "\n")
        stream.flush()
        return subprocess.run(argv, stdout=stream, stderr=subprocess.STDOUT).returncode


def fresh_output(path: Path) -> Path:
    if path.exists() or path.is_symlink():
        raise ValueError(f"output already exists; use a fresh directory: {path}")
    path.mkdir(parents=True)
    return path.resolve()


def build(args: argparse.Namespace) -> dict:
    root = args.runtime_root.resolve()
    if not (root / "usr/lib/libSystem.B.dylib").is_file():
        raise ValueError("source runtime libSystem is missing")
    if not (root / "usr/lib/system/libdyld.dylib").is_file():
        raise ValueError("source runtime libdyld is missing")
    output = fresh_output(args.output_dir)
    sdk = args.sdk.resolve() if args.sdk else Path(subprocess.check_output(
        ["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True).strip())
    compiler = subprocess.check_output(["xcrun", "--find", "clang"], text=True).strip()
    source = REPO / "runtime/probes/ssh_capability_probe.c"
    bridge = REPO / "runtime/compat/sdk27_compile.h"
    binary = output / "root/usr/libexec/blauos-ssh-capability-probe"
    binary.parent.mkdir(parents=True)
    argv = [compiler, "-O2", "-Wall", "-Wextra", "-Wno-nullability-completeness",
            "-include", str(bridge), str(source), "-o", str(binary),
            "-arch", "arm64", "-isysroot", str(sdk), "-mmacosx-version-min=11.0",
            "-fno-builtin", "-fno-stack-protector", "-nostdlib", f"-L{root}/usr/lib",
            "-Wl,-dylib_file,/usr/lib/system/libdyld.dylib:" + str(root / "usr/lib/system/libdyld.dylib"),
            "-Wl,-platform_version,macos,11.0,11.5", "-Wl,-fixup_chains", "-lSystem",
            "-Wl,-dylinker_install_name,/usr/lib/dyld"]
    status = run(argv, output / "build.log")
    result = {"schema_version": 1, "status": "pass" if not status else "blocked",
              "execution_verified": False, "entropy_quality_verified": False,
              "entropy_admission_sysctl": "kern.blauos_ssh_entropy_ready",
              "command": argv, "exit_code": status, "artifacts": []}
    if not status:
        binary.chmod(0o755)
        files = [{"path": str(path.relative_to(REPO)), "sha256": digest(path)}
                 for path in (source, bridge)]
        snapshot = json.dumps(sorted(files, key=lambda entry: entry["path"]),
                              sort_keys=True, separators=(",", ":"))
        artifact = {"path": "/usr/libexec/blauos-ssh-capability-probe", "sha256": digest(binary),
                    "source": "blauos", "revision": "workspace-sha256:" +
                    hashlib.sha256(snapshot.encode()).hexdigest(),
                    "source_path": str(source.relative_to(REPO)), "source_file_sha256": digest(source),
                    "source_files": files, "workspace_revision": subprocess.check_output(
                        ["git", "-C", str(REPO), "rev-parse", "HEAD"], text=True).strip()}
        result["artifacts"] = [artifact]
        result["runtime_inputs"] = [{"path": str(path), "sha256": digest(path)} for path in
                                    (root / "usr/lib/libSystem.B.dylib", root / "usr/lib/system/libdyld.dylib")]
        write(output / "provenance.json", {"schema_version": 1, "artifacts": [artifact]})
    write(output / "build.json", result)
    return result


def regression(args: argparse.Namespace) -> dict:
    source_root = args.source.resolve()
    actual = subprocess.check_output(["git", "-C", str(source_root), "rev-parse", "HEAD"], text=True).strip()
    if actual != PUREDARWIN_PIN:
        raise ValueError("PureDarwin checkout differs from the audited pin")
    if subprocess.check_output(["git", "-C", str(source_root), "status", "--porcelain"], text=True).strip():
        raise ValueError("regression requires a clean pinned source checkout")
    output = fresh_output(args.output_dir)
    source = source_root / "src/Kernel/Extensions/corecrypto"
    copied = output / "patched-source/src/Kernel/Extensions/corecrypto"
    shutil.copytree(source, copied)
    patch = REPO / "vm/patches/puredarwin-ssh-prng.patch"
    subprocess.run(["patch", "-p1", "-i", str(patch)], cwd=output / "patched-source", check=True,
                   capture_output=True)
    compiler = subprocess.check_output(["xcrun", "--find", "clang"], text=True).strip()
    sdk = subprocess.check_output(["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True).strip()
    harness = REPO / "runtime/probes/prng_seed_regression.c"
    base = [compiler, "-isysroot", sdk, "-O2", "-Wall", "-Wextra", "-Wno-unused-parameter",
            "-Wno-deprecated-non-prototype", f"-I{source}/include", f"-I{source}/algorithms", str(harness)]
    common = [str(source / "cc_clear.c"), *[str(source / f"algorithms/yarrow/{name}.c")
              for name in ("prng", "sha1mod", "comp", "yarrowUtils")]]
    tests = []
    for name, provider in (("unpatched", source / "algorithms/cckprng.c"),
                           ("patched", copied / "algorithms/cckprng.c")):
        binary = output / name
        argv = [*base, str(provider), *common, "-o", str(binary)]
        code = run(argv, output / f"{name}-build.log")
        record = {"name": name, "command": argv, "build_exit_code": code, "passed": False}
        if not code:
            invocation = subprocess.run([str(binary)], capture_output=True, text=True)
            record.update(exit_code=invocation.returncode, stdout=invocation.stdout, stderr=invocation.stderr)
            record["passed"] = (invocation.returncode == 1 and "identical output" in invocation.stderr
                                if name == "unpatched" else invocation.returncode == 0)
            if name == "patched":
                short = subprocess.run([str(binary), "--short-seed"], capture_output=True, text=True)
                record["short_seed"] = {"exit_code": short.returncode, "stderr": short.stderr}
                record["passed"] = record["passed"] and short.returncode == -6
        tests.append(record)
    inputs = {harness, patch, source / "algorithms/cckprng.c", source / "cc_clear.c",
              *[Path(item) for item in common], *source.glob("algorithms/yarrow/*.h"),
              *source.glob("include/corecrypto/*.h")}
    result = {"schema_version": 1, "status": "pass" if all(test["passed"] for test in tests) else "blocked",
              "source_pin": actual, "tests": tests, "platform_entropy_quality_verified": False,
              "inputs": [{"path": str(path), "sha256": digest(path)} for path in sorted(inputs)]}
    write(output / "result.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    probe = commands.add_parser("build")
    probe.add_argument("--runtime-root", type=Path, required=True)
    probe.add_argument("--output-dir", type=Path, required=True)
    probe.add_argument("--sdk", type=Path)
    crypto = commands.add_parser("regression")
    crypto.add_argument("--source", type=Path, required=True)
    crypto.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        if platform.system() != "Darwin":
            raise ValueError("the ARM64 Darwin probe/source regression requires a macOS build host")
        result = build(args) if args.operation == "build" else regression(args)
        print(json.dumps({"status": result["status"], "output": str(args.output_dir)}, indent=2))
        return 0 if result["status"] == "pass" else 1
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"SSH capability build: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
