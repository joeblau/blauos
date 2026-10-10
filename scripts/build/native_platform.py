#!/usr/bin/env python3
"""Build the pinned experimental T8103 platform extension without booting it."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile

from headers import ROOT, clean_git_environment, locked_source
from native_m1 import mach_header
from xnu import (BUILD_WRAPPER_INPUTS, canonical_digest, clean_build_environment,
                 execute, fresh_build_root)

WRAPPER_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
WRAPPER_INPUTS = BUILD_WRAPPER_INPUTS + [
    {"path": "scripts/build/native_platform.py", "sha256": WRAPPER_SHA256},
    {"path": "scripts/build/native_m1.py", "sha256": hashlib.sha256(
        (ROOT / "scripts/build/native_m1.py").read_bytes()).hexdigest()},
]


def driver_metadata_errors(header: dict) -> list[str]:
    errors = []
    if header["architecture"] != "arm64e" or header["filetype"] != "11":
        errors.append("Platform binary is not a native ARM64e MH_KEXT_BUNDLE")
    if int(header["cpu_subtype"], 16) != 0xC0000002:
        errors.append("Platform binary does not use the matching ARM64e KER00 ABI")
    if header["uuid"] is None:
        errors.append("Platform bundle UUID is absent")
    if not any(segment["initial_protection"] & 4 and segment["filesize"] > 0
               for segment in header["segments"]):
        errors.append("Platform bundle has no file-backed executable segment")
    if any(segment["initial_protection"] & 6 == 6 for segment in header["segments"]):
        errors.append("Platform bundle contains writable executable memory")
    if set(header["load_commands"]) & {"0xc", "0x80000018", "0x8000001f", "0x20", "0x80000023"}:
        errors.append("Platform bundle imports a userspace dynamic library")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "sources.lock.json")
    parser.add_argument("--source-root", type=Path, default=ROOT / ".blauos/sources")
    parser.add_argument("--build-root", type=Path, default=ROOT / ".blauos/build/native/platform")
    parser.add_argument("--evidence", type=Path, default=ROOT / "evidence/native/platform")
    parser.add_argument("--sdk", type=Path)
    parser.add_argument("--patch", type=Path, action="append", required=True)
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    if args.jobs < 1:
        raise ValueError("--jobs must be positive")
    build = fresh_build_root(args.build_root)
    evidence = args.evidence.resolve()
    source = locked_source(args.manifest, "puredarwin-vm-next", args.source_root).resolve()
    git_environment = clean_git_environment()
    pin = next(item for item in json.loads(args.manifest.read_text())["sources"]
               if item["name"] == "puredarwin-vm-next")
    archive, overlay = build / "source.tar", build / "source-overlay"
    with archive.open("xb") as output:
        subprocess.run(["git", "-C", str(source), "archive", "HEAD"],
                       env=git_environment, stdout=output, check=True)
    excluded_links = []
    def archive_filter(member, destination):
        if member.issym() and Path(member.linkname).is_absolute():
            excluded_links.append(member.name)
            return None
        return tarfile.data_filter(member, destination)
    with tarfile.open(archive) as stream:
        stream.extractall(overlay, filter=archive_filter)
    subprocess.run(["git", "init", "--quiet", str(overlay)], env=git_environment, check=True)
    patches = []
    for index, path in enumerate(args.patch, 1):
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        snapshot = evidence / "inputs" / f"{index:02d}-{digest}.patch"
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        with snapshot.open("xb") as output:
            output.write(data)
        subprocess.run(["git", "apply", "--check", str(snapshot)],
                       cwd=overlay, env=git_environment, check=True)
        subprocess.run(["git", "apply", str(snapshot)],
                       cwd=overlay, env=git_environment, check=True)
        patches.append({"path": str(path.resolve()), "snapshot_path": str(snapshot), "sha256": digest})
    environment = clean_build_environment()
    environment["DEVELOPER_DIR"] = str(Path(environment.get("DEVELOPER_DIR") or
        subprocess.check_output(["/usr/bin/xcode-select", "--print-path"],
                                env=environment, text=True).strip()).resolve())
    sdk = (args.sdk or Path(subprocess.check_output(
        ["/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-path"],
        env=environment, text=True).strip())).resolve()
    environment["SDKROOT"] = str(sdk)
    for name, tool in (("CC", "clang"), ("CXX", "clang++")):
        environment[name] = subprocess.check_output(
            ["/usr/bin/xcrun", "--find", tool], env=environment, text=True).strip()
    recipe = {"source_name": pin["name"], "source_revision": pin["revision"],
              "source_tree": pin["tree"], "manifest_sha256": hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
              "machine": "T8103", "target": "PDArmPlatformExpert", "kernel_configuration": "DEVELOPMENT",
              "sdk": str(sdk), "source": str(overlay),
              "source_archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
              "excluded_absolute_symlinks": excluded_links, "patches": patches,
              "wrapper_inputs": WRAPPER_INPUTS,
              "tool_environment": environment, "commands": []}
    configure = ["cmake", "-S", str(overlay), "-B", str(build), "-G", "Unix Makefiles",
                 "-DCMAKE_POLICY_VERSION_MINIMUM=3.5", "-DPUREDARWIN_ARCH=arm64",
                 "-DCMAKE_OSX_ARCHITECTURES=arm64", f"-DCMAKE_OSX_SYSROOT={sdk}",
                 f"-DCMAKE_C_COMPILER={environment['CC']}", f"-DCMAKE_CXX_COMPILER={environment['CXX']}",
                 "-DPUREDARWIN_ARM64_MACHINE_CONFIG=T8103", "-DPUREDARWIN_XNU_KERNEL_CONFIG=DEVELOPMENT",
                 "-DPUREDARWIN_ENABLE_KERNEL=ON", "-DPUREDARWIN_ENABLE_LIBRARIES=ON",
                 "-DPUREDARWIN_ENABLE_TOOLS=ON", "-DPUREDARWIN_ENABLE_USERSPACE=OFF",
                 "-DPUREDARWIN_ENABLE_PROJECTS=OFF"]
    code = execute(configure, ROOT, build / "logs/configure.log", evidence, "configure",
                   environment=environment, recipe_steps=recipe["commands"])
    if code == 0:
        code = execute(["cmake", "--build", str(build), "--target", "PDArmPlatformExpert",
                        "--parallel", str(args.jobs)], ROOT, build / "logs/build.log", evidence,
                       "build", environment=environment, recipe_steps=recipe["commands"])
    artifacts, errors = [], []
    for path in sorted(build.rglob("*.kext/Contents/MacOS/PDArmPlatformExpert")):
        data = path.read_bytes()
        header = mach_header(data)
        errors.extend(driver_metadata_errors(header))
        artifacts.append({"path": str(path), "sha256": hashlib.sha256(data).hexdigest(),
                          "bytes": len(data), "header": header})
    if code == 0 and not artifacts:
        errors.append("No linked platform extension was produced")
    success = code == 0 and bool(artifacts) and not errors
    report = {"schema_version": 1, "status": "driver-linked-not-hardware-validated" if success else "blocked",
              "exit_code": code, "recipe": recipe, "recipe_sha256": canonical_digest(recipe),
              "artifacts": artifacts, "errors": errors,
              "kernel_linkage_verified": False, "native_boot_assets_ready": False,
              "physical_interrupt_delivery_verified": False, "physical_gate_passed": False}
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "exit_code": code,
                      "artifacts": len(artifacts), "errors": errors}))
    return 0 if success else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f"Native platform experiment blocked: {error}", file=sys.stderr)
        sys.exit(1)
