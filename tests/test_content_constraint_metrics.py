from __future__ import annotations

import sys
import unittest
from pathlib import Path

from docx import Document

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from apply_format_spec import audit_content_constraints  # noqa: E402


class ContentConstraintMetricTests(unittest.TestCase):
    def _doc(self, abstract: str, keywords: str) -> Document:
        document = Document()
        abstract_style = document.styles.add_style("AbstractBodyFixture", 1)
        keyword_style = document.styles.add_style("KeywordsFixture", 1)
        document.add_paragraph(abstract, abstract_style)
        document.add_paragraph(keywords, keyword_style)
        return document

    def test_chinese_bounds_use_declared_codepoint_metric(self) -> None:
        document = self._doc("甲" * 299, "关键词：甲")
        findings = audit_content_constraints(
            document,
            {
                "abstract_zh": {"required": True, "min_chars": 300, "max_chars": 1000,
                                "length_metric": "unicode_codepoints"},
                "keywords_zh": {"required": True},
            },
            {"abstract_body_zh": {"style_name": "AbstractBodyFixture"}, "keywords_zh": {"style_name": "KeywordsFixture"}},
        )
        self.assertTrue(any(item["property"] == "abstract_zh.min_chars" for item in findings), findings)

    def test_keyword_item_limit_reports_item_without_truncation(self) -> None:
        document = self._doc("摘要", "关键词：甲乙丙丁戊己庚辛")
        findings = audit_content_constraints(
            document,
            {
                "abstract_zh": {"required": True},
                "keywords_zh": {"required": True, "max_item_chars": 7,
                                 "item_length_metric": "cjk_characters"},
            },
            {"abstract_body_zh": {"style_name": "AbstractBodyFixture"}, "keywords_zh": {"style_name": "KeywordsFixture"}},
        )
        violation = next(item for item in findings if "max_item_chars" in item["property"])
        self.assertEqual(violation["template_value"], 8)
        self.assertEqual(violation["item"], "甲乙丙丁戊己庚辛")


if __name__ == "__main__":
    unittest.main()
