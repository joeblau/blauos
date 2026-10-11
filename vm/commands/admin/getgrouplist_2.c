/* SPDX-License-Identifier: MIT
 * Bridge Apple's allocating group-list SPI to the independent runtime's
 * public flat-file getgrouplist implementation. No DirectoryService is used.
 */
#include <sys/types.h>
#include <errno.h>
#include <grp.h>
#include <limits.h>
#include <stdint.h>
#include <stdlib.h>
#include <unistd.h>

int32_t
getgrouplist_2(const char *name, gid_t primary, gid_t **result)
{
    int capacity = 16;
    gid_t *list = NULL;

    _Static_assert(sizeof(gid_t) == sizeof(int), "Darwin group-list ABI");
    if (name == NULL || result == NULL) {
        errno = EINVAL;
        return -1;
    }
    *result = NULL;
    for (;;) {
        gid_t *grown = realloc(list, (size_t)capacity * sizeof(*list));
        if (grown == NULL) {
            free(list);
            return -1;
        }
        list = grown;
        int count = capacity;
        errno = 0;
        if (getgrouplist(name, (int)primary, (int *)list, &count) >= 0) {
            if (count < 0 || count > capacity) {
                free(list);
                errno = EIO;
                return -1;
            }
            *result = list;
            return count;
        }
        if (errno != 0 && errno != ERANGE) {
            int saved = errno;
            free(list);
            errno = saved;
            return -1;
        }
        /* Darwin's public API may report only the count filled on overflow;
         * retry with a larger buffer rather than treating it as a full count.
         */
        if (capacity >= 1048576 || capacity > INT_MAX / 2 ||
            (size_t)capacity * 2 > SIZE_MAX / sizeof(*list) || count > 1048576) {
            free(list);
            errno = EOVERFLOW;
            return -1;
        }
        capacity = count > capacity ? count : capacity * 2;
    }
}
