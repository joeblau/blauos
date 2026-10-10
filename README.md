# blauOS

A headless Darwin server project for Apple Silicon: native XNU, independent
userspace, and reproducible releases.

The project is establishing an ARM64 build and virtual baseline. Source-built
components and recorded experiments are tracked separately from proven guest
execution and physical M1 hardware support. Check the evidence before treating
an experiment as a completed operating system.

- [Release scope and evidence gates](docs/scope.md)
- [Repository/source architecture](docs/architecture.md)
- [Pinned inputs and build instructions](docs/build.md)
- [Kernel patch policy](docs/kernel-patches.md)
- [Virtual baseline](docs/virtualization.md)
- [Milestone 1 evidence and remaining work](docs/milestone-1.md)
- [GitHub roadmap](https://github.com/joeblau/blauos/issues/1)
- [Milestone 1: reproducible baseline](https://github.com/joeblau/blauos/milestone/1)

```sh
python3 scripts/blauos.py validate-lock
python3 scripts/bootstrap.py host --verify-sdk
python3 scripts/blauos.py fetch --profile puredarwin-vm
```

External sources, SDKs, and generated artifacts are kept in ignored `.blauos/`.
The project MIT license applies to this repository's original work; external
components retain their own licenses and notices.
