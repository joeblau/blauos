# Native M1 interrupt-controller hardening

Issue [#34](https://github.com/joeblau/blauos/issues/34) remains open. This
audit closes the remaining source gaps of the `PDAppleAIC` driver identified
in the [platform gap record](native-platform-gaps.md). It builds on the
[interrupt-controller experiment](native-aic.md), whose patch 0007 repaired
post-event re-arming. No interrupt has been delivered on physical M1 hardware
in this experiment.

## Source change

[Patch 0016](../../patches/native/0016-t8103-aic-hardening.patch) (SHA-256
`cafdcedad82c691ed3207be1e79cc793ceda75479c6e68694819f19df35b30ee`) touches
only
`src/Kernel/Extensions/PDArmPlatformExpert/PDAppleAIC.cpp` of the pinned
PureDarwin `1bbaeb2ee6aa41e6f792a0b1e2943cc295b3b290` tree and applies
cleanly after native patches 0001–0011 (`git apply --check` verified in a
fresh worktree prepared by [scripts/patches.py](../../scripts/patches.py)).
It implements four hardening changes:

- **Resource bounds validation.** `aic_find_registers` now requires complete
  three-cell `ranges` entries (no truncation, no trailing partial entry) and
  whole two-cell `reg` entries. It rejects a zero-size register window, a
  window that escapes the arm-io range size, and a wrapping physical-base
  addition. After reading the controller info register, `initAIC` computes
  the highest register offset it will touch — the event register, the
  per-source target registers and both mask banks sized by the reported
  source count — and fails initialization when the mapped span does not
  cover it.
- **Per-phandle controller name.** The driver's own comment described the
  `IOInterruptController%08X` name that IODeviceTree builds from the
  controller node's phandle
  (`IODeviceTreeSupport.cpp::IODTInterruptControllerName`), but only the
  default name was registered. `aic_find_registers` now reads the node's
  `AAPL,phandle`, and `initAIC` registers the per-phandle name in addition
  to the default registration, which remains the fallback for nubs with no
  explicit interrupt parent.
- **Bounded runtime dispatch.** `handleInterrupt`'s event loop is now
  bounded by the reported source count. Each event read acknowledges (and
  masks) its source, so one dispatch cannot legitimately see more distinct
  external sources than exist; the bound also covers interleaved
  non-external events. Exceeding the bound logs and returns with events
  pending instead of looping without limit.
- **Initialization failure propagation.** The `cpuIC->enableInterrupt`
  return is now checked; failure rolls back the registration and fails
  initialization instead of continuing. Publishing under both controller
  names moved after all fallible setup, so a failed init no longer leaves a
  half-initialized controller reachable by name; a name-allocation failure
  disables and unregisters the CPU interrupt before failing.

The change uses the existing XNU registry, MMIO and interrupt-controller
interfaces. It does not introduce Linux driver code.

## Actual build

The [build wrapper](../../scripts/build/native_platform.py) ran with the
eleven-patch profile of the final kernel plus patch 0016, using the SDK at
`/Applications/Xcode-27.2.0-Beta.app`. The
[actual result](../../evidence/native/platform-aic-hardening/result.json)
records the full commands, wrapper hashes, source archive hash, immutable
patch snapshots and raw-log identities.

Reproduce in new, empty output and evidence directories:

```sh
python3 scripts/build/native_platform.py \
  --build-root .blauos/build/native/platform-aic-hardening \
  --evidence evidence/native/platform-aic-hardening \
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
  --patch patches/native/0016-t8103-aic-hardening.patch \
  --jobs 4
```

Configure and build both exited 0. The 161,408-byte bundle has UUID
`361845FA-4A95-3A87-AD35-14BB19F87E87` and SHA-256
`d2d9f87125d424f1e06025398074817e069723cc12a3709669e5dc18f73eadc2`. The
wrapper's driver-metadata checks passed: ARM64e subtype `0xc0000002`
(KER00), `MH_KEXT_BUNDLE`, no writable executable segment, no userspace
dynamic-library load commands. The only compiler warnings in the driver file
are the pre-existing sign-conversion notes on the patch-0007 and original
lines; the new code adds none.

## Remaining physical acceptance

This is a source and link audit, not an interrupt delivery test. Real
controller discovery against the target ADT (including whether the actual
`ranges`/`reg` values pass the new validation), repeated device interrupts,
disabled-source behavior, spurious events, bounded interrupt storms,
affinity policy, per-CPU routing and IPI interaction all require physical
tests; none is claimed here. Explicit interrupt-parent nubs still need
reconciliation with the actual registry and resource construction on the
target. Native import resolution and kernel-collection linkage remain as
recorded in the [interrupt-controller experiment](native-aic.md). The
broader CPU startup and boot integration gaps remain tracked in
[the milestone record](../milestone-2.md).
