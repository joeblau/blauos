/* SPDX-License-Identifier: MIT
 * Real XNU system calls omitted from the selected monolithic libc export list.
 * These are linked privately into commands; the accepted runtime is untouched.
 */
#include <sys/types.h>
#include <sys/stat.h>
#include <sys/attr.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <stdlib.h>
#include <stdint.h>
#include <errno.h>

int chflags(const char *path, unsigned int flags)
{
    return (int)syscall(SYS_chflags, path, flags);
}

int setattrlist(const char *path, void *attrs, void *buffer,
                size_t size, unsigned int options)
{
    return (int)syscall(SYS_setattrlist, path, attrs, buffer, size, options);
}

int getattrlistbulk(int fd, void *attrs, void *buffer,
                   size_t size, uint64_t options)
{
    return (int)syscall(SYS_getattrlistbulk, fd, attrs, buffer, size, options);
}

/* Preserve allocation failure semantics and prevent multiplication overflow. */
void *reallocarray(void *pointer, size_t count, size_t size)
{
    if (size != 0 && count > SIZE_MAX / size) {
        errno = ENOMEM;
        return NULL;
    }
    return realloc(pointer, count * size);
}
