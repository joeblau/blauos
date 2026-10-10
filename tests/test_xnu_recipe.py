"""Build-input guards and synthetic orchestration; no native execution proof."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/build"))
spec = importlib.util.spec_from_file_location("xnu_recipe_tests", ROOT / "scripts/build/xnu.py")
xnu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(xnu)


class XnuRecipeTests(unittest.TestCase):
    def test_reviewed_platform_and_warning_flags_remain_available(self):
        self.assertEqual(xnu.reviewed_make_settings([
            "PLATFORM=MacOSX", "WARNFLAGS_STD=-Wall  -Wextra -Wno-missing-include-dirs"
        ]), {"PLATFORM": "MacOSX", "WARNFLAGS_STD": "-Wall -Wextra -Wno-missing-include-dirs"})

    def test_source_profile_sdk_output_and_toolchain_overrides_rejected(self):
        protected = ("SRCROOT", "SOURCE", "MACHINE_CONFIGS", "CURRENT_MACHINE_CONFIG", "ARCH_CONFIGS",
                     "ARCH_STRING_FOR_CURRENT_MACHINE_CONFIG", "KERNEL_CONFIGS", "SDKROOT", "SDKROOT_RESOLVED",
                     "HOST_SDKROOT", "SDKVERSION", "DEVELOPER_DIR", "CC", "CXX", "HOST_CC", "MIG", "XCRUN",
                     "CFLAGS", "CPPFLAGS", "CXXFLAGS", "LDFLAGS", "OBJROOT", "SYMROOT", "DSTROOT",
                     "KDKROOT", "MAKEFLAGS", "MAKEFILES", "RC_DARWIN_KERNEL_VERSION", "PLATFORMPATH")
        for name in protected:
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "reserved"):
                xnu.reviewed_make_settings([name + "=untrusted"])

    def test_warning_option_cannot_inject_macros_commands_or_make_expansion(self):
        for value in ("", "-Wall -DARM64_BOARD_CONFIG_VIRT", "-Wall -I/private/other-source",
                      "-Wl,-arch,arm64", "-Wp,-Dfake", "$(shell touch /tmp/never-execute)",
                      "`touch /tmp/never-execute`", "-Wall;touch /tmp/never-execute", "-Wall @args.rsp"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                xnu.reviewed_make_settings(["WARNFLAGS_STD=" + value])

    def test_duplicate_or_different_platform_rejected(self):
        for settings in (["PLATFORM=MacOSX", "PLATFORM=MacOSX"], ["PLATFORM=iPhoneOS"], ["WARNFLAGS_STD"]):
            with self.assertRaises(ValueError): xnu.reviewed_make_settings(settings)

    def test_ignored_puredarwin_make_settings_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "does not apply"):
            xnu.reviewed_make_settings(["PLATFORM=MacOSX"], puredarwin=True)

    def test_ignored_puredarwin_version_and_make_expansion_rejected_before_build(self):
        with tempfile.TemporaryDirectory() as directory:
            for arguments in (['--puredarwin', '--darwin-version', '25.5.0'],
                              ['--darwin-version', '$(shell false)']):
                process = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/build/xnu.py'),
                    '--manifest', str(Path(directory) / 'missing-lock.json'), *arguments],
                    capture_output=True, text=True)
                self.assertEqual(process.returncode, 1)
                self.assertIn('--darwin-version', process.stderr)
                self.assertNotIn('missing-lock.json', process.stderr)

    def test_cli_rejects_override_before_source_lookup_or_build(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "must-not-exist"
            for setting in ("MACHINE_CONFIGS=VIRT", "SRCROOT=" + directory,
                            "WARNFLAGS_STD=$(shell touch " + str(marker) + ")"):
                process = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/build/xnu.py"),
                    "--manifest", str(Path(directory) / "missing-lock.json"), "--make-setting", setting],
                    capture_output=True, text=True)
                self.assertEqual(process.returncode, 1)
                self.assertNotIn("missing-lock", process.stderr)
                self.assertFalse(marker.exists())

    def test_inherited_source_flags_and_make_injections_removed(self):
        allowed = {"PATH": "/usr/bin:/bin", "HOME": "/tmp/synthetic-home", "DEVELOPER_DIR": "/synthetic/Xcode"}
        hostile = {name: "untrusted" for name in ("SRCROOT", "SDKROOT", "CC", "CXX", "CFLAGS", "CPPFLAGS",
                   "CXXFLAGS", "LDFLAGS", "MAKEFLAGS", "MFLAGS", "MAKELEVEL", "MAKEFILES", "MAKEOVERRIDES",
                   "GNUMAKEFLAGS", "CMAKE_TOOLCHAIN_FILE", "ARCH_STRING_FOR_CURRENT_MACHINE_CONFIG",
                   "DYLD_INSERT_LIBRARIES", "BASH_ENV", "ENV", "SECRET_TOKEN")}
        self.assertEqual(xnu.clean_build_environment(dict(allowed, **hostile)), allowed)

    def test_execute_records_actual_command_log_and_sanitized_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            steps = []
            command = [sys.executable, "-c", "import os; print(os.getenv('MAKEFILES', 'absent'))"]
            with patch.dict(os.environ, {"MAKEFILES": "untrusted-makefile", "CFLAGS": "-Dfake"}):
                status = xnu.execute(command, base, base / "raw.log", base / "evidence", "synthetic", recipe_steps=steps)
            self.assertEqual(status, 0)
            self.assertEqual((base / "raw.log").read_text(), "absent\n")
            self.assertEqual(steps[0]["argv"], command)
            self.assertEqual(steps[0]["cwd"], str(base))
            self.assertEqual(steps[0]["raw_log_sha256"], hashlib.sha256((base / "raw.log").read_bytes()).hexdigest())

    def test_stale_kernel_or_cmake_cache_is_rejected_before_build(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for name in ('kernel.development.t8103', 'CMakeCache.txt'):
                build = base / name.replace('.', '-')
                build.mkdir()
                existing = build / name
                existing.write_bytes(b'prior output must remain untouched')
                process = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/build/xnu.py'),
                    '--build-root', str(build), '--manifest', str(base / 'missing-lock.json')],
                    capture_output=True, text=True)
                self.assertEqual(process.returncode, 1)
                self.assertIn('fresh build root', process.stderr)
                self.assertNotIn('missing-lock.json', process.stderr)
                self.assertEqual(existing.read_bytes(), b'prior output must remain untouched')

    def test_source_metadata_and_patch_archive_ignore_inherited_git_redirect(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            source = base / 'sources/xnu'
            unrelated = base / 'unrelated'
            environment = xnu.clean_git_environment()
            for repo, text in ((source, 'pinned source\n'), (unrelated, 'unrelated source\n')):
                repo.mkdir(parents=True)
                subprocess.run(['git', 'init', '--quiet', str(repo)], env=environment, check=True)
                (repo / 'input.h').write_text(text)
                subprocess.run(['git', '-C', str(repo), 'add', 'input.h'], env=environment, check=True)
                subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Build recipe test',
                    '-c', 'user.email=recipe@example.invalid', 'commit', '--quiet', '-m', 'fixture'],
                    env=environment, check=True)
            version_path = source / 'src/Kernel/xnu/config/MasterVersion'
            version_path.parent.mkdir(parents=True)
            version_path.write_text('20.5.0\n# source version\n')
            subprocess.run(['git', '-C', str(source), 'add', '.'], env=environment, check=True)
            subprocess.run(['git', '-C', str(source), '-c', 'user.name=Build recipe test',
                '-c', 'user.email=recipe@example.invalid', 'commit', '--quiet', '-m', 'source version'],
                env=environment, check=True)
            revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'],
                                               env=environment, text=True).strip()
            tree = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD^{tree}'],
                                           env=environment, text=True).strip()
            manifest = base / 'lock.json'
            manifest.write_text(json.dumps({'sources': [{'name': 'xnu', 'revision': revision, 'tree': tree}]}))
            amendment = base / 'reviewed.patch'
            amendment.write_text('diff --git a/input.h b/input.h\n--- a/input.h\n+++ b/input.h\n'
                                 '@@ -1 +1 @@\n-pinned source\n+reviewed source amendment\n')
            sdk = base / 'sdk'; sdk.mkdir()
            build, evidence = base / 'build', base / 'evidence'
            actual_output = subprocess.check_output
            actual_run = subprocess.run
            def tool_output(command, **kwargs):
                if command[0] == 'git':
                    self.assertNotIn('GIT_DIR', kwargs['env'])
                    self.assertNotIn('GIT_WORK_TREE', kwargs['env'])
                    return actual_output(command, **kwargs)
                if '--print-path' in command: return '/synthetic/Xcode\n'
                if '--find' in command: return '/synthetic/Xcode/usr/bin/' + command[-1] + '\n'
                raise AssertionError(command)
            def mutate_original_after_patch_check(command, *arguments, **kwargs):
                result = actual_run(command, *arguments, **kwargs)
                if command[:3] == ['git', 'apply', '--check']:
                    amendment.write_text('superseded invalid patch bytes\n')
                return result
            def fail_configure(command, cwd, raw_log, evidence_dir, name, **kwargs):
                kwargs['recipe_steps'].append({'argv': command, 'cwd': str(cwd), 'exit_code': 2})
                return 2  # No compiler, kernel, or native execution is simulated.
            argv = ['xnu.py', '--puredarwin', '--manifest', str(manifest), '--source-root', str(source.parent),
                    '--sdk', str(sdk), '--build-root', str(build), '--evidence', str(evidence),
                    '--machine', 'T8103', '--patch', str(amendment)]
            with patch.object(sys, 'argv', argv), \
                 patch.object(xnu.subprocess, 'check_output', side_effect=tool_output), \
                 patch.object(xnu.subprocess, 'run', side_effect=mutate_original_after_patch_check), \
                 patch.object(xnu, 'execute', side_effect=fail_configure), \
                 patch.dict(os.environ, {'GIT_DIR': str(unrelated / '.git'), 'GIT_WORK_TREE': str(unrelated)}):
                self.assertEqual(xnu.main(), 1)
            result = json.loads((evidence / 'result.json').read_text())
            self.assertEqual(result['source_revision'], revision)
            self.assertEqual(result['recipe']['source_tree'], tree)
            recorded_patch = result['source_amendments'][1]
            snapshot_bytes = Path(recorded_patch['snapshot_path']).read_bytes()
            self.assertEqual(recorded_patch['path'], str(amendment))
            self.assertEqual(hashlib.sha256(snapshot_bytes).hexdigest(), recorded_patch['sha256'])
            self.assertNotEqual(snapshot_bytes, amendment.read_bytes())
            self.assertEqual((build / 'source-overlay/input.h').read_text(), 'reviewed source amendment\n')
            self.assertEqual((source / 'input.h').read_text(), 'pinned source\n')
            self.assertEqual((unrelated / 'input.h').read_text(), 'unrelated source\n')
            self.assertEqual(result['status'], 'blocked')
            self.assertEqual(result['artifacts'], [])
            self.assertEqual(result['build_wrapper_sha256'], xnu.BUILD_WRAPPER_INPUTS[0]['sha256'])
            self.assertEqual(result['recipe']['build_wrapper_inputs'], xnu.BUILD_WRAPPER_INPUTS)

    def synthetic_recipe(self, puredarwin=False, configure_failed=False):
        """Observe command/receipt agreement using mocked tools, not fake build proof."""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            source, sdk, build, evidence = [base / name for name in ("source", "sdk", "build", "evidence")]
            source.mkdir(); sdk.mkdir()
            if puredarwin:
                version_path = source / 'src/Kernel/xnu/config/MasterVersion'
                version_path.parent.mkdir(parents=True)
                version_path.write_text('20.5.0\n# actual source fixture version\n')
            manifest = base / "lock.json"
            manifest.write_text(json.dumps({"sources": [{"name": "xnu", "revision": "a" * 40}]}))
            argv = ["xnu.py", "--manifest", str(manifest), "--machine", "T8103", "--sdk", str(sdk),
                    "--build-root", str(build), "--evidence", str(evidence)]
            if puredarwin:
                argv.append("--puredarwin")
            else:
                argv += ["--make-setting", "PLATFORM=MacOSX", "--make-setting", "WARNFLAGS_STD=-Wall -Wextra"]
            executed = []
            def tool_output(command, **kwargs):
                if command[0] == "git": return ("b" * 40 if command[-1] == "HEAD^{tree}" else "a" * 40) + "\n"
                if "--print-path" in command: return "/synthetic/Xcode\n"
                if "--find" in command: return "/synthetic/Xcode/usr/bin/" + command[-1] + "\n"
                if "--show-sdk-version" in command: return "27.2\n"
                if "--show-sdk-platform-path" in command: return "/synthetic/Xcode/platform\n"
                raise AssertionError(command)
            def observe(command, cwd, raw_log, evidence_dir, name, **kwargs):
                environment = kwargs["environment"]
                self.assertNotIn("MAKEFILES", environment)
                self.assertNotIn("SRCROOT", environment)
                self.assertEqual(environment["SDKROOT"], str(sdk))
                self.assertEqual(environment["CC"], "/synthetic/Xcode/usr/bin/clang")
                code = 2 if configure_failed else 0
                record = {"argv": command, "cwd": str(cwd), "exit_code": code}
                kwargs["recipe_steps"].append(record)
                executed.append(record)
                return code
            with patch.object(sys, "argv", argv), patch.object(xnu, "locked_source", return_value=source), \
                 patch.object(xnu.subprocess, "check_output", side_effect=tool_output), \
                 patch.object(xnu, "execute", side_effect=observe), \
                 patch.dict(os.environ, {"MAKEFILES": "/untrusted", "SRCROOT": "/untrusted", "CC": "/untrusted", "SDKROOT": "/untrusted"}):
                self.assertEqual(xnu.main(), 1)  # No compiled artifact was fabricated.
            result = json.loads((evidence / "result.json").read_text())
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["artifacts"], [])
            self.assertEqual(result["recipe"]["commands"], executed)
            self.assertEqual(result["recipe_sha256"], xnu.canonical_digest(result["recipe"]))
            self.assertEqual(result["recipe"]["machine"], result["machine"])
            self.assertEqual(result["recipe"]["source_revision"], result["source_revision"])
            self.assertEqual(result["recipe"]["manifest_sha256"], result["manifest_sha256"])
            self.assertEqual(result["recipe"]["sdk"], result["sdk"])
            self.assertFalse(result["boot_validated"])
            return result

    def test_upstream_command_and_receipt_share_effective_source_profile_sdk(self):
        result = self.synthetic_recipe()
        recipe = result["recipe"]
        command = recipe["commands"][0]["argv"]
        for key, value in (("MACHINE_CONFIGS", "T8103"), ("SRCROOT", recipe["effective_source"]),
                           ("SDKROOT", recipe["sdk"]), ("CC", recipe["tool_environment"]["CC"]),
                           ("PLATFORM", "MacOSX"), ("WARNFLAGS_STD", "-Wall -Wextra")):
            self.assertEqual([arg for arg in command if arg.startswith(key + "=")], [key + "=" + value])

    def test_puredarwin_recipe_pins_compilers_and_cmake_profile(self):
        recipe = self.synthetic_recipe(puredarwin=True)["recipe"]
        configure = recipe["commands"][0]["argv"]
        self.assertIn("-DPUREDARWIN_ARM64_MACHINE_CONFIG=T8103", configure)
        self.assertIn("-DCMAKE_C_COMPILER=" + recipe["tool_environment"]["CC"], configure)
        self.assertIn("-DCMAKE_OSX_SYSROOT=" + recipe["sdk"], configure)
        self.assertEqual(recipe['darwin_version'], '20.5.0')
        self.assertIsNone(recipe['requested_apple_darwin_version'])
        self.assertIsNone(recipe['applied_apple_darwin_version'])
        self.assertEqual(recipe['source_master_version']['version'], '20.5.0')

    def test_configure_failure_still_retains_recipe_and_runs_no_build(self):
        result = self.synthetic_recipe(puredarwin=True, configure_failed=True)
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(len(result["recipe"]["commands"]), 1)


if __name__ == "__main__":
    unittest.main()
