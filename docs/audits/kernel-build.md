# DEVELOPMENT ARM64 XNU build experiments

Tracks #15 and #16. On 2026-10-10, PureDarwin's pinned XNU produced a genuine
`DEVELOPMENT ARM64 VIRT` Mach-O kernel, symbols and dSYM on the macOS build host.
The latest published Apple XNU experiment reached concrete missing security
interfaces after its public generated inputs were resolved. Neither result
establishes physical M1 boot or Gate 2.

## Reproduction

Fetch the source pins with the repository source tooling. Commands below use
`sources.lock.json` and reject a mismatched or modified checkout.

```sh
python3 scripts/build/headers.py
python3 scripts/build/xnu.py \
  --sdk .blauos/build/kernel/headers/MacOSX.sdk
python3 scripts/build/xnu.py \
  --sdk .blauos/build/kernel/headers/MacOSX.sdk \
  --build-root .blauos/build/kernel/resolved-includes \
  --evidence evidence/kernel/resolved-includes \
  --make-setting 'WARNFLAGS_STD=-Wall -Wextra -Wno-missing-include-dirs'
python3 scripts/build/xnu.py \
  --puredarwin --source-name puredarwin-vm-next --machine VIRT \
  --with-kexts \
  --build-root .blauos/build/kernel/puredarwin \
  --evidence evidence/kernel/puredarwin-replay
```

The first modern kernel command is expected to fail on this host; the
diagnostic warning setting removes absent include-directory warnings so actual
required private interfaces become visible. It changes warning policy and
does not create fake headers or remove security functionality. The modern
experiment preserves `ARCH_CONFIGS=ARM64`, `KERNEL_CONFIGS=DEVELOPMENT`, the
audited SDK overlay and all arguments in its command JSON.

Use a new `--build-root` for a clean repeat. `headers.py` builds AvailabilityVersions,
installs genuine exported/MIG-generated XNU interfaces and creates an isolated
SDK overlay. It never writes into the installed Xcode SDK. Target images must
not package that overlay's proprietary host SDK contents.
Two isolated header regenerations installed the same 3,426 files with matching
SHA-256 values for every file; see
[header reproducibility](../../evidence/kernel/header-reproducibility.json).

The PureDarwin experiment's configuration is DEVELOPMENT, not RELEASE. Its
make invocation also selects ARM64 and MACHINE_CONFIGS=VIRT, BUILD_LTO=0,
BUILD_WERROR=0 and DO_CTFMERGE=0 through the pinned CMake build. The original
fresh configuration and build commands are preserved in
[configure command](../../evidence/kernel/puredarwin-configure-command.json) and
[build command](../../evidence/kernel/puredarwin-build-command.json).

## Observed artifacts

| Artifact | Result |
| --- | --- |
| Installed kernel | `.blauos/build/kernel/puredarwin/src/Kernel/xnu/xnu/System/Library/Kernels/kernel.development.virt` |
| Kernel format | Mach-O 64-bit executable ARM64, 12,571,728 bytes |
| Kernel SHA-256 | `d5b34ec4ec049d6a8c9e6939c16981e339d04204f49e7c661951943ac04d2ad9` |
| Symbol-bearing kernel | `.blauos/build/kernel/puredarwin/src/Kernel/xnu/xnu_sym/kernel.development.virt` |
| Symbol kernel SHA-256 | `2b703541dd2b562292970b8643d04b27817c6b73bd00db7960f833929ca707a8` |
| dSYM | `xnu_sym/kernel.development.virt.dSYM/Contents/Resources/DWARF/kernel.development.virt` |
| dSYM SHA-256 | `b4a0ff4437b54fb9d17fc1279bc052c8f8f9954cc7d5ddde0e405adaf7f388d6` |
| Mach-O UUID | `24B6DB58-FF3D-33AC-8F0B-9E1E2A3EA5EA` |
| Entry state | `LC_UNIXTHREAD`, PC `0xfffffff00736a498` |
| Undefined symbols | `nm -u` returned no undefined symbols |
| Source | Clean pinned PureDarwin checkout `1bbaeb2ee6aa41e6f792a0b1e2943cc295b3b290`, kernel MasterVersion `20.5.0` |

