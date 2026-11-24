"""Synthetic merge/receipt regressions; no thesis inputs or renderers."""
from __future__ import annotations

import copy
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import property_receipts
from requirements_engine import merge_llm_primary, _merge_scoped_projection


def merge_structure(properties, baseline=None):
    clauses = [{"id": f"C{i:05}", "text": f"Synthetic structural duty {i}.",
                "evidence_ids": [f"E{i:05}"]} for i in range(len(properties))]
    response = {
        "contract_version": "3.0",
        "requirements": [
            {"role": "document_structure", "properties": copy.deepcopy(props),
             "clause_ids": [clause["id"]], "evidence_ids": clause["evidence_ids"],
             "confidence": 1.0, "reason": "Synthetic structural requirement."}
            for props, clause in zip(properties, clauses)
        ],
        "clause_reviews": [
            {"clause_id": clause["id"], "classification": "executable",
             "reason": "Synthetic structural requirement.",
             "normative_basis": "explicit_normative_text",
             "obligations": [{"id": clause["id"] + "-duty-1", "status": "covered",
                              "reason": "Synthetic structural requirement.",
                              "actor": "author", "action": "include", "target": "structure",
                              "condition": "unconditional", "source_quote": clause["text"],
                              "force": "required", "applicability": "applicable",
                              "route": "automatic"}]}
            for clause in clauses
        ],
        "reported_conflicts": [], "unsupported_items": [],
    }
    rule_spec = {"roles": {}, "requirements": []}
    if baseline is not None:
        rule_spec["document_structure"] = copy.deepcopy(baseline)
    original = copy.deepcopy(response)
    result = merge_llm_primary(Path("/tmp/synthetic-structure.docx"), rule_spec,
                               clauses, response, {c["evidence_ids"][0] for c in clauses})
    assert response == original, "merge mutated the source response"
    return result


def requirement(identifier, roles, **properties):
    return {"id": identifier, "role": "document_structure",
            "properties": {"required_roles": roles, **properties}}


class RequiredRoleProjectionTests(unittest.TestCase):
    def test_merge_unions_each_local_requirement_without_losing_records(self):
        properties = [{"required_roles": ["abstract_title_zh", "keywords_zh"]},
                      {"required_roles": ["bibliography_heading"]},
                      {"required_roles": ["keywords_zh", "heading_acknowledgments"]}]
        spec, conflicts, _ = merge_structure(properties)
        self.assertEqual([r["properties"] for r in spec["requirements"]], properties)
        self.assertEqual(spec["document_structure"]["required_roles"],
                         ["abstract_title_zh", "keywords_zh", "bibliography_heading",
                          "heading_acknowledgments"])
        self.assertFalse([c for c in conflicts if c.get("property") == "required_roles"])

    def test_merge_retains_baseline_required_roles(self):
        spec, conflicts, _ = merge_structure(
            [{"required_roles": ["keywords_zh"]}],
            {"required_roles": ["abstract_title_zh"], "toc_depth": 2})
        self.assertEqual(spec["document_structure"],
                         {"required_roles": ["abstract_title_zh", "keywords_zh"], "toc_depth": 2})
        self.assertFalse([c for c in conflicts if c.get("property") == "required_roles"])

    def test_order_constraints_remain_distinct_not_a_fabricated_global_order(self):
        spec, _, _ = merge_structure([
            {"required_roles": ["keywords_zh"], "ordered_roles": ["abstract_title_zh", "keywords_zh"]},
            {"required_roles": ["bibliography_heading"], "ordered_roles": ["body_text", "bibliography_heading"]}])
        self.assertEqual(spec["document_structure"]["ordered_roles"],
                         ["abstract_title_zh", "keywords_zh"])
        self.assertEqual(spec["document_structure"]["ordered_role_groups"],
                         [["body_text", "bibliography_heading"]])

    def test_unrelated_list_and_scalar_projection_keeps_existing_semantics(self):
        target = {"order": ["first"], "toc_depth": 2}
        conflicts = _merge_scoped_projection(target, {"order": ["second"], "toc_depth": 3},
                                             prefer_incoming=True)
        self.assertEqual(target, {"order": ["second"], "toc_depth": 3})
        self.assertEqual({c[0] for c in conflicts}, {"order", "toc_depth"})


