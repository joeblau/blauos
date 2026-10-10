#!/usr/bin/env python3
"""Run a real ARM64 guest and require fresh guest output and both network directions."""
import argparse
import hashlib
import http.server
import json
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

REQUIRED = {"kernel", "launchd", "launchd_services", "shell", "runtime", "guest_to_host", "clock", "dns", "network_service", "host_to_guest"}
FAILURE = re.compile(r"(?:panic\(cpu|Kernel panic|BLAUOS_SMOKE_ERROR|dyld:.*(?:not found|Symbol not found))", re.I)


def sha256(path):
    value = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def read_markers(log, nonce):
    """Only standalone markers for this boot count; echoed shell commands do not."""
    found = set()
    for line in log.splitlines():
        match = re.fullmatch(r"BLAUOS_SMOKE ([0-9a-f]{64}) ([a-z_]+)", line.strip())
        if match and match.group(1) == nonce:
            found.add(match.group(2))
    return found


def validate_artifacts(args):
    manifest = json.loads(args.manifest.read_text())
    if manifest.get("schema_version") != 1 or manifest.get("architecture") != "arm64":
        raise ValueError("Expected schema_version=1 ARM64 image manifest")
    # An independent closure result is mandatory, even for a stock-kernel scaffold.
    if manifest.get("runtime_audit_passed") is not True:
        raise ValueError("Image has no passing recursive runtime/provenance audit")
    audit = args.manifest.parent / "runtime-audit.json"
    if not audit.is_file() or manifest.get("runtime_audit_sha256") != sha256(audit):
        raise ValueError("Runtime audit is absent or differs from the image manifest")
    report = json.loads(audit.read_text())
    if report.get("status") != "pass" or report.get("offline_dependency_closure") is not True:
        raise ValueError("Runtime audit did not establish an independent dependency closure")
    for name, path in (("image", args.image), ("firmware", args.firmware), ("vars_template", args.vars_template)):
        if not path.is_file() or manifest.get("sha256", {}).get(name) != sha256(path):
            raise ValueError(f"Missing or mismatched {name} artifact")
    if not args.symbols.is_file():
        raise ValueError("Kernel symbols must be supplied for every boot test")
    if manifest.get("sha256", {}).get("symbols") != sha256(args.symbols):
        raise ValueError("Kernel symbol hash does not match image manifest")
    version = subprocess.check_output([args.qemu, "--version"], text=True).splitlines()[0]
    if manifest.get("qemu_version") != version:
        raise ValueError(f"Unpinned emulator: got {version!r}")
    return manifest


def qemu_command(args, variables):
    return [args.qemu, "-machine", args.machine + ",gic-version=3", "-accel", "tcg",
            "-cpu", "neoverse-n2", "-smp", str(args.cpus), "-m", str(args.memory),
            "-boot", "order=c,strict=on",
            "-drive", f"if=pflash,format=raw,unit=0,readonly=on,file={args.firmware.resolve()}",
            "-drive", f"if=pflash,format=raw,unit=1,file={variables.resolve()}",
            "-drive", f"if=none,id=system,format=raw,snapshot=on,file={args.image.resolve()}",
            "-device", "virtio-blk-pci,drive=system,bootindex=1", "-device", "ramfb",
            "-device", "virtio-net-pci,netdev=net0",
            "-netdev", f"user,id=net0,net=10.0.2.0/24,hostfwd=tcp:127.0.0.1:{args.guest_port}-10.0.2.15:18080",
            "-device", "qemu-xhci,id=xhci", "-device", "usb-kbd,bus=xhci.0",
            "-display", "none", "-monitor", "none", "-serial", "stdio", "-no-reboot", "-no-shutdown"]


