# M1 handoff validation contract

Offline parsing is implemented for pinned source layouts. No physical M1
handoff, memory map, exception handler or MMU state has been validated.
This work prepares issues #30, #31 and #32; their physical acceptance criteria
remain open. The [contract](../../platform/apple-m1/handoff/contract.json)
records exact source revisions, file hashes and field offsets.

The native source target is Apple XNU `12377.121.6`, commit
`ac9718fb1af618d5ce8678d0dc6e8a58f252216f`, with T8103 configuration.
PureDarwin's XNU 7195 lineage is a separate ABI comparison and virtual baseline.
A separate T8103 source-build experiment is also being evaluated; neither that
experiment nor its successful QEMU boot establishes native M1 compatibility.

Apple describes a chain involving Boot ROM, LLB, LocalPolicy, paired firmware
and iBoot. Permissive Security permits locally signed custom boot objects;
it does not supply missing kernel drivers or a supported custom-kernel ABI.
Handoff compatibility must be measured on the chosen OS/firmware/kernel
combination. These tools make no boot-policy changes. See Apple's
[boot-process description](https://support.apple.com/guide/security/boot-process-secac71d5623/web)
and [startup-security policy](https://support.apple.com/guide/security/startup-disk-security-policy-control-sec7d92dc49f/web).

## Source-specific boot arguments

Both source headers use LP64 fields and Version 2 / Revision 2 constants, but
their command-line arrays differ. Revision/version alone cannot identify the
layout. The inspector requires an explicit profile and exact input length.

| Field (offset in bytes unless stated) | Apple XNU 12377 | PureDarwin XNU 7195 |
| --- | ---: | ---: |
| Profile | `apple-xnu-12377` | `puredarwin-xnu-7195` |
| Total structure bytes | 1152 | 736 |
| Revision / Version offsets | 0 / 2 | 0 / 2 |
| virtBase / physBase / memSize / topOfKernelData | 8 / 16 / 24 / 32 | 8 / 16 / 24 / 32 |
| Boot_Video / machineType | 40 / 88 | 40 / 88 |
| deviceTreeP / deviceTreeLength | 96 / 104 | 96 / 104 |
| CommandLine start / capacity | 108 / 1024 | 108 / 608 |
| bootFlags / memSizeActual | 1136 / 1144 | 720 / 728 |

Offsets include LP64 alignment padding. Four compile-only ARM64/ARM64e checks
against the actual pinned headers pass, recorded in
[abi-layout.json](../../evidence/native/handoff/abi-layout.json). The checks
use empty shims for unrelated debug/prototype includes, then compile
`sizeof`/`offsetof` assertions against the original header. They execute no
kernel or replacement runtime.

The modern source definition is
[arm64/boot.h](https://github.com/apple-oss-distributions/xnu/blob/ac9718fb1af618d5ce8678d0dc6e8a58f252216f/pexpert/pexpert/arm64/boot.h).
Its [generated assembly offsets](https://github.com/apple-oss-distributions/xnu/blob/ac9718fb1af618d5ce8678d0dc6e8a58f252216f/osfmk/arm64/genassym.c)
come from `offsetof`. Actual firmware may use a different layout or tail;
unsupported revisions and sizes fail rather than being silently reinterpreted.

## Apple device tree and reservations

The Apple flattened tree has a little-endian 8-byte node header containing
property and child counts. Each property has a 32-byte NUL-terminated name,
a 32-bit length, its value bytes and padding to four bytes. Children follow
their parent's properties. This differs from the standard Linux FDT format.
The definition and traversal are in
[device_tree.h](https://github.com/apple-oss-distributions/xnu/blob/ac9718fb1af618d5ce8678d0dc6e8a58f252216f/pexpert/pexpert/device_tree.h)
and [device_tree.c](https://github.com/apple-oss-distributions/xnu/blob/ac9718fb1af618d5ce8678d0dc6e8a58f252216f/pexpert/gen/device_tree.c).

The parser bounds total bytes, nesting, nodes, properties and property lengths.
It rejects missing names, duplicate properties/siblings, truncation and trailing
data. Its reviewed subset requires named nodes and a single 16-byte LP64
`DTMemoryMapRange` per `/chosen/memory-map` property. Other encodings, empty
sentinel nodes and trailing allocation padding require a separate source and
capture review; they are rejected by the current strict profile.

`physBase`/`memSize` identify the kernel-managed region, not necessarily every
physical RAM bank. `memSizeActual` is retained as metadata, never used to invent
another contiguous RAM range. The operator supplies RAM bounds, firmware and
kernel reservations, boot-argument physical location, page-size assumption and
the device-tree pointer's address space. All DT memory-map entries are treated
conservatively as reservations; they are not inferred to be MMIO or free RAM.

The validator checks unsigned arithmetic, page alignment, RAM containment and
overlaps. Boot arguments and ADT storage become explicit reservations. A
kernel-virtual DT pointer is translated only through the stated linear window;
the tool never strips PAC bits or guesses the pointer space. Known nested
reservations require an explicit pair in `allowed_containment_overlaps`.
Partial overlaps and overlapping RAM banks always fail. It returns no free
memory map: missing firmware, secure-monitor, framebuffer, BootKC/AuxKC,
trust-cache, stack or page-table reservations must be reviewed using physical
evidence before an allocator can rely on the map.

## Entry, MMU and exception assumptions

The reviewed native entry path is source-specific. The platform entry shim
and actual BootKC still require separate validation; an offline structure
match does not establish that iBoot accepted or entered the kernel.

| Source observation | Physical evidence still required |
| --- | --- |
| `start_first_cpu` documents x0 as the boot-argument pointer and copies it to x20. | Entry PC, x0, pointer space and captured boot-argument bytes from the actual native boot. |
| Startup masks DAIF, establishes exception/interrupt stacks and writes VBAR_EL1. | CurrentEL, stack ranges, vector alignment/mapping and a real controlled exception record. |
| `common_start` writes TCR_EL1, TTBR0/TTBR1, MAIR_EL1 and SCTLR_EL1. | Initial and resulting register values, usable translation tables, mappings, permissions and TLB behavior. |
| Page granule selection depends on `__ARM_16K_PG__`; it is not inferred from the file. | Exact target compile configuration and captured hardware/MMU granule agreement. |
| T8103 board configuration enables secure-pmap integration when CONFIG_SPTM is absent. | Matching platform/KDK security interfaces, firmware state and required monitor handoff. |
| `arm_vm_init` rounds managed memory down to PAGE_SIZE and starts allocation at topOfKernelData. | Actual RAM banks, carveouts, all live boot assets and reservations that survive initialization. |

These observations come from
[start.s](https://github.com/apple-oss-distributions/xnu/blob/ac9718fb1af618d5ce8678d0dc6e8a58f252216f/osfmk/arm64/start.s),
[arm_vm_init.c](https://github.com/apple-oss-distributions/xnu/blob/ac9718fb1af618d5ce8678d0dc6e8a58f252216f/osfmk/arm64/arm_vm_init.c),
[board_config.h](https://github.com/apple-oss-distributions/xnu/blob/ac9718fb1af618d5ce8678d0dc6e8a58f252216f/pexpert/pexpert/arm64/board_config.h)
and [proc_reg.h](https://github.com/apple-oss-distributions/xnu/blob/ac9718fb1af618d5ce8678d0dc6e8a58f252216f/osfmk/arm64/proc_reg.h).
The reviewed startup writes EL1 registers; it does not prove a particular
loader's incoming EL/MMU state or supply a generic EL2-to-EL1 adapter.

## Replay and evidence

```sh
python3 scripts/platform/verify_m1_handoff_layout.py
python3 -B -m unittest discover -s platform/apple-m1/handoff/tests -v
python3 scripts/platform/inspect_m1_handoff.py \
  --profile apple-xnu-12377 \
  --boot-args /path/to/approved-boot-args.bin \
  --adt /path/to/approved-apple-device-tree.bin \
  --memory-plan /path/to/reviewed-memory-plan.json \
  --report .blauos/build/native-handoff/inspection.json
```

The memory-plan schema is demonstrated by the explicitly
[synthetic fixture plan](../../evidence/native/handoff/synthetic-memory-plan.json).
The [test receipt](../../evidence/native/handoff/tests.json) records 36 passing
synthetic tests, successful inspection of both source layouts, and rejection
of a deliberately truncated ADT. The
[readiness record](../../evidence/native/handoff/readiness.json) keeps each
physical acceptance criterion pending.
Synthetic test data verifies parser behavior only. A live macOS IORegistry
snapshot is not a raw iBoot ADT or an independent custom-kernel handoff.
Every offline report retains `physical_execution_verified`, `mmu_verified`
and `exceptions_verified` as false, even when byte validation passes.