class RequiredRoleReceiptTests(unittest.TestCase):
    def receipts(self, requirements, actual_by_role=None, actual_by_requirement=None):
        return property_receipts.build_property_receipts(
            requirements, {}, actual_by_role or {}, serialized_docx_sha256="0" * 64,
            actual_by_requirement=actual_by_requirement)

    def local_actuals(self, requirements, presence):
        function = getattr(property_receipts, "required_role_actuals", None)
        self.assertTrue(callable(function), "missing requirement-scoped required-role observations")
        return function(requirements, presence)

    def test_present_superset_satisfies_each_required_subset(self):
        requirements = [requirement("R1", ["abstract_title_zh"]),
                        requirement("R2", ["keywords_zh"])]
        receipts = self.receipts(requirements, {"document_structure": {
            "required_roles": ["keywords_zh", "abstract_title_zh", "bibliography_heading"]}})
        self.assertEqual([r["status"] for r in receipts], ["verified", "verified"])

    def test_other_list_properties_still_require_exact_order(self):
        self.assertFalse(property_receipts.satisfies("ordered_roles", ["body_text", "toc_title"],
                                                     ["toc_title", "body_text"]))
        self.assertFalse(property_receipts.satisfies("other_roles", ["a", "b"], ["a"]))
        self.assertFalse(property_receipts.satisfies("required_roles", ["a"], ["missing"]))
        # A coincidentally named list on another role has no structure semantics.
        receipts = self.receipts([{"id": "R1", "role": "other",
                                  "properties": {"required_roles": ["a"]}}],
                                {"other": {"required_roles": ["a", "b"]}})
        self.assertEqual(receipts[0]["status"], "failed")

    def test_null_empty_and_malformed_role_evidence_cannot_pass(self):
        for actual, expected in [(None, ["body_text"]), ([], ["body_text"]),
                                 (["body_text"], []), (None, None),
                                 ([1], [1]), ("body_text", ["body_text"])]:
            with self.subTest(actual=actual, expected=expected):
                receipt = self.receipts([requirement("R1", expected)],
                    {"document_structure": {"required_roles": actual}})[0]
                self.assertNotEqual(receipt["status"], "verified")

    def test_requirement_actuals_do_not_require_an_unrelated_role_wide_value(self):
        receipts = self.receipts([requirement("R1", ["keywords_zh"])],
                                actual_by_requirement={"R1": {"required_roles": ["keywords_zh"]}})
        self.assertEqual(receipts[0]["status"], "verified")

    def test_unknown_cover_does_not_make_witnessed_keywords_missing(self):
        requirements = [requirement("RC", ["cover"]), requirement("RK", ["keywords_zh"])]
        actuals = self.local_actuals(requirements, {"cover": None, "keywords_zh": True})
        self.assertEqual(actuals, {"RC": {}, "RK": {"required_roles": ["keywords_zh"]}})
        receipts = self.receipts(requirements, {"document_structure": {"required_roles": ["bibliography_heading"]}}, actuals)
        self.assertEqual([r["status"] for r in receipts], ["unverified", "verified"])
        self.assertEqual([r["actual"] for r in receipts], [None, ["keywords_zh"]])

    def test_no_observation_is_unknown_not_evidence_of_absence(self):
        requirements = [requirement("R1", ["body_text", "keywords_en"])]
        actuals = self.local_actuals(requirements, {"body_text": True})
        self.assertEqual(self.receipts(requirements, actual_by_requirement=actuals)[0]["status"], "unverified")

    def test_explicit_negative_observation_can_fail_only_its_local_requirement(self):
        requirements = [requirement("R1", ["abstract_title_zh"]), requirement("R2", ["keywords_en"])]
        actuals = self.local_actuals(requirements, {"abstract_title_zh": True, "keywords_en": False})
        receipts = self.receipts(requirements, actual_by_requirement=actuals)
        self.assertEqual([r["status"] for r in receipts], ["verified", "failed"])
        self.assertEqual([r["actual"] for r in receipts], [["abstract_title_zh"], []])

    def test_truthy_nonboolean_observations_cannot_prove_presence(self):
        requirements = [requirement("R1", ["cover"])]
        for value in [1, "present", [], {}]:
            with self.subTest(value=value):
                actuals = self.local_actuals(requirements, {"cover": value})
                self.assertEqual(self.receipts(requirements, actual_by_requirement=actuals)[0]["status"], "unverified")

    def test_local_unknown_does_not_shadow_other_observed_structure_properties(self):
        function = getattr(property_receipts, "required_role_actuals", None)
        self.assertTrue(callable(function), "missing requirement-scoped required-role observations")
        requirements = [requirement("R1", ["cover"], toc_depth=3)]
        actuals = function(requirements, {}, structure_actuals={"required_roles": ["unrelated"], "toc_depth": 3})
        self.assertEqual(actuals, {"R1": {"toc_depth": 3}})
        receipts = self.receipts(requirements, actual_by_requirement=actuals)
        self.assertEqual({r["property_path"]: r["status"] for r in receipts},
                         {"required_roles": "unverified", "toc_depth": "verified"})


if __name__ == "__main__":
    unittest.main()
