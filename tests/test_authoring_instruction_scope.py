"""Author work-item authorization must not confuse quality limits with negation."""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from source_obligation_compiler import is_explicit_authoring_content_quote
from native_semantic_review import NativeSemanticReviewError, validate_obligation_coverage_response


class AuthoringInstructionScopeTests(unittest.TestCase):
    def test_positive_content_directives_survive_separate_quality_prohibitions(self):
        sources = (
            "本部分主要撰写国内的研究现状，不能是文献资料的简单摘录，计算在重复率内，需要分类、总结、归纳",
            "本部分主要撰写国外的研究现状，不能是文献资料的简单摘录，计算在重复率内，需要分类、总结、归纳",
            "本章应当撰写研究方法，不得照抄文献。",
            "本章必须撰写研究综述，不能照抄文献。",
            "本节应补充研究现状及研究方法。",
            "本节应补充国内研究现状以及国外文献综述，不得照抄。",
            "本部分是对上述2.1、2.2的小结，重点说明以往研究对本研究的基础贡献、影响等",
            "本节为对上述研究的小结，主要阐述已有研究对本研究的支撑。",
            "不得简单摘录文献；本节必须补充研究结果。",
            "本部分主要介绍选题的背景，根据实际情况自行填写，不要照搬文献。",
            "作者应提供真实研究内容，不得照抄。",
            "不得抄袭，作者应提供真实研究内容。",
            "The author must provide genuine content; do not copy literature.",
            "Do not copy literature. The author must provide genuine content.",
        )
        for source in sources:
            with self.subTest(source=source):
                self.assertTrue(is_explicit_authoring_content_quote(source))

    def test_negation_conditions_quotes_and_layout_never_authorize_authoring(self):
        sources = (
            "本部分不应撰写国内的研究现状，不能是文献资料的简单摘录。",
            "本部分无需撰写研究结果。",
            "不要作者提供真实研究内容。",
            "作者不需要提供真实研究内容。",
            "本部分主要撰写研究现状；不得撰写研究现状。",
            "本部分主要撰写研究现状，但作者不必提供真实研究内容。",
            "如果需要，本部分主要撰写国内的研究现状，不能简单摘录。",
            "仅在开展实验时，本节主要撰写国内的研究现状。",
            "在有国外资料时，本部分主要撰写国外的研究现状。",
            "本部分主要撰写研究现状（必要时）。",
            "本部分主要撰写研究现状（仅当开展研究时），不得照抄。",
            "示例：本部分主要撰写研究现状，不能简单摘录。",
            "不得抄袭；示例：作者应提供真实研究内容。",
            "不得抄袭；作者给出的示例：本部分主要撰写研究现状。",
            "指南引用：‘本部分主要撰写研究现状，不能简单摘录。’",
            "本部分主要撰写图题，不能超出一行。",
            "本节应补充研究现状及页码。",
            "本部分主要介绍研究现状，不能简单摘录。",
            "研究现状不能是文献资料的简单摘录，需要分类、总结、归纳。",
            "The author must not provide genuine content.",
            "If necessary, the author must provide genuine content; do not copy.",
            'The guide quotes: "The author must provide genuine content; do not copy."',
            "本章不是主要撰写研究方法。",
            "本部分主要撰写研究现状，不是要求作者撰写真实研究内容。",
            "Do not plagiarize. For example, the author should provide genuine content.",
            "作者根据实际情况填写页码，不得乱填。",
            "填写姓名、学号、日期，根据本人论文实际情况撰写",
            "页面布局：The author must provide genuine content",
            "仅供排版示例；The author must provide genuine content",
            "本部分是对表格格式的小结，重点说明字体和字号。",
            "本部分不是对上述研究的小结，重点说明以往研究对本研究的贡献。",
        )
        for source in sources:
            with self.subTest(source=source):
                self.assertFalse(is_explicit_authoring_content_quote(source))

    @staticmethod
    def pending_case(source):
        check = {
            "check_id": "arbitrary-source-clause", "document_text": source,
            "review_context": {
                "classification": "requires_source_content", "requires_requirement": False,
                "linked_requirements": [], "machine_obligation_ids": [],
            },
        }
        response = {"results": [{
            "check_id": check["check_id"], "verdict": "source_content_pending",
            "rationale": "The source explicitly requires section content; human input remains pending.",
            "evidence_quotes": [source], "machine_obligation_ids": [],
            "identified_obligations": [
                {"source_quote": source, "disposition": "authoring_content_pending",
                 "obligation_summary": "Write research-status content.", "requirement_refs": []},
                {"source_quote": source, "disposition": "authoring_content_pending",
                 "obligation_summary": "Synthesize the original research-status discussion.", "requirement_refs": []},
            ],
        }]}
        return check, response

    def test_full_source_quote_is_accepted_only_as_unchanged_pending_work(self):
        source = "本部分主要撰写国内的研究现状，不能是文献资料的简单摘录，计算在重复率内，需要分类、总结、归纳"
        check, response = self.pending_case(source)
        original = copy.deepcopy((check, response))
        validated = validate_obligation_coverage_response(response, [check])
        self.assertEqual(validated[0]["verdict"], "source_content_pending")
        self.assertEqual(len(validated[0]["identified_obligations"]), 2)
        self.assertEqual((check, response), original)
        self.assertNotIn("requirements", response)
        for obligation in validated[0]["identified_obligations"]:
            self.assertEqual(obligation["disposition"], "authoring_content_pending")
            self.assertEqual(obligation["source_quote"], source)
            self.assertEqual(obligation["requirement_refs"], [])

    def test_pending_authoring_still_rejects_false_completion_and_forged_source(self):
        source = "本节应当撰写研究方法，不得照抄文献。"
        check, response = self.pending_case(source)
        changed = copy.deepcopy(response)
        changed["results"][0]["verdict"] = "consistent"
        with self.assertRaises(NativeSemanticReviewError):
            validate_obligation_coverage_response(changed, [check])
        for quote in ("不得照抄文献", "本节应当撰写研究结果"):
            changed = copy.deepcopy(response)
            changed["results"][0]["identified_obligations"][0]["source_quote"] = quote
            with self.subTest(quote=quote), self.assertRaises(NativeSemanticReviewError):
                validate_obligation_coverage_response(changed, [check])

    def test_substring_cannot_discard_source_scope_or_quotation(self):
        quote = "本部分主要撰写国内的研究现状"
        sources = (
            "如果需要，" + quote,
            quote + "（仅当开展研究时）",
            "示例：" + quote,
            "指南引用：‘" + quote + "’",
            "仅供排版示例；" + quote,
            quote + "，但本部分不应撰写研究现状。",
            quote + "。示例再次引用：‘" + quote + "’",
        )
        self.assertTrue(is_explicit_authoring_content_quote(quote))
        for source in sources:
            check, response = self.pending_case(source)
            for obligation in response["results"][0]["identified_obligations"]:
                obligation["source_quote"] = quote
            with self.subTest(source=source):
                self.assertFalse(is_explicit_authoring_content_quote(quote, source_text=source))
                with self.assertRaises(NativeSemanticReviewError):
                    validate_obligation_coverage_response(response, [check])
        source = quote + "，不能是文献资料的简单摘录。"
        self.assertTrue(is_explicit_authoring_content_quote(quote, source_text=source))


if __name__ == "__main__":
    unittest.main()
