# PureDarwin reuse and ARM64 audit

Audit date: 2026-10-10. This audit selects source candidates for the ARM64
virtual prototype. Compilation is recorded separately from execution, and a
virtual prototype does not establish M1 hardware support.

The audited sources are immutable entries in `sources.lock.json`:

| Source | Revision | Decision |
| --- | --- | --- |
| PureDarwin main | `88753c478c97b9a08bcdb66cecc68ba5881ff3af` | Retain for historical reference; its CMake architecture is x86_64 and its libc target supplies headers, not a modern independent ARM64 runtime. |
| PureDarwin next | `1bbaeb2ee6aa41e6f792a0b1e2943cc295b3b290` | Prototype candidate: source ARM64 XNU, dyld, libSystem, launchd and shell, with a QEMU virt platform. Validate each component below. |
| PureDarwin XPC | `ad1163a623528ec83af7ec3c1d28ee0700bdef58` | Reject as the runtime foundation. The repository is archived, its README declares incomplete functionality, and required paths contain unimplemented operations. |
| LLVM compiler-rt | `2078da43e25a4623cab2d0d60decddf709aaea28` | Build real ARM64 compiler builtins from source; keep the symbol visibility overlay reviewable. |

The [current upstream status](https://puredarwin.org/handbook/status.html)
describes an ARM64 QEMU prototype. Its claims are discovery evidence, not our
acceptance evidence. The vendored component Git tree identifiers are recorded
in `evidence/runtime/source-trees.json`; they prevent a component name or
version label from hiding a different source snapshot.

| Component in next | ARM64 and dependency finding | Reuse decision and verification needed |
| --- | --- | --- |
| dyld (`src/Libraries/dyld`) | Apple dyld source plus PureDarwin loader adaptations; actual ARM64 loader compiles under the installed SDK experiment. SDK 27 libc++ removed the internal vector base used by older source. | Keep the narrow vector specialization/include overlay; verify source libSystem initialization and every loaded image in the guest. Abort paths for Block operations and single-thread-only loader dispatch shims do not establish general runtime support. |
| libSystem (`src/Libraries/libSystem`) | Aggregates source libc, Mach/BSD syscall wrappers, libplatform, pthread, malloc, dispatch, math, resolver, crypto and compiler builtins; reexports source libdyld. Several actual ARM64 archives compile. | Keep as the minimum candidate, require the complete linked dylib and dependency closure. Replace its return-only stack probe with the tested ARM64 page-touch helper. Do not treat placeholder APIs as functional merely because they link. |
| launchd and launchctl (`src/Libraries/XPC/launchd`) | Source job manager and Mach IPC/MIG interfaces exist. The implementation is the older launchd 842.91.1 lineage, with compatibility files. | Use the real implementation; validate PID 1, RunAtLoad, MachServices, KeepAlive, load/unload, failures and shutdown with guest fixtures. Never substitute a small PID 1 program. |
| libxpc/libinfo/notify (`src/Libraries/XPC`) | Source ARM64 targets exist. notify needs generated MIG declarations; libinfo mixes old resolver headers with the modern SDK unless the matching source header is selected. | Review compatibility patches and test the required service subset. `xpc_create_from_plist` and sandbox enforcement remain excluded, not claimed as supported. |
| libc++/libc++abi/libunwind | Source archives build for ARM64. Modern SDK C++ headers must be explicit for the old dyld build. | Include source archive provenance and test loading/unwinding before declaring broad C++ support. |
| IOKit and DiskArbitration | Source trees exist, but presence does not implement M-series hardware drivers. | Defer beyond runtime baseline; require separate driver tests. |
| corecrypto/CommonCrypto | Source crypto links into the candidate runtime; corecrypto is GPL version 3 or later while CommonCrypto carries Apple notices. | Preserve file notices; audit the linked distribution's obligations and license compatibility before release. No license clearance is implied by this prototype. |

The [archived XPC repository](https://github.com/PureDarwin/XPC) was verified
through GitHub's repository metadata (`archived: true`, captured in
`evidence/runtime/archived-xpc-repository.json`). Its event-handler setter does
not retain/install the handler, endpoint creation returns NULL, transactions
are no-ops, and plist conversion aborts. An actual Xcode build also fails on
missing `darwinbuild.xcconfig` and an unsupported deployment target; the
compact failure logs are in `evidence/runtime/archived-xpc-*.log`.

Licenses must be evaluated per file and linked artifact. Representative
notices include Apple APSL 2.0 (`APPLE_LICENSE` in Apple-derived components),
BSD/ISC notices in libc and resolver sources, LLVM's Apache 2.0 with LLVM
exceptions, PureDarwin notices in its added files, and GPLv3-or-later in
`src/Libraries/libSystem/corecrypto/cc.c` and `corecrypto/LICENSE`. The
[upstream licensing guide](https://puredarwin.org/handbook/licensing.html)
is an orientation aid, not a single license for this aggregate. A release
needs a file-level inventory, retained notices, corresponding-source review
and review of the aggregate license conditions. SDK availability does not
authorize redistributing Apple's SDK or runtime binaries.

The pinned Nix path requires a locally supplied proprietary
`MacOSX11.3.sdk.tar.xz` with SHA-256
`9adc1373d3879e1973d28ad9f17c9051b02931674a3ec2a2498128989ece2cb1`.
The installed SDK 27.2 experiment is a separate build profile, with its SDK
settings, compiler identity and overlays recorded. Neither profile copies a
stock macOS dyld or libSystem into the candidate root filesystem. Firmware,
Apple boot policy and hardware drivers are separate dependencies and remain
outside this reuse audit.

Replay commands and current build/execution evidence are in
[runtime-build.md](runtime-build.md). The precise launchd service subset is
machine-readable in `runtime/service-subset.json`.
