from __future__ import annotations

import copy
from concurrent.futures import ALL_COMPLETED
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import host_agent_bridge as bridge  # noqa: E402
import requirements_engine as engine  # noqa: E402
from semantic_contract import attach_request_provenance  # noqa: E402
from semantic_source_references import build_source_reference_packet  # noqa: E402


def bind_mock_review_to_source_spans(review_result: dict, request: dict, output_dir: Path) -> dict:
    """Give mocked native-review results the same request-bound compilation receipt as production."""
    packet = build_source_reference_packet(request)
    checks = {item["check_id"]: item for item in packet["checks"]}
    results = copy.deepcopy(review_result.get("results") or [])
    selections = []
    for result in results:
        check_id = result["check_id"]
        source_spans = checks[check_id]["source_spans"]

        def resolve_span(quote: str) -> dict:
            matches = [span for span in source_spans if quote in span["text"]]
            if not matches:
                raise AssertionError(f"fixture quote is not in source spans: {quote!r}")
            return min(matches, key=lambda span: len(span["text"]))

        selected_spans = []
        for obligation_index, obligation in enumerate(result.get("identified_obligations", [])):
            span = resolve_span(obligation["source_quote"])
            obligation["source_quote"] = span["text"]
            selected_spans.append({
                "obligation_index": obligation_index,
                "source_ref": span["ref_id"],
                "span": copy.deepcopy(span),
            })
        evidence_spans = []
        for index, quote in enumerate(result.get("evidence_quotes", [])):
            span = resolve_span(quote)
            result["evidence_quotes"][index] = span["text"]
            evidence_spans.append(span)
        selections.append({
            "check_id": check_id,
            "spans": list({span["ref_id"]: copy.deepcopy(span)
                           for span in [*evidence_spans, *[item["span"] for item in selected_spans]]}.values()),
            "obligations": selected_spans,
        })
    output_dir.mkdir(parents=True, exist_ok=True)
    request_sha = bridge.sha256_json(request)
    compilation = {
        "protocol": "semantic_source_references_v2",
        "run_id": request.get("run_id"),
        "request_sha256": request_sha,
        "packet_sha256": bridge.sha256_json(packet),
        "raw_response_sha256": "0" * 64,
        "compiled_response_sha256": "1" * 64,
        "selections": selections,
        "semantic_verdicts_unchanged": True,
    }
    compilation_path = output_dir / "source-reference-compilation.json"
    bridge._write_json(compilation_path, compilation)
    return {
        **review_result,
        "request_sha256": request_sha,
        "results": results,
        "source_reference_compilation_path": str(compilation_path),
        "source_reference_compilation_sha256": bridge.sha256_file(compilation_path),
    }


