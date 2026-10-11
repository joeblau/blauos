#!/usr/bin/env python3
"""Build, verify, index, and stage signed BlauOS guest packages (.bpm).

A .bpm package is an uncompressed ustar archive (the independent guest
runtime provides libSystem only: no zlib, no libarchive, no TLS), containing:

  manifest.json       package metadata + per-file SHA-256 (schema_version 1,
                      manifest_kind blauos-package), bound to a packages/*.json
                      recipe by path and hash
  manifest.json.sig   Ed25519 signature over the exact manifest bytes, in the
                      scripts/release/signing.py scheme and trust-root format
  payload/<path>      the packaged files, one per manifest files[] entry

The guest tool (vm/commands/bpm/bpm.c) verifies the signature against the
image-baked trust root, re-hashes every payload member, and fails closed
before writing anything when verification fails. This host tool is the
reference implementation of that verification (`inspect`).

Subcommands:
  build       turn a built layer root + packages/*.json recipe into a signed .bpm
  inspect     verify a .bpm (signature, manifest schema, payload hashes)
  index       create a signed index.json for a directory of .bpm packages
  stage-repo  stage an image layer with the trust root, a local file:// repo,
              and empty package-db directories for compose_root.py/assemble.py

All operations are offline and write JSON receipts in the release tooling
conventions. Nothing here boots or authorizes anything on physical hardware.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tarfile
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from release import (ROOT, ReleaseError, canonical, file_sha256, hex64,  # noqa: E402
                     load_json, load_json_file, receipt, require, sha256, utcnow, write_json)
import signing  # noqa: E402

MANIFEST_KIND = "blauos-package"
INDEX_KIND = "blauos-package-index"
NAME_RE = re.compile(r"[a-z0-9][a-z0-9+.-]{0,62}\Z")
VERSION_RE = re.compile(r"[0-9][A-Za-z0-9.+-]{0,62}\Z")
MODE_RE = re.compile(r"0[0-7]{3}\Z")
MAX_FILES = 4096
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
MAX_SIGNATURE_BYTES = 64 * 1024
FORBIDDEN_PREFIX = "/var/db/bpm"
TRUST_ROOT_PATH = "/etc/blauos/trust-root.json"


def valid_guest_path(path: str) -> str:
    """A package file path: absolute, normalized, outside package-manager state."""
    require(isinstance(path, str) and path.startswith("/") and 1 < len(path) <= 400,
            f"file path must be absolute and bounded: {path!r}")
    require(".." not in PurePosixPath(path).parts and "//" not in path and "." not in PurePosixPath(path).parts,
            f"file path must be normalized: {path!r}")
    require(all(32 <= ord(char) < 127 for char in path),
            f"file path must be printable ASCII: {path!r}")
    require(not path.startswith(FORBIDDEN_PREFIX + "/") and path != TRUST_ROOT_PATH,
            f"file path is package-manager state and cannot be packaged: {path!r}")
    member = "payload" + path
    require(len(member.encode()) <= 255, f"file path exceeds ustar limits: {path!r}")
    return path


def collect_files(root: Path) -> list[dict]:
    """Hash every regular file in a built layer root into manifest entries."""
    require(root.is_dir(), f"layer root missing: {root}")
    entries = []
    total = 0
    for path in sorted(root.rglob("*")):
        relative = "/" + path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ReleaseError(f"layer file is a symlink; packages ship regular files only: {relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ReleaseError(f"unsupported layer file type: {relative}")
        valid_guest_path(relative)
        size = path.stat().st_size
        require(size <= MAX_FILE_BYTES, f"file exceeds the package size bound: {relative}")
        total += size
        require(total <= MAX_TOTAL_BYTES, "package exceeds the total size bound")
        require(len(entries) < MAX_FILES, "package exceeds the file count bound")
        entries.append({"path": relative, "sha256": file_sha256(path), "size": size,
                        "mode": f"{stat.S_IMODE(path.stat().st_mode):04o}"})
    require(entries, "layer root contains no files to package")
    return entries


def load_recipe(recipe_path: Path, name: str) -> dict:
    recipe = load_json_file(recipe_path)
    require(recipe.get("schema_version") == 1 and isinstance(recipe.get("name"), str),
            "recipe: schema_version 1 with a name required")
    require(recipe["name"] == name, f"recipe name {recipe['name']!r} does not match package name {name!r}")
    depends = recipe.get("depends")
    require(isinstance(depends, list) and all(isinstance(item, str) and NAME_RE.fullmatch(item)
                                              for item in depends), "recipe depends: package names required")
    return recipe


def build_manifest(root: Path, recipe_path: Path, name: str, version: str) -> dict:
    require(NAME_RE.fullmatch(name), "package name must be a lowercase identifier")
    require(isinstance(version, str) and VERSION_RE.fullmatch(version),
            "package version must start with a digit and stay printable")
    recipe = load_recipe(recipe_path, name)
    files = collect_files(root)
    outputs = recipe.get("outputs")
    if isinstance(outputs, list) and outputs:
        missing = ["/" + output for output in outputs
                   if "/" + output not in {entry["path"] for entry in files}]
        require(not missing, f"recipe outputs missing from the packaged layer: {missing}")
    return {"schema_version": 1, "manifest_kind": MANIFEST_KIND, "name": name, "version": version,
            "depends": recipe.get("depends", []),
            "recipe": {"path": str(recipe_path), "sha256": file_sha256(recipe_path)},
            "summary": recipe.get("summary", ""), "built_at": utcnow(),
            "file_count": len(files), "payload_bytes": sum(entry["size"] for entry in files),
            "files": files}


def ustar_member(name: str, mode: int, size: int, directory: bool = False) -> tarfile.TarInfo:
    encoded = name.encode()
    if len(encoded) > 100:
        split = name.rfind("/", 0, 156)
        require(0 < split and len(encoded) - split - 1 <= 100, f"member does not fit ustar: {name}")
    info = tarfile.TarInfo(name)
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mode = mode
    info.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
    info.size = 0 if directory else size
    return info


def write_package(manifest_bytes: bytes, signature: dict, root: Path, output: Path) -> None:
    require(not output.exists() and not output.is_symlink(), f"refusing to overwrite: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    signature_bytes = (json.dumps(signature, indent=2) + "\n").encode()
    with tempfile.NamedTemporaryFile(dir=output.parent, prefix=output.name + ".", delete=False) as temp:
        temp_path = Path(temp.name)
    try:
        with tarfile.open(temp_path, "w", format=tarfile.USTAR_FORMAT) as tarball:
            tarball.addfile(ustar_member("manifest.json", 0o644, len(manifest_bytes)), io.BytesIO(manifest_bytes))
            tarball.addfile(ustar_member("manifest.json.sig", 0o644, len(signature_bytes)), io.BytesIO(signature_bytes))
            manifest = json.loads(manifest_bytes)
            seen_dirs = set()
            for entry in manifest["files"]:
                parts = ("payload" + entry["path"]).split("/")[:-1]
                for index in range(1, len(parts) + 1):
                    directory = "/".join(parts[:index])
                    if directory not in seen_dirs:
                        seen_dirs.add(directory)
                        tarball.addfile(ustar_member(directory, 0o755, 0, directory=True))
                member = "payload" + entry["path"]
                with (root / entry["path"].lstrip("/")).open("rb") as stream:
                    tarball.addfile(ustar_member(member, int(entry["mode"], 8), entry["size"]), stream)
        os.rename(temp_path, output)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def tar_members(tarball: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = []
    for member in tarball:
        relative = PurePosixPath(member.name)
        require(not relative.is_absolute() and ".." not in relative.parts and member.name not in ("", "."),
                f"unsafe package member: {member.name!r}")
        require(member.isfile() or member.isdir(),
                f"unsupported package member type: {member.name!r}")
        members.append(member)
    return members


def load_member(tarball: tarfile.TarFile, member: tarfile.TarInfo, limit: int) -> bytes:
    require(member.size <= limit, f"package member exceeds its bound: {member.name}")
    stream = tarball.extractfile(member)
    require(stream is not None, f"unreadable package member: {member.name}")
    return stream.read()


def validate_manifest(manifest) -> dict:
    require(isinstance(manifest, dict) and manifest.get("schema_version") == 1
            and manifest.get("manifest_kind") == MANIFEST_KIND, "manifest: unsupported package schema")
    require(isinstance(manifest.get("name"), str) and NAME_RE.fullmatch(manifest["name"]),
            "manifest: invalid package name")
    require(isinstance(manifest.get("version"), str) and VERSION_RE.fullmatch(manifest["version"]),
            "manifest: invalid package version")
    depends = manifest.get("depends")
    require(isinstance(depends, list) and all(isinstance(item, str) and NAME_RE.fullmatch(item)
                                              for item in depends), "manifest: invalid depends")
    files = manifest.get("files")
    require(isinstance(files, list) and 0 < len(files) <= MAX_FILES, "manifest: files array required")
    seen = set()
    for entry in files:
        require(isinstance(entry, dict), "manifest file entry: object required")
        path = valid_guest_path(entry.get("path"))
        require(path not in seen, f"manifest: duplicate file path {path}")
        seen.add(path)
        hex64(entry.get("sha256"), f"manifest {path} sha256")
        require(isinstance(entry.get("size"), int) and 0 <= entry["size"] <= MAX_FILE_BYTES,
                f"manifest {path}: invalid size")
        require(isinstance(entry.get("mode"), str) and MODE_RE.fullmatch(entry["mode"]),
                f"manifest {path}: invalid mode")
    recipe = manifest.get("recipe")
    require(isinstance(recipe, dict) and isinstance(recipe.get("path"), str)
            and recipe["path"].startswith("packages/"),
            "manifest: recipe binding (path + sha256) required")
    hex64(recipe.get("sha256"), "manifest recipe sha256")
    return manifest


def verify_package(package: Path, trust_root_path: Path) -> dict:
    """Reference verification, identical in policy to the guest bpm install."""
    require(package.is_file() and not package.is_symlink()
            and package.stat().st_size <= MAX_TOTAL_BYTES + MAX_MANIFEST_BYTES + MAX_SIGNATURE_BYTES,
            f"package: bounded regular file required: {package}")
    with tarfile.open(package, "r:") as tarball:
        members = tar_members(tarball)
        names = [member.name for member in members if member.isfile()]
        require(names.count("manifest.json") == 1 and names.count("manifest.json.sig") == 1,
                "package: exactly one manifest.json and one manifest.json.sig required")
        files = {member.name: member for member in members if member.isfile()}
        require(len(names) == len(files), "package: duplicate member names")
        manifest_bytes = load_member(tarball, files["manifest.json"], MAX_MANIFEST_BYTES)
        signature_bytes = load_member(tarball, files["manifest.json.sig"], MAX_SIGNATURE_BYTES)
        manifest = validate_manifest(load_json(manifest_bytes))
        payload = {}
        for member in members:
            if member.isfile() and member.name.startswith("payload/"):
                payload["/" + member.name.removeprefix("payload/")] = member
        recorded = {entry["path"]: entry for entry in manifest["files"]}
        require(set(payload) == set(recorded),
                "package payload members differ from the signed manifest file list")
        hashes = {}
        for path, member in sorted(payload.items()):
            entry = recorded[path]
            require(member.size == entry["size"], f"payload size differs from manifest: {path}")
            digest = hashlib.sha256(tarball.extractfile(member).read()).hexdigest()
            require(digest == entry["sha256"], f"payload hash differs from manifest: {path}")
            require(member.mode & 0o7777 == int(entry["mode"], 8), f"payload mode differs from manifest: {path}")
            hashes[path] = digest
    with tempfile.TemporaryDirectory(prefix="blauos-package-verify-") as folder:
        work = Path(folder)
        manifest_path = work / "manifest.json"
        signature_path = work / "manifest.json.sig"
        manifest_path.write_bytes(manifest_bytes)
        signature_path.write_bytes(signature_bytes)
        verification = signing.verify_manifest(trust_root_path, manifest_path, signature_path)
    return {"schema_version": 1, "verified": True, "package": str(package),
            "package_sha256": file_sha256(package), "name": manifest["name"],
            "version": manifest["version"], "depends": manifest["depends"],
            "recipe": manifest["recipe"], "key_id": verification["key_id"],
            "signed_at": verification["signed_at"], "file_count": len(recorded),
            "payload_sha256": hashes}


def stageable_guest_path(path: str) -> str:
    """Image-layer path check for stage-repo (lighter than package payload rules:
    the package-manager state directories are legitimate layer content here)."""
    require(isinstance(path, str) and path.startswith("/") and 1 < len(path) <= 400
            and ".." not in PurePosixPath(path).parts and "//" not in path
            and all(32 <= ord(char) < 127 for char in path),
            f"invalid layer path: {path!r}")
    return path


def cmd_build(args) -> dict:
    require(args.root.is_dir() and not args.root.is_symlink(), f"layer root missing: {args.root}")
    manifest = build_manifest(args.root.resolve(), args.recipe, args.name, args.version)
    manifest_bytes = (json.dumps(manifest, indent=2) + "\n").encode()
    require(len(manifest_bytes) <= MAX_MANIFEST_BYTES, "manifest exceeds its size bound")
    with tempfile.TemporaryDirectory(prefix="blauos-package-build-") as folder:
        manifest_path = Path(folder) / "manifest.json"
        manifest_path.write_bytes(manifest_bytes)
        signature = signing.sign_manifest(args.key, manifest_path, args.trust_root)
    write_package(manifest_bytes, signature, args.root.resolve(), args.output)
    result = {"status": "built", "package": str(args.output), "package_sha256": file_sha256(args.output),
              "name": manifest["name"], "version": manifest["version"],
              "file_count": manifest["file_count"], "payload_bytes": manifest["payload_bytes"],
              "key_id": signature["key_id"]}
    verified = None
    try:
        verified = verify_package(args.output, args.trust_root)
    except (ReleaseError, OSError, ValueError, KeyError, TypeError, tarfile.TarError,
            json.JSONDecodeError):
        args.output.unlink(missing_ok=True)
        raise
    require(verified["verified"] is True, "freshly built package failed re-verification")
    return result


def cmd_index(args) -> dict:
    require(args.repo_dir.is_dir(), f"repo directory missing: {args.repo_dir}")
    entries = []
    for package in sorted(args.repo_dir.glob("*.bpm")):
        verified = verify_package(package, args.trust_root)
        entries.append({"name": verified["name"], "version": verified["version"],
                        "file": package.name, "sha256": verified["package_sha256"],
                        "size": package.stat().st_size,
                        "depends": verified["depends"]})
    names = [entry["name"] for entry in entries]
    require(len(names) == len(set(names)), "repo: duplicate package name")
    index = {"schema_version": 1, "manifest_kind": INDEX_KIND, "created_at": utcnow(),
             "packages": entries}
    index_path = args.repo_dir / "index.json"
    write_json(index_path, index)
    signature = signing.sign_manifest(args.key, index_path, args.trust_root)
    write_json(args.repo_dir / "index.json.sig", signature)
    return {"status": "indexed", "packages": len(entries), "index": str(index_path),
            "index_sha256": file_sha256(index_path), "key_id": signature["key_id"]}


def cmd_stage_repo(args) -> dict:
    require(args.repo_dir.is_dir(), f"repo directory missing: {args.repo_dir}")
    index = load_json_file(args.repo_dir / "index.json")
    require(index.get("schema_version") == 1 and index.get("manifest_kind") == INDEX_KIND,
            "repo: build index.json first (package.py index)")
    signing.verify_manifest(args.trust_root, args.repo_dir / "index.json", args.repo_dir / "index.json.sig")
    require(not args.output.exists(), f"use a fresh output directory: {args.output}")
    root = args.output / "root"
    artifacts = []

    def stage(source: Path, guest_path: str) -> None:
        stageable_guest_path(guest_path)
        target = root / guest_path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        artifacts.append({"path": guest_path, "sha256": file_sha256(target), "source": "blauos-packages"})

    trust = args.trust_root
    signing.load_trust_root(trust)
    stage(trust, TRUST_ROOT_PATH)
    stage(args.repo_dir / "index.json", "/var/db/bpm/repo/index.json")
    stage(args.repo_dir / "index.json.sig", "/var/db/bpm/repo/index.json.sig")
    for entry in index["packages"]:
        stage(args.repo_dir / entry["file"], "/var/db/bpm/repo/" + entry["file"])
    (root / "var/db/bpm/db").mkdir(parents=True)
    (root / "var/db/bpm/tmp").mkdir()
    provenance = {"schema_version": 1, "artifacts": artifacts, "built": True,
                  "stock_runtime_used": False, "guest_execution_verified": False,
                  "note": "Package trust root, local file:// repo, and empty bpm database directories."}
    write_json(args.output / "provenance.json", provenance)
    return {"status": "staged", "layer": str(root), "artifacts": len(artifacts)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="Build and sign a .bpm from a built layer root")
    build.add_argument("--root", type=Path, required=True, help="Built layer root (e.g. .blauos/build/commands/vim-run-1/root)")
    build.add_argument("--recipe", type=Path, required=True, help="Binding packages/*.json recipe")
    build.add_argument("--name", required=True, help="Package name; must match the recipe")
    build.add_argument("--version", required=True)
    build.add_argument("--key", type=Path, required=True, help="Offline signing key JSON (never committed)")
    build.add_argument("--trust-root", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True, help="Fresh .bpm output path")
    build.add_argument("--receipt", type=Path)
    inspect_cmd = commands.add_parser("inspect", help="Verify a signed package (reference implementation)")
    inspect_cmd.add_argument("--trust-root", type=Path, required=True)
    inspect_cmd.add_argument("--package", type=Path, required=True)
    inspect_cmd.add_argument("--receipt", type=Path)
    index = commands.add_parser("index", help="Verify and index a directory of .bpm packages")
    index.add_argument("--repo-dir", type=Path, required=True)
    index.add_argument("--key", type=Path, required=True)
    index.add_argument("--trust-root", type=Path, required=True)
    index.add_argument("--receipt", type=Path)
    stage = commands.add_parser("stage-repo", help="Stage trust root + local repo as an image layer")
    stage.add_argument("--repo-dir", type=Path, required=True)
    stage.add_argument("--trust-root", type=Path, required=True)
    stage.add_argument("--output", type=Path, required=True)
    stage.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "build":
            result = cmd_build(args)
        elif args.command == "inspect":
            result = verify_package(args.package, args.trust_root)
        elif args.command == "index":
            result = cmd_index(args)
        else:
            result = cmd_stage_repo(args)
        if getattr(args, "receipt", None):
            inputs = {"trust_root_sha256": file_sha256(args.trust_root)}
            if getattr(args, "key", None):
                inputs["key_sha256"] = file_sha256(args.key)
            if args.command == "build":
                inputs["recipe_sha256"] = file_sha256(args.recipe)
            receipt(args.receipt, sys.argv, inputs,
                    {"result_sha256": file_sha256(args.output) if args.command == "build" else None},
                    {k: v for k, v in result.items() if not isinstance(v, dict) or k == "recipe"})
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ReleaseError, ValueError, KeyError, TypeError, tarfile.TarError, json.JSONDecodeError) as error:
        print(f"Package operation blocked: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
