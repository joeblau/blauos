"""Host-requested orderly shutdown tests (#102): the guest-visible event is
decoded from the real firmware fixture, the PL061 handler model is exercised,
and the patches/probe keep their bounded behavior. No VM is run here."""
import hashlib
import json
from pathlib import Path
import struct
import unittest

ROOT = Path(__file__).resolve().parents[1]
FIRMWARE = ROOT / "evidence/virtualization/virtualbuddy-platform/firmware"
INVENTORY = ROOT / "evidence/virtualization/virtualbuddy-platform/firmware-inventory.json"
PATCHES = ROOT / "vm/patches"

# PL061 registers used by puredarwin-virtualbuddy-power-button.patch.
PL061_DIR, PL061_IS, PL061_IBE, PL061_IEV = 0x400, 0x404, 0x408, 0x40C
PL061_RIS, PL061_IC = 0x414, 0x41C


def parse_power_button(dsdt):
    """Mirror of the loader's vb power-button scan in
    vm/patches/xnu-loader-virtualbuddy-power-button.patch."""
    def u32(at):
        return int.from_bytes(dsdt[at:at + 4], "little")

    def buffer_descriptors(at):
        at += 1 + (dsdt[at] >> 6)          # BufferOp PkgLength
        assert dsdt[at] == 0x0A            # BytePrefix buffer size
        end = at + 2 + dsdt[at + 1]
        at += 2
        items = []
        while at < end:
            tag = dsdt[at]
            n = 3 + dsdt[at + 1] + (dsdt[at + 2] << 8) if tag & 0x80 else 1 + (tag & 7)
            assert n <= end - at
            items.append((tag, at, n))
            if tag == 0x79:
                break
            at += n
        return items

    i = 36
    while i + 16 < len(dsdt):
        if dsdt[i:i + 8] != b"ARMH0061":
            i += 1
            continue
        window = min(i + 0x800, len(dsdt))
        crs = aei = 0
        for j in range(i + 8, window - 6):
            if dsdt[j:j + 4] == b"_CRS" and dsdt[j + 4] == 0x11:
                crs = j + 5
            if dsdt[j:j + 4] == b"_AEI" and dsdt[j + 4] == 0x11:
                aei = j + 5
        assert crs and aei
        base = irq = pin = None
        for tag, at, n in buffer_descriptors(crs):
            if tag == 0x86 and n == 12:                      # Memory32Fixed
                base, length = u32(at + 4), u32(at + 8)
                assert length == 0x1000
            if tag == 0x89 and n >= 7:                       # Extended IRQ
                irq = u32(at + n - 4)                        # last payload word
        for tag, at, n in buffer_descriptors(aei):
            if tag == 0x8C and n >= 15 and dsdt[at + 3] == 1:  # GpioInt, interrupt
                for k in range(at + 5, at + n - 4):
                    if dsdt[k:k + 4] == b"GPIO" and dsdt[k + 4] == 0:
                        pin = dsdt[k - 2] | (dsdt[k - 1] << 8)
                        break
        assert base and not base & 0xFFF and 32 <= irq < 1020 and pin is not None and pin < 8
        handler = b"_E0" + b"0123456789ABCDEF"[pin:pin + 1]
        notifies = declared = False
        for k in range(36, len(dsdt) - 9):
            if dsdt[k:k + 4] == handler and dsdt[k + 4] == 0 and dsdt[k + 5:k + 10] == b"\x86PWRB":
                notifies = True
            if dsdt[k:k + 7] == b"PNP0C0C":
                declared = True
        assert notifies and declared
        return {"base": base, "irq": irq, "pin": pin, "handler": handler.decode()}
    raise AssertionError("ARMH0061 not present")


