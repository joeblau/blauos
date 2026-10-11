#!/usr/bin/env python3
"""Build source SSH layers, compose an audited image, and verify two real VM logins."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import sys
import uuid

import accounts
from native_ci import NIX_IMAGE, Pipeline, interrupt_pipeline, parse_nix_outputs

ROOT = Path(__file__).resolve().parents[2]
ENTROPY_PATCHES = ("vm/patches/xnu-loader-ssh-entropy.patch", "vm/patches/puredarwin-ssh-prng.patch")
HASH = re.compile(r"[0-9a-f]{64}\Z")


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def verify_entropy_receipt(receipt, kernel_hash, loader_hash):
    if not isinstance(receipt, dict) or type(receipt.get("schema_version")) is not int or receipt["schema_version"] != 1:
        raise ValueError("Expected entropy build receipt schema_version 1")
    expected = {"kernel_sha256": kernel_hash, "loader_sha256": loader_hash,
                "source_lock_sha256": digest(ROOT / "sources.lock.json"),
                "kernel_prng_provider": "yarrow-fixed-v1"}
    for key, value in expected.items():
        if receipt.get(key) != value:
            raise ValueError("Entropy build receipt mismatch: " + key)
    if receipt.get("loader_require_efi_rng") is not True or receipt.get("virtio_entropy_device") is not True:
        raise ValueError("SSH profile requires strict EFI RNG and the explicit virtio entropy device")
    patches = receipt.get("patches")
    if not isinstance(patches, list) or len(patches) != len(ENTROPY_PATCHES) or any(not isinstance(p, dict) for p in patches):
        raise ValueError("Expected exactly the two audited entropy source overlays")
    found = {entry.get("path"): entry.get("sha256") for entry in patches}
    if found != {name: digest(ROOT / name) for name in ENTROPY_PATCHES}:
        raise ValueError("Entropy source overlays differ from the current workspace")
    # Source/binary identity is a precondition, never proof of guest entropy admission.
    return {"build_inputs_verified": True, "guest_entropy_admission_verified": False,
            "admission_sysctl": "kern.blauos_ssh_entropy_ready"}


def compose(baseline, provenance_path, layers, output):
    if output.exists() or output.is_symlink():
        raise ValueError("Use a fresh root composition directory")
    provenance = json.loads(provenance_path.read_text())
    entries = provenance.get("artifacts")
    if provenance.get("schema_version") != 1 or not isinstance(entries, list):
        raise ValueError("Expected baseline source artifact provenance")
    paths = {entry.get("path") for entry in entries}
    if len(paths) != len(entries):
        raise ValueError("Baseline provenance has duplicate artifact paths")
    pending = []
    for layer, receipt in layers:
        data = json.loads(receipt.read_text())
        if data.get("schema_version") != 1 or not isinstance(data.get("artifacts"), list):
            raise ValueError("Invalid additive SSH provenance")
        for entry in data["artifacts"]:
            name = entry.get("path")
            if not isinstance(name, str) or not name.startswith("/") or ".." in Path(name).parts:
                raise ValueError("Invalid additive artifact path")
            if name in paths or (baseline / name.lstrip("/")).exists() or (baseline / name.lstrip("/")).is_symlink():
                raise ValueError("Refusing to replace baseline artifact " + name)
            source = layer / name.lstrip("/")
            if source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to(layer.resolve()) or digest(source) != entry.get("sha256"):
                raise ValueError("Additive source artifact hash mismatch: " + name)
            paths.add(name)
            pending.append((name, source, entry))
    output.mkdir(parents=True)
    destination = output / "root"
    shutil.copytree(baseline, destination, symlinks=True)
    for name, source, entry in pending:
        target = destination / name.lstrip("/")
        for component in target.relative_to(destination).parents:
            if (destination / component).is_symlink():
                raise ValueError("Symlink in additive artifact parent: " + name)
        if target.is_symlink():
            raise ValueError("Symlink at additive artifact destination: " + name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        entries.append(entry)
    write_json(output / "provenance.json", provenance)
    return destination, output / "provenance.json"


def remove_container(name):
    if not isinstance(name, str) or not re.fullmatch(r"blauos-ssh-ci-[0-9a-f]{12}", name):
        raise ValueError("Refusing to remove an unrecognized SSH workflow container")
    result = subprocess.run(["docker", "inspect", "--format", "{{ index .Config.Labels \"blauos.ssh-workflow\" }}", name],
                            text=True, capture_output=True, timeout=30)
    if result.returncode:
        if "No such object" in result.stderr or "No such container" in result.stderr:
            return
        raise RuntimeError("Cannot verify owned SSH container for cleanup")
    if result.stdout.strip() != name:
        raise ValueError("Container ownership label differs; refusing cleanup")
    subprocess.run(["docker", "rm", "--force", name], check=True, capture_output=True, timeout=60)


def export_evidence(output, public_keys):
    """Export an allowlist of text receipts/logs; disks, roots and keys stay local."""
    export = output / "evidence"
    export.mkdir(exist_ok=True)
    patterns = ("result.json", "host.json", "baseline-audit.json", "runtime-audit.json", "source-inputs.json",
                "archive-inputs.json",
                "entropy-receipt.json", "linux-tools.json", "logs/*.log", "openssh/inputs.json",
                "openssh/provenance.json", "openssh/config.h", "openssh/LICENCE", "capabilities/build.json",
                "capabilities/provenance.json", "startup/inputs.json", "startup/provenance.json",
                "commands/build.json", "commands/catalogue.json", "commands/provenance.json",
                "commands/third-party-notices.txt", "commands/*/third-party-notices.txt",
                "commands/*/provenance.json", "commands/*/commands.json", "commands/*/inputs.json",
                "composed/provenance.json", "image/result.json", "image/account.json", "image/assemble.log",
                "smoke/**/checkpoint.json", "smoke/**/result.json", "smoke/**/serial.log", "smoke/**/probe.log",
                "smoke/**/*.stdout", "smoke/**/*.stderr", "smoke/**/known_hosts")
    exported = []
    for pattern in patterns:
        for source in output.glob(pattern):
            if not source.is_file() or source.is_symlink():
                continue
            content = source.read_text(errors="strict")
            if re.search(r"-----BEGIN[^\n]*PRIVATE KEY-----", content):
                raise ValueError("Private-key content found in evidence candidate; export refused")
            for key in public_keys:
                content = content.replace(key, "[authorized client public key omitted]")
            target = export / source.relative_to(output)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            exported.append({"path": str(target.relative_to(export)), "sha256": digest(target)})
    write_json(export / "export.json", {"schema_version": 1, "files": exported,
               "excludes": ["private keys", "authorized client public-key files", "root trees", "disk images", "NVRAM"],
               "physical_boot_gate": False})


def execute(args):
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise ValueError("SSH workflow requires the ARM64 macOS build/Virtualization host")
    accounts.validate_account(args.username, args.uid, args.gid)
    accounts.offline_root(args.root)
    if args.output.resolve().is_relative_to(args.root.resolve()):
        raise ValueError("Build output must be outside the input baseline root")
    for value in args.network:
        ipaddress.IPv4Address(value)
    ipaddress.IPv4Network((args.network[0], args.network[1]), strict=False)
    for path, expected in ((args.kernel, args.kernel_sha256), (args.loader, args.loader_sha256)):
        if not expected or not HASH.fullmatch(expected) or path.is_symlink() or not path.is_file() or digest(path) != expected:
            raise ValueError("Kernel/loader requires an ordinary file and its exact recorded SHA-256")
    admission = verify_entropy_receipt(json.loads(args.entropy_receipt.read_text()), args.kernel_sha256, args.loader_sha256)
    user_keys = accounts.public_keys(args.public_key.read_text() if args.public_key else os.environ.get(args.public_key_env, ""))
    pipeline = Pipeline(args.output)
    pipeline.result.update(ssh_login_verified=False, guest_entropy_admission_verified=False,
                           acceptance_profile="generic EFI with explicit virtio entropy device",
                           entropy_build_admission=admission)
    container = "blauos-ssh-ci-" + uuid.uuid4().hex[:12]
    attempted = False
    identity = args.output / "private/test_identity"
    old_signals = {signum: signal.signal(signum, interrupt_pipeline) for signum in (signal.SIGINT, signal.SIGTERM)}
    try:
        status = subprocess.check_output(["git", "-C", str(ROOT), "status", "--porcelain"], text=True).strip()
        if args.require_clean_project and status:
            raise ValueError("The workflow checkout must be clean")
        pipeline.result["project_revision"] = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
        pipeline.result["project_dirty"] = bool(status)
        pipeline.python("validate-lock", "blauos.py", "validate-lock", timeout=60)
        pipeline.python("host-preflight", "blauos.py", "doctor", "--verify-sdk", "--report", args.output / "host.json", timeout=300)
        pipeline.python("baseline-audit", "build/runtime.py", "inspect", "--root", args.root,
                        "--provenance", args.provenance, "--output", args.output / "baseline-audit.json", timeout=300)
        if json.loads((args.output / "baseline-audit.json").read_text()).get("offline_dependency_closure") is not True:
            raise ValueError("Independent baseline dependency closure did not pass")
        inputs = args.output / "inputs"
        inputs.mkdir()
        for name, source, expected in (("kernel.classic", args.kernel, args.kernel_sha256), ("loader.efi", args.loader, args.loader_sha256)):
            shutil.copyfile(source, inputs / name)
            if digest(inputs / name) != expected:
                raise ValueError("Kernel/loader changed while copied into the owned build")
        shutil.copyfile(args.entropy_receipt, args.output / "entropy-receipt.json")
        verify_entropy_receipt(json.loads((args.output / "entropy-receipt.json").read_text()), args.kernel_sha256, args.loader_sha256)
        sources = args.output / "sources"
        pipeline.python("fetch-ssh-sources", "blauos.py", "fetch", "openssh-portable", "puredarwin-vm-next",
                        "apple-file_cmds", "apple-libutil", "apple-shell-cmds", "apple-text-commands",
                        "--sources-dir", sources, "--report", args.output / "source-inputs.json", timeout=1800)
        inputs_dir = args.output / "inputs"
        pipeline.python("fetch-vim-archive", "blauos.py", "fetch-archives", "vim-9.2.1175",
                        "--inputs-dir", inputs_dir, "--report", args.output / "archive-inputs.json", timeout=600)
        commands = args.output / "commands"
        pipeline.python("build-command-tools", "vm/build_commands.py", "--source", sources / "puredarwin-vm-next",
                        "--file-source", sources / "apple-file_cmds", "--libutil-source", sources / "apple-libutil",
                        "--shell-source", sources / "apple-shell-cmds", "--text-source", sources / "apple-text-commands",
                        "--vim-archive", inputs_dir / "vim-9.2.1175.tar.gz",
                        "--runtime-root", args.root, "--output", commands, timeout=900)
        ssh = args.output / "openssh"
        pipeline.python("build-openssh", "build/openssh.py", "--source", sources / "openssh-portable",
                        "--runtime-root", args.root, "--output", ssh, "--jobs", args.jobs, timeout=1800)
        capability = args.output / "capabilities"
        pipeline.python("build-capabilities", "build/ssh_capabilities.py", "build", "--runtime-root", args.root,
                        "--output-dir", capability, timeout=300)
        startup = args.output / "startup"
        pipeline.python("build-ssh-startup", "vm/build_ssh_start.py", "--runtime-root", args.root,
                        "--output", startup, timeout=300)
        composed_root, provenance = compose(args.root, args.provenance,
            [(layer / "root", layer / "provenance.json") for layer in (ssh, capability, startup, commands)], args.output / "composed")
        notice = composed_root / "usr/share/doc/blauos-commands/third-party-notices.txt"
        notice.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(commands / "third-party-notices.txt", notice)
        pipeline.python("ssh-runtime-audit", "build/runtime.py", "inspect", "--root", composed_root,
                        "--provenance", provenance, "--output", args.output / "runtime-audit.json", timeout=300)
        if json.loads((args.output / "runtime-audit.json").read_text()).get("offline_dependency_closure") is not True:
            raise ValueError("Independent SSH runtime closure did not pass")
        identity.parent.mkdir(mode=0o700)
        pipeline.run("owned-test-key", ["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "blauos-owned-test",
                                       "-f", identity], timeout=60)
        test_keys = accounts.public_keys(identity.with_suffix(".pub").read_text())
        combined = inputs / "authorized-client-public-keys"
        combined.write_text("\n".join(user_keys + test_keys) + "\n")
        combined.chmod(0o600)
        attempted = True
        pipeline.result["linux_container"] = container
        pipeline.save()
        pipeline.run("linux-container", ["docker", "run", "-d", "--rm", "--platform", "linux/arm64", "--name", container,
                     "--label", "blauos.ssh-workflow=" + container,
                     "--mount", f"type=bind,source={ROOT},target={ROOT},readonly",
                     "--mount", f"type=bind,source={args.output},target={args.output}",
                     "--mount", "type=volume,source=blauos-native-ci-nix-store,target=/nix", NIX_IMAGE, "sleep", "infinity"], timeout=300)
        expression = f'''let f=builtins.getFlake {json.dumps('path:' + str(sources / 'puredarwin-vm-next'))};
            p=import f.inputs.nixpkgs {{system="aarch64-linux";}};
            in builtins.attrValues {{python=p.python314; mtools=p.mtools; fat=p.dosfstools; ext4=p.e2fsprogs;
                 gpt=p.gptfdisk; util=p.util-linux; fakeroot=p.fakeroot;}}'''
        raw = pipeline.run("linux-tools", ["docker", "exec", container, "nix", "--extra-experimental-features", "nix-command flakes",
                           "build", "--impure", "--expr", expression, "--no-link", "--json", "--max-jobs", "1", "--cores", "2"], timeout=1800)
        tool_outputs = parse_nix_outputs(raw)
        write_json(args.output / "linux-tools.json", tool_outputs)
        tools = [path for entry in tool_outputs for path in entry["outputs"].values()]
        environment = ":".join(str(Path(path) / folder) for path in tools for folder in ("bin", "sbin")) + ":/root/.nix-profile/bin:/bin"
        python = Path(next(entry["outputs"]["out"] for entry in tool_outputs if "python3-" in entry["drvPath"])) / "bin/python3"
        image_output = args.output / "image"
        pipeline.run("assemble-ssh-image", ["docker", "exec", "-e", "PATH=" + environment, container, python,
                     ROOT / "scripts/vm/assemble.py", "--root", composed_root, "--provenance", provenance,
                     "--kernel", inputs / "kernel.classic", "--loader", inputs / "loader.efi", "--output", image_output,
                     "--root-mb", "1024", "--boot-args", "-v debug=0x218 serial=3 keepsyms=1 no_interrupt_masked_debug=1 gopconsole=1",
                     "--ssh-user", args.username, "--ssh-uid", args.uid, "--ssh-gid", args.gid,
                     "--ssh-public-key", combined, "--network", *args.network], timeout=900)
        image = image_output / "blauos-arm64.img"
        image_hash = json.loads((image_output / "result.json").read_text())["image_sha256"]
        probe = args.output / "virtualization-probe"
        entitlement = inputs / "probe-entitlements.plist"
        entitlement.write_text('<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd"><plist version="1.0"><dict><key>com.apple.security.virtualization</key><true/></dict></plist>\n')
        pipeline.run("build-public-vz-probe", ["xcrun", "swiftc", ROOT / "vm/virtualbuddy_probe.swift", "-o", probe], timeout=300)
        pipeline.run("sign-public-vz-probe", ["codesign", "--force", "--sign", "-", "--entitlements", entitlement, probe], timeout=60)
        smoke = args.output / "smoke"
        pipeline.python("two-boot-ssh-acceptance", "vm/ssh_smoke.py", "--image", image, "--image-sha256", image_hash,
                        "--probe", probe, "--guest-ip", args.network[0], "--username", args.username,
                        "--uid", args.uid, "--gid", args.gid, "--identity", identity, "--output", smoke,
                        "--entropy-device", "--commands-smoke", ROOT / "vm/commands/smoke.sh",
                        "--timeout-seconds", args.boot_timeout, timeout=args.boot_timeout * 2 + 300)
        checkpoint = json.loads((smoke / "checkpoint.json").read_text())
        if checkpoint.get("passed") is not True:
            raise ValueError("Actual two-boot SSH acceptance did not pass")
        pipeline.result.update(status="passed", guest_boot_passed=True, ssh_login_verified=True,
                               guest_entropy_admission_verified=True, ssh_checkpoint_sha256=digest(smoke / "checkpoint.json"),
                               image_sha256=image_hash, image=str(image), physical_boot_gate=False)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, KeyError, StopIteration) as error:
        pipeline.result["failure"] = str(error)
    finally:
        if attempted:
            try:
                remove_container(container)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                pipeline.result.update(container_cleanup_failure=str(error), status="failed")
        # Never read either private key. Remove only the key pair created by this run.
        for path in (identity, identity.with_suffix(".pub"), args.output / "smoke/wrong-key", args.output / "smoke/wrong-key.pub"):
            path.unlink(missing_ok=True)
        pipeline.save()
        try:
            export_evidence(args.output, user_keys)
        except (OSError, ValueError, UnicodeError) as error:
            pipeline.result.update(evidence_export_failure=str(error), status="failed")
            pipeline.save()
            write_json(args.output / "evidence/result.json", pipeline.result)
        for signum, handler in old_signals.items():
            signal.signal(signum, handler)
    print(json.dumps(pipeline.result, indent=2))
    return 0 if pipeline.result["status"] == "passed" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline-receipt", type=Path, help="JSON paths/hashes for the prepared independent baseline")
    for name in ("root", "provenance", "kernel", "loader", "entropy-receipt"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--kernel-sha256")
    parser.add_argument("--loader-sha256")
    keys = parser.add_mutually_exclusive_group()
    keys.add_argument("--public-key", type=Path)
    keys.add_argument("--public-key-env", help="Environment variable containing bare Ed25519 public keys")
    parser.add_argument("--username", default="joeblau")
    parser.add_argument("--uid", type=int, default=1000)
    parser.add_argument("--gid", type=int, default=1000)
    parser.add_argument("--network", nargs=4, default=["192.168.64.201", "255.255.255.0", "192.168.64.1", "192.168.64.1"],
                        metavar=("ADDRESS", "NETMASK", "GATEWAY", "DNS"))
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--boot-timeout", type=int, default=180)
    parser.add_argument("--require-clean-project", action="store_true")
    parser.add_argument("--cleanup-only", action="store_true")
    args = parser.parse_args()
    args.output = args.output.resolve()
    try:
        if args.cleanup_only:
            receipt = args.output / "result.json"
            if receipt.is_file():
                container = json.loads(receipt.read_text()).get("linux_container")
                if container:
                    remove_container(container)
            return 0
        if args.baseline_receipt:
            data = json.loads(args.baseline_receipt.read_text())
            if data.get("schema_version") != 1:
                raise ValueError("Expected baseline receipt schema_version 1")
            for name in ("root", "provenance", "kernel", "loader", "entropy_receipt", "kernel_sha256", "loader_sha256"):
                if getattr(args, name) is not None:
                    raise ValueError("Use either a baseline receipt or explicit baseline inputs")
                value = data.get(name)
                if not isinstance(value, str) or not value:
                    raise ValueError("Baseline receipt is missing " + name)
                setattr(args, name, value if name.endswith("sha256") else Path(value))
        for name in ("root", "provenance", "kernel", "loader", "entropy_receipt"):
            value = getattr(args, name)
            if value is None:
                raise ValueError("Required baseline input missing: --" + name.replace("_", "-"))
            setattr(args, name, value.absolute())
        if not args.public_key and not args.public_key_env:
            raise ValueError("Supply a public-key file or environment variable")
        if not 1 <= args.jobs <= 64 or not 60 <= args.boot_timeout <= 600:
            raise ValueError("Jobs must be 1–64 and boot timeout 60–600 seconds")
        return execute(args)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
