# Guest package management (bpm)

The independent ARM64 Darwin guest has a package manager: `bpm`, a
repo-native, libSystem-only C tool (`vm/commands/bpm/bpm.c`, cross-compiled by
`scripts/vm/build_bpm_commands.py` like the other toolkit layers). It installs,
removes, lists, and re-verifies **signed** `.bpm` packages against the
image-baked Ed25519 trust root `/etc/blauos/trust-root.json`, using the same
signature scheme and trust-root format as the release tooling
(`scripts/release/signing.py`).

## Why not an existing package manager

Cross-building opkg, dpkg, or pkgsrc `pkg_install` was evaluated and rejected:

* All three assume compression libraries that the independent runtime does
  not provide. `.ipk`/`.deb`/pkgsrc packages are gzip (or xz/bzip2)
  compressed tarballs; the guest runtime ships libSystem-era dylibs only —
  no zlib, no liblzma, no bzip2, no libarchive. Porting a decompressor stack
  would already exceed the size of a purpose-built tool.
* dpkg additionally needs Perl and a POSIX shell for maintainer scripts;
  pkgsrc `pkg_install` needs OpenSSL for signature verification; opkg's
  useful configurations pull in libcurl or wget for transport. None of these
  dependencies exist in the guest, and adding them contradicts the minimal
  server-image profile (`docs/audits/server-principles.md`).
* The repository already has audited, pure-stdlib Ed25519 signing with trust
  roots, rotation, and revocation. Reusing that exact trust model in a small
  guest tool keeps the verified code paths identical between host and guest
  instead of introducing a second, weaker signature stack (opkg's usign,
  dpkg's optional debsig, etc.).

The honest scope cut is therefore a minimal repo-native tool, with the package
format deliberately bound to the existing `packages/*.json` recipes and
`scripts/release/signing.py` trust roots.

## Package format

A `.bpm` file is an **uncompressed ustar** archive (no gzip — there is no
zlib in the guest):

```
manifest.json        schema_version 1, manifest_kind blauos-package:
                     name, version, depends, recipe {path, sha256},
                     files[] {path, sha256, size, mode}
manifest.json.sig    Ed25519 signature over the exact manifest bytes
                     (ed25519-pure-python-rfc8032 scheme, same as releases)
payload/<path>       one member per manifest files[] entry
```

The signature covers the manifest bytes; the manifest covers every payload
file by SHA-256, size, and mode. The guest verifies signature → manifest
digest → per-file payload hashes, in that order, **before writing anything**,
and fails closed with no partial install on any mismatch.

Build and verify a package from a built layer and its recipe:

```sh
python3 scripts/release/package.py build \
  --root .blauos/build/commands/vim-run-1/root \
  --recipe packages/vim.json --name vim --version 9.2.1175 \
  --key .blauos/release/package-key.json \
  --trust-root .blauos/release/package-trust-root.json \
  --output vim-9.2.1175.bpm --receipt receipt.json

python3 scripts/release/package.py inspect \
  --trust-root .blauos/release/package-trust-root.json \
  --package vim-9.2.1175.bpm
```

`inspect` is the reference implementation of the guest's verification policy.
Keys and trust roots are managed with `scripts/release/signing.py`
(`keygen --trust-root`, `rotate`, `revoke`); private keys stay in the ignored
`.blauos/` tree and are never committed. Recipe outputs declared in
`packages/*.json` must be present in the packaged layer, so a package cannot
silently drop a recipe's declared artifact.

## Guest commands

```
bpm install <package.bpm|name>   verify signature+hashes, unpack, record db
bpm remove <name>                remove recorded files; refuses dependents
                                 and locally modified/missing files
bpm list                         list installed packages
bpm verify [name]                re-hash installed files against the db
bpm repo                         list the signed local repo index
bpm recv <output>                receive a hex-framed file over the console
```

* Local database: `/var/db/bpm/db/<name>.json` (the verified manifest bytes).
* Dependency model: `depends` entries must name installed packages; the
  pseudo-dependency `runtime` is the independent base image and is always
  satisfied. There is no upgrade command: install refuses an already
  installed name and refuses to overwrite any existing file.
* Trust policy on the guest: the signing key must be present in the trust
  root with status `active` or `retired`; `revoked` and unknown keys are
  rejected. Validity-window evaluation stays host-side (the guest has no
  trusted clock); rotation/revocation are enforced by baking an updated
  trust root into the image.
* The key-validity `--at` policy, receipts, and rotation remain with the
  host tooling (`scripts/release/signing.py`).

## Transport

There is no network package fetch in the guest and none is faked:

* **Local `file://` repo** — packages plus a signed `index.json` are baked
  into the image at `/var/db/bpm/repo/` by `package.py index` +
  `package.py stage-repo` (an ordinary compose_root.py layer). The guest
  verifies the index signature and the package hash from the index before
  resolving `bpm install <name>`.
* **Serial console** — `bpm recv <path>` reads hexadecimal lines from the
  console (injected through the virtio console input FIFO by
  `scripts/vm/console_input.py`) until `END <sha256>`, and keeps the file
  only if the digest matches. This is the transport demonstrated live in
  VirtualBuddy; it is slow and intended for small packages.
* **SFTP** — on the SSH profile (`docs/ssh-login.md`), packages can be
  uploaded with the already-verified SFTP channel. That profile requires the
  strict entropy loader and virtio entropy device, which stock
  VirtualBuddy 2.1 omits, so SFTP is not available in the VirtualBuddy
  console acceptance below.

## Image integration

* `ssh_workflow.py` builds the bpm layer (`build-bpm` stage) and composes it
  into SSH images; `vm/commands/smoke.sh` runs read-only bpm checks where the
  tool is present (`BLAUOS_COMMAND_CHECK package pass`).
* The VirtualBuddy console image composes the bpm layer and the staged repo
  layer with `scripts/vm/compose_root.py` and assembles with
  `scripts/vm/assemble.py` — the same `--standalone --console-shell` profile
  as the console evidence.

## Live acceptance

Recorded in [evidence/virtualization/package-manager](../evidence/virtualization/package-manager/checkpoint.json):
in a VirtualBuddy (generic EFI) guest, the vim package installed by name from
the baked signed repo and ran from an alternate root; the blauos-hello
package transferred over the serial console installed and ran from
`/usr/local/bin`; `list`/`verify`/`remove` passed; tampered-payload,
tampered-manifest, and untrusted-key packages were all rejected before any
file write.

## Remaining work

* No upgrades, no package scripts, no dependency solving beyond
  presence checks, no remote repositories.
* Key rotation reaches the guest only by baking a new trust root into an
  image (or installing a trust-root update through a signed package is
  deliberately impossible: `/etc/blauos/trust-root.json` is not packagable).
* Serial transfer throughput limits practical package size to tens of KB;
  larger packages use the baked repo or the SFTP profile.
