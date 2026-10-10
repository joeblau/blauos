#!/usr/bin/env python3
"""Build from fresh pinned sources on macOS, then audit and boot in Linux."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
NIX_IMAGE = 'nixos/nix@sha256:a326ac1ed46069ead5cdcba3a3a1e7255ebf72300c8319bf2c12cacfe9ab2787'
BUILD_ENV_OVERRIDES = ('CC', 'CXX', 'OBJC', 'OBJCXX', 'CFLAGS', 'CXXFLAGS',
                       'OBJCFLAGS', 'OBJCXXFLAGS', 'CPPFLAGS', 'LDFLAGS',
                       'SDKROOT', 'DEVELOPER_DIR', 'PUREDARWIN_SDKROOT', 'MACOSX_DEPLOYMENT_TARGET',
                       'CPATH', 'C_INCLUDE_PATH', 'CPLUS_INCLUDE_PATH',
                       'OBJC_INCLUDE_PATH', 'LIBRARY_PATH', 'CMAKE_TOOLCHAIN_FILE',
                       'CMAKE_PREFIX_PATH', 'CMAKE_INCLUDE_PATH', 'CMAKE_LIBRARY_PATH')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def remove_container(name):
    if not re.fullmatch(r'blauos-ci-[0-9a-f]{12}', name):
        raise ValueError('Refusing cleanup of an unrecognized container name')
    subprocess.run(['docker', 'rm', '--force', name], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=60, check=True)


def interrupt_pipeline(signum, frame):
    raise RuntimeError('CI interrupted by ' + signal.Signals(signum).name)


def parse_nix_outputs(raw):
    decoder = json.JSONDecoder()
    for position in reversed([index for index, character in enumerate(raw) if character == '[']):
        try:
            value, _ = decoder.raw_decode(raw[position:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, list) and value and all(
            isinstance(item, dict) and isinstance(item.get('drvPath'), str)
            and item['drvPath'].startswith('/nix/store/') and item['drvPath'].endswith('.drv')
            and isinstance(item.get('outputs'), dict) and item['outputs']
            and all(isinstance(path, str) and path.startswith('/nix/store/') for path in item['outputs'].values())
            for item in value
        ):
            return value
    raise ValueError('No complete Nix build-output array in tool log')


class Pipeline:
    def __init__(self, output):
        if output.exists():
            raise ValueError('Use a fresh output directory; clean-build evidence cannot reuse a previous build')
        self.output = output
        self.environment = dict(os.environ)
        removed = [key for key in BUILD_ENV_OVERRIDES if key in self.environment]
        for key in BUILD_ENV_OVERRIDES:
            self.environment.pop(key, None)
        (output / 'logs').mkdir(parents=True)
        self.result = {'schema_version': 1, 'status': 'failed', 'steps': [],
                       'source_lock_sha256': digest(ROOT / 'sources.lock.json'),
                       'toolchain_lock_sha256': digest(ROOT / 'toolchain.lock.json'),
                       'guest_boot_passed': False, 'physical_boot_gate': False}
        self.result['cleared_build_environment_overrides'] = removed
        self.save()

    def save(self):
        temporary = self.output / 'result.json.tmp'
        temporary.write_text(json.dumps(self.result, indent=2) + '\n')
        temporary.replace(self.output / 'result.json')

    def run(self, name, command, timeout=3600):
        command = [str(value) for value in command]
        log = self.output / 'logs' / (name + '.log')
        step = {'name': name, 'command': command, 'timeout_seconds': timeout,
                'log': str(log), 'passed': False}
        self.result['steps'].append(step)
        self.save()
        started = time.monotonic()
        print(name, flush=True)
        try:
            with log.open('w') as stream:
                process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT,
                                           start_new_session=True, env=self.environment)
                try:
                    process.wait(timeout=timeout)
                except BaseException:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                        process.wait(timeout=5)
                    except ProcessLookupError:
                        pass
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)
                    raise
            step['exit_code'] = process.returncode
            if process.returncode:
                raise RuntimeError(f'{name} failed with exit {process.returncode}; see {log}')
            step['passed'] = True
            return log.read_text()
        finally:
            step['elapsed_seconds'] = round(time.monotonic() - started, 3)
            if log.exists():
                step['log_sha256'] = digest(log)
            self.save()

    def python(self, name, script, *arguments, timeout=3600):
        return self.run(name, [sys.executable, ROOT / 'scripts' / script, *arguments], timeout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--jobs', type=int, default=4)
    parser.add_argument('--boot-timeout', type=int, default=300)
    parser.add_argument('--require-clean-project', action='store_true')
    parser.add_argument('--cleanup-only', action='store_true', help='Remove this run\'s recorded container after cancellation')
    args = parser.parse_args()
    if args.cleanup_only:
        result_path = args.output.resolve() / 'result.json'
        if result_path.is_file():
            name = json.loads(result_path.read_text()).get('linux_container')
            if name is not None:
                remove_container(name)
        return 0
    if args.jobs < 1 or not 30 <= args.boot_timeout <= 900:
        parser.error('Positive jobs and boot timeout between 30 and 900 seconds required')
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        parser.error('The native profile requires the pinned ARM64 macOS build host')
    output = args.output.resolve()
    pipeline = Pipeline(output)
    container = 'blauos-ci-' + uuid.uuid4().hex[:12]
    container_attempted = False
    previous_handlers = {signum: signal.signal(signum, interrupt_pipeline)
                         for signum in (signal.SIGINT, signal.SIGTERM)}
    try:
        project_status = subprocess.check_output(['git', '-C', str(ROOT), 'status', '--porcelain'], text=True).strip()
        pipeline.result['project_revision'] = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
        pipeline.result['project_dirty'] = bool(project_status)
        if args.require_clean_project and project_status:
            raise ValueError('The CI project checkout must be clean')
        pipeline.python('validate-lock', 'blauos.py', 'validate-lock', timeout=60)
        pipeline.python('host-preflight', 'blauos.py', 'doctor', '--verify-sdk', '--report', output / 'host.json', timeout=300)
        checks = json.loads((output / 'host.json').read_text())['checks']
        sdk = Path(checks['sdk_path'])
        pipeline.environment.update(SDKROOT=str(sdk), DEVELOPER_DIR=checks['developer_dir'])
        for variable, tool in (('CC', 'clang'), ('CXX', 'clang++')):
            pipeline.environment[variable] = subprocess.check_output(
                ['xcrun', '--sdk', 'macosx', '--find', tool], env=pipeline.environment, text=True).strip()
        pipeline.result['build_environment'] = {key: pipeline.environment[key]
                                               for key in ('SDKROOT', 'DEVELOPER_DIR', 'CC', 'CXX')}
        sources = output / 'sources'
        pipeline.python('fetch-sources', 'blauos.py', 'fetch', 'puredarwin-vm-next', 'llvm-project', 'kc-tools', 'xnu-loader', 'dash',
                        '--sources-dir', sources, '--report', output / 'source-inputs.json', timeout=1800)
        inputs = output / 'inputs'
        pipeline.python('fetch-archives', 'blauos.py', 'fetch-archives', 'icu4c-76.1', '--inputs-dir', inputs,
                        '--report', output / 'archive-inputs.json', timeout=900)
        kernel = output / 'kernel'
        pipeline.python('kernel-and-drivers', 'build/xnu.py', '--puredarwin', '--source-root', sources,
                        '--source-name', 'puredarwin-vm-next', '--machine', 'VIRT', '--with-kexts',
                        '--patch', ROOT / 'vm/patches/xnu-classic-arm64-protections.patch',
                        '--patch', ROOT / 'vm/patches/pdarm-qemu-psci-shutdown.patch', '--qemu-psci-hvc', '--sdk', sdk,
                        '--build-root', kernel, '--evidence', output / 'evidence/kernel', '--jobs', args.jobs, timeout=3600)
        runtime = output / 'runtime'
        runtime_root = runtime / 'root'
        pipeline.python('base-runtime', 'build/runtime.py', 'build', '--sources-dir', sources, '--output-dir', runtime,
                        '--sdk', sdk, '--jobs', args.jobs, '--output', output / 'evidence/runtime.json', timeout=3600)
        pipeline.python('objc-and-cxxabi', 'build/objc.py', '--sources-dir', sources, '--output-dir', runtime,
                        '--sdk', sdk, '--jobs', args.jobs, '--output', output / 'evidence/objc.json', timeout=1800)
        pipeline.python('libcxx', 'build/libcxx.py', '--source', runtime / 'source-overlay', '--root', runtime_root,
                        '--output-dir', runtime / 'libcxx', '--sdk', sdk,
                        '--evidence', output / 'evidence/libcxx', '--jobs', args.jobs, timeout=1800)
        provenance = runtime / 'provenance.json'
        data = json.loads(provenance.read_text())
        extra = json.loads((output / 'evidence/libcxx/provenance.json').read_text())['artifacts']
        paths = {entry['path'] for entry in extra}
        data['artifacts'] = [entry for entry in data['artifacts'] if entry['path'] not in paths] + extra
        provenance.write_text(json.dumps(data, indent=2) + '\n')
        pipeline.python('icu-cf', 'build/corefoundation.py', '--output-dir', runtime, '--sdk', sdk,
                        '--archive', inputs / 'icu4c-76_1-src.tgz', '--jobs', args.jobs,
                        '--evidence', output / 'evidence/corefoundation', timeout=3600)
        pipeline.python('iokit', 'build/iokit.py', '--output-dir', runtime, '--root', runtime_root,
                        '--sdk', sdk, '--jobs', args.jobs, '--output', output / 'evidence/iokit.json', timeout=1800)
        pipeline.python('launchd-launchctl', 'build/launchd.py', '--output-dir', runtime, '--sdk', sdk,
                        '--output', output / 'evidence/launchd.json', timeout=1800)
        pipeline.python('dns-responder', 'build/mdns.py', '--output-dir', runtime, '--sdk', sdk,
                        '--jobs', args.jobs, '--output', output / 'evidence/mdns.json', timeout=1800)
        pipeline.python('runtime-closure', 'build/runtime.py', 'inspect', '--root', runtime_root,
                        '--provenance', provenance, '--output', output / 'runtime-closure.json', timeout=300)
        # A failed/timed-out Docker CLI can still create a daemon-owned
        # container. Own cleanup before invoking the daemon, not after success.
        container_attempted = True
        pipeline.result['linux_container'] = container
        pipeline.run('linux-container', ['docker', 'run', '-d', '--rm', '--platform', 'linux/arm64', '--name', container,
                     '--mount', f'type=bind,source={ROOT},target={ROOT},readonly',
                     '--mount', f'type=bind,source={output},target={output}',
                     '--mount', 'type=volume,source=blauos-native-ci-nix-store,target=/nix',
                     NIX_IMAGE, 'sleep', 'infinity'], timeout=300)
        expr = f'''let f=builtins.getFlake {json.dumps('path:' + str(sources / 'puredarwin-vm-next'))};
          p=import f.inputs.nixpkgs {{system="aarch64-linux";}};
          in {{ loader=f.inputs.xnu-loader.packages.aarch64-linux.arm64-virt;
            qemu=p.qemu; firmware=p.pkgsCross.aarch64-multiplatform.OVMF.fd;
            python=p.python314; git=p.git; autotools=p.autoreconfHook;
            autoconf=p.autoconf; automake=p.automake; sed=p.gnused;
            mtools=p.mtools; fat=p.dosfstools; ext4=p.e2fsprogs;
            gpt=p.gptfdisk; util=p.util-linux; fakeroot=p.fakeroot; }}'''
        raw = pipeline.run('linux-tools', ['docker', 'exec', container, 'nix', '--extra-experimental-features',
                           'nix-command flakes', 'build', '--impure', '--expr', expr,
                           '--no-link', '--json', '--max-jobs', '1', '--cores', '2'], timeout=1800)
        # Nix diagnostics share the log with JSON; retain the complete log and
        # parse its final array rather than treating substituted tools as builds.
        tool_outputs = parse_nix_outputs(raw)
        (output / 'linux-tools.json').write_text(json.dumps(tool_outputs, indent=2) + '\n')
        paths = [path for entry in tool_outputs for path in entry['outputs'].values()]
        path_env = ':'.join(str(Path(path) / folder) for path in paths for folder in ('bin', 'sbin')) + ':/root/.nix-profile/bin:/bin'
        def find(fragment, key='out'):
            return Path(next(entry['outputs'][key] for entry in tool_outputs if fragment in entry['drvPath']))
        def linux(name, script, *arguments, timeout=900):
            return pipeline.run(name, ['docker', 'exec', '-e', 'PATH=' + path_env, container,
                                find('python3-') / 'bin/python3', ROOT / 'scripts' / script, *arguments], timeout)
        prepared = output / 'dash-prepared'
        linux('prepare-shell', 'vm/prepare_shell.py', '--source', sources / 'dash', '--output', prepared)
        shell = output / 'shell'
        pipeline.python('build-shell', 'vm/build_shell.py', '--source', sources / 'dash', '--prepared', prepared / 'source',
                        '--runtime-root', runtime_root, '--output', shell, timeout=900)
        commands = output / 'commands'
        pipeline.python('build-commands', 'vm/build_userspace.py', '--source', sources / 'puredarwin-vm-next',
                        '--runtime-root', runtime_root, '--output', commands, timeout=900)
        composed = output / 'composed'
        pipeline.python('compose-root', 'vm/compose_root.py', '--layer', runtime_root, provenance,
                        '--layer', commands / 'root', commands / 'provenance.json',
                        '--layer', shell / 'root', shell / 'provenance.json', '--output', composed, timeout=300)
        spec = importlib.util.spec_from_file_location('collection', ROOT / 'scripts/vm/build_collection.py')
        collection = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(collection)
        kext_root = kernel / 'src/Kernel/Extensions'
        bundles = [collection.bundle_evidence(kext_root / name / (name + '.kext')) for name in collection.ORDER]
        revision = next(entry['revision'] for entry in json.loads((ROOT / 'sources.lock.json').read_text())['sources']
                        if entry['name'] == 'puredarwin-vm-next')
        inventory = output / 'kext-inventory.json'
        inventory.write_text(json.dumps({'source_revision': revision, 'bundles': bundles}, indent=2) + '\n')
        symbols = kernel / 'src/Kernel/xnu/xnu_sym/kernel.development.virt'
        cache = output / 'collection'
        pipeline.python('kernel-collection', 'vm/build_collection.py', '--source', sources / 'kc-tools',
                        '--kernel', symbols, '--kernel-sha256', digest(symbols), '--inventory', inventory,
                        '--codeless', kernel / 'src/Kernel/xnu/xnu/System/Library/Extensions/System.kext/PlugIns',
                        '--output', cache, timeout=900)
        relocation_arguments = []
        for bundle in bundles:
            relocation_arguments += ['--bundle', bundle['path']]
        pipeline.python('verify-kernel-relocations', 'build/verify_kext_relocations.py',
                        '--collection', cache / 'kernel.classic', '--kernel', symbols,
                        *relocation_arguments, '--output', output / 'evidence/kernel-relocations.json', timeout=300)
        image = output / 'image'
        loader = find('xnu-loader-') / 'xnu-loader.efi'
        firmware = find('OVMF-', 'fd') / 'FV'
        qemu = find('qemu-') / 'bin/qemu-system-aarch64'
        linux('assemble-image', 'vm/assemble.py', '--root', composed / 'root', '--provenance', composed / 'provenance.json',
              '--kernel', cache / 'kernel.classic', '--loader', loader, '--output', image, timeout=900)
        artifacts = output / 'artifacts'
        common = ['--image', image / 'blauos-arm64.img', '--firmware', firmware / 'AAVMF_CODE.fd',
                  '--vars-template', firmware / 'AAVMF_VARS.fd', '--symbols', symbols, '--qemu', qemu]
        linux('final-audit', 'vm/finalize.py', *common, '--root', image / 'root-staging',
              '--provenance', composed / 'provenance.json', '--output', artifacts, timeout=300)
        linux('boot-and-network', 'vm/smoke.py', *common, '--manifest', artifacts / 'image-manifest.json',
              '--machine', 'virt-10.2', '--memory', '1024', '--timeout', args.boot_timeout,
              '--require-shutdown', '--shutdown-timeout', '60',
              '--output', output / 'boot', timeout=args.boot_timeout + 180)
        smoke = json.loads((output / 'boot/result.json').read_text())
        if smoke.get('passed') is not True or smoke.get('guest_shutdown_passed') is not True:
            raise ValueError('The real guest smoke result did not pass')
        pipeline.result.update(status='passed', guest_boot_passed=True)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, KeyError, StopIteration) as error:
        pipeline.result['failure'] = str(error)
    finally:
        if container_attempted:
            try:
                # Forced removal also succeeds when --rm already removed it.
                remove_container(container)
            except (OSError, subprocess.SubprocessError) as error:
                pipeline.result['container_cleanup_failure'] = str(error)
                pipeline.result['status'] = 'failed'
        pipeline.save()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    print(json.dumps(pipeline.result, indent=2))
    return 0 if pipeline.result['status'] == 'passed' else 1


if __name__ == '__main__':
    sys.exit(main())
