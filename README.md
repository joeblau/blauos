# blauOS

A headless Darwin server project for Apple Silicon: native XNU, independent
userspace, and reproducible releases.

Milestone 1 is complete: a clean pinned source build boots an independent ARM64
XNU guest to launchd and a shell, with networking, runtime/service tests and clean
shutdown. [GitHub CI](https://github.com/joeblau/blauos/actions/runs/38074322304)
and the [Gate 1 decision](evidence/gate-1.json) preserve the evidence and limits.
Milestone 2 is in progress: native T8103 build experiments and physical lab,
handoff and capture tools are available. Physical M1 kernel execution and
driver acceptance remain unverified.

- [Release scope and evidence gates](docs/scope.md)
- [Repository/source architecture](docs/architecture.md)
- [Pinned inputs and build instructions](docs/build.md)
- [Kernel patch policy](docs/kernel-patches.md)
- [Virtual baseline](docs/virtualization.md)
- [Milestone 1 acceptance evidence](docs/milestone-1.md)
- [Milestone 2 execution record](docs/milestone-2.md)
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
