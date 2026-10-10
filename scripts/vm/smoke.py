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
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

REQUIRED = {"kernel", "launchd", "launchd_services", "shell", "runtime", "concurrent_once", "blocks_lifetime", "locale_conversions", "mach_timer", "atfork_handlers", "dynamic_loader", "guest_to_host", "clock", "dns", "network_service", "host_to_guest"}
SHUTDOWN_REQUIRED = {"shutdown_job_started", "shutdown_requested", "shutdown_child_reaped", "shutdown_job_exit"}
FAILURE = re.compile(r"(?:panic\(cpu|Kernel panic|BLAUOS_SMOKE_ERROR|The System bootstrapper has crashed|dyld:.*(?:not found|Symbol not found))", re.I)


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
    expected_profile = {"machine": args.machine, "virtualization": False, "gic_version": 3,
                        "accel": "tcg", "cpu": "neoverse-n2"}
    if manifest.get("emulator_profile") != expected_profile:
        raise ValueError("Emulator profile differs from the image manifest; the PSCI HVC profile requires virtualization=off")
    return manifest


def qemu_command(args, variables, qmp=None, disk_copy=None):
    command = [args.qemu, "-machine", args.machine + ",gic-version=3,virtualization=off", "-accel", "tcg",
            "-cpu", "neoverse-n2", "-smp", str(args.cpus), "-m", str(args.memory),
            "-boot", "order=c,strict=on",
            "-drive", f"if=pflash,format=raw,unit=0,readonly=on,file={args.firmware.resolve()}",
            "-drive", f"if=pflash,format=raw,unit=1,file={variables.resolve()}",
            "-drive", f"if=none,id=system,format=raw,snapshot={'off' if disk_copy else 'on'},file={(disk_copy or args.image).resolve()}",
            "-device", "virtio-blk-pci,drive=system,bootindex=1", "-device", "ramfb",
            "-device", "virtio-net-pci,netdev=net0",
            "-netdev", f"user,id=net0,net=10.0.2.0/24,hostfwd=tcp:127.0.0.1:{args.guest_port}-10.0.2.15:18080",
            "-device", "qemu-xhci,id=xhci", "-device", "usb-kbd,bus=xhci.0",
            "-display", "none", "-monitor", "none", "-serial", "stdio", "-no-reboot", "-no-shutdown"]
    if qmp is not None:
        command += ["-qmp", f"unix:{qmp},server=on,wait=off"]
    return command


def is_guest_shutdown(message):
    """Host termination, guest resets and panics cannot prove clean shutdown."""
    data = message.get("data", {})
    return message.get("event") == "SHUTDOWN" and data.get("guest") is True and data.get("reason") == "guest-shutdown"


