# ARM64 runtime build and execution evidence

The current experiment builds pinned PureDarwin next sources with the local
Xcode 27.2 beta SDK. It is an alternative feasibility profile, distinct from
upstream's pinned SDK 11.3 Nix build. Read `evidence/runtime/build.json` for
the latest per-target commands, exit codes, input hashes and artifact hashes.
The script exits nonzero while the linked runtime closure or independent
execution remains unverified; compiled archives alone do not satisfy #17.

Fetch the pinned inputs with the repository tooling, then replay:

```sh
python3 scripts/blauos.py fetch puredarwin-vm-next
python3 scripts/blauos.py fetch llvm-project
python3 scripts/build/runtime.py build --output evidence/runtime/build.json
python3 -m unittest discover -s runtime/tests -v
```

The build accepts `--sources-dir`, `--output-dir`, `--sdk`, `--manifest` and
`--jobs`. Primary source checkouts must be clean and match the lock. Changes
are applied to a separate worktree under `.blauos/build/runtime`; SDK C++
headers are copied only into this ignored build workspace. Source archives
and raw logs also remain there. Compact experimental logs retain the raw
log SHA-256, byte count and location in
`evidence/runtime/experiment-log-index.json`.

The reviewed compatibility inputs are:

* `runtime/compat/sdk27_compile.h`: compile-time annotation compatibility
  for old source headers and the new SDK. It supplies no runtime functions.
* `runtime/compat/sdk27-dyld.patch`: uses current libc++ vector specialization,
  explicitly selects the SDK C++ headers, selects the matching source DNS
  header, orders generated notify MIG declarations, and integrates the real
  ARM64 stack helper.
* `runtime/compat/compiler-rt-exports.patch`: changes visibility options for
  the source-built compiler-rt archive, as required by exported libSystem
  builtins. No downloaded prebuilt compiler runtime is substituted.
* `runtime/compat/arm64_stack_probe.S`: probes every 4 KiB and the final
  partial allocation below the caller's stack pointer, preserving scratch
  registers and condition flags. The size-in-x9 ABI is captured from the
  selected compiler's actual assembly. This replaces PureDarwin's return-only
  helper and preserves stack guard faults; it is not a successful no-op.

The runtime-only configuration does not configure the kernel header installer.
The script generates the source libc headers and connects the build's
expected XNU include location to that generated header stage. This provides
the matching old syscall definitions rather than silently using removed or
renumbered SDK syscall constants.

Real compilation has produced ARM64 source archives for libc, internal libc,
Mach/BSD syscalls, libplatform, pthread, math, corecrypto, libc++, libc++abi,
libunwind, and LLVM compiler-rt, plus the source dyld loader and launchd MIG
archive. The complete libSystem and launchd link/build results remain the
authority in `build.json`. Newly exposed incompatibilities are fixed in the
reviewed overlay, then the same script is replayed. The target filesystem,
guest launchd tests, and source-runtime execution remain required checks.

The probe sources are real, independently inspectable programs:

* `runtime/probes/runtime_probe.c` verifies allocation/reallocation, writable
  file I/O and fsync, mmap, pthread creation/join, fork/wait/exit status, and
  enumerates the loaded dyld images.
* `runtime/probes/bootstrap_probe.c` checks in a launchd MachService, performs
  a bounded request/reply roundtrip, then exits with status 17 so KeepAlive
  supervision can be observed by a new server PID.
* `runtime/probes/stack_probe_test.c` exercises compiler-generated ARM64
  dynamic frames, page-boundary sizes and a guard-page fault in a child. The
  actual passing output is in `evidence/runtime/stack-probe-test.log`.

The host control experiment is reproducible with:

```sh
python3 scripts/build/runtime.py host-probe --output evidence/runtime/host-probe.json
xcrun clang -arch arm64 -O2 -Wall -Wextra -Werror \
  runtime/probes/stack_probe_test.c runtime/compat/arm64_stack_probe.S \
  -o .blauos/build/runtime/host-probe/stack-probe-test
.blauos/build/runtime/host-probe/stack-probe-test
```

The host runtime probe passes, but it uses the stock host kernel, dyld,
libSystem and shared cache. `host-probe.json` explicitly records
`meets_issue_18_acceptance: false`. It validates the probe, not independent
Darwin userspace. Do not run the bootstrap server or the launchd job fixtures
against the host launchd.

Audit a staged guest root before boot:

```sh
python3 scripts/build/runtime.py inspect --root PATH_TO_ROOT \
  --provenance PATH_TO_PROVENANCE_JSON --output PATH_TO_AUDIT_JSON
```

The inspector parses bounded ARM64 Mach-O load commands without invoking a
host loader. It verifies `/usr/lib/dyld`, `/usr/lib/libSystem.B.dylib`,
`/usr/lib/system/libdyld.dylib`, every Mach-O dependency within the root, and
every artifact's SHA-256 against source provenance. Missing files, escaped
symlinks, unsupported architectures, malformed commands and unpinned sources
fail closed. Context-dependent `@rpath` and `@executable_path` require a later
explicit resolution design. An offline closure pass sets
`execution_verified: false`; it does not prove the guest avoided a host cache.

Provenance uses `schema_version: 1` and an `artifacts` array. External source
entries carry `path`, `sha256`, `source` (a lock name) and `revision`. Locally
built probes carry `source: "blauos"`, exact `source_files` path/hash pairs,
and the canonical `workspace-sha256:` revision generated by
`local_source_revision()`. Optional `workspace_revision` identifies the Git
baseline, and `--project-root` selects the files whose contents are verified.
This avoids attributing local probe code to PureDarwin. Patched runtime
artifacts additionally require recorded overlay/helper hashes; source identity
alone must not conceal build amendments.

The guest test in `runtime/fixtures/launchd-test.sh` is guarded by
`BLAUOS_GUEST_TESTING=1`. It validates real job load, Mach bootstrap roundtrip,
KeepAlive restart, unload and missing-plist errors. Clean PID 1 shutdown and
failed-executable reporting require their own guest evidence. Until these
checks and independent runtime execution pass, #17, #18 and #20 stay open.
