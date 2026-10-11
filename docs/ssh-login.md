# Independent SSH login workflow

The SSH workflow builds OpenSSH from its pinned source, provisions a named
non-root account, and verifies command, interactive PTY, and SFTP sessions over
two independent ARM64 Darwin boots. It retains the same writable guest disk
between boots to check account files, home data, and the host key. It does not
prove physical Apple Silicon boot or production readiness.

The accepted test profile is the public `Virtualization.framework` generic EFI
probe with **an explicitly configured `VZVirtioEntropyDeviceConfiguration`**.
The loader also requires a firmware-provided cryptographic EFI RNG seed. The
kernel uses the audited seed-handling fix and exposes a read-only entropy
admission checkpoint. The startup wrapper refuses to start SSH unless the
guest capability probe passes that checkpoint and its account, permission,
PTY, credential transition, sandbox, descriptor-passing, and randomness API
checks. Varying random bytes alone is not an entropy-quality test.

Stock VirtualBuddy 2.1's Linux configuration omits the virtio entropy device.
It is a different configuration from this accepted profile. This workflow
does not create or replace a VirtualBuddy application VM; a separately patched
VirtualBuddy build and actual application verification are needed before
claiming the same configuration there.

The [recorded full workflow](../evidence/virtualization/ssh-login/checkpoint.json)
passed all 15 build and acceptance stages. Each of its two boots passed 20
baseline checks and 11 SSH capability checks. Account, terminal,
authentication, SFTP and persistence results are in the
[guest checkpoint](../evidence/virtualization/ssh-login/smoke/checkpoint.json).
The configuration prefers hybrid key exchange with a Curve25519 fallback;
these receipts do not claim which individual algorithm was negotiated.

## Prepare the baseline

Run on the pinned ARM64 macOS/Xcode build host with Docker and sufficient disk
space. Start from an independently audited baseline root and its complete
source provenance. The kernel collection and loader must already be built
with these source overlays:

* `vm/patches/puredarwin-ssh-prng.patch` — corrected kernel PRNG seed handling
  and read-only `kern.blauos_ssh_entropy_ready` admission.
* `vm/patches/xnu-loader-ssh-entropy.patch` — strict cryptographic EFI RNG seed
  acquisition; missing RNG fails closed.

Provide an entropy build receipt binding the complete kernel/loader hashes,
current source lock, and exact overlay hashes:

```json
{
  "schema_version": 1,
  "kernel_sha256": "<full collection SHA-256>",
  "loader_sha256": "<EFI executable SHA-256>",
  "source_lock_sha256": "<current sources.lock.json SHA-256>",
  "loader_require_efi_rng": true,
  "kernel_prng_provider": "yarrow-fixed-v1",
  "virtio_entropy_device": true,
  "patches": [
    {"path": "vm/patches/xnu-loader-ssh-entropy.patch", "sha256": "<overlay SHA-256>"},
    {"path": "vm/patches/puredarwin-ssh-prng.patch", "sha256": "<overlay SHA-256>"}
  ],
  "guest_entropy_admission_verified": false
}
```

This build receipt admits the inputs to the experiment. It does not substitute
for the kernel checkpoint and actual login tests. Preserve the source build
receipts that produced these binaries.

The entropy asset helper rebuilds all corecrypto objects from pinned source
and prepares a strict loader in an isolated tree. It reuses the supplied
audited kernel, generated headers and remaining kexts, and records those inputs:

```sh
python3 scripts/build/ssh_entropy.py \
  --kernel-build /path/to/audited-kernel-build \
  --collection-command /path/to/collection-command.json \
  --container YOUR_OWNED_ARM64_NIX_BUILDER \
  --output .blauos/experiments/ssh-entropy/run-1
```

The existing builder must mount this output path writable and provide its
pinned Nix store. `assets.json` gives the kernel, loader and entropy receipt
paths for the SSH workflow. Existing build directories are preserved.

## Execute locally

Choose a fresh output directory and a free guest IP. The default is
`192.168.64.201/24` with gateway/DNS `192.168.64.1`. Stop another guest using
that address before running this experiment; the workflow preserves existing
VMs and never stops them automatically.

