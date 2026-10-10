/* SPDX-License-Identifier: MIT
 * Real compiler-generated dynamic stack frames linked to BlauOS's probe.
 * The guard test invokes the helper with a huge requested frame in a child,
 * before the caller adjusts SP or stores data. A missing probe returns and
 * therefore fails this test; touching the stack guard terminates the child.
 */
#include <signal.h>
#include <stddef.h>
#include <stdio.h>
#include <sys/wait.h>
#include <unistd.h>

__attribute__((noinline)) static int large_frame(size_t size)
{
    volatile unsigned char frame[size];
    frame[0] = 11;
    frame[size - 1] = 31;
    return frame[0] + frame[size - 1];
}

static void direct_probe(size_t bytes)
{
    __asm__ volatile("mov x9, %0\nbl ___chkstk_darwin" : : "r"(bytes) : "x9", "x30", "memory");
}

int main(void)
{
    const size_t sizes[] = {0, 1, 4095, 4096, 4097, 16384, 65536};
    for (size_t i = 0; i < sizeof(sizes) / sizeof(sizes[0]); ++i) {
        direct_probe(sizes[i]);
        if (sizes[i] && large_frame(sizes[i]) != (sizes[i] == 1 ? 62 : 42)) return 1;
    }
    pid_t child = fork();
    if (child < 0) return 2;
    if (!child) { direct_probe((size_t)1 << 32); _exit(3); }
    int status = 0;
    if (waitpid(child, &status, 0) != child || !WIFSIGNALED(status) ||
        (WTERMSIG(status) != SIGSEGV && WTERMSIG(status) != SIGBUS)) return 4;
    puts("BLAUOS_STACK_PROBE allocation=pass guard_page=pass");
    return 0;
}
