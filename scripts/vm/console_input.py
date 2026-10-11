#!/usr/bin/env python3
"""Inject validated console input frames into the guest virtio console FIFO.

The backend probe vm/virtualbuddy_console_probe.swift attaches the guest
serial port's read side to console-input.fifo; frames written here are what
the guest console input path (virtio console RX queue 0) delivers to the
source shell. Newlines are carriage returns, matching the terminal input the
console tty line discipline expects; Ctrl-C and EOF are single control bytes.
This script only writes to a FIFO created by a running probe; it never
fabricates guest execution evidence.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import time

CONTROL_BYTES = {
    "ctrl-c": b"\x03",   # tty line discipline: SIGINT to the foreground shell
    "eof": b"\x04",      # Ctrl-D: end-of-file on a canonical read
    "backspace": b"\x7f",
    "escape": b"\x1b",
}


def encode_line(text):
    """One typed command line: printable ASCII plus tab, terminated by CR."""
    if not text or len(text) > 512:
        raise ValueError("Command must be 1-512 characters")
    if any(ord(c) < 0x20 and c != "\t" for c in text) or not text.isascii() or "\x7f" in text:
        raise ValueError("Command must be printable ASCII; control keys use --control")
    return text.encode("ascii") + b"\r"


def encode_control(name):
    if name not in CONTROL_BYTES:
        raise ValueError(f"Unsupported control key: {name}")
    return CONTROL_BYTES[name]


def build_frames(commands, controls, interleave_ctrl_c):
    """Ordered (label, bytes) frame list for a scripted interactive session."""
    frames = []
    for index, command in enumerate(commands):
        frames.append((f"command:{command}", encode_line(command)))
        if interleave_ctrl_c and index < len(commands) - 1:
            frames.append(("ctrl-c", encode_control("ctrl-c")))
    for name in controls:
        frames.append((name, encode_control(name)))
    return frames


def write_frames(fifo, frames, byte_delay_ms, frame_delay_ms):
    if not stat.S_ISFIFO(os.stat(fifo).st_mode):
        raise ValueError("Refusing to write: console input path must be the probe FIFO")
    if not 0 <= byte_delay_ms <= 1000 or not 0 <= frame_delay_ms <= 60000:
        raise ValueError("Delays out of supported bounds")
    descriptor = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)  # ENXIO: no probe reading
    sent = []
    try:
        for label, payload in frames:
            for byte in payload:
                os.write(descriptor, bytes([byte]))
                if byte_delay_ms:
                    time.sleep(byte_delay_ms / 1000)
            sent.append({"frame": label, "bytes": len(payload),
                         "sha256": hashlib.sha256(payload).hexdigest()})
            if frame_delay_ms:
                time.sleep(frame_delay_ms / 1000)
    finally:
        os.close(descriptor)
    return sent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fifo", type=Path, required=True,
                        help="console-input.fifo inside a running probe's output directory")
    parser.add_argument("--command", action="append", default=[],
                        help="Typed command line (repeatable); newline is appended as CR")
    parser.add_argument("--control", action="append", default=[],
                        choices=sorted(CONTROL_BYTES), help="Control key sent after all commands")
    parser.add_argument("--ctrl-c-between-commands", action="store_true",
                        help="Send Ctrl-C between consecutive commands (line-cancel coverage)")
    parser.add_argument("--byte-delay-ms", type=int, default=10)
    parser.add_argument("--frame-delay-ms", type=int, default=250)
    parser.add_argument("--receipt", type=Path, required=True,
                        help="Fresh JSON receipt path; records frames and exact byte hashes")
    args = parser.parse_args()
    if not args.command and not args.control:
        print("Nothing to send", file=sys.stderr)
        return 1
    if args.receipt.exists():
        print("Receipt must not already exist", file=sys.stderr)
        return 1
    try:
        frames = build_frames(args.command, args.control, args.ctrl_c_between_commands)
        sent = write_frames(args.fifo, frames, args.byte_delay_ms, args.frame_delay_ms)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    receipt = {"schema_version": 1, "fifo": str(args.fifo), "frames": sent,
               "guest_execution": "not established by host-side injection"}
    args.receipt.write_text(json.dumps(receipt, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