```sh
python3 scripts/vm/ssh_workflow.py \
  --root /path/to/audited-root \
  --provenance /path/to/runtime-provenance.json \
  --kernel /path/to/strict-kernel.classic \
  --kernel-sha256 '<full collection SHA-256>' \
  --loader /path/to/strict-loader.efi \
  --loader-sha256 '<EFI executable SHA-256>' \
  --entropy-receipt /path/to/entropy-receipt.json \
  --public-key "$HOME/.ssh/id_ed25519.pub" \
  --username joeblau --uid 1000 --gid 1000 \
  --output .blauos/build/ssh-login/local-run-1
```

Only the public key is read. The workflow creates a separate temporary test
identity, authorizes its public key alongside yours, and passes the owned
private-key filename to host SSH/SFTP clients. It never reads or prints the
private-key contents. Its test key pair is removed after acceptance or failure;
the image retains the now unusable test public key until an explicit future
account-management operation removes it.

The workflow fetches locked OpenSSH/PureDarwin sources, builds the SSH binaries
and local probes, adds their exact artifacts to a copied baseline, and audits
the full independent runtime closure. It formats the account-enabled disk
inside a uniquely named, labelled Linux ARM64 Nix container, compiles and signs
the public VZ probe, and runs `scripts/vm/ssh_smoke.py`. It removes only its own
formatter container and leaves the Nix cache available for another build.

A passing `result.json` requires the real two-boot harness to pass. The harness
pins the host key obtained through trusted guest console output, checks fresh
session challenges, rejects another key and root login, verifies actual
UID/GID/home and root-only access denial, tests a controlling PTY, and checks
SFTP and persisted data/host identity after guest-initiated poweroff and a
second boot. Both guests are stopped when acceptance finishes. A listening
port, a host-only unit test, or a copied prior receipt cannot establish success.

## Run the GitHub workflow

The manually dispatched workflow is
`.github/workflows/ssh-login.yml`, using the same
`[self-hosted, macOS, ARM64, blauos-native-sdk]` runner labels as the native
baseline. The runner needs the prepared baseline and entropy-enabled artifacts;
this job does not reconstruct a kernel from missing build inputs.

Store a runner-local baseline receipt outside the checked-out repository:

```json
{
  "schema_version": 1,
  "root": "/absolute/path/to/audited-root",
  "provenance": "/absolute/path/to/runtime-provenance.json",
  "kernel": "/absolute/path/to/strict-kernel.classic",
  "kernel_sha256": "<full collection SHA-256>",
  "loader": "/absolute/path/to/strict-loader.efi",
  "loader_sha256": "<EFI executable SHA-256>",
  "entropy_receipt": "/absolute/path/to/entropy-receipt.json"
}
```

The checkout step cleans the worktree, including ignored `.blauos` files.
Keep the baseline receipt and every referenced input outside that worktree.
Dispatch with its path, your bare Ed25519 **public** key, and the account IDs.
Inputs reach Python through environment variables and argument arrays, so they
are not interpolated into shell code. The CI job requires a clean checkout and
serializes its runs to avoid their shared test IP and challenge port colliding.

The CLI also accepts `--baseline-receipt` instead of the seven explicit baseline
arguments and `--public-key-env ENVIRONMENT_VARIABLE` instead of a public-key
file. No private SSH key belongs in a workflow input or repository secret.

Only the workflow's `evidence/` allowlist is uploaded. It contains source/build
receipts, audits, account fingerprints, and redacted text logs. Root trees,
images, NVRAM, private keys, and authorized client public-key files remain
local. `evidence/export.json` records the exported files and their hashes.

## Connect after deployment

Acceptance uses an owned test disk and shuts it down. A provisioned, running
instance of the same supported profile can use your already authorized key:

```sh
ssh -i "$HOME/.ssh/id_ed25519" joeblau@192.168.64.201
```

Verify its host fingerprint against the trusted guest console/build workflow
record before trusting the first connection. An independently assembled image
generates its own host key on first boot; do not reuse another machine's host
fingerprint. Your Mac's zsh runs the SSH client, and the initial guest shell is
the independently built `/bin/sh`.

See [local account provisioning](accounts.md) for database formats, safe
idempotency, and filesystem ownership. SSH service restart, remote reboot,
account creation within a running guest, password authentication, and physical
hardware enablement remain separate acceptance work.
