The text command layer builds upstream BSD programs for the independent ARM64
Darwin runtime. It adds `cat`, `pwd`, `hostname`, `head`, `tail`, `wc`, `cut`,
`tr`, `uniq`, `tee`, `printenv`, `basename`, `dirname`, `mktemp`, `comm`, `paste`,
`rev`, `fold`, `expand`, `unexpand`, `grep`, and `sed`.

The first twenty programs use the locked PureDarwin source tree. `grep` and
`sed` use Apple's locked `text_cmds-199` source. The BSD `grep` retains its
matching engine, regular expressions, context output, recursive traversal,
status codes, and Darwin behavior. Its optional compressed-input support is
excluded with an explicit source patch and build macro, because this profile
has no source-built zlib, bzip2, or xz libraries. `-J`, `-M`, `-X`, `-Z`, and
their long option equivalents are rejected. Plain files and standard input
remain supported.

`grep` privately links PureDarwin's genuine libc `fts.c` and matching `fts.h`;
the SDK's current FTS header is not substituted for that ABI. It shares the
file command layer's reviewed source amendments: remove an unused private
malloc header, use libc assertions, declare missing public-header functions,
and call XNU's real directory-attribute syscall. The overflow-checked
`reallocarray` adapter calls the guest's real `realloc`. There are no simulated
commands or traversal results.

Build with a fresh output directory:

```sh
python3 scripts/vm/build_text_commands.py \
  --source .blauos/sources/puredarwin-vm-next \
  --apple-source .blauos/sources/apple-text-commands \
  --runtime-root .blauos/experiments/virtualbuddy-platform/runtime-final-root \
  --output .blauos/experiments/command-tools/text-new
```

The builder verifies both source commits and trees, refuses dirty sources and
existing outputs, and emits an additive `root/`, `provenance.json`,
`commands.json`, and `build.log`. Compilation uses SDK headers with `-Z` and
`-nostdlib`; the only runtime library is the supplied independently built
`libSystem.B.dylib`. Source, amendments, compiler, SDK settings, runtime inputs,
and executable hashes are recorded. The original runtime is never staged into
or modified by this builder.

Successful compilation records `guest_execution_verified: false`. Actual
guest behavior is verified separately by `vm/commands/smoke.sh` through an
authenticated, unprivileged SSH session. A matching build receipt and the
guest test result are both required before describing the commands as working.
Component copyright and license notices in the pinned source files apply,
including the Apple Public Source License amendments to libc FTS.