def collect_qmp(path, messages, stopped, log_path):
    """Subscribe to QMP events; never issue a host poweroff/reset command."""
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(1)
    try:
        deadline = time.monotonic() + 10
        while not stopped.is_set():
            try:
                connection.connect(str(path))
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if time.monotonic() >= deadline:
                    raise TimeoutError("QMP event socket did not become available")
                stopped.wait(0.1)
        if stopped.is_set():
            return
        buffer = b""
        with log_path.open("w") as log:
            while not stopped.is_set():
                try:
                    block = connection.recv(65536)
                except socket.timeout:
                    continue
                if not block:
                    if not stopped.is_set():
                        messages.put({"qmp_error": "QMP connection closed"})
                    return
                buffer += block
                if len(buffer) > 1024 * 1024:
                    raise ValueError("QMP message exceeded bounded buffer")
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    message = json.loads(line)
                    log.write(json.dumps(message, sort_keys=True) + "\n")
                    log.flush()
                    if "QMP" in message:
                        connection.sendall(b'{"execute":"qmp_capabilities","id":"capabilities"}\r\n')
                    elif "error" in message:
                        messages.put({"qmp_error": str(message["error"])})
                    else:
                        messages.put(message)
    except (OSError, ValueError, TimeoutError) as error:
        if not stopped.is_set():
            messages.put({"qmp_error": str(error)})
    finally:
        connection.close()


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
    variables.chmod(0o600)
    qmp_directory = tempfile.TemporaryDirectory(prefix="blauos-qmp-") if args.require_shutdown else None
    qmp_path = Path(qmp_directory.name) / "events.sock" if qmp_directory else None
    qmp_messages = queue.Queue()
    qmp_stopped = threading.Event()
    disk_copy = args.output / "guest-writable.img" if args.writable_disk_copy else None
    if disk_copy:
        if disk_copy.exists() or disk_copy.is_symlink():
            raise ValueError("Use a fresh output directory for a private writable guest disk")
        shutil.copyfile(args.image, disk_copy)
    command = qemu_command(args, variables, qmp_path, disk_copy)
    if args.trace_exceptions:
        command += ["-d", "int", "-D", str((args.output / "qemu-exceptions.log").resolve())]
    (args.output / "command.json").write_text(json.dumps(command, indent=2) + "\n")
    (args.output / "image-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    shutil.copyfile(args.symbols, args.output / "kernel.symbols")
    log_path = args.output / "serial.log"
    stderr_path = args.output / "qemu.stderr.log"
    lines = queue.Queue()
    collected = ""
    required = REQUIRED | SHUTDOWN_REQUIRED if args.require_shutdown else REQUIRED
    boot_required = REQUIRED | {"shutdown_job_started"} if args.require_shutdown else REQUIRED
    result = {"schema_version": 1, "passed": False, "nonce": nonce, "guest_challenge_seen": False,
              "host_to_guest_passed": False, "required_markers": sorted(required), "missing_markers": sorted(required),
              "readiness_passed": False, "shutdown_required": args.require_shutdown, "shutdown_requested": False,
              "guest_shutdown_passed": False,
              "private_writable_disk": str(disk_copy) if disk_copy else None,
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
            if qmp_path:
                threading.Thread(target=collect_qmp, args=(qmp_path, qmp_messages, qmp_stopped, args.output / "qmp-events.jsonl"), daemon=True).start()
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
                result["missing_markers"] = sorted(required - markers)
                while not qmp_messages.empty():
                    message = qmp_messages.get_nowait()
                    if "qmp_error" in message:
                        raise RuntimeError(message["qmp_error"])
                    if message.get("event") == "SHUTDOWN":
                        if not result["shutdown_requested"] or not is_guest_shutdown(message):
                            raise RuntimeError("Unexpected QMP shutdown: " + json.dumps(message))
                        result["guest_shutdown_passed"] = True
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
                if not (boot_required - markers) and result["guest_challenge_seen"] and result["host_to_guest_passed"]:
                    result["readiness_passed"] = True
                    if not args.require_shutdown:
                        result["passed"] = True
                        break
                    if not result["shutdown_requested"]:
                        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                        with opener.open(f"http://127.0.0.1:{args.guest_port}/shutdown/{nonce}", timeout=2) as response:
                            if response.status != 202 or response.read(128) != (nonce + "\n").encode():
                                raise RuntimeError("Guest did not acknowledge the shutdown request")
                        result["shutdown_requested"] = True
                        deadline = time.monotonic() + args.shutdown_timeout
                    if not result["missing_markers"] and result["guest_shutdown_passed"]:
                        result["passed"] = True
                        break
            if not result["passed"]:
                raise TimeoutError(f"VM readiness/shutdown deadline exceeded; missing {result['missing_markers']}; guest shutdown={result['guest_shutdown_passed']}")
    except (OSError, ValueError, RuntimeError, TimeoutError) as error:
        result["failure"] = str(error)
    finally:
        qmp_stopped.set()
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        server.shutdown()
        server.server_close()
        if qmp_directory:
            qmp_directory.cleanup()
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
    parser.add_argument("--machine", default="virt-10.2")
    parser.add_argument("--memory", type=int, default=1024)
    parser.add_argument("--cpus", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--guest-port", type=int, default=18080)
    parser.add_argument("--require-shutdown", action="store_true", help="After readiness, require real PID 1 child termination/reaping and guest-triggered QMP poweroff")
    parser.add_argument("--writable-disk-copy", action="store_true", help="Retain guest core dumps and filesystem changes in a fresh ordinary disk copy under the output directory")
    parser.add_argument("--trace-exceptions", action="store_true", help="Record QEMU interrupt/exception diagnostics in the output directory")
    parser.add_argument("--shutdown-timeout", type=int, default=60)
    parser.add_argument("--output", type=Path, default=Path(".blauos/build/vm/boot"))
    args = parser.parse_args()
    if not 1 <= args.timeout <= 1800 or not 1024 <= args.guest_port <= 65535 or not 10 <= args.shutdown_timeout <= 300:
        parser.error("Boot timeout must be 1..1800 seconds, shutdown timeout 10..300 seconds, and guest port 1024..65535")
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
