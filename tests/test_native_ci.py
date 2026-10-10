import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('native_ci', ROOT / 'scripts/vm/native_ci.py')
native_ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native_ci)


class NativePipelineTests(unittest.TestCase):
    def test_inherited_sdk_and_compiler_overrides_do_not_reach_builds(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {
            'SDKROOT': '/different/sdk', 'CC': '/different/compiler',
            'DEVELOPER_DIR': '/different/xcode',
            'CXXFLAGS': '-I/unrecorded/headers', 'BLAUOS_TEST_SENTINEL': 'preserved',
        }):
            pipeline = native_ci.Pipeline(Path(temporary) / 'clean')
            raw = pipeline.run('environment', [sys.executable, '-c',
                'import json,os; print(json.dumps({k:os.environ.get(k) for k in '
                '["SDKROOT","CC","DEVELOPER_DIR","CXXFLAGS","BLAUOS_TEST_SENTINEL"]}))'])
            self.assertEqual(json.loads(raw), {'SDKROOT': None, 'CC': None,
                                              'DEVELOPER_DIR': None, 'CXXFLAGS': None,
                                              'BLAUOS_TEST_SENTINEL': 'preserved'})

    def test_termination_stops_active_child_and_retains_failed_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'clean'
            pid_file = Path(temporary) / 'child.pid'
            child = ('import os,time; from pathlib import Path; '
                     f'Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(60)')
            runner_code = (f'import importlib.util,signal; from pathlib import Path; '
                           f's=importlib.util.spec_from_file_location("ci",{str(ROOT / "scripts/vm/native_ci.py")!r}); '
                           'm=importlib.util.module_from_spec(s); s.loader.exec_module(m); '
                           f'p=m.Pipeline(Path({str(output)!r})); signal.signal(signal.SIGTERM,m.interrupt_pipeline)\n'
                           f'try: p.run("interrupted",{[sys.executable, "-c", child]!r})\n'
                           'except RuntimeError as error:\n'
                           ' assert "SIGTERM" in str(error)\n'
                           'else: raise RuntimeError("Termination was accepted as success")\n')
            runner = subprocess.Popen([sys.executable, '-c', runner_code],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 5
                while not pid_file.exists() and runner.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(pid_file.exists(), 'Build child did not start')
                os.kill(runner.pid, signal.SIGTERM)
                stdout, stderr = runner.communicate(timeout=5)
                self.assertEqual(runner.returncode, 0, stdout + stderr)
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(pid_file.read_text()), 0)
                result = json.loads((output / 'result.json').read_text())
                self.assertEqual(result['status'], 'failed')
                self.assertFalse(result['steps'][0]['passed'])
                self.assertTrue((output / 'logs/interrupted.log').is_file())
            finally:
                if runner.poll() is None:
                    runner.kill()
                    runner.wait(timeout=5)
                if pid_file.exists():
                    try:
                        os.kill(int(pid_file.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_nix_diagnostics_after_json_do_not_hide_tool_outputs(self):
        outputs = [{'drvPath': '/nix/store/example-tool.drv', 'outputs': {'out': '/nix/store/example-tool'}}]
        raw = 'fetching dependencies\n' + json.dumps(outputs) + '\nthese paths will be fetched\n'
        self.assertEqual(native_ci.parse_nix_outputs(raw), outputs)

    def test_incomplete_nix_record_is_rejected(self):
        with self.assertRaises(ValueError):
            native_ci.parse_nix_outputs('[{"drvPath":"/nix/store/example.drv", "outputs":{}}]')

    def test_failed_build_remains_failed_and_keeps_log(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'clean'
            pipeline = native_ci.Pipeline(output)
            with self.assertRaises(RuntimeError):
                pipeline.run('actual-failure', [sys.executable, '-c', 'print("compiler failure"); raise SystemExit(7)'])
            result = json.loads((output / 'result.json').read_text())
            self.assertEqual(result['status'], 'failed')
            self.assertFalse(result['guest_boot_passed'])
            self.assertFalse(result['steps'][0]['passed'])
            self.assertEqual(result['steps'][0]['exit_code'], 7)
            self.assertEqual(result['steps'][0]['log_sha256'], native_ci.digest(output / 'logs/actual-failure.log'))

    def test_timeout_cannot_count_as_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'clean'
            pipeline = native_ci.Pipeline(output)
            with self.assertRaises(subprocess.TimeoutExpired):
                pipeline.run('deadline', [sys.executable, '-c', 'import time; time.sleep(60)'], timeout=0.05)
            result = json.loads((output / 'result.json').read_text())
            self.assertFalse(result['steps'][0]['passed'])
            self.assertFalse(result['guest_boot_passed'])

    def test_rejects_existing_build_without_changing_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            evidence = output / 'result.json'
            evidence.write_text('previous evidence\n')
            with self.assertRaises(ValueError):
                native_ci.Pipeline(output)
            self.assertEqual(evidence.read_text(), 'previous evidence\n')


if __name__ == '__main__':
    unittest.main()
