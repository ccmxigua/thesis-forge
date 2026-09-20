from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from format_spec_validation import load_and_validate  # noqa: E402
from host_review_contract import validate_response  # noqa: E402
from apply_format_spec import compile_cover_contract  # noqa: E402
from evidence_context_guards import sample_content_guard  # noqa: E402
import requirements_engine as engine  # noqa: E402


def _request(clause_text: str, evidence_id: str = "E1") -> dict:
    clause = {"id": "C1", "text": clause_text, "evidence_ids": [evidence_id]}
    evidence = {"evidence": [{"id": evidence_id, "text": clause_text, "kind": "paragraph"}]}
    return engine.build_llm_request([], [clause], evidence, {}, "full")


class RootCauseGuardTests(unittest.TestCase):
    def test_merge_guards_are_explicitly_versioned_and_authorized(self) -> None:
        clauses = [
            {
                "id": "C0", "text": "参考文献",
                "evidence_ids": ["E0"], "source_kind": "paragraph",
                "location": {"part": "document", "child_index": 10, "order": 10},
            },
            {
                "id": "C1", "text": "[1] Doe. Sample thesis.",
                "evidence_ids": ["E1"], "source_kind": "paragraph",
                "location": {"part": "document", "child_index": 11, "order": 11},
            },
        ]
        response = {
            "contract_version": "2.1",
            "requirements": [{
                "role": "body_text", "properties": {"font": {"size_pt": 12}},
                "clause_ids": ["C1"], "evidence_ids": ["E1"],
                "confidence": 0.9, "reason": "The sample entry was present.",
            }],
            "clause_reviews": [
                {"clause_id": "C0", "classification": "informational",
                 "requirement_indexes": [], "reason": "Heading only."},
                {"clause_id": "C1", "classification": "executable",
                 "requirement_indexes": [0], "reason": "The sample entry was present."},
            ],
            "unsupported_items": [], "reported_conflicts": [],
        }
        spec, conflicts, audit = engine.merge_llm_primary(
            Path("synthetic-source"),
            {"schema_version": "1.0", "roles": {}, "page": {},
             "requirements": [], "content_instances": []},
            clauses, response, {"E0", "E1"},
        )
        self.assertFalse(any(item.get("type") == "llm_internal_conflict" for item in conflicts))
        policy = next(item for item in audit if item.get("type") == "merge_transformation_policy")
        self.assertEqual(policy["semantic_transformation_policy_version"], "merge-semantic-guards-v1")
        guard = next(item for item in audit if item.get("type") == "normative_scope_guard")
        self.assertEqual(guard["policy_version"], "merge-semantic-guards-v1")
        self.assertEqual(guard["authorization"], "registered_evidence_context_guard_v1")
        self.assertEqual(spec["requirements"], [])

    def test_sample_content_context_uses_document_structure_not_broad_order_window(self) -> None:
        clauses = [
            {
                "id": "C_APPENDIX_HEADING",
                "text": "附录A 示例数据",
                "source_kind": "paragraph",
                "location": {"part": "document", "child_index": 10, "order": 10},
            },
            {
                "id": "C_APPENDIX_CELL",
                "text": "2024年度数据",
                "source_kind": "table_cell",
                "location": {
                    "part": "document", "table_child_index": 11, "order": 11,
                },
            },
            {
                "id": "C_SPINE_CELL",
                "text": "论文题目",
                "source_kind": "table_cell",
                "location": {
                    "part": "document", "table_child_index": 100, "order": 30,
                },
            },
        ]

        self.assertEqual(
            sample_content_guard(clauses[1], clauses)["kind"],
            "appendix_sample_content",
        )
        self.assertIsNone(sample_content_guard(clauses[2], clauses))

    def test_partial_chinese_abstract_cannot_claim_full_coverage(self) -> None:
        request = _request("中文摘要一般300～1000字，使用第三人称，包含目的、方法、成果、结论和创新性，不加评论。")
        response = {
            "contract_version": "2.1",
            "requirements": [{
                "role": "content_constraints",
                "properties": {"abstract_zh": {"required": True, "max_chars": 1000}},
                "clause_ids": ["C1"], "evidence_ids": ["E1"],
                "confidence": 0.9, "reason": "摘要存在且有上限",
            }],
            "clause_reviews": [{
                "clause_id": "C1", "classification": "executable",
                "requirement_indexes": [0], "reason": "已覆盖",
            }],
            "unsupported_items": [], "reported_conflicts": [],
        }
        errors = validate_response(response, request)
        self.assertTrue(any("partial_clause_coverage" in error for error in errors), errors)
        self.assertTrue(any("abstract_zh.min_chars" in error for error in errors), errors)

    def test_abstract_novelty_phrase_is_not_a_section_list(self) -> None:
        clauses = [
            {
                "id": "C00066",
                "text": (
                    "中文摘要是论文内容的简要陈述，一般以第三人称语气撰写，"
                    "300～1000字，体现出论文的新理论、新方法、新技术等"
                ),
                "evidence_ids": ["E59"],
            },
            {
                "id": "C00067",
                "text": "其内容包括：目的意义、研究方法、研究成果和结论。",
                "evidence_ids": ["E59"],
            },
        ]
        evidence = {
            "evidence": [{"id": "E59", "text": "同一来源段落", "kind": "paragraph"}],
        }
        request = engine.build_llm_request([], clauses, evidence, {}, "full")
        response = {
            "contract_version": "2.1",
            "requirements": [
                {
                    "role": "content_constraints",
                    "properties": {
                        "abstract_zh": {
                            "required": True,
                            "min_chars": 300,
                            "max_chars": 1000,
                            "require_third_person": True,
                        },
                    },
                    "clause_ids": ["C00066"],
                    "evidence_ids": ["E59"],
                    "confidence": 0.9,
                    "reason": "摘要基本限制",
                },
                {
                    "role": "content_constraints",
                    "properties": {
                        "abstract_zh": {
                            "required": True,
                            "required_sections": [
                                "purpose", "methods", "results", "conclusions",
                            ],
                        },
                    },
                    "clause_ids": ["C00067"],
                    "evidence_ids": ["E59"],
                    "confidence": 0.9,
                    "reason": "摘要章节要求",
                },
            ],
            "clause_reviews": [
                {
                    "clause_id": "C00066",
                    "classification": "executable",
                    "requirement_indexes": [0],
                    "reason": "基本限制已表示",
                },
                {
                    "clause_id": "C00067",
                    "classification": "executable",
                    "requirement_indexes": [1],
                    "reason": "章节清单已表示",
                },
            ],
            "unsupported_items": [],
            "reported_conflicts": [],
        }
        errors = validate_response(response, request)
        self.assertFalse(
            any("C00066" in error and "required_sections" in error for error in errors),
            errors,
        )

    def test_ambiguous_english_translation_cannot_be_executable(self) -> None:
        request = _request("The following English is not correct. The Chinese abstract should be 300 to 1,000 words.")
        response = {
            "contract_version": "2.1",
            "requirements": [{
                "role": "content_constraints",
                "properties": {"abstract_en": {"required": True, "min_words": 300, "max_words": 1000}},
                "clause_ids": ["C1"], "evidence_ids": ["E1"],
                "confidence": 0.9, "reason": "英文摘要限制",
            }],
            "clause_reviews": [{
                "clause_id": "C1", "classification": "executable",
                "requirement_indexes": [0], "reason": "已覆盖",
            }],
            "unsupported_items": [], "reported_conflicts": [],
        }
        errors = validate_response(response, request)
        self.assertTrue(any("abstract_target_or_translation_ambiguous" in error for error in errors), errors)

    def test_english_keyword_chinese_character_metric_must_be_explicit(self) -> None:
        request = _request("English keywords: each item is up to 7 Chinese characters.")
        response = {
            "contract_version": "2.1",
            "requirements": [{
                "role": "content_constraints",
                "properties": {"keywords_en": {"max_item_chars": 7, "item_length_metric": "words"}},
                "clause_ids": ["C1"], "evidence_ids": ["E1"],
                "confidence": 0.9, "reason": "保留原文计量对象",
            }],
            "clause_reviews": [{
                "clause_id": "C1", "classification": "executable",
                "requirement_indexes": [0], "reason": "已覆盖",
            }],
            "unsupported_items": [], "reported_conflicts": [],
        }
        errors = validate_response(response, request)
        self.assertTrue(any("item_length_metric:cjk_characters" in error for error in errors), errors)

    def test_approval_fields_cannot_be_mapped_to_classification_or_completion(self) -> None:
        request = _request("非公开学位论文审批表编号和批准日期")
        response = {
            "contract_version": "2.1",
            "requirements": [{
                "role": "cover",
                "properties": {
                    "institution": "Example",
                    "fields": [
                        {"id": "classification_number", "label": "审批表编号",
                         "value_from": "thesis_profile.cover_metadata.classification_number",
                         "display_policy": "if_present", "order": 1},
                        {"id": "completion_date", "label": "批准日期",
                         "value_from": "thesis_profile.cover_metadata.completion_date",
                         "display_policy": "if_present", "order": 2},
                    ],
                },
                "clause_ids": ["C1"], "evidence_ids": ["E1"],
                "confidence": 0.9, "reason": "表格字段",
            }],
            "clause_reviews": [{
                "clause_id": "C1", "classification": "executable",
                "requirement_indexes": [0], "reason": "已覆盖",
            }],
            "unsupported_items": [], "reported_conflicts": [],
        }
        errors = validate_response(response, request)
        self.assertTrue(any("must bind to 'approval_number'" in error for error in errors), errors)
        self.assertTrue(any("must bind to 'approval_date'" in error for error in errors), errors)

    def test_administrative_labels_cannot_stay_in_ordinary_cover(self) -> None:
        request = _request("非公开学位论文保密期限、审批表编号和批准日期")
        response = {
            "contract_version": "2.1",
            "requirements": [{
                "role": "cover",
                "properties": {
                    "institution": "Example",
                    "fields": [
                        {"id": "approval_number", "label": "审批表编号",
                         "value_from": "thesis_profile.cover_metadata.approval_number",
                         "display_policy": "if_present", "order": 1},
                        {"id": "embargo_until", "label": "保密期限",
                         "value_from": "thesis_profile.cover_metadata.embargo_until",
                         "display_policy": "if_present", "order": 2},
                    ],
                    "non_public_administration": {
                        "applicability": {"status": "conditional", "conditions": [
                            {"fact": "thesis_profile.security_level", "operator": "in",
                             "value": ["restricted", "classified"]},
                        ]},
                        "fields": [{"id": "embargo_until", "label": "保密期限",
                                     "value_from": "thesis_profile.cover_metadata.embargo_until",
                                     "display_policy": "if_present", "order": 1}],
                        "public_policy": "blank", "source_region": "official_admin_table",
                    },
                },
                "clause_ids": ["C1"], "evidence_ids": ["E1"],
                "confidence": 0.9, "reason": "表格字段",
            }],
            "clause_reviews": [{
                "clause_id": "C1", "classification": "executable",
                "requirement_indexes": [0], "reason": "已覆盖",
            }],
            "unsupported_items": [], "reported_conflicts": [],
        }
        errors = validate_response(response, request)
        self.assertTrue(any("ordinary cover.fields" in error for error in errors), errors)
        self.assertTrue(any("embargo range" in error for error in errors), errors)

    def test_duplicate_confidentiality_label_can_bind_ordered_date_range(self) -> None:
        request = _request("非公开学位论文保密期限起止日期")
        response = {
            "contract_version": "2.1",
            "requirements": [{
                "role": "cover",
                "properties": {
                    "institution": "Example",
                    "fields": [{"id": "title_zh", "label": "论文题目",
                                 "value_from": "thesis_profile.cover_metadata.title_zh",
                                 "display_policy": "required", "order": 1}],
                    "non_public_administration": {
                        "applicability": {"status": "conditional", "conditions": [
                            {"fact": "thesis_profile.security_level", "operator": "in",
                             "value": ["restricted", "classified"]},
                        ]},
                        "fields": [
                            {"id": "embargo_start", "label": "保密期限",
                             "value_from": "thesis_profile.cover_metadata.embargo_start",
                             "display_policy": "required", "order": 1},
                            {"id": "embargo_until", "label": "保密期限",
                             "value_from": "thesis_profile.cover_metadata.embargo_until",
                             "display_policy": "required", "order": 2},
                        ],
                        "public_policy": "blank", "source_region": "official_admin_table",
                    },
                },
                "clause_ids": ["C1"], "evidence_ids": ["E1"],
                "confidence": 0.9, "reason": "明确的起止日期区间",
            }],
            "clause_reviews": [{
                "clause_id": "C1", "classification": "executable",
                "requirement_indexes": [0], "reason": "已覆盖",
            }],
            "unsupported_items": [], "reported_conflicts": [],
        }
        self.assertEqual(validate_response(response, request), [])

    def test_unknown_input_key_is_rejected_by_format_loader(self) -> None:
        spec = {
            "schema_version": "1.0",
            "requirements": [{
                "id": "R1", "role": "body_text", "properties": {"font": {"size_pt": 12}},
                "clause_ids": ["C1"], "input_prerequisites": [{
                    "kind": "runtime", "key": "runtime.model_guess", "required": True,
                    "reason": "not a registered runtime producer",
                }],
            }],
        }
        errors = load_and_validate(spec, ROOT / "schema" / "format-spec.schema.json")
        self.assertTrue(any("unregistered input path" in error for error in errors), errors)

    def test_unknown_checker_id_is_rejected_but_explanation_text_remains_free_form(self) -> None:
        spec = {
            "schema_version": "1.0",
            "requirements": [{
                "id": "R1", "role": "body_text", "properties": {"font": {"size_pt": 12}},
                "clause_ids": ["C1"],
                "verification": {
                    "mode": "static_docx", "checks": ["学校要求的自然语言检查说明"],
                    "checker_ids": ["docx.does_not_exist"],
                },
            }],
        }
        errors = load_and_validate(spec, ROOT / "schema" / "format-spec.schema.json")
        self.assertTrue(any("unregistered checker id" in error for error in errors), errors)

    def test_runtime_anchor_inventory_requires_one_unique_source_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "target.docx"
            path.write_bytes(b"fixture")
            none = engine.build_runtime_anchor_inventory(path, {"evidence": []})
            one = engine.build_runtime_anchor_inventory(path, {"evidence": [{"id": "E1", "text": "摘要"}]})
            two = engine.build_runtime_anchor_inventory(path, {"evidence": [
                {"id": "E1", "text": "摘要"}, {"id": "E2", "text": "摘要"},
            ]})
        self.assertEqual(none["status"], "blocked")
        self.assertEqual(one["status"], "verified")
        self.assertEqual(one["selected"]["name"], "abstract_title_zh")
        self.assertEqual(two["status"], "blocked")

    def test_public_non_public_admin_region_stays_blank_without_placeholder(self) -> None:
        cover = {
            "institution": "Example",
            "fields": [{"id": "title_zh", "label": "论文题目",
                         "value_from": "thesis_profile.cover_metadata.title_zh",
                         "display_policy": "required", "order": 1}],
            "non_public_administration": {
                "applicability": {"status": "conditional", "conditions": [
                    {"fact": "thesis_profile.security_level", "operator": "in",
                     "value": ["restricted", "classified"]},
                ]},
                "fields": [{"id": "approval_number", "label": "审批表编号",
                             "value_from": "thesis_profile.cover_metadata.approval_number",
                             "display_policy": "blank_when_public", "order": 1}],
                "public_policy": "blank", "source_region": "official_admin_table",
            },
        }
        profile = {"security_level": "public", "cover_metadata": {
            "trust": {"source": "user_confirmed", "confirmed": True},
            "title_zh": "测试", "author_name": "作者", "title_en": "Test",
            "student_id": "1", "supervisor_name": "导师", "completion_date": "2026-01",
        }}
        contract = compile_cover_contract(cover, profile)
        administration = contract["non_public_administration"]
        self.assertEqual(administration["status"], "blank_public")
        self.assertEqual(administration["fields"][0]["value_kind"], "omitted")


if __name__ == "__main__":
    unittest.main()
