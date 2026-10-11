/*
 * Intel ACPI Component Architecture
 * AML/ASL+ Disassembler version 20260408 (64-bit version)
 * Copyright (c) 2000 - 2026 Intel Corporation
 * 
 * Disassembling to symbolic ASL+ operators
 *
 * Disassembly of /Users/joeblau/Developer/joeblau/src/blauos/.blauos/experiments/virtualbuddy-platform/inventory-run/tables/ACPI-DSDT.bin
 *
 * Original Table Header:
 *     Signature        "DSDT"
 *     Length           0x0000033E (830)
 *     Revision         0x02
 *     Checksum         0x87
 *     OEM ID           "APPLE "
 *     OEM Table ID     "Apple Vz"
 *     OEM Revision     0x00000001 (1)
 *     Compiler ID      "AAPL"
 *     Compiler Version 0x20180427 (538444839)
 */
DefinitionBlock ("", "DSDT", 2, "APPLE ", "Apple Vz", 0x00000001)
{
    Scope (_SB)
    {
        Device (PCI0)
        {
            Name (_HID, EisaId ("PNP0A08") /* PCI Express Bus */)  // _HID: Hardware ID
            Name (_CID, EisaId ("PNP0A03") /* PCI Bus */)  // _CID: Compatible ID
            Name (_ADR, Zero)  // _ADR: Address
            Name (_UID, Zero)  // _UID: Unique ID
            Name (_BBN, Zero)  // _BBN: BIOS Bus Number
            Name (_CCA, One)  // _CCA: Cache Coherency Attribute
            Device (RES0)
            {
                Name (_HID, EisaId ("PNP0C02") /* PNP Motherboard Resources */)  // _HID: Hardware ID
                Name (_CRS, Buffer (0x0E)  // _CRS: Current Resource Settings
                {
                    /* 0000 */  0x86, 0x09, 0x00, 0x01, 0x00, 0x00, 0x00, 0x40,  // .......@
                    /* 0008 */  0x00, 0x00, 0x00, 0x10, 0x79, 0x00               // ....y.
                })
            }

            Name (_CRS, Buffer (0x74)  // _CRS: Current Resource Settings
            {
                /* 0000 */  0x88, 0x0D, 0x00, 0x02, 0x0C, 0x00, 0x00, 0x00,  // ........
                /* 0008 */  0x00, 0x00, 0xFF, 0x00, 0x00, 0x00, 0x00, 0x01,  // ........
                /* 0010 */  0x87, 0x17, 0x00, 0x00, 0x0C, 0x01, 0x00, 0x00,  // ........
                /* 0018 */  0x00, 0x00, 0x00, 0x00, 0x00, 0x50, 0xFF, 0xFF,  // .....P..
                /* 0020 */  0xFD, 0x6F, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,  // .o......
                /* 0028 */  0xFE, 0x1F, 0x8A, 0x2B, 0x00, 0x00, 0x0C, 0x01,  // ...+....
                /* 0030 */  0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,  // ........
                /* 0038 */  0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00,  // ........
                /* 0040 */  0xFF, 0xFF, 0xFF, 0xFF, 0x04, 0x00, 0x00, 0x00,  // ........
                /* 0048 */  0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,  // ........
                /* 0050 */  0x00, 0x00, 0x00, 0x00, 0x04, 0x00, 0x00, 0x00,  // ........
                /* 0058 */  0x87, 0x17, 0x00, 0x01, 0x0C, 0x03, 0x00, 0x00,  // ........
                /* 0060 */  0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0xFF, 0xFF,  // ........
                /* 0068 */  0x00, 0x00, 0x00, 0x00, 0xFF, 0x6F, 0x00, 0x00,  // .....o..
                /* 0070 */  0x01, 0x00, 0x79, 0x00                           // ..y.
            })
            Name (_PRT, Package (0x20)  // _PRT: PCI Routing Table
            {
                Package (0x04)
                {
                    0xFFFF, 
                    Zero, 
                    Zero, 
                    0x40
                }, 

                Package (0x04)
                {
                    0x0001FFFF, 
                    Zero, 
                    Zero, 
                    0x41
                }, 

                Package (0x04)
                {
                    0x0002FFFF, 
                    Zero, 
                    Zero, 
                    0x42
                }, 

                Package (0x04)
                {
                    0x0003FFFF, 
                    Zero, 
                    Zero, 
                    0x43
                }, 

                Package (0x04)
                {
                    0x0004FFFF, 
                    Zero, 
                    Zero, 
                    0x44
                }, 

                Package (0x04)
                {
                    0x0005FFFF, 
                    Zero, 
                    Zero, 
                    0x45
                }, 

                Package (0x04)
                {
                    0x0006FFFF, 
                    Zero, 
                    Zero, 
                    0x46
                }, 

                Package (0x04)
                {
                    0x0007FFFF, 
                    Zero, 
                    Zero, 
                    0x47
                }, 

                Package (0x04)
                {
                    0x0008FFFF, 
                    Zero, 
                    Zero, 
                    0x48
                }, 

                Package (0x04)
                {
                    0x0009FFFF, 
                    Zero, 
                    Zero, 
                    0x49
                }, 

                Package (0x04)
                {
                    0x000AFFFF, 
                    Zero, 
                    Zero, 
                    0x4A
                }, 

                Package (0x04)
                {
                    0x000BFFFF, 
                    Zero, 
                    Zero, 
                    0x4B
                }, 

                Package (0x04)
                {
                    0x000CFFFF, 
                    Zero, 
                    Zero, 
                    0x4C
                }, 

                Package (0x04)
                {
                    0x000DFFFF, 
                    Zero, 
                    Zero, 
                    0x4D
                }, 

                Package (0x04)
                {
                    0x000EFFFF, 
                    Zero, 
                    Zero, 
                    0x4E
                }, 

                Package (0x04)
                {
                    0x000FFFFF, 
                    Zero, 
                    Zero, 
                    0x4F
                }, 

                Package (0x04)
                {
                    0x0010FFFF, 
                    Zero, 
                    Zero, 
                    0x50
                }, 

                Package (0x04)
                {
                    0x0011FFFF, 
                    Zero, 
                    Zero, 
                    0x51
                }, 

                Package (0x04)
                {
                    0x0012FFFF, 
                    Zero, 
                    Zero, 
                    0x52
                }, 

                Package (0x04)
                {
                    0x0013FFFF, 
                    Zero, 
                    Zero, 
                    0x53
                }, 

                Package (0x04)
                {
                    0x0014FFFF, 
                    Zero, 
                    Zero, 
                    0x54
                }, 

                Package (0x04)
                {
                    0x0015FFFF, 
                    Zero, 
                    Zero, 
                    0x55
                }, 

                Package (0x04)
                {
                    0x0016FFFF, 
                    Zero, 
                    Zero, 
                    0x56
                }, 

                Package (0x04)
                {
                    0x0017FFFF, 
                    Zero, 
                    Zero, 
                    0x57
                }, 

                Package (0x04)
                {
                    0x0018FFFF, 
                    Zero, 
                    Zero, 
                    0x58
                }, 

                Package (0x04)
                {
                    0x0019FFFF, 
                    Zero, 
                    Zero, 
                    0x59
                }, 

                Package (0x04)
                {
                    0x001AFFFF, 
                    Zero, 
                    Zero, 
                    0x5A
                }, 

                Package (0x04)
                {
                    0x001BFFFF, 
                    Zero, 
                    Zero, 
                    0x5B
                }, 

                Package (0x04)
                {
                    0x001CFFFF, 
                    Zero, 
                    Zero, 
                    0x5C
                }, 

                Package (0x04)
                {
                    0x001DFFFF, 
                    Zero, 
                    Zero, 
                    0x5D
                }, 

                Package (0x04)
                {
                    0x001EFFFF, 
                    Zero, 
                    Zero, 
                    0x5E
                }, 

                Package (0x04)
                {
                    0x001FFFFF, 
                    Zero, 
                    Zero, 
                    0x5F
                }
            })
        }
    }

    Scope (_PR)
    {
        Device (CP00)
        {
            Name (_HID, "ACPI0007" /* Processor Device */)  // _HID: Hardware ID
            Name (_UID, Zero)  // _UID: Unique ID
        }

        Device (PWRB)
        {
            Name (_HID, "PNP0C0C" /* Power Button Device */)  // _HID: Hardware ID
            Name (_UID, Zero)  // _UID: Unique ID
        }

        Device (GPIO)
        {
            Name (_HID, "ARMH0061")  // _HID: Hardware ID
            Name (_UID, Zero)  // _UID: Unique ID
            Name (_CRS, Buffer (0x17)  // _CRS: Current Resource Settings
            {
                /* 0000 */  0x86, 0x09, 0x00, 0x01, 0x00, 0x00, 0x06, 0x20,  // ....... 
                /* 0008 */  0x00, 0x10, 0x00, 0x00, 0x89, 0x06, 0x00, 0x01,  // ........
                /* 0010 */  0x01, 0x25, 0x00, 0x00, 0x00, 0x79, 0x00         // .%...y.
            })
            Name (_AEI, Buffer (0x20)  // _AEI: ACPI Event Interrupts
            {
                /* 0000 */  0x8C, 0x1B, 0x00, 0x01, 0x00, 0x01, 0x00, 0x01,  // ........
                /* 0008 */  0x00, 0x01, 0x00, 0x00, 0x00, 0x00, 0x17, 0x00,  // ........
                /* 0010 */  0x00, 0x19, 0x00, 0x05, 0x00, 0x00, 0x00, 0x06,  // ........
                /* 0018 */  0x00, 0x47, 0x50, 0x49, 0x4F, 0x00, 0x79, 0x00   // .GPIO.y.
            })
            Method (_E06, 0, NotSerialized)  // _Exx: Edge-Triggered GPE, xx=0x00-0xFF
            {
                Notify (PWRB, 0x80) // Status Change
            }
        }
    }
}