def run(args):
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = validate_artifacts(args)
    nonce = secrets.token_hex(32)
    challenge_seen = threading.Event()

    class Challenge(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/challenge":
                self.send_error(404)
                return
            body = f"{nonce} {int(time.time())}\n".encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            challenge_seen.set()

        def log_message(self, *_):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 18081), Challenge)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    variables = args.output / "AAVMF_VARS.fd"
    shutil.copyfile(args.vars_template, variables)
    command = qemu_command(args, variables)
    (args.output / "command.json").write_text(json.dumps(command, indent=2) + "\n")
    (args.output / "image-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    shutil.copyfile(args.symbols, args.output / "kernel.symbols")
    log_path = args.output / "serial.log"
    stderr_path = args.output / "qemu.stderr.log"
    lines = queue.Queue()
    collected = ""
    result = {"schema_version": 1, "passed": False, "nonce": nonce, "guest_challenge_seen": False,
              "host_to_guest_passed": False, "required_markers": sorted(REQUIRED), "missing_markers": sorted(REQUIRED),
              "physical_boot_gate": False, "stock_kernel_used": manifest.get("stock_kernel_used", True)}
    process = None
    try:
        with open(log_path, "w") as serial, open(stderr_path, "w") as stderr:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=stderr, text=True, bufsize=1, start_new_session=True)

            def collect():
                for line in process.stdout:
                    lines.put(line)
                lines.put(None)

            threading.Thread(target=collect, daemon=True).start()
            deadline = time.monotonic() + args.timeout
            next_network_probe = 0
            while time.monotonic() < deadline:
                try:
                    line = lines.get(timeout=min(0.2, max(0.01, deadline - time.monotonic())))
                except queue.Empty:
                    line = ""
                if line is None:
                    raise RuntimeError(f"Guest exited before readiness (QEMU exit {process.poll()})")
                if line:
                    serial.write(line)
                    serial.flush()
                    collected += line
                    if FAILURE.search(line):
                        raise RuntimeError("Guest panic, runtime loader failure or probe error")
                markers = read_markers(collected, nonce)
                result["missing_markers"] = sorted(REQUIRED - markers)
                if challenge_seen.is_set() and "network_service" in markers and time.monotonic() >= next_network_probe:
                    next_network_probe = time.monotonic() + 1
                    try:
                        # No environment proxy is allowed to impersonate guest connectivity.
                        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                        with opener.open(f"http://127.0.0.1:{args.guest_port}/health", timeout=1) as response:
                            result["host_to_guest_passed"] = response.status == 200 and response.read(128) == (nonce + "\n").encode()
                    except (OSError, urllib.error.URLError):
                        pass
                result["guest_challenge_seen"] = challenge_seen.is_set()
                if not result["missing_markers"] and result["guest_challenge_seen"] and result["host_to_guest_passed"]:
                    result["passed"] = True
                    break
            if not result["passed"]:
                raise TimeoutError(f"VM readiness deadline exceeded; missing {result['missing_markers']}")
    except (OSError, ValueError, RuntimeError, TimeoutError) as error:
        result["failure"] = str(error)
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        server.shutdown()
        server.server_close()
        (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--firmware", type=Path, required=True)
    parser.add_argument("--vars-template", type=Path, required=True)
    parser.add_argument("--symbols", type=Path, required=True)
    parser.add_argument("--qemu", default="qemu-system-aarch64")
    parser.add_argument("--machine", default="virt-9.2")
    parser.add_argument("--memory", type=int, default=2048)
    parser.add_argument("--cpus", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--guest-port", type=int, default=18080)
    parser.add_argument("--output", type=Path, default=Path(".blauos/build/vm/boot"))
    args = parser.parse_args()
    if not 1 <= args.timeout <= 1800 or not 1024 <= args.guest_port <= 65535:
        parser.error("Timeout must be 1..1800 seconds; guest port must be 1024..65535")
    try:
        result = run(args)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        args.output.mkdir(parents=True, exist_ok=True)
        result = {"schema_version": 1, "passed": False, "failure": str(error), "physical_boot_gate": False}
        (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
