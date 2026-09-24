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
    NativeSemanticReviewError,
    OBLIGATION_COVERAGE_SCHEMA,
    RetryableNativeSemanticReviewError,
    build_obligation_coverage_request,
    validate_response,
    validate_obligation_coverage_response,
)
import native_semantic_review as native_review  # noqa: E402
from host_review_schema import native_schema_support_errors  # noqa: E402


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
        self.assertEqual(packet["protocol"], "native_source_obligation_coverage_review_v1")
        self.assertEqual(packet["provenance"], chunk["provenance"])
        self.assertEqual(check["document_text"], source)
        self.assertEqual(check["review_context"]["linked_requirements"][0]["requirement_index"], 0)
        self.assertIn("table_caption.alignment_center", check["review_context"]["machine_obligation_ids"])

    def test_independent_obligation_review_requires_exact_full_coverage_and_safe_unresolved(self) -> None:
        check = {
            "check_id": "C1", "document_text": "表格应居中",
            "review_context": {
                "classification": "covered", "requires_requirement": True,
                "linked_requirements": [{"requirement_index": 0}],
                "machine_obligation_ids": ["table_caption.alignment_center"],
            },
        }
        good = {"results": [{
            "check_id": "C1", "verdict": "consistent", "rationale": "The value is represented.",
            "evidence_quotes": ["表格应居中"],
            "machine_obligation_ids": ["table_caption.alignment_center"],
            "identified_obligations": [{
                "source_quote": "表格应居中", "disposition": "represented",
                "requirement_indexes": [0],
            }],
        }]}
        self.assertEqual(validate_obligation_coverage_response(good, [check])[0]["verdict"], "consistent")

        bad_cases = [
            {"results": [{**good["results"][0], "machine_obligation_ids": []}]},
            {"results": [{**good["results"][0], "identified_obligations": [{
                "source_quote": "表格应居中", "disposition": "represented", "requirement_indexes": [1],
            }]}]},
            {"results": [{**good["results"][0], "evidence_quotes": ["不是原文"]}]},
            {"results": [{**good["results"][0], "identified_obligations": [{
                "source_quote": "表格应居中", "disposition": "unrepresented", "requirement_indexes": [],
            }]}]},
            {"results": [{**good["results"][0], "identified_obligations": [{
                "source_quote": "表格应居中", "disposition": "represented", "requirement_indexes": [0, 0],
            }]}]},
        ]
        for response in bad_cases:
            with self.subTest(response=response), self.assertRaises(NativeSemanticReviewError):
                validate_obligation_coverage_response(response, [check])

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
                "source_quote": "约3cm", "disposition": "ambiguous", "requirement_indexes": [],
            }],
        }]}
        self.assertEqual(
            validate_obligation_coverage_response(safe_uncertain, [unresolved_check])[0]["verdict"],
            "uncertain",
        )

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

    def test_obligation_review_prompt_requires_quotes_for_zero_obligation_conclusions(self) -> None:
        prompt = native_review._prompt({
            "protocol": native_review.OBLIGATION_COVERAGE_PROTOCOL,
            "checks": [{
                "check_id": "C00021", "document_text": "作者姓名",
                "review_context": {"classification": "informational"},
            }],
        })
        self.assertIn("even when no obligations are identified or the clause is informational", prompt)
        self.assertIn("Never return an empty evidence_quotes array", prompt)

    def test_c00069_qualifier_hardening_is_incomplete_not_ambiguous(self) -> None:
        source = "关键词在摘要内容后另起一行，一般3～8个，之间用分号分开"
        check = {
            "check_id": "C00069",
            "document_text": source,
            "review_context": {
                "classification": "covered",
                "requires_requirement": True,
                "linked_requirements": [{"requirement_index": 1}],
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
                    "requirement_indexes": [1],
                },
                {
                    "source_quote": "一般3～8个",
                    "disposition": "unrepresented",
                    "requirement_indexes": [1],
                },
                {
                    "source_quote": "之间用分号分开",
                    "disposition": "represented",
                    "requirement_indexes": [1],
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
                "requirement_indexes": [],
            }],
        }]}
        self.assertEqual(
            validate_obligation_coverage_response(deferred, [check])[0]["verdict"],
            "manual_review_required",
        )
        for unsafe in (
            {**check, "review_context": {**check["review_context"], "classification": "executable"}},
            {**check, "review_context": {**check["review_context"], "linked_requirements": [{"requirement_index": 0}]}},
            {**check, "review_context": {**check["review_context"], "manual_review_codes": []}},
        ):
            with self.subTest(context=unsafe["review_context"]), self.assertRaises(NativeSemanticReviewError):
                validate_obligation_coverage_response(deferred, [unsafe])
        unrepresented = json.loads(json.dumps(deferred, ensure_ascii=False))
        unrepresented["results"][0]["identified_obligations"].append({
            "source_quote": "300 to 1,000 words",
            "disposition": "unrepresented",
            "requirement_indexes": [],
        })
        with self.assertRaisesRegex(NativeSemanticReviewError, "manual deferral is not authorized"):
            validate_obligation_coverage_response(unrepresented, [check])

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
                "requirement_indexes": [],
            }],
        }]}
        validated = validate_obligation_coverage_response(pending, [check])
        self.assertEqual(validated[0]["verdict"], "external_compliance_pending")

        unsafe_contexts = (
            {**check["review_context"], "requires_requirement": True},
            {**check["review_context"], "linked_requirements": [{"requirement_index": 0}]},
            {**check["review_context"], "primary_obligations": [{
                "id": "physical_stamp", "status": "covered", "reason": "claimed covered",
            }]},
        )
        for context in unsafe_contexts:
            with self.subTest(context=context), self.assertRaises(NativeSemanticReviewError):
                validate_obligation_coverage_response(pending, [{**check, "review_context": context}])

        executable = {**check, "check_id": "C1", "review_context": {
            "classification": "executable", "requires_requirement": True,
            "linked_requirements": [{"requirement_index": 0}],
            "machine_obligation_ids": [],
        }}
        forged = json.loads(json.dumps(pending, ensure_ascii=False))
        forged["results"][0]["check_id"] = "C1"
        with self.assertRaisesRegex(NativeSemanticReviewError, "only valid for external_compliance"):
            validate_obligation_coverage_response(forged, [executable])

    def test_external_pending_protocol_is_explicitly_non_docx_completion(self) -> None:
        prompt = native_review._prompt({
            "protocol": native_review.OBLIGATION_COVERAGE_PROTOCOL,
            "checks": [],
        })
        self.assertIn("external_compliance_pending", prompt)
        self.assertIn("external_action_pending", prompt)
        self.assertIn("never DOCX satisfaction", prompt)

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
            "requirement_index": 0,
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
        response = {"results": [{
            "check_id": "C1", "verdict": "uncertain", "rationale": "The source is ambiguous.",
            "evidence_quotes": ["3cm"], "machine_obligation_ids": [],
            "identified_obligations": [{
                "source_quote": "3cm", "disposition": "ambiguous", "requirement_indexes": [],
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
                native_review.run_native_semantic_review(
                    {
                        "protocol": native_review.OBLIGATION_COVERAGE_PROTOCOL,
                        "case_id": "case", "run_id": "run", "checks": [check],
                    },
                    output_dir=output_dir, host_runtime="codex", model="gpt-5.6-luna",
                    timeout=5,
                )

            local_schema = json.loads((output_dir / "response-schema.json").read_text(encoding="utf-8"))
            provider_schema_path = output_dir / "provider-response-schema.json"
            provider_schema = json.loads(provider_schema_path.read_text(encoding="utf-8"))
            self.assertEqual(local_schema, OBLIGATION_COVERAGE_SCHEMA)
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
