# Milestone 1 evidence

Milestone 1 is **Phase 0 — Reproducible baseline**, tracked by issue #3.
Review date: 2026-10-10. Technical reviewer: Codex, acting on the project's
request to implement and validate the milestone. Joe Blau owns go/no-go
decisions under the [release contract](scope.md).

The source-built DEVELOPMENT ARM64 VIRT kernel and independent userspace now
pass the [complete virtual guest test](../evidence/virtualization/pid1-checkpoint/result.json).
The guest mounts a persistent GPT/ext4 root, runs source launchd as PID 1,
executes source dash and the complete runtime probe, supervises Mach service
jobs, configures Ethernet/DNS/time, serves traffic in both directions, and
shuts down through the reviewed QEMU PSCI hook. All required fresh-nonce
markers and the authentic guest shutdown event were observed.

The [runtime execution record](../evidence/runtime/guest-runtime-v11.json) and
[source-runtime audit](../evidence/runtime/source-runtime-closure.json) identify
actual target artifacts, complete source dependency closure and provider routes.
The earlier interrupted runtime pass and failed boot/core diagnostics remain
separate evidence; they are not substituted for the complete passing guest.

The earlier [fresh source-build experiment](../evidence/ci/native-run-2/result.json)
compiled XNU, all 13 essential extensions, runtime, launchd, shell and commands,
but failed during guest initialization. Real cores and logs led to the reviewed
loader, syscall, locale, provider and initialization corrections. The corrected
[fresh GitHub source build and complete guest replay](https://github.com/joeblau/blauos/actions/runs/38074322304)
now passes at clean revision `bf6d081f6eb2348d7d3d5b80a6b80899f5518695`. All 24
pipeline stages and 20 fresh-nonce guest checks pass. The uploaded archive was
downloaded and verified against its SHA-256; it contains full audits, manifests,
symbols, serial output and results. [Tracked CI evidence](../evidence/ci/github-native-38074322304/result.json)
preserves actual receipts and compact audits with exact raw hashes.

| Issue | Evidence | Acceptance status |
| --- | --- | --- |
| #8 Scope contract | [scope.md](scope.md) | Defined hardware, exclusions, gates and decision ownership |
| #9 Dependency audit | [source-dependencies.md](audits/source-dependencies.md) | Dependency graph, input classes and observed blockers recorded |
| #10 Reuse audit | [puredarwin-reuse.md](audits/puredarwin-reuse.md) | Component reuse decisions, real compatibility experiments and license obligations recorded |
| #11 Repository architecture | [architecture.md](architecture.md), `scripts/blauos.py` | Pinned fetch workflow implemented and exercised |
| #12 Patch isolation | [kernel-patches.md](kernel-patches.md), [checks](../evidence/host/patch-series.json) | Clean series checks and separate clean candidate-kernel builds recorded |
| #13 Input pins | [sources.lock.json](../sources.lock.json), [toolchain.lock.json](../toolchain.lock.json), [fresh fetch](../evidence/host/input-reconstruction-current.json), [archive fetch](../evidence/host/archive-reconstruction.json) | 18 Git inputs and the ICU source archive pinned and independently retrieved; licensed/unavailable inputs disclosed |
| #14 Host bootstrap | [preflight](../evidence/host/preflight.json), [bootstrap](../evidence/host/bootstrap.json) | Isolated workspace bootstrap and exact tool/SDK preflight passed |
| #15 Generated inputs | [header comparison](../evidence/kernel/header-reproducibility.json), [audit](audits/source-dependencies.md) | Clean regeneration passed; remaining modern private-input blockers documented |
| #16 ARM64 kernel build | [kernel-build.md](audits/kernel-build.md), [two-build comparison](../evidence/kernel/reproducibility/comparison.json) | Real DEVELOPMENT kernel and symbols built twice; byte differences disclosed |
| #17 Independent runtime | [runtime audit](audits/runtime-build.md), [closure](../evidence/runtime/source-runtime-closure.json), [guest proof](../evidence/runtime/guest-runtime-v11.json) | Source builds, complete closure and real independent runtime execution pass; shared cache absent |
| #18 Independent executable | [guest execution](../evidence/runtime/guest-runtime-v11.json), `runtime/probes/runtime_probe.c` | Allocation, file I/O, syscalls, threads, once, Blocks, timers, fork callbacks, loader and process exit pass in the source XNU guest |
| #19 VM execution experiment | [virtualization.md](virtualization.md), [kernel checkpoint](../evidence/virtualization/kernel-checkpoint/result.json) | Real source-built guest kernel execution observed |
| #20 launchd and IPC | `runtime/service-subset.json`, [guest log](../evidence/virtualization/pid1-checkpoint/serial.log) | Job launch, Mach registration/IPC, KeepAlive restart, reaping, load/unload, errors and complete guest shutdown pass |
| #21 Shell/PID 1 image | `scripts/vm/native_ci.py`, [actual boot](../evidence/virtualization/pid1-checkpoint/result.json) | Source PID 1, dash, utilities and services execute from the persistent root image |
| #22 VM networking | `vm/guest_probe.c`, [guest log](../evidence/virtualization/pid1-checkpoint/serial.log) | Real BSD en0 registration, address/route/DNS/time, guest-to-host challenge and host-to-guest service pass |
| #23 Build/boot CI | `.github/workflows/native-baseline.yml`, `scripts/vm/native_ci.py` | All 24 fresh pipeline stages and complete guest replay pass; archive download/digest/content verified |
| #24 Gate 1 | Runtime, executable and clean-build evidence above | **PASS/GO**, technical reviewer Codex; [named decision and limitations](../evidence/gate-1.json) |

Source-build repeatability and identical output bytes are separate findings.
The two kernel builds have different timestamps, UUIDs and embedded absolute
paths. The header-generation experiment produced identical output. Release
reproducibility requires controlling the remaining kernel/runtime/image inputs
and comparing complete releases; it has not been established here.

The modern Apple profile still requires matching security interfaces and ARM64
KDK support. The unmodified PureDarwin Nix profile requires its exact local
SDK 11.3 archive. The tested SDK 27.2 adaptation has a separate input identity
and reviewed compatibility changes. The passing independent runtime applies
to that tested profile. Virtual execution does not establish physical M1 boot.

All 18 milestone issues meet their scoped acceptance criteria. Codex records
**PASS/GO for Gate 1 technical acceptance** in the [decision record](../evidence/gate-1.json).
The investment decision owner remains Joe Blau; no human approval or physical
M1 boot is implied. The next work is Phase 1 native kernel bring-up.
