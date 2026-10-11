# Administrative commands

These commands compile official BSD source against the independently built
Darwin runtime. The build does not copy macOS executables or runtime libraries.

| Installed commands | Pinned source |
| --- | --- |
| `/usr/bin/id`, `/usr/bin/whoami`, `/usr/bin/groups` | Apple `shell_cmds-216.60.1`, `id/id.c` |
| `/usr/bin/env` | Apple `shell_cmds-216.60.1`, `env/env.c`, `env/envopts.c` |
| `/bin/date` | Apple `shell_cmds-216.60.1`, `date/date.c`, `date/vary.c`, `date/netdate.c` |
| `/bin/ps` | Locked PureDarwin snapshot, `src/Userspace/ps` |

The identity programs select their normal BSD behavior from their executable
name. They are separate executable files so image composition does not depend
on symlink preservation.

`getgrouplist_2.c` implements Apple's allocating group-list SPI using the
runtime's actual public `getgrouplist` account lookup. Account names and
memberships come from the guest's own account database.

`date-optional-utmpx.patch` and `optional_utmpx.c` look up the legacy clock-change
accounting provider through `dlsym` and call it when available. The clock operation remains the real
`settimeofday` syscall and keeps its privilege checks. The normal BSD date
options, including formatting, UTC, epoch input and `-j -f` parsing, remain
upstream code. Clock-change accounting is not supplied by this initial image.

The upstream copyright and license notices remain in the fetched source.
Build receipts record its pinned commit, all compiled files, compatibility
amendments, toolchain inputs and runtime hashes. Compilation and dependency
inspection do not establish that a command works in a guest; the integrated
SSH acceptance workflow provides that evidence.

Build with `python3 scripts/vm/build_admin_commands.py --runtime-root ROOT
--output FRESH`. Optional `--source` chooses the locked Apple shell commands
checkout and `--darwin-source` chooses the locked PureDarwin checkout.
