/*
 * VM integration probe for the source-built Darwin runtime used by sshd.
 * Random output tests establish API behavior only. Entropy admission must be
 * established separately from the boot loader and kernel source/boot evidence.
 */
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <pwd.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/random.h>
#include <sys/resource.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/sysctl.h>
#include <sys/time.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <termios.h>
#include <unistd.h>

static int failures;
static const char *username;
static uid_t target_uid;
static gid_t target_gid;
static char target_home[1024], target_shell[1024];
/* Source libSystem implements these Darwin compatibility entry points using
 * the real setreuid/setregid traps; OpenSSH selects them from its link probe. */
extern int setresuid(uid_t, uid_t, uid_t);
extern int setresgid(gid_t, gid_t, gid_t);

static void result(const char *stage, int passed, int error)
{
    printf("BLAUOS_SSH_CAPABILITY %s %s errno=%d\n", stage,
           passed ? "pass" : "fail", error);
    fflush(stdout);
    if (!passed) failures++;
}

static void expired(int unused)
{
    (void)unused;
    const char message[] = "BLAUOS_SSH_CAPABILITY timeout fail\n";
    write(STDERR_FILENO, message, sizeof(message) - 1);
    _exit(124);
}

static int successful_child(pid_t child)
{
    int status;
    pid_t got;
    do got = waitpid(child, &status, 0); while (got < 0 && errno == EINTR);
    if (got != child) return 0;
    if (!WIFEXITED(status) || WEXITSTATUS(status)) {
        fprintf(stderr, "BLAUOS_SSH_CAPABILITY child_status=%d\n", status);
        return 0;
    }
    return 1;
}

static int drop_credentials(void)
{
    if (initgroups(username, target_gid) || setgid(target_gid) || setuid(target_uid))
        return -1;
    if (getuid() != target_uid || geteuid() != target_uid ||
        getgid() != target_gid || getegid() != target_gid) return -1;
    gid_t groups[128];
    int count = getgroups(128, groups), found = 0;
    if (count < 0) return -1;
    for (int i = 0; i < count; i++) {
        if (groups[i] == 0) return -1;
        if (groups[i] == target_gid) found = 1;
    }
    if (!found) return -1;
    errno = 0;
    if (setuid(0) != -1 || errno != EPERM || getuid() != target_uid ||
        geteuid() != target_uid) return -1;
    return 0;
}

static int account_lookup(void)
{
    struct passwd *pw = getpwnam(username);
    if (!pw || pw->pw_uid != target_uid || pw->pw_gid != target_gid ||
        !pw->pw_dir || !pw->pw_shell || !pw->pw_name ||
        strcmp(pw->pw_name, username) || pw->pw_dir[0] != '/' ||
        strlen(pw->pw_dir) >= sizeof(target_home) ||
        strlen(pw->pw_shell) >= sizeof(target_shell)) return -1;
    strcpy(target_home, pw->pw_dir);
    strcpy(target_shell, pw->pw_shell);
    pw = getpwuid(target_uid);
    if (!pw || strcmp(pw->pw_name, username) || pw->pw_gid != target_gid ||
        strcmp(pw->pw_dir, target_home) || strcmp(pw->pw_shell, target_shell)) return -1;
    struct group *group = getgrgid(target_gid);
    if (!group || !group->gr_name) return -1;
    char name[1024];
    if (strlen(group->gr_name) >= sizeof(name)) return -1;
    strcpy(name, group->gr_name);
    group = getgrnam(name);
    if (!group || group->gr_gid != target_gid) return -1;
    if (getpwnam("blauos-account-that-must-not-exist-7ad394")) return -1;
    printf("BLAUOS_SSH_ACCOUNT name=%s uid=%u gid=%u home=%s shell=%s\n",
           username, target_uid, target_gid, target_home, target_shell);
    return 0;
}

static void credentials_and_permissions(void)
{
    char private_path[] = "/tmp/blauos-ssh-root-private.XXXXXX";
    int private_fd = mkstemp(private_path);
    if (private_fd < 0 || fchmod(private_fd, 0600)) {
        int error = errno;
        if (private_fd >= 0) { close(private_fd); unlink(private_path); }
        result("credentials_permissions", 0, error);
        return;
    }
    close(private_fd);
    pid_t child = fork();
    if (child == 0) {
        if (drop_credentials()) _exit(10);
        errno = 0;
        int forbidden = open(private_path, O_RDONLY);
        if (forbidden != -1 || errno != EACCES) _exit(11);
        if (chdir(target_home)) _exit(12);
        char writable[] = ".blauos-ssh-capability.XXXXXX";
        int fd = mkstemp(writable);
        struct stat st;
        if (fd < 0 || fstat(fd, &st) || st.st_uid != target_uid ||
            st.st_gid != target_gid || write(fd, "owned\n", 6) != 6) _exit(13);
        close(fd);
        if (unlink(writable)) _exit(14);
        _exit(0);
    }
    int passed = child > 0 && successful_child(child), error = errno;
    unlink(private_path);
    result("credentials_permissions", passed, passed ? 0 : error);
}

