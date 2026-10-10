"""Bounded, read-only inspection of source-profile Apple ARM64 handoff bytes.

An offline result never establishes physical kernel execution or MMU validity.
The Apple flattened device tree is not a standard big-endian Linux FDT.
"""
from __future__ import annotations

import hashlib
import struct

U64_LIMIT = 1 << 64
PROFILES = {
    "apple-xnu-12377": {"command_line_size": 1024, "size": 1152},
    "puredarwin-xnu-7195": {"command_line_size": 608, "size": 736},
}
MAX_ADT_BYTES = 16 * 1024 * 1024
MAX_NODES = 65536
MAX_PROPERTIES = 65536
MAX_DEPTH = 64
MAX_MEMORY_RANGES = 4096


class HandoffError(ValueError):
    """Untrusted input does not satisfy the explicit offline contract."""


def u64(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < U64_LIMIT:
        raise HandoffError(f"{name}: expected an unsigned 64-bit integer")
    return value


def interval(base, size, name):
    base, size = u64(base, name + ".base"), u64(size, name + ".size")
    if size == 0 or base + size >= U64_LIMIT:
        raise HandoffError(f"{name}: empty or overflowing half-open range")
    return {"name": name, "base": base, "size": size, "end": base + size}


def c_string(data, name, max_length=None):
    terminator = data.find(b"\0")
    if terminator < 0 or (max_length is not None and terminator > max_length):
        raise HandoffError(f"{name}: missing or out-of-bounds NUL terminator")
    try:
        return data[:terminator].decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise HandoffError(f"{name}: invalid UTF-8") from error


def parse_boot_args(data, profile):
    if profile not in PROFILES:
        raise HandoffError("unknown source profile; automatic ABI inference is forbidden")
    layout = PROFILES[profile]
    if len(data) != layout["size"]:
        raise HandoffError(f"{profile}: expected exactly {layout['size']} boot_args bytes")
    revision, version = struct.unpack_from("<HH", data)
    if (revision, version) != (2, 2):
        raise HandoffError("only the explicitly reviewed Version 2 / Revision 2 layout is supported")
    result = {"profile": profile, "revision": revision, "version": version}
    for name, offset in (("virtBase", 8), ("physBase", 16), ("memSize", 24),
                         ("topOfKernelData", 32), ("deviceTreeP", 96)):
        result[name] = struct.unpack_from("<Q", data, offset)[0]
    result["video"] = dict(zip(("base", "display", "row_bytes", "width", "height", "depth"),
                               struct.unpack_from("<6Q", data, 40)))
    result["machineType"] = struct.unpack_from("<I", data, 88)[0]
    result["deviceTreeLength"] = struct.unpack_from("<I", data, 104)[0]
    count = layout["command_line_size"]
    result["commandLine"] = c_string(data[108:108 + count], "CommandLine")
    flags_offset = (108 + count + 7) & ~7
    result["bootFlags"], result["memSizeActual"] = struct.unpack_from("<QQ", data, flags_offset)
    interval(result["physBase"], result["memSize"], "kernel-managed-memory")
    interval(result["virtBase"], result["memSize"], "kernel-linear-virtual-window")
    if not result["physBase"] <= result["topOfKernelData"] < result["physBase"] + result["memSize"]:
        raise HandoffError("topOfKernelData lies outside kernel-managed memory")
    if not 0 < result["deviceTreeLength"] <= MAX_ADT_BYTES or result["deviceTreeP"] == 0:
        raise HandoffError("invalid device-tree pointer or size")
    return result


def parse_adt(data):
    if not 8 <= len(data) <= MAX_ADT_BYTES:
        raise HandoffError("ADT size exceeds the bounded input contract")
    cursor, node_count, property_count = 0, 0, 0

    def take(count):
        nonlocal cursor
        if count < 0 or count > len(data) - cursor:
            raise HandoffError(f"truncated ADT at byte {cursor}: need {count} bytes")
        value = data[cursor:cursor + count]
        cursor += count
        return value

    def node(depth):
        nonlocal node_count, property_count
        if depth > MAX_DEPTH or node_count >= MAX_NODES:
            raise HandoffError("ADT depth or node limit exceeded")
        node_count += 1
        properties, children = struct.unpack("<II", take(8))
        if not properties or properties > (len(data) - cursor) // 36:
            raise HandoffError("invalid or impossible ADT property count")
        if property_count + properties > MAX_PROPERTIES or children > MAX_NODES - node_count:
            raise HandoffError("ADT declared node/property count exceeds limits")
        property_count += properties
        values = {}
        for _ in range(properties):
            name_bytes, length = struct.unpack("<32sI", take(36))
            name = c_string(name_bytes, "ADT property name", 31)
            if not name or name in values:
                raise HandoffError("empty or duplicate ADT property name")
            values[name] = take(length)
            take((-length) & 3)
        if "name" not in values:
            raise HandoffError("ADT node has no name property")
        name = c_string(values["name"], "ADT node name", 63)
        if not name or "/" in name or name in (".", ".."):
            raise HandoffError("invalid ADT node name")
        descendants = [node(depth + 1) for _ in range(children)]
        if len({child["name"] for child in descendants}) != len(descendants):
            raise HandoffError("duplicate sibling ADT node names")
        return {"name": name, "properties": values, "children": descendants}

    root = node(0)
    if cursor != len(data):
        raise HandoffError(f"unconsumed ADT bytes at {cursor}; trailing data is not silently accepted")
    return root


def walk_adt(root):
    def visit(node, path):
        yield path, node
        for child in node["children"]:
            child_path = path.rstrip("/") + "/" + child["name"]
            yield from visit(child, child_path)
    return visit(root, "/")


def summarize_adt(root):
    return [{"path": path, "properties": {name: {"length": len(value),
             "sha256": hashlib.sha256(value).hexdigest()} for name, value in node["properties"].items()}}
            for path, node in walk_adt(root)]


def memory_map_ranges(root):
    entries = dict(walk_adt(root))
    if "/chosen/memory-map" not in entries:
        raise HandoffError("missing /chosen/memory-map; reservations cannot be inferred")
    ranges = []
    for name, value in entries["/chosen/memory-map"]["properties"].items():
        if name == "name":
            continue
        if len(value) != 16:
            raise HandoffError(f"memory-map/{name}: unsupported range encoding, expected one LP64 DTMemoryMapRange")
        base, size = struct.unpack("<QQ", value)
        ranges.append(interval(base, size, "adt:" + name))
    if not ranges:
        raise HandoffError("empty /chosen/memory-map")
    return ranges


def validate_memory_plan(boot, root, plan, adt_length):
    if not isinstance(plan, dict) or type(plan.get("schema_version")) is not int or plan["schema_version"] != 1:
        raise HandoffError("memory plan schema_version must be 1")
    page = plan.get("page_size")
    if page not in (4096, 16384) or isinstance(page, bool):
        raise HandoffError("explicit 4096/16384 page size required; this is not a hardware observation")
    if boot["deviceTreeLength"] != adt_length:
        raise HandoffError("boot_args deviceTreeLength does not match the supplied ADT bytes")
    ram_values, reserved_values = plan.get("ram"), plan.get("reservations")
    if not isinstance(ram_values, list) or not ram_values or not isinstance(reserved_values, list):
        raise HandoffError("explicit nonempty RAM and reservation arrays required")
    if len(ram_values) + len(reserved_values) > MAX_MEMORY_RANGES:
        raise HandoffError("memory plan range limit exceeded")
    def from_record(record):
        if not isinstance(record, dict) or not isinstance(record.get("name"), str) or not record["name"]:
            raise HandoffError("named memory range required")
        return interval(record.get("base"), record.get("size"), record["name"])
    ram = [from_record(item) for item in ram_values]
    reserved = [from_record(item) for item in reserved_values]
    managed = interval(boot["physBase"], boot["memSize"], "kernel-managed-memory")
    if any(boot[key] % page for key in ("physBase", "virtBase", "memSize", "topOfKernelData")):
        raise HandoffError("kernel-managed bases, size and allocation top must match the explicit page alignment")
    if any(item["base"] % page or item["size"] % page for item in ram):
        raise HandoffError("RAM ranges must match the explicit page alignment")
    def contains(outer, inner):
        return outer["base"] <= inner["base"] and inner["end"] <= outer["end"]
    if not any(contains(item, managed) for item in ram):
        raise HandoffError("kernel-managed memory is not wholly covered by an explicit RAM range")
    pointer = boot["deviceTreeP"]
    space = plan.get("device_tree_pointer_space")
    if space == "kernel_virtual":
        window = interval(boot["virtBase"], boot["memSize"], "kernel-linear-virtual-window")
        if not window["base"] <= pointer or pointer + adt_length > window["end"]:
            raise HandoffError("deviceTreeP is outside the stated kernel linear virtual window")
        pointer = pointer - boot["virtBase"] + boot["physBase"]
    elif space != "physical":
        raise HandoffError("explicit device_tree_pointer_space is required; no pointer-space guessing")
    reserved += [interval(plan.get("boot_args_phys"), PROFILES[boot["profile"]]["size"], "boot-args"),
                 interval(pointer, adt_length, "device-tree")]
    reserved += memory_map_ranges(root)
    if len(ram) + len(reserved) > MAX_MEMORY_RANGES:
        raise HandoffError("memory range limit exceeded")
    names = [item["name"] for item in ram + reserved]
    if len(names) != len(set(names)):
        raise HandoffError("duplicate memory range names")
    allow = plan.get("allowed_containment_overlaps", [])
    if not isinstance(allow, list) or len(allow) > 8192 or any(not isinstance(pair, list) or len(pair) != 2 or
        not all(isinstance(name, str) for name in pair) or pair[0] == pair[1] for pair in allow):
        raise HandoffError("invalid named containment overlap allowlist")
    allowances = {frozenset(pair) for pair in allow}
    reserved_names = {item["name"] for item in reserved}
    if any(not pair <= reserved_names for pair in allowances):
        raise HandoffError("overlap allowance refers to an unknown reservation")
    comparisons = 0
    for group in (ram, reserved):
        ordered = sorted(group, key=lambda item: item["base"])
        for index, left in enumerate(ordered):
            for other_index in range(index + 1, len(ordered)):
                right = ordered[other_index]
                if right["base"] >= left["end"]:
                    break
                comparisons += 1
                if comparisons > 100000:
                    raise HandoffError("memory overlap comparison budget exceeded")
                if group is ram or frozenset((left["name"], right["name"])) not in allowances:
                    raise HandoffError(f"overlap: {left['name']} and {right['name']}")
                if not (contains(left, right) or contains(right, left)):
                    raise HandoffError("partial overlap cannot be waived as containment")
    for item in reserved:
        if not any(contains(region, item) for region in ram):
            raise HandoffError(f"reservation {item['name']} is outside explicit RAM")
    # Return reservations, never a guessed free-memory map. Firmware carveouts
    # and kernel collection segments still need independent capture/review.
    return {"page_size_assumption": page, "kernel_managed": managed, "ram": ram,
            "reservations": reserved, "allowed_containment_overlaps": allow,
            "free_memory_derived": False}
