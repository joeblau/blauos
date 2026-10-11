#!/usr/bin/env python3
"""Generate and independently verify BlauOS release manifests and SBOMs.

The manifest binds a release identifier to the repository's pinned inputs
(sources.lock.json, toolchain.lock.json, the reviewed patch series, and
transitive flake locks), component license notices, a component SBOM, the
observed build environment, per-artifact SHA-256 hashes, source provenance,
and compatibility metadata (supported machine/SoC identifiers, firmware and
boot-policy requirements, image formats, configuration schema, and data
migration constraints).

`verify` recomputes every pinned input hash against a local checkout so a
third party can confirm a published manifest without trusting the publisher.
The full third-party procedure is documented in docs/audits/release-tooling.md.

This tool is software-level evidence generation. It does not stage, authorize,
or boot anything on physical hardware.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from release import (ROOT, ReleaseError, canonical, file_sha256, hex64,  # noqa: E402
                     load_json_file, receipt, require, sha256, utcnow, write_json)

SCHEMA_VERSION = 1
PATCH_DIRECTORIES = ("patches/native", "patches/xnu", "patches/kc-tools", "vm/patches")
GIT_OBJECT_ID = re.compile(r"[0-9a-f]{40}\Z")  # pinned git revisions/trees are SHA-1 object IDs

# Reviewed compatibility metadata. These entries describe what the release
# claims to support; physical-M1 entries remain hardware-gated and are NOT
# acceptance evidence (Gate 2 is not run).
COMPATIBILITY = {
    "machines": [
        {"model": "Macmini9,1", "soc": "t8103", "platform": "J274AP",
         "status": "hardware-gated; no physical M1 acceptance has been performed"},
        {"model": "VMAPPLE", "soc": "virtual", "platform": "arm64-virtual",
         "status": "software-verified virtual experiment profile"},
        {"model": "VIRT", "soc": "virtual", "platform": "arm64-qemu-virt",
         "status": "software-verified virtual experiment profile"},
    ],
    "firmware_boot_policy": {
        "physical": "Paired 1TR with permissive security mode is required for a custom "
                    "kernel on Apple silicon; distribution signatures never substitute "
                    "for machine-local boot-policy authorization.",
        "virtual": "Generic EFI or QEMU direct kernel load per the recorded VM profiles.",
    },
    "image_format": {
        "kernel": "Thin little-endian Mach-O arm64e MH_EXECUTE (physical profile) or "
                  "arm64 (virtual profiles), plus a raw disk image for the guest userspace.",
        "manifest": "JSON, schema_version 1, canonical key order not required; integrity "
                    "is provided by the signed-manifest scheme in scripts/release/signing.py.",
    },
    "configuration_schema": {
        "path": "runtime/service-subset.json",
        "description": "Reviewed launchd service subset for the independent userspace.",
    },
    "data_migration_constraints": [
        "No persistent on-disk format is versioned by this release; guest disk images "
        "are disposable experiment state.",
        "Cache directories produced by scripts/release/download.py are safe to delete; "
        "verified artifacts are re-fetched and re-verified on demand.",
    ],
}

VERIFY_PROCEDURE = [
    "Clone the repository and check out the release tag named by manifest.release_id.",
    "Run: python3 scripts/release/manifest.py verify MANIFEST.json --receipt verify-receipt.json",
    "Confirm every entry under result.checks reports match=true; any mismatch exits nonzero.",
    "Rebuild artifacts with the pinned recipes (scripts/build/xnu.py, vm/smoke-image.nix) "
    "and compare SHA-256 values against manifest.artifacts.",
    "Verify the manifest signature with scripts/release/signing.py verify and the "
    "operator-distributed trust root.",
]


def hash_tree(paths: list[Path], base: Path, suffixes=(".patch", "series")) -> list[dict]:
    entries = []
    for directory in paths:
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if path.is_file() and (path.suffix in suffixes or path.name == "series"):
                entries.append({"path": str(path.relative_to(base)), "sha256": file_sha256(path),
                                "size": path.stat().st_size})
    return entries


def sbom_components(lock: dict) -> list[dict]:
    components = []
    for source in lock.get("sources", []):
        components.append({
            "type": "source", "name": source["name"], "version": source.get("tag", source["revision"]),
            "repository": source["url"], "revision": source["revision"],
            "tree": source.get("tree"), "license": source.get("license", "unknown"),
            "role": source.get("role", ""),
            "purl": f"pkg:github/{'/'.join(source['url'].rstrip('/').removesuffix('.git').split('/')[-2:])}"
                    f"@{source['revision']}",
        })
    for archive in lock.get("archives", []):
        components.append({
            "type": "archive", "name": archive["name"], "version": archive.get("version"),
            "url": archive["url"], "sha256": archive["sha256"],
            "license": archive.get("license", "unknown"), "role": archive.get("role", ""),
        })
    for transitive in lock.get("transitive_locks", []):
        components.append({
            "type": "transitive-lock", "name": transitive["path"], "sha256": transitive["sha256"],
            "scope": transitive.get("scope", ""),
        })
    return components


def generate(release_id: str, source_lock: Path, toolchain_lock: Path,
             artifacts_dir: Path | None, patches_root: Path = ROOT) -> dict:
    require(release_id and "/" not in release_id and ".." not in release_id,
            "release-id must be a plain label")
    lock = load_json_file(source_lock)
    toolchain = load_json_file(toolchain_lock)
    require(lock.get("schema_version") == 1, "sources.lock.json: schema_version 1 required")
    require(toolchain.get("schema_version") == 1, "toolchain.lock.json: schema_version 1 required")
    for source in lock.get("sources", []):
        require(isinstance(source.get("revision"), str)
                and GIT_OBJECT_ID.fullmatch(source["revision"]),
                f"sources.{source.get('name')}.revision: immutable pinned git revision required")
        if source.get("tree"):
            require(GIT_OBJECT_ID.fullmatch(source["tree"]),
                    f"sources.{source.get('name')}.tree: pinned git tree required")
        if source.get("sha256"):
            hex64(source["sha256"], f"sources.{source.get('name')}.sha256")
    patches = hash_tree([patches_root / name for name in PATCH_DIRECTORIES], patches_root)
    artifacts = []
    if artifacts_dir is not None:
        artifacts_dir = artifacts_dir.resolve()
        require(artifacts_dir.is_dir(), f"artifacts directory missing: {artifacts_dir}")
        for path in sorted(artifacts_dir.rglob("*")):
            if path.is_file() and not path.is_symlink():
                relative = path.relative_to(artifacts_dir)
                require(".." not in relative.parts, "artifact path escapes directory")
                artifacts.append({"path": str(relative), "size": path.stat().st_size,
                                  "sha256": file_sha256(path)})
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "manifest_kind": "blauos-release",
        "release_id": release_id,
        "created_at": utcnow(),
        "source_pins": {
            "lock_path": str(source_lock), "lock_sha256": file_sha256(source_lock),
            "sources": lock["sources"], "archives": lock.get("archives", []),
            "profiles": lock.get("profiles", {}), "transitive_locks": lock.get("transitive_locks", []),
            "unavailable_inputs": lock.get("unavailable_inputs", []),
            "license_policy": lock.get("license_policy", ""),
        },
        "toolchain": {
            "lock_path": str(toolchain_lock), "lock_sha256": file_sha256(toolchain_lock),
            "profile": toolchain.get("profile"), "host": toolchain.get("host"),
            "tools": toolchain.get("tools"), "sdk": toolchain.get("sdk"),
            "utilities": toolchain.get("utilities"),
            "required_external_inputs": toolchain.get("required_external_inputs", []),
            "limitations": toolchain.get("limitations", []),
        },
        "patches": patches,
        "sbom": {"format": "blauos-component-sbom-v1", "components": sbom_components(lock)},
        "artifacts": artifacts,
        "artifacts_dir": str(artifacts_dir) if artifacts_dir is not None else None,
        "provenance": {
            "scope": "Pinned sources, reviewed patch series, and recorded host toolchain. "
                     "Artifact provenance conventions follow vm/provenance.py: per-file "
                     "sha256, source repository, and pinned revision.",
            "workspace_revision_recording": "Build receipts record the git revision of each "
                                            "pinned source at build time (scripts/build/xnu.py).",
        },
        "compatibility": COMPATIBILITY,
        "third_party_verification": VERIFY_PROCEDURE,
        "physical_acceptance": {
            "gate_2_status": "NOT RUN; no physical M1 exists in the lab",
            "statement": "This manifest is software/compile-level evidence only. It does not "
                         "claim physical boot, staging, or authorization on Apple silicon.",
        },
    }
    manifest["manifest_content_sha256"] = sha256(canonical(manifest))
    return manifest


def verify(manifest_path: Path, source_lock: Path, toolchain_lock: Path,
           artifacts_dir: Path | None, patches_root: Path = ROOT) -> dict:
    manifest = load_json_file(manifest_path)
    require(manifest.get("schema_version") == SCHEMA_VERSION
            and manifest.get("manifest_kind") == "blauos-release",
            "manifest: unsupported release schema")
    result = {"schema_version": 1, "manifest": str(manifest_path),
              "manifest_sha256": file_sha256(manifest_path), "checks": [], "verified": False}
    expected_content = hex64(manifest.get("manifest_content_sha256"), "manifest_content_sha256")
    regenerated = dict(manifest)
    regenerated.pop("manifest_content_sha256", None)
    result["checks"].append({
        "name": "manifest_content_sha256", "match": sha256(canonical(regenerated)) == expected_content})
    checks = result["checks"]
    def compare(name, expected, actual):
        checks.append({"name": name, "expected": expected, "actual": actual, "match": expected == actual})
    compare("sources_lock_sha256", manifest["source_pins"]["lock_sha256"], file_sha256(source_lock))
    compare("toolchain_lock_sha256", manifest["toolchain"]["lock_sha256"], file_sha256(toolchain_lock))
    recorded_patches = {entry["path"]: entry["sha256"] for entry in manifest.get("patches", [])}
    current_patches = {entry["path"]: entry["sha256"]
                       for entry in hash_tree([patches_root / name for name in PATCH_DIRECTORIES], patches_root)}
    for path in sorted(set(recorded_patches) | set(current_patches)):
        compare(f"patch:{path}", recorded_patches.get(path), current_patches.get(path))
    recorded_artifacts = {entry["path"]: entry for entry in manifest.get("artifacts", [])}
    if recorded_artifacts:
        require(artifacts_dir is not None,
                "manifest records artifacts; pass --artifacts DIR with the published artifact set")
        root = artifacts_dir.resolve()
        for relative, entry in sorted(recorded_artifacts.items()):
            target = root / relative
            actual = file_sha256(target) if target.is_file() else None
            compare(f"artifact:{relative}", entry["sha256"], actual)
            if target.is_file():
                compare(f"artifact-size:{relative}", entry["size"], target.stat().st_size)
    result["verified"] = all(check.get("match") is True for check in checks)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    gen = commands.add_parser("generate", help="Generate a release manifest + SBOM from pinned inputs")
    gen.add_argument("--release-id", required=True)
    gen.add_argument("--source-lock", type=Path, default=ROOT / "sources.lock.json")
    gen.add_argument("--toolchain-lock", type=Path, default=ROOT / "toolchain.lock.json")
    gen.add_argument("--artifacts", type=Path, help="Directory of published artifact files")
    gen.add_argument("--output", type=Path, required=True)
    gen.add_argument("--receipt", type=Path)
    ver = commands.add_parser("verify", help="Third-party verification of a published manifest")
    ver.add_argument("manifest", type=Path)
    ver.add_argument("--source-lock", type=Path, default=ROOT / "sources.lock.json")
    ver.add_argument("--toolchain-lock", type=Path, default=ROOT / "toolchain.lock.json")
    ver.add_argument("--artifacts", type=Path)
    ver.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    if args.command == "generate":
        manifest = generate(args.release_id, args.source_lock, args.toolchain_lock, args.artifacts)
        write_json(args.output, manifest)
        outputs = {"manifest": str(args.output), "manifest_sha256": file_sha256(args.output)}
        if args.receipt:
            receipt(args.receipt, sys.argv,
                    {"source_lock_sha256": file_sha256(args.source_lock),
                     "toolchain_lock_sha256": file_sha256(args.toolchain_lock)},
                    outputs, {"status": "generated", "release_id": args.release_id,
                              "physical_acceptance": manifest["physical_acceptance"]["gate_2_status"]})
        print(json.dumps(outputs))
        return 0
    result = verify(args.manifest, args.source_lock, args.toolchain_lock, args.artifacts)
    outputs = {"verification": result}
    if args.receipt:
        receipt(args.receipt, sys.argv,
                {"manifest_sha256": file_sha256(args.manifest),
                 "source_lock_sha256": file_sha256(args.source_lock),
                 "toolchain_lock_sha256": file_sha256(args.toolchain_lock)},
                {"manifest_sha256": result["manifest_sha256"]},
                {"verified": result["verified"],
                 "mismatches": [c["name"] for c in result["checks"] if not c.get("match")]})
    print(json.dumps({"verified": result["verified"],
                      "checks": len(result["checks"]),
                      "mismatches": [c["name"] for c in result["checks"] if not c.get("match")]}))
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ReleaseError, ValueError, KeyError, TypeError) as error:
        print(f"Release manifest operation blocked: {error}", file=sys.stderr)
        sys.exit(1)
