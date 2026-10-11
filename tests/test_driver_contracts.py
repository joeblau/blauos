"""Validate the native M1 driver-contract receipt against schema and pinned inputs."""
import hashlib
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / "evidence/native/driver-contracts.json"
REQUIRED_DRIVERS = {"aic", "uart", "clocks-resets-power-domains", "dart-iommu", "ans-nvme", "ethernet-pcie"}
CONTRACT_STATUSES = {"compile-and-link-only", "compile-and-object-inspection-only", "contract-only-no-driver"}
UNVERIFIED = "unverified-no-physical-execution"


def load_receipt():
    return json.loads(RECEIPT.read_text())


def sha256(path):
    return hashlib.sha256((ROOT / path).read_bytes()).hexdigest()


class DriverContractsTests(unittest.TestCase):
    def test_schema_and_top_level_keys(self):
        data = load_receipt()
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["kind"], "native-m1-driver-contract-audit")
        self.assertEqual(data["issue"], 41)
        for key in ("input_hashes", "reuse_boundary", "driver_contracts", "hardware_matrix", "acceptance"):
            self.assertIsInstance(data[key], (dict, list), key)
        self.assertTrue((ROOT / data["audit_document"]).is_file())

    def test_input_hashes_match_working_tree(self):
        data = load_receipt()
        for field in ("sources_lock", "puredarwin_flake_lock", "native_patch_series"):
            record = data["input_hashes"][field]
            self.assertEqual(sha256(record["path"]), record["sha256"], field)
        self.assertEqual(data["input_hashes"]["native_patch_series"]["patch_count"],
                         len([line for line in (ROOT / "patches/native/series").read_text().splitlines()
                              if line.strip() and not line.startswith("#")]))

    def test_pinned_revisions_match_sources_lock(self):
        data = load_receipt()
        lock = json.loads((ROOT / "sources.lock.json").read_text())
        locked = {source["name"]: source for source in lock["sources"]}
        families = {family["profile"]: family for family in data["reuse_boundary"]["public_pinned_families"]}
        self.assertEqual(set(families), {"upstream-modern", "puredarwin-vm"})
        modern, puredarwin = families["upstream-modern"], families["puredarwin-vm"]
        self.assertEqual(modern["primary_source"]["revision"], locked["xnu"]["revision"])
        self.assertEqual(modern["primary_source"]["tree"], locked["xnu"]["tree"])
        self.assertEqual(puredarwin["primary_source"]["revision"], locked["puredarwin-vm-next"]["revision"])
        self.assertEqual(puredarwin["primary_source"]["tree"], locked["puredarwin-vm-next"]["tree"])
        for profile in lock["profiles"]:
            self.assertEqual(sorted(families[profile]["members"]), sorted(lock["profiles"][profile]["sources"]))
        transitive = lock["transitive_locks"][0]
        self.assertEqual(puredarwin["transitive_lock"]["path"], transitive["path"])
        self.assertEqual(puredarwin["transitive_lock"]["sha256"], transitive["sha256"])

    def test_every_essential_driver_has_a_contract(self):
        data = load_receipt()
        contracts = {contract["id"]: contract for contract in data["driver_contracts"]}
        self.assertEqual(set(contracts), REQUIRED_DRIVERS)
        for driver in sorted(REQUIRED_DRIVERS):
            with self.subTest(driver=driver):
                contract = contracts[driver]
                self.assertIn(contract["verification"], CONTRACT_STATUSES)
                self.assertTrue(contract["iokit_interface"]["obligations"])
                self.assertTrue(contract["source_availability"])
                for patch in contract["patches"]:
                    self.assertTrue((ROOT / patch).is_file(), patch)
                for reference in contract["evidence"]:
                    self.assertTrue((ROOT / reference).exists(), reference)

    def test_every_matrix_row_has_explicit_unverified_status(self):
        data = load_receipt()
        rows = data["hardware_matrix"]
        self.assertEqual({row["driver"] for row in rows}, REQUIRED_DRIVERS)
        for row in rows:
            with self.subTest(driver=row["driver"]):
                self.assertEqual(row["model"], "Macmini9,1")
                self.assertEqual(row["soc"], "T8103")
                self.assertEqual(row["verification_status"], UNVERIFIED)

    def test_no_physical_or_build_acceptance_is_claimed(self):
        data = load_receipt()
        acceptance = data["acceptance"]
        self.assertFalse(acceptance["native_build_verified"])
        self.assertFalse(acceptance["driver_executed_on_hardware"])
        self.assertFalse(acceptance["physical_gate_passed"])
        self.assertEqual(acceptance["gate_2_status"], "not_run")
        blob = json.dumps(data)
        self.assertNotIn('"verification_status": "verified"', blob)

    def test_restricted_components_are_not_present_in_repo(self):
        data = load_receipt()
        restricted = data["reuse_boundary"]["restricted_components"]
        self.assertGreaterEqual(len(restricted), 5)
        for component in restricted:
            self.assertTrue(component["name"])
            self.assertTrue(component["classification"])
            for flag in ("present", "present_in_repo"):
                if flag in component:
                    self.assertFalse(component[flag], component["name"])
        research = data["reuse_boundary"]["research_only_references"]
        for reference in research:
            if "code_imported" in reference:
                self.assertFalse(reference["code_imported"])


if __name__ == "__main__":
    unittest.main()
