"""Build input isolation tests; no target executable runs on the host."""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("openssh_build", ROOT / "scripts/build/openssh.py")
ssh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ssh)


class OpenSSHBuildTests(unittest.TestCase):
    def source_fixture(self, folder):
        root = Path(folder)
        source = root / "source"
        source.mkdir()
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        (source / "configure").write_text("# upstream source fixture\n")
        subprocess.run(["git", "-C", str(source), "add", "."], check=True)
        subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "commit", "-qm", "fixture"], check=True)
        revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        (root / "sources.lock.json").write_text(json.dumps({"sources": [{"name": "openssh-portable", "revision": revision}]}))
        args = argparse.Namespace(source=source, output=root / "output", runtime_root=root / "runtime", jobs=1)
        return root, args

    def test_dirty_source_refused_before_configuring_or_creating_output(self):
        with tempfile.TemporaryDirectory() as folder:
            root, args = self.source_fixture(folder)
            (args.source / "configure").write_text("# modified\n")
            with patch.object(ssh, "ROOT", root), patch.object(ssh.sys, "platform", "darwin"):
                with self.assertRaisesRegex(ValueError, "pristine"):
                    ssh.build(args)
            self.assertFalse(args.output.exists())

    def test_unpinned_source_refused_before_configuring_or_creating_output(self):
        with tempfile.TemporaryDirectory() as folder:
            root, args = self.source_fixture(folder)
            (root / "sources.lock.json").write_text(json.dumps({"sources": [{"name": "openssh-portable", "revision": "0" * 40}]}))
            with patch.object(ssh, "ROOT", root), patch.object(ssh.sys, "platform", "darwin"):
                with self.assertRaisesRegex(ValueError, "source lock"):
                    ssh.build(args)
            self.assertFalse(args.output.exists())

    def test_existing_build_receipts_preserved_by_cli_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "kept"
            output.mkdir()
            receipt = output / "provenance.json"
            receipt.write_bytes(b"prior build receipt\n")
            result = subprocess.run(["python3", str(ROOT / "scripts/build/openssh.py"),
                                     "--runtime-root", folder, "--output", str(output)], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(receipt.read_bytes(), b"prior build receipt\n")
            self.assertEqual(sorted(path.name for path in output.iterdir()), ["provenance.json"])

    def test_host_framework_or_untracked_library_is_rejected(self):
        for library in ("/System/Library/Frameworks/Security.framework/Versions/A/Security",
                        "/usr/lib/libcrypto.46.dylib", "/opt/homebrew/lib/libcrypto.dylib"):
            with self.subTest(library=library):
                output = "sshd:\n\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n\t" + library + " (compatibility version 1.0.0)\n"
                with self.assertRaisesRegex(ValueError, "Unexpected target dylib"):
                    ssh.checked_dependencies(output)

    def test_load_commands_retain_only_independent_runtime_install_names(self):
        output = "sshd:\n\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1292.100.5)\n"
        self.assertEqual(ssh.checked_dependencies(output), ["/usr/lib/libSystem.B.dylib"])


if __name__ == "__main__":
    unittest.main()
