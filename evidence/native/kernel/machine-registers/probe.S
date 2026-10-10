#define ARM64_BOARD_CONFIG_T8103 1
#include "/Users/joeblau/Developer/joeblau/src/blauos/.blauos/build/native/kernel/native-machine-registers-patch-source/src/Kernel/xnu/osfmk/arm64/native_apple_sysregs.h"
.text
.globl _native_machine_register_probe
_native_machine_register_probe:
mrs x0, CPU_CFG
msr CPU_CFG, x0
mrs x0, HID13
msr HID13, x0
mrs x0, HID14
msr HID14, x0
mrs x0, VMSA_LOCK_EL1
msr VMSA_LOCK_EL1, x0
ret
