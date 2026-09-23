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
    build_obligation_coverage_request,
    validate_response,
    validate_obligation_coverage_response,
)
import native_semantic_review as native_review  # noqa: E402


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
