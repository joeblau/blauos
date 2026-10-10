#!/usr/bin/env python3
"""Generate dash configure files from locked source using explicit build tools."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--autoreconf", default="autoreconf")
    args = parser.parse_args()
    args.source = args.source.resolve()
    args.output = args.output.resolve()
    try:
        if args.output.exists():
            raise ValueError("Use a fresh output directory")
        lock = json.loads((ROOT / "sources.lock.json").read_text())
        revision = next(item["revision"] for item in lock["sources"] if item["name"] == "dash")
        actual = subprocess.check_output(["git", "-C", str(args.source), "rev-parse", "HEAD"], text=True).strip()
        if actual != revision or subprocess.check_output(["git", "-C", str(args.source), "status", "--porcelain"]):
            raise ValueError("dash checkout must be pristine and match sources.lock.json")
        args.output.mkdir(parents=True)
        archive = args.output / "source.tar"
        with archive.open("wb") as stream:
            subprocess.run(["git", "-C", str(args.source), "archive", "HEAD"], stdout=stream, check=True)
        copied = args.output / "source"
        copied.mkdir()
        with tarfile.open(archive) as stream:
            stream.extractall(copied, filter="data")
        command = [args.autoreconf, "-fiv"]
        with (args.output / "prepare.log").open("w") as log:
            subprocess.run(command, cwd=copied, stdout=log, stderr=subprocess.STDOUT, check=True)
        inputs = {"schema_version": 1, "source": "dash", "revision": revision,
                  "source_archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                  "configure_sha256": hashlib.sha256((copied / "configure").read_bytes()).hexdigest(),
                  "command": command, "autoreconf_version": subprocess.check_output([args.autoreconf, "--version"], text=True).splitlines()[0],
                  "autoconf_version": subprocess.check_output(["autoconf", "--version"], text=True).splitlines()[0],
                  "automake_version": subprocess.check_output(["automake", "--version"], text=True).splitlines()[0],
                  "guest_execution_verified": False}
        (args.output / "prepare.json").write_text(json.dumps(inputs, indent=2) + "\n")
        print(json.dumps(inputs, indent=2))
        return 0
    except (OSError, ValueError, StopIteration, tarfile.TarError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
