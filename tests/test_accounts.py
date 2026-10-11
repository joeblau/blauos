"""Offline provisioning tests; actual lookup/PTY/login require guest evidence."""
import base64
import copy
import importlib.util
from pathlib import Path
import stat
import struct
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("blauos_accounts", ROOT / "scripts/vm/accounts.py")
accounts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(accounts)


def key(byte=1):
    blob = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + bytes([byte]) * 32
    return "ssh-ed25519 " + base64.b64encode(blob).decode("ascii")


class AccountProvisioningTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name) / "root"
        (self.root / "bin").mkdir(parents=True)
        (self.root / "bin/sh").write_bytes(b"fixture shell")
        (self.root / "bin/sh").chmod(0o755)

    def seed(self, passwd=None, master=None, group=None):
        (self.root / "etc").mkdir()
        for name, value in (("passwd", passwd or accounts.BASE_PASSWD),
                            ("master.passwd", master or accounts.BASE_MASTER),
                            ("group", group or accounts.BASE_GROUP)):
            (self.root / "etc" / name).write_text(value)

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob("*") if p.is_file() and not p.is_symlink()}

    def test_account_consistency_key_only_and_modes(self):
        receipt = accounts.provision(self.root, "joeblau", key() + " personal comment")
        passwd = (self.root / "etc/passwd").read_text()
        master = (self.root / "etc/master.passwd").read_text()
        group = (self.root / "etc/group").read_text()
        self.assertIn("joeblau:*:1000:1000:joeblau:/var/home/joeblau:/bin/sh\n", passwd)
        self.assertIn("joeblau:*NP*:1000:1000::0:0:joeblau:/var/home/joeblau:/bin/sh\n", master)
        self.assertIn("sshd:*:74:74:SSH privilege separation:/var/empty:/bin/false\n", passwd)
        self.assertIn("joeblau:*:1000:\n", group)
        self.assertIn("sshd:*:74:\n", group)
        self.assertEqual((self.root / "var/home/joeblau/.ssh/authorized_keys").read_text(), key() + "\n")
        for relative, mode in (("etc/passwd", 0o644), ("etc/master.passwd", 0o600),
                               ("etc/group", 0o644), ("var/home/joeblau", 0o700),
                               ("var/home/joeblau/.ssh", 0o700),
                               ("var/home/joeblau/.ssh/authorized_keys", 0o600),
                               ("var/empty", 0o755)):
            self.assertEqual(stat.S_IMODE((self.root / relative).stat().st_mode), mode)
        self.assertFalse(receipt["metadata_applied"])
        self.assertFalse(receipt["guest_login_verified"])
        self.assertNotIn(key(), str(receipt))
        self.assertEqual(receipt["public_key_fingerprints"], [accounts.fingerprint(key())])

    def test_preserves_existing_accounts_comments_home_data_and_is_idempotent(self):
        self.seed("# local users\n" + accounts.BASE_PASSWD + "alice:*:2000:2000:Alice:/var/home/alice:/bin/sh\n",
                  accounts.BASE_MASTER + "alice:*NP*:2000:2000::0:0:Alice:/var/home/alice:/bin/sh\n",
                  accounts.BASE_GROUP + "alice:*:2000:\n")
        home = self.root / "var/home/joeblau"
        home.mkdir(parents=True)
        (home / "notes.txt").write_text("keep my data")
        first = accounts.provision(self.root, "joeblau", key())
        before = self.snapshot()
        second = accounts.provision(self.root, "joeblau", key())
        self.assertEqual(first, second)
        self.assertEqual(before, self.snapshot())
        self.assertTrue((self.root / "etc/passwd").read_text().startswith("# local users\n"))
        self.assertEqual((home / "notes.txt").read_text(), "keep my data")

    def test_uid_collision_fails_before_mutation(self):
        self.seed(accounts.BASE_PASSWD + "alice:*:1000:2000:Alice:/var/home/alice:/bin/sh\n",
                  accounts.BASE_MASTER + "alice:*NP*:1000:2000::0:0:Alice:/var/home/alice:/bin/sh\n",
                  accounts.BASE_GROUP + "alice:*:2000:\n")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "numeric ID 1000"):
            accounts.provision(self.root, "joeblau", key())
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.root / "var").exists())

    def test_gid_collision_fails_before_mutation(self):
        self.seed(group=accounts.BASE_GROUP + "other:*:1000:\n")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "numeric ID 1000"):
            accounts.provision(self.root, "joeblau", key())
        self.assertEqual(before, self.snapshot())

    def test_service_uid_collision_is_not_reused(self):
        self.seed(accounts.BASE_PASSWD + "other:*:74:74:Other:/var/empty:/bin/false\n",
                  accounts.BASE_MASTER + "other:*:74:74::0:0:Other:/var/empty:/bin/false\n",
                  accounts.BASE_GROUP + "other:*:74:\n")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "numeric ID 74"):
            accounts.provision(self.root, "joeblau", key())
        self.assertEqual(before, self.snapshot())

    def test_mismatched_public_private_lookup_database_is_rejected(self):
        self.seed(master=accounts.BASE_MASTER.replace("root:*:0:0", "root:*:0:1"))
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "disagree for root"):
            accounts.provision(self.root, "joeblau", key())
        self.assertEqual(before, self.snapshot())

    def test_duplicate_database_ids_are_rejected(self):
        self.seed(group=accounts.BASE_GROUP + "admin:*:0:\n")
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            accounts.provision(self.root, "joeblau", key())

    def test_existing_authorized_keys_are_never_replaced(self):
        accounts.provision(self.root, "joeblau", key())
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "authorized_keys differs"):
            accounts.provision(self.root, "joeblau", key(2))
        self.assertEqual(before, self.snapshot())

    def test_symlink_escape_cannot_touch_external_tree(self):
        outside = Path(self.folder.name) / "outside"
        outside.mkdir()
        (outside / "passwd").write_text("untouched")
        (self.root / "etc").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            accounts.provision(self.root, "joeblau", key())
        self.assertEqual((outside / "passwd").read_text(), "untouched")
        self.assertEqual(set(p.name for p in outside.iterdir()), {"passwd"})

    def test_unsafe_names_ids_and_keys_create_no_account_files(self):
        cases = [("root", 1000, 1000, key()), ("../escape", 1000, 1000, key()),
                 ("user:name", 1000, 1000, key()), ("joeblau", 0, 1000, key()),
                 ("joeblau", 1000, 0, key()), ("joeblau", True, 1000, key()),
                 ("joeblau", 1000, 1000, "-----BEGIN OPENSSH PRIVATE KEY-----"),
                 ("joeblau", 1000, 1000, 'command="/bin/sh" ' + key()),
                 ("joeblau", 1000, 1000, "ssh-ed25519 bad-base64!"),
                 ("joeblau", 1000, 1000, "ssh-ed25519 " + base64.b64encode(b"not an SSH blob").decode()),
                 ("joeblau", 1000, 1000, key() + "\0")]
        for username, uid, gid, public_key in cases:
            with self.subTest(username=username, uid=uid, public_key=public_key[:30]):
                with self.assertRaises(ValueError):
                    accounts.provision(self.root, username, public_key, uid, gid)
                self.assertFalse((self.root / "etc").exists())

    def test_populated_service_chroot_is_rejected(self):
        (self.root / "var/empty").mkdir(parents=True)
        (self.root / "var/empty/content").write_text("keep")
        with self.assertRaisesRegex(ValueError, "must be empty"):
            accounts.provision(self.root, "joeblau", key())
        self.assertFalse((self.root / "etc").exists())

    def test_metadata_tampering_and_nonroot_application_are_rejected_before_chown(self):
        receipt = accounts.provision(self.root, "joeblau", key())
        tampered = copy.deepcopy(receipt)
        tampered["metadata"][-1]["path"] = "../../host/secret"
        with mock.patch.object(accounts.os, "chown") as chown:
            with self.assertRaisesRegex(ValueError, "differs"):
                accounts.apply_metadata(self.root, tampered)
            with mock.patch.object(accounts.os, "geteuid", return_value=501):
                with self.assertRaisesRegex(ValueError, "inside fakeroot"):
                    accounts.apply_metadata(self.root, receipt)
            chown.assert_not_called()

    def test_host_root_is_forbidden(self):
        with self.assertRaisesRegex(ValueError, "host / is forbidden"):
            accounts.provision(Path("/"), "joeblau", key())


if __name__ == "__main__":
    unittest.main()
