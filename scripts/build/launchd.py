#!/usr/bin/env python3
"""Link real pinned launchd/launchctl and prepare guest-only functional tests."""
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources-dir", type=Path, default=REPO / ".blauos/sources")
    parser.add_argument("--output-dir", type=Path, default=REPO / ".blauos/build/runtime")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--corefoundation-prefix", type=Path)
    parser.add_argument("--iokit-prefix", type=Path)
    parser.add_argument("--sdk", type=Path)
    parser.add_argument("--output", type=Path, default=REPO / "evidence/runtime/launchd-build.json")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    root = (args.root or output / "root").resolve()
    source = output / "source-overlay"
    build = output / "puredarwin-overlay"
    cf = (args.corefoundation_prefix or root).resolve()
    iokit = (args.iokit_prefix or root).resolve()
    sdk = args.sdk or Path(subprocess.check_output(["xcrun", "--show-sdk-path"], text=True).strip())
    pin = next(s["revision"] for s in json.loads((REPO / "sources.lock.json").read_text())["sources"] if s["name"] == "puredarwin-vm-next")
    result = {"schema_version": 1, "status": "blocked", "source_revision": pin,
              "implementation": "real PureDarwin next launchd/launchctl", "steps": [], "errors": [],
              "guest_functional_tests_verified": False, "shutdown_verified": False}
    if not (source / "src/Libraries/XPC/launchd/pd_launchd_main.c").is_file():
        result["errors"].append("run runtime.py build to prepare the pinned runtime overlay first")
        runtime.write_result(args.output, result)
        return 1
    revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if revision != pin:
        raise ValueError("launchd source does not match lock")
    libs = build / "src/Libraries"
    archives = {name: libs / "XPC" / directory / f"lib{name}.a" for name, directory in
                (("XPC_launchd_static", "launchd"), ("XPC_launchd_mig_static", "launchd"),
                 ("XPC_libxpc_static", "libxpc"), ("XPC_libinfo_static", "libinfo"), ("XPC_libnv_static", "libnv"))}
    archives["CrashReporterClient"] = libs / "CrashReporterClient/libCrashReporterClient.a"
    for name, artifact in archives.items():
        if not artifact.is_file():
            result["errors"].append(f"source archive missing: {name}: {artifact}")
    for artifact in (root / "usr/lib/libSystem.B.dylib", cf / "usr/lib/libCoreFoundation.dylib", iokit / "usr/lib/libIOKitCF.dylib"):
        if not artifact.is_file():
            result["errors"].append(f"source-built link dependency missing: {artifact}")
    if result["errors"]:
        runtime.write_result(args.output, result)
        print(json.dumps({"status": result["status"], "errors": result["errors"]}, indent=2))
        return 1
    common = ["xcrun", "clang", "-arch", "arm64", "-mmacosx-version-min=11.0", "-isysroot", str(sdk), "-nostdlib", "-Wl,-Z", "-DDEPLOYMENT_RUNTIME_OBJC=1",
              "-include", str(REPO / "runtime/compat/sdk27_compile.h"),
              f"-I{source}/src/Libraries/XPC/launchd", f"-I{source}/src/Libraries/XPC/launchd/System-compat",
              f"-I{source}/src/Libraries/XPC/launchd/SystemStarter", f"-I{source}/src/Libraries/XPC/libxpc/include",
              f"-I{source}/src/Libraries/XPC/libxpc", f"-I{source}/src/Libraries/XPC/libinfo",
              f"-I{source}/src/Libraries/libSystem/libc", f"-I{source}/src/Libraries/libSystem/libplatform/private",
              f"-I{source}/src/Libraries/libSystem/pthread/compat-include", f"-I{build}/src/Libraries/XPC/launchd",
              f"-I{cf}/include", f"-I{cf}/usr/include", f"-I{iokit}/include", f"-I{iokit}/usr/include",
              f"-F{cf}/System/Library/Frameworks", f"-L{root}/usr/lib", f"-L{cf}/usr/lib", f"-L{iokit}/usr/lib",
              f"-Wl,-dylib_file,/usr/lib/system/libdyld.dylib:{root}/usr/lib/system/libdyld.dylib",
              "-Wl,-dylinker_install_name,/usr/lib/dyld", "-Wl,-platform_version,macos,11.0,11.5", "-Wl,-fixup_chains",
              "-Wl,-U,_OBJC_CLASS_$_NSObject", "-Wl,-U,_OBJC_METACLASS_$_NSObject", "-Wl,-U,__objc_empty_cache"]
    linked = []
    for name, source_path in (("launchd", "src/Libraries/XPC/launchd/pd_launchd_main.c"),
                              ("launchctl", "src/Libraries/XPC/launchctl/launchctl.c")):
        binary = output / "launchd" / name
        binary.parent.mkdir(parents=True, exist_ok=True)
        selected = [value for key, value in archives.items() if key != "XPC_launchd_static" or name == "launchd"]
        # PID 1 owns the MIG server implementations. launchctl uses only client
        # object members; forcing server members into the client is invalid.
        command = common + ([f"-Wl,-force_load,{archive}" for archive in selected]
                            if name == "launchd" else [str(archive) for archive in selected])
        if name == "launchctl":
            command += ["-DHAVE_LIBAUDITD=0", "-DSO_EXECPATH=0x1085"]
        command += [str(source / source_path), "-lCoreFoundation", "-lIOKitCF", "-lSystem",
                    str(output / "compiler-rt-install/lib/libcompiler_rt.a"), "-o", str(binary)]
        log = output / "logs" / f"{name}-link.log"
        code = runtime.command(command, log)
        result["steps"].append({"name": name, "command": command, "exit_code": code, "log": str(log)})
        if code:
            result["errors"].append(f"{name} link failed; see {log}")
        else:
            linked.append((name, binary))
    provenance_path = output / "provenance.json"
    provenance = json.loads(provenance_path.read_text())
    for name, binary in linked:
        path = "/sbin/launchd" if name == "launchd" else "/usr/bin/launchctl"
        destination = root / path.lstrip("/")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(binary, destination)
        provenance["artifacts"] = [entry for entry in provenance["artifacts"] if entry["path"] != path]
        provenance["artifacts"].append({"path": path, "sha256": runtime.digest(destination),
                                       "source": "puredarwin-vm-next", "revision": pin,
                                       "build_amendments": [{"path": value, "sha256": runtime.digest(REPO / value)} for value in
                                                            ("scripts/build/launchd.py", "runtime/compat/sdk27-dyld.patch", "runtime/compat/sdk27_compile.h")]})
    if any(name == "launchctl" for name, _ in linked):
        # The genuine PID 1 bootstrap sequence executes this historical path.
        # Keep the link guest-relative so staged-root validation can resolve it.
        alias = root / "bin/launchctl"
        alias.parent.mkdir(parents=True, exist_ok=True)
        alias.unlink(missing_ok=True)
        alias.symlink_to("../usr/bin/launchctl")
        result["bootstrap_launchctl_alias"] = "/bin/launchctl -> ../usr/bin/launchctl"
    runtime.write_result(provenance_path, provenance)
    result["unmet_acceptance"] = ["real guest job/bootstrap/supervision/error/shutdown tests remain unverified"]
    if not result["errors"]:
        result["status"] = "source_build_pass"
    runtime.write_result(args.output, result)
    print(json.dumps({"status": result["status"], "errors": result["errors"]}, indent=2))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
