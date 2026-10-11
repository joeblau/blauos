/* Start source-built OpenSSH only after the independent guest prerequisites pass. */
#include <sys/types.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define HOST_DIRECTORY "/var/db/ssh"
#define HOST_KEY HOST_DIRECTORY "/ssh_host_ed25519_key"
#define HOST_PUBLIC HOST_KEY ".pub"

static volatile sig_atomic_t active_child;

static void expired(int unused)
{
    (void)unused;
    static const char message[] = "BLAUOS_SSH_START fail startup_timeout\n";
    (void)write(STDERR_FILENO, message, sizeof(message) - 1);
    if (active_child > 0) (void)kill((pid_t)active_child, SIGKILL);
    _exit(124);
}

static int fail(const char *step)
{
    fprintf(stderr, "BLAUOS_SSH_START fail %s errno=%d\n", step, errno);
    return 1;
}

static int wait_child(pid_t child)
{
    int status;
    pid_t waited;
    do { waited = waitpid(child, &status, 0); } while (waited < 0 && errno == EINTR);
    active_child = 0;
    return waited == child && WIFEXITED(status) && WEXITSTATUS(status) == 0 ? 0 : -1;
}

static int command(char *const arguments[], char *capture, size_t capacity)
{
    int descriptors[2] = {-1, -1};
    if (capture && pipe(descriptors)) return -1;
    pid_t child = fork();
    if (child < 0) {
        if (capture) { close(descriptors[0]); close(descriptors[1]); }
        return -1;
    }
    if (!child) {
        if (capture) {
            close(descriptors[0]);
            if (dup2(descriptors[1], STDOUT_FILENO) < 0) _exit(126);
            close(descriptors[1]);
        }
        execv(arguments[0], arguments);
        _exit(127);
    }
    active_child = (sig_atomic_t)child;
    if (capture) {
        close(descriptors[1]);
        size_t used = 0;
        int error = 0;
        while (1) {
            char bytes[256];
            ssize_t count = read(descriptors[0], bytes, sizeof(bytes));
            if (count < 0 && errno == EINTR) continue;
            if (count < 0) { error = 1; break; }
            if (!count) break;
            if (used + (size_t)count >= capacity) { errno = EOVERFLOW; error = 1; break; }
            memcpy(capture + used, bytes, (size_t)count);
            used += (size_t)count;
        }
        close(descriptors[0]);
        capture[used] = 0;
        if (error) (void)kill(child, SIGKILL);
        int successful = wait_child(child);
        return error ? -1 : successful;
    }
    return wait_child(child);
}

static int safe_directory(const char *path, mode_t mode, int create)
{
    struct stat attributes;
    if (lstat(path, &attributes)) {
        if (errno != ENOENT || !create || mkdir(path, mode) || lstat(path, &attributes)) return -1;
    }
    if (!S_ISDIR(attributes.st_mode) || attributes.st_uid != 0 || attributes.st_gid != 0 ||
        (attributes.st_mode & 0022)) { errno = EPERM; return -1; }
    return create ? chmod(path, mode) : 0;
}

static int safe_key(const char *path, int private_key)
{
    struct stat attributes;
    if (lstat(path, &attributes)) return -1;
    if (!S_ISREG(attributes.st_mode) || attributes.st_nlink != 1 || attributes.st_uid != 0 ||
        attributes.st_gid != 0 || !attributes.st_size || attributes.st_size > 16 * 1024 ||
        (private_key ? (attributes.st_mode & 0777) != 0600 : (attributes.st_mode & 0022) != 0)) {
        errno = EPERM; return -1;
    }
    return 0;
}

static int validate_public(char *key)
{
    /* ssh-keygen -y validates the private key; omit its optional public comment. */
    char *newline = strchr(key, '\n');
    if (newline) {
        if (newline[1]) return -1;
        *newline = 0;
    }
    const char *prefix = "ssh-ed25519 ";
    if (strncmp(key, prefix, strlen(prefix))) return -1;
    char *encoded = key + strlen(prefix);
    char *comment = strpbrk(encoded, " \t");
    if (comment) *comment = 0;
    if (strlen(encoded) != 68) return -1;
    for (const char *byte = encoded; *byte; byte++) {
        if (!((*byte >= 'A' && *byte <= 'Z') || (*byte >= 'a' && *byte <= 'z') ||
              (*byte >= '0' && *byte <= '9') || *byte == '+' || *byte == '/' || *byte == '=')) return -1;
    }
    return 0;
}