static void pty_shell(void)
{
    int master = posix_openpt(O_RDWR | O_NOCTTY);
    if (master < 0 || grantpt(master) || unlockpt(master)) {
        int error = errno;
        if (master >= 0) close(master);
        result("pty_shell", 0, error);
        return;
    }
    char *name = ptsname(master);
    char slave_name[128];
    if (!name || strlen(name) >= sizeof(slave_name)) {
        close(master); result("pty_shell", 0, errno); return;
    }
    strcpy(slave_name, name);
    int slave = open(slave_name, O_RDWR | O_NOCTTY);
    struct winsize window = {.ws_row = 30, .ws_col = 100};
    if (slave < 0 || fchown(slave, target_uid, target_gid) ||
        fchmod(slave, 0600) || ioctl(slave, TIOCSWINSZ, &window)) {
        int error = errno;
        close(master); if (slave >= 0) close(slave);
        result("pty_shell", 0, error); return;
    }
    pid_t child = fork();
    if (child == 0) {
        close(master);
        if (setsid() < 0 || ioctl(slave, TIOCSCTTY, 0)) _exit(20);
        for (int fd = 0; fd < 3; fd++) if (dup2(slave, fd) < 0) _exit(21);
        if (slave > 2) close(slave);
        if (drop_credentials() || chdir(target_home)) _exit(22);
        int controlling = open("/dev/tty", O_RDWR);
        struct winsize observed;
        if (controlling < 0 || !isatty(0) || tcgetpgrp(0) != getpgrp() ||
            getsid(0) != getpid() || ioctl(0, TIOCGWINSZ, &observed) ||
            observed.ws_col != window.ws_col || observed.ws_row != window.ws_row) _exit(23);
        close(controlling);
        if (setenv("HOME", target_home, 1) || setenv("USER", username, 1) ||
            setenv("LOGNAME", username, 1) || setenv("SHELL", target_shell, 1) ||
            setenv("TERM", "vt100", 1) || setenv("PATH", "/bin:/usr/bin:/sbin:/usr/sbin", 1)) _exit(24);
        puts("BLAUOS_PTY_CONTROLLING_TERMINAL_PASS"); fflush(stdout);
        execl(target_shell, target_shell, "-i", (char *)NULL);
        _exit(25);
    }
    close(slave);
    if (child < 0) { close(master); result("pty_shell", 0, errno); return; }
    /* Split the success word in the input: terminal echo cannot satisfy it. */
    const char command[] =
        "IFS= read -r blauos_pty_input; test \"$blauos_pty_input\" = 'guest-input-4c711b' && "
        "printf '%s%s\\n' 'BLAUOS_PTY_' 'SHELL_PASS'; exit $?\n"
        "guest-input-4c711b\n";
    size_t sent = 0;
    while (sent < sizeof(command) - 1) {
        ssize_t count = write(master, command + sent, sizeof(command) - 1 - sent);
        if (count < 0 && errno == EINTR) continue;
        if (count <= 0) break;
        sent += (size_t)count;
    }
    char output[16384]; size_t used = 0;
    struct timeval deadline, now;
    gettimeofday(&deadline, NULL); deadline.tv_sec += 15;
    for (;;) {
        gettimeofday(&now, NULL);
        if (now.tv_sec >= deadline.tv_sec || used == sizeof(output) - 1) break;
        fd_set read_set; FD_ZERO(&read_set); FD_SET(master, &read_set);
        struct timeval delay = {1, 0};
        int ready = select(master + 1, &read_set, NULL, NULL, &delay);
        if (ready < 0 && errno == EINTR) continue;
        if (ready < 0) break;
        if (!ready) continue;
        ssize_t count = read(master, output + used, sizeof(output) - 1 - used);
        if (count <= 0) break;
        used += (size_t)count;
    }
    output[used] = 0;
    printf("BLAUOS_PTY_OUTPUT_BEGIN\n%s\nBLAUOS_PTY_OUTPUT_END\n", output);
    int shell_pass = sent == sizeof(command) - 1 &&
        strstr(output, "BLAUOS_PTY_CONTROLLING_TERMINAL_PASS") &&
        strstr(output, "BLAUOS_PTY_SHELL_PASS");
    if (!shell_pass) kill(child, SIGKILL);
    close(master);
    int child_pass = successful_child(child);
    result("pty_shell", shell_pass && child_pass, 0);
}

