#!/bin/sh
# A bounded real-kernel experiment, separate from the userspace/network smoke gate.
set -eu
if [ "$#" -ne 7 ] && [ "$#" -ne 8 ]; then
  echo 'usage: kernel-checkpoint.sh QEMU CODE_FD VARS_FD LOADER_EFI KERNEL OUTPUT TIMEOUT_SECONDS [BOOT_ARGS]' >&2
  exit 2
fi
qemu=$1
code=$2
vars_template=$3
loader=$4
kernel=$5
output=$6
seconds=$7
boot_args=${8:--v -s debug=0x218 serial=3 keepsyms=1 no_interrupt_masked_debug=1}
for file in "$qemu" "$code" "$vars_template" "$loader" "$kernel"; do
  test -f "$file" || { echo "missing artifact: $file" >&2; exit 2; }
done
case "$output" in /dev/*) echo 'output must be an ordinary directory' >&2; exit 2 ;; esac
case "$seconds" in ''|*[!0-9]*) echo 'timeout must be an integer' >&2; exit 2 ;; esac
test "$seconds" -ge 1 && test "$seconds" -le 600
mkdir -p "$output"
image="$output/kernel-checkpoint.img"
truncate -s 64M "$image"
mkfs.fat -F 32 "$image" > "$output/image-build.log"
mmd -i "$image" ::/EFI ::/EFI/BOOT
mcopy -i "$image" "$loader" ::/EFI/BOOT/BOOTAA64.EFI
mcopy -i "$image" "$kernel" ::/EFI/BOOT/kernel
printf '%s' "$boot_args" > "$output/boot-args.txt"
mcopy -i "$image" "$output/boot-args.txt" ::/EFI/BOOT/boot-args.txt
cp "$vars_template" "$output/AAVMF_VARS.fd"
chmod u+w "$output/AAVMF_VARS.fd"
sha256sum "$kernel" "$loader" "$code" "$vars_template" > "$output/input-sha256.txt"
"$qemu" --version > "$output/qemu-version.txt"
set +e
timeout --kill-after=5 "$seconds" "$qemu" \
  -machine virt-10.2,gic-version=3 -accel tcg -cpu neoverse-n2 -smp 1 -m 1024 \
  -drive "if=pflash,format=raw,unit=0,readonly=on,file=$code" \
  -drive "if=pflash,format=raw,unit=1,file=$output/AAVMF_VARS.fd" \
  -drive "if=none,id=system,format=raw,snapshot=on,file=$image" \
  -device virtio-blk-pci,drive=system,bootindex=1 -device ramfb \
  -display none -monitor none -serial stdio -no-reboot -no-shutdown \
  > "$output/serial.log" 2> "$output/qemu.stderr.log"
status=$?
set -e
printf '%s\n' "$status" > "$output/qemu-exit-code.txt"
if grep -q 'Darwin Kernel Version 20.5.0' "$output/serial.log"; then
  printf '%s\n' '{"schema_version":1,"guest_kernel_execution_observed":true,"userspace_boot_passed":false,"networking_passed":false,"physical_boot_gate":false}' > "$output/result.json"
  cat "$output/result.json"
  exit 0
fi
printf '%s\n' '{"schema_version":1,"guest_kernel_execution_observed":false,"userspace_boot_passed":false,"networking_passed":false,"physical_boot_gate":false}' > "$output/result.json"
cat "$output/result.json"
exit 1
