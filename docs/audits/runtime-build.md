# ARM64 runtime build and execution evidence

The current experiment builds pinned PureDarwin next sources with the local
Xcode 27.2 beta SDK. It is an alternative feasibility profile, distinct from
upstream's pinned SDK 11.3 Nix build. Read `evidence/runtime/build.json` for
the latest per-target commands, exit codes, input hashes and artifact hashes.
The build command exits zero only when the actual source targets and base
staging pass (`source_build_pass`). Separate closure and independent execution
fields stay explicit; compiled archives alone do not satisfy #17. The inspect
command exits nonzero for an incomplete offline closure.

The genuine ARM64 guest now passes the complete runtime, launchd service,
network and clean shutdown tests. It runs the source shell and verifies
allocation, locale conversion, file I/O, syscalls, threads, synchronized once,
Blocks lifetime, Mach timers, atfork callbacks, process exit and dynamic
loading. It identifies the independently built XNU 20.5.0 kernel, verifies the
loaded source System/libdyld/ObjC images and rejects a dyld shared cache.
Launchd loads a real MachService, restarts its failed server with a different
PID, reaps the original child, unloads jobs and reports missing job/executable
errors. DNS, both network directions and shutdown pass with a fresh host
challenge. The shutdown job terminates and reaps its child; the host observes
a genuine QMP guest shutdown. Captured source hashes and all observed output
are in `evidence/runtime/guest-runtime-v11.json`. A complete fresh GitHub
native CI replay remains required.

The earlier runtime-only success remains in `guest-runtime-v8.json`. That
experiment ended when the lab container's original two-hour sleep expired
while the next service check was pending; no guest fault ended it. Tracked
build reports now reference the later post-build validation. A new invocation
of a build script still reports only compilation and requires its own guest
execution evidence.

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
  ARM64 stack helper. It exports the real standard string dispatchers, builds
  the omitted source `llabs`/`lldiv` and segment lookup implementations, and
  uses the ARM64 sigwait spelling. Source objc4 uses `getprogname()` instead of
  importing the obsolete executable-global `__progname` variable.
  A real guest core exposed a pre-rebase `strcmp` callback-table dereference.
  The loader now selects the pinned libplatform static string/memory routines
  before rebasing its globals; weak kernel callbacks allow those genuine
  strong definitions to win. `-fno-builtin` keeps the libplatform loops from
  being lowered back into callback wrappers. Symbol and disassembly evidence
  for 13 bootstrap primitives is in `dyld-bootstrap-primitives.log`.
  After the genuine Objective-C library is built, libSystem is relinked with
  a strong load dependency on that source provider. The initial bootstrap
  System ABI and final dependency graph are recorded separately.
* `runtime/compat/compiler-rt-exports.patch`: changes visibility options for
  the source-built compiler-rt archive, as required by exported libSystem
  builtins. No downloaded prebuilt compiler runtime is substituted.
* `runtime/compat/arm64_stack_probe.S`: probes every 4 KiB and the final
  partial allocation below the caller's stack pointer, preserving scratch
  registers and condition flags. The size-in-x9 ABI is captured from the
  selected compiler's actual assembly. This replaces PureDarwin's return-only
  helper and preserves stack guard faults; it is not a successful no-op.
* `runtime/compat/libdyld.exports`: bounds the source libdyld's exports to
  loader, blocks and C++ ABI APIs. Ordinary libdyld imports the initialized
  source System runtime; the bootstrap dyld executable retains its separate
  early runtime. The links use source providers and the real unwinder, with
  the SDK System framework removed so SDK declarations cannot select runtime
  imports ahead of the source implementation.

The runtime-only configuration does not configure the kernel header installer.
The script generates the source libc headers and connects the build's
expected XNU include location to that generated header stage. This provides
the matching old syscall definitions rather than silently using removed or
renumbered SDK syscall constants.

Real compilation has produced ARM64 source archives for libc, internal libc,
Mach/BSD syscalls, libplatform, pthread, math, corecrypto, libc++, libc++abi,
libunwind, and LLVM compiler-rt, plus linked source dyld, libdyld and libSystem
and launchd/MIG archives. The base libraries stage under the ignored runtime
`root/`; complete command and hash evidence is in `build.json`. Runtime CMake
and the explicit ABI/unwind compiles now target macOS 11.0 instead of defaulting
object deployment metadata to the host SDK version. An offline symbol closure
alone cannot validate initialization ordering or behavior. The captured guest
proves the required runtime, launchd service and shutdown subsets.

The actual Objective-C, full C++, ICU, C/ObjC CoreFoundation, IOKitCF and
launchd/launchctl source compile/link recipes now pass, followed by genuine
independent execution. Replay the libraries in dependency order:

```sh
python3 scripts/build/objc.py
python3 scripts/build/libcxx.py
python3 scripts/blauos.py fetch-archives icu4c-76.1
python3 scripts/build/corefoundation.py --action all
python3 scripts/build/iokit.py
python3 scripts/build/launchd.py
```

