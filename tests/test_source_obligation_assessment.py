from __future__ import annotations

import copy
import hashlib
import sys
import unittest

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from obligation_submission_gate import audit_obligation_submission_gate
from format_spec_validation import load_and_validate
from semantic_contract import sha256_json
from source_obligation_assessment import (
    assessment_projection_valid, build_obligation_assessment, expected_scope_id,
)


class SourceObligationAssessmentTests(unittest.TestCase):
    def setUp(self):
        self.binding = {
            "run_id": "run-current", "case_id": "case-current",
            "source_sha256": "a" * 64, "format_spec_sha256": "b" * 64,
        }
        self.docx_sha256 = "c" * 64

    def unit(self, index, *, force="required", applicability="applicable", route="automatic",
             target="abstract", condition="when supplied"):
        span = {
            "evidence_id": "E-1", "start_offset": 0, "end_offset": 20,
            "text": "摘要应包含研究目的。",
            "source_sha256": "d" * 64,
            "location": {"part": "document", "paragraph": 1},
        }
        key = f"{index:064x}"
        return {
            "evaluation_unit_id": "EU-" + f"{index:032x}",
            "canonical_obligation_key": key,
            "force": force, "applicability": applicability, "route": route,
            "source_sha256": self.binding["source_sha256"],
            "source_span_sha256": sha256_json(span),
            "semantic_basis": "typed_source_atom",
            "source_context": {
                "source_sha256": self.binding["source_sha256"], "clause_id": "C-1",
                "span": span, "evidence_ids": ["E-1"],
            },
            "semantic_assertion": {
                "actor": "作者", "action": f"说明事项{index}", "target": target,
                "condition": condition, "source_quote": f"摘要应包含研究目的{index}。",
            },
        }

    def inventory(self, units, statuses, *, complete=True):
        first = units[0]
        source_context = first["source_context"]
        source_binding = {
            "source_sha256": self.binding["source_sha256"], "clause_id": "C-1",
            "source_span_sha256": first["source_span_sha256"], "evidence_ids": ["E-1"],
        }
        atoms = []
        receipts = []
        for index, (unit, atom_statuses) in enumerate(zip(units, statuses), start=1):
            receipt_ids = []
            for check_index, status in enumerate(atom_statuses, start=1):
                receipt_id = f"R-{index}-{check_index}"
                receipt_ids.append(receipt_id)
                receipts.append({
                    "receipt_id": receipt_id, "status": status,
                    "property_path": f"abstract.atom_{index}",
                    "serialized_docx_sha256": self.docx_sha256,
                    "evaluation_units": [copy.deepcopy(unit)],
                })
            atoms.append({
                "evaluation_unit_id": unit["evaluation_unit_id"],
                "evidence_receipt_ids": receipt_ids,
            })
        scope = {
            "scope_id": expected_scope_id(source_binding, "abstract", "when supplied"),
            "source_binding": source_binding, "object_ref": "abstract",
            "condition": "when supplied", "inventory_complete": complete,
            "expected_atom_ids": [unit["evaluation_unit_id"] for unit in units],
            "atoms": atoms, "importance": "not_assessed",
            "review_priority": {"effect": "sort_only_no_compliance_or_release_effect", "rank": None},
        }
        inventory = {
            "schema_version": "1.0", "policy": "explicit_source_scope_inventory_v1",
            "binding": copy.deepcopy(self.binding), "inventory_complete": complete,
            "scopes": [scope],
        }
        if complete:
            inventory["review_attestation"] = {
                "reviewer_id": "reviewer-test", "method": "human_source_first_scope_review",
                "reviewed_source_sha256": self.binding["source_sha256"],
            }
        return inventory, {"receipts": receipts}

    def assessment(self, units, statuses, *, complete=True):
        inventory, receipt_audit = self.inventory(units, statuses, complete=complete)
        unit_map = {"req-current": units}
        return (build_obligation_assessment(
                    self.binding, unit_map, receipt_audit, inventory, self.docx_sha256,
                ),
                inventory, receipt_audit, unit_map)

    def test_four_verified_two_unknown_reports_coverage_without_satisfaction_rate(self):
        units = [self.unit(i, route="human" if i == 6 else "automatic") for i in range(1, 7)]
        result, _, _, _ = self.assessment(
            units, [["verified"], ["verified"], ["verified"], ["verified"], ["unverified"], []],
        )
        scope = result["scopes"][0]
        self.assertEqual(scope["assessment_coverage"], 0.6667)
        self.assertIsNone(scope["satisfaction_ratio"])
        self.assertEqual(scope["outcome"], "partially_verified")
        self.assertEqual(scope["counts"]["satisfied"], 4)
        self.assertEqual(scope["counts"]["unverified"], 1)
        self.assertEqual(scope["counts"]["pending"], 1)
        self.assertTrue(any(item.startswith("hard_obligation_unresolved:")
                            for item in result["blockers"]))
        self.assertFalse(result["submission_ready"])
        self.assertTrue(assessment_projection_valid(result))

    def test_four_verified_two_failed_is_partially_satisfied_and_hard_blocked(self):
        units = [self.unit(i) for i in range(1, 7)]
        result, _, _, _ = self.assessment(
            units, [["verified"]] * 4 + [["failed"], ["failed"]],
        )
        scope = result["scopes"][0]
        self.assertEqual(scope["assessment_coverage"], 1.0)
        self.assertEqual(scope["satisfaction_ratio"], 0.6667)
        self.assertEqual(scope["outcome"], "partially_satisfied")
        self.assertEqual(scope["counts"]["failed"], 2)
        self.assertTrue(any(item.startswith("hard_obligation_failed:")
                            for item in result["blockers"]))
        self.assertTrue(assessment_projection_valid(result))

    def test_failure_and_unknown_are_both_retained(self):
        units = [self.unit(i, route="human" if i == 6 else "automatic") for i in range(1, 7)]
        result, _, _, _ = self.assessment(
            units, [["verified"]] * 4 + [["failed", "unverified"], []],
        )
        scope = result["scopes"][0]
        self.assertEqual(scope["outcome"], "partially_assessed_with_failures")
        self.assertEqual(scope["assessment_coverage"], 0.6667)
        self.assertIsNone(scope["satisfaction_ratio"])
        self.assertEqual(scope["counts"]["failed"], 1)
        self.assertEqual(scope["counts"]["unverified"], 1)
        self.assertTrue(assessment_projection_valid(result))

    def test_duplicate_checks_do_not_increase_atom_denominator(self):
        units = [self.unit(i, route="human" if i == 6 else "automatic") for i in range(1, 7)]
        result, _, _, _ = self.assessment(
            units, [["verified", "verified"], ["verified"], ["verified"], ["verified"], ["unverified"], []],
        )
        scope = result["scopes"][0]
        self.assertEqual(scope["atom_count"], 6)
        self.assertEqual(scope["assessment_coverage"], 0.6667)
        self.assertEqual(len(scope["atoms"][0]["evidence_receipt_ids"]), 2)

    def test_stale_serialized_receipt_never_counts_as_satisfied(self):
        unit = self.unit(1)
        result, inventory, receipts, unit_map = self.assessment([unit], [["verified"]])
        receipts["receipts"][0]["serialized_docx_sha256"] = "f" * 64
        result = build_obligation_assessment(
            self.binding, unit_map, receipts, inventory, self.docx_sha256,
        )
        atom = result["scopes"][0]["atoms"][0]
        self.assertEqual(atom["status"], "unverified")
        self.assertEqual(atom["stale_evidence_receipt_ids"], ["R-1-1"])
        self.assertIsNone(result["scopes"][0]["satisfaction_ratio"])
        gate = audit_obligation_submission_gate(
            required=True, inventory=inventory, expected_binding=self.binding,
            units_by_requirement=unit_map, receipt_audit=receipts,
            serialized_docx_sha256=self.docx_sha256,
        )
        self.assertFalse(gate["valid"])
        self.assertIn("scope_atom_receipt_stale_docx:R-1-1", gate["errors"])

    def test_scope_inventory_schema_enforces_binding_and_attestation_shape(self):
        units = [self.unit(1)]
        _, inventory, _, _ = self.assessment(units, [["verified"]])
        errors = load_and_validate(
            inventory, ROOT / "schema" / "source-obligation-scope-inventory.schema.json",
        )
        self.assertEqual(errors, [])
        forged = copy.deepcopy(inventory)
        forged["review_attestation"]["reviewed_source_sha256"] = "e" * 64
        self.assertEqual(
            load_and_validate(forged, ROOT / "schema" / "source-obligation-scope-inventory.schema.json"),
            [],
        )
        # The JSON shape is valid, but the evaluator must reject the stale
        # attestation against the current source binding.
        with self.assertRaisesRegex(ValueError, "attestation"):
            build_obligation_assessment(
                self.binding, {"req-current": units}, self.inventory(units, [["verified"]])[1], forged,
                self.docx_sha256,
            )

    def test_incomplete_scope_has_no_percentages(self):
        units = [self.unit(i) for i in range(1, 3)]
        result, _, _, _ = self.assessment(units, [["verified"], ["failed"]], complete=False)
        self.assertFalse(result["inventory_complete"])
        self.assertIsNone(result["assessment_coverage"])
        self.assertIsNone(result["satisfaction_ratio"])
        self.assertEqual(result["scopes"][0]["outcome"], "scope_incomplete")
        self.assertTrue(assessment_projection_valid(result))

    def test_stale_binding_wrong_scope_or_missing_na_evidence_rejected(self):
        units = [self.unit(1)]
        result, inventory, receipts, unit_map = self.assessment(units, [["verified"]])
        stale = copy.deepcopy(inventory)
        stale["binding"]["source_sha256"] = "e" * 64
        with self.assertRaisesRegex(ValueError, "binding_mismatch"):
            build_obligation_assessment(self.binding, unit_map, receipts, stale)
        wrong_object = copy.deepcopy(inventory)
        wrong_object["scopes"][0]["object_ref"] = "different target"
        with self.assertRaisesRegex(ValueError, "scope id"):
            build_obligation_assessment(self.binding, unit_map, receipts, wrong_object)
        na_unit = self.unit(2, applicability="not_applicable")
        na_inventory, na_receipts = self.inventory([na_unit], [[]])
        with self.assertRaisesRegex(ValueError, "source-bound applicability evidence"):
            build_obligation_assessment(self.binding, {"req": [na_unit]}, na_receipts, na_inventory)

    def test_human_owned_atom_stays_pending_and_is_never_relabelled_na(self):
        unit = self.unit(1, route="human")
        result, _, _, _ = self.assessment([unit], [[]])
        atom = result["scopes"][0]["atoms"][0]
        self.assertEqual(atom["applicability"], "applicable")
        self.assertEqual(atom["status"], "pending")

    def test_unknown_force_or_conflicted_applicability_cannot_count_as_a_pass(self):
        for unit in (
            self.unit(1, force="unknown"),
            self.unit(2, applicability="conflicted"),
        ):
            with self.subTest(unit=unit["evaluation_unit_id"]):
                result, _, _, _ = self.assessment([unit], [["verified"]])
                atom = result["scopes"][0]["atoms"][0]
                self.assertEqual(atom["status"], "unknown")
                self.assertEqual(result["scopes"][0]["satisfaction_ratio"], None)
                self.assertTrue(any("unresolved" in item for item in result["blockers"]))
                self.assertTrue(assessment_projection_valid(result))

    def test_unknown_route_cannot_become_satisfied_from_a_verified_property(self):
        unit = self.unit(1, route="unknown")
        result, inventory, receipt_audit, unit_map = self.assessment([unit], [["verified"]])
        atom = result["scopes"][0]["atoms"][0]
        self.assertEqual(atom["status"], "unknown")
        self.assertIsNone(result["satisfaction_ratio"])
        self.assertTrue(assessment_projection_valid(result))
        gate = audit_obligation_submission_gate(
            required=True, inventory=inventory, expected_binding=self.binding,
            units_by_requirement=unit_map, receipt_audit=receipt_audit,
            serialized_docx_sha256=self.docx_sha256,
        )
        self.assertFalse(gate["hard_gate_passed"])
        self.assertTrue(any(item.startswith("obligation_route_unresolved:")
                            for item in gate["blockers"]))

    def test_projection_recomputes_counts_ratios_outcome_and_source_binding(self):
        unit = self.unit(1)
        result, _, _, _ = self.assessment([unit], [["verified"]])
        self.assertTrue(assessment_projection_valid(result))
        for mutate in (
            lambda x: x["scopes"][0]["counts"].update(satisfied=0),
            lambda x: x["scopes"][0].update(assessment_coverage=0.0),
            lambda x: x["scopes"][0].update(outcome="failed"),
            lambda x: x["scopes"][0]["source_binding"].update(source_sha256="f" * 64),
        ):
            with self.subTest(mutation=mutate):
                tampered = copy.deepcopy(result)
                mutate(tampered)
                self.assertFalse(assessment_projection_valid(tampered))

    def test_legacy_units_cannot_be_promoted_to_complete_inventory(self):
        legacy = {
            "evaluation_unit_id": "EU-" + "f" * 32,
            "canonical_obligation_key": "f" * 64,
            "force": "unknown", "applicability": "unknown", "route": "unknown",
            "source_sha256": self.binding["source_sha256"],
            "source_span_sha256": "1" * 64,
            "semantic_basis": "legacy_unknown_dimensions",
        }
        inventory = {
            "schema_version": "1.0", "policy": "explicit_source_scope_inventory_v1",
            "binding": copy.deepcopy(self.binding), "inventory_complete": True,
            "review_attestation": {
                "reviewer_id": "reviewer-test", "method": "human_source_first_scope_review",
                "reviewed_source_sha256": self.binding["source_sha256"],
            },
            "scopes": [],
        }
        result = build_obligation_assessment(
            self.binding, {"legacy": [legacy]}, {"receipts": []}, inventory,
        )
        self.assertFalse(result["inventory_complete"])
        self.assertEqual(result["legacy_unknown_unit_count"], 1)
        self.assertIsNone(result["assessment_coverage"])
        self.assertIn("source_obligation_scope_inventory_incomplete", result["blockers"])

    def test_independent_submission_gate_blocks_missing_stale_and_failed_hard_evidence(self):
        units = [self.unit(1)]
        result, inventory, receipt_audit, unit_map = self.assessment(units, [["verified"]])
        gate = audit_obligation_submission_gate(
            required=True, inventory=None, expected_binding=self.binding,
            units_by_requirement=unit_map, receipt_audit=receipt_audit,
            serialized_docx_sha256=self.docx_sha256,
        )
        self.assertFalse(gate["valid"])
        self.assertIn("source_obligation_scope_inventory_missing", gate["blockers"])
        gate = audit_obligation_submission_gate(
            required=True, inventory=inventory, expected_binding=self.binding,
            units_by_requirement=unit_map, receipt_audit=receipt_audit,
            serialized_docx_sha256=self.docx_sha256,
        )
        self.assertTrue(gate["hard_gate_passed"])
        self.assertFalse(gate["submission_ready"])
        failed_inventory, failed_receipts = self.inventory(units, [["failed"]])
        gate = audit_obligation_submission_gate(
            required=True, inventory=failed_inventory, expected_binding=self.binding,
            units_by_requirement=unit_map, receipt_audit=failed_receipts,
            serialized_docx_sha256=self.docx_sha256,
        )
        self.assertFalse(gate["hard_gate_passed"])
        self.assertTrue(any(item.startswith("hard_obligation_failed:") for item in gate["blockers"]))


if __name__ == "__main__":
    unittest.main()
