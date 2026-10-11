"""Offline schema/validator tests; guest boot-time application remains open (#62, gates #59/#60)."""
import base64
import copy
import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("blauos_validate_config",
                                              ROOT / "scripts/provisioning/validate_config.py")
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


def key(byte=1):
    blob = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + bytes([byte]) * 32
    return "ssh-ed25519 " + base64.b64encode(blob).decode("ascii")


def valid_config():
    return {
        "schema_version": 1,
        "hostname": "node-1.lab.blauos.local",
        "image": {"channel": "nightly", "version": "2026.10.11"},
        "accounts": [{"name": "joeblau", "uid": 1000, "gid": 1000, "ssh_authorized_keys": [key()]}],
        "services": [{"label": "org.blauos.smoke", "program": "/usr/local/bin/smoke",
                      "run_at_load": True, "keep_alive": False}],
        "network": {"interfaces": [{"name": "en0", "mode": "static", "address": "192.168.64.20",
                                    "prefix_length": 24, "gateway": "192.168.64.1"}],
                    "dns": ["192.168.64.1"], "mtu": 1500},
        "volumes": [{"name": "data", "device": "/dev/vdb", "filesystem": "ext4",
                     "mount_point": "/var/data", "read_only": False, "required": True}],
        "fleet": {"role": "build-node", "labels": {"site": "lab"}, "inventory_id": "lab-01"},
        "secret_references": [{"name": "api-token", "ref": "vault://kv/blauos/node-1/token"}],
    }


def encode(config):
    return json.dumps(config).encode()


def errors_of(config_bytes):
    return validator.validate(config_bytes)["errors"]


class PositiveTests(unittest.TestCase):
    def test_repository_examples_validate(self):
        for name in ("basic-server.json", "static-file-server.json"):
            with self.subTest(example=name):
                result = validator.validate((ROOT / "runtime/examples" / name).read_bytes())
                self.assertEqual(result["errors"], [])
                self.assertTrue(result["config_valid"])
                self.assertFalse(result["boot_time_application_verified"])
                self.assertEqual(result["guest_acceptance"], "not-run")
                self.assertEqual(result["open_dependencies"], ["#59", "#60"])
                self.assertIn("service_subset_sha256", result)
                self.assertIn("schema_sha256", result)

    def test_minimal_config_validates_with_warnings(self):
        result = validator.validate(encode({"schema_version": 1, "hostname": "node-1"}))
        self.assertTrue(result["config_valid"])
        self.assertEqual(len(result["warnings"]), 2)
        self.assertEqual(result["summary"]["accounts"], 0)

    def test_full_config_summary_and_no_secret_material(self):
        result = validator.validate(encode(valid_config()))
        self.assertTrue(result["config_valid"])
        self.assertEqual(result["summary"]["accounts"], 1)
        self.assertEqual(result["summary"]["services"], 1)
        self.assertEqual(result["summary"]["volumes"], 1)
        self.assertEqual(result["summary"]["secret_references"], 1)
        self.assertNotIn(key(), json.dumps(result))
        self.assertNotIn("vault://", json.dumps(result))


class MalformedInputTests(unittest.TestCase):
    def test_invalid_json(self):
        errors = errors_of(b'{"schema_version": 1,')
        self.assertTrue(any("invalid JSON" in error for error in errors))

    def test_duplicate_json_keys_rejected(self):
        errors = errors_of(b'{"schema_version": 1, "hostname": "a", "hostname": "b"}')
        self.assertTrue(any("duplicate JSON key" in error for error in errors))

    def test_nonfinite_values_rejected(self):
        errors = errors_of(b'{"schema_version": 1, "hostname": "a", "network": {"mtu": NaN}}')
        self.assertTrue(any("nonfinite" in error for error in errors))

    def test_top_level_array_rejected(self):
        errors = errors_of(b"[1, 2, 3]")
        self.assertTrue(any("top-level JSON object" in error for error in errors))

    def test_oversized_config_rejected(self):
        errors = errors_of(b" " * (validator.MAX_CONFIG_BYTES + 1))
        self.assertTrue(any("at most" in error for error in errors))


class VersionTests(unittest.TestCase):
    def test_unknown_version_rejected_before_structure(self):
        for version in (0, 2, "1", None):
            with self.subTest(version=version):
                config = valid_config()
                config["schema_version"] = version
                result = validator.validate(encode(config))
                self.assertFalse(result["config_valid"])
                self.assertEqual(len(result["errors"]), 1)
                self.assertIn("schema_version", result["errors"][0])
                self.assertEqual(result["summary"], {})


