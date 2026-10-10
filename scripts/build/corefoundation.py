#!/usr/bin/env python3
"""Build real ICU and CoreFoundation libraries for the ARM64 guest."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tarfile

REPO = Path(__file__).resolve().parents[2]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=REPO / ".blauos/build/runtime")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--sdk", type=Path)
    parser.add_argument("--archive", type=Path, default=REPO / ".blauos/inputs/icu4c-76_1-src.tgz")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--action", choices=["icu-host", "icu-target", "corefoundation", "all"], default="all")
    parser.add_argument("--evidence", type=Path, default=REPO / "evidence/runtime/corefoundation")
    args = parser.parse_args()
    if args.jobs < 1:
        raise ValueError("--jobs must be positive")
    out = args.output_dir.resolve()
    root = (args.root or out / "root").resolve()
    evidence = args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    sdk = args.sdk or Path(subprocess.check_output(["xcrun", "--show-sdk-path"], text=True).strip())
    lock = json.loads((REPO / "sources.lock.json").read_text())
    pin = next(s["revision"] for s in lock["sources"] if s["name"] == "puredarwin-vm-next")
    archive_pin = next(s for s in lock["archives"] if s["name"] == "icu4c-76.1")
    if digest(args.archive) != archive_pin["sha256"]:
        raise ValueError("ICU archive differs from the pinned input")
    source = out / "source-overlay"
    if subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip() != pin:
        raise ValueError("Runtime source revision differs from source lock")
    report = {"status": "blocked", "source_revision": pin, "icu_archive_sha256": digest(args.archive),
              "sdk": str(sdk), "action": args.action, "steps": [], "artifacts": [], "execution_verified": False}
    def run(argv, cwd, name, environment=None):
        raw = out / "logs" / (name + ".log")
        raw.parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        if environment:
            env.update(environment)
        with raw.open("w") as stream:
            process = subprocess.run(argv, cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT)
        lines = raw.read_text(errors="replace").splitlines()
        errors = [line for line in lines if "error:" in line or "Undefined symbols" in line or "No such file" in line]
        (evidence / (name + ".log")).write_text("\n".join(lines[:12] + errors[:80] + lines[-35:]) + "\n")
        report["steps"].append({"name": name, "argv": argv, "cwd": str(cwd), "env": environment or {},
                                "exit_code": process.returncode, "raw_log_sha256": digest(raw)})
        (evidence / (args.action + "-result.json")).write_text(json.dumps(report, indent=2) + "\n")
        if process.returncode:
            raise RuntimeError(f"{name} failed; see {evidence / (name + '.log')}")
    def stage(path, install, external=False):
        destination = root / install.lstrip("/")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        record = {"path": install, "sha256": digest(destination),
                  "source": "icu4c-76.1" if external else "puredarwin-vm-next",
                  "revision": archive_pin.get("revision", archive_pin["sha256"]) if external else pin,
                  "build_amendments": [{"path": "scripts/build/corefoundation.py", "sha256": digest(Path(__file__))}]}
        if external:
            record["archive_sha256"] = archive_pin["sha256"]
        else:
            record["build_amendments"].append({"path": "runtime/compat/sdk27-corefoundation.patch",
                                               "sha256": digest(REPO / "runtime/compat/sdk27-corefoundation.patch")})
        provenance_path = out / "provenance.json"
        provenance = json.loads(provenance_path.read_text())
        provenance["artifacts"] = [r for r in provenance["artifacts"] if r["path"] != install] + [record]
        provenance_path.write_text(json.dumps(provenance, indent=2) + "\n")
        report["artifacts"].append(record)
    def require(*names):
        for name in names:
            if not (root / "usr/lib" / name).is_file():
                raise RuntimeError(f"Missing source-built target library {name}")
    common_link = ["xcrun", "clang", "-arch", "arm64", "-isysroot", str(sdk), "-dynamiclib", "-nostdlib", "-Wl,-Z",
                   f"-L{root}/usr/lib", f"-Wl,-dylib_file,/usr/lib/system/libdyld.dylib:{root}/usr/lib/system/libdyld.dylib",
                   "-Wl,-platform_version,macos,11.0,11.5", "-Wl,-fixup_chains", "-Wl,-not_for_dyld_shared_cache"]
    icu_source = out / "icu-source/icu"
    if not icu_source.exists():
        icu_source.parent.mkdir(parents=True)
        with tarfile.open(args.archive) as stream:
            stream.extractall(icu_source.parent, filter="data")
    if args.action in ("icu-host", "all"):
        native = out / "icu-native"
        native.mkdir(parents=True, exist_ok=True)
        run([str(icu_source / "source/configure"), "--disable-tests", "--disable-samples", "--disable-extras", "--disable-icuio"],
            native, "icu-native-configure")
        run(["make", f"-j{args.jobs}"], native, "icu-native-build")
    if args.action in ("icu-target", "all"):
        require("libSystem.B.dylib", "libc++abi.dylib", "libc++.dylib")
        target = out / "icu-target"
        target.mkdir(parents=True, exist_ok=True)
        flags = f"-arch arm64 -isysroot {sdk} -mmacosx-version-min=11.0 -include {REPO}/runtime/compat/sdk27_compile.h"
        cxx = f"{flags} -nostdinc++ -I{source}/src/Libraries/libcxxabi/config -I{source}/src/Libraries/libcxx/include"
        link = " ".join(common_link[common_link.index("-nostdlib"):]) + " -lSystem -lc++abi -lc++"
        environment = {"CC": "clang", "CXX": "clang++", "AR": "ar", "RANLIB": "ranlib",
                       "CFLAGS": flags, "CXXFLAGS": cxx, "LDFLAGS": link}
        run([str(icu_source / "source/configure"), "--host=arm64-apple-darwin20.4", f"--with-cross-build={out}/icu-native",
             "--disable-renaming", "--disable-tests", "--disable-samples", "--disable-extras", "--disable-icuio",
             "--disable-tools", "--with-data-packaging=library"], target, "icu-target-configure", environment)
        # Darwin's configure probes select SDK pthread/m stubs. The complete
        # source libSystem supplies those APIs; forbid all SDK library search.
        run(["make", "clean"], target, "icu-target-clean", environment)
        run(["make", f"-j{args.jobs}", "DEFAULT_LIBS=-lSystem"], target, "icu-target-build", environment)
        # --disable-tools omits data from top-level SUBDIRS. Cross-build's native
        # icupkg/pkgdata still generate the real archived Unicode data library.
        run(["make", "-C", "data", f"-j{args.jobs}", "DEFAULT_LIBS=-lSystem"], target, "icu-target-data", environment)
        for path in sorted((target / "lib").glob("*.dylib")):
            if path.is_symlink():
                continue
            run(["xcrun", "install_name_tool", "-id", "/usr/lib/" + path.name, str(path)], target, "icu-id-" + path.stem)
            for dep in ("libicuuc.76.dylib", "libicudata.76.dylib", "libicui18n.76.dylib"):
                run(["xcrun", "install_name_tool", "-change", dep, "/usr/lib/" + dep, str(path)], target, "icu-dep-" + path.stem + "-" + dep)
            stage(path, "/usr/lib/" + path.name, external=True)
        for name in ("icuuc", "icudata", "icui18n"):
            for suffix in ("", ".76"):
                linkpath = root / "usr/lib" / ("lib" + name + suffix + ".dylib")
                linkpath.unlink(missing_ok=True)
                linkpath.symlink_to("lib" + name + ".76.1.dylib")
        wrapper_source = target / "reexport.c"
        wrapper_source.write_text("/* Re-export shell; all ICU implementations are in source-built libraries. */\n")
        obj = target / "reexport.o"
        run(["xcrun", "clang", "-arch", "arm64", "-isysroot", str(sdk), "-mmacosx-version-min=11.0", "-c", str(wrapper_source), "-o", str(obj)], target, "icu-reexport-compile")
        wrapper = target / "libicucore.A.dylib"
        run(common_link + ["-Wl,-install_name,/usr/lib/libicucore.A.dylib", "-lSystem", str(obj)] +
            [f"-Wl,-reexport_library,{root}/usr/lib/lib{name}.76.1.dylib" for name in ("icuuc", "icudata", "icui18n")] +
            ["-o", str(wrapper)], target, "icu-reexport-link")
        stage(wrapper, "/usr/lib/libicucore.A.dylib", external=True)
        linkpath = root / "usr/lib/libicucore.dylib"
        linkpath.unlink(missing_ok=True)
        linkpath.symlink_to("libicucore.A.dylib")
        headers = root / "usr/include/unicode"
        for folder in ("common", "i18n"):
            shutil.copytree(icu_source / "source" / folder / "unicode", headers, dirs_exist_ok=True)
        config = headers / "uconfig.h"
        config.write_text(config.read_text().replace("#define U_DISABLE_RENAMING 0", "#define U_DISABLE_RENAMING 1"))
    if args.action in ("corefoundation", "all"):
        require("libSystem.B.dylib", "libobjc.A.dylib", "libicucore.A.dylib")
        cf_source = out / "corefoundation-source"
        shutil.copytree(source / "src/Libraries/CoreFoundation", cf_source, dirs_exist_ok=True)
        patch = REPO / "runtime/compat/sdk27-corefoundation.patch"
        # Strip the monorepo prefix when applying to the isolated CF subtree.
        run(["patch", "-p4", "-i", str(patch)], cf_source, "cf-source-patch")
        support = out / "corefoundation-support"
        foundation = support / "Foundation"
        foundation.mkdir(parents=True, exist_ok=True)
        for header in (source / "src/Libraries/Foundation").rglob("*.h"):
            shutil.copy2(header, foundation / header.name)
        support_lib = support / "lib"
        support_lib.mkdir(parents=True, exist_ok=True)
        # Both implementations are genuinely exported by source libSystem.
        # These build-time aliases satisfy CF's upstream library names.
        for name in ("libBlocksRuntime.dylib", "libdispatch.dylib"):
            path = support_lib / name
            path.unlink(missing_ok=True)
            path.symlink_to(root / "usr/lib/libSystem.B.dylib")
        flags = f"-include {REPO}/runtime/compat/sdk27_compile.h -I{root}/usr/include -I{out}/objc/include -I{source}/src/Libraries/libSystem/libc/pd-compat-include -I{support} -DU_DISABLE_RENAMING=1 -DDEPLOYMENT_RUNTIME_OBJC=1 -DINCLUDE_OBJC=1 -U_FORTIFY_SOURCE -D_FORTIFY_SOURCE=0"
        link = " ".join(common_link[common_link.index("-nostdlib"):]) + f" -L{support_lib} -lSystem -lobjc"
        cf_build = out / "corefoundation"
        run(["cmake", "-S", str(cf_source), "-B", str(cf_build), "-G", "Unix Makefiles",
             "-DCMAKE_POLICY_VERSION_MINIMUM=3.5", "-DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY",
             "-DCMAKE_OSX_ARCHITECTURES=arm64", f"-DCMAKE_OSX_SYSROOT={sdk}", "-DCMAKE_OSX_DEPLOYMENT_TARGET=11.0",
             "-DBUILD_SHARED_LIBS=ON", f"-DCMAKE_C_FLAGS={flags}", f"-DCMAKE_OBJC_FLAGS={flags} -fno-objc-arc",
             f"-DCMAKE_SHARED_LINKER_FLAGS={link}", "-DCMAKE_INSTALL_NAME_DIR=/usr/lib", "-DCMAKE_BUILD_WITH_INSTALL_NAME_DIR=ON"],
            REPO, "cf-configure")
        run(["cmake", "--build", str(cf_build), "--parallel", str(args.jobs)], REPO, "cf-build")
        stage(cf_build / "CoreFoundation.framework/libCoreFoundation.dylib", "/usr/lib/libCoreFoundation.dylib")
        for name in ("Headers", "PrivateHeaders"):
            shutil.copytree(cf_build / "CoreFoundation.framework" / name, root / "usr/include/CoreFoundation", dirs_exist_ok=True)
    report["status"] = "source_build_pass"
    (evidence / (args.action + "-result.json")).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "artifacts": [a["path"] for a in report["artifacts"]]}, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Runtime library build blocked: {error}")
        raise SystemExit(1)
