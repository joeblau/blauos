# Local accounts for the SSH workflow

The first login profile uses a named, non-root account and an Ed25519 public key.
Provisioning happens in a fresh offline filesystem tree before image formatting.
The account files and home directory then live on the guest's persistent root
filesystem. Rebooting that disk preserves them; rebuilding a disk creates a new
filesystem and does not migrate account data automatically.

The initial defaults are:

| Account | UID/GID | Home | Shell | Purpose |
| --- | --- | --- | --- | --- |
| `joeblau` | `1000/1000` | `/var/home/joeblau` | `/bin/sh` | Public-key SSH login |
| `sshd` | `74/74` | `/var/empty` | `/bin/false` | OpenSSH privilege separation |

Existing `root`, `nobody`, other local accounts, and group records are preserved.
The login account gets its own primary group and no membership in `wheel`.
Provisioning grants no root escalation mechanism.

## Prepare an account

After composing the independent runtime and source-built SSH layer into an
offline root, run:

```sh
python3 scripts/vm/accounts.py provision \
  --root /path/to/fresh/root-staging \
  --username joeblau --uid 1000 --gid 1000 \
  --public-key "$HOME/.ssh/id_ed25519.pub" \
  --receipt /path/to/build/account.json
```

The input is a **public** key file. Keep the private key on the client. This
initial profile accepts one to 16 bare `ssh-ed25519` keys and rejects private
keys, malformed SSH wire encodings, and `authorized_keys` option prefixes.
Comments are omitted from the installed keys. No host passwd database or
DirectoryService records are imported.

The receipt records the account IDs, paths, modes, and SHA-256 key fingerprints.
It contains no public-key text or private-key material and does not claim a
successful guest login. Use a new receipt filename for each build.

An identical account and key set can be provisioned again without resetting
home contents or credentials. Conflicting account names, numeric IDs, group
records, or existing `authorized_keys` cause a failure before the offline tree
is changed. Key rotation needs an explicit future account-management operation.
Never edit the disk while VirtualBuddy is using it.

## Apply guest ownership during formatting

Host UID/GID values are not the guest's account IDs. The image assembler resets
its staging copy to root ownership under `fakeroot`, so the account metadata
must be applied **after that reset and in the same fakeroot session that runs
`mke2fs`**:

```sh
python3 scripts/vm/accounts.py apply-metadata \
  --root /path/to/fresh/root-staging \
  --receipt /path/to/build/account.json
```

This command is an image-builder step, executed inside its existing fakeroot
session. Do not run it with `sudo` against host directories. Only the exact
validated account paths can receive ownership changes; symlinks and path escapes
are rejected. The home and `.ssh` directory use mode `0700`, `authorized_keys`
uses `0600`, and all three belong to the named account. `/var/empty` remains an
empty root-owned directory with mode `0755`.

`/etc/passwd`, `/etc/group`, and `/etc/shells` are root-owned and readable at
`0644`. `/etc/master.passwd` is root-owned at `0600`. All passwd/master.passwd
records have matching identities, homes, and shells. The public password field
is `*`; the new login account's private field is the impossible password marker
`*NP*`. It is not an empty password or a stored credential. The SSH service must
also disable password and keyboard-interactive authentication and require
public-key authentication. `root` login remains disabled by SSH configuration.

## Runtime contract and validation

The pinned PureDarwin libinfo implementation reads ten-field
`/etc/master.passwd` records for effective UID 0 and seven-field `/etc/passwd`
records for other users. Its flat-file group lookup supplies group membership.
Both views must agree before OpenSSH can safely look up an account, call
`initgroups`, and switch UID/GID. Source:
[pinned libinfo file module](https://github.com/PureDarwin/PureDarwin/blob/1bbaeb2ee6aa41e6f792a0b1e2943cc295b3b290/src/Libraries/XPC/libinfo/file_module.c).

OpenSSH checks account eligibility separately from key validity. The selected
OpenSSH 10.6p1 Darwin build has no `LOCKED_PASSWD_*` configuration macros, so
`*NP*` permits public-key account eligibility without supplying a usable
password. The source-built profile uses the explicit `sshd`
privilege-separation user and `/var/empty` chroot. See
[pinned OpenSSH account checks](https://github.com/openssh/openssh-portable/blob/a8c72b842423f0774f542d3c2197247c3dd28d74/platform.c)
and [sshd configuration](https://man.openbsd.org/sshd_config).

Host provisioning tests verify preservation, ID collision rejection, public-key
validation, restrictive modes, and safe paths. Guest acceptance must separately
prove root and non-root account lookup, actual UID/GID transitions, home writes,
denied access to root-only files, rejected unauthorized keys, interactive PTY
commands, and retained account data after reboot. Creating these files alone
does not establish any of those guest properties.