static void random_api(void)
{
    int admission = 0; size_t admission_length = sizeof(admission);
    int admitted = sysctlbyname("kern.blauos_ssh_entropy_ready", &admission,
                               &admission_length, NULL, 0) == 0 &&
        admission_length == sizeof(admission) && admission == 1;
    if (admitted) {
        errno = 0;
        int modified = 0;
        admitted = sysctlbyname("kern.blauos_ssh_entropy_ready", NULL, NULL,
                               &modified, sizeof(modified)) == -1 && errno == EPERM;
    }
    result("entropy_admission", admitted, admitted ? 0 : errno);
    unsigned char first[256], second[256], oversized[257];
    if (getentropy(first, sizeof(first)) || getentropy(second, sizeof(second))) {
        result("getentropy_api", 0, errno); return;
    }
    errno = 0;
    int rejects_oversized = getentropy(oversized, sizeof(oversized)) == -1 && errno == EINVAL;
    /* Equality is a catastrophic test, not an entropy estimate. */
    result("getentropy_api", rejects_oversized && memcmp(first, second, sizeof(first)), 0);
    int fd = open("/dev/urandom", O_RDONLY);
    struct stat st;
    if (fd < 0 || fstat(fd, &st) || !S_ISCHR(st.st_mode)) {
        int error = errno; if (fd >= 0) close(fd);
        result("urandom_device_api", 0, error); return;
    }
    size_t used = 0;
    while (used < sizeof(second)) {
        ssize_t count = read(fd, second + used, sizeof(second) - used);
        if (count < 0 && errno == EINTR) continue;
        if (count <= 0) break;
        used += (size_t)count;
    }
    int error = errno; close(fd);
    result("urandom_device_api", used == sizeof(second), used == sizeof(second) ? 0 : error);
    puts("BLAUOS_SSH_ENTROPY_QUALITY byte_variation_is_not_an_entropy_estimate");
}

static void permanent_credentials(void)
{
    pid_t child = fork();
    if (!child) {
        if (initgroups(username, target_gid) ||
            setresgid(target_gid, target_gid, target_gid) ||
            setresuid(target_uid, target_uid, target_uid)) _exit(50);
        if (getuid() != target_uid || geteuid() != target_uid ||
            getgid() != target_gid || getegid() != target_gid) _exit(51);
        errno = 0;
        if (seteuid(0) != -1 || errno != EPERM) _exit(52);
        errno = 0;
        if (setuid(0) != -1 || errno != EPERM) _exit(53);
        errno = 0;
        if (setresuid((uid_t)-1, 0, (uid_t)-1) != -1 || errno != EPERM) _exit(54);
        errno = 0;
        if (setegid(0) != -1 || errno != EPERM) _exit(55);
        errno = 0;
        if (setgid(0) != -1 || errno != EPERM) _exit(56);
        _exit(0);
    }
    int passed = child > 0 && successful_child(child);
    result("permanent_credentials", passed, passed ? 0 : errno);
}

static void sandbox_select(void)
{
    int pair[2];
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, pair)) {
        result("sandbox_select", 0, errno); return;
    }
    pid_t child = fork();
    if (!child) {
        close(pair[0]);
        struct rlimit zero = {0, 0};
        if (setrlimit(RLIMIT_NOFILE, &zero) || setrlimit(RLIMIT_FSIZE, &zero) ||
            setrlimit(RLIMIT_NPROC, &zero)) _exit(30);
        fd_set readable; FD_ZERO(&readable); FD_SET(pair[1], &readable);
        struct timeval timeout = {5, 0};
        if (select(pair[1] + 1, &readable, NULL, NULL, &timeout) != 1 ||
            !FD_ISSET(pair[1], &readable)) _exit(31);
        char byte;
        if (read(pair[1], &byte, 1) != 1 || byte != 's') _exit(32);
        if (write(pair[1], "p", 1) != 1) _exit(33);
        _exit(0);
    }
    close(pair[1]);
    int passed = child > 0 && write(pair[0], "s", 1) == 1;
    if (passed) {
        fd_set readable; FD_ZERO(&readable); FD_SET(pair[0], &readable);
        struct timeval timeout = {8, 0};
        char byte;
        passed = select(pair[0] + 1, &readable, NULL, NULL, &timeout) == 1 &&
            read(pair[0], &byte, 1) == 1 && byte == 'p';
    }
    close(pair[0]);
    if (child > 0) passed = successful_child(child) && passed;
    result("sandbox_select", passed, passed ? 0 : errno);
}

