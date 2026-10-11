"""Pinned package recipe validation and clean-rebuild comparison tests.

No source is fetched or compiled; validation compares recipe pins against the
repository lock files, and rebuild comparison runs on synthetic receipts.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("package_builds", ROOT / "scripts/build/packages.py")
packages = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packages)

RECIPES = ROOT / "packages"
LOCK = ROOT / "sources.lock.json"
FLAKE = ROOT / "manifests/puredarwin-flake.lock"
TOOLCHAIN = ROOT / "toolchain.lock.json"


def recipe_fixture(folder):
    """Copy the real recipes and locks into a scratch root for mutation tests."""
    root = Path(folder)
    recipes = root / "packages"
    recipes.mkdir()
    for path in RECIPES.glob("*.json"):
        shutil.copy2(path, recipes / path.name)
    for name in ("sources.lock.json", "toolchain.lock.json"):
        shutil.copy2(ROOT / name, root / name)
    (root / "manifests").mkdir()
    shutil.copy2(FLAKE, root / "manifests/puredarwin-flake.lock")
    return root, recipes


def mutate(recipes, name, change):
    path = recipes / f"{name}.json"
    recipe = json.loads(path.read_text())
    change(recipe)
    path.write_text(json.dumps(recipe, indent=2) + "\n")


class RepoRecipeTests(unittest.TestCase):
    def test_every_recipe_pin_matches_the_lock_files(self):
        receipt = packages.validate_recipes(RECIPES, LOCK, FLAKE, TOOLCHAIN)
        self.assertEqual(receipt["result"], "pass")
        names = [entry["name"] for entry in receipt["recipes"]]
        self.assertEqual(names, receipt["build_order"])
        self.assertEqual(set(names), {"kernel", "kernel-collection-tools", "runtime",
                                      "dash-shell", "command-toolkit", "openssh"})
        # Dependencies resolve before their dependents.
        self.assertLess(names.index("runtime"), names.index("openssh"))
        self.assertLess(names.index("kernel-collection-tools"), names.index("kernel"))
        for entry in receipt["recipes"]:
            sources = entry["sources"]
            self.assertTrue(sources)
            lock = json.loads(LOCK.read_text())
            pins = {item["name"]: item for item in lock["sources"]}
            recipe = json.loads((RECIPES / f"{entry['name']}.json").read_text())
            for source in recipe["sources"]:
                pin = pins[source["lock_name"]]
                self.assertEqual(source["url"], pin["url"])
                self.assertEqual(source["revision"], pin["revision"])
                self.assertEqual(source.get("tree"), pin.get("tree"))
                self.assertEqual(source.get("tag"), pin.get("tag"))
            # Every pin comparison is recorded as an explicit check.
            for source in recipe["sources"]:
                self.assertIn(f"{source['lock_name']}.revision", entry["checks"])
        # Flake-locked tool revisions are cross-checked against the flake lock.
        tools = next(entry for entry in receipt["recipes"] if entry["name"] == "kernel-collection-tools")
        for lock_name in ("iig-tools", "kc-tools", "xnu-loader"):
            self.assertIn(f"{lock_name}.flake-rev", tools["checks"])
        self.assertEqual(receipt["transitive_locks"][0]["path"], "manifests/puredarwin-flake.lock")

    def test_validate_cli_passes_on_the_repository(self):
        result = subprocess.run(["python3", str(ROOT / "scripts/build/packages.py"), "validate"],
                                cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("6 recipes", result.stdout)

    def test_every_recipe_records_license_metadata(self):
        for path in sorted(RECIPES.glob("*.json")):
            with self.subTest(recipe=path.name):
                recipe = json.loads(path.read_text())
                license_field = recipe["license"]
                self.assertTrue(license_field["notes"].strip())
                self.assertTrue(license_field["determined_from"].strip())
                if license_field["spdx"] is not None:
                    packages.parse_spdx(license_field["spdx"])

    def test_no_recipe_claims_clean_rebuild_verification(self):
        # Clean-environment rebuilds with artifact comparison are remaining work.
        for path in sorted(RECIPES.glob("*.json")):
            recipe = json.loads(path.read_text())
            self.assertIs(recipe["rebuild"]["clean_rebuild_verified"], False)


class MalformedRecipeTests(unittest.TestCase):
    def test_wrong_revision_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root, recipes = recipe_fixture(folder)
            mutate(recipes, "openssh", lambda r: r["sources"][0].update(revision="0" * 40))
            with self.assertRaisesRegex(packages.RecipeError, "does not match sources.lock.json"):
                packages.validate_recipes(recipes, root / "sources.lock.json",
                                          root / "manifests/puredarwin-flake.lock",
                                          root / "toolchain.lock.json", root=ROOT)

    def test_wrong_url_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root, recipes = recipe_fixture(folder)
            mutate(recipes, "dash-shell",
                   lambda r: r["sources"][0].update(url="https://example.invalid/dash.git"))
            with self.assertRaisesRegex(packages.RecipeError, "url"):
                packages.validate_recipes(recipes, root / "sources.lock.json",
                                          root / "manifests/puredarwin-flake.lock",
                                          root / "toolchain.lock.json", root=ROOT)

    def test_missing_license_metadata_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root, recipes = recipe_fixture(folder)
            mutate(recipes, "kernel", lambda r: r.pop("license"))
            with self.assertRaisesRegex(packages.RecipeError, "license"):
                packages.validate_recipes(recipes, root / "sources.lock.json",
                                          root / "manifests/puredarwin-flake.lock",
                                          root / "toolchain.lock.json", root=ROOT)

    def test_null_spdx_without_notice_citation_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root, recipes = recipe_fixture(folder)
            mutate(recipes, "kernel", lambda r: r["license"].update(notes="see upstream"))
            with self.assertRaisesRegex(packages.RecipeError, "null SPDX"):
                packages.validate_recipes(recipes, root / "sources.lock.json",
                                          root / "manifests/puredarwin-flake.lock",
                                          root / "toolchain.lock.json", root=ROOT)

    def test_unknown_spdx_identifier_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root, recipes = recipe_fixture(folder)
            mutate(recipes, "dash-shell", lambda r: r["license"].update(spdx="BSD-99-Clause"))
            with self.assertRaisesRegex(packages.RecipeError, "Unknown SPDX license"):
                packages.validate_recipes(recipes, root / "sources.lock.json",
                                          root / "manifests/puredarwin-flake.lock",
                                          root / "toolchain.lock.json", root=ROOT)

    def test_unknown_dependency_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root, recipes = recipe_fixture(folder)
            mutate(recipes, "openssh", lambda r: r.update(depends=["not-a-recipe"]))
            with self.assertRaisesRegex(packages.RecipeError, "unknown dependency"):
                packages.validate_recipes(recipes, root / "sources.lock.json",
                                          root / "manifests/puredarwin-flake.lock",
                                          root / "toolchain.lock.json", root=ROOT)

    def test_dependency_cycle_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root, recipes = recipe_fixture(folder)
            mutate(recipes, "runtime", lambda r: r.update(depends=["openssh"]))
            with self.assertRaisesRegex(packages.RecipeError, "cycle"):
                packages.validate_recipes(recipes, root / "sources.lock.json",
                                          root / "manifests/puredarwin-flake.lock",
                                          root / "toolchain.lock.json", root=ROOT)

    def test_missing_builder_script_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root, recipes = recipe_fixture(folder)
            mutate(recipes, "openssh", lambda r: r["builder"].update(
                script="scripts/build/nope.py", command=["python3", "scripts/build/nope.py"]))
            with self.assertRaisesRegex(packages.RecipeError, "does not exist"):
                packages.validate_recipes(recipes, root / "sources.lock.json",
                                          root / "manifests/puredarwin-flake.lock",
                                          root / "toolchain.lock.json", root=ROOT)

    def test_missing_tracked_patch_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root, recipes = recipe_fixture(folder)
            mutate(recipes, "openssh", lambda r: r["builder"].update(
                patches=["vm/patches/not-tracked.patch"]))
            with self.assertRaisesRegex(packages.RecipeError, "patch"):
                packages.validate_recipes(recipes, root / "sources.lock.json",
                                          root / "manifests/puredarwin-flake.lock",
                                          root / "toolchain.lock.json", root=ROOT)

    def test_absolute_output_path_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root, recipes = recipe_fixture(folder)
            mutate(recipes, "dash-shell", lambda r: r.update(outputs=["/bin/sh"]))
            with self.assertRaisesRegex(packages.RecipeError, "relative path"):
                packages.validate_recipes(recipes, root / "sources.lock.json",
                                          root / "manifests/puredarwin-flake.lock",
                                          root / "toolchain.lock.json", root=ROOT)

    def test_flake_lock_disagreement_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root, recipes = recipe_fixture(folder)
            flake_path = root / "manifests/puredarwin-flake.lock"
            flake = json.loads(flake_path.read_text())
            flake["nodes"]["kc-tools"]["locked"]["rev"] = "f" * 40
            flake_path.write_text(json.dumps(flake))
            # Keep the transitive-lock hash consistent so only the rev check fires.
            lock_path = root / "sources.lock.json"
            lock = json.loads(lock_path.read_text())
            lock["transitive_locks"][0]["sha256"] = hashlib.sha256(flake_path.read_bytes()).hexdigest()
            lock_path.write_text(json.dumps(lock))
            with self.assertRaisesRegex(packages.RecipeError, "flake"):
                packages.validate_recipes(recipes, lock_path, flake_path,
                                          root / "toolchain.lock.json", root=ROOT)

    def test_malformed_json_recipe_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root, recipes = recipe_fixture(folder)
            (recipes / "openssh.json").write_text("{ not json\n")
            with self.assertRaises(packages.RecipeError):
                packages.validate_recipes(recipes, root / "sources.lock.json",
                                          root / "manifests/puredarwin-flake.lock",
                                          root / "toolchain.lock.json", root=ROOT)


def artifact(sha256_char, path):
    return {"path": path, "size": 16, "sha256": sha256_char * 64}


class RebuildComparisonTests(unittest.TestCase):
    def receipt(self, artifacts, root="/build/root"):
        return {"schema_version": 1, "artifacts":
                [artifact(char, root + "/" + name) for char, name in artifacts],
                "built": True}

    def test_identical_rebuilds_match_across_build_roots(self):
        first = self.receipt([("a", "usr/bin/ssh"), ("b", "usr/sbin/sshd")], root="/build/run-1")
        second = self.receipt([("b", "usr/sbin/sshd"), ("a", "usr/bin/ssh")], root="/build/run-2")
        report = packages.compare_receipts(first, second)
        self.assertTrue(report["match"])
        self.assertEqual(report["artifact_count_first"], 2)
        self.assertEqual(report["artifact_set_sha256_first"], report["artifact_set_sha256_second"])

    def test_differing_artifact_hash_is_a_mismatch(self):
        first = self.receipt([("a", "usr/bin/ssh")])
        second = self.receipt([("c", "usr/bin/ssh")])
        report = packages.compare_receipts(first, second)
        self.assertFalse(report["match"])
        self.assertEqual(report["hash_mismatch"], ["ssh"])

    def test_missing_or_extra_artifact_is_a_mismatch(self):
        first = self.receipt([("a", "usr/bin/ssh"), ("b", "usr/sbin/sshd")])
        second = self.receipt([("a", "usr/bin/ssh")])
        report = packages.compare_receipts(first, second)
        self.assertFalse(report["match"])
        self.assertEqual(report["only_first"], ["sshd"])
        self.assertEqual(report["only_second"], [])

    def test_receipt_without_artifacts_rejected(self):
        with self.assertRaisesRegex(packages.RecipeError, "artifacts"):
            packages.compare_receipts({"schema_version": 1}, self.receipt([("a", "x")]))

    def test_malformed_artifact_hash_rejected(self):
        bad = {"artifacts": [{"path": "usr/bin/ssh", "sha256": "not-a-hash"}]}
        with self.assertRaisesRegex(packages.RecipeError, "malformed"):
            packages.compare_receipts(bad, self.receipt([("a", "x")]))

    def test_compare_cli_exit_codes_and_report(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first = root / "run-1.json"
            second = root / "run-2.json"
            report_path = root / "comparison.json"
            first.write_text(json.dumps(self.receipt([("a", "usr/bin/ssh")], root="/one")))
            second.write_text(json.dumps(self.receipt([("a", "usr/bin/ssh")], root="/two")))
            script = ROOT / "scripts/build/packages.py"
            result = subprocess.run(["python3", str(script), "compare", str(first), str(second),
                                     "--output", str(report_path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(report_path.read_text())["match"])
            second.write_text(json.dumps(self.receipt([("d", "usr/bin/ssh")], root="/two")))
            result = subprocess.run(["python3", str(script), "compare", str(first), str(second)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("hash_mismatch", result.stderr)


class SpdxParserTests(unittest.TestCase):
    def test_recorded_expressions_parse(self):
        packages.parse_spdx("BSD-3-Clause")
        packages.parse_spdx("BSD-3-Clause AND GPL-2.0-or-later")
        packages.parse_spdx("Apache-2.0 WITH LLVM-exception")

    def test_invalid_expressions_rejected(self):
        for expression in ("", "BSD", "MIT OR", "AND MIT", "Unknown-1.0",
                           "Apache-2.0 WITH Classpath-exception-2.0", "(MIT"):
            with self.subTest(expression=expression):
                with self.assertRaises(packages.RecipeError):
                    packages.parse_spdx(expression)


if __name__ == "__main__":
    unittest.main()
