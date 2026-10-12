from __future__ import annotations

import copy
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from draft_scorecard import build_scorecard, scorecard_lines
from semantic_contract import sha256_json, strict_json_dumps
from source_obligation_inventory_input import load_bound_scope_inventory
from thesis_format_pipeline import bind_scope_inventory_for_apply
from source_obligation_assessment import expected_scope_id


class ScorecardScopeContractTests(unittest.TestCase):
    def setUp(self):
        self.binding = {
            "run_id": "run-scope-test", "case_id": "case-scope-test",
            "source_sha256": "a" * 64, "format_spec_sha256": "b" * 64,
        }
        self.docx_sha256 = "c" * 64

    def unit(self, index: int, *, applicability: str = "applicable", route: str = "automatic"):
        span = {
            "evidence_id": "E-1", "start_offset": 0, "end_offset": 18,
            "text": "摘要应包含研究目的。", "source_sha256": self.binding["source_sha256"],
            "location": {"part": "document", "paragraph": 1},
        }
        key = f"{index:032x}" + "0" * 32
        return {
            "evaluation_unit_id": "EU-" + f"{index:032x}",
            "canonical_obligation_key": key,
            "force": "required", "applicability": applicability, "route": route,
            "source_sha256": self.binding["source_sha256"],
            "source_span_sha256": sha256_json(span),
            "semantic_basis": "typed_source_atom",
            "source_context": {
                "source_sha256": self.binding["source_sha256"], "clause_id": "C-1",
                "span": span, "evidence_ids": ["E-1"],
            },
            "semantic_assertion": {
                "actor": "学校", "action": f"要求事项{index}", "target": "abstract",
                "condition": "when supplied", "source_quote": "摘要应包含研究目的。",
            },
        }

    def scope_inputs(self, units, atom_receipt_statuses, *, complete=True, include_na_evidence=True):
        source_binding = {
            "source_sha256": self.binding["source_sha256"], "clause_id": "C-1",
            "source_span_sha256": units[0]["source_span_sha256"], "evidence_ids": ["E-1"],
        }
        receipts = []
        atoms = []
        for index, (unit, statuses) in enumerate(zip(units, atom_receipt_statuses), start=1):
            receipt_ids = []
            for check_index, status in enumerate(statuses, start=1):
                receipt_id = f"R-{index}-{check_index}"
                receipt_ids.append(receipt_id)
                receipts.append({
                    "receipt_id": receipt_id, "requirement_id": f"R{index}",
                    "status": status, "role": "abstract", "property_path": f"atom_{index}",
                    "actual": status, "expected": "verified",
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
        if any(unit["applicability"] == "not_applicable" for unit in units) and include_na_evidence:
            scope["applicability_evidence"] = {
                "source_sha256": self.binding["source_sha256"], "clause_id": "C-1",
                "quote": "仅在论文含研究对象时适用。", "evidence_ids": ["E-1"],
            }
        inventory = {
            "schema_version": "1.0", "policy": "explicit_source_scope_inventory_v1",
            "binding": copy.deepcopy(self.binding), "inventory_complete": complete,
            "scopes": [scope],
        }
        if complete:
            inventory["review_attestation"] = {
                "reviewer_id": "reviewer-test",
                "method": "human_source_first_scope_review",
                "reviewed_source_sha256": self.binding["source_sha256"],
            }
        receipt_audit = {
            "missing_count": 0, "unexpected_count": 0, "duplicate_count": 0,
            "expected_receipt_ids": [item["receipt_id"] for item in receipts],
            "receipts": receipts,
        }
        unit_map = {f"R{index}": [copy.deepcopy(unit)] for index, unit in enumerate(units, start=1)}
        return inventory, receipt_audit, unit_map

    def card(self, inventory=None, units=None, statuses=None, *, complete=True, include_na_evidence=True):
        units = units if units is not None else []
        if inventory is None and units:
            inventory, receipt_audit, unit_map = self.scope_inputs(
                units, statuses or [[] for _ in units], complete=complete,
                include_na_evidence=include_na_evidence,
            )
        else:
            receipt_audit = {
                "missing_count": 0, "unexpected_count": 0, "duplicate_count": 0,
                "expected_receipt_ids": [], "receipts": [],
            }
            unit_map = None
        return build_scorecard(
            self.binding, receipt_audit, [], [],
            evaluation_units_by_requirement=unit_map,
            obligation_scope_inventory=inventory,
            expected_docx_sha256=self.docx_sha256,
        )

    def test_missing_scope_keeps_diagnostic_failure_out_of_overall_score(self):
        receipt_audit = {
            "missing_count": 0, "unexpected_count": 0, "duplicate_count": 0,
            "expected_receipt_ids": ["R-1"],
            "receipts": [{"receipt_id": "R-1", "status": "failed", "property_path": "font.size"}],
        }
        card = build_scorecard(self.binding, receipt_audit, [], [])
        lines = "\n".join(scorecard_lines(card))
        self.assertEqual(card["score"], None)
        self.assertEqual(card["score_status"], "unscored")
        self.assertEqual(card["score_reason"], "source_scope_inventory_missing")
        self.assertIn("来源义务加权总分：未评分（缺少来源义务范围清单）", lines)
        self.assertIn("单项检查0/100：已证实未满足", lines)
        self.assertNotIn("来源义务加权总分：0/100", lines)

    def test_composite_scope_reports_partial_satisfaction_separately_from_score(self):
        units = [self.unit(1), self.unit(2)]
        card = self.card(units=units, statuses=[["verified"], ["failed"]])
        assessment = card["obligation_assessment"]
        scope = assessment["scopes"][0]
        lines = "\n".join(scorecard_lines(card))
        self.assertEqual(assessment["status"], "complete")
        self.assertEqual(scope["assessment_coverage"], 1.0)
        self.assertEqual(scope["satisfaction_ratio"], 0.5)
        self.assertEqual(scope["outcome"], "partially_satisfied")
        self.assertEqual(card["score"], None)
        self.assertEqual(card["score_status"], "unscored")
        self.assertEqual(card["score_reason"], "importance_not_assessed")
        self.assertIn("满足率50.0%", lines)
        self.assertFalse(card["submission_ready"])

    def test_unknown_atom_keeps_coverage_but_never_fabricates_partial_satisfaction(self):
        units = [self.unit(1), self.unit(2)]
        card = self.card(units=units, statuses=[["verified"], ["unverified"]])
        assessment = card["obligation_assessment"]
        scope = assessment["scopes"][0]
        lines = "\n".join(scorecard_lines(card))
        self.assertEqual(scope["assessment_coverage"], 0.5)
        self.assertIsNone(scope["satisfaction_ratio"])
        self.assertEqual(scope["outcome"], "partially_verified")
        self.assertEqual(card["score_reason"], "source_obligations_unresolved")
        self.assertIn("仍有未核验、待人工或规范维度未知", lines)
        self.assertNotIn("满足率50.0%", lines)

    def test_source_bound_not_applicable_atom_is_excluded_and_not_marked_failed(self):
        unit = self.unit(1, applicability="not_applicable")
        inventory, receipt_audit, unit_map = self.scope_inputs([unit], [[]])
        card = build_scorecard(
            self.binding, receipt_audit, [], [], evaluation_units_by_requirement=unit_map,
            obligation_scope_inventory=inventory, expected_docx_sha256=self.docx_sha256,
        )
        assessment = card["obligation_assessment"]
        self.assertEqual(assessment["scopes"][0]["counts"]["not_applicable"], 1)
        self.assertIsNone(assessment["assessment_coverage"])
        self.assertIsNone(assessment["satisfaction_ratio"])
        self.assertEqual(assessment["scopes"][0]["outcome"], "not_assessable")
        self.assertEqual(card["score_reason"], "no_applicable_source_obligations")
        self.assertEqual(card["evaluation_units"][0]["status"], "not_applicable")
        self.assertEqual(card["entries"], [])

    def test_na_without_source_bound_evidence_and_stale_binding_reject(self):
        unit = self.unit(1, applicability="not_applicable")
        inventory, receipt_audit, unit_map = self.scope_inputs([unit], [[]], include_na_evidence=False)
        with self.assertRaisesRegex(ValueError, "source-bound applicability evidence"):
            build_scorecard(
                self.binding, receipt_audit, [], [], evaluation_units_by_requirement=unit_map,
                obligation_scope_inventory=inventory, expected_docx_sha256=self.docx_sha256,
            )
        inventory["scopes"][0]["applicability_evidence"] = {
            "source_sha256": "f" * 64, "clause_id": "C-1",
            "quote": "仅在论文含研究对象时适用。", "evidence_ids": ["E-1"],
        }
        with self.assertRaisesRegex(ValueError, "source-bound applicability evidence"):
            build_scorecard(
                self.binding, receipt_audit, [], [], evaluation_units_by_requirement=unit_map,
                obligation_scope_inventory=inventory, expected_docx_sha256=self.docx_sha256,
            )

    def test_incomplete_inventory_has_no_percentages_and_no_scalar_score(self):
        units = [self.unit(1), self.unit(2)]
        card = self.card(units=units, statuses=[["verified"], ["failed"]], complete=False)
        assessment = card["obligation_assessment"]
        self.assertEqual(assessment["status"], "scope_inventory_incomplete")
        self.assertIsNone(assessment["assessment_coverage"])
        self.assertIsNone(assessment["satisfaction_ratio"])
        self.assertEqual(card["score_reason"], "source_scope_inventory_incomplete")
        self.assertIsNone(card["score"])


class ScopeInventoryPipelineBindingTests(unittest.TestCase):
    def setUp(self):
        self.binding = {
            "run_id": "run-scope-test", "case_id": "case-scope-test",
            "source_sha256": "a" * 64, "format_spec_sha256": "b" * 64,
        }
        span = {"source_sha256": self.binding["source_sha256"], "clause_id": "C-1"}
        source_binding = {
            "source_sha256": self.binding["source_sha256"], "clause_id": "C-1",
            "source_span_sha256": sha256_json(span), "evidence_ids": ["E-1"],
        }
        unit = "EU-" + f"{1:032x}"
        self.inventory = {
            "schema_version": "1.0", "policy": "explicit_source_scope_inventory_v1",
            "binding": copy.deepcopy(self.binding), "inventory_complete": False,
            "scopes": [{
                "scope_id": expected_scope_id(source_binding, "abstract", "when supplied"),
                "source_binding": source_binding, "object_ref": "abstract",
                "condition": "when supplied", "inventory_complete": False,
                "expected_atom_ids": [unit],
                "atoms": [{"evaluation_unit_id": unit, "evidence_receipt_ids": []}],
                "importance": "not_assessed",
                "review_priority": {"effect": "sort_only_no_compliance_or_release_effect", "rank": None},
            }],
        }

    def test_pipeline_records_exact_scope_file_and_passes_its_path(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "scope.json"
            path.write_text(strict_json_dumps(self.inventory, ensure_ascii=False, indent=2), encoding="utf-8")
            manifest = {"inputs": {}}
            command = ["apply_format_spec.py"]
            bound = bind_scope_inventory_for_apply(
                path, manifest=manifest, apply_cmd=command, expected_binding=self.binding,
            )
            record = manifest["inputs"]["obligation_scope_inventory"]
            self.assertEqual(bound, self.inventory)
            self.assertEqual(command[-2:], ["--obligation-scope-inventory", str(path.resolve())])
            self.assertEqual(record["path"], str(path.resolve()))
            self.assertEqual(record["bytes"], path.stat().st_size)
            self.assertEqual(record["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(manifest["obligation_scope_inventory"]["status"], "bound")
            self.assertFalse(manifest["obligation_scope_inventory"]["submission_ready"])

    def test_pipeline_scope_path_rejects_stale_source_binding_or_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "scope.json"
            path.write_text(strict_json_dumps(self.inventory, ensure_ascii=False, indent=2), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "binding mismatch: source_sha256"):
                bind_scope_inventory_for_apply(
                    path, manifest={"inputs": {}}, apply_cmd=[],
                    expected_binding={**self.binding, "source_sha256": "f" * 64},
                )
            initial, record = load_bound_scope_inventory(
                path, expected_binding=self.binding,
                schema_path=ROOT / "schema" / "source-obligation-scope-inventory.schema.json",
            )
            path.write_text(path.read_text() + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "file record mismatch: bytes"):
                load_bound_scope_inventory(
                    path, expected_binding=self.binding,
                    schema_path=ROOT / "schema" / "source-obligation-scope-inventory.schema.json",
                    expected_file_record=record,
                )

    def test_missing_scope_is_explicit_in_pipeline_manifest(self):
        manifest = {"inputs": {}}
        command = ["apply_format_spec.py"]
        self.assertIsNone(bind_scope_inventory_for_apply(
            None, manifest=manifest, apply_cmd=command, expected_binding=self.binding,
        ))
        self.assertNotIn("--obligation-scope-inventory", command)
        self.assertEqual(manifest["obligation_scope_inventory"]["status"], "not_supplied")
        self.assertEqual(
            manifest["obligation_scope_inventory"]["reason"],
            "source_obligation_scope_inventory_missing",
        )


if __name__ == "__main__":
    unittest.main()
