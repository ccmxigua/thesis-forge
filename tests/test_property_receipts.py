from __future__ import annotations

import unittest

from scripts.property_receipts import (
    audit_property_receipts,
    build_property_receipts,
    expected_receipt_ids,
)


class PropertyReceiptTests(unittest.TestCase):
    def test_style_properties_are_verified_at_property_level(self) -> None:
        requirements = [{
            "id": "R1", "role": "body_text", "clause_ids": ["C1"],
            "properties": {"font": {"cjk": "SimSun", "size_pt": 12}},
        }]
        receipts = build_property_receipts(
            requirements,
            {"body_text": {"style_name": "Thesis Body Text"}},
            {"body_text": {"font": {"cjk": "SimSun", "size_pt": 12}}},
            serialized_docx_sha256="a" * 64,
        )
        self.assertEqual({item["property_path"] for item in receipts}, {"font.cjk", "font.size_pt"})
        self.assertTrue(all(item["status"] == "verified" for item in receipts))
        self.assertTrue(audit_property_receipts(receipts)["valid"])
        self.assertTrue(all(item["target_locator"] == "style:Thesis Body Text" for item in receipts))

    def test_structural_property_without_backend_receipt_is_unverified(self) -> None:
        receipts = build_property_receipts(
            [{"id": "R2", "role": "page", "clause_ids": ["C2"],
              "properties": {"page_number": {"body_format": "decimal"}}}],
            {}, {}, serialized_docx_sha256="b" * 64,
        )
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["status"], "unverified")
        self.assertFalse(audit_property_receipts(receipts)["valid"])

    def test_absent_conditional_role_does_not_create_failed_requirement_receipt(self) -> None:
        receipts = build_property_receipts(
            [{"id": "R3", "role": "heading_4", "clause_ids": ["C3"],
              "properties": {"numbering": {"format": "1.1.1.1"}}}],
            {"heading_4": {"style_name": "Heading 4"}},
            {"heading_4": {"font": {"size_pt": 12}}},
            serialized_docx_sha256="c" * 64,
            applicable_roles=set(),
        )
        self.assertEqual(receipts, [])
        self.assertTrue(audit_property_receipts(receipts)["valid"])

    def test_count_bounds_are_constraints_not_exact_values(self) -> None:
        receipts = build_property_receipts(
            [{"id": "R4", "role": "content_constraints", "clause_ids": ["C4"],
              "properties": {"keywords_zh": {"min_count": 3, "max_count": 8}}}],
            {}, {"content_constraints": {"keywords_zh": {"min_count": 5, "max_count": 5}}},
            serialized_docx_sha256="d" * 64,
        )
        self.assertEqual({item["status"] for item in receipts}, {"verified"})

    def test_max_item_chars_receipt_uses_upper_bound_semantics(self) -> None:
        receipts = build_property_receipts(
            [{"id": "R6", "role": "content_constraints", "clause_ids": ["C6"],
              "properties": {"keywords_zh": {"max_item_chars": 7}}}],
            {},
            {"content_constraints": {"keywords_zh": {"max_item_chars": 6}}},
            serialized_docx_sha256="e" * 64,
        )
        self.assertEqual(receipts[0]["status"], "verified")

    def test_boolean_is_not_accepted_as_a_numeric_bound_measurement(self) -> None:
        receipts = build_property_receipts(
            [{"id": "R7", "role": "content_constraints", "clause_ids": ["C7"],
              "properties": {"keywords_zh": {"max_count": 7}}}],
            {},
            {"content_constraints": {"keywords_zh": {"max_count": True}}},
            serialized_docx_sha256="f" * 64,
        )
        self.assertEqual(receipts[0]["status"], "failed")

    def test_expected_receipt_set_rejects_silent_missing_receipt(self) -> None:
        expected = expected_receipt_ids([{
            "id": "R5", "role": "body_text", "properties": {"font": {"cjk": "SimSun"}},
        }])
        audit = audit_property_receipts([], expected_receipt_ids=expected)
        self.assertFalse(audit["valid"])
        self.assertEqual(audit["missing_count"], 1)
        self.assertEqual(audit["failures"][0]["status"], "missing")


if __name__ == "__main__":
    unittest.main()
