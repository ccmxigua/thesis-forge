"""Source-owned author work must not disappear during classification repair."""
from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import host_agent_bridge as bridge
import native_semantic_review as native
from test_host_agent_bridge import bind_mock_review_to_source_spans


class AuthoringCorrectionRoutingTests(unittest.TestCase):
    @staticmethod
    def fixture(classification="informational", mixed=True, source=None):
        source = source or "本部分主要撰写国内的研究现状，不能是文献资料的简单摘录，需要分类、总结、归纳"
        evidence = {"E-dynamic": {"id": "E-dynamic", "text": source}}
        clause = {
            "id": "dynamic-clause", "text": source, "evidence_ids": list(evidence),
            "source_span": {
                "evidence_id": "E-dynamic", "start_offset": 0, "end_offset": len(source),
                "text": source, "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
            },
        }
        provenance = {"run_id": "new-run", "source_sha256": "a" * 64,
                      "clause_sha256": "b" * 64, "evidence_sha256": "c" * 64,
                      "request_sha256": "d" * 64}
        chunk = {"clauses": [clause], "evidence_context": evidence,
                 "provenance": provenance, "case_id": "dynamic-case"}
        candidate = {"contract_version": "3.0", "provenance": provenance,
                     "requirements": [], "clause_reviews": [{
                         "clause_id": clause["id"], "classification": classification,
                         "reason": "Primary source interpretation.",
                     }]}
        obligations = [{"source_quote": source, "disposition": "authoring_content_pending",
                        "obligation_summary": "Write the research-status section.", "requirement_refs": []},
                       {"source_quote": source,
                        "disposition": "unrepresented" if mixed else "authoring_content_pending",
                        "obligation_summary": "Synthesize rather than copy the literature.", "requirement_refs": []}]
        result = {"check_id": clause["id"], "verdict": "incomplete",
                  "rationale": "The source duties are not covered by this candidate.",
                  "identified_obligations": obligations, "evidence_quotes": [source],
                  "machine_obligation_ids": []}
        return chunk, candidate, {"results": [result]}

    @staticmethod
    def run_review(candidate, chunk, output, response, calls):
        def reviewer(request, **kwargs):
            calls.append(copy.deepcopy(request))
            bound = bind_mock_review_to_source_spans(
                {"protocol": bridge.OBLIGATION_COVERAGE_PROTOCOL, "status": "completed",
                 "results": copy.deepcopy((response[len(calls) - 1] if isinstance(response, list)
                                            else response)["results"]), "summary": {}},
                request, kwargs["output_dir"],
            )
            native.validate_obligation_coverage_response(
                {"results": bound["results"]}, request["checks"],
            )
            bound["response_sha256"] = bridge.sha256_json({"results": bound["results"]})
            return bound
        with patch.object(bridge, "run_native_semantic_review", side_effect=reviewer), \
                patch.object(bridge.time, "sleep"):
            return bridge._run_independent_obligation_coverage_review(
                candidate, chunk, review_dir=Path(output), run_id="new-run", chunk_index=1,
                attempt=1, host_runtime="codex", model="gpt-5.6-luna", timeout=5,
                agent_id="main", runner="exec", binary="codex", config_path=None,
                controller=bridge.RunController(),
            )

    def test_mixed_inventory_routes_once_without_rewriting_or_claiming_success(self):
        for mixed in (False, True):
            chunk, candidate, response = self.fixture(mixed=mixed)
            original = copy.deepcopy((candidate, response))
            calls = []
            with self.subTest(mixed=mixed), tempfile.TemporaryDirectory() as td:
                with self.assertRaises(bridge.IndependentObligationReviewError) as caught:
                    self.run_review(candidate, chunk, td, response, calls)
                self.assertEqual(len(calls), 1)
                self.assertTrue(caught.exception.retryable)
                record = caught.exception.error_records[0]
                self.assertEqual(record["primary_retry_authorization"],
                                 "source_bound_authoring_content_reclassification_v1")
                self.assertEqual(len(record["missing_obligations"]), 2)
                ledger = json.loads(next(Path(td).rglob("obligation-analysis-ledger.json")).read_text())
                self.assertFalse(ledger["submission_ready"])
                self.assertEqual(len(ledger["obligations"]), 2)
                self.assertEqual([o["disposition"] for o in ledger["obligations"]],
                                 [o["disposition"] for o in response["results"][0]["identified_obligations"]])
                self.assertEqual((candidate, response), original)

    def test_after_primary_repair_unrepresented_duties_still_block_and_pending_is_kept(self):
        chunk, candidate, response = self.fixture("requires_source_content")
        calls = []
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(bridge.IndependentObligationReviewError) as caught:
                self.run_review(candidate, chunk, td, response, calls)
            self.assertEqual(len(calls), 2)
            self.assertFalse(caught.exception.retryable)
            record = caught.exception.error_records[0]
            self.assertIsNone(record["primary_retry_authorization"])
            self.assertEqual(len(record["identified_obligations"]), 2)
            self.assertEqual(len(record["missing_obligations"]), 2)
            self.assertEqual({o["disposition"] for o in record["missing_obligations"]},
                             {"authoring_content_pending", "unrepresented"})

    def test_corrected_pending_inventory_is_analysis_only_and_source_bound(self):
        chunk, candidate, response = self.fixture("requires_source_content", mixed=False)
        response["results"][0]["verdict"] = "source_content_pending"
        calls = []
        with tempfile.TemporaryDirectory() as td:
            pointer = self.run_review(candidate, chunk, td, response, calls)
            self.assertEqual(pointer["status"], "completed")
            self.assertEqual(len(calls), 1)
            ledger = json.loads(next(Path(td).rglob("obligation-analysis-ledger.json")).read_text())
            self.assertFalse(ledger["submission_ready"])
            self.assertEqual(ledger["candidate_response_sha256"], bridge._response_sha256(candidate))
            self.assertEqual(ledger["run_id"], "new-run")
            self.assertEqual(len({o["analysis_obligation_id"] for o in ledger["obligations"]}), 2)
            for obligation in ledger["obligations"]:
                self.assertEqual(obligation["disposition"], "authoring_content_pending")
                self.assertEqual(obligation["source_quote"], chunk["clauses"][0]["text"])
                self.assertEqual(obligation["requirement_refs"], [])

    def test_source_scope_stale_hash_and_adjacent_duties_never_authorize_primary_repair(self):
        chunk, candidate, response = self.fixture()
        check = native.build_obligation_coverage_request(candidate, chunk, run_id="new-run", chunk_index=1)["checks"][0]
        context = check["review_context"]
        missing = response["results"][0]["identified_obligations"]
        for key, value in (("primary_obligations", [{"status": "covered"}]),
                           ("machine_obligation_ids", ["layout-duty"]),
                           ("manual_review_codes", ["ambiguity"]),
                           ("source_content_verification_codes", ["verification"])):
            with self.subTest(key=key):
                self.assertFalse(bridge._v3_authoring_content_retry_is_source_bound(
                    {**context, key: value}, chunk["clauses"][0], chunk["evidence_context"], missing,
                ))
        stale = copy.deepcopy(chunk["clauses"][0])
        stale["source_span"]["source_sha256"] = "0" * 64
        self.assertFalse(bridge._v3_authoring_content_retry_is_source_bound(
            context, stale, chunk["evidence_context"], missing,
        ))
        quote = "本部分主要撰写国内的研究现状"
        for source in ("如果需要，" + quote, "示例：" + quote):
            bad_chunk, bad_candidate, bad_response = self.fixture(source=source)
            bad_check = native.build_obligation_coverage_request(
                bad_candidate, bad_chunk, run_id="new-run", chunk_index=1,
            )["checks"][0]
            bad_missing = [{"source_quote": quote, "disposition": "authoring_content_pending"}]
            self.assertFalse(bridge._v3_authoring_content_retry_is_source_bound(
                bad_check["review_context"], bad_chunk["clauses"][0], bad_chunk["evidence_context"], bad_missing,
            ))

    def test_mixed_inventory_cannot_be_promoted_by_verdict_or_refs(self):
        chunk, candidate, response = self.fixture()
        checks = native.build_obligation_coverage_request(candidate, chunk, run_id="new-run", chunk_index=1)["checks"]
        for verdict in ("consistent", "source_content_pending", "uncertain"):
            changed = copy.deepcopy(response)
            changed["results"][0]["verdict"] = verdict
            with self.subTest(verdict=verdict), self.assertRaises(native.NativeSemanticReviewError):
                native.validate_obligation_coverage_response(changed, checks)
        changed = copy.deepcopy(response)
        changed["results"][0]["identified_obligations"][0]["requirement_refs"] = ["forged-ref"]
        with self.assertRaises(native.NativeSemanticReviewError):
            native.validate_obligation_coverage_response(changed, checks)

    def test_incorrect_pending_verdict_has_bounded_feedback_not_fake_coverage(self):
        chunk, candidate, response = self.fixture("requires_source_content", mixed=False)
        calls = []
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(bridge.IndependentObligationReviewError) as caught:
                self.run_review(candidate, chunk, td, response, calls)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[1]["retry_feedback"]["code"], native.InconsistentObligationVerdictError.code)
            self.assertEqual(calls[1]["retry_feedback"]["clause_ids"], ["dynamic-clause"])
            self.assertFalse(caught.exception.retryable)
            self.assertEqual(caught.exception.error_records[0]["code"],
                             "independent_obligation_review_correction_exhausted")

    def test_successful_verdict_retry_preserves_candidate_and_complete_inventory(self):
        chunk, candidate, first = self.fixture("requires_source_content", mixed=False)
        second = copy.deepcopy(first)
        second["results"][0]["verdict"] = "source_content_pending"
        original = copy.deepcopy(candidate)
        calls = []
        with tempfile.TemporaryDirectory() as td:
            pointer = self.run_review(candidate, chunk, td, [first, second], calls)
            self.assertEqual(pointer["status"], "completed")
            self.assertEqual(pointer["provider_attempt"], 2)
            self.assertEqual(calls[0]["checks"], calls[1]["checks"])
            self.assertEqual(calls[1]["retry_feedback"]["code"], native.InconsistentObligationVerdictError.code)
            self.assertEqual(calls[1]["retry_feedback"]["clause_ids"], ["dynamic-clause"])
            ledger = json.loads((Path(td) / pointer["obligation_analysis_ledger_path"]).read_text())
            self.assertFalse(ledger["submission_ready"])
            self.assertEqual(len(ledger["obligations"]), 2)
            self.assertEqual(candidate, original)
            self.assertEqual(pointer["candidate_response_sha256"], bridge._response_sha256(candidate))
            self.assertEqual(len(pointer["provider_attempt_history"]), 1)

    def test_mixed_clauses_isolate_authoring_correction_and_preserve_font_gap(self):
        chunk, candidate, response = self.fixture(mixed=False)
        font_source = "论文中出现英文时需要使用Times New Roman字体"
        font_id, evidence_id = "another-dynamic-clause", "E-font-dynamic"
        chunk["evidence_context"][evidence_id] = {"id": evidence_id, "text": font_source}
        chunk["clauses"].append({
            "id": font_id, "text": font_source, "evidence_ids": [evidence_id],
            "source_span": {"evidence_id": evidence_id, "start_offset": 0,
                            "end_offset": len(font_source), "text": font_source,
                            "source_sha256": hashlib.sha256(font_source.encode()).hexdigest()},
        })
        candidate["clause_reviews"].append({"clause_id": font_id, "classification": "covered",
                                             "reason": "Primary claims font coverage."})
        candidate["requirements"].append({"role": "body_text", "properties": {"font": {"ascii": "Arial"}},
                                          "clause_ids": [font_id], "evidence_ids": [evidence_id]})
        response["results"].append({
            "check_id": font_id, "verdict": "incomplete", "rationale": "The English font is missing.",
            "identified_obligations": [{"source_quote": font_source, "disposition": "unrepresented",
                                        "obligation_summary": "Apply Times New Roman to English.",
                                        "requirement_refs": []}],
            "evidence_quotes": [font_source], "machine_obligation_ids": [],
        })
        original = copy.deepcopy(candidate)
        calls = []
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(bridge.IndependentObligationReviewError) as caught:
                self.run_review(candidate, chunk, td, response, calls)
            self.assertEqual(len(calls), 1)
            self.assertTrue(caught.exception.retryable)
            self.assertEqual([r["clause_id"] for r in caught.exception.error_records], ["dynamic-clause"])
            pointer = caught.exception.independent_review_audit
            plan_path = Path(td) / pointer["primary_repair_plan_path"]
            self.assertEqual(bridge.sha256_file(plan_path), pointer["primary_repair_plan_sha256"])
            plan = json.loads(plan_path.read_text())
            self.assertFalse(plan["submission_ready"])
            self.assertTrue(plan["requires_fresh_complete_review"])
            self.assertEqual([r["clause_id"] for r in plan["deferred_error_records"]], [font_id])
            ledger = json.loads((Path(td) / pointer["obligation_analysis_ledger_path"]).read_text())
            self.assertEqual(len(ledger["obligations"]), 3)
            self.assertEqual(candidate, original)
        # A classification change is not a font fix or a release authorization.
        corrected = copy.deepcopy(candidate)
        corrected["clause_reviews"][0]["classification"] = "requires_source_content"
        fresh = copy.deepcopy(response)
        fresh["results"][0]["verdict"] = "source_content_pending"
        calls = []
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(bridge.IndependentObligationReviewError) as caught:
                self.run_review(corrected, chunk, td, fresh, calls)
            self.assertEqual([r["clause_id"] for r in caught.exception.error_records], [font_id])
            self.assertTrue(all(r.get("primary_retry_authorization") !=
                                "source_bound_authoring_content_reclassification_v1"
                                for r in caught.exception.error_records))
            author_check = next(check for check in calls[0]["checks"]
                                if check["check_id"] == "dynamic-clause")
            self.assertEqual(author_check["review_context"]["classification"],
                             "requires_source_content")
            self.assertEqual(corrected["requirements"], original["requirements"])


if __name__ == "__main__":
    unittest.main()
