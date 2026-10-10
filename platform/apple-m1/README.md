# M1 platform module boundary

The `handoff/` module implements bounded offline decoding of explicit pinned
boot-argument layouts, Apple device trees and reservation plans. It does not
initialize hardware or supply a kernel allocator. See the
[handoff contract](../../docs/platform/m1-handoff.md).

Source-built M1 platform drivers remain unverified. No driver is implemented
by creating a directory or passing offline parser tests.

Modules own their hardware discovery, MMIO resources, interrupt setup, DMA buffer
lifetimes, clocks/resets, cleanup, and diagnostics. Platform hooks in XNU should
be narrow and separately reviewed. UART, AIC, DART, ANS, Ethernet, and CPU power
management remain separate subsystems. Generic kernel fixes belong in the
ordered XNU patch series, not in a large hardware fork.

QEMU GIC/virtio support does not establish Apple M1 hardware support.
