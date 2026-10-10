from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from docx import Document  # noqa: E402
from draft_scorecard import (append_scorecard, build_scorecard, reconcile_external_scorecard,
                             scorecard_lines)  # noqa: E402
from review_package_export import export_review_package, strip_scorecard_display  # noqa: E402
from semantic_contract import sha256_json  # noqa: E402


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class ReviewPackageExportTests(unittest.TestCase):
    def make_card(self, source_sha: str) -> dict:
        binding = {
            "run_id": "parent-run", "case_id": "bsu-test",
            "source_sha256": source_sha, "input_source_sha256": source_sha,
            "format_spec_sha256": "b" * 64,
        }
        receipt_audit = {
            "missing_count": 0, "unexpected_count": 0, "duplicate_count": 0,
            "expected_receipt_ids": ["r1"], "receipts": [{
                "receipt_id": "r1", "status": "unverified", "role": "body",
                "property_path": "font.cjk", "expected": "SimSun",
            }],
        }
        manual = [{
            "marker_id": "MR-0001", "source_code": "missing_cover_metadata:unit_code",
            "source_text": "unit_code 尚未确认", "reason": "用户尚未确认",
            "action": "请确认 unit_code", "clause_ids": [],
        }]
        return build_scorecard(binding, receipt_audit, manual, [])

    def test_strip_removes_only_exact_scorecard_and_retains_manual_marker(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source.docx"
            baseline = root / "baseline.docx"
            output = root / "paper.docx"
            scorecard_path = root / "scorecard.json"
            report_path = root / "split-report.json"
            Document().save(source)
            card = self.make_card(digest(source))
            doc = Document()
            doc.add_paragraph("原始论文正文")
            append_scorecard(doc, card)
            doc.add_paragraph("【MR-0001｜人工待审】unit_code 尚待确认")
            doc.add_paragraph("来源绑定的论文正文仍保留")
            doc.save(baseline)
            write_json(scorecard_path, card)

            result = strip_scorecard_display(source, baseline, scorecard_path, output, report_path)
            self.assertEqual(result["removed_scorecard_item_ids"],
                             [entry["item_id"] for entry in card["entries"]])
            self.assertEqual(result["retained_manual_review_marker_count"], 1)
            self.assertEqual(result["changed_ooxml_parts"], ["word/document.xml"])
            text = "\n".join(p.text for p in Document(output).paragraphs)
            self.assertNotIn("【SC-", text)
            self.assertNotIn("自动核验评分（", text)
            self.assertIn("【MR-0001｜人工待审】", text)
            self.assertIn("来源绑定的论文正文仍保留", text)

    def test_strip_rejects_scorecard_or_manual_marker_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source.docx"
            baseline = root / "baseline.docx"
            output = root / "paper.docx"
            card_path = root / "scorecard.json"
            report = root / "report.json"
            Document().save(source)
            card = self.make_card(digest(source))
            doc = Document()
            doc.add_paragraph("body")
            append_scorecard(doc, card)
            doc.add_paragraph("【MR-0002｜人工待审】错误标记")
            doc.save(baseline)
            write_json(card_path, card)
            with self.assertRaisesRegex(ValueError, "manual-review markers"):
                strip_scorecard_display(source, baseline, card_path, output, report)

            doc = Document()
            doc.add_paragraph("body")
            append_scorecard(doc, card)
            doc.paragraphs[0].text = "tampered summary"
            doc.add_paragraph("【MR-0001｜人工待审】标记")
            doc.save(baseline)
            with self.assertRaisesRegex(ValueError, "front block"):
                strip_scorecard_display(source, baseline, card_path, output, report)

    def test_external_ledger_exports_all_entries_and_current_binding(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source.docx"
            paper = root / "paper.docx"
            scorecard_path = root / "scorecard.json"
            reconciliation_path = root / "reconciliation.json"
            ledger_docx = root / "review-ledger.docx"
            ledger_json = root / "review-ledger.json"
            summary = root / "summary.md"
            Document().save(source)
            doc = Document()
            doc.add_paragraph("【MR-0001｜人工待审】unit_code 尚待确认")
            doc.add_paragraph("论文正文")
            doc.save(paper)
            card = self.make_card(digest(source))
            report = {
                "protocol": "rendered_format_audit_v1",
                "docx_sha256": digest(paper),
                "final_docx": {"sha256": digest(paper)},
                "pdf_sha256": "c" * 64,
                "findings": [{"code": "drawing_not_found_in_rendered_pdf", "drawing_index": 1}],
                "submission_ready": False, "field_refresh_claimed": False,
            }
            report["audit_sha256"] = sha256_json(report)
            current_card = reconcile_external_scorecard(card, report, paper)
            write_json(scorecard_path, current_card)
            reconciliation = {
                "status": "complete", "submission_ready": False,
                "model_request_made": False, "semantic_review_is_new": False,
                "current_output": {"docx_sha256": digest(paper), "pdf_sha256": "c" * 64},
                "scorecard_reconciliation": {"status": "external_ledger_only"},
            }
            write_json(reconciliation_path, reconciliation)

            result = export_review_package(source, paper, scorecard_path, reconciliation_path,
                                           ledger_docx, ledger_json, summary)
            self.assertEqual(result["item_count"], current_card["item_count"])
            self.assertFalse(result["submission_ready"])
            wrapper = json.loads(ledger_json.read_text())
            self.assertEqual(len(wrapper["scorecard"]["entries"]), current_card["item_count"])
            self.assertEqual(wrapper["paper_docx"]["sha256"], digest(paper))
            self.assertIn("unit_code", summary.read_text())
            editable = Document(ledger_docx)
            self.assertEqual(len(editable.tables[-1].rows) - 1, current_card["item_count"])

    def test_external_package_rejects_stale_output_hash_and_release_claim(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source.docx"
            paper = root / "paper.docx"
            Document().save(source)
            doc = Document()
            doc.add_paragraph("paper")
            doc.save(paper)
            card = self.make_card(digest(source))
            report = {"protocol": "rendered_format_audit_v1", "docx_sha256": digest(paper),
                      "final_docx": {"sha256": digest(paper)}, "pdf_sha256": "c" * 64,
                      "findings": [], "submission_ready": False, "field_refresh_claimed": False}
            report["audit_sha256"] = sha256_json(report)
            write_json(root / "scorecard.json", reconcile_external_scorecard(card, report, paper))
            recon = {"status": "complete", "submission_ready": False,
                     "model_request_made": False, "semantic_review_is_new": False,
                     "current_output": {"docx_sha256": "d" * 64, "pdf_sha256": "c" * 64}}
            write_json(root / "recon.json", recon)
            with self.assertRaisesRegex(ValueError, "not bound"):
                export_review_package(source, paper, root / "scorecard.json", root / "recon.json",
                                      root / "ledger.docx", root / "ledger.json", root / "summary.md")
            recon["current_output"]["docx_sha256"] = digest(paper)
            recon["submission_ready"] = True
            write_json(root / "recon.json", recon)
            with self.assertRaisesRegex(ValueError, "inherited, non-model, and non-submission"):
                export_review_package(source, paper, root / "scorecard.json", root / "recon.json",
                                      root / "ledger.docx", root / "ledger.json", root / "summary.md")


if __name__ == "__main__":
    unittest.main()
