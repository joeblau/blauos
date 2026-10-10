#!/usr/bin/env python3
"""Apply and verify the exact ordered XNU patch series in an isolated worktree."""
import argparse
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
from blauos import ROOT, clean_git_environment, load_manifest, run, verify_source

def series(directory):
    directory = Path(directory).resolve()
    result = []
    for entry in (directory / 'series').read_text().splitlines():
        entry = entry.strip()
        if not entry or entry.startswith('#'):
            continue
        path = directory / entry
        if path.is_symlink() or path.resolve().parent != directory or not path.is_file():
            raise ValueError(f'Invalid patch entry: {entry}')
        if path in result:
            raise ValueError(f'Duplicate patch entry: {entry}')
        result.append(path)
    return result

def apply_patches(worktree, patches):
    for patch in patches:
        run(['git', 'apply', '--check', '--index', str(patch)], cwd=worktree)
        run(['git', 'apply', '--index', str(patch)], cwd=worktree)

def git_diff(worktree):
    # Preserve bytes: decoding would change a patch identity for non-UTF-8 paths.
    return subprocess.check_output(['git', 'diff', '--binary', 'HEAD', '--'],
                                   cwd=worktree, env=clean_git_environment())

def prepare(source, revision, worktree, patches):
    worktree = Path(worktree).resolve()
    if worktree.exists() and any(worktree.iterdir()):
        raise ValueError(f'Existing worktree preserved: {worktree}; choose a fresh --worktree')
    worktree.parent.mkdir(parents=True, exist_ok=True)
    run(['git', 'worktree', 'add', '--quiet', '--detach', str(worktree), revision], cwd=source)
    apply_patches(worktree, patches)
    return worktree

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['check','apply','verify','export'])
    parser.add_argument('--manifest', default=str(ROOT / 'sources.lock.json'))
    parser.add_argument('--source', default=str(ROOT / '.blauos/sources/xnu'))
    parser.add_argument('--source-name', default='xnu')
    parser.add_argument('--series-dir', default=str(ROOT / 'patches/xnu'))
    parser.add_argument('--worktree', default=str(ROOT / '.blauos/worktrees/xnu'))
    parser.add_argument('--output', help='Export the exact uncommitted diff to this new file')
    args = parser.parse_args()
    source = Path(args.source)
    data = load_manifest(args.manifest)
    entry = next(s for s in data['sources'] if s['name'].lower() == args.source_name.lower())
    # Validate the pristine locked source. The explicitly patched worktree is
    # compared against the ordered series below and must not be required clean.
    verify_source(entry, source)
    source = source.resolve()
    patches = series(args.series_dir)
    if args.command == 'apply':
        worktree = prepare(source, entry['revision'], args.worktree, patches)
        print(f'Prepared {worktree} with {len(patches)} patches; build separately to establish kernel acceptance')
        return
    if args.command == 'export':
        if not args.output:
            raise ValueError('--output is required for export')
        output = Path(args.output)
        with output.open('xb') as stream:
            stream.write(git_diff(args.worktree))
        print(f'Exported exact worktree diff to {output}; split changes into independently reviewable patches')
        return
    with tempfile.TemporaryDirectory(prefix='blauos-patch-check-') as temporary:
        check_path = Path(temporary) / 'xnu'
        try:
            prepare(source, entry['revision'], check_path, patches)
            expected = git_diff(check_path)
            if args.command == 'verify':
                worktree = Path(args.worktree)
                if run(['git','rev-parse','HEAD'], cwd=worktree) != entry['revision']:
                    raise ValueError('Worktree HEAD differs from locked commit')
                if run(['git','ls-files','--others','--exclude-standard'], cwd=worktree):
                    raise ValueError('Unexpected untracked files in kernel worktree')
                if git_diff(worktree) != expected:
                    raise ValueError('Kernel worktree contains changes outside the ordered patch series')
            print(f'Verified {len(patches)} ordered patches; resulting diff SHA-256 {hashlib.sha256(expected).hexdigest()}')
        finally:
            if check_path.exists():
                run(['git','worktree','remove','--force',str(check_path)], cwd=source)

if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, OSError, StopIteration, subprocess.CalledProcessError) as error:
        print(f'error: {error}', file=sys.stderr)
        raise SystemExit(1)