This is a self-built XNU kernel, with the real kernel sources and virtual-machine
platform code. It is not the stock macOS kernel. The pinned PureDarwin tree
already includes extensive patches and compatibility implementations: they
need review and ongoing provenance tracking. A virtual-machine target is
separate from physical Apple Silicon platform enablement.

The initial kernel build completed with exit zero and produced 959 object files
under the build root. Its 209,789,378-byte warning log is retained under ignored
`.blauos/build/kernel/puredarwin/logs/initial-build.log`, with SHA-256
`b12ea330e100dd20a8530dcac78686dffce64b87ad0c4b374439331bb8089769`.
The repository contains a compact
[build excerpt](../../evidence/kernel/puredarwin-build.log),
[artifact provenance](../../evidence/kernel/puredarwin-result.json),
[entry/load commands](../../evidence/kernel/puredarwin-mach-o-load-commands.log),
[symbol sample](../../evidence/kernel/puredarwin-symbols.log) and
[UUID capture](../../evidence/kernel/puredarwin-build-id.log).

## Modern source results and remaining inputs

The official source pin is `xnu-12377.121.6` at
`ac9718fb1af618d5ce8678d0dc6e8a58f252216f`, from Apple's macOS 26.5 source set.
The initial untouched make experiment failed to discover a kernel version from
the SDK/KDK. Supplying Darwin `25.5.0` moved it into SETUP host-tool compilation
and exposed missing availability tooling. Building pinned AvailabilityVersions
resolved that tooling. Header installation then completed with 3,426 installed
header/definition files, including 3,213 `.h` files and 160 `.defs` files.
These include both source exports and generated headers; the count is not a
claim that every header was synthesized.

The next kernel attempt encountered missing private include roots. The
diagnostic build then exposed actual required interfaces:
`CoreEntitlements/V2/API.h`, `TrustCache/API.h`, and
`CodeSignature/Entitlements.h`. A search of repositories owned by the official
Apple open-source distributions organization found no standalone repositories
with those package names; that limited search does not prove the interfaces
are unavailable everywhere. No replacements were fabricated.

Apple's [pinned XNU instructions](https://github.com/apple-oss-distributions/xnu/blob/ac9718fb1af618d5ce8678d0dc6e8a58f252216f/README.md)
require a matching Kernel Debug Kit for an external ARM64 build. None is
installed on this host. Its make rules additionally reference machine support
archives in `KDKROOT/System/Library/KernelSupport`, public libdispatch firehose,
and the kernel compiler runtime. The modern experiment has not reached a
successful link; a KDK-based binary would require provenance and independence
review before inclusion in an independently built distribution.

For the modern build, completion evidence is a replayable blocker rather than a
kernel. For the PureDarwin VM build, compilation and artifact inspection pass.
The original PureDarwin Nix profile expects a separately hashed macOS 11.3 SDK;
this macOS 27.2 host-SDK build is a documented adaptation experiment.

## Boot packaging and limits

The 13 essential headless VIRT kernel extension bundles also compiled from the
same pinned checkout and kernel headers: platform expert, PCI, storage, ext4,
filesystem selection, corecrypto, pthread, networking and VirtIO transport/block/net.
`--with-kexts` reproduces those targets. The
[bundle inventory](../../evidence/kernel/kexts/result.json) records identifiers,
library dependencies and executable/plist hashes.

The VIRT kernel requires the appropriate loader, device-tree/boot arguments,
memory layout, root image and QEMU device configuration. Its ARM64 entry load
command is necessary but insufficient to establish a working VM. The VM task
owns actual boot tests and serial evidence.

