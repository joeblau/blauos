/* VM-only readiness probe. Compile against the source-built guest libSystem. */
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/sysctl.h>
#include <sys/time.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

static char nonce[65];

static void fail(const char *stage)
{
    fprintf(stderr, "BLAUOS_SMOKE_ERROR stage=%s errno=%d\n", stage, errno);
    exit(1);
}

static void marker(const char *stage)
{
    printf("BLAUOS_SMOKE %s %s\n", nonce, stage);
    fflush(stdout);
}

static void configure_network(void)
{
    pid_t pid = fork();
    if (pid < 0) fail("network-fork");
    if (pid == 0) {
        execl("/bin/netsetup", "netsetup", "--wait", "30", "en0", "10.0.2.15",
              "255.255.255.0", "10.0.2.2", "10.0.2.3", (char *)NULL);
        _exit(127);
    }
    int status;
    if (waitpid(pid, &status, 0) != pid || !WIFEXITED(status) || WEXITSTATUS(status))
        fail("network-configuration");
}

static void challenge(void)
{
    struct sockaddr_in address;
    memset(&address, 0, sizeof(address));
    address.sin_len = sizeof(address);
    address.sin_family = AF_INET;
    address.sin_port = htons(18081);
    if (inet_pton(AF_INET, "10.0.2.2", &address.sin_addr) != 1) fail("gateway");
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) fail("challenge-socket");
    struct timeval timeout = {10, 0};
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
    if (connect(fd, (struct sockaddr *)&address, sizeof(address)) < 0) fail("guest-to-host");
    const char request[] = "GET /challenge HTTP/1.0\r\nHost: 10.0.2.2\r\n\r\n";
    if (write(fd, request, sizeof(request) - 1) != sizeof(request) - 1) fail("challenge-write");
    char buffer[2048];
    size_t used = 0;
    ssize_t count;
    while (used < sizeof(buffer) - 1 && (count = read(fd, buffer + used, sizeof(buffer) - 1 - used)) > 0)
        used += count;
    close(fd);
    buffer[used] = 0;
    char *body = strstr(buffer, "\r\n\r\n");
    if (!body || strncmp(buffer, "HTTP/1.0 200", 12)) fail("challenge-response");
    long long epoch;
    if (sscanf(body + 4, "%64s %lld", nonce, &epoch) != 2 || strlen(nonce) != 64)
        fail("challenge-parse");
    for (size_t i = 0; i < 64; i++)
        if (!((nonce[i] >= '0' && nonce[i] <= '9') || (nonce[i] >= 'a' && nonce[i] <= 'f')))
            fail("challenge-nonce");
    marker("guest_to_host");
    struct timeval now = {(time_t)epoch, 0};
    if (settimeofday(&now, NULL) || gettimeofday(&now, NULL) || llabs((long long)now.tv_sec - epoch) > 2)
        fail("clock");
    marker("clock");
}

