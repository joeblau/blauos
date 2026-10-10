#!/usr/bin/env python3
"""Build a classic ARM64 kernel collection from pinned source and audited kexts."""
import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[2]
ORDER = ["IOPCIFamily", "IOStorageFamily", "IONetworkingFamily", "IOVirtIOFamily",
         "PDArmPlatformExpert", "PDArmPCI", "IOVirtIOBlock", "IOVirtIONet", "ext4",
         "Ext4FileSystemDriver", "AppleFileSystemDriver", "corecrypto", "pthread"]
PATCHES = [ROOT / "vm/patches/kc-tools-arm64-16k.patch",
           ROOT / "patches/kc-tools/0002-arm64-dynamic-relocations.patch",
           ROOT / "patches/kc-tools/0003-classic-arm64-protections.patch"]


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def bundle_evidence(path, codeless=False):
    info = path / "Contents/Info.plist"
    if not info.is_file():
        info = path / "Info.plist"
    plist = plistlib.loads(info.read_bytes())
    result = {"path": str(path), "bundle_id": plist["CFBundleIdentifier"],
              "plist_sha256": digest(info)}
    if plist.get("CFBundleExecutable") and not codeless:
        binary = path / "Contents/MacOS" / plist["CFBundleExecutable"]
        result.update(binary=str(binary), binary_sha256=digest(binary))
    return result


