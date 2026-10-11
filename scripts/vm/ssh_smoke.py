#!/usr/bin/env python3
"""Accept real key-authenticated SSH/SFTP across two independent guest boots."""
import argparse
import http.server
import ipaddress
import json
from pathlib import Path
import re
import secrets
import shlex
import shutil
import socketserver
import subprocess
import sys
import threading
import time

from accounts import fingerprint, public_keys, validate_account
from smoke import FAILURE, REQUIRED, SHUTDOWN_REQUIRED, read_markers, sha256
from virtualbuddy_smoke import request

CAPABILITIES = {"account_lookup", "credentials_permissions", "pty_shell", "entropy_admission", "getentropy_api",
                "urandom_device_api", "permanent_credentials", "sandbox_select", "descriptor_passing",
                "sshd_chroot", "runtime_prerequisites"}


def capabilities_passed(text):
    return {match.group(1) for line in text.splitlines()
            if (match := re.fullmatch(r"BLAUOS_SSH_CAPABILITY ([a-z_]+) pass errno=0", line.strip()))}


def host_identity(text):
    """Accept a source wrapper's complete serial lines, never network keyscan."""
    lines = text.splitlines()
    if text and not text.endswith("\n"):
        lines = lines[:-1]
    keys, fingerprints = set(), set()
    for line in lines:
        line = line.strip()
        if line.startswith("BLAUOS_SSH_HOST_PUBLIC_KEY "):
            keys.add(public_keys(line.removeprefix("BLAUOS_SSH_HOST_PUBLIC_KEY "))[0])
        if line.startswith("BLAUOS_SSH_HOST_FINGERPRINT "):
            fingerprints.add(line.removeprefix("BLAUOS_SSH_HOST_FINGERPRINT "))
    if len(keys) > 1 or len(fingerprints) > 1:
        raise ValueError("Conflicting guest host identities in this boot")
    if not keys or not fingerprints:
        return None
    key = next(iter(keys))
    if fingerprints != {fingerprint(key)}:
        raise ValueError("Guest host public key and fingerprint disagree")
    return key


def session_passed(output, username, uid, gid, pty, persistence, challenge):
    lines = {line.strip() for line in output.splitlines()}
    return (f"BLAUOS_SSH_SESSION pass user={username} uid={uid} gid={gid} pty={int(pty)} persistent={persistence}" in lines
            and f"BLAUOS_SSH_CHALLENGE {challenge}" in lines)


def authentication_refused(result):
    return result.returncode == 255 and "Permission denied (publickey" in result.stderr


def guest_stopped(receipt):
    names = {event.get("name") for event in receipt.get("events", [])}
    return "guest-stopped" in names and not names.intersection({"host-stopped-probe", "bounded-observation-finished",
                                                               "vm-stopped-with-error", "probe-watchdog-expired"})


