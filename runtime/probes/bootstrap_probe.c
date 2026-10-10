/* SPDX-License-Identifier: MIT
 * Guest-only launchd MachServices round-trip fixture. Server intentionally
 * exits with 17 after replying so launchd KeepAlive must supervise and restart.
 */
#include <mach/mach.h>
#include <servers/bootstrap.h>
#include <errno.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <sys/sysctl.h>
#include <unistd.h>

#define SERVICE "org.blauos.test.bootstrap"
struct message { mach_msg_header_t header; int value; int pid; };
struct receive_buffer { struct message payload; mach_msg_max_trailer_t trailer; };

int main(int argc, char **argv)
{
    if (argc == 3 && !strcmp(argv[1], "--reaped")) {
        char *end = NULL;
        errno = 0;
        long pid = strtol(argv[2], &end, 10);
        if (errno || !end || *end || pid <= 1 || pid > 2147483647) return 64;
        int mib[] = {CTL_KERN, KERN_PROC, KERN_PROC_PID, (int)pid};
        struct kinfo_proc process;
        size_t length = sizeof(process);
        /* A zombie still has a KERN_PROC record. A fully reaped process does not. */
        return sysctl(mib, 4, &process, &length, NULL, 0) || length != 0;
    }
    if (argc != 2) return 64;
    mach_port_t port;
    if (!strcmp(argv[1], "--server")) {
        if (bootstrap_check_in(bootstrap_port, SERVICE, &port) != KERN_SUCCESS) return 1;
        struct receive_buffer incoming = {0};
        incoming.payload.header.msgh_local_port = port;
        if (mach_msg(&incoming.payload.header, MACH_RCV_MSG | MACH_RCV_TIMEOUT, 0,
                     sizeof(incoming), port, 30000, MACH_PORT_NULL) != MACH_MSG_SUCCESS) return 2;
        if (incoming.payload.header.msgh_size != sizeof(struct message) ||
            incoming.payload.header.msgh_id != 100 || incoming.payload.value != 42) return 3;
        struct message reply = {0};
        reply.header.msgh_bits = MACH_MSGH_BITS(MACH_MSG_TYPE_MOVE_SEND_ONCE, 0);
        reply.header.msgh_size = sizeof(reply);
        reply.header.msgh_remote_port = incoming.payload.header.msgh_remote_port;
        reply.header.msgh_id = 101;
        reply.value = 43;
        reply.pid = getpid();
        if (mach_msg(&reply.header, MACH_SEND_MSG | MACH_SEND_TIMEOUT,
                     sizeof(reply), 0, MACH_PORT_NULL, 5000, MACH_PORT_NULL) != MACH_MSG_SUCCESS) return 4;
        return 17;
    }
    if (strcmp(argv[1], "--client")) return 64;
    if (bootstrap_look_up(bootstrap_port, SERVICE, &port) != KERN_SUCCESS) return 5;
    mach_port_t reply_port;
    if (mach_port_allocate(mach_task_self(), MACH_PORT_RIGHT_RECEIVE, &reply_port) != KERN_SUCCESS) return 6;
    struct receive_buffer request = {0};
    request.payload.header.msgh_bits = MACH_MSGH_BITS(MACH_MSG_TYPE_COPY_SEND, MACH_MSG_TYPE_MAKE_SEND_ONCE);
    request.payload.header.msgh_size = sizeof(struct message);
    request.payload.header.msgh_remote_port = port;
    request.payload.header.msgh_local_port = reply_port;
    request.payload.header.msgh_id = 100;
    request.payload.value = 42;
    if (mach_msg(&request.payload.header, MACH_SEND_MSG | MACH_RCV_MSG | MACH_SEND_TIMEOUT | MACH_RCV_TIMEOUT,
                 sizeof(struct message), sizeof(request), reply_port, 5000, MACH_PORT_NULL) != MACH_MSG_SUCCESS) return 7;
    if (request.payload.header.msgh_size != sizeof(struct message) ||
        request.payload.header.msgh_id != 101 || request.payload.value != 43 || request.payload.pid <= 1) return 8;
    printf("%d\n", request.payload.pid);
    mach_port_deallocate(mach_task_self(), port);
    mach_port_mod_refs(mach_task_self(), reply_port, MACH_PORT_RIGHT_RECEIVE, -1);
    return 0;
}
