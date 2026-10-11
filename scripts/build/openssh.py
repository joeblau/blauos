#!/usr/bin/env python3
"""Cross-build pinned OpenSSH portable against BlauOS's source-built Darwin runtime."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
PATCHES = ("vm/patches/openssh-blauos-cross-rlimit.patch", "vm/patches/openssh-darwin-source-runtime.patch")
INSTALL = {
    "sshd": "usr/sbin/sshd",
    "sshd-auth": "usr/libexec/sshd-auth",
    "sshd-session": "usr/libexec/sshd-session",
    "sftp-server": "usr/libexec/sftp-server",
    "ssh-keygen": "usr/bin/ssh-keygen",
    "ssh": "usr/bin/ssh",
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checked_dependencies(otool_output):
    """Reject target linkage to host frameworks or untracked dylibs."""
    linked = [line.strip().split(" (", 1)[0] for line in otool_output.splitlines()[1:] if line.strip()]
    allowed = ("/usr/lib/libSystem.B.dylib", "/usr/lib/system/libdyld.dylib")
    if not linked or any(path not in allowed for path in linked):
        raise ValueError(f"Unexpected target dylib dependency: {linked}")
    return linked


def build(args):
    if sys.platform != "darwin":
        raise ValueError("This ARM64 native compiler profile requires a macOS build host")
    if args.output.exists():
        raise ValueError("Use a fresh output directory to preserve prior build evidence")
    lock = json.loads((ROOT / "sources.lock.json").read_text())
    pin = next((entry["revision"] for entry in lock["sources"] if entry["name"] == "openssh-portable"), None)
    if not pin:
        raise ValueError("Pin openssh-portable in sources.lock.json before building")
    revision = subprocess.check_output(["git", "-C", str(args.source), "rev-parse", "HEAD"], text=True).strip()
    if revision != pin or subprocess.check_output(["git", "-C", str(args.source), "status", "--porcelain"]):
        raise ValueError("OpenSSH source must be pristine and match the source lock")
    for relative in ("usr/lib/libSystem.B.dylib", "usr/lib/system/libdyld.dylib"):
        if not (args.runtime_root / relative).is_file():
            raise ValueError(f"Missing source-built runtime link input: {relative}")
    sdk = subprocess.check_output(["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True).strip()
    clang = subprocess.check_output(["xcrun", "--find", "clang"], text=True).strip()
    args.output.mkdir(parents=True)
    copied = args.output / "source"
    shutil.copytree(args.source, copied, ignore=shutil.ignore_patterns(".git"), symlinks=True)
    subprocess.run(["git", "init", "-q", str(copied)], check=True)
    for patch in PATCHES:
        subprocess.run(["git", "-C", str(copied), "apply", "--check", str(ROOT / patch)], check=True)
        subprocess.run(["git", "-C", str(copied), "apply", str(ROOT / patch)], check=True)
    env = dict(os.environ)
    # Configure cannot execute this independent target on the macOS host.
    # Native ABI sizes are detected with compile checks; guest probes must verify
    # the rlimit/select assumption before SSH acceptance is claimed.
    values = {
        "CC": clang,
        "CPPFLAGS": "",
        "CFLAGS": shlex.join(["-O2", "-arch", "arm64", "-isysroot", sdk,
                              "-mmacosx-version-min=11.0", "-fno-builtin", "-fno-stack-protector",
                              "-include", str(ROOT / "runtime/compat/sdk27_compile.h")]),
        "LDFLAGS": shlex.join(["-Z", "-nostdlib", "-L" + str(args.runtime_root / "usr/lib"),
                               "-Wl,-dylib_file,/usr/lib/system/libdyld.dylib:" + str(args.runtime_root / "usr/lib/system/libdyld.dylib"),
                               "-Wl,-platform_version,macos,11.0,11.5", "-Wl,-fixup_chains",
                               "-Wl,-dylinker_install_name,/usr/lib/dyld"]),
        "LIBS": "-lSystem",
        "BLAUOS_CROSS_RLIMIT_SELECT": "yes",
        # Use upstream's source-built ChaCha RNG and real getentropy/device
        # seeding; the older Darwin libc arc4random implementation is unused.
        "ac_cv_func_arc4random": "no",
        "ac_cv_func_arc4random_buf": "no",
        "ac_cv_func_arc4random_uniform": "no",
    }
    env.update(values)
    command = ["sh", str(copied / "configure"), "--host=aarch64-apple-darwin20",
               "--build=aarch64-apple-darwin" + platform.release().split(".")[0],
               "--without-openssl", "--without-zlib", "--without-pam", "--without-audit",
               "--without-libedit", "--disable-pkcs11", "--disable-security-key",
               "--with-sandbox=rlimit", "--prefix=/usr", "--sysconfdir=/etc/ssh",
               "--with-privsep-user=sshd", "--with-privsep-path=/var/empty", "--disable-strip"]
    amendments = [{"path": name, "sha256": digest(ROOT / name)} for name in
                  ("scripts/build/openssh.py", "runtime/compat/sdk27_compile.h", *PATCHES)]
    metadata = {"schema_version": 1, "source": "openssh-portable", "revision": revision,
                "source_lock_sha256": digest(ROOT / "sources.lock.json"),
                "configure_command": command, "environment": values, "build_amendments": amendments,
                "upstream_configure_sha256": digest(args.source / "configure"),
                "configure_sha256": digest(copied / "configure"),
                "compiler_version": subprocess.check_output([clang, "--version"], text=True).splitlines()[0],
                "compiler_sha256": digest(Path(clang)), "sdk": str(Path(sdk).resolve()),
                "runtime_link_inputs": {name: digest(args.runtime_root / name) for name in
                                        ("usr/lib/libSystem.B.dylib", "usr/lib/system/libdyld.dylib")},
                "stock_runtime_used": False, "target_execute_during_configure": False,
                "configure_runtime_assumptions_verified": False, "guest_execution_verified": False,
                "crypto_profile": "upstream internal crypto; Ed25519, X25519/hybrid KEX, ChaCha20-Poly1305; no OpenSSL or zlib",
                "privilege_separation": "upstream monitor, unprivileged sshd account, chroot and rlimit sandbox"}
    (args.output / "inputs.json").write_text(json.dumps(metadata, indent=2) + "\n")
    with (args.output / "build.log").open("w") as log:
        subprocess.run(command, cwd=copied, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
        log.flush()
        if "checking whether we are cross compiling... yes" not in (args.output / "build.log").read_text():
            raise ValueError("Configure did not establish cross compilation; refuse host target execution")
        make_command = ["make", "-j" + str(args.jobs), *INSTALL]
        metadata["make_command"] = make_command
        (args.output / "inputs.json").write_text(json.dumps(metadata, indent=2) + "\n")
        subprocess.run(make_command, cwd=copied, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
    artifacts = []
    for binary, relative in INSTALL.items():
        dest = args.output / "root" / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(copied / binary, dest)
        dependencies = subprocess.check_output(["xcrun", "otool", "-L", str(dest)], text=True)
        # In-tree crypto and compatibility objects are statically linked. The
        # only target dylibs permitted are from the audited independent runtime.
        linked = checked_dependencies(dependencies)
        artifacts.append({"path": "/" + relative, "sha256": digest(dest), "source": "openssh-portable",
                          "revision": revision, "build_amendments": amendments,
                          "linked_dylibs": linked, "license": "Multiple permissive licenses; see upstream LICENCE",
                          "build_inputs": metadata})
    shutil.copy2(copied / "config.h", args.output / "config.h")
    shutil.copy2(args.source / "LICENCE", args.output / "LICENCE")
    result = {"schema_version": 1, "artifacts": artifacts, "built": True, "stock_runtime_used": False,
              "guest_execution_verified": False, "configure_runtime_assumptions_verified": False,
              "config_h_sha256": digest(copied / "config.h")}
    (args.output / "provenance.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / ".blauos/sources/openssh-portable")
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    for name in ("source", "runtime_root", "output"):
        setattr(args, name, getattr(args, name).resolve())
    output_preexisting = args.output.exists()
    try:
        if not 1 <= args.jobs <= 64:
            raise ValueError("--jobs must be between 1 and 64")
        print(json.dumps(build(args), indent=2))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        if not output_preexisting and args.output.is_dir():
            (args.output / "failure.json").write_text(json.dumps({"built": False, "error": str(error)}, indent=2) + "\n")
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
