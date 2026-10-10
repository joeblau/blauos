	.build_version macos, 27, 2	sdk_version 27, 2
	.section	__TEXT,__text,regular,pure_instructions
	.globl	_frame_probe                    ; -- Begin function frame_probe
	.p2align	2
_frame_probe:                           ; @frame_probe
	.cfi_startproc
; %bb.0:
	stp	x29, x30, [sp, #-16]!           ; 16-byte Folded Spill
	mov	x29, sp
	sub	sp, sp, #16
	.cfi_def_cfa w29, 16
	.cfi_offset w30, -8
	.cfi_offset w29, -16
Lloh0:
	adrp	x8, ___stack_chk_guard@GOTPAGE
Lloh1:
	ldr	x8, [x8, ___stack_chk_guard@GOTPAGEOFF]
Lloh2:
	ldr	x8, [x8]
	stur	x8, [x29, #-8]
	mov	x9, x0
Lloh3:
	adrp	x16, ___chkstk_darwin@GOTPAGE
Lloh4:
	ldr	x16, [x16, ___chkstk_darwin@GOTPAGEOFF]
	blr	x16
	mov	x8, sp
	add	x9, x0, #15
	and	x9, x9, #0xfffffffffffffff0
	sub	x8, x8, x9
	mov	sp, x8
	mov	w9, #1                          ; =0x1
	strb	w9, [x8]
	add	x9, x8, x0
	mov	w10, #2                         ; =0x2
	sturb	w10, [x9, #-1]
	ldrsb	w8, [x8]
	ldursb	w9, [x9, #-1]
	ldur	x10, [x29, #-8]
Lloh5:
	adrp	x11, ___stack_chk_guard@GOTPAGE
Lloh6:
	ldr	x11, [x11, ___stack_chk_guard@GOTPAGEOFF]
Lloh7:
	ldr	x11, [x11]
	cmp	x11, x10
	b.ne	LBB0_2
; %bb.1:
	add	w0, w9, w8
	mov	sp, x29
	ldp	x29, x30, [sp], #16             ; 16-byte Folded Reload
	ret
LBB0_2:
	bl	___stack_chk_fail
	.loh AdrpLdrGotLdr	Lloh5, Lloh6, Lloh7
	.loh AdrpLdrGotLdr	Lloh0, Lloh1, Lloh2
	.loh AdrpLdrGot	Lloh3, Lloh4
	.cfi_endproc
                                        ; -- End function
.subsections_via_symbols
