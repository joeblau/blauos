/* SPDX-License-Identifier: MIT
 * Darwin ARM64 compiler helper: frame size is in x9. Probe every 4 KiB down
 * from the caller's SP, including the final partial page. Four KiB increments
 * also cover XNU's 16 KiB ARM64 pages. Preserve all registers and NZCV; the
 * caller performs the actual stack adjustment after returning.
 * The ABI is verified against the selected compiler's emitted assembly.
 */
#if !defined(__arm64__) && !defined(__aarch64__)
#error "This stack probe implements the Darwin ARM64 ABI only"
#endif
    .text
    .p2align 2
    .globl ___chkstk_darwin
    .globl ____chkstk_darwin
___chkstk_darwin:
____chkstk_darwin:
    stp x10, x11, [sp, #-32]!
    str x12, [sp, #16]
    mrs x12, nzcv
    add x10, sp, #32
    mov x11, x9
L_blauos_probe_loop:
    cbz x11, L_blauos_probe_done
    cmp x11, #4096
    b.lo L_blauos_probe_partial
    sub x10, x10, #4096
    ldr xzr, [x10]
    sub x11, x11, #4096
    b L_blauos_probe_loop
L_blauos_probe_partial:
    sub x10, x10, x11
    ldr xzr, [x10]
L_blauos_probe_done:
    msr nzcv, x12
    ldr x12, [sp, #16]
    ldp x10, x11, [sp], #32
    ret
    .subsections_via_symbols
