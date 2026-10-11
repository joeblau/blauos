# Command-line tools

The bootstrap image initially provided only a shell and a handful of commands.
The account-enabled SSH image now builds a normal BSD command toolkit from
pinned Apple and PureDarwin sources, using the independently built Darwin
runtime. The Mac supplies compiler tools and SDK headers; its executables and
runtime libraries are not copied into the guest.

The [recorded acceptance](../evidence/virtualization/command-tools/checkpoint.json)
passed all 16 image workflow stages, both actual guest boots, and 187 host tests.
The running account also passed all 40 command checks. A second account update
preserved its existing nonempty profile. Private profiles, keys, and disk images
are excluded from the public evidence.

The toolkit includes:

| Purpose | Commands |
| --- | --- |
| Files and directories | `ls`, `cp`, `mv`, `rm`, `mkdir`, `rmdir`, `ln`, `chmod`, `touch`, `stat`, `du`, `df` |
| Reading and processing text | `cat`, `head`, `tail`, `wc`, `cut`, `tr`, `uniq`, `tee`, `grep`, `sed`, `comm`, `paste`, `rev`, `fold`, `expand`, `unexpand` |
| Paths and environment | `pwd`, `basename`, `dirname`, `mktemp`, `printenv`, `env`, `hostname` |
| Accounts and processes | `id`, `whoami`, `groups`, `date`, `ps` |

The original `uname`, `sleep`, `echo`, `test`, `true`, `false`, shell, and SSH
tools remain available. The shell also supplies `cd`, `printf`, and other POSIX
builtins. This toolkit does not yet include an editor, `sort`, a package
manager, or a guest zsh.

## Build and include in images

The [SSH image workflow](ssh-login.md) builds this layer automatically before
auditing and assembling the account-enabled image. It tests actual directory
listings, recursive copies, symlinks, hard links, permissions, error statuses,
text pipelines, search, environment passing, and account identity on both
guest boots. The resulting images install utilities under `/bin` and
`/usr/bin`, already on the default login PATH.

To build only the additive command layer on the prepared macOS build host:

```sh
python3 scripts/blauos.py fetch puredarwin-vm-next apple-file_cmds apple-libutil apple-shell-cmds apple-text-commands
python3 scripts/vm/build_commands.py \
  --runtime-root /path/to/independently-built-runtime \
  --output .blauos/build/commands/run-1
```

The output contains `root/`, exact source provenance, compiler commands, and a
command catalogue. Existing inputs and build directories are preserved. A
successful build alone does not establish that the programs run; the full
runtime audit and actual guest acceptance are required.

## Update an existing logged-in VM

The installer uses the already authorized SSH account and a previously trusted
host key. It first audits the command layer with the supplied independent
runtime, uploads a separate versioned directory to the account's home via SFTP,
downloads the executables again to verify their hashes, and executes the guest
acceptance script before changing PATH. It preserves existing `.profile`
content and refuses symlinked managed paths or a profile changed during the
installation. It does not stop the VM or write to its running disk from the
host.

```sh
python3 scripts/vm/install_commands.py \
  --commands-root .blauos/build/commands/run-1/root \
  --provenance .blauos/build/commands/run-1/provenance.json \
  --runtime-root /path/to/the-guest-independent-runtime \
  --runtime-provenance /path/to/runtime-provenance.json \
  --identity "$HOME/.ssh/id_ed25519" \
  --known-hosts /path/to/trusted-known-hosts \
  --username joeblau --guest-ip 192.168.64.201 \
  --output .blauos/build/commands/install-1
```

Only the SSH client's identity filename is passed; the installer never reads
the private key contents. This update is scoped to the named account because
the live login account cannot write system directories. New images contain the
same tools in the system paths.

After installation, reconnect or refresh your current guest shell:

```sh
. "$HOME/.profile"
ls -la
```

Earlier versions stay under `$HOME/.local` until explicitly removed. To undo a
PATH update, restore the saved `original.profile` from the installer output
and start a new login session. Your account files and SSH host key are retained.

## Supported profile

The file utilities use BSD/POSIX behavior and source-built traversal/formatting
support. Apple-specific ACL, extended-attribute, membership, resource-fork,
color, and secure-delete features require additional providers; the explicit
file command profile documents their exclusions in
[`vm/commands/file`](../vm/commands/file/README.md).
The grep build handles plain files and regex/recursive searches; compressed
input switches are excluded. Date formatting and clock privilege checks use
the upstream implementation; historical clock-change accounting is optional.
See the tracked compatibility patches and the
[administrative command notes](../vm/commands/admin/README.md).