def quoted_path(path):
    value = str(path.resolve())
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Control characters are forbidden in command paths")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def connection_options(identity, known_hosts):
    return ["-F", "/dev/null", "-i", str(identity.resolve()),
            "-o", "UserKnownHostsFile=" + quoted_path(known_hosts),
            "-o", "GlobalKnownHostsFile=/dev/null", "-o", "StrictHostKeyChecking=yes",
            "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
            "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
            "-o", "PreferredAuthentications=publickey", "-o", "HostKeyAlgorithms=ssh-ed25519",
            "-o", "ConnectTimeout=5", "-o", "ConnectionAttempts=1",
            "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
            "-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "UpdateHostKeys=no"]


def command_receipt(command, directory, name, deadline, stdin=None):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("SSH acceptance deadline exceeded")
    result = subprocess.run(command, input=stdin, capture_output=True, text=True, timeout=min(40, remaining))
    (directory / (name + ".stdout")).write_text(result.stdout)
    (directory / (name + ".stderr")).write_text(result.stderr)
    return result, {"argv": command, "exit_code": result.returncode,
                    "stdout_sha256": sha256(directory / (name + ".stdout")),
                    "stderr_sha256": sha256(directory / (name + ".stderr"))}


def ssh_checks(args, directory, deadline, key, disk_nonce, boot, tools):
    known = directory / "known_hosts"
    known.write_text(f"{args.guest_ip} {key}\n")
    known.chmod(0o600)
    options = connection_options(args.identity, known)
    checks = {}
    persistence = "written" if boot == 1 else "verified"
    for pty, name in ((False, "command"), (True, "pty")):
        challenge = secrets.token_hex(32)
        helper = ["/usr/libexec/blauos-ssh-session-probe", args.username, str(args.uid), str(args.gid), "--challenge"]
        if pty:
            helper.append("--pty")
        else:
            helper += ["--nonce" if boot == 1 else "--expect-nonce", disk_nonce]
        command = [tools["ssh"], *options, "-tt" if pty else "-T", f"{args.username}@{args.guest_ip}", shlex.join(helper)]
        process, receipt = command_receipt(command, directory, name, deadline, challenge + "\n")
        passed = process.returncode == 0 and session_passed(process.stdout, args.username, args.uid, args.gid,
                                                          pty, "none" if pty else persistence, challenge)
        checks[name] = {**receipt, "challenge": challenge, "passed": passed}
        (directory / "ssh-checks.json").write_text(json.dumps(checks, indent=2) + "\n")
        if not passed:
            raise RuntimeError(f"Authenticated {name} session failed (see {directory / (name + '.stderr')})")
    for name, username, identity in (("unauthorized_key", args.username, args.output / "wrong-key"),
                                     ("root_login", "root", args.identity)):
        command = [tools["ssh"], *connection_options(identity, known), "-T", f"{username}@{args.guest_ip}", "/bin/true"]
        process, receipt = command_receipt(command, directory, name, deadline)
        checks[name] = {**receipt, "passed": authentication_refused(process)}
        (directory / "ssh-checks.json").write_text(json.dumps(checks, indent=2) + "\n")
        if not checks[name]["passed"]:
            raise RuntimeError(f"{name} did not produce an authentication rejection")
    uploaded, downloaded = args.output / "upload.bin", directory / "download.bin"
    remote = f"/var/home/{args.username}/.blauos-sftp-fixture"
    batch = directory / "sftp.batch"
    contents = (f"put {quoted_path(uploaded)} {remote}\n" if boot == 1 else "") + f"get {remote} {quoted_path(downloaded)}\n"
    batch.write_text(contents)
    command = [tools["sftp"], *options, "-b", str(batch), f"{args.username}@{args.guest_ip}"]
    process, receipt = command_receipt(command, directory, "sftp", deadline)
    passed = process.returncode == 0 and downloaded.is_file() and downloaded.read_bytes() == uploaded.read_bytes()
    checks["sftp"] = {**receipt, "passed": passed, "fixture_sha256": sha256(uploaded), "retained_from_first_boot": boot == 2}
    (directory / "ssh-checks.json").write_text(json.dumps(checks, indent=2) + "\n")
    if not passed:
        raise RuntimeError("SFTP fixture transfer or persistence failed")
    return checks


def boot_once(args, disk, number, expected_key, disk_nonce, tools):
    directory = args.output / f"boot{number}"
    directory.mkdir()
    nonce, challenged = secrets.token_hex(32), threading.Event()

    class Challenge(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/challenge" or self.client_address[0] != args.guest_ip:
                self.send_error(404)
                return
            body = f"{nonce} {int(time.time())}\n".encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            challenged.set()

        def log_message(self, *unused):
            pass

    class Server(http.server.ThreadingHTTPServer):
        daemon_threads = True

        def server_bind(self):
            socketserver.TCPServer.server_bind(self)
            self.server_name, self.server_port = "BlauOS SSH smoke", self.server_address[1]

    server = Server(("0.0.0.0", 18081), Challenge)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    vm = directory / "vm"
    command = [str(args.probe.resolve()), str(vm.resolve()), str(disk.resolve()), "--timeout-seconds", str(args.timeout_seconds)]
    if args.entropy_device:
        command.append("--entropy-device")
    result = {"schema_version": 1, "boot": number, "passed": False, "nonce": nonce, "argv": command,
              "guest_challenge_seen": False, "host_to_guest_passed": False, "shutdown_requested": False,
              "guest_shutdown_passed": False, "ssh_checks": {}, "host_key_persisted": False,
              "entropy_device": args.entropy_device, "physical_boot_gate": False}
    process = None
    try:
        with (directory / "probe.log").open("wb") as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + args.timeout_seconds
            key = None
            while time.monotonic() < deadline:
                serial = vm / "serial.log"
                text = serial.read_text(errors="replace") if serial.exists() else ""
                markers = read_markers(text, nonce)
                result["markers"] = sorted(markers)
                capabilities = capabilities_passed(text)
                result["capabilities"] = sorted(capabilities)
                result["guest_challenge_seen"] = challenged.is_set()
                if FAILURE.search(text) or re.search(r"BLAUOS_SSH_(?:START|CAPABILITY|SESSION)[^\r\n]*\bfail\b", text):
                    raise RuntimeError("Guest reported failure")
                key = host_identity(text)
                if key:
                    if expected_key is not None and key != expected_key:
                        raise RuntimeError("Persistent host key changed after reboot")
                    result["host_public_key"] = key
                    result["host_key_fingerprint"] = fingerprint(key)
                    result["host_key_persisted"] = number == 2 and key == expected_key and "BLAUOS_SSH_HOST_KEY preserved" in text
                if challenged.is_set() and "network_service" in markers and not result["host_to_guest_passed"]:
                    result["host_to_guest_passed"] = request(args.guest_ip, "health", nonce, 200)
                if (key and result["host_to_guest_passed"] and not (CAPABILITIES - capabilities)
                        and not ((REQUIRED | {"shutdown_job_started"}) - markers)
                        and not result["shutdown_requested"]):
                    result["ssh_checks"] = ssh_checks(args, directory, deadline, key, disk_nonce, number, tools)
                    if number == 2 and not result["host_key_persisted"]:
                        raise RuntimeError("Guest did not preserve its host key across boots")
                    if not request(args.guest_ip, "shutdown/" + nonce, nonce, 202):
                        raise RuntimeError("Guest did not acknowledge clean shutdown")
                    result["shutdown_requested"] = True
                receipt = vm / "result.json"
                if receipt.exists():
                    result["guest_shutdown_passed"] = result["shutdown_requested"] and guest_stopped(json.loads(receipt.read_text()))
                if result["guest_shutdown_passed"] and not ((REQUIRED | SHUTDOWN_REQUIRED) - markers):
                    if process.wait(timeout=10) != 0:
                        raise RuntimeError("Backend probe failed after guest stop")
                    result["passed"] = True
                    break
                if process.poll() is not None:
                    raise RuntimeError("VM exited before SSH acceptance completed")
                time.sleep(0.5)
            if not result["passed"]:
                raise TimeoutError("SSH boot acceptance deadline exceeded")
    except (OSError, ValueError, RuntimeError, TimeoutError, subprocess.SubprocessError) as error:
        result["failure"] = str(error)
        if (directory / "ssh-checks.json").exists():
            result["ssh_checks"] = json.loads((directory / "ssh-checks.json").read_text())
    finally:
        if process is not None and process.poll() is None:
            result["host_cleanup_termination"] = True
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    if (vm / "serial.log").is_file():
        result["serial_sha256"] = sha256(vm / "serial.log")
    (directory / "checkpoint.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def run(args):
    if args.output.exists() or args.output.is_symlink():
        raise ValueError("Use a fresh evidence directory; prior VMs and receipts are preserved")
    if not args.image.is_file() or sha256(args.image) != args.image_sha256:
        raise ValueError("Image is absent or differs from its preparation hash")
    if not args.probe.is_file() or not args.identity.is_file():
        raise ValueError("Provide the public VZ probe and an owned test identity file")
    if ipaddress.ip_address(args.guest_ip).version != 4:
        raise ValueError("SSH VM smoke requires a numeric IPv4 address")
    validate_account(args.username, args.uid, args.gid)
    if not 60 <= args.timeout_seconds <= 3600:
        raise ValueError("Per-boot timeout must be between 60 and 3600 seconds")
    tools = {name: shutil.which(name) for name in ("ssh", "sftp", "ssh-keygen")}
    if not all(tools.values()):
        raise ValueError("Host SSH, SFTP and ssh-keygen clients are required")
    args.output.mkdir(parents=True, mode=0o700)
    args.output.chmod(0o700)
    disk = args.output / "writable.img"
    shutil.copyfile(args.image, disk)
    disk.chmod(0o600)
    if sha256(disk) != args.image_sha256:
        raise ValueError("Owned writable image copy differs from its input")
    subprocess.run([tools["ssh-keygen"], "-q", "-t", "ed25519", "-N", "", "-C", "blauos-negative-test",
                    "-f", str(args.output / "wrong-key")], check=True, capture_output=True, timeout=20)
    (args.output / "upload.bin").write_bytes(secrets.token_bytes(4096))
    disk_nonce = secrets.token_hex(32)
    result = {"schema_version": 1, "passed": False, "image_sha256": args.image_sha256,
              "probe_sha256": sha256(args.probe), "guest_ip": args.guest_ip, "username": args.username,
              "uid": args.uid, "gid": args.gid, "entropy_device": args.entropy_device,
              "disk_persistence_nonce": disk_nonce, "boots": [], "physical_boot_gate": False,
              "remote_reboot_verified": False, "service_restart_verified": False,
              "host_tools": {name: {"path": path, "sha256": sha256(Path(path))} for name, path in tools.items()}}
    expected_key = None
    for number in (1, 2):
        receipt = boot_once(args, disk, number, expected_key, disk_nonce, tools)
        result["boots"].append(receipt)
        if not receipt["passed"]:
            result["failure"] = f"Boot {number}: " + receipt.get("failure", "acceptance failed")
            break
        expected_key = receipt["host_public_key"]
    result["passed"] = len(result["boots"]) == 2 and all(boot["passed"] for boot in result["boots"])
    result["source_image_unchanged"] = sha256(args.image) == args.image_sha256
    result["writable_image_final_sha256"] = sha256(disk)
    if not result["source_image_unchanged"]:
        result["passed"] = False
        result["failure"] = "Source image changed during acceptance"
    (args.output / "checkpoint.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("image", "probe", "identity", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--image-sha256", required=True)
    parser.add_argument("--guest-ip", default="192.168.64.201")
    parser.add_argument("--username", required=True)
    parser.add_argument("--uid", type=int, default=1000)
    parser.add_argument("--gid", type=int, default=1000)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--entropy-device", action="store_true")
    args = parser.parse_args()
    try:
        result = run(args)
        print(json.dumps(result, indent=2))
        return 0 if result["passed"] else 1
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