A physical M1 experiment additionally needs the correct hardware target,
native authorized boot assets/kernel collection, compatible firmware and
platform drivers. Selecting VIRT or VMAPPLE does not provide those interfaces.
Native boot policy, required kernel collection format, KDK/firmware version
compatibility and essential platform drivers remain experimentally unproven.

No byte-for-byte reproducibility claim follows from the first kernel build.
Upstream version generators include build time, identity and paths, and tool
versions can affect output. The source pins, exact commands, generated-input
hashes and artifact identifiers provide an auditable experiment; two isolated
builds with controlled timestamps and paths are required for a deterministic
release claim. Gate reviews and physical boot cannot be replaced by the
successful compile reported here.

A second clean build used a separately fetched, verified checkout at
`.blauos/verification/sources/puredarwin-vm-next` and a new
`.blauos/build/kernel/reproducibility` output root. It completed with exit zero,
produced a genuine ARM64 kernel and dSYM, and had no undefined kernel symbols.
The [comparison](../../evidence/kernel/reproducibility/comparison.json) records
kernel SHA-256 `bcb57010ef19e53081b52f2730fb78b7042501e7e4ada6accacf4979da71f943`
and UUID `677FAA24-7DF9-33E9-AB33-06B22D3208B6` for this repeat. The two kernels
contain different build timestamps and absolute source paths; their bytes and
sizes differ. This proves repeatable source compilation with pinned inputs,
while deterministic release bytes remain additional work.

The VM experiment subsequently executed this genuine kernel and printed its
Darwin banner on the emulated ARM64 serial console. Packaging the independently
built extensions exposed three separate defects in the pinned external KC tool:
4 KiB synthetic segments for a 16 KiB kernel, unprocessed ARM64 dynamic pointer
relocations, and split-segment metadata that suppressed individual extension
memory protections. The VM wrapper replays the reviewed
[page-layout patch](../../vm/patches/kc-tools-arm64-16k.patch),
[relocation patch](../../patches/kc-tools/0002-arm64-dynamic-relocations.patch), and
[protection patch](../../patches/kc-tools/0003-classic-arm64-protections.patch)
against an isolated copy of the pinned tool. Original external checkouts stay
unmodified.

The [independent pointer audit](../../evidence/kernel/kc-relocations/pointer-verification.json)
checks all 16,344 actual import/local pointer slots across thirteen source-built
extensions against the kernel and preceding dependency exports. It also checks
executable start/stop hooks, 16 KiB segment alignment, complete slide metadata,
and absence of writable executable segments. The
[malformed-input checks](../../evidence/kernel/kc-relocations/rejection-tests.json)
reject unsupported relocation types, unresolved imports, unmapped targets and
out-of-bounds tables without emitting a partial collection. These static checks
do not substitute for the VM boot test.

With the current host compiler, corecrypto additionally needs `-fno-stack-check`:
the default compiler emits a macOS `___chkstk_darwin` import that the target
kernel does not export. The
[source rebuild](../../evidence/kernel/corecrypto-no-stack-check/result.json)
removed that import while retaining the clean-repeat kernel's exact hash.
`xnu.py --with-kexts` now records this C/C++ compilation setting explicitly.
Kernel execution is established; a successful shell, PID 1 and networking must
be demonstrated by their separate boot acceptance tests.

The exact fourth VM collection also passes an independent
[pointer verification](../../evidence/kernel/kc-relocations/v4-pointer-verification.json).
Its real boot registered all thirteen extensions and started corecrypto and
pthread. A later fault in corecrypto's `_cckprng_generate` came from
`arm_vm_prot_finalize` replacing the classic cache's individually protected
executable segments with a blanket nonexecutable mapping during lockdown.
The reviewed [VIRT kernel patch](../../vm/patches/xnu-classic-arm64-protections.patch)
preserves protections already installed by OSKext for this classic VIRT layout;
coalesced caches and other hardware retain the original policy. It introduces
no writable executable mapping or random-number fallback.

A fresh isolated pinned-source overlay compiled that patch successfully:

