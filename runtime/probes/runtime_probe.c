/* SPDX-License-Identifier: MIT
 * Exercises a real C runtime. Build against the target sysroot for acceptance;
 * compiling/running with the macOS SDK is only a host toolchain diagnostic.
 */
#include <errno.h>
#include <fcntl.h>
#include <mach-o/dyld.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

static void *thread_probe(void *value) { return value; }

int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "usage: runtime-probe WRITABLE_TEST_DIRECTORY\n");
        return 64;
    }
    char *allocation = malloc(4096);
    if (!allocation) return 1;
    memset(allocation, 0xa5, 4096);
    allocation = realloc(allocation, 8192);
    if (!allocation) return 2;
    for (size_t i = 0; i < 4096; ++i)
        if ((unsigned char)allocation[i] != 0xa5) return 3;
    free(allocation);
    puts("BLAUOS_RUNTIME allocation=pass");

    char path[4096];
    if (snprintf(path, sizeof(path), "%s/blauos-probe.XXXXXX", argv[1]) >= (int)sizeof(path))
        return 4;
    int fd = mkstemp(path);
    if (fd < 0) { perror("mkstemp"); return 5; }
    /* Unlink immediately: neither a successful nor failed probe leaves data. */
    if (unlink(path)) return 6;
    const char content[] = "blauos-file-io";
    char received[sizeof(content)];
    if (write(fd, content, sizeof(content)) != sizeof(content) ||
        fsync(fd) || lseek(fd, 0, SEEK_SET) != 0 ||
        read(fd, received, sizeof(received)) != sizeof(received) ||
        memcmp(received, content, sizeof(content)) || close(fd)) return 7;
    puts("BLAUOS_RUNTIME file_io=pass");

    void *mapping = mmap(NULL, 4096, PROT_READ | PROT_WRITE, MAP_ANON | MAP_PRIVATE, -1, 0);
    if (mapping == MAP_FAILED) return 8;
    *(volatile int *)mapping = 42;
    if (*(volatile int *)mapping != 42 || munmap(mapping, 4096) || getpid() <= 0) return 9;
    puts("BLAUOS_RUNTIME syscall=pass");

    pthread_t thread;
    void *thread_result = NULL;
    if (pthread_create(&thread, NULL, thread_probe, &thread_result) ||
        pthread_join(thread, &thread_result) || thread_result != &thread_result) return 10;
    puts("BLAUOS_RUNTIME pthread=pass");

    pid_t child = fork();
    if (child < 0) return 11;
    if (!child) _exit(23);
    int status;
    while (waitpid(child, &status, 0) < 0) { if (errno != EINTR) return 12; }
    if (!WIFEXITED(status) || WEXITSTATUS(status) != 23) return 13;
    puts("BLAUOS_RUNTIME process_exit=pass");

    for (uint32_t i = 0; i < _dyld_image_count(); ++i)
        printf("BLAUOS_RUNTIME image=%s\n", _dyld_get_image_name(i));
    puts("BLAUOS_RUNTIME result=pass");
    return 0;
}
