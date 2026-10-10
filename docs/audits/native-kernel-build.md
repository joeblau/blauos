# Native M1 kernel and boot-asset experiment

Issue #27 remains open. A genuine DEVELOPMENT T8103 ARM64e kernel now compiles and links from the reviewed public-source experiment. No native boot collection has been produced or locally authorized, and no project kernel has executed on an M1. The available host is an M3 Max; the requested target is an M1 Mac mini, `Macmini9,1` / `J274AP` / `T8103`. These are distinct hardware profiles in the [Asahi device list](https://asahilinux.org/docs/hw/devices/device-list/).

The source audit and experiments use the existing lock, SHA256 `8022bbbfb33204b56bb3297cd8cbb39fb6c08ea65c476492d99462f7edfe3657`. Apple XNU is `xnu-12377.121.6`, revision `ac9718fb1af618d5ce8678d0dc6e8a58f252216f`. The pinned PureDarwin tree with a verified VIRT build is revision `1bbaeb2ee6aa41e6f792a0b1e2943cc295b3b290`; its kernel reports Darwin 20.5.0. Their source profiles are recorded separately in [source-audit.json](../../evidence/native/kernel/source-audit.json).

The observed build host is macOS 27.2 (`26B5101f`), Xcode 27.2 beta, Clang 21.0.0. The independently verified SDK content fingerprint is `1fd70cc3700dc127c65cde0fcd2f248f280d6562768ca67b3930d1eda2bebc14`. [host.json](../../evidence/native/kernel/host.json) records the actual tool hashes. This host SDK is an experimental input, and has not been matched to the unavailable target's installed firmware/OS. No KDK is installed.

## Actual builds

Fresh public-header generation succeeded with 3,426 installed files. Every generated path/hash matches the earlier independent baseline. The full inventory remains under the ignored build directory; its hash, count and comparison are preserved in [headers/result.json](../../evidence/native/kernel/headers/result.json).

```sh
python3 scripts/build/headers.py \
  --build-root .blauos/build/native/kernel/headers \
  --evidence evidence/native/kernel/headers --jobs 4
python3 scripts/build/xnu.py --machine T8103 \
  --sdk .blauos/build/native/kernel/headers/MacOSX.sdk \
  --build-root .blauos/build/native/kernel/apple-t8103-platform \
  --evidence evidence/native/kernel/apple-t8103-platform \
  --make-setting 'WARNFLAGS_STD=-Wall -Wextra -Wno-missing-include-dirs' \
  --make-setting PLATFORM=MacOSX --jobs 4
```

The modern native attempt compiled 49 genuine T8103 objects, then failed on unavailable `TrustCache/API.h` and `CoreEntitlements/V2/API.h`. No linked native kernel was produced. The first attempt's SDK platform lookup noise was resolved by the explicit MacOSX setting; the remaining failure is preserved in [apple-t8103-platform/result.json](../../evidence/native/kernel/apple-t8103-platform/result.json) and its build excerpt. The pinned make system subsequently requires the matching KDK platform support archive, including `KernelSupport/libT8103.os.DEVELOPMENT.a`. The separate [public security-input audit](native-security-inputs.md) checks the matching release catalog, full Security/PCC trees and historical headers; none establishes compatible current V2 layouts or implementations.

PureDarwin's generated make template omits T8103 from its supported target list, although the existing kernel contains T8103 board configuration, H13, PPL and Apple AMP code. [0001-puredarwin-t8103-build-profile.patch](../../patches/native/0001-puredarwin-t8103-build-profile.patch) adds a narrow experimental native profile: `arm64e`, `apple-a14`, 16K pages and native large-memory geometry. It preserves PMU, PPL and kernel integrity, and uses the genuine MacOSX master configuration. It does not relabel the proven VIRT kernel.