```sh
python3 scripts/build/xnu.py --puredarwin --source-name puredarwin-vm-next \
  --machine VIRT --build-root .blauos/build/kernel/classic-protections-v3 \
  --evidence evidence/kernel/classic-protections-v3 \
  --patch vm/patches/xnu-classic-arm64-protections.patch --jobs 8
```

The initial [build result](../../evidence/kernel/classic-protections-v2/result.json)
records the source and patch hashes, commands and genuine kernel artifacts.
The unstripped kernel used for extension linking has SHA-256
`434d68fa3ffac5f3eedede65b34e0de2e28762ae41d03f1f9589a6958d65a0a6`.
That first patch used an undefined platform macro, so the intended branch was
excluded and the fifth VM boot reproduced the fault. The corrected patch uses
the actual `QEMUVIRT` definition from the pinned platform header; its build uses
a separate `classic-protections-v3` source overlay and evidence directory.
The corrected [clean build](../../evidence/kernel/classic-protections-v3/result.json)
completed successfully, with unstripped kernel SHA-256
`9fa18b9912389d142e65a4625d01d19b1402585eac0a6428a28d9a1ec06f0f8c`.
An [object disassembly comparison](../../evidence/kernel/classic-protections-v3/compiled-branch-check.json)
verifies that the compiled function now contains the additional cache-layout
comparison and branch over blanket RNX; the earlier object lacked that branch.
The overlay excludes one unused upstream absolute symlink,
`src/Libraries/libSystem/libc/gen/FreeBSD/result`, and records that exclusion;
it never recreates a developer's absolute host path. Successful compilation
does not establish that this patch reaches userspace; the VM evidence owns
that execution result.

The exact sixth collection passes the independent
[pointer and segment check](../../evidence/kernel/kc-relocations/v6-pointer-verification.json)
and the [source review](../../evidence/kernel/kc-relocations/review.json).
Its recorded guest execution passes corecrypto lockdown, publishes the VirtIO
disk and network device, and starts the essential drivers. A later full source
image mounts the ext4 root filesystem and attempts PID 1; the captured PID 1
fault is in dyld's bootstrap rather than kernel driver initialization.

The pinned ARM platform driver also lacks a power-off callback: its inherited
`haltRestart` returns `-1`, after which `halt_all_cpus` spins. The reviewed
[platform patch](../../vm/patches/pdarm-qemu-psci-shutdown.patch) provides PSCI
`SYSTEM_OFF` and `SYSTEM_RESET` through HVC for the explicitly selected QEMU
`virt-10.2,virtualization=off` profile. It defaults off at build time and also
checks the loader's exact `qemuvirt-io` marker before calling firmware. The
[primary source findings](../../evidence/kernel/qemu-psci/platform-proof.json)
record why this profile uses HVC and why a successful power-off must produce a
guest-originated QMP shutdown event. A build result alone does not satisfy the
guest shutdown acceptance criterion.

The [fresh full build](../../evidence/kernel/qemu-psci/result.json) compiled
the kernel and all thirteen extensions with both reviewed patches:

```sh
python3 scripts/build/xnu.py --puredarwin --source-name puredarwin-vm-next \
  --machine VIRT --with-kexts --qemu-psci-hvc --jobs 8 \
  --build-root .blauos/build/kernel/qemu-psci \
  --evidence evidence/kernel/qemu-psci \
  --patch vm/patches/xnu-classic-arm64-protections.patch \
  --patch vm/patches/pdarm-qemu-psci-shutdown.patch
```

The platform extension is byte-identical to the independently compiled
platform-only build. Its [decoded instructions](../../evidence/kernel/qemu-psci/platform-fast/compiled-hook-check.json)
contain the board guard, one `HVC #0`, and the exact two PSCI function IDs.
The [invalid profile checks](../../evidence/kernel/qemu-psci/profile-rejections.json)
reject enabling this profile without the driver build or without the reviewed
platform source patch before creating any build directory.
