/* SPDX-License-Identifier: MIT */
#include <stdlib.h>
#include <string.h>

int blauos_loader_fixture(int value)
{
    char *memory = malloc(64);
    if (!memory) return -1;
    memcpy(memory, "source-loader-fixture", 22);
    int result = !strcmp(memory, "source-loader-fixture") ? value + 1 : -2;
    free(memory);
    return result;
}