The first genuine older native build configured successfully, then the current assembler rejected older Apple symbolic register names. [0002-t8103-public-register-encodings.patch](../../patches/native/0002-t8103-public-register-encodings.patch) replaces those spellings with published numeric encodings only for T8103. Reads, writes and barriers retain their meaning. The 56 encodings, including all sixteen macro-selected uncore counters, and primary research pins/file hashes are in [register-encodings.json](../../evidence/native/kernel/register-encodings.json). An independent review compiled and decoded 112 actual ARM64e register instructions and the source's macro-generated register builtins. Studying m1n1's register definitions did not activate the fallback boot path.

The retry passed compilation and reached a genuine `arm64e.kernel` link. Its unresolved native definitions were traced to the MacOSX master configuration omitting the public `nos_arm_pmap` / `nos_arm_asm` options; these select the actual published pmap, entry, context switch and platform routines. [0004-t8103-public-platform-sources.patch](../../patches/native/0004-t8103-public-platform-sources.patch) enables those sources only for T8103 and compiles the source firehose archive with the matching native kernel architecture. It does not invent replacement implementations. A small warning-only patch retains ordinary current-Clang diagnostics, including the configured `BUILD_WERROR` policy, without millions of unrelated reserved-identifier warnings. Selecting the public platform sources exposed a genuine private `memmap_types.h` dependency in the CTRR implementation. A deletion-only experiment failed because that header supplies register aliases and protection bits. [0005-t8103-public-ctrr-interface.patch](../../patches/native/0005-t8103-public-ctrr-interface.patch) instead supplies the eight published M1 CTRR register encodings and the existing write-protect/PXN mask from pinned Asahi documentation; it preserves every read, write, EL check, lock, TLB invalidation and barrier. [ctrr-interface.json](../../evidence/native/kernel/ctrr-interface.json) records the primary source hash and bit definitions. This intermediate five-patch attempt remained incomplete; the final linked experiment below includes the later PPL and PAC restorations.

```sh
python3 scripts/build/xnu.py --puredarwin --source-name puredarwin-vm-next \
  --machine T8103 \
  --patch patches/native/0001-puredarwin-t8103-build-profile.patch \
  --patch patches/native/0002-t8103-public-register-encodings.patch \
  --patch patches/native/0003-t8103-current-clang-diagnostics.patch \
  --patch patches/native/0004-t8103-public-platform-sources.patch \
  --patch patches/native/0005-t8103-public-ctrr-interface.patch \
  --build-root .blauos/build/native/kernel/puredarwin-t8103-ctrr-interface \
  --evidence evidence/native/kernel/puredarwin-t8103-ctrr-interface --jobs 4
```

Selecting those public sources exposed further published omissions in the native PPL path. [0008-t8103-public-ppl-port.patch](../../patches/native/0008-t8103-public-ppl-port.patch) restores 25 source-bound blocks from official Apple XNU `xnu-7195.60.75`, revision `b967469df23b10bc70788727627457ebdfe3653f`, tree `2fd24b44e4468b7a60261620cccca97bdc7262fa`. These include the APRR permission map, page-permission conversions, protected entry/return, exception checks, PAC-authenticated handler dispatch, bootstrap/reset initialization and CTRR locking. [ppl-port-inputs.json](../../evidence/native/kernel/ppl-port-inputs.json) records each copied block's original path, line and hash. The donor calls an unpublished `PPL_MODE`; the selected spill macro only distinguishes signed normal state from unsigned hibernation state, so the port explicitly uses the existing signed `KERNEL_MODE` path.

Three trust-cache selectors are also unpublished. The experimental source-local BlauOS contract appends IDs 77–79, retains existing IDs 0–76, and uses one header for the generated wrappers, 80-entry table and assembly bounds. These are **new project-local IDs, not verified stock Apple driver IDs**. Their compatibility with proprietary drivers is unknown. [0009-t8103-pac-atomic-storage.patch](../../patches/native/0009-t8103-pac-atomic-storage.patch) resolves current Clang's rejection of address-discriminated pointer atomics by comparing/storing the already signed pointer bits. The field's PAC qualifier, signing key, exact address/discriminator and relaxed ordering remain intact. Actual linked native instructions show `PACDA`, the 64-bit `CAS`, and authenticated reads with `AUTDA` plus an authentication-failure trap, preserved in [actual-pmap.json](../../evidence/native/kernel/pac-atomic/actual-pmap.json). A separate four-alias [machine register patch](../../patches/native/0010-t8103-machine-register-encodings.patch) preserves the source's CPU/HID/VMSA register operations; eight actual instructions were compiled and independently decoded against the published encodings.

