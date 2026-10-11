#!/usr/bin/env python3
"""Build the normal command-line toolkit as an additive independent-runtime layer."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

from install_commands import command_artifacts
from ssh_workflow import digest, write_json

ROOT = Path(__file__).resolve().parents[2]


def build(args):
    if args.output.exists() or args.output.is_symlink():
        raise ValueError("Use a fresh command build output directory")
    for input_path in (args.source, args.file_source, args.libutil_source, args.shell_source,
                       args.text_source, args.runtime_root):
        if args.output.resolve().is_relative_to(input_path.resolve()):
            raise ValueError("Build output must be outside source and runtime inputs")
    spec = importlib.util.spec_from_file_location("blauos_source_verifier", ROOT / "scripts/blauos.py")
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    pins = {entry["name"]: entry for entry in verifier.load_manifest(ROOT / "sources.lock.json")["sources"]}
    sources = (("puredarwin-vm-next", args.source), ("apple-file_cmds", args.file_source),
               ("apple-libutil", args.libutil_source), ("apple-shell-cmds", args.shell_source),
               ("apple-text-commands", args.text_source))
    for name, source in sources:
        verifier.verify_source(pins[name], source.absolute())
    args.output.mkdir(parents=True)
    layers = {
        "file": ["--source", args.file_source, "--libutil-source", args.libutil_source,
                 "--darwin-source", args.source],
        "text": ["--source", args.source, "--apple-source", args.text_source],
        "admin": ["--source", args.shell_source, "--darwin-source", args.source],
    }

    def compile_layer(item):
        name, inputs = item
        output = args.output / name
        command = [sys.executable, ROOT / f"scripts/vm/build_{name}_commands.py", *inputs,
                   "--runtime-root", args.runtime_root, "--output", output]
        with (args.output / (name + ".log")).open("w") as log:
            code = subprocess.run(list(map(str, command)), stdout=log, stderr=subprocess.STDOUT,
                                  timeout=900).returncode
        return {"name": name, "exit_code": code, "argv": list(map(str, command))}

    with ThreadPoolExecutor(max_workers=3) as executor:
        receipts = list(executor.map(compile_layer, layers.items()))
    failures = [entry for entry in receipts if entry["exit_code"]]
    write_json(args.output / "build.json", {"schema_version": 1, "built": not failures, "layers": receipts,
                                          "guest_execution_verified": False, "stock_runtime_used": False})
    if failures:
        raise ValueError("Command source build failed: " + ", ".join(entry["name"] for entry in failures))
    for name, source in sources:
        verifier.verify_source(pins[name], source.absolute())
    root = args.output / "root"
    root.mkdir()
    artifacts, names = [], set()
    for name in layers:
        layer = args.output / name
        for basename, entry in command_artifacts(layer / "root", layer / "provenance.json").items():
            if basename in names:
                raise ValueError("Command layer collision: " + basename)
            names.add(basename)
            destination = root / entry["path"].lstrip("/")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(entry["source"], destination)
        artifacts.extend(json.loads((layer / "provenance.json").read_text())["artifacts"])
    provenance = {"schema_version": 1, "artifacts": artifacts, "built": True,
                  "source_lock_sha256": digest(ROOT / "sources.lock.json"),
                  "stock_runtime_used": False, "guest_execution_verified": False}
    write_json(args.output / "provenance.json", provenance)
    write_json(args.output / "catalogue.json", {"schema_version": 1, "commands": sorted(names),
                                              "provenance_sha256": digest(args.output / "provenance.json")})
    notices = []
    for name in layers:
        source = args.output / name / "third-party-notices.txt"
        if source.is_file():
            notices.append(source.read_text())
    if notices:
        bases = dict(sources)
        seen = set()
        for entry in artifacts:
            for key in ("upstream_source_files", "source_inputs", "additional_source_files"):
                for item in entry.get(key, []):
                    source_name = item.get("source", entry["source"])
                    relative = item["path"]
                    identity = (source_name, relative)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    text = (bases[source_name] / relative).read_text()
                    prefix = re.match(r"\s*(?:(?:/\*.*?\*/)|(?://[^\n]*\n)|\s)+", text, re.DOTALL)
                    if prefix:
                        notices.append(f"{source_name}: {relative}\n" + prefix.group().strip())
        for source_name, base in sources:
            for name in ("APPLE_LICENSE", "LICENSE", "COPYING"):
                source = base / name
                if source.is_file():
                    notices.append(f"{source_name}: {name}\n" + source.read_text())
        (args.output / "third-party-notices.txt").write_text("\n\n".join(notices))
    return {"built": True, "count": len(names), "commands": sorted(names), "output": str(args.output)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name, source in (("source", "puredarwin-vm-next"), ("file-source", "apple-file_cmds"),
                         ("libutil-source", "apple-libutil"), ("shell-source", "apple-shell-cmds"),
                         ("text-source", "apple-text-commands")):
        parser.add_argument("--" + name, type=Path, default=ROOT / ".blauos/sources" / source)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(build(args), indent=2))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
