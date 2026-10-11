# Release tooling: manifests, signing, and resumable download

Audit date: 2026-10-11. This audit covers the software-level release tooling in
`scripts/release/` implementing issues #65 (release manifests/SBOM/provenance),
#78 (release signing, trust roots, key lifecycle), and #79 (resumable
signed-release download and bounded caching). All evidence here is
software/compile-level. **Gate 2 is NOT RUN; no physical M1 exists.** Nothing
in this tooling stages, authorizes, or boots a release on hardware, and no
physical acceptance is claimed.

## Components

| Tool | Issue | Role |
| --- | --- | --- |
| `scripts/release/manifest.py` | #65 | Generate a release manifest + component SBOM from the pinned inputs (`sources.lock.json`, `toolchain.lock.json`, the reviewed patch series under `patches/` and `vm/patches/`), artifact hashes, compatibility metadata, and an embedded third-party verification procedure; `verify` recomputes every recorded hash against a local checkout. |
| `scripts/release/signing.py` | #78 | Offline release signing: pure-stdlib Ed25519 (RFC 8032), trust-root JSON with key status/validity windows, rotation, revocation, and verifier policy. |
| `scripts/release/download.py` | #79 | Resumable signed-release fetch/cache library + CLI: Range-based resume, per-file SHA-256 and manifest-signature verification, LRU-evicted disk-bounded cache. |
| `scripts/release/__init__.py` | — | Shared strict-JSON, hashing, canonical-encoding, and receipt helpers. |

Tests: `tests/test_release_manifest.py`, `tests/test_release_signing.py`,
`tests/test_release_download.py` (37 tests, stdlib unittest).

## #65 — Manifest, SBOM, compatibility, provenance

`manifest.py generate --release-id ID --artifacts DIR --output M --receipt R`
records:

- exact SHA-256 of `sources.lock.json` and `toolchain.lock.json`, plus their
  full contents (source revisions/trees are validated as immutable 40-hex git
  object IDs; archive SHA-256 pins are validated);
- every reviewed patch file with SHA-256 and size;
- a component SBOM (`blauos-component-sbom-v1`) with per-component license,
  repository, pinned revision, and purl-style identifier;
- per-artifact path/size/SHA-256 for the published artifact set;
- compatibility metadata: machine/SoC identifiers (`Macmini9,1`/t8103/J274AP —
  explicitly hardware-gated; VMAPPLE and QEMU VIRT virtual profiles),
  firmware/boot-policy requirements, image formats, configuration schema
  (`runtime/service-subset.json`), and data-migration constraints;
- `manifest_content_sha256`, a self-binding digest over the canonical
  manifest, so a third party can detect post-generation edits before checking
  any individual entry.

Receipts follow the repository convention: exact argv, cwd, input hashes,
output hashes, and the JSON result.

### Third-party verification procedure

1. Clone the repository and check out the release tag named by
   `manifest.release_id`.
2. Run `python3 scripts/release/manifest.py verify MANIFEST.json --artifacts
   DIR --receipt verify-receipt.json`. The command recomputes the manifest
   content digest, both lock-file hashes, every patch hash, and every artifact
   hash/size; it exits nonzero and names every mismatching check.
3. Verify the manifest signature: `python3 scripts/release/signing.py verify
   --trust-root TRUST.json --manifest MANIFEST.json --signature
   MANIFEST.sig.json`.
4. For rebuild verification, rerun the pinned recipes recorded in the build
   receipts (`scripts/build/xnu.py`, `vm/smoke-image.nix`) and compare artifact
   SHA-256 values against `manifest.artifacts`. Complete release inputs are the
   pinned lock files plus the patch series, both fully enumerated and hashed in
   the manifest itself, so archiving the repository checkout at the release tag
   archives the complete input set.

Limitations: rebuild bit-for-bit identity is only as strong as the recorded
host toolchain (Xcode/SDK hashes in `toolchain.lock.json` describe an observed
experiment environment, not redistributable inputs); `unavailable_inputs` from
the source lock are carried into the manifest verbatim.

## #78 — Signing, trust roots, key lifecycle

The repository has no `cryptography` dependency (checked: only an unrelated
OpenSSH `config.h` evidence file mentions the name), so signatures use a
self-contained pure-Python Ed25519 implementation following RFC 8032 on
`hashlib.sha512` (`scripts/release/signing.py`). It is validated against the
RFC 8032 test vectors in the test suite. Honest cost: pure-Python scalar
multiplication takes a fraction of a second per operation, acceptable for
small manifest payloads and nothing else.

