"""Absence versus empty inventory is representation, never permission to lose duties."""
from __future__ import annotations

import copy
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import host_agent_bridge as bridge
from host_review_schema import normalize_native_response
from test_publication_policy_inventory import fixture, proposed


class EmptyInformationInventoryTests(unittest.TestCase):
    def setUp(self):
        self.old, self.chunk, _ = fixture(include_information=True)
        self.schema = self.chunk["response_schema"]

    def normalize(self, response):
        return normalize_native_response(response, self.schema)

    def test_absent_null_empty_normalize_identically_and_idempotently(self):
        for classification in ("informational", "not_applicable"):
            parent = copy.deepcopy(self.old)
            parent["clause_reviews"][-1]["classification"] = classification
            expected = self.normalize(parent)
            for empty in (None, []):
                raw = copy.deepcopy(parent)
                raw["clause_reviews"][-1]["obligations"] = empty
                frozen = copy.deepcopy(raw)
                with self.subTest(classification=classification, value=empty):
                    normalized = self.normalize(raw)
                    self.assertEqual(normalized, expected)
                    self.assertEqual(normalized["clause_reviews"][-1]["obligations"], [])
                    self.assertEqual(self.normalize(normalized), normalized)
                    self.assertEqual(raw, frozen)
                    self.assertEqual(normalized["requirements"], parent["requirements"])

    def test_nonempty_and_malformed_inventories_are_not_erased(self):
        for value in ([{"id": "real-atom"}], [None], {}, "", False, 0):
            raw = copy.deepcopy(self.old)
            raw["clause_reviews"][-1]["obligations"] = value
            with self.subTest(value=value):
                self.assertEqual(self.normalize(raw)["clause_reviews"][-1]["obligations"], value)

    def test_executable_pending_and_unknown_classifications_keep_empty_inventory(self):
        for classification in ("covered", "executable", "unresolved", "external_compliance",
                               "executable_with_external_check", "requires_source_content",
                               "unknown-value", [], None):
            raw = copy.deepcopy(self.old)
            raw["clause_reviews"][-1].update(classification=classification, obligations=[])
            with self.subTest(classification=classification):
                self.assertEqual(self.normalize(raw)["clause_reviews"][-1]["obligations"], [])

    def test_current_schema_must_explicitly_allow_optional_empty_array(self):
        raw = copy.deepcopy(self.old)
        raw["clause_reviews"][-1]["obligations"] = []
        for defect in ("required", "nonempty", "undeclared", "old-contract"):
            schema = copy.deepcopy(self.schema)
            for branch in schema["properties"]["clause_reviews"]["items"]["anyOf"]:
                if defect == "required": branch["required"].append("obligations")
                elif defect == "nonempty": branch["properties"]["obligations"]["minItems"] = 1
                elif defect == "undeclared": branch["properties"].pop("obligations")
            if defect == "old-contract": schema["properties"]["contract_version"]["const"] = "2.1"
            with self.subTest(defect=defect):
                self.assertEqual(normalize_native_response(raw, schema)["clause_reviews"][-1]["obligations"], [])
                absent = copy.deepcopy(raw)
                absent["clause_reviews"][-1].pop("obligations")
                if defect == "old-contract": absent["contract_version"] = "2.1"
                self.assertNotIn("obligations", normalize_native_response(absent, schema)["clause_reviews"][-1])

    def test_linked_or_referenced_reviews_preserve_presence_instead_of_inventing_empty_proof(self):
        for kind in ("clause_ids", "source_fragment_clause_ids", "nested-reference", "diagnostic"):
            raw = copy.deepcopy(self.old)
            cid = raw["clause_reviews"][-1]["clause_id"]
            if kind in {"clause_ids", "source_fragment_clause_ids"}:
                raw["requirements"][0].setdefault(kind, []).append(cid)
            elif kind == "nested-reference":
                raw["requirements"][0]["properties"]["context"] = {"selector": cid}
            else:
                raw["unsupported_items"].append("Review " + cid)
            with self.subTest(kind=kind):
                self.assertNotIn("obligations", self.normalize(raw)["clause_reviews"][-1])
                raw["clause_reviews"][-1]["obligations"] = []
                self.assertEqual(self.normalize(raw)["clause_reviews"][-1]["obligations"], [])

    def test_schema_rejection_survives_complete_candidate_preparation(self):
        for defect in ("required-missing", "nonempty-array", "undeclared", "executable-empty"):
            raw = proposed(self.old)
            chunk = copy.deepcopy(self.chunk)
            review = raw["clause_reviews"][-1]
            if defect != "required-missing": review["obligations"] = []
            if defect == "executable-empty": review["classification"] = "executable"
            for branch in chunk["response_schema"]["properties"]["clause_reviews"]["items"]["anyOf"]:
                if defect == "required-missing": branch["required"].append("obligations")
                elif defect == "nonempty-array": branch["properties"]["obligations"]["minItems"] = 1
                elif defect == "undeclared": branch["properties"].pop("obligations")
            with self.subTest(defect=defect):
                normalized = normalize_native_response(raw, chunk["response_schema"])
                self.assertTrue(bridge.validate_host_agent_response(normalized, chunk))
                with self.assertRaises(ValueError):
                    bridge.prepare_native_response_candidate(raw, chunk)

    def test_context_edge_projection_still_rejects_missing_inventory(self):
        from test_context_relation_projection import fixture as context_fixture
        from semantic_contract import sha256_json
        raw, chunk = context_fixture()
        del raw["clause_reviews"][0]["obligations"]
        with self.assertRaises(ValueError):
            bridge.prepare_native_response_candidate(raw, chunk,
                source_projection_validation_sha256=sha256_json(chunk))
        raw["clause_reviews"][0]["obligations"] = []
        candidate, _ = bridge.prepare_native_response_candidate(raw, chunk,
            source_projection_validation_sha256=sha256_json(chunk))
        again, _ = bridge.prepare_native_response_candidate(candidate, chunk,
            source_projection_validation_sha256=sha256_json(chunk))
        self.assertEqual(candidate, again)

    def test_real_inventory_deletion_and_classification_change_remain_drift(self):
        old = self.normalize(self.old)
        for defect in ("delete-duty", "reclassify", "source-edge", "change-reason"):
            raw = proposed(self.old)
            raw["clause_reviews"][-1]["obligations"] = []
            if defect == "delete-duty": raw["clause_reviews"][0]["obligations"].pop()
            elif defect == "reclassify": raw["clause_reviews"][-1]["classification"] = "not_applicable"
            elif defect == "source-edge": raw["requirements"][0]["clause_ids"].pop()
            else: raw["clause_reviews"][-1]["reason"] = "Different interpretation"
            new = self.normalize(raw)
            records = bridge.contract_error_records(bridge.validate_host_agent_response(old, self.chunk),
                response=old, chunk=self.chunk)
            with self.subTest(defect=defect):
                error, paths = bridge._retry_semantic_change_error(old, new, records,
                    contract_version="3.0", chunk=self.chunk)
                self.assertIsNotNone(error)
                self.assertTrue(paths)


if __name__ == "__main__":
    unittest.main()
