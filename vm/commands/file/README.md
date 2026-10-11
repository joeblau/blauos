# Source-built file commands

This profile builds `ls`, `cp`, `mv`, `rm`, `mkdir`, `rmdir`, `ln`, `chmod`,
`touch`, `stat`, `df`, and `du` as ARM64 Darwin executables linked exclusively
to the independent `/usr/lib/libSystem.B.dylib`. It neither copies macOS
command binaries nor changes the accepted runtime root. Guest behavior must
be validated separately; successful compilation is not guest evidence.

The official Apple sources are deliberately pinned to `file_cmds-272.250.1`
(`1ef2ec6f0f57762a907001ad6ff4e967db79e1a6`). Its BSD implementations expose
the ordinary traversal, copy, rename, permissions, timestamps, and disk-space
operations without requiring proprietary platform integrations. This is a
compatibility baseline, not a claim that this historical release has modern
macOS feature coverage or has received current vulnerability fixes.

The selected runtime lacks exported FTS traversal, `strmode`, `fflagstostr`,
and `getbsize`. The builder compiles their genuine source implementations
from locked PureDarwin into individual executables, together with Apple's
source `lchmod` implementation where needed. Locked `libutil-73`
(`0f4264d617b0a94d8905521fa444e2aa215f0a4d`) supplies `humanize_number` for
`ls -h` and `df -h`. No extra shared libraries are introduced.

`libc-support.patch` removes an unused private malloc include and maps
the source assertions to standard `assert`. FTS retains its actual XNU
`getattrlistbulk` call and upstream `opendir`/`readdir` fallback.
`syscalls.c` supplies genuine XNU calls for `chflags`, `setattrlist`, and
`getattrlistbulk`, plus overflow-checked `reallocarray`. `fortify.c` retains
bounds and overlap checks for the SDK-generated `__strncat_chk` call.
The compiler uses SDK headers, `-nostdlib`, and `-Z`; load-command inspection
rejects any dependency other than the independent libSystem.

## Supported profile and limits

Ordinary listings, recursive traversal, regular file and symbolic link
copying, directory creation/removal, hard links, renaming, numeric/symbolic
permissions, timestamps, stat formatting, and disk usage use the upstream
implementations. `cp -p` and `cp -a` preserve POSIX permissions, ownership,
and access/modification times where permitted. They do not preserve ACLs,
extended attributes, resource forks, or other macOS metadata. `cp -X`
explicitly selects that same metadata-free profile. Symlink mode changes
go through real XNU `setattrlist`; unsupported filesystem operations return
the actual kernel error.

These unavailable options fail with a nonzero exit status before modifying
files: `cp -c` (clones), `ls -G` (color), `ls -e` (ACL display), `ls -@`
(extended attributes), `rm -P` (secure erasure), and `rm -W` (whiteouts).
The BSD `chmod` option parser rejects Apple's ACL options, including `-N`,
and the BSD mode parser rejects ACL expressions. Encountering a whiteout
during removal returns `ENOTSUP`. Extended metadata support is deliberately
excluded by `file_cmds.patch`; no fake success-returning ACL functions exist.

`mv` uses the real `rename` fast path. Its upstream fallback for directory
moves across filesystems runs source-built `cp` and `rm` through `PATH`, so
a per-account tool installation works before a new full image is installed.
Cross-filesystem moves therefore require these tools in `PATH`. Ordinary
regular-file cross-filesystem moves retain the upstream direct copy path.

`stat` uses BSD format strings, for example `stat -f '%z %Sp' file`.
The initial guest profile is headless and does not ship a termcap database.

## Build and provenance

```sh
python3 scripts/vm/build_file_commands.py \
  --source .blauos/experiments/command-tools/file-source-272 \
  --darwin-source .blauos/sources/puredarwin-vm-next \
  --libutil-source .blauos/experiments/command-tools/libutil-source \
  --runtime-root .blauos/experiments/virtualbuddy-platform/runtime-final-root \
  --output .blauos/experiments/command-tools/file-final
```

The output must be fresh. Every source checkout must match the revision and
tree in `sources.lock.json` and be pristine. The additive `root/` contains
twelve separate executable files, with no command aliases or libraries.
`commands.json` records the actual compiler invocations, `build.log` records
diagnostics, and `provenance.json` records artifact hashes, source/provider
pins, exact source/header hashes, workspace amendments, compiler identity,
and runtime link inputs. Failed builds preserve their failure evidence and
do not alter earlier output directories.

## Licenses

The upstream files retain their original copyright and license notices in
the pristine checkout and explicit build overlays. Apple file commands use
a mixture of BSD licenses and Apple Public Source License notices; several
historical BSD files include an advertising acknowledgement. PureDarwin's
selected libc files likewise include BSD and APSL-2.0 notices. Apple's
`humanize_number.c` uses a BSD license with The NetBSD Foundation's copyright.
The local bridge files use MIT. Binary distribution materials must retain
the source notices and the applicable licence terms; the builder emits
`third-party-notices.txt` alongside its provenance for this purpose.

This product includes software developed by the University of California,
Berkeley and its contributors.

Primary sources: [Apple file_cmds](https://github.com/apple-oss-distributions/file_cmds/tree/file_cmds-272.250.1),
[Apple libutil](https://github.com/apple-oss-distributions/libutil/tree/libutil-73),
and the PureDarwin source pinned by `sources.lock.json`.
