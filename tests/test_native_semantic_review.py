from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from native_semantic_review import (  # noqa: E402
    ExternalComplianceCorrectionRequiredError,
    MissingExecutableObligationInventoryError,
    NativeSemanticReviewError,
    OBLIGATION_COVERAGE_SCHEMA,
    RetryableNativeSemanticReviewError,
    build_obligation_coverage_request,
    validate_response,
    validate_obligation_coverage_response,
)
import native_semantic_review as native_review  # noqa: E402
from host_review_schema import native_schema_support_errors  # noqa: E402
from compliance import FORMAT_BLOCKING_STATES, normalized_state  # noqa: E402


class NativeSemanticReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.checks = [{
            "check_id": "abstract_zh.require_third_person",
            "document_text": "本文提出一种模型。",
        }, {
            "check_id": "abstract_en.required_sections",
            "document_text": "This study proposes a model.",
        }]

    def test_valid_exact_coverage_and_quotes_are_accepted(self) -> None:
        response = {"results": [
            {"check_id": "abstract_en.required_sections", "verdict": "uncertain",
             "rationale": "The source rule is unclear.", "evidence_quotes": ["This study"]},
            {"check_id": "abstract_zh.require_third_person", "verdict": "satisfied",
             "rationale": "Third person wording is present.", "evidence_quotes": ["本文提出"]},
        ]}
        results = validate_response(response, self.checks)
        self.assertEqual([item["check_id"] for item in results], sorted(
            item["check_id"] for item in self.checks
        ))

    def test_unknown_duplicate_missing_and_unquoted_evidence_are_rejected(self) -> None:
        valid = {
            "check_id": "abstract_zh.require_third_person", "verdict": "satisfied",
            "rationale": "Evidence supports this.", "evidence_quotes": ["本文提出"],
        }
        for response in (
            {"results": [valid, {**valid, "check_id": "unknown"}]},
            {"results": [valid, valid]},
            {"results": [valid]},
            {"results": [valid, {
                "check_id": "abstract_en.required_sections", "verdict": "satisfied",
                "rationale": "Not supported.", "evidence_quotes": ["not a quote"],
            }]},
        ):
            with self.subTest(response=response), self.assertRaises(NativeSemanticReviewError):
                validate_response(response, self.checks)

    def test_extra_fields_and_invalid_verdict_are_rejected_by_schema(self) -> None:
        response = {"results": [{
            "check_id": "abstract_zh.require_third_person", "verdict": "maybe",
            "rationale": "unclear", "evidence_quotes": ["本文提出"], "provenance": "forged",
        }]}
        with self.assertRaises(NativeSemanticReviewError):
            validate_response(response, self.checks)

    def test_source_obligation_packet_is_bound_to_current_clause_and_candidate(self) -> None:
        source = "表格应居中"
        chunk = {
            "case_id": "case-1",
            "provenance": {
                "run_id": "run-1", "source_sha256": "a" * 64,
                "clause_sha256": "b" * 64, "evidence_sha256": "c" * 64,
                "request_sha256": "d" * 64,
            },
            "clauses": [{"id": "C1", "text": source, "evidence_ids": ["E1"]}],
            "evidence_context": {"E1": {"id": "E1", "text": source}},
        }
        candidate = {
            "clause_reviews": [{
                "clause_id": "C1", "classification": "covered", "reason": "centered",
                "obligations": [{"id": "O1", "status": "covered", "reason": "center"}],
            }],
            "requirements": [{
                "id": "R1", "clause_ids": ["C1"], "role": "table",
                "properties": {"paragraph": {"alignment": "center"}},
                "evidence_ids": ["E1"], "verification": {"checker_ids": ["docx.property_receipts"]},
            }],
        }
        packet = build_obligation_coverage_request(candidate, chunk, run_id="run-1", chunk_index=4)
        check = packet["checks"][0]
        self.assertEqual(packet["protocol"], "native_source_obligation_coverage_review_v5")
        self.assertEqual(packet["provenance"], chunk["provenance"])
        self.assertEqual(check["document_text"], source)
        requirement_ref = check["review_context"]["linked_requirements"][0]["requirement_ref"]
        self.assertTrue(requirement_ref.startswith("RR"))
        self.assertNotIn("requirement_index", check["review_context"]["linked_requirements"][0])
        self.assertIn("table_caption.alignment_center", check["review_context"]["machine_obligation_ids"])

    def test_independent_obligation_review_requires_exact_full_coverage_and_safe_unresolved(self) -> None:
        check = {
            "check_id": "C1", "document_text": "表格应居中",
            "review_context": {
                "classification": "covered", "requires_requirement": True,
                "linked_requirements": [{"requirement_ref": "RR-table-1"}],
                "machine_obligation_ids": ["table_caption.alignment_center"],
            },
        }
        good = {"results": [{
            "check_id": "C1", "verdict": "consistent", "rationale": "The value is represented.",
            "evidence_quotes": ["表格应居中"],
            "machine_obligation_ids": ["table_caption.alignment_center"],
            "identified_obligations": [{
                "source_quote": "表格应居中", "disposition": "represented",
                "requirement_refs": ["RR-table-1"],
            }],
        }]}
        self.assertEqual(validate_obligation_coverage_response(good, [check])[0]["verdict"], "consistent")

        bad_cases = [
            {"results": [{**good["results"][0], "machine_obligation_ids": []}]},
            {"results": [{**good["results"][0], "identified_obligations": [{
                "source_quote": "表格应居中", "disposition": "represented", "requirement_refs": ["RR-unknown"],
            }]}]},
            {"results": [{**good["results"][0], "evidence_quotes": ["不是原文"]}]},
            {"results": [{**good["results"][0], "identified_obligations": [{
                "source_quote": "表格应居中", "disposition": "unrepresented", "requirement_refs": [],
            }]}]},
            {"results": [{**good["results"][0], "identified_obligations": [{
                "source_quote": "表格应居中", "disposition": "represented", "requirement_refs": ["RR-table-1", "RR-table-1"],
            }]}]},
        ]
        for response in bad_cases:
            with self.subTest(response=response), self.assertRaises(NativeSemanticReviewError):
                validate_obligation_coverage_response(response, [check])

    def test_scope_dependency_metadata_is_schema_bound_to_scope_unresolved(self) -> None:
        source = "密级"
        check = {
            "check_id": "C00004", "document_text": source,
            "review_context": {
                "classification": "covered", "requires_requirement": True,
                "linked_requirements": [{"requirement_ref": "RR-security"}],
                "machine_obligation_ids": [],
            },
        }
        valid = {"results": [{
            "check_id": "C00004", "verdict": "consistent",
            "rationale": "The conditional field is represented by the linked requirement.",
            "evidence_quotes": [source], "machine_obligation_ids": [],
            "identified_obligations": [{
                "source_quote": source, "disposition": "represented",
                "requirement_refs": ["RR-security"],
            }],
        }]}
        self.assertEqual(
            validate_obligation_coverage_response(valid, [check])[0]["verdict"],
            "consistent",
        )

        # Regression for the fresh BSU failure: a condition attached to a
        # represented requirement is not scope_unresolved metadata. Reject it
        # at the contract boundary; do not silently strip or reinterpret it.
        invalid = json.loads(json.dumps(valid))
        invalid["results"][0]["identified_obligations"][0][
            "scope_dependency_dimensions"
        ] = ["condition"]
        with self.assertRaisesRegex(NativeSemanticReviewError, "violates its JSON schema"):
            validate_obligation_coverage_response(invalid, [check])

        unresolved_check = {
            "check_id": "C2", "document_text": "该处约3cm",
            "review_context": {
                "classification": "unresolved", "requires_requirement": False,
                "linked_requirements": [], "machine_obligation_ids": [],
            },
        }
        safe_uncertain = {"results": [{
            "check_id": "C2", "verdict": "uncertain", "rationale": "The physical object is unclear.",
            "evidence_quotes": ["约3cm"], "machine_obligation_ids": [],
            "identified_obligations": [{
                "source_quote": "约3cm", "disposition": "ambiguous", "requirement_refs": [],
            }],
        }]}
        self.assertEqual(
            validate_obligation_coverage_response(safe_uncertain, [unresolved_check])[0]["verdict"],
            "uncertain",
        )

    def test_backend_unsupported_is_analysis_only_and_strictly_bound_to_primary_state(self) -> None:
        machine_id = "cover.security_marking_options.shorter_duration_allowed"
        source = "注：限制★2年(可少于2年)"
        check = {
            "check_id": "C00050", "document_text": source,
            "review_context": {
                "classification": "unsupported_backend", "requires_requirement": False,
                "primary_obligations": [], "linked_requirements": [],
                "machine_obligation_ids": [machine_id],
            },
        }
        accepted = {"results": [{
            "check_id": "C00050", "verdict": "backend_unsupported",
            "rationale": "The source qualifier is identified, but the current backend has no executor.",
            "evidence_quotes": [source], "machine_obligation_ids": [machine_id],
            "identified_obligations": [{
                "source_quote": "可少于2年", "disposition": "backend_unsupported",
                "requirement_refs": [],
            }],
        }]}
        result = validate_obligation_coverage_response(accepted, [check])
        self.assertEqual(result[0]["verdict"], "backend_unsupported")
        primary_state = normalized_state(check["review_context"]["classification"])
        self.assertEqual(primary_state, "unsupported_backend")
        self.assertIn(primary_state, FORMAT_BLOCKING_STATES)

        bad_responses = [
            {"results": [{**accepted["results"][0], "verdict": "consistent"}]},
            {"results": [{**accepted["results"][0], "identified_obligations": []}]},
            {"results": [{**accepted["results"][0], "identified_obligations": [{
                **accepted["results"][0]["identified_obligations"][0],
                "disposition": "unrepresented",
            }]}]},
            {"results": [{**accepted["results"][0], "identified_obligations": [
                accepted["results"][0]["identified_obligations"][0], {
                    "source_quote": "限制★2年", "disposition": "unrepresented",
                    "requirement_refs": [],
                },
            ]}]},
        ]
        for response in bad_responses:
            with self.subTest(response=response), self.assertRaises(NativeSemanticReviewError):
                validate_obligation_coverage_response(response, [check])

        linked_check = {
            **check,
            "review_context": {
                **check["review_context"],
                "linked_requirements": [{"requirement_ref": "RR-cover"}],
            },
        }
        with self.assertRaisesRegex(NativeSemanticReviewError, "analysis disposition"):
            validate_obligation_coverage_response(accepted, [linked_check])

        executable_check = {
            **check,
            "review_context": {
                **check["review_context"], "classification": "executable",
                "requires_requirement": True,
            },
        }
        with self.assertRaisesRegex(NativeSemanticReviewError, "analysis disposition"):
            validate_obligation_coverage_response(accepted, [executable_check])

    def test_unsupported_backend_cannot_be_called_consistent_or_release_ready(self) -> None:
        check = {
            "check_id": "C00050", "document_text": "可少于2年",
            "review_context": {
                "classification": "unsupported_backend", "requires_requirement": False,
                "primary_obligations": [], "linked_requirements": [],
                "machine_obligation_ids": [],
            },
        }
        response = {"results": [{
            "check_id": "C00050", "verdict": "consistent",
            "rationale": "The source was reviewed.", "evidence_quotes": ["可少于2年"],
            "machine_obligation_ids": [], "identified_obligations": [],
        }]}
        with self.assertRaisesRegex(NativeSemanticReviewError, "cannot be marked consistent"):
            validate_obligation_coverage_response(response, [check])

    def test_independent_review_requires_quote_even_when_informational_clause_has_no_obligations(self) -> None:
        check = {
            "check_id": "C00021", "document_text": "作者姓名",
            "review_context": {
                "classification": "informational", "requires_requirement": False,
                "linked_requirements": [], "machine_obligation_ids": [],
            },
        }
        response = {"results": [{
            "check_id": "C00021", "verdict": "consistent",
            "rationale": "The source is a label and states no independent obligation.",
            "evidence_quotes": [], "machine_obligation_ids": [],
            "identified_obligations": [],
        }]}
        quoted_response = {"results": [{
            **response["results"][0], "evidence_quotes": ["作者姓名"],
        }]}
        self.assertEqual(
            validate_obligation_coverage_response(quoted_response, [check])[0]["check_id"],
            "C00021",
        )
        with self.assertRaisesRegex(NativeSemanticReviewError, "requires at least 1 items"):
            validate_obligation_coverage_response(response, [check])

    def test_executable_consistent_review_with_empty_inventory_has_specific_error(self) -> None:
        source = "提交的学位论文电子版与纸质本论文的内容一致，如因不同造成不良后果由本人自负"
        check = {
            "check_id": "C00061", "document_text": source,
            "review_context": {
                "classification": "executable", "requires_requirement": True,
                "linked_requirements": [{"requirement_ref": "RR-declaration"}],
                "machine_obligation_ids": [], "manual_review_codes": [],
            },
        }
        response = {"results": [{
            "check_id": "C00061", "verdict": "consistent",
            "rationale": "The linked declaration appears to cover the source.",
            "evidence_quotes": [source], "machine_obligation_ids": [],
            "identified_obligations": [],
        }]}

        with self.assertRaises(MissingExecutableObligationInventoryError) as caught:
            validate_obligation_coverage_response(response, [check])
        self.assertEqual(caught.exception.clause_ids, ("C00061",))

    def test_retry_prompt_limits_missing_inventory_correction_to_same_candidate(self) -> None:
        prompt = native_review._prompt({
            "protocol": native_review.OBLIGATION_COVERAGE_PROTOCOL,
            "checks": [],
            "retry_feedback": {
                "code": MissingExecutableObligationInventoryError.code,
                "clause_ids": ["C00061"],
            },
        })
        self.assertIn("same candidate", prompt)
        self.assertIn("C00061", prompt)
        self.assertIn("Never invent an obligation", prompt)

    def test_author_input_is_pending_not_requirement_or_satisfaction(self) -> None:
        source = "以下示例内容是编写的，请作者根据需要自行撰写真实研究内容。"
        check = {
            "check_id": "C00102", "document_text": source,
            "review_context": {
                "classification": "requires_source_content", "requires_requirement": False,
                "linked_requirements": [], "machine_obligation_ids": [],
            },
        }
        pending = {"results": [{
            "check_id": "C00102", "verdict": "source_content_pending",
            "rationale": "The source explicitly asks the author to replace the sample with genuine content.",
            "evidence_quotes": ["请作者根据需要自行撰写真实研究内容"],
            "machine_obligation_ids": [],
            "identified_obligations": [{
                "source_quote": "请作者根据需要自行撰写真实研究内容",
                "disposition": "authoring_content_pending", "requirement_refs": [],
            }],
        }]}
        self.assertEqual(
            validate_obligation_coverage_response(pending, [check])[0]["verdict"],
            "source_content_pending",
        )

        unsafe_checks = [
            {**check, "review_context": {**check["review_context"], "classification": "informational"}},
            {**check, "review_context": {**check["review_context"], "linked_requirements": [{"requirement_ref": "RR-old"}]}},
        ]
        for unsafe_check in unsafe_checks:
            with self.subTest(context=unsafe_check["review_context"]), self.assertRaises(
                NativeSemanticReviewError,
            ):
                validate_obligation_coverage_response(pending, [unsafe_check])

        unrelated_source = "表格应居中，表题应置于表格上方。"
        unrelated_check = {
            "check_id": "C00200", "document_text": unrelated_source,
            "review_context": {
                "classification": "requires_source_content", "requires_requirement": False,
                "linked_requirements": [], "machine_obligation_ids": [],
            },
        }
        fabricated_pending = {"results": [{
            "check_id": "C00200", "verdict": "source_content_pending",
            "rationale": "The author still needs to provide content.",
            "evidence_quotes": [unrelated_source],
            "machine_obligation_ids": [],
            "identified_obligations": [{
                "source_quote": unrelated_source,
                "disposition": "authoring_content_pending", "requirement_refs": [],
            }],
        }]}
        with self.assertRaisesRegex(NativeSemanticReviewError, "explicit source authoring instruction"):
            validate_obligation_coverage_response(fabricated_pending, [unrelated_check])

        forged = json.loads(json.dumps(pending, ensure_ascii=False))
        forged["results"][0]["identified_obligations"][0]["requirement_refs"] = ["RR-forged"]
        with self.assertRaisesRegex(NativeSemanticReviewError, "unrelated requirement"):
            validate_obligation_coverage_response(forged, [check])

    def test_obligation_review_prompt_requires_quotes_for_zero_obligation_conclusions(self) -> None:
        prompt = native_review._prompt({
            "protocol": native_review.OBLIGATION_COVERAGE_PROTOCOL,
            "checks": [{
                "check_id": "C00021", "document_text": "作者姓名",
                "review_context": {"classification": "informational"},
            }],
        })
        self.assertIn("even when no obligations are identified or the clause is informational", prompt)
        self.assertIn("Never return an empty evidence_refs array", prompt)
        self.assertIn("Code resolves the selected ranges without changing whitespace or punctuation", prompt)

    def test_c00069_qualifier_hardening_is_incomplete_not_ambiguous(self) -> None:
        source = "关键词在摘要内容后另起一行，一般3～8个，之间用分号分开"
        check = {
            "check_id": "C00069",
            "document_text": source,
            "review_context": {
                "classification": "covered",
                "requires_requirement": True,
                "linked_requirements": [{"requirement_ref": "RR-keywords"}],
                "machine_obligation_ids": [
                    "keywords_zh.placement_after_abstract",
                    "keywords_zh.count_range",
                    "keywords_zh.separator",
                ],
            },
        }
        response = {"results": [{
            "check_id": "C00069",
            "verdict": "incomplete",
            "rationale": "The linked range turns the source's general guidance into a mandatory limit.",
            "evidence_quotes": [source],
            "machine_obligation_ids": check["review_context"]["machine_obligation_ids"],
            "identified_obligations": [
                {
                    "source_quote": "关键词在摘要内容后另起一行",
                    "disposition": "represented",
                    "requirement_refs": ["RR-keywords"],
                },
                {
                    "source_quote": "一般3～8个",
                    "disposition": "unrepresented",
                    "requirement_refs": ["RR-keywords"],
                },
                {
                    "source_quote": "之间用分号分开",
                    "disposition": "represented",
                    "requirement_refs": ["RR-keywords"],
                },
            ],
        }]}

        validated = validate_obligation_coverage_response(response, [check])
        self.assertEqual(validated[0]["verdict"], "incomplete")

        misleading_response = json.loads(json.dumps(response, ensure_ascii=False))
        misleading_response["results"][0]["identified_obligations"][1]["disposition"] = "ambiguous"
        with self.assertRaisesRegex(NativeSemanticReviewError, "lacks an unrepresented obligation"):
            validate_obligation_coverage_response(misleading_response, [check])

    def test_only_registered_unresolved_ambiguity_can_be_deferred_to_manual_review(self) -> None:
        source = (
            "The Chinese abstract is a brief statement of the content of the paper, "
            "300 to 1,000 words (the word count may be slightly extended if special requirements are encountered)."
        )
        check = {
            "check_id": "C00076",
            "document_text": source,
            "review_context": {
                "classification": "unresolved",
                "requires_requirement": False,
                "linked_requirements": [],
                "machine_obligation_ids": [],
                "manual_review_codes": ["abstract_target_metric_ambiguity"],
            },
        }
        deferred = {"results": [{
            "check_id": "C00076",
            "verdict": "manual_review_required",
            "rationale": "The named abstract language conflicts with the words metric; do not infer the target.",
            "evidence_quotes": ["The Chinese abstract", "300 to 1,000 words"],
            "machine_obligation_ids": [],
            "identified_obligations": [{
                "source_quote": "The Chinese abstract",
                "disposition": "ambiguous",
                "requirement_refs": [],
            }],
        }]}
        self.assertEqual(
            validate_obligation_coverage_response(deferred, [check])[0]["verdict"],
            "manual_review_required",
        )
        for unsafe in (
            {**check, "review_context": {**check["review_context"], "classification": "executable"}},
            {**check, "review_context": {**check["review_context"], "linked_requirements": [{"requirement_ref": "RR-x"}]}},
            {**check, "review_context": {**check["review_context"], "manual_review_codes": []}},
        ):
            with self.subTest(context=unsafe["review_context"]), self.assertRaises(NativeSemanticReviewError):
                validate_obligation_coverage_response(deferred, [unsafe])
        unrepresented = json.loads(json.dumps(deferred, ensure_ascii=False))
        unrepresented["results"][0]["identified_obligations"].append({
            "source_quote": "300 to 1,000 words",
            "disposition": "unrepresented",
            "requirement_refs": [],
        })
        with self.assertRaisesRegex(NativeSemanticReviewError, "manual deferral is not authorized"):
            validate_obligation_coverage_response(unrepresented, [check])

    def test_abstract_scope_ambiguity_code_excludes_examples_conditions_and_quotes(self) -> None:
        valid = (
            "The Chinese abstract is described as 300 to 1,000 words, "
            "with its applicable target still unclear."
        )
        self.assertEqual(
            native_review.compile_unresolved_manual_review_codes(valid),
            ["abstract_target_metric_ambiguity"],
        )
        unsafe_sources = [
            "For example, the Chinese abstract is 300 to 1,000 words.",
            "If applicable, the Chinese abstract is 300 to 1,000 words.",
            'The guide quotes: "The Chinese abstract is 300 to 1,000 words."',
        ]
        for source in unsafe_sources:
            with self.subTest(source=source):
                self.assertEqual(native_review.compile_unresolved_manual_review_codes(source), [])
                check = {
                    "check_id": "C00076", "document_text": source,
                    "review_context": {
                        "classification": "unresolved", "requires_requirement": False,
                        "linked_requirements": [], "machine_obligation_ids": [],
                        "manual_review_codes": native_review.compile_unresolved_manual_review_codes(source),
                    },
                }
                deferred = {"results": [{
                    "check_id": "C00076", "verdict": "manual_review_required",
                    "rationale": "Treat this scope as unresolved.",
                    "evidence_quotes": [source], "machine_obligation_ids": [],
                    "identified_obligations": [{
                        "source_quote": source, "disposition": "scope_unresolved",
                        "obligation_summary": "The target is unclear.",
                        "scope_dependency_codes": ["abstract_target_metric_ambiguity"],
                        "scope_dependency_dimensions": ["target"], "requirement_refs": [],
                    }],
                }]}
                with self.assertRaisesRegex(NativeSemanticReviewError, "not authorized"):
                    validate_obligation_coverage_response(deferred, [check])

    def test_scope_unresolved_records_analysis_without_claiming_execution_coverage(self) -> None:
        source = (
            "The Chinese abstract is usually written in third person, 300 to 1,000 words; "
            "it should not contain figures or tables."
        )
        check = {
            "check_id": "C00076",
            "document_text": source,
            "review_context": {
                "classification": "unresolved",
                "requires_requirement": False,
                "linked_requirements": [],
                "machine_obligation_ids": [],
                "manual_review_codes": ["abstract_target_metric_ambiguity"],
            },
        }
        accepted = {"results": [{
            "check_id": "C00076",
            "verdict": "manual_review_required",
            "rationale": "The source duties are identifiable but their abstract target is unresolved.",
            "evidence_quotes": ["The Chinese abstract", "300 to 1,000 words", "figures or tables"],
            "machine_obligation_ids": [],
            "identified_obligations": [
                {
                    "source_quote": "The Chinese abstract",
                    "disposition": "scope_unresolved",
                    "obligation_summary": "The guidance applies to an abstract whose language target is unresolved.",
                    "scope_dependency_codes": ["abstract_target_metric_ambiguity"],
                    "scope_dependency_dimensions": ["target"],
                    "requirement_refs": [],
                },
                {
                    "source_quote": "300 to 1,000 words",
                    "disposition": "scope_unresolved",
                    "obligation_summary": "The count is stated in words but its target abstract is unresolved.",
                    "scope_dependency_codes": ["abstract_target_metric_ambiguity"],
                    "scope_dependency_dimensions": ["target", "metric"],
                    "requirement_refs": [],
                },
                {
                    "source_quote": "figures or tables",
                    "disposition": "scope_unresolved",
                    "obligation_summary": "The prohibition is readable but its target abstract is unresolved.",
                    "scope_dependency_codes": ["abstract_target_metric_ambiguity"],
                    "scope_dependency_dimensions": ["target"],
                    "requirement_refs": [],
                },
            ],
        }]}
        result = validate_obligation_coverage_response(accepted, [check])[0]
        self.assertEqual(result["verdict"], "manual_review_required")
        self.assertTrue(all(
            item["disposition"] == "scope_unresolved"
            and item["requirement_refs"] == []
            for item in result["identified_obligations"]
        ))

        cases = []
        forged_code = json.loads(json.dumps(accepted))
        forged_code["results"][0]["identified_obligations"][0]["scope_dependency_codes"] = ["made_up"]
        cases.append(forged_code)
        wrong_dimension = json.loads(json.dumps(accepted))
        wrong_dimension["results"][0]["identified_obligations"][0]["scope_dependency_dimensions"] = ["strength"]
        cases.append(wrong_dimension)
        linked = json.loads(json.dumps(accepted))
        linked_check = {
            **check,
            "review_context": {
                **check["review_context"],
                "linked_requirements": [{"requirement_ref": "RR-1"}],
            },
        }
        cases.append((linked, linked_check))
        hidden_omission = json.loads(json.dumps(accepted))
        hidden_omission["results"][0]["identified_obligations"].append({
            "source_quote": "figures or tables",
            "disposition": "unrepresented",
            "requirement_refs": [],
        })
        cases.append(hidden_omission)
        for case in cases:
            candidate, candidate_check = case if isinstance(case, tuple) else (case, check)
            with self.subTest(case=candidate), self.assertRaises(NativeSemanticReviewError):
                validate_obligation_coverage_response(candidate, [candidate_check])

    def test_external_compliance_is_recorded_as_pending_not_docx_satisfied(self) -> None:
        source = "北京体育大学学位评定委员会办公室盖章(有效)"
        check = {
            "check_id": "C00049",
            "document_text": source,
            "review_context": {
                "classification": "external_compliance",
                "requires_requirement": False,
                "primary_obligations": [{
                    "id": "physical_stamp", "status": "unverifiable",
                    "reason": "Physical administrative stamping occurs outside the DOCX pipeline.",
                }],
                "linked_requirements": [],
                "machine_obligation_ids": [],
            },
        }
        pending = {"results": [{
            "check_id": "C00049",
            "verdict": "external_compliance_pending",
            "rationale": "The source requires a real administrative stamp, which this DOCX cannot provide.",
            "evidence_quotes": [source],
            "machine_obligation_ids": [],
            "identified_obligations": [{
                "source_quote": "办公室盖章(有效)",
                "disposition": "external_action_pending",
                "requirement_refs": [],
            }],
        }]}
        validated = validate_obligation_coverage_response(pending, [check])
        self.assertEqual(validated[0]["verdict"], "external_compliance_pending")

        unsafe_contexts = (
            {**check["review_context"], "requires_requirement": True},
            {**check["review_context"], "linked_requirements": [{"requirement_ref": "RR-x"}]},
            {**check["review_context"], "primary_obligations": [{
                "id": "physical_stamp", "status": "covered", "reason": "claimed covered",
            }]},
        )
        for context in unsafe_contexts:
            with self.subTest(context=context), self.assertRaises(NativeSemanticReviewError):
                validate_obligation_coverage_response(pending, [{**check, "review_context": context}])

        executable = {**check, "check_id": "C1", "review_context": {
            "classification": "executable", "requires_requirement": True,
            "linked_requirements": [{"requirement_ref": "RR-x"}],
            "machine_obligation_ids": [],
        }}
        forged = json.loads(json.dumps(pending, ensure_ascii=False))
        forged["results"][0]["check_id"] = "C1"
        with self.assertRaisesRegex(NativeSemanticReviewError, "only valid for external_compliance"):
            validate_obligation_coverage_response(forged, [executable])

    def test_external_unrepresented_source_action_requests_only_a_bounded_re_review(self) -> None:
        source = "学位论文作者签名： 年 月 日"
        check = {
            "check_id": "C00037", "document_text": source,
            "review_context": {
                "classification": "external_compliance", "requires_requirement": False,
                "primary_obligations": [], "linked_requirements": [],
                "machine_obligation_ids": [],
            },
        }
        incomplete = {"results": [{
            "check_id": "C00037", "verdict": "incomplete",
            "rationale": "The source action is not represented in the candidate inventory.",
            "evidence_quotes": [source], "machine_obligation_ids": [],
            "identified_obligations": [{
                "source_quote": source, "disposition": "unrepresented", "requirement_refs": [],
            }],
        }]}

        with self.assertRaises(ExternalComplianceCorrectionRequiredError) as caught:
            validate_obligation_coverage_response(incomplete, [check])
        self.assertEqual(caught.exception.check_ids, ("C00037",))
        self.assertEqual(caught.exception.corrections, ({
            "check_id": "C00037", "source_quotes": [source],
        },))

        linked_check = {
            **check,
            "review_context": {
                **check["review_context"],
                "linked_requirements": [{"requirement_ref": "RR-cover"}],
            },
        }
        with self.assertRaises(NativeSemanticReviewError) as unsafe:
            validate_obligation_coverage_response(incomplete, [linked_check])
        self.assertNotIsInstance(unsafe.exception, ExternalComplianceCorrectionRequiredError)

    def test_external_pending_correction_must_preserve_the_triggering_source_quote(self) -> None:
        original_quote = "学位论文作者签名： 年 月 日"
        alternate_quote = "原创性声明由作者负责"
        source = original_quote + "；" + alternate_quote
        check = {
            "check_id": "C00037", "document_text": source,
            "review_context": {
                "classification": "external_compliance", "requires_requirement": False,
                "primary_obligations": [], "linked_requirements": [],
                "machine_obligation_ids": [],
            },
        }
        request = {
            "protocol": native_review.OBLIGATION_COVERAGE_PROTOCOL,
            "checks": [check],
            "retry_feedback": {
                "code": ExternalComplianceCorrectionRequiredError.code,
                "checks": [{"check_id": "C00037", "source_quotes": [original_quote]}],
            },
        }
        alternate_pending = {"results": [{
            "check_id": "C00037", "verdict": "external_compliance_pending",
            "rationale": "An external action remains pending.",
            "evidence_quotes": [alternate_quote], "machine_obligation_ids": [],
            "identified_obligations": [{
                "source_quote": alternate_quote, "disposition": "external_action_pending",
                "requirement_refs": [],
            }],
        }]}
        validated = validate_obligation_coverage_response(alternate_pending, [check])
        self.assertEqual(validated[0]["verdict"], "external_compliance_pending")
        with self.assertRaisesRegex(
            NativeSemanticReviewError, "exact source-obligation inventory",
        ):
            native_review._validate_external_compliance_retry_result(alternate_pending, request)

        exact_pending = json.loads(json.dumps(alternate_pending, ensure_ascii=False))
        exact_pending["results"][0]["evidence_quotes"] = [original_quote]
        exact_pending["results"][0]["identified_obligations"][0]["source_quote"] = original_quote
        native_review._validate_external_compliance_retry_result(exact_pending, request)

    def test_external_pending_protocol_is_explicitly_non_docx_completion(self) -> None:
        prompt = native_review._prompt({
            "protocol": native_review.OBLIGATION_COVERAGE_PROTOCOL,
            "checks": [],
        })
        self.assertIn("external_compliance_pending", prompt)
        self.assertIn("external_action_pending", prompt)
        self.assertIn("never DOCX satisfaction", prompt)

    def test_external_compliance_retry_prompt_is_source_bound_and_not_a_pass_override(self) -> None:
        prompt = native_review._prompt({
            "protocol": native_review.OBLIGATION_COVERAGE_PROTOCOL,
            "checks": [],
            "retry_feedback": {
                "code": ExternalComplianceCorrectionRequiredError.code,
                "checks": [{
                    "check_id": "C00037", "source_quotes": ["学位论文作者签名： 年 月 日"],
                }],
            },
        })
        self.assertIn("same unchanged candidate", prompt)
        self.assertIn("C00037", prompt)
        self.assertIn("only if the source itself clearly requires a real-world action", prompt)
        self.assertIn("Any result that still fails the original local contract will be rejected", prompt)

    def test_native_runner_preserves_external_correction_signal_for_bridge(self) -> None:
        source = "学位论文作者签名： 年 月 日"
        check = {
            "check_id": "C00037", "document_text": source,
            "review_context": {
                "classification": "external_compliance", "requires_requirement": False,
                "primary_obligations": [], "linked_requirements": [],
                "machine_obligation_ids": [],
            },
        }
        request = {
            "protocol": native_review.OBLIGATION_COVERAGE_PROTOCOL,
            "case_id": "case", "run_id": "run", "checks": [check],
        }
        source_packet = native_review.build_source_reference_packet(request)
        source_ref = source_packet["checks"][0]["source_spans"][0]["ref_id"]
        response = {"results": [{
            "check_id": "C00037", "verdict": "incomplete",
            "rationale": "The external action is not represented in the primary inventory.",
            "evidence_refs": [source_ref],
            "identified_obligations": [{
                "source_ref": source_ref, "disposition": "unrepresented", "requirement_refs": [],
            }],
        }]}

        def write_last_message(**kwargs):
            kwargs["last_message_path"].write_text("{}", encoding="utf-8")
            return ["codex"]

        patches = self._stub_codex_host(CompletedProcess(["codex"], 0, "{}", ""))
        patches[4] = patch.object(
            native_review.codex_adapter, "build_command", side_effect=write_last_message,
        )
        patches.append(patch.object(
            native_review.codex_adapter, "parse_result",
            return_value=(response, {"event_types": ["task_complete"]}),
        ))
        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td) / "native"
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
                with self.assertRaises(ExternalComplianceCorrectionRequiredError):
                    native_review.run_native_semantic_review(
                        request, output_dir=output_dir, host_runtime="codex",
                        model="gpt-5.6-luna", timeout=5,
                    )
            self.assertTrue((output_dir / "compiled-response.json").is_file())
            self.assertFalse((output_dir / "response.json").exists())

    def test_source_clause_support_is_explicit_for_shared_requirement_edges(self) -> None:
        chunk = {
            "case_id": "case-1",
            "provenance": {"run_id": "run-1", "source_sha256": "a" * 64,
                           "clause_sha256": "b" * 64, "evidence_sha256": "c" * 64,
                           "request_sha256": "d" * 64},
            "clauses": [
                {"id": "C_SOFT", "text": "关键词一般3～8个", "evidence_ids": ["E1"]},
                {"id": "C_HARD", "text": "最少3组，最多8组", "evidence_ids": ["E2"]},
            ],
            "evidence_context": {},
        }
        response = {
            "clause_reviews": [
                {"clause_id": "C_SOFT", "classification": "executable", "reason": "source"},
                {"clause_id": "C_HARD", "classification": "executable", "reason": "source"},
            ],
            "requirements": [{
                "role": "content_constraints",
                "properties": {"keywords_zh": {
                    "min_count": 3, "max_count": 8,
                    "count_guidance": {"min_count": 3, "max_count": 8, "strength": "general_guidance"},
                }},
                "clause_ids": ["C_SOFT", "C_HARD"],
                "evidence_ids": ["E1", "E2"],
            }],
        }
        packet = build_obligation_coverage_request(response, chunk, run_id="run-1", chunk_index=1)
        check = next(item for item in packet["checks"] if item["check_id"] == "C_SOFT")
        self.assertEqual(check["review_context"]["source_clause_support"], [{
            "requirement_ref": check["review_context"]["linked_requirements"][0]["requirement_ref"],
            "clause_id": "C_HARD",
            "document_text": "最少3组，最多8组",
            "evidence_ids": ["E2"],
        }])

    def test_obligation_review_prompt_defines_qualifier_fidelity(self) -> None:
        prompt = native_review._prompt({
            "protocol": native_review.OBLIGATION_COVERAGE_PROTOCOL,
            "checks": [{
                "check_id": "C00069",
                "document_text": "一般3～8个",
                "review_context": {},
            }],
        })
        self.assertIn("hardening or weakening source qualifiers", prompt)
        self.assertIn("Use ambiguous only when the source text itself cannot be interpreted reliably", prompt)
        self.assertIn("use incomplete when any obligation is missing or materially misrepresented", prompt)
        self.assertIn("never emit numeric positions or invent a reference", prompt)

    def test_backend_unsupported_prompt_is_explicitly_analysis_only(self) -> None:
        prompt = native_review._prompt({
            "protocol": native_review.OBLIGATION_COVERAGE_PROTOCOL,
            "checks": [],
        })
        self.assertIn("analysis-only accounting", prompt)
        self.assertIn("not DOCX satisfaction", prompt)
        self.assertIn("never changes the primary unsupported_backend classification", prompt)

    def _stub_codex_host(self, process_result):
        context = SimpleNamespace(
            runtime="codex", as_audit=lambda: {"host_runtime": "codex"},
        )
        if isinstance(process_result, BaseException) or callable(process_result):
            run_process_patch = patch.object(
                native_review, "run_process", side_effect=process_result,
            )
        else:
            run_process_patch = patch.object(
                native_review, "run_process", return_value=process_result,
            )
        patches = [
            patch.object(native_review, "require_host_runtime", return_value=context),
            patch.object(native_review, "automatic_adapter_id", return_value="codex"),
            patch.object(native_review.codex_adapter, "resolve_binary", return_value="codex"),
            patch.object(native_review.codex_adapter, "probe_capabilities", return_value={
                "output_schema_supported": True, "structured_output_mode": "native_schema",
            }),
            patch.object(native_review.codex_adapter, "build_command", return_value=["codex"]),
            run_process_patch,
        ]
        return patches

    def test_native_runner_persists_timeout_outputs_before_failing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td) / "native"
            patches = self._stub_codex_host(CompletedProcess(
                ["codex"], 124, "partial stdout", "partial stderr\n[process-timeout] timed out",
            ))
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                with self.assertRaisesRegex(NativeSemanticReviewError, "exceeded 5 seconds"):
                    native_review.run_native_semantic_review(
                        {"case_id": "case", "run_id": "run", "checks": self.checks},
                        output_dir=output_dir, host_runtime="codex", model="gpt-5.6-luna",
                        timeout=5,
                    )
            self.assertEqual((output_dir / "stdout.jsonl").read_text(), "partial stdout")
            self.assertIn("[process-timeout]", (output_dir / "stderr.txt").read_text())
            self.assertFalse((output_dir / "response.json").exists())

    def test_native_runner_classifies_structured_codex_capacity_failure_for_bounded_retry(self) -> None:
        stdout = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "thread-capacity"}),
            json.dumps({"type": "error", "message": "Selected model is at capacity."}),
            json.dumps({
                "type": "turn.failed",
                "error": {"message": "Selected model is at capacity. Please try a different model."},
            }),
        ])
        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td) / "native"
            patches = self._stub_codex_host(CompletedProcess(
                ["codex"], 1, stdout, "provider capacity response",
            ))
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                with self.assertRaises(RetryableNativeSemanticReviewError) as caught:
                    native_review.run_native_semantic_review(
                        {
                            "protocol": native_review.OBLIGATION_COVERAGE_PROTOCOL,
                            "case_id": "case", "run_id": "run", "checks": self.checks,
                        },
                        output_dir=output_dir, host_runtime="codex", model="gpt-5.6-luna",
                        timeout=5,
                    )
            self.assertEqual(caught.exception.retry_code, "model_capacity")
            self.assertIn("turn.failed", (output_dir / "stdout.jsonl").read_text())
            self.assertIn("provider capacity response", (output_dir / "stderr.txt").read_text())
            self.assertFalse((output_dir / "response.json").exists())

    def test_codex_runner_uses_provider_compatible_projection_and_keeps_local_constraints(self) -> None:
        check = {
            "check_id": "C1", "document_text": "该处约3cm",
            "review_context": {
                "classification": "unresolved", "requires_requirement": False,
                "linked_requirements": [], "machine_obligation_ids": [],
            },
        }
        request = {
            "protocol": native_review.OBLIGATION_COVERAGE_PROTOCOL,
            "case_id": "case", "run_id": "run", "checks": [check],
        }
        packet = native_review.build_source_reference_packet(request)
        full_source_ref = packet["checks"][0]["source_spans"][0]["ref_id"]
        response = {"results": [{
            "check_id": "C1", "verdict": "uncertain", "rationale": "The source is ambiguous.",
            "evidence_refs": [full_source_ref],
            "identified_obligations": [{
                "source_ref": full_source_ref, "disposition": "ambiguous", "requirement_refs": [],
                "obligation_summary": None,
            }],
        }]}
        observed = {}

        def build_command(**kwargs):
            observed.update(kwargs)
            kwargs["last_message_path"].write_text("{}", encoding="utf-8")
            return ["codex"]

        patches = self._stub_codex_host(CompletedProcess(["codex"], 0, "{}", ""))
        patches[4] = patch.object(native_review.codex_adapter, "build_command", side_effect=build_command)
        patches.append(patch.object(
            native_review.codex_adapter, "parse_result",
            return_value=(response, {"event_types": ["task_complete"]}),
        ))
        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td) / "native"
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
                audit = native_review.run_native_semantic_review(
                    request,
                    output_dir=output_dir, host_runtime="codex", model="gpt-5.6-luna",
                    timeout=5,
                )

            local_schema = json.loads((output_dir / "response-schema.json").read_text(encoding="utf-8"))
            provider_schema_path = output_dir / "provider-response-schema.json"
            provider_schema = json.loads(provider_schema_path.read_text(encoding="utf-8"))
            expected_packet = native_review.build_source_reference_packet(request)
            self.assertEqual(local_schema, native_review.source_reference_schema(
                OBLIGATION_COVERAGE_SCHEMA, expected_packet, coverage=True,
            ))
            canonical_schema = json.loads(
                (output_dir / "canonical-response-schema.json").read_text(encoding="utf-8")
            )
            self.assertEqual(canonical_schema, OBLIGATION_COVERAGE_SCHEMA)
            self.assertEqual(observed["output_schema_path"], provider_schema_path)
            self.assertEqual(native_schema_support_errors(provider_schema), [])

            def contains_unique_items(node):
                if isinstance(node, dict):
                    return "uniqueItems" in node or any(contains_unique_items(value) for value in node.values())
                if isinstance(node, list):
                    return any(contains_unique_items(value) for value in node)
                return False

            self.assertTrue(contains_unique_items(local_schema))
            self.assertFalse(contains_unique_items(provider_schema))
            raw = json.loads((output_dir / "raw-response.json").read_text(encoding="utf-8"))
            compiled_candidate = json.loads(
                (output_dir / "compiled-response.json").read_text(encoding="utf-8")
            )
            canonical = json.loads((output_dir / "response.json").read_text(encoding="utf-8"))
            compilation = json.loads(
                (output_dir / "source-reference-compilation.json").read_text(encoding="utf-8")
            )
            self.assertEqual(raw["results"][0]["evidence_refs"], [full_source_ref])
            self.assertNotIn("machine_obligation_ids", raw["results"][0])
            self.assertNotIn(
                "scope_dependency_codes", raw["results"][0]["identified_obligations"][0],
            )
            self.assertEqual(canonical["results"][0]["evidence_quotes"], ["该处约3cm"])
            self.assertEqual(compiled_candidate, canonical)
            self.assertNotIn(
                "scope_dependency_codes",
                canonical["results"][0]["identified_obligations"][0],
            )
            self.assertNotIn(
                "scope_dependency_dimensions",
                canonical["results"][0]["identified_obligations"][0],
            )
            self.assertNotIn(
                "obligation_summary",
                canonical["results"][0]["identified_obligations"][0],
            )
            self.assertEqual(canonical["results"][0]["machine_obligation_ids"], [])
            self.assertEqual(audit["source_reference_protocol"], "semantic_source_references_v2")
            self.assertEqual(compilation["run_id"], "run")
            self.assertEqual(
                compilation["provider_nullable_normalization"]["policy"],
                "strict_native_optional_nulls_to_omitted_v1",
            )
            self.assertEqual(
                compilation["raw_response_sha256"],
                native_review.sha256_json(raw),
            )

    def test_native_runner_does_not_call_non_timeout_exit_124_a_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td) / "native"
            patches = self._stub_codex_host(CompletedProcess(
                ["codex"], 124, "partial stdout", "provider returned 124 without timeout marker",
            ))
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                with self.assertRaisesRegex(NativeSemanticReviewError, r"process failed \(124\)"):
                    native_review.run_native_semantic_review(
                        {"case_id": "case", "run_id": "run", "checks": self.checks},
                        output_dir=output_dir, host_runtime="codex", model="gpt-5.6-luna",
                        timeout=5,
                    )
            self.assertIn("without timeout marker", (output_dir / "stderr.txt").read_text())

    def test_native_runner_records_interrupt_and_declared_runtime_environment(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td) / "native"
            observed = {}

            def interrupt(command, **kwargs):
                observed.update(kwargs)
                raise KeyboardInterrupt

            patches = self._stub_codex_host(interrupt)
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
                with self.assertRaises(KeyboardInterrupt):
                    native_review.run_native_semantic_review(
                        {"case_id": "case", "run_id": "run", "checks": self.checks},
                        output_dir=output_dir, host_runtime="codex", model="gpt-5.6-luna",
                        timeout=5,
                    )
            self.assertEqual(observed["env"]["THESIS_FORGE_HOST_RUNTIME"], "codex")
            self.assertTrue((output_dir / "stdout.jsonl").exists())
            self.assertIn("[process-interrupted]", (output_dir / "stderr.txt").read_text())


if __name__ == "__main__":
    unittest.main()
