#!/usr/bin/env python3
"""Independently check ARM64 classic prelinked pointers against source binaries."""
import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import struct


class MachO:
    def __init__(self, data):
        self.data = data
        if len(data) < 32 or struct.unpack_from("<I", data)[0] != 0xFEEDFACF:
            raise ValueError("Expected little-endian Mach-O 64")
        count = struct.unpack_from("<I", data, 16)[0]
        self.segments = []
        self.commands = []
        self.symtab = None
        self.dysymtab = None
        offset = 32
        for _ in range(count):
            cmd, size = struct.unpack_from("<II", data, offset)
            if size < 8 or offset + size > len(data):
                raise ValueError("Invalid Mach-O load command")
            self.commands.append(cmd)
            if cmd == 0x19:
                fields = struct.unpack_from("<16sQQQQiiII", data, offset + 8)
                self.segments.append({"name": fields[0].rstrip(b"\0").decode(),
                                      "vmaddr": fields[1], "vmsize": fields[2],
                                      "fileoff": fields[3], "filesize": fields[4],
                                      "protection": fields[6]})
            elif cmd == 2:
                self.symtab = struct.unpack_from("<IIII", data, offset + 8)
            elif cmd == 0xB:
                self.dysymtab = struct.unpack_from("<18I", data, offset + 8)
            offset += size

    def file_offset(self, address, size=8):
        for seg in self.segments:
            delta = address - seg["vmaddr"]
            if 0 <= delta and delta + size <= seg["filesize"]:
                offset = seg["fileoff"] + delta
                if offset + size <= len(self.data):
                    return offset
        raise ValueError(f"Unmapped address {address:#x}")

    def pointer(self, address):
        return struct.unpack_from("<Q", self.data, self.file_offset(address))[0]

    def symbols(self):
        offset, count, strings, strings_size = self.symtab
        result = []
        for index in range(count):
            string, kind, section, desc, value = struct.unpack_from(
                "<IBBHQ", self.data, offset + index * 16)
            if string >= strings_size:
                raise ValueError("Symbol string outside table")
            end = self.data.index(b"\0", strings + string, strings + strings_size)
            result.append((self.data[strings + string:end].decode(), kind, value))
        return result

    def exports(self, slide=0):
        return {name: value + slide for name, kind, value in self.symbols()
                if not (kind & 0xE0) and kind & 1 and kind & 0xE == 0xE and value}

    def relocations(self, external):
        pair = 14 if external else 16
        offset, count = self.dysymtab[pair:pair + 2]
        for index in range(count):
            address, word = struct.unpack_from("<iI", self.data, offset + index * 8)
            yield address, word & 0xFFFFFF, (word >> 24) & 1, (word >> 25) & 3, (word >> 27) & 1, word >> 28


def verify(collection_path, kernel_path, bundles):
    collection_data = collection_path.read_bytes()
    collection = MachO(collection_data)
    info = next(s for s in collection.segments if s["name"] == "__PRELINK_INFO")
    entries = plistlib.loads(collection_data[info["fileoff"]:
                                            info["fileoff"] + info["filesize"]].rstrip(b"\0"))
    entries = {e["CFBundleIdentifier"]: e for e in entries["_PrelinkInfoDictionary"]}
    exports = MachO(kernel_path.read_bytes()).exports()
    reports = []
    for bundle in bundles:
        meta = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
        identifier = meta["CFBundleIdentifier"]
        entry = entries[identifier]
        original = MachO((bundle / "Contents/MacOS" / meta["CFBundleExecutable"]).read_bytes())
        load = entry["_PrelinkExecutableLoadAddr"]
        start = collection.file_offset(load, 4)
        linked = MachO(collection_data[start:start + entry["_PrelinkExecutableSize"]])
        assert linked.dysymtab[15] == 0, f"{identifier}: external relocations remain"
        assert 0x1E not in linked.commands, f"{identifier}: split metadata would suppress individual VM protections"
        for segment in linked.segments:
            assert not segment["vmaddr"] % 0x4000 and not segment["vmsize"] % 0x4000
            assert segment["protection"] & 6 != 6, f"{identifier}: writable executable segment"
        symbols = original.symbols()
        expected_slide = set()
        counts = {"imports": 0, "locals": 0}
        for external in (False, True):
            for address, symbol, pcrel, length, ext, kind in original.relocations(external):
                assert (pcrel, length, ext, kind) == (0, 3, int(external), 0)
                initial = original.pointer(address)
                if external:
                    name = symbols[symbol][0]
                    target = exports.get(name) or (exports.get(name[1:]) if name.startswith("_") else None)
                    assert target, f"{identifier}: unresolved {name}"
                    expected = (initial + target) & 0xFFFFFFFFFFFFFFFF
                    counts["imports"] += 1
                elif symbol:
                    expected = (initial + load) & 0xFFFFFFFFFFFFFFFF
                    counts["locals"] += 1
                else:
                    assert linked.pointer(load + address) == initial
                    continue
                actual = linked.pointer(load + address)
                assert actual == expected, f"{identifier}: pointer {address:#x}: {actual:#x} != {expected:#x}"
                expected_slide.add(address)
        slide = set()
        for address, symbol, pcrel, length, ext, kind in linked.relocations(False):
            assert (pcrel, length, ext, kind) == (0, 3, 0, 0)
            slide.add(address)
        assert slide == expected_slide, f"{identifier}: normalized slide table is incomplete"
        kmod = entry["_PrelinkKmodInfo"]
        module_start = linked.pointer(kmod + 180)
        module_stop = linked.pointer(kmod + 188)
        for function in (module_start, module_stop):
            assert any(s["vmaddr"] <= function < s["vmaddr"] + s["vmsize"]
                       and s["protection"] & 4 for s in linked.segments), f"{identifier}: nonexecutable kmod hook"
        exports.update(original.exports(load))
        reports.append({"identifier": identifier, "load_address": hex(load),
                        "module_start": hex(module_start), "module_stop": hex(module_stop),
                        "imports_verified": counts["imports"], "locals_verified": counts["locals"],
                        "slide_entries_verified": len(slide)})
    return {"status": "passed", "collection_sha256": hashlib.sha256(collection_data).hexdigest(),
            "kernel_sha256": hashlib.sha256(kernel_path.read_bytes()).hexdigest(), "kexts": reports,
            "total_pointers_verified": sum(r["imports_verified"] + r["locals_verified"] for r in reports)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collection", type=Path, required=True)
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, action="append", required=True,
                        help="Original bundles in the collection's dependency order")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = json.dumps(verify(args.collection, args.kernel, args.bundle), indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report)
    print(report, end="")


if __name__ == "__main__":
    main()
