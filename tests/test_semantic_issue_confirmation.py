from __future__ import annotations

import copy
import importlib
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from semantic_contract import sha256_json  # noqa: E402
from semantic_issue_confirmation import (  # noqa: E402
    bind_confirmations,
    build_confirmation_receipt,
    confirmed_clause_ids,
    validate_confirmation_receipt,
    validate_bound_ledger_for_spec,
)


class SemanticIssueConfirmationTest(unittest.TestCase):
    @staticmethod
    def _fixtures() -> tuple[dict, list[dict], list[dict]]:
        provenance = {
            "version": "1.0",
            "origin": "fresh_host_agent",
            "source_sha256": "1" * 64,
            "evidence_sha256": "2" * 64,
            "clause_sha256": "3" * 64,
            "request_sha256": "4" * 64,
            "run_id": "run-bsu-26",
        }
        clauses = [
            {"id": "C00074", "text": "The following English is not correct.", "evidence_ids": ["E00063"]},
            {"id": "C00076", "text": "The Chinese abstract is ... 300 to 1,000 words", "evidence_ids": ["E00065"]},
            {"id": "C00421", "text": "3cm左右", "evidence_ids": ["E00346"]},
        ]
        questions = [
            {"id": "Q0001", "clause_id": "C00074", "question": "需要人工确认", "evidence_ids": ["E00063"]},
            {"id": "Q0002", "clause_id": "C00076", "question": "需要人工确认", "evidence_ids": ["E00065"]},
            {"id": "Q0003", "clause_id": "C00421", "question": "需要人工确认", "evidence_ids": ["E00346"]},
        ]
        spec = {
            "run_id": "run-bsu-26",
            "status": "needs_clarification",
            "semantic_review_provenance_valid": True,
            "semantic_review_provenance": provenance,
            "completeness": {"unresolved_clause_ids": [item["id"] for item in clauses]},
            "clause_compliance": [
                {"clause_id": item["id"], "status": "unresolved", "requirement_ids": [],
                 "evidence_ids": item["evidence_ids"]}
                for item in clauses
            ],
        }
        return spec, questions, clauses

    @staticmethod
    def _raw() -> dict:
        return {
            "schema_version": "1.0",
            "case_id": "bsu",
            "scope": "analysis_only",
            "disposition": "confirmed_semantic_issue",
            "source_binding": {
                "run_id": "run-bsu-26",
                "case_id": "bsu",
                "source_sha256": "1" * 64,
                "evidence_sha256": "2" * 64,
                "clause_sha256": "3" * 64,
                "request_sha256": "4" * 64,
            },
            "confirmations": [
                {"clause_id": "C00074", "question_id": "Q0001", "evidence_ids": ["E00063"],
                 "reason": "英文提示与相邻摘要内容的目标关系不明确。"},
                {"clause_id": "C00076", "question_id": "Q0002", "evidence_ids": ["E00065"],
                 "reason": "Chinese abstract 与英文段落的适用对象和字数指标冲突。"},
                {"clause_id": "C00421", "question_id": "Q0003", "evidence_ids": ["E00346"],
                 "reason": "3cm 标注缺少受约束对象和位置证据。"},
            ],
        }

    def test_bind_is_current_run_bound_and_preserves_unresolved_semantics(self):
        spec, questions, clauses = self._fixtures()
        ledger = bind_confirmations(self._raw(), spec, questions, clauses, expected_case_id="bsu")
        self.assertEqual(confirmed_clause_ids(ledger), {"C00074", "C00076", "C00421"})
        self.assertEqual(ledger["scope"], "analysis_only")
        self.assertEqual(ledger["disposition"], "confirmed_semantic_issue")
        self.assertEqual(ledger["binding"]["run_id"], "run-bsu-26")
        self.assertEqual(ledger["binding"]["questions_sha256"], sha256_json(questions))
        self.assertEqual(ledger["binding"]["clauses_sha256"], sha256_json(clauses))
        self.assertEqual(validate_bound_ledger_for_spec(ledger, spec, expected_case_id="bsu"),
                         {"C00074", "C00076", "C00421"})
        self.assertTrue(all(item["clause_sha256"] and item["question_sha256"] for item in ledger["confirmations"]))
        receipt = build_confirmation_receipt(ledger)
        validate_confirmation_receipt(receipt, ledger)
        self.assertEqual(receipt["confirmed_clause_ids"], ["C00074", "C00076", "C00421"])
        self.assertEqual(receipt["binding"], ledger["binding"])

    def test_rejects_unknown_question_and_wrong_evidence(self):
        spec, questions, clauses = self._fixtures()
        unknown_clause = copy.deepcopy(self._raw())
        unknown_clause["confirmations"][0]["clause_id"] = "C9999"
        with self.assertRaisesRegex(ValueError, "unknown clause_id"):
            bind_confirmations(unknown_clause, spec, questions, clauses, expected_case_id="bsu")

        unknown_question = copy.deepcopy(self._raw())
        unknown_question["confirmations"][0]["question_id"] = "Q9999"
        with self.assertRaisesRegex(ValueError, "unknown question_id"):
            bind_confirmations(unknown_question, spec, questions, clauses, expected_case_id="bsu")

        wrong_evidence = copy.deepcopy(self._raw())
        wrong_evidence["confirmations"][0]["evidence_ids"] = ["E9999"]
        with self.assertRaisesRegex(ValueError, "evidence_ids"):
            bind_confirmations(wrong_evidence, spec, questions, clauses, expected_case_id="bsu")

        duplicate = copy.deepcopy(self._raw())
        duplicate["confirmations"].append(copy.deepcopy(duplicate["confirmations"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            bind_confirmations(duplicate, spec, questions, clauses, expected_case_id="bsu")

    def test_rejects_stale_run_and_non_unresolved_clause(self):
        spec, questions, clauses = self._fixtures()
        with self.assertRaisesRegex(ValueError, "current case_id is required"):
            bind_confirmations(self._raw(), spec, questions, clauses)
        stale = copy.deepcopy(self._raw())
        stale["case_id"] = "other-school"
        with self.assertRaisesRegex(ValueError, "case_id mismatch"):
            bind_confirmations(stale, spec, questions, clauses, expected_case_id="bsu")

        stale_binding = copy.deepcopy(self._raw())
        stale_binding["source_binding"]["run_id"] = "old-run"
        with self.assertRaisesRegex(ValueError, "source binding"):
            bind_confirmations(stale_binding, spec, questions, clauses, expected_case_id="bsu")

        stale_source_hash = copy.deepcopy(self._raw())
        stale_source_hash["source_binding"]["source_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "source binding"):
            bind_confirmations(stale_source_hash, spec, questions, clauses, expected_case_id="bsu")

        ledger = bind_confirmations(self._raw(), spec, questions, clauses, expected_case_id="bsu")
        with self.assertRaisesRegex(ValueError, "must be unbound"):
            bind_confirmations(ledger, spec, questions, clauses, expected_case_id="bsu")

        resolved_spec = copy.deepcopy(spec)
        resolved_spec["clause_compliance"][0]["status"] = "informational"
        with self.assertRaisesRegex(ValueError, "not currently unresolved"):
            bind_confirmations(self._raw(), resolved_spec, questions, clauses, expected_case_id="bsu")

    def test_supported_subset_relief_does_not_apply_to_full(self):
        pipeline = importlib.import_module("thesis_format_pipeline")
        spec, questions, _ = self._fixtures()
        ids = {"C00074", "C00076", "C00421"}
        self.assertEqual(
            pipeline.requirement_blockers(spec, questions, "supported_subset", set(), ids), []
        )
        full_blockers = pipeline.requirement_blockers(spec, questions, "full", set(), ids)
        self.assertIn("confirmed_semantic_issues", full_blockers)
        self.assertIn("status_needs_clarification", full_blockers)

    def test_apply_gate_has_the_same_full_release_boundary(self):
        apply_module = importlib.import_module("apply_format_spec")
        spec, _, _ = self._fixtures()
        ids = {"C00074", "C00076", "C00421"}
        self.assertEqual(apply_module.format_spec_blockers(spec, "supported_subset", ids), [])
        self.assertIn("confirmed_semantic_issues", apply_module.format_spec_blockers(spec, "full", ids))

    def test_other_unresolved_questions_still_block_supported_subset(self):
        pipeline = importlib.import_module("thesis_format_pipeline")
        spec, questions, _ = self._fixtures()
        spec["completeness"]["unresolved_clause_ids"].append("C00999")
        spec["clause_compliance"].append(
            {"clause_id": "C00999", "status": "unresolved", "requirement_ids": []}
        )
        questions.append(
            {"id": "Q00999", "clause_id": "C00999", "question": "仍需确认", "evidence_ids": ["E00999"]}
        )
        blockers = pipeline.requirement_blockers(
            spec, questions, "supported_subset", set(), {"C00074", "C00076", "C00421"}
        )
        self.assertIn("unresolved_clauses", blockers)
        self.assertIn("open_questions", blockers)

    def test_capability_report_separates_confirmed_semantic_issues(self):
        planner = importlib.import_module("capability_planner")
        spec, questions, clauses = self._fixtures()
        ledger = bind_confirmations(self._raw(), spec, questions, clauses, expected_case_id="bsu")
        registry = json.loads((ROOT / "resources/backend-capabilities.default.json").read_text())
        subset = planner.plan_capabilities(
            spec, registry, "supported_subset", extracted_clauses=clauses,
            semantic_issue_ledger=ledger,
        )
        self.assertEqual(subset["status"], "analysis_ready_with_confirmed_semantic_issues")
        self.assertEqual(subset["summary"]["confirmed_semantic_issues"], 3)
        self.assertEqual(subset["summary"]["backend_capability_gaps"], 0)
        self.assertFalse(any(item["blocking"] for item in subset["findings"]))
        self.assertTrue(all(item["category"] == "confirmed_semantic_issue" for item in subset["clauses"]))
        self.assertEqual(
            {item["clause_id"] for item in subset["semantic_issue_confirmations"]},
            {"C00074", "C00076", "C00421"},
        )
        self.assertEqual(subset["semantic_issue_binding"], ledger["binding"])
        self.assertTrue(all(
            item["semantic_issue"]["question_sha256"]
            and item["semantic_issue"]["clause_sha256"]
            for item in subset["clauses"]
        ))

        full = planner.plan_capabilities(
            spec, registry, "full", extracted_clauses=clauses,
            semantic_issue_ledger=ledger,
        )
        self.assertEqual(full["status"], "blocked")
        self.assertEqual(full["summary"]["confirmed_semantic_issue_blockers"], 3)
        self.assertTrue(all(item["blocking"] for item in full["findings"]))


if __name__ == "__main__":
    unittest.main()
