# Minimal server image and PCC design-principle evaluation

Audit date: 2026-10-11. Issue: [#60](https://github.com/joeblau/blauos/issues/60),
parent [#6](https://github.com/joeblau/blauos/issues/6). This is a **software-level
decision record**. It is blocked by Gate 4 ([#59](https://github.com/joeblau/blauos/issues/59)),
which has no PASS/GO decision, and Gate 2 physical M1 acceptance remains
`NOT_RUN` (`evidence/gate-2.json`). Nothing here authorizes a release, claims a
physical boot, or assumes Apple PCC binaries, hardware, or `darwin-init` are
reusable on Mac minis.

Primary reference:
[Apple Private Cloud Compute software foundations](https://security.apple.com/documentation/private-cloud-compute/softwarefoundations).

## PCC principles studied

PCC runs a minimized, hardened subset of iOS with three stated goals: the
system's behavior is analyzable by a security researcher, the entire runnable
software set is measurable and described by attestation, and software is
immutable at runtime, changing only across a reboot that invalidates previous
attestations. Its mechanisms include Secure Boot with Image4 personalization,
a Trusted Execution Monitor (TXM) enforcing trust caches so only attested code
executes, Software Sealed Registers ratcheting configuration and cryptex
measurements into the SEP, Restricted Execution Mode (REM) as a one-way
transition that kills setup-only daemons before user data is served, Ephemeral
Data Mode with a per-boot randomized data-volume key hierarchy, removal of
dynamic code execution (system shells, interpreters, debuggers, JIT), and a
dedicated initialization task (`darwin-init`) that configures the node from BMC
input and locks the configuration register before entering REM.

## Evaluation against the headless Darwin SSH server goal

| PCC principle | Fit for blauOS | Current state (software evidence only) | Decision |
| --- | --- | --- |
| Minimal image / smallest audit surface | Direct fit. | The image already builds only what `scripts/vm/assemble.py` requires; no GUI, interpreter, debugger or JIT exists. | Adopt: the shipped set is frozen as a machine-readable allowlist, `runtime/server-image.json`. Additions require the same pinned-source and guest-acceptance path. |
| Verifiable transparency (measurable software set) | Fits in software form. | `sources.lock.json` pins every input by commit/tree; builders emit `provenance.json` with artifact hashes; `scripts/build/runtime.py` audits the offline dependency closure; `evidence/` publishes receipts. There is no SEP/TXM equivalent, so "attestation" today means reproducible, hashed receipts a reviewer can verify offline — not hardware-backed measurement. | Adopt as software-level transparency: every allowlist entry carries source provenance, and the image-level receipts stay the unit of verification. Hardware attestation is out of scope until native boot exists. |
| Immutability at runtime | Partial fit, not yet met. | The guest root is a writable ext4 filesystem; accounts and host keys must persist across the two-boot acceptance. | Defer: an immutable system partition plus a small persistent account/host-key volume is a design goal recorded here (D2), gated on Gate 4 and physical boot work. No immutability claim is made today. |
| Ephemeral data / anti-persistence | Partial fit, deliberately not adopted. | A server needs persistent operator accounts and a stable host identity; the acceptance workflow explicitly verifies persistence across reboot. | Reject per-boot obliteration for the server profile; retain the PCC idea only as "no user data outside the declared account/host-key paths", which the current `AllowUsers` + key-only profile narrows but does not enforce at the storage layer. |
| No privileged runtime access | Direct fit. | `PermitRootLogin no`, password/keyboard-interactive/PAM disabled, a single named non-root account, sshd privilege separation with `sshd-auth` chrooted into `/var/empty`, sessions dropped to the named user, and a probe-verified root-only file denial. The sandbox is upstream's resource-limit sandbox, **not** seatbelt — the independent runtime excludes seatbelt and AMFI. | Adopt with the recorded limitation: privilege dropping is real and guest-verified (`evidence/virtualization/ssh-login/`), but sandbox confinement is rlimit-only. |
| Non-targetability | Partial fit by definition. | PCC nodes are not addressable by users at all; a blauOS server exists to be SSH'd into. The applicable residue is a narrow, single-purpose listener: one TCP service, one account, key-only, no forwarding. | Adopt the narrow-surface reading: sshd is the only network-facing daemon in the allowlist; `org.blauos.smoke` writes to the console and opens no port. |
| Removal of dynamic code execution | Partial fit. | PCC removes shells; a headless operator server needs a login shell. No interpreters, debuggers or JIT are built today. | Adopt partially (D4): retain the source-built `dash` shell as the operator interface; keep the no-interpreter/no-debugger/no-JIT exclusion as an explicit allowlist rule. |
| Dedicated initialization (`darwin-init`) | Concept fits; binary does not. | `darwin-init` is proprietary and unreusable. BlauOS already has the functional analogue: `/usr/libexec/blauos-ssh-start` is a fail-closed initialization gate that requires the read-only `kern.blauos_ssh_entropy_ready` admission and the guest capability probe before sshd starts, and generates per-guest host keys only after admission. | Adopt the pattern (D3): initialization is a separate, auditable stage that must pass before the service starts; do not assume any Apple PCC component is reusable. |
| Restricted Execution Mode (kill setup-only code before serving) | Concept fits partially. | There is no TXM and no one-way execution lockdown. The wrapper pattern gives a weaker, software-only version: setup (entropy check, host-key generation) completes before the listener starts. | Record as future work: a post-boot lockdown of setup binaries has no enforcement mechanism in the current kernel profile. |

## Minimal server image definition

The shipped-services allowlist is machine-readable in
[`runtime/server-image.json`](../../runtime/server-image.json). It enumerates
the launchd services/daemons and runtime components the minimal image ships,
each with pinned source provenance, the builder that produced it, an evidence
link, and an explicit inclusion justification. Counts (kept in sync by
`tests/test_server_image.py`):

```
allowlist_services: 3
allowlist_runtime_components: 13
allowlist_component_paths: 60
```

The three services are `pid1.launchd` (the source launchd 842.91.1 lineage as
PID 1; the verified subset in `runtime/service-subset.json`),
`org.blauos.sshd` (the fail-closed wrapper `/usr/libexec/blauos-ssh-start`
supervising OpenSSH 10.6p1, `RunAtLoad` + `KeepAlive`), and `org.blauos.smoke`
(the boot-health/acceptance probe on `/dev/console`). The 13 component groups
cover the init/runtime (`launchd`, `launchctl`, `dyld`, `libSystem`), the
operator shell (`dash`), network configuration (`netsetup`), the OpenSSH server
set and its initialization/verification helpers, acceptance instrumentation,
and the three source-built command toolkits (file, admin, text). The `excluded`
list records deliberate omissions with reasons: launchd test fixtures, the SSH
client, password/root/forwarding authentication paths, seatbelt/AMFI/XPC
compatibility, GUI and dynamic-code mechanisms, the development entropy
fallback profile, embedded host keys, and unbuilt conveniences (editor, sort,
package manager, zsh).

## Recorded project decisions

- **D1 — Allowlist-governed surface.** The minimal server ships exactly the
  entries in `runtime/server-image.json`. Any new service or component needs
  pinned-source provenance, a justification, and guest acceptance evidence
  before entering the allowlist; the unittest fails on undocumented count drift.
- **D2 — Immutability deferred, not claimed.** The writable ext4 root is a
  known gap against PCC immutability. A read-only system volume plus a minimal
  persistent account/host-key volume is the recorded direction, blocked on
  Gate 4 (#59) and native boot work.
- **D3 — Dedicated initialization via fail-closed wrapper.** SSH starts only
  after entropy admission and capability verification; the development entropy
  fallback loader profile must never ship in this image. This is the project's
  software analogue of `darwin-init` + REM ordering, without claiming Apple's
  enforcement.
- **D4 — Shell retained, dynamic code otherwise excluded.** The operator shell
  (`dash`) is a deliberate inclusion; interpreters, debuggers and JIT remain
  excluded because none are built and none may be added without D1.
- **D5 — Software-level transparency only until hardware gates pass.** Receipts,
  pins and hashes are the transparency mechanism. No attestation, verified boot,
  or physical-acceptance claim is made; Gate 4 (#59) and Gate 2 remain open
  with named-review requirements unchanged.

## Limitations

All evidence cited here is software-level: compilation receipts, source pins,
and virtual-guest two-boot SSH acceptance
(`evidence/virtualization/ssh-login/checkpoint.json`). The VM firmware and
virtualization host remain in the trust boundary (see
[ssh-entropy.md](ssh-entropy.md)). The sshd sandbox is rlimit-based, not
seatbelt; the kernel PRNG provider is the legacy published Yarrow
implementation without production certification. No PCC binary, SEP/TXM
mechanism, Image4 manifest, or `darwin-init` artifact is available to or reused
by this project. Physical M1 acceptance has never run and is explicitly not
claimed.