These commands show the library dependency order. `libcxx.py` writes its own
additive provenance to `evidence/runtime/libcxx/provenance.json`; a standalone
caller must merge those artifact records into the global runtime provenance
before the closure audit. For a complete reproducible replay, use the native
CI command in [the build guide](../build.md), which performs that merge,
builds the source commands and shell, composes the image, and requires actual
guest execution and shutdown.

The Objective-C recipe compiles the real 33 objc4 Objective-C++ translation
units and three ARM64 assembly sources, builds a real C++ ABI/unwind provider,
and records every command. Disabled DTrace macros remove instrumentation,
not language runtime operations. It uses libc++'s supported restoration macro
for the old `std::binary_function` declaration. The launchd link requires
source CoreFoundation, IOKitCF and the base runtime; missing source libraries
produce an explicit blocked result. The scripts never start host launchd jobs.
Source IOKitCF selects ARM64 MIG, the C/ObjC framework umbrella header and
MacTypes before CoreFoundation, rather than selecting Swift ABI by accident.
Changed framework header contents invalidate old compile objects even when
the installed headers retain old source timestamps. Final native library and
launchd links use `-Z` to forbid default SDK library searches. launchctl links
only required archive members; PID 1 owns the MIG server implementations.

The probe sources are real, independently inspectable programs:

* `runtime/probes/runtime_probe.c` verifies allocation/reallocation, writable
  file I/O and fsync, mmap, pthread creation/join, fork/wait/exit status, and
  enumerates the loaded dyld images. Guest mode `--require-source-images`
  requires the actual System.B, libdyld and ObjC images and rejects a loaded
  dyld shared cache using the source dyld API. The stock-host negative control
  correctly rejects this mode with exit 16; it cannot qualify as independence.
* `runtime/probes/bootstrap_probe.c` checks in a launchd MachService, performs
  a bounded request/reply roundtrip, then exits with status 17 so KeepAlive
  supervision can be observed by a new server PID. Its `--reaped PID` mode
  verifies that an exited child has no remaining kernel process record;
  zombies do not pass that check. The fixture sets the supported one-second
  rapid-exit throttle, and clients wait fifteen seconds. This avoids losing a
  reply port before the normal ten-second launchd respawn throttle ends.
  Console diagnostics retain exact Mach operation and error codes.
