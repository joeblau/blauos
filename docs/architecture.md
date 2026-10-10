# Repository and source architecture

This repository contains integration tooling, platform modules, independently
reviewable patches, evidence, and documentation. It pins external source trees
instead of importing kernel and runtime forks into the project history.

| Path | Purpose | Review boundary |
| --- | --- | --- |
| `sources.lock.json` | Exact source commits and source roles | Build/source maintainers |
| `toolchain.lock.json` | Recorded host tools and licensed SDK requirements | Build maintainers |
| `manifests/` | Upstream dependency locks copied at pinned revisions | Build/source maintainers |
| `scripts/blauos.py` | Input validation, source fetching, provenance | Build maintainers |
| `scripts/build/` | Kernel, headers, runtime, and launchd experiments | Relevant kernel/runtime owner |
| `scripts/vm/`, `vm/` | Virtual image, boot, and networking validation | Virtual-platform owner |
| `scripts/platform/` | Read-only lab inventory, capture and native evidence inspection | Platform owner and evidence reviewer |
| `platform/apple-m1/` | Source-bound offline handoff parser and future isolated M1 drivers | Platform/driver maintainers |
| `patches/xnu/` | Ordered, separately reviewed generic/platform patches | Kernel/platform maintainers |
| `patches/native/` | Separate experimental native source-build compatibility patches | Kernel/platform maintainers |
| `tests/` | Meaningful negative-path and integration tests | Implementation owner and reviewer |
| `evidence/` | Small reproducible experiment logs and results | Reviewer of the corresponding issue |
| `.blauos/` | Ignored downloads, SDK overlays, worktrees, and artifacts | Local generated state |

Ownership describes engineering boundaries, not automatic assignment to a
person or an invented team. Named engineers claim GitHub issues when work starts.

## Source boundaries

`upstream-modern` is an audit/build experiment against Apple's coherent published
distribution. `puredarwin-vm` is a candidate standalone virtual baseline. These
are distinct source profiles: combining a recent XNU with old userspace without
an ABI/build experiment is not a supported baseline.

PureDarwin's source remains external. Its accumulated changes and embedded
third-party trees must be audited; using its commit does not imply that the
project's own XNU changes are minimal or suitable for native M1 boot.

## Source refresh

1. Choose an upstream distribution/tag or a complete PureDarwin commit; inspect
   the dependency and license changes before changing the lock.
2. Resolve tags to commits and record exact tree identities and dependency locks.
3. Fetch into a new ignored workspace and validate the inputs. Preserve an
   existing modified checkout; the fetcher does not reset it automatically.
4. Reapply the ordered XNU patch series in a separate worktree and inspect the
   resulting diff. Generic fixes and M1-specific modules are separate reviews.
5. Run actual clean build and guest/native tests for the affected profile. Update
   evidence, compatibility limits, and the gate decision only after the run.

See [kernel patch policy](kernel-patches.md) and [build inputs](build.md).
