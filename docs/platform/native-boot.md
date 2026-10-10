# Native custom-XNU boot experiment

Tracks [#26](https://github.com/joeblau/blauos/issues/26) and
[#28](https://github.com/joeblau/blauos/issues/28). Path A is the first physical
experiment. No dedicated M1 machine, target recoveryOS, policy change, native
boot-object acceptance or independently built physical kernel execution has
been observed. The [lab inventory](../../evidence/native/lab/build-host-inventory-redacted.json)
identifies the current M3 Max build host. The successful Phase 0 QEMU guest is
a separate platform. See the [lab runbook](m1-lab.md) before any target action.

Apple documents per-installed-OS security policies. Reduced Security permits
Apple-signed kernels and user-authorized third-party kexts in an Auxiliary
Kernel Collection. Permissive Security permits locally Secure Enclave-signed
boot objects, including custom XNU Boot Kernel Collections, while signatures
remain checked in the boot chain. Significant policy downgrades require a
physical recovery gesture and owner authorization. Apple explicitly does
not provide or support custom XNU kernels. This establishes an architectural
capability, not compatibility of our build or a supported distribution.
[Apple policy reference](https://support.apple.com/guide/security/sec7d92dc49f/web).

Keep three objects distinct: the independently linked kernel, its Boot Kernel
Collection and drivers, and the custom boot object supplied to `configure-boot`.
An AuxKC authorization for third-party drivers does not replace the kernel.
An accepted custom object might be a shim rather than XNU. Its authorization
does not prove that its entry point ran, or that any included driver worked.
The [native asset inspector](../../scripts/build/native_m1.py) records the
source profile and actual Mach-O information without declaring signing or
physical readiness.

Apple documents LocalPolicy `coih` as a **SHA384 of the CustomOS Image4
manifest**, changed by `kmutil configure-boot` in 1TR. This is a different
measurement from our SHA256 of a payload file. `smb1` permits locally signed
custom kernels and requires `smb0`; `smb2` permits an AuxKC and is the Reduced
Security kext path. `vuid` identifies the selected APFS volume group; it is
not a hardware identity or itself a security boundary. Preserve actual
policy captures and the selected volume group privately before and after
the experiment, then publish reviewed measurements.
[Apple LocalPolicy fields](https://support.apple.com/guide/security/secc745a0845/web).

| Environment | OS, firmware and tooling | Observed result | Required follow-up |
| --- | --- | --- | --- |
| Current build host, Mac15,8/M3 Max | macOS 27.2; installed `kmutil configure-boot` help; `bputil` v0.1.14 | Read-only help and inventory inspected; no recovery execution | Capture target tooling separately; host help does not establish recovery compatibility |
| Dedicated Macmini9,1/J274/T8103 | Target OS, paired recoveryOS, firmware and volume-group identity unavailable | No policy or native boot experiment | Select and verify the lab, build a genuine native candidate, then record each tested combination |
| Asahi stage-1 reference | macOS 12.1 OS/stub or later with paired 1TR | Upstream documents raw m1n1 installation | This is a reference for m1n1, not a tested XNU recipe |
| Physical repeatability | Cold/warm sessions and recovery results unavailable | No native checkpoint | Preserve fresh per-boot challenges, exact artifacts and physical serial capture |

The installed `/usr/share/man/man8/kmutil.8`, inspected read-only on the build
host, says to select Reduced Security in Recovery first; `configure-boot`
then prompts to move the selected OS to Permissive Security. The installed
`kmutil help configure-boot` identifies the command as recovery-only and
accepts a Mach-O object, optional compression, or a raw file with explicit
lowest virtual address and entry virtual address. Those values must come
from the actual image's documented entry contract. The raw entry value 2048
in Asahi's example belongs to m1n1 and must not be copied to XNU.
[Asahi m1n1 installation guide](https://asahilinux.org/docs/sw/m1n1-user-guide/).

The following commands are read-only discovery for the selected target. Keep
volume identifiers, raw policy data and credentials out of public logs.
Repeat help inspection inside the actual paired recoveryOS: build-host
versions can differ.

```sh
sw_vers
uname -r
kmutil help configure-boot
bputil -h
diskutil apfs listVolumeGroups
# Replace with the selected test OS volume-group UUID; display only.
bputil --display-policy --json --vuid REPLACE_WITH_TEST_VOLUME_GROUP_UUID
```

From shutdown, holding power reaches paired recoveryOS; double-pressing and
holding power selects fallback recoveryOS. These are different environments.
Record how the operator entered recovery and which OS it is paired with.
[Apple boot modes](https://support.apple.com/guide/security/sec10869885b/web).
Asahi documents that custom installation needs paired 1TR and machine-owner
authentication. It also describes XNU's handoff: no EFI services, relocatable
images, boot arguments, memory layout, Apple device tree and firmware pointer
chains. Device-tree and coprocessor firmware interfaces are version-sensitive.
Capture the actual bytes and select an explicit source ABI rather than
assuming compatibility from a version number alone.
[Asahi platform introduction](https://asahilinux.org/docs/platform/introduction/).

Only after the lab, native source build, package inspection and exact selected
OS are verified, the operator's proposed Mach-O experiment is:

```sh
# Target paired 1TR only. This changes that OS's policy and boot assets.
# Both variables must identify the reviewed artifact and disposable test OS.
kmutil configure-boot --custom-boot-object "$BLAUOS_NATIVE_BOOT_OBJECT" \
  --volume "$BLAUOS_TEST_SYSTEM_VOLUME"
```

This command has **not been executed by the project**. Record its exact
arguments, target recovery/tool versions, exit status, authorization result,
payload hash, resulting `coih` and serial output. Do not add global SIP,
CTRR, boot-argument or SSV downgrades from unrelated debugging recipes.
If a specific additional setting is required, record the observed failure,
source rationale and minimal tested policy. Authenticate interactively;
never put owner passwords in command-line arguments or evidence.

The build-host `bputil -h` says `--permissive-security` and
`--reduced-security` recreate LocalPolicy with only explicitly requested
settings. Therefore a bare `bputil -n` is not a verified reversal command.
Do not assume that copying a saved policy file restores a SEP-authorized
policy. For reversal, first select the separately preserved known-good
macOS from startup options. For the test OS, restore its original Apple-signed
boot assets using that OS's verified recovery tooling, then restore its
intended policy through Startup Security Utility or the verified Full
Security operation. Capture the resulting policy and a successful known-good
boot. The exact reversal remains untested until the M1 lab is available.

If ordinary recovery fails, Apple currently specifies a second Mac running
macOS 14 or later, Finder, Internet access and a USB-C data/charging cable
(not Thunderbolt 3). Try a firmware revive before a restore: revive preserves
data; restore erases the machine. The backup and recovery operator must be
established before the first policy change.
[Apple revive/restore procedure](https://support.apple.com/en-us/108900).
On the M1 Mac mini the DFU port is the leftmost USB-C port while facing the
rear. Recheck the exact model table if the target changes.
[Apple DFU port identification](https://support.apple.com/en-us/120694).

Remaining compatibility questions are experimental: matching KDK/private
build interfaces; native collection format and required drivers; the target
iBoot's accepted entry/layout; source-profile handoff alignment; firmware
driver ABI; boot-argument filtering and UART availability; and restoring the
selected test OS. Publish precise blockers and follow-ups when observed.
Missing hardware access alone does not justify switching to m1n1. A native
failure or a concrete debugging need can trigger a separately reviewed
fallback experiment; its shim, translation layer and execution mode must
remain explicit in the evidence.
