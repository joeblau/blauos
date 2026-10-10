#!/usr/bin/env python3
"""Prepare an isolated build workspace or package an already-owned local SDK."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
from blauos import ROOT, atomic_json, digest, doctor

def sdk_archive(args):
    if not re.fullmatch(r'[0-9]+(?:\.[0-9]+)*', args.version):
        raise ValueError('SDK version must be a numeric version such as 27.2')
    source = Path(args.sdk_path).resolve()
    if not (source / 'SDKSettings.json').is_file():
        raise ValueError('Expected a complete Apple macOS SDK with SDKSettings.json')
    target = Path(args.output).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise ValueError(f'Output already exists, preserved: {target}')
    uncompressed = target.with_suffix('')
    if uncompressed.exists():
        raise ValueError(f'Temporary archive already exists, preserved: {uncompressed}')
    def normalize(info):
        info.uid = info.gid = info.mtime = 0
        info.uname = info.gname = ''
        return info
    with tarfile.open(uncompressed, 'w', format=tarfile.PAX_FORMAT) as archive:
        archive.add(source, arcname=f'MacOSX{args.version}.sdk', filter=normalize)
    subprocess.run(['xz', '-T4', '-1', str(uncompressed)], check=True)
    generated = Path(str(uncompressed) + '.xz')
    if generated != target:
        generated.rename(target)
    report = {'schema_version': 1, 'name': target.name, 'version': args.version,
              'sha256': digest(target), 'root_directory': f'MacOSX{args.version}.sdk',
              'redistributable': False, 'purpose': 'Locally owned build-host SDK; not target runtime libraries'}
    atomic_json(str(target) + '.json', report)
    print(json.dumps(report, indent=2))

def bootstrap(args):
    # Doctor checks the pinned host before allocating any build workspace.
    doctor(args)
    workspace = Path(args.workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    for name in ['sources', 'build/kernel', 'build/runtime', 'build/vm', 'reports', 'inputs', 'worktrees']:
        (workspace / name).mkdir(parents=True, exist_ok=True)
    probe_dir = workspace / 'bootstrap'
    probe_dir.mkdir(exist_ok=True)
    source = probe_dir / 'host-compiler-probe.c'
    source.write_text('#if !defined(__aarch64__)\n#error ARM64 compiler required\n#endif\nint blauos_host_probe(void) { return 0; }\n')
    command = ['xcrun', '--sdk', 'macosx', 'clang', '-arch', 'arm64', '-Wall', '-Werror', '-c', str(source), '-o', str(probe_dir / 'host-compiler-probe.o')]
    subprocess.run(command, check=True)
    report = {'status': 'pass', 'command': command, 'artifact_sha256': digest(probe_dir / 'host-compiler-probe.o'),
              'scope': 'host compiler/preflight only; no XNU or independent userspace acceptance'}
    atomic_json(args.bootstrap_report, report)
    print(json.dumps(report, indent=2))

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    host = sub.add_parser('host')
    host.add_argument('--workspace', default=str(ROOT / '.blauos'))
    host.add_argument('--toolchain-lock', default=str(ROOT / 'toolchain.lock.json'))
    host.add_argument('--report', default=str(ROOT / '.blauos/reports/host.json'))
    host.add_argument('--bootstrap-report', default=str(ROOT / '.blauos/reports/bootstrap.json'))
    host.add_argument('--verify-sdk', action='store_true')
    sdk = sub.add_parser('sdk-archive')
    sdk.add_argument('--sdk-path', required=True)
    sdk.add_argument('--version', required=True)
    sdk.add_argument('--output', required=True)
    args = parser.parse_args()
    if args.command == 'sdk-archive':
        sdk_archive(args)
    else:
        bootstrap(args)

if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f'error: {error}', file=sys.stderr)
        raise SystemExit(1)
