# Milestone 2 execution record

Milestone 2 is **Phase 1 — Native kernel bring-up**, tracked by
[#4](https://github.com/joeblau/blauos/issues/4). Work started on 2026-10-10
with three parallel engineering agents and a coordinating reviewer.
The milestone and Gate 2 remain open: no independently built kernel has
executed on the required physical M1 Mac mini.

## Observed results

The [actual host inventory](../evidence/native/lab/build-host-inventory-redacted.json)
identifies `Mac15,8`, an M3 Max MacBook Pro. The collector rejects it as a
test-machine target with exit 2. Read-only noninteractive SSH probes to two
existing network Macs failed authentication; their models and suitability
remain unknown. Target access, dedicated ownership, a recovery operator and
native UART transport have not been established. No policy, disk, firmware
or boot assets on those machines were changed.

Two pinned source families are under actual T8103 build investigation. Modern
Apple XNU reaches compilation but requires unavailable TrustCache and
CoreEntitlements interfaces. The older PureDarwin source has H13/T8103 board
definitions and an Apple AIC integration; a separate narrow native build
profile now completes a fresh ARM64e KER00 DEVELOPMENT kernel build.
It produces a raw executable and debug symbols; target-compatible boot assets
and physical execution remain unverified. The
[native source/build audit](audits/native-kernel-build.md) and
[raw experiment receipts](../evidence/native/kernel/) record actual commands,
tool/SDK identities, patch hashes, failures and artifact counts. No VIRT
kernel was relabeled or packaged as a native candidate.

The [native platform extension experiment](audits/native-aic.md) compiles and
links the actual AppleARM/AIC module with matching ARM64e KER00 objects. A
reviewed source change re-arms enabled hardware IRQs after the AIC event read
automatically masks them. The linked extension's kernel imports remain to be
resolved against the independent native kernel; real interrupt delivery,
affinity, IPIs and storm behavior remain untested.

The [P0 source audit](audits/native-platform-gaps.md) identifies remaining
implementation work as well as physical tests. The platform module does not
register processors or finalize the native CPU count, and its secondary
startup returns failure. Runtime resource bounds, initialization failure
propagation, controller naming/affinity and timer discovery also need work
against the captured target handoff. A linked kernel does not resolve those
interfaces or establish a functioning native platform.

Read-only lab inventory, bounded UART collection, explicit-source boot-argument
decoding, Apple device-tree parsing and reservation validation are implemented.
The actual pinned headers pass four ARM64/ARM64e compiler layout assertions;
both use Version 2 / Revision 2 but have different sizes. Synthetic malformed
inputs and PTYs test software behavior only. The
[physical evidence validator](../scripts/platform/physical_evidence.py)
binds artifacts, source inputs and captures for offline review. It always
leaves hardware attestation and Gate 2 false. A separate
[partial early diagnostic hook](platform/m1-early-diagnostics.md) now compiles
as a real T8103 ARM64e kernel object and reads the live PC, kernel UUID and
architectural MMU/timer registers through the existing `kprintf` path. Its
13 tests exercise the pinned boot-argument parser and nonce validation. It
does not implement the full physical checkpoint protocol or establish a
functioning native UART, delivered interrupts, timer callbacks or SMP.

The repository, runtime and handoff suites pass 146 tests. Four source-bound
ARM64/ARM64e ABI compile assertions also pass. Those checks establish software
behavior and compilation; physical boot acceptance remains unverified.

## Follow-up implementation wave (2026-10-10/11)

A second wave of parallel agents closed the remaining *source-level* P0 gaps
identified above, landing five new patches (0012–0016) plus the full physical
checkpoint protocol emitter (0017) in [patches/native/](../../patches/native/series).
The 17-patch series builds clean: kernel `kernel.development.t8103` (ARM64e
KER00, UUID `CD37C6C4-1CBD-3122-B636-C3C3B311CD64`,
[receipt](../../evidence/native/kernel/puredarwin-t8103-native-17/result.json))
and platform extension (UUID `B43BA1D4-4251-3CE9-B1FA-FDEF70D2F32D`,
[receipt](../../evidence/native/platform-17/result.json)):

- [#35 kernel side](audits/native-cpu-registration.md) — `cpu_start` now
  propagates the real `PE_cpu_start` result (with CTRR cluster-claim rollback
  on failure), and T8103 gains the CPU-count finalization path so
  `ml_wait_max_cpus` cannot wait indefinitely.
- [#35 platform side](audits/native-cpu-startup.md) — new `PDAppleARMCPU`
  personality with real `ml_processor_register`, documented H13 reset/power
  secondary startup, implemented quiesce/halt, and the ordinary `IOCPU::start`
  boot path; runtime CPU count sizing instead of the compile-time H13 maximum.
- [#30](audits/native-handoff-validation.md) — `PE_init_platform` propagates
  `pe_arm_init_interrupts` failure (call sites panic on failure), legacy
  property reads check size/status, and SoC register-window computation is
  overflow- and containment-checked via `pe_arm_soc_reg_range`.
- [#33](audits/native-timer-discovery.md) — the `device_type=timer` node is
  validated and, when absent/malformed on arm64, falls back cleanly to the
  architectural timer; unknown-source FIQs are accounted boundedly.
- [#34](audits/native-aic-hardening.md) — the AIC driver validates full
  three-cell `ranges`/overflow/span containment, registers the per-phandle
  `IOInterruptController%08X` name, bounds its dispatch event loop, and
  propagates initialization failures.
- [#29 software side](audits/native-checkpoint-protocol.md) — the full opt-in
  `BLAUOS_PHYSICAL` checkpoint protocol emitter with the contract's symbol
  labels (8-byte region) and exactly one provisioned `BLAUOS_PHYSICAL_BUILD_ID`
  marker; the offline validator accepts a synthetic format-agreement bundle at
  entry and checkpoint levels with all physical flags still false.

All of this is compile/link/format evidence only: no byte has run on or been
captured from a physical M1, and every hardware acceptance row below is
unchanged. The repository, runtime and handoff suites now pass 187 tests plus
the source-bound ABI assertions.

## Task status

| Issue | Delivered work | Acceptance still required |
| --- | --- | --- |
| #25 M1 lab | [Inventory and recovery/UART runbook](platform/m1-lab.md), actual wrong-host rejection | Dedicated target access, complete hardware inventory, assigned operators, verified backup/Recovery/known-good boot and capture |
| #26 Native policy | [Apple/Asahi policy research and exact host-tool observations](platform/native-boot.md) | Tested target OS/firmware/recovery/tool matrix and reversal |
| #27 Native boot assets | [Fresh source-built T8103 kernel, symbols and conservative Mach-O inspection](audits/native-kernel-build.md) | Target-compatible BootKC, complete driver/firmware inventory, actual package/signing/authorization evidence |
| #28 Path A boot | [Experiment and artifact/session contract](platform/native-boot.md) | Physical authorization attempt, actual kernel entry or a precise observed target blocker, recovery result |
| #29 UART | Bounded raw-byte collection, [compiled partial early diagnostics](platform/m1-early-diagnostics.md), [full opt-in checkpoint protocol emitter with build-ID marker and validator format agreement](audits/native-checkpoint-protocol.md) | Working native polling UART and source identity/checkpoint/panic output from independent physical XNU |
| #30 Discovery and memory | [Explicit ABI, bounded Apple ADT and reservation parser](platform/m1-handoff.md), [init failure propagation and validated SoC register-window computation](audits/native-handoff-validation.md) | Captured M1 handoff/RAM/carveouts and essential device resources; safe native initialization |
| #31 MMU | Reviewed public PPL/CTRR/PAC restoration, actual native objects/link and offline range checks | Actual entry EL/granule/registers, mappings, permissions, cache/TLB transition and physical diagnostics |
| #32 Exceptions | Compiled native vectors, protected entry/return and authenticated saved-state paths | Real controlled exception, ESR/FAR/registers/symbolication and defined outcome |
| #33 Timers | Source-bound architectural timer/FIQ audit, compiled register diagnostics, [validated timer-node discovery with clean architectural fallback and bounded unknown-FIQ accounting](audits/native-timer-discovery.md) | Actual one-shot/periodic delivery, monotonicity, drift, IRQ/FIQ routing and CPU behavior |
| #34 AIC | [Reviewed IRQ re-arm fix and actual ARM64e KER00 platform extension build](audits/native-aic.md), [validated ranges/span checks, per-phandle controller naming, bounded dispatch and failure propagation](audits/native-aic-hardening.md) | Discovery against the real target ADT, routing/affinity/IPI and real normal/spurious/storm tests |
| #35 CPU startup | [Kernel cpu_start failure propagation and T8103 count finalization](audits/native-cpu-registration.md), [platform PDAppleARMCPU registration/reset-handoff startup](audits/native-cpu-startup.md) | Real secondary startup, per-CPU state, IPIs/scheduling/synchronization and repeated boots |
| #36 m1n1 design | Fallback remains conditional | Activate only for an observed native-path blocker or concrete debugging need, then evaluate pin/build/state/translation/licensing |
| #37 Translation layer | Not activated | Reviewed fallback implementation and actual compatible XNU entry when required |
| #38 Fallback boot | Not activated | Actual fallback checkpoint, firmware matrix and recovery when required |
| #39 Repeatable checkpoint | [Offline evidence and cold/warm capture contract](platform/m1-checkpoint.md) | Repeated physical independent-kernel checkpoints and measured remaining gaps |
| #40 Gate 2 | [NOT RUN decision record](../evidence/gate-2.json) | Named review of actual independent physical execution, checkpoint and measured reconstruction scope; explicit PASS/GO |
| #4 Phase 1 | Concrete preparation, source experiments and documented blockers | Mandatory children accepted, conditional fallback resolved, Gate 2 PASS |

## Resume the physical experiment

Select the dedicated M1 target and obtain usable SSH/local access plus a
physical recovery/capture operator. Complete the lab verifications; the
remaining platform initialization, CPU-registration, timer-discovery, AIC
validation and checkpoint-emission interfaces are now implemented in source
(patches 0012–0017, see the follow-up wave above), so the next step is to
prepare target-compatible boot assets from the reviewed native candidate.
Then attempt Path A in the target's paired recoveryOS and capture execution
separately from boot-object authorization. The
[lab](platform/m1-lab.md), [native boot](platform/native-boot.md),
[handoff](platform/m1-handoff.md) and [checkpoint](platform/m1-checkpoint.md)
records define that work. Missing access and offline tool success do not
constitute a failed physical boot experiment or a passing investment gate.
