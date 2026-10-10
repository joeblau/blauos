# Pinned build inputs and experiments

Use Python 3 and Git for source validation/fetching. The macOS experiment profile
records the exact host, Apple compiler/linker/MIG tool hashes, and SDK content in
`toolchain.lock.json`. The Linux virtual-build route uses the pinned PureDarwin
flake and container inputs documented in [virtualization](virtualization.md).

```sh
python3 scripts/blauos.py validate-lock
python3 scripts/bootstrap.py host --verify-sdk
python3 scripts/blauos.py fetch --profile puredarwin-vm
python3 scripts/patches.py check
```

The host bootstrap creates ignored workspace directories and compiles an ARM64
object without changing the selected Xcode, installing a KDK, altering a system
SDK, or changing boot policy. Its success proves the host compiler works; it does
not prove an independent kernel or runtime. A mismatched compiler, SDK, or host
version is an error rather than an implicit toolchain refresh.

Fetches resolve full immutable commits and verify Git tree identities and origin
URLs. Existing changed checkouts are preserved and rejected. `--sources-dir`
supports a fresh reconstruction; `--report` writes the verified identities and
lock checksum. Large sources and generated artifacts remain under `.blauos/`.
The LLVM compiler-runtime source is a locked sparse checkout: the allowed
prefixes are in the manifest, and its full commit/tree identities remain pinned.

## Two distinct source profiles

`upstream-modern` records a coherent published Apple XNU/runtime distribution for
dependency auditing and the first make-system attempt. Its public SDK is not a
complete replacement for the matching ARM64 KDK and private kernel interfaces.
See the [source audit](audits/source-dependencies.md) and
[kernel build evidence](audits/kernel-build.md).

`puredarwin-vm` pins a newer PureDarwin development snapshot with an ARM64 VIRT
target. This is a candidate virtual baseline, not M1 hardware support. It carries
external upstream-derived modifications; these must not be described as a small
project-owned patch to current Apple XNU.

The unmodified PureDarwin Nix build requires an operator-supplied
`MacOSX11.3.sdk.tar.xz` with SHA-256
`9adc1373d3879e1973d28ad9f17c9051b02931674a3ec2a2498128989ece2cb1`.
That input is not available on the recorded build host. The current SDK27.2
source-build adaptation is a separate experiment with its own actual hash.

If needed for a local Linux builder, package an already-owned SDK deterministically:

```sh
python3 scripts/bootstrap.py sdk-archive \
  --sdk-path /path/to/MacOSX27.2.sdk --version 27.2 \
  --output .blauos/inputs/MacOSX27.2.sdk.tar.xz
```

The archive normalizes owner/group/timestamps, preserves internal symlinks, and
records its actual SHA-256. It contains licensed build inputs and must remain
local. Supplying SDK headers is different from copying proprietary dylibs into
the target root filesystem; the latter would fail the runtime independence gate.

## Evidence and acceptance

Build scripts record commands, source pins, logs, artifacts, and unresolved
requirements. Raw warning logs, SDKs, binaries, and symbols stay ignored; small
results and meaningful diagnostics are committed under `evidence/`.

- Host compilation is not guest execution.
- A successful ARM64 kernel link is not a guest boot or physical M1 execution.
- A source-built executable linked to stock `/usr/lib/libSystem.B.dylib` is a
  host diagnostic and does not pass independent-runtime acceptance.
- Missing readiness, source provenance, PID 1/shell execution, or network proof
  keeps the relevant issue and gate open.

The minimum runtime and launchd experiments are documented in the
[runtime audit](audits/runtime-build.md); guest image and network tests are
documented in [virtualization](virtualization.md).