- **Signed-manifest format** (`schema_version` 1, scheme
  `ed25519-pure-python-rfc8032`): the signature covers the canonical JSON of
  `{manifest_sha256, key_id, signed_at}`, where `manifest_sha256` is the
  SHA-256 of the exact manifest file bytes. Artifact coverage comes from the
  per-artifact SHA-256 entries inside the signed manifest.
- **Trust root**: a JSON document listing `key_id` (SHA-256/16 of the public
  key), public key, status (`active`/`retired`/`revoked`), optional
  `not_before`/`not_after` validity windows, `superseded_by`/`supersedes`
  rotation links, and `revoked_at`/`revocation_reason`. Duplicate key IDs and
  IDs that do not match their public key are rejected at load. The trust root
  is the verifier's entire trust policy: it is distributed out of band by the
  operator.
- **Key custody**: `keygen` writes a private-key JSON with mode 0600 and a
  custody warning; signing is fully offline and writes a receipt recording
  argv and input/output hashes. Recovery is by rotation: a successor key is
  added and the compromised key revoked with a recorded reason.
- **Verifier policy**: unknown key IDs, revoked keys, keys whose validity
  window does not cover `signed_at` (old/superseded keys), keys outside their
  window at verification time, digest mismatches (tampered manifests), and bad
  Ed25519 signatures are all rejected with distinct errors. Signatures made by
  a key while it was active remain verifiable after retirement, so rotation
  does not invalidate old releases; a retired key cannot produce new
  signatures. All of these cases are tested.
- **Separation from boot authorization**: distribution signatures authenticate
  release bytes only. They are explicitly distinct from Apple machine-local
  boot-policy authorization (paired 1TR, permissive security mode,
  LocalPolicy); neither can substitute for the other, and every receipt records
  `boot_authorization_performed: false`.

## #79 — Resumable download and bounded cache

`download.py fetch --base-url URL --trust-root TRUST.json --cache DIR
--max-cache-bytes N` fetches `release-manifest` bytes plus their `.sig.json`
sidecar, verifies the signature through the same code path as offline
verification, then fetches each artifact:

- partial transfers live in `cache/partial/NAME.part` with a
  `NAME.state.json` record (URL, expected SHA-256, bytes received), so
  incomplete files are always distinguishable from verified artifacts and a
  process restart resumes from the recorded offset via an HTTP Range request;
- a server that ignores or mismatches the Range header causes the partial file
  to be discarded and the transfer restarted from zero — partial state is
  never extended on unverifiable terms;
- a completed `.part` whose hash matches is promoted without network access;
  a hash mismatch discards the corrupted bytes entirely;
- the cache is bounded by `--max-cache-bytes`; least-recently-used verified
  artifacts are evicted to make room, and a bound that cannot be satisfied
  (or ENOSPC during write) raises a distinct `DiskBoundError`;
- offline hosts raise `OfflineError`; transfers have timeouts and retry with
  exponential backoff;
- nothing outside the cache directory is touched
  (`active_system_assets_touched: false` in every result).

The test suite uses a local threaded HTTP fixture server and covers:
truncation mid-transfer with later resume from the exact offset, a server
without Range support, crash-after-complete promotion, same-length corruption
rejection, full-disk simulation via a failing write, offline hosts, cache
eviction under a tight bound, unsatisfiable bounds, on-disk tampering of a
cached artifact, tampered manifest rejection, and signatures from an
untrusted key.

Known limitations: the size bound counts artifact and partial files; the small
signed-metadata copies under `cache/metadata/` are not counted. Authenticated
transport means HTTPS plus the manifest signature; plain HTTP is accepted for
local fixture/testing and relies on signature verification alone.

## Hardware-gated remainder

The following acceptance items remain open and hardware-gated:

- staging a signed, downloaded release onto a physical M1 target;
- machine-local boot-policy authorization (paired 1TR / permissive security)
  for a release artifact on hardware;
- any physical boot or Gate 2 acceptance of a release produced by this
  tooling.

These are tracked under Gate 2 / Phase 4 issues (#80 and its blockers) and
require the physical lab Mac; no claim is made here.
