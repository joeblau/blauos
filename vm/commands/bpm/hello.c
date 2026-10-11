/* blauos-hello: minimal package-manager demo payload for the independent
 * ARM64 Darwin guest. Installed by the signed blauos-hello .bpm package; it
 * exists to prove end-to-end package installation (verify, unpack, run,
 * re-verify, remove) of a repo-native binary. libSystem-only. */
#include <stdio.h>

int main(void)
{
    printf("BLAUOS_HELLO_PACKAGE pass\n");
    return 0;
}
