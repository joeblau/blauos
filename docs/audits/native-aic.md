# Native M1 interrupt-controller experiment

Issue [#34](https://github.com/joeblau/blauos/issues/34) remains open. The
T8103 platform extension compiled and linked from the pinned PureDarwin source;
no interrupt has been delivered on physical M1 hardware in this experiment.

## Source change

[Patch 0007](../../patches/native/0007-t8103-aic-rearm-and-driver-abi.patch)
repairs an existing `PDAppleAIC::handleInterrupt` path. Reading an AIC hardware
IRQ event automatically masks that source. The previous path invoked the
registered handler but did not re-enable an otherwise enabled source afterward.
The revised path orders the acknowledging read, invokes the handler, rechecks
registration and software-disable state, and unmasks the source only when it
remains enabled. It completes controller mask writes before clearing
`interruptActive`.

The hardware behavior comes from the
[Asahi AIC documentation at the exact research revision](https://github.com/AsahiLinux/docs/blob/0d1f8917fa88745d62a4c05d802c4a7298c8616b/docs/hw/soc/aic.md).
The [inspection receipt](../../evidence/native/aic/native-final/inspection.json)
records the retrieved file's SHA-256. The change uses the existing XNU vector
and MMIO interfaces. It does not introduce Linux driver code.

The same patch scopes ARM64e `-mcpu=apple-a14 -mkernel -fno-stack-check` to
the T8103 platform extension and its `libkmod` objects. This produces the
authenticated KER00 kernel ABI used by the native kernel profile. Other
platform profiles retain their existing build configuration.

## Actual build

[The build wrapper](../../scripts/build/native_platform.py) checks the locked
source, requires a fresh output directory, archives the pristine source and
applies immutable byte snapshots of the requested patches. The
[actual final result](../../evidence/native/aic/native-final/result.json)
contains the full commands, loaded wrapper hashes, source archive hash, patch
snapshots and raw-log identities. Its patch set is 0001 through 0011, matching
the final native kernel experiment. This wrapper builds only the platform
extension.

Reproduce in new, empty output and evidence directories:

```sh
python3 scripts/build/native_platform.py \
  --build-root .blauos/build/native/aic/driver-replay \
  --evidence evidence/native/aic/driver-replay \
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
  --jobs 4
```

Configure and build both exited 0. All eight platform and `libkmod` objects
have ARM64e subtype `0xc0000002` (KER00). The 161,080-byte bundle has UUID
`50CF9889-A4D1-3E1B-A466-FA817019495C` and SHA-256
`3a544f93617a9f02f7afec8cd275a386ac00366e3b6eda2556f46f0fee0c45ce`.
Its executable and writable segments are separate and aligned to 16 KiB.
It has no userspace dynamic-library load commands. Actual object disassembly
review also found authenticated callback and vtable operations.

The bundle still has 453 undefined kernel imports. Dynamic lookup permits this
isolated extension to link. An
[actual symbol-name comparison](../../evidence/native/aic/import-availability-native-final.json)
finds all 453 names in both the stripped and unstripped final source-built native
kernel. Name availability does not establish export-policy permission,
authentication fixups, ABI compatibility or collection linkage. Import
resolution during native collection construction remains a separate requirement.
No native kernel collection, firmware authorization or boot is established by
this result. The wrapper always leaves
native boot readiness and physical gate flags false.

## Remaining physical acceptance

The vector handling retains XNU's software-disable concurrency semantics.
A concurrent disable after the post-handler recheck can leave one pending
event, which must be acknowledged and masked without another disabled-handler
callback. Callback self-unregistration is not promised by this change; the
inherited unregister path has its own locking and active-vector behavior.

Real controller discovery and resource mapping, repeated device interrupts,
disabled-source behavior, spurious events, bounded interrupt storms, affinity,
per-CPU routing and IPIs require physical tests. Architectural timer delivery
also needs its actual FIQ path tested. No synthetic interrupt tests or compile
results satisfy those criteria. The broader CPU startup and boot integration
gaps remain tracked in [the milestone record](../milestone-2.md).