static void descriptor_passing(void)
{
    int pair[2];
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, pair)) {
        result("descriptor_passing", 0, errno); return;
    }
    int sent_fd = open("/dev/null", O_RDWR);
    if (sent_fd < 0) {
        close(pair[0]); close(pair[1]); result("descriptor_passing", 0, errno); return;
    }
    union { struct cmsghdr align; char bytes[CMSG_SPACE(sizeof(int))]; } control;
    memset(&control, 0, sizeof(control));
    char byte = 'd'; struct iovec vector = {&byte, 1};
    struct msghdr message; memset(&message, 0, sizeof(message));
    message.msg_iov = &vector; message.msg_iovlen = 1;
    message.msg_control = control.bytes; message.msg_controllen = sizeof(control.bytes);
    struct cmsghdr *header = CMSG_FIRSTHDR(&message);
    header->cmsg_level = SOL_SOCKET; header->cmsg_type = SCM_RIGHTS;
    header->cmsg_len = CMSG_LEN(sizeof(int));
    memcpy(CMSG_DATA(header), &sent_fd, sizeof(int));
    int passed = sendmsg(pair[0], &message, 0) == 1;
    close(sent_fd);
    memset(control.bytes, 0, sizeof(control.bytes));
    message.msg_controllen = sizeof(control.bytes);
    if (passed) passed = recvmsg(pair[1], &message, 0) == 1 && byte == 'd' &&
        !(message.msg_flags & (MSG_CTRUNC | MSG_TRUNC));
    header = CMSG_FIRSTHDR(&message);
    int received_fd = -1;
    if (passed && header && header->cmsg_level == SOL_SOCKET &&
        header->cmsg_type == SCM_RIGHTS && header->cmsg_len == CMSG_LEN(sizeof(int))) {
        memcpy(&received_fd, CMSG_DATA(header), sizeof(int));
        struct stat st;
        passed = received_fd >= 0 && fstat(received_fd, &st) == 0 && S_ISCHR(st.st_mode) &&
            write(received_fd, "passed", 6) == 6;
    } else passed = 0;
    if (received_fd >= 0) close(received_fd);
    close(pair[0]); close(pair[1]);
    result("descriptor_passing", passed, passed ? 0 : errno);
}

static void sshd_chroot(void)
{
    struct passwd *pw = getpwnam("sshd");
    if (!pw || pw->pw_uid != 74 || pw->pw_gid != 74) {
        result("sshd_chroot", 0, ENOENT); return;
    }
    char directory[] = "/tmp/blauos-sshd-chroot.XXXXXX";
    if (!mkdtemp(directory) || chmod(directory, 0755)) {
        result("sshd_chroot", 0, errno); return;
    }
    pid_t child = fork();
    if (!child) {
        if (initgroups("sshd", 74) || chroot(directory) || chdir("/") ||
            setgid(74) || setuid(74)) _exit(40);
        if (getuid() != 74 || geteuid() != 74 || getgid() != 74 || getegid() != 74) _exit(41);
        char cwd[16];
        if (!getcwd(cwd, sizeof(cwd)) || strcmp(cwd, "/")) _exit(42);
        errno = 0;
        int outside = open("/etc/passwd", O_RDONLY);
        if (outside != -1 || errno != ENOENT) _exit(43);
        errno = 0;
        if (chroot("/") != -1 || errno != EPERM) _exit(44);
        errno = 0;
        if (setuid(0) != -1 || errno != EPERM) _exit(45);
        _exit(0);
    }
    int passed = child > 0 && successful_child(child);
    rmdir(directory);
    result("sshd_chroot", passed, passed ? 0 : errno);
}

int main(int argc, char **argv)
{
    char kernel[1024]; size_t length = sizeof(kernel);
    if (argc != 4 || getuid() != 0 || geteuid() != 0 ||
        sysctlbyname("kern.version", kernel, &length, NULL, 0)) {
        fprintf(stderr, "Usage in independent guest as root: %s USER UID GID\n", argv[0]);
        return 2;
    }
    kernel[sizeof(kernel) - 1] = 0;
    if (!strstr(kernel, "Darwin Kernel Version 20.5.0")) {
        fputs("BLAUOS_SSH_CAPABILITY guest_kernel fail\n", stderr); return 2;
    }
    username = argv[1];
    char *end;
    unsigned long uid = strtoul(argv[2], &end, 10);
    if (!*argv[2] || *end || uid == 0 || uid > 0x7ffffffeUL) return 2;
    unsigned long gid = strtoul(argv[3], &end, 10);
    if (!*argv[3] || *end || gid == 0 || gid > 0x7ffffffeUL) return 2;
    target_uid = (uid_t)uid; target_gid = (gid_t)gid;
    signal(SIGALRM, expired); alarm(60);
    int accounts = account_lookup() == 0;
    result("account_lookup", accounts, accounts ? 0 : errno);
    if (accounts) {
        credentials_and_permissions();
        permanent_credentials();
        pty_shell();
    } else {
        result("credentials_permissions", 0, ENOENT);
        result("permanent_credentials", 0, ENOENT);
        result("pty_shell", 0, ENOENT);
    }
    sandbox_select();
    descriptor_passing();
    sshd_chroot();
    random_api();
    alarm(0);
    result("runtime_prerequisites", failures == 0, 0);
    return failures ? 1 : 0;
}