def build(args):
    if args.output.exists():
        raise ValueError("Use a fresh output directory to preserve earlier experiments")
    lock = json.loads((ROOT / "sources.lock.json").read_text())
    pins = {item["name"]: item["revision"] for item in lock["sources"]}
    revision = subprocess.check_output(["git", "-C", str(args.source), "rev-parse", "HEAD"], text=True).strip()
    if revision != pins["kc-tools"]:
        raise ValueError("kc-tools source revision differs from sources.lock.json")
    if subprocess.check_output(["git", "-C", str(args.source), "status", "--porcelain"]):
        raise ValueError("kc-tools source checkout must be clean; patches are applied only to a private copy")
    if digest(args.kernel) != args.kernel_sha256:
        raise ValueError("Kernel input hash differs from the supplied build evidence")
    inventory = json.loads(args.inventory.read_text())
    if inventory["source_revision"] != pins["puredarwin-vm-next"]:
        raise ValueError("Kext inventory source differs from the locked PureDarwin revision")
    expected = {Path(item["path"]).stem: item for item in inventory["bundles"]}
    if set(expected) != set(ORDER):
        raise ValueError("Expected all 13 audited essential kext bundles")
    overrides = {}
    replacement_evidence = []
    for name, bundle, metadata in (("corecrypto", args.corecrypto, args.corecrypto_evidence),
                                   ("PDArmPlatformExpert", args.platformexpert, args.platformexpert_evidence)):
        if not bundle:
            continue
        replacement = json.loads(metadata.read_text())
        if replacement["source_revision"] != pins["puredarwin-vm-next"] or replacement["exit_code"] != 0:
            raise ValueError(f"Replacement {name} must have successful locked-source build evidence")
        if "binary_sha256" not in replacement:
            raise ValueError(f"Replacement {name} lacks its actual binary hash")
        # A compiler-flag-only replacement may keep the original audited plist.
        replacement.setdefault("plist_sha256", expected[name]["plist_sha256"])
        for amendment in replacement.get("source_amendments", []):
            if digest(ROOT / amendment["path"]) != amendment["sha256"]:
                raise ValueError(f"Replacement {name} source amendment differs from build evidence")
        overrides[name] = replacement
        replacement_evidence.append({"name": name, "path": str(metadata), "sha256": digest(metadata),
                                     "source_amendments": replacement.get("source_amendments", [])})
    inputs = []
    for name in ORDER:
        path = (args.kext_root / name / (name + ".kext")) if args.kext_root else Path(expected[name]["path"])
        if name == "corecrypto" and args.corecrypto:
            path = args.corecrypto
        if name == "PDArmPlatformExpert" and args.platformexpert:
            path = args.platformexpert
        evidence = bundle_evidence(path)
        reference = overrides.get(name, expected[name])
        for key in ("binary_sha256", "plist_sha256"):
            if key in reference and evidence.get(key) != reference[key]:
                raise ValueError(f"{name}: {key} differs from build evidence")
        if evidence["bundle_id"] != expected[name]["bundle_id"]:
            raise ValueError(f"{name}: bundle identity differs from original inventory")
        inputs.append(evidence)
    codeless = [bundle_evidence(path, codeless=True) for path in sorted(args.codeless.glob("*.kext"))]
    if not codeless or any("binary" in item for item in codeless):
        raise ValueError("Expected audited codeless System.kext plug-ins")
    args.output.mkdir(parents=True)
    copied = args.output / "source"
    copied.mkdir()
    archive = args.output / "kc-tools-source.tar"
    with archive.open("wb") as stream:
        subprocess.run(["git", "-C", str(args.source), "archive", "HEAD"], stdout=stream, check=True)
    with tarfile.open(archive) as stream:
        stream.extractall(copied, filter="data")
    result = {"schema_version": 1, "source": "kc-tools", "source_revision": revision,
              "source_archive_sha256": digest(archive), "kernel": str(args.kernel),
              "kernel_sha256": args.kernel_sha256, "kexts": inputs, "codeless": codeless,
              "kext_inventory_sha256": digest(args.inventory), "replacement_build_evidence": replacement_evidence,
              "patches": [{"path": str(path.relative_to(ROOT)), "sha256": digest(path)} for path in PATCHES],
              "format": "classic prelinked", "arm64_page_size": 16384,
              "kext_vm_base": "0xfffffff004004000", "built": False, "guest_boot_verified": False}
    (args.output / "inputs.json").write_text(json.dumps(result, indent=2) + "\n")
    commands = []
    with (args.output / "build.log").open("w") as log:
        def run(command, cwd=None):
            commands.append(command)
            log.write("$ " + repr(command) + "\n")
            log.flush()
            subprocess.run(command, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, check=True)
        # A private Git root prevents git apply inheriting the enclosing repo
        # and silently skipping a git-format patch outside its path prefix.
        run(["git", "init", "--quiet", str(copied)])
        for patch in PATCHES:
            run(["git", "apply", "--check", str(patch)], cwd=copied)
            run(["git", "apply", str(patch)], cwd=copied)
        if "bind_arm64_kext_pointers" not in (copied / "lib/linker.c").read_text():
            raise ValueError("ARM64 relocation patch was not applied")
        run(["cmake", "-S", str(copied), "-B", str(args.output / "tools"), "-DCMAKE_BUILD_TYPE=Release"])
        run(["cmake", "--build", str(args.output / "tools"), "--target", "kc-builder", "-j", "2"])
        image = args.output / "kernel.classic"
        command = [str(args.output / "tools/kc-builder"), "-kernel", str(args.kernel)]
        for item in inputs:
            command += ["-kext", item["path"]]
        for item in codeless:
            command += ["-codeless", item["path"]]
        command += ["-o", str(image), "-classic", "-kext-vmbase", result["kext_vm_base"]]
        run(command)
        result.update(built=True, collection=str(image), collection_sha256=digest(image))
    (args.output / "commands.json").write_text(json.dumps(commands, indent=2) + "\n")
    (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / ".blauos/sources/kc-tools")
    parser.add_argument("--kernel", type=Path, required=True, help="Unstripped source-built kernel with its symbol table")
    parser.add_argument("--kernel-sha256", required=True)
    parser.add_argument("--inventory", type=Path, default=ROOT / "evidence/kernel/kexts/result.json")
    parser.add_argument("--kext-root", type=Path, help="Override recorded extension build root for another checkout")
    parser.add_argument("--corecrypto", type=Path, help="Source-built -fno-stack-check corecrypto bundle")
    parser.add_argument("--corecrypto-evidence", type=Path, default=ROOT / "evidence/kernel/corecrypto-no-stack-check/result.json")
    parser.add_argument("--platformexpert", type=Path, help="Source-built opt-in QEMU PSCI platform expert bundle")
    parser.add_argument("--platformexpert-evidence", type=Path, default=ROOT / "evidence/kernel/qemu-psci/platform-fast/result.json")
    parser.add_argument("--codeless", type=Path, required=True, help="System.kext/PlugIns directory from the kernel build")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    for name in ("source", "kernel", "inventory", "kext_root", "corecrypto", "corecrypto_evidence",
                 "platformexpert", "platformexpert_evidence", "codeless", "output"):
        value = getattr(args, name)
        if value is not None:
            setattr(args, name, value.resolve())
    try:
        print(json.dumps(build(args), indent=2))
        return 0
    except (OSError, ValueError, KeyError, tarfile.TarError, subprocess.SubprocessError) as error:
        if args.output.is_dir():
            (args.output / "failure.json").write_text(json.dumps({"built": False, "error": str(error)}, indent=2) + "\n")
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
