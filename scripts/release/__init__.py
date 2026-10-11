#!/usr/bin/env python3
"""Shared helpers for the BlauOS release tooling package.

The release tools are software-level only: they generate, sign, and fetch
release metadata. Nothing here authorizes, stages, or boots a physical Mac;
hardware-gated acceptance is tracked separately (see
docs/audits/release-tooling.md).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
HEX64 = re.compile(r"[0-9a-f]{64}\Z")


class ReleaseError(ValueError):
    """Validation failure in release tooling."""


def require(condition, message):
    if not condition:
        raise ReleaseError(message)


def utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def load_json(data: bytes):
    """Strict JSON: reject duplicate keys and non-finite constants."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, f"duplicate JSON key: {key}")
            result[key] = value
        return result
    def reject_constant(value):
        raise ReleaseError(f"nonfinite JSON value: {value}")
    try:
        return json.loads(data, object_pairs_hook=unique, parse_constant=reject_constant)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ReleaseError("invalid JSON") from error


def load_json_file(path: Path, max_bytes: int = 32 * 1024 * 1024):
    require(path.is_file() and 0 < path.stat().st_size <= max_bytes,
            f"{path}: bounded regular JSON file required")
    return load_json(path.read_bytes())


def hex64(value, name: str) -> str:
    require(isinstance(value, str) and HEX64.fullmatch(value), f"{name}: lowercase SHA-256 required")
    return value


def write_json(path: Path, value, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2) + "\n"
    with path.open("x" if exclusive else "w") as stream:
        stream.write(payload)


def receipt(path: Path, argv: list[str], inputs: dict, outputs: dict, result: dict) -> None:
    """Record the exact invocation, input/output hashes, and JSON result."""
    record = {"schema_version": 1, "tool": Path(argv[0]).name if argv else "unknown",
              "argv": argv, "cwd": os.getcwd(), "recorded_at": utcnow(),
              "inputs": inputs, "outputs": outputs, "result": result}
    write_json(path, record)
    record["receipt_sha256"] = file_sha256(path)
    write_json(path, record)
