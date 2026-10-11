# bpm — BlauOS guest package manager

`bpm.c` is the repo-native, libSystem-only package manager for the
independent ARM64 Darwin guest. It verifies and installs signed `.bpm`
packages (uncompressed ustar: `manifest.json` + `manifest.json.sig` +
`payload/`), removes them with dependency and modification checks, lists the
local database, re-hashes installed files, lists the signed local repo index,
and receives hex-framed files over the serial console (`recv`).

Cryptography and formats are self-contained (SHA-256, SHA-512, Ed25519 per
RFC 8032, minimal JSON, ustar) and byte-compatible with the host release
tooling: signatures produced by `scripts/release/signing.py` verify in the
guest, and `scripts/release/package.py inspect` is the host reference for the
guest's verification policy. The Ed25519 field arithmetic mirrors the
repository's pure-Python RFC 8032 reference and is validated against the RFC
test vectors through `tests/test_bpm_command.py` (host-compiled) and the live
guest acceptance under `evidence/virtualization/package-manager/`.

`hello.c` is the tiny demo payload packaged as `blauos-hello`
(packages/blauos-hello.json) for the live acceptance; it is built by
`scripts/vm/build_hello_demo.py` and is never staged into an image directly.

See `docs/packages.md` for the format, trust policy, transports, and the
evaluation against existing package managers.
