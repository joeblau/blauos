#!/usr/bin/env python3
"""Build and stage the pinned POSIX mDNSResponder daemon for genuine DNS."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import plistlib
import shutil
import subprocess

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("blauos_runtime", REPO / "scripts/build/runtime.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)

SOURCES = (
    "mDNSPosix/PosixDaemon.c", "mDNSPosix/mDNSPosix.c", "mDNSPosix/mDNSUNP.c",
    "mDNSPosix/posix_utilities.c", "mDNSCore/mDNS.c", "mDNSCore/DNSDigest.c",
    "mDNSCore/uDNS.c", "mDNSCore/DNSCommon.c", "mDNSShared/uds_daemon.c",
    "mDNSShared/mDNSDebug.c", "mDNSShared/dnssd_ipc.c", "mDNSShared/GenLinkedList.c",
    "mDNSShared/PlatformCommon.c", "mDNSShared/ClientRequests.c",
    "mDNSShared/dnssd_clientshim.c", "DSO/dso.c", "DSO/dso-transport.c",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=REPO / ".blauos/build/runtime")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--sdk", type=Path)
    parser.add_argument("--manifest", type=Path, default=REPO / "sources.lock.json")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--output", type=Path, default=REPO / "evidence/runtime/mdns/build.json")
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    output = args.output_dir.resolve()
    root = (args.root or output / "root").resolve()
    source = output / "source-overlay"
    tree = source / "src/Libraries/mDNSResponder"
    pin = next(s["revision"] for s in json.loads(args.manifest.read_text())["sources"]
               if s["name"] == "puredarwin-vm-next")
    result = {"schema_version": 1, "status": "blocked", "source_revision": pin,
              "implementation": "pinned POSIX mDNSResponder and real unicast DNS",
              "steps": [], "errors": [], "guest_dns_verified": False}
    if not tree.is_dir():
        result["errors"].append("run runtime.py build to prepare the pinned source overlay")
        runtime.write_result(args.output, result)
        return 1
    if subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip() != pin:
        raise ValueError("mDNSResponder source does not match the lock")
    # Include headers and the launch job in the clean-source check.
    paths = ("src/Libraries/mDNSResponder", "src/Userspace/mdnsd")
    changed = subprocess.check_output(["git", "-C", str(source), "diff", "--name-only", pin,
                                       "--", *paths], text=True).strip()
    untracked = subprocess.check_output(["git", "-C", str(source), "ls-files", "--others",
                                         "--exclude-standard", "--", *paths], text=True).strip()
    if changed or untracked:
        raise ValueError(f"unrecorded mDNSResponder source amendments: {changed or untracked}")
    sdk = (args.sdk or Path(subprocess.check_output(["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True).strip())).resolve()
    clang = subprocess.check_output(["xcrun", "--sdk", "macosx", "--find", "clang"], text=True).strip()
    binary = output / "mdns/mdnsd"
    objects = output / "mdns/objects"
    logs = output / "logs/mdns"
    objects.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    system = root / "usr/lib/libSystem.B.dylib"
    libdyld = root / "usr/lib/system/libdyld.dylib"
    if not system.is_file() or not libdyld.is_file():
        result["errors"].append("source-built libSystem and libdyld are required")
        runtime.write_result(args.output, result)
        return 1
    dependencies = {str(path): runtime.digest(path) for path in (system, libdyld)}
    result["link_dependencies"] = [{"path": path, "sha256": sha} for path, sha in dependencies.items()]
    common = [clang, "-arch", "arm64", "-mmacosx-version-min=11.0", "-isysroot", str(sdk),
              "-DPOSIX_BUILD", "-fno-builtin", "-fblocks", "-g", "-O2",
              "-Wno-nullability-completeness", "-include", str(REPO / "runtime/compat/sdk27_compile.h")]
    common += [f"-I{tree / directory}" for directory in ("mDNSCore", "mDNSShared", "mDNSPosix", "DSO")]
    result["compiler"] = clang
    result["sdk"] = str(sdk)
    result["sdk_settings_sha256"] = runtime.digest(sdk / "SDKSettings.json")
    result["source_inputs"] = [{"path": f"src/Libraries/mDNSResponder/{value}",
                                "sha256": runtime.digest(tree / value)} for value in SOURCES]

    def compile_one(relative: str) -> tuple[dict, Path]:
        name = relative.replace("/", "_")
        obj = objects / (name + ".o")
        log = logs / (name + ".log")
        command = common + ["-c", str(tree / relative), "-o", str(obj)]
        code = runtime.command(command, log)
        return {"name": relative, "command": command, "exit_code": code, "log": str(log)}, obj

    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        compiled = list(executor.map(compile_one, SOURCES))
    for step, _ in compiled:
        result["steps"].append(step)
        if step["exit_code"]:
            result["errors"].append(f"{step['name']} failed; see {step['log']}")
    if not result["errors"]:
        command = [clang, "-arch", "arm64", "-mmacosx-version-min=11.0", "-isysroot", str(sdk),
                   "-nostdlib", "-Wl,-Z", "-Wl,-dylinker_install_name,/usr/lib/dyld",
                   "-Wl,-platform_version,macos,11.0,11.5", "-Wl,-fixup_chains",
                   f"-Wl,-dylib_file,/usr/lib/system/libdyld.dylib:{libdyld}",
                   *map(str, (obj for _, obj in compiled)), str(system), "-o", str(binary)]
        code = runtime.command(command, logs / "link.log")
        result["steps"].append({"name": "mdnsd-link", "command": command,
                                "exit_code": code, "log": str(logs / "link.log")})
        if code:
            result["errors"].append("mdnsd link failed; see link.log")
    if not result["errors"]:
        if any(runtime.digest(Path(path)) != sha for path, sha in dependencies.items()):
            raise ValueError("source runtime changed during the mdnsd build; replay with stable inputs")
        metadata = runtime.macho(binary)
        if metadata["architecture"] != "arm64" or metadata["dylinker"] != "/usr/lib/dyld" or \
                {item["path"] for item in metadata["dependencies"]} != {"/usr/lib/libSystem.B.dylib"}:
            raise ValueError("mdnsd links outside the source-built target runtime")
        plist = source / "src/Userspace/mdnsd/com.apple.mDNSResponder.plist"
        service = plistlib.loads(plist.read_bytes())
        if service["ProgramArguments"] != ["/usr/sbin/mdnsd", "-debug"]:
            raise ValueError("mdnsd must remain in the foreground under launchd")
        paths = {"/usr/sbin/mdnsd": binary,
                 "/System/Library/LaunchDaemons/com.apple.mDNSResponder.plist": plist}
        provenance_path = output / "provenance.json"
        provenance = json.loads(provenance_path.read_text())
        amendments = [{"path": value, "sha256": runtime.digest(REPO / value)} for value in
                      ("scripts/build/mdns.py", "runtime/compat/sdk27_compile.h")]
        result["artifacts"] = []
        for install, built in paths.items():
            destination = root / install.lstrip("/")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(built, destination)
            if install == "/usr/sbin/mdnsd":
                destination.chmod(0o755)
            record = {"path": install, "sha256": runtime.digest(destination),
                      "source": "puredarwin-vm-next", "revision": pin,
                      "build_amendments": amendments, "compiler": clang,
                      "sdk_settings_sha256": result["sdk_settings_sha256"]}
            provenance["artifacts"] = [entry for entry in provenance["artifacts"] if entry["path"] != install]
            provenance["artifacts"].append(record)
            result["artifacts"].append(record)
        for directory in ("var/run", "var/log"):
            (root / directory).mkdir(parents=True, exist_ok=True)
        runtime.write_result(provenance_path, provenance)
        result["macho"] = metadata
        result["status"] = "source_build_pass"
    result["unmet_acceptance"] = ["real guest daemon startup and DNS response remain unverified"]
    runtime.write_result(args.output, result)
    print(json.dumps({"status": result["status"], "errors": result["errors"]}, indent=2))
    return int(bool(result["errors"]))


if __name__ == "__main__":
    raise SystemExit(main())
