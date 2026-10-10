# Native M1 early diagnostic preparation

Issue #29 has a source-built diagnostic hook, but no physical M1 execution
evidence. [Patch 0006](../../patches/native/0006-t8103-early-diagnostics.patch)
adds an opt-in helper to the pinned PureDarwin XNU source at revision
`1bbaeb2ee6aa41e6f792a0b1e2943cc295b3b290`. It runs immediately after
`kernel_startup_initialize_upto(STARTUP_SUB_KPRINTF)` in `arm_init` and uses
the existing `kprintf` path. It does not initialize a UART or replace a driver.
The helper and call are present only for T8103 DEVELOPMENT or DEBUG builds.

The actual `arm_init.o` compiled with the existing native source-generated
recipe: `-arch arm64e`, `-mcpu=apple-a14`, `-mkernel`, DEVELOPMENT, 16 KiB
page geometry and the existing kernel integrity configuration. The object is
an ARM64e kernel Mach-O object, not a linked kernel or a BootKC. Its
disassembly contains all ten requested architectural register reads, an
`adr` instruction for the live PC and no system register writes in the helper.
The existing arm64e pointer authentication prologue is retained.

The [receipt](../../evidence/native/early-diagnostics/result.json) records the
patch, actual source, compiler, generated recipe, object and 339 compiler input
hashes. Full input and disassembly reports remain in the ignored build directory;
the receipt records their hashes and paths. This experiment reuses an existing
configured build's generated headers and recipe. It does not establish a fresh
full native build, resolve the separately recorded PPL build blockers, prove
firmware acceptance, or validate runtime behavior on an M1.

## Opt-in interface

For a future authorized physical experiment, the boot argument is:

```text
blauos_boot_nonce=hex:<64 lowercase hexadecimal characters>
```

`PE_parse_boot_argn` interprets numeric-looking arguments as integers, even when
the destination is a character buffer. The `hex:` prefix selects its actual
string path for every nonce, including a nonce consisting entirely of digits.
The helper requires the exact prefix and 64 lowercase hexadecimal characters.
Its 70-byte buffer leaves one extra copied character before the terminator,
allowing it to reject an overlong token rather than accept a silently truncated
64-character nonce. It emits no snapshot when the argument is missing or invalid.
An invalid argument produces only an unbound `disabled reason=invalid_nonce`
line, which cannot be treated as a successful checkpoint.

Every observation line starts with `BLAUOS_NATIVE_DIAG <64-character nonce>`.
The first line identifies `checkpoint=kprintf_ready protocol=partial-v1`.
This prefix is distinct from the full proposed physical validation protocol.
The fields are:

| Observation | Source and limit |
| --- | --- |
| `source_profile`, `declared_source_revision` | Build-time declaration of the pinned source. This is not a self-authenticating source or artifact hash. |
| `kernel_uuid` | Actual linked Mach-O UUID from `getuuidfromheader`; `unavailable` if absent. Displayed as 32 lowercase hex characters. |
| `kernel_version`, `version_variant` | Actual kernel globals, bounded to 256 and 96 characters. Version text can contain a newline. |
| `pc`, `current_el` | Actual `adr` and `MRS CurrentEL` observations. CurrentEL is reported raw; bits 3:2 encode the execution level. |
| `sctlr_el1`, `tcr_el1`, `ttbr0_el1`, `ttbr1_el1`, `mair_el1` | Actual EL1 register reads. These do not validate page tables, memory permissions or physical reservations. |
| `cntfrq_el0`, `cntvct_el0`, `cntv_ctl_el0`, `cntv_cval_el0` | Actual virtual counter/timer register reads. A single snapshot does not demonstrate counter progress, timer callbacks or interrupt delivery. |

Reads occur across several `kprintf` calls; they are not an atomic snapshot.
The helper reports no hardware model, ADT checksum, memory-map result, exception
test, IRQ counter, timer callback result or secondary CPU startup result.
The build-time source declaration must be associated with a separately verified
kernel UUID and source/BootKC receipts before it can support physical acceptance.

An additional explicit `blauos_diag_panic=1` requests the kernel's real `panic`
path after a valid nonce and the register output. It is off by default and has
not been executed on physical hardware. It does not promise bounded panic
completion, a reboot, recovery or a functioning serial sink.

## Replay and tests

After the source build has generated the native T8103 recipe and headers,
compile the isolated diagnostic object into a new empty output directory:

```sh
python3 evidence/native/early-diagnostics/replay.py \
  --configured-build .blauos/build/native/kernel/puredarwin-t8103-ctrr-interface \
  --output-dir .blauos/build/native/early-diagnostics-replay-new
```

The script applies the snapshotted patch to the locked pristine `arm_init.c`,
obtains the actual compiler invocation from the generated make recipe, and
changes the source/object/dependency output paths while retaining its native
compiler flags and includes. It records the complete invocation and inputs.
It also preprocesses the actual patched source under T8103, VIRT and T8010:
the emitter appears only under T8103. These are preprocessing checks, not full
builds of alternate platforms.

Thirteen host tests compile XNU's actual pinned boot argument parsing functions
with the actual patched validator. A host-only input seam supplies `PE_boot_args`;
no architectural register or kernel hardware function is executed. Tests cover
missing, empty, short, overlong, truncated, uppercase, nonhex and embedded-NUL
inputs, valid mixed/letter/numeric nonces, and the numeric-without-prefix error.
Buffer canaries remain intact. This host fixture uses the installed macOS SDK
and runtime for its unit tests; it is not independent Darwin userspace evidence.

Issue #29 and Gate 2 remain open until an independently built, authorized native
kernel actually reaches a measured checkpoint on the dedicated M1, with serial
capture and the required hardware observations tied to that exact boot attempt.
