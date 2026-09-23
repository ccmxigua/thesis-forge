from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from source_obligation_compiler import (  # noqa: E402
    compile_continuation_caption_requirement,
    compile_known_source_obligations,
    compile_known_source_obligation_ids,
    materialize_known_source_verification,
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


if __name__ == "__main__":
    unittest.main()
