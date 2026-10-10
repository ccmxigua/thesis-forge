from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from compliance import finalize_records, reconcile_rendered_output_records, summarize  # noqa: E402
from semantic_contract import sha256_json  # noqa: E402


DOCX_SHA = "a" * 64


def receipt(status: str = "verified", *, docx_sha: str = DOCX_SHA,
            property_path: str = "font.cjk", target: str = "style:Table Text") -> dict:
    return {
        "receipt_id": "PR-R1-0001", "requirement_id": "R1", "role": "table_text",
        "property_path": property_path, "target_locator": target,
        "expected": "SimSun", "actual": "SimSun" if status == "verified" else "Noto Sans CJK",
        "status": status, "serialized_docx_sha256": docx_sha,
    }


class CurrentOutputComplianceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.records = [{
            "clause_id": "C1", "scope": "docx", "status": "generated_and_verified",
            "requirement_ids": ["R1"], "reason": "Historical pass from semantic review.",
        }]
        self.requirements = [{
            "id": "R1", "role": "table_text", "properties": {"font": {"cjk": "SimSun"}},
        }]

    def audit(self, rows: list[dict]) -> dict:
        return {"expected_receipt_ids": ["PR-R1-0001"], "receipts": rows}

    def finalize(self, rows: list[dict]):
        return finalize_records(
            self.records, self.requirements, {"table_text": True}, set(),
            current_docx_sha256=DOCX_SHA, property_receipt_audit=self.audit(rows),
            role_mappings={"table_text": {"style_name": "Table Text"}},
        )

    def test_current_receipt_pass_preserves_prior_verified_state(self) -> None:
        result = self.finalize([receipt()])[0]
        self.assertEqual(result["status"], "generated_and_verified")
        self.assertEqual(result["historical_status"], "generated_and_verified")
        self.assertEqual(result["current_output_instance"]["status"], "verified")
        self.assertEqual(result["current_output_instance"]["docx_sha256"], DOCX_SHA)

    def test_current_exact_failure_overrides_prior_verified_and_blocks_compliance(self) -> None:
        result = self.finalize([receipt("failed")])[0]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["historical_status"], "generated_and_verified")
        self.assertEqual(result["current_output_instance"]["status"], "failed")
        self.assertFalse(summarize([result])["docx_fully_compliant"])

    def test_missing_or_wrong_hash_receipt_is_unverifiable_not_failed(self) -> None:
        missing = self.finalize([])[0]
        wrong_hash = self.finalize([receipt(docx_sha="b" * 64)])[0]
        self.assertEqual(missing["status"], "unverifiable")
        self.assertEqual(wrong_hash["status"], "unverifiable")
        self.assertEqual(missing["current_output_instance"]["status"], "unverified")
        self.assertEqual(wrong_hash["current_output_instance"]["status"], "unverified")
        self.assertEqual(missing["historical_status"], "generated_and_verified")

    def test_wrong_property_or_target_cannot_borrow_another_receipt(self) -> None:
        wrong_path = receipt(property_path="font.latin")
        wrong_target = receipt(target="style:Another Style")
        for candidate in (wrong_path, wrong_target):
            with self.subTest(candidate=candidate):
                result = self.finalize([candidate])[0]
                self.assertEqual(result["status"], "unverifiable")
                self.assertEqual(result["current_output_instance"]["status"], "unverified")

    def test_receipt_failure_is_used_even_when_role_snapshot_has_no_finding(self) -> None:
        # Reproduces the old call-order bug: finding_roles was snapshotted before
        # full-mode property receipt findings were appended.
        result = finalize_records(
            [{**self.records[0], "status": "pending_execution"}],
            self.requirements, {"table_text": True}, set(),
            current_docx_sha256=DOCX_SHA, property_receipt_audit=self.audit([receipt("failed")]),
            role_mappings={"table_text": {"style_name": "Table Text"}},
        )[0]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["current_output_instance"]["findings"][0]["receipt_id"], "PR-R1-0001")

    def test_instance_reconciliation_does_not_promote_historical_pending_clause(self) -> None:
        result = finalize_records(
            [{**self.records[0], "status": "pending_execution"}],
            self.requirements, {}, set(), current_docx_sha256=DOCX_SHA,
            property_receipt_audit=self.audit([receipt()]),
            role_mappings={"table_text": {"style_name": "Table Text"}},
            promote_pending=False,
        )[0]
        self.assertEqual(result["status"], "pending_execution")
        self.assertNotIn("current_output_instance", result)

    def test_unrelated_historical_clause_is_not_reclassified_without_its_binding(self) -> None:
        unrelated = {**self.records[0], "clause_id": "C2", "requirement_ids": ["R2"]}
        result = finalize_records(
            [self.records[0], unrelated], self.requirements, {"table_text": True}, set(),
            current_docx_sha256=DOCX_SHA, property_receipt_audit=self.audit([receipt("failed")]),
            role_mappings={"table_text": {"style_name": "Table Text"}},
        )
        self.assertEqual(result[0]["status"], "failed")
        self.assertEqual(result[1]["status"], "unverifiable")
        self.assertEqual(result[1]["historical_status"], "generated_and_verified")

    def test_wrong_nonempty_target_locator_is_unverified(self) -> None:
        result = self.finalize([receipt(target="style:Wrong Table Style")])[0]
        self.assertEqual(result["status"], "unverifiable")
        self.assertEqual(result["current_output_instance"]["status"], "unverified")

    def test_rendered_font_finding_binds_only_exact_required_font_and_requirement(self) -> None:
        second = {"id": "R2", "role": "table_text",
                  "properties": {"font": {"cjk": "FangSong"}}}
        records = [
            {"clause_id": "C1", "scope": "docx", "status": "generated_and_verified",
             "requirement_ids": ["R1"]},
            {"clause_id": "C2", "scope": "docx", "status": "generated_and_verified",
             "requirement_ids": ["R2"]},
        ]
        requirements = [self.requirements[0], second]
        report = {
            "protocol": "rendered_format_audit_v1", "docx_sha256": DOCX_SHA,
            "final_docx": {"sha256": DOCX_SHA}, "pdf_sha256": "b" * 64,
            "submission_ready": False, "field_refresh_claimed": False,
            "findings": [{"code": "rendered_pdf_font_mismatch", "expected": {
                "role": "table_text", "style_name": "Table Text", "script": "cjk",
                "name": "SimSun", "requirement_ids": ["R1"],
            }}],
        }
        report["audit_sha256"] = sha256_json(report)
        result = reconcile_rendered_output_records(
            records, requirements, {"table_text": {"style_name": "Table Text"}},
            report, DOCX_SHA,
        )
        by_clause = {item["clause_id"]: item for item in result["records"]}
        self.assertEqual(by_clause["C1"]["status"], "failed")
        self.assertEqual(by_clause["C2"]["status"], "unverifiable")
        self.assertEqual(result["bound_rendered_findings"][0]["requirement_ids"], ["R1"])
        self.assertEqual(records[0]["status"], "generated_and_verified")

    def test_rendered_report_wrong_requirement_ids_and_bad_hash_fail_closed(self) -> None:
        report = {
            "protocol": "rendered_format_audit_v1", "docx_sha256": DOCX_SHA,
            "final_docx": {"sha256": DOCX_SHA}, "pdf_sha256": "b" * 64,
            "submission_ready": False, "field_refresh_claimed": False,
            "findings": [{"code": "rendered_pdf_font_mismatch", "expected": {
                "role": "table_text", "style_name": "Table Text", "script": "cjk",
                "name": "SimSun", "requirement_ids": ["R2"],
            }}],
        }
        report["audit_sha256"] = sha256_json(report)
        result = reconcile_rendered_output_records(
            self.records, [*self.requirements,
                           {"id": "R2", "role": "table_text",
                            "properties": {"font": {"cjk": "FangSong"}}}],
            {"table_text": {"style_name": "Table Text"}}, report, DOCX_SHA,
        )
        self.assertEqual(result["records"][0]["status"], "unverifiable")
        self.assertEqual(result["unbound_rendered_findings"][0]["status"], "failed")
        tampered = {**report, "pdf_sha256": "c" * 64}
        with self.assertRaisesRegex(ValueError, "integrity hash"):
            reconcile_rendered_output_records(
                self.records, self.requirements, {"table_text": {"style_name": "Table Text"}},
                tampered, DOCX_SHA,
            )


if __name__ == "__main__":
    unittest.main()
