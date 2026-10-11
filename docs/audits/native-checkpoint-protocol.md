# Native M1 physical checkpoint protocol emitter

Tracks [#29](https://github.com/joeblau/blauos/issues/29) and the software
side of [#39](https://github.com/joeblau/blauos/issues/39). The
[physical evidence contract](../platform/m1-checkpoint.md) previously stated
that no source emitted the full checkpoint protocol.
[Patch 0017](../../patches/native/0017-t8103-physical-checkpoint-protocol.patch)
(SHA-256 `e6a407fe9cceb706968a8f5b60558d1c730f11fc387da5330f015aa2b78732b2`)
now implements the emitter in the pinned PureDarwin T8103 kernel and the
AIC platform extension. This is compile-time software evidence: nothing here
ran on physical hardware, no UART byte was captured, and Gate 2 remains open.

## Opt-in boundary

Every hook is inert unless the boot arguments carry an exact
`blauos-checkpoint-nonce=<64 lowercase hex>` token. Without the token the
kernel behaves as before; the emitter never changes default boot behavior.
The nonce scanner reads the raw `CommandLine` bytes because
`PE_parse_boot_argn` integer-converts digit-leading values.

## Implementation sites

The emitter (`osfmk/arm/blauos_checkpoint.c`, declarations in
`osfmk/arm/blauos_checkpoint.h`) measures events at their real sites rather
than reporting constants:

| Event | Site | Measurement |
| --- | --- | --- |
| `entry` | `arm_init`, immediately after `STARTUP_SUB_KPRINTF` | Live PC read by `adr` inside the dedicated symbols, kernel UUID from `_mh_execute_header`, `vm_kernel_slide`, ADT `model` and `/chosen/chip-id` |
| `handoff` | `PE_init_platform(FALSE)` after `pe_identify_machine` | Self-contained SHA-256 of the received iBoot `boot_args` bytes, the ADT bytes (bounded at 16 MiB), and the derived memory plan |
| `mmu` | `arm_vm_init` after page tables are live | `CurrentEL`, `SCTLR_EL1`, `TCR_EL1`, `TTBR0_EL1`, `TTBR1_EL1`, plus a translation readback comparing `pmap_find_phys(kernel_pmap, …)` with `ml_static_vtop` on a written sentinel |
| `exception` | `sleh_synchronous` `ESR_EC_BRK_AARCH64` kernel path | The controlled `brk #0xB105` probe armed only during finalize; records real `ESR`/`ELR`/`FAR` and resumes after the instruction. Every other BRK keeps the panic-by-design path |
| `timer` | `rtclock_intr` delivery counter + finalize one-shot | Real `CNTFRQ_EL0`/`CNTVCT_EL0` values and the count of delivered timer interrupts within a one-second bound; zero delivery is reported as-is |
| `interrupt` | `PDAppleAIC::handleInterrupt` after a registered handler runs | First genuinely delivered external interrupt with its IRQ number; bounded and emitted once |
| `cpus` | `arm_init_cpu` online point | Bitmask of CPUs that actually reached online state, observed with a five-second bound; shortfalls are reported, not hidden |
| `checkpoint` | `kernel_bootstrap_thread` after `bsd_init` | `controlled-stop`, then halt through the registered `PE_halt_restart` callback |

The dedicated labels `_blauos_physical_checkpoint_entry` /
`_blauos_physical_checkpoint_entry_end` delimit a two-instruction (8-byte)
region, well under the 4096-byte contract bound. The emitter refuses to emit
the entry event if the region or measured PC violate the contract.

## Build identity

The build wrapper gained an opt-in
[`--build-id`](../../scripts/build/xnu.py) flag (64 lowercase hex, native
T8103 PureDarwin profile only) that forwards
`-DPUREDARWIN_BLAUOS_BUILD_ID=<hex>` through CMake to the kernel make build as
the `BLAUOS_PHYSICAL_BUILD_ID` preprocessor define. A build without the flag
carries no marker and must fail offline validation. The provisioned identity
`4fa89c0d292d7d75c767687fe3ec056b519e2b74d8896c84622f91a46b5d5b99` is the
SHA-256 of the canonical source profile (`puredarwin-xnu-7195`, revision
`1bbaeb2ee6aa41e6f792a0b1e2943cc295b3b290`) and the sorted
`{label, role, sha256}` records of the instrumentation patch and recipe
wrapper, matching `physical_evidence.py::source_identity`.

## Build and inspection results

The full 17-patch series builds clean:

- Kernel (`evidence/native/kernel/puredarwin-t8103-native-17/result.json`):
  configure and build exit 0. Unstripped `kernel.development.t8103`:
  18,008,512 bytes, SHA-256
  `358ebdf64e0fe339cffe4d0c767fd38663d0948bab82eca28e10b085cc3fd523`, UUID
  `CD37C6C4-1CBD-3122-B636-C3C3B311CD64`, ARM64e KER00 MH_EXECUTE.
- Platform extension (`evidence/native/platform-17/result.json`):
  `PDArmPlatformExpert` MH_KEXT_BUNDLE ARM64e KER00, 166,320 bytes, SHA-256
  `4158ea904ccac316f0f1c95c7e26e4a09624f296a19c60928ce28c1ee9cebb7e`, UUID
  `B43BA1D4-4251-3CE9-B1FA-FDEF70D2F32D`.

[Kernel inspection](kernel-inspection.json) of the unstripped image with the
validator's own `inspect_kernel` confirms both checkpoint symbols, an 8-byte
region, and exactly one NUL-terminated
`BLAUOS_PHYSICAL_BUILD_ID=4fa89c0d…5b99` marker.

Two earlier build attempts failed on real compile errors (a missing prototype
in `pe_init.c`, a BSD `libkern.h` include conflict, an undeclared
`PE_halt_restart` after the include cleanup) and remain preserved under
`evidence/native/kernel/puredarwin-t8103-native-17-failed-1/` and
`-failed-2/`; an unprovisioned 17-patch build (exit 0, no marker) is kept in
`puredarwin-t8103-native-17-unprovisioned/`.

## Validator agreement (synthetic)

`evidence/native/checkpoint-protocol/generate_synthetic.py` builds a clearly
labeled SYNTHETIC bundle whose serial payload is the exact format the emitter
produces, bound to the actual built kernel (UUID, symbol addresses, build-ID
marker). The offline validator accepts it at both levels:
[entry](entry-validation.json) and
[checkpoint](checkpoint-validation.json) both exit 0 with
`evidence_consistency_passed: true`. As with every synthetic fixture,
`hardware_attestation_verified` and `physical_gate_passed` remain false and
`review_required` remains true.

## What remains physical

The emitter satisfies the software side of the contract only. Still required
and unverified: a working native polling UART path on the target, actual
`BLAUOS_PHYSICAL` bytes from the independent kernel on a dedicated Macmini9,1,
real nonce delivery through the target's boot-argument policy, cold/warm
capture sessions, panic/recovery behavior, and the named Gate 2 review. A
format-consistent synthetic log is not a physical checkpoint.
