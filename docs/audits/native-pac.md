# Native T8103 pointer authentication source restoration

The older PureDarwin T8103 candidate needs real native PAC key management.
Its selected source only implemented the corresponding paravirtualized path:
the native user-key helpers had empty bodies, context switches skipped native
keys, and startup omitted the published Apple key initialization. A successful
link of that source did not establish usable native PAC behavior.

[Patch 0011](../../patches/native/0011-t8103-public-native-pac.patch) restores
the T8103 source path using public Apple
[`xnu-7195.60.75`](https://github.com/apple-oss-distributions/xnu/tree/b967469df23b10bc70788727627457ebdfe3653f).
The [input receipt](../../evidence/native/native-pac/inputs.json) identifies
the immutable donor commit/tree, complete source-file hashes, and exact
line ranges and hashes of fifteen transplanted blocks. The receiving source
is PureDarwin `1bbaeb2ee6aa41e6f792a0b1e2943cc295b3b290`, with native
patches 0001–0010 already applied. No published source pin was changed.

The restored path includes:

- H13 APSTS/UserKeyEn features and published APCTL/APSTS/KERNKey encodings.
- The real startup master-key-valid poll, AppleMode control, and A, B,
  kernel and generic key initialization before the SCTLR transition.
- User-key enable/restore helpers with genuine register writes, ISBs and
  separate returns, preserving the saved-key calling convention.
- Native B-key context switches and conditional fast A-key context switches.
- Slow A-key restoration before kernel saved-state signing and programming
  before EL0 exception return, with the existing authenticated state retained.
- Default CPU/task diversifiers consistent with the restored startup values.

H13 supports both fast and slow A-key paths. The published implementation
reads `APCTL_EL1.UserKeyEn` at runtime; it does not assume every CPU model or
revision supports that bit. Fast switching programs KERNKey and keeps kernel
A keys separate. Slow switching restores kernel A keys on kernel entry and
programs the process's A keys before returning to EL0. The existing
paravirtualized HVC path remains in its original branch.

Every addition is restricted to `ARM64_BOARD_CONFIG_T8103`. Two small
adaptations preserve the receiving source's interfaces: native ROP programming
uses the macro scratch register for increments so its caller caches the
original key, and the default-key functions stay in the selected
`machine_routines_apple.c` location. The published nonzero ROP diversifier
replaces that profile's previous zero return. No PAC qualifier, CTRR/PPL
protection, saved-state authentication, or PMU operation is disabled.

## Actual object evidence

The [object receipt](../../evidence/native/native-pac/result.json) records
six real ARM64e KER00 objects: startup, exception/vector code, context switch,
assembly key helpers, C default-key selection, and kernel initialization.
They compile using the inactive successful T8103 v4 build's actual generated
make recipes, with isolated patched source and exported-header copies. The
original v4 objects, linked kernel and generated headers are preserved.
The receipt links hashes of every actual compiler dependency and full raw
commands/disassemblies under the ignored build directory.

The [disassembly excerpt](../../evidence/native/native-pac/key-path-disassembly.log)
shows separate key-helper returns, live bit-4 feature tests, APCTL bit-1
transitions, native A/KERNKey writes, and B-key cache updates preserving their
original input. An independent reviewer checked these instructions, startup
poll/key writes, saved-state ordering, and all source/dependency hashes.
Eight actual VIRT/T8010 preprocessing comparisons retain every nonempty
assembly line unchanged; only empty output lines are ignored. These are
preprocessing comparisons, not alternate-profile build or execution tests.

To repeat this object experiment after the recorded v4 base build is present:

```sh
python3 evidence/native/native-pac/replay.py \
  --configured-build .blauos/build/native/kernel/puredarwin-t8103-public-ppl-v4 \
  --base-receipt evidence/native/kernel/puredarwin-t8103-public-ppl-v4/result.json \
  --output-dir .blauos/build/native/native-pac-fresh
```

The output directory must be new and empty. The experiment reconstructs
changed files from the verified PureDarwin source and the base receipt's
immutable patch snapshots, then applies a captured 0011 patch. Untouched
generated configuration/headers remain recorded inputs from the base recipe.
A separate [fresh whole native build](../../evidence/native/kernel/puredarwin-t8103-native-final/result.json)
with all eleven patches now compiles and links successfully. Its candidate
is ARM64e KER00, with UUID `E3ED90E4-1CB3-314B-BAA7-344D3AF6DF0B` and
SHA-256 `173b449882f760161120a9885b51381c322075afda50ffc091b9bad0843bff62`;
the receipt records native objects,
unstripped kernel and source/patch identities. The decoded instructions in
all six final objects match the isolated replay exactly. This establishes a linked
source candidate, not an authorized or executable target BootKC.

## Remaining acceptance

Object compilation establishes instruction generation, not hardware key
semantics. The published static kernel diversifiers are explicitly placeholder
values; the master-key-valid state and per-boot hardware diversification are
firmware assumptions that need observation on the target. Compatibility with
the target boot chain, authorized BootKC, firmware and any proprietary driver
PAC ABI remains unverified. Native runtime transitions, protected memory,
exception return and P/E CPU context switches have not executed on physical
M1 hardware. Gate 2 and issues #29–#35 remain open.
