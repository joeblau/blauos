"""Receipt isolation checks; these do not claim guest login or boot success."""
import argparse
import base64
import importlib.util
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/vm"))
spec = importlib.util.spec_from_file_location("ssh_acceptance", ROOT / "scripts/vm/ssh_smoke.py")
ssh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ssh)


def key(value=1):
    blob = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + bytes([value]) * 32
    return "ssh-ed25519 " + base64.b64encode(blob).decode("ascii")


def serial_identity(public):
    return f"BLAUOS_SSH_HOST_PUBLIC_KEY {public}\nBLAUOS_SSH_HOST_FINGERPRINT {ssh.fingerprint(public)}\n"


class SSHSmokeTests(unittest.TestCase):
    def test_host_identity_comes_from_complete_wrapper_markers(self):
        self.assertEqual(ssh.host_identity(serial_identity(key())), key())
        self.assertIsNone(ssh.host_identity("echo " + serial_identity(key())))
        self.assertIsNone(ssh.host_identity(serial_identity(key()).rstrip("\n")))

    def test_corrupted_or_conflicting_host_identity_is_rejected(self):
        for text in (serial_identity(key()) + serial_identity(key(2)),
                     serial_identity(key()).replace(ssh.fingerprint(key()), ssh.fingerprint(key(2))),
                     "BLAUOS_SSH_HOST_PUBLIC_KEY ssh-ed25519 malformed\n"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    ssh.host_identity(text)

    def test_pty_echo_and_stale_nonce_do_not_prove_session(self):
        nonce = "1" * 64
        session = "BLAUOS_SSH_SESSION pass user=joeblau uid=1000 gid=1000 pty=1 persistent=none\n"
        self.assertFalse(ssh.session_passed(session + nonce + "\r\n", "joeblau", 1000, 1000, True, "none", nonce))
        self.assertFalse(ssh.session_passed(session + "BLAUOS_SSH_CHALLENGE " + "2" * 64,
                                           "joeblau", 1000, 1000, True, "none", nonce))
        output = session + "BLAUOS_SSH_CHALLENGE " + nonce + "\r\n"
        self.assertTrue(ssh.session_passed(output, "joeblau", 1000, 1000, True, "none", nonce))
        self.assertFalse(ssh.session_passed(output, "joeblau", 0, 0, True, "none", nonce))

    def test_network_failures_do_not_count_as_authentication_rejection(self):
        for message in ("Connection refused", "Connection timed out", "Host key verification failed"):
            result = subprocess.CompletedProcess([], 255, "", message)
            self.assertFalse(ssh.authentication_refused(result))
        result = subprocess.CompletedProcess([], 255, "", "root@192.168.64.201: Permission denied (publickey).")
        self.assertTrue(ssh.authentication_refused(result))
        self.assertFalse(ssh.authentication_refused(subprocess.CompletedProcess([], 0, "", result.stderr)))

    def test_host_stop_watchdog_or_crash_cannot_prove_guest_shutdown(self):
        guest = {"events": [{"name": "guest-stopped"}]}
        self.assertTrue(ssh.guest_stopped(guest))
        for name in ("host-stopped-probe", "bounded-observation-finished", "probe-watchdog-expired", "vm-stopped-with-error"):
            with self.subTest(name=name):
                self.assertFalse(ssh.guest_stopped({"events": [*guest["events"], {"name": name}]}))
        self.assertFalse(ssh.guest_stopped({"events": []}))

    def test_capability_echo_or_failure_cannot_be_counted_as_pass(self):
        text = "BLAUOS_SSH_CAPABILITY entropy_admission pass errno=0\n"
        self.assertEqual(ssh.capabilities_passed(text), {"entropy_admission"})
        self.assertFalse(ssh.capabilities_passed("echo " + text))
        self.assertFalse(ssh.capabilities_passed(text.replace("pass", "fail")))
        self.assertFalse(ssh.capabilities_passed(text.replace("errno=0", "errno=1")))

    def test_wrong_image_hash_refuses_before_reading_identity_or_mutating_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            image = path / "source.img"
            image.write_bytes(b"source image")
            args = argparse.Namespace(output=path / "new", image=image, image_sha256="0" * 64)
            with self.assertRaisesRegex(ValueError, "preparation hash"):
                ssh.run(args)
            self.assertFalse(args.output.exists())
            self.assertEqual(image.read_bytes(), b"source image")

    def test_existing_vm_or_evidence_cannot_be_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            receipt = output / "checkpoint.json"
            receipt.write_text("existing evidence\n")
            with self.assertRaisesRegex(ValueError, "fresh evidence"):
                ssh.run(argparse.Namespace(output=output))
            self.assertEqual(receipt.read_text(), "existing evidence\n")
            self.assertEqual([path.name for path in output.iterdir()], ["checkpoint.json"])

    def test_connection_disables_host_config_agents_multiplexing_and_tofu(self):
        options = ssh.connection_options(Path("owned-key"), Path("strict known hosts"))
        for setting in ("StrictHostKeyChecking=yes", "GlobalKnownHostsFile=/dev/null", "IdentityAgent=none",
                        "IdentitiesOnly=yes", "BatchMode=yes", "ControlPath=none", "UpdateHostKeys=no"):
            self.assertIn(setting, options)
        self.assertEqual(options[:2], ["-F", "/dev/null"])
        self.assertTrue(next(value for value in options if value.startswith("UserKnownHostsFile=")).endswith('"'))


if __name__ == "__main__":
    unittest.main()
