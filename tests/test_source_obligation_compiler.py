from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from source_obligation_compiler import (  # noqa: E402
    compile_abstract_source_constraints,
    compile_continuation_caption_requirement,
    compile_explicit_keyword_count_range,
    compile_known_source_obligations,
    compile_known_source_obligation_ids,
    compile_soft_keyword_count_guidance,
    compile_unresolved_manual_review_codes,
    materialize_complete_abstract_source_constraints,
    materialize_known_source_verification,
    materialize_soft_keyword_count_guidance,
)


class SourceObligationCompilerTests(unittest.TestCase):
    def test_bsu_continuation_clause_compiles_all_explicit_obligations(self) -> None:
        source = "表序后跟表题(可省略)和“(续)”，居中置于表上方，续表均应重复表头"
        self.assertEqual(compile_continuation_caption_requirement(source)["state"], "optional")
        self.assertEqual(compile_known_source_obligation_ids(source), [
            "table.continuation.caption_optional",
            "table.continuation.caption_suffix",
            "table.continuation.repeat_header_row",
            "table_caption.alignment_center",
            "table_caption.position_above",
        ])

    def test_negative_mandatory_quoted_conditional_and_example_text_fail_closed(self) -> None:
        cases = [
            ("续表题不可省略", "required"),
            ("续表题不可以省略", "required"),
            ("表题不允许省略", "required"),
            ("续表题不得省略", "required"),
            ("续表题并非可省略", "unknown"),
            ("表题未说明可省略", "unknown"),
            ("续表题“可省略”仅为错误示例", "unknown"),
            ("如果续表题可省略，则需人工确认", "unknown"),
            ("仅当续表时，续表题可省略", "unknown"),
            ("如下例，续表题可省略", "unknown"),
            ("例：续表题可省略", "unknown"),
            ("续表题可省略，除非另有规定", "unknown"),
            ("续表题可省略，但审批页另有规定", "unknown"),
            ("续表题可省略或必须保留", "unknown"),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(compile_continuation_caption_requirement(text)["state"], expected)

    def test_unknown_and_unrelated_source_do_not_generate_keys(self) -> None:
        self.assertEqual(compile_known_source_obligation_ids("表格宽度适当"), [])
        self.assertEqual(compile_known_source_obligation_ids(None), [])

    def test_known_obligation_inventory_ignores_conditional_and_example_text(self) -> None:
        cases = [
            "如果续表（续）仅为错误示例，且续表均应重复表头",
            "例如，续表均应重复表头，续表题可省略",
            "仅当续表时，表题应置于表上方并居中",
            "续表均应重复表头，但本句是反例",
        ]
        for source in cases:
            with self.subTest(source=source):
                self.assertEqual(compile_known_source_obligation_ids(source), [])

    def test_compiled_facts_bind_to_role_specific_properties(self) -> None:
        source = "表序后跟表题(可省略)和“(续)”，居中置于表上方，续表均应重复表头"
        facts = compile_known_source_obligations(source)
        self.assertEqual(
            {item["id"]: (
                item["roles"], item["property_path"], item["expected_value"],
                item["required_checker_ids"],
            ) for item in facts},
            {
                "table.continuation.caption_optional": (
                    ["table"], "properties.continuation.caption_required_on_continuation", False,
                    ["docx.property_receipts", "docx.word_render"],
                ),
                "table.continuation.caption_suffix": (
                    ["table"], "properties.continuation.caption_suffix", "(续)",
                    ["docx.property_receipts", "docx.word_render"],
                ),
                "table.continuation.repeat_header_row": (
                    ["table"], "properties.continuation.repeat_header_row", True,
                    ["docx.property_receipts", "docx.word_render"],
                ),
                "table_caption.alignment_center": (
                    ["table_caption", "figure_table_title"],
                    "properties.paragraph.alignment", "center",
                    ["docx.property_receipts"],
                ),
                "table_caption.position_above": (
                    ["table_caption", "figure_table_title"], "properties.position", "above",
                    ["docx.property_receipts"],
                ),
            },
        )

    def test_checker_bindings_are_materialized_only_for_exact_source_payloads(self) -> None:
        source = "表序后跟表题(可省略)和“(续)”，居中置于表上方，续表均应重复表头"
        clauses = [{"id": "C1", "text": source, "evidence_ids": ["E1"]}]
        response = {
            "requirements": [
                {
                    "role": "table",
                    "properties": {"continuation": {
                        "caption_suffix": "(续)",
                        "repeat_header_row": True,
                        "caption_required_on_continuation": False,
                    }},
                    "clause_ids": ["C1"], "evidence_ids": ["E1"],
                },
                {
                    "role": "table_caption",
                    "properties": {"position": "above", "paragraph": {"alignment": "center"}},
                    "clause_ids": ["C1"], "evidence_ids": ["E1"],
                },
            ],
        }
        projected, audit = materialize_known_source_verification(response, clauses)
        self.assertNotIn("verification", response["requirements"][0])
        self.assertEqual(
            projected["requirements"][0]["verification"],
            {
                "mode": "word_render",
                "checks": [
                    "核对生成 DOCX 的属性回执是否覆盖这项来源义务。",
                    "通过 Microsoft Word 渲染结果核验这项来源义务。",
                ],
                "checker_ids": ["docx.property_receipts", "docx.word_render"],
            },
        )
        self.assertEqual(
            projected["requirements"][1]["verification"],
            {
                "mode": "static_docx",
                "checks": ["核对生成 DOCX 的属性回执是否覆盖这项来源义务。"],
                "checker_ids": ["docx.property_receipts"],
            },
        )
        self.assertEqual(len(audit), 2)
        self.assertTrue(all(
            item["authorization"] == "exact_compiled_source_obligation_checker_binding"
            for item in audit
        ))
        _again, second_audit = materialize_known_source_verification(projected, clauses)
        self.assertEqual(second_audit, [])

    def test_wrong_or_unbound_payload_does_not_receive_source_checkers(self) -> None:
        clauses = [{
            "id": "C1",
            "text": "表题间空1个字距，居中置于表的上方",
            "evidence_ids": ["E1"],
        }]
        response = {"requirements": [{
            "role": "table_caption",
            "properties": {"position": "below", "paragraph": {"alignment": "left"}},
            "clause_ids": ["C1"], "evidence_ids": ["E1"],
        }, {
            "role": "table_caption",
            "properties": {"position": "above", "paragraph": {"alignment": "center"}},
            "clause_ids": ["C404"], "evidence_ids": ["E404"],
        }]}
        projected, audit = materialize_known_source_verification(response, clauses)
        self.assertNotIn("verification", projected["requirements"][0])
        self.assertNotIn("verification", projected["requirements"][1])
        self.assertEqual(audit, [])

    def test_soft_keyword_range_stays_guidance_and_separate_mandate_remains_hard(self) -> None:
        clauses = [
            {"id": "C69", "text": "关键词一般3～8个", "evidence_ids": ["E69"]},
            {"id": "C72", "text": "关键词最少3组，最多8组", "evidence_ids": ["E72"]},
            {"id": "C77", "text": "Keywords generally 3~8", "evidence_ids": ["E77"]},
            {"id": "C80", "text": "up to 7 Chinese characters", "evidence_ids": ["E80"]},
        ]
        response = {"requirements": [
            {
                "role": "content_constraints",
                "clause_ids": ["C69", "C72"], "evidence_ids": ["E69", "E72"],
                "properties": {"keywords_zh": {"min_count": 3, "max_count": 8}},
                "verification": {"checks": ["Verify the Chinese keyword count is 3 to 8."]},
            },
            {
                "role": "content_constraints",
                "clause_ids": ["C77", "C80"], "evidence_ids": ["E77", "E80"],
                "properties": {"keywords_en": {"min_count": 3, "max_count": 8}},
                "verification": {"checks": ["Verify the keyword count is 3 to 8."]},
            },
        ]}
        self.assertEqual(compile_soft_keyword_count_guidance(clauses[0]["text"])["strength"], "general_guidance")
        self.assertEqual(compile_soft_keyword_count_guidance(clauses[2]["text"])["language_key"], "keywords_en")
        self.assertEqual(compile_explicit_keyword_count_range(clauses[1]["text"]), {
            "min_count": 3, "max_count": 8,
        })

        projected, audit = materialize_soft_keyword_count_guidance(response, clauses)
        self.assertEqual(projected["requirements"][0]["properties"]["keywords_zh"]["min_count"], 3)
        self.assertEqual(projected["requirements"][0]["properties"]["keywords_zh"]["max_count"], 8)
        self.assertEqual(
            projected["requirements"][0]["properties"]["keywords_zh"]["count_guidance"],
            {"min_count": 3, "max_count": 8, "strength": "general_guidance"},
        )
        english_rule = projected["requirements"][1]["properties"]["keywords_en"]
        self.assertNotIn("min_count", english_rule)
        self.assertNotIn("max_count", english_rule)
        self.assertEqual(english_rule["count_guidance"]["strength"], "general_guidance")
        self.assertEqual(projected["requirements"][1]["verification"]["checks"], [])
        self.assertEqual(len(audit), 2)
        again, second_audit = materialize_soft_keyword_count_guidance(projected, clauses)
        self.assertEqual(again, projected)
        self.assertEqual(second_audit, [])

    def test_unrelated_numeric_range_does_not_authorize_keyword_hard_bounds(self) -> None:
        clauses = [
            {"id": "C69", "text": "关键词一般3～8个", "evidence_ids": ["E69"]},
            {"id": "C72", "text": "摘要最少3组，最多8组", "evidence_ids": ["E72"]},
        ]
        response = {"requirements": [{
            "role": "content_constraints",
            "clause_ids": ["C69", "C72"],
            "evidence_ids": ["E69", "E72"],
            "properties": {"keywords_zh": {"min_count": 3, "max_count": 8}},
            "verification": {"checks": ["Verify the Chinese keyword count is 3 to 8."]},
        }]}

        self.assertIsNone(compile_explicit_keyword_count_range(clauses[1]["text"]))
        projected, audit = materialize_soft_keyword_count_guidance(response, clauses)

        rule = projected["requirements"][0]["properties"]["keywords_zh"]
        self.assertNotIn("min_count", rule)
        self.assertNotIn("max_count", rule)
        self.assertFalse(audit[0]["independently_mandatory_range_present"])
        self.assertEqual(projected["requirements"][0]["verification"]["checks"], [])

    def test_complete_abstract_bundles_materialize_but_ambiguous_english_stays_manual(self) -> None:
        c66 = (
            "中文摘要是论文内容的简要陈述，一般以第三人称语气撰写，"
            "300～1000字（如遇特殊需要字数可以略多），不加评论和解释，"
            "是一篇具有独立性和完整性的短文，能准确反映论文的中心思想，"
            "规范的学术用语，逻辑性强、结构严谨，体现出论文的新理论、新方法、新技术等"
        )
        c67 = (
            "其内容包括：目的意义、研究方法、研究成果和结论，应与论文等同的主要信息，"
            "要突出本论文的创造性成果，不可出现图、表、化学方程式、非公知公用的符号和术语"
        )
        clauses = [
            {"id": "C66", "text": c66, "evidence_ids": ["E1"],
             "location": {"part": "document", "child_index": 5, "order": 5}},
            {"id": "C67", "text": c67, "evidence_ids": ["E1"],
             "location": {"part": "document", "child_index": 5, "order": 5}},
            {"id": "C76", "text": "The Chinese abstract uses 300 to 1,000 words.", "evidence_ids": ["E2"]},
        ]
        self.assertIsNotNone(compile_abstract_source_constraints(c66))
        self.assertIsNone(compile_abstract_source_constraints(c67))
        self.assertEqual(
            compile_unresolved_manual_review_codes(clauses[2]["text"]),
            ["abstract_target_metric_ambiguity"],
        )
        response = {
            "contract_version": "3.0",
            "requirements": [],
            "clause_reviews": [
                {"clause_id": cid, "classification": "unresolved", "reason": "not represented",
                 "obligations": [{"id": "source", "status": "unresolved", "reason": "not represented"}]}
                for cid in ("C66", "C67")
            ],
        }
        projected, audit = materialize_complete_abstract_source_constraints(response, clauses)
        self.assertEqual(len(projected["requirements"]), 2)
        reviews = {item["clause_id"]: item for item in projected["clause_reviews"]}
        self.assertEqual(reviews["C66"]["classification"], "executable")
        self.assertEqual(reviews["C67"]["classification"], "executable")
        zh_properties = projected["requirements"][0]["properties"]["abstract_zh"]
        self.assertEqual(zh_properties["third_person_guidance"], "general_guidance")
        self.assertNotIn("require_third_person", zh_properties)
        self.assertEqual(zh_properties["length_guidance"]["min_chars"], 300)
        self.assertNotIn("min_chars", zh_properties)
        self.assertTrue(zh_properties["prohibit_commentary"])
        self.assertIn("main_information_equivalent_to_thesis", projected["requirements"][1]["properties"]["abstract_zh"]["quality_guidance"])
        self.assertTrue(all(item["status"] == "covered" for review in reviews.values() for item in review["obligations"]))
        self.assertEqual(len(audit), 2)
        again, second_audit = materialize_complete_abstract_source_constraints(projected, clauses)
        self.assertEqual(again, projected)
        self.assertEqual(second_audit, [])

    def test_abstract_context_is_not_joined_without_stable_source_locations(self) -> None:
        c66 = (
            "中文摘要是论文内容的简要陈述，一般以第三人称语气撰写，"
            "300～1000字（如遇特殊需要字数可以略多），不加评论和解释，"
            "是一篇具有独立性和完整性的短文，能准确反映论文的中心思想，"
            "规范的学术用语，逻辑性强、结构严谨，体现出论文的新理论、新方法、新技术等"
        )
        c67 = (
            "其内容包括：目的意义、研究方法、研究成果和结论，应与论文等同的主要信息，"
            "要突出本论文的创造性成果，不可出现图、表、化学方程式、非公知公用的符号和术语"
        )
        clauses = [
            {"id": "C66", "text": c66, "evidence_ids": ["E1"]},
            {"id": "C67", "text": c67, "evidence_ids": ["E1"]},
        ]
        response = {
            "contract_version": "3.0",
            "requirements": [],
            "clause_reviews": [
                {"clause_id": cid, "classification": "unresolved", "reason": "not represented",
                 "obligations": [{"id": "source", "status": "unresolved", "reason": "not represented"}]}
                for cid in ("C66", "C67")
            ],
        }

        projected, audit = materialize_complete_abstract_source_constraints(response, clauses)

        self.assertEqual(len(projected["requirements"]), 1)
        self.assertEqual(projected["requirements"][0]["clause_ids"], ["C66"])
        review_by_id = {item["clause_id"]: item for item in projected["clause_reviews"]}
        self.assertEqual(review_by_id["C67"]["classification"], "unresolved")
        self.assertEqual(len(audit), 1)

    def test_abstract_compiler_does_not_upgrade_partial_or_ambiguous_source(self) -> None:
        self.assertIsNone(compile_abstract_source_constraints("中文摘要一般300～1000字，使用第三人称。"))
        self.assertIsNone(compile_abstract_source_constraints(
            "其内容包括：目的意义、研究方法、研究成果和结论，应与论文等同的主要信息。",
            source_context="与摘要无关的上一段落。",
        ))
        self.assertEqual(
            compile_unresolved_manual_review_codes(
                "The Chinese abstract is a brief statement, 300 to 1,000 words."
            ),
            ["abstract_target_metric_ambiguity"],
        )


if __name__ == "__main__":
    unittest.main()
