from __future__ import annotations

import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from source_obligation_compiler import (  # noqa: E402
    SOURCE_VERIFICATION_CLASSIFICATION_POLICY_VERSION,
    compile_abstract_source_constraints,
    compile_continuation_caption_requirement,
    compile_explicit_keyword_count_range,
    compile_keyword_source_constraints,
    compile_known_source_obligations,
    compile_known_source_obligation_ids,
    has_explicit_keyword_count_signal,
    has_explicit_authoring_action_cue,
    is_explicit_authoring_content_quote,
    compile_security_marking_options,
    compile_security_marking_shorter_allowances,
    compile_soft_keyword_count_guidance,
    compile_source_content_verification_codes,
    compile_unresolved_manual_review_codes,
    materialize_complete_abstract_source_constraints,
    materialize_known_source_verification,
    materialize_source_verification_classifications,
    materialize_source_keyword_constraints,
    materialize_soft_keyword_count_guidance,
    source_fact_value_matches,
    has_mixed_external_document_action_signal,
)


class SourceObligationCompilerTests(unittest.TestCase):
    def test_publication_sentence_compiles_two_distinct_source_effects_only_when_complete(self) -> None:
        text = "未经批准的均为公开学位论文（公开的学位论文本项为空白）"
        facts = compile_known_source_obligations(text)
        self.assertEqual(
            {fact["id"] for fact in facts},
            {"cover.publication_default.unapproved_is_public",
             "cover.publication_default.public_blank"},
        )
        self.assertEqual({fact["evidence_text"] for fact in facts}, {
            "未经批准的均为公开学位论文", "公开的学位论文本项为空白",
        })
        for unsafe in (
            "示例：" + text, "“" + text + "”", text + "但另有例外",
            "未经批准的均为公开学位论文", "公开的学位论文本项为空白",
        ):
            with self.subTest(unsafe=unsafe):
                self.assertFalse(
                    {fact["id"] for fact in compile_known_source_obligations(unsafe)}
                    & {"cover.publication_default.unapproved_is_public",
                       "cover.publication_default.public_blank"}
                )

    def test_actual_circumstances_authoring_requires_an_explicit_content_directive(self) -> None:
        for quote in (
            "本部分主要介绍选题的背景及选题原因，根据实际情况自行填写",
            "本部分主要撰写选题的目的，根据实际情况自行填写",
            "本部分主要撰写选题的意义，根据实际情况自行填写",
            "本部分主要撰写选题的理论与应用价值，根据实际情况自行填写",
            "研究方法同上不再累述，根据实际情况填写",
            "根据论文实际情况撰写",
            "根据本人论文的实际情况填写",
        ):
            with self.subTest(quote=quote):
                self.assertTrue(is_explicit_authoring_content_quote(quote))

        for quote in (
            "如果需要，本部分主要撰写选题的意义，根据实际情况自行填写",
            "本部分不应撰写选题的意义，根据实际情况自行填写",
            "本部分主要撰写国内的研究现状，不能是文献资料的简单摘录，需要分类、总结、归纳",
            "示例：本部分主要撰写选题的意义，根据实际情况自行填写",
            "样例正文写着‘本部分主要撰写选题的意义，根据实际情况自行填写’",
            "本部分主要介绍图的样式，根据实际情况自行填写",
            "作者根据实际情况自行填写页码",
            "根据论文实际情况撰写图题",
            "这里演示图的样式，以下不再重复，根据论文实际情况添加即可",
            "作者须撰写摘要",
        ):
            with self.subTest(quote=quote):
                self.assertFalse(is_explicit_authoring_content_quote(quote))

    def test_chinese_phrase_for_english_keywords_resolves_one_language_and_cjk_cap(self) -> None:
        source = "英文关键词最多7个汉字"
        clause = {
            "id": "C_EN_KEYWORDS",
            "text": source,
            "source_text_full": source,
            "evidence_ids": ["E_EN_KEYWORDS"],
            "source_span": {
                "evidence_id": "E_EN_KEYWORDS",
                "start_offset": 0,
                "end_offset": len(source),
                "text": source,
                "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            },
        }

        compiled = compile_keyword_source_constraints(clause)

        self.assertEqual(compiled["language_key"], "keywords_en")
        self.assertEqual(compiled["properties"]["max_item_chars"], 7)
        self.assertEqual(compiled["properties"]["item_length_metric"], "cjk_characters")

    def test_source_keyword_constraints_project_to_nested_contract_without_semantic_drift(self) -> None:
        evidence = {
            "E00060": (
                "关键词是为了便于做文献索引和检索工作而从论文中选取出来用以表示全文主题内容信息的"
                "单词或术语，在论文中有明确出处，关键词在摘要内容后另起一行，一般3～8个，之间用分号分开。"
            ),
            "E00061": "关键词：术语；最多7个汉字；最少3组，最多8组",
            "E00066": (
                "Keywords in the abstract content after another line, generally 3~8, "
                "separated by semicolons. The key words are selected from the paper."
            ),
        }

        def clause(clause_id: str, evidence_id: str, quote: str) -> dict:
            full = evidence[evidence_id]
            start = full.index(quote)
            return {
                "id": clause_id,
                "text": quote,
                "source_text_full": full,
                "evidence_ids": [evidence_id],
                "source_span": {
                    "evidence_id": evidence_id,
                    "start_offset": start,
                    "end_offset": start + len(quote),
                    "text": quote,
                    "source_sha256": hashlib.sha256(full.encode("utf-8")).hexdigest(),
                },
            }

        clauses = [
            clause(
                "C00069", "E00060",
                "关键词在摘要内容后另起一行，一般3～8个，之间用分号分开",
            ),
            clause("C00071", "E00061", "最多7个汉字"),
            clause("C00072", "E00061", "最少3组，最多8组"),
            clause(
                "C00077", "E00066",
                "Keywords in the abstract content after another line, generally 3~8, "
                "separated by semicolons.",
            ),
            clause(
                "C00068", "E00060",
                "关键词是为了便于做文献索引和检索工作而从论文中选取出来用以表示全文主题内容信息的"
                "单词或术语，在论文中有明确出处",
            ),
        ]
        response = {
            "contract_version": "3.0",
            "requirements": [
                {
                    "role": "keywords_zh", "properties": {"separator": "semicolon"},
                    "clause_ids": ["C00069", "C00071", "C00072"],
                    "evidence_ids": ["E00060", "E00061"], "confidence": 0.97,
                },
                {
                    "role": "keywords_en", "properties": {"separator": "semicolon"},
                    "clause_ids": ["C00077"], "evidence_ids": ["E00066"],
                    "confidence": 0.98,
                },
            ],
            "clause_reviews": [
                {"clause_id": item["id"], "classification": "executable"}
                for item in clauses if item["id"] != "C00068"
            ] + [{"clause_id": "C00068", "classification": "requires_source_verification"}],
        }

        self.assertEqual(
            compile_keyword_source_constraints(clauses[3])["properties"]["separator"],
            "semicolon",
        )
        projected, audit = materialize_source_keyword_constraints(response, clauses)
        self.assertEqual(response["requirements"], projected["requirements"][:2])
        self.assertEqual(len(audit), 2)
        zh = projected["requirements"][2]["properties"]["keywords_zh"]
        en = projected["requirements"][3]["properties"]["keywords_en"]
        self.assertEqual(zh["count_guidance"], {
            "min_count": 3, "max_count": 8, "strength": "general_guidance",
        })
        self.assertEqual((zh["min_count"], zh["max_count"]), (3, 8))
        self.assertEqual((zh["max_item_chars"], zh["item_length_metric"]), (7, "cjk_characters"))
        self.assertEqual((zh["require_after_role"], zh["separator"]), ("abstract_body_zh", "semicolon"))
        self.assertEqual(en["count_guidance"], {
            "min_count": 3, "max_count": 8, "strength": "general_guidance",
        })
        self.assertEqual((en["require_after_role"], en["separator"]), ("abstract_body_en", "semicolon"))
        self.assertNotIn("min_count", en)
        self.assertNotIn("max_count", en)
        self.assertEqual(
            projected["clause_reviews"][-1]["classification"],
            "requires_source_verification",
        )
        self.assertNotIn("C00068", [
            clause_id for item in audit for clause_id in item["clause_ids"]
        ])
        again, second_audit = materialize_source_keyword_constraints(projected, clauses)
        self.assertEqual(again, projected)
        self.assertEqual(second_audit, [])

    def test_source_keyword_projection_preserves_conflicting_existing_values(self) -> None:
        source = "关键词：术语；最多7个汉字"
        clause = {
            "id": "C1", "text": "最多7个汉字", "source_text_full": source,
            "evidence_ids": ["E1"],
            "source_span": {
                "evidence_id": "E1", "start_offset": 7, "end_offset": 13,
                "text": "最多7个汉字",
                "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            },
        }
        response = {
            "contract_version": "3.0",
            "requirements": [
                {"role": "keywords_zh", "properties": {}, "clause_ids": ["C1"],
                 "evidence_ids": ["E1"], "confidence": 0.9},
                {"role": "content_constraints", "properties": {"keywords_zh": {
                    "max_item_chars": 99,
                }}, "clause_ids": ["C1"], "evidence_ids": ["E1"], "confidence": 0.9},
            ],
            "clause_reviews": [{"clause_id": "C1", "classification": "executable"}],
        }
        projected, audit = materialize_source_keyword_constraints(response, [clause])
        self.assertEqual(projected["requirements"][1]["properties"]["keywords_zh"]["max_item_chars"], 99)
        self.assertIn("max_item_chars", audit[0]["unprojected_conflicting_fields"])

    def test_source_keyword_projection_uses_directly_bound_content_constraint(self) -> None:
        sources = {
            "E0": "关键词在摘要内容后另起一行，一般3～8个，之间用分号分开",
            "E1": "关键词：术语；最多7个汉字；最少3组，最多8组",
        }

        def clause(clause_id: str, evidence_id: str, quote: str) -> dict:
            source = sources[evidence_id]
            start = source.index(quote)
            return {
                "id": clause_id, "text": quote, "source_text_full": source,
                "evidence_ids": [evidence_id],
                "source_span": {
                    "evidence_id": evidence_id, "start_offset": start,
                    "end_offset": start + len(quote), "text": quote,
                    "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                },
            }

        clauses = [
            clause("C69", "E0", sources["E0"]),
            clause("C71", "E1", "最多7个汉字"),
            clause("C72", "E1", "最少3组，最多8组"),
        ]
        response = {
            "contract_version": "3.0",
            "requirements": [
                {"role": "keywords_zh", "properties": {}, "clause_ids": ["C69"],
                 "evidence_ids": ["E0"], "confidence": 0.97},
                {"role": "content_constraints", "properties": {"keywords_zh": {
                    "min_count": None, "max_count": None,
                    "count_guidance": {"min_count": 3, "max_count": 8,
                                       "strength": "general_guidance"},
                }}, "clause_ids": ["C69", "C71", "C72"],
                 "evidence_ids": ["E0", "E1"], "confidence": 0.97},
            ],
            "clause_reviews": [
                {"clause_id": item["id"], "classification": "executable"}
                for item in clauses
            ],
        }

        projected, audits = materialize_source_keyword_constraints(response, clauses)
        self.assertEqual(len(projected["requirements"]), 2)
        nested = projected["requirements"][1]["properties"]["keywords_zh"]
        self.assertEqual((nested["min_count"], nested["max_count"]), (3, 8))
        self.assertEqual((nested["max_item_chars"], nested["item_length_metric"]),
                         (7, "cjk_characters"))
        self.assertEqual(audits[0]["parent_requirement_indexes"], [0, 1])
        again, second_audit = materialize_source_keyword_constraints(projected, clauses)
        self.assertEqual(again, projected)
        self.assertEqual(second_audit, [])

    def test_source_keyword_projection_rejects_unbound_content_constraint(self) -> None:
        source = "关键词：最少3组，最多8组"
        clause = {
            "id": "C72", "text": "最少3组，最多8组", "source_text_full": source,
            "evidence_ids": ["E1"],
            "source_span": {
                "evidence_id": "E1", "start_offset": source.index("最少"),
                "end_offset": len(source), "text": "最少3组，最多8组",
                "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            },
        }
        response = {
            "contract_version": "3.0",
            "requirements": [{
                "role": "content_constraints", "properties": {"keywords_zh": {
                    "count_guidance": {"min_count": 3, "max_count": 8,
                                       "strength": "general_guidance"},
                }}, "clause_ids": ["C72"], "evidence_ids": ["E_OTHER"],
                "confidence": 0.97,
            }],
            "clause_reviews": [{"clause_id": "C72", "classification": "executable"}],
        }
        projected, audits = materialize_source_keyword_constraints(response, [clause])
        self.assertEqual(projected, response)
        self.assertEqual(audits, [])

    def test_registered_keyword_source_verification_corrects_only_misclassified_human_check(self) -> None:
        source = (
            "关键词是为了便于做文献索引和检索工作而从论文中选取出来用以表示全文主题内容信息的"
            "单词或术语，在论文中有明确出处"
        )
        clause = {
            "id": "C00068", "text": source, "evidence_ids": ["E00060"],
        }
        response = {
            "contract_version": "3.0",
            "provenance": {"run_id": "run-current", "source_sha256": "a" * 64},
            "requirements": [],
            "clause_reviews": [{
                "clause_id": "C00068",
                "classification": "requires_source_content",
                "reason": "The actual keyword provenance must be supplied or verified.",
                "normative_basis": "explicit_normative_text",
                "obligations": [{
                    "id": "C00068-obligation-1",
                    "status": "requires_source_content",
                    "reason": "The keyword origin must be verified from the thesis.",
                }],
            }],
            "unsupported_items": [], "reported_conflicts": [],
        }

        projected, audit = materialize_source_verification_classifications(
            response, [clause], provenance=response["provenance"],
        )

        self.assertEqual(response["clause_reviews"][0]["classification"], "requires_source_content")
        self.assertEqual(response["clause_reviews"][0]["obligations"][0]["status"], "requires_source_content")
        self.assertEqual(projected["clause_reviews"][0]["classification"], "requires_source_verification")
        self.assertEqual(projected["clause_reviews"][0]["obligations"], [])
        self.assertEqual(
            projected["clause_reviews"][0]["reason"],
            response["clause_reviews"][0]["reason"],
        )
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["clause_id"], "C00068")
        self.assertEqual(
            audit[0]["policy_version"], SOURCE_VERIFICATION_CLASSIFICATION_POLICY_VERSION,
        )
        self.assertEqual(audit[0]["provenance"]["run_id"], "run-current")
        self.assertEqual(audit[0]["source_evidence_ids"], ["E00060"])
        self.assertTrue(audit[0]["human_verification_required"])
        self.assertIs(audit[0]["submission_ready"], False)
        self.assertEqual(audit[0]["original_primary_obligations"], response["clause_reviews"][0]["obligations"])
        again, second_audit = materialize_source_verification_classifications(
            projected, [clause], provenance=response["provenance"],
        )
        self.assertEqual(again, projected)
        self.assertEqual(second_audit, [])

    def test_source_verification_projection_does_not_erase_authoring_or_mixed_work(self) -> None:
        source = "关键词须源自论文并有明确出处。"
        response = {
            "contract_version": "3.0", "requirements": [],
            "clause_reviews": [{
                "clause_id": "C1", "classification": "requires_source_content",
                "reason": "pending", "obligations": [{
                    "id": "O1", "status": "requires_source_content", "reason": "pending",
                }],
            }],
        }
        cases = [
            # The exact source explicitly asks the author to provide genuine content.
            ([{"id": "C1", "text": "关键词须源自论文；作者应补充本人真实研究内容。", "evidence_ids": ["E1"]}], response),
            # A genuine author task need not mention a sample or placeholder.
            ([{"id": "C1", "text": "关键词须源自论文并有明确出处；作者须撰写摘要。", "evidence_ids": ["E1"]}], response),
            # A requirement edge means this is mixed with executable work.
            ([{"id": "C1", "text": source, "evidence_ids": ["E1"]}], {
                **response, "requirements": [{"clause_ids": ["C1"]}],
            }),
            # More than the narrowly recognized single source-content inventory is ambiguous.
            ([{"id": "C1", "text": source, "evidence_ids": ["E1"]}], {
                **response, "clause_reviews": [{
                    **response["clause_reviews"][0],
                    "obligations": [
                        response["clause_reviews"][0]["obligations"][0],
                        {"id": "O2", "status": "requires_source_content", "reason": "another duty"},
                    ],
                }],
            }),
            # Different disposition remains untouched.
            ([{"id": "C1", "text": source, "evidence_ids": ["E1"]}], {
                **response, "clause_reviews": [{
                    **response["clause_reviews"][0],
                    "obligations": [{"id": "O1", "status": "unverifiable", "reason": "pending"}],
                }],
            }),
        ]
        for clauses, candidate in cases:
            with self.subTest(clauses=clauses, requirements=candidate["requirements"]):
                projected, audit = materialize_source_verification_classifications(candidate, clauses)
                self.assertEqual(projected, candidate)
                self.assertEqual(audit, [])

        mixed_source = "关键词须源自论文并有明确出处；作者须撰写摘要。"
        self.assertEqual(
            compile_source_content_verification_codes(mixed_source),
            ["keyword_source_traceability_verification"],
        )
        self.assertFalse(is_explicit_authoring_content_quote(mixed_source))
        self.assertTrue(has_explicit_authoring_action_cue(mixed_source))

    def test_ambiguous_author_action_wording_conservatively_disables_projection(self) -> None:
        response = {
            "contract_version": "3.0", "requirements": [],
            "clause_reviews": [{
                "clause_id": "C1", "classification": "requires_source_content",
                "reason": "pending", "obligations": [{
                    "id": "O1", "status": "requires_source_content", "reason": "pending",
                }],
            }],
        }
        sources = [
            "关键词须源自论文并有明确出处；作者不得提供未经核实的关键词。",
            "关键词须源自论文并有明确出处；如作者提供关键词，应核查来源。",
            "关键词须源自论文并有明确出处；作者提供的论文需要注明出处。",
        ]
        for source in sources:
            with self.subTest(source=source):
                clause = {"id": "C1", "text": source, "evidence_ids": ["E1"]}
                self.assertTrue(has_explicit_authoring_action_cue(source))
                projected, audit = materialize_source_verification_classifications(
                    response, [clause],
                )
                self.assertEqual(projected, response)
                self.assertEqual(audit, [])

    def test_each_source_verification_projection_hashes_its_own_intermediate_response(self) -> None:
        source = "关键词须源自论文并有明确出处。"
        clauses = [
            {"id": clause_id, "text": source, "evidence_ids": [f"E{index}"]}
            for index, clause_id in enumerate(("C1", "C2"), start=1)
        ]
        response = {
            "contract_version": "3.0", "requirements": [],
            "clause_reviews": [{
                "clause_id": clause["id"],
                "classification": "requires_source_content",
                "reason": "Source-origin verification remains pending.",
                "obligations": [{
                    "id": f"{clause['id']}-O1",
                    "status": "requires_source_content",
                    "reason": "Verify the source in the thesis.",
                }],
            } for clause in clauses],
        }

        projected, audit = materialize_source_verification_classifications(response, clauses)
        first_intermediate = copy.deepcopy(response)
        first_intermediate["clause_reviews"][0]["classification"] = "requires_source_verification"
        first_intermediate["clause_reviews"][0]["obligations"] = []
        encode = lambda value: hashlib.sha256(json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()

        self.assertEqual(len(audit), 2)
        self.assertEqual(audit[0]["after_response_sha256"], encode(first_intermediate))
        self.assertEqual(audit[1]["after_response_sha256"], encode(projected))

    def test_mixed_external_and_local_document_action_cue_is_detected_conservatively(self) -> None:
        self.assertTrue(has_mixed_external_document_action_signal(
            "封面须写明学号，并由导师签字盖章"
        ))
        self.assertTrue(has_mixed_external_document_action_signal(
            "Enter the student ID and obtain the advisor's signature."
        ))
        self.assertTrue(has_mixed_external_document_action_signal(
            "封面应有学号，并由导师签字盖章"
        ))
        self.assertTrue(has_mixed_external_document_action_signal(
            "表格续页应重复表头，并由导师签字盖章"
        ))
        self.assertTrue(has_mixed_external_document_action_signal(
            "封面需要有作者姓名，导师负责签字盖章"
        ))
        self.assertTrue(has_mixed_external_document_action_signal(
            "签章后保留签名栏。"
        ))
        self.assertTrue(has_mixed_external_document_action_signal(
            "签字后在首页保留落款。"
        ))
        self.assertTrue(has_mixed_external_document_action_signal(
            "装订并保留签字页。"
        ))
        self.assertFalse(has_mixed_external_document_action_signal(
            "北京体育大学学位评定委员会办公室盖章(有效)"
        ))
        self.assertFalse(has_mixed_external_document_action_signal("封面须写明学号"))
        self.assertFalse(has_mixed_external_document_action_signal(None))

    def test_security_marking_choices_compile_and_project_from_exact_source(self) -> None:
        source = "□限制(≤2年) □秘密(≤10年) □机密(≤20年)"
        expected = [
            {"label": "限制", "maximum_duration": {"value": 2, "unit": "年"}},
            {"label": "秘密", "maximum_duration": {"value": 10, "unit": "年"}},
            {"label": "机密", "maximum_duration": {"value": 20, "unit": "年"}},
        ]
        self.assertEqual(compile_security_marking_options(source), expected)
        self.assertIn(
            "cover.security_marking_options", compile_known_source_obligation_ids(source),
        )
        fact = next(
            item for item in compile_known_source_obligations(source)
            if item["id"] == "cover.security_marking_options"
        )
        self.assertEqual(fact["expected_value"], expected)
        self.assertEqual(fact["required_checker_ids"], ["cover_non_public_administration"])

        clause = {"id": "C43", "text": source, "evidence_ids": ["E43"]}
        response = {
            "clause_reviews": [{"clause_id": "C43", "classification": "covered"}],
            "requirements": [{
                "role": "cover",
                "properties": {"non_public_administration": {
                    "applicability": {"status": "conditional", "conditions": []},
                    "fields": [{"id": "security_marking", "label": "密级", "order": 1}],
                    "public_policy": "blank", "source_region": "official_admin_table",
                }},
                "clause_ids": ["C43"], "evidence_ids": ["E43"],
            }],
        }
        projected, audit = materialize_known_source_verification(response, [clause])
        self.assertEqual(
            projected["requirements"][0]["properties"]["non_public_administration"][
                "security_marking_options"
            ], expected,
        )
        verification = projected["requirements"][0]["verification"]
        self.assertEqual(verification["mode"], "external")
        self.assertIn("cover_non_public_administration", verification["checker_ids"])
        self.assertEqual(audit[0]["authorization"], "exact_source_checkbox_duration_projection_v1")
        self.assertEqual(audit[0]["source_clause_ids"], ["C43"])
        self.assertNotIn("security_marking_options", response["requirements"][0]["properties"][
            "non_public_administration"
        ])
        _again, second_audit = materialize_known_source_verification(projected, [clause])
        self.assertEqual(second_audit, [])

    def test_security_marking_compiler_fails_closed_on_incomplete_or_ambiguous_choices(self) -> None:
        for source in (
            "□限制(≤2年)",
            "□限制(≤2年) □秘密",
            "□限制(≤2年) □限制(≤10年)",
            "例如 □限制(≤2年) □秘密(≤10年)",
            "如果 □限制(≤2年) □秘密(≤10年)",
        ):
            with self.subTest(source=source):
                self.assertIsNone(compile_security_marking_options(source))

    def test_shorter_security_marking_qualifier_compiles_exact_source_fact(self) -> None:
        source = "注：限制★2年(可少于2年)"
        expected = [{
            "label": "限制",
            "maximum_duration": {"value": 2, "unit": "年"},
            "shorter_duration_allowed": True,
        }]
        self.assertEqual(compile_security_marking_shorter_allowances(source), expected)
        obligation_id = "cover.security_marking_options.shorter_duration_allowed"
        self.assertIn(obligation_id, compile_known_source_obligation_ids(source))
        fact = next(
            item for item in compile_known_source_obligations(source)
            if item["id"] == obligation_id
        )
        self.assertEqual(fact["expected_value"], expected)
        self.assertEqual(fact["match_mode"], "security_marking_option")

        multi = "注：限制★2年(可少于2年)；秘密★10年(可少于10年)"
        self.assertEqual(len(compile_security_marking_shorter_allowances(multi) or []), 2)

    def test_shorter_security_marking_qualifier_fails_closed_on_mismatch_or_example(self) -> None:
        for source in (
            "注：限制★2年(可少于3年)",
            "注：限制★2年(可少于2月)",
            "注：限制★2年(可少于2年)，机密可少于20年",
            "例如：限制★2年(可少于2年)",
            "注：限制★2年(可少于2年)；限制★2年(可少于2年)",
        ):
            with self.subTest(source=source):
                self.assertIsNone(compile_security_marking_shorter_allowances(source))

    def test_security_marking_fact_match_allows_only_known_optional_qualifier(self) -> None:
        expected = [{
            "label": "限制", "maximum_duration": {"value": 2, "unit": "年"},
        }]
        with_qualifier = [{**expected[0], "shorter_duration_allowed": True}]
        with_unknown_property = [{**expected[0], "invented_policy": "yes"}]
        self.assertTrue(source_fact_value_matches(
            with_qualifier, expected, "security_marking_options",
        ))
        self.assertFalse(source_fact_value_matches(
            with_unknown_property, expected, "security_marking_options",
        ))

    def test_shorter_security_marking_qualifier_projects_only_exact_linked_values(self) -> None:
        source = "注：限制★2年(可少于2年)"
        clause = {"id": "C50", "text": source, "evidence_ids": ["E50"]}
        base = {
            "label": "限制", "maximum_duration": {"value": 2, "unit": "年"},
        }
        response = {
            "clause_reviews": [{"clause_id": "C50", "classification": "executable"}],
            "requirements": [{
                "role": "cover", "clause_ids": ["C50"], "evidence_ids": ["E50"],
                "properties": {"non_public_administration": {
                    "security_marking_options": [
                        base,
                        {"label": "秘密", "maximum_duration": {"value": 10, "unit": "年"}},
                    ],
                }},
                "verification": {"mode": "static_docx", "checks": [], "checker_ids": []},
            }],
        }
        projected, audit = materialize_known_source_verification(response, [clause])
        self.assertTrue(projected["requirements"][0]["properties"]["non_public_administration"]
                        ["security_marking_options"][0]["shorter_duration_allowed"])
        qualifier_audit = next(
            item for item in audit
            if item.get("authorization") == "exact_source_security_marking_qualifier_projection_v1"
        )
        self.assertEqual(qualifier_audit["source_clause_ids"], ["C50"])
        self.assertEqual(qualifier_audit["source_evidence_ids"], ["E50"])
        self.assertIn(
            "cover_non_public_administration",
            projected["requirements"][0]["verification"]["checker_ids"],
        )
        _again, second_audit = materialize_known_source_verification(projected, [clause])
        self.assertEqual(second_audit, [])

        conflicting = copy.deepcopy(response)
        conflicting["requirements"][0]["properties"]["non_public_administration"][
            "security_marking_options"][0]["shorter_duration_allowed"] = False
        unchanged, conflict_audit = materialize_known_source_verification(conflicting, [clause])
        self.assertFalse(unchanged["requirements"][0]["properties"]["non_public_administration"]
                         ["security_marking_options"][0]["shorter_duration_allowed"])
        self.assertFalse(any(
            item.get("authorization") == "exact_source_security_marking_qualifier_projection_v1"
            for item in conflict_audit
        ))

    def test_shorter_security_marking_qualifier_does_not_promote_unsupported_or_unlinked_requirements(self) -> None:
        source = "注：限制★2年(可少于2年)"
        clause = {"id": "C50", "text": source, "evidence_ids": ["E50"]}
        response = {
            "clause_reviews": [{"clause_id": "C50", "classification": "unsupported_backend"}],
            "requirements": [{
                "role": "cover", "clause_ids": ["C42"], "evidence_ids": ["E42"],
                "properties": {"non_public_administration": {
                    "security_marking_options": [
                        {"label": "限制", "maximum_duration": {"value": 2, "unit": "年"}},
                        {"label": "秘密", "maximum_duration": {"value": 10, "unit": "年"}},
                    ],
                }},
            }],
        }
        projected, audit = materialize_known_source_verification(response, [clause])
        self.assertNotIn(
            "shorter_duration_allowed",
            projected["requirements"][0]["properties"]["non_public_administration"]
            ["security_marking_options"][0],
        )
        self.assertEqual(audit, [])

    def test_security_marking_projection_never_overwrites_conflicts_or_guesses_links(self) -> None:
        source = "□限制(≤2年) □秘密(≤10年)"
        clause = {"id": "C43", "text": source, "evidence_ids": ["E43"]}
        wrong = [{"label": "限制", "maximum_duration": {"value": 9, "unit": "年"}}]
        base_requirement = {
            "role": "cover", "properties": {"non_public_administration": {
                "security_marking_options": wrong,
            }}, "clause_ids": ["C43"], "evidence_ids": ["E43"],
        }
        for requirements, classification in (
            ([base_requirement], "covered"),
            ([base_requirement, {**base_requirement}], "covered"),
            ([base_requirement], "external_compliance"),
        ):
            response = {
                "clause_reviews": [{"clause_id": "C43", "classification": classification}],
                "requirements": requirements,
            }
            projected, audit = materialize_known_source_verification(response, [clause])
            self.assertEqual(
                projected["requirements"][0]["properties"]["non_public_administration"][
                    "security_marking_options"
                ], wrong,
            )
            self.assertFalse(any(
                item.get("authorization") == "exact_source_checkbox_duration_projection_v1"
                for item in audit
            ))

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

    def test_soft_projection_preserves_hard_bounds_when_subject_is_in_full_evidence(self) -> None:
        clauses = [
            {
                "id": "C69", "text": "关键词一般3～8个", "source_text_full": "关键词一般3～8个",
                "evidence_ids": ["E1"],
            },
            {
                "id": "C72", "text": "at least 3 groups, with a maximum of 8 sets",
                "source_text_full": "Key Words: Terminology; at least 3 groups, with a maximum of 8 sets",
                "source_span": {"text": "at least 3 groups, with a maximum of 8 sets"},
                "evidence_ids": ["E1"],
            },
        ]
        response = {"requirements": [{
            "role": "content_constraints", "clause_ids": ["C69", "C72"],
            "evidence_ids": ["E1"],
            "properties": {"keywords_zh": {"min_count": 3, "max_count": 8}},
            "verification": {"checks": ["Verify the Chinese keyword count is 3 to 8."]},
        }]}

        self.assertTrue(has_explicit_keyword_count_signal(clauses[1]["source_text_full"]))
        self.assertIsNone(compile_explicit_keyword_count_range(clauses[1]["source_text_full"]))
        projected, audit = materialize_soft_keyword_count_guidance(response, clauses)

        rule = projected["requirements"][0]["properties"]["keywords_zh"]
        self.assertEqual((rule["min_count"], rule["max_count"]), (3, 8))
        self.assertEqual(
            rule["count_guidance"],
            {"min_count": 3, "max_count": 8, "strength": "general_guidance"},
        )
        self.assertTrue(audit[0]["mandatory_count_signal_present"])
        self.assertFalse(audit[0]["mandatory_count_signal_fully_compiled"])

    def test_mixed_explicit_count_units_remain_unresolved_and_block_scope(self) -> None:
        source = "Key Words: at least 3 groups, with a maximum of 8 sets."
        self.assertTrue(has_explicit_keyword_count_signal(source))
        self.assertIsNone(compile_explicit_keyword_count_range(source))
        self.assertEqual(
            compile_unresolved_manual_review_codes(source),
            ["quantitative_scope_unit_ambiguity"],
        )
        self.assertIsNotNone(compile_explicit_keyword_count_range(
            "Key Words: at least 3 groups, with a maximum of 8 groups."
        ))

    def test_registered_manual_correction_and_keyword_source_checks_are_narrow(self) -> None:
        correction_note = "The following English is not correct."
        self.assertEqual(
            compile_unresolved_manual_review_codes(correction_note),
            ["source_correction_target_ambiguity"],
        )
        self.assertEqual(
            compile_unresolved_manual_review_codes('The guide quotes: "The following English is not correct."'),
            [],
        )
        self.assertEqual(
            compile_unresolved_manual_review_codes("Please replace the following English with corrected text."),
            [],
        )

        keyword_source = (
            "关键词是为了便于做文献索引和检索工作而从论文中选取出来用以表示全文主题内容信息的"
            "单词或术语，在论文中有明确出处"
        )
        self.assertEqual(
            compile_source_content_verification_codes(keyword_source),
            ["keyword_source_traceability_verification"],
        )
        self.assertEqual(
            compile_source_content_verification_codes("关键词一般用分号分隔。"),
            [],
        )
        self.assertEqual(
            compile_source_content_verification_codes("例如，关键词应从论文中选取并有明确出处。"),
            [],
        )
        self.assertEqual(
            compile_source_content_verification_codes(
                "The following example: Keywords must originate from the thesis, but have a clear source in the text."
            ),
            [],
        )
        self.assertEqual(
            compile_source_content_verification_codes(
                "The guide says, for example, keywords must originate from the thesis, but have a clear source in the text."
            ),
            [],
        )
        self.assertEqual(
            compile_source_content_verification_codes(
                "An example was discussed. Keywords must originate from the thesis, but have a clear source in the text."
            ),
            ["keyword_source_traceability_verification"],
        )
        self.assertEqual(
            compile_source_content_verification_codes('指南引用：“关键词须源自论文。”'),
            [],
        )
        self.assertEqual(
            compile_source_content_verification_codes("关键词不必源自论文。"),
            [],
        )
        self.assertEqual(
            compile_source_content_verification_codes(
                "关键词须源自论文，但应在论文中有明确出处。"
            ),
            ["keyword_source_traceability_verification"],
        )
        self.assertEqual(
            compile_source_content_verification_codes(
                "Keywords must originate from the thesis, but have a clear source in the text."
            ),
            ["keyword_source_traceability_verification"],
        )

    def test_explicit_keyword_range_rejects_one_sided_or_different_units(self) -> None:
        self.assertIsNone(compile_explicit_keyword_count_range(
            "关键词最少3组，最多8个。"
        ))
        self.assertIsNone(compile_explicit_keyword_count_range(
            "关键词最少3组，最多8项。"
        ))
        self.assertEqual(
            compile_explicit_keyword_count_range("关键词最少3组，最多8组。"),
            {"min_count": 3, "max_count": 8},
        )
        self.assertIsNone(compile_explicit_keyword_count_range(
            "关键词最少3组，正文最多8组。"
        ))
        self.assertIsNone(compile_explicit_keyword_count_range(
            "Key Words: at least 3 groups, while the body maximum is 8 groups."
        ))
        self.assertEqual(compile_explicit_keyword_count_range(
            "Key Words: at least 3 groups, with a maximum of 8 groups."
        ), {"min_count": 3, "max_count": 8})

    def test_key_words_spacing_is_recognized_without_inventing_a_hard_bound(self) -> None:
        guidance = compile_soft_keyword_count_guidance("Key Words are generally 3 to 8.")
        self.assertIsNotNone(guidance)
        self.assertEqual(guidance["language_key"], "keywords_en")
        self.assertEqual(guidance["strength"], "general_guidance")

    def test_unsafe_unrelated_sentence_does_not_erase_safe_source_obligation(self) -> None:
        source = "续表应标注（续）。错误示例：公式中的变量不得省略。"
        self.assertIn(
            "table.continuation.caption_suffix",
            compile_known_source_obligation_ids(source),
        )

    def test_unsafe_same_target_sentence_suppresses_source_obligation_promotion(self) -> None:
        source = "续表题可以省略。错误示例：续表题不得省略。"
        ids = compile_known_source_obligation_ids(source)
        self.assertNotIn("table.continuation.caption_optional", ids)
        self.assertNotIn("table.continuation.caption_required", ids)

    def test_unitless_explicit_keyword_range_stays_unresolved(self) -> None:
        source = "关键词最少3，最多8"
        self.assertTrue(has_explicit_keyword_count_signal(source))
        self.assertIsNone(compile_explicit_keyword_count_range(source))
        self.assertEqual(
            compile_unresolved_manual_review_codes(source),
            ["quantitative_scope_unit_ambiguity"],
        )

    def test_keyword_character_limit_is_not_a_keyword_item_count(self) -> None:
        self.assertFalse(has_explicit_keyword_count_signal("关键词最多7个汉字"))
        self.assertFalse(has_explicit_keyword_count_signal("Key Words: up to 7 Chinese characters"))

    def test_character_limit_does_not_hide_separate_unresolved_keyword_count(self) -> None:
        source = "关键词最多7个汉字；最少3，最多8"
        self.assertTrue(has_explicit_keyword_count_signal(source))
        self.assertIsNone(compile_explicit_keyword_count_range(source))

    def test_abstract_materializer_uses_exact_bound_source_span(self) -> None:
        source = (
            "中文摘要是论文内容的简要陈述，一般以第三人称语气撰写，"
            "300～1000字（如遇特殊需要字数可以略多），不加评论和解释，"
            "是一篇具有独立性和完整性的短文，能准确反映论文的中心思想，"
            "规范的学术用语，逻辑性强、结构严谨，体现出论文的新理论、新方法、新技术等。"
        )
        clause = {
            "id": "C_RAW", "text": "不含摘要规则的规范化字段", "evidence_ids": ["E_RAW"],
            "source_span": {"text": source},
        }
        response = {
            "contract_version": "3.0",
            "clause_reviews": [{"clause_id": "C_RAW", "classification": "executable"}],
            "requirements": [],
        }
        projected, audit = materialize_complete_abstract_source_constraints(
            response, [clause],
        )
        self.assertEqual(len(projected["requirements"]), 1)
        self.assertEqual(projected["requirements"][0]["clause_ids"], ["C_RAW"])
        self.assertEqual(len(audit), 1)
        self.assertEqual(
            audit[0]["source_quote_sha256"],
            hashlib.sha256(source.encode("utf-8")).hexdigest(),
        )

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
                {"clause_id": cid, "classification": "executable", "reason": "source bundle identified",
                 "obligations": []}
                for cid in ("C66", "C67")
            ],
        }
        reviews_before = copy.deepcopy(response["clause_reviews"])
        projected, audit = materialize_complete_abstract_source_constraints(response, clauses)
        self.assertEqual(len(projected["requirements"]), 2)
        reviews = {item["clause_id"]: item for item in projected["clause_reviews"]}
        self.assertEqual(reviews["C66"]["classification"], "executable")
        self.assertEqual(reviews["C67"]["classification"], "executable")
        self.assertEqual(projected["clause_reviews"], reviews_before)
        zh_properties = projected["requirements"][0]["properties"]["abstract_zh"]
        self.assertEqual(zh_properties["third_person_guidance"], "general_guidance")
        self.assertNotIn("require_third_person", zh_properties)
        self.assertEqual(zh_properties["length_guidance"]["min_chars"], 300)
        self.assertNotIn("min_chars", zh_properties)
        self.assertTrue(zh_properties["prohibit_commentary"])
        self.assertIn("main_information_equivalent_to_thesis", projected["requirements"][1]["properties"]["abstract_zh"]["quality_guidance"])
        self.assertEqual(len(audit), 2)
        self.assertTrue(all(item["semantic_review_preserved"] for item in audit))
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
                {"clause_id": cid,
                 "classification": "executable" if cid == "C66" else "unresolved",
                 "reason": "source bundle identified" if cid == "C66" else "not represented",
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

    def test_unresolved_complete_abstract_bundle_is_never_projected_or_reclassified(self) -> None:
        source = (
            "中文摘要是论文内容的简要陈述，一般以第三人称语气撰写，"
            "300～1000字（如遇特殊需要字数可以略多），不加评论和解释，"
            "是一篇具有独立性和完整性的短文，能准确反映论文的中心思想，"
            "规范的学术用语，逻辑性强、结构严谨，体现出论文的新理论、新方法、新技术等"
        )
        clause = {"id": "C76", "text": source, "evidence_ids": ["E76"]}
        review = {
            "clause_id": "C76", "classification": "unresolved",
            "reason": "target remains semantically unresolved",
            "obligations": [{
                "id": "source-obligation", "status": "unresolved",
                "reason": "the target has not been confirmed",
            }],
        }
        response = {
            "contract_version": "3.0", "requirements": [],
            "clause_reviews": [copy.deepcopy(review)],
        }

        projected, audit = materialize_complete_abstract_source_constraints(
            response, [clause],
        )

        self.assertEqual(projected, response)
        self.assertEqual(projected["clause_reviews"][0]["classification"], "unresolved")
        self.assertEqual(projected["requirements"], [])
        self.assertEqual(audit, [])

        linked_response = copy.deepcopy(response)
        linked_response["requirements"].append({
            "role": "content_constraints", "clause_ids": ["C76"],
            "evidence_ids": ["E76"], "properties": {"abstract_zh": {}},
        })
        linked_projected, linked_audit = materialize_complete_abstract_source_constraints(
            linked_response, [clause],
        )
        self.assertEqual(linked_projected, linked_response)
        self.assertEqual(linked_audit, [])

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
