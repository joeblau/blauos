#!/usr/bin/env python3
"""Execute the source build, image audit and real VM boot as one failing CI job."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def run(command, log=None, timeout=7200):
    if log:
        with log.open("w") as stream:
            subprocess.run(command, check=True, stdout=stream, stderr=subprocess.STDOUT, timeout=timeout)
    else:
        subprocess.run(command, check=True, timeout=timeout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / ".blauos/sources/puredarwin-vm-next")
    parser.add_argument("--sdk-archive", type=Path, required=True)
    parser.add_argument("--sdk-sha256", required=True)
    parser.add_argument("--sdk-version", required=True)
    parser.add_argument("--output", type=Path, default=ROOT / ".blauos/build/vm/ci")
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    status = {"schema_version": 1, "status": "failed", "steps": [], "guest_boot_passed": False, "physical_boot_gate": False}
    try:
        run([sys.executable, str(ROOT / "scripts/blauos.py"), "validate-lock"], args.output / "validate-lock.log", timeout=60)
        status["steps"].append("validate-lock")
        run([sys.executable, str(ROOT / "scripts/vm/build.py"), "--source", str(args.source),
             "--output", str(args.output / "image"), "--sdk-archive", str(args.sdk_archive.resolve()),
             "--sdk-sha256", args.sdk_sha256, "--sdk-version", args.sdk_version], args.output / "image-build.log")
        status["steps"].append("kernel-runtime-image-build")
        source = args.output / "image/source"
        outputs = json.loads((args.output / "image/outputs.json").read_text())
        image_output = Path(outputs[0]["outputs"]["out"])
        expr = f'''let f=builtins.getFlake {json.dumps("path:" + str(source))}; p=import f.inputs.nixpkgs {{system=builtins.currentSystem;}}; in [p.qemu p.pkgsCross.aarch64-multiplatform.OVMF.fd]'''
        tools = subprocess.check_output(["nix", "--extra-experimental-features", "nix-command flakes", "build",
                                         "--impure", "--expr", expr, "--no-link", "--json"], text=True, timeout=900)
        (args.output / "vm-tool-outputs.json").write_text(tools)
        tool_outputs = json.loads(tools)
        qemu = next(Path(item["outputs"]["out"]) / "bin/qemu-system-aarch64" for item in tool_outputs if "qemu-" in item["drvPath"])
        firmware = next(Path(item["outputs"]["fd"]) / "FV" for item in tool_outputs if "OVMF-" in item["drvPath"])
        image = image_output / "puredarwin-arm64-virt-minimal-release.img"
        run([sys.executable, str(ROOT / "scripts/vm/finalize.py"), "--image", str(image),
             "--root", str(image_output / "root-staging"), "--provenance", str(image_output / "runtime-provenance.json"),
             "--firmware", str(firmware / "AAVMF_CODE.fd"), "--vars-template", str(firmware / "AAVMF_VARS.fd"),
             "--symbols", str(image_output / "kernel.symbols"), "--qemu", str(qemu),
             "--output", str(args.output / "artifacts")], args.output / "runtime-audit.log", timeout=300)
        status["steps"].append("runtime-dependency-audit")
        run([sys.executable, str(ROOT / "scripts/vm/smoke.py"), "--image", str(image),
             "--manifest", str(args.output / "artifacts/image-manifest.json"), "--firmware", str(firmware / "AAVMF_CODE.fd"),
             "--vars-template", str(firmware / "AAVMF_VARS.fd"), "--symbols", str(image_output / "kernel.symbols"),
             "--qemu", str(qemu), "--machine", "virt-10.2", "--memory", "1024", "--timeout", "300",
             "--output", str(args.output / "boot")], args.output / "smoke-command.log", timeout=330)
        status["steps"].append("real-guest-boot-network")
        status["guest_boot_passed"] = True
        status["status"] = "passed"
    except (OSError, ValueError, subprocess.SubprocessError, StopIteration, KeyError) as error:
        status["failure"] = str(error)
    finally:
        (args.output / "result.json").write_text(json.dumps(status, indent=2) + "\n")
    print(json.dumps(status, indent=2))
    return 0 if status["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
