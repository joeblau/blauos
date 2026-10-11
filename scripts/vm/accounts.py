#!/usr/bin/env python3
"""Provision public-key-only accounts in an offline BlauOS filesystem tree.

The returned metadata must be applied inside the image builder's fakeroot
session after its default root ownership reset and before filesystem creation.
This tool never imports host account databases or private SSH keys.
"""
import argparse
import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import sys
import tempfile

NAME = re.compile(r"[a-z_][a-z0-9_-]{0,31}\Z")
PASSWORD_MARKER = "*NP*"
SERVICE_UID = 74
SERVICE_GID = 74
BASE_PASSWD = "root:*:0:0:root:/var/root:/bin/sh\nnobody:*:65534:65534:nobody:/var/empty:/bin/false\n"
BASE_MASTER = "root:*:0:0::0:0:root:/var/root:/bin/sh\nnobody:*:65534:65534::0:0:nobody:/var/empty:/bin/false\n"
BASE_GROUP = "wheel:*:0:root\nnobody:*:65534:\n"


def validate_account(username, uid, gid):
    if not isinstance(username, str) or not NAME.fullmatch(username) or username in {"root", "nobody", "sshd"}:
        raise ValueError("Use a non-reserved lowercase account name of at most 32 characters")
    for value, label in ((uid, "UID"), (gid, "GID")):
        if type(value) is not int or not 1000 <= value <= 60000:
            raise ValueError(f"{label} must be an integer between 1000 and 60000")


def public_keys(text):
    """Accept bare Ed25519 public keys; reject private keys and key options."""
    if not isinstance(text, str) or len(text.encode("utf-8")) > 64 * 1024:
        raise ValueError("Public key input must be at most 64 KiB")
    result = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if any(ord(c) < 32 and c != "\t" or ord(c) == 127 for c in line):
            raise ValueError("Public key contains a control character")
        fields = line.split(None, 2)
        if len(fields) < 2 or fields[0] != "ssh-ed25519":
            raise ValueError("Supply bare ssh-ed25519 public keys, without authorized_keys options or private keys")
        try:
            blob = base64.b64decode(fields[1], validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("Invalid public key base64") from error
        expected = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32)
        if len(blob) != len(expected) + 32 or not blob.startswith(expected) or not any(blob[len(expected):]):
            raise ValueError("Invalid Ed25519 SSH public-key wire encoding")
        key = "ssh-ed25519 " + base64.b64encode(blob).decode("ascii")
        if key not in result:
            result.append(key)
    if not result or len(result) > 16:
        raise ValueError("Supply between one and 16 distinct Ed25519 public keys")
    return result


def fingerprint(key):
    blob = base64.b64decode(key.split()[1], validate=True)
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")


def offline_root(root):
    root = Path(root).absolute()
    if root.is_symlink() or not root.is_dir() or root.resolve() == Path("/"):
        raise ValueError("Expected an ordinary offline root directory; host / is forbidden")
    return root


def safe_path(root, relative, kind=None):
    """Reject symlinks in every existing component of a managed path."""
    path = Path(relative)
    if path.is_absolute() or not path.parts or any(p in {".", ".."} for p in path.parts):
        raise ValueError("Managed paths must stay inside the offline root")
    current = root
    for index, part in enumerate(path.parts):
        current = current / part
        if current.is_symlink():
            raise ValueError(f"Refusing symlink in /{relative}")
        if current.exists():
            expected_dir = index < len(path.parts) - 1 or kind == "directory"
            if expected_dir and not current.is_dir():
                raise ValueError(f"Expected directory at /{relative}")
            if index == len(path.parts) - 1 and kind == "file" and not current.is_file():
                raise ValueError(f"Expected ordinary file at /{relative}")
    return current


def database(text, fields, label):
    records, names, numbers = {}, set(), set()
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        row = line.split(":")
        if len(row) != fields or not NAME.fullmatch(row[0]):
            raise ValueError(f"Malformed {label} entry")
        try:
            number = int(row[2])
            if not 0 <= number <= 2**32 - 2 or (fields != 4 and not 0 <= int(row[3]) <= 2**32 - 2):
                raise ValueError()
        except ValueError as error:
            raise ValueError(f"Invalid numeric ID in {label}") from error
        if row[0] in names or number in numbers:
            raise ValueError(f"Duplicate account name or numeric ID in {label}")
        names.add(row[0])
        numbers.add(number)
        records[row[0]] = row
    return records


