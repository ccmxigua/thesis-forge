from __future__ import annotations

import hashlib
import io
import sys
import unittest
from pathlib import Path

from docx import Document

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import apply_format_spec  # noqa: E402
from draft_scorecard import build_scorecard, scorecard_lines  # noqa: E402
from manual_review import build_manual_review_ledger  # noqa: E402
from property_receipts import audit_property_receipts, build_property_receipts  # noqa: E402
from source_binding_report import build_source_binding_report  # noqa: E402
from source_scope_binding import diagnose_source_scope_reference  # noqa: E402


def _binding() -> dict:
    return {
        "case_id": "tjufe-adjacent-synthetic",
        "run_id": "offline-report-fixture-01",
        "source_sha256": "a" * 64,
        "clause_sha256": "b" * 64,
        "evidence_sha256": "c" * 64,
        "request_sha256": None,
        "requirements_sha256": "d" * 64,
        "input_source_sha256": "e" * 64,
        "format_spec_sha256": "f" * 64,
        "official_template_sha256": None,
        "official_template_source": "not_supplied",
    }


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class SourceBindingReportTests(unittest.TestCase):
    def test_adjacent_clause_question_and_approval_survive_todo_and_scorecard(self) -> None:
        clause_a = {"id": "C1", "text": "第一条：封面字段格式。", "evidence_ids": ["E1"]}
        clause_b = {"id": "C2", "text": "第一条：正文段落格式。", "evidence_ids": ["E2"]}
        ambiguous_clause = {
            "id": "C3", "text": "第一条中导师姓名使用小四号。", "evidence_ids": ["E3"],
        }
        diagnosis = diagnose_source_scope_reference(
            ambiguous_clause, [clause_a, clause_b, ambiguous_clause],
        )
        self.assertEqual(diagnosis["classification"], "semantic_ambiguity")
        ambiguity = {
            "question_id": "Q1", "clause_id": "C3", "evidence_ids": ["E3"],
            "source_text": ambiguous_clause["text"], "question": "请明确所指条款。",
            "source_scope_diagnosis": diagnosis,
        }
        parser_gap = {
            "question_id": "Q2", "clause_id": "C4", "evidence_ids": ["E4"],
            "source_text": "字段按下表规定填写。", "question": "目标表格锚点不可见。",
            "source_scope_diagnosis": {
                "status": "pending_input", "classification": "parser_capability_gap",
                "reason": "forward_target_not_structurally_observable",
                "source_clause_id": "C4", "candidate_clause_ids": [],
                "resolution_policy": "human_selects_exact_clause_ids_before_execution",
            },
        }
        approval = {
            "source_code": "signatory_approval", "category": "semantic_content_review",
            "source_binding_classification": "human_approval_required",
            "clause_ids": ["C5"], "evidence_ids": ["E5"],
            "source_text": "导师签字确认。", "reason": "需要有权人员完成签字。",
            "action": "由导师本人签字；系统不得代签或自动标记完成。",
            "execution_authorized": False,
        }
        ledger = build_manual_review_ledger(
            {}, [ambiguity, parser_gap], binding=_binding(), release_gates=[approval],
        )
        by_class = {
            item.get("source_scope_diagnosis", {}).get("classification"): item
            for item in ledger["items"] if item.get("source_scope_diagnosis")
        }
        self.assertEqual(by_class["semantic_ambiguity"]["source_scope_diagnosis"]["candidate_clause_ids"], ["C1", "C2"])
        self.assertIn("不会按邻近位置代选", by_class["semantic_ambiguity"]["action"])
        self.assertIn("补充可读取的结构锚点", by_class["parser_capability_gap"]["action"])
        approval_item = next(item for item in ledger["items"] if item.get("source_binding_classification"))
        self.assertEqual(approval_item["source_binding_classification"], "human_approval_required")
        self.assertFalse(approval_item["execution_authorized"])

        docx_hash = _sha(b"synthetic-output-docx")
        receipts = [
            {"receipt_id": "PR-cover-fields", "requirement_id": "R-COVER",
             "clause_ids": ["C10"], "role": "cover", "property_path": "fields",
             "target_locator": "role:cover", "expected": [{"id": "title_zh"}],
             "actual": [], "status": "failed", "status_reason": "封面实例缺失。",
             "diagnostic_classification": "missing_target_instance",
             "diagnostic_summary": "规范对象已解析、缺实际封面实例",
             "serialized_docx_sha256": docx_hash},
            {"receipt_id": "PR-scoped-style", "requirement_id": "R-SCOPE",
             "clause_ids": ["C1", "C2"], "role": "cover_field_label",
             "property_path": "font.size_pt", "target_locator": "content_instances:CFI-1",
             "expected": 12, "actual": None, "status": "unverified",
             "status_reason": "绑定实例尚未取得序列化观察。",
             "serialized_docx_sha256": docx_hash},
            {"receipt_id": "PR-body-font", "requirement_id": "R-BODY",
             "clause_ids": ["C20"], "role": "body_text", "property_path": "font.size_pt",
             "target_locator": "style:Thesis Body", "expected": 12, "actual": 12,
             "status": "verified", "serialized_docx_sha256": docx_hash},
        ]
        receipt_audit = audit_property_receipts(
            receipts, expected_receipt_ids={item["receipt_id"] for item in receipts},
        )
        card = build_scorecard(_binding(), receipt_audit, ledger["items"], [], [])
        lines = "\n".join(scorecard_lines(card))
        self.assertIn("规范对象已解析、缺实际封面实例", lines)
        self.assertIn("来源范围歧义，待人工选择精确条款", lines)
        self.assertIn("需有权人员签字或审批", lines)
        self.assertIn("source_scope_diagnosis", lines)

        report = build_source_binding_report(
            _binding(), source_docx_sha256="1" * 64,
            output_docx_sha256=docx_hash,
            questions=[ambiguity, parser_gap],
            content_instance_audit=[{
                "instance_id": "CFI-1", "role": "cover_field_label",
                "text": "导师姓名", "status": "not_present", "match_count": 0,
                "source_bindings": [{"clause_id": "C10", "text": "导师姓名"}],
                "serialized_target_status": "unverified",
                "serialized_target_errors": ["target_literal_not_present_in_serialized_paragraph"],
            }],
            property_receipts=receipts, manual_items=ledger["items"],
        )
        classes = {row["classification"] for row in report["outcomes"]}
        self.assertTrue({"semantic_ambiguity", "parser_capability_gap",
                         "missing_target_instance", "bound_but_unverified",
                         "human_approval_required", "property_passed"} <= classes)
        statuses = {row["status"] for row in report["outcomes"]}
        self.assertTrue({"pending_input", "failed", "unverified", "passed",
                         "pending_human_approval"} <= statuses)
        self.assertFalse(report["summary"]["human_approval_auto_satisfied"])
        self.assertFalse(report["submission_ready"])

    def test_receipt_classifies_empty_actual_cover_fields_as_missing_instance(self) -> None:
        expected_fields = [{
            "id": field_id, "label": label,
            "value_from": f"thesis_profile.cover_metadata.{field_id}",
            "display_policy": "required", "order": index,
        } for index, (field_id, label) in enumerate((
            ("title_zh", ""), ("author_name", "作者"),
            ("supervisor_name", "导师"), ("first_discipline", "一级学科"),
        ), start=1)]
        receipts = build_property_receipts(
            [{"id": "R-COVER", "role": "cover", "clause_ids": ["C10"],
              "properties": {"fields": expected_fields}}],
            {}, {"cover": {"fields": []}},
            serialized_docx_sha256="2" * 64,
        )
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["status"], "failed")
        self.assertEqual(receipts[0]["diagnostic_classification"], "missing_target_instance")
        self.assertEqual(receipts[0]["diagnostic_summary"], "规范对象已解析、缺实际封面实例")

    def test_complete_cover_checks_four_values_without_treating_labels_or_date_as_values(self) -> None:
        cover = {
            "institution": "合成测试机构",
            "fields": [{
                "id": field_id, "label": label,
                "value_from": f"thesis_profile.cover_metadata.{field_id}",
                "display_policy": "required", "order": index,
            } for index, (field_id, label) in enumerate((
                ("title_zh", ""), ("author_name", "作者"),
                ("supervisor_name", "导师"), ("first_discipline", "一级学科"),
            ), start=1)],
        }
        values = {
            "trust": {"source": "source_document", "confirmed": True},
            "title_zh": "合成论文题目", "author_name": "合成作者",
            "supervisor_name": "合成导师", "first_discipline": "合成学科",
        }
        profile = {"cover_metadata": values}
        contract = apply_format_spec.compile_cover_contract(cover, profile)
        doc = Document()
        apply_format_spec.apply_cover(doc, cover, profile, contract)
        output = io.BytesIO()
        doc.save(output)
        output.seek(0)
        serialized = Document(output)
        findings = apply_format_spec.audit_cover(serialized, cover, profile)
        self.assertEqual(findings, [])
        self.assertEqual([row["value"] for row in contract["fields"]], [
            "合成论文题目", "合成作者", "合成导师", "合成学科",
        ])
        self.assertEqual([row["id"] for row in contract["fields"]], [
            "title_zh", "author_name", "supervisor_name", "first_discipline",
        ])
        self.assertNotIn("completion_date", [row["id"] for row in contract["fields"]])
        doc_bytes = output.getvalue()
        self.assertEqual(_sha(doc_bytes), _sha(doc_bytes))

        actual, _methods, _observability, _fonts = apply_format_spec._receipt_semantic_actuals(
            serialized, {"cover": cover}, {},
            [{"id": "R-COVER", "role": "cover", "properties": {"fields": cover["fields"]}}],
            {}, [], contract,
        )
        receipt = build_property_receipts(
            [{"id": "R-COVER", "role": "cover", "clause_ids": ["C10"],
              "properties": {"fields": cover["fields"]}}],
            {}, actual, serialized_docx_sha256=_sha(doc_bytes),
        )[0]
        self.assertEqual(receipt["status"], "verified")
        self.assertNotIn("diagnostic_classification", receipt)

    def test_source_report_refuses_property_receipt_from_another_output(self) -> None:
        with self.assertRaisesRegex(ValueError, "DOCX hash"):
            build_source_binding_report(
                _binding(), source_docx_sha256="1" * 64,
                output_docx_sha256="2" * 64, questions=[],
                content_instance_audit=[],
                property_receipts=[{
                    "receipt_id": "PR-x", "status": "verified",
                    "serialized_docx_sha256": "3" * 64,
                }], manual_items=[],
            )


if __name__ == "__main__":
    unittest.main()
