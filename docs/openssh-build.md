# Independent OpenSSH build

BlauOS cross-builds OpenSSH portable 10.6p1 from the upstream `V_10_6_P1` release
commit `a8c72b842423f0774f542d3c2197247c3dd28d74`. The source lock records this input.
Apple's compiler and SDK provide build tools and declarations; target executables
link against BlauOS's independently built Darwin runtime. They do not use the
macOS host's runtime libraries. [Upstream release notes](https://www.openssh.org/releasenotes.html)

```sh
python3 scripts/blauos.py fetch openssh-portable
python3 scripts/build/openssh.py \
  --runtime-root .blauos/experiments/virtualbuddy-platform/runtime-final-root \
  --output .blauos/build/openssh
```

The output root contains `/usr/sbin/sshd`, `/usr/libexec/sshd-auth`,
`/usr/libexec/sshd-session`, `/usr/libexec/sftp-server`, `/usr/bin/ssh-keygen`,
and `/usr/bin/ssh`. OpenSSH 10.6 requires both server helper executables;
copying `sshd` alone is insufficient.

The builder refuses dirty or unpinned source and existing output directories.
`inputs.json`, `build.log`, `config.h`, and `provenance.json` record the actual
configuration, compiler, independent runtime hashes, patches, and every installed
artifact hash. `-Z -nostdlib` removes default target library searches, and a final
load-command check permits only the source-built `libSystem.B.dylib` and its
`libdyld.dylib` provider. The target's interpreter remains `/usr/lib/dyld`.
Configure runs in cross-compilation mode and target programs are never executed
on the build host.

The initial crypto profile uses `--without-openssl --without-zlib`. Its genuine
crypto implementations are compiled from the pinned OpenSSH tree: Ed25519 keys,
X25519 and hybrid key exchange, ChaCha20-Poly1305, AES-CTR, and upstream hash
implementations. RSA, ECDSA, PKCS#11, FIDO keys, and transport compression are
absent. Provision an Ed25519 authorized key for the first login. Upstream labels
the internal crypto profile experimental; extending it with a source-built
libcrypto is a separate profile, rather than borrowing the host's library.
[Upstream build instructions](https://github.com/openssh/openssh-portable/blob/V_10_6_P1/README.md)

Privilege separation remains enabled: the listener and monitor retain only the
privileges their roles require, the network-facing authentication helper drops
to the dedicated `sshd` account and chroots into `/var/empty`, and the authenticated
session drops to the named user. The profile selects upstream's resource-limit
sandbox because BlauOS does not supply Apple's proprietary seatbelt runtime.
[Upstream privilege separation](https://github.com/openssh/openssh-portable/blob/V_10_6_P1/README.privsep)

Two patches are explicit build inputs. The Darwin compatibility patch matches
the SDK's integer `rresvport_af` argument in OpenSSH's fallback and declares the
source runtime's `setresuid`/`setresgid` compatibility functions, which the SDK
does not provide. These existing runtime operations call real Darwin credential
syscalls; the OpenSSH patch supplies no replacement credential operations. The cross
configure patch permits `BLAUOS_CROSS_RLIMIT_SELECT=yes` to select the upstream
rlimit sandbox. The unmodified cross configure branch assumes failure without
running a guest test. This build assumption must be verified in a guest child
process with zero file-descriptor and file-size limits before accepting SSH.

Successful compilation alone does not prove account lookup, privilege dropping,
PTY operation, cryptographic entropy, or authentication. Build receipts keep
guest verification false. Acceptance requires independent runtime probes and a
real encrypted connection, including refusal of unauthorized keys, a non-root
interactive session, and reboot persistence. Host keys must be generated inside
the guest only after its entropy source passes source inspection and runtime
checks; images must not embed shared private host keys.
