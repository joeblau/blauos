# Milestone 1 evidence

Milestone 1 is **Phase 0 — Reproducible baseline**, tracked by issue #3.
Review date: 2026-10-10. Technical reviewer: Codex, acting on the project's
request to implement and validate the milestone. Joe Blau owns go/no-go
decisions under the [release contract](scope.md).

The source-built DEVELOPMENT ARM64 VIRT kernel has compiled from two separate
clean checkouts and executed under QEMU. Neither result establishes a usable
operating system: the guest currently stops before PID 1, and independent
runtime execution remains unverified.

| Issue | Evidence | Acceptance status |
| --- | --- | --- |
| #8 Scope contract | [scope.md](scope.md) | Defined hardware, exclusions, gates and decision ownership |
| #9 Dependency audit | [source-dependencies.md](audits/source-dependencies.md) | Dependency graph, input classes and observed blockers recorded |
| #10 Reuse audit | [puredarwin-reuse.md](audits/puredarwin-reuse.md) | Component reuse decisions, real compatibility experiments and license obligations recorded |
| #11 Repository architecture | [architecture.md](architecture.md), `scripts/blauos.py` | Pinned fetch workflow implemented and exercised |
| #12 Patch isolation | [kernel-patches.md](kernel-patches.md), [checks](../evidence/host/patch-series.json) | Clean series checks and separate clean candidate-kernel builds recorded |
| #13 Input pins | [sources.lock.json](../sources.lock.json), [toolchain.lock.json](../toolchain.lock.json), [fresh fetch](../evidence/host/input-reconstruction.json) | Inputs pinned and independently retrieved; licensed/unavailable inputs disclosed |
| #14 Host bootstrap | [preflight](../evidence/host/preflight.json), [bootstrap](../evidence/host/bootstrap.json) | Isolated workspace bootstrap and exact tool/SDK preflight passed |
| #15 Generated inputs | [header comparison](../evidence/kernel/header-reproducibility.json), [audit](audits/source-dependencies.md) | Clean regeneration passed; remaining modern private-input blockers documented |
| #16 ARM64 kernel build | [kernel-build.md](audits/kernel-build.md), [two-build comparison](../evidence/kernel/reproducibility/comparison.json) | Real DEVELOPMENT kernel and symbols built twice; byte differences disclosed |
| #17 Independent runtime | [runtime-build.md](audits/runtime-build.md) | In progress; complete linked dependency closure required |
| #18 Independent executable | [host control](../evidence/runtime/host-probe.json), `runtime/probes/runtime_probe.c` | Open; host control does not satisfy independent execution |
| #19 VM execution experiment | [virtualization.md](virtualization.md), [kernel checkpoint](../evidence/virtualization/kernel-checkpoint/result.json) | Real source-built guest kernel execution observed |
| #20 launchd and IPC | `runtime/fixtures/launchd-test.sh`, `runtime/service-subset.json` | Open; source-built guest functional checks required |
| #21 Shell/PID 1 image | `scripts/vm/build.py`, `vm/` | Open; assembly and actual userspace boot required |
| #22 VM networking | `vm/guest_probe.c`, `scripts/vm/smoke.py` | Open; actual DNS, time and bidirectional service proof required |
| #23 Build/boot CI | `.github/workflows/baseline.yml`, `scripts/vm/ci.py` | Open; full source-build and real guest boot run required |
| #24 Gate 1 | Runtime, executable and clean-build evidence above | **FAIL/NO-GO at this checkpoint**; minimum runtime execution has not passed |

Source-build repeatability and identical output bytes are separate findings.
The two kernel builds have different timestamps, UUIDs and embedded absolute
paths. The header-generation experiment produced identical output. Release
reproducibility requires controlling the remaining kernel/runtime/image inputs
and comparing complete releases; it has not been established here.

The modern Apple profile still requires matching security interfaces and ARM64
KDK support. The unmodified PureDarwin Nix profile requires its exact local
SDK 11.3 archive. The tested SDK 27.2 adaptation has a separate input identity
and reviewed compatibility changes. These observations do not count as passes
for the independent-runtime or physical-boot gates.

Keep #3 and #24 open until the mandatory runtime, shell, service, networking and
automated guest tests pass. Update this record with the actual passing evidence
before changing their status.
