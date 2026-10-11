#!/usr/bin/env python3
"""Validate versioned declarative BlauOS server configuration offline.

The schema lives in runtime/config-schema.json. Boot-time application of a
validated configuration inside the guest, idempotent re-application with
preserved data, and migrations are NOT implemented here; gate dependencies
#59 and #60 remain open. A passing result is a syntax/semantics check only —
it is not guest acceptance or physical-hardware evidence. Secrets never
appear in the configuration or in the emitted result; only opaque references
(vault://, file://, keychain://) are accepted.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import ipaddress
import json
from pathlib import Path
import posixpath
import re
import stat
import struct
import sys


PROJECT = Path(__file__).resolve().parents[2]
SCHEMA = PROJECT / "runtime/config-schema.json"
SERVICE_SUBSET = PROJECT / "runtime/service-subset.json"
MAX_CONFIG_BYTES = 256 * 1024
MAX_ERRORS = 32

ACCOUNT_NAME = re.compile(r"[a-z_][a-z0-9_-]{0,31}\Z")
RESERVED_ACCOUNTS = {"root", "nobody", "sshd"}
HOSTNAME_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\Z")
SERVICE_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,62}(\.[A-Za-z0-9][A-Za-z0-9-]{0,62})+\Z")
IMAGE_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,63}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
DEVICE = re.compile(r"/dev/[A-Za-z0-9][A-Za-z0-9._/-]{0,63}\Z")
INTERFACE_NAME = re.compile(r"[a-z][a-z0-9]{0,15}\Z")
SLUG = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
SECRET_REF = re.compile(r"(?:vault|file|keychain)://[^\s\"']{1,251}\Z")
SECRET_MARKERS = ("PRIVATE KEY-----",)
FILESYSTEMS = ("ext4", "apfs")
IMAGE_CHANNELS = ("stable", "nightly", "local")

CONFIG_KEYS = {"schema_version", "hostname", "image", "accounts", "network",
               "services", "volumes", "fleet", "secret_references"}
IMAGE_KEYS = {"channel", "version", "sha256"}
ACCOUNT_KEYS = {"name", "uid", "gid", "ssh_authorized_keys"}
NETWORK_KEYS = {"interfaces", "dns", "mtu"}
INTERFACE_KEYS = {"name", "mode", "address", "prefix_length", "gateway"}
SERVICE_KEYS = {"label", "program", "program_arguments", "run_at_load", "keep_alive", "mach_services"}
VOLUME_KEYS = {"name", "device", "filesystem", "mount_point", "read_only", "required"}
FLEET_KEYS = {"role", "labels", "inventory_id"}
SECRET_KEYS = {"name", "ref"}

LIMITATIONS = [
    "Validation is offline; it does not apply or verify configuration in a guest.",
    "Boot-time application, idempotent re-application, and migrations are open (issue #62).",
    "Gate dependencies #59 and #60 are unresolved; no physical M1 acceptance exists.",
]


class Collector:
    """Accumulate bounded errors instead of aborting on the first violation."""

    def __init__(self):
        self.errors = []
        self.suppressed = 0

    def error(self, message):
        if len(self.errors) < MAX_ERRORS:
            self.errors.append(message)
        else:
            self.suppressed += 1

    def check(self, condition, message):
        if not condition:
            self.error(message)
        return condition

    def finish(self):
        if self.suppressed:
            self.errors.append(f"error limit {MAX_ERRORS} reached; {self.suppressed} further error(s) suppressed")
        return self.errors


def load_json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result
    def reject_constant(value):
        raise ValueError(f"nonfinite JSON value: {value}")
    return json.loads(data, object_pairs_hook=unique, parse_constant=reject_constant)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def is_int(value):
    return type(value) is int


def bounded_string(collector, value, name, maximum, pattern=None):
    if not collector.check(isinstance(value, str), f"{name}: string required"):
        return None
    collector.check(0 < len(value) <= maximum, f"{name}: length outside supported bounds")
    if pattern is not None:
        collector.check(pattern.fullmatch(value) is not None, f"{name}: unsupported characters or format")
    return value


def absolute_guest_path(collector, value, name):
    """Absolute, normalized guest path without escapes; '/' itself is rejected."""
    if not collector.check(isinstance(value, str) and value.startswith("/"),
                           f"{name}: absolute guest path required"):
        return None
    normalized = posixpath.normpath(value)
    collector.check(len(value) <= 1024 and normalized == value and value != "/",
                    f"{name}: must be a normalized path below / without '.' or '..' components")
    return value


def ipv4(collector, value, name):
    if not collector.check(isinstance(value, str), f"{name}: IPv4 string required"):
        return None
    try:
        ipaddress.IPv4Address(value)
    except ValueError:
        collector.error(f"{name}: invalid IPv4 address")
        return None
    return value


def ed25519_key(collector, value, name):
    """Bare Ed25519 public key; mirrors scripts/vm/accounts.py wire checks."""
    if not collector.check(isinstance(value, str) and len(value) <= 256,
                           f"{name}: ssh-ed25519 key string required"):
        return None
    fields = value.split(" ")
    if not collector.check(len(fields) == 2 and fields[0] == "ssh-ed25519",
                           f"{name}: bare ssh-ed25519 key without options or comments required"):
        return None
    try:
        blob = base64.b64decode(fields[1], validate=True)
    except (binascii.Error, ValueError):
        collector.error(f"{name}: invalid public key base64")
        return None
    expected = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32)
    collector.check(len(blob) == len(expected) + 32 and blob.startswith(expected) and any(blob[len(expected):]),
                    f"{name}: invalid Ed25519 SSH public-key wire encoding")
    return value


def scan_for_inline_secrets(collector, value, path="config"):
    if isinstance(value, str):
        for marker in SECRET_MARKERS:
            if marker in value:
                collector.error(f"{path}: inline secret material is forbidden; use secret_references")
                return
    elif isinstance(value, list):
        for index, item in enumerate(value):
            scan_for_inline_secrets(collector, item, f"{path}[{index}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            scan_for_inline_secrets(collector, item, f"{path}.{key}")


def reject_unknown(collector, record, allowed, name):
    if not isinstance(record, dict):
        collector.error(f"{name}: JSON object required")
        return False
    for key in sorted(set(record) - allowed):
        collector.error(f"{name}: unknown field '{key}'")
    return True


def validate_hostname(collector, value):
    if not collector.check(isinstance(value, str) and 0 < len(value) <= 253,
                           "hostname: RFC 1123 hostname of at most 253 characters required"):
        return
    collector.check(all(HOSTNAME_LABEL.fullmatch(label) for label in value.split(".")),
                    "hostname: each label must be 1-63 lowercase alphanumeric characters or hyphens")


def validate_image(collector, image):
    if not reject_unknown(collector, image, IMAGE_KEYS, "image"):
        return
    channel = image.get("channel", "stable")
    collector.check(channel in IMAGE_CHANNELS, "image.channel: stable, nightly, or local required")
    bounded_string(collector, image.get("version"), "image.version", 64, IMAGE_VERSION)
    digest = image.get("sha256")
    if digest is not None:
        collector.check(isinstance(digest, str) and HEX64.fullmatch(digest),
                        "image.sha256: lowercase SHA-256 digest required")


def validate_accounts(collector, accounts):
    if not collector.check(isinstance(accounts, list) and len(accounts) <= 16,
                           "accounts: array of at most 16 accounts required"):
        return
    names, uids, gids = set(), set(), set()
    for index, account in enumerate(accounts):
        name = f"accounts[{index}]"
        if not reject_unknown(collector, account, ACCOUNT_KEYS, name):
            continue
        username = bounded_string(collector, account.get("name"), name + ".name", 32, ACCOUNT_NAME)
        if username is not None:
            collector.check(username not in RESERVED_ACCOUNTS,
                            f"{name}.name: reserved account '{username}' cannot be a login account")
            collector.check(username not in names, f"{name}.name: duplicate account '{username}'")
            names.add(username)
        for field in ("uid", "gid"):
            value = account.get(field)
            if collector.check(is_int(value) and 1000 <= value <= 60000,
                               f"{name}.{field}: integer between 1000 and 60000 required"):
                seen = uids if field == "uid" else gids
                collector.check(value not in seen, f"{name}.{field}: {field.upper()} {value} is already used")
                seen.add(value)
        keys = account.get("ssh_authorized_keys")
        if not collector.check(isinstance(keys, list) and 1 <= len(keys) <= 16,
                               f"{name}.ssh_authorized_keys: 1 to 16 public keys required"):
            continue
        distinct = set()
        for key_index, key in enumerate(keys):
            if ed25519_key(collector, key, f"{name}.ssh_authorized_keys[{key_index}]") is not None:
                collector.check(key not in distinct, f"{name}.ssh_authorized_keys[{key_index}]: duplicate key")
                distinct.add(key)


def validate_services(collector, services):
    if not collector.check(isinstance(services, list) and len(services) <= 64,
                           "services: array of at most 64 launchd jobs required"):
        return
    labels = set()
    for index, service in enumerate(services):
        name = f"services[{index}]"
        if not reject_unknown(collector, service, SERVICE_KEYS, name):
            continue
        label = bounded_string(collector, service.get("label"), name + ".label", 200, SERVICE_LABEL)
        if label is not None:
            collector.check(label not in labels, f"{name}.label: duplicate service label '{label}'")
            labels.add(label)
        absolute_guest_path(collector, service.get("program"), name + ".program")
        arguments = service.get("program_arguments", [])
        if collector.check(isinstance(arguments, list) and len(arguments) <= 32,
                           f"{name}.program_arguments: array of at most 32 strings required"):
            for argument_index, argument in enumerate(arguments):
                collector.check(isinstance(argument, str) and len(argument) <= 1024,
                                f"{name}.program_arguments[{argument_index}]: string of at most 1024 characters required")
        for field in ("run_at_load", "keep_alive"):
            value = service.get(field)
            if value is not None:
                collector.check(type(value) is bool, f"{name}.{field}: boolean required")
        mach = service.get("mach_services", [])
        if collector.check(isinstance(mach, list) and len(mach) <= 16,
                           f"{name}.mach_services: array of at most 16 service names required"):
            for service_index, service_name in enumerate(mach):
                bounded_string(collector, service_name, f"{name}.mach_services[{service_index}]", 200, SERVICE_LABEL)


def validate_network(collector, network):
    if not reject_unknown(collector, network, NETWORK_KEYS, "network"):
        return
    interfaces = network.get("interfaces", [])
    names = set()
    if collector.check(isinstance(interfaces, list) and len(interfaces) <= 8,
                       "network.interfaces: array of at most 8 interfaces required"):
        for index, interface in enumerate(interfaces):
            name = f"network.interfaces[{index}]"
            if not reject_unknown(collector, interface, INTERFACE_KEYS, name):
                continue
            interface_name = bounded_string(collector, interface.get("name"), name + ".name", 16, INTERFACE_NAME)
            if interface_name is not None:
                collector.check(interface_name not in names, f"{name}.name: duplicate interface '{interface_name}'")
                names.add(interface_name)
            mode = interface.get("mode")
            if not collector.check(mode in ("dhcp", "static"), f"{name}.mode: dhcp or static required"):
                continue
            if mode == "dhcp":
                for field in ("address", "prefix_length", "gateway"):
                    collector.check(field not in interface, f"{name}.{field}: forbidden in dhcp mode")
            else:
                ipv4(collector, interface.get("address"), name + ".address")
                prefix = interface.get("prefix_length")
                collector.check(is_int(prefix) and 0 <= prefix <= 32,
                                f"{name}.prefix_length: integer between 0 and 32 required in static mode")
                if "gateway" in interface:
                    ipv4(collector, interface.get("gateway"), name + ".gateway")
    resolvers = network.get("dns", [])
    if collector.check(isinstance(resolvers, list) and len(resolvers) <= 4,
                       "network.dns: array of at most 4 resolvers required"):
        seen = set()
        for index, resolver in enumerate(resolvers):
            if ipv4(collector, resolver, f"network.dns[{index}]") is not None:
                collector.check(resolver not in seen, f"network.dns[{index}]: duplicate resolver")
                seen.add(resolver)
    mtu = network.get("mtu")
    if mtu is not None:
        collector.check(is_int(mtu) and 576 <= mtu <= 9000, "network.mtu: integer between 576 and 9000 required")


def validate_volumes(collector, volumes):
    if not collector.check(isinstance(volumes, list) and len(volumes) <= 32,
                           "volumes: array of at most 32 data volumes required"):
        return
    names, devices, mounts = set(), set(), set()
    for index, volume in enumerate(volumes):
        name = f"volumes[{index}]"
        if not reject_unknown(collector, volume, VOLUME_KEYS, name):
            continue
        volume_name = bounded_string(collector, volume.get("name"), name + ".name", 64, SLUG)
        if volume_name is not None:
            collector.check(volume_name not in names, f"{name}.name: duplicate volume '{volume_name}'")
            names.add(volume_name)
        device = bounded_string(collector, volume.get("device"), name + ".device", 65, DEVICE)
        if device is not None:
            collector.check(device not in devices, f"{name}.device: duplicate device '{device}'")
            devices.add(device)
        collector.check(volume.get("filesystem") in FILESYSTEMS,
                        f"{name}.filesystem: {' or '.join(FILESYSTEMS)} required")
        mount_point = absolute_guest_path(collector, volume.get("mount_point"), name + ".mount_point")
        if mount_point is not None:
            collector.check(mount_point not in mounts, f"{name}.mount_point: duplicate mount point")
            mounts.add(mount_point)
        for field in ("read_only", "required"):
            value = volume.get(field)
            if value is not None:
                collector.check(type(value) is bool, f"{name}.{field}: boolean required")


def validate_fleet(collector, fleet):
    if not reject_unknown(collector, fleet, FLEET_KEYS, "fleet"):
        return
    role = fleet.get("role")
    if role is not None:
        bounded_string(collector, role, "fleet.role", 64, SLUG)
    labels = fleet.get("labels", {})
    if collector.check(isinstance(labels, dict) and len(labels) <= 32,
                       "fleet.labels: object of at most 32 entries required"):
        for key, value in labels.items():
            collector.check(isinstance(key, str) and 0 < len(key) <= 64 and SLUG.fullmatch(key),
                            f"fleet.labels: key '{key}' must be a lowercase slug of at most 64 characters")
            collector.check(isinstance(value, str) and len(value) <= 64,
                            f"fleet.labels['{key}']: string of at most 64 characters required")
    inventory = fleet.get("inventory_id")
    if inventory is not None:
        bounded_string(collector, inventory, "fleet.inventory_id", 128)


def validate_secret_references(collector, references):
    if not collector.check(isinstance(references, list) and len(references) <= 64,
                           "secret_references: array of at most 64 opaque references required"):
        return
    names = set()
    for index, reference in enumerate(references):
        name = f"secret_references[{index}]"
        if not reject_unknown(collector, reference, SECRET_KEYS, name):
            continue
        reference_name = bounded_string(collector, reference.get("name"), name + ".name", 64, SLUG)
        if reference_name is not None:
            collector.check(reference_name not in names, f"{name}.name: duplicate reference '{reference_name}'")
            names.add(reference_name)
        ref = reference.get("ref")
        if collector.check(isinstance(ref, str) and SECRET_REF.fullmatch(ref),
                           f"{name}.ref: vault://, file://, or keychain:// opaque reference required"):
            if ref.startswith("file://"):
                path = ref[len("file://"):]
                collector.check(path.startswith("/") and posixpath.normpath(path) == path and path != "/",
                                f"{name}.ref: file:// references must be normalized absolute paths without '..'")


def service_subset_identity(path, collector):
    try:
        subset = load_json(path.read_bytes())
    except (OSError, ValueError) as error:
        collector.error(f"service subset: unreadable runtime/service-subset.json ({error})")
        return None
    if not isinstance(subset, dict) or subset.get("schema_version") != 1:
        collector.error("service subset: unsupported runtime/service-subset.json schema")
        return None
    return sha(path.read_bytes())


def validate(config_bytes, schema_path=SCHEMA, subset_path=SERVICE_SUBSET):
    collector = Collector()
    result = {"schema_version": 1, "tool": "scripts/provisioning/validate_config.py",
              "config_valid": False, "config_sha256": sha(config_bytes),
              "errors": [], "warnings": [], "summary": {},
              "boot_time_application_verified": False,
              "guest_acceptance": "not-run",
              "open_dependencies": ["#59", "#60"],
              "limitations": list(LIMITATIONS)}
    try:
        schema = load_json(schema_path.read_bytes())
        supported = schema.get("supported_config_versions") if isinstance(schema, dict) else None
        if not (isinstance(supported, list) and supported and all(is_int(v) for v in supported)):
            raise ValueError("schema does not declare supported_config_versions")
        result["schema_sha256"] = sha(schema_path.read_bytes())
    except (OSError, ValueError) as error:
        collector.error(f"schema: cannot load runtime/config-schema.json ({error})")
        result["errors"] = collector.finish()
        return result
    subset_digest = service_subset_identity(subset_path, collector)
    if subset_digest is not None:
        result["service_subset_sha256"] = subset_digest
    config = None
    if collector.check(0 < len(config_bytes) <= MAX_CONFIG_BYTES,
                       f"config: non-empty file of at most {MAX_CONFIG_BYTES} bytes required"):
        try:
            config = load_json(config_bytes)
        except (UnicodeError, ValueError) as error:
            collector.error(f"config: invalid JSON ({error})")
    if config is not None:
        if not collector.check(isinstance(config, dict), "config: top-level JSON object required"):
            config = None
    if config is not None:
        version = config.get("schema_version")
        if not collector.check(version in supported,
                               f"schema_version: supported versions are {supported}; got {version!r}"):
            config = None
    if config is not None:
        reject_unknown(collector, config, CONFIG_KEYS, "config")
        validate_hostname(collector, config.get("hostname"))
        scan_for_inline_secrets(collector, config)
        if "image" in config:
            validate_image(collector, config["image"])
        accounts = config.get("accounts", [])
        validate_accounts(collector, accounts)
        services = config.get("services", [])
        validate_services(collector, services)
        if "network" in config:
            validate_network(collector, config["network"])
        volumes = config.get("volumes", [])
        validate_volumes(collector, volumes)
        if "fleet" in config:
            validate_fleet(collector, config["fleet"])
        references = config.get("secret_references", [])
        validate_secret_references(collector, references)
        if not accounts:
            result["warnings"].append("no accounts declared; no SSH login will be provisioned")
        if not services:
            result["warnings"].append("no services declared; guest will boot without managed jobs")
        result["summary"] = {"hostname": config.get("hostname"), "accounts": len(accounts),
                             "services": len(services), "volumes": len(volumes),
                             "secret_references": len(references)}
    result["errors"] = collector.finish()
    result["config_valid"] = not result["errors"]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="Declarative server configuration JSON")
    parser.add_argument("--schema", type=Path, default=SCHEMA)
    parser.add_argument("--service-subset", type=Path, default=SERVICE_SUBSET)
    parser.add_argument("--output", type=Path, help="Write the machine-readable result to a new path")
    args = parser.parse_args()
    if args.output is not None and args.output.exists():
        parser.error("Use a new output path so prior validation results are preserved")
    try:
        if not (args.config.is_file() and stat.S_ISREG(args.config.stat().st_mode)):
            raise ValueError("Expected a regular configuration file")
        config_bytes = args.config.read_bytes()
    except OSError as error:
        print(str(error), file=sys.stderr)
        return 1
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    result = validate(config_bytes, args.schema, args.service_subset)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x") as stream:
            stream.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"config": str(args.config), "config_valid": result["config_valid"],
                      "config_sha256": result["config_sha256"], "errors": result["errors"],
                      "warnings": result["warnings"],
                      "boot_time_application_verified": False}, indent=2))
    return 0 if result["config_valid"] else 2


if __name__ == "__main__":
    sys.exit(main())
