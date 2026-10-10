#!/usr/bin/env python3
"""Validate explicitly supplied handoff captures; never reads physical memory."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "platform/apple-m1/handoff/decoder.py"
spec = importlib.util.spec_from_file_location("blauos_m1_handoff", MODULE)
decoder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(decoder)


def read_bounded(path, limit):
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise decoder.HandoffError(f"{path}: input exceeds {limit} bytes")
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, choices=sorted(decoder.PROFILES))
    parser.add_argument("--boot-args", type=Path, required=True)
    parser.add_argument("--adt", type=Path, required=True)
    parser.add_argument("--memory-plan", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--synthetic-fixture", action="store_true")
    args = parser.parse_args()
    report = {"schema_version": 1, "status": "failed", "profile": args.profile,
              "validation_scope": "offline bytes and explicit memory assumptions",
              "synthetic_fixture": args.synthetic_fixture, "physical_execution_verified": False,
              "mmu_verified": False, "exceptions_verified": False, "errors": []}
    try:
        contract_path = ROOT / "platform/apple-m1/handoff/contract.json"
        contract = json.loads(contract_path.read_text())
        lock = json.loads((ROOT / "sources.lock.json").read_text())
        pins = {source["name"]: source["revision"] for source in lock["sources"]}
        for source in contract["profiles"][args.profile]["sources"]:
            if pins.get(source["source"]) != source["revision"]:
                raise decoder.HandoffError("source contract no longer matches the locked profile")
        report["source_contract_sha256"] = hashlib.sha256(contract_path.read_bytes()).hexdigest()
        raw = {"boot_args": read_bounded(args.boot_args, 4096),
               "adt": read_bounded(args.adt, decoder.MAX_ADT_BYTES),
               "memory_plan": read_bounded(args.memory_plan, 8 * 1024 * 1024)}
        report["input_sha256"] = {name: hashlib.sha256(data).hexdigest() for name, data in raw.items()}
        boot = decoder.parse_boot_args(raw["boot_args"], args.profile)
        tree = decoder.parse_adt(raw["adt"])
        plan = json.loads(raw["memory_plan"])
        memory = decoder.validate_memory_plan(boot, tree, plan, len(raw["adt"]))
        report.update(status="offline_validation_pass", boot_args=boot,
                      device_tree=decoder.summarize_adt(tree), memory=memory)
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as error:
        report["errors"].append(str(error))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: report[key] for key in ("status", "errors", "physical_execution_verified")}))
    return 0 if report["status"] == "offline_validation_pass" else 1


if __name__ == "__main__":
    sys.exit(main())
