# SSH entropy admission

SSH host-key creation and service startup require both a cryptographic boot
seed and a kernel PRNG adapter that actually incorporates it. A successful
`getentropy()` call, different bytes on successive reads, or a passing statistical
test does not establish either condition.

The audit uses PureDarwin commit
`1bbaeb2ee6aa41e6f792a0b1e2943cc295b3b290` and loader commit
`8295cd89c184785896d007d0554b398d22d8e32f`, with the separately recorded VM
platform overlays. It identified two blockers in the earlier VM image:

1. `src/devtree.c` in the loader used timer-seeded xorshift64 when
   `EFI_RNG_PROTOCOL` was absent or failed. That compatibility fallback supplies
   seed bytes but cannot serve as the cryptographic seed for SSH.
2. `src/Kernel/Extensions/corecrypto/algorithms/cckprng.c` called
   `prngAllowReseed()` after adding a seed to Yarrow, and ignored its result.
   This kernel Yarrow build disables entropy-estimate accounting. Its estimates
   stay zero, and `prngAllowReseed()` returns
   `PRNG_ERR_NOT_ENOUGH_ENTROPY`. The adapter therefore left the output generator
   in its fixed initialization state. The separate default `ccrng()` provider
   also created its own generator without seeding it.

The source regression harness
[`prng_seed_regression.c`](../../runtime/probes/prng_seed_regression.c)
compiled with the unchanged published adapter reproduces identical output from
two deliberately different public seed vectors. These vectors are test inputs
only and never seed an operating-system image.

## Required overlays

Apply [`xnu-loader-ssh-entropy.patch`](../../vm/patches/xnu-loader-ssh-entropy.patch)
after the EFI diagnostics and VirtualBuddy platform patches. Build the SSH image
loader with `-DXNU_LOADER_REQUIRE_EFI_RNG=ON`. It requests 64 bytes through
`EFI_RNG_PROTOCOL`, rejects failure and an all-zero result, and refuses kernel
handoff if the request fails. Logs identify the source and status without
printing the seed. The device tree records `blauos,boot-entropy-source=efi-rng`;
the local seed buffer is erased after copying it into the device tree. XNU
consumes and erases the device-tree seed through `PE_get_random_seed()`.

With the option off, the loader continues to allow its historical development
fallback and explicitly labels that path `development_only`. This profile must
not start SSH or create host keys.

Apply [`puredarwin-ssh-prng.patch`](../../vm/patches/puredarwin-ssh-prng.patch)
to the pinned kernel sources and rebuild `corecrypto.kext`. The overlay:

- Uses the published Yarrow forced-reseed operation for the already admitted
  boot seed and subsequent mixed input, and checks every PRNG operation result.
- Serializes generation, state updates and reseeding with a kernel mutex.
- Retains the XNU entropy callback, consumes its accepted positive sample-count
  result, and refreshes it instead of discarding it after startup.
- Uses the initialized kernel context for the default `ccrng()` provider.
- Exposes read-only `kern.blauos_ssh_entropy_ready=1` only after successful
  initialization of this corrected provider with an EFI RNG boot-source tag.

The existing XNU bootstrap path hashes the 64-byte boot seed before passing it
to the registered provider. The provider rejects an absent or shorter-than-32
byte initialization seed. The new admission value remains zero with an absent
EFI source tag; an older kernel/provider has no such sysctl. The startup wrapper
must treat both cases as refusal. The boot capability probe checks the value and
also verifies that a sysctl write is denied.

The source regression verifies seed influence, accepted callback use,
the default provider's context, concurrent state accounting, and refusal of a
short seed. These checks validate the repaired adapter; they do not measure
entropy quality. The provider remains the legacy published Yarrow implementation,
and this work does not claim production cryptographic certification.

## Boot evidence and remaining trust

Acceptance requires an actual boot with both overlays, the strict loader option,
an EFI RNG success log, a corrected-provider log, and a passing
`entropy_admission` capability check before any host-key generation. Record the
loader, kernel collection, source patches and image hashes with the boot result.
Persist guest-generated host keys on the writable root filesystem; do not embed
shared keys, host-generated seed files or public test vectors in an image.

