/* SPDX-License-Identifier: MIT
 * Optional legacy clock-change accounting. The initial independent runtime
 * has no writable utmpx/ASL provider; the actual clock syscall is in date.c.
 */
#include <dlfcn.h>
#include <utmpx.h>

void
blauos_record_clock_change(const struct utmpx *record)
{
    struct utmpx *(*writer)(const struct utmpx *) =
        (struct utmpx *(*)(const struct utmpx *))dlsym(RTLD_DEFAULT, "pututxline");
    if (writer != NULL)
        (void)writer(record);
}
