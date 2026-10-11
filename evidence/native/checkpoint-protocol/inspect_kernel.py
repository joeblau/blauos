#!/usr/bin/env python3
"""Inspect a built T8103 kernel for the BlauOS physical checkpoint labels/marker.

Software-side build inspection only: this proves the compiled image carries the
contract's symbols and build-ID marker.  It is not physical execution evidence.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("physical_evidence",
                                              ROOT / "scripts/platform/physical_evidence.py")
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)


def main():
    kernel_path, build_id = Path(sys.argv[1]), sys.argv[2]
    data = kernel_path.read_bytes()
    result = evidence.inspect_kernel(data, build_id)
    marker = b"BLAUOS_PHYSICAL_BUILD_ID=" + build_id.encode() + b"\0"
    report = {
        "kernel": str(kernel_path),
        "kernel_sha256": hashlib.sha256(data).hexdigest(),
        "kernel_bytes": len(data),
        "build_id": build_id,
        "validator_inspect_kernel": result,
        "checkpoint_region_bytes": result["checkpoint_end"] - result["checkpoint_start"],
        "marker_occurrences_in_file": data.count(marker),
        "note": "Build-image inspection only; no physical execution is claimed.",
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
