#!/usr/bin/env python3
"""Compose independently built artifact layers without overwrites or lost provenance."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compose(layers, output):
    if output.exists():
        raise ValueError("Use a fresh output directory")
    records = {}
    providers = set()
    for directory, manifest in layers:
        metadata = json.loads(manifest.read_text())
        for provider in metadata.get("flat_namespace_providers", []):
            if not isinstance(provider, str) or not provider.startswith("/") or ".." in Path(provider).parts:
                raise ValueError(f"Invalid flat namespace provider: {provider!r}")
            providers.add(provider)
        for record in metadata["artifacts"]:
            path = record["path"]
            relative = path.removeprefix("/")
            if not relative or ".." in Path(relative).parts:
                raise ValueError(f"Invalid guest artifact path: {path}")
            binary = directory / relative
            if binary.is_symlink() or not binary.is_file() or digest(binary) != record["sha256"]:
                raise ValueError(f"Layer artifact differs from provenance: {path}")
            if path in records and records[path] != record:
                raise ValueError(f"Conflicting artifact provenance: {path}")
            records[path] = record
    if any(provider not in records for provider in providers):
        raise ValueError("Each flat namespace provider requires an artifact source/hash record")
    output.mkdir(parents=True)
    staged = output / "root"
    staged.mkdir()
    for directory, _ in layers:
        for source in sorted(directory.rglob("*")):
            target = staged / source.relative_to(directory)
            if source.is_symlink():
                if target.exists() or target.is_symlink():
                    if not target.is_symlink() or target.readlink() != source.readlink():
                        raise ValueError(f"Conflicting layer symlink: {target}")
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.symlink_to(source.readlink())
            elif source.is_dir():
                if target.is_symlink() or (target.exists() and not target.is_dir()):
                    raise ValueError(f"Conflicting layer directory: {target}")
                target.mkdir(parents=True, exist_ok=True)
            elif source.is_file():
                if target.is_symlink() or (target.exists() and (not target.is_file() or digest(target) != digest(source))):
                    raise ValueError(f"Conflicting layer file: {target}")
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    shutil.copy2(source, target)
            else:
                raise ValueError(f"Unsupported artifact type: {source}")
    provenance = output / "provenance.json"
    provenance.write_text(json.dumps({"schema_version": 1, "artifacts": list(records.values()),
                                      "flat_namespace_providers": sorted(providers)}, indent=2) + "\n")
    command = [sys.executable, str(ROOT / "scripts/build/runtime.py"), "inspect", "--root", str(staged),
               "--provenance", str(provenance), "--manifest", str(ROOT / "sources.lock.json"),
               "--output", str(output / "runtime-audit.json")]
    subprocess.run(command, check=True)
    result = {"schema_version": 1, "root": str(staged), "provenance": str(provenance),
              "runtime_audit": str(output / "runtime-audit.json"), "composed": True, "guest_boot_verified": False}
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--layer", nargs=2, type=Path, action="append", required=True, metavar=("ROOT", "PROVENANCE"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(compose([(root.resolve(), provenance.resolve()) for root, provenance in args.layer], args.output.resolve()), indent=2))
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
