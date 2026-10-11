# VirtualBuddy ARM64 platform experiment

BlauOS now boots its source-built XNU kernel and independent Darwin userspace
on VirtualBuddy's generic EFI platform. The complete public
Virtualization.framework test passes all 20 fresh-challenge checks, including
persistent ext4 root mounting, launchd as PID 1, source dash shell execution,
runtime and service tests, DNS, both TCP directions, child cleanup and
**guest-triggered poweroff**. A second test passes networking and poweroff with
VirtualBuddy's device configuration and **no added serial port**.

The packaged standalone demo also boots inside the VirtualBuddy application;
its live health response and open VM disk are recorded separately.
The [checkpoint](../../evidence/virtualization/virtualbuddy-platform/checkpoint.json)
links the actual guest logs, public framework callbacks, build inputs and image
hashes. These are VM results. They do **not** pass physical Apple Silicon Gate 2.
An M1 Mac mini still requires native boot-policy validation and Apple hardware
drivers; this port uses virtual GIC and virtio devices.

## Open the local demonstration

The new VM is `BlauOS-VirtualBuddy.vbvm` in the VirtualBuddy library. Open it and
press Play. The original `BlauOS-ARM64-Experiment` and `BlauOS-EFI-Diagnostics`
VMs remain separate. The demo stays running and exposes:

```text
http://192.168.64.200:18080/health
```

That endpoint reports completion of the kernel, PID 1, shell, runtime and DNS
checks. This static address belongs to the observed local NAT subnet; use a
free address and the actual gateway/DNS when building for another host. Run
only one copy using that address at a time. The demo has no SSH server or
working interactive USB keyboard driver yet. Its shell is exercised as a guest
process. It is an experimental bootable image, not a production installation.

