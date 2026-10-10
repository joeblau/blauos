#!/usr/bin/env python3
"""Run genuine DEVELOPMENT ARM64 XNU experiments and retain honest evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile

from headers import ROOT, locked_source

HEADLESS_KEXT_TARGETS = [
    "PDArmPlatformExpert", "PDArmPCI", "IOPCIFamily", "IOStorageFamily",
    "ext4.kext", "Ext4FileSystemDriver.kext", "AppleFileSystemDriver.kext",
    "corecrypto.kext", "pthread.kext", "IONetworkingFamily.kext",
    "IOVirtIOFamily.kext", "IOVirtIOBlock.kext", "IOVirtIONet.kext",
]


def execute(command: list[str], cwd: Path, raw_log: Path, evidence: Path,
            name: str) -> int:
    raw_log.parent.mkdir(parents=True, exist_ok=True)
    evidence.mkdir(parents=True, exist_ok=True)
    record = {"argv": command, "cwd": str(cwd)}
    (evidence / f"{name}-command.json").write_text(json.dumps(record, indent=2) + "\n")
    with raw_log.open("w") as output:
        result = subprocess.run(command, cwd=cwd, stdout=output,
                                stderr=subprocess.STDOUT, check=False)
    data = raw_log.read_bytes()
    clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", data.decode(errors="replace")).replace("\r", "\n")
    lines = clean.splitlines()
    errors = [(index, line) for index, line in enumerate(lines)
              if re.search(r"(?:fatal )?error:|\*\*\*|Undefined symbols|command not found|No such file", line)]
    selected = set(range(min(60, len(lines)))) | set(range(max(0, len(lines) - 100), len(lines)))
    for index, _ in errors[:100]:
        selected.update(range(max(0, index - 3), min(len(lines), index + 6)))
    excerpt = "\n".join(f"{index + 1}: {lines[index]}" for index in sorted(selected)) + "\n"
    (evidence / f"{name}.log").write_text(excerpt)
    record.update({"exit_code": result.returncode, "raw_log": str(raw_log),
                   "raw_log_bytes": len(data), "raw_log_sha256": hashlib.sha256(data).hexdigest(),
                   "errors": [line for _, line in errors[:100]]})
    (evidence / f"{name}-result.json").write_text(json.dumps(record, indent=2) + "\n")
    print(f"{name}: exit {result.returncode}; evidence {evidence}")
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "sources.lock.json")
    parser.add_argument("--source-root", type=Path, default=ROOT / ".blauos/sources")
    parser.add_argument("--source-name", default="xnu")
    parser.add_argument("--build-root", type=Path, default=ROOT / ".blauos/build/kernel/development")
    parser.add_argument("--evidence", type=Path, default=ROOT / "evidence/kernel/development")
    parser.add_argument("--sdk", type=Path)
    parser.add_argument("--kdk", type=Path)
    parser.add_argument("--machine", default="VMAPPLE")
    parser.add_argument("--darwin-version", default="25.5.0")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--puredarwin", action="store_true")
    parser.add_argument("--patch", type=Path, action="append", default=[],
                        help="Reviewed patch applied to an isolated pinned source overlay")
    parser.add_argument("--with-kexts", action="store_true",
                        help="Build the source-pinned headless VIRT kernel extensions")
    parser.add_argument("--qemu-psci-hvc", action="store_true",
                        help="Enable the reviewed QEMU VIRT virtualization=false PSCI HVC profile")
    parser.add_argument("--make-setting", action="append", default=[], metavar="NAME=VALUE",
                        help="Additional explicit upstream make setting, retained in evidence")
    args = parser.parse_args()
    if args.jobs < 1:
        raise ValueError("--jobs must be positive")
    if args.with_kexts and (not args.puredarwin or args.machine != "VIRT"):
        raise ValueError("--with-kexts currently supports only the PureDarwin VIRT profile")
    if args.qemu_psci_hvc and (not args.puredarwin or args.machine != "VIRT" or not args.with_kexts):
        raise ValueError("--qemu-psci-hvc requires --with-kexts and the PureDarwin VIRT profile")
    source = locked_source(args.manifest, args.source_name, args.source_root).resolve()
    build = args.build_root.resolve()
    evidence = args.evidence.resolve()
    original_source = source
    amendments = []
    if args.patch:
        if not args.puredarwin:
            raise ValueError("--patch currently supports the PureDarwin source profile")
        overlay = build / "source-overlay"
        if overlay.exists():
            raise ValueError("Patched experiment requires a fresh build root")
        overlay.mkdir(parents=True)
        archive = build / "source.tar"
        with archive.open("wb") as output:
            subprocess.run(["git", "-C", str(source), "archive", "HEAD"], stdout=output, check=True)
        excluded_links = []
        def source_filter(member, destination):
            # The published tree contains an unused developer-local `result`
            # symlink. Never recreate absolute host paths in a build overlay.
            if member.issym() and Path(member.linkname).is_absolute():
                excluded_links.append(member.name)
                return None
            return tarfile.data_filter(member, destination)
        with tarfile.open(archive) as stream:
            stream.extractall(overlay, filter=source_filter)
        amendments.append({"source_archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                           "excluded_absolute_symlinks": excluded_links})
        subprocess.run(["git", "init", "--quiet", str(overlay)], check=True)
        for patch in args.patch:
            patch = patch.resolve()
            subprocess.run(["git", "apply", "--check", str(patch)], cwd=overlay, check=True)
            subprocess.run(["git", "apply", str(patch)], cwd=overlay, check=True)
            amendments.append({"path": str(patch), "sha256": hashlib.sha256(patch.read_bytes()).hexdigest()})
        source = overlay
    if args.qemu_psci_hvc:
        platform_source = source / "src/Kernel/Extensions/PDArmPlatformExpert"
        if ("option(PUREDARWIN_QEMU_PSCI_HVC" not in
                (platform_source / "CMakeLists.txt").read_text() or
                "PDArmPlatformExpert::haltRestart" not in
                (platform_source / "PDArmPlatformExpert.cpp").read_text()):
            raise ValueError("--qemu-psci-hvc requires the reviewed platform shutdown patch")
    sdk = args.sdk or Path(subprocess.check_output(["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True).strip())
    if args.puredarwin:
        command = ["cmake", "-S", str(source), "-B", str(build), "-G", "Unix Makefiles",
                   "-DCMAKE_POLICY_VERSION_MINIMUM=3.5", "-DPUREDARWIN_ARCH=arm64",
                   "-DCMAKE_OSX_ARCHITECTURES=arm64", f"-DCMAKE_OSX_SYSROOT={sdk}",
                   f"-DPUREDARWIN_ARM64_MACHINE_CONFIG={args.machine}",
                   "-DPUREDARWIN_XNU_KERNEL_CONFIG=DEVELOPMENT",
                   "-DPUREDARWIN_ENABLE_KERNEL=ON", "-DPUREDARWIN_ENABLE_LIBRARIES=ON",
                   "-DPUREDARWIN_ENABLE_TOOLS=ON", "-DPUREDARWIN_ENABLE_USERSPACE=OFF",
                   "-DPUREDARWIN_ENABLE_PROJECTS=OFF"]
        if args.with_kexts:
            # Current macOS Clang otherwise imports ___chkstk_darwin in
            # corecrypto. The target kernel does not export that host helper.
            command.extend(["-DCMAKE_C_FLAGS=-fno-stack-check",
                            "-DCMAKE_CXX_FLAGS=-fno-stack-check"])
        if args.qemu_psci_hvc:
            command.append("-DPUREDARWIN_QEMU_PSCI_HVC=ON")
        if execute(command, ROOT, build / "logs/configure.log", evidence, "configure"):
            return 1
        command = ["cmake", "--build", str(build), "--target", "xnu", "--parallel", str(args.jobs)]
        code = execute(command, ROOT, build / "logs/build.log", evidence, "build")
        if code == 0 and args.with_kexts:
            command = ["cmake", "--build", str(build), "--target",
                       *HEADLESS_KEXT_TARGETS, "--parallel", str(args.jobs)]
            code = execute(command, ROOT, build / "logs/kexts.log", evidence, "kexts")
    else:
        settings = sdk / "SDKSettings.json"
        base_sdk = settings.resolve().parent if settings.is_file() else sdk.resolve()
        version = subprocess.check_output(["xcrun", "--sdk", str(base_sdk), "--show-sdk-version"], text=True).strip()
        platform = subprocess.check_output(["xcrun", "--sdk", str(base_sdk), "--show-sdk-platform-path"], text=True).strip()
        command = ["make", f"-j{args.jobs}", f"SDKROOT={sdk.resolve()}",
                   f"SDKROOT_RESOLVED={sdk.resolve()}", f"SDKVERSION={version}",
                   f"PLATFORMPATH={platform}", "ARCH_CONFIGS=ARM64", "KERNEL_CONFIGS=DEVELOPMENT",
                   f"MACHINE_CONFIGS={args.machine}", f"RC_DARWIN_KERNEL_VERSION={args.darwin_version}",
                   f"OBJROOT={build / 'obj'}", f"SYMROOT={build / 'sym'}", f"DSTROOT={build / 'dst'}",
                   "CONCISE=1"]
        if args.kdk:
            command.append(f"KDKROOT={args.kdk.resolve()}")
        for setting in args.make_setting:
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", setting):
                raise ValueError(f"Invalid make setting: {setting}")
            command.append(setting)
        code = execute(command, source, build / "logs/build.log", evidence, "build")
    artifacts = []
    for path in sorted(build.rglob("kernel.development*")):
        if path.is_file() and path.suffix not in (".json", ".log", ".filelist", ".exp"):
            kind = subprocess.check_output(["file", str(path)], text=True).strip()
            if "Mach-O" in kind:
                artifacts.append({"path": str(path), "size": path.stat().st_size,
                                  "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "file": kind})
    report = {"status": "kernel-built-not-boot-validated" if code == 0 and artifacts else "blocked",
              "exit_code": code, "source": str(source), "configuration": "DEVELOPMENT ARM64",
              "source_revision": subprocess.check_output(["git", "-C", str(original_source), "rev-parse", "HEAD"], text=True).strip(),
              "source_amendments": amendments,
              "manifest_sha256": hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
              "machine": args.machine, "sdk": str(sdk), "artifacts": artifacts,
              "qemu_psci_hvc_profile": args.qemu_psci_hvc,
              "boot_validated": False}
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    return 0 if code == 0 and artifacts else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"XNU experiment blocked: {error}", file=sys.stderr)
        sys.exit(1)
