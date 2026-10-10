# Physical M1 execution evidence

Tracks [#28](https://github.com/joeblau/blauos/issues/28) and
[#39](https://github.com/joeblau/blauos/issues/39). No physical checkpoint has
been captured. [physical_evidence.py](../../scripts/platform/physical_evidence.py)
checks evidence files offline; it does not authorize boot objects, install
images, change policy, reset hardware or approve Gate 2. The currently
full checkpoint protocol below is **not implemented**. A separate
[partial early diagnostic hook](m1-early-diagnostics.md) has compiled as a
native kernel object, but emits a different protocol and lacks the required
hardware tests and bindings. A source build, passive serial collector and
unit tests do not complete the hardware tasks.

The dedicated target is Macmini9,1, Apple M1/T8103, J274AP. Those identifiers
refer to the 2020 M1 Mac mini in the
[Asahi device list](https://asahilinux.org/docs/hw/devices/device-list/).
Use [m1-lab.md](m1-lab.md) to identify it and qualify recovery and UART, then
[native-boot.md](native-boot.md) for the authorization experiment. The current
M3 build-host inventory and the tested VIRT guest remain separate evidence.

The UART requirement is physical: Asahi describes a 1.2 V UART on the DFU
Type-C port enabled by vendor USB-PD messages. Its two-M1 host method needs
a SuperSpeed/SBU cable and exposes `/dev/cu.debug-console` at 115200 baud.
An independently qualified level-shifting/PD adapter is another route.
`macvdmtool reboot serial` changes device state and reboots the target;
it is not part of our passive collector. The M3 host is not validated by the
two-M1 instructions.
[Asahi physical serial reference](https://asahilinux.org/docs/hw/soc/serial-debug/).
m1n1 USB proxy and hypervisor UART appear after m1n1 initializes USB. Normally
macOS names the proxy `/dev/cu.usbmodemP_01` and virtual UART
`/dev/cu.usbmodemP_03`; enumerate and verify actual roles. Such captures must
be labeled m1n1 and cannot satisfy this Path A contract.
[Asahi m1n1 guide](https://asahilinux.org/docs/sw/m1n1-user-guide/).

Before each actual boot, create a fresh challenge into a new local path:

```sh
python3 scripts/platform/physical_evidence.py challenge \
  --output .blauos/lab/m1/boot-001/challenge.json
```

The command creates a cryptographically random 64-character hexadecimal
nonce and its proposed `blauos-checkpoint-nonce=` boot argument. It creates
no log and reports no boot. The operator must deliver that argument through
the target's verified boot-argument policy. The instrumented kernel must
read it from its actual handoff; printing it from a host shell is insufficient.
Use a distinct identity-capture nonce for the lab inventory. Do not reuse
either nonce across boot sessions.

Start the qualified native UART collector before the physical operator
boots the selected OS:

```sh
python3 scripts/platform/capture_serial.py \
  --device /dev/cu.REPLACE_WITH_VERIFIED_TARGET_UART \
  --transport native-uart --baud 115200 --seconds 600 \
  --output .blauos/lab/m1/boot-001/uart
```

Preserve raw `serial.log` and `capture.json`. The collector sends zero
application bytes and no reset command, but opening a serial device may
affect adapter control signals. Its receipt proves byte/hash consistency,
not physical origin. A timeout, banner, synthetic PTY or empty capture is
not successful boot. Review private logs before publication; preserve
original hashes if a sanitized derivative is published.

The manifest uses `schema_version: 1` and
`evidence_kind: "physical-xnu-checkpoint"`. Each referenced file is an object
`{"path": "relative/bundle/path", "sha256": "<actual lowercase file hash>"}`.
Paths resolve within the explicit evidence bundle; empty files, traversal,
escaping symlinks and hashes that changed after capture fail validation.
The table defines required top-level fields without fabricating sample
hardware logs.

| Field | Evidence bound by the validator |
| --- | --- |
| `inventory` | Sanitized target collector result with test-machine role, actual model/chip/board/firmware/RAM/CPU observations and optional private-capture fingerprint |
| `source_lock` | Copy byte-identical to the operator-selected project lock |
| `source_profile` | Explicit `xnu_source`, pinned `revision`, `board_config: "T8103"`, `architecture: "arm64e"`, `boot_args_profile` |
| `build_inputs` | Hashed source instrumentation and recipe files, plus patches/toolchain records as needed; each has distinct `label` and `role` |
| `build_id` | SHA256 of canonical source profile and sorted `{label,role,sha256}` input records |
| `kernel` | Actual unstripped thin ARM64e MH_EXECUTE kernel, unique LC_UUID, executable checkpoint start/end symbols and one loadable embedded build-ID marker |
| `build_receipt` | Successful source recipe status/exit, exact source profile, build ID and kernel SHA256; actual source build logs remain available for review |
| `boot_object` | Exact bytes supplied to target authorization |
| `package_receipt` | Successful package result binding boot-object SHA256, included kernel SHA256 and kernel UUID |
| `boots` | Distinct witnessed sessions, each with nonce, cold/warm mode, raw serial, passive capture receipt, authorization receipt and optional handoff captures |

Allowlisted ABI pairs are `xnu` with `apple-xnu-12377` and
`puredarwin-vm-next` with `puredarwin-xnu-7195`, matching the lock and
[reviewed handoff contract](../../platform/apple-m1/handoff/contract.json).
The latter describes a possible native build of the pinned older source;
it does not permit the existing VIRT artifact. The layouts differ despite
sharing Version 2 / Revision 2. Source profile is mandatory; automatic ABI
inference is forbidden. Future revisions need a reviewed profile update.

The source instrumentation and recipe must produce two real labels:
`_blauos_physical_checkpoint_entry` and
`_blauos_physical_checkpoint_entry_end`, delimiting at most 4096 executable
bytes. The loadable image must contain exactly one NUL-terminated
`BLAUOS_PHYSICAL_BUILD_ID=<computed build_id>` marker. Build ID calculation
excludes output hashes to avoid a self-referential hash; the separate build
receipt binds the resulting kernel bytes. The current native source does
not yet emit this protocol or these labels. A future emitter must measure
events at their real implementation sites and remain independently reviewable.

An event is a complete serial line with this **proposed format**, where the
payload is a single JSON object:

```text
BLAUOS_PHYSICAL <fresh boot nonce> <event name> <measured JSON object>
```

The validator rejects stale nonces, duplicate/unknown events and shell-prefixed
markers. Generic Darwin banners are insufficient. These required payloads
describe observations; setting their fields does not itself prove hardware
execution.

| Event | Required measured payload |
| --- | --- |
| `entry` | `build_id`, actual `kernel_uuid`, `model: "Macmini9,1"`, `soc: "t8103"`, selected `identity_fingerprint_sha256`, `execution_environment: "native-iboot"`, hexadecimal `slide` and actual `pc` inside the checkpoint symbols after 16 KiB aligned slide |
| `handoff` | Explicit source `profile` and SHA256 of the captured `boot_args`, `device_tree`, `memory_plan` files |
| `mmu` | `current_el: 1`, `page_size: 16384`, measured hexadecimal `sctlr_el1`, `tcr_el1`, `ttbr0_el1`, `ttbr1_el1`, and actual `translation_readback: "matched"` |
| `exception` | `kind: "controlled-brk"`, measured hexadecimal `esr_el1`, `elr_el1`, `far_el1`, and `recovered: true` after actual controlled BRK recovery |
| `timer` | Actual integer `frequency_hz`, `counter_before`, `counter_after`, positive `delivered` callback count |
| `interrupt` | `controller: "apple-aic"`, observed integer `irq` and positive `handled_count` |
| `cpus` | `boot_cpu: 0`, actual `online` CPU IDs 0 through 7 after secondary startup |
| `checkpoint` | `status: "controlled-stop"` from the intended initialization checkpoint |

The inventory fingerprint in `entry` links the selected lab record to the
session. It is an identifier supplied to the experiment, not a kernel-made
hardware attestation. For the full checkpoint, the validator also parses
the actual Apple device tree and checks model/board compatibility and the
`/chosen/chip-id` value. Asahi's
[source device-info routine](https://github.com/AsahiLinux/m1n1/blob/main/src/main.c)
reads root `model`, `target-type` and `/chosen/chip-id`; this is source
evidence for where to look, not a target capture.

Each boot object in `boots` includes `nonce`, `mode: "cold"|"warm"`, hashed
`serial`, `capture_receipt`, `authorization_receipt`, and for full checks,
hashed `boot_args`, `device_tree`, `memory_plan`. The normalized authorization
receipt records `schema_version: 1`, `environment: "paired-1tr"`,
`security_mode: "permissive"`, `exit_code`, `boot_object_sha256`,
`policy_after.coih_sha384`, and hashed raw `transcript` / `policy_capture`
references. A reviewer must reconcile this normalization with the actual
target tool output; it is not a cryptographic validation of LocalPolicy.
Never substitute the boot-object SHA256 for Image4-manifest `coih` SHA384.

Offline entry validation:

```sh
python3 scripts/platform/physical_evidence.py validate \
  .blauos/lab/m1/evidence/manifest.json --level entry \
  --output .blauos/lab/m1/evidence/entry-validation.json
```

For the optional private identity check, explicitly provide
`--private-platform-plist .blauos/private/m1-platform.plist`. Only this explicit
input is read; public manifest paths never cause private identifier access.
The report verifies captured identity hash/nonce consistency without printing
UUID or serial. Absence of the private input leaves that check unverified.
Preserve the private file separately; it must not enter published evidence.

For repeatable full initialization, collect at least two cold boots and two
warm boots with distinct fresh challenges and capture receipts, then use
`--level checkpoint`. The checks require measured registers, actual exception
and interrupt delivery, all supported CPUs, explicit RAM/reservations and
handoff/hash agreement. They reuse the bounded offline decoder; they do not
derive a free-memory map or certify that MMIO/firmware reservations are complete.
Warm/cold labels and physical transport remain operator claims until reviewed.

Exit 0 means **offline evidence consistency only**. Exit 2 identifies a missing
or inconsistent requirement. Every report keeps
`hardware_attestation_verified: false`, `physical_gate_passed: false` and
`review_required: true`, including synthetic unit-test fixtures. A named
reviewer must independently inspect source/recipe, actual operator/physical
session evidence, artifact inclusion, symbolicated checkpoint, measured
hardware behavior, cold/warm power transitions, failures and recovery. Record
the reviewer's explicit decision and exact manifest hash in the gate issue.
Do not close Gate 2 or #39 on an offline report alone.

Capture receipts also bind the reviewed passive collector's source SHA256.
For historical receipts, select the exact preserved source snapshot with
`--capture-collector`; the current collector is the default. This preserves
versioned evidence without treating arbitrary producer code as trusted.
