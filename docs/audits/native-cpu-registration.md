# Native M1 CPU registration and secondary startup (kernel side)

Issue [#35](https://github.com/joeblau/blauos/issues/35) remains open. This
record covers the **kernel-side** source patch only; the platform-module side
(`PDArmCPU::startCPU`, real per-CPU registration from
`PDAppleARMPlatformExpert`) is a separate change. Everything here is
compile-time software evidence from the pinned PureDarwin source experiment.
No project kernel has executed on an M1, and no secondary CPU has been
started or observed.

The experiment uses the pinned PureDarwin `puredarwin-vm-next` tree at
revision `1bbaeb2ee6aa41e6f792a0b1e2943cc295b3b290`, tree
`ed2345d7f3c11dad2e7f6ffeeb377adc789b12b8`, with the existing eleven-patch
native profile plus the new patch below. The pristine locked trees remain
unchanged; all work happens through patches.

## What the kernel already had

Source review found that the core registration interfaces already exist and
link into the T8103 kernel; they were not the missing piece:

- `ml_processor_register` — `osfmk/arm64/machine_routines.c:1350`, allocates
  per-CPU data, wires console buffers, cluster/pset placement, IPI and PMI
  handlers, and rejects registrations beyond the parsed topology.
- `ml_set_max_cpus` / `ml_wait_max_cpus` —
  `osfmk/arm/machine_routines_common.c:1040`/`:1053`; the setter only
  publishes that the count is final (its argument is ignored), the waiter
  sleeps until that publication.
- `ml_parse_cpu_topology` — `osfmk/arm64/machine_routines.c:994`, builds the
  topology from the ADT `/cpus` node bounded by `MAX_CPUS` (8) and
  `MAX_CPU_CLUSTERS` (2) for T8103.

The symbol table of the previous eleven-patch kernel
(`puredarwin-t8103-native-final/kernel.nm.txt`) shows `_ml_processor_register`,
`_ml_set_max_cpus`, `_cpu_start`, `_PE_cpu_start` and `_PE_lockdown_iokit`
all linked as external `(__TEXT_EXEC,__text)` symbols. What was missing was
the failure propagation and the T8103 count-finalization path described in
[the platform-gap audit](native-platform-gaps.md).

## Source changes — patch 0012

[Patch 0012](../../patches/native/0012-t8103-cpu-registration.patch)
(SHA-256 `a691ac6fbbf04cca9407832ecc5fc7a1e591796ecb415c1ddb17ccfcb23d3a01`)
touches exactly two kernel files. It is **not** yet listed in
`patches/native/series`; series integration is left to the coordinator.

### `osfmk/arm64/cpu.c` — `cpu_start` failure propagation

Pristine SHA-256 `938478568ba5e8b21ed24358b5f18a8e9d616c9556155209c5e5d997f836dd65`;
patched `9437c5609d63d14ed6bc98ca38db8de667b5edfada62a9786c8b51afb213c140`.

The secondary-CPU path previously ran
`(void) PE_cpu_start(...)` and then returned `KERN_SUCCESS` unconditionally,
so a platform refusal to release the CPU from reset was invisible to
`processor_start` and could not serve as a secondary-startup witness. The
patch returns the actual `PE_cpu_start` result. Its caller
`processor_start` (`osfmk/kern/processor.c:895`) already reverts the
processor to `PROCESSOR_OFF_LINE` on failure, so propagation is meaningful.

The change also repairs a latent hang that plain propagation would expose:
the first `cpu_start` in a cluster claims the cluster CTRR lockdown by
setting `CTRR_LOCKING`, and the secondary CPU itself later sets
`CTRR_LOCKED` (`osfmk/arm/arm_init.c:1216`) and wakes waiters. If
`PE_cpu_start` fails, the secondary never runs, and without a rollback every
later `cpu_start` in that cluster would block forever in
`assert_wait`/`thread_block`. On failure with a held claim, the patch
restores `CTRR_UNLOCKED` and issues `thread_wakeup` under
`ctrr_cpu_start_lck`, mirroring the existing wakeup idiom in
`osfmk/arm64/amcc_rorgn.c:701-702`. The boot-CPU path is unchanged and still
returns `KERN_SUCCESS`.

### `pexpert/arm/pe_init.c` — T8103 count finalization

Pristine SHA-256 `7b656b12e21353443af6d636d578d78d823c5fe1c8eca420854c4a2a8b3b1a83`;
patched `857feee70081247d3ef67057f18524f57f665e28725e85fb7ba29c4e952eb9bf`.

`PE_lockdown_iokit` previously ran the `ml_set_max_cpus(0)` finalization
shortcut only for `ARM64_BOARD_CONFIG_BCM2837`/`ARM64_BOARD_CONFIG_QEMUVIRT`.
The patch extends the condition to `ARM64_BOARD_CONFIG_T8103`. On T8103 the
platform module registers at most the boot CPU and never calls
`ml_set_max_cpus` (`PDArmCPU::startCPU` unconditionally returns
`KERN_FAILURE`), so without this, early `ml_wait_max_cpus` consumers such as
`kperf_init_early` can wait indefinitely — a source-derived risk, not an
observed boot failure. The published count itself comes from
`machine_conf()` (`ml_get_topology_info()->max_cpu_id + 1`); a later
driver-side `ml_set_max_cpus` call is a harmless no-op once the count is set,
so this shortcut does not preclude real platform registration.

## Build receipt

The fresh twelve-patch build used the same wrapper, SDK and machine profile
as the final eleven-patch experiment, in its own build/evidence directories:

```sh
python3 scripts/build/xnu.py --puredarwin --source-name puredarwin-vm-next \
  --machine T8103 \
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
  --patch patches/native/0012-t8103-cpu-registration.patch \
  --build-root .blauos/build/native/kernel/t8103-cpu-registration \
  --evidence evidence/native/kernel/t8103-cpu-registration --jobs 3
```

Configure and build both exited 0. The
[build receipt](../../evidence/native/kernel/t8103-cpu-registration/result.json)
records recipe SHA-256
`0e67c488a6e8357d0fe65eef1325095bd4f7461914b9fead97b58931af18d591` and raw
build-log SHA-256
`c526687c4bd8dc6b4d49107ea78a60736e8168b0093797821f628d9118cb34e9`, and
keeps byte snapshots of all twelve patches. The patch was also verified with
`git apply --check --index` after patches 0001–0011 in a fresh worktree of
the locked revision.

The linked `kernel.development.t8103` is a 15,078,648-byte Mach-O `ARM64`
subtype `E` `KER00` `MH_EXECUTE` (arm64e), SHA-256
`5253934a9822e0a8fd71a9d5cec307184b3b2a8062b90a5e9df6756fd57bcfb1`, UUID
`5D2F13D4-59D6-3914-A07A-A51BCE42B566`, with `NOUNDEFS` set and zero
undefined symbols in the unstripped binary. `_cpu_start`,
`_ml_processor_register`, `_ml_set_max_cpus`, `_PE_cpu_start` and
`_PE_lockdown_iokit` remain external text symbols; `_cpu_start` moved from
`0xfffffe00073d48a8` to `0xfffffe00073d08a8`, consistent with the added
rollback path.

## Remaining gaps and physical acceptance

This patch closes only the kernel-side source gaps. Still open for #35:

- Nothing in this build calls `ml_processor_register` for T8103:
  `USE_APPLEARMSMP` is unset for this board, so `AppleARMSMP.cpp` compiles
  empty, and registration is expected from the platform module, whose
  `PDArmCPU::startCPU` currently returns `KERN_FAILURE`. That side is owned
  by the platform-module change, not this patch.
- `PE_cpu_start` for T8103 still resolves to that failing platform path;
  this patch makes the failure visible rather than fixing the platform.
- Real supported P/E topology against the actual target ADT, secondary
  reset/power handoff documentation, per-CPU stacks/MMU/PAC/timer bring-up,
  IPI delivery, scheduling and synchronization all remain unimplemented or
  unverified.
- Compile, link and symbol-inspection results are not physical observations.
  Repeated cold/warm boot measurements on a dedicated verified target, with
  captured UART evidence of secondary startup and CTRR lockdown, remain
  required. Gate 2 is unaffected by this result.
