# ARM64 virtual baseline

An independently built ARM64 XNU kernel **executed in a real QEMU guest** on
2026-10-10. The guest printed its Darwin 20.5.0 DEVELOPMENT_ARM64_VIRT banner,
initialized kernel subsystems and produced a real panic/backtrace. The kernel,
bootloader and subsequent kernel extensions were built from pinned public
sources. No stock Apple kernel or proprietary target runtime was booted.

This completes the execution experiment for issue #19. It does **not** complete
the shell/root image (#21), networking (#22), complete build-and-boot CI (#23),
or physical independent boot (Gate 2). The guest currently stops before PID 1.

## Selected experimental backend

| Input | Immutable identity |
| --- | --- |
| PureDarwin `next` (including XNU 20.5.0 and VIRT platform code) | `1bbaeb2ee6aa41e6f792a0b1e2943cc295b3b290` |
| Source-built UEFI XNU loader | `8295cd89c184785896d007d0554b398d22d8e32f` |
| Kernel collection tools | `802fbc70ef5431950f24e79b02f2d938385d91bd` |
| IIG generator | `8d6651f5f1417820783e3b3eb38df8e481d0a2fb` |
| Nix Linux ARM64 container | `nixos/nix@sha256:a326ac1ed46069ead5cdcba3a3a1e7255ebf72300c8319bf2c12cacfe9ab2787` |
| Nix | `2.35.2` |
| QEMU | `10.2.4`, machine `virt-10.2`, TCG, `neoverse-n2`, GICv3 |
| AAVMF firmware | Nix `OVMF-202602`; file hashes accompany the execution logs |

The upstream flake and every transitive source are pinned in its checked-in
`flake.lock`; BlauOS keeps that lock under `manifests/`. This is a modified
Darwin 20 test target, not evidence that Apple's current published XNU works
unchanged on generic ARM virtual hardware or M-series Macs.

PureDarwin describes a QEMU `virt` target with a platform expert, GIC, virtio
drivers and a 16K page granule. Its instructions informed this experiment;
claims of a usable shell require our own boot evidence before acceptance.
[PureDarwin ARM64 target](https://puredarwin.org/handbook/targets/arm64-virt.html)

QEMU `virt` supplies generic virtual hardware, including PL011, GIC and PCI
virtio devices. It does not emulate Apple's AIC, DART or ANS. The firmware
passes control to the source-built loader, which constructs XNU's device tree
and boot arguments. [QEMU virt documentation](https://www.qemu.org/docs/master/system/arm/virt.html)

## Alternatives and limits

| Environment | Boot contract and device requirements | Decision |
| --- | --- | --- |
| QEMU + AAVMF + PureDarwin loader | Source-built VIRT kernel; matching platform/GIC/PCI/storage/network kexts; independent root and runtime | Selected for baseline experiments; actual guest kernel execution observed |
| Virtualization.framework macOS guest | `VZMacPlatformConfiguration`, supported restore image, Apple macOS bootloader and matching Mac virtual devices | Useful stock-kernel runtime scaffold; no public custom-kernel URL API establishes an independent boot path |
| Virtualization.framework Linux bootloader | Linux kernel loading contract and generic platform configuration | Cannot substitute an arbitrary XNU Mach-O for a Linux kernel |
| Virtualization.framework generic EFI | Valid generic EFI configuration demonstrated locally; its firmware and virtual-device contract still require an XNU loader and matching platform/drivers | Candidate requiring a separate adaptation experiment; successful configuration validation is not kernel execution |
| PureDarwin 17.4 x86_64 image | Legacy Intel kernel and RTL8139/PC devices | Historical comparison; cannot satisfy an ARM64 baseline |

Apple documents Linux loading separately from its Mac restore-image path.
The read-only capability probe built against the installed SDK found
Virtualization.framework support and validated a generic EFI/NAT configuration.
It never installed or ran a macOS guest.
[Apple Linux bootloader](https://developer.apple.com/documentation/virtualization/vzlinuxbootloader),
[Apple macOS VM installation](https://developer.apple.com/documentation/virtualization/installing-macos-on-a-virtual-machine).

QEMU and its packaged firmware are open source; the PureDarwin tree and each
reused component retain their own licenses. Kernel/runtime builds use a locally
owned proprietary Apple SDK as a **build input**. Do not upload that SDK or a
stock macOS image. No SDK/runtime binary is copied into the proposed guest.

## Reproduce the kernel execution experiment

Build the DEVELOPMENT VIRT kernel using [the kernel build record](audits/kernel-build.md). Retrieve the
pinned inputs using `scripts/blauos.py fetch`. On an ARM64 Linux builder with
Nix, obtain the VM tools from the pinned PureDarwin flake:

```sh
nix --extra-experimental-features 'nix-command flakes' build --impure --expr '
  let f = builtins.getFlake "path:/absolute/path/to/puredarwin-vm-next";
      p = import f.inputs.nixpkgs { system = "aarch64-linux"; };
  in { loader = f.inputs.xnu-loader.packages.aarch64-linux.arm64-virt;
       qemu = p.qemu;
       firmware = p.pkgsCross.aarch64-multiplatform.OVMF.fd;
       mtools = p.mtools; dosfstools = p.dosfstools; }
' --no-link --json --max-jobs 1 --cores 2
```

Put mtools and dosfstools on `PATH`, then run the bounded experiment with the
actual paths returned above:

```sh
sh scripts/vm/kernel-checkpoint.sh \
  /path/to/qemu-system-aarch64 \
  /path/to/AAVMF_CODE.fd /path/to/AAVMF_VARS.fd \
  /path/to/xnu-loader.efi \
  /path/to/kernel.development.virt \
  .blauos/build/vm/kernel-checkpoint 120
```

This creates an ordinary 64 MB file, loads the source-built kernel through EFI,
uses a disposable NVRAM copy and a snapshot disk, and terminates QEMU after at
most 120 seconds. No physical disk or host boot policy is changed. An execution
result requires the guest's Darwin kernel banner; it never reports shell or
network readiness.

Tracked logs and input hashes are in
`evidence/virtualization/kernel-checkpoint/`. The experiment's QEMU exit code
was 124 from its deliberate timeout. Its raw-kernel panic occurs in
`read_random`, where the PRNG callback remains unregistered without corecrypto.

## Kernel collections and remaining boot blocker

Thirteen source-built ARM64 extensions were linked with `kc-builder`. Both the
unmodified fileset format and its classic prelinked alternative stopped before
the kernel banner. Source analysis found a 4K synthetic segment in a 16K kernel
and a prelink placement above the kernel where XNU expected it below.

The independently reviewable
`vm/patches/kc-tools-arm64-16k.patch` applies only in an isolated tool build.
The classic collection is linked with `-kext-vmbase 0xfffffff004004000`.
This combination restored kernel execution and kext sliding. It still panics
in `read_random` before the crypto provider registers. Collection logs and
hashes are in `evidence/virtualization/collection-checkpoint/`; the original
upstream sources and successful raw kernel remain intact.

There is a second incompatibility in the pinned loader: its `load_ramdisk`
calls are commented out. Consequently the upstream minimal image with
`rd=md0` cannot be accepted as working. The image wrapper selects
`image-arm64-virt-minimal-release`, which uses an ordinary ext4 root instead.

## Runtime images and CI

`scripts/vm/build.py` prepares an isolated source copy, injects source-built
runtime/launchd/network probes and optionally pins a locally owned SDK archive.
The upstream Nix SDK input is MacOSX11.3 with SHA-256
`9adc1373d3879e1973d28ad9f17c9051b02931674a3ec2a2498128989ece2cb1`.
An actual realization failed with the precise missing-input message in
`evidence/virtualization/nix-sdk-realize.log`. Evaluating the original minimal
image succeeded: 102 source derivations and 380 substituted paths, 1.1 GiB
download and 5.4 GiB unpacked. Evaluation is recorded separately from building.

The explicit SDK27.2 adaptation uses the private local archive hash
`8230e9c59a84f995731a7242a41303cc69a352d2e1e8a0be4cf1021e3c943ea7`.
It is an experiment and cannot be substituted silently for the upstream SDK.
Source archive extraction omits developer symlinks escaping the source tree and
records each omission; those links must never import host build outputs.

The boot smoke test requires a passing recursive Mach-O dependency and source
provenance audit, pinned image/firmware/symbol hashes and a pinned QEMU version.
A fresh host nonce must appear in actual guest markers for kernel, launchd,
shell, runtime operations, clock, DNS, guest-to-host networking, a target-built
service and host-to-guest networking. The host also checks the live service's
nonce response. Panic, missing runtime, process exit and timeout fail the run.
Tests of those failure checks do not count as guest-boot evidence.

The GitHub workflow runs lock validation and harness tests on ordinary pull
requests. Its explicitly requested VM job requires a provisioned Linux ARM64
runner labelled `blauos-sdk`, Nix and the local SDK archive, version and hash
in repository variables. The SDK stays on that runner. The job archives
manifests, source-built kernel symbols, serial logs and results; it must fail
until source builds, image audit and real guest readiness all succeed.
