# Native M1 CPU registration and secondary startup (platform-module side)

Issue [#35](https://github.com/joeblau/blauos/issues/35) remains open. This
record covers the **platform-module** source patch only; the kernel side is
[the patch-0012 record](native-cpu-registration.md). Everything here is
compile-time software evidence from the pinned PureDarwin source experiment.
No project kernel or platform extension has executed on an M1, and no
secondary CPU has been started or observed.

The experiment uses the pinned PureDarwin `puredarwin-vm-next` tree at
revision `1bbaeb2ee6aa41e6f792a0b1e2943cc295b3b290`, tree
`ed2345d7f3c11dad2e7f6ffeeb377adc789b12b8`, with the existing eleven-patch
native profile plus the new patch below. The pristine locked trees remain
unchanged; all work happens through patches.

## Source changes — patch 0015

[Patch 0015](../../patches/native/0015-t8103-platform-cpu-startup.patch)
(SHA-256 `b150edaa11b1e61b90dda080a663436a333aeace0aa93be20167f718433151ca`)
touches only `src/Kernel/Extensions/PDArmPlatformExpert/` module files. It is
**not** listed in `patches/native/series`; series integration is left to the
coordinator. It applies cleanly after patch 0011 (verified with
`git apply --check` in a fresh worktree of the locked revision), and it shares
no files with patches 0012–0014 (kernel) or 0016 (AIC driver), so it is
independent of their ordering.

### `PDAppleARMCPU.cpp` / `PDAppleARMCPU.h` — new Apple-SoC IOCPU personality

New files, SHA-256
`0548e88bb59132ba002a4b6d58c67f7881702e7ee0806b0da809cf812be07799` and
`9fb1bffa06a3d4b39829045f8b182e7196fc0d6446227183541f36b01cb48378`. The
virt/Pi `PDArmCPU` behavior is unchanged: its `startCPU` still returns
`KERN_FAILURE` and only the Apple profile uses the new class.

- `registerProcessor()` calls the kernel's `ml_processor_register()`
  (`osfmk/arm64/machine_routines.c:1350`, exported via
  `config/Unsupported.exports`) with the parsed topology entry: `cpu_id` is
  the `IOCPU` object (the generic `PE_cpu_start`/`PE_cpu_machine_init` path in
  `iokit/Kernel/IOCPU.cpp` casts it back), `phys_id` is the MPIDR[15:0] value,
  and cluster/cache identity comes from `ml_get_topology_info()`. The returned
  `processor_t` is kept in the ordinary `IOCPU::machProcessor` member and the
  returned IPI handler in `IOCPU::ipi_handler`.
- `startCPU()` performs the documented H13/M1 reset/power handoff instead of
  the old unconditional `KERN_FAILURE`: the entry address is programmed (or
  confirmed) in the core's RVBAR slot at `cpu-impl-reg + 0x00`, the core is
  marked in the system-wide alive bitmask at `pmgr + 0x54004`, and the
  per-cluster start bit is set at `pmgr + 0x54008 + 4 * cluster`. The offsets
  are the M1-series values from the
  [Asahi SMP documentation at the pinned research revision](https://github.com/AsahiLinux/docs/blob/0d1f8917fa88745d62a4c05d802c4a7298c8616b/docs/hw/cpu/smp.md).
  It returns real results: `KERN_FAILURE` for an unregistered or boot CPU
  object, `KERN_RESOURCE_SHORTAGE` when the cpu-impl block or PMGR registers
  cannot be found or mapped, and `KERN_INVALID_ARGUMENT` when no reset entry
  address exists. `cpu_start()` previously discarded this result; the
  kernel-side patch 0012 propagates it.
- `startCommon()` creates one `PDArmCPUInterruptController` shared by the
  whole supported CPU set, sized with the runtime topology count (not the
  compile-time H13 capacity of 8 CPUs / 2 clusters), so the source count
  passed to `ml_set_max_cpus()` and the `enableCPUInterrupt()` check-in
  barrier reflect the supported set instead of a hardcoded single CPU.
- `initCPU()` registers the processor once, installs the per-CPU interrupt
  handler once, and marks the CPU Running; it runs on the boot CPU from
  `startCommon()` and on each secondary from `PE_cpu_machine_init()` as the
  core comes up.
- `quiesceCPU()` and `haltCPU()` replace the empty stubs: quiesce runs the
  platform quiesce actions on the boot CPU, marks the CPU Stopped and enters
  `ml_arm_sleep()`; halt takes a secondary offline through
  `processor_exit()` and, on the boot CPU, quiesces the platform and sleeps
  in `ml_arm_sleep()` until wake, then runs the platform active actions.
- `signalCPU()`/`signalCPUDeferred()`/`signalCPUCancel()` map onto the H13
  fast-IPI primitives `ml_cpu_signal()`/`ml_cpu_signal_deferred()`/
  `ml_cpu_signal_retract()` addressed by the target's MPIDR phys_id.

`cpu_start()` in this profile passes a null `start_paddr`, so `startCPU()`
reads the RVBAR value that iBoot programmed from the kernel entry point
("Reset vector = (__start & ~0xfff)", `osfmk/arm64/start.s:286-288`; the
Asahi documentation notes iBoot locks RVBAR only for cpu0) and programs a
caller-supplied address when one is provided. If both are zero the start
fails honestly instead of releasing a core to a null entry.

### `PDArmPlatformExpert.cpp` — boot-CPU path and secondary startup

Pristine SHA-256 `1d1bf7ada114d22974e826f4b0016abad34d537c43cbb87ad3e4b3e690f4029c`;
patched `7a0a4e9e83659288655c9cc3b123890030a71660d8de46e0551e44bffe290761`.

`PDArmPlatformExpert::start()` now goes through three new virtual hooks:
`newBootCPU()`, `startBootCPU()` and `startSecondaryCPUs()`. The base
implementations preserve the existing virt/Pi behavior exactly. The
`PDAppleARMPlatformExpert` overrides:

- create a `PDAppleARMCPU` for the boot CPU, bind it to the topology's
  boot-CPU entry, and start it with the platform expert as provider through
  the ordinary `IOCPU::start()` group/property path instead of the direct
  `startCommon()` bypass;
- walk the parsed topology, create one `PDAppleARMCPU` per non-boot CPU,
  register each with `ml_processor_register()` and release it with
  `processor_start()` (`processor_start` reverts the processor to
  `PROCESSOR_OFF_LINE` when the propagated `PE_cpu_start` result fails);
- call `ml_cpu_init_completed()` and publish `gIOAllCPUInitializedKey` after
  all processors are registered, matching the `AppleARMSMP.cpp` contract
  (`USE_APPLEARMSMP` is unset for T8103, so that in-kernel path compiles
  empty and this module is the only registration connection).

Secondaries are released before `initPlatformInterruptsLate()` so the AIC's
registration with the shared CPU interrupt controller waits on cores that
have actually been started rather than blocking ahead of them.

### `PDArmCPU.h` / `CMakeLists.txt`

`PDArmCPU.h` pristine SHA-256
`959c65d0ee38b6c210f207541d3cb86e596cf10f0a1fda1f2e78a8a83ea3d12e`, patched
`945e84273e8d32abde669ac4303a9b20c4339e3d49697da556030961a7113702`: the
`cpuIC`/`startCommonCompleted` members move to protected and `startCommon()`
becomes virtual so the Apple subclass can share the controller.
`CMakeLists.txt` pristine SHA-256
`aff6bcf50c2fa100d73bbbd31342741955ca4037b9e826d44c085c11de8e829e`, patched
`bc9bc5fe881606db9129589db7bda5de20a734dddf3124ed45a682580321d873`: adds the
new source file.

## Build receipt

The fresh twelve-patch build used the same wrapper, SDK, machine profile and
eleven-patch profile as the final AIC experiment, in its own build/evidence
directories:

```sh
python3 scripts/build/native_platform.py \
  --sdk /Applications/Xcode-27.2.0-Beta.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk \
  --patch patches/native/0001-puredarwin-t8103-build-profile.patch \
  --patch patches/native/0002-t8103-public-register-encodings.patch \
  --patch patches/native/0003-t8103-current-clang-diagnostics.patch \
  --patch patches/native/0004-t8103-public-platform-sources.patch \
  --patch patches/native/0005-t8103-public-ctrr-interface.patch \
  --patch patches/native/0006-t8103-early-diagnostics.patch \
  --patch patches/native/0007-t8103-aic-rearm-and-driver-abi.patch \
  --patch patches/native/0008-t8103-public-ppl-port.patch \
  --patch patches/native/0009-t8103-pac-atomic-storage.patch \
  --patch patches/native/0010-t8103-machine-register-encodings.patch \
  --patch patches/native/0011-t8103-public-native-pac.patch \
  --patch patches/native/0015-t8103-platform-cpu-startup.patch \
  --build-root .blauos/build/native/platform-cpu-startup \
  --evidence evidence/native/platform-cpu-startup --jobs 3
```

Configure and build both exited 0. The
[build receipt](../../evidence/native/platform-cpu-startup/result.json)
records recipe SHA-256
`f6fdc04986445e5d7c3910f3ef72ab1829ec9b8f664fba61a1e2b93050bbbd57`, source
archive SHA-256
`1c955560333a1e47a38f2018ead9fd90580eb4b9520f44435ebfa03afcd3c1f6` (identical
to the final AIC experiment), and byte snapshots of all twelve patches.

The linked `PDArmPlatformExpert` bundle is a 165,472-byte Mach-O `ARM64`
subtype `E` `KER00` `MH_KEXT_BUNDLE` (arm64e), SHA-256
`b04137942319f32569422669f7bea651eca73bea810720d83aa2be7862634c0d`, UUID
`E8D5BBEB-7860-3B4A-A728-C920C39019F6`, with separate 16 KiB-aligned
executable and writable segments and no userspace dynamic-library load
commands.

The bundle has 468 undefined kernel imports (453 before this patch). An
[actual symbol-name comparison](../../evidence/native/platform-cpu-startup/import-availability.json)
finds all 468 names — including `_ml_processor_register`,
`_ml_get_topology_info`, `_ml_cpu_signal`, `_ml_cpu_signal_deferred`,
`_ml_cpu_signal_retract`, `_ml_arm_sleep`, `_ml_cpu_init_completed`,
`_processor_start`, `_processor_exit`, `_IOCPURunPlatformQuiesceActions`,
`_IOCPURunPlatformActiveActions` and `_gIOAllCPUInitializedKey` — in the final
eleven-patch source-built native kernel. Name availability does not establish
export-policy permission, authentication fixups, ABI compatibility or
collection linkage; import resolution during native collection construction
remains a separate requirement.

## Interface assumptions and coordination

- The kernel side of #35 is patch 0012
  ([record](native-cpu-registration.md)). It propagates the `PE_cpu_start`
  result and finalizes the CPU count for T8103 in `PE_lockdown_iokit`; a
  later module-side `ml_set_max_cpus()` is a documented harmless no-op there.
  Patch 0012 leaves `PE_cpu_start(cpu_id, NULL, NULL)` arguments unchanged,
  so this module takes the reset entry from the firmware-programmed RVBAR
  unless a caller supplies `start_paddr`. A future one-line kernel change
  passing the `LowResetVectorBase` physical address through `start_paddr`
  would remove that firmware assumption.
- The AIC driver file (`PDAppleAIC.cpp`) is untouched; it is owned by the
  AIC-hardening patch 0016. The shared CPU interrupt controller keeps the
  existing vector-0 contract the AIC attaches to.

## Remaining physical acceptance

This patch closes only the platform-module source gaps. Still open for #35:

- Real secondary startup on hardware: whether iBoot's RVBAR programming is
  sufficient, whether the PMGR alive/start writes match the target silicon
  revision, and whether the core reaches `start_cpu` at all. None of this
  has run on a physical M1.
- Per-CPU stacks/MMU/PAC/timer bring-up on the secondary path, IPI delivery
  through the fast-IPI FIQ route, scheduling across both clusters, and
  synchronization all remain unverified.
- The shared interrupt controller's check-in barrier means a CPU that fails
  its handoff stalls later interrupt-controller registrations; that is the
  in-tree `IOCPUInterruptController` contract, observed only in source.
- Compile, link and symbol-inspection results are not physical observations.
  Repeated cold/warm boot measurements on a dedicated verified target, with
  captured UART evidence of secondary startup, remain required. Gate 2 is
  unaffected by this result.
