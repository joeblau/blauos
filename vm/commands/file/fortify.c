/* SPDX-License-Identifier: MIT
 * Checked string append requested by SDK inlining; retain real bounds checks.
 */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

char *__strncat_chk(char *restrict destination, const char *restrict source,
                   size_t count, size_t capacity)
{
    size_t used = strnlen(destination, capacity);
    size_t copied = strnlen(source, count);
    if (used == capacity || copied >= capacity - used)
        abort();
    uintptr_t start = (uintptr_t)destination;
    uintptr_t other = (uintptr_t)source;
    size_t written = used + copied + 1;
    if (start <= other ? other - start < written : start - other < copied)
        abort();
    memcpy(destination + used, source, copied);
    destination[used + copied] = '\0';
    return destination;
}