def exact_source_clause(clause_id: str, source: str, evidence_id: str = "E1") -> dict:
    """Build a clause fixture whose exact span is bound to its evidence text."""
    return {
        "id": clause_id,
        "text": source,
        "evidence_ids": [evidence_id],
        "source_span": {
            "evidence_id": evidence_id,
            "start_offset": 0,
            "end_offset": len(source),
            "text": source,
            "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        },
    }


class HostAgentBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._host_runtime_env = patch.dict(
            os.environ,
            {"THESIS_FORGE_HOST_RUNTIME": "openclaw"},
        )
        self._host_runtime_env.start()
        self._independent_review_patch = patch.object(
            bridge,
            "_run_independent_obligation_coverage_review",
            side_effect=self._fake_independent_review,
        )
        self._independent_review_patch.start()
        self.addCleanup(self._independent_review_patch.stop)

    def tearDown(self) -> None:
        self._host_runtime_env.stop()

    @staticmethod
    def _fake_independent_review(
        response: dict, chunk: dict, *, review_dir: Path, run_id: str,
        chunk_index: int, attempt: int, **_kwargs,
    ) -> dict:
        """Offline, source-bound reviewer double for bridge orchestration tests."""
        response_sha = bridge._response_sha256(response)
        provenance = response.get("provenance")
        findings = [{
            "check_id": str(item.get("clause_id")), "verdict": "consistent",
            "rationale": "offline bridge integration fixture",
            "evidence_quotes": [], "identified_obligations": [],
            "machine_obligation_ids": [],
        } for item in response.get("clause_reviews", []) if isinstance(item, dict)]
        out_dir = review_dir / f"independent-review-chunk-{chunk_index:04d}-attempt-{attempt:02d}"
        audit_path = out_dir / "coverage-audit.json"
        ledger_path = out_dir / "obligation-analysis-ledger.json"
        ledger = {
            "schema_version": "1.0", "protocol": "obligation_analysis_ledger_v1",
            "status": "analysis_only",
            "run_id": provenance.get("run_id") if isinstance(provenance, dict) else None,
            "case_id": None, "chunk_index": chunk_index, "attempt": attempt,
            "candidate_response_sha256": response_sha,
            "provenance": copy.deepcopy(provenance), "submission_ready": False,
            "obligations": [],
        }
        bridge._write_json(ledger_path, ledger)
        ledger_pointer = {
            "path": ledger_path.relative_to(review_dir).as_posix(),
            "sha256": bridge.sha256_file(ledger_path),
            "protocol": ledger["protocol"], "status": ledger["status"],
            "obligation_count": 0, "submission_ready": False,
        }
        envelope = {
            "protocol": bridge.OBLIGATION_COVERAGE_PROTOCOL,
            "status": "completed", "run_id": provenance.get("run_id") if isinstance(provenance, dict) else None,
            "chunk_index": chunk_index, "attempt": attempt,
            "candidate_response_sha256": response_sha,
            "provenance": copy.deepcopy(provenance), "results": findings,
            "obligation_analysis_ledger": ledger_pointer,
        }
        bridge._write_json(audit_path, envelope)
        return {
            "status": "completed", "protocol": envelope["protocol"],
            "audit_path": audit_path.relative_to(review_dir).as_posix(),
            "audit_sha256": bridge.sha256_file(audit_path), "run_id": run_id,
            "chunk_index": chunk_index, "candidate_response_sha256": response_sha,
            "obligation_analysis_ledger_path": ledger_pointer["path"],
            "obligation_analysis_ledger_sha256": ledger_pointer["sha256"],
            "obligation_analysis_ledger_status": ledger_pointer["status"],
            "summary": {"consistent": len(findings), "incomplete": 0, "uncertain": 0},
        }

    def test_independent_coverage_gate_persists_run_and_candidate_binding(self) -> None:
        self._independent_review_patch.stop()
        source = "表格应居中"
        provenance = {
            "run_id": "run-coverage-test", "source_sha256": "a" * 64,
            "clause_sha256": "b" * 64, "evidence_sha256": "c" * 64,
            "request_sha256": "d" * 64,
        }
        chunk = {
            "provenance": provenance,
            "clauses": [{
                "id": "C1", "text": source, "evidence_ids": ["E1"],
                "source_span": {
                    "evidence_id": "E1", "start_offset": 0, "end_offset": len(source),
                    "text": source, "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                },
            }],
            "evidence_context": {"E1": {"id": "E1", "text": source}},
        }
        response = {
            "provenance": provenance,
            "clause_reviews": [{
                "clause_id": "C1", "classification": "covered", "reason": "centered",
                "obligations": [{"id": "O1", "status": "covered", "reason": "center"}],
            }],
            "requirements": [{
                "id": "R1", "role": "table", "clause_ids": ["C1"], "evidence_ids": ["E1"],
                "properties": {"paragraph": {"alignment": "center"}},
                "verification": {"checker_ids": ["docx.property_receipts"]},
            }],
        }
        reviewer_result = {
            "protocol": bridge.OBLIGATION_COVERAGE_PROTOCOL,
            "status": "completed",
            "response_sha256": "f" * 64,
            "results": [{
                "check_id": "C1", "verdict": "consistent", "rationale": "source represented",
                "evidence_quotes": [source], "identified_obligations": [{
                    "source_quote": source, "disposition": "represented", "requirement_refs": ["RR-test"],
                }],
                "machine_obligation_ids": ["table_caption.alignment_center"],
            }],
            "summary": {"consistent": 1, "incomplete": 0, "uncertain": 0},
        }
        with tempfile.TemporaryDirectory() as td:
            review_dir = Path(td)
            coverage_request = bridge.build_obligation_coverage_request(
                response, chunk, run_id="run-coverage-test", chunk_index=1,
            )
            coverage_request["attempt"] = 1
            coverage_request["provider_attempt"] = 1
            source_packet = build_source_reference_packet(coverage_request)
            source_span = source_packet["checks"][0]["source_spans"][0]
            reviewer_result["request_sha256"] = bridge.sha256_json(coverage_request)
            compilation_path = (
                review_dir / "independent-review-chunk-0001-attempt-01"
                / "source-reference-compilation.json"
            )
            compilation_path.parent.mkdir(parents=True)
            compilation = {
                "protocol": "semantic_source_references_v2",
                "run_id": "run-coverage-test",
                "request_sha256": reviewer_result["request_sha256"],
                "packet_sha256": bridge.sha256_json(source_packet),
                "selections": [{
                    "check_id": "C1",
                    "spans": [source_span],
                    "obligations": [{
                        "obligation_index": 0,
                        "source_ref": source_span["ref_id"],
                        "span": source_span,
                    }],
                }],
            }
            bridge._write_json(compilation_path, compilation)
            reviewer_result["source_reference_compilation_path"] = str(compilation_path)
            reviewer_result["source_reference_compilation_sha256"] = bridge.sha256_file(
                compilation_path
            )
            with patch.object(bridge, "run_native_semantic_review", return_value=reviewer_result):
                pointer = bridge._run_independent_obligation_coverage_review(
                    response, chunk, review_dir=review_dir, run_id="run-coverage-test",
                    chunk_index=1, attempt=1, host_runtime="codex", model="gpt-5.6-luna",
                    timeout=10, agent_id="main", runner="exec", binary="codex",
                    config_path=None, controller=bridge.RunController(),
                )
            audit_path = review_dir / pointer["audit_path"]
            envelope = json.loads(audit_path.read_text(encoding="utf-8"))
            self.assertEqual(pointer["status"], "completed")
            self.assertEqual(pointer["audit_sha256"], bridge.sha256_file(audit_path))
            self.assertEqual(envelope["candidate_response_sha256"], bridge._response_sha256(response))
            self.assertEqual(envelope["provenance"], provenance)
            self.assertEqual(envelope["run_id"], "run-coverage-test")
            ledger = json.loads(
                (review_dir / pointer["obligation_analysis_ledger_path"]).read_text(encoding="utf-8")
            )
            self.assertFalse(ledger["submission_ready"])
            self.assertFalse(ledger["obligations"][0]["execution_authorized"])
            self.assertEqual(ledger["obligations"][0]["source_quote"], source)

    def test_c00076_scope_unresolved_is_persisted_only_as_non_executable_analysis(self) -> None:
        self._independent_review_patch.stop()
        source = (
            "The Chinese abstract is usually written in third person, "
            "300 to 1,000 words."
        )
        provenance = {
            "run_id": "run-c76-ledger", "source_sha256": "a" * 64,
            "clause_sha256": "b" * 64, "evidence_sha256": "c" * 64,
            "request_sha256": "d" * 64,
        }
        chunk = {
            "provenance": provenance,
            "clauses": [exact_source_clause("C00076", source, "E00076")],
            "evidence_context": {"E00076": {"id": "E00076", "text": source}},
        }
        response = {
            "provenance": provenance,
            "clause_reviews": [{
                "clause_id": "C00076", "classification": "unresolved",
                "reason": "The target abstract and metric remain ambiguous.",
            }],
            "requirements": [],
        }
        reviewer_result = {
            "protocol": bridge.OBLIGATION_COVERAGE_PROTOCOL,
            "status": "completed", "response_sha256": "f" * 64,
            "results": [{
                "check_id": "C00076", "verdict": "manual_review_required",
                "rationale": "The scope depends on the registered target and metric ambiguity.",
                "evidence_quotes": [source], "machine_obligation_ids": [],
                "identified_obligations": [{
                    "source_quote": source, "disposition": "scope_unresolved",
                    "obligation_summary": "The word-count guidance has an unresolved target.",
                    "scope_dependency_codes": ["abstract_target_metric_ambiguity"],
                    "scope_dependency_dimensions": ["target", "metric"],
                    "requirement_refs": [],
                }],
            }],
            "summary": {"consistent": 0, "incomplete": 0, "uncertain": 0},
        }
        with tempfile.TemporaryDirectory() as td:
            review_dir = Path(td)
            with patch.object(
                bridge, "run_native_semantic_review",
                side_effect=lambda request, **kwargs: bind_mock_review_to_source_spans(
                    reviewer_result, request, kwargs["output_dir"],
                ),
            ):
                pointer = bridge._run_independent_obligation_coverage_review(
                    response, chunk, review_dir=review_dir, run_id="run-c76-ledger",
                    chunk_index=1, attempt=1, host_runtime="codex", model="gpt-5.6-luna",
                    timeout=10, agent_id="main", runner="exec", binary="codex",
                    config_path=None, controller=bridge.RunController(),
                )
            ledger = json.loads(
                (review_dir / pointer["obligation_analysis_ledger_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(ledger["run_id"], "run-c76-ledger")
            self.assertEqual(ledger["candidate_response_sha256"], bridge._response_sha256(response))
            self.assertFalse(ledger["submission_ready"])
            self.assertEqual(ledger["obligations"][0]["disposition"], "scope_unresolved")
            self.assertEqual(ledger["obligations"][0]["requirement_refs"], [])
            self.assertFalse(ledger["obligations"][0]["execution_authorized"])

    def test_independent_coverage_retries_capacity_once_with_same_model_and_fresh_attempt_dir(self) -> None:
        self._independent_review_patch.stop()
        source = "表格应居中"
        provenance = {
            "run_id": "run-capacity-retry",
            "source_sha256": "a" * 64,
            "clause_sha256": "b" * 64,
            "evidence_sha256": "c" * 64,
            "request_sha256": "d" * 64,
        }
        chunk = {
            "provenance": provenance,
            "clauses": [exact_source_clause("C1", source)],
            "evidence_context": {"E1": {"id": "E1", "text": source}},
        }
        response = {
            "provenance": provenance,
            "clause_reviews": [{
                "clause_id": "C1", "classification": "covered", "reason": "centered",
                "obligations": [{"id": "O1", "status": "covered", "reason": "center"}],
            }],
            "requirements": [{
                "id": "R1", "role": "table", "clause_ids": ["C1"], "evidence_ids": ["E1"],
                "properties": {"paragraph": {"alignment": "center"}},
                "verification": {"checker_ids": ["docx.property_receipts"]},
            }],
        }
        review_result = {
            "protocol": bridge.OBLIGATION_COVERAGE_PROTOCOL,
            "status": "completed", "request_sha256": "e" * 64,
            "response_sha256": "f" * 64,
            "results": [{
                "check_id": "C1", "verdict": "consistent", "rationale": "source represented",
                "evidence_quotes": [source], "identified_obligations": [{
                    "source_quote": source, "disposition": "represented", "requirement_refs": ["RR-test"],
                }], "machine_obligation_ids": ["table_caption.alignment_center"],
            }],
            "summary": {"consistent": 1, "incomplete": 0, "uncertain": 0},
        }
        calls: list[tuple[dict, dict]] = []

        def fail_once_for_capacity(request: dict, **kwargs: dict) -> dict:
            calls.append((copy.deepcopy(request), kwargs))
            if len(calls) == 1:
                raise bridge.RetryableNativeSemanticReviewError(
                    "Codex reported model capacity", retry_code="model_capacity",
                )
            return bind_mock_review_to_source_spans(
                review_result, request, kwargs["output_dir"],
            )

        with tempfile.TemporaryDirectory() as td:
            review_dir = Path(td)
            with patch.object(bridge, "run_native_semantic_review", side_effect=fail_once_for_capacity), \
                    patch.object(bridge.time, "sleep") as sleep:
                pointer = bridge._run_independent_obligation_coverage_review(
                    response, chunk, review_dir=review_dir, run_id="run-capacity-retry",
                    chunk_index=3, attempt=1, host_runtime="codex", model="gpt-5.6-luna",
                    timeout=10, agent_id="main", runner="exec", binary="codex",
                    config_path=None, controller=bridge.RunController(),
                )

            self.assertEqual(len(calls), 2)
            self.assertEqual([kwargs["model"] for _, kwargs in calls], ["gpt-5.6-luna"] * 2)
            self.assertNotEqual(calls[0][1]["output_dir"], calls[1][1]["output_dir"])
            self.assertEqual([request["provider_attempt"] for request, _ in calls], [1, 2])
            self.assertEqual([request["attempt"] for request, _ in calls], [1, 1])
            self.assertEqual([request["provenance"] for request, _ in calls], [provenance, provenance])
            sleep.assert_called_once_with(bridge.INDEPENDENT_REVIEW_RETRY_BACKOFF_SECONDS)

            first_audit = review_dir / "independent-review-chunk-0003-attempt-01" / "coverage-audit.json"
            failed = json.loads(first_audit.read_text(encoding="utf-8"))
            self.assertEqual(failed["retry_code"], "model_capacity")
            self.assertTrue(failed["retryable"])

            accepted_audit = json.loads((review_dir / pointer["audit_path"]).read_text(encoding="utf-8"))
            self.assertEqual(pointer["provider_attempt"], 2)
            self.assertEqual(accepted_audit["provider_attempt_history"][0]["retry_code"], "model_capacity")
            self.assertEqual(accepted_audit["candidate_response_sha256"], bridge._response_sha256(response))

    @staticmethod
    def _missing_inventory_review_case() -> tuple[dict, dict, dict]:
        source = "提交的学位论文电子版与纸质本论文的内容一致，如因不同造成不良后果由本人自负"
        provenance = {
            "run_id": "run-empty-inventory-correction",
            "source_sha256": "a" * 64,
            "clause_sha256": "b" * 64,
            "evidence_sha256": "c" * 64,
            "request_sha256": "d" * 64,
        }
        chunk = {
            "provenance": provenance,
            "clauses": [{
                "id": "C00061", "text": source, "evidence_ids": ["E1"],
                "source_span": {
                    "evidence_id": "E1", "start_offset": 0, "end_offset": len(source),
                    "text": source, "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                },
            }],
            "evidence_context": {"E1": {"id": "E1", "text": source}},
        }
        response = {
            "provenance": provenance,
            "clause_reviews": [{
                "clause_id": "C00061", "classification": "executable",
                "reason": "This declaration must be preserved.",
                "obligations": [{"id": "O1", "status": "covered", "reason": "fixed text"}],
            }],
            "requirements": [{
                "id": "R1", "role": "declarations", "clause_ids": ["C00061"],
                "evidence_ids": ["E1"], "properties": {"declarations": {"body": source}},
                "verification": {"checker_ids": ["docx.declaration_text"]},
            }],
        }
        request = bridge.build_obligation_coverage_request(
            response, chunk, run_id="run-empty-inventory-correction", chunk_index=4,
        )
        requirement_ref = request["checks"][0]["review_context"]["linked_requirements"][0]["requirement_ref"]
        valid_review = {
            "protocol": bridge.OBLIGATION_COVERAGE_PROTOCOL,
            "status": "completed", "request_sha256": "e" * 64,
            "response_sha256": "f" * 64,
            "results": [{
                "check_id": "C00061", "verdict": "consistent",
                "rationale": "The fixed declaration is represented by its linked requirement.",
                "evidence_quotes": [source],
                "identified_obligations": [{
                    "source_quote": source, "disposition": "represented",
                    "requirement_refs": [requirement_ref],
                }],
                "machine_obligation_ids": [],
            }],
            "summary": {"consistent": 1, "incomplete": 0, "uncertain": 0},
        }
        return chunk, response, valid_review

    def test_independent_coverage_corrects_only_empty_executable_inventory_once(self) -> None:
        self._independent_review_patch.stop()
        chunk, response, valid_review = self._missing_inventory_review_case()
        initial_candidate_sha = bridge._response_sha256(response)
        calls: list[tuple[dict, dict]] = []

        def reject_then_review(request: dict, **kwargs: dict) -> dict:
            calls.append((copy.deepcopy(request), kwargs))
            if len(calls) == 1:
                raise bridge.MissingExecutableObligationInventoryError(["C00061"])
            return bind_mock_review_to_source_spans(
                valid_review, request, kwargs["output_dir"],
            )

        with tempfile.TemporaryDirectory() as td:
            review_dir = Path(td)
            with patch.object(bridge, "run_native_semantic_review", side_effect=reject_then_review), \
                    patch.object(bridge.time, "sleep") as sleep:
                pointer = bridge._run_independent_obligation_coverage_review(
                    response, chunk, review_dir=review_dir,
                    run_id="run-empty-inventory-correction", chunk_index=4, attempt=1,
                    host_runtime="codex", model="gpt-5.6-luna", timeout=10,
                    agent_id="main", runner="exec", binary="codex", config_path=None,
                    controller=bridge.RunController(),
                )

            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0][0]["provenance"], calls[1][0]["provenance"])
            self.assertEqual([item[0]["provider_attempt"] for item in calls], [1, 2])
            self.assertNotEqual(calls[0][1]["output_dir"], calls[1][1]["output_dir"])
            self.assertNotIn("retry_feedback", calls[0][0])
            self.assertEqual(calls[1][0]["retry_feedback"], {
                "code": bridge.MissingExecutableObligationInventoryError.code,
                "clause_ids": ["C00061"],
            })
            sleep.assert_called_once_with(bridge.INDEPENDENT_REVIEW_RETRY_BACKOFF_SECONDS)
            self.assertEqual(bridge._response_sha256(response), initial_candidate_sha)
            self.assertEqual(pointer["candidate_response_sha256"], initial_candidate_sha)

            first_audit_path = review_dir / (
                "independent-review-chunk-0004-attempt-01/coverage-audit.json"
            )
            first_audit = json.loads(first_audit_path.read_text(encoding="utf-8"))
            self.assertEqual(first_audit["status"], "rejected")
            self.assertTrue(first_audit["retryable"])
            self.assertEqual(first_audit["missing_clause_ids"], ["C00061"])

            accepted_audit = json.loads((review_dir / pointer["audit_path"]).read_text(encoding="utf-8"))
            self.assertEqual(accepted_audit["status"], "completed")
            self.assertEqual(accepted_audit["candidate_response_sha256"], initial_candidate_sha)
            self.assertEqual(accepted_audit["provider_attempt_history"][0]["status"], "semantic_contract_rejected")
            self.assertEqual(accepted_audit["retry_feedback"]["clause_ids"], ["C00061"])

    def test_independent_coverage_empty_inventory_correction_exhaustion_fails_closed(self) -> None:
        self._independent_review_patch.stop()
        chunk, response, _ = self._missing_inventory_review_case()
        with tempfile.TemporaryDirectory() as td:
            review_dir = Path(td)
            with patch.object(
                bridge, "run_native_semantic_review",
                side_effect=bridge.MissingExecutableObligationInventoryError(["C00061"]),
            ) as review_call, patch.object(bridge.time, "sleep") as sleep:
                with self.assertRaises(bridge.IndependentObligationReviewError) as caught:
                    bridge._run_independent_obligation_coverage_review(
                        response, chunk, review_dir=review_dir,
                        run_id="run-empty-inventory-correction", chunk_index=4, attempt=1,
                        host_runtime="codex", model="gpt-5.6-luna", timeout=10,
                        agent_id="main", runner="exec", binary="codex", config_path=None,
                        controller=bridge.RunController(),
                    )

            self.assertEqual(review_call.call_count, 2)
            sleep.assert_called_once_with(bridge.INDEPENDENT_REVIEW_RETRY_BACKOFF_SECONDS)
            self.assertEqual(
                caught.exception.error_records[0]["code"],
                "independent_obligation_review_correction_exhausted",
            )
            self.assertEqual(len(caught.exception.error_records[0]["provider_attempt_history"]), 2)
            for suffix in ("", "-provider-attempt-02"):
                audit_path = review_dir / (
                    "independent-review-chunk-0004-attempt-01" + suffix + "/coverage-audit.json"
                )
                self.assertEqual(json.loads(audit_path.read_text(encoding="utf-8"))["status"], "rejected")

    def test_independent_coverage_does_not_retry_unclassified_provider_or_contract_errors(self) -> None:
        self._independent_review_patch.stop()
        source = "表格应居中"
        provenance = {"run_id": "run-no-retry"}
        chunk = {
            "provenance": provenance,
            "clauses": [exact_source_clause("C1", source)],
            "evidence_context": {"E1": {"id": "E1", "text": source}},
        }
        response = {
            "provenance": provenance,
            "clause_reviews": [{"clause_id": "C1", "classification": "covered", "reason": "centered"}],
            "requirements": [{
                "id": "R1", "role": "table", "clause_ids": ["C1"], "evidence_ids": ["E1"],
                "properties": {"paragraph": {"alignment": "center"}},
            }],
        }
        with tempfile.TemporaryDirectory() as td:
            with patch.object(
                bridge, "run_native_semantic_review", side_effect=RuntimeError("invalid response contract"),
            ) as review_call:
                with self.assertRaises(bridge.IndependentObligationReviewError):
                    bridge._run_independent_obligation_coverage_review(
                        response, chunk, review_dir=Path(td), run_id="run-no-retry",
                        chunk_index=1, attempt=1, host_runtime="codex", model="gpt-5.6-luna",
                        timeout=10, agent_id="main", runner="exec", binary="codex",
                        config_path=None, controller=bridge.RunController(),
                    )
            self.assertEqual(review_call.call_count, 1)

    def test_independent_coverage_capacity_exhaustion_fails_closed_after_two_calls(self) -> None:
        self._independent_review_patch.stop()
        source = "表格应居中"
        provenance = {"run_id": "run-capacity-exhausted"}
        chunk = {
            "provenance": provenance,
            "clauses": [exact_source_clause("C1", source)],
            "evidence_context": {"E1": {"id": "E1", "text": source}},
        }
        response = {
            "provenance": provenance,
            "clause_reviews": [{"clause_id": "C1", "classification": "covered", "reason": "centered"}],
            "requirements": [{
                "id": "R1", "role": "table", "clause_ids": ["C1"], "evidence_ids": ["E1"],
                "properties": {"paragraph": {"alignment": "center"}},
            }],
        }
        with tempfile.TemporaryDirectory() as td:
            review_dir = Path(td)
            with patch.object(
                bridge,
                "run_native_semantic_review",
                side_effect=bridge.RetryableNativeSemanticReviewError(
                    "Codex reported model capacity", retry_code="model_capacity",
                ),
            ) as review_call, patch.object(bridge.time, "sleep") as sleep:
                with self.assertRaises(bridge.IndependentObligationReviewError) as caught:
                    bridge._run_independent_obligation_coverage_review(
                        response, chunk, review_dir=review_dir, run_id="run-capacity-exhausted",
                        chunk_index=2, attempt=1, host_runtime="codex", model="gpt-5.6-luna",
                        timeout=10, agent_id="main", runner="exec", binary="codex",
                        config_path=None, controller=bridge.RunController(),
                    )

            self.assertEqual(review_call.call_count, bridge.INDEPENDENT_REVIEW_PROVIDER_MAX_ATTEMPTS)
            sleep.assert_called_once_with(bridge.INDEPENDENT_REVIEW_RETRY_BACKOFF_SECONDS)
            record = caught.exception.error_records[0]
            self.assertEqual(record["code"], "independent_obligation_review_retry_exhausted")
            self.assertEqual(record["provider_attempts"], 2)
            self.assertEqual(len(record["provider_attempt_history"]), 2)
            for suffix in ("", "-provider-attempt-02"):
                attempt_dir = review_dir / ("independent-review-chunk-0002-attempt-01" + suffix)
                audit_path = attempt_dir / "coverage-audit.json"
                self.assertTrue(audit_path.is_file())
                self.assertEqual(json.loads(audit_path.read_text())["status"], "failed")

    def test_independent_coverage_gate_rejects_unrepresented_source_obligation(self) -> None:
        self._independent_review_patch.stop()
        source = "表格应居中"
        provenance = {"run_id": "run-coverage-test"}
        chunk = {
            "provenance": provenance,
            "clauses": [exact_source_clause("C1", source)],
            "evidence_context": {"E1": {"id": "E1", "text": source}},
        }
        response = {
            "provenance": provenance,
            "clause_reviews": [{"clause_id": "C1", "classification": "covered", "reason": "covered"}],
            "requirements": [{
                "id": "R1", "role": "table", "clause_ids": ["C1"], "evidence_ids": ["E1"],
                "properties": {},
            }],
        }
        reviewer_result = {
            "protocol": bridge.OBLIGATION_COVERAGE_PROTOCOL,
            "status": "completed", "request_sha256": "e" * 64,
            "response_sha256": "f" * 64,
            "results": [{
                "check_id": "C1", "verdict": "incomplete", "rationale": "alignment missing",
                "evidence_quotes": [source], "identified_obligations": [{
                    "source_quote": source, "disposition": "unrepresented", "requirement_refs": [],
                }], "machine_obligation_ids": ["table_caption.alignment_center"],
            }],
            "summary": {"consistent": 0, "incomplete": 1, "uncertain": 0},
        }
        with tempfile.TemporaryDirectory() as td:
            with patch.object(
                bridge, "run_native_semantic_review",
                side_effect=lambda request, **kwargs: bind_mock_review_to_source_spans(
                    reviewer_result, request, kwargs["output_dir"],
                ),
            ):
                with self.assertRaises(bridge.IndependentObligationReviewError) as caught:
                    bridge._run_independent_obligation_coverage_review(
                        response, chunk, review_dir=Path(td), run_id="run-coverage-test",
                        chunk_index=1, attempt=1, host_runtime="codex", model="gpt-5.6-luna",
                        timeout=10, agent_id="main", runner="exec", binary="codex",
                        config_path=None, controller=bridge.RunController(),
                    )
            self.assertEqual(
                caught.exception.error_records[0]["code"],
                "independent_obligation_review_incomplete",
            )
            self.assertTrue(caught.exception.retryable)
            pointer = caught.exception.independent_review_audit
            envelope = json.loads((Path(td) / pointer["audit_path"]).read_text(encoding="utf-8"))
            self.assertEqual(pointer["status"], "rejected")
            self.assertEqual(envelope["status"], "rejected")

    @staticmethod
    def _external_compliance_review_case() -> tuple[dict, dict, dict, str]:
        source = "学位论文作者签名： 年 月 日"
        provenance = {
            "run_id": "run-external-correction",
            "source_sha256": "a" * 64,
            "clause_sha256": "b" * 64,
            "evidence_sha256": "c" * 64,
            "request_sha256": "d" * 64,
        }
        chunk = {
            "provenance": provenance,
            "clauses": [{
                "id": "C00037", "text": source, "evidence_ids": ["E1"],
                "source_span": {
                    "evidence_id": "E1", "start_offset": 0, "end_offset": len(source),
                    "text": source, "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                },
            }],
            "evidence_context": {"E1": {"id": "E1", "text": source}},
        }
        response = {
            "provenance": provenance,
            "clause_reviews": [{
                "clause_id": "C00037", "classification": "external_compliance",
                "reason": "The author must sign and date the declaration.",
                "obligations": [],
            }],
            "requirements": [],
        }
        request = bridge.build_obligation_coverage_request(
            response, chunk, run_id="run-external-correction", chunk_index=2,
        )
        machine_ids = request["checks"][0]["review_context"]["machine_obligation_ids"]
        accepted_review = {
            "protocol": bridge.OBLIGATION_COVERAGE_PROTOCOL,
            "status": "completed", "request_sha256": "e" * 64,
            "response_sha256": "f" * 64,
            "results": [{
                "check_id": "C00037", "verdict": "external_compliance_pending",
                "rationale": "The author signature and date are external actions, not DOCX work.",
                "evidence_quotes": [source], "machine_obligation_ids": machine_ids,
                "identified_obligations": [{
                    "source_quote": source, "disposition": "external_action_pending",
                    "requirement_refs": [],
                }],
            }],
            "summary": {"consistent": 0, "incomplete": 0, "uncertain": 0},
        }
        return chunk, response, accepted_review, source

    def test_external_compliance_incomplete_review_gets_one_same_candidate_correction(self) -> None:
        self._independent_review_patch.stop()
        chunk, response, accepted_review, source = self._external_compliance_review_case()
        initial_response_sha = bridge._response_sha256(response)
        correction = bridge.ExternalComplianceCorrectionRequiredError([{
            "check_id": "C00037", "source_quotes": [source],
        }])
        calls: list[tuple[dict, dict]] = []

        def correct_external_review(request: dict, **kwargs: dict) -> dict:
            calls.append((copy.deepcopy(request), kwargs))
            if len(calls) == 1:
                raise correction
            return bind_mock_review_to_source_spans(
                accepted_review, request, kwargs["output_dir"],
            )

        with tempfile.TemporaryDirectory() as td:
            review_dir = Path(td)
            with patch.object(bridge, "run_native_semantic_review", side_effect=correct_external_review), \
                    patch.object(bridge.time, "sleep") as sleep:
                pointer = bridge._run_independent_obligation_coverage_review(
                    response, chunk, review_dir=review_dir, run_id="run-external-correction",
                    chunk_index=2, attempt=1, host_runtime="codex", model="gpt-5.6-luna",
                    timeout=10, agent_id="main", runner="exec", binary="codex",
                    config_path=None, controller=bridge.RunController(),
                )

            self.assertEqual(len(calls), 2)
            self.assertEqual([call[0]["provider_attempt"] for call in calls], [1, 2])
            self.assertEqual([call[1]["model"] for call in calls], ["gpt-5.6-luna"] * 2)
            self.assertNotEqual(calls[0][1]["output_dir"], calls[1][1]["output_dir"])
            self.assertEqual(calls[0][0]["provenance"], calls[1][0]["provenance"])
            self.assertEqual(calls[0][0]["checks"], calls[1][0]["checks"])
            self.assertNotIn("retry_feedback", calls[0][0])
            self.assertEqual(calls[1][0]["retry_feedback"], {
                "code": bridge.ExternalComplianceCorrectionRequiredError.code,
                "checks": [{"check_id": "C00037", "source_quotes": [source]}],
            })
            sleep.assert_called_once_with(bridge.INDEPENDENT_REVIEW_RETRY_BACKOFF_SECONDS)
            self.assertEqual(bridge._response_sha256(response), initial_response_sha)
            self.assertEqual(response["requirements"], [])
            self.assertEqual(response["clause_reviews"][0]["obligations"], [])
            self.assertEqual(pointer["status"], "completed")
            self.assertEqual(pointer["provider_attempt"], 2)
            self.assertEqual(pointer["candidate_response_sha256"], initial_response_sha)

            first_audit_path = review_dir / (
                "independent-review-chunk-0002-attempt-01/coverage-audit.json"
            )
            first_audit = json.loads(first_audit_path.read_text(encoding="utf-8"))
            self.assertEqual(first_audit["status"], "rejected")
            self.assertTrue(first_audit["retryable"])
            accepted_audit = json.loads(
                (review_dir / pointer["audit_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(accepted_audit["status"], "completed")
            self.assertEqual(
                accepted_audit["provider_attempt_history"][0]["retry_code"],
                bridge.ExternalComplianceCorrectionRequiredError.code,
            )
            self.assertEqual(accepted_audit["candidate_response_sha256"], initial_response_sha)

    def test_external_compliance_correction_exhaustion_fails_closed(self) -> None:
        self._independent_review_patch.stop()
        chunk, response, _, source = self._external_compliance_review_case()
        correction = bridge.ExternalComplianceCorrectionRequiredError([{
            "check_id": "C00037", "source_quotes": [source],
        }])
        with tempfile.TemporaryDirectory() as td:
            review_dir = Path(td)
            with patch.object(
                bridge, "run_native_semantic_review", side_effect=[correction, correction],
            ) as review_call, patch.object(bridge.time, "sleep") as sleep:
                with self.assertRaises(bridge.IndependentObligationReviewError) as caught:
                    bridge._run_independent_obligation_coverage_review(
                        response, chunk, review_dir=review_dir,
                        run_id="run-external-correction", chunk_index=2, attempt=1,
                        host_runtime="codex", model="gpt-5.6-luna", timeout=10,
                        agent_id="main", runner="exec", binary="codex", config_path=None,
                        controller=bridge.RunController(),
                    )

            self.assertEqual(review_call.call_count, bridge.INDEPENDENT_REVIEW_PROVIDER_MAX_ATTEMPTS)
            sleep.assert_called_once_with(bridge.INDEPENDENT_REVIEW_RETRY_BACKOFF_SECONDS)
            record = caught.exception.error_records[0]
            self.assertEqual(record["code"], "independent_obligation_review_correction_exhausted")
            self.assertEqual(record["retry_code"], bridge.ExternalComplianceCorrectionRequiredError.code)
            self.assertEqual(record["check_ids"], ["C00037"])
            self.assertFalse(getattr(caught.exception, "retryable", False))
            for suffix in ("", "-provider-attempt-02"):
                audit_path = review_dir / (
                    "independent-review-chunk-0002-attempt-01" + suffix + "/coverage-audit.json"
                )
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                self.assertEqual(audit["status"], "rejected")
                if suffix:
                    self.assertFalse(audit["retryable"])
                else:
                    self.assertTrue(audit["retryable"])

    def _packet(
        self, directory: Path, *, contract_version: str | None = None,
    ) -> tuple[Path, dict]:
        directory.mkdir(parents=True, exist_ok=True)
        clauses = [{
            "id": "C1", "text": "正文使用宋体", "evidence_ids": ["E1"],
            "source_kind": "paragraph", "location": {}, "part_index": 0,
        }]
        evidence = {"evidence": [{"id": "E1", "text": "正文使用宋体", "kind": "paragraph"}]}
        if contract_version == "3.0":
            clauses = self._bind_test_source_spans(clauses, evidence)
        if contract_version is None:
            request = engine.build_llm_request(
                [], clauses, evidence, {}, "full",
                runtime_context={"code_fingerprint_sha256": "f" * 64},
            )
        else:
            request = engine.build_llm_request(
                [], clauses, evidence, {}, "full", contract_version=contract_version,
                runtime_context={"code_fingerprint_sha256": "f" * 64},
            )
        request = attach_request_provenance(
            request, source_sha256="a" * 64, evidence_doc=evidence, clauses=clauses,
            run_id="run-bridge-test",
        )
        engine.prepare_host_agent_review_packets(
            request, clauses, evidence, "a" * 64, directory, chunk_size=1,
        )
        chunk = json.loads((directory / "llm-request-chunks.json").read_text(encoding="utf-8"))[0]
        return directory, chunk

    @staticmethod
    def _bind_test_source_spans(clauses: list[dict], evidence_doc: dict) -> list[dict]:
        evidence_by_id = {
            str(item.get("id")): item for item in evidence_doc.get("evidence", [])
            if isinstance(item, dict) and item.get("id")
        }
        for clause in clauses:
            evidence_ids = clause.get("evidence_ids") or []
            if len(evidence_ids) != 1:
                raise AssertionError("test source span requires one evidence ID")
            evidence = evidence_by_id[str(evidence_ids[0])]
            source = evidence["text"]
            exact = str(clause["text"])
            start = source.find(exact)
            if start < 0 or source.find(exact, start + 1) >= 0:
                raise AssertionError(f"test clause is not a unique evidence substring: {exact!r}")
            clause["source_span"] = {
                "evidence_id": str(evidence_ids[0]), "start_offset": start,
                "end_offset": start + len(exact), "text": exact,
                "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            }
        return clauses

    def _response(self, chunk: dict) -> dict:
        return {
            "contract_version": "2.1",
            "provenance": chunk["provenance"],
            "requirements": [],
            "clause_reviews": [{
                "clause_id": "C1",
                "classification": "informational",
                "requirement_indexes": [],
                "reason": "The current evidence was reviewed.",
            }],
            "unsupported_items": [],
            "reported_conflicts": [],
        }

    def _executable_response(self, chunk: dict, *, invalid_verification: bool = False) -> dict:
        response = self._response(chunk)
        response["requirements"] = [{
            "role": "body_text",
            "properties": {"font": {"cjk": "SimSun", "size_pt": 12}},
            "clause_ids": ["C1"],
            "evidence_ids": ["E1"],
            "confidence": 0.9,
            "reason": "The clause specifies the body-text font.",
            "verification": "word_render" if invalid_verification else {
                "mode": "word_render",
                "checks": ["Check the body-text font."],
            },
        }]
        response["clause_reviews"] = [{
            "clause_id": "C1",
            "classification": "executable",
            "requirement_indexes": [0],
            "reason": "The clause is executable in DOCX.",
        }]
        return response

    def test_compact_model_packet_preserves_the_machine_readable_contract(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _review_dir, chunk = self._packet(Path(td) / "requirements")
            chunk["source_continuity_context"] = {
                "policy": "orientation_only_not_for_review",
                "preceding_clauses": [{"id": "C0", "text": "前一段"}],
                "following_clauses": [{"id": "C2", "text": "后一段"}],
            }
            packet = bridge.compact_model_packet(chunk)
            contract = packet["requirement_contract"]
            self.assertEqual(contract["allowed_roles"], chunk["requirement_contract"]["allowed_roles"])
            self.assertEqual(
                contract["role_properties_schema"],
                chunk["requirement_contract"]["role_properties_schema"],
            )
            self.assertIn("roleSpec", contract["$defs"])
            self.assertIn("verificationSpec", packet["response_schema"]["$defs"])
            self.assertIn("inputPrerequisiteSpec", packet["response_schema"]["$defs"])
            self.assertNotIn("provenance", packet)
            self.assertEqual(
                packet["source_continuity_context"]["following_clauses"][0]["id"],
                "C2",
            )

    def test_chunk_packets_expose_bounded_continuity_without_expanding_scope(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td) / "requirements"
            clauses = [
                {"id": f"C{i}", "text": f"段落{i}", "evidence_ids": [f"E{i}"],
                 "source_kind": "paragraph", "location": {"order": i}, "part_index": 0}
                for i in range(1, 5)
            ]
            evidence = {"evidence": [
                {"id": f"E{i}", "text": f"段落{i}", "kind": "paragraph"}
                for i in range(1, 5)
            ]}
            request = engine.build_llm_request([], clauses, evidence, {}, "full")
            request = attach_request_provenance(
                request, source_sha256="a" * 64, evidence_doc=evidence,
                clauses=clauses, run_id="run-continuity-test",
            )
            engine.prepare_host_agent_review_packets(
                request, clauses, evidence, "a" * 64, directory, chunk_size=2,
            )
            chunks = json.loads((directory / "llm-request-chunks.json").read_text())
            self.assertEqual(
                [item["id"] for item in chunks[0]["source_continuity_context"]["following_clauses"]],
                ["C3", "C4"],
            )
            self.assertEqual(
                [item["id"] for item in chunks[1]["source_continuity_context"]["preceding_clauses"]],
                ["C1", "C2"],
            )
            self.assertEqual(chunks[0]["batch"]["clause_ids"], ["C1", "C2"])
            self.assertEqual(chunks[1]["batch"]["clause_ids"], ["C3", "C4"])

    def test_chunk_builder_keeps_one_physical_source_occurrence_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir = Path(td) / "requirements"
            clauses = [
                {"id": "C1", "text": "声明开头", "evidence_ids": ["E1"],
                 "source_kind": "paragraph", "location": {"order": 1}, "part_index": 0},
                {"id": "C2", "text": "声明续句", "evidence_ids": ["E1"],
                 "source_kind": "paragraph", "location": {"order": 1}, "part_index": 1},
                {"id": "C3", "text": "另一条格式", "evidence_ids": ["E2"],
                 "source_kind": "paragraph", "location": {"order": 2}, "part_index": 0},
            ]
            evidence = {"evidence": [
                {"id": "E1", "text": "声明开头声明续句", "kind": "paragraph",
                 "location": {"order": 1}},
                {"id": "E2", "text": "另一条格式", "kind": "paragraph",
                 "location": {"order": 2}},
            ]}
            request = engine.build_llm_request([], clauses, evidence, {}, "full")
            request = attach_request_provenance(
                request, source_sha256="a" * 64, evidence_doc=evidence,
                clauses=clauses, run_id="run-source-atomic-test",
            )
            manifest = engine.prepare_host_agent_review_packets(
                request, clauses, evidence, "a" * 64, review_dir, chunk_size=1,
            )
            chunks = json.loads((review_dir / "llm-request-chunks.json").read_text())
            self.assertEqual(manifest["chunk_count"], 2)
            self.assertEqual(chunks[0]["batch"]["clause_ids"], ["C1", "C2"])
            self.assertEqual(chunks[1]["batch"]["clause_ids"], ["C3"])
            engine.validate_host_review_chunk_source_projection(request, chunks, manifest)

    def test_declaration_anchor_preference_is_bound_to_current_structure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td) / "requirements"
            clauses = [{
                "id": "C1", "text": "固定声明正文", "evidence_ids": ["E1"],
                "source_kind": "paragraph", "location": {}, "part_index": 0,
            }]
            evidence = {
                "evidence": [{"id": "E1", "text": "固定声明正文", "kind": "paragraph"}],
                "structure_evidence": {"sections": [{
                    "first_paragraphs": [{"text": "摘要", "style_name": "Abstract Title CN"}],
                    "last_paragraphs": [],
                }]},
            }
            request = engine.build_llm_request([], clauses, evidence, {}, "full")
            self.assertEqual(request["declaration_anchor_candidates"], [
                "document_start", "abstract_title_zh",
            ])
            self.assertEqual(request["declaration_anchor_preference"], "abstract_title_zh")
            request = attach_request_provenance(
                request, source_sha256="a" * 64, evidence_doc=evidence,
                clauses=clauses, run_id="run-anchor-test",
            )
            engine.prepare_host_agent_review_packets(
                request, clauses, evidence, "a" * 64, directory, chunk_size=1,
            )
            chunk = json.loads((directory / "llm-request-chunks.json").read_text())[0]
            self.assertEqual(chunk["declaration_anchor_preference"], "abstract_title_zh")

    def test_preflight_rejects_invalid_declaration_anchor_and_signature_only_block(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _review_dir, chunk = self._packet(Path(td) / "requirements")
            chunk["clauses"][0]["text"] = "作者姓名"
            chunk["evidence_context"]["E1"]["text"] = "作者姓名"
            response = self._response(chunk)
            response["requirements"] = [{
                "role": "declarations",
                "properties": {
                    "before_role": "declarations",
                    "items": [{
                        "id": "author_signature",
                        "heading": "作者姓名",
                        "body_parts": ["年 月 日于北体大"],
                        "source_evidence_ids": ["E1"],
                        "signature_placeholders": [],
                    }],
                },
                "clause_ids": ["C1"], "evidence_ids": ["E1"],
                "confidence": 0.9, "reason": "固定文本",
            }]
            response["clause_reviews"] = [{
                "clause_id": "C1", "classification": "executable",
                "requirement_indexes": [0], "reason": "声明结构",
            }]
            errors = bridge.validate_host_agent_response(response, chunk)
            self.assertTrue(any("before_role" in error for error in errors), errors)
            self.assertTrue(any("generic author/date/signature" in error for error in errors), errors)

    def test_preflight_rejects_response_schema_drift(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _review_dir, chunk = self._packet(Path(td) / "requirements")
            response = self._executable_response(chunk, invalid_verification=True)
            errors = bridge.validate_host_agent_response(response, chunk)
            self.assertTrue(any("verification" in error for error in errors), errors)

    def test_preflight_rejects_sample_bibliography_as_executable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _review_dir, chunk = self._packet(Path(td) / "requirements")
            chunk["clauses"][0].update({
                "text": "[1] 示例作者. 示例文献[J]. 示例期刊, 2020.",
                "source_text_full": "[1] 示例作者. 示例文献[J]. 示例期刊, 2020.",
                "context_before": ["参考文献"],
                "context_after": [],
            })
            response = self._executable_response(chunk)
            errors = bridge.validate_host_agent_response(response, chunk)
            self.assertTrue(
                any("sample_content_cannot_be_executable" in error for error in errors),
                errors,
            )

    def test_preflight_rejects_informational_as_normative_basis(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _review_dir, chunk = self._packet(Path(td) / "requirements")
            response = self._response(chunk)
            response["clause_reviews"][0]["normative_basis"] = "informational"
            errors = bridge.validate_host_agent_response(response, chunk)
            self.assertTrue(any("normative_basis" in error for error in errors), errors)

    def test_preflight_rejects_requirement_index_on_nonexecutable_review(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _review_dir, chunk = self._packet(Path(td) / "requirements")
            response = self._executable_response(chunk)
            response["clause_reviews"][0]["classification"] = "informational"
            response["clause_reviews"][0]["requirement_indexes"] = [0]
            errors = bridge.validate_host_agent_response(response, chunk)
            self.assertTrue(
                any("nonexecutable_review_must_not_reference_requirement" in error for error in errors),
                errors,
            )

    def test_preflight_reports_exact_clause_and_matching_requirement_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _review_dir, chunk = self._packet(Path(td) / "requirements")
            chunk["clauses"].append({
                "id": "C2", "text": "标题居中", "evidence_ids": ["E2"],
                "source_kind": "paragraph", "location": {}, "part_index": 0,
            })
            chunk["evidence_context"]["E2"] = {
                "id": "E2", "text": "标题居中", "kind": "paragraph",
            }
            response = self._executable_response(chunk)
            response["requirements"].append({
                "role": "body_text",
                "properties": {"font": {"cjk": "SimSun", "size_pt": 12}},
                "clause_ids": ["C2"],
                "evidence_ids": ["E2"],
                "confidence": 0.9,
                "reason": "The clause specifies the heading alignment.",
                "verification": {
                    "mode": "word_render",
                    "checks": ["Check the heading alignment."],
                },
            })
            response["clause_reviews"][0]["requirement_indexes"] = [0, 1]
            response["clause_reviews"].append({
                "clause_id": "C2",
                "classification": "executable",
                "requirement_indexes": [1],
                "reason": "The clause is executable in DOCX.",
            })

            errors = bridge.validate_host_agent_response(response, chunk)
            backed_errors = [
                error for error in errors
                if "requirement_index_not_backed_by_clause" in error
            ]
            self.assertEqual(len(backed_errors), 1, errors)
            self.assertIn("clause_id=C1", backed_errors[0])
            self.assertIn("requirement_index=1", backed_errors[0])
            self.assertIn("matching_indexes=[0]", backed_errors[0])

            response["clause_reviews"][0]["requirement_indexes"] = [0]
            self.assertFalse(
                any(
                    "requirement_index_not_backed_by_clause" in error
                    for error in bridge.validate_host_agent_response(response, chunk)
                )
            )

    def test_host_prompt_exposes_mechanical_contract_rules(self) -> None:
        prompt = bridge._host_prompt(
            request_path=Path("request.json"),
            chunk_path=Path("chunk.json"),
            response_path=Path("response.json"),
            run_id="run-1",
            chunk_index=1,
            chunk_count=1,
        )
        self.assertIn("classification and normative_basis are different fields", prompt)
        self.assertIn("Only covered, executable, and verify_existing", prompt)
        self.assertIn("require_after_role", prompt)
        self.assertIn("A single clause may support multiple requirements", prompt)
        self.assertIn("Role boundary for equations", prompt)
        self.assertIn("partial_clause_coverage error never authorizes changing classification", prompt)
        retry = bridge._host_prompt(
            request_path=Path("request.json"),
            chunk_path=Path("chunk.json"),
            response_path=Path("response.json"),
            run_id="run-1",
            chunk_index=1,
            chunk_count=1,
            retry_hint="informational normative_basis nonexecutable_review_must_not_reference_requirement",
        )
        self.assertIn("targeted contract repair rules", retry)
        self.assertIn("Remove normative_basis", retry)
        self.assertIn("never replace it with another guessed value", retry)

        with tempfile.TemporaryDirectory() as td:
            parent_path = Path(td) / "parent-response.raw.json"
            parent_path.write_text('{"contract_version":"3.0","requirements":[],"clause_reviews":[]}', encoding="utf-8")
            retry_with_baseline = bridge._host_prompt(
                request_path=Path("request.json"),
                chunk_path=Path("chunk.json"),
                response_path=Path("response.json"),
                run_id="run-1",
                chunk_index=1,
                chunk_count=1,
                attempt=2,
                retry_hint="cover_binding_violation",
                retry_parent_response_sha256=bridge.sha256_file(parent_path),
                retry_parent_response_path=parent_path,
                retry_error_records=[{
                    "code": "cover_binding_violation",
                    "json_pointer": "$.requirements[0].properties.fields[2]",
                }],
            )
        self.assertIn(str(parent_path), retry_with_baseline)
        self.assertIn("Preserve every non-error semantic field", retry_with_baseline)
        self.assertIn("Do not rewrite explanatory reasons", retry_with_baseline)
        self.assertIn("Do not split, merge, add", retry_with_baseline)
        self.assertIn("FINAL RETRY INVARIANT", retry_with_baseline)
        self.assertIn("do not turn an unresolved or informational review into executable", retry_with_baseline)

    def test_retry_prompt_embeds_parent_semantics_without_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            parent_path = Path(td) / "parent-response.raw.json"
            parent_path.write_text(json.dumps({
                "contract_version": "3.0",
                "provenance": {"run_id": "must-not-be-copied"},
                "requirements": [{
                    "role": "body_text", "properties": {"font": {"cjk": "SimSun"}},
                    "clause_ids": ["C1"], "evidence_ids": ["E1"],
                    "reason": "preserve this semantic payload",
                }],
                "clause_reviews": [{
                    "clause_id": "C1", "classification": "executable",
                    "reason": "preserve this review",
                }],
                "unsupported_items": [], "reported_conflicts": [],
            }, ensure_ascii=False), encoding="utf-8")
            prompt = bridge._host_prompt(
                request_path=Path(td) / "request.json",
                chunk_path=Path(td) / "chunk.json",
                response_path=Path(td) / "response.json",
                run_id="run-1", chunk_index=1, chunk_count=1, attempt=2,
                retry_hint="local contract validation failed",
                retry_parent_response_sha256=bridge.sha256_file(parent_path),
                retry_parent_response_path=parent_path,
                retry_error_records=[{
                    "code": "missing_derived_requirement",
                    "json_pointer": "$.clause_reviews[0]",
                    "raw_error": "executable_review_requires_derived_requirement",
                }],
            )
        self.assertIn("<immutable_parent_response_without_provenance>", prompt)
        self.assertIn("preserve this semantic payload", prompt)
        self.assertIn("preserve this review", prompt)
        self.assertNotIn("must-not-be-copied", prompt)
        self.assertIn("Do not regenerate the response from the chunk", prompt)

    def test_retry_guidance_targets_exact_invalid_property_without_contradiction(self) -> None:
        retry = bridge._contract_repair_guidance(
            "local response contract validation failed: "
            "$.clause_reviews[12].normative_basis: 'informational' is not in "
            "['explicit_normative_text']; "
            "$.requirements[3].properties: unknown property 'style_hint'",
            include_base=False,
        )
        self.assertIn("clause_reviews[12]", retry)
        self.assertIn("remove the entire normative_basis property", retry)
        self.assertIn("Delete only the unknown property 'style_hint'", retry)
        self.assertNotIn("Do not repair by deleting evidence, clearing indexes", retry)

    def test_retry_guidance_targets_exact_requirement_clause_mapping(self) -> None:
        retry = bridge._contract_repair_guidance(
            "local response contract validation failed: "
            "$.clause_reviews[2]: requirement_index_not_backed_by_clause:"
            "clause_id=C00243:requirement_index=2:matching_indexes=[1]",
            include_base=False,
        )
        self.assertIn("clause_reviews[2]", retry)
        self.assertIn("C00243", retry)
        self.assertIn("requirements[2].clause_ids", retry)
        self.assertIn("matching requirement indexes are [1]", retry)
        self.assertIn("Do not copy a neighboring clause's index", retry)

    def test_retry_guidance_requires_payload_instead_of_field_key_only(self) -> None:
        retry = bridge._contract_repair_guidance(
            "local response contract validation failed: "
            "$.requirements[0].properties: must_include_semantic_payload",
            include_base=False,
        )
        self.assertIn("non-empty role-specific properties object", retry)
        self.assertIn("field_key alone", retry)
        self.assertIn("properties.text", retry)

    def test_retry_guidance_requires_real_semantic_obligation_inventory(self) -> None:
        retry = bridge._contract_repair_guidance(
            "$.clause_reviews[0].obligations: executable_review_requires_non_empty_inventory",
            include_base=False,
        )
        self.assertIn("non-empty array of distinct semantic duties", retry)
        self.assertIn("Do not", retry)
        self.assertIn("generic placeholder", retry)

    def test_retry_guidance_binds_prerequisites_and_admin_condition(self) -> None:
        retry = bridge._contract_repair_guidance(
            "local response contract validation failed: "
            "$.requirements[0].input_prerequisites[0].key: does not match "
            "'^(thesis_profile|source_inventory|template_profile|runtime)\\.'; "
            "$.requirements[1].properties.non_public_administration.applicability: "
            "must be conditional on thesis_profile.security_level",
            include_base=False,
        )
        self.assertIn("runtime_context.*", retry)
        self.assertIn("operator equals or in", retry)

    def test_invalid_retry_cannot_change_classification_to_escape_a_contract_error(self) -> None:
        previous = self._executable_response({"provenance": {}})
        current = self._executable_response({"provenance": {}})
        current["clause_reviews"][0]["classification"] = "informational"
        current["clause_reviews"][0].pop("requirement_indexes", None)
        change_error, changed = bridge._retry_semantic_change_error(
            previous,
            current,
            [{"code": "input_prerequisite_namespace"}],
            contract_version="2.1",
        )
        self.assertTrue(changed)
        self.assertIsNotNone(change_error)
        self.assertIn("semantic re-review", str(change_error))

    def test_v3_missing_obligation_inventory_retry_is_narrow_and_source_bound(self) -> None:
        source = "学位论文作者签名： 年 月 日"
        provenance = {
            "run_id": "run-inventory-retry", "source_sha256": "a" * 64,
            "clause_sha256": "b" * 64, "evidence_sha256": "c" * 64,
            "request_sha256": "d" * 64,
        }
        chunk = {
            "provenance": provenance,
            "batch": {"index": 2},
            "case_id": "case-inventory-retry",
            "response_schema": {"type": "object", "title": "retry test"},
            "runtime_context": {"code_fingerprint_sha256": "9" * 64},
            "clauses": [exact_source_clause("C00037", source)],
            "evidence_context": {"E1": {"id": "E1", "text": source}},
        }
        previous = {
            "contract_version": "3.0", "provenance": provenance,
            "requirements": [], "unsupported_items": [], "reported_conflicts": [],
            "clause_reviews": [{
                "clause_id": "C00037", "classification": "external_compliance",
                "normative_basis": "external_duty",
                "reason": "The author must sign and date the declaration.",
                "obligations": None,
            }],
        }
        current = copy.deepcopy(previous)
        current["clause_reviews"][0]["obligations"] = [{
            "id": "actual_signature_and_date", "status": "unverifiable",
            "reason": "The source requires an actual author signature and date.",
        }]
        records = [{
            "code": "executable_review_obligations_missing",
            "json_pointer": "$.clause_reviews[0].obligations",
            "clause_id": "C00037",
            "response_sha256": bridge._response_sha256(previous),
            "raw_error": "review_requires_non_empty_source_inventory",
        }]

        authorization: list[dict] = []
        error, changed = bridge._retry_semantic_change_error(
            previous, current, records, contract_version="3.0", chunk=chunk,
            authorization_out=authorization,
        )
        self.assertIsNone(error)
        self.assertEqual(changed, ["$.clause_reviews[0].obligations"])
        self.assertEqual(len(authorization), 1)
        self.assertEqual(authorization[0]["rule_id"], "v3_source_inventory_completion")
        self.assertTrue(authorization[0]["source_binding_complete"])
        self.assertEqual(authorization[0]["source_binding"]["run_id"], "run-inventory-retry")
        self.assertEqual(
            authorization[0]["source_evidence_bindings"][0]["clause_id"], "C00037",
        )

        # The allowance is not a blanket semantic retry grant.
        unsafe_candidates = []
        changed_classification = copy.deepcopy(current)
        changed_classification["clause_reviews"][0]["classification"] = "informational"
        unsafe_candidates.append(changed_classification)
        changed_reason = copy.deepcopy(current)
        changed_reason["clause_reviews"][0]["reason"] = "Unrelated rewrite"
        unsafe_candidates.append(changed_reason)
        covered_external_action = copy.deepcopy(current)
        covered_external_action["clause_reviews"][0]["obligations"][0]["status"] = "covered"
        unsafe_candidates.append(covered_external_action)
        unresolved_external_action = copy.deepcopy(current)
        unresolved_external_action["clause_reviews"][0]["obligations"][0]["status"] = "unresolved"
        unsafe_candidates.append(unresolved_external_action)
        empty_inventory = copy.deepcopy(current)
        empty_inventory["clause_reviews"][0]["obligations"] = []
        unsafe_candidates.append(empty_inventory)
        duplicate_ids = copy.deepcopy(current)
        duplicate_ids["clause_reviews"][0]["obligations"].append(
            copy.deepcopy(duplicate_ids["clause_reviews"][0]["obligations"][0])
        )
        unsafe_candidates.append(duplicate_ids)
        changed_requirement_graph = copy.deepcopy(current)
        changed_requirement_graph["requirements"].append({
            "role": "paragraph", "properties": {"text": source},
            "clause_ids": ["C00037"], "evidence_ids": ["E1"],
            "reason": "Unauthorized new requirement edge",
        })
        unsafe_candidates.append(changed_requirement_graph)

        for candidate in unsafe_candidates:
            with self.subTest(candidate=candidate):
                candidate_error, _ = bridge._retry_semantic_change_error(
                    previous, candidate, records, contract_version="3.0", chunk=chunk,
                )
                self.assertIsNotNone(candidate_error)

        stale_record = copy.deepcopy(records)
        stale_record[0]["response_sha256"] = "0" * 64
        stale_error, _ = bridge._retry_semantic_change_error(
            previous, current, stale_record, contract_version="3.0", chunk=chunk,
        )
        self.assertIsNotNone(stale_error)

        incomplete_fingerprints = copy.deepcopy(chunk)
        incomplete_fingerprints["runtime_context"].pop("code_fingerprint_sha256")
        fingerprint_error, _ = bridge._retry_semantic_change_error(
            previous, current, records, contract_version="3.0", chunk=incomplete_fingerprints,
        )
        self.assertIsNotNone(fingerprint_error)

        mismatched_evidence = copy.deepcopy(chunk)
        mismatched_evidence["evidence_context"]["E1"]["id"] = "E2"
        evidence_error, _ = bridge._retry_semantic_change_error(
            previous, current, records, contract_version="3.0", chunk=mismatched_evidence,
        )
        self.assertIsNotNone(evidence_error)

        duplicate_evidence_ids = copy.deepcopy(chunk)
        duplicate_evidence_ids["clauses"][0]["evidence_ids"] = ["E1", "E1"]
        duplicate_evidence_error, _ = bridge._retry_semantic_change_error(
            previous, current, records, contract_version="3.0", chunk=duplicate_evidence_ids,
        )
        self.assertIsNotNone(duplicate_evidence_error)

        mismatched_span = copy.deepcopy(chunk)
        mismatched_span["clauses"][0]["source_span"]["text"] = "不匹配的来源切片"
        invalid_spans = [mismatched_span]
        bool_offset = copy.deepcopy(chunk)
        bool_offset["clauses"][0]["source_span"]["start_offset"] = True
        invalid_spans.append(bool_offset)
        out_of_bounds = copy.deepcopy(chunk)
        out_of_bounds["clauses"][0]["source_span"]["end_offset"] = len(source) + 1
        invalid_spans.append(out_of_bounds)
        wrong_source_hash = copy.deepcopy(chunk)
        wrong_source_hash["clauses"][0]["source_span"]["source_sha256"] = "0" * 64
        invalid_spans.append(wrong_source_hash)
        unlinked_span_evidence = copy.deepcopy(chunk)
        unlinked_span_evidence["clauses"][0]["source_span"]["evidence_id"] = "E2"
        invalid_spans.append(unlinked_span_evidence)
        for invalid_chunk in invalid_spans:
            with self.subTest(source_span=invalid_chunk["clauses"][0]["source_span"]):
                span_error, _ = bridge._retry_semantic_change_error(
                    previous, current, records, contract_version="3.0", chunk=invalid_chunk,
                )
                self.assertIsNotNone(span_error)

    def test_retry_projection_keeps_only_exact_validator_targeted_inventory_fields(self) -> None:
        parent = {
            "contract_version": "3.0", "requirements": [], "unsupported_items": [],
            "clause_reviews": [
                {"clause_id": "C00037", "classification": "external_compliance",
                 "normative_basis": "external_duty", "reason": "parent reason", "obligations": None},
                {"clause_id": "C00040", "classification": "external_compliance",
                 "normative_basis": "external_duty", "reason": "preserve this reason",
                 "obligations": [{"id": "affirm", "status": "unverifiable", "reason": "source duty"}]},
            ],
        }
        model_retry = copy.deepcopy(parent)
        model_retry["clause_reviews"][0]["obligations"] = [
            {"id": "signature", "status": "unverifiable", "reason": "requires a signature"},
        ]
        model_retry["clause_reviews"][1]["reason"] = "unrequested semantic rewrite"
        records = [{
            "code": "executable_review_obligations_missing",
            "json_pointer": "$.clause_reviews[0].obligations",
            "clause_id": "C00037",
            "response_sha256": bridge._response_sha256(parent),
            "raw_error": "review_requires_non_empty_source_inventory",
        }]

        projected, audit = bridge._project_validator_targeted_obligation_fields(
            parent, model_retry, records,
        )
        self.assertIsNotNone(projected)
        self.assertEqual(projected["clause_reviews"][0]["obligations"],
                         model_retry["clause_reviews"][0]["obligations"])
        self.assertEqual(projected["clause_reviews"][1]["reason"], "preserve this reason")
        self.assertEqual(audit["status"], "projected")
        self.assertEqual(audit["applied_paths"], ["$.clause_reviews[0].obligations"])
        self.assertEqual(audit["discarded_unrequested_paths"], ["$.clause_reviews[1].reason"])

        stale_record = copy.deepcopy(records)
        stale_record[0]["response_sha256"] = "0" * 64
        broad_record = copy.deepcopy(records)
        broad_record[0]["json_pointer"] = "$.clause_reviews"
        wrong_clause_record = copy.deepcopy(records)
        wrong_clause_record[0]["clause_id"] = "C00040"
        reordered = copy.deepcopy(model_retry)
        reordered["clause_reviews"].reverse()
        not_a_completion = copy.deepcopy(model_retry)
        not_a_completion["clause_reviews"][0]["obligations"] = None
        for candidate, candidate_records in (
            (model_retry, stale_record), (model_retry, broad_record),
            (model_retry, wrong_clause_record), (reordered, records),
            (not_a_completion, records),
        ):
            with self.subTest(records=candidate_records, candidate=candidate):
                rejected, rejected_audit = bridge._project_validator_targeted_obligation_fields(
                    parent, candidate, candidate_records,
                )
                self.assertIsNone(rejected)
                self.assertEqual(rejected_audit["status"], "blocked")

    def test_v3_inventory_completion_is_revalidated_and_independently_reviewed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir = Path(td) / "requirements"
            review_dir.mkdir(parents=True)
            source = "论文作者须在声明页亲笔签名并填写日期。"
            unrelated_source = "本人确认论文相关信息真实有效。"
            clauses = [{
                "id": "C00037", "text": source, "evidence_ids": ["E1"],
                "source_kind": "paragraph", "location": {}, "part_index": 0,
            }, {
                "id": "C00040", "text": unrelated_source, "evidence_ids": ["E2"],
                "source_kind": "paragraph", "location": {}, "part_index": 0,
            }]
            evidence = {"evidence": [
                {"id": "E1", "text": source, "kind": "paragraph"},
                {"id": "E2", "text": unrelated_source, "kind": "paragraph"},
            ]}
            clauses = self._bind_test_source_spans(clauses, evidence)
            request = engine.build_llm_request(
                [], clauses, evidence, {}, "full", contract_version="3.0",
                runtime_context={"code_fingerprint_sha256": "f" * 64},
            )
            request = attach_request_provenance(
                request, source_sha256="a" * 64, evidence_doc=evidence,
                clauses=clauses, run_id="run-inventory-bridge-test",
            )
            engine.prepare_host_agent_review_packets(
                request, clauses, evidence, "a" * 64, review_dir, chunk_size=2,
            )
            first = {
                "contract_version": "3.0", "requirements": [],
                "clause_reviews": [
                    {
                        "clause_id": "C00037", "classification": "external_compliance",
                        "normative_basis": "external_duty",
                        "reason": "The author must sign and date the declaration.",
                        "obligations": None,
                    },
                    {
                        "clause_id": "C00040", "classification": "external_compliance",
                        "normative_basis": "external_duty",
                        "reason": "The author affirms the accuracy of the information.",
                        "obligations": [{
                            "id": "affirm_accuracy", "status": "unverifiable",
                            "reason": "The author must personally affirm the statement.",
                        }],
                    },
                ],
                "unsupported_items": [], "reported_conflicts": [],
            }
            second = copy.deepcopy(first)
            second["clause_reviews"][0]["obligations"] = [{
                "id": "actual_signature_and_date", "status": "unverifiable",
                "reason": "An actual signature and date cannot be generated by the formatter.",
            }]
            second["clause_reviews"][1]["reason"] = (
                "Unrelated rewrite that was not named by the validator."
            )
            envelopes = [
                {"runId": "inventory-retry-1", "status": "ok", "provider": "openai",
                 "model": "gpt-5.6-luna", "result": {"payloads": [{"text": json.dumps(first)}]}},
                {"runId": "inventory-retry-2", "status": "ok", "provider": "openai",
                 "model": "gpt-5.6-luna", "result": {"payloads": [{"text": json.dumps(second)}]}},
            ]
            host_results = [
                subprocess.CompletedProcess(["openclaw"], 0, json.dumps(item), "")
                for item in envelopes
            ]
            independent_candidates: list[dict] = []

            def review_candidate(candidate: dict, chunk: dict, **kwargs: dict) -> dict:
                independent_candidates.append(copy.deepcopy(candidate))
                return self._fake_independent_review(candidate, chunk, **kwargs)

            with patch.object(bridge, "_run_independent_obligation_coverage_review",
                              side_effect=review_candidate), \
                    patch.object(bridge, "_run_command", side_effect=host_results) as host_call:
                audit = bridge.run_bridge(
                    review_dir, response_out=Path(td) / "host-agent-response.json",
                    agent_id="main", timeout=1, max_attempts=2,
                    openclaw_bin="openclaw", model="openai/gpt-5.6-luna",
                )

            self.assertEqual(host_call.call_count, 2)
            self.assertEqual(audit["status"], "merged")
            self.assertEqual(len(independent_candidates), 1)
            accepted_reviews = independent_candidates[0]["clause_reviews"]
            accepted_review = accepted_reviews[0]
            self.assertEqual(accepted_review["classification"], "external_compliance")
            self.assertEqual(accepted_review["obligations"], second["clause_reviews"][0]["obligations"])
            self.assertEqual(
                accepted_reviews[1]["reason"], first["clause_reviews"][1]["reason"],
                "unrequested model drift must not enter the validated candidate",
            )
            retry_comparison = audit["chunk_runs"][0]["retry_stage_comparison"]
            targeted_projection = retry_comparison["validator_targeted_field_projection"]
            self.assertEqual(targeted_projection["status"], "projected")
            self.assertEqual(
                targeted_projection["applied_paths"],
                ["$.clause_reviews[0].obligations"],
            )
            self.assertIn(
                "$.clause_reviews[1].reason",
                targeted_projection["discarded_unrequested_paths"],
            )
            self.assertEqual(
                retry_comparison["raw_semantic_changed_paths"],
                [
                    "$.clause_reviews[0].obligations", "$.clause_reviews[1].reason",
                ],
            )
            self.assertEqual(
                retry_comparison["authorized_projected_changed_paths"],
                ["$.clause_reviews[0].obligations"],
            )
            raw_retry = json.loads(
                Path(audit["chunk_runs"][0]["raw_response_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(
                raw_retry["clause_reviews"][1]["reason"],
                second["clause_reviews"][1]["reason"],
                "the raw provider response must remain available as unmodified evidence",
            )
            self.assertEqual(raw_retry["requirements"], second["requirements"])
            self.assertEqual(independent_candidates[0]["requirements"], [])
            self.assertEqual(
                audit["chunk_runs"][0]["semantic_retry_authorizations"]["paths"][0]["rule_id"],
                "v3_source_inventory_completion",
            )

    def test_retry_stage_pair_compares_raw_to_raw_and_cannot_hide_raw_drift(self) -> None:
        raw = {
            "contract_version": bridge.HOST_REVIEW_CONTRACT_V3,
            "requirements": [],
            "clause_reviews": [{
                "clause_id": "C00066", "classification": "executable",
                "reason": "model-authored source summary",
                "obligations": [{"id": "quality", "status": "covered"}],
            }],
            "unsupported_items": [],
        }
        projected = copy.deepcopy(raw)
        projected["clause_reviews"][0]["reason"] = "code-owned complete abstract projection"
        projected["clause_reviews"][0]["obligations"] = [
            {"id": f"abstract_zh.quality_guidance:{index}", "status": "covered"}
            for index in range(5)
        ]
        with tempfile.TemporaryDirectory() as td:
            parent_raw_path = Path(td) / "attempt-01.raw.json"
            current_raw_path = Path(td) / "attempt-02.raw.json"
            parent_raw_path.write_text(json.dumps(raw), encoding="utf-8")
            current_raw_path.write_text(json.dumps(raw), encoding="utf-8")
            parent_loaded, current_loaded = bridge._load_normalized_retry_raw_pair(
                parent_raw_path, current_raw_path, {},
            )
        error, changed = bridge._retry_semantic_change_error(
            parent_loaded, current_loaded, [],
            contract_version=bridge.HOST_REVIEW_CONTRACT_V3,
        )
        self.assertIsNone(error)
        self.assertEqual(changed, [])
        # The old cross-stage pairing would report the code projection as
        # model drift; a real raw change must still fail even if projection
        # later overwrites that field to the same candidate value.
        self.assertTrue(bridge._retry_change_paths(raw, projected))
        changed_raw = copy.deepcopy(raw)
        changed_raw["clause_reviews"][0]["reason"] = "different model assertion"
        error, changed = bridge._retry_semantic_change_error(
            raw, changed_raw, [],
            contract_version=bridge.HOST_REVIEW_CONTRACT_V3,
        )
        self.assertIsNotNone(error)
        self.assertIn("$.clause_reviews[0].reason", changed)

    def test_retry_parent_prompt_rejects_a_hash_for_a_different_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            parent_path = Path(td) / "parent.raw.json"
            parent_path.write_text('{"requirements":[]}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "path/hash binding mismatch"):
                bridge._host_prompt(
                    request_path=Path(td) / "request.json",
                    chunk_path=Path(td) / "chunk.json",
                    response_path=Path(td) / "candidate.json",
                    run_id="run-hash", chunk_index=1, chunk_count=1, attempt=2,
                    retry_hint="test",
                    retry_parent_response_sha256="0" * 64,
                    retry_parent_response_path=parent_path,
                )

    def test_no_progress_requires_same_raw_plan_candidate_and_complete_fingerprints(self) -> None:
        raw = {"clause_reviews": [{"clause_id": "C1", "reason": "same"}]}
        errors = [{"code": "contract_validation_error", "json_pointer": "$.x"}]
        fingerprints = {
            "run_id": "run-retry-test",
            "case_id": "case-retry-test",
            "source_sha256": "a" * 64,
            "clause_sha256": "b" * 64,
            "evidence_sha256": "c" * 64,
            "request_sha256": "d" * 64,
            "chunk_index": 1,
            "chunk_sha256": "9" * 64,
            "schema_sha256": "e" * 64,
            "code_fingerprint_sha256": "f" * 64,
        }
        self.assertTrue(bridge._retry_has_no_progress(
            raw, copy.deepcopy(raw), errors, copy.deepcopy(errors),
            "candidate-sha", "candidate-sha", fingerprints, copy.deepcopy(fingerprints),
        ))
        incomplete_fingerprints = dict(fingerprints)
        incomplete_fingerprints.pop("code_fingerprint_sha256")
        self.assertFalse(bridge._retry_has_no_progress(
            raw, copy.deepcopy(raw), errors, copy.deepcopy(errors),
            "candidate-sha", "candidate-sha", incomplete_fingerprints,
            copy.deepcopy(incomplete_fingerprints),
        ))
        self.assertFalse(bridge._retry_has_no_progress(
            raw, copy.deepcopy(raw), errors, copy.deepcopy(errors),
            "parent-candidate", "new-candidate", fingerprints, copy.deepcopy(fingerprints),
        ))
        for changed_key, changed_value in (
            ("run_id", "another-run"),
            ("case_id", "another-case"),
            ("chunk_index", 2),
            ("chunk_sha256", "8" * 64),
            ("schema_sha256", "7" * 64),
            ("code_fingerprint_sha256", "6" * 64),
        ):
            with self.subTest(changed_key=changed_key):
                changed_fingerprints = {**fingerprints, changed_key: changed_value}
                self.assertFalse(bridge._retry_has_no_progress(
                    raw, copy.deepcopy(raw), errors, copy.deepcopy(errors),
                    "candidate-sha", "candidate-sha", fingerprints, changed_fingerprints,
                ))

    def test_authoring_primary_retry_eligibility_requires_exact_cited_source(self) -> None:
        source = "以下示例内容请作者根据需要自行撰写真实研究内容。"
        evidence = {"E1": {"id": "E1", "text": source}}
        clause = {
            "id": "C1", "text": "规范化后的语义片段", "evidence_ids": ["E1"],
            "source_span": {
                "evidence_id": "E1", "start_offset": 0, "end_offset": len(source),
                "text": source, "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            },
        }
        review_context = {
            "classification": "informational", "requires_requirement": False,
            "linked_requirements": [], "cited_evidence": evidence,
        }
        missing = [{
            "disposition": "unrepresented", "source_quote": source,
        }]
        self.assertTrue(bridge._v3_authoring_content_retry_is_source_bound(
            review_context, clause, evidence, missing,
        ))

        clause_with_context = copy.deepcopy(clause)
        clause_with_context["evidence_ids"] = ["E1", "E2"]
        evidence_with_context = {
            **evidence,
            "E2": {"id": "E2", "text": "相邻说明，不包含作者撰写指令。"},
        }
        context_review = {
            **review_context,
            "cited_evidence": evidence_with_context,
        }
        self.assertTrue(bridge._v3_authoring_content_retry_is_source_bound(
            context_review, clause_with_context, evidence_with_context, missing,
        ))

        linked = {**review_context, "linked_requirements": [{"requirement_ref": "RR1"}]}
        self.assertFalse(bridge._v3_authoring_content_retry_is_source_bound(
            linked, clause, evidence, missing,
        ))
        bad_quote = [{**missing[0], "source_quote": "图题应居中"}]
        self.assertFalse(bridge._v3_authoring_content_retry_is_source_bound(
            review_context, clause, evidence, bad_quote,
        ))
        stale_span_clause = copy.deepcopy(clause)
        stale_span_clause["source_span"]["text"] = "以下示例内容"
        self.assertFalse(bridge._v3_authoring_content_retry_is_source_bound(
            review_context, stale_span_clause, evidence, missing,
        ))

    def test_informational_authoring_omission_makes_bounded_primary_retryable(self) -> None:
        self._independent_review_patch.stop()
        source = "以下示例内容请作者根据需要自行撰写真实研究内容。"
        provenance = {
            "run_id": "run-authoring-primary-retry", "source_sha256": "a" * 64,
            "evidence_sha256": "b" * 64, "clause_sha256": "c" * 64,
            "request_sha256": "d" * 64,
        }
        chunk = {
            "provenance": provenance,
            "clauses": [{
                "id": "C1", "text": "规范化后的语义片段", "evidence_ids": ["E1"],
                "source_span": {
                    "evidence_id": "E1", "start_offset": 0, "end_offset": len(source),
                    "text": source,
                    "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                },
            }],
            "evidence_context": {"E1": {"id": "E1", "text": source}},
        }
        response = {
            "provenance": provenance,
            "clause_reviews": [{
                "clause_id": "C1", "classification": "informational",
                "reason": "The sample was mistaken for descriptive text.",
            }],
            "requirements": [],
        }
        incomplete = {
            "protocol": bridge.OBLIGATION_COVERAGE_PROTOCOL,
            "status": "completed", "response_sha256": "f" * 64,
            "results": [{
                "check_id": "C1", "verdict": "incomplete",
                "rationale": "The author-input obligation is unrepresented.",
                "evidence_quotes": [source], "machine_obligation_ids": [],
                "identified_obligations": [{
                    "source_quote": source, "disposition": "unrepresented",
                    "obligation_summary": "The author must supply genuine thesis content.",
                    "requirement_refs": [],
                }],
            }],
            "summary": {"consistent": 0, "incomplete": 1, "uncertain": 0},
        }
        calls = []

        def reject_both_attempts(request: dict, **kwargs: dict) -> dict:
            calls.append(copy.deepcopy(request))
            return bind_mock_review_to_source_spans(
                incomplete, request, kwargs["output_dir"],
            )

        with tempfile.TemporaryDirectory() as td:
            with patch.object(bridge, "run_native_semantic_review", side_effect=reject_both_attempts), \
                    patch.object(bridge.time, "sleep"):
                with self.assertRaises(bridge.IndependentObligationReviewError) as caught:
                    bridge._run_independent_obligation_coverage_review(
                        response, chunk, review_dir=Path(td),
                        run_id="run-authoring-primary-retry", chunk_index=1, attempt=1,
                        host_runtime="codex", model="gpt-5.6-luna", timeout=5,
                        agent_id="main", runner="exec", binary="codex", config_path=None,
                        controller=bridge.RunController(),
                    )
        self.assertEqual([item["provider_attempt"] for item in calls], [1, 2])
        self.assertTrue(caught.exception.retryable)
        error_record = caught.exception.error_records[0]
        self.assertEqual(
            error_record["primary_retry_authorization"],
            "source_bound_authoring_content_reclassification_v1",
        )

    def test_author_content_reclassification_retry_is_exactly_source_bound(self) -> None:
        source = "以下示例内容是编写的，请作者根据需要自行撰写真实研究内容。"
        provenance = {
            "run_id": "run-authoring-retry", "source_sha256": "a" * 64,
            "evidence_sha256": "b" * 64, "clause_sha256": "c" * 64,
            "request_sha256": "d" * 64,
        }
        parent = {
            "contract_version": "3.0",
            "requirements": [],
            "clause_reviews": [{
                "clause_id": "C1", "classification": "informational",
                "reason": "Sample wording was treated as descriptive.",
            }],
        }
        candidate = copy.deepcopy(parent)
        candidate["clause_reviews"][0]["classification"] = "requires_source_content"
        chunk = {
            "provenance": provenance, "batch": {"index": 1},
            "case_id": "case-authoring-retry",
            "response_schema": {"type": "object"},
            "runtime_context": {"code_fingerprint_sha256": "9" * 64},
            "clauses": [exact_source_clause("C1", source)],
            "evidence_context": {"E1": {"id": "E1", "text": source}},
        }
        record = {
            "code": "independent_obligation_review_incomplete", "clause_id": "C1",
            "json_pointer": "$.clause_reviews[0].classification",
            "baseline_classification": "informational", "evidence_ids": ["E1"],
            "missing_source_quotes": ["请作者根据需要自行撰写真实研究内容"],
            "candidate_response_sha256": bridge._response_sha256(
                bridge._bind_current_invocation_provenance(parent, provenance)
            ),
            "candidate_semantic_sha256": bridge._response_sha256(bridge._semantic_retry_view(parent)),
            "review_request_sha256": "e" * 64, "review_response_sha256": "f" * 64,
        }
        with patch.object(bridge, "validate_host_agent_response", return_value=[]):
            repaired, repair_audit = bridge._v3_authoring_content_reclassification_response(
                parent, candidate, [record], chunk=chunk,
            )
            changed_paths = bridge._retry_change_paths(parent, candidate)
            change_error, authorized_paths = bridge._retry_semantic_change_error(
                parent, candidate, [record], contract_version="3.0", chunk=chunk,
            )
            authorization_ledger = bridge._retry_authorization_ledger(
                parent, candidate, [record], changed_paths,
                contract_version="3.0", chunk=chunk,
            )

        self.assertEqual(repaired, candidate)
        self.assertEqual(repair_audit["affected_clause_ids"], ["C1"])
        self.assertEqual(changed_paths, ["$.clause_reviews[0].classification"])
        self.assertIsNone(change_error)
        self.assertEqual(authorized_paths, changed_paths)
        self.assertTrue(authorization_ledger[0]["source_binding_complete"])

        multi_evidence_chunk = copy.deepcopy(chunk)
        multi_evidence_chunk["clauses"][0]["evidence_ids"] = ["E1", "E2"]
        multi_evidence_chunk["evidence_context"]["E2"] = {
            "id": "E2", "text": "相邻上下文不重复作者撰写指令。",
        }
        multi_evidence_record = {**record, "evidence_ids": ["E1", "E2"]}
        with patch.object(bridge, "validate_host_agent_response", return_value=[]):
            multi_repaired, _ = bridge._v3_authoring_content_reclassification_response(
                parent, candidate, [multi_evidence_record], chunk=multi_evidence_chunk,
            )
        self.assertEqual(multi_repaired, candidate)

        changed_reason = copy.deepcopy(candidate)
        changed_reason["clause_reviews"][0]["reason"] = "Unrelated rewrite"
        with patch.object(bridge, "validate_host_agent_response", return_value=[]):
            self.assertIsNone(bridge._v3_authoring_content_reclassification_response(
                parent, changed_reason, [record], chunk=chunk,
            )[0])

        forged_record = {**record, "candidate_response_sha256": "0" * 64}
        with patch.object(bridge, "validate_host_agent_response", return_value=[]):
            self.assertIsNone(bridge._v3_authoring_content_reclassification_response(
                parent, candidate, [forged_record], chunk=chunk,
            )[0])

        unrelated_source = "图题应居中，编号按章节递增。"
        unrelated_chunk = copy.deepcopy(chunk)
        unrelated_chunk["clauses"][0]["text"] = unrelated_source
        unrelated_chunk["evidence_context"]["E1"]["text"] = unrelated_source
        unrelated_parent = copy.deepcopy(parent)
        unrelated_candidate = copy.deepcopy(candidate)
        unrelated_record = {
            **record,
            "missing_source_quotes": [unrelated_source],
            "candidate_response_sha256": bridge._response_sha256(
                bridge._bind_current_invocation_provenance(unrelated_parent, provenance)
            ),
            "candidate_semantic_sha256": bridge._response_sha256(
                bridge._semantic_retry_view(unrelated_parent)
            ),
        }
        with patch.object(bridge, "validate_host_agent_response", return_value=[]):
            self.assertIsNone(bridge._v3_authoring_content_reclassification_response(
                unrelated_parent, unrelated_candidate, [unrelated_record], chunk=unrelated_chunk,
            )[0])

    def test_retry_allows_only_completion_of_an_empty_requirement_payload(self) -> None:
        previous = self._executable_response({"provenance": {}})
        previous["requirements"][0]["properties"] = {}
        current = self._executable_response({"provenance": {}})
        records = [{
            "code": "empty_requirement_properties",
            "json_pointer": "$.requirements[0].properties",
        }]
        self.assertTrue(
            bridge._retry_changes_allowed(
                records,
                ["$.requirements[0].properties.font"],
                contract_version="2.1",
                previous_response=previous,
                current_response=current,
            )
        )
        current["requirements"][0]["properties"]["font"]["size_pt"] = 11
        self.assertFalse(
            bridge._retry_changes_allowed(
                records,
                ["$.requirements[0].properties.font.size_pt"],
                contract_version="2.1",
                previous_response=current,
                current_response=current,
            )
        )

    def test_empty_payload_fill_may_rewrite_only_the_requirement_reason(self) -> None:
        previous = self._executable_response({"provenance": {}})
        previous["requirements"][0]["properties"] = {}
        previous["requirements"][0]["reason"] = "首轮解释"
        current = self._executable_response({"provenance": {}})
        current["requirements"][0]["reason"] = "重试后的解释"
        records = [{
            "code": "empty_requirement_properties",
            "json_pointer": "$.requirements[0].properties",
        }]
        changed = bridge._retry_change_paths(previous, current)
        self.assertEqual(
            changed,
            ["$.requirements[0].properties.font", "$.requirements[0].reason"],
        )
        self.assertTrue(bridge._retry_changes_allowed(
            records,
            changed,
            contract_version="2.1",
            previous_response=previous,
            current_response=current,
        ))
        current["requirements"][0]["clause_ids"] = ["C999"]
        changed = bridge._retry_change_paths(previous, current)
        self.assertFalse(bridge._retry_changes_allowed(
            records,
            changed,
            contract_version="2.1",
            previous_response=previous,
            current_response=current,
        ))

    def test_retry_allows_empty_payload_fill_and_validator_named_property_removal(self) -> None:
        previous = {
            "contract_version": "3.0",
            "requirements": [
                {
                    "role": "equations", "properties": {},
                    "clause_ids": ["C1"], "evidence_ids": ["E1"],
                },
                {
                    "role": "equation", "properties": {"style": "three_line"},
                    "clause_ids": ["C2"], "evidence_ids": ["E2"],
                },
            ],
            "clause_reviews": [
                {"clause_id": "C1", "classification": "executable"},
                {"clause_id": "C2", "classification": "executable"},
            ],
            "unsupported_items": [],
        }
        current = json.loads(json.dumps(previous))
        current["requirements"][0]["properties"] = {"same_line": False}
        current["requirements"][1]["properties"] = {}
        records = [
            {"code": "empty_requirement_properties", "json_pointer": "$.requirements[0].properties"},
            {
                "code": "unknown_property",
                "json_pointer": "$.requirements[1].properties",
                "raw_error": "unknown property 'style'",
            },
        ]
        changed = bridge._retry_change_paths(previous, current)
        self.assertEqual(
            changed,
            ["$.requirements[0].properties.same_line", "$.requirements[1].properties.style"],
        )
        self.assertTrue(bridge._retry_changes_allowed(
            records, changed, contract_version="3.0",
            previous_response=previous, current_response=current,
        ))

    def test_retry_allows_only_exact_fixed_text_repair(self) -> None:
        previous = {
            "requirements": [
                {
                    "role": "abstract_title_zh", "properties": {"heading": "wrong"},
                    "clause_ids": ["C1"], "evidence_ids": ["E1"],
                },
                {
                    "role": "abstract_title_zh", "properties": {"heading": "wrong other"},
                    "clause_ids": ["C2"], "evidence_ids": ["E2"],
                },
            ],
            "clause_reviews": [],
        }
        current = json.loads(json.dumps(previous))
        current["requirements"][0]["properties"]["heading"] = "Exact cited text"
        records = [{
            "code": "fixed_text_evidence_mismatch",
            "json_pointer": "$.requirements[0].properties.heading",
        }]
        chunk = {"evidence_context": {"E1": {"text": "Exact cited text"}}}
        self.assertTrue(
            bridge._retry_changes_allowed(
                records,
                ["$.requirements[0].properties.heading"],
                contract_version="2.1",
                previous_response=previous,
                current_response=current,
                chunk=chunk,
            )
        )
        current["requirements"][1]["properties"]["heading"] = "unrelated change"
        self.assertFalse(
            bridge._retry_changes_allowed(
                records,
                ["$.requirements[0].properties.heading", "$.requirements[1].properties.heading"],
                contract_version="2.1",
                previous_response=previous,
                current_response=current,
                chunk=chunk,
            )
        )
        self.assertFalse(
            bridge._retry_changes_allowed(
                records,
                ["$.clause_reviews[0].classification"],
                contract_version="2.1",
                previous_response=previous,
                current_response=current,
                chunk=chunk,
            )
        )

    def test_cover_binding_retry_requires_schema_directed_proof(self) -> None:
        previous = {
            "contract_version": "3.0",
            "requirements": [{
                "role": "cover",
                "properties": {
                    "institution": "——",
                    "fields": [{"id": "security_marking", "label": "密级"}],
                },
                "clause_ids": ["C1"],
                "evidence_ids": ["E1"],
            }],
            "clause_reviews": [{
                "clause_id": "C1", "classification": "executable",
                "normative_basis": "template_structure",
            }],
            "unsupported_items": [],
        }
        current = json.loads(json.dumps(previous))
        current["requirements"][0]["properties"]["fields"] = []
        current["requirements"][0]["properties"]["non_public_administration"] = {
            "fields": [{"id": "security_marking", "label": "密级"}],
        }
        current["clause_reviews"][0]["reason"] = "已将密级字段迁移到行政区域。"
        records = [{
            "code": "cover_binding_violation",
            "json_pointer": "$.requirements[0].properties.fields[0]",
        }]
        changed = bridge._retry_change_paths(previous, current)
        self.assertTrue(changed)
        self.assertFalse(
            bridge._retry_changes_allowed(
                records, changed, contract_version="3.0",
                previous_response=previous, current_response=current,
            )
        )

        current["clause_reviews"][0]["classification"] = "not_applicable"
        changed = bridge._retry_change_paths(previous, current)
        self.assertFalse(
            bridge._retry_changes_allowed(
                records, changed, contract_version="3.0",
                previous_response=previous, current_response=current,
            )
        )

    def test_v3_retry_allows_only_new_requirement_for_missing_executable_clause(self) -> None:
        previous = {
            "contract_version": "3.0",
            "requirements": [{
                "role": "cover_field_label", "properties": {"text": "标题"},
                "clause_ids": ["C1"], "evidence_ids": ["E1"],
            }],
            "clause_reviews": [
                {"clause_id": "C1", "classification": "executable"},
                {"clause_id": "C2", "classification": "executable"},
            ],
            "unsupported_items": [],
        }
        current = json.loads(json.dumps(previous))
        current["requirements"].append({
            "role": "declarations", "properties": {
                "before_role": "abstract_title_zh", "items": [{
                    "id": "authorization", "heading": "授权书",
                    "body_parts": ["固定正文"], "source_evidence_ids": ["E2"],
                    "signature_placeholders": [],
                }],
            }, "clause_ids": ["C2"], "evidence_ids": ["E2"],
        })
        records = [{
            "code": "requirement_relation_mismatch",
            "json_pointer": "$.clause_reviews[1]",
        }]
        changed = bridge._retry_change_paths(previous, current)
        self.assertEqual(changed, ["$.requirements"])
        self.assertTrue(bridge._retry_changes_allowed(
            records, changed, contract_version="3.0",
            previous_response=previous, current_response=current,
        ))

        current["requirements"][0]["properties"]["text"] = "改过的旧要求"
        changed = bridge._retry_change_paths(previous, current)
        self.assertFalse(bridge._retry_changes_allowed(
            records, changed, contract_version="3.0",
            previous_response=previous, current_response=current,
        ))

    def test_v3_retry_allows_only_first_occurrence_evidence_deduplication(self) -> None:
        previous = {
            "contract_version": "3.0",
            "requirements": [{
                "role": "declarations", "properties": {"text": "授权书"},
                "clause_ids": ["C1"], "evidence_ids": ["E1", "E2", "E2"],
            }],
            "clause_reviews": [{"clause_id": "C1", "classification": "executable"}],
            "unsupported_items": [], "reported_conflicts": [],
        }
        current = json.loads(json.dumps(previous))
        current["requirements"][0]["evidence_ids"] = ["E1", "E2"]
        records = [{
            "code": "duplicate_evidence_ids",
            "json_pointer": "$.requirements[0].evidence_ids",
        }]
        changed = bridge._retry_change_paths(previous, current)
        self.assertEqual(changed, ["$.requirements"])
        self.assertTrue(bridge._retry_changes_allowed(
            records, changed, contract_version="3.0",
            previous_response=previous, current_response=current,
        ))

        current["requirements"][0]["evidence_ids"] = ["E2", "E1"]
        changed = bridge._retry_change_paths(previous, current)
        self.assertFalse(bridge._retry_changes_allowed(
            records, changed, contract_version="3.0",
            previous_response=previous, current_response=current,
        ))
        current = json.loads(json.dumps(previous))
        current["requirements"][0]["properties"]["text"] = "改动后的授权书"
        current["requirements"][0]["evidence_ids"] = ["E1", "E2"]
        changed = bridge._retry_change_paths(previous, current)
        self.assertFalse(bridge._retry_changes_allowed(
            records, changed, contract_version="3.0",
            previous_response=previous, current_response=current,
        ))

    def test_v3_relation_completion_restores_requirements_after_reason_rewrite(self) -> None:
        previous = {
            "contract_version": "3.0",
            "requirements": [
                {
                    "role": "heading_2",
                    "properties": {"text": "5.1 研究结论"},
                    "clause_ids": ["C1"],
                    "evidence_ids": ["E1"],
                    "confidence": 0.98,
                    "reason": "原始解释",
                },
            ],
            "clause_reviews": [
                {"clause_id": "C1", "classification": "executable"},
                {"clause_id": "C2", "classification": "executable"},
            ],
            "unsupported_items": [],
            "reported_conflicts": [],
        }
        current = json.loads(json.dumps(previous))
        current["requirements"][0]["reason"] = "重试后的同义解释"
        current["requirements"].append({
            "role": "heading_3",
            "properties": {"text": "5.1.1 研究结论"},
            "clause_ids": ["C2"],
            "evidence_ids": ["E2"],
            "confidence": 0.97,
            "reason": "新增 requirement 的解释",
        })
        records = [{
            "code": "missing_derived_requirement",
            "json_pointer": "$.clause_reviews[1]",
            "raw_error": "$.clause_reviews[1]: executable_review_requires_derived_requirement",
        }]
        with patch.object(bridge, "validate_host_agent_response", return_value=[]):
            repaired, audit = bridge._v3_relation_completion_response(
                previous, current, records, chunk={},
            )
        self.assertIsNotNone(repaired)
        self.assertEqual(repaired["requirements"][0]["reason"], "原始解释")
        self.assertEqual(
            [item["clause_ids"] for item in repaired["requirements"]],
            [["C1"], ["C2"]],
        )
        self.assertEqual(audit["preserved_requirement_count"], 1)
        self.assertEqual(audit["added_requirement_count"], 1)

        current["requirements"][0]["properties"]["text"] = "改过的旧 requirement"
        with patch.object(bridge, "validate_host_agent_response", return_value=[]):
            repaired, audit = bridge._v3_relation_completion_response(
                previous, current, records, chunk={},
            )
        self.assertIsNone(repaired)
        self.assertIsNone(audit)

    def test_v3_relation_completion_cannot_authorize_top_level_drift(self) -> None:
        previous = {
            "contract_version": "3.0",
            "requirements": [{
                "role": "heading_2", "properties": {"text": "5.1 结论"},
                "clause_ids": ["C1"], "evidence_ids": ["E1"],
                "confidence": 0.9, "reason": "已有要求",
            }],
            "clause_reviews": [
                {"clause_id": "C1", "classification": "executable"},
                {"clause_id": "C2", "classification": "executable"},
            ],
            "unsupported_items": [],
            "reported_conflicts": [],
        }
        current = json.loads(json.dumps(previous))
        current["requirements"].append({
            "role": "heading_3", "properties": {"text": "5.1.1 结论"},
            "clause_ids": ["C2"], "evidence_ids": ["E2"],
            "confidence": 0.9, "reason": "新增要求",
        })
        records = [{
            "code": "missing_derived_requirement",
            "json_pointer": "$.clause_reviews[1]",
            "raw_error": "$.clause_reviews[1]: executable_review_requires_derived_requirement",
        }]
        with patch.object(bridge, "validate_host_agent_response", return_value=[]):
            repaired, _audit = bridge._v3_relation_completion_response(
                previous, current, records, chunk={},
            )
            self.assertIsNotNone(repaired)

            wrong_contract = json.loads(json.dumps(current))
            wrong_contract["contract_version"] = "2.1"
            repaired, audit = bridge._v3_relation_completion_response(
                previous, wrong_contract, records, chunk={},
            )
            self.assertIsNone(repaired)
            self.assertIsNone(audit)
            error, changed = bridge._retry_semantic_change_error(
                previous, wrong_contract, records,
                contract_version="3.0", chunk={},
            )
            self.assertIsNotNone(error)
            self.assertIn("$.contract_version", changed)

            extra_field = json.loads(json.dumps(current))
            extra_field["unrecognized_semantic_field"] = {"status": "changed"}
            repaired, audit = bridge._v3_relation_completion_response(
                previous, extra_field, records, chunk={},
            )
            self.assertIsNone(repaired)
            self.assertIsNone(audit)
            error, changed = bridge._retry_semantic_change_error(
                previous, extra_field, records,
                contract_version="3.0", chunk={},
            )
            self.assertIsNotNone(error)
            self.assertIn("$.top_level.unrecognized_semantic_field", changed)

            authorization: list[dict] = []
            bound_chunk = {
                "provenance": {
                    "run_id": "run-retry-test", "source_sha256": "a" * 64,
                    "evidence_sha256": "b" * 64, "clause_sha256": "c" * 64,
                    "request_sha256": "d" * 64,
                },
                "case_id": "case-retry-test",
                "batch": {"index": 1},
                "response_schema": {"type": "object"},
                "runtime_context": {"code_fingerprint_sha256": "9" * 64},
                "clauses": [
                    {"id": "C1", "text": "原条款一", "evidence_ids": ["E1"]},
                    {"id": "C2", "text": "原条款二", "evidence_ids": ["E2"]},
                ],
                "evidence_context": {
                    "E1": {"id": "E1", "text": "原条款一"},
                    "E2": {"id": "E2", "text": "原条款二"},
                },
            }
            error, changed = bridge._retry_semantic_change_error(
                previous, current, records,
                contract_version="3.0", chunk=bound_chunk,
                authorization_out=authorization,
            )
            self.assertIsNone(error)
            self.assertEqual(changed, ["$.requirements"])
            self.assertEqual(len(authorization), 1)
            entry = authorization[0]
            self.assertEqual(entry["authorization_type"], "named_response_rule")
            self.assertEqual(entry["old_value_sha256"], bridge._response_sha256(previous["requirements"]))
            self.assertEqual(entry["new_value_sha256"], bridge._response_sha256(current["requirements"]))
            self.assertFalse(entry["validator_records"])
            self.assertTrue(entry["source_binding_complete"])
            self.assertEqual(entry["source_binding"]["run_id"], "run-retry-test")
            self.assertEqual(
                {item["clause_id"] for item in entry["source_evidence_bindings"]},
                {"C1", "C2"},
            )
            self.assertEqual(entry["transform"]["kind"], "constrained_model_retry")
            self.assertTrue(entry["error_bundle_sha256"])
            self.assertEqual(
                entry["response_rule_evidence"][0]["code"],
                "missing_derived_requirement",
            )

    def test_retry_reorder_cannot_transfer_a_field_repair_grant(self) -> None:
        previous = {
            "contract_version": "3.0",
            "requirements": [
                {"role": "heading_1", "properties": {"text": "一"},
                 "clause_ids": ["C1"], "evidence_ids": ["E1"], "reason": "原文一"},
                {"role": "heading_2", "properties": {"text": "二"},
                 "clause_ids": ["C2"], "evidence_ids": ["E2"], "reason": "原文二"},
            ],
            "clause_reviews": [], "unsupported_items": [], "reported_conflicts": [],
        }
        reordered = json.loads(json.dumps(previous))
        reordered["requirements"].reverse()
        error, changed = bridge._retry_semantic_change_error(
            previous, reordered, [{"code": "test_only"}], contract_version="3.0",
        )
        self.assertIsNone(error)
        self.assertEqual(changed, [])

        reordered["requirements"][0]["reason"] = "夹带的语义变化"
        error, changed = bridge._retry_semantic_change_error(
            previous, reordered, [{"code": "contract_validation_error"}],
            contract_version="3.0",
        )
        self.assertIsNotNone(error)
        self.assertTrue(changed)

    def test_invalid_normative_basis_does_not_authorize_neighboring_review_change(self) -> None:
        previous = {
            "contract_version": "3.0", "requirements": [], "unsupported_items": [],
            "reported_conflicts": [],
            "clause_reviews": [
                {"clause_id": "C1", "classification": "executable",
                 "normative_basis": "informational", "reason": "条款一"},
                {"clause_id": "C2", "classification": "executable",
                 "normative_basis": "explicit_normative_text", "reason": "条款二"},
            ],
        }
        current = copy.deepcopy(previous)
        current["clause_reviews"][0]["normative_basis"] = "explicit_normative_text"
        # This second change is legal according to the enum, but no validator
        # finding authorizes changing C2.
        current["clause_reviews"][1]["normative_basis"] = "template_structure"
        records = [{
            "code": "normative_basis_invalid",
            "json_pointer": "$.clause_reviews[0].normative_basis",
            "clause_id": "C1",
        }]
        error, changed = bridge._retry_semantic_change_error(
            previous, current, records, contract_version="3.0",
        )
        self.assertIsNotNone(error)
        self.assertIn("$.clause_reviews[1].normative_basis", changed)

    def test_clause_review_reorder_or_duplicate_identity_cannot_transfer_grant(self) -> None:
        previous = {
            "contract_version": "3.0", "requirements": [], "unsupported_items": [],
            "reported_conflicts": [],
            "clause_reviews": [
                {"clause_id": "C1", "classification": "executable", "reason": "一"},
                {"clause_id": "C2", "classification": "executable", "reason": "二"},
            ],
        }
        reordered = copy.deepcopy(previous)
        reordered["clause_reviews"].reverse()
        reordered["clause_reviews"][0]["reason"] = "夹带变化"
        error, changed = bridge._retry_semantic_change_error(
            previous, reordered, [{"code": "missing_clause_review"}],
            contract_version="3.0",
        )
        self.assertIsNotNone(error)
        self.assertTrue(changed)

        duplicated = copy.deepcopy(previous)
        duplicated["clause_reviews"][1]["clause_id"] = "C1"
        error, changed = bridge._retry_semantic_change_error(
            previous, duplicated, [{"code": "missing_clause_review"}],
            contract_version="3.0",
        )
        self.assertIsNotNone(error)
        self.assertTrue(changed)

    def test_initial_dispatch_cancellation_writes_failure_audit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, _chunk = self._packet(Path(td) / "requirements")

            class CancelBeforeDispatch(bridge.RunController):
                def check(self) -> None:
                    raise bridge.HostAgentCancelled("cancelled before initial dispatch")

            response_out = Path(td) / "host-agent-response.json"
            with patch.object(bridge, "_resolve_openclaw", return_value="openclaw"), \
                 patch.object(bridge, "RunController", return_value=CancelBeforeDispatch()):
                with self.assertRaisesRegex(bridge.HostAgentCancelled, "before initial dispatch"):
                    bridge.run_bridge(
                        review_dir, response_out=response_out,
                        agent_id="main", timeout=1, max_attempts=1,
                        openclaw_bin="openclaw", model="openai/gpt-5.6-luna",
                    )

            failure = json.loads((review_dir / "host-agent-run.json").read_text(encoding="utf-8"))
            self.assertEqual(failure["status"], "cancelled")
            self.assertEqual(failure["terminal_status"], "cancelled")
            self.assertFalse(failure["merged_response_written"])
            self.assertFalse(response_out.exists())
            lifecycle = failure["chunk_lifecycle"][0]
            self.assertEqual(lifecycle["status"], "not_started")
            self.assertEqual(lifecycle["dispatch_state"], "not_dispatched")

    def test_interruption_finalizer_closes_nested_running_and_retrying_attempts(self) -> None:
        record = {"attempts": [
            {"attempt": 1, "status": "failed"},
            {"attempt": 2, "status": "retrying"},
            {"attempt": 3, "status": "running"},
        ]}
        bridge._finalize_interrupted_attempts(record, "operator interrupted")
        self.assertEqual(
            [item["status"] for item in record["attempts"]],
            ["failed", "terminated", "terminated"],
        )
        for attempt in record["attempts"][1:]:
            self.assertEqual(attempt["remote_operation_state"], "unknown")
            self.assertEqual(attempt["termination_reason"], "operator interrupted")

    def test_completed_sibling_is_harvested_before_concurrent_failure_audit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir = Path(td) / "requirements"
            clauses = [
                {"id": "C1", "text": "正文使用宋体", "evidence_ids": ["E1"],
                 "source_kind": "paragraph", "location": {}, "part_index": 0},
                {"id": "C2", "text": "正文使用黑体", "evidence_ids": ["E2"],
                 "source_kind": "paragraph", "location": {}, "part_index": 0},
            ]
            evidence = {"evidence": [
                {"id": "E1", "text": clauses[0]["text"], "kind": "paragraph"},
                {"id": "E2", "text": clauses[1]["text"], "kind": "paragraph"},
            ]}
            request = engine.build_llm_request([], clauses, evidence, {}, "full")
            request = attach_request_provenance(
                request, source_sha256="a" * 64, evidence_doc=evidence, clauses=clauses,
                run_id="concurrent-failure-audit-test",
            )
            engine.prepare_host_agent_review_packets(
                request, clauses, evidence, "a" * 64, review_dir, chunk_size=1,
            )

            def fake_chunk(**kwargs):
                index = kwargs["chunk_index"]
                if index == 1:
                    raise ValueError("synthetic first chunk failure")
                chunk = kwargs["chunk"]
                clause = chunk["clauses"][0]
                response = {
                    "contract_version": chunk["contract_version"],
                    "provenance": chunk["provenance"],
                    "requirements": [],
                    "clause_reviews": [{
                        "clause_id": clause["id"], "classification": "informational",
                        "requirement_indexes": [], "reason": "Completed sibling review.",
                    }],
                    "unsupported_items": [], "reported_conflicts": [],
                }
                kwargs["response_path"].write_text(
                    json.dumps(response, ensure_ascii=False), encoding="utf-8",
                )
                return {"chunk_index": index, "attempt": 1, "status": "ok"}

            response_out = Path(td) / "host-agent-response.json"
            real_wait = bridge.wait

            def wait_until_all_done(futures, return_when):
                return real_wait(futures, return_when=ALL_COMPLETED)

            with patch.object(bridge, "_resolve_openclaw", return_value="openclaw"), \
                 patch.object(bridge, "run_host_agent_chunk", side_effect=fake_chunk), \
                 patch.object(bridge, "wait", side_effect=wait_until_all_done):
                with self.assertRaisesRegex(ValueError, "synthetic first chunk failure"):
                    bridge.run_bridge(
                        review_dir, response_out=response_out,
                        agent_id="main", timeout=2, max_attempts=1,
                        max_concurrency=2, openclaw_bin="openclaw",
                        model="openai/gpt-5.6-luna",
                    )

            failure = json.loads((review_dir / "host-agent-run.json").read_text(encoding="utf-8"))
            self.assertFalse(failure["merged_response_written"])
            self.assertFalse(response_out.exists())
            self.assertEqual(failure["completed_chunk_indexes"], [2])
            self.assertEqual(failure["in_flight_chunk_indexes"], [])
            self.assertEqual([item["chunk_index"] for item in failure["chunk_runs"]], [2])
            lifecycle = {item["chunk_index"]: item for item in failure["chunk_lifecycle"]}
            self.assertEqual(lifecycle[1]["status"], "failed")
            self.assertEqual(lifecycle[2]["status"], "completed")

    def test_v3_relation_completion_handles_multiple_missing_clauses_and_empty_placeholder(self) -> None:
        previous = {
            "contract_version": "3.0",
            "requirements": [
                {
                    "role": "body_text", "properties": {"font": {"cjk": "SimSun"}},
                    "clause_ids": ["C1"], "evidence_ids": ["E1"],
                    "confidence": 0.9, "reason": "保留的已有要求",
                },
                {
                    "role": "abstract_title_zh", "properties": {},
                    "clause_ids": [], "evidence_ids": [], "confidence": 0,
                    "reason": "",
                },
            ],
            "clause_reviews": [
                {"clause_id": "C1", "classification": "executable"},
                {"clause_id": "C2", "classification": "executable"},
                {"clause_id": "C3", "classification": "executable"},
            ],
            "unsupported_items": [],
            "reported_conflicts": [],
        }
        current = {
            "contract_version": "3.0",
            "requirements": [
                json.loads(json.dumps(previous["requirements"][0])),
                json.loads(json.dumps(previous["requirements"][1])),
                {
                    "role": "content_constraints", "properties": {"keywords_zh": {"max_chars": 10}},
                    "clause_ids": ["C2"], "evidence_ids": ["E2"],
                    "confidence": 0.9, "reason": "C2 的证据支持关键词限制",
                },
                {
                    "role": "content_constraints", "properties": {"keywords_en": {"max_chars": 10}},
                    "clause_ids": ["C3"], "evidence_ids": ["E3"],
                    "confidence": 0.9, "reason": "C3 的证据支持英文关键词限制",
                },
            ],
            "clause_reviews": previous["clause_reviews"],
            "unsupported_items": [],
            "reported_conflicts": [],
        }
        records = [
            {
                "code": "empty_requirement_properties",
                "json_pointer": "$.requirements[1].properties",
                "raw_error": "$.requirements[1].properties: must_include_semantic_payload",
            },
            {
                "code": "missing_derived_requirement",
                "json_pointer": "$.clause_reviews[1]",
                "raw_error": "$.clause_reviews[1]: executable_review_requires_derived_requirement",
            },
            {
                "code": "missing_derived_requirement",
                "json_pointer": "$.clause_reviews[2]",
                "raw_error": "$.clause_reviews[2]: executable_review_requires_derived_requirement",
            },
            {
                "code": "requirement_relation_mismatch",
                "json_pointer": "$.requirements[1]",
                "raw_error": "requirements_not_referenced_by_clause_review:1",
            },
        ]
        with patch.object(bridge, "validate_host_agent_response", return_value=[]):
            repaired, audit = bridge._v3_relation_completion_response(
                previous, current, records, chunk={},
            )
        self.assertIsNotNone(repaired)
        self.assertEqual(
            [item["clause_ids"] for item in repaired["requirements"]],
            [["C1"], ["C2"], ["C3"]],
        )
        self.assertEqual(audit["missing_clause_ids"], ["C2", "C3"])
        self.assertEqual(audit["removed_unbound_placeholder_count"], 1)
        self.assertEqual(audit["added_requirement_count"], 2)

        current["requirements"][2]["clause_ids"] = ["C9"]
        with patch.object(bridge, "validate_host_agent_response", return_value=[]):
            repaired, audit = bridge._v3_relation_completion_response(
                previous, current, records, chunk={},
            )
        self.assertIsNone(repaired)
        self.assertIsNone(audit)

    def test_v3_retry_allows_only_proven_informational_projection(self) -> None:
        previous = {
            "contract_version": "3.0",
            "requirements": [
                {"role": "body_text", "properties": {"text": "保留"}, "clause_ids": ["C1"]},
                {"role": "body_text", "properties": {"text": "删除一"}, "clause_ids": ["C2"]},
                {"role": "body_text", "properties": {"text": "删除二"}, "clause_ids": ["C3"]},
            ],
            "clause_reviews": [
                {"clause_id": "C1", "classification": "executable"},
                {"clause_id": "C2", "classification": "informational"},
                {"clause_id": "C3", "classification": "informational"},
            ],
            "unsupported_items": [],
            "reported_conflicts": [],
        }
        current = json.loads(json.dumps(previous))
        current["requirements"] = [json.loads(json.dumps(previous["requirements"][0]))]
        records = [
            {"code": "informational_requirement_forbidden", "requirement_index": 1},
            {"code": "informational_requirement_forbidden", "requirement_index": 2},
        ]
        changed = bridge._retry_change_paths(previous, current)
        self.assertEqual(changed, ["$.requirements"])
        self.assertTrue(bridge._retry_changes_allowed(
            records, changed, contract_version="3.0",
            previous_response=previous, current_response=current,
            chunk={"clauses": [{"id": f"C{i}"} for i in range(1, 4)]},
        ))
        current["requirements"][0]["properties"]["text"] = "改动了保留项"
        changed = bridge._retry_change_paths(previous, current)
        self.assertFalse(bridge._retry_changes_allowed(
            records, changed, contract_version="3.0",
            previous_response=previous, current_response=current,
            chunk={"clauses": [{"id": f"C{i}"} for i in range(1, 4)]},
        ))

    def test_v3_retry_allows_only_proven_non_requirement_projection(self) -> None:
        previous = {
            "contract_version": "3.0",
            "requirements": [
                {
                    "role": "body_text", "properties": {"text": "保留"},
                    "clause_ids": ["C1"], "evidence_ids": ["E1"],
                },
                {
                    "role": "content_constraints", "properties": {"text": "未解决"},
                    "clause_ids": ["C2"], "evidence_ids": ["E2"],
                },
            ],
            "clause_reviews": [
                {"clause_id": "C1", "classification": "executable"},
                {"clause_id": "C2", "classification": "unresolved"},
            ],
            "unsupported_items": [],
            "reported_conflicts": [],
        }
        current = json.loads(json.dumps(previous, ensure_ascii=False))
        current["requirements"].pop(1)
        records = [{
            "code": "non_requirement_classification_relation",
            "json_pointer": "$.requirements[1]",
            "requirement_index": 1,
            "relation_category": "non_requirement_classification",
            "clause_ids": ["C2"],
        }]
        chunk = {"clauses": [{"id": "C1"}, {"id": "C2"}]}
        changed = bridge._retry_change_paths(previous, current)
        self.assertEqual(changed, ["$.requirements"])
        self.assertTrue(bridge._v3_non_requirement_projection_allowed(
            previous, current, records, changed, chunk=chunk,
        ))
        self.assertTrue(bridge._retry_changes_allowed(
            records, changed, contract_version="3.0",
            previous_response=previous, current_response=current, chunk=chunk,
        ))

        unsafe = json.loads(json.dumps(previous, ensure_ascii=False))
        unsafe["requirements"].pop(0)
        self.assertFalse(bridge._retry_changes_allowed(
            records, bridge._retry_change_paths(previous, unsafe),
            contract_version="3.0", previous_response=previous,
            current_response=unsafe, chunk=chunk,
        ))

    def test_v3_uncovered_obligation_retry_drops_stale_empty_requirement_shell(self) -> None:
        previous = {
            "contract_version": "3.0",
            "requirements": [{
                "role": "figure_caption",
                "properties": {"position": "below"},
                "clause_ids": ["C1"],
                "evidence_ids": ["E1"],
                "existing_requirement_id": "R1",
                "reason": "现有图题要求。",
            }],
            "clause_reviews": [{
                "clause_id": "C1",
                "classification": "verify_existing",
                "obligations": [{
                    "id": "C1.separator",
                    "status": "unverifiable",
                    "reason": "分隔要求需要人工确认。",
                }],
                "reason": "当前候选只能覆盖部分要求。",
                "normative_basis": "explicit_normative_text",
            }],
            "unsupported_items": [],
            "reported_conflicts": [],
        }
        current = copy.deepcopy(previous)
        current["clause_reviews"][0]["classification"] = "unsupported_backend"
        # This is the exact invalid intermediate shape emitted by the native
        # retry: it removes the last clause edge but leaves the old reference
        # and evidence behind. The bridge must drop this shell, not preserve it.
        current["requirements"][0]["clause_ids"] = []
        records = [{
            "code": "executable_review_obligations_uncovered",
            "json_pointer": "$.clause_reviews[0].obligations",
            "clause_id": "C1",
            "raw_error": "$.clause_reviews[0].obligations: executable_review_requires_all_obligations_covered",
        }]
        chunk = {"clauses": [{"id": "C1"}]}
        with patch.object(bridge, "validate_host_agent_response", return_value=[]):
            repaired, audit = bridge._v3_uncovered_obligation_reclassification_response(
                previous, current, records, chunk=chunk,
            )
        self.assertIsNotNone(repaired)
        assert repaired is not None
        self.assertEqual(repaired["requirements"], [])
        self.assertEqual(
            repaired["clause_reviews"][0]["classification"], "unsupported_backend",
        )
        self.assertEqual(audit["dropped_stale_empty_requirement_count"], 1)

        # A payload edit next to the stale shell is still semantic drift and
        # must remain fail-closed.
        current["requirements"][0]["reason"] = "被篡改的说明"
        with patch.object(bridge, "validate_host_agent_response", return_value=[]):
            rejected, _ = bridge._v3_uncovered_obligation_reclassification_response(
                previous, current, records, chunk=chunk,
            )
        self.assertIsNone(rejected)

    def test_v3_retry_allows_non_requirement_projection_with_named_payload_cleanup(self) -> None:
        previous = {
            "contract_version": "3.0",
            "requirements": [
                {
                    "role": "body_text", "properties": {"style": "bad"},
                    "clause_ids": ["C1"], "evidence_ids": ["E1"],
                },
                {
                    "role": "body_text", "properties": {"text": "未解决"},
                    "clause_ids": ["C2"], "evidence_ids": ["E2"],
                },
            ],
            "clause_reviews": [
                {"clause_id": "C1", "classification": "executable"},
                {"clause_id": "C2", "classification": "unsupported_backend"},
            ],
            "unsupported_items": [],
        }
        current = json.loads(json.dumps(previous, ensure_ascii=False))
        current["requirements"] = [{
            "role": "body_text", "properties": {},
            "clause_ids": ["C1"], "evidence_ids": ["E1"],
        }]
        records = [
            {
                "code": "unknown_property",
                "json_pointer": "$.requirements[0].properties",
                "raw_error": "unknown property 'style'",
            },
            {
                "code": "non_requirement_classification_relation",
                "json_pointer": "$.requirements[1]",
                "requirement_index": 1,
                "relation_category": "non_requirement_classification",
                "clause_ids": ["C2"],
            },
        ]
        chunk = {
            "clauses": [
                {"id": "C1", "evidence_ids": ["E1"]},
                {"id": "C2", "evidence_ids": ["E2"]},
            ],
            "evidence_context": {
                "E1": {"text": "固定正文"},
                "E2": {"text": "未解决"},
            },
            "requirement_contract": {
                "role_properties_schema": {"body_text": {"$ref": "#/$defs/roleSpec"}},
            },
        }
        changed = bridge._retry_change_paths(previous, current)
        self.assertEqual(changed, ["$.requirements"])
        self.assertTrue(bridge._retry_changes_allowed(
            records, changed, contract_version="3.0",
            previous_response=previous, current_response=current, chunk=chunk,
        ))

        current["requirements"][0]["properties"] = {"text": "猜测正文"}
        self.assertFalse(bridge._retry_changes_allowed(
            records, bridge._retry_change_paths(previous, current),
            contract_version="3.0", previous_response=previous,
            current_response=current, chunk=chunk,
        ))

    def test_v3_retry_allows_only_exact_fixed_declaration_completion(self) -> None:
        previous = {
            "contract_version": "3.0",
            "requirements": [
                {
                    "role": "body_text",
                    "properties": {"text": "保留 requirement"},
                    "clause_ids": ["C1"],
                    "evidence_ids": ["E1"],
                },
                {
                    "role": "table_text",
                    "properties": {},
                    "clause_ids": [],
                    "evidence_ids": [],
                    "confidence": 0,
                    "reason": "",
                },
            ],
            "clause_reviews": [
                {"clause_id": "C1", "classification": "executable", "reason": "保留"},
                {"clause_id": "C2", "classification": "executable", "reason": "原始标题"},
                {"clause_id": "C3", "classification": "executable", "reason": "原始正文"},
            ],
            "unsupported_items": [],
            "reported_conflicts": [],
        }
        current = json.loads(json.dumps(previous, ensure_ascii=False))
        current["requirements"] = [
            json.loads(json.dumps(previous["requirements"][0], ensure_ascii=False)),
            {
                "role": "declarations",
                "properties": {
                    "before_role": "abstract_title_zh",
                    "items": [{
                        "heading": "学位论文使用授权书",
                        "body_parts": ["本人同意提交电子版"],
                        "source_evidence_ids": ["E2", "E3"],
                    }],
                },
                "clause_ids": ["C2", "C3"],
                "evidence_ids": ["E2", "E3"],
                "confidence": 0.99,
                "reason": "由当前证据固定生成",
            },
        ]
        current["clause_reviews"][2]["reason"] = "当前证据明确该固定声明正文"
        records = [
            {
                "code": "empty_requirement_properties",
                "json_pointer": "$.requirements[1].properties",
                "raw_error": "must_include_semantic_payload",
            },
            {
                "code": "requirement_relation_mismatch",
                "json_pointer": "$.requirements[1]",
                "raw_error": "requirements_not_referenced_by_clause_review:1",
            },
            {
                "code": "missing_derived_requirement",
                "json_pointer": "$.clause_reviews[1]",
                "raw_error": "executable_review_requires_derived_requirement",
            },
            {
                "code": "missing_derived_requirement",
                "json_pointer": "$.clause_reviews[2]",
                "raw_error": "executable_review_requires_derived_requirement",
            },
        ]
        chunk = {
            "declaration_anchor_preference": "abstract_title_zh",
            "clauses": [
                {"id": "C1", "text": "保留 requirement", "evidence_ids": ["E1"]},
                {"id": "C2", "text": "学位论文使用授权书", "evidence_ids": ["E2"]},
                {"id": "C3", "text": "本人同意提交电子版", "evidence_ids": ["E3"]},
                {"id": "C4", "text": "摘要", "evidence_ids": ["E4"]},
            ],
            "evidence_context": {
                "E1": {"text": "保留 requirement"},
                "E2": {"text": "学位论文使用授权书"},
                "E3": {"text": "本人同意提交电子版"},
                "E4": {"text": "摘要"},
            },
        }
        changed = bridge._retry_change_paths(previous, current)
        self.assertEqual(
            changed,
            ["$.clause_reviews[2].reason", "$.requirements"],
        )
        with patch.object(bridge, "validate_host_agent_response", return_value=[]):
            self.assertTrue(bridge._retry_changes_allowed(
                records, changed, contract_version="3.0",
                previous_response=previous, current_response=current, chunk=chunk,
            ))

        current["requirements"][1]["properties"]["items"][0]["body_parts"] = ["猜测正文"]
        changed = bridge._retry_change_paths(previous, current)
        with patch.object(bridge, "validate_host_agent_response", return_value=[]):
            self.assertFalse(bridge._retry_changes_allowed(
                records, changed, contract_version="3.0",
                previous_response=previous, current_response=current, chunk=chunk,
            ))

    def test_v3_retry_restores_omitted_baseline_requirements_before_accepting_additions(self) -> None:
        previous = {
            "contract_version": "3.0",
            "requirements": [
                {
                    "role": "cover_field_label", "properties": {"text": "标题"},
                    "clause_ids": ["C1"], "evidence_ids": ["E1"],
                },
                {
                    "role": "body_text", "properties": {"font": {"latin": "Times New Roman"}},
                    "clause_ids": ["C3"], "evidence_ids": ["E3"],
                },
            ],
            "clause_reviews": [
                {"clause_id": "C1", "classification": "executable"},
                {"clause_id": "C2", "classification": "executable"},
                {"clause_id": "C3", "classification": "informational"},
            ],
            "unsupported_items": [],
            "reported_conflicts": [],
        }
        current = {
            "contract_version": "3.0",
            "requirements": [
                json.loads(json.dumps(previous["requirements"][0])),
                {
                    "role": "heading_1", "properties": {"text": "第一章"},
                    "clause_ids": ["C2"], "evidence_ids": ["E2"],
                },
            ],
            "clause_reviews": previous["clause_reviews"],
            "unsupported_items": [],
            "reported_conflicts": [],
        }
        records = [{
            "code": "requirement_relation_mismatch",
            "json_pointer": "$.clause_reviews[1]",
            "raw_error": "$.clause_reviews[1]: executable_review_requires_derived_requirement",
        }]
        chunk = {}
        with patch.object(bridge, "validate_host_agent_response", return_value=[]):
            repaired, audit = bridge._v3_relation_completion_response(
                previous, current, records, chunk=chunk,
            )
        self.assertIsNotNone(repaired)
        self.assertEqual(
            [item["clause_ids"] for item in repaired["requirements"]],
            [["C1"], ["C3"], ["C2"]],
        )
        self.assertEqual(audit["preserved_requirement_count"], 2)
        self.assertEqual(audit["added_requirement_count"], 1)

        current["requirements"][0]["properties"]["text"] = "改过的旧要求"
        with patch.object(bridge, "validate_host_agent_response", return_value=[]):
            repaired, audit = bridge._v3_relation_completion_response(
                previous, current, records, chunk=chunk,
            )
        self.assertIsNone(repaired)
        self.assertIsNone(audit)

    def test_v3_relation_completion_accepts_exact_text_after_unknown_property_removal(self) -> None:
        previous = {
            "contract_version": "3.0",
            "requirements": [
                {
                    "role": "cover_field_label",
                    "properties": {"style": "three_line"},
                    "clause_ids": ["C1"],
                    "evidence_ids": ["E1"],
                },
                {
                    "role": "body_text",
                    "properties": {"text": "正文"},
                    "clause_ids": ["C2"],
                    "evidence_ids": ["E2"],
                },
            ],
            "clause_reviews": [
                {"clause_id": "C1", "classification": "executable"},
                {"clause_id": "C2", "classification": "executable"},
                {"clause_id": "C3", "classification": "executable"},
            ],
            "unsupported_items": [],
            "reported_conflicts": [],
        }
        current = json.loads(json.dumps(previous, ensure_ascii=False))
        current["requirements"] = [
            {
                "role": "cover_field_label",
                "properties": {"text": "论文题目"},
                "clause_ids": ["C1"],
                "evidence_ids": ["E1"],
            },
            current["requirements"][1],
            {
                "role": "body_text",
                "properties": {"text": "新增正文"},
                "clause_ids": ["C3"],
                "evidence_ids": ["E3"],
            },
        ]
        records = [
            {
                "code": "unknown_property",
                "json_pointer": "$.requirements[0].properties",
                "raw_error": "unknown property 'style'",
            },
            {
                "code": "missing_derived_requirement",
                "json_pointer": "$.clause_reviews[2]",
                "clause_id": "C3",
                "raw_error": "$.clause_reviews[2]: executable_review_requires_derived_requirement",
            },
        ]
        chunk = {
            "clauses": [
                {"id": "C1", "evidence_ids": ["E1"]},
                {"id": "C2", "evidence_ids": ["E2"]},
                {"id": "C3", "evidence_ids": ["E3"]},
            ],
            "evidence_context": {
                "E1": {"text": "论文题目"},
                "E2": {"text": "正文"},
                "E3": {"text": "新增正文"},
            },
            "requirement_contract": {
                "role_properties_schema": {
                    "cover_field_label": {"$ref": "#/$defs/roleSpec"},
                    "body_text": {"$ref": "#/$defs/roleSpec"},
                },
            },
        }
        with patch.object(bridge, "validate_host_agent_response", return_value=[]):
            repaired, audit = bridge._v3_relation_completion_response(
                previous, current, records, chunk=chunk,
            )
        self.assertIsNotNone(repaired)
        self.assertIsNotNone(audit)
        assert repaired is not None
        self.assertEqual(repaired["requirements"][0]["properties"], {"text": "论文题目"})
        self.assertEqual(repaired["requirements"][-1]["clause_ids"], ["C3"])

    def test_v3_retry_allows_bounded_completion_of_truncated_response(self) -> None:
        previous = {
            "contract_version": "3.0",
            "requirements": [{
                "role": "cover_field_label", "properties": {},
                "clause_ids": ["C1"], "evidence_ids": ["E1"],
            }],
            "clause_reviews": [], "unsupported_items": [],
        }
        current = {
            "contract_version": "3.0",
            "requirements": [{
                "role": "cover_field_label", "properties": {"text": "标题"},
                "clause_ids": ["C1"], "evidence_ids": ["E1"],
            }, {
                "role": "body_text", "properties": {"text": "正文"},
                "clause_ids": ["C2"], "evidence_ids": ["E2"],
            }],
            "clause_reviews": [
                {"clause_id": "C1", "classification": "executable"},
                {"clause_id": "C2", "classification": "unsupported_backend"},
            ],
            "unsupported_items": ["C2：后端无法验证该要求"],
        }
        records = [
            {"code": "empty_requirement_properties"},
            {
                "code": "contract_validation_error",
                "raw_error": "clause_reviews_must_cover_each_chunk_clause_exactly_once",
            },
            {
                "code": "requirement_relation_mismatch",
                "raw_error": "requirements_not_referenced_by_clause_review:0",
            },
        ]
        chunk = {
            "evidence_context": {
                "E1": {"text": "标题"}, "E2": {"text": "正文"},
            },
            "requirement_contract": {
                "role_properties_schema": {
                    "cover_field_label": {"$ref": "#/$defs/roleSpec"},
                    "body_text": {"$ref": "#/$defs/roleSpec"},
                },
            },
        }
        changed = bridge._retry_change_paths(previous, current)
        self.assertEqual(
            changed,
            ["$.clause_reviews", "$.requirements", "$.unsupported_items"],
        )
        with patch.object(bridge, "validate_host_agent_response", return_value=[]):
            self.assertTrue(bridge._retry_changes_allowed(
                records, changed, contract_version="3.0",
                previous_response=previous, current_response=current, chunk=chunk,
            ))

        current["unsupported_items"] = ["C9：不属于当前 chunk"]
        changed = bridge._retry_change_paths(previous, current)
        with patch.object(bridge, "validate_host_agent_response", return_value=[]):
            self.assertFalse(bridge._retry_changes_allowed(
                records, changed, contract_version="3.0",
                previous_response=previous, current_response=current, chunk=chunk,
            ))

    def test_v3_retry_allows_truncated_completion_with_fixed_text_and_placeholder(self) -> None:
        previous = {
            "contract_version": "3.0",
            "requirements": [
                {
                    "role": "declarations",
                    "properties": {
                        "before_role": "abstract_title_zh",
                        "items": [{
                            "id": "authorization",
                            "body_parts": ["截短的固定声明正文"],
                        }],
                    },
                    "clause_ids": ["C1"],
                    "evidence_ids": ["E1"],
                    "confidence": 0.9,
                    "reason": "保留声明关系",
                },
                {
                    "role": "heading_1",
                    "properties": {"style": "three_line"},
                    "clause_ids": [],
                    "evidence_ids": [],
                    "confidence": 0,
                    "reason": "",
                },
            ],
            "clause_reviews": [],
            "unsupported_items": [],
            "reported_conflicts": [],
        }
        current = {
            "contract_version": "3.0",
            "requirements": [
                {
                    "role": "declarations",
                    "properties": {
                        "before_role": "abstract_title_zh",
                        "items": [{
                            "id": "authorization",
                            "body_parts": ["完整固定声明正文"],
                        }],
                    },
                    "clause_ids": ["C1"],
                    "evidence_ids": ["E1"],
                    "confidence": 0.9,
                    "reason": "保留声明关系",
                },
                {
                    "role": "body_text",
                    "properties": {"text": "正文"},
                    "clause_ids": ["C2"],
                    "evidence_ids": ["E2"],
                    "confidence": 0.8,
                    "reason": "补齐当前条款",
                },
            ],
            "clause_reviews": [
                {"clause_id": "C1", "classification": "executable"},
                {"clause_id": "C2", "classification": "executable"},
            ],
            "unsupported_items": [],
            "reported_conflicts": [],
        }
        records = [
            {
                "code": "contract_validation_error",
                "json_pointer": "$.requirements[1].reason",
                "raw_error": "$.requirements[1].reason: is shorter than 1 characters",
            },
            {
                "code": "fixed_text_evidence_mismatch",
                "json_pointer": "$.requirements[0].properties.items[0].body_parts[0]",
                "raw_error": "must equal a complete cited source-evidence text",
            },
            {
                "code": "unknown_property",
                "json_pointer": "$.requirements[1].properties",
                "raw_error": "unknown property 'style'",
            },
            {
                "code": "schema_contract_violation",
                "json_pointer": "$.requirements[1].clause_ids",
                "raw_error": "must_be_non_empty",
            },
            {
                "code": "schema_contract_violation",
                "json_pointer": "$.requirements[1].evidence_ids",
                "raw_error": "must_be_non_empty",
            },
            {
                "code": "missing_clause_review",
                "json_pointer": "$.requirements[0]",
                "raw_error": "$.requirements[0]:missing_clause_review:clause_ids=C1",
            },
            {
                "code": "contract_validation_error",
                "raw_error": "clause_reviews_must_cover_each_chunk_clause_exactly_once",
            },
            {
                "code": "requirement_relation_mismatch",
                "json_pointer": "$.requirements[1]",
                "raw_error": "requirements_not_referenced_by_clause_review:1",
                "relation_category": "missing_clause_relation",
            },
        ]
        chunk = {
            "clauses": [{"id": "C1"}, {"id": "C2"}],
            "evidence_context": {
                "E1": {"text": "完整固定声明正文"},
                "E2": {"text": "正文"},
            },
        }
        changed = bridge._retry_change_paths(previous, current)
        self.assertEqual(changed, ["$.clause_reviews", "$.requirements"])
        with patch.object(bridge, "validate_host_agent_response", return_value=[]):
            self.assertTrue(bridge._retry_changes_allowed(
                records, changed, contract_version="3.0",
                previous_response=previous, current_response=current, chunk=chunk,
            ))

        current["requirements"][0]["properties"]["items"][0]["body_parts"] = ["模型猜测正文"]
        changed = bridge._retry_change_paths(previous, current)
        with patch.object(bridge, "validate_host_agent_response", return_value=[]):
            self.assertFalse(bridge._retry_changes_allowed(
                records, changed, contract_version="3.0",
                previous_response=previous, current_response=current, chunk=chunk,
            ))

    def test_v3_retry_allows_only_validator_named_missing_clause_review(self) -> None:
        previous = {
            "contract_version": "3.0",
            "requirements": [{
                "role": "table", "properties": {"style": "three_line"},
                "clause_ids": ["C1"], "evidence_ids": ["E1"],
            }, {
                "role": "table", "properties": {"border_widths_pt": {"top": 1.5}},
                "clause_ids": ["C2"], "evidence_ids": ["E2"],
            }],
            "clause_reviews": [{"clause_id": "C1", "classification": "executable"}],
            "unsupported_items": [], "reported_conflicts": [],
        }
        current = json.loads(json.dumps(previous))
        current["clause_reviews"].append({
            "clause_id": "C2", "classification": "informational",
        })
        records = [{
            "code": "missing_clause_review",
            "json_pointer": "$.requirements[0]",
            "raw_error": "$.requirements[0]:missing_clause_review:clause_ids=C2",
        }, {
            "code": "contract_validation_error",
            "raw_error": "clause_reviews_must_cover_each_chunk_clause_exactly_once",
        }]
        chunk = {"clauses": [{"id": "C1"}, {"id": "C2"}]}
        changed = bridge._retry_change_paths(previous, current)
        self.assertEqual(changed, ["$.clause_reviews"])
        with patch.object(bridge, "validate_host_agent_response", return_value=[]):
            self.assertTrue(bridge._retry_changes_allowed(
                records, changed, contract_version="3.0",
                previous_response=previous, current_response=current, chunk=chunk,
            ))

        current["clause_reviews"][0]["classification"] = "informational"
        changed = bridge._retry_change_paths(previous, current)
        with patch.object(bridge, "validate_host_agent_response", return_value=[]):
            self.assertFalse(bridge._retry_changes_allowed(
                records, changed, contract_version="3.0",
                previous_response=previous, current_response=current, chunk=chunk,
            ))

    def test_v3_retry_discards_only_unbound_truncated_placeholder(self) -> None:
        previous = {
            "contract_version": "3.0",
            "requirements": [{
                "role": "body_text", "properties": {"style": "three_line"},
                "clause_ids": [], "evidence_ids": [], "reason": "",
            }],
            "clause_reviews": [], "unsupported_items": [],
        }
        current = {
            "contract_version": "3.0",
            "requirements": [{
                "role": "cover_field_label", "properties": {"text": "标题"},
                "clause_ids": ["C1"], "evidence_ids": ["E1"],
            }],
            "clause_reviews": [{
                "clause_id": "C1", "classification": "executable",
            }],
            "unsupported_items": [],
        }
        records = [
            {"code": "contract_validation_error", "raw_error": "$.requirements[0].reason: is shorter than 1 characters"},
            {"code": "unknown_property", "raw_error": "$.requirements[0].properties: unknown property 'style'"},
            {"code": "schema_contract_violation", "raw_error": "$.requirements[0].clause_ids: must_be_non_empty"},
            {"code": "schema_contract_violation", "raw_error": "$.requirements[0].evidence_ids: must_be_non_empty"},
            {"code": "contract_validation_error", "raw_error": "clause_reviews_must_cover_each_chunk_clause_exactly_once"},
        ]
        chunk = {
            "evidence_context": {"E1": {"text": "标题"}},
            "requirement_contract": {
                "role_properties_schema": {
                    "cover_field_label": {"$ref": "#/$defs/roleSpec"},
                },
            },
        }
        changed = bridge._retry_change_paths(previous, current)
        with patch.object(bridge, "validate_host_agent_response", return_value=[]):
            self.assertTrue(bridge._retry_changes_allowed(
                records, changed, contract_version="3.0",
                previous_response=previous, current_response=current, chunk=chunk,
            ))
        previous["requirements"][0]["clause_ids"] = ["C0"]
        self.assertFalse(bridge._retry_changes_allowed(
            records, changed, contract_version="3.0",
            previous_response=previous, current_response=current, chunk=chunk,
        ))

    def test_v3_retry_allows_schema_directed_cover_completion_only(self) -> None:
        previous = {
            "contract_version": "3.0",
            "requirements": [{
                "role": "cover", "properties": {
                    "before_role": "document_start", "items": [],
                }, "clause_ids": ["C1"], "evidence_ids": ["E1"],
                "verification": {
                    "mode": "static_docx", "checks": ["保留检查", ""],
                },
            }],
            "clause_reviews": [{
                "clause_id": "C1", "classification": "executable",
            }],
            "unsupported_items": [],
        }
        current = json.loads(json.dumps(previous))
        current["requirements"][0]["properties"] = {
            "institution": "——",
            "fields": [{
                "id": "title_zh", "label": "论文题目：",
                "value_from": "thesis_profile.cover_metadata.title_zh",
                "display_policy": "required", "order": 1,
            }],
            "before_role": "document_start",
            "missing_value_policy": "placeholder",
            "missing_value_placeholder": "——",
            "layout_id": "linear",
        }
        records = [
            {"code": "contract_validation_error", "json_pointer": "$.requirements[0].properties"},
            {"code": "schema_contract_violation", "json_pointer": "$.requirements[0].properties"},
            {"code": "unknown_property", "json_pointer": "$.requirements[0].properties"},
            {"code": "cover_binding_violation", "json_pointer": "$.requirements[0].properties.fields"},
            {
                "code": "contract_validation_error",
                "json_pointer": "$.requirements[0].verification.checks",
                "raw_error": "$.requirements[0].verification.checks[1]: is shorter than 1 characters",
            },
        ]
        current["requirements"][0]["verification"]["checks"] = ["保留检查"]
        changed = bridge._retry_change_paths(previous, current)
        chunk = {"response_schema": {"type": "object"}}
        with patch.object(bridge, "validate_host_agent_response", return_value=[]):
            self.assertTrue(bridge._retry_changes_allowed(
                records, changed, contract_version="3.0",
                previous_response=previous, current_response=current, chunk=chunk,
            ))
        current["clause_reviews"][0]["classification"] = "not_applicable"
        changed = bridge._retry_change_paths(previous, current)
        self.assertFalse(bridge._retry_changes_allowed(
            records, changed, contract_version="3.0",
            previous_response=previous, current_response=current, chunk=chunk,
        ))

    def test_mechanical_repair_removes_informational_only_requirement(self) -> None:
        response = {
            "requirements": [{
                "role": "body_text", "properties": {"text": "签名"},
                "clause_ids": ["C1"], "evidence_ids": ["E1"],
            }],
            "clause_reviews": [{
                "clause_id": "C1", "classification": "informational",
            }],
        }
        repaired, repairs = bridge._apply_safe_mechanical_repairs(
            response,
            [{
                "code": "informational_requirement_forbidden",
                "requirement_index": 0,
                "raw_error": "requirements_not_referenced_by_clause_review:0",
            }],
        )
        self.assertEqual(repaired["requirements"], [])
        self.assertEqual(repairs[0]["removed_clause_ids"], ["C1"])
        self.assertEqual(repairs[0]["removed_requirement"]["clause_ids"], ["C1"])
        self.assertEqual(
            repairs[0]["removed_requirement_sha256"],
            bridge._response_sha256(response["requirements"][0]),
        )
        self.assertEqual(len(response["requirements"]), 1)

    def test_external_pending_requirement_edge_is_projected_with_source_bound_audit(self) -> None:
        source = "北京体育大学学位评定委员会办公室盖章(有效)"
        clauses = [{
            "id": "C1", "text": source, "evidence_ids": ["E1"],
            "source_kind": "paragraph", "location": {}, "part_index": 0,
        }]
        evidence = {"evidence": [{"id": "E1", "text": source, "kind": "paragraph"}]}
        clauses = self._bind_test_source_spans(clauses, evidence)
        chunk = engine.build_llm_request(
            [], clauses, evidence, {}, "full", contract_version="3.0",
        )
        chunk = attach_request_provenance(
            chunk, source_sha256="e" * 64, evidence_doc=evidence,
            clauses=clauses, run_id="external-action-projection-test",
        )
        response = {
            "contract_version": "3.0",
            "provenance": chunk["provenance"],
            "requirements": [{
                "role": "body_text", "properties": {"text": source},
                "clause_ids": ["C1"], "evidence_ids": ["E1"],
                "confidence": 0.95, "reason": "The source requires a real-world stamp.",
                "verification": {
                    "mode": "external", "checks": ["Obtain and verify the actual stamp."],
                },
            }],
            "clause_reviews": [{
                "clause_id": "C1", "classification": "external_compliance",
                "normative_basis": "external_duty", "reason": "This is a real-world action.",
                "obligations": [{
                    "id": "stamp", "status": "unverifiable",
                    "reason": "The real-world stamp cannot be applied in DOCX.",
                }],
            }],
            "unsupported_items": [], "reported_conflicts": [],
        }
        errors = bridge.validate_host_agent_response(response, chunk)
        records = bridge.contract_error_records(errors, response=response, chunk=chunk)
        self.assertEqual({item["code"] for item in records}, {
            "non_requirement_classification_relation",
        })

        repaired, repairs = bridge._apply_safe_mechanical_repairs(
            response, records, chunk=chunk,
        )
        self.assertIsNotNone(repaired)
        self.assertEqual(repaired["requirements"], [])
        self.assertEqual(repaired["clause_reviews"], response["clause_reviews"])
        self.assertEqual(repairs[0]["rule_id"], "external_action_relation_projection_v1")
        self.assertEqual(repairs[0]["removed_requirements"], response["requirements"])
        self.assertTrue(repairs[0]["external_actions_remain_pending"])
        self.assertEqual(bridge.validate_host_agent_response(repaired, chunk), [])

        stale = copy.deepcopy(records)
        stale[0]["response_sha256"] = "0" * 64
        self.assertIsNone(bridge._apply_safe_mechanical_repairs(
            response, stale, chunk=chunk,
        )[0])

    def test_external_pending_projection_refuses_mixed_edges_and_existing_identity(self) -> None:
        response = {
            "contract_version": "3.0",
            "requirements": [{
                "role": "body_text", "properties": {"text": "外部事项"},
                "clause_ids": ["C1"], "evidence_ids": ["E1"],
                "verification": {"mode": "external", "checks": ["人工核验"]},
            }],
            "clause_reviews": [
                {"clause_id": "C1", "classification": "external_compliance"},
                {"clause_id": "C2", "classification": "executable"},
            ],
        }
        chunk = {"clauses": [
            {"id": "C1", "evidence_ids": ["E1"]},
            {"id": "C2", "evidence_ids": ["E2"]},
        ], "evidence_context": {
            "E1": {"id": "E1", "text": "外部事项"},
            "E2": {"id": "E2", "text": "正文事项"},
        }}
        mixed = copy.deepcopy(response)
        mixed["requirements"][0]["clause_ids"] = ["C1", "C2"]
        records = [{"code": "non_requirement_classification_relation",
                    "json_pointer": "$.requirements[0]", "requirement_index": 0,
                    "response_sha256": bridge._response_sha256(mixed)}]
        self.assertIsNone(bridge._project_external_action_requirements(mixed, records, chunk)[0])

        identified = copy.deepcopy(response)
        identified["requirements"][0]["existing_requirement_id"] = "R1"
        records[0]["response_sha256"] = bridge._response_sha256(identified)
        self.assertIsNone(bridge._project_external_action_requirements(identified, records, chunk)[0])

    def test_mechanical_repair_removes_batch_informational_requirements_by_mask(self) -> None:
        response = {
            "requirements": [
                {"role": "body_text", "properties": {"text": "一"}, "clause_ids": ["C1"]},
                {"role": "body_text", "properties": {"text": "二"}, "clause_ids": ["C2"]},
                {"role": "body_text", "properties": {"text": "三"}, "clause_ids": ["C3"]},
                {"role": "body_text", "properties": {"text": "四"}, "clause_ids": ["C4"]},
            ],
            "clause_reviews": [
                {"clause_id": "C1", "classification": "informational"},
                {"clause_id": "C2", "classification": "informational"},
                {"clause_id": "C3", "classification": "executable"},
                {"clause_id": "C4", "classification": "informational"},
            ],
        }
        repaired, repairs = bridge._apply_safe_mechanical_repairs(
            response,
            [
                {"code": "informational_requirement_forbidden", "requirement_index": 0},
                {"code": "informational_requirement_forbidden", "requirement_index": 1},
                {"code": "informational_requirement_forbidden", "requirement_index": 3},
            ],
        )
        self.assertEqual(
            [item["clause_ids"] for item in repaired["requirements"]], [["C3"]]
        )
        self.assertEqual(repairs[-1]["removed_requirement_indexes"], [0, 1, 3])
        self.assertEqual(repairs[-1]["projection"], "ordered_mask")
        self.assertEqual(
            repairs[-1]["original_index_to_repaired_index"],
            {"0": None, "1": None, "2": 0, "3": None},
        )
        self.assertEqual(
            repairs[-1]["removed_requirement_fingerprints"],
            [
                bridge._response_sha256(response["requirements"][0]),
                bridge._response_sha256(response["requirements"][1]),
                bridge._response_sha256(response["requirements"][3]),
            ],
        )
        self.assertEqual(
            repairs[-1]["source_response_sha256"],
            bridge._response_sha256(response),
        )
        self.assertEqual(
            repairs[-1]["repaired_response_sha256"],
            bridge._response_sha256(repaired),
        )
        self.assertEqual(len(response["requirements"]), 4)

    def test_mechanical_repair_removes_only_unbound_empty_requirement_placeholder(self) -> None:
        response = {
            "requirements": [
                {
                    "role": "declarations",
                    "properties": {"items": [{"heading": "固定声明"}]},
                    "clause_ids": ["C1"],
                    "evidence_ids": ["E1"],
                    "reason": "固定声明文本有来源依据。",
                    "confidence": 0.9,
                },
                {
                    "role": "body_text",
                    "properties": {
                        "style": None,
                        "top_border_pt": None,
                        "header_border_pt": None,
                        "bottom_border_pt": None,
                        "remove_vertical_borders": None,
                        "repeat_header_row": None,
                        "allow_row_split": None,
                        "keep_with_caption": None,
                        "border_widths_pt": None,
                        "continuation": None,
                    },
                    "clause_ids": [],
                    "evidence_ids": [],
                    "confidence": 0,
                    "reason": "",
                },
            ],
            "clause_reviews": [{"clause_id": "C1", "classification": "covered"}],
        }
        repaired, repairs = bridge._apply_safe_mechanical_repairs(
            response,
            [{
                "code": "contract_validation_error",
                "json_pointer": "$.requirements[1].reason",
                "raw_error": "$.requirements[1].reason: is shorter than 1 characters",
            }, {
                "code": "empty_requirement_properties",
                "json_pointer": "$.requirements[1].properties",
                "raw_error": "$.requirements[1].properties: must_include_semantic_payload",
            }, {
                "code": "schema_contract_violation",
                "json_pointer": "$.requirements[1].clause_ids",
                "raw_error": "$.requirements[1].clause_ids: must_be_non_empty",
            }, {
                "code": "schema_contract_violation",
                "json_pointer": "$.requirements[1].evidence_ids",
                "raw_error": "$.requirements[1].evidence_ids: must_be_non_empty",
            }, {
                "code": "requirement_relation_mismatch",
                "json_pointer": "$.requirements[1]",
                "raw_error": "requirements_not_referenced_by_clause_review:1",
                "relation_category": "missing_clause_relation",
            }],
        )
        self.assertIsNotNone(repaired)
        self.assertEqual(len(repaired["requirements"]), 1)
        self.assertEqual(repaired["requirements"][0]["role"], "declarations")
        self.assertEqual(
            repairs[-1]["rule_id"],
            "remove_unbound_empty_requirement_placeholder_v1",
        )
        self.assertEqual(repairs[-1]["removed_requirement_indexes"], [1])

        for field, value in (("clause_ids", ["C2"]), ("evidence_ids", ["E2"]), ("reason", "有语义")):
            response["requirements"][1][field] = value
            rejected, _ = bridge._apply_safe_mechanical_repairs(
                response,
                [{
                    "code": "empty_requirement_properties",
                    "json_pointer": "$.requirements[1].properties",
                    "raw_error": "must_include_semantic_payload",
                }],
            )
            self.assertIsNone(rejected)
            response["requirements"][1][field] = [] if field != "reason" else ""

    def test_mechanical_repair_never_removes_mixed_or_noninformational_relations(self) -> None:
        cases = [
            (
                [{"role": "body_text", "properties": {"text": "混合"}, "clause_ids": ["C1", "C2"]}],
                [
                    {"clause_id": "C1", "classification": "informational"},
                    {"clause_id": "C2", "classification": "executable"},
                ],
            ),
            (
                [{"role": "body_text", "properties": {"text": "外部"}, "clause_ids": ["C1"]}],
                [{"clause_id": "C1", "classification": "external_compliance"}],
            ),
            (
                [{"role": "body_text", "properties": {"text": "缺审查"}, "clause_ids": ["C1"]}],
                [],
            ),
        ]
        for requirements, reviews in cases:
            response = {"requirements": requirements, "clause_reviews": reviews}
            repaired, repairs = bridge._apply_safe_mechanical_repairs(
                response,
                [{
                    "code": "informational_requirement_forbidden",
                    "requirement_index": 0,
                }],
            )
            self.assertIsNone(repaired)
            self.assertEqual(repairs, [])

    def test_mechanical_repair_normalizes_explicitly_optional_continuation_caption(self) -> None:
        response = {
            "requirements": [{
                "role": "table",
                "properties": {"continuation": {
                    "caption_suffix": "(续)",
                    "repeat_header_row": True,
                    "caption_required_on_continuation": True,
                    "verification": "word_render",
                }},
                "clause_ids": ["C00243"],
                "evidence_ids": ["E00174"],
            }],
            "clause_reviews": [{"clause_id": "C00243", "classification": "executable"}],
        }
        chunk = {"clauses": [{
            "id": "C00243",
            "text": "表序后跟表题(可省略)和“(续)”，居中置于表上方，续表均应重复表头",
        }]}
        repaired, repairs = bridge._apply_safe_mechanical_repairs(
            response,
            [{
                "code": "partial_clause_coverage",
                "json_pointer": "$.clause_reviews[0]",
                "raw_error": "$.clause_reviews[0]: partial_clause_coverage:table.continuation.caption_optional",
            }],
            chunk=chunk,
        )
        self.assertIsNotNone(repaired)
        self.assertFalse(
            repaired["requirements"][0]["properties"]["continuation"]["caption_required_on_continuation"]
        )
        self.assertEqual(repairs[0]["rule_id"], "compile_continuation_caption_requirement_v1")
        self.assertTrue(response["requirements"][0]["properties"]["continuation"]["caption_required_on_continuation"])

    def test_bsu_chunk13_mixed_errors_replay_through_production_candidate_boundary(self) -> None:
        """Replay the captured BSU failure shape without a provider call.

        The real packet's two existing caption selectors are retained, while
        their native nullable properties are empty and the continuation
        requirement incorrectly says captions are required.  This exercises
        the same candidate preparation function used by run_host_agent_chunk.
        """
        clauses = [
            {
                "id": "C00243",
                "text": "表序后跟表题(可省略)和“(续)”，居中置于表上方，续表均应重复表头",
                "evidence_ids": ["E00174"], "source_kind": "paragraph",
                "location": {}, "part_index": 0,
            },
            {
                "id": "C00246",
                "text": "表题间空1个字距，居中置于表的上方",
                "evidence_ids": ["E00175"], "source_kind": "paragraph",
                "location": {}, "part_index": 0,
            },
        ]
        evidence = {"evidence": [
            {"id": "E00174", "text": "如某表需要转页接排时，在随后的各页上应重复表序。表序后跟表题(可省略)和“(续)”，居中置于表上方，续表均应重复表头。", "kind": "paragraph"},
            {"id": "E00175", "text": "说明3：表序与表题，表序即表的编号，由“表”和从“1”开始的阿拉伯数字组成；表题即表的名称，应简明，置于表序之后，表序和表题间空1个字距，居中置于表的上方。", "kind": "paragraph"},
        ]}
        clauses = self._bind_test_source_spans(clauses, evidence)
        existing = [
            {
                "id": "R00530", "role": "table_caption",
                "properties": {"position": "above", "paragraph": {"alignment": "center"}},
                "clause_ids": ["C00243"], "evidence_ids": ["E00174"],
                "source_text": clauses[0]["text"],
                "verification": {"mode": "static_docx", "checks": ["核对表题位置和对齐。"], "checker_ids": ["docx.property_receipts"]},
            },
            {
                "id": "R00531", "role": "table_caption",
                "properties": {"position": "above", "paragraph": {"alignment": "center"}},
                "clause_ids": ["C00246"], "evidence_ids": ["E00175"],
                "source_text": clauses[1]["text"],
                "verification": {"mode": "static_docx", "checks": ["核对表题位置和对齐。"], "checker_ids": ["docx.property_receipts"]},
            },
        ]
        request = engine.build_llm_request(
            [], clauses, evidence, {"requirements": existing}, "full",
            contract_version="3.0",
        )
        request = attach_request_provenance(
            request, source_sha256="b" * 64, evidence_doc=evidence,
            clauses=clauses, run_id="bsu-chunk13-offline-regression",
        )
        chunk = engine._build_host_review_chunks(
            request, clauses, evidence, "b" * 64, chunk_size=20,
        )[0]

        null_role_properties = {
            "font": None, "paragraph": None, "numbering": None,
            "position": None, "prefix": None, "separator": None,
            "style_hint": None, "text": None, "header_content": None,
            "bottom_border": None,
        }
        raw_response = {
            "contract_version": "3.0",
            "requirements": [
                {
                    "role": "table",
                    "properties": {
                        "style": None, "top_border_pt": None,
                        "header_border_pt": None, "bottom_border_pt": None,
                        "remove_vertical_borders": None, "repeat_header_row": None,
                        "allow_row_split": None, "keep_with_caption": None,
                        "border_widths_pt": None,
                        "continuation": {
                            "caption_suffix": "(续)",
                            "repeat_header_row": True,
                            "caption_required_on_continuation": True,
                            "verification": "word_render",
                        },
                    },
                    "clause_ids": ["C00243"], "evidence_ids": ["E00174"],
                    "confidence": 0.98,
                    "reason": "The continuation table properties are supported by the cited source.",
                    "verification": {
                        "mode": "word_render", "checks": ["核对续表属性。"],
                        "checker_ids": ["docx.property_receipts", "docx.word_render"],
                    },
                },
                {
                    "existing_requirement_id": "R00530", "role": "table_caption",
                    "properties": copy.deepcopy(null_role_properties),
                    "clause_ids": ["C00243"], "evidence_ids": ["E00174"],
                    "confidence": 0.98,
                    "reason": "The selected existing caption requirement is supported by this occurrence.",
                    "verification": copy.deepcopy(existing[0]["verification"]),
                },
                {
                    "existing_requirement_id": "R00531", "role": "table_caption",
                    "properties": copy.deepcopy(null_role_properties),
                    "clause_ids": ["C00246"], "evidence_ids": ["E00175"],
                    "confidence": 0.98,
                    "reason": "The selected existing caption requirement is supported by this occurrence.",
                    "verification": copy.deepcopy(existing[1]["verification"]),
                },
            ],
            "clause_reviews": [
                {
                    "clause_id": "C00243", "classification": "executable",
                    "normative_basis": "explicit_normative_text",
                    "reason": "The explicit continuation-table rules are represented by linked requirements.",
                    "obligations": [
                        {"id": "suffix", "status": "covered", "reason": "The continuation suffix is represented."},
                        {"id": "header", "status": "covered", "reason": "The repeated header is represented."},
                        {"id": "caption", "status": "covered", "reason": "The caption position is represented."},
                    ],
                },
                {
                    "clause_id": "C00246", "classification": "executable",
                    "normative_basis": "explicit_normative_text",
                    "reason": "The explicit caption placement rule is represented by a linked requirement.",
                    "obligations": [
                        {"id": "caption-placement", "status": "covered", "reason": "The caption position is represented."},
                    ],
                },
            ],
            "unsupported_items": [], "reported_conflicts": [],
        }
        raw_before = copy.deepcopy(raw_response)
        normalized = bridge.normalize_native_response(raw_response, chunk["response_schema"])
        self.assertEqual(normalized["requirements"][1]["properties"], {})
        self.assertEqual(normalized["requirements"][2]["properties"], {})
        self.assertEqual(raw_response, raw_before, "normalization must not mutate captured raw response")

        candidate, audit = bridge.prepare_native_response_candidate(raw_response, chunk)
        self.assertEqual(
            [item["existing_requirement_id"] for item in audit["existing_requirement_payload_projections"]],
            ["R00530", "R00531"],
        )
        self.assertEqual(
            candidate["requirements"][1]["properties"], existing[0]["properties"],
        )
        self.assertEqual(
            candidate["requirements"][2]["properties"], existing[1]["properties"],
        )
        self.assertFalse(
            candidate["requirements"][0]["properties"]["continuation"][
                "caption_required_on_continuation"
            ]
        )
        self.assertEqual(audit["mechanical_repair_revalidation"]["status"], "passed")
        self.assertEqual(audit["mechanical_repairs"][0]["planner_id"], "composable_source_bound_patch_plan_v1")
        self.assertEqual(bridge.validate_host_agent_response(candidate, chunk), [])
        self.assertEqual(raw_response, raw_before, "candidate preparation must preserve the original response")

    def test_mechanical_repair_compiles_explicit_empty_table_caption_properties(self) -> None:
        response = {
            "requirements": [
                {
                    "role": "table",
                    "properties": {"continuation": {"caption_suffix": "(续)"}},
                    "clause_ids": ["C1"],
                    "evidence_ids": ["E1"],
                },
                {
                    "role": "table_caption",
                    "properties": {},
                    "clause_ids": ["C1"],
                    "evidence_ids": ["E1"],
                },
                {
                    "role": "table_caption",
                    "properties": {},
                    "clause_ids": ["C2"],
                    "evidence_ids": ["E2"],
                },
            ],
            "clause_reviews": [
                {"clause_id": "C1", "classification": "executable"},
                {"clause_id": "C2", "classification": "executable"},
            ],
        }
        chunk = {
            "clauses": [
                {"id": "C1", "text": "表序后跟表题，居中置于表上方"},
                {"id": "C2", "text": "表题居中置于表的上方"},
            ],
        }
        repaired, repairs = bridge._apply_safe_mechanical_repairs(
            response,
            [
                {
                    "code": "empty_requirement_properties",
                    "json_pointer": "$.requirements[1].properties",
                },
                {
                    "code": "empty_requirement_properties",
                    "json_pointer": "$.requirements[2].properties",
                },
                {
                    "code": "partial_clause_coverage",
                    "json_pointer": "$.clause_reviews[0]",
                    "raw_error": (
                        "$.clause_reviews[0]: partial_clause_coverage:"
                        "table_caption.position:above,table_caption.paragraph.alignment:center"
                    ),
                },
                {
                    "code": "partial_clause_coverage",
                    "json_pointer": "$.clause_reviews[1]",
                    "raw_error": (
                        "$.clause_reviews[1]: partial_clause_coverage:"
                        "table_caption.paragraph.alignment:center"
                    ),
                },
            ],
            chunk=chunk,
        )
        self.assertIsNotNone(repaired)
        assert repaired is not None
        self.assertEqual(
            repaired["requirements"][1]["properties"],
            {"position": "above", "paragraph": {"alignment": "center"}},
        )
        self.assertEqual(
            repaired["requirements"][2]["properties"],
            {"position": "above", "paragraph": {"alignment": "center"}},
        )
        self.assertEqual(
            {item["rule_id"] for item in repairs},
            {
                "compile_explicit_table_caption_position_v1",
                "compile_explicit_table_caption_alignment_v1",
            },
        )

        unrelated = json.loads(json.dumps(response, ensure_ascii=False))
        unrelated["requirements"][1]["clause_ids"] = ["C3"]
        rejected, _ = bridge._apply_safe_mechanical_repairs(
            unrelated,
            [
                {
                    "code": "empty_requirement_properties",
                    "json_pointer": "$.requirements[1].properties",
                },
                {
                    "code": "partial_clause_coverage",
                    "json_pointer": "$.clause_reviews[0]",
                    "raw_error": (
                        "$.clause_reviews[0]: partial_clause_coverage:"
                        "table_caption.position:above"
                    ),
                },
            ],
            chunk=chunk,
        )
        self.assertIsNone(rejected)

    def test_fixed_declaration_candidate_is_derived_from_exact_chunk_evidence(self) -> None:
        packet = bridge.compact_model_packet({
            "contract_version": "3.0",
            "clauses": [
                {"id": "C1", "text": "学位论文使用授权书", "evidence_ids": ["E1"]},
                {"id": "C2", "text": "本人同意提交电子版", "evidence_ids": ["E2"]},
                {"id": "C3", "text": "学位论文作者暨授权人签字", "evidence_ids": ["E3"]},
                {"id": "C4", "text": "摘要", "evidence_ids": ["E4"]},
            ],
            "evidence_context": {
                "E1": {"id": "E1", "kind": "paragraph", "text": "学位论文使用授权书"},
                "E2": {"id": "E2", "kind": "paragraph", "text": "本人同意提交电子版"},
                "E3": {"id": "E3", "kind": "paragraph", "text": "学位论文作者暨授权人签字"},
                "E4": {"id": "E4", "kind": "paragraph", "text": "摘要"},
            },
            "declaration_anchor_preference": "abstract_title_zh",
            "requirement_contract": {},
        })
        self.assertEqual(packet["fixed_declaration_candidates"][0]["clause_ids"], ["C1", "C2"])
        self.assertEqual(packet["fixed_declaration_candidates"][0]["before_role"], "abstract_title_zh")

    def test_non_public_declaration_heading_is_derived_from_exact_chunk_evidence(self) -> None:
        packet = bridge.compact_model_packet({
            "contract_version": "3.0",
            "clauses": [
                {"id": "C1", "text": "非公开学位论文标注说明", "evidence_ids": ["E1"]},
                {"id": "C2", "text": "根据北京体育大学有关规定，非公开学位论文须经批准方能标注。", "evidence_ids": ["E2"]},
                {"id": "C3", "text": "摘要", "evidence_ids": ["E3"]},
            ],
            "evidence_context": {
                "E1": {"id": "E1", "kind": "paragraph", "text": "非公开学位论文标注说明"},
                "E2": {"id": "E2", "kind": "paragraph", "text": "根据北京体育大学有关规定，非公开学位论文须经批准方能标注。"},
                "E3": {"id": "E3", "kind": "paragraph", "text": "摘要"},
            },
            "declaration_anchor_preference": "abstract_title_zh",
            "requirement_contract": {},
        })
        candidate = packet["fixed_declaration_candidates"][0]
        self.assertEqual(candidate["clause_ids"], ["C1", "C2"])
        self.assertEqual(candidate["heading_evidence_ids"], ["E1"])

    def test_unknown_property_is_removed_deterministically_without_semantic_retry(self) -> None:
        response = {
            "requirements": [{"properties": {"style": {"name": "bad"}, "text": "标题"}}],
        }
        repaired, repairs = bridge._apply_safe_mechanical_repairs(
            response,
            [{
                "code": "unknown_property",
                "json_pointer": "$.requirements[0].properties",
                "raw_error": "$.requirements[0].properties: unknown property 'style'",
            }],
        )
        self.assertEqual(repairs[0]["removed_property"], "style")
        self.assertIsNotNone(repaired)
        self.assertNotIn("style", repaired["requirements"][0]["properties"])
        self.assertEqual(repaired["requirements"][0]["properties"]["text"], "标题")
        self.assertIn("style", response["requirements"][0]["properties"])

    def test_retry_allows_exact_text_fill_after_unknown_property_removal(self) -> None:
        previous = {
            "contract_version": "3.0",
            "requirements": [{
                "role": "abstract_title_zh",
                "properties": {"style": "three_line"},
                "clause_ids": ["C1"],
                "evidence_ids": ["E1"],
            }],
            "clause_reviews": [{"clause_id": "C1", "classification": "executable"}],
            "unsupported_items": [],
        }
        current = json.loads(json.dumps(previous))
        current["requirements"][0]["properties"] = {"text": "摘  要"}
        records = [{
            "code": "unknown_property",
            "json_pointer": "$.requirements[0].properties",
            "raw_error": "unknown property 'style'",
        }]
        chunk = {
            "evidence_context": {"E1": {"text": "摘  要"}},
            "requirement_contract": {
                "role_properties_schema": {
                    "abstract_title_zh": {"$ref": "#/$defs/roleSpec"},
                },
            },
        }
        changed = bridge._retry_change_paths(previous, current)
        self.assertEqual(
            changed,
            [
                "$.requirements[0].properties.style",
                "$.requirements[0].properties.text",
            ],
        )
        self.assertTrue(bridge._retry_changes_allowed(
            records,
            changed,
            contract_version="3.0",
            previous_response=previous,
            current_response=current,
            chunk=chunk,
        ))
        current["requirements"][0]["properties"]["text"] = "猜测标题"
        changed = bridge._retry_change_paths(previous, current)
        self.assertFalse(bridge._retry_changes_allowed(
            records,
            changed,
            contract_version="3.0",
            previous_response=previous,
            current_response=current,
            chunk=chunk,
        ))

    def test_mixed_contract_errors_yield_only_a_partial_candidate(self) -> None:
        response = {"requirements": [{"properties": {"style": {}}}]}
        repaired, repairs = bridge._apply_safe_mechanical_repairs(
            response,
            [
                {
                    "code": "unknown_property",
                    "json_pointer": "$.requirements[0].properties",
                    "raw_error": "unknown property 'style'",
                },
                {
                    "code": "requirement_relation_mismatch",
                    "json_pointer": "$.requirements",
                    "raw_error": "requirements_not_referenced_by_clause_review:0",
                },
            ],
        )
        self.assertIsNotNone(repaired)
        self.assertEqual(repaired["requirements"][0]["properties"], {})
        self.assertEqual(response["requirements"][0]["properties"], {"style": {}})
        self.assertTrue(all(item["partial_candidate_only"] for item in repairs))
        self.assertTrue(all(item["planner_id"] == "composable_source_bound_patch_plan_v1" for item in repairs))

    def test_unbacked_evidence_id_is_removed_deterministically(self) -> None:
        response = {
            "requirements": [{
                "evidence_ids": ["E1", "E2"],
                "clause_ids": ["C1"],
            }],
        }
        repaired, repairs = bridge._apply_safe_mechanical_repairs(
            response,
            [{
                "code": "evidence_relation_mismatch",
                "json_pointer": "$.requirements[0].evidence_ids",
                "raw_error": "$.requirements[0].evidence_ids: not_backed_by_clause:E2",
            }],
        )
        self.assertEqual(repairs[0]["removed_evidence_id"], "E2")
        self.assertEqual(repaired["requirements"][0]["evidence_ids"], ["E1"])
        self.assertEqual(response["requirements"][0]["evidence_ids"], ["E1", "E2"])

    def test_empty_role_payload_is_filled_from_exact_evidence_text(self) -> None:
        response = {
            "requirements": [{
                "role": "abstract_title_zh", "evidence_ids": ["E1"],
                "properties": {},
            }],
        }
        repaired, repairs = bridge._apply_safe_mechanical_repairs(
            response,
            [{
                "code": "empty_requirement_properties",
                "json_pointer": "$.requirements[0].properties",
                "raw_error": "must_include_semantic_payload",
            }],
            chunk={
                "evidence_context": {
                    "E1": {"style_name": "heading 1", "text": "摘 要"},
                },
                "requirement_contract": {
                    "role_properties_schema": {
                        "abstract_title_zh": {"$ref": "#/$defs/roleSpec"},
                    },
                },
            },
        )
        self.assertEqual(repaired["requirements"][0]["properties"]["text"], "摘 要")
        self.assertEqual(repairs[0]["filled_property"], "text")
        self.assertNotIn("text", response["requirements"][0]["properties"])

    def test_exact_acknowledgments_limit_is_compiled_into_nested_payload(self) -> None:
        response = {
            "requirements": [{
                "role": "content_constraints",
                "properties": {},
                "clause_ids": ["C1"],
                "evidence_ids": ["E1"],
            }],
        }
        repaired, repairs = bridge._apply_safe_mechanical_repairs(
            response,
            [{
                "code": "empty_requirement_properties",
                "json_pointer": "$.requirements[0].properties",
                "raw_error": "must_include_semantic_payload",
            }],
            chunk={
                "clauses": [{
                    "id": "C1", "text": "字数一般不超过500字", "evidence_ids": ["E1"],
                }],
                "evidence_context": {
                    "E1": {"id": "E1", "text": "字数一般不超过500字"},
                },
                "requirement_contract": {},
            },
        )
        self.assertIsNotNone(repaired)
        self.assertEqual(
            repaired["requirements"][0]["properties"],
            {"acknowledgments": {"max_chars": 500}},
        )
        self.assertEqual(repairs[0]["rule_id"], "compile_exact_acknowledgments_max_chars_v1")

        response["requirements"][0]["clause_ids"] = ["C2"]
        rejected, _ = bridge._apply_safe_mechanical_repairs(
            response,
            [{
                "code": "empty_requirement_properties",
                "json_pointer": "$.requirements[0].properties",
                "raw_error": "must_include_semantic_payload",
            }],
            chunk={
                "clauses": [{"id": "C2", "text": "致谢内容应简短", "evidence_ids": ["E1"]}],
                "evidence_context": {"E1": {"id": "E1", "text": "致谢内容应简短"}},
                "requirement_contract": {},
            },
        )
        self.assertIsNone(rejected)

    def test_exact_appendix_page_break_is_compiled_into_declared_payload(self) -> None:
        response = {
            "requirements": [{
                "role": "appendices",
                "properties": {
                    "required_when_profile_has_appendices": None,
                    "label_style": None,
                    "label_prefix": None,
                    "page_break_each": None,
                    "per_appendix_title_required": None,
                },
                "clause_ids": ["C1"],
                "evidence_ids": ["E1"],
            }],
        }
        repaired, repairs = bridge._apply_safe_mechanical_repairs(
            response,
            [{
                "code": "empty_requirement_properties",
                "json_pointer": "$.requirements[0].properties",
                "raw_error": "must_include_semantic_payload",
            }],
            chunk={
                "clauses": [{
                    "id": "C1", "text": "附录放在正文之后另起页", "evidence_ids": ["E1"],
                }],
                "evidence_context": {
                    "E1": {"id": "E1", "text": "附录放在正文之后另起页"},
                },
                "requirement_contract": {},
            },
        )
        self.assertIsNotNone(repaired)
        self.assertEqual(
            repaired["requirements"][0]["properties"],
            {"page_break_each": True},
        )
        self.assertEqual(repairs[0]["rule_id"], "compile_exact_appendix_page_break_v1")

        response["requirements"][0]["clause_ids"] = ["C2"]
        rejected, _ = bridge._apply_safe_mechanical_repairs(
            response,
            [{
                "code": "empty_requirement_properties",
                "json_pointer": "$.requirements[0].properties",
                "raw_error": "must_include_semantic_payload",
            }],
            chunk={
                "clauses": [{"id": "C2", "text": "附录放在正文之后"}],
                "evidence_context": {"E1": {"id": "E1", "text": "附录放在正文之后"}},
                "requirement_contract": {},
            },
        )
        self.assertIsNone(rejected)

    def test_exact_appendix_label_title_is_compiled_into_declared_payload(self) -> None:
        response = {
            "requirements": [{
                "role": "appendices",
                "properties": {
                    "required_when_profile_has_appendices": None,
                    "label_style": None,
                    "label_prefix": None,
                    "page_break_each": None,
                    "per_appendix_title_required": None,
                },
                "clause_ids": ["C1"],
                "evidence_ids": ["E1"],
            }],
        }
        repaired, repairs = bridge._apply_safe_mechanical_repairs(
            response,
            [{
                "code": "empty_requirement_properties",
                "json_pointer": "$.requirements[0].properties",
                "raw_error": "must_include_semantic_payload",
            }],
            chunk={
                "clauses": [{
                    "id": "C1",
                    "text": "附录的序号用A，B，C，…系列，如附录A，附录B等，每个附录应有标题",
                    "evidence_ids": ["E1"],
                }],
                "evidence_context": {
                    "E1": {
                        "id": "E1",
                        "text": "附录的序号用A，B，C，…系列，如附录A，附录B等，每个附录应有标题",
                    },
                },
                "requirement_contract": {},
            },
        )
        self.assertIsNotNone(repaired)
        self.assertEqual(
            repaired["requirements"][0]["properties"],
            {"label_style": "alpha_upper", "per_appendix_title_required": True},
        )
        self.assertEqual(repairs[0]["rule_id"], "compile_exact_appendix_label_title_v1")

    def test_empty_administrative_cover_institution_uses_neutral_placeholder(self) -> None:
        response = {
            "requirements": [{
                "role": "cover",
                "properties": {
                    "institution": "",
                    "fields": [{"id": "title_zh"}],
                    "non_public_administration": None,
                    "missing_value_policy": "placeholder",
                    "missing_value_placeholder": "——",
                },
                "clause_ids": ["C1"],
                "evidence_ids": ["E1"],
            }],
        }
        repaired, repairs = bridge._apply_safe_mechanical_repairs(
            response,
            [{
                "code": "cover_institution_placeholder",
                "json_pointer": "$.requirements[0].properties",
            }, {
                "code": "cover_institution_placeholder",
                "json_pointer": "$.requirements[0].properties.institution",
            }],
        )
        self.assertIsNotNone(repaired)
        self.assertEqual(
            repaired["requirements"][0]["properties"]["institution"],
            "——",
        )
        self.assertEqual(
            repairs[0]["rule_id"],
            "compile_neutral_cover_institution_placeholder_v1",
        )

        response["requirements"][0]["properties"].pop("missing_value_placeholder")
        rejected, _ = bridge._apply_safe_mechanical_repairs(
            response,
            [{
                "code": "cover_institution_placeholder",
                "json_pointer": "$.requirements[0].properties.institution",
            }],
        )
        self.assertIsNone(rejected)

    def test_cover_security_marking_is_migrated_only_from_exact_linked_source(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _review_dir, chunk = self._packet(
                Path(td) / "requirements", contract_version="3.0",
            )
            clause_source = "封面密  级"
            evidence_source = "封面密  级："
            chunk["clauses"] = [{
                "id": "C1", "text": clause_source, "evidence_ids": ["E1"],
                "source_kind": "paragraph", "location": {}, "part_index": 0,
                "source_span": {
                    "evidence_id": "E1", "start_offset": 0,
                    "end_offset": len(clause_source), "text": clause_source,
                    "source_sha256": hashlib.sha256(evidence_source.encode("utf-8")).hexdigest(),
                },
            }]
            chunk["evidence_context"] = {
                "E1": {"id": "E1", "text": evidence_source},
            }
            contract_version = chunk["contract_version"]
            security_field = {
                "id": "security_marking", "label": "密  级：",
                "value_from": "thesis_profile.cover_metadata.security_marking",
                "display_policy": "if_present", "order": 3,
            }
            response = {
                "contract_version": contract_version,
                "provenance": chunk["provenance"],
                "requirements": [{
                    "role": "cover",
                    "properties": {
                        "institution": "",
                        "fields": [
                            {
                                "id": "classification_number", "label": "分类号：",
                                "value_from": "thesis_profile.cover_metadata.classification_number",
                                "display_policy": "required", "order": 1,
                            },
                            {
                                "id": "unit_code", "label": "学校代码：",
                                "value_from": "thesis_profile.cover_metadata.unit_code",
                                "display_policy": "if_present", "order": 2,
                            },
                            security_field,
                        ],
                        "non_public_administration": None,
                        "before_role": "document_start",
                        "missing_value_policy": "placeholder",
                        "missing_value_placeholder": "——",
                        "layout_id": "linear",
                    },
                    "clause_ids": ["C1"], "evidence_ids": ["E1"],
                    "confidence": 0.98,
                    "reason": "来源明确标注封面密级字段。",
                    "applicability": {"status": "always"},
                    "input_prerequisites": [],
                    "verification": {"mode": "word_render", "checks": ["核验封面字段"]},
                }],
                "clause_reviews": [{
                    "clause_id": "C1", "classification": "executable",
                    "reason": "来源明确规定封面密级字段。",
                    "normative_basis": "template_structure",
                }],
                "unsupported_items": [], "reported_conflicts": [],
            }
            if contract_version == "2.1":
                response["clause_reviews"][0]["requirement_indexes"] = [0]
            else:
                response["clause_reviews"][0]["obligations"] = [{
                    "id": "cover_security_label", "status": "covered",
                    "reason": "精确字段标签已由来源支持。",
                }]

            errors = bridge.validate_host_agent_response(response, chunk)
            self.assertTrue(errors)
            records = bridge.contract_error_records(errors, response=response, chunk=chunk)
            self.assertIn("cover_binding_violation", {item["code"] for item in records})
            repaired, audit = bridge._apply_safe_mechanical_repairs(
                response, records, chunk=chunk,
            )
            self.assertIsNotNone(repaired, repr(records))
            assert repaired is not None
            self.assertEqual(bridge.validate_host_agent_response(repaired, chunk), [])

            repaired_properties = repaired["requirements"][0]["properties"]
            self.assertEqual(repaired_properties["institution"], "——")
            self.assertEqual(
                repaired_properties["fields"],
                response["requirements"][0]["properties"]["fields"][:2],
            )
            admin = repaired_properties["non_public_administration"]
            self.assertEqual(admin["fields"], [security_field])
            self.assertEqual(admin["public_policy"], "blank")
            self.assertEqual(admin["source_region"], "cover")
            self.assertEqual(
                admin["applicability"]["conditions"],
                [{
                    "fact": "thesis_profile.security_level", "operator": "in",
                    "value": ["restricted", "classified"],
                }],
            )
            migration = next(
                item for item in audit
                if item.get("rule_id") == "source_bound_cover_security_marking_migration_v1"
            )
            self.assertEqual(migration["source_clause_ids"], ["C1"])
            self.assertEqual(migration["source_evidence_ids"], ["E1"])
            self.assertEqual(
                migration["authorized_changed_paths"],
                [
                    "$.requirements[0].properties.fields",
                    "$.requirements[0].properties.non_public_administration",
                ],
            )

            candidate, candidate_audit = bridge.prepare_native_response_candidate(
                response, chunk,
            )
            self.assertEqual(bridge.validate_host_agent_response(candidate, chunk), [])
            self.assertEqual(
                candidate_audit["mechanical_repairs"][0]["rule_id"],
                "source_bound_cover_security_marking_migration_v1",
            )

    def test_cover_security_marking_migration_fails_closed_without_exact_source_binding(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _review_dir, chunk = self._packet(Path(td) / "requirements")
            source = "封面设置密级字段"
            chunk["clauses"] = [{
                "id": "C1", "text": source, "evidence_ids": ["E1"],
                "source_kind": "paragraph", "location": {}, "part_index": 0,
            }]
            chunk["evidence_context"] = {"E1": {"id": "E1", "text": source}}
            response = {
                "contract_version": chunk["contract_version"],
                "requirements": [{
                    "role": "cover",
                    "properties": {
                        "institution": "——",
                        "fields": [{
                            "id": "security_marking", "label": "密级：",
                            "value_from": "thesis_profile.cover_metadata.security_marking",
                            "display_policy": "if_present", "order": 1,
                        }],
                        "non_public_administration": None,
                    },
                    "clause_ids": ["C1"], "evidence_ids": ["E1"],
                    "confidence": 0.9, "reason": "封面字段",
                }],
                "clause_reviews": [],
            }
            record = {
                "code": "cover_binding_violation",
                "json_pointer": "$.requirements[0].properties.fields[0]",
                "raw_error": "$.requirements[0].properties.fields[0]: administrative label '密级' must be declared under cover.non_public_administration, not ordinary cover.fields",
                "response_sha256": bridge._response_sha256(response),
            }
            self.assertIsNone(
                bridge._project_source_bound_cover_security_marking(
                    copy.deepcopy(response), record, chunk=chunk,
                    baseline_response_sha256=bridge._response_sha256(response),
                )
            )
            self.assertIsNone(
                bridge._apply_safe_mechanical_repairs(response, [record], chunk=chunk)[0]
            )

    def test_missing_admin_fields_become_bound_manual_review_without_guessing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _review_dir, chunk = self._packet(Path(td) / "requirements")
            chunk["clauses"] = [
                {"id": "C1", "text": "正文使用宋体", "evidence_ids": ["E1"]},
                {"id": "C2", "text": "非公开论文须经审批，公开论文该项为空白", "evidence_ids": ["E2"]},
            ]
            chunk["evidence_context"] = {
                "E1": {"id": "E1", "text": "正文使用宋体"},
                "E2": {"id": "E2", "text": "非公开论文须经审批，公开论文该项为空白"},
            }
            response = {
                "contract_version": "2.1",
                "requirements": [{
                    "role": "cover",
                    "properties": {
                        "institution": "——",
                        "fields": [],
                        "non_public_administration": {
                            "applicability": {
                                "status": "conditional",
                                "conditions": [{
                                    "fact": "thesis_profile.security_level",
                                    "operator": "in",
                                    "value": ["restricted", "classified"],
                                }],
                            },
                            "fields": [],
                            "public_policy": "blank",
                            "source_region": "E2",
                        },
                    },
                    "clause_ids": ["C1", "C2"],
                    "evidence_ids": ["E1", "E2"],
                    "confidence": 0.9,
                    "reason": "行政说明",
                    "verification": {"mode": "static_docx", "checks": ["检查"]},
                }, {
                    "role": "body_text",
                    "properties": {"font": {"cjk": "SimSun", "size_pt": 12}},
                    "clause_ids": ["C1"],
                    "evidence_ids": ["E1"],
                    "confidence": 0.9,
                    "reason": "正文格式",
                    "verification": {"mode": "static_docx", "checks": ["检查"]},
                }],
                "clause_reviews": [{
                    "clause_id": "C1", "classification": "executable",
                    "requirement_indexes": [1], "reason": "正文格式",
                    "obligations": [{
                        "id": "fixed_statement", "status": "covered",
                        "reason": "固定文本",
                    }],
                }, {
                    "clause_id": "C2", "classification": "executable",
                    "requirement_indexes": [0], "reason": "行政规则",
                    "obligations": [{
                        "id": "public_blank_policy", "status": "covered",
                        "reason": "公开论文留白",
                    }],
                }],
                "unsupported_items": [], "reported_conflicts": [],
            }
            records = [{
                "code": "contract_validation_error",
                "json_pointer": "$.requirements[0].properties",
                "raw_error": "must match at least one schema in anyOf",
            }, {
                "code": "non_public_administration_fields_missing",
                "json_pointer": "$.requirements[0].properties.non_public_administration.fields",
                "raw_error": "requires at least 1 items",
            }]
            repaired, repairs = bridge._apply_safe_mechanical_repairs(
                response, records, chunk=chunk,
            )
            self.assertIsNotNone(repaired)
            assert repaired is not None
            self.assertEqual(
                [item["clause_id"] for item in repaired["clause_reviews"]
                 if item["classification"] == "requires_source_content"],
                ["C2"],
            )
            self.assertEqual(repaired["clause_reviews"][1]["requirement_indexes"], [])
            self.assertEqual(repaired["requirements"][0]["clause_ids"], ["C1"])
            self.assertEqual(
                repairs[0]["rule_id"],
                "downgrade_unlabeled_admin_region_to_manual_review_v1",
            )
            self.assertEqual(bridge.validate_host_agent_response(repaired, chunk), [])

            chunk["clauses"][1]["text"] = "非公开论文需填写审批表编号和批准日期"
            chunk["evidence_context"]["E2"]["text"] = chunk["clauses"][1]["text"]
            rejected, _ = bridge._apply_safe_mechanical_repairs(
                response, records, chunk=chunk,
            )
            self.assertIsNone(rejected)

    def test_contract_error_records_name_missing_admin_fields_explicitly(self) -> None:
        response = {"requirements": [{
            "role": "cover",
            "properties": {"non_public_administration": {"fields": []}},
        }]}
        records = bridge.contract_error_records([
            "$.requirements[0].properties.non_public_administration.fields: requires at least 1 items",
        ], response=response, chunk={"clauses": []})
        self.assertEqual(records[0]["code"], "non_public_administration_fields_missing")
        self.assertTrue(records[0]["semantic_review_required"])

    def test_combined_mechanical_repairs_do_not_require_semantic_retry(self) -> None:
        response = {
            "contract_version": "3.0",
            "requirements": [
                {
                    "role": "cover_field_label",
                    "properties": {"style": "three_line", "continuation": None},
                    "clause_ids": ["C1"], "evidence_ids": ["E1"],
                },
                {
                    "role": "cover",
                    "properties": {
                        "institution": "",
                        "fields": [],
                        "missing_value_policy": "placeholder",
                        "missing_value_placeholder": "——",
                    },
                    "clause_ids": ["C2"], "evidence_ids": ["E2"],
                },
                {
                    "role": "declarations",
                    "properties": {"items": [{
                        "id": "authorization",
                        "source_evidence_ids": ["E3", "E3", "E4"],
                    }]},
                    "clause_ids": ["C3"], "evidence_ids": ["E3", "E4"],
                },
            ],
            "clause_reviews": [], "unsupported_items": [], "reported_conflicts": [],
        }
        records = [
            {
                "code": "unknown_property",
                "json_pointer": "$.requirements[0].properties",
                "raw_error": "unknown property 'style'",
            },
            {
                "code": "cover_institution_placeholder",
                "json_pointer": "$.requirements[1].properties",
            },
            {
                "code": "contract_validation_error",
                "json_pointer": "$.requirements[2].properties",
                "raw_error": "must match at least one schema in anyOf",
            },
            {
                "code": "contract_validation_error",
                "json_pointer": "$.requirements[2].properties.items[0].source_evidence_ids",
                "raw_error": "items must be unique",
            },
        ]
        repaired, repairs = bridge._apply_safe_mechanical_repairs(response, records)
        self.assertIsNotNone(repaired)
        assert repaired is not None
        self.assertNotIn("style", repaired["requirements"][0]["properties"])
        self.assertEqual(
            repaired["requirements"][1]["properties"]["institution"], "——",
        )
        self.assertEqual(
            repaired["requirements"][2]["properties"]["items"][0]["source_evidence_ids"],
            ["E3", "E4"],
        )
        self.assertTrue(any(item.get("rule_id") == "deduplicate_first_occurrence_source_evidence_ids_v1"
                            for item in repairs))

    def test_unknown_layout_properties_are_removed_then_exact_text_is_filled(self) -> None:
        response = {
            "requirements": [{
                "role": "cover_field_label", "evidence_ids": ["E1"],
                "properties": {
                    "style": "three_line",
                    "remove_vertical_borders": False,
                    "repeat_header_row": False,
                    "allow_row_split": False,
                    "keep_with_caption": False,
                },
            }],
        }
        records = [
            {
                "code": "unknown_property",
                "json_pointer": "$.requirements[0].properties",
                "raw_error": f"unknown property '{name}'",
            }
            for name in (
                "style", "remove_vertical_borders", "repeat_header_row",
                "allow_row_split", "keep_with_caption",
            )
        ]
        repaired, repairs = bridge._apply_safe_mechanical_repairs(
            response,
            records,
            chunk={
                "evidence_context": {"E1": {"text": "论文题目（中文）"}},
                "requirement_contract": {
                    "role_properties_schema": {
                        "cover_field_label": {"$ref": "#/$defs/roleSpec"},
                    },
                },
            },
        )
        self.assertEqual(
            repaired["requirements"][0]["properties"],
            {"text": "论文题目（中文）"},
        )
        self.assertEqual(
            [item["code"] for item in repairs],
            ["unknown_property"] * 5 + ["empty_requirement_properties"],
        )

    def test_english_presence_fact_is_compiled_only_from_explicit_clause(self) -> None:
        response = {
            "requirements": [{
                "role": "body_text",
                "properties": {"font": {"latin": "Times New Roman"}},
                "clause_ids": ["C1"],
                "applicability": {
                    "status": "conditional",
                    "conditions": [{
                        "fact": "English text appears in the thesis",
                        "operator": "present",
                        "value": None,
                    }],
                    "exceptions": None,
                },
            }],
        }
        repaired, repairs = bridge._apply_safe_mechanical_repairs(
            response,
            [{
                "code": "applicability_fact_namespace",
                "json_pointer": "$.requirements[0].applicability.conditions[0].fact",
                "raw_error": (
                    "$.requirements[0].applicability.conditions[0].fact: "
                    "does not match the registered fact namespace"
                ),
            }],
            chunk={
                "clauses": [{
                    "id": "C1",
                    "text": "论文中出现英文时需要使用Times New Roman字体",
                }],
            },
        )
        self.assertEqual(
            repaired["requirements"][0]["applicability"]["conditions"][0]["fact"],
            "source_inventory.english_text",
        )
        self.assertEqual(repairs[0]["rule_id"], "explicit_english_presence_times_new_roman")
        self.assertEqual(
            response["requirements"][0]["applicability"]["conditions"][0]["fact"],
            "English text appears in the thesis",
        )

    def test_english_presence_fact_is_not_guessed_for_other_conditions(self) -> None:
        response = {
            "requirements": [{
                "role": "body_text",
                "properties": {"font": {"latin": "Times New Roman"}},
                "clause_ids": ["C1"],
                "applicability": {
                    "status": "conditional",
                    "conditions": [{
                        "fact": "some prose condition",
                        "operator": "present",
                        "value": None,
                    }],
                },
            }],
        }
        repaired, repairs = bridge._apply_safe_mechanical_repairs(
            response,
            [{
                "code": "applicability_fact_namespace",
                "json_pointer": "$.requirements[0].applicability.conditions[0].fact",
                "raw_error": "does not match the registered fact namespace",
            }],
            chunk={"clauses": [{"id": "C1", "text": "正文采用小四号字体"}]},
        )
        self.assertIsNone(repaired)
        self.assertEqual(repairs, [])

    def test_bridge_retries_locally_rejected_contract_in_a_new_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, chunk = self._packet(Path(td) / "requirements")
            invalid = self._executable_response(chunk, invalid_verification=True)
            valid = self._executable_response(chunk)
            envelopes = [
                {"runId": "openclaw-run-contract-retry-1", "status": "ok",
                 "provider": "openai", "model": "gpt-5.6-luna",
                 "result": {"payloads": [{"text": json.dumps(invalid)}]}},
                {"runId": "openclaw-run-contract-retry-2", "status": "ok",
                 "provider": "openai", "model": "gpt-5.6-luna",
                 "result": {"payloads": [{"text": json.dumps(valid)}]}},
            ]
            response_out = Path(td) / "host-agent-response.json"
            fake_results = [
                subprocess.CompletedProcess(["openclaw"], 0, json.dumps(envelopes[0]), ""),
                subprocess.CompletedProcess(["openclaw"], 0, json.dumps(envelopes[1]), ""),
            ]
            with patch.object(bridge, "_run_command", side_effect=fake_results) as run:
                audit = bridge.run_bridge(
                    review_dir, response_out=response_out,
                    agent_id="main", timeout=1, max_attempts=2,
                    openclaw_bin="openclaw", model="openai/gpt-5.6-luna",
                )
            self.assertEqual(audit["status"], "merged")
            self.assertEqual(audit["chunk_runs"][0]["attempt"], 2)
            self.assertIn("local response contract validation failed",
                          audit["chunk_runs"][0]["attempt_failures"][0])
            retry_ledger = audit["chunk_runs"][0]["semantic_retry_authorizations"]
            self.assertEqual(
                retry_ledger["status"],
                "accepted_after_provenance_and_contract_validation",
            )
            self.assertEqual(
                retry_ledger["paths"][0]["path"],
                "$.requirements[0].verification",
            )
            self.assertTrue(retry_ledger["paths"][0]["validator_records"])
            second_prompt = Path(
                run.call_args_list[1].args[0][run.call_args_list[1].args[0].index("--message-file") + 1]
            ).read_text(encoding="utf-8")
            self.assertIn("local contract validation failed", second_prompt)

    def test_successful_retry_fails_closed_if_raw_response_artifact_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, chunk = self._packet(Path(td) / "requirements")
            invalid = self._executable_response(chunk, invalid_verification=True)
            valid = self._executable_response(chunk)
            envelopes = [
                {"runId": "openclaw-run-raw-integrity-1", "status": "ok",
                 "provider": "openai", "model": "gpt-5.6-luna",
                 "result": {"payloads": [{"text": json.dumps(invalid)}]}},
                {"runId": "openclaw-run-raw-integrity-2", "status": "ok",
                 "provider": "openai", "model": "gpt-5.6-luna",
                 "result": {"payloads": [{"text": json.dumps(valid)}]}},
            ]
            response_out = Path(td) / "host-agent-response.json"
            fake_results = [
                subprocess.CompletedProcess(["openclaw"], 0, json.dumps(envelopes[0]), ""),
                subprocess.CompletedProcess(["openclaw"], 0, json.dumps(envelopes[1]), ""),
            ]
            real_atomic_write_text = bridge.atomic_write_text

            def omit_second_attempt_raw(path: Path, text: str, **kwargs: object) -> None:
                if ".attempt-02.raw.json" in Path(path).name:
                    return
                real_atomic_write_text(path, text, **kwargs)

            with patch.object(bridge, "atomic_write_text", side_effect=omit_second_attempt_raw):
                with patch.object(bridge, "_run_command", side_effect=fake_results) as run:
                    with self.assertRaisesRegex(
                        bridge.RetryRawArtifactIntegrityError,
                        "current raw response artifact is missing",
                    ):
                        bridge.run_bridge(
                            review_dir, response_out=response_out,
                            agent_id="main", timeout=1, max_attempts=3,
                            openclaw_bin="openclaw", model="openai/gpt-5.6-luna",
                        )

            self.assertEqual(run.call_count, 2)
            self.assertFalse(response_out.exists())
            failure = json.loads((review_dir / "host-agent-run.json").read_text(encoding="utf-8"))
            self.assertEqual(failure["terminal_error"]["type"], "RetryRawArtifactIntegrityError")
            self.assertTrue(failure["primary_error"]["records"])
            self.assertTrue(any(
                record.get("code") == "retry_raw_artifact_missing"
                for record in failure["chunk_lifecycle"][0]["structured_error_records"]
            ))
            self.assertTrue(any(
                item.get("kind") == "retry_raw_artifact_integrity"
                for item in failure["secondary_errors"]
            ))
            self.assertEqual(
                failure["chunk_lifecycle"][0]["attempts"][-1]["retry_authorizing_error_records"],
                failure["primary_error"]["records"],
            )

    def test_retryable_independent_review_incompleteness_uses_bounded_primary_retry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, chunk = self._packet(Path(td) / "requirements")
            response = self._response(chunk)
            envelope = {
                "runId": "openclaw-run-obligation-retry", "status": "ok",
                "provider": "openai", "model": "gpt-5.6-luna",
                "result": {"payloads": [{"text": json.dumps(response)}]},
            }
            response_out = Path(td) / "host-agent-response.json"
            fake_results = [
                subprocess.CompletedProcess(["openclaw"], 0, json.dumps(envelope), ""),
                subprocess.CompletedProcess(["openclaw"], 0, json.dumps(envelope), ""),
            ]
            calls = 0

            def fail_independent_once(candidate, current_chunk, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    error = bridge.IndependentObligationReviewError(
                        "source obligation was not represented",
                    )
                    error.error_records = [{
                        "code": "independent_obligation_review_incomplete",
                        "clause_id": "C1", "baseline_classification": "informational",
                        "json_pointer": "$.clause_reviews[0].classification",
                        "missing_source_quotes": ["正文使用宋体"],
                        "candidate_response_sha256": bridge._response_sha256(candidate),
                    }]
                    error.independent_review_audit = {"status": "rejected", "audit_path": "rejected.json"}
                    error.retryable = True
                    raise error
                return self._fake_independent_review(candidate, current_chunk, **kwargs)

            self._independent_review_patch.stop()
            with patch.object(bridge, "_run_independent_obligation_coverage_review", side_effect=fail_independent_once), \
                    patch.object(bridge, "_run_command", side_effect=fake_results) as run:
                audit = bridge.run_bridge(
                    review_dir, response_out=response_out,
                    agent_id="main", timeout=1, max_attempts=2,
                    openclaw_bin="openclaw", model="openai/gpt-5.6-luna",
                )

            self.assertEqual(audit["status"], "merged")
            self.assertEqual(run.call_count, 2)
            self.assertEqual(calls, 2)
            self.assertEqual(audit["chunk_lifecycle"][0]["attempts"][0]["status"], "retrying")
            self.assertEqual(
                audit["chunk_lifecycle"][0]["attempts"][0]["error_records"][0]["code"],
                "independent_obligation_review_incomplete",
            )
    def test_retry_cannot_hide_a_semantic_classification_change(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, chunk = self._packet(Path(td) / "requirements")
            invalid = self._executable_response(chunk, invalid_verification=True)
            semantic_change = self._response(chunk)
            envelopes = [
                {"runId": "openclaw-run-semantic-retry-1", "status": "ok",
                 "provider": "openai", "model": "gpt-5.6-luna",
                 "result": {"payloads": [{"text": json.dumps(invalid)}]}},
                {"runId": "openclaw-run-semantic-retry-2", "status": "ok",
                 "provider": "openai", "model": "gpt-5.6-luna",
                 "result": {"payloads": [{"text": json.dumps(semantic_change)}]}},
            ]
            response_out = Path(td) / "host-agent-response.json"
            fake_results = [
                subprocess.CompletedProcess(["openclaw"], 0, json.dumps(envelopes[0]), ""),
                subprocess.CompletedProcess(["openclaw"], 0, json.dumps(envelopes[1]), ""),
            ]
            with patch.object(bridge, "_run_command", side_effect=fake_results):
                with self.assertRaisesRegex(ValueError, "semantic re-review"):
                    bridge.run_bridge(
                        review_dir, response_out=response_out,
                        agent_id="main", timeout=1, max_attempts=2,
                        openclaw_bin="openclaw", model="openai/gpt-5.6-luna",
                    )
            failure = json.loads((review_dir / "host-agent-run.json").read_text(encoding="utf-8"))
            self.assertFalse(failure["merged_response_written"])
            self.assertTrue(failure["structured_error_records"])
            self.assertEqual(
                failure["structured_error_records"][-1]["code"],
                "semantic_retry_change",
            )
            # The terminal structured record retains the independently
            # detected raw-to-raw semantic drift; the original contract error
            # remains separately attached as the primary retry authorizer.
            self.assertEqual(failure["primary_error"]["code"], "contract_validation_error")
            self.assertEqual(failure["primary_error"]["chunk_index"], 1)
            self.assertTrue(failure["primary_error"]["message"])
            self.assertTrue(any(
                record.get("code") == "schema_contract_violation"
                for record in failure["primary_error"]["records"]
            ))
            self.assertIn("semantic re-review", failure["terminal_error"]["message"])
            self.assertEqual(failure["secondary_errors"][0]["kind"], "retry_semantic_drift")
            self.assertEqual(failure["secondary_errors"][0]["chunk_index"], 1)
            lifecycle = failure["chunk_lifecycle"][0]
            self.assertTrue(lifecycle["secondary_retry_drift_failures"])
            self.assertIn(
                "semantic re-review",
                lifecycle["secondary_retry_drift_failures"][-1]["error"],
            )

    def test_bridge_binds_missing_native_provenance_and_preserves_raw_response(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, chunk = self._packet(Path(td) / "requirements")
            response = self._response(chunk)
            del response["provenance"]
            envelope = {
                "runId": "openclaw-run-provenance-bind",
                "status": "ok",
                "provider": "openai", "model": "gpt-5.6-luna",
                "result": {"payloads": [{"text": json.dumps(response)}]},
            }
            response_out = Path(td) / "host-agent-response.json"
            fake = subprocess.CompletedProcess(
                ["openclaw"], 0, json.dumps(envelope), "",
            )
            with patch.object(bridge, "_run_command", return_value=fake):
                audit = bridge.run_bridge(
                    review_dir, response_out=response_out,
                    agent_id="main", timeout=1, max_attempts=1,
                    openclaw_bin="openclaw", model="openai/gpt-5.6-luna",
                )
            self.assertTrue(response_out.exists())
            raw_responses = list(review_dir.glob("*.raw.json"))
            self.assertEqual(len(raw_responses), 1)
            raw = json.loads(raw_responses[0].read_text(encoding="utf-8"))
            self.assertNotIn("provenance", raw)
            merged = json.loads(response_out.read_text(encoding="utf-8"))
            full_request = json.loads(
                (review_dir / "llm-request.json").read_text(encoding="utf-8")
            )
            self.assertEqual(merged["provenance"], full_request["provenance"])
            self.assertEqual(
                audit["chunk_runs"][0]["provenance_binding"],
                "bridge_generated",
            )
            self.assertEqual(
                audit["chunk_runs"][0]["provenance_mismatch_fields"],
                [],
            )

    def test_bridge_rejects_conflicting_native_provenance_without_rebinding(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, chunk = self._packet(Path(td) / "requirements")
            response = self._response(chunk)
            response["provenance"] = dict(chunk["provenance"])
            response["provenance"]["request_sha256"] = "0" * 64
            envelope = {
                "runId": "openclaw-run-provenance-conflict",
                "status": "ok",
                "provider": "openai", "model": "gpt-5.6-luna",
                "result": {"payloads": [{"text": json.dumps(response)}]},
            }
            response_out = Path(td) / "host-agent-response.json"
            fake = subprocess.CompletedProcess(
                ["openclaw"], 0, json.dumps(envelope), "",
            )
            with patch.object(bridge, "_run_command", return_value=fake):
                with self.assertRaisesRegex(bridge.HostAgentProvenanceMismatch, "provenance conflict"):
                    bridge.run_bridge(
                        review_dir, response_out=response_out,
                        agent_id="main", timeout=1, max_attempts=2,
                        openclaw_bin="openclaw", model="openai/gpt-5.6-luna",
                    )
            self.assertFalse(response_out.exists())
            audit = json.loads((review_dir / "host-agent-run.json").read_text(encoding="utf-8"))
            self.assertEqual(audit["error_type"], "HostAgentProvenanceMismatch")
            self.assertFalse(audit["merged_response_written"])

    def test_semantic_retry_projection_rebinds_current_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td) / "requirements"
            clauses = [{
                "id": "C1", "text": "图题应简明并置于图序之后", "evidence_ids": ["E1"],
                "source_kind": "paragraph", "location": {}, "part_index": 0,
            }]
            evidence = {
                "evidence": [{"id": "E1", "text": clauses[0]["text"], "kind": "paragraph"}],
            }
            clauses = self._bind_test_source_spans(clauses, evidence)
            request = engine.build_llm_request(
                [], clauses, evidence, {}, "full", contract_version="3.0",
                runtime_context={"code_fingerprint_sha256": "f" * 64},
            )
            request = attach_request_provenance(
                request, source_sha256="a" * 64, evidence_doc=evidence,
                clauses=clauses, run_id="run-v3-provenance-retry",
            )
            engine.prepare_host_agent_review_packets(
                request, clauses, evidence, "a" * 64, directory, chunk_size=1,
            )
            chunk = json.loads(
                (directory / "llm-request-chunks.json").read_text(encoding="utf-8")
            )[0]

            def review(classification: str) -> dict:
                return {
                    "clause_id": "C1",
                    "classification": classification,
                    "obligations": [
                        {"id": "caption_after_number", "status": "covered", "reason": "位置已由 requirement 表示。"},
                        {"id": "concise_caption", "status": "unverifiable", "reason": "简明性需要人工判断。"},
                    ],
                    "reason": "当前证据已审查。",
                    "normative_basis": "explicit_normative_text",
                }

            first = {
                "contract_version": "3.0",
                "requirements": [{
                    "role": "figure_caption",
                    "properties": {"position": "below"},
                    "clause_ids": ["C1"],
                    "evidence_ids": ["E1"],
                    "confidence": 0.9,
                    "reason": "证据支持图题位置。",
                    "verification": {"mode": "static_docx", "checks": ["检查图题位置。"]},
                }],
                "clause_reviews": [review("executable")],
                "unsupported_items": [],
                "reported_conflicts": [],
            }
            second = {
                "contract_version": "3.0",
                "requirements": [],
                "clause_reviews": [review("unverifiable")],
                "unsupported_items": [],
                "reported_conflicts": [],
            }
            envelopes = [
                {"runId": "openclaw-v3-provenance-retry-1", "status": "ok",
                 "provider": "openai", "model": "gpt-5.6-luna",
                 "result": {"payloads": [{"text": json.dumps(first, ensure_ascii=False)}]}},
                {"runId": "openclaw-v3-provenance-retry-2", "status": "ok",
                 "provider": "openai", "model": "gpt-5.6-luna",
                 "result": {"payloads": [{"text": json.dumps(second, ensure_ascii=False)}]}},
            ]
            response_out = Path(td) / "host-agent-response.json"
            fake_results = [
                subprocess.CompletedProcess(["openclaw"], 0, json.dumps(envelopes[0]), ""),
                subprocess.CompletedProcess(["openclaw"], 0, json.dumps(envelopes[1]), ""),
            ]
            with patch.object(bridge, "_run_command", side_effect=fake_results):
                audit = bridge.run_bridge(
                    directory, response_out=response_out,
                    agent_id="main", timeout=1, max_attempts=2,
                    openclaw_bin="openclaw", model="openai/gpt-5.6-luna",
                )

            merged = json.loads(response_out.read_text(encoding="utf-8"))
            full_request = json.loads(
                (directory / "llm-request.json").read_text(encoding="utf-8")
            )
            self.assertEqual(merged["provenance"], full_request["provenance"])
            self.assertEqual(merged["clause_reviews"][0]["classification"], "unverifiable")
            self.assertEqual(merged["requirements"], [])
            self.assertEqual(audit["status"], "merged")
            self.assertEqual(audit["chunk_runs"][0]["provenance_binding"], "bridge_generated")

    def test_bridge_rejects_tampered_chunk_before_starting_native_agent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, _chunk = self._packet(Path(td) / "requirements")
            chunks_path = review_dir / "llm-request-chunks.json"
            chunks = json.loads(chunks_path.read_text(encoding="utf-8"))
            chunks[0]["clauses"][0]["text"] = "被篡改的正文"
            chunks_path.write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
            with patch.object(bridge, "_run_command") as run:
                with self.assertRaisesRegex(ValueError, "source projection"):
                    bridge.run_bridge(
                        review_dir, response_out=Path(td) / "host-agent-response.json",
                        agent_id="main", timeout=1, max_attempts=1,
                        openclaw_bin="openclaw", model="openai/gpt-5.6-luna",
                    )
            run.assert_not_called()

    def test_bridge_requires_declared_host_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, _chunk = self._packet(Path(td) / "requirements")
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "THESIS_FORGE_HOST_RUNTIME"):
                    bridge.run_bridge(review_dir, openclaw_bin="openclaw")

    def test_bridge_refuses_host_runtime_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, _chunk = self._packet(Path(td) / "requirements")
            with self.assertRaisesRegex(RuntimeError, "host runtime mismatch"):
                bridge.run_bridge(
                    review_dir, host_runtime="codex", openclaw_bin="openclaw",
                )

    def test_bridge_refuses_recent_parent_session_guess(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, _chunk = self._packet(Path(td) / "requirements")
            with self.assertRaisesRegex(RuntimeError, "refusing to select a recent"):
                bridge.run_bridge(
                    review_dir, inherit_parent_model=True,
                    openclaw_bin="openclaw",
                )

    def test_bridge_refuses_unbound_gateway_default(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, _chunk = self._packet(Path(td) / "requirements")
            with self.assertRaisesRegex(RuntimeError, "explicit model route"):
                bridge.run_bridge(review_dir, openclaw_bin="openclaw")

    def test_failed_bridge_persists_failure_audit_without_merged_response(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, chunk = self._packet(Path(td) / "requirements")
            response = self._response(chunk)
            response["clause_reviews"][0]["requirement_indexes"] = [0]
            envelope = {
                "runId": "openclaw-run-final-contract-failure",
                "status": "ok",
                "provider": "openai", "model": "gpt-5.6-luna",
                "result": {"payloads": [{"text": json.dumps(response)}]},
            }
            response_out = Path(td) / "host-agent-response.json"
            fake = subprocess.CompletedProcess(
                ["openclaw"], 0, json.dumps(envelope), "",
            )
            with patch.object(bridge, "_run_command", return_value=fake):
                with self.assertRaises(ValueError):
                    bridge.run_bridge(
                        review_dir, response_out=response_out,
                        agent_id="main", timeout=1, max_attempts=1,
                        openclaw_bin="openclaw", model="openai/gpt-5.6-luna",
                    )
            failure = json.loads((review_dir / "host-agent-run.json").read_text(encoding="utf-8"))
            self.assertEqual(failure["status"], "failed")
            self.assertFalse(failure["merged_response_written"])
            self.assertFalse(response_out.exists())
            self.assertEqual(len(failure["chunk_lifecycle"]), 1)
            self.assertEqual(failure["chunk_lifecycle"][0]["status"], "failed")
            self.assertEqual(failure["chunk_lifecycle"][0]["attempts"][0]["status"], "failed")

    def test_bridge_calls_openclaw_without_delivery_and_merges_fresh_response(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, chunk = self._packet(Path(td) / "requirements")
            response = self._response(chunk)
            envelope = {
                "runId": "openclaw-run-test",
                "status": "ok",
                "provider": "openai", "model": "gpt-5.6-luna",
                "result": {"payloads": [{"text": json.dumps(response, ensure_ascii=False)}]},
            }
            response_out = Path(td) / "host-agent-response.json"
            fake = subprocess.CompletedProcess(
                ["openclaw"], 0, json.dumps(envelope), "",
            )
            with patch.object(bridge, "_run_command", return_value=fake) as run:
                audit = bridge.run_bridge(
                    review_dir, response_out=response_out,
                    agent_id="main", timeout=1, openclaw_bin="openclaw",
                    model="openai/gpt-5.6-luna", runner="gateway",
                )
            self.assertEqual(audit["status"], "merged")
            self.assertEqual(audit["chunk_count"], 1)
            self.assertEqual(json.loads(response_out.read_text(encoding="utf-8"))["contract_version"], "2.1")
            command = run.call_args.args[0]
            self.assertIn("--session-key", command)
            self.assertIn("--message-file", command)
            self.assertNotIn("--deliver", command)
            self.assertTrue((review_dir / "host-agent-run.json").is_file())

    def test_bridge_calls_native_codex_without_openclaw_route_or_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, chunk = self._packet(Path(td) / "requirements")
            response = self._response(chunk)
            events = "\n".join([
                json.dumps({"type": "thread.started", "thread_id": "codex-thread"}),
                json.dumps({
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": json.dumps(response)},
                }),
                json.dumps({"type": "turn.completed", "usage": {"output_tokens": 1}}),
            ])
            response_out = Path(td) / "host-agent-response.json"
            fake = subprocess.CompletedProcess(["codex", "exec"], 0, events, "")
            with patch.dict(os.environ, {"THESIS_FORGE_HOST_RUNTIME": "codex"}):
                with patch.object(bridge, "_run_command", return_value=fake) as run:
                    audit = bridge.run_bridge(
                        review_dir, response_out=response_out,
                        timeout=1, max_attempts=1,
                        codex_bin=sys.executable,
                        allow_prompt_only=True,
                    )
            command = run.call_args.args[0]
            self.assertEqual(command[1], "exec")
            self.assertIn("--json", command)
            self.assertIn("--sandbox", command)
            self.assertNotIn("--model", command)
            self.assertNotIn("openclaw", " ".join(command).lower())
            self.assertNotIn("--session-key", command)
            self.assertEqual(audit["adapter_id"], "codex")
            self.assertEqual(audit["route_visibility"], "unobservable")
            self.assertEqual(audit["observed_routes"], ["unobservable"])
            self.assertEqual(audit["chunk_runs"][0]["actual_route"], "unobservable")
            self.assertEqual(json.loads(response_out.read_text(encoding="utf-8"))["contract_version"], "2.1")

    def test_bridge_refuses_prompt_only_codex_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, _chunk = self._packet(Path(td) / "requirements")
            with patch.dict(os.environ, {"THESIS_FORGE_HOST_RUNTIME": "codex"}):
                with patch.object(bridge.codex_adapter, "probe_capabilities", return_value={
                    "output_schema_supported": False,
                    "structured_output_mode": "prompt_only",
                }):
                    with self.assertRaisesRegex(RuntimeError, "refusing prompt-only"):
                        bridge.run_bridge(
                            review_dir,
                            host_runtime="codex",
                            codex_bin=sys.executable,
                            max_attempts=1,
                        )

    def test_cli_does_not_inject_parent_inheritance_for_codex(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {"THESIS_FORGE_HOST_RUNTIME": "codex"}):
                with patch.object(bridge, "run_bridge", return_value={"status": "mocked"}) as run:
                    code = bridge.main([
                        td,
                        "--host-runtime", "codex",
                    ])
            self.assertEqual(code, 0)
            self.assertFalse(run.call_args.kwargs["inherit_parent_model"])
            self.assertIsNone(run.call_args.kwargs["codex_model"])

    def test_resolve_parent_model_copies_provider_and_model_override(self) -> None:
        parent_key = "agent:main:telegram:direct:chat:thread:38479"
        sessions = {
            "sessions": [{
                "key": parent_key,
                "agentId": "main",
                "kind": "direct",
                "modelOverride": "gpt-5.6-luna",
                "providerOverride": "openai",
                "updatedAt": 123,
            }],
        }
        fake = subprocess.CompletedProcess(
            ["openclaw", "sessions"], 0, json.dumps(sessions), "",
        )
        with patch.object(bridge, "_run_command", return_value=fake) as run:
            resolved = bridge.resolve_parent_model(
                "openclaw", agent_id="main", parent_session_key=parent_key,
            )
        self.assertEqual(resolved["model"], "openai/gpt-5.6-luna")
        self.assertEqual(resolved["source"], "parent-session-override")
        self.assertEqual(resolved["parent_model_override"], "gpt-5.6-luna")
        self.assertEqual(resolved["parent_provider_override"], "openai")
        self.assertEqual(run.call_args.args[0][0:3], ["openclaw", "sessions", "--json"])
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--agent") + 1], "main")

    def test_bridge_passes_inherited_parent_route_to_each_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, chunk = self._packet(Path(td) / "requirements")
            response = self._response(chunk)
            parent_key = "agent:main:telegram:direct:chat:thread:38479"
            sessions = {
                "sessions": [{
                    "key": parent_key,
                    "agentId": "main",
                    "kind": "direct",
                    "modelOverride": "gpt-5.6-luna",
                    "providerOverride": "openai",
                    "updatedAt": 123,
                }],
            }
            envelope = {
                "runId": "openclaw-run-inherit-test",
                "status": "ok",
                "result": {
                    "payloads": [{"text": json.dumps(response)}],
                    "meta": {"agentMeta": {
                        "provider": "openai", "model": "gpt-5.6-luna",
                    }},
                },
            }
            session_result = subprocess.CompletedProcess(
                ["openclaw", "sessions"], 0, json.dumps(sessions), "",
            )
            agent_result = subprocess.CompletedProcess(
                ["openclaw", "agent"], 0, json.dumps(envelope), "",
            )
            response_out = Path(td) / "host-agent-response.json"

            def fake_run(command, **kwargs):
                if "sessions" in command:
                    return session_result
                return agent_result

            with patch.object(bridge, "_run_command", side_effect=fake_run) as run:
                audit = bridge.run_bridge(
                    review_dir, response_out=response_out,
                    agent_id="main", timeout=1, openclaw_bin="openclaw",
                    inherit_parent_model=True, parent_session_key=parent_key,
                    auth_env_only=True,
                )
            command = next(
                call.args[0] for call in run.call_args_list if "agent" in call.args[0]
            )
            self.assertEqual(command[command.index("--model") + 1], "openai/gpt-5.6-luna")
            self.assertIn("--auth-env-only", command)
            self.assertEqual(audit["model"], "openai/gpt-5.6-luna")
            self.assertEqual(audit["model_source"], "parent-session-override")
            self.assertEqual(audit["chunk_runs"][0]["actual_route"], "openai/gpt-5.6-luna")

    def test_bridge_can_use_gateway_runner_without_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, chunk = self._packet(Path(td) / "requirements")
            response = self._response(chunk)
            envelope = {
                "runId": "openclaw-run-gateway-test",
                "status": "ok",
                "result": {
                    "payloads": [{"text": json.dumps(response)}],
                    "meta": {"agentMeta": {
                        "provider": "openai", "model": "gpt-5.6-luna",
                    }},
                },
            }
            response_out = Path(td) / "host-agent-response.json"
            fake = subprocess.CompletedProcess(
                ["openclaw", "agent"], 0, json.dumps(envelope), "",
            )
            with patch.object(bridge, "_run_command", return_value=fake) as run:
                audit = bridge.run_bridge(
                    review_dir, response_out=response_out,
                    agent_id="main", timeout=1, openclaw_bin="openclaw",
                    model="openai/gpt-5.6-luna", runner="gateway",
                )
            command = run.call_args.args[0]
            self.assertIn("--agent", command)
            self.assertIn("--session-key", command)
            self.assertIn("--model", command)
            self.assertNotIn("--deliver", command)
            session_key = command[command.index("--session-key") + 1]
            self.assertTrue(session_key.startswith("agent:main:thesis-host-agent:"))
            self.assertEqual(audit["runner"], "gateway")
            self.assertEqual(audit["chunk_runs"][0]["runner"], "gateway-agent")

    def test_resolve_parent_model_uses_effective_route_without_override(self) -> None:
        parent_key = "agent:main:telegram:direct:chat:thread:38479"
        sessions = {
            "sessions": [{
                "key": parent_key,
                "agentId": "main",
                "kind": "direct",
                "modelProvider": "sub2api",
                "model": "gpt-5.6-sol",
                "updatedAt": 123,
            }],
        }
        fake = subprocess.CompletedProcess(
            ["openclaw", "sessions"], 0, json.dumps(sessions), "",
        )
        with patch.object(bridge, "_run_command", return_value=fake):
            resolved = bridge.resolve_parent_model(
                "openclaw", agent_id="main", parent_session_key=parent_key,
            )
        self.assertEqual(resolved["model"], "sub2api/gpt-5.6-sol")
        self.assertEqual(resolved["source"], "parent-session-effective")
        self.assertEqual(resolved["parent_effective_provider"], "sub2api")
        self.assertEqual(resolved["parent_effective_model"], "gpt-5.6-sol")

    def test_resolve_parent_model_refuses_unresolved_effective_route(self) -> None:
        parent_key = "agent:main:telegram:direct:chat:thread:38479"
        sessions = {
            "sessions": [{
                "key": parent_key,
                "agentId": "main",
                "kind": "direct",
                "updatedAt": 123,
            }],
        }
        fake = subprocess.CompletedProcess(
            ["openclaw", "sessions"], 0, json.dumps(sessions), "",
        )
        with patch.object(bridge, "_run_command", return_value=fake):
            with self.assertRaisesRegex(ValueError, "refusing to use the gateway default"):
                bridge.resolve_parent_model(
                    "openclaw", agent_id="main", parent_session_key=parent_key,
                )

    def test_bridge_fails_closed_on_child_route_mismatch_without_retry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, chunk = self._packet(Path(td) / "requirements")
            response = self._response(chunk)
            parent_key = "agent:main:telegram:direct:chat:thread:38479"
            sessions = {
                "sessions": [{
                    "key": parent_key,
                    "agentId": "main",
                    "kind": "direct",
                    "modelProvider": "openai",
                    "model": "gpt-5.6-luna",
                    "updatedAt": 123,
                }],
            }
            envelope = {
                "runId": "openclaw-run-route-mismatch",
                "status": "ok",
                "provider": "sub2api",
                "model": "gpt-5.6-sol",
                "payloads": [{"text": json.dumps(response)}],
            }
            session_result = subprocess.CompletedProcess(
                ["openclaw", "sessions"], 0, json.dumps(sessions), "",
            )
            agent_result = subprocess.CompletedProcess(
                ["openclaw", "agent", "exec"], 0, json.dumps(envelope), "",
            )
            calls = []

            def fake_run(command, **kwargs):
                calls.append(command)
                return session_result if "sessions" in command else agent_result

            with patch.object(bridge, "_run_command", side_effect=fake_run) as run:
                with self.assertRaises(bridge.HostAgentRouteMismatch):
                    bridge.run_bridge(
                        review_dir,
                        response_out=Path(td) / "host-agent-response.json",
                        agent_id="main", timeout=1, max_attempts=2,
                        openclaw_bin="openclaw",
                        inherit_parent_model=True,
                        parent_session_key=parent_key,
                    )
            # One owned session-discovery call plus one child call; a route
            # mismatch must still not trigger a second child attempt.
            self.assertEqual(len(calls), 2)
            self.assertIn("sessions", calls[0])
            self.assertIn("agent", calls[1])
            self.assertFalse((Path(td) / "host-agent-response.json").exists())

    def test_bridge_refuses_existing_chunk_response_instead_of_reusing_it(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, chunk = self._packet(Path(td) / "requirements")
            response_name = json.loads(
                (review_dir / "host-agent-review-manifest.json").read_text(encoding="utf-8")
            )["response_files"][0]
            (review_dir / response_name).write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "refusing to reuse an existing chunk response"):
                bridge.run_bridge(
                    review_dir, response_out=Path(td) / "response.json", timeout=1,
                    model="openai/gpt-5.6-luna",
                )

    def test_bridge_retries_rejected_json_in_a_new_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, chunk = self._packet(Path(td) / "requirements")
            response = self._response(chunk)
            envelope = {
                "runId": "openclaw-run-retry-test",
                "status": "ok",
                "provider": "openai", "model": "gpt-5.6-luna",
                "result": {"payloads": [{"text": json.dumps(response, ensure_ascii=False)}]},
            }
            response_out = Path(td) / "host-agent-response.json"
            fake_results = [
                subprocess.CompletedProcess(["openclaw"], 0, "{broken", ""),
                subprocess.CompletedProcess(["openclaw"], 0, json.dumps(envelope), ""),
            ]
            with patch.object(bridge, "_run_command", side_effect=fake_results) as run:
                audit = bridge.run_bridge(
                    review_dir, response_out=response_out,
                    agent_id="main", timeout=1, max_attempts=2,
                    openclaw_bin="openclaw", model="openai/gpt-5.6-luna",
                    runner="gateway",
                )
            self.assertEqual(audit["status"], "merged")
            self.assertEqual(audit["chunk_runs"][0]["attempt"], 2)
            self.assertEqual(len(audit["chunk_runs"][0]["attempt_failures"]), 1)
            self.assertEqual(
                audit["chunk_runs"][0]["retry_stage_comparison"]["status"],
                "no_prior_semantic_response",
            )
            self.assertRegex(
                audit["chunk_runs"][0]["retry_stage_comparison"]["parent_raw_envelope_sha256"],
                r"^[0-9a-f]{64}$",
            )
            self.assertEqual(run.call_count, 2)
            first_command = run.call_args_list[0].args[0]
            second_command = run.call_args_list[1].args[0]
            first_key = first_command[first_command.index("--session-key") + 1]
            second_key = second_command[second_command.index("--session-key") + 1]
            self.assertNotEqual(first_key, second_key)
            self.assertIn("attempt-01", first_key)
            self.assertIn("attempt-02", second_key)

    def test_retry_compares_latest_decoded_response_across_intervening_parse_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, chunk = self._packet(Path(td) / "requirements")
            response = self._response(chunk)
            envelopes = [
                {
                    "runId": "openclaw-run-retry-chain-1", "status": "ok",
                    "provider": "openai", "model": "gpt-5.6-luna",
                    "result": {"payloads": [{"text": json.dumps(response)}]},
                },
                "{broken",
                {
                    "runId": "openclaw-run-retry-chain-3", "status": "ok",
                    "provider": "openai", "model": "gpt-5.6-luna",
                    "result": {"payloads": [{"text": json.dumps(response)}]},
                },
            ]
            fake_results = [
                subprocess.CompletedProcess(
                    ["openclaw"], 0,
                    json.dumps(item) if isinstance(item, dict) else item,
                    "",
                )
                for item in envelopes
            ]
            response_out = Path(td) / "host-agent-response.json"
            review_calls = 0

            def fail_independent_once(candidate, current_chunk, **kwargs):
                nonlocal review_calls
                review_calls += 1
                if review_calls == 1:
                    error = bridge.IndependentObligationReviewError(
                        "source obligation was not represented",
                    )
                    error.error_records = [{
                        "code": "independent_obligation_review_incomplete",
                        "clause_id": "C1", "baseline_classification": "informational",
                        "json_pointer": "$.clause_reviews[0].classification",
                        "missing_source_quotes": ["正文使用宋体"],
                        "candidate_response_sha256": bridge._response_sha256(candidate),
                    }]
                    error.independent_review_audit = {
                        "status": "rejected", "audit_path": "rejected.json",
                    }
                    error.retryable = True
                    raise error
                return self._fake_independent_review(candidate, current_chunk, **kwargs)

            self._independent_review_patch.stop()
            with patch.object(
                bridge, "_run_independent_obligation_coverage_review",
                side_effect=fail_independent_once,
            ), patch.object(bridge, "_run_command", side_effect=fake_results) as run:
                audit = bridge.run_bridge(
                    review_dir, response_out=response_out,
                    agent_id="main", timeout=1, max_attempts=3,
                    openclaw_bin="openclaw", model="openai/gpt-5.6-luna",
                )

            self.assertEqual(audit["status"], "merged")
            self.assertEqual(run.call_count, 3)
            self.assertEqual(review_calls, 2)
            successful_attempt = audit["chunk_runs"][0]
            self.assertEqual(successful_attempt["attempt"], 3)
            comparison = successful_attempt["retry_stage_comparison"]
            self.assertEqual(comparison["raw_parent_attempt"], 1)
            self.assertEqual(comparison["raw_candidate_attempt"], 3)
            self.assertEqual(comparison["authorization_parent_attempt"], 1)
            first_blocker = audit["chunk_lifecycle"][0]["attempts"][0]["error_records"]
            self.assertEqual(
                comparison["authorization_error_records_sha256"],
                bridge._response_sha256(first_blocker),
            )
            self.assertEqual(len(comparison["intervening_attempt_receipts"]), 1)
            self.assertEqual(comparison["intervening_attempt_receipts"][0]["attempt"], 2)
            self.assertEqual(
                comparison["intervening_attempt_receipts"][0]["kind"],
                "no_semantic_response",
            )

    def test_retry_will_not_continue_after_a_prior_decoded_raw_artifact_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, chunk = self._packet(Path(td) / "requirements")
            invalid = self._executable_response(chunk, invalid_verification=True)
            valid = self._response(chunk)
            envelopes = [
                {"runId": "openclaw-run-raw-chain-1", "status": "ok",
                 "provider": "openai", "model": "gpt-5.6-luna",
                 "result": {"payloads": [{"text": json.dumps(invalid)}]}},
                "{broken",
                {"runId": "openclaw-run-raw-chain-3", "status": "ok",
                 "provider": "openai", "model": "gpt-5.6-luna",
                 "result": {"payloads": [{"text": json.dumps(valid)}]}},
            ]
            response_name = json.loads(
                (review_dir / "host-agent-review-manifest.json").read_text(encoding="utf-8")
            )["response_files"][0]
            response_path = review_dir / response_name
            first_raw_path = response_path.with_name(
                f"{response_path.stem}.attempt-01.raw{response_path.suffix}"
            )
            fake_results = [
                subprocess.CompletedProcess(
                    ["openclaw"], 0,
                    json.dumps(item) if isinstance(item, dict) else item,
                    "",
                )
                for item in envelopes
            ]
            real_parse_result = bridge.openclaw_adapter.parse_result
            parse_calls = 0

            def remove_parent_after_retry_envelope_is_persisted(stdout: str):
                nonlocal parse_calls
                parse_calls += 1
                if parse_calls == 2:
                    first_raw_path.unlink()
                return real_parse_result(stdout)

            with patch.object(
                bridge.openclaw_adapter, "parse_result",
                side_effect=remove_parent_after_retry_envelope_is_persisted,
            ):
                with patch.object(bridge, "_run_command", side_effect=fake_results) as run:
                    with self.assertRaisesRegex(
                        bridge.RetryRawArtifactIntegrityError,
                        "decoded raw response artifact is missing",
                    ):
                        bridge.run_bridge(
                            review_dir,
                            response_out=Path(td) / "host-agent-response.json",
                            agent_id="main", timeout=1, max_attempts=3,
                            openclaw_bin="openclaw", model="openai/gpt-5.6-luna",
                        )

            self.assertEqual(run.call_count, 2)
            failure = json.loads((review_dir / "host-agent-run.json").read_text(encoding="utf-8"))
            self.assertEqual(failure["terminal_error"]["type"], "RetryRawArtifactIntegrityError")
            self.assertTrue(any(
                record.get("code") == "retry_raw_artifact_receipt_mismatch"
                for record in failure["chunk_lifecycle"][0]["structured_error_records"]
            ))
            self.assertTrue(any(
                record.get("code") == "schema_contract_violation"
                for record in failure["primary_error"]["records"]
            ), repr(failure["primary_error"]["records"]))

    def test_retry_will_not_dispatch_after_a_prior_decoded_raw_artifact_is_modified(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, chunk = self._packet(Path(td) / "requirements")
            invalid = self._executable_response(chunk, invalid_verification=True)
            valid = self._response(chunk)
            envelopes = [
                {"runId": "openclaw-run-raw-modified-1", "status": "ok",
                 "provider": "openai", "model": "gpt-5.6-luna",
                 "result": {"payloads": [{"text": json.dumps(invalid)}]}},
                {"runId": "openclaw-run-raw-modified-2", "status": "ok",
                 "provider": "openai", "model": "gpt-5.6-luna",
                 "result": {"payloads": [{"text": json.dumps(valid)}]}},
            ]
            response_name = json.loads(
                (review_dir / "host-agent-review-manifest.json").read_text(encoding="utf-8")
            )["response_files"][0]
            response_path = review_dir / response_name
            first_raw_path = response_path.with_name(
                f"{response_path.stem}.attempt-01.raw{response_path.suffix}"
            )
            fake_results = [
                subprocess.CompletedProcess(["openclaw"], 0, json.dumps(item), "")
                for item in envelopes
            ]
            real_validate_receipt = bridge._validate_retry_attempt_artifact
            validation_calls = 0

            def mutate_raw_before_retry_receipt_validation(
                path: Path, attempt_number: int, attempt_record: dict,
            ):
                nonlocal validation_calls
                validation_calls += 1
                if validation_calls == 1:
                    raw = json.loads(first_raw_path.read_text(encoding="utf-8"))
                    raw["clause_reviews"][0]["reason"] = "tampered after initial failure"
                    first_raw_path.write_text(json.dumps(raw), encoding="utf-8")
                return real_validate_receipt(path, attempt_number, attempt_record)

            review_calls = 0

            def count_review_calls(candidate, current_chunk, **kwargs):
                nonlocal review_calls
                review_calls += 1
                return self._fake_independent_review(candidate, current_chunk, **kwargs)

            self._independent_review_patch.stop()
            with patch.object(
                bridge, "_validate_retry_attempt_artifact",
                side_effect=mutate_raw_before_retry_receipt_validation,
            ), patch.object(
                bridge, "_run_independent_obligation_coverage_review",
                side_effect=count_review_calls,
            ):
                with patch.object(bridge, "_run_command", side_effect=fake_results) as run:
                    with self.assertRaisesRegex(
                        bridge.RetryRawArtifactIntegrityError,
                        "decoded raw response artifact hash differs from its receipt",
                    ):
                        bridge.run_bridge(
                            review_dir,
                            response_out=Path(td) / "host-agent-response.json",
                            agent_id="main", timeout=1, max_attempts=2,
                            openclaw_bin="openclaw", model="openai/gpt-5.6-luna",
                        )

            self.assertEqual(run.call_count, 1, "modified parent evidence must stop before retry dispatch")
            self.assertEqual(review_calls, 0, "tampered parent evidence must not reach independent review")
            failure = json.loads((review_dir / "host-agent-run.json").read_text(encoding="utf-8"))
            self.assertEqual(failure["terminal_error"]["type"], "RetryRawArtifactIntegrityError")
            self.assertTrue(any(
                record.get("code") == "retry_raw_artifact_receipt_mismatch"
                for record in failure["chunk_lifecycle"][0]["structured_error_records"]
            ))

    def test_retry_will_not_continue_after_a_prior_validated_candidate_is_tampered(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, chunk = self._packet(Path(td) / "requirements")
            valid = self._executable_response(chunk)
            envelopes = [
                {"runId": "openclaw-run-candidate-chain-1", "status": "ok",
                 "provider": "openai", "model": "gpt-5.6-luna",
                 "result": {"payloads": [{"text": json.dumps(valid)}]}},
                {"runId": "openclaw-run-candidate-chain-2", "status": "ok",
                 "provider": "openai", "model": "gpt-5.6-luna",
                 "result": {"payloads": [{"text": json.dumps(valid)}]}},
            ]
            response_name = json.loads(
                (review_dir / "host-agent-review-manifest.json").read_text(encoding="utf-8")
            )["response_files"][0]
            response_path = review_dir / response_name
            first_candidate_path = response_path.with_name(
                f"{response_path.stem}.attempt-01{response_path.suffix}"
            )
            fake_results = [
                subprocess.CompletedProcess(["openclaw"], 0, json.dumps(item), "")
                for item in envelopes
            ]
            real_validate_receipt = bridge._validate_retry_attempt_artifact
            validation_calls = 0

            def mutate_candidate_before_receipt_validation(
                path: Path, attempt_number: int, attempt_record: dict,
            ):
                nonlocal validation_calls
                validation_calls += 1
                if validation_calls == 1:
                    first_candidate_path.write_text("tampered candidate", encoding="utf-8")
                return real_validate_receipt(path, attempt_number, attempt_record)

            review_calls = 0

            def fail_first_independent_review(candidate, current_chunk, **kwargs):
                nonlocal review_calls
                review_calls += 1
                if review_calls == 1:
                    error = bridge.IndependentObligationReviewError(
                        "source obligation was not represented",
                    )
                    error.error_records = [{
                        "code": "independent_obligation_review_incomplete",
                        "clause_id": "C1",
                        "json_pointer": "$.clause_reviews[0]",
                    }]
                    error.retryable = True
                    raise error
                return self._fake_independent_review(candidate, current_chunk, **kwargs)

            self._independent_review_patch.stop()
            with patch.object(
                bridge, "_validate_retry_attempt_artifact",
                side_effect=mutate_candidate_before_receipt_validation,
            ), patch.object(
                bridge, "_run_independent_obligation_coverage_review",
                side_effect=fail_first_independent_review,
            ):
                with patch.object(bridge, "_run_command", side_effect=fake_results) as run:
                    with self.assertRaisesRegex(
                        bridge.RetryRawArtifactIntegrityError,
                        "validated candidate artifact hash differs from its receipt",
                    ):
                        bridge.run_bridge(
                            review_dir,
                            response_out=Path(td) / "host-agent-response.json",
                            agent_id="main", timeout=1, max_attempts=2,
                            openclaw_bin="openclaw", model="openai/gpt-5.6-luna",
                        )

            self.assertEqual(run.call_count, 1)
            self.assertEqual(review_calls, 1)
            failure = json.loads((review_dir / "host-agent-run.json").read_text(encoding="utf-8"))
            self.assertEqual(failure["terminal_error"]["type"], "RetryRawArtifactIntegrityError")
            self.assertTrue(any(
                record.get("code") == "retry_candidate_artifact_receipt_mismatch"
                for record in failure["chunk_lifecycle"][0]["structured_error_records"]
            ))

    def test_retry_refuses_a_mutated_raw_envelope_from_a_parse_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, chunk = self._packet(Path(td) / "requirements")
            valid = self._response(chunk)
            fake_results = [
                subprocess.CompletedProcess(["openclaw"], 0, "{broken", ""),
                subprocess.CompletedProcess(
                    ["openclaw"], 0,
                    json.dumps({
                        "runId": "openclaw-run-envelope-tamper", "status": "ok",
                        "provider": "openai", "model": "gpt-5.6-luna",
                        "result": {"payloads": [{"text": json.dumps(valid)}]},
                    }),
                    "",
                ),
            ]
            response_out = Path(td) / "host-agent-response.json"
            real_validate_receipt = bridge._validate_retry_attempt_artifact
            validation_calls = 0

            def mutate_envelope_before_receipt_validation(
                response_path: Path, attempt_number: int, attempt_record: dict,
            ):
                nonlocal validation_calls
                validation_calls += 1
                if validation_calls == 1:
                    envelope_path = response_path.with_name(
                        f"{response_path.stem}.attempt-01.raw-envelope.txt"
                    )
                    envelope_path.write_text("tampered after parse receipt", encoding="utf-8")
                return real_validate_receipt(response_path, attempt_number, attempt_record)

            with patch.object(
                bridge, "_validate_retry_attempt_artifact",
                side_effect=mutate_envelope_before_receipt_validation,
            ):
                with patch.object(bridge, "_run_command", side_effect=fake_results) as run:
                    with self.assertRaisesRegex(
                        bridge.RetryRawArtifactIntegrityError,
                        "raw envelope artifact hash differs from its receipt",
                    ):
                        bridge.run_bridge(
                            review_dir, response_out=response_out,
                            agent_id="main", timeout=1, max_attempts=2,
                            openclaw_bin="openclaw", model="openai/gpt-5.6-luna",
                        )

            self.assertEqual(run.call_count, 1)
            failure = json.loads((review_dir / "host-agent-run.json").read_text(encoding="utf-8"))
            self.assertEqual(failure["terminal_error"]["type"], "RetryRawArtifactIntegrityError")
            self.assertTrue(any(
                record.get("code") == "retry_raw_envelope_receipt_mismatch"
                for record in failure["chunk_lifecycle"][0]["structured_error_records"]
            ))

    def test_no_semantic_response_retry_receipt_requires_invocation_binding(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, chunk = self._packet(Path(td) / "requirements")
            response_path = review_dir / "llm-response-chunk-0001.json"
            envelope_path = response_path.with_name(
                f"{response_path.stem}.attempt-01.raw-envelope.txt"
            )
            envelope_path.write_text("provider response that did not decode", encoding="utf-8")
            binding = bridge._retry_input_fingerprints(chunk)
            error_record = {
                "code": "host_response_parse_error",
                "raw_envelope_path": str(envelope_path.resolve()),
                "raw_envelope_sha256": bridge.sha256_file(envelope_path),
            }
            attempt_record = {
                "retry_input_fingerprints": binding,
                "no_semantic_response_invocation_fingerprints": copy.deepcopy(binding),
                "error_records": [error_record],
            }
            receipt = bridge._validate_retry_attempt_artifact(
                response_path, 1, attempt_record,
            )
            self.assertEqual(receipt["kind"], "no_semantic_response")

            mismatched = copy.deepcopy(attempt_record)
            mismatched["no_semantic_response_invocation_fingerprints"]["run_id"] = "stale-run"
            with self.assertRaisesRegex(
                bridge.RetryRawArtifactIntegrityError,
                "raw envelope receipt does not match the attempt invocation fingerprints",
            ):
                bridge._validate_retry_attempt_artifact(response_path, 1, mismatched)

            missing_binding = copy.deepcopy(attempt_record)
            del missing_binding["no_semantic_response_invocation_fingerprints"]
            with self.assertRaisesRegex(
                bridge.RetryRawArtifactIntegrityError,
                "raw envelope receipt does not match the attempt invocation fingerprints",
            ):
                bridge._validate_retry_attempt_artifact(response_path, 1, missing_binding)

    def test_run_command_kills_and_reaps_the_process_group_on_timeout(self) -> None:
        controller = bridge.RunController()
        timeout_result = subprocess.CompletedProcess(
            ["openclaw"], 124, "partial stdout", "partial stderr\n[process-timeout] command exceeded 7s and was terminated",
        )
        with patch.object(bridge, "run_process", return_value=timeout_result) as run:
            with self.assertRaises(subprocess.TimeoutExpired) as raised:
                bridge._run_command(["openclaw"], timeout=7, controller=controller)
        run.assert_called_once_with(
            ["openclaw"], cwd=bridge.ROOT, timeout=7, controller=controller,
        )
        self.assertEqual(raised.exception.output, "partial stdout")
        self.assertIn("partial stderr", str(raised.exception.stderr))

    def test_generic_validator_path_does_not_authorize_retry_value_change(self) -> None:
        previous = {
            "contract_version": "3.0", "requirements": [], "unsupported_items": [],
            "reported_conflicts": [],
            "clause_reviews": [{
                "clause_id": "C1", "classification": "executable",
                "normative_basis": "explicit_normative_text",
            }],
        }
        current = copy.deepcopy(previous)
        current["clause_reviews"][0]["normative_basis"] = "template_structure"
        self.assertFalse(bridge._retry_changes_allowed(
            [{
                "code": "contract_validation_error",
                "json_pointer": "$.clause_reviews[0].normative_basis",
            }],
            ["$.clause_reviews[0].normative_basis"],
            contract_version="3.0", previous_response=previous,
            current_response=current,
        ))

    def test_completed_chunk_set_rejects_non_integer_and_duplicate_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            response = {"requirements": [], "clause_reviews": []}
            response_path = root / "accepted.json"
            response_path.write_text(json.dumps(response), encoding="utf-8")
            digest = bridge._response_sha256(response)
            lifecycle = {
                1: {"status": "completed", "remote_operation_state": "completed"},
                2: {"status": "completed", "remote_operation_state": "completed"},
            }
            audits = [
                {"chunk_index": 1, "accepted_response_sha256": digest, "response_path": str(response_path)},
                {"chunk_index": 1, "accepted_response_sha256": digest, "response_path": str(response_path)},
            ]
            with self.assertRaisesRegex(ValueError, "each chunk exactly once"):
                bridge._validate_completed_chunk_set(root, ["accepted.json", "accepted.json"], lifecycle, audits)
            audits[1]["chunk_index"] = []
            with self.assertRaisesRegex(ValueError, "each chunk exactly once"):
                bridge._validate_completed_chunk_set(root, ["accepted.json", "accepted.json"], lifecycle, audits)

    def test_cancellation_after_raw_response_never_publishes_accepted_response(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, chunk = self._packet(Path(td) / "requirements")
            response = self._response(chunk)
            envelope = {
                "runId": "openclaw-run-cancel-race",
                "status": "ok",
                "result": {"payloads": [{"text": json.dumps(response)}]},
            }

            class CancelAfterRaw(bridge.RunController):
                def __init__(self) -> None:
                    super().__init__()
                    self.check_count = 0

                def check(self) -> None:
                    self.check_count += 1
                    if self.check_count >= 3:
                        raise bridge.HostAgentCancelled("fatal sibling failure")

            controller = CancelAfterRaw()
            response_path = review_dir / "llm-response-chunk-0001.attempt-01.json"
            with patch.object(
                bridge, "_run_command",
                return_value=subprocess.CompletedProcess(
                    ["openclaw"], 0, json.dumps(envelope), ""
                ),
            ):
                with self.assertRaises(bridge.HostAgentCancelled):
                    bridge.run_host_agent_chunk(
                        request_path=review_dir / "llm-request.json",
                        chunk_path=review_dir / "llm-request-chunks.json",
                        chunk=chunk,
                        response_path=response_path,
                        run_id="run-bridge-test",
                        chunk_index=1,
                        chunk_count=1,
                        agent_id="main",
                        timeout=1,
                        openclaw_bin="openclaw",
                        prompt_path=review_dir / "prompt.txt",
                        controller=controller,
                    )
            self.assertFalse(response_path.exists())
            self.assertTrue(response_path.with_name(
                f"{response_path.stem}.raw{response_path.suffix}"
            ).exists())

    def test_parse_openclaw_result_accepts_one_json_fence(self) -> None:
        envelope = {
            "status": "ok",
            "result": {"payloads": [{"text": "```json\n{\"ok\": true}\n```"}]},
        }
        response, _ = bridge.parse_openclaw_result(json.dumps(envelope))
        self.assertEqual(response, {"ok": True})

    def test_parse_openclaw_exec_result_reads_root_payloads_and_route(self) -> None:
        envelope = {
            "status": "ok",
            "provider": "openai",
            "model": "gpt-5.6-luna",
            "payloads": [{"text": "{\"ok\": true}"}],
        }
        response, parsed = bridge.parse_openclaw_result(json.dumps(envelope))
        self.assertEqual(response, {"ok": True})
        route = bridge.verify_host_agent_route(parsed, "openai/gpt-5.6-luna")
        self.assertEqual(route["route"], "openai/gpt-5.6-luna")


if __name__ == "__main__":
    unittest.main()
