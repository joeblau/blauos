#!/usr/bin/env python3
"""Run genuine DEVELOPMENT ARM64 XNU experiments and retain honest evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile

from headers import ROOT, clean_git_environment, locked_source

HEADLESS_KEXT_TARGETS = [
    "PDArmPlatformExpert", "PDArmPCI", "IOPCIFamily", "IOStorageFamily",
    "ext4.kext", "Ext4FileSystemDriver.kext", "AppleFileSystemDriver.kext",
    "corecrypto.kext", "pthread.kext", "IONetworkingFamily.kext",
    "IOVirtIOFamily.kext", "IOVirtIOBlock.kext", "IOVirtIONet.kext",
]

BUILD_ENVIRONMENT_KEYS = frozenset({
    "PATH", "HOME", "TMPDIR", "TMP", "TEMP", "USER", "LOGNAME", "SHELL",
    "LANG", "LC_ALL", "DEVELOPER_DIR",
})

# Capture the loaded wrapper's inputs before a long experiment. Later workspace
# edits must not relabel an already-running process as the new recipe.
BUILD_WRAPPER_INPUTS = [
    {"path": name, "sha256": hashlib.sha256((ROOT / name).read_bytes()).hexdigest()}
    for name in ("scripts/build/xnu.py", "scripts/build/headers.py", "scripts/blauos.py")
]


def reviewed_make_settings(settings: list[str], puredarwin: bool = False) -> dict[str, str]:
    """Allow diagnostics without replacing source/profile/toolchain inputs."""
    if puredarwin and settings:
        raise ValueError("--make-setting does not apply to the PureDarwin CMake profile")
    result = {}
    for setting in settings:
        name, separator, value = setting.partition("=")
        if not separator or name not in ("PLATFORM", "WARNFLAGS_STD"):
            raise ValueError(f"Unreviewed or reserved make setting: {name}")
        if name in result:
            raise ValueError(f"Duplicate make setting: {name}")
        if name == "PLATFORM":
            if value != "MacOSX":
                raise ValueError("The reviewed PLATFORM override is exactly MacOSX")
        else:
            flags = value.split()
            if not flags or len(flags) > 64 or any(not re.fullmatch(r"-W[A-Za-z0-9][A-Za-z0-9_-]*(?:=[A-Za-z0-9_-]+)?", flag)
                                                 for flag in flags):
                raise ValueError("WARNFLAGS_STD accepts only plain -W warning options")
            value = " ".join(flags)
        result[name] = value
    return result


def clean_build_environment(environment=None) -> dict[str, str]:
    """Exclude inherited make/CMake flags, source overrides and injected files."""
    values = os.environ if environment is None else environment
    return {name: value for name, value in values.items() if name in BUILD_ENVIRONMENT_KEYS}


def canonical_digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def fresh_build_root(path: Path) -> Path:
    """Preserve old outputs/cache inputs instead of claiming a fresh build."""
    path = path.resolve()
    if path.exists() and (not path.is_dir() or next(path.iterdir(), None) is not None):
        raise ValueError("Experiment requires a fresh build root; existing contents preserved")
    path.mkdir(parents=True, exist_ok=True)
    return path


def execute(command: list[str], cwd: Path, raw_log: Path, evidence: Path,
            name: str, *, environment=None, recipe_steps=None) -> int:
    raw_log.parent.mkdir(parents=True, exist_ok=True)
    evidence.mkdir(parents=True, exist_ok=True)
    record = {"argv": command, "cwd": str(cwd)}
    (evidence / f"{name}-command.json").write_text(json.dumps(record, indent=2) + "\n")
    with raw_log.open("w") as output:
        result = subprocess.run(command, cwd=cwd, env=clean_build_environment() if environment is None else environment, stdout=output,
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
    if recipe_steps is not None:
        recipe_steps.append(record)
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
    parser.add_argument("--darwin-version",
                        help="Apple make RC_DARWIN_KERNEL_VERSION (default 25.5.0); not a PureDarwin override")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--puredarwin", action="store_true")
    parser.add_argument("--patch", type=Path, action="append", default=[],
                        help="Reviewed patch applied to an isolated pinned source overlay")
    parser.add_argument("--with-kexts", action="store_true",
                        help="Build the source-pinned headless VIRT kernel extensions")
    parser.add_argument("--qemu-psci-hvc", action="store_true",
                        help="Enable the reviewed QEMU VIRT virtualization=false PSCI HVC profile")
    parser.add_argument("--virtualbuddy", action="store_true",
                        help="Build the explicit generic EFI virtio framebuffer profile with its three extra kexts")
    parser.add_argument("--make-setting", action="append", default=[], metavar="NAME=VALUE",
                        help="Reviewed PLATFORM=MacOSX or plain WARNFLAGS_STD diagnostics only")
    args = parser.parse_args()
    if args.jobs < 1:
        raise ValueError("--jobs must be positive")
    if args.puredarwin and args.darwin_version is not None:
        raise ValueError("--darwin-version is not consumed by the PureDarwin CMake profile")
    apple_darwin_version = None if args.puredarwin else (args.darwin_version or "25.5.0")
    if apple_darwin_version is not None and not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,2}", apple_darwin_version):
        raise ValueError("--darwin-version must be a numeric Apple Darwin version")
    make_settings = reviewed_make_settings(args.make_setting, args.puredarwin)
    if args.puredarwin and args.kdk:
        raise ValueError("--kdk is not consumed by the PureDarwin CMake profile")
    if args.with_kexts and (not args.puredarwin or args.machine != "VIRT"):
        raise ValueError("--with-kexts currently supports only the PureDarwin VIRT profile")
    if args.qemu_psci_hvc and (not args.puredarwin or args.machine != "VIRT" or not args.with_kexts):
        raise ValueError("--qemu-psci-hvc requires --with-kexts and the PureDarwin VIRT profile")
    if args.virtualbuddy and (not args.puredarwin or args.machine != "VIRT" or not args.with_kexts):
        raise ValueError("--virtualbuddy requires --with-kexts and the PureDarwin VIRT profile")
    build = fresh_build_root(args.build_root)
    source = locked_source(args.manifest, args.source_name, args.source_root).resolve()
    evidence = args.evidence.resolve()
    original_source = source
    git_environment = clean_git_environment()
    revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"],
                                       env=git_environment, text=True).strip()
    tree = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD^{tree}"],
                                   env=git_environment, text=True).strip()
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
            subprocess.run(["git", "-C", str(source), "archive", "HEAD"],
                           env=git_environment, stdout=output, check=True)
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
        subprocess.run(["git", "init", "--quiet", str(overlay)], env=git_environment, check=True)
        for index, patch in enumerate(args.patch, 1):
            patch = patch.resolve()
            patch_bytes = patch.read_bytes()
            patch_sha256 = hashlib.sha256(patch_bytes).hexdigest()
            snapshot = evidence / "inputs" / f"{index:02d}-{patch_sha256}.patch"
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            with snapshot.open("xb") as output:
                output.write(patch_bytes)
            subprocess.run(["git", "apply", "--check", str(snapshot)],
                           env=git_environment, cwd=overlay, check=True)
            subprocess.run(["git", "apply", str(snapshot)], env=git_environment, cwd=overlay, check=True)
            amendments.append({"path": str(patch), "snapshot_path": str(snapshot),
                               "sha256": patch_sha256})
        source = overlay
    if args.qemu_psci_hvc:
        platform_source = source / "src/Kernel/Extensions/PDArmPlatformExpert"
        if ("option(PUREDARWIN_QEMU_PSCI_HVC" not in
                (platform_source / "CMakeLists.txt").read_text() or
                "PDArmPlatformExpert::haltRestart" not in
                (platform_source / "PDArmPlatformExpert.cpp").read_text()):
            raise ValueError("--qemu-psci-hvc requires the reviewed platform shutdown patch")
    master_version = None
    if args.puredarwin:
        version_path = source / "src/Kernel/xnu/config/MasterVersion"
        effective_version = version_path.read_text().splitlines()[0].strip()
        master_version = {"path": "src/Kernel/xnu/config/MasterVersion",
                          "sha256": hashlib.sha256(version_path.read_bytes()).hexdigest(),
                          "version": effective_version}
    else:
        effective_version = apple_darwin_version
    environment = clean_build_environment()
    environment["DEVELOPER_DIR"] = str(Path(environment.get("DEVELOPER_DIR") or
        subprocess.check_output(["/usr/bin/xcode-select", "--print-path"], env=environment, text=True).strip()).resolve())
    sdk = (args.sdk or Path(subprocess.check_output(["/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-path"], env=environment, text=True).strip())).resolve()
    environment["SDKROOT"] = str(sdk)
    for variable, tool in (("CC", "clang"), ("CXX", "clang++")):
        environment[variable] = subprocess.check_output(["/usr/bin/xcrun", "--find", tool], env=environment, text=True).strip()
    recipe = {"schema_version": 1, "build_system": "puredarwin_cmake" if args.puredarwin else "apple_make",
              "manifest_sha256": hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
              "source_name": args.source_name, "source_revision": revision, "source_tree": tree,
              "machine": args.machine, "architecture_config": "ARM64", "kernel_config": "DEVELOPMENT",
              "original_source": str(original_source), "effective_source": str(source),
              "sdk": str(sdk), "kdk": str(args.kdk.resolve()) if args.kdk else None,
              "darwin_version": effective_version,
              "requested_apple_darwin_version": args.darwin_version,
              "applied_apple_darwin_version": apple_darwin_version,
              "source_master_version": master_version, "make_settings": make_settings,
              "build_wrapper_inputs": BUILD_WRAPPER_INPUTS,
              "source_amendments": amendments, "commands": [],
              "tool_environment": {name: environment[name] for name in sorted(environment)}}
    if args.puredarwin:
        command = ["cmake", "-S", str(source), "-B", str(build), "-G", "Unix Makefiles",
                   "-DCMAKE_POLICY_VERSION_MINIMUM=3.5", "-DPUREDARWIN_ARCH=arm64",
                   "-DCMAKE_OSX_ARCHITECTURES=arm64", f"-DCMAKE_OSX_SYSROOT={sdk}",
                   f"-DCMAKE_C_COMPILER={environment['CC']}", f"-DCMAKE_CXX_COMPILER={environment['CXX']}",
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
        if args.virtualbuddy:
            command.append("-DPUREDARWIN_VIRTUALBUDDY=ON")
        code = execute(command, ROOT, build / "logs/configure.log", evidence, "configure", environment=environment, recipe_steps=recipe["commands"])
        if code == 0:
            command = ["cmake", "--build", str(build), "--target", "xnu", "--parallel", str(args.jobs)]
            code = execute(command, ROOT, build / "logs/build.log", evidence, "build", environment=environment, recipe_steps=recipe["commands"])
        if code == 0 and args.with_kexts:
            command = ["cmake", "--build", str(build), "--target",
                       *HEADLESS_KEXT_TARGETS, "--parallel", str(args.jobs)]
            code = execute(command, ROOT, build / "logs/kexts.log", evidence, "kexts", environment=environment, recipe_steps=recipe["commands"])
        if code == 0 and args.virtualbuddy:
            command = ["cmake", "--build", str(build), "--target", "IOGraphicsFamily.kext",
                       "IOGOPFramebuffer.kext", "IOVirtIOGPU.kext", "--parallel", str(args.jobs)]
            code = execute(command, ROOT, build / "logs/graphics.log", evidence, "graphics", environment=environment, recipe_steps=recipe["commands"])
    else:
        settings = sdk / "SDKSettings.json"
        base_sdk = settings.resolve().parent if settings.is_file() else sdk.resolve()
        version = subprocess.check_output(["/usr/bin/xcrun", "--sdk", str(base_sdk), "--show-sdk-version"], env=environment, text=True).strip()
        platform = subprocess.check_output(["/usr/bin/xcrun", "--sdk", str(base_sdk), "--show-sdk-platform-path"], env=environment, text=True).strip()
        command = ["make", f"-j{args.jobs}", f"SDKROOT={sdk.resolve()}",
                   f"SDKROOT_RESOLVED={sdk.resolve()}", f"SDKVERSION={version}",
                   f"PLATFORMPATH={platform}", "ARCH_CONFIGS=ARM64", "KERNEL_CONFIGS=DEVELOPMENT",
                   f"MACHINE_CONFIGS={args.machine}", f"RC_DARWIN_KERNEL_VERSION={apple_darwin_version}",
                   f"SRCROOT={source}", f"CC={environment['CC']}", f"CXX={environment['CXX']}",
                   f"OBJROOT={build / 'obj'}", f"SYMROOT={build / 'sym'}", f"DSTROOT={build / 'dst'}",
                   "CONCISE=1"]
        if args.kdk:
            command.append(f"KDKROOT={args.kdk.resolve()}")
        command += [name + "=" + value for name, value in make_settings.items()]
        code = execute(command, source, build / "logs/build.log", evidence, "build", environment=environment, recipe_steps=recipe["commands"])
    artifacts = []
    for path in sorted(build.rglob("kernel.development*")):
        if path.is_file() and path.suffix not in (".json", ".log", ".filelist", ".exp"):
            kind = subprocess.check_output(["file", str(path)], text=True).strip()
            if "Mach-O" in kind:
                artifacts.append({"path": str(path), "size": path.stat().st_size,
                                  "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "file": kind})
    report = {"status": "kernel-built-not-boot-validated" if code == 0 and artifacts else "blocked",
              "exit_code": code, "source": str(source), "configuration": "DEVELOPMENT ARM64",
              "source_revision": revision,
              "source_amendments": amendments,
              "manifest_sha256": hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
              "machine": args.machine, "sdk": str(sdk), "artifacts": artifacts,
              "qemu_psci_hvc_profile": args.qemu_psci_hvc,
              "recipe": recipe, "recipe_sha256": canonical_digest(recipe),
              "build_wrapper_sha256": BUILD_WRAPPER_INPUTS[0]["sha256"],
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
