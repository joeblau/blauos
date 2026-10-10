"""Synthetic PTY tests verify byte preservation and bounded capture, not hardware boot."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pty
import tempfile
import termios
import threading
import time
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("capture_serial", Path(__file__).resolve().parents[1] / "scripts/platform/capture_serial.py")
serial = importlib.util.module_from_spec(spec)
spec.loader.exec_module(serial)


class SerialCaptureTests(unittest.TestCase):
    def run_capture(self, payload, limit, duration=0.3):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master, slave = pty.openpty()
            before = termios.tcgetattr(slave)
            device = Path(os.ttyname(slave))

            def sender():
                deadline = time.monotonic() + 2
                while termios.tcgetattr(slave)[3] & termios.ICANON:
                    if time.monotonic() >= deadline:
                        return
                    time.sleep(0.005)
                os.write(master, payload)

            thread = threading.Thread(target=sender)
            thread.start()
            try:
                receipt = serial.capture(device, root / "capture", "synthetic-pty", duration, limit)
                thread.join(timeout=3)
                raw = (root / "capture/serial.log").read_bytes()
                restored = termios.tcgetattr(slave)
                # Darwin sets PENDIN when returning to canonical input with unread
                # bytes; this is terminal queue state, not a changed setting.
                restored[3] &= ~getattr(termios, "PENDIN", 0)
                before[3] &= ~getattr(termios, "PENDIN", 0)
                self.assertEqual(restored, before)
                self.assertFalse(receipt["physical_boot_validated"])
                self.assertEqual(receipt["application_bytes_sent"], 0)
                self.assertEqual(receipt["serial"]["sha256"], hashlib.sha256(raw).hexdigest())
                self.assertEqual(json.loads((root / "capture/capture.json").read_text()), receipt)
                offset = 0
                for chunk in receipt["chunks"]:
                    self.assertEqual(chunk["offset"], offset)
                    piece = raw[offset:offset + chunk["length"]]
                    self.assertEqual(hashlib.sha256(piece).hexdigest(), chunk["sha256"])
                    offset += chunk["length"]
                self.assertEqual(offset, len(raw))
                return receipt, raw
            finally:
                os.close(master)
                os.close(slave)

    def test_binary_bytes_preserved_and_timeout_is_not_boot_success(self):
        payload = b"synthetic\r\n\0\xff\x80end\r\n"
        receipt, raw = self.run_capture(payload, 4096)
        self.assertEqual(raw, payload)
        self.assertEqual(receipt["stop_reason"], "duration-limit")

    def test_capture_is_bounded_and_reports_truncation(self):
        receipt, raw = self.run_capture(b"0123456789", 5)
        self.assertEqual(raw, b"01234")
        self.assertEqual(receipt["stop_reason"], "byte-limit")

    def test_chunk_metadata_is_bounded_independently_of_byte_count(self):
        with patch.object(serial, "MAX_CHUNKS", 1):
            receipt, raw = self.run_capture(b"a", 4096)
        self.assertEqual(raw, b"a")
        self.assertEqual(receipt["stop_reason"], "chunk-limit")
        self.assertEqual(len(receipt["chunks"]), 1)

    def test_regular_file_and_reused_output_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "fake-serial").write_bytes(b"kernel booted")
            with self.assertRaises(ValueError):
                serial.capture(root / "fake-serial", root / "capture", "native-uart", 0.1, 10)
            master, slave = pty.openpty()
            try:
                output = root / "old"
                output.mkdir()
                (output / "serial.log").write_bytes(b"preserve me")
                with self.assertRaises(FileExistsError):
                    serial.capture(Path(os.ttyname(slave)), output, "synthetic-pty", 0.1, 10)
                self.assertEqual((output / "serial.log").read_bytes(), b"preserve me")
            finally:
                os.close(master)
                os.close(slave)


if __name__ == "__main__":
    unittest.main()
