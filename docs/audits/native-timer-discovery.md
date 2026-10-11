# Native M1 timer discovery and FIQ dispatch experiment

Issue [#33](https://github.com/joeblau/blauos/issues/33) remains open. The
T8103 kernel compiles and links with the timer-discovery repair below; no
timer interrupt has been delivered on physical M1 hardware in this
experiment.

## Source change

[Patch 0014](../../patches/native/0014-t8103-timer-discovery.patch), SHA-256
`9097aaa49804e8db932e376a28cde60ba4ca11e50de9afaf2bc0a9b29692492f`, closes
the kernel-side source gaps recorded in the
[platform-gaps audit](native-platform-gaps.md#timers--33). It applies
cleanly on top of the ordered series 0001–0011 and touches two files in the
pinned PureDarwin `1bbaeb2ee6aa41e6f792a0b1e2943cc295b3b290` tree.

`src/Kernel/xnu/pexpert/arm/pe_identify_machine.c`,
`pe_arm_map_interrupt_controller`: the previous path required a
`device_type=timer` node, dereferenced its `reg` property without checking
the lookup status, the property size or the cell values, mapped
`soc_phys + reg[0]` without an overflow check, and returned hard failure
whenever the node or mapping was absent. The revised path checks the
`SecureDTGetProperty` status, requires at least two address cells with a
non-zero size, rejects offset addition overflow, and only maps validated
input. On `__arm64__`, an absent, malformed or unmappable timer node no
longer fails interrupt initialization: the code logs the fallback
explicitly and returns success so `pe_arm_init_timer` still registers the
empty timebase operations. That is a clean fallback, not a silent one —
`ml_set_decrementer`, `ml_get_decrementer` and `ml_get_timer_pending` in
`osfmk/arm64/machine_routines.c` program `CNTV_TVAL_EL0`/`CNTV_CTL_EL0`
directly when no board-specific callbacks are installed, and the ARM64
setup never dereferences `gTimerBase`. The 32-bit ARM behavior is
unchanged: a missing timer node still fails there, because those SoC
paths genuinely require the MMIO timer.

`src/Kernel/xnu/osfmk/arm64/sleh.c`, `sleh_fiq`: the non-IPI/non-PMU
fall-through previously serviced every unidentified FIQ through
`rtclock_intr(TRUE)` with only a comment admitting the source identity was
unknown. The revised path distinguishes the confirmed architectural-timer
case (`ml_get_timer_pending()` set `DBG_INTR_TYPE_TIMER`) from an unknown
source. Unknown FIQs keep the existing servicing semantics — the source
cannot be masked at an unknown origin, so dropping the timer path could
lose a real deadline — but are now accounted for explicitly and boundedly:
the first sixteen occurrences log `sleh_fiq: unknown FIQ source`, then the
path stays silent so a misrouted source cannot log at FIQ rate. No new
interrupt model is introduced; IPI, PMI and timer dispatch order and the
AIC IRQ path are unchanged.

## Actual build

The build wrapper archives the pristine locked source, applies immutable
byte snapshots of patches 0001–0011 followed by 0014, and records every
input hash. The
[build receipt](../../evidence/native/kernel/t8103-timer-discovery/result.json)
contains the full commands, wrapper hash, source archive hash, patch
snapshots and raw-log identities.

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
  --patch patches/native/0014-t8103-timer-discovery.patch \
  --build-root .blauos/build/native/kernel/t8103-timer-discovery \
  --evidence evidence/native/kernel/t8103-timer-discovery --jobs 3
```

Configure and build both exited 0. The build produced a 15,078,720-byte
ARM64e kernel, SHA-256
`f2d95fa0603744e5aa8b18b01e63319e0fd4c0ba3df60c5bd2d862e6394d01e8`, UUID
`93422966-C7E5-3969-BFE0-711701FA572B`, subtype KER00 with `NOUNDEFS`.
Both edited translation units compiled as genuine ARM64e KER00 objects,
and the linked unstripped kernel contains the new fallback and
unknown-FIQ log strings. These are compile and inspection results, not
physical observations.

## Remaining physical acceptance

The discovery path now tolerates a missing or malformed timer node, but
its suitability for the actual target handoff has not been observed: the
real ADT may present a valid timer node whose mapping is still exercised
unchanged. One-shot and periodic deadline delivery, timebase
monotonicity, drift against a reference clock, correct IRQ/FIQ
acknowledgement, and continued delivery on the supported CPU set remain
physical tests on a verified target. The unknown-FIQ bound is a
diagnostic limit, not evidence about real misrouted sources; whether any
non-IPI/non-PMU/non-timer FIQs occur on T8103 firmware is unknown.
Interrupt-controller discovery failure handling and the discarded
`pe_arm_init_interrupts` return in `PE_init_platform` belong to the
device-discovery audit (#30) and are unchanged here. No synthetic test or
compile result satisfies the physical criteria; Gate 2 remains not run.
