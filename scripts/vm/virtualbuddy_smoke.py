#!/usr/bin/env python3
"""Verify generic EFI boot with a fresh network challenge and guest poweroff."""
import argparse
import http.server
import ipaddress
import json
from pathlib import Path
import secrets
import socketserver
import subprocess
import sys
import threading
import time

from smoke import FAILURE, REQUIRED, SHUTDOWN_REQUIRED, read_markers, sha256


def request(ip, route, nonce, status):
    command = ["curl", "--noproxy", "*", "--silent", "--show-error", "--max-time", "8",
               "--write-out", "\n%{http_code}", f"http://{ip}:18080/{route}"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    return result.returncode == 0 and result.stdout == f"{nonce}\n\n{status}"


def run(args):
    if args.output.exists():
        raise ValueError("Use a fresh evidence directory")
    if not args.image.is_file() or sha256(args.image) != args.image_sha256:
        raise ValueError("Use an ordinary image file matching its preparation receipt")
    if ipaddress.ip_address(args.guest_ip).version != 4:
        raise ValueError("This experimental profile requires IPv4")
    args.output.mkdir(parents=True)
    nonce, challenge_seen = secrets.token_hex(32), threading.Event()

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
            challenge_seen.set()

        def log_message(self, *unused):
            pass

    class Server(http.server.ThreadingHTTPServer):
        def server_bind(self):
            socketserver.TCPServer.server_bind(self)
            self.server_name, self.server_port = "BlauOS smoke", self.server_address[1]

    server = Server(("0.0.0.0", 18081), Challenge)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    vm_output = args.output / "vm"
    command = [str(args.probe.resolve()), str(vm_output.resolve()), str(args.image.resolve())]
    if args.no_serial:
        command.append("--no-serial")
    result = {"schema_version": 1, "passed": False, "nonce": nonce,
              "guest_challenge_seen": False, "host_to_guest_passed": False,
              "shutdown_requested": False, "guest_shutdown_passed": False,
              "physical_boot_gate": False, "extra_serial_port": not args.no_serial,
              "image_sha256": args.image_sha256, "probe_sha256": sha256(args.probe), "argv": command}
    with (args.output / "probe.log").open("wb") as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 95
        try:
            while time.monotonic() < deadline:
                serial = vm_output / "serial.log"
                text = serial.read_text(errors="replace") if serial.exists() else ""
                markers = read_markers(text, nonce)
                result["markers"] = sorted(markers)
                result["guest_challenge_seen"] = challenge_seen.is_set()
                if FAILURE.search(text):
                    raise RuntimeError("Guest failure signal in serial log")
                ready = challenge_seen.is_set() and (args.no_serial or "network_service" in markers)
                if ready and not result["shutdown_requested"]:
                    result["host_to_guest_passed"] = request(args.guest_ip, "health", nonce, 200)
                    if result["host_to_guest_passed"]:
                        # The serial run requires every boot check. Without serial,
                        # the health endpoint is reached after the same guest tests.
                        complete = args.no_serial or not ((REQUIRED | {"shutdown_job_started"}) - markers)
                        if complete:
                            if not request(args.guest_ip, "shutdown/" + nonce, nonce, 202):
                                raise RuntimeError("Guest did not acknowledge shutdown")
                            result["shutdown_requested"] = True
                receipt = vm_output / "result.json"
                if receipt.exists():
                    events = json.loads(receipt.read_text())["events"]
                    result["guest_shutdown_passed"] = result["shutdown_requested"] and any(
                        event["name"] == "guest-stopped" for event in events) and not any(
                        event["name"] == "host-stopped-probe" for event in events)
                complete = args.no_serial or not ((REQUIRED | SHUTDOWN_REQUIRED) - markers)
                if complete and result["guest_shutdown_passed"]:
                    result["passed"] = True
                    break
                if process.poll() is not None:
                    raise RuntimeError("VM exited before completing acceptance")
                time.sleep(1)
            if not result["passed"]:
                raise TimeoutError("Boot/network/shutdown acceptance deadline exceeded")
        except (OSError, RuntimeError, TimeoutError, subprocess.SubprocessError) as error:
            result["failure"] = str(error)
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)
            server.shutdown()
            server.server_close()
    result["serial_sha256"] = sha256(vm_output / "serial.log") if (vm_output / "serial.log").exists() else None
    (args.output / "checkpoint.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--image-sha256", required=True)
    parser.add_argument("--probe", type=Path, required=True)
    parser.add_argument("--guest-ip", default="192.168.64.200")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--no-serial", action="store_true", help="Match stock VirtualBuddy; require network and public guest-stop callback")
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
