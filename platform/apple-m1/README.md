# M1 platform module boundary

This directory is reserved for source-built M1 platform drivers. No driver is
implemented by creating this directory.

Modules own their hardware discovery, MMIO resources, interrupt setup, DMA buffer
lifetimes, clocks/resets, cleanup, and diagnostics. Platform hooks in XNU should
be narrow and separately reviewed. UART, AIC, DART, ANS, Ethernet, and CPU power
management remain separate subsystems. Generic kernel fixes belong in the
ordered XNU patch series, not in a large hardware fork.

QEMU GIC/virtio support does not establish Apple M1 hardware support.