VirtualBuddy's normal Stop request is not handled by the guest yet. Close the
VM window and confirm “Stop VM” to force-stop this demo before changing its disk.
Guest-triggered orderly poweroff passes in the automated backend tests;
the running demo has no shutdown endpoint. Host-requested shutdown is tracked
in [#102](https://github.com/joeblau/blauos/issues/102). Interactive keyboard work is
tracked in [#101](https://github.com/joeblau/blauos/issues/101), and SSH in
[#55](https://github.com/joeblau/blauos/issues/55).

The framebuffer driver initializes a 1280×800 text console. Screen-capture
permission was unavailable during the automated tests, so the guest logs and
network prove execution; a captured VirtualBuddy GUI screen is not claimed.
The original stalled screen will persist if the original VM is started again.

## What changed

The original loader prints through EFI, then polls QEMU's PL011 UART at
`0x09000000`. That assumption explains why disabling that path advanced the
old experiment. The exact original faulting instruction was not captured.
The new explicitly selected loader reads EFI/ACPI data and prepares XNU's
memory, interrupt and PCI properties. It cleans the handoff data from caches
before leaving EFI, and can use a bounded polled virtio serial console when a
backend probe supplies one. Stock VirtualBuddy boots without that extra port.

The tested profile is one CPU and 1 GiB RAM, VirtualBuddy 2.1 (325), hosted on
an M3 Max running macOS 27.2 beta. Firmware reported:

| Resource | Observed value |
| --- | --- |
| RAM | `0x70000000–0xB0000000` |
| GICv3 distributor / redistributor | `0x10000000` / `0x10010000` |
| Redistributor region | `0x20000` bytes |
| Virtual timer | IRQ 27, 24 MHz |
| PCI ECAM | `0x40000000`, segment 0, buses 0–255 |
| PCI memory window | `0x50000000–0x6FFDFFFF` |

The loader validates table checksums and lengths and rejects unsupported
layouts. PCI and GIC drivers consume the supplied properties. The optional
console patch enables ARM64 virtio text output, uses the backend's accepted
opaque BGRX resource format, and avoids the unavailable compressed boot-logo
decoder. The VIRT console selector stays writable for the late framebuffer
transition. Virtio reset waits for acknowledgement, and the network driver
negotiates the advertised MTU while retaining a standard 1500-byte guest MTU.
It does not acknowledge unimplemented checksum or segmentation offloads.

## Build and package

Keep pristine pinned sources intact. Apply patches in isolated source trees:

- Loader `8295cd89c184785896d007d0554b398d22d8e32f`: EFI diagnostics patch,
  then `xnu-loader-virtualbuddy-platform.patch`. Build with
  [`virtualbuddy-loader.nix`](../../vm/virtualbuddy-loader.nix) on the pinned
  ARM64 Linux Nix builder. The VirtualBuddy option defaults OFF.
- PureDarwin `1bbaeb2ee6aa41e6f792a0b1e2943cc295b3b290`: classic ARM64
  protections, PSCI shutdown, VirtualBuddy platform, then console patches.

The native kernel command is:

```sh
python3 scripts/build/xnu.py \
  --source-name puredarwin-vm-next --puredarwin --machine VIRT \
  --with-kexts --qemu-psci-hvc --virtualbuddy \
  --patch vm/patches/xnu-classic-arm64-protections.patch \
  --patch vm/patches/pdarm-qemu-psci-shutdown.patch \
  --patch vm/patches/puredarwin-virtualbuddy-platform.patch \
  --patch vm/patches/puredarwin-virtualbuddy-console.patch \
  --build-root .blauos/build/virtualbuddy/kernel \
  --evidence .blauos/build/virtualbuddy/kernel-evidence
```

Build the independent runtime and guest commands with the existing native
pipeline. Audit all 16 extension bundles, including `IOGraphicsFamily`,
`IOGOPFramebuffer` and `IOVirtIOGPU`, and supply that inventory to
`build_collection.py --virtualbuddy`. Keep the source-built collection format
and builder patches from the accepted QEMU pipeline.

Use `assemble.py` on the pinned Linux builder with the audited runtime root,
its current provenance, the new collection and EFI loader. Add:

```sh
--root-mb 1024 \
--boot-args '-v debug=0x218 serial=3 keepsyms=1 no_interrupt_masked_debug=1 gopconsole=1' \
--network 192.168.64.200 255.255.255.0 192.168.64.1 192.168.64.1
```

Append `--standalone` for the GUI demonstration. This mode prints `BLAUOS_DEMO`
messages and serves a health page. It cannot emit acceptance markers or invoke
the authenticated smoke-test shutdown route. Without that option, the image
requires a fresh host challenge and is suitable for automated acceptance.
DNS configuration is written before mDNSResponder starts. The EFI application
and collection must reside at `/EFI/BOOT/BOOTAA64.EFI` and `/EFI/BOOT/kernel`.
Verify both files by copying them back from the stopped image.

Package a fresh VM with the assembler's image hash:

```sh
python3 scripts/vm/virtualbuddy.py \
  --image /path/to/blauos-arm64.img --image-sha256 SHA256 \
  --output '/path/to/BlauOS-VirtualBuddy.vbvm'
```

This copies an ordinary disk image, verifies it, assigns a fresh UUID/MAC and
creates the tested one-CPU, 1-GiB configuration. VirtualBuddy creates fresh EFI
variables on first start. Existing VM bundles are rejected and preserved.
The Linux selector chooses generic EFI; it does not make the guest Linux.
The actual [assembly commands and receipts](../../evidence/virtualization/virtualbuddy-platform/)
record all inputs. Bit-identical filesystem rebuilds have not been established.

## Automated acceptance

Compile the public backend probe on macOS and sign it with virtualization
entitlements:

```sh
swiftc vm/virtualbuddy_probe.swift -o /path/to/probe
codesign --force --sign - --entitlements vm/virtualization.entitlements.plist /path/to/probe
python3 scripts/vm/virtualbuddy_smoke.py \
  --image /path/to/disposable-test.img --image-sha256 SHA256 \
  --probe /path/to/probe --output /path/to/fresh-evidence
```

The test writes to the supplied disposable image and creates fresh NVRAM.
It serves a random challenge to the configured guest address, checks every
serial marker, verifies the guest's health response, requests shutdown and
requires the public `guestDidStop` callback. Host termination cannot pass.
Repeat with a fresh disk copy and `--no-serial` to match stock VirtualBuddy.
That run proves the guest test program reaches its health service and powers
off, but does not claim serial markers it could not capture. No private
Virtualization APIs or Accessibility permission changes are used.

## Historical diagnostic evidence

The earlier EFI-only
[checkpoint](../../evidence/virtualization/virtualbuddy-efi-diagnostics/checkpoint.json)
stopped before XNU. Its 606,076,928-byte input, SHA-256 `35208e01…`, came from
an older **failed userspace experiment**, not the accepted baseline. Earlier
wording calling that image accepted was incorrect. The port first reached
independent userspace after switching to the accepted v11 root image,
SHA-256 `7ffa519c…`, and now builds fresh images with audited current commands.
Historical diagnostic traces retain their original input identities.

Sources: [pinned loader serial implementation](https://github.com/PureDarwin/xnu-loader/blob/8295cd89c184785896d007d0554b398d22d8e32f/src/serial.c),
[VirtualBuddy generic EFI helper](https://github.com/insidegui/VirtualBuddy/blob/088351b0fc67e0b24b83e7954ad48314dda4ce04/VirtualCore/Source/Virtualization/Helpers/LinuxVirtualMachineConfigurationHelper.swift),
[VIRTIO specification](https://docs.oasis-open.org/virtio/virtio/v1.2/virtio-v1.2.html).
