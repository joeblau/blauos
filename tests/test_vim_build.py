"""Vim layer pin, amendment and smoke-contract tests; none compile or fetch."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/vm"))
spec = importlib.util.spec_from_file_location("build_vim_commands",
                                              ROOT / "scripts/vm/build_vim_commands.py")
vim_build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vim_build)

LOCK = ROOT / "sources.lock.json"


class VimPinTests(unittest.TestCase):
    def test_archive_pin_matches_the_real_fetched_tarball(self):
        pin = vim_build.archive_pin(LOCK)
        self.assertEqual(pin["filename"], "vim-9.2.1175.tar.gz")
        self.assertEqual(pin["url"],
                         "https://github.com/vim/vim/archive/refs/tags/v9.2.1175.tar.gz")
        self.assertRegex(pin["sha256"], r"^[0-9a-f]{64}$")
        self.assertIn("Vim license", pin["license"])
        fetched = ROOT / ".blauos/inputs" / pin["filename"]
        if fetched.exists():
            self.assertEqual(vim_build.digest(fetched), pin["sha256"])

    def test_archive_pin_requires_a_locked_entry(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest = Path(folder) / "lock.json"
            manifest.write_text(json.dumps({"schema_version": 1, "archives": []}))
            with self.assertRaisesRegex(ValueError, "not pinned"):
                vim_build.archive_pin(manifest)
            manifest.write_text(json.dumps({"schema_version": 1, "archives": [
                {"name": vim_build.ARCHIVE_NAME, "filename": "v.tgz"}]}))
            with self.assertRaisesRegex(ValueError, "requires"):
                vim_build.archive_pin(manifest)

    def test_verify_archive_rejects_mismatch_without_modifying(self):
        pin = {"name": "vim-test", "filename": "vim-test.tar.gz", "sha256": "0" * 64}
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / "vim-test.tar.gz"
            archive.write_bytes(b"caller-owned bytes")
            with self.assertRaisesRegex(ValueError, "preserved"):
                vim_build.verify_archive(pin, archive)
            self.assertEqual(archive.read_bytes(), b"caller-owned bytes")
            with self.assertRaisesRegex(ValueError, "filename"):
                other = Path(folder) / "other.tar.gz"
                other.write_bytes(b"caller-owned bytes")
                vim_build.verify_archive(pin, other)

    def test_extract_archive_rejects_unsafe_members(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            payload = base / "payload"
            payload.mkdir()
            (payload / "vim-9.2.1175/src/main.c").parent.mkdir(parents=True)
            (payload / "vim-9.2.1175/src/main.c").write_text("int main;\n")
            good = base / "good.tar.gz"
            with tarfile.open(good, "w:gz") as tarball:
                tarball.add(payload / "vim-9.2.1175", arcname="vim-9.2.1175")
            root = vim_build.extract_archive(good, base / "out")
            self.assertEqual(root.name, "vim-9.2.1175")
            self.assertTrue((root / "src/main.c").is_file())
            for index, name in enumerate(("../escape", "/absolute", "dir/../../escape")):
                bad = base / f"bad{index}.tar.gz"
                with tarfile.open(bad, "w:gz") as tarball:
                    info = tarfile.TarInfo(name)
                    data = b"x"
                    info.size = len(data)
                    import io
                    tarball.addfile(info, io.BytesIO(data))
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, "Unsafe"):
                    vim_build.extract_archive(bad, base / f"out-bad{index}")
            linked = base / "linked.tar.gz"
            with tarfile.open(linked, "w:gz") as tarball:
                info = tarfile.TarInfo("vim-9.2.1175/link")
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
                tarball.addfile(info)
            with self.assertRaisesRegex(ValueError, "Unsupported"):
                vim_build.extract_archive(linked, base / "out-linked")

    def test_pathdef_source_is_deterministic_and_host_neutral(self):
        first = vim_build.pathdef_source("cc -O2", "cc -o vim")
        self.assertEqual(first, vim_build.pathdef_source("cc -O2", "cc -o vim"))
        self.assertIn('"blauos-builder"', first)
        self.assertIn('"/usr/share/vim"', first)
        self.assertIn('char_u *all_cflags = (char_u *)"cc -O2";', first)


class VimAmendmentTests(unittest.TestCase):
    def test_tracked_amendments_are_declared_and_present(self):
        for relative in vim_build.BUILD_INPUTS:
            self.assertTrue((ROOT / relative).is_file(), relative)
        self.assertIn(vim_build.PATCH, vim_build.BUILD_INPUTS)

    def test_patch_applies_to_the_pinned_source_layout(self):
        # The patch context paths must match the release tarball layout.
        text = (ROOT / vim_build.PATCH).read_text()
        self.assertIn("a/src/termlib.c", text)
        self.assertIn("a/src/proto/termlib.pro", text)
        self.assertIn("int (*outc)(int)", text)

    def test_config_selects_tiny_features_without_host_libraries(self):
        text = (ROOT / "vm/commands/vim/config.h").read_text()
        self.assertIn("#define FEAT_TINY 1", text)
        self.assertIn("#define HAVE_TGETENT 1", text)
        self.assertIn("#define HAVE_TERMIOS_H 1", text)
        for excluded in ("HAVE_TERMCAP_H", "HAVE_ICONV", "HAVE_X11", "HAVE_GETTEXT"):
            self.assertNotIn(f"#define {excluded}", text)

    def test_source_list_matches_upstream_makefile_objects(self):
        makefile = None
        extracted = sorted((ROOT / ".blauos/build/commands").glob("vim-run-*/vim-source/*/src/Makefile"))
        if extracted:
            makefile = extracted[0]
        if makefile is None:
            self.skipTest("extracted vim source not present; build receipt comparison covers it")
        import re
        lines = makefile.read_text().splitlines()
        objects = []
        capture = False
        for line in lines:
            if capture:
                if not line.startswith("\t"):
                    capture = False
                    continue
                item = line.strip().rstrip("\\").strip()
                if item.startswith("objects/") and item.endswith(".o"):
                    objects.append(item[len("objects/"):-2])
            elif line.startswith(("OBJ_COMMON = \\", "OBJ_MAIN = \\")):
                capture = True
        expected = sorted(name for name in objects if name != "pathdef") + ["termlib"]
        self.assertEqual(sorted(vim_build.VIM_SOURCES), sorted(expected))


class VimSmokeContractTests(unittest.TestCase):
    def test_smoke_script_checks_editor_over_plain_serial(self):
        text = (ROOT / "vm/commands/smoke.sh").read_text()
        self.assertIn(" vim", text.split("for tool in", 1)[1].split("; do", 1)[0])
        self.assertIn("vim --version | grep -q 'Vi IMproved'", text)
        self.assertIn("vim -es -u NONE -i NONE", text)
        self.assertIn("BLAUOS_COMMAND_CHECK editor pass", text)
        self.assertIn("interactive serial console", text)

    def test_installer_requires_the_editor_check(self):
        from install_commands import CHECKS
        self.assertIn("editor", CHECKS)

    def test_build_commands_includes_the_vim_layer(self):
        import build_commands
        parser_source = (ROOT / "scripts/vm/build_commands.py").read_text()
        self.assertIn('"vim": ["--archive", args.vim_archive]', parser_source)
        self.assertTrue(hasattr(build_commands, "verify_archive"))

    def test_provenance_schema_of_recorded_build(self):
        receipts = sorted((ROOT / ".blauos/build/commands").glob("vim-run-*/provenance.json"))
        if not receipts:
            self.skipTest("no recorded vim build receipt")
        data = json.loads(receipts[-1].read_text())
        self.assertEqual(data["schema_version"], 1)
        self.assertTrue(data["built"])
        self.assertIs(data["stock_runtime_used"], False)
        self.assertIs(data["guest_execution_verified"], False)
        entry = next(item for item in data["artifacts"] if item["path"] == "/usr/bin/vim")
        self.assertEqual(entry["linked_dylibs"], ["/usr/lib/libSystem.B.dylib"])
        self.assertEqual(entry["archive_sha256"], vim_build.archive_pin(LOCK)["sha256"])
        self.assertIn("FEAT_TINY", entry["feature_config"])
        for item in entry["build_amendments"]:
            self.assertEqual(vim_build.digest(ROOT / item["path"]), item["sha256"])


if __name__ == "__main__":
    unittest.main()