The fresh `puredarwin-t8103-public-ppl-v4` experiment configured and built successfully. It produced a 15,095,024-byte ARM64e kernel, SHA256 `ebcc31d919f88ed3358d6bc38393349ab8f3b33a2f32808aedaced28bcbd23ac`, UUID `9B0E1920-0B14-3A66-9EE5-2830064699C6`, with file-backed executable entry PC `0xfffffe00073e8530`. A real unstripped kernel and dSYM were also produced. The [linked native proof](../../evidence/native/kernel/puredarwin-t8103-public-ppl-v4/linked-native-proof.json) binds the exact recipe, eight native objects, protected segments, aligned trampoline and linked selectors. No undefined kernel symbols remain. Initial PPL segment header permissions are RW; the compiled startup/pmap/APRR code must apply runtime restrictions. Those restrictions have not been exercised on hardware.

That historical linked candidate has a concrete native PAC lifecycle gap: its `ml_enable_user_jop_key` and `ml_disable_user_jop_key` labels alias the same address because their bodies are guarded for paravirtualized PAC only. Its original artifact and receipt remain preserved. [0011-t8103-public-native-pac.patch](../../patches/native/0011-t8103-public-native-pac.patch) restores the published H13 startup, default diversifiers, key programming, user/kernel exception transitions and context switch paths from the same official donor. It preserves both fast and slow key switching selected by the actual APCTL feature bit. [Native PAC source and instruction evidence](../../evidence/native/native-pac/result.json) binds fifteen exact donor blocks and six genuinely compiled native objects; eight VIRT/T8010 assembly comparisons remain byte-identical. Runtime behavior of these native paths is still unverified.

The final fresh `puredarwin-t8103-native-final` build applies all eleven patches in [series](../../patches/native/series). Configure and build both exited zero. The [build receipt](../../evidence/native/kernel/puredarwin-t8103-native-final/result.json) records recipe SHA256 `fa6082498e8113839969b0e9872b313d43a0d10c75dcc76e78b3c17fe2afbe7c`, the loaded wrapper/helper hashes, exact SDK/compiler inputs, commands, log hashes and retained patch snapshots. It produced a 15,095,024-byte native kernel, SHA256 `173b449882f760161120a9885b51381c322075afda50ffc091b9bad0843bff62`, UUID `E3ED90E4-1CB3-314B-BAA7-344D3AF6DF0B`, and entry PC `0xfffffe00073e8530`. The [final linked proof](../../evidence/native/kernel/puredarwin-t8103-native-final/linked-native-proof.json) verifies nine actual ARM64e KER00 objects and zero unresolved kernel symbols. Its disassembly contains PAC-authenticated PPL dispatch, the source-local selector wrappers and distinct native JOP helpers at `0xfffffe00073f1218` and `0xfffffe00073f1284`, with real fast/slow key writes, APCTL updates, barriers and returns. The linked early diagnostic helper reads actual EL/MMU/timer registers. These are compile and inspection results, not physical observations.

The matching source platform extension also builds as ARM64e KER00. All 453 imported symbol names occur in the final stripped and unstripped kernel, recorded in [import-availability-native-final.json](../../evidence/native/aic/import-availability-native-final.json). Symbol availability does not establish private driver ABI compatibility or collection link success. The target's complete driver inventory remains unavailable.

The successful final build used this command with fresh output directories:

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
  --build-root .blauos/build/native/kernel/puredarwin-t8103-native-final \
  --evidence evidence/native/kernel/puredarwin-t8103-native-final --jobs 6