def append_record(text, records, record, label):
    name, number = record[0], int(record[2])
    if name in records:
        if records[name] != record:
            raise ValueError(f"Existing {label} entry for {name} differs; refusing to overwrite")
        return text
    if any(int(row[2]) == number for row in records.values()):
        raise ValueError(f"{label} numeric ID {number} already belongs to another account")
    records[name] = record
    return text + ("\n" if text and not text.endswith("\n") else "") + ":".join(record) + "\n"


def metadata(username, uid, gid):
    validate_account(username, uid, gid)
    home = "var/home/" + username
    return [
        {"path": "etc", "uid": 0, "gid": 0, "mode": "0755", "kind": "directory"},
        *[{"path": "etc/" + name, "uid": 0, "gid": 0, "mode": mode, "kind": "file"}
          for name, mode in (("passwd", "0644"), ("master.passwd", "0600"), ("group", "0644"), ("shells", "0644"))],
        {"path": "var", "uid": 0, "gid": 0, "mode": "0755", "kind": "directory"},
        {"path": "var/home", "uid": 0, "gid": 0, "mode": "0755", "kind": "directory"},
        {"path": "var/empty", "uid": 0, "gid": 0, "mode": "0755", "kind": "directory"},
        {"path": home, "uid": uid, "gid": gid, "mode": "0700", "kind": "directory"},
        {"path": home + "/.ssh", "uid": uid, "gid": gid, "mode": "0700", "kind": "directory"},
        {"path": home + "/.ssh/authorized_keys", "uid": uid, "gid": gid, "mode": "0600", "kind": "file"},
    ]


