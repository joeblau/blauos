# Darwin Server 0.1 release contract

blauOS is a headless Darwin server project. Release 0.1 must provide native XNU,
independently built userspace, headless administration, and reproducible releases.
Building an ARM64 executable on macOS does not prove an independent operating
system. Accepting a custom boot collection does not prove that its kernel ran.

## Initial hardware

The first physical target is the 2020 **M1 Mac mini, Macmini9,1**, with its standard
1 Gb/s wired Ethernet configuration. The optional 10 Gb/s configuration and other
Apple Silicon models are not qualified by this contract. A dedicated machine's
actual controller, firmware, memory, storage, and recovery environment must be
inventoried before driver work. The current development host is a build host;
its presence is not evidence that the target machine has been tested.

## Required behavior

- The executing kernel is a project-built XNU, rather than the stock macOS kernel.
- PID 1, the shell, and their runtime libraries are built from identified source.
  No proprietary macOS runtime library or shared cache supplies target symbols.
- Persistent storage, wired networking, SSH, and remote reboot work without a
  display or graphical login.
- Release source revisions, patches, tools, image construction, hashes, and
  verification instructions are available. Unavailable or licensed inputs are
  disclosed instead of being described as reproducible public inputs.
- Apple firmware and locally authorized boot assets are recorded separately from
  source-built target runtime components. Their presence does not establish an
  open implementation of the associated drivers.

P0 work covers exception handling, timers, MMU, interrupts, early diagnostics,
device discovery, and memory layout. P1 work covers DART/DMA, ANS/storage, Ethernet,
and the persistent headless server. USB and CPU frequency/idle support are P2.
GPU, audio, display drivers, Wi-Fi, Bluetooth, and graphical login are excluded
from 0.1. The virtual baseline uses a separately qualified virtual platform;
virtual GIC/virtio drivers are not M1 AIC/DART/ANS drivers.

## Evidence gates and decision ownership

| Gate | Required evidence | Decision owner |
| --- | --- | --- |
| [1: reproducible build](https://github.com/joeblau/blauos/issues/24) | Clean XNU and minimum-runtime builds, pinned inputs, hashes, source-built executable and complete runtime dependency closure | Joe Blau (`joeblau`), with recorded technical review |
| [2: independent boot](https://github.com/joeblau/blauos/issues/40) | Physical M1 logs identifying self-built XNU execution, selected boot path, and essential-driver gaps | Joe Blau (`joeblau`), with kernel/platform review |
| [3: independent userspace](https://github.com/joeblau/blauos/issues/51) | Physical PID 1 and shell execution, with no proprietary target runtime dependencies | Joe Blau (`joeblau`), with runtime review |
| [4: persistent server](https://github.com/joeblau/blauos/issues/59) | Repeated storage, Ethernet, SSH, and remote-reboot tests against a predeclared reliability threshold | Joe Blau (`joeblau`), with operations review |

A reviewer records their name, the evidence, and an explicit PASS/GO or
FAIL/NO-GO. A closed experiment with a documented blocker does not pass a gate.
Gate 2 is the decisive investment checkpoint: extensive proprietary driver
reconstruction triggers reassessment before committing to distribution work.

Native boot through Apple's local policy is the first experiment. m1n1 remains
an explicitly activated fallback and debugging path. The exact kernel collection,
firmware handoff, authorization, and OS-version compatibility require experiments.
An immutable system with separate writable data is the intended image model;
APFS containers, boot policy, authorized assets, and recoverability must determine
the eventual disk arrangement and update state machine.

## Planning assumptions

The first 30 days focus on source/runtime feasibility and custom-kernel boot.
Package management, fleet agents, and installer implementation follow a proven
native server. The 0–4, 5–10, 11–18, 19–28, and 29–40+ week phase windows assume
3–5 experienced systems engineers, a dedicated test machine, and reusable source
components. These are estimates, not validated commitments. Phase 1–2 uncertainty
may extend the project beyond 40 weeks.

Progress is tracked in the [engineering roadmap](https://github.com/joeblau/blauos/issues/1)
and [first-month tracker](https://github.com/joeblau/blauos/issues/2).