```

Every patched experiment starts from a fresh Git archive in its own ignored overlay. The pristine locked trees remain unchanged. Current receipts bind the loaded wrapper/helper hashes, sanitized tool environment, exact commands, source archive and exclusively snapshotted patch bytes. Failed attempts and their raw-log hashes remain available; [patch-snapshots.json](../../evidence/native/kernel/patch-snapshots.json) binds historical variants, including the abandoned include-deletion experiment. A failed build is never promoted to a boot asset.

## Native collection and handoff contract

The selected source-documented native format is an `arm64e` `MH_FILESET` BootKC with `com.apple.kernel`. The modern kernel's [`arm_slide_rebase_and_sign_image`](https://github.com/apple-oss-distributions/xnu/blob/ac9718fb1af618d5ce8678d0dc6e8a58f252216f/osfmk/arm/arm_init.c) handles fileset relocation and pointer signing. The [pinned Apple README](https://github.com/apple-oss-distributions/xnu/blob/ac9718fb1af618d5ce8678d0dc6e8a58f252216f/README.md) documents `kmutil create` using a self-built kernel and the actual machine's driver inventory. The example command's T8101 must be replaced with the target table's T8103. The README requires collection generation on the machine being booted.

Before packaging, acquire the target's actual OS/build, paired firmware, driver bundle identifiers, dependency metadata and UUIDs. Inventory every input binary/plist with origin and hash. The documented path uses target Apple drivers plus KDK support; those proprietary inputs would need separate accounting against the independent-system requirement. The thirteen QEMU drivers are not a native M1 inventory. No local signing, recovery, boot-policy, NVRAM or disk operations were executed in this experiment. Apple's [startup disk security documentation](https://support.apple.com/guide/security/sec7d92dc49f/web) describes local authorization of a custom BootKC under the appropriate policy; actual target authorization remains untested.

Both pinned kernels declare boot-argument version/revision 2/2, but their ARM64 structures differ: modern XNU uses a 1,024-byte command line and 1,152-byte structure; the older kernel uses 608 and 736 bytes. A version/revision tuple cannot select the layout. Bind the header hash and source revision explicitly, as in the [handoff contract](../platform/m1-handoff.md). Current target iBoot compatibility has not been measured.

The existing `PDAppleARMPlatformExpert` does match `AppleARM` and invokes `PDAppleAIC_init`; native interrupt dispatch is present. Its actual limits include one interrupt-controller CPU, `PDArmCPU::startCPU` returning `KERN_FAILURE`, ignored non-external AIC events, and initialization failure that only logs a warning. P0 timer/IPI/SMP behavior requires physical validation and further implementation.

The read-only inspector rejects malformed ranges, duplicate UUIDs, missing file-backed entry instructions, VIRT/stock provenance and recipes that override native source/profile/SDK inputs. Fifteen synthetic negative tests pass without fetching sources or requiring an Apple SDK. The [final source audit](../../evidence/native/kernel/puredarwin-t8103-native-final/source-audit.json) binds the chosen older source profile. Inspecting the actual final raw kernel with `--require-native` exits 2: pinned native source provenance verifies, but the artifact is `MH_EXECUTE`, not a BootKC. [boot-asset-negative.json](../../evidence/native/kernel/puredarwin-t8103-native-final/boot-asset-negative.json) retains that result. Actual macOS `kmutil inspect` of the existing VM fileset exited 71 because `_PrelinkKCID` is missing, preserved in [vm-collection-negative](../../evidence/native/kernel/vm-collection-negative/input.json). No native collection exists to inspect yet.

[Early native diagnostics](../platform/m1-early-diagnostics.md) now compile as genuine T8103 code after `kernel_startup_initialize_upto(STARTUP_SUB_KPRINTF)`. The opt-in helper validates a fresh `hex:` boot nonce and reads the actual UUID, PC, CurrentEL, EL1 MMU and virtual timer registers. It remains a partial diagnostic protocol: no native log has been captured, and mapping tests, exception recovery, timer/IRQ delivery and SMP observations remain outstanding. Existing VM logs and offline synthetic tests cannot stand in for those measurements. Gate 2 remains **not run**.
