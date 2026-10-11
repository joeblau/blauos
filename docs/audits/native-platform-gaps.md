# Native M1 P0 platform gaps

> **Follow-up (2026-10-10/11):** the source-level gaps below were closed by
> patches 0012–0017 — see [native-cpu-registration](native-cpu-registration.md),
> [native-handoff-validation](native-handoff-validation.md),
> [native-timer-discovery](native-timer-discovery.md),
> [native-cpu-startup](native-cpu-startup.md),
> [native-aic-hardening](native-aic-hardening.md) and
> [native-checkpoint-protocol](native-checkpoint-protocol.md). Every *physical*
> criterion in this audit remains open. The text below is the original
> point-in-time audit.

The T8103 source experiment now has a fresh linked older kernel candidate,
including the native PAC restoration. It has not booted on a physical M1.
Several important interfaces remain incomplete, especially CPU registration
and secondary startup. Issues #29–#35 remain open.

The [machine-readable audit](../../evidence/native/platform-gaps.json)
records exact source paths, original/effective hashes, patches, compile
receipts, implemented source, missing interfaces and physical criteria. This
is a source audit, not a successful boot experiment. It compares two distinct
profiles: modern Apple XNU `ac9718fb1af618d5ce8678d0dc6e8a58f252216f`
and the older PureDarwin `1bbaeb2ee6aa41e6f792a0b1e2943cc295b3b290`
native T8103 experiment. The successful VIRT runtime is separate evidence.
Neither version is assumed compatible with the test machine's installed
firmware, boot assets or proprietary drivers.

## UART and diagnostics — #29

The selected kernel already contains an Apple polling UART implementation.
H13 includes the S3c and dock-channel interfaces. `pe_serial.c::serial_init`
discovers `arm-io`, then a dock-channel or `boot-console`/`uart0`/`uart1`
node. It maps the reported register offset plus the SoC physical base and
selects an implementation from the compatible string. Clock/divider properties
may alter the default 115200 configuration. This is implemented source;
a new speculative UART register map is unnecessary at this point.

`PE_init_kprintf` selects the serial output path after `serial_init`, otherwise
it falls back to `cnputc_unbuffered`. DEVELOPMENT output also requires the
existing debug/output boot-argument configuration. `uart_putc` waits for TX
readiness without a bounded timeout; a bad resource or unready UART can stop
progress. Several legacy property reads use pointers without checking the
returned size. These paths still need target-resource validation.

[Patch 0006](../../patches/native/0006-t8103-early-diagnostics.patch)
adds a real opt-in hook after `STARTUP_SUB_KPRINTF`. Its
[actual object and parser evidence](../../evidence/native/early-diagnostics/result.json)
establishes source identity/UUID, PC and register-read instruction generation,
with a bounded validated nonce. The
[partial protocol](../platform/m1-early-diagnostics.md) does not prove the
selected UART is physically reachable or implement the full checkpoint
contract. Native UART transport, bytes from independent XNU, and captured
panic/recovery behavior remain required.

## Device discovery and memory reservations — #30

`PE_init_platform` consumes the boot-argument pointer and declared ADT length,
converts the ARM64 ADT physical pointer through `ml_static_ptovirt`, and
initializes the Apple tree parser. `pe_identify_machine` discovers SoC and
clock information. `pe_arm_get_soc_base_phys` treats `arm-io/ranges[1]` as
the physical parent base; the UART, interrupt-controller and timer paths
then add child `reg` offsets.

This source assumes the selected trusted boot environment supplies its
expected layout. Some property reads lack size/status checks, and offset
addition/window containment is not comprehensively validated. The AIC driver
checks minimum sizes for `ranges` and `reg`, but does not validate the full
three-cell range, overflow or that every register offset fits the mapped span.
`PE_init_platform` also discards `pe_arm_init_interrupts`' failure return.
These are concrete source gaps, not observed malformed physical handoffs.

The [offline handoff implementation](../platform/m1-handoff.md) rejects
bounded malformed inputs, truncation, overflow and invalid reservation
overlaps. Its explicit profiles distinguish two Version 2 / Revision 2
structures: modern 1152 bytes and older 736 bytes. It does not repair kernel
resource discovery or infer a free memory map. Actual M1 boot arguments,
ADT/resource schema, RAM banks, boot assets, firmware carveouts, page tables
and stacks must be captured and reconciled before initializing the allocator.

## MMU and protected execution — #31

H13/T8103 selects 16 KiB granules, native ARM64e PAC, CTRR, register locks,
PPL and the P/E topology. `start.s` establishes stacks, bootstrap tables,
TCR/TTBR0/TTBR1/MAIR/SCTLR and VBAR. `arm_vm_init.c` and `pmap.c` implement
the later mappings and permission transitions. Published numeric register
aliases and the public platform/PPL source selection preserve the protected
operations rather than disabling their feature definitions.

[Patch 0008](../../patches/native/0008-t8103-public-ppl-port.patch)
restores the published APRR/PPL entry, exception, dispatch and return source.
It retains selectors 0–76 and appends three explicitly local selectors; the
local table is not asserted compatible with proprietary driver dispatch IDs.
[Patch 0009](../../patches/native/0009-t8103-pac-atomic-storage.patch)
preserves the PAC-qualified field and CASes its already signed bits. Real
object reviews show the authenticated handler call and signed pointer CAS.
The [native PAC restoration](native-pac.md) adds the startup and native
key-transition code missing from the first linked candidate, while retaining
both fast and slow H13 switching. Its six actual objects compile, and a
[fresh whole-kernel replay with all eleven patches](../../evidence/native/kernel/puredarwin-t8103-native-final/result.json)
now links successfully. Both remain software evidence.

