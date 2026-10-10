# ARM64 virtual baseline

A source-built ARM64 Darwin guest **passed the complete boot test** on
2026-10-10. Its independently built XNU kernel mounts persistent ext4 storage,
starts source launchd as PID 1, runs the source dash shell and passes the full
runtime and service tests. Native Ethernet registration, real DNS resolution,
guest-to-host and host-to-guest traffic, clock synchronization and guest shutdown
all pass. No stock Apple kernel or proprietary target runtime was booted.

The immutable proof is
[`guest-pass-v11/checkpoint.json`](../evidence/virtualization/pid1-checkpoint/guest-pass-v11/checkpoint.json),
with the actual serial log, boot result, image/library provenance, compact audit,
input hashes and QMP events beside it. The complete raw audit remains an ignored
local artifact; the summary preserves its path and exact hash. All 20 required
fresh-nonce markers passed. QMP confirms `guest=true`, `reason=guest-shutdown`
after source launchd terminated jobs and reaped the shutdown witness's child.

This establishes the virtual image (#21), networking (#22), independent runtime
and launchd functional tests. The clean build-and-boot CI replay (#23) is still
pending. Physical independent boot (Gate 2) requires separate M-series hardware
work; this virtual target does not satisfy it.

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
  in [ f.inputs.xnu-loader.packages.aarch64-linux.arm64-virt
       p.qemu p.pkgsCross.aarch64-multiplatform.OVMF.fd
       p.mtools p.dosfstools ]
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
This combination restored kernel execution but exposed unbound ARM64 dynamic
relocations, which caused XNU to reject every driver. The separately reviewed
`patches/kc-tools/0002-arm64-dynamic-relocations.patch` binds actual imports and
local pointers, normalizes the remaining slide table, and rejects unsupported
or unresolved input. Patch `0003-classic-arm64-protections.patch` removes the
consumed split-segment marker so XNU applies each kext's existing segment
permissions. No segment is made writable and executable.

The next genuine boot registered all 13 extensions and successfully started
corecrypto (`register ret=0`) and pthread. XNU's lockdown then removed execute
permission from the independently protected classic kext code. The narrow
VIRT adaptation in `vm/patches/xnu-classic-arm64-protections.patch` preserves
those existing per-segment protections; its selected branch was independently
checked in the compiled machine code. The actual corrected guest now starts
VirtIO storage and networking drivers, detects the diagnostic disk and publishes
`/dev/disk0`. That diagnostic image contains only an ESP. This is driver
initialization, not network connectivity. Actual serial output,
collection build commands, patches and input hashes are recorded under
`evidence/virtualization/driver-checkpoint/`. Earlier failed experiments remain
under `collection-checkpoint/`. The original pinned source and successful raw
kernel build remain intact.

`scripts/vm/build_collection.py` reproduces the patched tool build in a private
source copy, verifies the kernel/kext hashes, and orders driver dependencies
before their consumers. It requires the unstripped kernel's symbol table and
the documented source-built `-fno-stack-check` corecrypto variant; it refuses
to bind that module to a private kernel stack-probe helper.

There is a second incompatibility in the pinned loader: its `load_ramdisk`
calls are commented out. Consequently the upstream minimal image with
`rd=md0` cannot be accepted as working. The image wrapper selects
`image-arm64-virt-minimal-release`, which uses an ordinary ext4 root instead.

## Runtime images and CI

The native artifact path uses `scripts/vm/build_userspace.py` for source-built
network setup, shell commands, reboot and the runtime/launchd/network probes.
`scripts/vm/build_shell.py` cross-configures the pinned dash POSIX shell against
the independently built Darwin libraries; configure cannot run target programs
on the stock host runtime. Its build tools may run on the host.
`scripts/vm/assemble.py` audits the staged Mach-O closure before formatting
ordinary GPT, FAT ESP and ext4 files in Linux. Their actual source builds and
accepted guest execution are bound by the v11 artifact hashes and logs.

The first assembled image passed the recursive source-provenance audit and
mounted its genuine ext4 root as `/dev/disk0s2`. It attempted the source-built
`/sbin/launchd`; source dyld then faulted before main. A second diagnostic image
included `/cores` and retained writes in a fresh private disk copy. XNU produced
an actual PID 1 core. LLDB identified a `strcmp` helper-table dereference during
dyld's self rebasing. Using genuine upstream bootstrap primitives resolved that
fault in the next guest, whose actual core exposes a recursive `_open` /
`_open$UNIX2003` alias while looking for the absent shared cache. Routing to the
genuine raw syscall resolved that fault. The next actual guest maps thirteen
source libraries and reaches dyld console logging, then faults in a recursive
`strtoull` / `strtoull_l` compatibility alias during library initialization.
The genuine source locale implementations resolved that fault in the next
guest: launchd logged its PID 1 startup and stayed alive. Adding the relative
`/bin/launchctl` alias required by its bootstrap code then advanced the real
guest through fork, Mach post-fork ping, uncork and source launchctl execution.
The child faults with an upstream poisoned `malloc_zones` pointer; its actual
EL0 trace matches the `default_zone_malloc` dereference in source libdyld.
No child core was produced. Routing source libdyld through the genuine System
allocator and initializer resolved that failure: source launchctl loaded jobs,
and launchd executed the guest probe and source DNS daemon. The actual source
shell and expanded runtime tests then passed, including runtime image provenance
and the absence of a shared cache. This proof is retained under
`pid1-checkpoint/runtime-pass-v8-host-interrupted/`; the container
exited normally when its original two-hour lifetime expired before the
remaining smoke checks could finish. A replacement container uses `sleep infinity`
with bounded individual commands.

The network driver publishes its MAC but `en0` requires a native
IONetworkStack naming/registration request. The independently reviewed
`vm/patches/netsetup-interface-registration.patch` adds that genuine IOKit/CF
protocol to source netsetup. It succeeds only after the requested BSD interface
is actually visible through `SIOCGIFFLAGS`. Its compilation and links to the
source IOKitCF, CoreFoundation and System libraries passed. The v11 guest then
attached `en0`, configured its address and route, resolved `example.com` through
the real source mDNS daemon, and exchanged the host challenge and nonce-bound
health response in both network directions. The retained guest
`mdnsd.log` shows the actual `example.com` query, UDP send to `10.0.2.3:53`,
received DNS reply and returned addresses; it was extracted read-only from the
shut-down guest disk.
Serial logs, the prior core hashes,
input manifest, fault registers and debugger reports are tracked in
`evidence/virtualization/pid1-checkpoint/` and
`evidence/virtualization/pid1-core-v2-lldb.log`, `pid1-core-v3-lldb.log`, and
`pid1-core-v4-lldb.log`. The cores and writable disks
remain ignored local artifacts. `--writable-disk-copy --trace-exceptions`
reproduces this bounded diagnostic mode without changing the input image.
On Linux, `python3 scripts/vm/extract_core.py --disk /path/to/guest-writable.img
--output /fresh/core-output` extracts the ordinary root partition with read-only
`debugfs`, validates the actual ARM64 Mach-O core, and records its hash.

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
nonce response. With `--require-shutdown`, it asks the guest to terminate PID 1
and requires real launchd job termination, child reaping, and a QMP `SHUTDOWN`
event explicitly attributed to guest shutdown. Host process termination cannot
satisfy that check. Panic, missing runtime, process exit and timeout fail the run.
Tests of those failure checks do not count as guest-boot evidence.

The GitHub workflow runs lock validation and harness tests on ordinary pull
requests. `native-baseline.yml` runs `scripts/vm/native_ci.py` on a provisioned
ARM64 macOS runner labelled `blauos-native-sdk`. It checks the exact local SDK
and tools, fetches fresh pinned source trees, builds the kernel and all userspace
components, and formats/boots the image inside a disposable pinned Linux
container. The first complete clean pipeline reached the guest and failed on
the same early dyld fault; successful source builds alone do not pass this job.

The earlier explicitly requested Nix VM job requires a provisioned Linux ARM64
runner labelled `blauos-sdk`, Nix and the local SDK archive, version and hash
in repository variables. The SDK stays on that runner. The job archives
manifests, source-built kernel symbols, serial logs and results; it must fail
until source builds, image audit and real guest readiness all succeed. Its Nix
source-build route remains a separate experiment; the passing v11 guest used the
source-backed native macOS build profile and Linux image/VM tools.
