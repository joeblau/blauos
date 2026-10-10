#!/usr/bin/env python3
"""Build the source libc++ provider used by ICU, without a host C++ runtime."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]
SOURCES = '''algorithm any atomic barrier bind call_once charconv chrono condition_variable
condition_variable_destructor error_category exception expected filesystem/directory_entry
filesystem/directory_iterator filesystem/filesystem_clock filesystem/filesystem_error
filesystem/operations filesystem/path fstream functional future hash ios ios.instantiations
iostream locale memory memory_resource mutex mutex_destructor new new_handler new_helpers
optional ostream print random random_shuffle regex shared_mutex stdexcept string strstream
system_error thread typeinfo valarray variant vector verbose_abort ryu/d2fixed ryu/d2s ryu/f2s'''.split()

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT / '.blauos/build/runtime/source-overlay')
    parser.add_argument('--root', type=Path, default=ROOT / '.blauos/build/runtime/root')
    parser.add_argument('--output-dir', type=Path, default=ROOT / '.blauos/build/runtime/libcxx')
    parser.add_argument('--evidence', type=Path, default=ROOT / 'evidence/runtime/libcxx')
    parser.add_argument('--jobs', type=int, default=4)
    parser.add_argument('--sdk', type=Path)
    parser.add_argument('--compile-only', action='store_true')
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error('--jobs must be positive')
    source, root, output = args.source.resolve(), args.root.resolve(), args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    args.evidence.mkdir(parents=True, exist_ok=True)
    pin = next(s['revision'] for s in json.loads((ROOT / 'sources.lock.json').read_text())['sources'] if s['name'] == 'puredarwin-vm-next')
    revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    if revision != pin:
        raise ValueError('libc++ source does not match lock')
    diff = subprocess.check_output(['git', '-C', str(source), 'diff', '--binary'])
    sdk = args.sdk.resolve() if args.sdk else Path(subprocess.check_output(['xcrun', '--sdk', 'macosx', '--show-sdk-path'], text=True).strip())
    libraries = source / 'src/Libraries'
    abi, cxx = libraries / 'libcxxabi', libraries / 'libcxx'
    common = ['xcrun', 'clang', '-arch', 'arm64', '-mmacosx-version-min=11.0', '-isysroot', str(sdk),
              '-std=c++23', '-nostdinc++', '-funwind-tables', '-fexceptions', '-fPIC', '-Os', '-DNDEBUG',
              '-include', str(ROOT / 'runtime/compat/sdk27_compile.h'),
              '-D_LIBCPP_BUILDING_LIBRARY', '-D_LIBCXXABI_BUILDING_LIBRARY']
    for path in [abi / 'config', abi / 'include', cxx / 'src', cxx / 'include', libraries / 'llvm-libc', libraries / 'llvm-libc/include']:
        common += ['-I', str(path)]
    snapshot = {'source_revision': pin, 'source_diff_sha256': hashlib.sha256(diff).hexdigest(),
                'compiler': subprocess.check_output(['xcrun', 'clang', '--version'], text=True).strip(),
                'sdk_settings_sha256': digest(sdk / 'SDKSettings.json'), 'compile_flags': common,
                'annotation_sha256': digest(ROOT / 'runtime/compat/sdk27_compile.h')}
    result = {'schema_version': 1, 'status': 'blocked', 'inputs': snapshot, 'steps': [], 'errors': [],
              'independent_execution_verified': False}
    cache = output / 'compile-inputs.json'
    snapshot_cache = json.loads(cache.read_text()) if cache.is_file() else {}
    reusable = snapshot_cache.get('inputs') == snapshot
    def compile_one(name):
        path = cxx / 'src' / (name + '.cpp')
        obj = output / (name.replace('/', '_') + '.o')
        log = output / (name.replace('/', '_') + '.log')
        command = common + (['-D_LIBCPP_DISABLE_NEW_DELETE_DEFINITIONS'] if name == 'new' else []) + ['-c', str(path), '-o', str(obj)]
        object_hashes = snapshot_cache.get('objects', {})
        if reusable and obj.is_file() and digest(obj) == object_hashes.get(name):
            return {'name': name, 'command': command, 'exit_code': 0, 'cache_reused': True,
                    'source_sha256': digest(path), 'object_sha256': digest(obj)}
        with log.open('w') as stream:
            code = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT).returncode
        record = {'name': name, 'command': command, 'exit_code': code, 'log': str(log), 'source_sha256': digest(path)}
        if code:
            (args.evidence / (name.replace('/', '_') + '.log')).write_text('\n'.join(log.read_text().splitlines()[-60:]) + '\n')
        else:
            record['object_sha256'] = digest(obj)
        return record
    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        result['steps'] = list(executor.map(compile_one, SOURCES))
    result['errors'] = [f"compile failed: {item['name']}" for item in result['steps'] if item['exit_code']]
    if not result['errors']:
        cache.write_text(json.dumps({'inputs': snapshot, 'objects': {
            item['name']: item['object_sha256'] for item in result['steps']}}, indent=2) + '\n')
        result['compilation_passed'] = True
        if not args.compile_only:
            for path in [root / 'usr/lib/libSystem.B.dylib', root / 'usr/lib/system/libdyld.dylib', root / 'usr/lib/libc++abi.dylib']:
                if not path.is_file():
                    result['errors'].append(f'source-built link input missing: {path}')
            if not result['errors']:
                result['link_inputs'] = [{'path': str(path.relative_to(root)), 'sha256': digest(path)}
                                        for path in [root / 'usr/lib/libSystem.B.dylib',
                                                     root / 'usr/lib/system/libdyld.dylib',
                                                     root / 'usr/lib/libc++abi.dylib']]
                binary = output / 'libc++.1.dylib'
                command = ['xcrun', 'clang', '-arch', 'arm64', '-isysroot', str(sdk), '-dynamiclib', '-nostdlib',
                           '-Wl,-Z',
                           '-L', str(root / 'usr/lib'), '-Wl,-platform_version,macos,11.0,11.5',
                           '-Wl,-install_name,/usr/lib/libc++.1.dylib', '-Wl,-fixup_chains',
                           '-Wl,-not_for_dyld_shared_cache', '-Wl,-reexport-lc++abi', '-lSystem',
                           f'-Wl,-dylib_file,/usr/lib/system/libdyld.dylib:{root}/usr/lib/system/libdyld.dylib',
                           *[str(output / (name.replace('/', '_') + '.o')) for name in SOURCES], '-o', str(binary)]
                log = output / 'link.log'
                with log.open('w') as stream:
                    code = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT).returncode
                result['steps'].append({'name': 'link', 'command': command, 'exit_code': code, 'log': str(log)})
                (args.evidence / 'link.log').write_text(log.read_text())
                if code:
                    result['errors'].append('libc++ source link failed')
                else:
                    destination = root / 'usr/lib/libc++.1.dylib'
                    shutil.copy2(binary, destination)
                    alias = root / 'usr/lib/libc++.dylib'
                    if not alias.exists():
                        alias.symlink_to('libc++.1.dylib')
                    result['artifact'] = {'path': '/usr/lib/libc++.1.dylib', 'sha256': digest(destination),
                                          'source': 'puredarwin-vm-next', 'revision': pin,
                                          'source_diff_sha256': snapshot['source_diff_sha256'],
                                          'build_amendments': [{'path': str(path.relative_to(ROOT)),
                                                               'sha256': digest(path)} for path in
                                                              [ROOT / 'scripts/build/libcxx.py',
                                                               ROOT / 'runtime/compat/sdk27_compile.h']],
                                          'link_inputs': result['link_inputs']}
                    (args.evidence / 'provenance.json').write_text(json.dumps({'schema_version': 1, 'artifacts': [result['artifact']]}, indent=2) + '\n')
        result['status'] = 'compiled' if args.compile_only else ('built' if not result['errors'] else 'blocked')
    (args.evidence / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({key: result.get(key) for key in ['status', 'compilation_passed', 'errors', 'artifact']}, indent=2))
    return 0 if not result['errors'] else 1

if __name__ == '__main__':
    raise SystemExit(main())
