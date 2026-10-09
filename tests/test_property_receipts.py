from __future__ import annotations

import unittest
import copy

from scripts.property_receipts import (
    audit_property_receipts,
    build_property_receipts,
    expected_receipt_ids,
    satisfies,
)


class PropertyReceiptTests(unittest.TestCase):
    def test_guidance_is_not_a_fabricated_execution_receipt(self) -> None:
        requirements = [{"id": "R_GUIDANCE", "role": "content_constraints", "properties": {
            "keywords_zh": {"count_guidance": {"min_count": 3, "max_count": 8,
                "strength": "general_guidance"}, "separator": "semicolon"},
        }}]
        original = copy.deepcopy(requirements)
        receipts = build_property_receipts(requirements, {}, {"content_constraints": {
            "keywords_zh": {"separator": "semicolon"},
        }}, serialized_docx_sha256="a" * 64)
        self.assertEqual(requirements, original)
        self.assertEqual([item["property_path"] for item in receipts], ["keywords_zh.separator"])
        self.assertEqual(receipts[0]["receipt_id"], "PR-R_GUIDANCE-0004")
        expected = expected_receipt_ids(requirements)
        self.assertTrue(audit_property_receipts(receipts, expected_receipt_ids=expected)["valid"])
        self.assertFalse(audit_property_receipts([], expected_receipt_ids=expected)["valid"])
        self.assertFalse(audit_property_receipts(receipts + receipts,
            expected_receipt_ids=expected)["valid"])
        extra = dict(receipts[0], receipt_id="PR-R_GUIDANCE-0001",
                     property_path="keywords_zh.count_guidance.min_count")
        self.assertFalse(audit_property_receipts(receipts + [extra],
            expected_receipt_ids=expected)["valid"])

    def test_guidance_only_does_not_manufacture_a_requirement_pass(self) -> None:
        requirements = [{"id": "R_GUIDANCE", "role": "content_constraints", "properties": {
            "abstract_zh": {"length_guidance": {"min_chars": 300, "max_chars": 1000,
                "length_metric": "cjk_characters", "strength": "general_guidance", "exception_text": ""},
                "third_person_guidance": "general_guidance"},
            "keywords_en": {"count_guidance": {"min_count": 3, "max_count": 8,
                "strength": "general_guidance"}},
        }}]
        self.assertEqual(expected_receipt_ids(requirements), set())
        self.assertEqual(build_property_receipts(requirements, {}, {},
            serialized_docx_sha256="b" * 64), [])

    def test_guidance_does_not_demote_independent_hard_bounds(self) -> None:
        requirements = [{"id": "R_MIXED", "role": "content_constraints", "properties": {
            "keywords_zh": {"count_guidance": {"min_count": 3, "max_count": 8,
                "strength": "general_guidance"}, "min_count": 4, "max_count": 6},
        }}]
        for measured, valid in ((2, False), (5, True), (9, False)):
            with self.subTest(measured=measured):
                receipts = build_property_receipts(requirements, {}, {"content_constraints": {
                    "keywords_zh": {"min_count": measured, "max_count": measured},
                }}, serialized_docx_sha256="c" * 64)
                self.assertEqual(len(receipts), 2)
                self.assertEqual(audit_property_receipts(receipts,
                    expected_receipt_ids=expected_receipt_ids(requirements))["valid"], valid)

    def test_unknown_or_malformed_guidance_is_not_exempted(self) -> None:
        base = {"min_count": 3, "max_count": 8, "strength": "general_guidance"}
        cases = [
            ("content_constraints", "keywords_zh", dict(base, strength="mandatory")),
            ("content_constraints", "keywords_zh", dict(base, min_count=True)),
            ("content_constraints", "keywords_zh", dict(base, max_count=0)),
            ("content_constraints", "keywords_zh", dict(base, min_count=9)),
            ("content_constraints", "keywords_zh", dict(base, hidden_requirement=True)),
            ("content_constraints", "unknown_role", base),
            ("body_text", "keywords_zh", base),
        ]
        for role, key, guidance in cases:
            with self.subTest(role=role, key=key, guidance=guidance):
                requirements = [{"id": "R_INVALID", "role": role,
                                 "properties": {key: {"count_guidance": guidance}}}]
                receipts = build_property_receipts(requirements, {}, {}, serialized_docx_sha256="d" * 64)
                self.assertTrue(receipts)
                self.assertTrue(expected_receipt_ids(requirements))
                self.assertFalse(audit_property_receipts(receipts,
                    expected_receipt_ids=expected_receipt_ids(requirements))["valid"])

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

    def test_cover_field_requirement_is_an_ordered_source_bound_subset(self) -> None:
        actual = [
            {"id": "classification_number", "label": "分类号：",
             "value_from": "thesis_profile.cover_metadata.classification_number",
             "display_policy": "required"},
            {"id": "title_zh", "label": "论文题目：",
             "value_from": "thesis_profile.cover_metadata.title_zh",
             "display_policy": "required"},
            {"id": "title_en", "label": "English title",
             "value_from": "thesis_profile.cover_metadata.title_en",
             "display_policy": "required"},
        ]
        expected = [
            {"id": "title_zh", "label": "论文题目",
             "value_from": "thesis_profile.cover_metadata.title_zh",
             "display_policy": "required"},
            {"id": "title_en", "label": "English title",
             "value_from": "thesis_profile.cover_metadata.title_en",
             "display_policy": "required"},
        ]
        self.assertTrue(satisfies("fields", actual, expected))
        wrong_source = copy.deepcopy(expected)
        wrong_source[0]["value_from"] = "thesis_profile.cover_metadata.subtitle_zh"
        self.assertFalse(satisfies("fields", actual, wrong_source))
        wrong_order = list(reversed(expected))
        self.assertFalse(satisfies("fields", actual, wrong_order))

    def test_unbound_cover_variants_are_unverified_and_bound_instance_still_fails(self) -> None:
        actual_fields = [{"id": "title_zh", "label": "论文题目：",
                          "value_from": "thesis_profile.cover_metadata.title_zh",
                          "display_policy": "required"}]
        requirements = [
            {"id": "R_MAIN", "role": "cover", "clause_ids": ["C1"],
             "properties": {"fields": copy.deepcopy(actual_fields)}},
            {"id": "R_VARIANT", "role": "cover", "clause_ids": ["C2"],
             "properties": {"fields": [{"id": "title_zh", "label": "论文题目（中文）",
                 "value_from": "thesis_profile.cover_metadata.title_zh",
                 "display_policy": "required"}, {"id": "title_en", "label": "English title",
                 "value_from": "thesis_profile.cover_metadata.title_en",
                 "display_policy": "required"}]}},
        ]
        receipts = build_property_receipts(requirements, {}, {"cover": {"fields": actual_fields}},
                                           serialized_docx_sha256="a" * 64)
        by_requirement = {item["requirement_id"]: item for item in receipts}
        self.assertEqual(by_requirement["R_MAIN"]["status"], "verified")
        self.assertEqual(by_requirement["R_VARIANT"]["status"], "unverified")
        self.assertIn("instance-specific binding", by_requirement["R_VARIANT"]["status_reason"])

        scoped = copy.deepcopy(requirements[1])
        scoped["cover_instance_id"] = "academic-master-cover"
        scoped_receipt = build_property_receipts(
            [requirements[0], scoped], {}, {"cover": {"fields": actual_fields}},
            actual_by_requirement={"R_VARIANT": {"fields": actual_fields}},
            serialized_docx_sha256="b" * 64,
        )
        scoped_variant = next(item for item in scoped_receipt if item["requirement_id"] == "R_VARIANT")
        self.assertEqual(scoped_variant["status"], "failed")

        explicitly_empty = build_property_receipts(
            [requirements[0], scoped], {}, {"cover": {"fields": actual_fields}},
            actual_by_requirement={"R_VARIANT": {}},
            serialized_docx_sha256="e" * 64,
        )
        empty_variant = next(item for item in explicitly_empty
                             if item["requirement_id"] == "R_VARIANT")
        self.assertEqual(empty_variant["status"], "unverified")
        self.assertIsNone(empty_variant["actual"])

    def test_font_property_without_latin_content_is_unknown_not_failed(self) -> None:
        requirement = [{"id": "R_FONT", "role": "bibliography_heading",
                        "clause_ids": ["C_FONT"],
                        "properties": {"font": {"latin": "Times New Roman"}}}]
        receipt = build_property_receipts(
            requirement, {"bibliography_heading": {"style_name": "Thesis Bibliography Heading"}},
            {"bibliography_heading": {"font": {"latin": "Cambria"}}},
            role_observability={"bibliography_heading": {"font.latin": {
                "observed": False,
                "reason": "no Latin letters occur in this role's serialized text",
                "verification_method": "serialized_docx_role_text_inventory",
            }}},
            serialized_docx_sha256="c" * 64,
        )[0]
        self.assertEqual(receipt["actual"], "Cambria")
        self.assertEqual(receipt["status"], "unverified")
        self.assertIn("no Latin letters", receipt["status_reason"])
        self.assertEqual(receipt["verification_method"], "serialized_docx_role_text_inventory")

    def test_final_docx_hash_rebind_preserves_role_observability_status(self) -> None:
        requirement = [{"id": "R_FONT", "role": "bibliography_heading",
                        "clause_ids": ["C_FONT"],
                        "properties": {"font": {"latin": "Times New Roman"}}}]
        mappings = {"bibliography_heading": {"style_name": "References Heading"}}
        actual = {"bibliography_heading": {"font": {"latin": "Cambria"}}}
        observation = {"bibliography_heading": {"font.latin": {
            "observed": False,
            "reason": "no Latin letters occur in this role's serialized text",
            "verification_method": "serialized_docx_role_text_inventory",
        }}}
        first = build_property_receipts(
            requirement, mappings, actual, role_observability=observation,
            serialized_docx_sha256="f" * 64,
        )[0]
        rebound = build_property_receipts(
            requirement, mappings, actual, role_observability=observation,
            serialized_docx_sha256="0" * 64,
        )[0]
        self.assertEqual(first["status"], "unverified")
        self.assertEqual(rebound["status"], "unverified")
        self.assertNotEqual(first["serialized_docx_sha256"], rebound["serialized_docx_sha256"])

    def test_observed_latin_run_mismatch_remains_a_proven_failure(self) -> None:
        requirement = [{"id": "R_FONT", "role": "bibliography_heading",
                        "clause_ids": ["C_FONT"],
                        "properties": {"font": {"latin": "Times New Roman"}}}]
        receipt = build_property_receipts(
            requirement, {}, {"bibliography_heading": {"font": {"latin": "Cambria"}}},
            role_observability={"bibliography_heading": {"font.latin": {
                "observed": True,
                "verification_method": "serialized_docx_role_latin_runs",
            }}},
            serialized_docx_sha256="d" * 64,
        )[0]
        self.assertEqual(receipt["status"], "failed")


if __name__ == "__main__":
    unittest.main()