* `runtime/probes/shutdown_probe.c` runs as a real launchd job, blocks SIGTERM
  before forking a child, then waits for the shutdown signal. It terminates and
  waits for its child, verifies that the child's process record disappeared,
  and emits nonce-bound termination markers before returning zero.
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
every artifact's SHA-256 against source provenance. It resolves strong chained
imports against the staged dependency's public symbols and reexports. Required
flat namespace imports need explicitly declared staged providers, matching
public symbols and a real strong dependency route that loads the provider.
An environment insertion or manifest declaration alone cannot pass the audit.
Final libSystem loads source `libobjc.A.dylib` through its actual load command.
Missing files, escaped
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
KeepAlive restart, child reaping, unload, missing-plist errors and failed
executable error reporting. The shutdown fixture and VM smoke require the
fresh nonce-bound job start, child reap and job exit markers plus a real
guest-triggered QMP shutdown event. Host process termination cannot satisfy
that criterion. The captured v11 run passes these tests and the real shutdown
witness. The [fresh GitHub replay](https://github.com/joeblau/blauos/actions/runs/38074322304)
now independently rebuilds every component and passes the same runtime, service,
network and shutdown checks with a new nonce. Its full raw audits and symbols are
archived; tracked compact evidence preserves all artifact identities and routes.

Two actual guest cores exposed bootstrap faults before PID 1 could initialize.
The first reached `rebaseDyld` and called a libkernel string callback through
an unrebased pointer table. The reviewed overlay now selects pinned libplatform
string and memory implementations and compiles them without builtin lowering.
The next core advanced to shared-cache discovery and showed infinite recursion
from the compatibility `_open` to `_open$UNIX2003` and back. ARM64's native
open/getrlimit/pthread implementations now supply the plain symbols; the
historical aliases only forward in one direction. The actual linked source
locations and native open syscall call are retained in
`evidence/runtime/dyld-native-aliases.log`.

The third core loaded thirteen actual source library images, then exposed a
libSystem locale-forwarder cycle while parsing the kernel's stack-guard string:
`strtoull` called `strtoull_l`, whose compatibility wrapper called `strtoull`
again. The locale-aware implementations were already present in the pinned
libc archives. Removing all eight conflicting locale compatibility functions
selects those genuine implementations and preserves locale semantics. Their
actual linked source locations are in
`evidence/runtime/libSystem-locale-providers.log`. The runtime probe checks the
captured stack-guard value, signed/unsigned conversion boundaries, overflow,
end pointers, floating conversion, locale formatting and per-thread C locale.
These historical core observations precede the later passing shell/runtime
experiment and explain why native provider selection matters.

The next actual guest reached source launchd's main function. Installing the
genuine `/bin/launchctl -> ../usr/bin/launchctl` convention allowed its System
bootstrap child to fork, exchange a Mach ping and execute source launchctl.
That child then dereferenced libdyld's private, uninitialized `malloc_zones`
storage. Ordinary libdyld now imports the process's initialized malloc,
pthread, libc and Mach providers from source libSystem. Its declaration-only
link interface is generated from the actual selected source archives and
records their hashes; it is absent from the staged root and cannot satisfy
runtime closure. The standalone dyld executable retains its independently
initialized early bootstrap runtime. The native libdyld initializer supplies
recursive loader locks, per-thread error buffers, real launchd ownership RPC
and TLV initialization. The later genuine runtime probe passes allocator
identity, concurrent loader error isolation and the source dylib roundtrip;
source launchctl now bootstraps actual jobs.

The complete compatibility-provider review found 36 genuine archive providers
and six genuine loader reexports shadowed by reduced wrappers. Those shadows
are removed. Mach timers, task/thread/VM clients, atfork registration,
credential queries, locks/atomics, cancellation, filesystem translation,
locale collation and diagnostics now select the pinned implementations.
The independent linked-provider verification is retained in
`evidence/kernel/runtime-provider-review/remediation-verification.json`.
Client atfork callbacks run before internal prepare locks and after internal
parent unlocks; the child resets both System's and dyld's genuine Mach caches
before invoking client callbacks.

The required startup-provider audit also replaced the libkernel pthread-self
no-op with pinned pthread initialization, selected real cancellation handling,
and replaced dyld's aborting Blocks hooks with the complete pinned C Blocks
runtime. Both private dyld once functions call libplatform's synchronized
`_os_once`; ordinary libSystem `dispatch_once_f` comes from libdispatch's
`once.c`. Both libdyld's launchd-ownership check and libSystem's public vproc
API now use the real `libvproc.c` bootstrap RPC implementation. Requested
`sandbox_init` policies fail with `ENOTSUP`, because this guest has no sandbox
kernel policy. `sandbox_check` reports the existing unsandboxed state; it does
not install a policy. `xpc_create_from_plist` remains an explicit unsupported
XPC API and is absent from the required launchd service probe path. The guest
baseline does not establish arbitrary XPC or sandbox compatibility.

The remaining libkernel pthread callback fallbacks are weak so genuine pthread
QoS and stack-query functions supply those symbols too. This prevents the
initializer from registering a fallback wrapper in its own callback table and
recursing on a later priority or termination operation.

The runtime probe now tests eight concurrent once callers while the callback
is blocked, verifies that completion is published only after initialization,
and exercises copied/retained Blocks plus `__block` forwarding. A stock-host
control passes; injecting the old unsynchronized once implementation produces
the expected exit 17. These controls validate the test's ability to detect the
bug and do not count as independent runtime execution. Their exact commands
and results are in `evidence/runtime/service-probe-controls.json`.

The expanded probe also requires actual Mach timer delivery, cancellation and
port destruction, atfork callbacks that allocate/free memory and use a mutex,
and a source dylib loaded with `dlopen`. It checks the real exported function,
allocator identity, missing-symbol errors, per-thread error isolation and
`dlclose`. Host controls reject the former fake timer, discarded atfork
callbacks and missing loader errors. Guest tests require the source dylib and
an absent dyld shared cache.

The retained compatibility surface is deliberately narrower than a complete
Darwin distribution. Saved credential IDs in `getresuid/getresgid` are
represented by effective IDs, `setresuid/setresgid` provide only the existing
Darwin mapping, and the historical suffixed `popen` adapter does not maintain
the native `pclose` child bookkeeping. Standard-directory enumeration and CPU
names cover only the existing profile; fat-header helpers assume swapping on
the little-endian target. These partial adapters are absent from the required
boot/service probe paths and do not establish general compatibility for those
APIs. Sandbox policies, arbitrary XPC plist construction, ASan/stack logging
and graphical/runtime-framework services remain outside the baseline.

Keep the complete symbol audit as a build artifact and publish a reviewable
summary without discarding errors, dependency routes or provenance:

```sh
python3 scripts/build/runtime.py inspect --root .blauos/build/runtime/root \
  --provenance .blauos/build/runtime/provenance.json \
  --output .blauos/build/runtime/source-runtime-closure-full.json
python3 scripts/build/runtime_summary.py \
  --input .blauos/build/runtime/source-runtime-closure-full.json \
  --output evidence/runtime/source-runtime-closure.json
```

The summary retains every artifact, dependency, loaded provider, error and
acceptance flag. Large import/export lists are represented by counts and
canonical SHA-256 digests; the complete raw JSON retains its file hash and
byte count. CI continues to preserve the full audit.
