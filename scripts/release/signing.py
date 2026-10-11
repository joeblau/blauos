#!/usr/bin/env python3
"""Offline BlauOS release signing: keys, trust roots, rotation, revocation.

The repository does not depend on the `cryptography` package, so signatures
use a self-contained, pure-stdlib Ed25519 implementation following RFC 8032
(hashlib.sha512 only). It is validated against the RFC 8032 test vectors in
tests/test_release_signing.py. Pure-Python Ed25519 is slow (seconds per
operation) and is suitable only for small manifest payloads, which is exactly
the release use case; it is not a general-purpose crypto library.

Scope and trust policy:
- A signature covers the exact bytes of a manifest file (SHA-256 digest plus
  the Ed25519 signature over the canonical signing payload).
- The verifier trusts exactly the keys in an operator-supplied trust-root JSON
  file. Key status (active/retired/revoked), validity windows, rotation
  linkage, and revocation reasons are part of that trust root.
- Distribution signatures are independent of Apple machine-local boot-policy
  authorization (paired 1TR / LocalPolicy). A valid release signature never
  authorizes a kernel on hardware, and boot-policy authorization never
  authenticates a distribution.
- Signing is fully offline; every command writes a JSON receipt recording the
  exact argv and input/output file hashes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from release import (ReleaseError, canonical, file_sha256, hex64, load_json_file,  # noqa: E402
                     receipt, require, utcnow, write_json)

SIGNATURE_SCHEME = "ed25519-pure-python-rfc8032"
KEY_STATUSES = ("active", "retired", "revoked")

# --- RFC 8032 Ed25519, pure Python (reference-equivalent formulae) ---------

_Q = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _Q - 2, _Q)) % _Q
_I = pow(2, (_Q - 1) // 4, _Q)


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * pow(_D * y * y + 1, _Q - 2, _Q) % _Q
    x = pow(xx, (_Q + 3) // 8, _Q)
    if (x * x - xx) % _Q != 0:
        x = x * _I % _Q
    if x % 2 != 0:
        x = _Q - x
    return x


_BY = 4 * pow(5, _Q - 2, _Q) % _Q
_B = (_xrecover(_BY), _BY)
_IDENTITY = (0, 1)


def _edwards_add(p, q):
    x1, y1 = p
    x2, y2 = q
    common = _D * x1 * x2 * y1 * y2 % _Q
    x3 = (x1 * y2 + x2 * y1) * pow(1 + common, _Q - 2, _Q)
    y3 = (y1 * y2 + x1 * x2) * pow(1 - common, _Q - 2, _Q)
    return (x3 % _Q, y3 % _Q)


def _scalarmult(point, scalar: int):
    result = _IDENTITY
    addend = point
    while scalar:
        if scalar & 1:
            result = _edwards_add(result, addend)
        addend = _edwards_add(addend, addend)
        scalar >>= 1
    return result


def _encode_int(value: int) -> bytes:
    return value.to_bytes(32, "little")


def _decode_int(data: bytes) -> int:
    return int.from_bytes(data, "little")


def _encode_point(point) -> bytes:
    x, y = point
    return _encode_int(y | ((x & 1) << 255))


def _is_on_curve(point) -> bool:
    x, y = point
    return (-x * x + y * y - 1 - _D * x * x * y * y) % _Q == 0


def _decode_point(data: bytes):
    require(len(data) == 32, "ed25519: 32-byte encoded point required")
    y = _decode_int(data) & ((1 << 255) - 1)
    x = _xrecover(y)
    if bool(x & 1) != bool(data[31] & 0x80):
        x = _Q - x
    point = (x, y)
    require(_is_on_curve(point), "ed25519: point is not on the curve")
    return point


def _hint(data: bytes) -> int:
    return _decode_int(hashlib.sha512(data).digest())


def ed25519_public_key(secret_seed: bytes) -> bytes:
    require(len(secret_seed) == 32, "ed25519: 32-byte secret seed required")
    digest = hashlib.sha512(secret_seed).digest()
    scalar = 2 ** 254 + sum(2 ** i * ((digest[i // 8] >> (i % 8)) & 1) for i in range(3, 254))
    return _encode_point(_scalarmult(_B, scalar))


def ed25519_sign(secret_seed: bytes, message: bytes) -> bytes:
    require(len(secret_seed) == 32, "ed25519: 32-byte secret seed required")
    digest = hashlib.sha512(secret_seed).digest()
    scalar = 2 ** 254 + sum(2 ** i * ((digest[i // 8] >> (i % 8)) & 1) for i in range(3, 254))
    prefix = digest[32:]
    public = _encode_point(_scalarmult(_B, scalar))
    r = _hint(prefix + message) % _L
    encoded_r = _encode_point(_scalarmult(_B, r))
    s = (r + _hint(encoded_r + public + message) * scalar) % _L
    return encoded_r + _encode_int(s)


def ed25519_verify(public: bytes, message: bytes, signature: bytes) -> bool:
    if len(public) != 32 or len(signature) != 64:
        return False
    try:
        a = _decode_point(public)
        r = _decode_point(signature[:32])
    except ReleaseError:
        return False
    s = _decode_int(signature[32:])
    if s >= _L:
        return False
    h = _hint(signature[:32] + public + message) % _L
    return (_scalarmult(_B, s) ==
            _edwards_add(r, _scalarmult(a, h)))


# --- Key, trust-root, and signature file formats ----------------------------


def key_id_for(public_key_hex: str) -> str:
    return hashlib.sha256(bytes.fromhex(public_key_hex)).hexdigest()[:32]


def generate_key() -> dict:
    seed = secrets.token_bytes(32)
    public = ed25519_public_key(seed).hex()
    return {"schema_version": 1, "scheme": SIGNATURE_SCHEME,
            "created_at": utcnow(), "secret_key": seed.hex(), "public_key": public,
            "key_id": key_id_for(public),
            "custody": "Offline operator custody. This file is a private signing key; "
                       "never commit or distribute it."}


def load_private_key(path: Path) -> dict:
    key = load_json_file(path, 64 * 1024)
    require(key.get("schema_version") == 1 and key.get("scheme") == SIGNATURE_SCHEME,
            "private key: unsupported scheme")
    secret = bytes.fromhex(require_hex(key.get("secret_key"), 64, "secret_key"))
    public = bytes.fromhex(require_hex(key.get("public_key"), 64, "public_key"))
    require(ed25519_public_key(secret) == public, "private key: secret/public mismatch")
    require(key.get("key_id") == key_id_for(public.hex()), "private key: key_id mismatch")
    return {"secret": secret, "public": public, "key_id": key["key_id"]}


def require_hex(value, length: int, name: str) -> str:
    require(isinstance(value, str) and len(value) == length
            and all(c in "0123456789abcdef" for c in value),
            f"{name}: {length}-character lowercase hex required")
    return value


def empty_trust_root() -> dict:
    return {"schema_version": 1, "trust_root_kind": "blauos-release-signing",
            "created_at": utcnow(), "keys": [],
            "policy": "Only keys listed here with status active and an open validity window "
                      "may produce new signatures. Verification accepts signatures from active "
                      "or retired keys whose validity window covers signed_at; revoked keys and "
                      "unknown key IDs are always rejected."}


def load_trust_root(path: Path) -> dict:
    root = load_json_file(path)
    require(root.get("schema_version") == 1
            and root.get("trust_root_kind") == "blauos-release-signing",
            "trust root: unsupported schema")
    keys = root.get("keys")
    require(isinstance(keys, list), "trust root: keys array required")
    seen = set()
    for entry in keys:
        require(isinstance(entry, dict), "trust root key: object required")
        require_hex(entry.get("public_key"), 64, "trust root public_key")
        require(entry.get("key_id") == key_id_for(entry["public_key"]),
                "trust root: key_id does not match public key")
        require(entry["key_id"] not in seen, "trust root: duplicate key_id")
        seen.add(entry["key_id"])
        require(entry.get("status") in KEY_STATUSES, "trust root: unknown key status")
        if entry["status"] == "revoked":
            require(isinstance(entry.get("revoked_at"), str) and isinstance(entry.get("revocation_reason"), str),
                    "trust root: revoked keys need revoked_at and revocation_reason")
    return root


def find_key(root: dict, key_id: str) -> dict | None:
    return next((entry for entry in root["keys"] if entry["key_id"] == key_id), None)


def signing_payload(manifest_sha256: str, key_id: str, signed_at: str) -> bytes:
    return canonical({"schema_version": 1, "scheme": SIGNATURE_SCHEME,
                      "manifest_sha256": manifest_sha256, "key_id": key_id,
                      "signed_at": signed_at})


def sign_manifest(key_path: Path, manifest_path: Path, trust_root_path: Path | None) -> dict:
    key = load_private_key(key_path)
    require(manifest_path.is_file() and manifest_path.stat().st_size <= 32 * 1024 * 1024,
            "manifest: bounded regular file required")
    if trust_root_path is not None:
        root = load_trust_root(trust_root_path)
        entry = find_key(root, key["key_id"])
        require(entry is not None, "signing key is not in the trust root")
        require(entry["status"] == "active",
                f"signing key status is {entry['status']}; only active keys may sign")
    manifest_bytes = manifest_path.read_bytes()
    signed_at = utcnow()
    digest = hashlib.sha256(manifest_bytes).hexdigest()
    payload = signing_payload(digest, key["key_id"], signed_at)
    signature = ed25519_sign(key["secret"], payload)
    return {"schema_version": 1, "scheme": SIGNATURE_SCHEME,
            "key_id": key["key_id"], "manifest_sha256": digest,
            "signed_at": signed_at, "signature": signature.hex(),
            "coverage": "Exact manifest file bytes (SHA-256). Per-artifact coverage comes from "
                        "the artifact SHA-256 entries inside the signed manifest.",
            "distinct_from_boot_authorization": True}


def _within_window(entry: dict, moment: str) -> bool:
    not_before = entry.get("not_before")
    not_after = entry.get("not_after")
    if not_before is not None and moment < not_before:
        return False
    if not_after is not None and moment > not_after:
        return False
    return True


def verify_manifest(trust_root_path: Path, manifest_path: Path, signature_path: Path,
                    at: str | None = None) -> dict:
    root = load_trust_root(trust_root_path)
    require(manifest_path.is_file(), "manifest: file required")
    manifest_bytes = manifest_path.read_bytes()
    digest = hashlib.sha256(manifest_bytes).hexdigest()
    signature = load_json_file(signature_path)
    require(signature.get("schema_version") == 1 and signature.get("scheme") == SIGNATURE_SCHEME,
            "signature: unsupported scheme")
    key_id = signature.get("key_id")
    entry = find_key(root, key_id) if isinstance(key_id, str) else None
    require(entry is not None, "signature rejected: unknown key_id")
    require(entry["status"] != "revoked",
            f"signature rejected: key revoked ({entry.get('revocation_reason')})")
    signed_at = signature.get("signed_at")
    require(isinstance(signed_at, str), "signature: signed_at required")
    require(_within_window(entry, signed_at),
            "signature rejected: signed_at outside key validity window (old/superseded key)")
    moment = at or utcnow()
    require(_within_window(entry, moment),
            "signature rejected: key is outside its validity window at verification time")
    require(hex64(signature.get("manifest_sha256"), "signature.manifest_sha256") == digest,
            "signature rejected: manifest bytes differ from the signed digest (tampered)")
    payload = signing_payload(digest, key_id, signed_at)
    signature_bytes = bytes.fromhex(require_hex(signature.get("signature"), 128, "signature"))
    public = bytes.fromhex(entry["public_key"])
    require(ed25519_verify(public, payload, signature_bytes),
            "signature rejected: Ed25519 verification failed")
    return {"schema_version": 1, "verified": True, "key_id": key_id,
            "key_status": entry["status"], "manifest_sha256": digest,
            "signed_at": signed_at, "verified_at": moment,
            "boot_authorization_performed": False}


def add_key_to_root(root: dict, key: dict, status: str = "active",
                    not_before: str | None = None, not_after: str | None = None) -> dict:
    require(find_key(root, key["key_id"]) is None, "trust root: key already present")
    entry = {"key_id": key["key_id"], "public_key": key["public"].hex(),
             "status": status, "created_at": utcnow()}
    if not_before is not None:
        entry["not_before"] = not_before
    if not_after is not None:
        entry["not_after"] = not_after
    root["keys"].append(entry)
    return entry


def rotate_key(root: dict, old_key_id: str, new_key: dict) -> None:
    old = find_key(root, old_key_id)
    require(old is not None, "rotation: old key not in trust root")
    require(old["status"] == "active", "rotation: only an active key can be retired")
    entry = add_key_to_root(root, new_key)
    old["status"] = "retired"
    old["retired_at"] = utcnow()
    old["superseded_by"] = new_key["key_id"]
    entry["supersedes"] = old_key_id


def revoke_key(root: dict, key_id: str, reason: str) -> None:
    entry = find_key(root, key_id)
    require(entry is not None, "revocation: key not in trust root")
    require(entry["status"] != "revoked", "revocation: key already revoked")
    require(reason, "revocation: reason required")
    entry["status"] = "revoked"
    entry["revoked_at"] = utcnow()
    entry["revocation_reason"] = reason


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    keygen = commands.add_parser("keygen", help="Generate an offline Ed25519 signing key")
    keygen.add_argument("--output", type=Path, required=True, help="Private key JSON (kept offline)")
    keygen.add_argument("--trust-root", type=Path, help="Trust root to add the new active key to")

    sign = commands.add_parser("sign", help="Sign a manifest offline")
    sign.add_argument("--key", type=Path, required=True)
    sign.add_argument("--manifest", type=Path, required=True)
    sign.add_argument("--trust-root", type=Path, help="Require the key to be active in this root")
    sign.add_argument("--output", type=Path, required=True)
    sign.add_argument("--receipt", type=Path)

    verify = commands.add_parser("verify", help="Verify a signed manifest against a trust root")
    verify.add_argument("--trust-root", type=Path, required=True)
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--signature", type=Path, required=True)
    verify.add_argument("--at", help="ISO timestamp override for verification-time policy checks")
    verify.add_argument("--receipt", type=Path)

    init = commands.add_parser("init-trust-root", help="Create an empty trust root")
    init.add_argument("--output", type=Path, required=True)

    rotate = commands.add_parser("rotate", help="Retire an active key and add its active successor")
    rotate.add_argument("--trust-root", type=Path, required=True)
    rotate.add_argument("--old-key-id", required=True)
    rotate.add_argument("--new-key", type=Path, required=True, help="Private key JSON of the successor")
    rotate.add_argument("--receipt", type=Path)

    revoke = commands.add_parser("revoke", help="Revoke a key with a recorded reason")
    revoke.add_argument("--trust-root", type=Path, required=True)
    revoke.add_argument("--key-id", required=True)
    revoke.add_argument("--reason", required=True)
    revoke.add_argument("--receipt", type=Path)

    args = parser.parse_args()

    if args.command == "keygen":
        require(not args.output.exists(), "keygen: refusing to overwrite an existing key file")
        key = generate_key()
        write_json(args.output, key)
        os.chmod(args.output, stat.S_IRUSR | stat.S_IWUSR)
        summary = {"key_id": key["key_id"], "public_key": key["public_key"]}
        if args.trust_root:
            root = load_trust_root(args.trust_root) if args.trust_root.exists() else empty_trust_root()
            add_key_to_root(root, {"key_id": key["key_id"], "public": bytes.fromhex(key["public_key"])})
            write_json(args.trust_root, root)
            summary["trust_root"] = str(args.trust_root)
        print(json.dumps(summary))
        return 0

    if args.command == "init-trust-root":
        write_json(args.output, empty_trust_root(), exclusive=True)
        print(json.dumps({"trust_root": str(args.output)}))
        return 0

    if args.command == "sign":
        signature = sign_manifest(args.key, args.manifest, args.trust_root)
        write_json(args.output, signature)
        if args.receipt:
            receipt(args.receipt, sys.argv,
                    {"key_sha256": file_sha256(args.key),
                     "manifest_sha256": file_sha256(args.manifest)},
                    {"signature": str(args.output), "signature_sha256": file_sha256(args.output)},
                    {"status": "signed", "key_id": signature["key_id"],
                     "offline": True, "boot_authorization_performed": False})
        print(json.dumps({"signature": str(args.output), "key_id": signature["key_id"]}))
        return 0

    if args.command == "verify":
        result = verify_manifest(args.trust_root, args.manifest, args.signature, args.at)
        if args.receipt:
            receipt(args.receipt, sys.argv,
                    {"trust_root_sha256": file_sha256(args.trust_root),
                     "manifest_sha256": file_sha256(args.manifest),
                     "signature_sha256": file_sha256(args.signature)},
                    {}, result)
        print(json.dumps(result))
        return 0

    if args.command == "rotate":
        root = load_trust_root(args.trust_root)
        new_key = load_private_key(args.new_key)
        rotate_key(root, args.old_key_id, new_key)
        write_json(args.trust_root, root)
        if args.receipt:
            receipt(args.receipt, sys.argv, {"trust_root": str(args.trust_root)},
                    {"trust_root_sha256": file_sha256(args.trust_root)},
                    {"status": "rotated", "retired": args.old_key_id, "active": new_key["key_id"]})
        print(json.dumps({"retired": args.old_key_id, "active": new_key["key_id"]}))
        return 0

    if args.command == "revoke":
        root = load_trust_root(args.trust_root)
        revoke_key(root, args.key_id, args.reason)
        write_json(args.trust_root, root)
        if args.receipt:
            receipt(args.receipt, sys.argv, {"trust_root": str(args.trust_root)},
                    {"trust_root_sha256": file_sha256(args.trust_root)},
                    {"status": "revoked", "key_id": args.key_id, "reason": args.reason})
        print(json.dumps({"revoked": args.key_id}))
        return 0

    raise ReleaseError(f"unknown command {args.command}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ReleaseError, ValueError, KeyError, TypeError) as error:
        print(f"Release signing operation blocked: {error}", file=sys.stderr)
        sys.exit(1)
