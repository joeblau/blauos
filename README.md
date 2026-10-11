# blauOS

A headless Darwin server project for Apple Silicon: native XNU, independent
userspace, and reproducible releases.

Milestone 1 is complete: a clean pinned source build boots an independent ARM64
XNU guest to launchd and a shell, with networking, runtime/service tests and clean
shutdown. [GitHub CI](https://github.com/joeblau/blauos/actions/runs/38074322304)
and the [Gate 1 decision](evidence/gate-1.json) preserve the evidence and limits.
Milestone 2 is in progress: a fresh experimental T8103 ARM64e kernel and
platform extension build from source. Lab, handoff and capture tools are available. Physical M1 kernel execution and
driver acceptance remain unverified.

The [SSH login workflow](docs/ssh-login.md) adds public-key accounts to an
audited VM baseline and verifies non-root commands, terminal sessions, SFTP,
and persistent home data and host keys across two boots. Its secure boot profile
requires a virtio entropy device; stock VirtualBuddy 2.1 needs the supplied
host configuration patch. Physical M1 support remains unverified.

The guest [package manager](docs/packages.md) (`bpm`) installs, verifies, and
removes Ed25519-signed package tarballs bound to the `packages/*.json` recipes,
with a baked `file://` repo and serial-console transfer as demonstrated
transports in the live VirtualBuddy guest.

- [Release scope and evidence gates](docs/scope.md)
- [Repository/source architecture](docs/architecture.md)
- [Pinned inputs and build instructions](docs/build.md)
- [Kernel patch policy](docs/kernel-patches.md)
- [Virtual baseline](docs/virtualization.md)
- [Account provisioning and SSH login](docs/ssh-login.md)
- [Command-line tools and updating an existing VM](docs/commands.md)
- [Guest package manager (bpm): signed packages, transports, live acceptance](docs/packages.md)
- [Milestone 1 acceptance evidence](docs/milestone-1.md)
- [Milestone 2 execution record](docs/milestone-2.md)
- [Native driver interface contracts and reuse boundary](docs/audits/native-driver-contracts.md)
- [Source-supported root filesystem decision](docs/audits/rootfs-decision.md)
- [Minimal server image and PCC design principles](docs/audits/server-principles.md)
- [Declarative server configuration](runtime/examples/README.md)
- [Release manifest, signing, and download tooling](docs/audits/release-tooling.md)
- [GitHub roadmap](https://github.com/joeblau/blauos/issues/1)
- [Milestone 1: reproducible baseline](https://github.com/joeblau/blauos/milestone/1)
- [Milestone 2: native kernel bring-up](https://github.com/joeblau/blauos/milestone/2)

```sh
python3 scripts/blauos.py validate-lock
python3 scripts/bootstrap.py host --verify-sdk
python3 scripts/blauos.py fetch --profile puredarwin-vm
```

External sources, SDKs, and generated artifacts are kept in ignored `.blauos/`.
The project MIT license applies to this repository's original work; external
components retain their own licenses and notices.
