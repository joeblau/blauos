# Root filesystem decision record

Audit date: 2026-10-11. Tracks
[#47](https://github.com/joeblau/blauos/issues/47). The machine-readable record
is [evidence/native/rootfs-decision.json](../../evidence/native/rootfs-decision.json).
This is a source-and-constraint decision only: no new guest run, build or
physical experiment was performed, and nothing here is physical M1 evidence.
Gate 2 remains NOT_RUN ([evidence/gate-2.json](../../evidence/gate-2.json)).

## Decision

**ext4 is the root filesystem for prototype images** — the PureDarwin-authored
`com.apple.filesystems.ext4` kext from the pinned `next` tree
(`1bbaeb2ee6aa41e6f792a0b1e2943cc295b3b290`, `src/Kernel/Extensions/ext4`).
The scope of this decision is the QEMU VIRT prototype and the native prototype
image plan. The physical M1 disk arrangement is explicitly **not decided**;
it is hardware-gated behind lab availability, storage drivers and firmware
experiments (constraints FS-C7 to FS-C9 below).

Revisit triggers: physical evidence that iBoot/LocalPolicy requires the custom
OS root inside an APFS container; a source-available APFS or HFS+
implementation reaching build and mount validation in this repository; or a
Gate 2 filesystem-related blocker.

## Candidate comparison

| Candidate | Source state in pinned inputs | License | Built/validated here | Decision |
| --- | --- | --- | --- | --- |
| ext4 | PureDarwin-authored read-write kext: extents, 32/64-bit block numbers, `metadata_csum` maintenance, JBD2 journaling with replay (`src/Kernel/Extensions/ext4/ext4.h`) | PureDarwin BSD-style (`PUREDARWIN_LICENSE.txt`) | Built as an ARM64 kext ([kexts receipt](../../evidence/kernel/kexts/result.json)); actual VIRT guest mounted a genuine ext4 root as `/dev/disk0s2` and retained writes ([virtualization doc](../virtualization.md)) | **Chosen** |
| HFS+ | Apple-derived core (BTree/catalog/extents) with a PureDarwin shim at `src/Kernel/Extensions/hfs`; the pinned modern `xnu-12377.121.6` contains **no** `bsd/hfs` implementation at all (only miscfs, VFS infrastructure, NFS client) | APSL-2.0, Apple copyright 1999-2017 headers | Not in the built kext set ([command.json](../../evidence/kernel/kexts/command.json)); no source-built `newfs_hfs`/`fsck_hfs` tooling in this repo | Deferred |
| APFS (Apple) | Not published in any pinned open-source input; APFS strings in `bsd/vfs` are VFS integration points, not an implementation | Proprietary | Cannot be built from audited source | Rejected as a rootfs (unavailable as source) |
| APFS (PureDarwin scaffold) | ~3100-line version-0.1 scaffold (`src/Kernel/Extensions/apfs`): container/volume structures, B-tree reader, mount path logging "mounted scaffold read-write"; `apfs_parse_disk_minor` returns ENOTSUP for any disk other than `disk0` | PureDarwin-authored | Never compiled or mounted by this repository's CI or guest | Rejected (incomplete prototype) |
| msdosfs | BSD-derived FAT support in the pinned tree | BSD notices | The ESP is FAT32, created by `mkfs.fat` in `scripts/vm/assemble.py`; it is a boot-asset partition | Not a rootfs candidate |

Licensing notes: ext4 and msdosfs carry no Apple APSL obligations; HFS+ reuse
would require APSL-2.0 notice retention per the
[source dependency audit](source-dependencies.md); Apple's APFS driver and any
KDK/firmware artifacts are proprietary inputs that must be disclosed, not
described as reproducible public inputs, per the
[release contract](../scope.md).

## Apple Silicon boot constraints

Each constraint is labeled **documented** (Apple documentation or read-only
host tooling inspection), **validated** (observed in this repository, virtual
platform only) or **hardware-gated** (requires the physical Macmini9,1 lab;
no such machine exists in the project).

| ID | Constraint | Status | Basis |
| --- | --- | --- | --- |
| FS-C1 | Per-installed-OS LocalPolicy; custom XNU requires Permissive Security via paired 1TR with owner authorization; signatures remain checked | documented | Apple Platform Security citations in [native-boot.md](../platform/native-boot.md) |
| FS-C2 | LocalPolicy `vuid` identifies the selected **APFS volume group**; boot policy on internal storage binds to APFS volume-group identity | documented | Apple LocalPolicy field reference cited in native-boot.md |
| FS-C3 | `kmutil configure-boot` is recovery-only, targets a system `--volume`; `coih` is a SHA384 of the CustomOS Image4 manifest | documented | Read-only host `kmutil.8`/help inspection (native-boot.md) |
| FS-C4 | macOS system volumes use a sealed system volume inside an APFS container; blauOS's intended model is an immutable system with separate writable data, with APFS containers/boot policy/recoverability determining the eventual disk arrangement | documented | [scope.md](../scope.md); native-boot.md forbids importing SSV-downgrade steps without an observed failure |
| FS-C5 | A block-device root is required: the pinned loader's `load_ramdisk` calls are commented out, so `rd=md0` cannot be accepted as working | validated (virtual) | [virtualization.md](../virtualization.md) loader inspection and image selection |
| FS-C6 | Source-built XNU + ext4 kext mounts a GPT ext4 root (`/dev/disk0s2`), retains writes, and carried the passing v11 PID 1/shell/runtime/network guest | validated (virtual) | virtualization.md and `evidence/virtualization/pid1-checkpoint/` |
| FS-C7 | Whether iBoot/LocalPolicy requires the custom OS root inside an APFS container/volume group on internal ANS storage, or permits a non-APFS root partition | **hardware-gated** | No physical M1 exists; requires target experiment with preserved policy captures |
| FS-C8 | Mounting any rootfs on physical internal storage needs ANS NVMe and DART/DMA drivers — P1, no source implementation, no physical validation | **hardware-gated** | [scope.md](../scope.md) P1 list; blocker HW-001 in [source-dependencies.md](source-dependencies.md) |
| FS-C9 | Actual APFS container behavior on a physical M1: container/volume creation for a custom OS, sealed-system implications for a non-macOS root, recoverability of the disk arrangement | **hardware-gated** | Requires the [m1-lab runbook](../platform/m1-lab.md) prerequisites; none verified |

The tension between FS-C2/FS-C4 (boot policy speaks APFS volume groups) and
the ext4 decision is real but unresolved by source: it concerns the
**container and boot-asset layer** on physical hardware, not the kernel's
root filesystem driver in the virtual prototype. It can only be settled by
the hardware-gated experiments above. No APFS write capability is available
from source, so an APFS-hosted arrangement would depend on Apple's proprietary
tools and must be recorded as such if ever adopted.

## Prototype image plan

Consistent with existing tooling; no new formatter is introduced.

1. **Current (validated, virtual):** `scripts/vm/assemble.py` assembles a GPT
   disk — partition 1 a FAT32 ESP (`EF00`) carrying `EFI/BOOT/BOOTAA64.EFI`,
   the kernel and `boot-args.txt`; partition 2 an ext4 root labeled
   `blauos-root`. Disk/root/ESP UUIDs are deterministic uuid5 values derived
   from the kernel, loader and provenance SHA-256 digests; account inode
   metadata is verified with `debugfs`; `result.json` records image and input
   hashes. `vm/smoke-image.nix` wraps the pinned upstream
   `image-arm64-virt-minimal-release` (ordinary ext4 root, since the loader's
   ramdisk path is unavailable) and injects source-built probes with a
   `runtime-provenance.json` receipt.
2. **Next (planned, not validated):** split the single ext4 root into an
   immutable ext4 system partition mounted read-only plus a separate writable
   ext4 data partition for `/var` and service state, matching the immutable
   model in scope.md. This requires `assemble.py` layout changes and a guest
   remount/write-guard proof before it may be claimed as validated.
3. **Physical (hardware-gated):** defer the M1 disk arrangement until the
   m1-lab prerequisites are met. Candidate arrangements — a non-APFS ext4 root
   if firmware permits it, or APFS-hosted boot assets with separate data
   volumes — are both unvalidated. ESP content contract (loader, kernel,
   boot-args) stays unchanged so existing collection and provenance tooling
   continues to bind image hashes.

## Honest limits

- The only validated filesystem evidence is the QEMU VIRT guest; virtual
  virtio-blk/ext4 success is not evidence for ANS-attached physical storage.
- The PureDarwin ext4 driver is young, project-scale code; its journaling and
  checksum claims come from its own source comments and one guest history,
  not from stress or power-fault testing.
- HFS+ and both APFS entries have zero build or runtime validation in this
  repository; the comparison is a source audit, not a benchmark.
