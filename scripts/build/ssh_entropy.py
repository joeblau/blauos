#!/usr/bin/env python3
"""Rebuild SSH entropy assets using explicitly recorded cached kernel inputs."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys

REPO = Path(__file__).resolve().parents[2]
CORE = Path("src/Kernel/Extensions/corecrypto")
REQUIRED_KEXTS = {"IOPCIFamily", "IOStorageFamily", "IONetworkingFamily", "IOVirtIOFamily",
                  "PDArmPlatformExpert", "PDArmPCI", "IOVirtIOBlock", "IOVirtIONet", "ext4",
                  "Ext4FileSystemDriver", "AppleFileSystemDriver", "corecrypto", "pthread",
                  "IOGraphicsFamily", "IOGOPFramebuffer", "IOVirtIOGPU"}


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        result = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def evidence(path: Path) -> dict:
    return {"path": str(path), "sha256": digest(path)}


def write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def run(argv: list[str], log: Path, cwd: Path | None = None) -> None:
    with log.open("w") as stream:
        stream.write(json.dumps(argv) + "\n"); stream.flush()
        code = subprocess.run(argv, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT).returncode
    if code:
        raise ValueError(f"command failed with status {code}; see {log}")


def pinned(path: Path, expected: str) -> None:
    actual = subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()
    if actual != expected:
        raise ValueError(f"source revision differs from lock: {path}")
    if subprocess.check_output(["git", "-C", str(path), "status", "--porcelain"], text=True).strip():
        raise ValueError(f"use clean pinned sources: {path}")


def tree_inputs(root: Path) -> list[dict]:
    return [evidence(path) for path in sorted(root.rglob("*")) if path.is_file()]


def patched_copy(source: Path, destination: Path, patches: list[Path]) -> None:
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns(".git"))
    for index, patch in enumerate(patches):
        run(["patch", "-p1", "-i", str(patch)], destination.parent / f"patch-{destination.name}-{index}.log",
            cwd=destination)


def corecrypto(args: argparse.Namespace, output: Path, pure: Path) -> tuple[Path, Path]:
    baseline = args.kernel_build.resolve()
    target = baseline / CORE
    metadata = target / "CMakeFiles/corecrypto.kext.dir"
    flags_path, link_path = metadata / "flags.make", metadata / "link.txt"
    flags = {line.split(" = ", 1)[0]: line.split(" = ", 1)[1] for line in
             flags_path.read_text().splitlines() if " = " in line}
    link = shlex.split(link_path.read_text())
    overlay = output / "corecrypto-source"
    original = overlay / CORE
    original.parent.mkdir(parents=True)
    shutil.copytree(pure / CORE, original)
    patch = REPO / "vm/patches/puredarwin-ssh-prng.patch"
    run(["patch", "-p1", "-i", str(patch)], output / "corecrypto-patch.log", cwd=overlay)
    build = output / "corecrypto"
    build.mkdir()
    bundle = build / "corecrypto.kext"
    (bundle / "Contents/MacOS").mkdir(parents=True)
    cached_plist = target / "corecrypto.kext/Contents/Info.plist"
    shutil.copy2(cached_plist, bundle / "Contents/Info.plist")
    inputs = [evidence(flags_path), evidence(link_path), evidence(cached_plist),
              evidence(baseline / "CMakeCache.txt")]
    includes: list[dict] = []
    commands: list[list[str]] = []
    replacement_objects: dict[str, str] = {}
    # Rebuild every object in this source kext. Only generated kernel headers,
    # kmod metadata and libkmod are reused; the old corecrypto objects are not.
    for token in link:
        if not token.endswith(".o"):
            continue
        relative = Path(token).relative_to("CMakeFiles/corecrypto.kext.dir")
        name = str(relative)[:-2]
        source = target / "kmod_info.c" if name == "kmod_info.c" else original / name
        if not source.is_file():
            raise ValueError(f"corecrypto source input is missing: {source}")
        language = "CXX" if source.suffix in {".cc", ".cpp"} else "C"
        compiler = Path(link[0]) if language == "CXX" else Path(link[0]).with_name("clang")
        header_flags = shlex.split(flags[language + "_INCLUDES"])
        updated: list[str] = []
        for value in header_flags:
            if not value.startswith("-I"):
                raise ValueError(f"unexpected cached header flag: {value}")
            path = Path(value[2:])
            if path == baseline / "source-overlay" / CORE / "include":
                path = original / "include"
            elif path == baseline / "source-overlay/src/Libraries/AvailabilityVersions/include":
                path = pure / "src/Libraries/AvailabilityVersions/include"
            updated.append("-I" + str(path))
            if not path.is_dir():
                raise ValueError(f"cached generated kernel header directory is missing: {path}")
            if str(path) not in {entry["path"] for entry in includes}:
                includes.append({"path": str(path), "files": tree_inputs(path)})
        obj = build / "objects" / relative
        obj.parent.mkdir(parents=True, exist_ok=True)
        argv = [str(compiler), *shlex.split(flags[language + "_DEFINES"]), *updated,
                *shlex.split(flags[language + "_FLAGS"]), "-c", str(source), "-o", str(obj)]
        run(argv, build / (str(relative).replace("/", "-") + ".log"))
        commands.append(argv)
        replacement_objects[token] = str(obj)
        if name == "kmod_info.c":
            inputs.append(evidence(source))
    argv = list(link)
    argv[argv.index("-o") + 1] = str(bundle / "Contents/MacOS/corecrypto")
    for index, token in enumerate(argv):
        if token in replacement_objects:
            argv[index] = replacement_objects[token]
        elif token.endswith(".a"):
            path = (target / token).resolve()
            inputs.append(evidence(path)); argv[index] = str(path)
    run(argv, build / "link.log")
    tools = {Path(command[0]) for command in commands}
    tools.add(Path(argv[0]))
    tools.update(Path(token.split("=", 1)[1]) for token in argv if token.startswith("-fuse-ld="))
    receipt = {"schema_version": 1, "exit_code": 0, "source_revision": args.pins["puredarwin-vm-next"],
               "profile": "all-corecrypto-objects-rebuilt-with-cached-generated-kernel-headers",
               "full_kernel_rebuilt": False, "source_files": tree_inputs(original),
               "cached_inputs": inputs, "header_inputs": includes, "compile_commands": commands,
               "compiler_and_linker_inputs": [evidence(path) for path in sorted(tools)],
               "argv": argv, "object_inputs": [evidence(Path(path)) for path in replacement_objects.values()],
               "source_amendments": [{"path": str(patch.relative_to(REPO)), "sha256": digest(patch)}],
               "binary_sha256": digest(bundle / "Contents/MacOS/corecrypto"),
               "plist_sha256": digest(bundle / "Contents/Info.plist")}
    receipt_path = build / "build.json"
    write(receipt_path, receipt)
    return bundle, receipt_path


def collection(args: argparse.Namespace, output: Path, bundle: Path) -> tuple[Path, Path]:
    command = json.loads(args.collection_command.read_text())
    if isinstance(command, dict):
        command = command.get("argv", command.get("command"))
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        raise ValueError("collection command must be the actual kc-builder argv")
    command = list(command)
    if command.count("-kernel") != 1 or command.count("-o") != 1:
        raise ValueError("collection command lacks a single kernel input/output")
    if "-classic" not in command or "-kext-vmbase" not in command:
        raise ValueError("collection command differs from the classic ARM64 profile")
    inputs = [evidence(Path(command[0]))]
    identities = []
    for index, token in enumerate(command):
        if token == "-kernel":
            inputs.append(evidence(Path(command[index + 1])))
        elif token in {"-kext", "-codeless"}:
            path = Path(command[index + 1])
            if token == "-kext":
                identities.append(path.stem)
                if path.stem == "corecrypto":
                    path = bundle
                    command[index + 1] = str(path)
            inputs += tree_inputs(path)
    if set(identities) != REQUIRED_KEXTS or len(identities) != 16:
        raise ValueError("cached collection must contain exactly the 16 VirtualBuddy code kexts")
    directory = output / "collection"
    directory.mkdir()
    kernel = directory / "kernel.classic"
    command[command.index("-o") + 1] = str(kernel)
    run(command, directory / "build.log")
    path = directory / "build.json"
    write(path, {"schema_version": 1, "profile": "cached-kernel-and-15-kexts-plus-rebuilt-corecrypto",
                 "full_kernel_rebuilt": False, "command": command, "inputs": inputs,
                 "output": str(kernel), "output_sha256": digest(kernel), "exit_code": 0})
    return kernel, path


def loader(args: argparse.Namespace, output: Path, pure: Path, source: Path) -> tuple[Path, Path]:
    patches = [REPO / "vm/patches" / name for name in
               ("xnu-loader-efi-diagnostics.patch", "xnu-loader-virtualbuddy-platform.patch",
                "xnu-loader-ssh-entropy.patch")]
    overlay = output / "loader-source"
    patched_copy(source, overlay, patches)
    expression = ("import " + str(REPO / "vm/virtualbuddy-loader.nix") + " { puredarwinSource = " +
                  json.dumps(str(pure)) + "; loaderSource = " + json.dumps(str(overlay)) + "; requireEfiRng = true; }")
    command = ["docker", "exec", args.container, "nix", "--extra-experimental-features",
               "nix-command flakes", "build", "--impure", "--expr", expression, "--no-link", "--json",
               "--max-jobs", "1", "--cores", "2", "--print-build-logs"]
    process = subprocess.run(command, capture_output=True, text=True)
    (output / "loader-build.log").write_text(process.stderr)
    (output / "loader-build-output.json").write_text(process.stdout)
    if process.returncode:
        raise ValueError("strict loader build failed; see loader-build.log")
    derivations = json.loads(process.stdout)
    nix_output = derivations[0]["outputs"]["out"]
    binary = output / "BOOTAA64.EFI"
    subprocess.run(["docker", "cp", args.container + ":" + nix_output + "/xnu-loader.efi", str(binary)],
                   check=True, capture_output=True)
    path = output / "loader-build.json"
    write(path, {"schema_version": 1, "source_revision": args.pins["xnu-loader"],
                 "source_files": tree_inputs(overlay), "patches": [evidence(patch) for patch in patches],
                 "nix_expression": evidence(REPO / "vm/virtualbuddy-loader.nix"),
                 "command": command, "nix_outputs": derivations, "loader_require_efi_rng": True,
                 "binary": str(binary), "binary_sha256": digest(binary), "exit_code": 0})
    return binary, path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel-build", required=True, type=Path)
    parser.add_argument("--collection-command", required=True, type=Path)
    parser.add_argument("--container", required=True)
    parser.add_argument("--output", "--output-dir", dest="output", required=True, type=Path)
    parser.add_argument("--puredarwin-source", type=Path, default=REPO / ".blauos/sources/puredarwin-vm-next")
    canonical_loader = REPO / ".blauos/sources/xnu-loader"
    legacy_loader = REPO / ".blauos/sources/xnu-loader-vm"
    parser.add_argument("--loader-source", type=Path,
                        default=canonical_loader if canonical_loader.is_dir() or not legacy_loader.is_dir() else legacy_loader)
    args = parser.parse_args()
    output = args.output.resolve()
    created = False
    try:
        if platform.system() != "Darwin":
            raise ValueError("this cached ARM64 kernel-header profile requires a macOS build host")
        if args.output.exists() or args.output.is_symlink():
            raise ValueError("use a fresh output directory to preserve prior build evidence")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", args.container):
            raise ValueError("container must be an existing caller-owned container name")
        lock = REPO / "sources.lock.json"
        lock_sha256 = digest(lock)
        args.pins = {item["name"]: item["revision"] for item in json.loads(lock.read_text())["sources"]}
        pure, source = args.puredarwin_source.resolve(), args.loader_source.resolve()
        pinned(pure, args.pins["puredarwin-vm-next"]); pinned(source, args.pins["xnu-loader"])
        # The caller supplies and owns the builder; this tool neither creates
        # nor deletes containers, volumes, source checkouts, or cached builds.
        container = json.loads(subprocess.check_output(["docker", "inspect", args.container], text=True))[0]
        if not container["State"]["Running"]:
            raise ValueError("caller-owned Nix builder is not running")
        output.mkdir(parents=True)
        created = True
        write(output / "container-input.json", {"id": container["Id"], "image": container["Image"],
                                                "mounts": container["Mounts"]})
        bundle, crypto_receipt = corecrypto(args, output, pure)
        kernel, collection_receipt = collection(args, output, bundle)
        efi, loader_receipt = loader(args, output, pure, source)
        if digest(lock) != lock_sha256:
            raise ValueError("source lock changed during the build; preserve this run and build from a fresh snapshot")
        receipt = {"schema_version": 1, "profile": "cached-kernel-entropy-overlay",
                   "full_kernel_rebuilt": False, "kernel_sha256": digest(kernel),
                   "loader_sha256": digest(efi), "source_lock_sha256": lock_sha256,
                   "loader_require_efi_rng": True, "kernel_prng_provider": "yarrow-fixed-v1",
                   "virtio_entropy_device": True, "guest_entropy_admission_verified": False,
                   "patches": [{"path": "vm/patches/" + name, "sha256": digest(REPO / "vm/patches" / name)}
                               for name in ("xnu-loader-ssh-entropy.patch", "puredarwin-ssh-prng.patch")],
                   "build_evidence": [evidence(path) for path in
                                      (crypto_receipt, collection_receipt, loader_receipt)]}
        write(output / "entropy-receipt.json", receipt)
        write(output / "assets.json", {"schema_version": 1, "kernel": str(kernel), "loader": str(efi),
                                       "kernel_sha256": digest(kernel), "loader_sha256": digest(efi),
                                       "entropy_receipt": str(output / "entropy-receipt.json")})
        print(json.dumps({"status": "pass", "output": str(output)}, indent=2))
        return 0
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as error:
        if created:
            write(output / "failure.json", {"status": "blocked", "error": str(error)})
        print(f"SSH entropy build: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
