"""Validate the rootfs decision record (issue #47); no guest or hardware simulation."""

import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
RECORD_PATH = ROOT / "evidence/native/rootfs-decision.json"
DOC_PATH = ROOT / "docs/audits/rootfs-decision.md"
CONSTRAINT_STATUSES = {"documented", "validated", "hardware-gated"}


def load_record():
    return json.loads(RECORD_PATH.read_text())


class RootfsDecisionRecordTests(unittest.TestCase):
    def test_required_top_level_fields(self):
        record = load_record()
        for field in ("schema_version", "record", "issue", "date", "status",
                      "decision", "candidates", "apple_silicon_constraints",
                      "prototype_image_plan", "hardware_gated_items",
                      "physical_m1_validation", "gate_2_passed"):
            self.assertIn(field, record, field)
        self.assertEqual(record["schema_version"], 1)
        self.assertEqual(record["record"], "rootfs-decision")
        self.assertEqual(record["issue"], 47)

    def test_chosen_filesystem_recorded_and_is_a_compared_candidate(self):
        record = load_record()
        chosen = record["decision"]["chosen_filesystem"]
        self.assertTrue(chosen)
        names = {candidate["name"] for candidate in record["candidates"]}
        self.assertIn(chosen, names)
        chosen_entries = [c for c in record["candidates"]
                          if c.get("decision") == "chosen"]
        self.assertEqual([c["name"] for c in chosen_entries], [chosen])
        for candidate in record["candidates"]:
            self.assertIn("license", candidate)
            self.assertTrue(candidate["license"])
            self.assertIn("decision", candidate)

    def test_every_constraint_labeled(self):
        record = load_record()
        constraints = record["apple_silicon_constraints"]
        self.assertGreaterEqual(len(constraints), 5)
        ids = set()
        for constraint in constraints:
            self.assertIn("id", constraint)
            self.assertIn("constraint", constraint)
            self.assertIn("basis", constraint)
            self.assertIn(constraint.get("status"), CONSTRAINT_STATUSES,
                          constraint.get("id"))
            ids.add(constraint["id"])
        self.assertEqual(len(ids), len(constraints), "duplicate constraint ids")

    def test_hardware_gated_items_explicit_and_referenced(self):
        record = load_record()
        gated = {c["id"] for c in record["apple_silicon_constraints"]
                 if c["status"] == "hardware-gated"}
        self.assertTrue(gated, "at least one hardware-gated constraint required")
        self.assertTrue(record["hardware_gated_items"])
        text = " ".join(record["hardware_gated_items"])
        for constraint_id in gated:
            self.assertIn(constraint_id, text)

    def test_no_physical_or_gate_claims(self):
        record = load_record()
        self.assertIs(record["physical_m1_validation"], False)
        self.assertIs(record["gate_2_passed"], False)
        self.assertIn("unverified", record["status"])
        for candidate in record["candidates"]:
            scope = candidate.get("validation_scope", "")
            self.assertNotIn("physical m1 mounted", scope.lower())
        gate2 = json.loads((ROOT / "evidence/gate-2.json").read_text())
        self.assertEqual(gate2["decision"], "NOT_RUN")

    def test_validated_constraints_scoped_to_virtual_platform(self):
        record = load_record()
        validated = [c for c in record["apple_silicon_constraints"]
                     if c["status"] == "validated"]
        self.assertTrue(validated)
        for constraint in validated:
            self.assertIn("virtual", constraint.get("validation_scope", "").lower())

    def test_prototype_plan_uses_existing_tooling(self):
        record = load_record()
        tooling = record["prototype_image_plan"]["current"]["tooling"]
        self.assertTrue(tooling)
        for entry in tooling:
            path = entry.split(":", 1)[0].strip()
            self.assertTrue((ROOT / path).is_file(), path)
        for step in record["prototype_image_plan"]["next_steps"]:
            self.assertIn("status", step)

    def test_decision_doc_exists_and_matches_record(self):
        self.assertTrue(DOC_PATH.is_file())
        text = DOC_PATH.read_text()
        record = load_record()
        self.assertIn(record["decision"]["chosen_filesystem"], text)
        self.assertIn("#47", text)
        for constraint in record["apple_silicon_constraints"]:
            self.assertIn(constraint["id"], text)
        self.assertIn("hardware-gated", text)
        self.assertIn("NOT_RUN", text)

    def test_cited_pinned_revisions_match_source_lock(self):
        record = load_record()
        lock = json.loads((ROOT / "sources.lock.json").read_text())
        revisions = {s["name"]: s["revision"] for s in lock["sources"]}
        for candidate in record["candidates"]:
            source = candidate.get("source")
            if source and source.get("revision"):
                self.assertEqual(source["revision"],
                                 revisions[source["tree"]],
                                 candidate["name"])


if __name__ == "__main__":
    unittest.main()
