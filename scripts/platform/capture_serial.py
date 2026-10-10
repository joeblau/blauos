#!/usr/bin/env python3
"""Capture a selected UART without sending application bytes or reset commands."""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import select
import stat
import sys
import termios
import time


BAUD_RATES = {rate: getattr(termios, f"B{rate}") for rate in (9600, 19200, 38400, 57600, 115200)}
MAX_CHUNKS = 16384


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def capture(device: Path, output: Path, transport: str, seconds: float,
            max_bytes: int, baud: int = 115200):
    if not 0 < seconds <= 3600 or not 0 < max_bytes <= 128 * 1024 * 1024:
        raise ValueError("Capture requires a duration <= 3600 seconds and size <= 128 MiB")
    if baud not in BAUD_RATES or transport not in ("native-uart", "m1n1-usb", "synthetic-pty"):
        raise ValueError("Unsupported baud rate or transport")
    device = device.resolve(strict=True)
    if not stat.S_ISCHR(device.stat().st_mode):
        raise ValueError("Select a character device, not a regular file containing a log")
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    started = utc_now()
    origin = time.monotonic_ns()
    chunks = []
    size = 0
    raw_hash = hashlib.sha256()
    reason = "setup-error"
    failure = None
    descriptor = None
    original = None
    log_path = output / "serial.log"
    try:
        descriptor = os.open(device, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
        if not os.isatty(descriptor):
            raise ValueError("Selected character device is not a terminal")
        original = termios.tcgetattr(descriptor)
        configured = termios.tcgetattr(descriptor)
        configured[0] = 0  # No input translation, parity processing or software flow control.
        configured[1] = 0
        configured[2] &= ~(termios.CSIZE | termios.PARENB | termios.CSTOPB)
        configured[2] |= termios.CS8 | termios.CLOCAL | termios.CREAD
        configured[3] = 0
        configured[4] = configured[5] = BAUD_RATES[baud]
        configured[6][termios.VMIN] = 0
        configured[6][termios.VTIME] = 0
        termios.tcsetattr(descriptor, termios.TCSANOW, configured)
        with log_path.open("xb") as stream:
            while True:
                remaining = seconds - (time.monotonic_ns() - origin) / 1e9
                if remaining <= 0:
                    reason = "duration-limit"
                    break
                if size == max_bytes:
                    reason = "byte-limit"
                    break
                if len(chunks) == MAX_CHUNKS:
                    reason = "chunk-limit"
                    break
                ready, _, _ = select.select([descriptor], [], [], min(remaining, 0.5))
                if not ready:
                    continue
                try:
                    payload = os.read(descriptor, min(65536, max_bytes - size))
                except BlockingIOError:
                    continue
                if not payload:
                    reason = "device-eof"
                    break
                stream.write(payload)
                raw_hash.update(payload)
                chunks.append({"offset": size, "length": len(payload), "received_at": utc_now(),
                               "monotonic_ns": time.monotonic_ns() - origin,
                               "sha256": hashlib.sha256(payload).hexdigest()})
                size += len(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except KeyboardInterrupt:
        reason = "interrupted"
    except (OSError, ValueError, termios.error) as error:
        reason = "capture-error"
        failure = str(error)
    finally:
        if descriptor is not None:
            if original is not None:
                try:
                    termios.tcsetattr(descriptor, termios.TCSANOW, original)
                except (OSError, termios.error) as error:
                    failure = f"Terminal settings could not be restored: {error}"
            os.close(descriptor)
    receipt = {
        "schema_version": 1, "transport": transport, "device": str(device), "baud": baud,
        "collector_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "format": "8N1", "application_bytes_sent": 0, "reset_commands_sent": 0,
        "started_at": started, "finished_at": utc_now(),
        "elapsed_seconds": (time.monotonic_ns() - origin) / 1e9,
        "limits": {"seconds": seconds, "bytes": max_bytes, "chunks": MAX_CHUNKS}, "stop_reason": reason,
        "serial": {"path": "serial.log", "bytes": size, "sha256": raw_hash.hexdigest()},
        "chunks": chunks, "error": failure,
        "physical_boot_validated": False,
        "limitations": ["This receipt records bytes received, not their hardware origin or kernel correctness.",
                        "Opening/configuring a serial port can change adapter control signals; qualify the transport separately.",
                        "A timeout, synthetic PTY, or m1n1 USB console cannot establish native M1 XNU execution."],
    }
    (output / "capture.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--transport", choices=("native-uart", "m1n1-usb", "synthetic-pty"), required=True)
    parser.add_argument("--seconds", type=float, default=600)
    parser.add_argument("--max-bytes", type=int, default=16 * 1024 * 1024)
    parser.add_argument("--baud", type=int, choices=BAUD_RATES, default=115200)
    args = parser.parse_args()
    try:
        receipt = capture(args.device, args.output, args.transport, args.seconds, args.max_bytes, args.baud)
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps({"output": str(args.output), "bytes": receipt["serial"]["bytes"],
                      "stop_reason": receipt["stop_reason"], "physical_boot_validated": False}))
    return 130 if receipt["stop_reason"] == "interrupted" else (1 if receipt["error"] else 0)


if __name__ == "__main__":
    sys.exit(main())