Modern XNU still requires unavailable matching security/KDK interfaces. The
older public source port requires target firmware/handoff compatibility
review. Physical entry EL, register/granule agreement, table bounds, RO/RW/XN
faults, cache/TLB transitions, CTRR/PPL lockdown and native PAC transitions
remain unverified. A raw register read is not an MMU protection test.

## Exceptions — #32

The selected `locore.s` vectors dispatch SP0/SP1 and user exceptions into
the existing fault handlers. `exception_asm.h` saves GPR/NEON/register state
and authenticates saved state. `sleh.c` distinguishes instruction/data aborts,
records syndrome/fault address and either triages a user exception or follows
the kernel panic path. The native PPL restoration includes its own protected
exception entry/return. Native PAC restoration puts slow kernel keys back
before signing the saved state.

This source route and real native object compilation do not demonstrate a
delivered physical exception. A controlled native fault must yield the actual
ESR/FAR/ELR/CPU/register record, match the exact linked symbols and captured
source identity, and end in the expected recovery or bounded panic. Earlier
QEMU crash/core captures belong to the VIRT profile.

## Timers — #33

The architecture implementation exists: `ml_get_hwclock` issues ISB then reads
`CNTVCT_EL0`; decrementer fallback reads/writes `CNTV_TVAL_EL0`; pending state
comes from `CNTV_CTL_EL0`. ARM64 timer setup selects the architectural fallback
when no board-specific callbacks are installed. However, the native discovery
path still requires a `device_type=timer` register node and maps it. That
requirement must be checked against the captured target ADT; its suitability
for this handoff has not been observed.

Native FIQ dispatch distinguishes the fast IPI register, PMU and timer pending
state. Remaining non-IPI/non-PMU FIQs fall through to `rtclock_intr(TRUE)` with
an explicit source comment that their identity is unknown. Standard external
AIC IRQ dispatch is separate, and the QEMU timer-PPI route is conditional VIRT
code. A counter snapshot or increasing counter is insufficient. Target tests
must demonstrate one-shot and periodic callbacks, monotonicity/drift, correct
IRQ/FIQ acknowledgement and continued delivery on the supported CPU set.

## Apple interrupt controller — #34

The AppleARM platform personality selects `PDAppleAIC`, not the QEMU GIC.
The driver discovers `arm-io` resources, maps uncached MMIO, masks/drains
sources, registers the primary IOKit controller, and attaches to the boot
CPU's interrupt vector. It routes all external sources to CPU 0, treats
vectors as shareable level interrupts and skips non-external event types.

[Patch 0007](../../patches/native/0007-t8103-aic-rearm-and-driver-abi.patch)
restores post-event hardware re-arming only when a handler remains registered
and enabled. It preserves hardware masking when disabled and adds ordering
before completing `interruptActive`. The
[platform module experiment](native-aic.md) genuinely links matching ARM64e
KER00 driver objects, and the [final driver build](../../evidence/native/aic/native-final/result.json)
uses the same eleven-patch profile as the final kernel. It is not an interrupt
delivery test.

The driver registers the default controller name, although its comment also
describes a per-phandle `IOInterruptController%08X` name that it does not
register. Explicit interrupt-parent nubs require reconciliation with the
actual registry/resource construction. Runtime dispatch has an unbounded
event loop, no implemented affinity policy and no demonstrated multi-CPU
delivery. Resource bounds and failure propagation remain incomplete.
Native import resolution, normal/spurious/storm behavior, handler disable
semantics, routing, affinity and IPI interaction need target evidence.

## CPU registration and secondary startup — #35

The platform module constructs one `PDArmCPU`, creates a one-CPU interrupt
controller, enables the boot interrupt vector and marks that CPU Running.
Its `startCPU` explicitly returns `KERN_FAILURE`; quiesce/halt are empty.
There is no `ml_processor_register` or `ml_set_max_cpus` implementation in this
module. The direct boot-CPU `startCommon` path also bypasses the ordinary
`IOCPU::start` group/provider setup. Marking an IOKit state Running does not
establish kernel processor registration or online topology.

The kernel has reset/secondary entry code, per-CPU state, MPIDR lookup and
fast-IPI source primitives. Those need a real platform startup/registration
connection. `cpu_start` discards the `PE_cpu_start` failure return before
returning success; that result cannot serve as a secondary-startup witness.
The count-finalization shortcut in `PE_lockdown_iokit` applies only to the
VIRT/BCM profiles, so it does not resolve the T8103 registration gap.
Early consumers of `ml_wait_max_cpus` can consequently wait indefinitely
without platform completion; this is a source-derived risk, not a measured
native boot failure.

Required work includes actual supported P/E topology/resources, processor
registration/count completion, documented secondary reset/power handoff,
per-CPU stacks/MMU/PAC/timers, IPIs, scheduling and synchronization, followed
by repeated cold/warm boot measurements. The H13 maximum of eight CPUs and
two clusters describes compile-time capacity rather than online processors.

## Evidence boundary

The audit's source and patch hashes make these conclusions reviewable.
Compiled objects, linked software, malformed-input tests and source review
are useful preparation. None supplies physical M1 execution, authorization,
boot assets, hardware attestation or Gate 2 acceptance. The next physical
experiment must use a dedicated verified target with recovery and UART
operators and capture the exact candidate, handoff and observed behavior.
