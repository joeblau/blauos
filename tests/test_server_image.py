"""Minimal server image allowlist validation; no target executable runs on the host."""
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = ROOT / "runtime/server-image.json"
DECISION_RECORD = ROOT / "docs/audits/server-principles.md"

PROVENANCE_KEYS = ("source", "revision", "builder", "evidence")


def load_allowlist():
    return json.loads(ALLOWLIST.read_text())


class ServerImageSchemaTests(unittest.TestCase):
    def setUp(self):
        self.image = load_allowlist()

    def test_schema_version_and_top_level_shape(self):
        self.assertEqual(self.image["schema_version"], 1)
        for key in ("image", "defined", "issue", "decision_record", "evidence_level",
                    "status", "services", "runtime_components", "excluded", "verification"):
            self.assertIn(key, self.image)
        self.assertIsInstance(self.image["services"], list)
        self.assertIsInstance(self.image["runtime_components"], list)
        self.assertIsInstance(self.image["excluded"], list)
        self.assertTrue(self.image["services"])
        self.assertTrue(self.image["runtime_components"])

    def test_evidence_level_is_software_and_gates_stay_open(self):
        self.assertEqual(self.image["evidence_level"], "software")
        self.assertIn("NOT_RUN", self.image["status"])
        self.assertIn("Gate 4", self.image["status"])
        self.assertEqual(self.image["verification"]["gate_2_physical_m1"], "NOT_RUN")
        self.assertEqual(self.image["verification"]["gate_4_issue_59"], "OPEN")
        self.assertIs(self.image["verification"]["physical_acceptance"], False)


class ServerImageServiceTests(unittest.TestCase):
    def setUp(self):
        self.image = load_allowlist()
        self.services = self.image["services"]

    def test_every_service_has_complete_provenance_and_justification(self):
        for service in self.services:
            with self.subTest(label=service.get("label")):
                for key in ("label", "kind", "program", "launchd_keys", "provenance", "justification"):
                    self.assertIn(key, service)
                self.assertIn(service["kind"], ("pid1", "launchd_daemon"))
                self.assertTrue(service["program"].startswith("/"))
                self.assertIsInstance(service["launchd_keys"], dict)
                self.assertTrue(service["justification"].strip())
                provenance = service["provenance"]
                for key in PROVENANCE_KEYS:
                    self.assertTrue(provenance.get(key, "").strip(), f"missing provenance.{key}")

    def test_no_duplicate_service_labels(self):
        labels = [service["label"] for service in self.services]
        self.assertEqual(len(labels), len(set(labels)))

    def test_daemon_services_have_plists_and_pid1_does_not(self):
        for service in self.services:
            with self.subTest(label=service["label"]):
                if service["kind"] == "launchd_daemon":
                    self.assertTrue(service["plist"].startswith("System/Library/LaunchDaemons/"))
                else:
                    self.assertIsNone(service["plist"])

    def test_service_programs_are_allowlisted_components(self):
        component_paths = {path for component in self.image["runtime_components"] for path in component["paths"]}
        for service in self.services:
            with self.subTest(label=service["label"]):
                self.assertIn(service["program"], component_paths)


class ServerImageComponentTests(unittest.TestCase):
    def setUp(self):
        self.image = load_allowlist()
        self.components = self.image["runtime_components"]

    def test_every_component_has_complete_provenance_and_justification(self):
        for component in self.components:
            with self.subTest(name=component.get("name")):
                for key in ("name", "paths", "role", "provenance", "justification"):
                    self.assertIn(key, component)
                self.assertTrue(component["paths"])
                self.assertTrue(component["justification"].strip())
                provenance = component["provenance"]
                for key in PROVENANCE_KEYS:
                    self.assertTrue(provenance.get(key, "").strip(), f"missing provenance.{key}")

    def test_no_duplicate_component_names_or_paths(self):
        names = [component["name"] for component in self.components]
        self.assertEqual(len(names), len(set(names)))
        paths = [path for component in self.components for path in component["paths"]]
        self.assertEqual(len(paths), len(set(paths)))

    def test_component_paths_are_absolute_installed_paths(self):
        for component in self.components:
            for path in component["paths"]:
                with self.subTest(path=path):
                    self.assertTrue(path.startswith("/"))
                    self.assertFalse(path.endswith("/"))
                    self.assertNotIn("//", path)

    def test_every_exclusion_has_a_reason(self):
        for exclusion in self.image["excluded"]:
            with self.subTest(name=exclusion.get("name")):
                self.assertTrue(exclusion.get("name", "").strip())
                self.assertTrue(exclusion.get("reason", "").strip())


class ServerImageDocConsistencyTests(unittest.TestCase):
    def doc_counts(self):
        text = DECISION_RECORD.read_text()
        counts = {}
        for key in ("allowlist_services", "allowlist_runtime_components", "allowlist_component_paths"):
            match = re.search(rf"^{key}: (\d+)$", text, re.MULTILINE)
            self.assertIsNotNone(match, f"{key} count missing from {DECISION_RECORD}")
            counts[key] = int(match[1])
        return counts

    def test_counts_in_decision_record_match_allowlist(self):
        image = load_allowlist()
        counts = self.doc_counts()
        self.assertEqual(counts["allowlist_services"], len(image["services"]))
        self.assertEqual(counts["allowlist_runtime_components"], len(image["runtime_components"]))
        path_count = sum(len(component["paths"]) for component in image["runtime_components"])
        self.assertEqual(counts["allowlist_component_paths"], path_count)

    def test_decision_record_labels_every_service(self):
        text = DECISION_RECORD.read_text()
        for service in load_allowlist()["services"]:
            with self.subTest(label=service["label"]):
                self.assertIn(service["label"], text)


if __name__ == "__main__":
    unittest.main()
