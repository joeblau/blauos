# Pinned build inputs and experiments

Use Python 3.12 or newer and Git 2.41 or newer for source validation/fetching.
Sparse-source validation uses Git's `sparse-checkout check-rules` command.
The macOS experiment profile
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
The independent shell uses a pinned dash checkout from its primary upstream.
ICU's official source archive includes generated data absent from a Git checkout;
its release archive has a separate mandatory SHA-256:

```sh
python3 scripts/blauos.py fetch-archives icu4c-76.1
```

Archive retrieval verifies the bytes before accepting an input and preserves an
existing mismatched archive. Archive hashes and licenses are recorded alongside
Git pins in the source lock.

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

## Fresh native build and virtual boot

On the pinned ARM64 macOS host, with Docker connected to an ARM64 Linux daemon:

```sh
python3 scripts/vm/native_ci.py --output .blauos/build/native-ci/run-new
```

Use a new output directory for each run. The orchestrator verifies the full
host/SDK fingerprint, fetches new source checkouts and the hash-pinned ICU
archive, and builds the kernel, essential drivers, runtime, launchd, shell, and
commands. A digest-pinned Nix container supplies Linux image-formatting tools,
the source-built EFI loader, QEMU, and firmware from the locked PureDarwin
flake. Disk formatting operates on files in the output directory.

Each step has a deadline and records its invocation, result, and log hash.
The pipeline clears inherited compiler flags and SDK overrides, then uses the
SDK and compiler paths verified by preflight. It records those paths without
copying the host environment into public logs. Interrupted builds terminate
their active child process group and remove the run's named Docker container;
the workflow also performs cleanup before archiving its evidence.
Complete runtime provenance and dependency checks precede image assembly.
The final result requires the actual guest smoke test; successful compilation
alone leaves `guest_boot_passed` false. The [fresh GitHub run](https://github.com/joeblau/blauos/actions/runs/38074322304)
passes from clean `bf6d081`; its complete uploaded archive was downloaded and
digest-verified. See [milestone-1.md](milestone-1.md) for acceptance and limitations.

The manual `Native ARM64 baseline` GitHub workflow uses a provisioned
`[self-hosted, macOS, ARM64, blauos-native-sdk]` runner with this host toolchain
and an ARM64 Docker daemon. It executes the same fresh pipeline and archives
manifests, kernel symbols, serial output, and test results. Its artifact list
excludes the SDK archive and SDK-derived headers.

## Evidence and acceptance

Build scripts record commands, source pins, logs, artifacts, and unresolved
requirements. Raw warning logs, SDKs, binaries, and symbols stay ignored; small
results and meaningful diagnostics are committed under `evidence/`.
Tracked closure summaries retain every artifact identity, dependency, load
route and error, with counts and hashes for complete symbol tables. Full raw
audits remain in the build output and are included in native CI artifacts.

- Host compilation is not guest execution.
- A successful ARM64 kernel link is not a guest boot or physical M1 execution.
- A source-built executable linked to stock `/usr/lib/libSystem.B.dylib` is a
  host diagnostic and does not pass independent-runtime acceptance.
- Missing readiness, source provenance, PID 1/shell execution, or network proof
  keeps the relevant issue and gate open.

The minimum runtime and launchd experiments are documented in the
[runtime audit](audits/runtime-build.md); guest image and network tests are
documented in [virtualization](virtualization.md).
