#!/usr/bin/env python3
"""Audit source-built target files and bind the VM artifacts to the smoke manifest."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("--firmware", type=Path, required=True)
    parser.add_argument("--vars-template", type=Path, required=True)
    parser.add_argument("--symbols", type=Path, required=True)
    parser.add_argument("--qemu", default="qemu-system-aarch64")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    audit_path = args.output / "runtime-audit.json"
    command = ["python3", str(ROOT / "scripts/build/runtime.py"), "inspect", "--root", str(args.root),
               "--provenance", str(args.provenance), "--manifest", str(ROOT / "sources.lock.json"),
               "--output", str(audit_path)]
    subprocess.run(command, check=True)
    report = json.loads(audit_path.read_text())
    if report["status"] != "pass" or not report["offline_dependency_closure"]:
        raise SystemExit("Runtime dependency audit failed")
    artifacts = {"image": args.image, "firmware": args.firmware, "vars_template": args.vars_template,
                 "symbols": args.symbols}
    manifest = {"schema_version": 1, "architecture": "arm64", "runtime_audit_passed": True,
                "runtime_audit_sha256": digest(audit_path), "stock_kernel_used": False,
                "guest_execution_observed": False, "sha256": {name: digest(path) for name, path in artifacts.items()},
                "qemu_version": subprocess.check_output([args.qemu, "--version"], text=True).splitlines()[0],
                "source_lock_sha256": digest(ROOT / "sources.lock.json")}
    (args.output / "image-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    shutil.copyfile(args.provenance, args.output / "runtime-provenance.json")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
