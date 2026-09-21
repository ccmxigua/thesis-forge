from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from apply_format_spec import append_manual_review_markers  # noqa: E402
from format_spec_validation import load_and_validate  # noqa: E402
from manual_review import build_manual_review_ledger  # noqa: E402
from submission_audit import PLACEHOLDER_PATTERNS  # noqa: E402


class ManualReviewTests(unittest.TestCase):
    def test_ledger_preserves_binding_and_merges_duplicate_question(self) -> None:
        report = {
            "findings": [{
                "code": "capability.clause_gap",
                "blocking": True,
                "message": "C00074 requires manual review",
                "evidence": [
                    {"kind": "clause_id", "value": "C00074"},
                    {"kind": "evidence_id", "value": "E00074"},
                    {"kind": "category", "value": "runtime_manual_unverifiable"},
                ],
            }],
        }
        questions = [{
            "question_id": "Q00074",
            "clause_id": "C00074",
            "evidence_id": "E00074",
            "question": "英文目标是否明确？",
        }]
        ledger = build_manual_review_ledger(
            report,
            questions,
            binding={
                "case_id": "bsu",
                "run_id": "run-1",
                "source_sha256": "0" * 64,
                "clause_sha256": "1" * 64,
                "evidence_sha256": "2" * 64,
            },
        )
        self.assertEqual(ledger["binding"]["run_id"], "run-1")
        self.assertFalse(ledger["submission_ready"])
        self.assertEqual(ledger["summary"]["total"], 2)
        self.assertEqual(
            {tuple(item["clause_ids"]) for item in ledger["items"]},
            {("C00074",)},
        )
        self.assertEqual(
            load_and_validate(ledger, ROOT / "schema" / "manual-review-ledger.schema.json"),
            [],
        )

    def test_markers_are_visible_and_red(self) -> None:
        document = Document()
        ledger = {
            "schema_version": "1.0",
            "policy": "review_draft_only",
            "binding": {"run_id": "run-1"},
            "submission_ready": False,
            "items": [{
                "marker_id": "MR-0001",
                "status": "pending_manual_review",
                "marker_required": True,
                "category": "input_prerequisite",
                "clause_ids": ["C00076"],
                "requirement_ids": [],
                "source_text": "300–1000 words",
                "reason": "目标语言和单位需要人工确认",
                "action": "请确认后回填",
                "placeholder_text": "【待人工处理：C00076】",
                "original_blocking": True,
            }],
        }
        receipts = append_manual_review_markers(document, ledger)
        self.assertEqual(len(receipts), 1)
        self.assertIn("MR-0001", document.paragraphs[-1].text)
        self.assertIn("【待人工处理：C00076】", document.paragraphs[-1].text)
        self.assertEqual(receipts[0]["placeholder_text"], "【待人工处理：C00076】")
        run = document.paragraphs[-1].runs[0]
        self.assertEqual(str(run.font.color.rgb), "C00000")
        shading = run._r.rPr.find(qn("w:shd"))
        self.assertIsNotNone(shading)
        self.assertEqual(shading.get(qn("w:fill")), "FFF2CC")

    def test_official_template_gate_is_a_run_bound_red_placeholder(self) -> None:
        ledger = build_manual_review_ledger(
            {}, [],
            release_gates=[{
                "source_code": "official_template_missing",
                "category": "input_prerequisite",
                "source_text": "官方版式模板未提供",
                "reason": "当前仅有中性参考文档。",
                "action": "提供官方模板后重新运行。",
                "placeholder_text": "【待提供：官方版式模板】",
            }],
            binding={
                "case_id": "bsu",
                "run_id": "run-1",
                "source_sha256": "0" * 64,
                "clause_sha256": "1" * 64,
                "evidence_sha256": "2" * 64,
            },
        )
        self.assertEqual(ledger["visual_policy"]["text_color"], "C00000")
        self.assertFalse(ledger["submission_ready"])
        item = ledger["items"][0]
        self.assertEqual(item["source_code"], "official_template_missing")
        self.assertEqual(item["placeholder_text"], "【待提供：官方版式模板】")
        self.assertEqual(
            load_and_validate(ledger, ROOT / "schema" / "manual-review-ledger.schema.json"),
            [],
        )

        document = Document()
        receipts = append_manual_review_markers(document, ledger)
        self.assertEqual(receipts[0]["placeholder_text"], "【待提供：官方版式模板】")
        self.assertIn("【待提供：官方版式模板】", document.paragraphs[-1].text)
        self.assertEqual(str(document.paragraphs[-1].runs[0].font.color.rgb), "C00000")

    def test_nonblocking_confirmed_semantic_issue_still_gets_marker(self) -> None:
        ledger = build_manual_review_ledger(
            {
                "findings": [{
                    "code": "capability.confirmed_semantic_issue",
                    "blocking": False,
                    "message": "Clause C00074 remains semantically unresolved.",
                    "evidence": [
                        {"kind": "category", "value": "confirmed_semantic_issue"},
                        {"kind": "clause_id", "value": "C00074"},
                    ],
                }],
            },
            [],
            binding={
                "case_id": "bsu",
                "run_id": "run-1",
                "source_sha256": "0" * 64,
                "clause_sha256": "1" * 64,
                "evidence_sha256": "2" * 64,
            },
        )
        self.assertEqual(ledger["summary"]["total"], 1)
        self.assertEqual(ledger["items"][0]["category"], "confirmed_semantic_issue")
        self.assertFalse(ledger["items"][0]["original_blocking"])

    def test_strict_audit_recognizes_manual_marker_as_placeholder(self) -> None:
        pattern = dict(PLACEHOLDER_PATTERNS)["manual_review_marker"]
        self.assertIsNotNone(pattern.search("【MR-0001｜人工待审】"))


if __name__ == "__main__":
    unittest.main()
