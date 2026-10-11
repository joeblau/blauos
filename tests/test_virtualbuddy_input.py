"""Console input path tests (#101): frame encoding, virtio RX ring protocol
model, and patch stacking. No guest execution is simulated or claimed."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/vm"))
spec = importlib.util.spec_from_file_location("console_input", ROOT / "scripts/vm/console_input.py")
console_input = importlib.util.module_from_spec(spec)
spec.loader.exec_module(console_input)

PATCHES = ROOT / "vm/patches"
KERNEL_SERIES = ["xnu-classic-arm64-protections", "pdarm-qemu-psci-shutdown",
                 "puredarwin-virtualbuddy-platform", "puredarwin-virtualbuddy-console",
                 "puredarwin-virtualbuddy-console-input", "puredarwin-virtualbuddy-power-button"]
LOADER_SERIES = ["xnu-loader-efi-diagnostics", "xnu-loader-virtualbuddy-platform",
                 "xnu-loader-virtualbuddy-console-input", "xnu-loader-virtualbuddy-power-button"]

# Ring layout shared with the loader/kernel console-input patches:
# descriptors at 0, available ring at 0x100, used ring at 0x200, one posted
# 0x100-byte device-writable data buffer at 0x300.
RING_BYTES = 0x4000
AVAIL_FLAGS, AVAIL_IDX, AVAIL_RING = 0x100, 0x102, 0x104
USED_IDX, USED_ELEM = 0x202, 0x204
DATA, DATA_CAPACITY = 0x300, 0x100


class GuestRxModel:
    """Python mirror of vb_console_rx_ready/vb_console_rx/vb_rx_repost in
    vm/patches/puredarwin-virtualbuddy-console-input.patch."""

    def __init__(self, ring):
        self.ring = ring
        self.consumed = struct.unpack_from("<H", ring, USED_IDX)[0]
        self.pos = self.length = 0
        self.failed = False
        self.notifications = 0

    def ready(self):
        if self.pos < self.length:
            return True
        if self.failed:
            return False
        if struct.unpack_from("<H", self.ring, USED_IDX)[0] == self.consumed:
            return False
        elem = USED_ELEM + 8 * (self.consumed % 8)
        descriptor_id, length = struct.unpack_from("<II", self.ring, elem)
        self.consumed += 1
        if descriptor_id != 0 or length > DATA_CAPACITY:
            self.failed = True
            return False
        self.length, self.pos = length, 0
        return self.length > 0

    def repost(self):
        struct.pack_into("<QIHH", self.ring, 0, 0xC000 + DATA, DATA_CAPACITY, 2, 0)
        index = struct.unpack_from("<H", self.ring, AVAIL_IDX)[0]
        struct.pack_into("<H", self.ring, AVAIL_RING + 2 * (index % 8), 0)
        struct.pack_into("<H", self.ring, AVAIL_IDX, index + 1)
        self.notifications += 1

    def read(self):
        if not self.ready():
            return -1
        byte = self.ring[DATA + self.pos]
        self.pos += 1
        if self.pos >= self.length:
            self.length = 0
            self.repost()
        return byte


def fresh_rx_ring():
    """Loader-prepared queue 0: descriptor 0 posted, available idx advanced."""
    ring = bytearray(RING_BYTES)
    struct.pack_into("<H", ring, AVAIL_FLAGS, 1)  # no interrupts; guest polls
    struct.pack_into("<QIHH", ring, 0, 0xC000 + DATA, DATA_CAPACITY, 2, 0)
    struct.pack_into("<H", ring, AVAIL_RING, 0)
    struct.pack_into("<H", ring, AVAIL_IDX, 1)
    return ring


def device_deliver(ring, payload):
    """Backend writes input bytes and completes descriptor 0."""
    assert len(payload) <= DATA_CAPACITY
    ring[DATA:DATA + len(payload)] = payload
    used = struct.unpack_from("<H", ring, USED_IDX)[0]
    struct.pack_into("<II", ring, USED_ELEM + 8 * (used % 8), 0, len(payload))
    struct.pack_into("<H", ring, USED_IDX, used + 1)


class ConsoleInputEncodingTests(unittest.TestCase):
    def test_command_line_terminates_with_carriage_return(self):
        self.assertEqual(console_input.encode_line("uname -s"), b"uname -s\r")
        self.assertEqual(console_input.encode_line("echo\thi"), b"echo\thi\r")

    def test_control_keys_match_tty_line_discipline(self):
        self.assertEqual(console_input.encode_control("ctrl-c"), b"\x03")
        self.assertEqual(console_input.encode_control("eof"), b"\x04")
        self.assertEqual(console_input.encode_control("backspace"), b"\x7f")

    def test_rejects_bytes_the_line_discipline_would_misread(self):
        for bad in ("", "x" * 513, "echo\nforged", "echo\rforged", "café", "\x7f"):
            with self.subTest(bad=bad[:16]), self.assertRaises(ValueError):
                console_input.encode_line(bad)
        with self.assertRaises(ValueError):
            console_input.encode_control("ctrl-alt-delete")

    def test_frames_interleave_ctrl_c_between_commands_only(self):
        frames = console_input.build_frames(["sleep 30", "uname"], ["eof"], True)
        self.assertEqual([label for label, _ in frames],
                         ["command:sleep 30", "ctrl-c", "command:uname", "eof"])
        self.assertEqual(b"".join(payload for _, payload in frames),
                         b"sleep 30\r\x03uname\r\x04")


class ConsoleInputFifoTests(unittest.TestCase):
    def test_full_scripted_session_round_trips_through_a_fifo(self):
        with tempfile.TemporaryDirectory() as folder:
            fifo = Path(folder) / "console-input.fifo"
            os.mkfifo(fifo, 0o600)
            received = []
            def drain():
                descriptor = os.open(fifo, os.O_RDONLY)
                chunks = []
                while True:
                    chunk = os.read(descriptor, 4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
                received.append(b"".join(chunks))
            reader = threading.Thread(target=drain, daemon=True)
            reader.start()
            frames = console_input.build_frames(["echo ok"], ["ctrl-c", "eof"], False)
            sent = console_input.write_frames(fifo, frames, 0, 0)
            reader.join(timeout=5)
            self.assertEqual(received, [b"echo ok\r\x03\x04"])
            self.assertEqual([frame["bytes"] for frame in sent], [8, 1, 1])
            for frame in sent:
                self.assertEqual(len(frame["sha256"]), 64)

    def test_refuses_plain_files_and_readerless_fifos(self):
        with tempfile.TemporaryDirectory() as folder:
            plain = Path(folder) / "not-a-fifo"
            plain.write_bytes(b"")
            with self.assertRaisesRegex(ValueError, "FIFO"):
                console_input.write_frames(plain, [("x", b"x\r")], 0, 0)
            fifo = Path(folder) / "lonely.fifo"
            os.mkfifo(fifo)
            with self.assertRaises(OSError):  # ENXIO: no probe is reading
                console_input.write_frames(fifo, [("x", b"x\r")], 0, 0)

    def test_cli_rejects_existing_receipt_and_writes_hashes(self):
        with tempfile.TemporaryDirectory() as folder:
            fifo = Path(folder) / "console-input.fifo"
            os.mkfifo(fifo)
            receipt = Path(folder) / "receipt.json"
            reader = threading.Thread(target=lambda: os.read(
                os.open(fifo, os.O_RDONLY), 4096), daemon=True)
            reader.start()
            command = [sys.executable, str(ROOT / "scripts/vm/console_input.py"),
                       "--fifo", str(fifo), "--command", "uname -s",
                       "--control", "eof", "--byte-delay-ms", "0",
                       "--frame-delay-ms", "0", "--receipt", str(receipt)]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads(receipt.read_text())
            self.assertEqual(record["frames"][0]["sha256"],
                             hashlib.sha256(b"uname -s\r").hexdigest())
            self.assertIn("not established", record["guest_execution"])
            again = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(again.returncode, 1)


class VirtioRxRingProtocolTests(unittest.TestCase):
    def test_typed_line_reaches_guest_byte_for_byte_then_reposts(self):
        ring = fresh_rx_ring()
        guest = GuestRxModel(ring)
        self.assertFalse(guest.ready())
        device_deliver(ring, b"ls\r")
        self.assertEqual([guest.read() for _ in range(3)], [ord("l"), ord("s"), 13])
        self.assertEqual(guest.read(), -1)
        self.assertEqual(guest.consumed, 1)
        # The drained buffer was reposted exactly once with identical shape.
        self.assertEqual(struct.unpack_from("<H", ring, AVAIL_IDX)[0], 2)
        self.assertEqual(struct.unpack_from("<QIHH", ring, 0), (0xC000 + DATA, DATA_CAPACITY, 2, 0))
        self.assertEqual(guest.notifications, 1)
        device_deliver(ring, b"\x03")  # Ctrl-C arrives as a single byte
        self.assertEqual(guest.read(), 3)

    def test_malformed_used_element_fails_closed(self):
        ring = fresh_rx_ring()
        guest = GuestRxModel(ring)
        struct.pack_into("<II", ring, USED_ELEM, 7, 4)  # wrong descriptor id
        struct.pack_into("<H", ring, USED_IDX, 1)
        self.assertEqual(guest.read(), -1)
        self.assertTrue(guest.failed)
        ring = fresh_rx_ring()
        guest = GuestRxModel(ring)
        struct.pack_into("<II", ring, USED_ELEM, 0, DATA_CAPACITY + 1)
        struct.pack_into("<H", ring, USED_IDX, 1)
        self.assertEqual(guest.read(), -1)
        self.assertTrue(guest.failed)

    def test_wraparound_uses_modulo_ring_slots(self):
        ring = fresh_rx_ring()
        guest = GuestRxModel(ring)
        for round_index in range(10):
            device_deliver(ring, b"x")
            self.assertEqual(guest.read(), ord("x"))
        self.assertEqual(guest.consumed, 10)
        self.assertEqual(struct.unpack_from("<H", ring, AVAIL_IDX)[0], 11)


class ConsoleInputPatchTests(unittest.TestCase):
    def test_kernel_patch_keeps_stock_four_word_property_and_adds_bounded_rx(self):
        patch = (PATCHES / "puredarwin-virtualbuddy-console-input.patch").read_text()
        self.assertIn("vb_length!=4*sizeof(uint64_t) && vb_length!=6*sizeof(uint64_t)", patch)
        self.assertIn("VIRTQ_DESC_F_WRITE", patch)
        self.assertIn("len>0x100", patch)  # bounded by the posted buffer capacity
        self.assertIn("vb_console_rx_ready", patch)
        self.assertNotIn("while (1)", patch)

    def test_loader_patch_posts_one_rx_buffer_and_extends_property(self):
        patch = (PATCHES / "xnu-loader-virtualbuddy-console-input.patch").read_text()
        self.assertIn("w16(rx,12,2)", patch)  # device-writable descriptor
        self.assertIn("gVB.console_rx_queue?sizeof(console):4*sizeof(UINT64)", patch)
        self.assertIn("XNU_BOOTINFO_BASE+0xC000", patch)

    def test_patches_stack_on_pinned_sources(self):
        kernel_source = ROOT / ".blauos/sources/puredarwin-vm-next"
        loader_source = ROOT / ".blauos/sources/xnu-loader-vm"
        if not (kernel_source / ".git").exists() or not (loader_source / ".git").exists():
            self.skipTest("pinned sources not present on this host")
        with tempfile.TemporaryDirectory() as folder:
            for source, series in ((kernel_source, KERNEL_SERIES), (loader_source, LOADER_SERIES)):
                overlay = Path(folder) / source.name
                overlay.mkdir()
                archive = subprocess.run(["git", "-C", str(source), "archive", "HEAD"],
                                         capture_output=True, check=True)
                subprocess.run(["tar", "-x", "-C", str(overlay)], input=archive.stdout, check=True)
                subprocess.run(["git", "init", "--quiet", str(overlay)], check=True)
                for name in series:
                    patch = PATCHES / (name + ".patch")
                    subprocess.run(["git", "apply", "--check", str(patch)],
                                   cwd=overlay, check=True, capture_output=True)
                    subprocess.run(["git", "apply", str(patch)],
                                   cwd=overlay, check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
