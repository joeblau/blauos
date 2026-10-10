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
profile reaches ARM64e compilation but has not linked a kernel. The
[native source/build audit](audits/native-kernel-build.md) and
[raw experiment receipts](../evidence/native/kernel/) record actual commands,
tool/SDK identities, patch hashes, failures and artifact counts. No VIRT
kernel was relabeled or packaged as a native candidate.

Read-only lab inventory, bounded UART collection, explicit-source boot-argument
decoding, Apple device-tree parsing and reservation validation are implemented.
The actual pinned headers pass four ARM64/ARM64e compiler layout assertions;
both use Version 2 / Revision 2 but have different sizes. Synthetic malformed
inputs and PTYs test software behavior only. The
[physical evidence validator](../scripts/platform/physical_evidence.py)
binds artifacts, source inputs and captures for offline review. It always
leaves hardware attestation and Gate 2 false. The proposed native kernel
checkpoint emitter is still outstanding.

## Task status

| Issue | Delivered work | Acceptance still required |
| --- | --- | --- |
| #25 M1 lab | [Inventory and recovery/UART runbook](platform/m1-lab.md), actual wrong-host rejection | Dedicated target access, complete hardware inventory, assigned operators, verified backup/Recovery/known-good boot and capture |
| #26 Native policy | [Apple/Asahi policy research and exact host-tool observations](platform/native-boot.md) | Tested target OS/firmware/recovery/tool matrix and reversal |
| #27 Native boot assets | [T8103 build experiments and conservative Mach-O inspection](audits/native-kernel-build.md) | Genuine linked native kernel, complete driver/firmware inventory, actual package/signing/authorization evidence |
| #28 Path A boot | [Experiment and artifact/session contract](platform/native-boot.md) | Physical authorization attempt, actual kernel entry or a precise observed target blocker, recovery result |
| #29 UART | Bounded raw-byte collection and [proposed source checkpoint protocol](platform/m1-checkpoint.md) | Integrated native early logging, source identity/checkpoint/panic output from independent XNU |
| #30 Discovery and memory | [Explicit ABI, bounded Apple ADT and reservation parser](platform/m1-handoff.md) | Captured M1 handoff/RAM/carveouts and essential device resources; safe native initialization |
| #31 MMU | Source layout/register assumptions and offline range checks | Actual entry EL/granule/registers, mappings, permissions, cache/TLB transition and physical diagnostics |
| #32 Exceptions | Source startup observations and evidence fields | Real controlled exception, ESR/FAR/registers/symbolication and defined outcome |
| #33 Timers | Evidence contract distinguishes counter progress and delivered callback | Actual one-shot/periodic delivery, monotonicity, drift, IRQ/FIQ routing and CPU behavior |
| #34 AIC | Existing AppleARM/AIC dispatch identified and source limitations recorded | Native discovery, masks/acknowledgment/routing/affinity/IPI and real normal/spurious/storm tests |
| #35 CPU startup | Existing platform CPU startup gap identified | Supported P/E secondary startup, per-CPU state, IPIs/scheduling/synchronization and repeated boots |
| #36 m1n1 design | Fallback remains conditional | Activate only for an observed native-path blocker or concrete debugging need, then evaluate pin/build/state/translation/licensing |
| #37 Translation layer | Not activated | Reviewed fallback implementation and actual compatible XNU entry when required |
| #38 Fallback boot | Not activated | Actual fallback checkpoint, firmware matrix and recovery when required |
| #39 Repeatable checkpoint | [Offline evidence and cold/warm capture contract](platform/m1-checkpoint.md) | Repeated physical independent-kernel checkpoints and measured remaining gaps |
| #40 Gate 2 | [NOT RUN decision record](../evidence/gate-2.json) | Named review of actual independent physical execution, checkpoint and measured reconstruction scope; explicit PASS/GO |
| #4 Phase 1 | Concrete preparation, source experiments and documented blockers | Mandatory children accepted, conditional fallback resolved, Gate 2 PASS |

## Resume the physical experiment

Select the dedicated M1 target and obtain usable SSH/local access plus a
physical recovery/capture operator. Complete the lab verifications, resolve
the recorded native source-build failures and inspect a genuine candidate.
Then attempt Path A in the target's paired recoveryOS and capture execution
separately from boot-object authorization. The
[lab](platform/m1-lab.md), [native boot](platform/native-boot.md),
[handoff](platform/m1-handoff.md) and [checkpoint](platform/m1-checkpoint.md)
records define that work. Missing access and offline tool success do not
constitute a failed physical boot experiment or a passing investment gate.
