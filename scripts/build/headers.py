#!/usr/bin/env python3
"""Regenerate public Apple XNU headers in a project-local SDK overlay.

The installed Xcode SDK remains a host input. Its contents are never copied
into evidence or modified. Public generated headers take precedence through
an isolated symlink overlay.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from blauos import clean_git_environment, verify_worktree_index


HEADER_BUILD_ENVIRONMENT_KEYS = frozenset({
    "PATH", "HOME", "TMPDIR", "TMP", "TEMP", "USER", "LOGNAME", "SHELL",
    "LANG", "LC_ALL", "DEVELOPER_DIR",
})


def clean_header_environment(environment=None) -> dict[str, str]:
    """Exclude inherited dry-run flags, source overrides and injected files."""
    values = os.environ if environment is None else environment
    return {name: value for name, value in values.items()
            if name in HEADER_BUILD_ENVIRONMENT_KEYS}


def fresh_header_build_root(path: Path) -> Path:
    """Reject stale outputs/cache inputs while preserving their contents."""
    path = path.resolve()
    if path.exists() and (not path.is_dir() or next(path.iterdir(), None) is not None):
        raise ValueError("Header generation requires a fresh build root; existing contents preserved")
    path.mkdir(parents=True, exist_ok=True)
    return path


def run(command: list[str], log: Path, cwd: Path | None = None) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    log.with_suffix(".command.json").write_text(
        json.dumps({"argv": command, "cwd": str(cwd or ROOT)}, indent=2) + "\n"
    )
    with log.open("w") as stream:
        result = subprocess.run(command, cwd=cwd or ROOT, stdout=stream,
                                stderr=subprocess.STDOUT, check=False,
                                env=clean_header_environment())
    print(f"{log.relative_to(ROOT) if log.is_relative_to(ROOT) else log}: exit {result.returncode}")
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, command)


def locked_source(manifest: Path, name: str, source_root: Path) -> Path:
    sources = json.loads(manifest.read_text())["sources"]
    found = next((item for item in sources if item["name"].lower() == name.lower()), None)
    if found is None:
        raise ValueError(f"Missing {name} in {manifest}")
    path = source_root / found["name"]
    if not path.is_dir():
        # A checked-out source may use its upstream spelling.
        path = source_root / name
    environment = clean_git_environment()
    worktree = Path(subprocess.check_output(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        env=environment, text=True).strip()).resolve()
    if worktree != path.resolve():
        raise ValueError(f"{name}: source directory is not its actual Git worktree")
    revision = subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"],
                                       env=environment, text=True).strip()
    if revision != found["revision"]:
        raise ValueError(f"{name}: expected {found['revision']}, found {revision}")
    if found.get("tree"):
        tree = subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD^{tree}"],
                                       env=environment, text=True).strip()
        if tree != found["tree"]:
            raise ValueError(f"{name}: source tree differs from lock")
    verify_worktree_index(path, found.get("sparse_paths"))
    dirty = subprocess.check_output(["git", "-C", str(path), "status", "--porcelain"],
                                    env=environment, text=True)
    if dirty:
        raise ValueError(f"{name}: source checkout has local changes")
    return path


def directory(path: Path, boundary: Path) -> None:
    """Expand symlinked SDK directories before any overlay write."""
    if not path.is_relative_to(boundary):
        raise ValueError(f"Overlay write outside build directory: {path}")
    if path == boundary:
        if path.is_symlink():
            raise ValueError(f"SDK overlay root must be a real directory: {path}")
        path.mkdir(parents=True, exist_ok=True)
        return
    directory(path.parent, boundary)
    if path.is_symlink():
        target = path.resolve(strict=True)
        if not target.is_dir():
            raise ValueError(f"Cannot overlay a file with a directory: {path}")
        children = list(target.iterdir())
        path.unlink()
        path.mkdir()
        for child in children:
            (path / child.name).symlink_to(child, target_is_directory=child.is_dir())
    elif not path.exists():
        path.mkdir()
    elif not path.is_dir():
        raise ValueError(f"Expected a directory: {path}")


def overlay(base: Path, additions: Path, destination: Path) -> None:
    if destination.is_symlink():
        raise ValueError(f"SDK overlay root must be a real directory: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    for child in base.iterdir():
        target = destination / child.name
        if not target.exists() and not target.is_symlink():
            target.symlink_to(child, target_is_directory=child.is_dir())
    for source in sorted(additions.rglob("*"), key=lambda p: len(p.parts)):
        target = destination / source.relative_to(additions)
        if source.is_dir() and not source.is_symlink():
            directory(target, destination)
        else:
            directory(target.parent, destination)
            if target.exists() or target.is_symlink():
                if target.is_dir() and not target.is_symlink():
                    raise ValueError(f"Refusing to replace a directory: {target}")
                target.unlink()
            if source.is_symlink():
                target.symlink_to(os.readlink(source))
            else:
                target.symlink_to(source)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "sources.lock.json")
    parser.add_argument("--source-root", type=Path, default=ROOT / ".blauos/sources")
    parser.add_argument("--build-root", type=Path, default=ROOT / ".blauos/build/kernel/headers")
    parser.add_argument("--evidence", type=Path, default=ROOT / "evidence/kernel/headers")
    parser.add_argument("--sdk", type=Path)
    parser.add_argument("--darwin-version", default="25.5.0")
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    if args.jobs < 1:
        raise ValueError("--jobs must be positive")
    # Reject old outputs before source discovery or tool/build execution.
    build = fresh_header_build_root(args.build_root)
    environment = clean_header_environment()
    sdk = (args.sdk or Path(subprocess.check_output(
        ["/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-path"],
        env=environment, text=True).strip())).resolve()
    evidence = args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    availability = locked_source(args.manifest, "AvailabilityVersions", args.source_root)
    availability_record = next(item for item in json.loads(args.manifest.read_text())["sources"]
                               if item["name"].lower() == "availabilityversions")
    availability_version = availability_record.get("tag", availability_record["revision"])
    xnu = locked_source(args.manifest, "xnu", args.source_root)
    av_obj, av_dst = build / "availability/obj", build / "availability/dst"
    av_dst.mkdir(parents=True, exist_ok=True)
    run(["cmake", "-S", str(availability), "-B", str(av_obj),
         f"-DSRCROOT={availability}", f"-DOBJROOT={av_obj}",
         f"-DDSTROOT={av_dst}", f"-DSYMROOT={build / 'availability/sym'}",
         "-DINSTALL_KERNEL_HEADERS=1", "-DDRIVERKIT=0",
         f"-DAV_VERSION={availability_version}"], evidence / "availability-configure.log")
    run(["cmake", "--build", str(av_obj), "--target", "install", "--parallel", str(args.jobs)], evidence / "availability-install.log")
    sdk_overlay = build / "MacOSX.sdk"
    overlay(sdk, av_dst, sdk_overlay)
    sdk_version = subprocess.check_output(
        ["/usr/bin/xcrun", "--sdk", str(sdk), "--show-sdk-version"],
        env=environment, text=True).strip()
    platform_path = subprocess.check_output(
        ["/usr/bin/xcrun", "--sdk", str(sdk), "--show-sdk-platform-path"],
        env=environment, text=True).strip()
    command = ["make", f"-j{args.jobs}", "installhdrs", f"SDKROOT={sdk_overlay}",
               f"SDKROOT_RESOLVED={sdk_overlay}", f"SDKVERSION={sdk_version}",
               f"PLATFORMPATH={platform_path}",
               "ARCH_CONFIGS=ARM64", "KERNEL_CONFIGS=DEVELOPMENT",
               f"RC_DARWIN_KERNEL_VERSION={args.darwin_version}",
               f"OBJROOT={build / 'xnu/obj'}", f"DSTROOT={build / 'xnu/dst'}",
               f"SYMROOT={build / 'xnu/sym'}", "CONCISE=1"]
    run(command, evidence / "xnu-installhdrs.log", xnu)
    overlay(sdk_overlay, build / "xnu/dst", sdk_overlay)
    files = []
    for path in sorted((build / "xnu/dst").rglob("*")):
        if path.is_file():
            files.append({"path": str(path.relative_to(build / "xnu/dst")),
                          "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    report = {"status": "headers-generated", "base_sdk": str(sdk),
              "sdk_overlay": str(sdk_overlay), "darwin_version": args.darwin_version,
              "manifest_sha256": hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
              "tool_environment": environment,
              "installed_files": files}
    (evidence / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(sdk_overlay)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Header regeneration blocked: {error}", file=sys.stderr)
        sys.exit(1)
