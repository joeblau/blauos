# Native T8103 handoff validation source patch

Issue #30's kernel-side gaps were source-confirmed in the
[platform gap audit](native-platform-gaps.md): `PE_init_platform` discarded
`pe_arm_init_interrupts`' failure return, several legacy device-tree property
reads dereferenced results without checking status or size, and the UART,
interrupt-controller and timer paths added child `reg` offsets to the
`arm-io/ranges[1]` parent base without overflow or window-containment
validation. These were concrete source gaps, not observed malformed physical
handoffs.

[Patch 0013](../../patches/native/0013-t8103-handoff-validation.patch), SHA-256
`73966f11254dc6ff9a0209821193f320724ef82652e3f060a4eb5dcea22d312b` (425 lines,
16,371 bytes), closes those kernel-side gaps. It applies cleanly after patch
0011 in a fresh worktree of the pinned PureDarwin source
`1bbaeb2ee6aa41e6f792a0b1e2943cc295b3b290` (`git apply --check` passes); the
pristine locked tree is unchanged. The offline Python handoff decoder and its
[contract](../platform/m1-handoff.md) are untouched; this patch is kernel C
source only.

## Implemented

- **Failure propagation.** `pexpert/arm/pe_init.c::PE_init_platform` now
  returns `kern_return_t` and returns `KERN_FAILURE` when
  `pe_arm_init_interrupts` reports failure, instead of discarding it. The
  shared `pexpert/pexpert/pexpert.h` prototype and the
  `pexpert/i386/pe_init.c` definition (which always returns `KERN_SUCCESS`)
  were updated to match. Both `osfmk/arm/arm_init.c` post-VM call sites check
  the result and panic on failure, the existing idiom for unrecoverable
  platform-expert initialization (compare `pe_init_debug_command`).
- **Property-read validation.** `pe_identify_machine.c::pe_arm_get_soc_base_phys`
  now requires successful, non-empty `device_type` and `ranges` reads
  (minimum two LP64 cells) before deriving the SoC physical base; a malformed
  `arm-io` node yields base 0 and fails the dependent discovery paths. It also
  captures the optional third `ranges` cell as the parent window length.
  `pe_serial.c::serial_init` adds status and minimum-size checks to the
  `boot-console`/`uart0`/`uart1`, `dockchannel-uart` and VMAPPLE `uart0` `reg`
  reads, to the S3C `pclk`/`sampling`/`ubrdiv` clock/divider reads, and to the
  dockchannel `max-aop-clk`/`enable-sw-drain`/`dock-wstat-mask` scalars
  (defaults preserved when absent).
- **Overflow-checked, contained address computation.** The new
  `pe_arm_soc_reg_range` helper (`pe_identify_machine.c`, declared in
  `pexpert/pexpert/arm/protos.h`) rejects short `reg` properties, zero
  lengths and wrapping base+offset addition, and checks the child window
  against the parent `ranges` length when the firmware supplies one. The
  interrupt-controller and timer mappings in `pe_arm_map_interrupt_controller`,
  the `cpu-debug-interface` mapping in `pe_arm_init_debug`, and the UART and
  dockchannel mappings in `serial_init` (via a file-local
  `serial_map_uart_regs`) all route through it. Invalid dockchannel `reg`
  keeps the existing `panic("Malformed dockchannel-uart property")` behavior;
  an unmapped dockchannel now falls back to the traditional UART instead of
  registering functions backed by a zero base.

The patch deliberately does not touch `cpu_start`, `ml_processor_register` or
the `device_type=timer` node lookup, which are owned by the #33/#35 work; only
the shared offset-addition path those discoverers use is validated. The VIRT
(QEMU/BCM2837) paths are unchanged.

## Build evidence

The [build receipt](../../evidence/native/kernel/t8103-handoff-validation/result.json)
records a fresh DEVELOPMENT ARM64e T8103 build with patches 0001–0011 plus
0013, recipe SHA-256 `46a46a85d0f68fae5f245880d9a2e2aa6871704704f1a508668c2163847f3d4e`,
with configure and build both exiting zero. It produced a
15,079,040-byte `kernel.development.t8103` (Mach-O arm64e), SHA-256
`488aada1dbcf0db3cfa13f8d2482cb53fada54a0043c10655b69f905445cc16d`, UUID
`9873F737-C195-31F9-8800-EF9D071F9831`, plus the unstripped kernel and dSYM.
The unstripped image contains `_pe_arm_soc_reg_range`, the updated
`_PE_init_platform` and `_pe_arm_init_interrupts`, and all five new failure
diagnostic strings, confirming the validation and propagation paths compiled
into the linked kernel.

## Physically unverified remainder

This is compile-time software evidence only. No physical M1 handoff has been
captured, so the real T8103 ADT's `ranges` layout is unconfirmed: if iBoot
supplies only two-cell entries, the window length is unknown and containment
checking is skipped (overflow and size checks still apply). A two-cell
`ranges` on the actual target would make every child offset unchecked against
a window until a real handoff is captured and reconciled. `uart_putc` still
waits for TX readiness without a bounded timeout (#29). The PDAppleAIC
driver's own `ranges`/`reg` checks are a separate platform-module surface
(#34). Timer-discovery suitability, CPU registration/secondary startup, and
Gate 2 physical acceptance remain open under #33 and #35.
