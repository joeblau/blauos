#!/usr/bin/env python3
"""Build and link real source IOKitCF needed by the launchd runtime."""
from __future__ import annotations
import argparse
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources-dir", type=Path, default=REPO / ".blauos/sources")
    parser.add_argument("--output-dir", type=Path, default=REPO / ".blauos/build/runtime")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--sdk", type=Path)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--output", type=Path, default=REPO / "evidence/runtime/iokit-build.json")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    source, build = output / "source-overlay", output / "puredarwin-overlay"
    root, prefix = (args.root or output / "root").resolve(), output / "iokitcf-prefix"
    sdk = args.sdk or Path(subprocess.check_output(["xcrun", "--show-sdk-path"], text=True).strip())
    revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    lock = json.loads((REPO / "sources.lock.json").read_text())
    pin = next(entry["revision"] for entry in lock["sources"] if entry["name"] == "puredarwin-vm-next")
    if revision != pin:
        raise ValueError("IOKit source differs from lock")
    headers = prefix / "include/CoreFoundation"
    headers.mkdir(parents=True, exist_ok=True)
    # Use the C/ObjC framework header output. A recursive source copy also finds
    # Base.subproj/SwiftRuntime/CoreFoundation.h and silently selects Swift ABI.
    framework_headers = root / "usr/include/CoreFoundation"
    if not (framework_headers / "CoreFoundation.h").is_file():
        raise ValueError("run corefoundation.py first to stage the source C/ObjC headers")
    shutil.copytree(framework_headers, headers, dirs_exist_ok=True)
    header_inputs = {str(path.relative_to(headers)): runtime.digest(path)
                     for path in sorted(headers.rglob("*.h"))}
    header_state = prefix / "header-inputs.json"
    if not header_state.is_file() or json.loads(header_state.read_text()) != header_inputs:
        # Framework installs retain source timestamps. Changed content must
        # invalidate old objects even when the original header timestamp is old.
        for path in headers.rglob("*.h"):
            os.utime(path, None)
        runtime.write_result(header_state, header_inputs)
    env = os.environ.copy()
    env.update(PUREDARWIN_SDKROOT=str(sdk), MACOSX_DEPLOYMENT_TARGET="11.0")
    result = {"schema_version": 1, "status": "blocked", "steps": [], "errors": [], "execution_verified": False}
    configure = ["cmake", "-S", str(source), "-B", str(build), "-DPUREDARWIN_ENABLE_IOKITCF=ON", f"-DPUREDARWIN_COREFOUNDATION_PREFIX={prefix}"]
    for name, command in (("configure", configure), ("archive", ["cmake", "--build", str(build), "--target", "IOKitCF", "--parallel", str(args.jobs)])):
        log = output / "logs" / f"iokit-{name}.log"
        code = runtime.command(command, log, env)
        result["steps"].append({"name": name, "command": command, "exit_code": code, "log": str(log)})
        if code:
            result["errors"].append(f"IOKit {name} failed")
            runtime.write_result(args.output, result)
            print(json.dumps({"status": result["status"], "errors": result["errors"]}, indent=2))
            return 1
    archive = build / "src/Libraries/IOKit/libIOKitCF.a"
    result["archive_sha256"] = runtime.digest(archive)
    cf = root / "usr/lib/libCoreFoundation.dylib"
    if not cf.is_file():
        result["errors"].append("real source-built CoreFoundation dylib is required for IOKitCF link")
    else:
        binary = output / "iokitcf-prefix/libIOKitCF.dylib"
        command = ["xcrun", "clang", "-arch", "arm64", "-mmacosx-version-min=11.0", "-isysroot", str(sdk), "-dynamiclib", "-nostdlib", "-Wl,-Z",
                   f"-L{root}/usr/lib", f"-Wl,-dylib_file,/usr/lib/system/libdyld.dylib:{root}/usr/lib/system/libdyld.dylib",
                   "-Wl,-install_name,/usr/lib/libIOKitCF.dylib", "-Wl,-platform_version,macos,11.0,11.5", "-Wl,-fixup_chains",
                   f"-Wl,-force_load,{archive}", "-lCoreFoundation", "-lSystem", "-o", str(binary)]
        log = output / "logs/iokit-link.log"
        code = runtime.command(command, log, env)
        result["steps"].append({"name": "link", "command": command, "exit_code": code, "log": str(log)})
        if code:
            result["errors"].append("real IOKitCF dylib link failed")
        else:
            destination = root / "usr/lib/libIOKitCF.dylib"
            shutil.copy2(binary, destination)
            source_headers = root / "usr/include/IOKit"
            source_headers.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source / "src/Libraries/IOKit/iokituser/include/IOKit", source_headers, dirs_exist_ok=True)
            entry = {"path": "/usr/lib/libIOKitCF.dylib", "sha256": runtime.digest(destination), "source": "puredarwin-vm-next", "revision": revision,
                     "build_amendments": [{"path": path, "sha256": runtime.digest(REPO / path)} for path in
                                          ("scripts/build/iokit.py", "runtime/compat/sdk27-dyld.patch", "runtime/compat/sdk27_compile.h")]}
            provenance_path = output / "provenance.json"
            provenance = json.loads(provenance_path.read_text())
            provenance["artifacts"] = [record for record in provenance["artifacts"] if record["path"] != entry["path"]] + [entry]
            runtime.write_result(provenance_path, provenance)
            result["artifacts"] = [entry]
            result["status"] = "source_build_pass"
    runtime.write_result(args.output, result)
    print(json.dumps({"status": result["status"], "errors": result["errors"]}, indent=2))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