static int ensure_public(const char *derived)
{
    struct stat attributes;
    if (lstat(HOST_PUBLIC, &attributes) && errno == ENOENT) {
        int descriptor = open(HOST_PUBLIC, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0644);
        if (descriptor < 0) return -1;
        size_t length = strlen(derived);
        int failed = write(descriptor, derived, length) != (ssize_t)length ||
            write(descriptor, "\n", 1) != 1 || fsync(descriptor);
        if (close(descriptor)) failed = 1;
        if (failed) return -1;
    }
    if (safe_key(HOST_PUBLIC, 0)) return -1;
    int descriptor = open(HOST_PUBLIC, O_RDONLY | O_NOFOLLOW);
    if (descriptor < 0) return -1;
    char public_key[512];
    ssize_t count = read(descriptor, public_key, sizeof(public_key) - 1);
    close(descriptor);
    if (count <= 0) return -1;
    public_key[count] = 0;
    if (count == sizeof(public_key) - 1 || memchr(public_key, 0, (size_t)count) ||
        validate_public(public_key) || strcmp(public_key, derived)) {
        errno = EINVAL; return -1;
    }
    return 0;
}

int main(int argc, char **argv)
{
    if (argc != 4 || getuid() != 0 || geteuid() != 0) {
        fputs("Usage in independent guest as root: blauos-ssh-start USER UID GID\n", stderr);
        return 2;
    }
    size_t name_length = strlen(argv[1]);
    if (!name_length || name_length > 32 || strspn(argv[1], "abcdefghijklmnopqrstuvwxyz0123456789_-") != name_length ||
        (argv[1][0] >= '0' && argv[1][0] <= '9') || argv[1][0] == '-') return 2;
    for (int index = 2; index <= 3; index++) {
        char *end;
        errno = 0;
        unsigned long number = strtoul(argv[index], &end, 10);
        if (errno || !*argv[index] || *end || number < 1000 || number > 60000) return 2;
    }
    (void)umask(077);
    signal(SIGALRM, expired);
    alarm(180);
    char *probe[] = {"/usr/libexec/blauos-ssh-capability-probe", argv[1], argv[2], argv[3], NULL};
    if (command(probe, NULL, 0)) return fail("guest_prerequisites");
    if (safe_directory("/var", 0755, 0) || safe_directory("/var/db", 0755, 0) ||
        safe_directory(HOST_DIRECTORY, 0700, 1)) return fail("host_key_directory");
    struct stat attributes;
    if (lstat(HOST_KEY, &attributes)) {
        if (errno != ENOENT) return fail("host_key_stat");
        /* Preserve partial state instead of silently replacing a prior host identity. */
        if (!lstat(HOST_PUBLIC, &attributes) || errno != ENOENT) return fail("orphan_host_public_key");
        char *generate[] = {"/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "blauos-host", "-f", HOST_KEY, NULL};
        if (command(generate, NULL, 0)) return fail("host_key_generation");
        puts("BLAUOS_SSH_HOST_KEY created");
    } else {
        puts("BLAUOS_SSH_HOST_KEY preserved");
    }
    if (safe_key(HOST_KEY, 1)) return fail("host_private_key_permissions");
    char public_key[512];
    char *derive[] = {"/usr/bin/ssh-keygen", "-y", "-f", HOST_KEY, NULL};
    if (command(derive, public_key, sizeof(public_key)) || validate_public(public_key) ||
        ensure_public(public_key)) return fail("host_public_key");
    char key_fingerprint[512], fingerprint[128];
    unsigned int bits;
    char *identify[] = {"/usr/bin/ssh-keygen", "-l", "-E", "sha256", "-f", HOST_PUBLIC, NULL};
    if (command(identify, key_fingerprint, sizeof(key_fingerprint)) ||
        sscanf(key_fingerprint, "%u %127s", &bits, fingerprint) != 2 || bits != 256 ||
        strncmp(fingerprint, "SHA256:", 7)) return fail("host_key_fingerprint");
    printf("BLAUOS_SSH_HOST_PUBLIC_KEY %s\n", public_key);
    printf("BLAUOS_SSH_HOST_FINGERPRINT %s\n", fingerprint);
    puts("BLAUOS_SSH_START exec_sshd");
    fflush(NULL);
    alarm(0);
    signal(SIGALRM, SIG_DFL);
    char *daemon[] = {"/usr/sbin/sshd", "-D", "-e", "-f", "/etc/ssh/sshd_config", NULL};
    execv(daemon[0], daemon);
    return fail("exec_sshd");
}
