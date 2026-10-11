# Native M1 essential-driver interface contracts and reuse boundaries

Audit date: 2026-10-11. Issue
[#41](https://github.com/joeblau/blauos/issues/41). This audit defines the
XNU/IOKit interface contracts that the essential M1 (T8103) drivers must
satisfy and the reuse boundary between pinned public source, binary-only
inputs and license-restricted components. It is a documentation and source
audit only: **no essential driver has executed on physical M1 hardware**,
Gate 2 ([independent boot](https://github.com/joeblau/blauos/issues/40)) is
**NOT RUN**, and every hardware-matrix entry below is unverified. The
machine-readable receipt is
[evidence/native/driver-contracts.json](../../evidence/native/driver-contracts.json).

This issue is blocked by #40, #30 and #12 in the roadmap. None of those
gates has an explicit PASS/GO decision, so the contracts here are
preparation, not an implementation authorization.

## Pinned inputs

| Input | Identity | SHA-256 / revision |
| --- | --- | --- |
| Source lock | `sources.lock.json` | `de98f7289628e8ea53089b8945798afa01c2dd1afb1328b82e6a9a2321537cee` (audit-time working-tree hash) |
| Transitive Nix lock | `manifests/puredarwin-flake.lock` | `78cab4ea95807142177b6be3537783432c913f852ba25812add6cc6dc63ff47f` (matches `sources.lock.json` `transitive_locks`) |
| Native patch series | `patches/native/series` (17 patches) | `3286a6c5ce447a6626e954c2159ce1d5ecacf438061220924c13241ef3aaebef` (audit-time working-tree hash) |
| Modern Apple XNU | `apple-oss-distributions/xnu` tag `xnu-12377.121.6` | revision `ac9718fb1af618d5ce8678d0dc6e8a58f252216f`, tree `cba06fba7c9d041633c55cfa3e3130fc367a5a1b` |
| PureDarwin baseline | `PureDarwin/PureDarwin` `next` snapshot | revision `1bbaeb2ee6aa41e6f792a0b1e2943cc295b3b290`, tree `ed2345d7f3c11dad2e7f6ffeeb377adc789b12b8` |

## Reuse boundary

### Public, pinned source families

Two source families are lawful, pinned and reviewable. The project MIT
license ([LICENSE](../../LICENSE)) does not relicense either family; per
`sources.lock.json` `license_policy`, each component retains its own
notices, and [puredarwin-reuse.md](puredarwin-reuse.md) requires a
file-level inventory before any redistribution.

1. **Modern Apple family** (`upstream-modern` profile): XNU
   `xnu-12377.121.6` and its companion Apple-oss-distributions projects
   (AvailabilityVersions, libdispatch, bootstrap_cmds, dyld, Libsystem,
   Libc, libplatform, libpthread, libmalloc), APSL-2.0 / Apache-2.0 / BSD
   notices per component. Status: "build experiment; matching KDK not
   present". This family publishes the kernel architecture the contracts
   target, but its ARM64 device drivers are not public.
2. **PureDarwin family** (`puredarwin-vm` profile): `puredarwin-vm-next`
   plus iig-tools, kc-tools, xnu-loader (all BSD-3-Clause), a sparse
   llvm-project pin (Apache-2.0 WITH LLVM-exception) and dash. The pinned
   Nix closure is fixed by `manifests/puredarwin-flake.lock`. This is the
   tree the 17-patch native series (`patches/native/`) applies to, and it
   supplies the only in-repo T8103 platform-extension source
   (`src/Kernel/Extensions/PDArmPlatformExpert`, including `PDAppleAIC`).

Hardware research from third parties (for example the Asahi Linux AIC
documentation at revision `0d1f8917fa88745d62a4c05d802c4a7298c8616b`,
used by [patch 0007](../../patches/native/0007-t8103-aic-rearm-and-driver-abi.patch)
with its retrieval hash recorded in
[evidence/native/aic/native-final/inspection.json](../../evidence/native/aic/native-final/inspection.json))
is reference material only. The repo does not import Linux driver code;
patches use existing XNU vector and MMIO interfaces.

### Binary-only and license-restricted components

The following are outside the reuse boundary and must never be described
as reproducible public inputs:

- **Matching ARM64 KDK** for the modern profile. Apple's pinned build
  instructions require a KDK matched to the OS release and force-load its
  platform-support archive (`makedefs/MakeInc.def`, recorded in
  [native-security-inputs.md](native-security-inputs.md) and
  `sources.lock.json` `unavailable_inputs`). Not downloaded; the macOS 27.2
  build host is not a validated match for macOS 26.5.
- **Original `MacOSX11.3.sdk.tar.xz`** (SHA-256
  `9adc1373d3879e1973d28ad9f17c9051b02931674a3ec2a2498128989ece2cb1`)
  required by the unmodified PureDarwin Nix profile; proprietary and
  locally supplied only ([puredarwin-reuse.md](puredarwin-reuse.md)).
  The host SDK 27.2 is a build input, never copied into the target image
  ([source-dependencies.md](source-dependencies.md)).
- **Missing security headers**: `TrustCache/API.h`,
  `CoreEntitlements/V2/API.h`, `CoreEntitlements/V2/Kernel.h` and
  `CodeSignature/Entitlements.h` are absent from the inspected public
  trees ([native-security-inputs.md](native-security-inputs.md),
  [evidence/native/security-inputs.json](../../evidence/native/security-inputs.json)).
  No compatibility stubs are permitted; historical TrustCache headers are
  explicitly not approved matching inputs.
- **Apple firmware, boot authorization and proprietary drivers**:
  recorded separately from source-built components per
  [docs/scope.md](../scope.md); their presence never establishes an open
  implementation of the associated driver. IONVMeFamily, the Apple PCIe
  root-complex driver, DART, ANS/RTKit and the M1 Ethernet driver ship
  only as Apple binaries; no public source exists in either pinned family.
- **Asahi m1n1**: referenced by issue #41 as primary research material
  and kept as the explicitly activated fallback/debugging boot path
  ([docs/scope.md](../scope.md)); it is not a blauOS build input.

## Interface contracts

Each contract lists the kernel-side services the driver must consume, the
source basis in this repository, and the explicit error-handling,
resource-ownership and DMA/interrupt-lifetime obligations required by
issue #41. "Implemented source" means pinned, patched, compiled and
linked software only — never a physical observation.

### AIC (Apple interrupt controller) — P0

- **Contract**: an IOKit `IOInterruptController` registered as the
  primary controller, discovered from the ADT `arm-io`/`aic` nodes with
  uncached MMIO mapping; per-vector register/enable/disable/handler
  semantics honoring XNU's `IOInterruptVector` software-disable
  concurrency; post-event hardware re-arm only while a handler stays
  registered and enabled (AIC event reads auto-mask the source); bounded
  dispatch, spurious-event and storm handling; explicit failure
  propagation from resource discovery.
- **Source basis**: `PDAppleAIC` in the pinned PureDarwin tree, repaired
  by [patch 0007](../../patches/native/0007-t8103-aic-rearm-and-driver-abi.patch)
  and hardened by [patch 0016](../../patches/native/0016-t8103-aic-hardening.patch);
  audits [native-aic.md](native-aic.md),
  [native-aic-hardening.md](native-aic-hardening.md) and
  [native-platform-gaps.md](native-platform-gaps.md#apple-interrupt-controller--34).
  The final extension build
  ([evidence/native/aic/native-final/result.json](../../evidence/native/aic/native-final/result.json))
  links ARM64e KER00 objects with 453 undefined kernel imports whose names
  all resolve in the final kernel — name availability only, not import
  authorization.
- **Open obligations**: no demonstrated interrupt delivery, affinity,
  per-CPU routing or IPI interaction; runtime dispatch loop remains
  unbounded in source; physical tests required.

### UART (early serial) — P0

- **Contract**: PEXpert (not IOKit) polling console selected by
  `pe_serial.c::serial_init` from the ADT (`arm-io` dock-channel or
  `boot-console`/`uart0`/`uart1` compatible string), register offset plus
  SoC physical base, default 115200 8N1 adjustable by clock/divider
  properties; `PE_init_kprintf` selection with `cnputc_unbuffered`
  fallback; bounded TX waits and size-checked property reads.
- **Source basis**: existing Apple polling-UART implementations already
  in the pinned tree ([native-platform-gaps.md](native-platform-gaps.md#uart-and-diagnostics--29));
  opt-in diagnostic hook from
  [patch 0006](../../patches/native/0006-t8103-early-diagnostics.patch)
  with object evidence in
  [evidence/native/early-diagnostics/result.json](../../evidence/native/early-diagnostics/result.json);
  protocol subset in
  [docs/platform/m1-early-diagnostics.md](../platform/m1-early-diagnostics.md).
- **Open obligations**: `uart_putc` still waits for TX readiness without
  a bounded timeout in unpatched paths; several legacy property reads do
  not check returned size; physical UART reachability, captured bytes and
  panic/recovery behavior are unproven.

### Clocks, resets and power domains — P0 prerequisite / P2

- **Contract**: a platform service exposing clock-gate, reset-line and
  power-domain control to the drivers below, driven by ADT
  clock/reset/power properties; every consumer must treat an absent or
  malformed clock node as an explicit, logged fallback — never a silent
  default; CPU frequency/idle management is P2 and excluded from 0.1
  ([docs/scope.md](../scope.md)).
- **Source basis**: `pe_identify_machine` discovers SoC and clock
  information and `pe_arm_get_soc_base_phys` derives the SoC physical
  base, with known unvalidated property-read and offset-arithmetic gaps
  ([native-platform-gaps.md](native-platform-gaps.md#device-discovery-and-memory-reservations--30));
  [patch 0014](../../patches/native/0014-t8103-timer-discovery.patch)
  establishes the validated-lookup/overflow-check/logged-fallback pattern
  for the timer node ([native-timer-discovery.md](native-timer-discovery.md)).
  No public Apple clock/reset/power-management (PMGR) driver source exists
  in either pinned family.
- **Open obligations**: the entire clock/reset/power-domain driver must
  be reconstructed from hardware research under a named license; the real
  target ADT schema is not yet captured.

### DART / IOMMU (DMA translation) — P1

- **Contract**: an `IOMapper` implementation per DART instance so that
  DMA-capable drivers (ANS, Ethernet) obtain translated, lifetime-managed
  mappings through `IODMACommand`/memory-descriptor interfaces; strict
  ownership — a mapping dies with its command, never with the device;
  fault reporting must surface through the controller's interrupt
  contract rather than corrupting the caller.
- **Source basis**: none in either pinned family; listed as P1 scope in
  [docs/scope.md](../scope.md) and as an open platform-driver dependency
  in [source-dependencies.md](source-dependencies.md). The QEMU VIRT
  baseline deliberately does not emulate DART
  ([docs/virtualization.md](../virtualization.md)), so no virtual
  substitute exists.
- **Open obligations**: full reconstruction; no XNU fork changes —
  implementation must fit the public `IOMapper`/IODMACommand seam.

### ANS / NVMe storage — P1

- **Contract**: block storage surfaced as an IOKit media service behind
  the Apple NVMe (ANS2/RTKit) coprocessor path: mailbox/queue lifecycle,
  DART-backed DMA for all requests, bounded command timeouts with explicit
  error returns, and clean teardown on coprocessor reset. Stock
  IONVMeFamily assumptions do not apply; the ANS interface is not public
  source.
- **Source basis**: none in either pinned family; P1 per
  [docs/scope.md](../scope.md); the release contract requires persistent
  storage without proprietary target runtime components.
- **Open obligations**: full reconstruction gated on the DART and clock/
  reset contracts above; APFS containers, boot policy and disk arrangement
  remain scope decisions, not driver evidence.

### Ethernet via PCIe — P1

- **Contract**: PCIe enumeration through a root-complex platform driver
  producing `IOPCIDevice` nubs, then a NIC driver claiming the
  Macmini9,1 standard 1 Gb/s controller ([docs/scope.md](../scope.md))
  with DART-backed DMA rings, AIC-delivered interrupts, link-state and
  error accounting, and remote-reboot-safe teardown. The optional 10 Gb/s
  configuration is explicitly out of qualification.
- **Source basis**: none in either pinned family; the Apple PCIe
  root-complex and Ethernet drivers are binary-only. Native Ethernet
  registration and real DNS are explicitly absent from the virtual
  baseline ([docs/virtualization.md](../virtualization.md)).
- **Open obligations**: full reconstruction gated on DART, AIC and
  clocks/resets; virtio-net results from the QEMU profile are not M1
  evidence.

## Hardware-support matrix

Seeded with the only qualified 0.1 target. Every row carries an explicit
verification status; none has executed.

| Model | SoC | Driver | Verification status |
| --- | --- | --- | --- |
| Macmini9,1 | T8103 | AIC | unverified — no physical execution |
| Macmini9,1 | T8103 | UART | unverified — no physical execution |
| Macmini9,1 | T8103 | Clocks/resets/power domains | unverified — no physical execution |
| Macmini9,1 | T8103 | DART/IOMMU | unverified — no physical execution |
| Macmini9,1 | T8103 | ANS/NVMe storage | unverified — no physical execution |
| Macmini9,1 | T8103 | Ethernet (standard 1 Gb/s) via PCIe | unverified — no physical execution |

## Acceptance boundary

This audit satisfies the documentation and reuse-enumeration criteria of
issue #41 at the source level. It does not satisfy, and does not claim:
native driver execution, import/collection linkage of the platform
extension, firmware authorization, hardware attestation, or any Gate 2
evidence. Existing compile-level receipts (kernel and AIC extension) are
cited above as software evidence only. The next step toward verification
is the physical checkpoint protocol
([native-checkpoint-protocol.md](native-checkpoint-protocol.md)) on a
dedicated inventoried target — after Gate 2 has an explicit PASS/GO.