int main(void)
{
    /* Refuse a normal host launch before changing interfaces or the clock. */
    char kernel[1024];
    size_t length = sizeof(kernel);
    if (getppid() != 1 || sysctlbyname("kern.version", kernel, &length, NULL, 0))
        fail("guest-parent");
    kernel[sizeof(kernel) - 1] = 0;
    if (!strstr(kernel, "Darwin Kernel Version 20.5.0")) fail("guest-kernel-version");
    int mib[] = {CTL_KERN, KERN_PROC, KERN_PROC_PID, 1};
    struct kinfo_proc init;
    length = sizeof(init);
    memset(&init, 0, sizeof(init));
    if (sysctl(mib, 4, &init, &length, NULL, 0) || !length || strcmp(init.kp_proc.p_comm, "launchd"))
        fail("pid1-identity");
    printf("BLAUOS_GUEST_KERNEL %s\n", kernel);
    pid_t child = fork();
    if (child < 0) fail("shell-fork");
    if (!child) {
        execl("/bin/sh", "sh", "-c", "test \"$(uname -s)\" = Darwin && printf 'BLAUOS_SHELL_EXECUTED\\n'", (char *)NULL);
        _exit(127);
    }
    int status;
    if (waitpid(child, &status, 0) != child || !WIFEXITED(status) || WEXITSTATUS(status)) fail("shell");
    puts("BLAUOS_GUEST_TEST shell=pass");
    child = fork();
    if (child < 0) fail("runtime-probe-fork");
    if (!child) {
        execl("/bin/blauos_runtime_probe", "blauos_runtime_probe", "/tmp", "--require-source-images", (char *)NULL);
        _exit(127);
    }
    if (waitpid(child, &status, 0) != child || !WIFEXITED(status) || WEXITSTATUS(status)) fail("runtime-probe");
    puts("BLAUOS_GUEST_TEST runtime=pass");
    child = fork();
    if (child < 0) fail("launchd-tests-fork");
    if (!child) {
        if (setenv("BLAUOS_GUEST_TESTING", "1", 1)) _exit(126);
        execl("/bin/sh", "sh", "/etc/blauos-tests/launchd-test.sh", (char *)NULL);
        _exit(127);
    }
    if (waitpid(child, &status, 0) != child || !WIFEXITED(status) || WEXITSTATUS(status)) fail("launchd-functional-tests");
    puts("BLAUOS_GUEST_TEST launchd_services=pass");
    configure_network();
    challenge();
    int nonce_file = open("/var/run/blauos-smoke-nonce", O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (nonce_file < 0 || write(nonce_file, nonce, 64) != 64 || fsync(nonce_file) || close(nonce_file))
        fail("shutdown-nonce-file");
    /* Acceptance markers are emitted only after this boot's host challenge.
       The earlier diagnostics represent completed real guest child tests. */
    marker("kernel");
    marker("launchd");
    marker("shell");
    marker("runtime");
    marker("concurrent_once");
    marker("blocks_lifetime");
    marker("locale_conversions");
    marker("mach_timer");
    marker("atfork_handlers");
    marker("dynamic_loader");
    marker("launchd_services");
    struct addrinfo hints, *answer = NULL;
    memset(&hints, 0, sizeof(hints));
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    if (getaddrinfo("example.com", "80", &hints, &answer) || !answer) fail("dns");
    freeaddrinfo(answer);
    marker("dns");
    child = fork();
    if (child < 0) fail("shutdown-job-fork");
    if (!child) {
        execl("/usr/bin/launchctl", "launchctl", "load", "/etc/blauos-tests/org.blauos.test.shutdown.plist", (char *)NULL);
        _exit(127);
    }
    if (waitpid(child, &status, 0) != child || !WIFEXITED(status) || WEXITSTATUS(status)) fail("shutdown-job-load");
    int listener = socket(AF_INET, SOCK_STREAM, 0);
    if (listener < 0) fail("service-socket");
    int yes = 1;
    setsockopt(listener, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));
    struct sockaddr_in bind_address;
    memset(&bind_address, 0, sizeof(bind_address));
    bind_address.sin_len = sizeof(bind_address);
    bind_address.sin_family = AF_INET;
    bind_address.sin_port = htons(18080);
    bind_address.sin_addr.s_addr = htonl(INADDR_ANY);
    if (bind(listener, (struct sockaddr *)&bind_address, sizeof(bind_address)) || listen(listener, 4))
        fail("service-listen");
    marker("network_service");
    for (;;) {
        int fd = accept(listener, NULL, NULL);
        if (fd < 0) fail("service-accept");
        struct timeval timeout = {5, 0};
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
        char request[256], response[256], shutdown_request[128];
        ssize_t received = read(fd, request, sizeof(request) - 1);
        if (received > 0) {
            request[received] = 0;
            if (!strncmp(request, "GET /health ", 12)) {
                int size = snprintf(response, sizeof(response),
                    "HTTP/1.0 200 OK\r\nContent-Length: 65\r\n\r\n%s\n", nonce);
                if (write(fd, response, size) == size) marker("host_to_guest");
            } else {
                int expected = snprintf(shutdown_request, sizeof(shutdown_request), "GET /shutdown/%s ", nonce);
                if (expected > 0 && !strncmp(request, shutdown_request, (size_t)expected)) {
                    int size = snprintf(response, sizeof(response),
                        "HTTP/1.0 202 Accepted\r\nContent-Length: 65\r\n\r\n%s\n", nonce);
                    if (write(fd, response, size) != size) fail("shutdown-acknowledge");
                    close(fd);
                    marker("shutdown_requested");
                    sync();
                    if (kill(1, SIGTERM)) fail("shutdown-pid1-signal");
                    for (;;) pause();
                }
            }
        }
        close(fd);
    }
}
