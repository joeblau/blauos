#!/usr/bin/env python3
"""Read an ARM64 PID 1 core from a retained ordinary GPT guest disk file."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def extract(args):
    if sys.platform != "linux":
        raise ValueError("Run read-only filesystem inspection in Linux")
    if not args.disk.is_file() or str(args.disk).startswith("/dev/"):
        raise ValueError("The guest disk must be an ordinary image file")
    if args.output.exists():
        raise ValueError("Use a fresh output directory")
    if not args.core_path.startswith("/") or ".." in Path(args.core_path).parts:
        raise ValueError("Invalid guest core path")
    if any(ord(character) < 32 for operand in (args.core_path, str(args.disk), str(args.output)) for character in operand):
        raise ValueError("Filesystem operands must not contain control characters")
    table = json.loads(subprocess.check_output([args.sfdisk, "--json", str(args.disk)], text=True))["partitiontable"]
    if table.get("label") != "gpt" or table.get("sectorsize") != 512:
        raise ValueError("Expected the assembled GPT disk with 512-byte sectors")
    roots = [partition for partition in table["partitions"] if partition.get("name") == "Darwin ext4 Root"]
    if len(roots) != 1:
        raise ValueError("Expected exactly one Darwin ext4 Root partition")
    partition = roots[0]
    start, size = partition["start"] * 512, partition["size"] * 512
    if start < 0 or size <= 0 or start + size > args.disk.stat().st_size:
        raise ValueError("Root partition exceeds the image file")
    args.output.mkdir(parents=True)
    root = args.output / "root-after-crash.ext4"
    with args.disk.open("rb") as source, root.open("wb") as target:
        source.seek(start)
        remaining = size
        while remaining:
            block = source.read(min(1024 * 1024, remaining))
            if not block:
                raise ValueError("Image ended before the root partition")
            target.write(block)
            remaining -= len(block)
    core = args.output / Path(args.core_path).name
    guest_operand = json.dumps(args.core_path, ensure_ascii=False)
    core_operand = json.dumps(str(core), ensure_ascii=False)
    commands = [[args.debugfs, "-R", "stat " + guest_operand, str(root)],
                [args.debugfs, "-R", "dump " + guest_operand + " " + core_operand, str(root)]]
    for name, command in zip(("core-stat.log", "core-dump.log"), commands):
        with (args.output / name).open("w") as stream:
            subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, check=True)
    # debugfs may exit zero for a missing file; require a genuine ARM64 Mach-O core.
    with core.open("rb") as stream:
        header = stream.read(32)
    if len(header) != 32 or struct.unpack_from("<III", header)[:2] != (0xFEEDFACF, 0x100000C) or struct.unpack_from("<I", header, 12)[0] != 4:
        raise ValueError("Extracted file is not an ARM64 Mach-O core")
    result = {"schema_version": 1, "core_extracted": True, "guest_boot_passed": False,
              "disk_sha256": digest(args.disk), "root_partition": partition,
              "core_path": args.core_path, "core_bytes": core.stat().st_size,
              "core_sha256": digest(core), "commands": commands}
    (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disk", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--core-path", default="/cores/core.1")
    parser.add_argument("--sfdisk", default="sfdisk")
    parser.add_argument("--debugfs", default="debugfs")
    args = parser.parse_args()
    args.disk, args.output = args.disk.resolve(), args.output.resolve()
    try:
        print(json.dumps(extract(args), indent=2))
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
