#!/usr/bin/env python3
"""Build a separate VirtualBuddy development app with Linux entropy enabled."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys

REPO = Path(__file__).resolve().parents[2]
PIN = "088351b0fc67e0b24b83e7954ad48314dda4ce04"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    try:
        if platform.system() != "Darwin":
            raise ValueError("VirtualBuddy app build requires a macOS/Xcode host")
        source = args.source.resolve()
        lock = REPO / "sources.lock.json"
        source_pin = next((item["revision"] for item in json.loads(lock.read_text())["sources"]
                           if item["name"] == "virtualbuddy-entropy-host"), None)
        if source_pin != PIN:
            raise ValueError("VirtualBuddy entropy-host lock differs from the reviewed 2.1 source pin")
        revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        if revision != source_pin:
            raise ValueError("VirtualBuddy source differs from the audited 2.1 pin")
        if subprocess.check_output(["git", "-C", str(source), "status", "--porcelain"], text=True).strip():
            raise ValueError("use a clean pinned VirtualBuddy checkout")
        if args.output_dir.exists() or args.output_dir.is_symlink():
            raise ValueError("use a fresh output directory; existing app/artifacts are preserved")
        output = args.output_dir.resolve()
        output.mkdir(parents=True)
        overlay = output / "source-overlay"
        # Only tracked files enter the overlay, so ignored build files or local
        # signing settings cannot be silently copied into this development app.
        files = subprocess.check_output(["git", "-C", str(source), "ls-files", "-z"]).split(b"\0")
        for raw in files:
            if not raw:
                continue
            relative = Path(raw.decode())
            destination = overlay / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            original = source / relative
            if original.is_symlink():
                destination.symlink_to(original.readlink())
            else:
                shutil.copy2(original, destination)
        patch = REPO / "vm/patches/virtualbuddy-linux-entropy.patch"
        subprocess.run(["patch", "-p1", "-i", str(patch)], cwd=overlay, check=True, capture_output=True)
        derived = output / "derived-data"
        command = ["xcodebuild", "-project", str(overlay / "VirtualBuddy.xcodeproj"),
                   "-scheme", "VirtualBuddy", "-configuration", "Release",
                   "-derivedDataPath", str(derived), "-onlyUsePackageVersionsFromResolvedFile",
                   "CODE_SIGNING_ALLOWED=NO", "CODE_SIGNING_REQUIRED=NO", "CODE_SIGN_IDENTITY=",
                   "DEVELOPMENT_TEAM=", "VB_BUNDLE_ID_PREFIX=org.blauos.dev.",
                   "OTHER_SWIFT_FLAGS=", "build"]
        result = {"schema_version": 1, "status": "prepared", "source_revision": revision,
                  "source_lock_sha256": digest(lock),
                  "patch_sha256": digest(patch), "command": command,
                  "source_overlay": str(overlay), "app_bundle_id_prefix": "org.blauos.dev.",
                  "automatic_updates_enabled": False, "app_launch_verified": False,
                  "guest_entropy_verified": False, "installed_app_modified": False}
        (output / "build.json").write_text(json.dumps(result, indent=2) + "\n")
        if not args.prepare_only:
            with (output / "build.log").open("w") as log:
                log.write(json.dumps(command) + "\n"); log.flush()
                code = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT).returncode
            result["exit_code"] = code
            result["status"] = "blocked" if code else "built"
            (output / "build.json").write_text(json.dumps(result, indent=2) + "\n")
            if not code:
                built = derived / "Build/Products/Release/VirtualBuddy.app"
                app = output / "VirtualBuddy-BlauOS.app"
                shutil.copytree(built, app, symlinks=True)
                info_path = app / "Contents/Info.plist"
                with info_path.open("rb") as stream:
                    info = plistlib.load(stream)
                info["CFBundleDisplayName"] = "VirtualBuddy BlauOS Development"
                with info_path.open("wb") as stream:
                    plistlib.dump(info, stream)
                # Sign nested code first with local ad-hoc signatures. The main
                # app receives only upstream public virtualization/audio rights.
                nested = [path for path in app.rglob("*") if not path.is_symlink()
                          and path.is_dir() and path.suffix in {".framework", ".xpc", ".app"}]
                for path in sorted(nested, key=lambda item: len(item.parts), reverse=True):
                    subprocess.run(["codesign", "--force", "--sign", "-", "--timestamp=none", str(path)],
                                   check=True, capture_output=True)
                entitlements = overlay / "VirtualBuddy/Config/Entitlements/VirtualBuddy.entitlements"
                subprocess.run(["codesign", "--force", "--sign", "-", "--timestamp=none",
                                "--entitlements", str(entitlements), str(app)], check=True, capture_output=True)
                subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)],
                               check=True, capture_output=True)
                result.update(app=str(app), signed="local-ad-hoc", status="pass",
                              executable_sha256=digest(app / "Contents/MacOS/VirtualBuddy"),
                              signing_entitlements_sha256=digest(entitlements))
        (output / "build.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({"status": result["status"], "output": str(output), "app": result.get("app")}, indent=2))
        return 0 if result["status"] in {"prepared", "pass"} else 1
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"VirtualBuddy entropy build: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
