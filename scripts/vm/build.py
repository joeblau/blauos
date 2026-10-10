#!/usr/bin/env python3
"""Prepare and build a pinned PureDarwin ARM64 VM without modifying upstream trees."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[2]
PIN = "1bbaeb2ee6aa41e6f792a0b1e2943cc295b3b290"


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def prepare(args):
    revision = subprocess.check_output(["git", "-C", str(args.source), "rev-parse", "HEAD"], text=True).strip()
    if revision != PIN:
        raise ValueError(f"Expected PureDarwin {PIN}, found {revision}")
    if subprocess.check_output(["git", "-C", str(args.source), "status", "--porcelain"], text=True).strip():
        raise ValueError("Upstream source tree must be clean; patches belong in the isolated build copy")
    args.output.mkdir(parents=True, exist_ok=True)
    source_copy = args.output / "source"
    if source_copy.exists():
        raise ValueError(f"Isolated source already exists: {source_copy}; use a fresh output directory")
    source_copy.mkdir()
    archive = args.output / "source.tar"
    with archive.open("wb") as target:
        subprocess.run(["git", "-C", str(args.source), "archive", PIN], stdout=target, check=True)
    excluded_links = []
    def source_filter(member, destination):
        try:
            return tarfile.data_filter(member, destination)
        except (tarfile.AbsoluteLinkError, tarfile.LinkOutsideDestinationError):
            # The pinned tree contains developer result symlinks. Preserve the
            # omission in evidence and never follow them into host artifacts.
            if not member.issym():
                raise
            excluded_links.append({"path": member.name, "target": member.linkname})
            return None
    with tarfile.open(archive) as source:
        source.extractall(source_copy, filter=source_filter)
    archive.unlink()
    probe = source_copy / "src/Userspace/blauos_probe"
    probe.mkdir()
    for name in ("CMakeLists.txt", "guest_probe.c"):
        shutil.copyfile(ROOT / "vm" / name, probe / name)
    shutil.copyfile(ROOT / "runtime/probes/runtime_probe.c", probe / "runtime_probe.c")
    shutil.copyfile(ROOT / "runtime/probes/bootstrap_probe.c", probe / "bootstrap_probe.c")
    cmake = source_copy / "src/Userspace/CMakeLists.txt"
    with cmake.open("a") as target:
        target.write("\nadd_subdirectory(blauos_probe)\n")
    metadata = {"schema_version": 1, "puredarwin_revision": revision,
                "upstream_flake_lock_sha256": digest(source_copy / "flake.lock"),
                "stock_kernel_used": False, "stock_runtime_copied": False,
                "source_path": str(source_copy.resolve()), "source_prepared": True,
                "excluded_external_symlinks": excluded_links,
                "build_passed": False, "guest_boot_passed": False}
    if args.sdk_archive:
        if not args.sdk_sha256 or not args.sdk_version:
            raise ValueError("SDK override requires explicit --sdk-sha256 and --sdk-version")
        actual = digest(args.sdk_archive)
        if actual != args.sdk_sha256:
            raise ValueError("Local SDK archive does not match its pinned hash")
        sdk_name = "MacOSX" + args.sdk_version + ".sdk.tar.xz"
        sdk_root = "MacOSX" + args.sdk_version + ".sdk"
        with tarfile.open(args.sdk_archive) as archive:
            first = archive.next()
            if first is None or first.name.rstrip("/") != sdk_root:
                raise ValueError(f"SDK archive must contain {sdk_root} as its root")
        sdk_derivation = source_copy / "nix/pkgs/toolchain/apple-sdk-pinned.nix"
        original = sdk_derivation.read_text()
        modified = original.replace("MacOSX11.3.sdk.tar.xz", sdk_name).replace(
            "9adc1373d3879e1973d28ad9f17c9051b02931674a3ec2a2498128989ece2cb1", actual)
        modified = modified.replace('sourceRoot = "MacOSX11.3.sdk";', f'sourceRoot = "{sdk_root}";')
        modified = modified.replace('version = "11.3";', f'version = "{args.sdk_version}";')
        sdk_derivation.write_text(modified)
        metadata["sdk_override"] = {"version": args.sdk_version, "archive_sha256": actual,
                                    "source_root": sdk_root, "proprietary_build_input": True}
    (args.output / "build-inputs.json").write_text(json.dumps(metadata, indent=2) + "\n")
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / ".blauos/sources/puredarwin-vm-next")
    parser.add_argument("--output", type=Path, default=ROOT / ".blauos/build/vm/image")
    parser.add_argument("--sdk-archive", type=Path)
    parser.add_argument("--sdk-sha256")
    parser.add_argument("--sdk-version")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    args.source = args.source.resolve()
    args.output = args.output.resolve()
    try:
        metadata = prepare(args)
        if args.prepare_only:
            print(json.dumps(metadata, indent=2))
            return 0
        if sys.platform == "darwin":
            raise ValueError("Upstream ARM64 image assembly requires a Linux builder; use --prepare-only then the documented container build")
        if args.sdk_archive:
            subprocess.run(["nix-store", "--add-fixed", "sha256", str(args.sdk_archive.resolve())], check=True)
        command = ["nix", "--extra-experimental-features", "nix-command flakes", "build", "--impure",
                   "--file", str(ROOT / "vm/smoke-image.nix"), "--argstr", "source", metadata["source_path"],
                   "--argstr", "sourceRevision", PIN, "--no-update-lock-file", "--no-link", "--json",
                   "--max-jobs", "1", "--cores", "2"]
        if args.dry_run:
            command.append("--dry-run")
        (args.output / "command.json").write_text(json.dumps(command, indent=2) + "\n")
        with (args.output / "build.log").open("w") as log, (args.output / "outputs.json").open("w") as output:
            subprocess.run(command, stdout=output, stderr=log, check=True, timeout=7200)
        metadata["build_passed"] = not args.dry_run
        metadata["dry_run_passed"] = args.dry_run
        (args.output / "build-inputs.json").write_text(json.dumps(metadata, indent=2) + "\n")
        print(json.dumps(metadata, indent=2))
        return 0
    except (ValueError, OSError, subprocess.SubprocessError, tarfile.TarError) as error:
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / "failure.json").write_text(json.dumps({"passed": False, "failure": str(error)}, indent=2) + "\n")
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
