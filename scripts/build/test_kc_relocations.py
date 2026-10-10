#!/usr/bin/env python3
"""Check that the patched KC builder rejects malformed ARM64 pointer tables."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import plistlib


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--builder", type=Path, required=True)
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    bundle = args.bundle.resolve()
    metadata = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
    relative_binary = Path("Contents/MacOS") / metadata["CFBundleExecutable"]
    original = (bundle / relative_binary).read_bytes()
    count = struct.unpack_from("<I", original, 16)[0]
    offset = 32
    dysymoff = symoff = None
    for _ in range(count):
        command, size = struct.unpack_from("<II", original, offset)
        if command == 0xB:
            dysymoff = offset
        elif command == 2:
            symoff = offset
        offset += size
    assert dysymoff is not None and symoff is not None
    table = struct.unpack_from("<I", original, dysymoff + 8 + 14 * 4)[0]
    symbols = struct.unpack_from("<I", original, symoff + 12)[0]
    word = struct.unpack_from("<I", original, table + 4)[0]
    cases = [
        ("unsupported_type", table + 4, word | (4 << 28), "unsupported dynamic relocation"),
        ("unresolved_symbol", table + 4, (word & ~0xFFFFFF) | symbols,
         "unresolved external relocation symbol"),
        ("unmapped_target", table, 0x7FFFFFF8, "relocation target outside mapped image"),
        ("table_outside_image", dysymoff + 8 + 14 * 4, len(original) + 8,
         "relocation table outside image"),
    ]
    results = []
    with tempfile.TemporaryDirectory(prefix="blauos-kc-relocation-test-") as directory:
        root = Path(directory)
        for name, field, value, expected in cases:
            candidate = root / name / bundle.name
            shutil.copytree(bundle, candidate)
            modified = bytearray(original)
            struct.pack_into("<I", modified, field, value)
            (candidate / relative_binary).write_bytes(modified)
            output = root / name / "kernel.classic"
            argv = [str(args.builder.resolve()), "-kernel", str(args.kernel.resolve()),
                    "-kext", str(candidate), "-classic", "-kext-vmbase",
                    "0xfffffff004004000", "-o", str(output)]
            process = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            assert process.returncode != 0, f"{name}: malformed input was accepted"
            assert expected in process.stdout, f"{name}: wrong diagnostic: {process.stdout}"
            assert not output.exists(), f"{name}: partial collection was emitted"
            results.append({"case": name, "exit_code": process.returncode,
                            "diagnostic": expected, "output_written": False})
    assert (bundle / relative_binary).read_bytes() == original
    report = {"status": "passed", "original_binary_sha256": hashlib.sha256(original).hexdigest(),
              "original_input_unchanged": True, "cases": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
