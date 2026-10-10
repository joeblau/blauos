/* SPDX-License-Identifier: MIT
 * Real guest-only launchd shutdown witness. The parent waits for SIGTERM from
 * PID 1, then terminates and reaps its own child before exiting successfully.
 */
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/sysctl.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

static char nonce[65];

static void marker(const char *stage)
{
    printf("BLAUOS_SMOKE %s %s\n", nonce, stage);
    fflush(stdout);
}

static int failed(const char *stage)
{
    fprintf(stderr, "BLAUOS_SMOKE_ERROR stage=shutdown-%s errno=%d\n", stage, errno);
    return 1;
}

int main(int argc, char **argv)
{
    char kernel[1024];
    size_t length = sizeof(kernel);
    if (argc != 2 || getppid() != 1 || getuid() != 0 ||
        sysctlbyname("kern.version", kernel, &length, NULL, 0)) return failed("guest-parent");
    kernel[sizeof(kernel) - 1] = 0;
    if (!strstr(kernel, "Darwin Kernel Version 20.5.0")) return failed("guest-kernel");
    FILE *input = fopen(argv[1], "r");
    if (!input || fscanf(input, "%64s", nonce) != 1 || strlen(nonce) != 64)
        return failed("nonce");
    fclose(input);
    for (size_t i = 0; i < 64; ++i)
        if (!((nonce[i] >= '0' && nonce[i] <= '9') || (nonce[i] >= 'a' && nonce[i] <= 'f')))
            return failed("nonce-format");
    sigset_t signals;
    sigemptyset(&signals);
    sigaddset(&signals, SIGTERM);
    if (sigprocmask(SIG_BLOCK, &signals, NULL)) return failed("signal-mask");
    int ready[2];
    if (pipe(ready)) return failed("pipe");
    pid_t child = fork();
    if (child < 0) return failed("fork");
    if (child == 0) {
        close(ready[0]);
        if (write(ready[1], "R", 1) != 1) _exit(2);
        close(ready[1]);
        int received = 0;
        if (sigwait(&signals, &received) || received != SIGTERM) _exit(3);
        _exit(0);
    }
    close(ready[1]);
    char byte = 0;
    if (read(ready[0], &byte, 1) != 1 || byte != 'R') return failed("child-ready");
    close(ready[0]);
    marker("shutdown_job_started");
    int received = 0;
    if (sigwait(&signals, &received) || received != SIGTERM) return failed("term");
    if (kill(child, SIGTERM) && errno != ESRCH) return failed("child-term");
    int status = 0;
    pid_t waited;
    do { waited = waitpid(child, &status, 0); } while (waited < 0 && errno == EINTR);
    if (waited != child || !WIFEXITED(status) || WEXITSTATUS(status)) return failed("child-wait");
    int mib[] = {CTL_KERN, KERN_PROC, KERN_PROC_PID, child};
    struct kinfo_proc remaining;
    length = sizeof(remaining);
    if (sysctl(mib, 4, &remaining, &length, NULL, 0) || length != 0)
        return failed("child-still-present");
    marker("shutdown_child_reaped");
    marker("shutdown_job_exit");
    return 0;
}
