# Dedicated M1 lab

Tracks [#25](https://github.com/joeblau/blauos/issues/25). This is a preparation
runbook; a dedicated lab and recovery procedure have **not** been verified.
The read-only [build-host inventory](../../evidence/native/lab/build-host-inventory-redacted.json)
identifies an M3 Max MacBook Pro, not the required 2020 M1 Mac mini. An actual
run of the collector with `--role test-machine` on that host returned exit 2
(`wrong-hardware`). No host boot policy or disk layout was changed.

## Select and identify the target

Use the [scope contract](../scope.md): `Macmini9,1`, Apple M1/T8103, board
J274, with the standard Ethernet configuration. Record the lab operator,
recovery operator, physical location, remote access and serial host in a
private lab record. Assign these roles to real people before an experiment.
Network names such as “Mini” do not establish the model or dedicated ownership.

Run the collector on the selected machine while it is still running known-good
macOS. Use a new output filename for each inventory. These commands are
read-only; the inventory does not establish independent XNU execution.

```sh
python3 scripts/platform/lab_inventory.py \
  --role test-machine \
  --output .blauos/lab/m1/inventory.json
```

Exit 0 identifies matching observed hardware, with lab verification still
pending. Exit 2 rejects a different model, chip or board. Preserve firmware,
macOS build, RAM, CPU counts, NVMe and Ethernet observations; empty fields need
an additional read-only probe. `expected_soc` describes the scope, not a
measured SoC identifier.

For the optional private machine-identity cross-check, generate a nonce with
`python3 -c 'import secrets; print(secrets.token_hex(32))'` and pass it explicitly:

```sh
python3 scripts/platform/lab_inventory.py \
  --role test-machine \
  --output .blauos/lab/m1/inventory-with-identity.json \
  --private-platform-output .blauos/private/m1-platform.plist \
  --identity-nonce REPLACE_WITH_FRESH_64_HEX_CHARACTERS
```

Public serial-device observations contain redacted device classes; exact
adapter paths may contain identifiers and belong in the private lab record.
The private plist contains device identifiers. The collector writes it with
mode 0600, refuses overwrite and rejects public repository paths. Share only
the sanitized inventory after review. The salted fingerprint supports later
consistency checks; it is not hardware attestation. Keep the private plist
outside the evidence published to GitHub.

## Verify recovery before installation

The lab record must include actual evidence for each item, not an unchecked
assumption:

1. A current backup and a demonstrated restore of representative data, with
   known-good macOS boot available. Identify what an eventual full restore
   would erase and where the backup is held privately.
2. A volume owner who can authenticate in paired recoveryOS. Record the
   selected test OS/volume group and its current per-OS security policy. Do
   not copy account credentials or recovery secrets into GitHub evidence.
3. A physical operator able to hold the power button, enter startup options,
   select known-good macOS and reach Recovery Terminal. [Apple's Recovery
   guide](https://support.apple.com/guide/mac-help/mchl82829c17/mac) describes
   this workflow. Verify it on the actual target.
4. A second Mac with macOS 14 or later, Internet access, sufficient free
   space and an appropriate USB-C cable for firmware recovery. Apple's
   [revive/restore procedure](https://support.apple.com/en-us/108900) specifies
   a cable supporting data and charging and excludes Thunderbolt 3 cables.
   Verify Finder can detect the target in DFU without starting a restore.
5. Record the exact firmware and OS pairing, the tested return to known-good
   boot, and who performed each verification. A firmware revive preserves
   data; a firmware restore erases it. Restore is a separate deliberate
   recovery action, not an automatic diagnostic step.

Apple's [DFU port table](https://support.apple.com/en-us/120694), published
2026-09-21, identifies the M1 Mac mini port as the leftmost USB-C port while
facing its rear ports. Asahi identifies the same port as nearest the power
plug. Recheck the table if the target model changes.

## Qualify the debug connection

The [Asahi serial-debug reference](https://asahilinux.org/docs/hw/soc/serial-debug/)
documents a **1.2 V UART** enabled through vendor USB-PD messages on that DFU
port. A conventional USB serial cable is insufficient. Choose a qualified
level-shifting/PD transport or a separately tested host arrangement. Its
two-M1 `macvdmtool` method requires a USB SuperSpeed cable carrying SBU signals;
the current M3 build host is not verified by that guide. Commands such as
`reboot serial` actively reboot the target and are not inventory commands.

The serial connection and Apple DFU recovery have different cable requirements
and may compete for the same port. Record and rehearse switching between them.
Enumerate devices on the actual capture host and verify the selected device
against target activity. A pre-existing `/dev/cu.debug-console` on the build
host does not prove that an M1 target is attached.

Once the native UART transport is independently qualified, start bounded
capture before the operator begins the boot experiment:

```sh
python3 scripts/platform/capture_serial.py \
  --device /dev/cu.REPLACE_WITH_VERIFIED_TARGET_UART \
  --transport native-uart \
  --baud 115200 --seconds 600 \
  --output .blauos/lab/m1/boot-001
```

The collector sends no application bytes or reset commands. Opening a serial
port can affect adapter control signals; transport qualification must cover
that behavior. It preserves raw `serial.log`, chunk timestamps and hashes in
`capture.json`, and restores terminal settings when possible. The collector
also bounds metadata at 16,384 chunks. A timeout, byte limit or chunk limit is
a capture outcome, not successful boot. Keep raw logs private
until reviewed for device identifiers, account data and secrets.

[m1n1 USB consoles](https://asahilinux.org/docs/sw/m1n1-user-guide/) appear only
after m1n1 is running and have separate proxy/hypervisor roles. Label such
captures `--transport m1n1-usb`; they do not establish native Path A or earliest
physical UART output. `synthetic-pty` is reserved for software tests.

## Record each physical experiment

Use the [native boot experiment](native-boot.md) and
[checkpoint evidence](m1-checkpoint.md) contracts. Preserve the exact source
profile, recipe, artifact hashes, tool versions, selected volume group,
operator commands, firmware/OS pairing, unique boot nonce, raw log and
recovery result. Record boot-chain authorization separately from execution of
the independently built kernel. Stock macOS inventory, generic Apple banners
and virtual guest logs do not satisfy that distinction.

Before an install attempt, inspect a genuine source-built native candidate
and verify the lab record above. Path A remains the first experiment. Lack of
hardware access does not activate the m1n1 fallback. The milestone remains
open until physical independent XNU checkpoints and repeated recovery pass.