class UnknownFieldTests(unittest.TestCase):
    def test_unknown_top_level_field(self):
        config = dict(valid_config(), unexpected=True)
        self.assertTrue(any("unknown field 'unexpected'" in error for error in errors_of(encode(config))))

    def test_unknown_nested_fields(self):
        config = valid_config()
        config["accounts"][0]["password"] = "hunter2"
        config["services"][0]["sandbox_profile"] = "none"
        config["network"]["interfaces"][0]["ipv6"] = True
        errors = errors_of(encode(config))
        self.assertTrue(any("accounts[0]: unknown field 'password'" in error for error in errors))
        self.assertTrue(any("services[0]: unknown field 'sandbox_profile'" in error for error in errors))
        self.assertTrue(any("network.interfaces[0]: unknown field 'ipv6'" in error for error in errors))


class FieldValidationTests(unittest.TestCase):
    def test_hostname_rules(self):
        for hostname in ("", "-bad", "bad..dots", "UPPER.example", "a" * 254, "under_score"):
            with self.subTest(hostname=hostname):
                config = valid_config()
                config["hostname"] = hostname
                self.assertTrue(any("hostname" in error for error in errors_of(encode(config))))

    def test_account_constraints(self):
        cases = [
            ({"name": "root"}, "reserved"),
            ({"name": "sshd"}, "reserved"),
            ({"name": "Bad"}, "accounts[0].name"),
            ({"uid": 999}, "uid"),
            ({"uid": True}, "uid"),
            ({"gid": 60001}, "gid"),
            ({"ssh_authorized_keys": []}, "1 to 16"),
            ({"ssh_authorized_keys": [key(i % 250 + 1) for i in range(17)]}, "1 to 16"),
            ({"ssh_authorized_keys": [key(), key()]}, "duplicate key"),
            ({"ssh_authorized_keys": ['command="/bin/sh" ' + key()]}, "bare ssh-ed25519"),
            ({"ssh_authorized_keys": ["-----BEGIN OPENSSH PRIVATE KEY-----"]}, "inline secret"),
            ({"ssh_authorized_keys": ["ssh-ed25519 bad-base64!"]}, "base64"),
            ({"ssh_authorized_keys": ["ssh-ed25519 " + base64.b64encode(b"short").decode()]},
             "wire encoding"),
        ]
        for patch, message in cases:
            with self.subTest(message=message):
                config = valid_config()
                account = dict(config["accounts"][0], **patch)
                config["accounts"] = [account]
                self.assertTrue(any(message in error for error in errors_of(encode(config))))

    def test_duplicate_account_ids(self):
        config = valid_config()
        config["accounts"].append({"name": "alice", "uid": 1000, "gid": 2000,
                                   "ssh_authorized_keys": [key(2)]})
        config["accounts"].append({"name": "bob", "uid": 2000, "gid": 1000,
                                   "ssh_authorized_keys": [key(3)]})
        errors = errors_of(encode(config))
        self.assertTrue(any("UID 1000 is already used" in error for error in errors))
        self.assertTrue(any("GID 1000 is already used" in error for error in errors))

    def test_service_constraints(self):
        cases = [
            ({"label": "no dots"}, "services[0].label"),
            ({"program": "relative/path"}, "absolute"),
            ({"program": "/usr/bin/../escape"}, "'..' components"),
            ({"program_arguments": "not-a-list"}, "array"),
            ({"run_at_load": "yes"}, "boolean"),
            ({"mach_services": ["plain"]}, "mach_services[0]"),
        ]
        for patch, message in cases:
            with self.subTest(message=message):
                config = valid_config()
                config["services"] = [dict(config["services"][0], **patch)]
                self.assertTrue(any(message in error for error in errors_of(encode(config))))

    def test_duplicate_service_labels(self):
        config = valid_config()
        config["services"].append(dict(config["services"][0]))
        self.assertTrue(any("duplicate service label" in error for error in errors_of(encode(config))))

    def test_network_constraints(self):
        cases = [
            ({"interfaces": [{"name": "en0", "mode": "dhcp", "address": "192.168.64.2"}]}, "forbidden in dhcp"),
            ({"interfaces": [{"name": "en0", "mode": "static"}]}, "address"),
            ({"interfaces": [{"name": "en0", "mode": "static", "address": "999.1.1.1",
                              "prefix_length": 24}]}, "invalid IPv4"),
            ({"interfaces": [{"name": "en0", "mode": "static", "address": "10.0.0.1",
                              "prefix_length": 33}]}, "prefix_length"),
            ({"interfaces": [{"name": "en0", "mode": "static", "address": "10.0.0.1",
                              "prefix_length": 24, "gateway": "not-an-ip"}]}, "gateway"),
            ({"dns": ["10.0.0.1", "10.0.0.1"]}, "duplicate resolver"),
            ({"dns": ["fe80::1"]}, "invalid IPv4"),
            ({"mtu": 9001}, "mtu"),
            ({"mtu": True}, "mtu"),
        ]
        for network, message in cases:
            with self.subTest(message=message):
                config = valid_config()
                config["network"] = network
                self.assertTrue(any(message in error for error in errors_of(encode(config))))

    def test_volume_constraints(self):
        cases = [
            ({"mount_point": "relative"}, "absolute"),
            ({"mount_point": "/"}, "below /"),
            ({"mount_point": "/var/../escape"}, "'..' components"),
            ({"mount_point": "/var/data/"}, "'..' components"),
            ({"filesystem": "zfs"}, "filesystem"),
            ({"device": "vdb"}, "device"),
            ({"read_only": "yes"}, "boolean"),
            ({"name": "Bad Name"}, "volumes[0].name"),
        ]
        for patch, message in cases:
            with self.subTest(message=message):
                config = valid_config()
                config["volumes"] = [dict(config["volumes"][0], **patch)]
                self.assertTrue(any(message in error for error in errors_of(encode(config))))

    def test_duplicate_volume_mounts_and_devices(self):
        config = valid_config()
        config["volumes"].append({"name": "other", "device": "/dev/vdb", "filesystem": "ext4",
                                  "mount_point": "/var/data"})
        errors = errors_of(encode(config))
        self.assertTrue(any("duplicate device" in error for error in errors))
        self.assertTrue(any("duplicate mount point" in error for error in errors))

    def test_secret_reference_constraints(self):
        cases = [
            ({"ref": "plaintext-password"}, "opaque reference"),
            ({"ref": "https://example.com/secret"}, "opaque reference"),
            ({"ref": "vault://has space"}, "opaque reference"),
            ({"ref": "file:///../etc/master.passwd"}, "without '..'"),
            ({"ref": "file://relative"}, "absolute"),
            ({"name": "Bad Name"}, "secret_references[0].name"),
        ]
        for patch, message in cases:
            with self.subTest(message=message):
                config = valid_config()
                config["secret_references"] = [dict(config["secret_references"][0], **patch)]
                self.assertTrue(any(message in error for error in errors_of(encode(config))))

    def test_inline_secret_marker_anywhere_rejected(self):
        config = valid_config()
        config["fleet"]["labels"]["leak"] = "-----BEGIN OPENSSH PRIVATE KEY-----"
        self.assertTrue(any("inline secret" in error for error in errors_of(encode(config))))

    def test_errors_are_bounded(self):
        config = {"schema_version": 1, "hostname": "node",
                  "accounts": [{"name": "Bad!", "uid": "x", "gid": None,
                                "ssh_authorized_keys": ["junk"], "extra": i} for i in range(16)]}
        result = validator.validate(encode(config))
        self.assertFalse(result["config_valid"])
        self.assertLessEqual(len(result["errors"]), validator.MAX_ERRORS + 1)
        self.assertTrue(any("error limit" in error for error in result["errors"]))


class CliTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.config = Path(self.folder.name) / "config.json"
        self.config.write_bytes(encode(valid_config()))
        self.output = Path(self.folder.name) / "result.json"
        self.script = ROOT / "scripts/provisioning/validate_config.py"

    def run_cli(self, *arguments):
        return subprocess.run([sys.executable, str(self.script), *map(str, arguments)],
                              capture_output=True, text=True)

    def test_valid_config_exit_zero_and_receipt(self):
        completed = self.run_cli(self.config, "--output", self.output)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        printed = json.loads(completed.stdout)
        self.assertTrue(printed["config_valid"])
        receipt = json.loads(self.output.read_text())
        self.assertTrue(receipt["config_valid"])
        self.assertFalse(receipt["boot_time_application_verified"])
        self.assertEqual(receipt["summary"]["accounts"], 1)

    def test_invalid_config_exit_two(self):
        self.config.write_bytes(encode({"schema_version": 2, "hostname": "node"}))
        completed = self.run_cli(self.config, "--output", self.output)
        self.assertEqual(completed.returncode, 2)
        self.assertFalse(json.loads(self.output.read_text())["config_valid"])

    def test_existing_output_path_preserved(self):
        self.output.write_text("prior evidence")
        completed = self.run_cli(self.config, "--output", self.output)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(self.output.read_text(), "prior evidence")

    def test_missing_config_file(self):
        completed = self.run_cli(Path(self.folder.name) / "absent.json")
        self.assertEqual(completed.returncode, 1)


if __name__ == "__main__":
    unittest.main()
