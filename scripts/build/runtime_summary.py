#!/usr/bin/env python3
"""Keep runtime audit evidence reviewable while retaining its complete raw JSON."""
import argparse
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        parser.error("Preserve the complete input separately from the summary")
    data = args.input.read_bytes()
    audit = json.loads(data)
    for artifact in audit.get("artifacts", []):
        for field in ("imports", "defined_external_symbols"):
            values = artifact.pop(field, None)
            if values is not None:
                canonical = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
                artifact[field + "_count"] = len(values)
                artifact[field + "_sha256"] = hashlib.sha256(canonical).hexdigest()
    audit["raw_audit"] = {"path": str(args.input), "sha256": hashlib.sha256(data).hexdigest(),
                          "bytes": len(data), "retained_complete": True}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(audit, indent=2) + "\n")
    print(json.dumps({"status": audit.get("status"), "errors": audit.get("errors", []),
                      "summary": str(args.output), "raw_audit": audit["raw_audit"]}))
    return 0 if audit.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
