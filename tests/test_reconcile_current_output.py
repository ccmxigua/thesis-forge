from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

from docx import Document

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from draft_scorecard import append_scorecard, audit_scorecard, build_scorecard  # noqa: E402
from reconcile_current_output import reconcile  # noqa: E402
from semantic_contract import sha256_json  # noqa: E402


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def rendered_report(docx: Path, *, with_font_finding: bool = True) -> dict:
    docx_sha = digest(docx)
    report = {
        "protocol": "rendered_format_audit_v1", "docx_sha256": docx_sha,
        "final_docx": {"sha256": docx_sha}, "pdf_sha256": "a" * 64,
        "findings": ([{"code": "rendered_pdf_font_mismatch", "expected": {
            "role": "body", "style_name": "Body", "script": "cjk",
            "name": "SimSun", "requirement_ids": ["R1"],
        }}] if with_font_finding else []),
        "submission_ready": False, "field_refresh_claimed": False,
    }
    report["audit_sha256"] = sha256_json(report)
    return report


class CurrentOutputReconciliationCliTests(unittest.TestCase):
    def test_cli_requires_rerender_after_visible_update_then_completes_hash_bound_audit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            docx = root / "review-draft.docx"
            scorecard_path = root / "scorecard.json"
            historical_path = root / "historical-clause-report.json"
            format_spec_path = root / "format-spec.json"
            style_map_path = root / "style-map.json"
            receipts_path = root / "property-receipt-audit.json"
            rendered_path = root / "rendered-report.json"
            output_path = root / "current-output-report.json"

            initial_sha = "0" * 64
            receipt_audit = {
                "valid": True, "missing_count": 0, "unexpected_count": 0,
                "duplicate_count": 0, "expected_receipt_ids": ["PR-R1-0001"],
                "receipts": [{"receipt_id": "PR-R1-0001", "requirement_id": "R1",
                    "role": "body", "property_path": "font.cjk", "target_locator": "style:Body",
                    "expected": "SimSun", "actual": "SimSun", "status": "verified",
                    "serialized_docx_sha256": initial_sha}],
            }
            card = build_scorecard(
                {"run_id": "historical-run", "case_id": "test",
                 "source_sha256": "b" * 64, "format_spec_sha256": "c" * 64},
                receipt_audit, [], [],
            )
            document = Document()
            append_scorecard(document, card)
            document.save(docx)
            write_json(scorecard_path, card)
            write_json(historical_path, {"mode": "full", "records": [{
                "clause_id": "C1", "scope": "docx", "status": "generated_and_verified",
                "requirement_ids": ["R1"], "reason": "historical pass",
            }]})
            write_json(format_spec_path, {"requirements": [{
                "id": "R1", "role": "body", "properties": {"font": {"cjk": "SimSun"}},
            }]})
            write_json(style_map_path, {"body": {"style_name": "Body"}})
            write_json(receipts_path, receipt_audit)
            write_json(rendered_path, rendered_report(docx))

            first, first_code = reconcile(
                historical_path, format_spec_path, style_map_path, receipts_path,
                rendered_path, docx, scorecard_path, output_path,
            )
            self.assertEqual(first_code, 2)
            self.assertEqual(first["status"], "rerender_required")
            self.assertTrue(first["scorecard_reconciliation"]["rendered_report_is_stale"])
            self.assertNotEqual(first["current_output"]["docx_sha256"], digest(docx))
            self.assertEqual(first["historical_records"][0]["status"], "generated_and_verified")
            self.assertEqual(first["records"][0]["historical_status"], "generated_and_verified")
            self.assertEqual(first["records"][0]["status"], "failed")

            # The visible scorecard changed the DOCX. A new serialized receipt
            # and PDF audit bind the exact resulting bytes before final attach.
            receipt_audit["receipts"][0]["serialized_docx_sha256"] = digest(docx)
            write_json(receipts_path, receipt_audit)
            write_json(rendered_path, rendered_report(docx))
            final, final_code = reconcile(
                historical_path, format_spec_path, style_map_path, receipts_path,
                rendered_path, docx, scorecard_path, output_path,
            )
            self.assertEqual(final_code, 0)
            self.assertEqual(final["status"], "complete")
            self.assertEqual(final["current_output_status"], "issues_found")
            self.assertEqual(final["records"][0]["status"], "failed")
            self.assertEqual(final["records"][0]["historical_status"], "generated_and_verified")
            self.assertEqual(final["current_output"]["docx_sha256"], digest(docx))
            self.assertTrue(audit_scorecard(docx, json.loads(scorecard_path.read_text()))["valid"])


if __name__ == "__main__":
    unittest.main()
