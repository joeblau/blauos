# VirtualBuddy EFI diagnostic experiment

The current QEMU VIRT image does **not boot BlauOS in VirtualBuddy**. On
2026-10-10, the original VirtualBuddy 2.1 (325) VM displayed only
`XNU EFI loader start`. A separate diagnostic loader now reads and parses the
source-built kernel and collects the firmware memory map. It intentionally
stops before staging the kernel or leaving EFI boot services. Neither a Darwin
kernel banner nor PID 1 has been observed on this backend.

The [checkpoint](../../evidence/virtualization/virtualbuddy-efi-diagnostics/checkpoint.json)
binds the actual build, patch, source revisions, image, backend probe and
[firmware trace](../../evidence/virtualization/virtualbuddy-efi-diagnostics/firmware-trace.txt).
This optional virtual-platform experiment does not pass the physical M1 boot
gate or change the accepted [QEMU baseline](../virtualization.md).

## Why the original screen stops

The pinned loader's `log_emit` first prints through EFI `ConOut`, then sends
the same message through its serial implementation. The QEMU ARM64 serial
implementation polls PL011 at physical address `0x09000000`. That address is
a QEMU device assumption, and the first EFI line can appear before the serial
operation stalls. Disabling that MMIO path in the diagnostic build allows the
loader to advance. This supports the UART incompatibility diagnosis; the exact
faulting instruction in the original run was not captured.

Sources: [loader console](https://github.com/PureDarwin/xnu-loader/blob/8295cd89c184785896d007d0554b398d22d8e32f/src/console.c),
[loader serial](https://github.com/PureDarwin/xnu-loader/blob/8295cd89c184785896d007d0554b398d22d8e32f/src/serial.c).
VirtualBuddy's [generic EFI configuration](https://github.com/insidegui/VirtualBuddy/blob/088351b0fc67e0b24b83e7954ad48314dda4ce04/VirtualCore/Source/Virtualization/Helpers/LinuxVirtualMachineConfigurationHelper.swift)
selects `VZGenericPlatformConfiguration` and `VZEFIBootLoader` for its Linux
system selector. It does not create a QEMU `virt` machine or load a Linux kernel
directly in this configuration.

The observed firmware has 29 memory descriptors, with stride 48 and version 1;
its largest conventional range is `0x70000000–0xA8000000`. The existing profile
expects RAM at `0x40000000` and constructs a QEMU device tree. Removing the UART
assumption alone cannot establish compatible interrupts, timers, memory or
device discovery for XNU.

## Diagnostic VM and expected screen

The local VM is `BlauOS-EFI-Diagnostics.vbvm` in the VirtualBuddy library. It
contains a fresh copy of the accepted 606,076,928-byte image with only the EFI
loader replaced. Its configuration uses one CPU, 1 GiB of memory, generic EFI,
virtio block, graphics and NAT networking, and USB input. The root partition
hash remains identical to the accepted baseline before and after the probe.

Open that VM and press Play. The diagnostic ends with:

```text
EFI checkpoint complete: kernel parsed, memory map collected
VirtualBuddy platform adaptation required; XNU was not started
Diagnostic result: Unsupported; XNU was not started.
Holding this report for 15 seconds.
```

The report is held for 15 seconds before returning to firmware. Firmware can
retry the loader. This behavior is a diagnostic checkpoint, not an installer,
shell or working operating system. The trace is also appended as UTF-16LE to
`\EFI\BOOT\blauos-efi.txt` in the disposable VM's EFI partition, capped at
64 KiB. Stop the VM before reading that disk with mtools:

```sh
mcopy -i /path/to/diagnostic/Disk.img@@1048576 \
  ::/EFI/BOOT/blauos-efi.txt firmware-trace.utf16
iconv -f UTF-16LE -t UTF-8 firmware-trace.utf16
```

## Reproduce the diagnostic loader

The patch is an explicitly selected external-loader overlay, outside the native
kernel patch series. Its base is locked XNU loader
`8295cd89c184785896d007d0554b398d22d8e32f`. Prepare a fresh source checkout from
that revision and apply
[`xnu-loader-efi-diagnostics.patch`](../../vm/patches/xnu-loader-efi-diagnostics.patch).
The [reconstruction receipt](../../evidence/virtualization/virtualbuddy-efi-diagnostics/patch-reconstruction.json)
checks every reconstructed source file against the actual Nix build input.

Build the patched source using the pinned PureDarwin flake's
`packages.aarch64-linux.arm64-virt` derivation, overriding `src` with the fresh
patched source and appending `-DXNU_LOADER_EFI_DIAGNOSTICS_ONLY=ON` to
`cmakeFlags`. The exact
[build command](../../evidence/virtualization/virtualbuddy-efi-diagnostics/build-command.json)
and [derivation](../../evidence/virtualization/virtualbuddy-efi-diagnostics/loader-derivation.json)
record the successful ARM64 Linux build with GCC 14.3.0 and GNU EFI 4.0.2.
Replace only `\EFI\BOOT\BOOTAA64.EFI` on a fresh, stopped VM disk copy.
The built EFI application SHA-256 is
`fe76eec178c2775a151fd1db08581595c39ccf6010653e2250fd8a82de205607`.

The option defaults to OFF and requires the ARM64 QEMU source profile. Actual
GCC preprocessing of all four modified C files is identical to pristine
source with diagnostics OFF, for both ARM64 QEMU and BCM2837 configurations.
CMake rejects a diagnostic build with the wrong profile. These checks and the
successful EFI build are recorded beside the checkpoint.

The tracked `backend-probe.swift` reproduces a bounded test using public
Virtualization.framework APIs and its own window. It uses the VirtualBuddy
device configuration without an extra serial port. Its AppKit captures do not
capture the remote VM graphics layer and are not visual boot evidence; the
firmware file trace establishes the checkpoint. Earlier private screenshot API
probes stopped unexpectedly at capture time. No-capture controls invalidate
that earlier guest-fault inference.

## Work needed for kernel execution

Tracked in [issue #99](https://github.com/joeblau/blauos/issues/99).

First inventory the actual firmware tables, RAM reservations, interrupt
controller, timer and PCI device layout. Then build an explicit VirtualBuddy
platform profile with matching XNU boot arguments, device discovery, early
console and essential drivers. Validate kernel execution before attempting
independent PID 1, persistent root mounting, a shell or networking. A VM that
starts or a loader that parses a kernel cannot satisfy those acceptance tests.
