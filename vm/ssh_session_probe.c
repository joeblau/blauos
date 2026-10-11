/* Verify a real authenticated SSH session's non-root identity and filesystem access. */
#include <sys/types.h>
#include <sys/stat.h>
#include <errno.h>
#include <fcntl.h>
#include <pwd.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static int fail(const char *step)
{
    fprintf(stderr, "BLAUOS_SSH_SESSION fail %s errno=%d\n", step, errno);
    return 1;
}

int main(int argc, char **argv)
{
    if (argc < 4) return fail("usage");
    size_t name_length = strlen(argv[1]);
    if (!name_length || name_length > 32 || strspn(argv[1], "abcdefghijklmnopqrstuvwxyz0123456789_-") != name_length ||
        (argv[1][0] >= '0' && argv[1][0] <= '9') || argv[1][0] == '-') return fail("username");
    unsigned long ids[2];
    for (int index = 2; index <= 3; index++) {
        char *end;
        errno = 0;
        ids[index - 2] = strtoul(argv[index], &end, 10);
        if (errno || !*argv[index] || *end || ids[index - 2] < 1000 || ids[index - 2] > 60000) return fail("numeric_ids");
    }
    uid_t uid = (uid_t)ids[0]; gid_t gid = (gid_t)ids[1];
    int require_pty = 0, write_nonce = 0, require_challenge = 0;
    const char *nonce = NULL;
    for (int index = 4; index < argc; index++) {
        if (!strcmp(argv[index], "--pty") && !require_pty) require_pty = 1;
        else if (!strcmp(argv[index], "--challenge") && !require_challenge) require_challenge = 1;
        else if ((!strcmp(argv[index], "--nonce") || !strcmp(argv[index], "--expect-nonce")) && !nonce && index + 1 < argc) {
            write_nonce = !strcmp(argv[index], "--nonce");
            nonce = argv[++index];
            if (strlen(nonce) != 64 || strspn(nonce, "0123456789abcdef") != 64) return fail("nonce");
        } else return fail("usage");
    }
    if (getuid() != uid || geteuid() != uid || getgid() != gid || getegid() != gid) return fail("identity");
    gid_t groups[128];
    int count = getgroups(128, groups), own_group = 0;
    if (count < 0) return fail("groups");
    for (int index = 0; index < count; index++) {
        if (!groups[index]) return fail("privileged_group");
        if (groups[index] == gid) own_group = 1;
    }
    if (!own_group) return fail("primary_group");
    char home[128], cwd[1024];
    if (snprintf(home, sizeof(home), "/var/home/%s", argv[1]) >= (int)sizeof(home)) return fail("home_length");
    struct passwd *account = getpwnam(argv[1]);
    if (!account || account->pw_uid != uid || account->pw_gid != gid || strcmp(account->pw_dir, home) ||
        strcmp(account->pw_shell, "/bin/sh") || !getcwd(cwd, sizeof(cwd)) || strcmp(cwd, home)) return fail("account_home");
    struct stat attributes;
    if (lstat(home, &attributes) || !S_ISDIR(attributes.st_mode) || attributes.st_uid != uid ||
        attributes.st_gid != gid || (attributes.st_mode & 0777) != 0700) return fail("home_permissions");
    errno = 0;
    int descriptor = open("/var/root/blauos-ssh-denied", O_RDONLY | O_NOFOLLOW);
    if (descriptor >= 0) { close(descriptor); return fail("root_fixture_access"); }
    if (errno != EACCES && errno != EPERM) return fail("root_fixture_missing_or_other_error");
    errno = 0;
    if (setuid(0) != -1 || errno != EPERM || getuid() != uid || geteuid() != uid) return fail("root_identity_regain");
    if (require_pty) {
        if (!isatty(STDIN_FILENO) || !isatty(STDOUT_FILENO)) return fail("pty_stdio");
        descriptor = open("/dev/tty", O_RDWR | O_NOFOLLOW);
        if (descriptor < 0 || !isatty(descriptor) || tcgetpgrp(descriptor) < 0) {
            if (descriptor >= 0) close(descriptor);
            return fail("controlling_terminal");
        }
        close(descriptor);
    }
    char challenge[67];
    if (require_challenge) {
        alarm(30);
        if (!fgets(challenge, sizeof(challenge), stdin)) return fail("challenge_input");
        alarm(0);
        size_t length = strlen(challenge);
        while (length && (challenge[length - 1] == '\r' || challenge[length - 1] == '\n')) challenge[--length] = 0;
        if (length != 64 || strspn(challenge, "0123456789abcdef") != 64) return fail("challenge_encoding");
    }
    if (nonce) {
        descriptor = open(".blauos-ssh-persistent", (write_nonce ? O_RDWR | O_CREAT : O_RDONLY) | O_NOFOLLOW, 0600);
        if (descriptor < 0) return fail("persistent_open");
        if (fstat(descriptor, &attributes) || !S_ISREG(attributes.st_mode) || attributes.st_nlink != 1 ||
            attributes.st_uid != uid || attributes.st_gid != gid || (attributes.st_mode & 0777) != 0600) {
            close(descriptor); return fail("persistent_permissions");
        }
        if (write_nonce && (ftruncate(descriptor, 0) || write(descriptor, nonce, 64) != 64 || fsync(descriptor))) {
            close(descriptor); return fail("persistent_write");
        }
        if (lseek(descriptor, 0, SEEK_SET) < 0) { close(descriptor); return fail("persistent_seek"); }
        char stored[65];
        ssize_t length = read(descriptor, stored, sizeof(stored));
        close(descriptor);
        if (length != 64 || memcmp(stored, nonce, 64)) return fail("persistent_content");
    }
    printf("BLAUOS_SSH_SESSION pass user=%s uid=%u gid=%u pty=%d persistent=%s\n",
           argv[1], (unsigned int)uid, (unsigned int)gid, require_pty,
           nonce ? (write_nonce ? "written" : "verified") : "none");
    if (require_challenge) printf("BLAUOS_SSH_CHALLENGE %s\n", challenge);
    return 0;
}
