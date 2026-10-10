#!/usr/bin/env python3
"""Build the pinned Darwin runtime and fail closed on incomplete provenance."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
from pathlib import Path
import platform
import re
import shutil
import shlex
import struct
import subprocess
import sys

REPO = Path(__file__).resolve().parents[2]
ARM64 = 0x0100000C
LOAD_LIBRARIES = {0xC, 0x80000018, 0x8000001F, 0x20, 0x80000023}
MAGICS = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
          b"\xfe\xed\xfa\xce", b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf",
          b"\xbe\xba\xfe\xca", b"\xbf\xba\xfe\xca"}


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        result = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
        return result.hexdigest()


def system_link_interface(source: Path, build_dir: Path, destination: Path) -> dict:
    """Declare actual selected source providers for the System/libdyld cycle.

    No implementation comes from the declaration file: the final staged Mach-O
    closure must resolve every import to a genuine runtime library.
    """
    link = build_dir / "src/Libraries/libSystem/stub/CMakeFiles/libSystem_B_stub.dir/link.txt"
    archives = set()
    for token in shlex.split(link.read_text()):
        candidate = token.rsplit(",", 1)[-1]
        if candidate.endswith(".a"):
            path = Path(candidate)
            path = path if path.is_absolute() else link.parents[2] / path
            if not path.is_file():
                raise ValueError(f"selected source System provider archive missing: {path}")
            archives.add(path.resolve())
    if not archives:
        raise ValueError("source libSystem link interface has no compiled providers")
    output = subprocess.check_output(["xcrun", "nm", "-g", "-U", *map(str, sorted(archives))], text=True)
    defined = {line.split()[-1] for line in output.splitlines()
               if re.search(r"\s[ASTDBICW]\s+\S+$", line)}
    requested = {line.split("#", 1)[0].strip() for line in
                 (source / "src/Libraries/libSystem/stub/libSystem.exports").read_text().splitlines()}
    symbols = sorted((requested - {""}) & defined)
    if not {"_malloc", "_free", "_pthread_create", "_mach_vm_region", "__os_once"} <= set(symbols):
        raise ValueError("source libSystem interface lacks required actual providers")
    destination.parent.mkdir(parents=True, exist_ok=True)
    declaration = "--- !tapi-tbd\ntbd-version: 4\ntargets: [ arm64-macos ]\ninstall-name: '/usr/lib/libSystem.B.dylib'\ncurrent-version: 1.0\ncompatibility-version: 1.0\nexports:\n  - targets: [ arm64-macos ]\n    symbols:\n"
    destination.write_text(declaration + "".join("      - " + json.dumps(symbol) + "\n" for symbol in symbols) + "...\n")
    return {"path": str(destination), "sha256": digest(destination), "declaration_only": True,
            "symbols_count": len(symbols), "symbols_sha256": hashlib.sha256(json.dumps(symbols).encode()).hexdigest(),
            "providers": [{"path": str(path), "sha256": digest(path)} for path in sorted(archives)]}


def macho(path: Path) -> dict | None:
    """Read bounded Mach-O load commands, including ARM64 universal slices."""
    size = path.stat().st_size
    with path.open("rb") as stream:
        magic = stream.read(4)
        if magic not in MAGICS:
            return None
        offset, extent = 0, size
        if magic in {b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"}:
            count = struct.unpack(">I", stream.read(4))[0]
            record_size = 32 if magic[-1] == 0xBF else 20
            if count > 128 or 8 + count * record_size > size:
                raise ValueError("invalid universal architecture table")
            candidates = []
            for _ in range(count):
                record = stream.read(record_size)
                cpu = struct.unpack_from(">I", record)[0]
                start, length = struct.unpack_from(">QQ" if record_size == 32 else ">II", record, 8)
                if start + length > size:
                    raise ValueError("universal slice outside file")
                if cpu == ARM64:
                    candidates.append((start, length))
            if not candidates:
                raise ValueError("no ARM64 slice")
            offset, extent = candidates[0]
        stream.seek(offset)
        header = stream.read(32)
        if len(header) != 32 or header[:4] != b"\xcf\xfa\xed\xfe":
            raise ValueError("expected little-endian 64-bit Mach-O")
        _, cpu, subtype, kind, count, command_bytes, flags, _ = struct.unpack("<8I", header)
        if cpu != ARM64:
            raise ValueError(f"expected ARM64, found CPU 0x{cpu:x}")
        if command_bytes > extent - 32 or count > command_bytes // 8:
            raise ValueError("load command table outside Mach-O slice")
        commands = stream.read(command_bytes)
        result = {"architecture": "arm64", "cpu_subtype": subtype, "file_type": kind,
                  "flags": flags, "dependencies": [], "rpaths": [], "dylinker": None,
                  "flat_namespace_imports": [], "imports": [], "defined_external_symbols": []}
        cursor = 0
        for _ in range(count):
            if cursor + 8 > len(commands):
                raise ValueError("truncated load command")
            cmd, length = struct.unpack_from("<II", commands, cursor)
            if length < 8 or cursor + length > len(commands):
                raise ValueError("invalid load command length")
            if cmd in LOAD_LIBRARIES | {0xE, 0x8000001C}:
                minimum = 24 if cmd in LOAD_LIBRARIES else 12
                if length < minimum:
                    raise ValueError("truncated string load command")
                string_offset = struct.unpack_from("<I", commands, cursor + 8)[0]
                if not minimum <= string_offset < length:
                    raise ValueError("load command string outside command")
                raw = commands[cursor + string_offset:cursor + length]
                if b"\0" not in raw:
                    raise ValueError("unterminated load command string")
                name = raw.split(b"\0", 1)[0].decode("utf-8", errors="strict")
                if cmd == 0xE:
                    if result["dylinker"] is not None:
                        raise ValueError("multiple dynamic linkers")
                    result["dylinker"] = name
                elif cmd == 0x8000001C:
                    result["rpaths"].append(name)
                else:
                    result["dependencies"].append({"path": name, "weak": cmd == 0x80000018,
                                                   "reexport": cmd == 0x8000001F})
            if cmd == 2:  # LC_SYMTAB; enough to validate explicit provider exports.
                if length < 24:
                    raise ValueError("truncated symbol table command")
                symoff, nsyms, stroff, strsize = struct.unpack_from("<4I", commands, cursor + 8)
                if symoff + nsyms * 16 > extent or stroff + strsize > extent:
                    raise ValueError("symbol table outside Mach-O slice")
                stream.seek(offset + stroff)
                strings = stream.read(strsize)
                stream.seek(offset + symoff)
                for _ in range(nsyms):
                    string_index, symbol_type, section, description, value = struct.unpack("<IBBHQ", stream.read(16))
                    if symbol_type & 1 and symbol_type & 0xE and not symbol_type & 0xF0:
                        if string_index >= len(strings) or b"\0" not in strings[string_index:]:
                            raise ValueError("invalid symbol name")
                        result["defined_external_symbols"].append(strings[string_index:].split(b"\0", 1)[0].decode())
            if cmd == 0x80000034:  # LC_DYLD_CHAINED_FIXUPS import descriptors.
                if length < 16:
                    raise ValueError("truncated chained fixups command")
                fixoff, fixsize = struct.unpack_from("<II", commands, cursor + 8)
                if fixsize < 28 or fixoff + fixsize > extent:
                    raise ValueError("chained fixups outside Mach-O slice")
                stream.seek(offset + fixoff)
                fixups = stream.read(fixsize)
                version, starts, imports_offset, symbols_offset, import_count, import_format, symbols_format = struct.unpack_from("<7I", fixups)
                sizes = {1: 4, 2: 8, 3: 16}
                if version or symbols_format or import_format not in sizes or imports_offset + import_count * sizes.get(import_format, 0) > fixsize or symbols_offset > fixsize:
                    raise ValueError("unsupported or malformed chained import table")
                for index in range(import_count):
                    descriptor = imports_offset + index * sizes[import_format]
                    bits = struct.unpack_from("<Q" if import_format == 3 else "<I", fixups, descriptor)[0]
                    mask, shift = (0xFFFF, 16) if import_format == 3 else (0xFF, 8)
                    ordinal, weak = bits & mask, bool((bits >> shift) & 1)
                    name_offset = bits >> (32 if import_format == 3 else 9)
                    symbol = symbols_offset + name_offset
                    if symbol >= fixsize or b"\0" not in fixups[symbol:]:
                        raise ValueError("chained import name outside symbols")
                    name = fixups[symbol:].split(b"\0", 1)[0].decode()
                    signed_ordinal = ordinal - mask - 1 if ordinal > mask // 2 else ordinal
                    result["imports"].append({"name": name, "weak": weak, "library_ordinal": signed_ordinal})
                    if signed_ordinal == -2:
                        result["flat_namespace_imports"].append({"name": name, "weak": weak})
            cursor += length
        return result


def within_root(root: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def local_source_revision(source_files: list[dict]) -> str:
    snapshot = json.dumps(sorted(source_files, key=lambda entry: entry["path"]), sort_keys=True, separators=(",", ":"))
    return "workspace-sha256:" + hashlib.sha256(snapshot.encode()).hexdigest()


def inspect(root: Path, provenance_path: Path | None, manifest_path: Path, project_root: Path = REPO) -> dict:
    root = root.resolve()
    errors, artifacts = [], []
    provenance = {}
    providers = []
    if provenance_path and provenance_path.is_file():
        data = json.loads(provenance_path.read_text())
        providers = data.get("flat_namespace_providers", [])
        for entry in data.get("artifacts", []):
            name = entry.get("path", "")
            if name in provenance:
                errors.append(f"duplicate provenance: {name}")
            provenance[name] = entry
    else:
        errors.append("source artifact provenance manifest is missing")
    lock = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
    source_pins = {item["name"]: item["revision"] for item in lock.get("sources", [])}
    archive_pins = {item["name"]: item for item in lock.get("archives", [])}
    source_pins.update({name: pin["sha256"] for name, pin in archive_pins.items()})
    if not root.is_dir():
        errors.append(f"staged root is missing: {root}")
    for current, directories, names in os.walk(root):
        # Never follow directory symlinks into a host runtime.
        for directory in directories:
            link = Path(current) / directory
            if link.is_symlink() and not within_root(root, link):
                errors.append(f"directory symlink escapes staged root: /{link.relative_to(root)}")
        directories[:] = [d for d in directories if not (Path(current) / d).is_symlink()]
        for name in names:
            target = Path(current) / name
            if not within_root(root, target):
                errors.append(f"symlink escapes staged root: /{target.relative_to(root)}")
                continue
            if not target.is_file():
                continue
            with target.open("rb") as stream:
                candidate = stream.read(4) in MAGICS
            if not candidate:
                continue
            install_path = "/" + str(target.relative_to(root))
            try:
                info = macho(target)
                info.update(path=install_path, sha256=digest(target))
                canonical_path = "/" + str(target.resolve().relative_to(root))
                entry = provenance.get(install_path, provenance.get(canonical_path, {}))
                if canonical_path != install_path:
                    info["symlink_target"] = canonical_path
                source, revision = entry.get("source"), entry.get("revision")
                info.update(source=source, source_revision=revision)
                amendments = entry.get("build_amendments", [])
                for amendment in amendments:
                    amendment_path = project_root / amendment.get("path", "")
                    if not within_root(project_root, amendment_path) or not amendment_path.is_file() or digest(amendment_path) != amendment.get("sha256"):
                        errors.append(f"build amendment missing or mismatched: {install_path}")
                info["build_amendments"] = amendments
                for additional in entry.get("additional_sources", []):
                    if source_pins.get(additional.get("source")) != additional.get("revision"):
                        errors.append(f"additional source is unpinned: {install_path}")
                if info["sha256"] != entry.get("sha256"):
                    errors.append(f"missing or mismatched source hash: {install_path}")
                if source == "blauos":
                    source_files = entry.get("source_files", [])
                    local_valid = bool(source_files) and revision == local_source_revision(source_files)
                    for local in source_files:
                        local_path = project_root / local.get("path", "")
                        if not within_root(project_root, local_path) or not local_path.is_file() or digest(local_path) != local.get("sha256"):
                            local_valid = False
                    if not local_valid:
                        errors.append(f"local project source snapshot missing or mismatched: {install_path}")
                    info["source_files"] = source_files
                    info["workspace_revision"] = entry.get("workspace_revision")
                elif not source or source_pins.get(source) != revision:
                    errors.append(f"unknown or unpinned source revision: {install_path}")
                elif source in archive_pins and entry.get("archive_sha256") != archive_pins[source]["sha256"]:
                    errors.append(f"source archive hash missing or mismatched: {install_path}")
                kernel = (install_path.startswith("/System/Library/Kernels/") and
                          info["file_type"] == 2 and not info["flags"] & 4 and
                          not info["dylinker"] and not info["dependencies"])
                info["artifact_kind"] = "kernel" if kernel else "userspace"
                if info["file_type"] == 2 and not kernel and info["dylinker"] != "/usr/lib/dyld":
                    errors.append(f"unexpected executable dynamic linker: {install_path}: {info['dylinker']}")
                dependencies = [d["path"] for d in info["dependencies"]]
                if info["dylinker"]:
                    dependencies.append(info["dylinker"])
                for dependency in dependencies:
                    if dependency.startswith("/"):
                        resolved = root / dependency.lstrip("/")
                    elif dependency.startswith("@loader_path/"):
                        resolved = target.parent / dependency[len("@loader_path/"):]
                    else:
                        # @executable_path and @rpath are context-dependent. A minimal
                        # system closure intentionally requires fixed system paths.
                        errors.append(f"context-dependent dependency rejected: {install_path}: {dependency}")
                        continue
                    if not within_root(root, resolved) or not resolved.is_file():
                        errors.append(f"dependency absent from staged root: {install_path}: {dependency}")
                    elif macho(resolved) is None:
                        errors.append(f"dependency is not ARM64 Mach-O: {install_path}: {dependency}")
                artifacts.append(info)
            except (OSError, ValueError, UnicodeError, struct.error) as exc:
                errors.append(f"invalid Mach-O: {install_path}: {exc}")
    present = {entry["path"] for entry in artifacts}
    by_path = {entry["path"]: entry for entry in artifacts}
    def dependency_path(owner, path):
        if path.startswith("@loader_path/"):
            return posixpath.normpath(posixpath.join(posixpath.dirname(owner), path[len("@loader_path/"):]))
        return path
    def loaded_dependencies(path, visited=None):
        visited = set() if visited is None else visited
        if path in visited or path not in by_path:
            return visited
        visited.add(path)
        entry = by_path[path]
        for dependency in entry["dependencies"]:
            # An optional load command cannot establish a required provider.
            if not dependency["weak"]:
                loaded_dependencies(dependency_path(path, dependency["path"]), visited)
        return visited
    def exports(path, visited=None):
        visited = set() if visited is None else visited
        if path in visited or path not in by_path:
            return set()
        visited.add(path)
        entry = by_path[path]
        names = set(entry["defined_external_symbols"])
        for dependency in entry["dependencies"]:
            if dependency["reexport"]:
                names.update(exports(dependency_path(path, dependency["path"]), visited))
        return names
    provider_symbols = set()
    for provider in providers:
        record = next((entry for entry in artifacts if entry["path"] == provider), None)
        if record is None:
            errors.append(f"flat namespace provider absent from staged root: {provider}")
        else:
            provider_symbols.update(exports(provider))
    for artifact in artifacts:
        loaded = loaded_dependencies(artifact["path"])
        artifact["loaded_flat_namespace_providers"] = sorted(set(providers) & loaded)
        for imported in artifact["flat_namespace_imports"]:
            if not imported["weak"] and imported["name"] not in provider_symbols:
                errors.append(f"required flat namespace import unresolved: {artifact['path']}: {imported['name']}")
            elif not imported["weak"] and not any(imported["name"] in exports(provider)
                                                   for provider in artifact["loaded_flat_namespace_providers"]):
                errors.append(f"required flat namespace provider is not loaded by dependency graph: {artifact['path']}: {imported['name']}")
        for imported in artifact["imports"]:
            ordinal = imported["library_ordinal"]
            if ordinal > 0 and not imported["weak"]:
                if ordinal > len(artifact["dependencies"]):
                    errors.append(f"import library ordinal outside dependency table: {artifact['path']}")
                else:
                    provider = dependency_path(artifact["path"], artifact["dependencies"][ordinal - 1]["path"])
                    if imported["name"] not in exports(provider):
                        errors.append(f"required dependency symbol unresolved: {artifact['path']}: {provider}: {imported['name']}")
    for required in ("/usr/lib/dyld", "/usr/lib/libSystem.B.dylib", "/usr/lib/system/libdyld.dylib"):
        if required not in present:
            errors.append(f"required runtime artifact missing: {required}")
    return {"schema_version": 1, "status": "pass" if not errors else "blocked",
            "root": str(root), "architecture": "arm64", "artifacts": artifacts,
            "errors": sorted(set(errors)), "offline_dependency_closure": not errors,
            "execution_verified": False, "host_dyld_shared_cache_used": "not_tested"}


def command(args: list[str], log: Path, env: dict | None = None) -> int:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w") as output:
        output.write("$ " + repr(args) + "\n")
        output.flush()
        result = subprocess.run(args, stdout=output, stderr=subprocess.STDOUT, env=env)
    return result.returncode


def write_result(path: Path, result: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2) + "\n")


def puredarwin_overlay(source: Path, output: Path, revision: str) -> Path:
    overlay = output / "source-overlay"
    patch = REPO / "runtime/compat/sdk27-dyld.patch"
    if not overlay.exists():
        subprocess.run(["git", "-C", str(source), "worktree", "add", "--detach", str(overlay), revision], check=True, capture_output=True)
    actual = subprocess.check_output(["git", "-C", str(overlay), "rev-parse", "HEAD"], text=True).strip()
    if actual != revision:
        raise ValueError("runtime overlay is based on a different source pin")
    diff = subprocess.check_output(["git", "-C", str(overlay), "diff"], text=True)
    if not diff:
        subprocess.run(["git", "-C", str(overlay), "apply", str(patch)], check=True, capture_output=True)
    elif diff != patch.read_text():
        raise ValueError("runtime overlay differs from reviewed SDK27 patch")
    return overlay


def compiler_rt(args: argparse.Namespace, output: Path, sdk: Path, result: dict) -> Path | None:
    source = args.sources_dir.resolve() / "llvm-project"
    lock = json.loads(args.manifest.read_text())
    pin = next((item for item in lock["sources"] if item["name"] == "llvm-project"), None)
    if not pin or not (source / "compiler-rt/lib/builtins/CMakeLists.txt").is_file():
        result["errors"].append("pinned llvm-project compiler-rt sources are missing")
        return None
    actual = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if actual != pin["revision"]:
        result["errors"].append("compiler-rt checkout differs from source pin")
        return None
    overlay, build_dir = output / "llvm-overlay", output / "compiler-rt-overlay"
    shutil.copytree(source, overlay, ignore=shutil.ignore_patterns(".git"), dirs_exist_ok=True)
    patch = REPO / "runtime/compat/compiler-rt-exports.patch"
    subprocess.run(["patch", "-p1", "-N", "-i", str(patch)], check=True, capture_output=True, cwd=overlay)
    prefix = output / "compiler-rt-install"
    configure = ["cmake", "-S", str(overlay / "compiler-rt/lib/builtins"), "-B", str(build_dir), "-G", "Unix Makefiles",
                 "-DCMAKE_BUILD_TYPE=Release", f"-DCMAKE_OSX_SYSROOT={sdk}", "-DCMAKE_OSX_ARCHITECTURES=arm64",
                 "-DCMAKE_OSX_DEPLOYMENT_TARGET=11.0", "-DCMAKE_C_COMPILER_TARGET=arm64-apple-macosx11.0",
                 "-DCMAKE_CXX_COMPILER_TARGET=arm64-apple-macosx11.0", "-DCMAKE_ASM_COMPILER_TARGET=arm64-apple-macosx11.0",
                 f"-DLLVM_CMAKE_DIR={overlay}/llvm/cmake/modules", "-DDARWIN_osx_BUILTIN_ARCHS=arm64",
                 "-DCOMPILER_RT_DEFAULT_TARGET_ONLY=ON", "-DCOMPILER_RT_INCLUDE_TESTS=OFF",
                 "-DCOMPILER_RT_ENABLE_IOS=OFF", "-DCOMPILER_RT_ENABLE_WATCHOS=OFF", "-DCOMPILER_RT_ENABLE_TVOS=OFF",
                 "-DCOMPILER_RT_ENABLE_MACCATALYST=OFF"]
    for name, invocation in (("compiler-rt-configure", configure), ("compiler-rt-build", ["cmake", "--build", str(build_dir), "--target", "clang_rt.builtins_arm64_osx", "--parallel", str(args.jobs)])):
        log = output / "logs" / f"{name}.log"
        code = command(invocation, log)
        result["steps"].append({"name": name, "command": invocation, "exit_code": code, "log": str(log)})
        if code:
            result["errors"].append(f"{name} failed")
            return None
    artifact = build_dir / "libclang_rt.builtins_arm64_osx.a"
    (prefix / "lib").mkdir(parents=True, exist_ok=True)
    shutil.copy2(artifact, prefix / "lib/libcompiler_rt.a")
    result["artifacts"].append({"path": str(prefix / "lib/libcompiler_rt.a"), "sha256": digest(artifact),
                                "source": "llvm-project", "revision": actual, "patch_sha256": digest(patch)})
    return prefix


def build(args: argparse.Namespace) -> dict:
    source = args.sources_dir.resolve() / args.source_name
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    logs, build_dir = output / "logs", output / "puredarwin-overlay"
    result = {"schema_version": 1, "status": "blocked", "independent_runtime": False,
              "profile": "installed-sdk-experiment", "steps": [], "artifacts": [], "errors": []}
    if platform.system() != "Darwin":
        result["errors"].append("installed-sdk experiment requires a macOS build host; use pinned upstream Nix on Linux")
        return result
    if not (source / "CMakeLists.txt").is_file():
        result["errors"].append(f"fetch {args.source_name} before building")
        return result
    revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    lock = json.loads(args.manifest.read_text())
    pin = next((item for item in lock["sources"] if item["name"] == args.source_name), None)
    if not pin or pin["revision"] != revision:
        result["errors"].append("source checkout does not match manifest pin")
        return result
    if subprocess.check_output(["git", "-C", str(source), "status", "--porcelain"], text=True).strip():
        result["errors"].append("source checkout is modified; build changes must use a reviewed overlay")
        return result
    sdk = args.sdk.resolve() if args.sdk else Path(subprocess.check_output(["xcrun", "--show-sdk-path"], text=True).strip())
    result.update(source=args.source_name, source_revision=revision, sdk=str(sdk),
                  sdk_settings_sha256=digest(sdk / "SDKSettings.json"),
                  compiler=subprocess.check_output(["xcrun", "clang", "--version"], text=True).strip())
    source = puredarwin_overlay(source, output, revision)
    result["source_patch_sha256"] = digest(REPO / "runtime/compat/sdk27-dyld.patch")
    result["annotation_header_sha256"] = digest(REPO / "runtime/compat/sdk27_compile.h")
    result["stack_probe_sha256"] = digest(REPO / "runtime/compat/arm64_stack_probe.S")
    rt_prefix = compiler_rt(args, output, sdk, result)
    if not rt_prefix:
        return result
    cxx_headers = output / "sdk-cxx-headers"
    if cxx_headers.is_symlink():
        cxx_headers.unlink()
    shutil.copytree(sdk / "usr/include/c++/v1", cxx_headers, dirs_exist_ok=True)
    env = os.environ.copy()
    env["PUREDARWIN_SDKROOT"] = str(sdk)
    env["MACOSX_DEPLOYMENT_TARGET"] = "11.0"
    compat = REPO / "runtime/compat/sdk27_compile.h"
    flags = f"-Wno-nullability-completeness -include {compat}"
    configure = ["cmake", "-S", str(source), "-B", str(build_dir), "-G", "Unix Makefiles",
                 "-DCMAKE_BUILD_TYPE=Debug", "-DCMAKE_OSX_DEPLOYMENT_TARGET=11.0", "-DCMAKE_POLICY_VERSION_MINIMUM=3.5",
                 "-DPUREDARWIN_ARCH=arm64", f"-DPUREDARWIN_MACOSX_SDK={sdk}",
                 "-DPUREDARWIN_ENABLE_PROJECTS=OFF", "-DPUREDARWIN_ENABLE_KERNEL=OFF",
                 "-DPUREDARWIN_ENABLE_LIBRARIES=ON", "-DPUREDARWIN_ENABLE_USERSPACE=OFF",
                 "-DPUREDARWIN_ENABLE_TOOLS=ON", f"-DPUREDARWIN_COMPILER_RT_PREFIX={rt_prefix}",
                 f"-DBLAUOS_STACK_PROBE={REPO / 'runtime/compat/arm64_stack_probe.S'}",
                 f"-DBLAUOS_LIBDYLD_EXPORTS={REPO / 'runtime/compat/libdyld.exports'}",
                 f"-DBLAUOS_SYSTEM_INTERFACE={output / 'system-interface/libSystem.tbd'}",
                 "-DBLAUOS_OBJC_DYLIB=",
                 f"-DBLAUOS_CXX_HEADERS={cxx_headers}", f"-DCMAKE_C_FLAGS={flags}", f"-DCMAKE_CXX_FLAGS={flags}"]
    stages = [("configure", configure),
              ("source-headers", ["cmake", "--build", str(build_dir), "--target", "libc_headers_gen_script", "--parallel", str(args.jobs)])]
    for stage, invocation in stages:
        code = command(invocation, logs / f"{stage}.log", env)
        result["steps"].append({"name": stage, "command": invocation, "exit_code": code, "log": str(logs / f"{stage}.log")})
        if code:
            result["errors"].append(f"{stage} failed; see log")
            return result
    # The runtime-only CMake profile does not configure XNU's header installer.
    # Point its expected build include path to the source-derived header stage.
    expected = build_dir / "src/Kernel/xnu/xnu_header_install/usr/include"
    expected.parent.mkdir(parents=True, exist_ok=True)
    headers = build_dir / "src/Libraries/libSystem/libc/headers/usr/include"
    if not expected.exists():
        expected.symlink_to(os.path.relpath(headers, expected.parent), target_is_directory=True)
    # Build genuine interface providers before linking the ordinary libdyld.
    # Main dyld has its own separately initialized bootstrap runtime.
    interface_targets = ("libsystem_kernel_static", "libplatform_static", "pthread_static",
                         "libmalloc_static", "libc_static", "libc_internal_static", "openlibm_static",
                         "corecrypto_static", "CrashReporterClient", "XPC_libxpc_static", "XPC_launchd_mig_static",
                         "XPC_notify_client_static", "mDNSResponder_dnssd_static", "XPC_libinfo_static",
                         "syslog_asl_static", "libsystem_trace_static", "libdarwin_static", "libSystemConfiguration_static",
                         "XPC_libnv_static", "XPC_libxpc_objc_static", "XPC_notifyd_mig_static", "pthread_full_static",
                         "libdispatch_static", "libresolv_static", "libcxxabi_static_build", "libunwind_static_build",
                         "libcxx_static_build", "commoncrypto_static")
    invocation = ["cmake", "--build", str(build_dir), "--target", *interface_targets, "--parallel", str(args.jobs)]
    code = command(invocation, logs / "system-interface-providers.log", env)
    result["steps"].append({"name": "system-interface-providers", "command": invocation, "exit_code": code,
                            "log": str(logs / "system-interface-providers.log")})
    if code:
        result["errors"].append("source System interface providers did not compile")
        return result
    result["system_link_interface"] = system_link_interface(source, build_dir, output / "system-interface/libSystem.tbd")
    for target in ("dyld", "libSystem_B_stub", "XPC_launchd_static", "XPC_launchd_mig_static"):
        invocation = ["cmake", "--build", str(build_dir), "--target", target, "--parallel", str(args.jobs)]
        code = command(invocation, logs / f"{target}.log", env)
        result["steps"].append({"name": target, "command": invocation, "exit_code": code, "log": str(logs / f"{target}.log")})
        if code:
            result["errors"].append(f"{target} failed; see log")
    for artifact in sorted(build_dir.rglob("*")):
        if artifact.is_file() and (artifact.suffix in {".a", ".dylib"} or artifact.name == "dyld"):
            result["artifacts"].append({"path": str(artifact), "sha256": digest(artifact),
                                        "source": args.source_name, "revision": revision})
    if not result["errors"]:
        stage = output / "root"
        mappings = {"/usr/lib/dyld": build_dir / "src/Libraries/dyld/dyld",
                    "/usr/lib/libSystem.B.dylib": build_dir / "src/Libraries/libSystem/stub/libSystem.B.dylib",
                    "/usr/lib/system/libdyld.dylib": build_dir / "src/Libraries/libSystem/libdyld/libdyld.dylib"}
        amendments = [{"path": path, "sha256": digest(REPO / path)} for path in
                      ("scripts/build/runtime.py", "runtime/compat/sdk27-dyld.patch", "runtime/compat/sdk27_compile.h",
                       "runtime/compat/arm64_stack_probe.S", "runtime/compat/compiler-rt-exports.patch",
                       "runtime/compat/libdyld.exports")]
        existing_provenance = output / "provenance.json"
        existing = json.loads(existing_provenance.read_text()) if existing_provenance.is_file() else {}
        records = [entry for entry in existing.get("artifacts", []) if entry["path"] not in mappings]
        for install_path, artifact in mappings.items():
            destination = stage / install_path.lstrip("/")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(artifact, destination)
            record = {"path": install_path, "sha256": digest(destination),
                            "source": args.source_name, "revision": revision,
                            "build_amendments": amendments,
                            "additional_sources": [{"source": "llvm-project", "revision": next(s["revision"] for s in lock["sources"] if s["name"] == "llvm-project")}],
                            "compiler": result["compiler"], "sdk_settings_sha256": result["sdk_settings_sha256"]}
            if install_path == "/usr/lib/system/libdyld.dylib":
                record["generated_link_interface"] = result["system_link_interface"]
            records.append(record)
        (stage / "usr/lib/libSystem.dylib").unlink(missing_ok=True)
        (stage / "usr/lib/libSystem.dylib").symlink_to("libSystem.B.dylib")
        provenance = output / "provenance.json"
        write_result(provenance, {"schema_version": 1, "artifacts": records,
                                 "flat_namespace_providers": ["/usr/lib/libobjc.A.dylib"]})
        audit = inspect(stage, provenance, args.manifest)
        write_result(output / "closure.json", audit)
        result.update(staged_root=str(stage), provenance=str(provenance),
                      closure=str(output / "closure.json"), offline_dependency_closure=audit["offline_dependency_closure"])
    # The build diagnostic does not stage or execute a target system. Mark this
    # explicitly even if all compile targets happen to finish successfully.
    result["source_patch_sha256"] = digest(REPO / "runtime/compat/sdk27-dyld.patch")
    result["base_build_verified"] = not result["errors"]
    result["independent_execution"] = False
    result["unmet_acceptance"] = ["complete target runtime and source userspace execution remain unverified"]
    if not result["errors"]:
        result["status"] = "source_build_pass"
    return result


def host_probe(args: argparse.Namespace) -> dict:
    output = args.output_dir.resolve() / "host-probe"
    output.mkdir(parents=True, exist_ok=True)
    binary = output / "runtime-probe"
    source = REPO / "runtime/probes/runtime_probe.c"
    fixture = output / "libblauos-loader-fixture.dylib"
    fixture_cmd = ["xcrun", "clang", "-arch", "arm64", "-dynamiclib", str(REPO / "runtime/probes/loader_fixture.c"),
                   "-o", str(fixture)]
    fixture_code = command(fixture_cmd, output / "fixture-compile.log")
    compile_cmd = ["xcrun", "clang", "-arch", "arm64", "-Wall", "-Wextra", "-Werror", "-fblocks", str(source), "-o", str(binary)]
    code = command(compile_cmd, output / "compile.log")
    steps = [{"command": fixture_cmd, "exit_code": fixture_code}, {"command": compile_cmd, "exit_code": code}]
    code = code or fixture_code
    if not code:
        env = os.environ.copy()
        env["DYLD_PRINT_LIBRARIES"] = "1"
        code = command([str(binary), str(output)], output / "execution.log", env)
        steps.append({"command": [str(binary), str(output)], "exit_code": code})
    return {"schema_version": 1, "status": "diagnostic_pass" if not code else "failed",
            "steps": steps, "binary": str(binary), "binary_sha256": digest(binary) if binary.exists() else None,
            "macho": macho(binary) if binary.exists() else None,
            "kernel": "stock host macOS", "runtime": "stock host dyld/libSystem",
            "host_dyld_shared_cache_used": True, "independent_runtime": False,
            "meets_issue_18_acceptance": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["build", "inspect", "host-probe"])
    parser.add_argument("--sources-dir", type=Path, default=REPO / ".blauos/sources")
    parser.add_argument("--source-name", default="puredarwin-vm-next")
    parser.add_argument("--output-dir", type=Path, default=REPO / ".blauos/build/runtime")
    parser.add_argument("--manifest", type=Path, default=REPO / "sources.lock.json")
    parser.add_argument("--sdk", type=Path)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--provenance", type=Path)
    parser.add_argument("--project-root", type=Path, default=REPO)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    if args.action == "inspect" and not args.root:
        parser.error("inspect requires --root")
    try:
        result = (inspect(args.root, args.provenance, args.manifest, args.project_root) if args.action == "inspect" else
                  host_probe(args) if args.action == "host-probe" else build(args))
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        result = {"schema_version": 1, "status": "blocked", "independent_runtime": False, "errors": [str(exc)]}
    output = args.output or args.output_dir / f"{args.action}-result.json"
    write_result(output, result)
    print(json.dumps({"status": result["status"], "result": str(output), "errors": result.get("errors", [])}, indent=2))
    return 0 if result["status"] in {"pass", "diagnostic_pass", "source_build_pass"} else 1


if __name__ == "__main__":
    sys.exit(main())
