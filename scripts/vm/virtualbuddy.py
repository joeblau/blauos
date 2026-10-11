#!/usr/bin/env python3
"""Create a separate VirtualBuddy bundle from a hash-verified BlauOS EFI image."""
import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import secrets
import shutil
import sys
import uuid


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def bundle(image, expected_hash, output):
    image, output = image.resolve(), output.absolute()
    if not image.is_file():
        raise ValueError("Input must be an ordinary image file")
    if output.suffix != ".vbvm" or output.exists() or output.is_symlink():
        raise ValueError("Choose a new .vbvm bundle; existing VMs are preserved")
    if digest(image) != expected_hash:
        raise ValueError("Image hash differs from the supplied build receipt")
    mac = "02:" + ":".join(f"{part:02x}" for part in secrets.token_bytes(5))
    config = {
        "version": 0, "systemType": "linux",
        "hardware": {
            "cpuCount": 1, "memorySize": 1073741824,
            "pointingDevice": {"kind": 0}, "keyboardDevice": {"kind": 0},
            "displayDevices": [{"id": str(uuid.uuid4()).upper(), "name": "1280x800",
                "width": 1280, "height": 800, "pixelsPerInch": 96,
                "automaticallyReconfiguresDisplay": False}],
            "networkDevices": [{"id": "Default", "name": "Default", "kind": 0, "macAddress": mac}],
            "soundDevices": [], "NVRAM": [],
            "_storageDevices": [{"id": str(uuid.uuid4()).upper(), "isBootVolume": True,
                "isEnabled": True, "isSavedStateClone": False, "isReadOnly": False,
                "isUSBMassStorageDevice": False,
                "backing": {"managedImage": {"_0": {"id": "__BOOT__", "filename": "Disk",
                    "size": image.stat().st_size, "format": 0}}}}],
        },
        "sharedFolders": [], "guestAdditionsEnabled": False,
        "rosettaSharingEnabled": False, "captureSystemKeys": False,
    }
    metadata = {"uuid": str(uuid.uuid4()).upper(), "version": 1, "installFinished": True}
    output.mkdir(parents=True)
    data = output / ".vbdata"
    data.mkdir()
    shutil.copyfile(image, output / "Disk.img")
    if digest(output / "Disk.img") != expected_hash:
        raise ValueError("Copied image failed verification; incomplete bundle retained")
    for name, value in (("Config.plist", config), ("Metadata.plist", metadata)):
        (data / name).write_bytes(plistlib.dumps(value, fmt=plistlib.FMT_BINARY))
    # VirtualBuddy creates fresh EFI variables through its public framework helper.
    receipt = {"schema_version": 1, "bundle": str(output), "image_sha256": expected_hash,
               "configuration": config, "metadata": metadata, "boot_verified": False,
               "physical_boot_gate": False, "nvram": "Fresh store created on first start"}
    (data / "blauos-prepared.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--image-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(bundle(args.image, args.image_sha256, args.output), indent=2))
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
