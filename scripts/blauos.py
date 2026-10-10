#!/usr/bin/env python3
"""Validate and retrieve explicitly pinned build inputs without changing the host."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]

def run(args, cwd=None):
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(f"{' '.join(map(str, args))}: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()

def digest(path):
    value = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()

def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', dir=path.parent, delete=False) as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        temporary = Path(stream.name)
    temporary.replace(path)

def load_manifest(path):
    data = json.loads(Path(path).read_text())
    if data.get('schema_version') != 1 or not isinstance(data.get('sources'), list):
        raise ValueError('Expected schema_version 1 and a sources array')
    names = set()
    for source in data['sources']:
        name = source.get('name', '')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', name) or name.lower() in names:
            raise ValueError(f'Unsafe or duplicate source name: {name!r}')
        names.add(name.lower())
        if not re.fullmatch(r'[0-9a-f]{40}', source.get('revision', '')):
            raise ValueError(f'{name}: require a full immutable Git commit')
        parsed_url = urlsplit(source.get('url', ''))
        if parsed_url.scheme != 'https' or not parsed_url.hostname or parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment:
            raise ValueError(f'{name}: require an explicit HTTPS source URL without credentials, query or fragment')
        if not source.get('license') or not source.get('role'):
            raise ValueError(f'{name}: license and role are required')
        if source.get('tree') and not re.fullmatch(r'[0-9a-f]{40}', source['tree']):
            raise ValueError(f'{name}: invalid Git tree identity')
        for prefix in source.get('sparse_paths', []):
            if not prefix or Path(prefix).is_absolute() or '..' in Path(prefix).parts:
                raise ValueError(f'{name}: unsafe sparse-checkout path {prefix!r}')
    for name, profile in data.get('profiles', {}).items():
        unknown = {n.lower() for n in profile.get('sources', [])} - names
        if unknown:
            raise ValueError(f'{name}: unknown sources {sorted(unknown)}')
    archive_names, filenames = set(), set()
    for archive in data.get('archives', []):
        name, filename = archive.get('name', ''), archive.get('filename', '')
        for label, value, seen in [('archive name', name, archive_names), ('archive filename', filename, filenames)]:
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', value) or value.lower() in seen:
                raise ValueError(f'Unsafe or duplicate {label}: {value!r}')
            seen.add(value.lower())
        if not archive.get('url', '').startswith('https://github.com/'):
            raise ValueError(f'{name}: require an explicit HTTPS GitHub archive URL')
        if not re.fullmatch(r'[0-9a-f]{64}', archive.get('sha256', '')):
            raise ValueError(f'{name}: require an immutable archive SHA-256')
        if not archive.get('license') or not archive.get('role'):
            raise ValueError(f'{name}: archive license and role are required')
    root = Path(path).resolve().parent
    for lock in data.get('transitive_locks', []):
        target = root / lock['path']
        if not target.resolve().is_relative_to(root) or target.is_symlink():
            raise ValueError(f'Unsafe transitive lock path: {lock["path"]}')
        if digest(target) != lock['sha256']:
            raise ValueError(f'Transitive lock hash differs: {lock["path"]}')
    return data

def select_sources(data, requested, profile):
    if profile:
        if profile not in data.get('profiles', {}):
            raise ValueError(f'Unknown source profile: {profile}')
        requested = data['profiles'][profile]['sources']
    wanted = {name.lower() for name in requested}
    available = {s['name'].lower() for s in data['sources']}
    if wanted - available:
        raise ValueError(f'Unknown sources: {sorted(wanted - available)}')
    return [s for s in data['sources'] if not wanted or s['name'].lower() in wanted]

def normalize_url(url):
    return url.rstrip('/').removesuffix('.git')

def verify_source(source, path):
    if path.is_symlink():
        raise ValueError(f'Refusing symlink source directory: {path}')
    if not (path / '.git').exists():
        raise ValueError(f'Not a Git checkout: {path}')
    revision = run(['git', 'rev-parse', 'HEAD'], cwd=path)
    tree = run(['git', 'rev-parse', 'HEAD^{tree}'], cwd=path)
    origin = run(['git', 'remote', 'get-url', 'origin'], cwd=path)
    if revision != source['revision']:
        raise ValueError(f"{source['name']}: HEAD {revision} differs from lock {source['revision']}; existing checkout preserved")
    if source.get('tree') and tree != source['tree']:
        raise ValueError(f"{source['name']}: tree differs from lock")
    if normalize_url(origin) != normalize_url(source['url']):
        raise ValueError(f"{source['name']}: origin differs from lock")
    if source.get('sparse_paths'):
        actual = set(run(['git', 'sparse-checkout', 'list'], cwd=path).splitlines())
        if actual != set(source['sparse_paths']):
            raise ValueError(f"{source['name']}: sparse checkout differs from locked paths")
    status = run(['git', 'status', '--porcelain', '--untracked-files=normal'], cwd=path)
    if status:
        raise ValueError(f"{source['name']}: source has local changes; preserved without reset\n{status}")
    # A Merkle tree identity covers all tracked content; SHA-256 identifies the lock itself.
    return {'name': source['name'], 'revision': revision, 'tree': tree, 'origin': origin}

def fetch(args):
    data = load_manifest(args.manifest)
    destination = Path(args.sources_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    results = []
    for source in select_sources(data, args.names, args.profile):
        path = destination / source['name']
        if not path.exists():
            temporary = Path(tempfile.mkdtemp(prefix=source['name'] + '-', dir=destination))
            run(['git', 'init', '--quiet', str(temporary)])
            run(['git', 'remote', 'add', 'origin', source['url']], cwd=temporary)
            fetch_args = ['git', 'fetch', '--quiet', '--depth=1']
            if source.get('sparse_paths'):
                run(['git', 'sparse-checkout', 'init', '--cone'], cwd=temporary)
                run(['git', 'sparse-checkout', 'set', *source['sparse_paths']], cwd=temporary)
                fetch_args.append('--filter=blob:none')
            run([*fetch_args, 'origin', source['revision']], cwd=temporary)
            run(['git', 'checkout', '--quiet', '--detach', 'FETCH_HEAD'], cwd=temporary)
            verify_source(source, temporary)
            temporary.rename(path)
        results.append(verify_source(source, path))
        print(f"Verified {source['name']} {source['revision']}", flush=True)
    report = {'schema_version': 1, 'source_lock_sha256': digest(args.manifest), 'sources': results}
    atomic_json(args.report, report)

def fingerprint_sdk(path):
    """Hash file bytes and symlink targets without following directory symlinks."""
    path = Path(path).resolve()
    aggregate = hashlib.sha256()
    count = 0
    for folder, dirs, files in os.walk(path, followlinks=False):
        for name in sorted(dirs + files):
            entry = Path(folder) / name
            rel = entry.relative_to(path).as_posix()
            if entry.is_symlink():
                value = 'link:' + os.readlink(entry)
            elif entry.is_file():
                value = 'file:' + digest(entry)
            else:
                continue
            aggregate.update((rel + '\0' + value + '\n').encode())
            count += 1
        dirs.sort()
    return {'content_sha256': aggregate.hexdigest(), 'entries': count, 'algorithm': 'sha256(relative-path NUL file-sha256-or-link-target LF; sorted walk; no symlink following)'}

def fetch_archives(args):
    data = load_manifest(args.manifest)
    archives = data.get('archives', [])
    requested = {name.lower() for name in args.names}
    unknown = requested - {entry['name'].lower() for entry in archives}
    if unknown:
        raise ValueError(f'Unknown archives: {sorted(unknown)}')
    destination = Path(args.inputs_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    results = []
    for entry in archives:
        if requested and entry['name'].lower() not in requested:
            continue
        target = destination / entry['filename']
        if target.is_symlink():
            raise ValueError(f'Existing archive symlink preserved: {target}')
        if not target.exists():
            temporary = None
            try:
                with tempfile.NamedTemporaryFile('wb', dir=destination, delete=False) as output:
                    temporary = Path(output.name)
                    with urllib.request.urlopen(entry['url'], timeout=60) as response:
                        shutil.copyfileobj(response, output)
                if digest(temporary) != entry['sha256']:
                    raise ValueError(f"{entry['name']}: downloaded archive hash differs from lock")
                temporary.rename(target)
            finally:
                if temporary and temporary.exists():
                    temporary.unlink()
        if digest(target) != entry['sha256']:
            raise ValueError(f"{entry['name']}: existing archive differs from lock; preserved")
        results.append({'name': entry['name'], 'filename': target.name, 'url': entry['url'],
                        'sha256': entry['sha256'], 'bytes': target.stat().st_size})
        print(f"Verified archive {entry['name']} {entry['sha256']}", flush=True)
    atomic_json(args.report, {'schema_version': 1, 'source_lock_sha256': digest(args.manifest), 'archives': results})

def doctor(args):
    lock = json.loads(Path(args.toolchain_lock).read_text())
    report = {'schema_version': 1, 'checks': {}, 'errors': [], 'limitations': lock.get('limitations', [])}
    commands = {
        'architecture': ['uname', '-m'],
        'os_version': ['sw_vers', '-productVersion'],
        'os_build': ['sw_vers', '-buildVersion'],
        'developer_dir': ['xcode-select', '-p'],
        'sdk_version': ['xcrun', '--sdk', 'macosx', '--show-sdk-version'],
        'sdk_path': ['xcrun', '--sdk', 'macosx', '--show-sdk-path'],
        'clang_version': ['xcrun', 'clang', '--version'],
        'git_version': ['git', '--version'],
        'python_version': ['python3', '--version'],
        'make_version': ['make', '--version'],
    }
    for name, command in commands.items():
        try:
            value = run(command)
            report['checks'][name] = value
            expected = lock.get('host', {}).get(name)
            if expected is not None and value != expected:
                report['errors'].append(f'{name}: expected {expected!r}, got {value!r}')
        except (RuntimeError, FileNotFoundError) as error:
            report['errors'].append(str(error))
    for name, tool in lock.get('tools', {}).items():
        try:
            executable = Path(run(['xcrun', '--find', name]))
            actual = digest(executable)
            report['checks'][name + '_sha256'] = actual
            if actual != tool['sha256']:
                report['errors'].append(f'{name}: executable differs from toolchain lock')
        except (RuntimeError, FileNotFoundError) as error:
            report['errors'].append(str(error))
    for name, tool in lock.get('utilities', {}).items():
        try:
            executable = shutil.which(name)
            if executable is None:
                raise FileNotFoundError(f'Required build utility is missing: {name}')
            version = run([executable, *tool['version_args']]).splitlines()[0]
            actual = digest(executable)
            report['checks'][name + '_version'] = version
            report['checks'][name + '_sha256'] = actual
            if version != tool['version'] or actual != tool['sha256']:
                report['errors'].append(f'{name}: version or executable differs from toolchain lock')
        except (RuntimeError, FileNotFoundError) as error:
            report['errors'].append(str(error))
    if args.verify_sdk and report['checks'].get('sdk_path'):
        actual = fingerprint_sdk(report['checks']['sdk_path'])
        report['checks']['sdk_fingerprint'] = actual
        if actual['content_sha256'] != lock['sdk']['content_sha256']:
            report['errors'].append('SDK content differs from toolchain lock')
    report['status'] = 'pass' if not report['errors'] else 'fail'
    atomic_json(args.report, report)
    print(json.dumps(report, indent=2))
    if report['errors']:
        raise SystemExit(1)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', default=str(ROOT / 'sources.lock.json'))
    sub = parser.add_subparsers(dest='command', required=True)
    validate = sub.add_parser('validate-lock')
    fetcher = sub.add_parser('fetch')
    fetcher.add_argument('--manifest', default=argparse.SUPPRESS)
    fetcher.add_argument('names', nargs='*')
    fetcher.add_argument('--profile')
    fetcher.add_argument('--sources-dir', default=str(ROOT / '.blauos/sources'))
    fetcher.add_argument('--report', default=str(ROOT / '.blauos/reports/sources.json'))
    archive_fetcher = sub.add_parser('fetch-archives')
    archive_fetcher.add_argument('--manifest', default=argparse.SUPPRESS)
    archive_fetcher.add_argument('names', nargs='*')
    archive_fetcher.add_argument('--inputs-dir', default=str(ROOT / '.blauos/inputs'))
    archive_fetcher.add_argument('--report', default=str(ROOT / '.blauos/reports/archives.json'))
    host = sub.add_parser('doctor')
    host.add_argument('--toolchain-lock', default=str(ROOT / 'toolchain.lock.json'))
    host.add_argument('--report', default=str(ROOT / '.blauos/reports/host.json'))
    host.add_argument('--verify-sdk', action='store_true')
    args = parser.parse_args()
    if args.command == 'validate-lock':
        data = load_manifest(args.manifest)
        print(f"Valid lock: {len(data['sources'])} immutable sources, {len(data.get('archives', []))} archives; SHA-256 {digest(args.manifest)}")
    elif args.command == 'fetch':
        fetch(args)
    elif args.command == 'fetch-archives':
        fetch_archives(args)
    else:
        doctor(args)

if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, OSError, KeyError) as error:
        print(f'error: {error}', file=sys.stderr)
        raise SystemExit(1)