The VM's firmware and virtualization host remain part of its trust boundary.
An EFI RNG success is admission through a defined firmware entropy interface,
not independent verification of the host's implementation. A root user who can
replace a kernel, boot loader, or disk image can replace this admission mechanism.
The read-only sysctl prevents accidental userspace override of the checked
provider; it is not a verified-boot substitute.

The [UEFI RNG protocol](https://uefi.org/specs/UEFI/2.11/37_Secure_Technologies.html#random-number-generator-protocol)
defines entropy and RNG services, including algorithm selection and errors.
Apple also exposes a
[virtual entropy device](https://developer.apple.com/documentation/virtualization/vzvirtioentropydeviceconfiguration)
for a guest. Its presence alone is insufficient: the firmware must expose it
through the loader's EFI RNG interface, or a guest driver must supply its bytes
to the kernel. The stock VirtualBuddy Linux device
profile does not configure that device. The strict loader was actually booted
with that profile and reported `EFI_NOT_FOUND`, then refused kernel handoff.
With `VZVirtioEntropyDeviceConfiguration` added to the public VM configuration,
the same strict loader reported successful EFI RNG admission and reached XNU.
That experiment establishes firmware access to the virtual entropy source;
the complete SSH image must additionally pass the corrected-provider check.

[`virtualbuddy-linux-entropy.patch`](../../vm/patches/virtualbuddy-linux-entropy.patch)
adds that public device to VirtualBuddy's Linux helper at commit
`088351b0fc67e0b24b83e7954ad48314dda4ce04`. This is a separate development app
profile; the installed stock VirtualBuddy app cannot boot the strict SSH image.
The build helper preserves the source checkout and installed app:

```sh
python3 scripts/blauos.py fetch virtualbuddy-entropy-host
python3 scripts/build/virtualbuddy_entropy.py \
  --source .blauos/sources/virtualbuddy-entropy-host \
  --output-dir .blauos/build/virtualbuddy-entropy
```

It builds an isolated Release app, freezes resolved Swift package versions,
uses a separate `org.blauos.dev.` bundle prefix, disables automatic updates,
and signs locally with the upstream public virtualization entitlement. It does
not launch or install the app. Successful compilation does not establish
actual GUI boot or SSH acceptance; those results need their own boot evidence.

The reproducible entropy-assets helper uses an explicit cached kernel profile:

```sh
python3 scripts/build/ssh_entropy.py \
  --kernel-build .blauos/experiments/virtualbuddy-platform/kernel-v2 \
  --collection-command PATH_TO_ACTUAL_KC_BUILDER_ARGV_JSON \
  --container CALLER_OWNED_RUNNING_NIX_CONTAINER \
  --output .blauos/build/ssh-entropy
```

It rebuilds all 26 corecrypto objects from a clean pinned source copy, applies
the reviewed PRNG patch, and records the cached generated kernel headers, kmod
metadata and libkmod archive. The collection retains the cached XNU kernel and
the other 15 kexts; it binds every reused binary and plist to its hash. The
loader comes from a separate clean source copy with the three reviewed loader
overlays and the strict RNG flag. `assets.json` and `entropy-receipt.json` bind
the resulting kernel collection and EFI loader to those build receipts and the
current source lock. This is not a fresh build of the entire kernel.

The helper was executed successfully: its independently rebuilt corecrypto,
kernel collection and EFI loader were byte-identical to the assets that passed
the real guest SSH tests. `guest_entropy_admission_verified` remains false in a
build receipt until an actual boot supplies the runtime evidence. The boot
capability probe passed all 11 checks on both boots of the same guest disk,
including the read-only admission value, permanent credential dropping,
PTY shell I/O, sandbox resource limits, descriptor passing and the sshd chroot.

Build the probe and run the source adapter regression separately:

```sh
python3 scripts/build/ssh_capabilities.py build \
  --runtime-root PATH_TO_AUDITED_SOURCE_RUNTIME \
  --output-dir .blauos/build/ssh-capabilities
python3 scripts/build/ssh_capabilities.py regression \
  --source .blauos/sources/puredarwin-vm-next \
  --output-dir .blauos/build/ssh-prng-regression
```