def atomic_text(path, text, mode):
    descriptor, temporary = tempfile.mkstemp(prefix=".blauos-account-", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def provision(root, username, public_key_text, uid=1000, gid=1000):
    """Prepare accounts offline and return guest metadata without key material."""
    validate_account(username, uid, gid)
    keys = public_keys(public_key_text)
    root = offline_root(root)
    paths = metadata(username, uid, gid)
    for entry in paths:
        safe_path(root, entry["path"], entry["kind"])
    shell = safe_path(root, "bin/sh", "file")
    if not shell.exists() or not shell.stat().st_mode & 0o111:
        raise ValueError("Offline root must contain an executable /bin/sh")
    empty = safe_path(root, "var/empty", "directory")
    if empty.exists() and any(empty.iterdir()):
        raise ValueError("Privilege-separation /var/empty must be empty")
    texts = {}
    for name, fallback in (("passwd", BASE_PASSWD), ("master.passwd", BASE_MASTER), ("group", BASE_GROUP), ("shells", "/bin/sh\n")):
        path = safe_path(root, "etc/" + name, "file")
        if path.exists() and path.stat().st_size > 1024 * 1024:
            raise ValueError(f"Account database {name} exceeds 1 MiB")
        texts[name] = path.read_text() if path.exists() else fallback
    passwd = database(texts["passwd"], 7, "passwd")
    master = database(texts["master.passwd"], 10, "master.passwd")
    groups = database(texts["group"], 4, "group")
    if set(passwd) != set(master):
        raise ValueError("passwd and master.passwd have different account sets")
    for name, row in passwd.items():
        private = master[name]
        if row[2:4] + row[4:7] != private[2:4] + private[7:10]:
            raise ValueError(f"passwd and master.passwd disagree for {name}")
    for name, number, group, home, shell_name, description, marker in (
        ("sshd", SERVICE_UID, SERVICE_GID, "/var/empty", "/bin/false", "SSH privilege separation", "*"),
        (username, uid, gid, "/var/home/" + username, "/bin/sh", username, PASSWORD_MARKER),
    ):
        texts["passwd"] = append_record(texts["passwd"], passwd,
            [name, "*", str(number), str(group), description, home, shell_name], "passwd")
        texts["master.passwd"] = append_record(texts["master.passwd"], master,
            [name, marker, str(number), str(group), "", "0", "0", description, home, shell_name], "master.passwd")
        texts["group"] = append_record(texts["group"], groups,
            [name, "*", str(group), ""], "group")
    shells = texts["shells"].splitlines()
    if "/bin/sh" not in shells:
        texts["shells"] += ("\n" if texts["shells"] and not texts["shells"].endswith("\n") else "") + "/bin/sh\n"
    authorized = safe_path(root, "var/home/" + username + "/.ssh/authorized_keys", "file")
    authorized_text = "\n".join(keys) + "\n"
    if authorized.exists() and authorized.read_text() != authorized_text:
        raise ValueError("Existing authorized_keys differs; refusing to overwrite or rotate keys implicitly")
    # All caller input and managed paths have been checked before modifying the offline tree.
    for entry in paths:
        path = root / entry["path"]
        if entry["kind"] == "directory":
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(int(entry["mode"], 8))
    for name in ("passwd", "master.passwd", "group", "shells"):
        atomic_text(root / "etc" / name, texts[name], 0o600 if name == "master.passwd" else 0o644)
    if not authorized.exists():
        atomic_text(authorized, authorized_text, 0o600)
    else:
        authorized.chmod(0o600)
    return {"schema_version": 1, "username": username, "uid": uid, "gid": gid,
            "home": "/var/home/" + username, "shell": "/bin/sh", "authentication": "public-key-only",
            "public_key_fingerprints": [fingerprint(key) for key in keys],
            "service_account": {"name": "sshd", "uid": SERVICE_UID, "gid": SERVICE_GID, "chroot": "/var/empty"},
            "metadata": paths, "metadata_applied": False, "guest_login_verified": False}


def apply_metadata(root, receipt):
    """Apply only known account paths under root or within the builder's fakeroot."""
    root = offline_root(root)
    expected = metadata(receipt.get("username"), receipt.get("uid"), receipt.get("gid"))
    if receipt.get("schema_version") != 1 or receipt.get("metadata") != expected:
        raise ValueError("Account metadata differs from the validated account layout")
    if os.geteuid() != 0:
        raise ValueError("Apply guest ownership inside fakeroot or as root, never as the host user")
    for entry in expected:
        path = safe_path(root, entry["path"], entry["kind"])
        if not path.exists():
            raise ValueError(f"Missing managed account path /{entry['path']}")
    for entry in expected:
        path = root / entry["path"]
        os.chown(path, entry["uid"], entry["gid"], follow_symlinks=False)
        path.chmod(int(entry["mode"], 8))
        attributes = path.stat()
        if (attributes.st_uid, attributes.st_gid, stat.S_IMODE(attributes.st_mode)) != (
                entry["uid"], entry["gid"], int(entry["mode"], 8)):
            raise ValueError(f"Guest ownership or mode was not applied to /{entry['path']}")
    return {"schema_version": 1, "username": receipt["username"], "metadata_applied": True,
            "guest_login_verified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    prepare = actions.add_parser("provision", help="Add an initial named account to an offline root")
    prepare.add_argument("--root", type=Path, required=True)
    prepare.add_argument("--username", default="joeblau")
    prepare.add_argument("--uid", type=int, default=1000)
    prepare.add_argument("--gid", type=int, default=1000)
    prepare.add_argument("--public-key", type=Path, required=True)
    prepare.add_argument("--receipt", type=Path, required=True)
    ownership = actions.add_parser("apply-metadata", help="Apply validated guest IDs inside the image builder's fakeroot session")
    ownership.add_argument("--root", type=Path, required=True)
    ownership.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.action == "provision":
            if args.receipt.exists():
                raise ValueError("Use a new receipt path; prior account evidence is preserved")
            if not args.public_key.is_file() or args.public_key.stat().st_size > 64 * 1024:
                raise ValueError("Expected a public-key file of at most 64 KiB")
            result = provision(args.root, args.username, args.public_key.read_text(), args.uid, args.gid)
            args.receipt.parent.mkdir(parents=True, exist_ok=True)
            atomic_text(args.receipt, json.dumps(result, indent=2) + "\n", 0o600)
        else:
            result = apply_metadata(args.root, json.loads(args.receipt.read_text()))
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, TypeError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