class PowerButtonDeviceTests(unittest.TestCase):
    """The guest-visible requestStop event, decoded from the captured fixture."""

    @classmethod
    def setUpClass(cls):
        cls.dsdt = (FIRMWARE / "ACPI-DSDT.bin").read_bytes()
        inventory = json.loads(INVENTORY.read_text())
        recorded = inventory["tables"]["ACPI-DSDT.bin"]["sha256"]
        assert hashlib.sha256(cls.dsdt).hexdigest() == recorded, "fixture drifted"

    def test_request_stop_event_is_pl061_pin_6_power_button_notify(self):
        device = parse_power_button(self.dsdt)
        self.assertEqual(device, {"base": 0x20060000, "irq": 37, "pin": 6, "handler": "_E06"})
        self.assertEqual(device["irq"] - 32, 5)  # GIC SPI 5

    def test_malformed_tables_are_rejected(self):
        truncated = self.dsdt[:700]  # drops _AEI/_E06 entirely
        with self.assertRaises(AssertionError):
            parse_power_button(truncated)
        broken_irq = bytearray(self.dsdt)
        i = broken_irq.find(b"\x89\x06\x00")
        broken_irq[i + 6:i + 10] = (7).to_bytes(4, "little")  # below SPI range
        with self.assertRaises(AssertionError):
            parse_power_button(bytes(broken_irq))


class PL061HandlerModelTests(unittest.TestCase):
    """Register-level model of PDArmPlatformExpert::powerButtonTick."""

    def setUp(self):
        self.registers = bytearray(0x1000)
        self.pin_mask = 1 << 6
        self.signals = []

    def configure(self):
        for offset in (PL061_DIR, PL061_IS, PL061_IBE):
            struct.pack_into("<I", self.registers, offset,
                             struct.unpack_from("<I", self.registers, offset)[0] & ~self.pin_mask)
        struct.pack_into("<I", self.registers, PL061_IEV,
                         struct.unpack_from("<I", self.registers, PL061_IEV)[0] | self.pin_mask)
        struct.pack_into("<I", self.registers, PL061_IC, self.pin_mask)
        self.shutdown_sent = False

    def host_press(self):
        struct.pack_into("<I", self.registers, PL061_RIS, self.pin_mask)

    def tick(self):
        if not (struct.unpack_from("<I", self.registers, PL061_RIS)[0] & self.pin_mask):
            return
        struct.pack_into("<I", self.registers, PL061_IC, self.pin_mask)
        struct.pack_into("<I", self.registers, PL061_RIS, 0)  # device clears on GPIOIC
        if self.shutdown_sent:
            return
        self.shutdown_sent = True
        self.signals.append((1, 15))  # proc_signal(1, SIGTERM)

    def test_first_press_signals_pid1_once_and_acknowledges(self):
        self.configure()
        self.tick()
        self.assertEqual(self.signals, [])
        self.host_press()
        self.tick()
        self.assertEqual(self.signals, [(1, 15)])
        self.assertEqual(struct.unpack_from("<I", self.registers, PL061_RIS)[0], 0)
        self.host_press()  # repeated presses are logged and ignored
        self.tick()
        self.assertEqual(self.signals, [(1, 15)])


class ShutdownPatchAndProbeTests(unittest.TestCase):
    def test_kernel_patch_routes_through_pid1_and_stays_gated(self):
        patch = (PATCHES / "puredarwin-virtualbuddy-power-button.patch").read_text()
        self.assertIn("proc_signal(1, SIGTERM)", patch)
        self.assertIn("setTimeoutMS(100)", patch)
        self.assertIn("BLAUOS_VIRTUALBUDDY", patch)
        self.assertIn('getProperty("blauos,power-button")', patch)
        self.assertIn("rejecting malformed", patch)
        self.assertIn("fPowerShutdownSent", patch)

    def test_loader_patch_validates_device_and_publishes_property(self):
        patch = (PATCHES / "xnu-loader-virtualbuddy-power-button.patch").read_text()
        self.assertIn("ARMH0061", patch)
        self.assertIn("blauos,power-button", patch)
        self.assertIn("gVB.power_irq<32||gVB.power_irq>=1020", patch)
        self.assertIn('"\\x86""PWRB"', patch)  # event handler must Notify the power button
        self.assertIn("EFI_COMPROMISED_DATA", patch)

    def test_probe_requests_stop_then_bounds_and_recovers(self):
        probe = (ROOT / "vm/virtualbuddy_console_probe.swift").read_text()
        self.assertIn("vm.requestStop", probe)
        self.assertIn("orderly-shutdown-timeout", probe)
        self.assertIn("force-stop-recovery", probe)
        self.assertIn("guestDidStop", probe)
        self.assertIn('"physical_gate_passed": false', probe)
        # The existing 20-check acceptance probe is untouched by this work.
        original = (ROOT / "vm/virtualbuddy_probe.swift").read_text()
        self.assertNotIn("requestStop", original)


if __name__ == "__main__":
    unittest.main()
