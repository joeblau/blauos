#!/usr/bin/env python3
"""Build real source Objective-C and C++ ABI libraries for the guest runtime."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("blauos_runtime", REPO / "scripts/build/runtime.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)

MM_SOURCES = "hashtable2 maptable NSObject objc-accessors objc-auto objc-block-trampolines objc-cache objc-cache-old objc-class objc-class-old objc-errors objc-exception objc-file objc-file-old objc-initialize objc-layout objc-loadmethod objc-load objc-lockdebug objc-opt objc-os objc-references objc-runtime objc-runtime-new objc-runtime-old objc-sel objc-sel-old objc-sel-set objc-sync objc-typeencoding objc-weak objc-zalloc Object Protocol".split()
ASM_SOURCES = ["Messengers.subproj/objc-msg-arm64", "objc-blocktramps-arm64", "objc-sel-table"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources-dir", type=Path, default=REPO / ".blauos/sources")
    parser.add_argument("--output-dir", type=Path, default=REPO / ".blauos/build/runtime")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--sdk", type=Path)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--output", type=Path, default=REPO / "evidence/runtime/objc-build.json")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    source, build = output / "source-overlay", output / "puredarwin-overlay"
    root, objdir = (args.root or output / "root").resolve(), output / "objc"
    sdk = args.sdk or Path(subprocess.check_output(["xcrun", "--show-sdk-path"], text=True).strip())
    pin = next(s["revision"] for s in json.loads((REPO / "sources.lock.json").read_text())["sources"] if s["name"] == "puredarwin-vm-next")
    result = {"schema_version": 1, "status": "blocked", "source_revision": pin, "steps": [], "artifacts": [], "errors": []}
    if not (root / "usr/lib/libSystem.B.dylib").is_file():
        result["errors"].append("source-built staged libSystem is missing; run runtime.py build first")
        runtime.write_result(args.output, result)
        return 1
    revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if revision != pin:
        raise ValueError("Objective-C sources differ from lock")
    o = source / "src/Libraries/objc4"
    objc_headers, generated = objdir / "include/objc", objdir / "generated"
    objc_headers.mkdir(parents=True, exist_ok=True)
    generated.mkdir(parents=True, exist_ok=True)
    for header in (o / "runtime").glob("*.h"):
        shutil.copy2(header, objc_headers / header.name)
    # Trace instrumentation only: generated from the upstream probe definitions,
    # with DTrace explicitly disabled. No language/runtime operation is replaced.
    (generated / "objc-probes.h").write_text("#ifndef _OBJC_PROBES_H\n#define _OBJC_PROBES_H\n#define OBJC_RUNTIME_OBJC_EXCEPTION_THROW(arg0) do {} while (0)\n#define OBJC_RUNTIME_OBJC_EXCEPTION_RETHROW() do {} while (0)\n#define OBJC_RUNTIME_OBJC_EXCEPTION_THROW_ENABLED() (0)\n#define OBJC_RUNTIME_OBJC_EXCEPTION_RETHROW_ENABLED() (0)\n#endif\n")
    common_link = ["xcrun", "clang++", "-arch", "arm64", "-mmacosx-version-min=11.0", "-isysroot", str(sdk), "-dynamiclib", "-nostdlib", "-Wl,-Z",
                   f"-L{root}/usr/lib", f"-Wl,-dylib_file,/usr/lib/system/libdyld.dylib:{root}/usr/lib/system/libdyld.dylib",
                   "-Wl,-platform_version,macos,11.0,11.5", "-Wl,-fixup_chains", "-Wl,-not_for_dyld_shared_cache"]
    abi = objdir / "libc++abi.dylib"
    archive_paths = [build / "src/Libraries/libcxxabi/libcxxabi_static.a", build / "src/Libraries/libcxxabi/libunwind_static.a"]
    abi_command = common_link + [f"-Wl,-force_load,{path}" for path in archive_paths] + [
        "-Wl,-u,__ZNSt3__117bad_function_callD1Ev", "-Wl,-u,__ZTINSt3__117bad_function_callE",
        "-Wl,-u,__ZTVNSt3__117bad_function_callE", str(build / "src/Libraries/libcxxabi/libcxx_static.a"),
        "-Wl,-install_name,/usr/lib/libc++abi.dylib", "-lSystem", str(output / "compiler-rt-install/lib/libcompiler_rt.a"), "-o", str(abi)]
    code = runtime.command(abi_command, output / "logs/objc-abi-link.log")
    result["steps"].append({"name": "libcxxabi-link", "command": abi_command, "exit_code": code})
    if code:
        result["errors"].append("source C++ ABI dylib link failed")
    else:
        # The ABI provider is useful to parallel source-library builds even if
        # the independent Objective-C link is still being repaired.
        shutil.copy2(abi, root / "usr/lib/libc++abi.dylib")
    private = o / "macosx.internal/System/Library/Frameworks/System.framework/Versions/B/PrivateHeaders"
    flags = ["xcrun", "clang++", "-arch", "arm64", "-isysroot", str(sdk), "-x", "objective-c++", "-std=gnu++17",
             "-mmacosx-version-min=11.0", "-fno-objc-arc", "-fexceptions", "-fno-delete-null-pointer-checks", "-fobjc-exceptions", "-fPIC", "-Os",
             "-DNDEBUG", "-DLIBC_NO_LIBCRASHREPORTERCLIENT", "-D__PUREDARWIN__=1", "-Wno-undef-prefix",
             "-D_LIBCPP_ENABLE_CXX17_REMOVED_UNARY_BINARY_FUNCTION",
             "-include", str(REPO / "runtime/compat/sdk27_compile.h"), f"-I{generated}", f"-I{objdir}/include",
             f"-I{o}/runtime", f"-I{private}", f"-I{root}/usr/include"]
    invocations = []
    for name in MM_SOURCES:
        target = objdir / f"{name}.o"
        invocations.append((name, flags + ["-c", str(o / f"runtime/{name}.mm"), "-o", str(target)], target))
    for name in ASM_SOURCES:
        target = objdir / f"{Path(name).name}.o"
        invocations.append((Path(name).name, ["xcrun", "clang", "-arch", "arm64", "-mmacosx-version-min=11.0", "-isysroot", str(sdk), f"-I{o}/runtime", "-c", str(o / f"runtime/{name}.s"), "-o", str(target)], target))
    def compile_one(item):
        name, command, target = item
        return {"name": name, "command": command, "exit_code": runtime.command(command, output / "logs" / f"objc-{name}.log")}
    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        for step in executor.map(compile_one, invocations):
            result["steps"].append(step)
            if step["exit_code"]:
                result["errors"].append(f"Objective-C source compile failed: {step['name']}")
    binary = objdir / "libobjc.A.dylib"
    if not result["errors"]:
        command = common_link + [f"-L{objdir}", "-Wl,-install_name,/usr/lib/libobjc.A.dylib", "-Wl,-init,__objc_init",
                    "-Wl,-sectalign,__DATA,__objc_data,0x1000", f"-Wl,-order_file,{o}/libobjc.order",
                    f"-Wl,-unexported_symbols_list,{o}/unexported_symbols", "-lc++abi", "-lSystem",
                    str(output / "compiler-rt-install/lib/libcompiler_rt.a"), "-o", str(binary)] + [str(item[2]) for item in invocations]
        code = runtime.command(command, output / "logs/objc-link.log")
        result["steps"].append({"name": "objc-link", "command": command, "exit_code": code})
        if code:
            result["errors"].append("Objective-C dylib link failed")
    if not result["errors"]:
        provenance_path = output / "provenance.json"
        provenance = json.loads(provenance_path.read_text())
        amendments = [{"path": str(path.relative_to(REPO)), "sha256": runtime.digest(path)} for path in
                      (REPO / "runtime/compat/sdk27_compile.h", REPO / "runtime/compat/sdk27-dyld.patch", REPO / "scripts/build/objc.py")]
        for artifact in (abi, binary):
            install_path = "/usr/lib/" + artifact.name
            destination = root / install_path.lstrip("/")
            shutil.copy2(artifact, destination)
            entry = {"path": install_path, "sha256": runtime.digest(destination), "source": "puredarwin-vm-next", "revision": pin, "build_amendments": amendments}
            provenance["artifacts"] = [record for record in provenance["artifacts"] if record["path"] != install_path] + [entry]
            result["artifacts"].append(entry)
        (root / "usr/include/objc").mkdir(parents=True, exist_ok=True)
        shutil.copytree(objc_headers, root / "usr/include/objc", dirs_exist_ok=True)
        (root / "usr/lib/libobjc.dylib").unlink(missing_ok=True)
        (root / "usr/lib/libobjc.dylib").symlink_to("libobjc.A.dylib")
        env = os.environ.copy()
        env.update(PUREDARWIN_SDKROOT=str(sdk), MACOSX_DEPLOYMENT_TARGET="11.0")
        bootstrap_hash = runtime.digest(root / "usr/lib/libSystem.B.dylib")
        for name, command in (("system-objc-configure", ["cmake", "-S", str(source), "-B", str(build), f"-DBLAUOS_OBJC_DYLIB={root}/usr/lib/libobjc.A.dylib"]),
                              ("system-objc-link", ["cmake", "--build", str(build), "--target", "libSystem_B_stub", "--parallel", str(args.jobs)])):
            code = runtime.command(command, output / "logs" / (name + ".log"), env)
            result["steps"].append({"name": name, "command": command, "exit_code": code})
            if code:
                result["errors"].append(f"strong source ObjC runtime dependency finalization failed: {name}")
                break
        if not result["errors"]:
            system = root / "usr/lib/libSystem.B.dylib"
            shutil.copy2(build / "src/Libraries/libSystem/stub/libSystem.B.dylib", system)
            # The final configure also rebuilds libdyld with the final common
            # target settings. Stage that exact linked input with libSystem.
            libdyld = root / "usr/lib/system/libdyld.dylib"
            shutil.copy2(build / "src/Libraries/libSystem/libdyld/libdyld.dylib", libdyld)
            for entry in provenance["artifacts"]:
                if entry["path"] == "/usr/lib/system/libdyld.dylib":
                    entry["sha256"] = runtime.digest(libdyld)
                    entry["build_amendments"] = list({record["path"]: record for record in entry.get("build_amendments", []) + amendments}.values())
                if entry["path"] == "/usr/lib/libSystem.B.dylib":
                    entry["sha256"] = runtime.digest(system)
                    entry["build_amendments"] = list({record["path"]: record for record in entry.get("build_amendments", []) + amendments}.values())
                    entry["bootstrap_link_input_sha256"] = bootstrap_hash
                    entry["required_runtime_provider"] = "/usr/lib/libobjc.A.dylib"
            result["runtime_provider_dependency"] = runtime.macho(system)["dependencies"]
        runtime.write_result(provenance_path, provenance)
        if not result["errors"]:
            result["status"] = "source_build_pass"
    result["execution_verified"] = False
    runtime.write_result(args.output, result)
    print(json.dumps({"status": result["status"], "errors": result["errors"]}, indent=2))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
