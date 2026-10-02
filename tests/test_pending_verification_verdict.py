"""An inconsistent human-pending verdict permits a reread, never a pass."""
from __future__ import annotations

import copy
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
import test_host_agent_bridge as fixtures


class PendingVerificationVerdictTests(unittest.TestCase):
    def fixture(self, directory):
        source = "对研究工作做出贡献的个人和集体，均已在论文中明确标明"
        _, chunk = fixtures.HostAgentBridgeTests()._packet(Path(directory), source=source, contract_version="3.0")
        candidate = {"contract_version": "3.0", "provenance": chunk["provenance"],
            "requirements": [{"role": "body_text", "properties": {"text": source},
                              "clause_ids": ["C1"], "evidence_ids": ["E1"], "reason": "Fixture declaration text.", "confidence": 1.0}],
            "clause_reviews": [{"clause_id": "C1", "classification": "covered", "reason": "Fixed text is represented.",
                                  "obligations": [{"id": "declared-body", "status": "covered", "reason": "Fixed source wording.",
                                      "actor": "preparer", "action": "include", "target": "declaration text", "source_quote": source,
                                      "force": "required", "applicability": "applicable", "route": "automatic"}]}],
            "unsupported_items": [], "reported_conflicts": []}
        request = native.build_obligation_coverage_request(candidate, chunk, run_id=chunk["provenance"]["run_id"], chunk_index=1)
        ref = request["checks"][0]["review_context"]["linked_requirements"][0]["requirement_ref"]
        review = {"status": "completed", "summary": {}, "results": [{"check_id": "C1", "verdict": "consistent",
            "rationale": "Fixed wording exists; the factual assertion remains unverified.",
            "evidence_quotes": [source], "machine_obligation_ids": [], "identified_obligations": [
                {"source_quote": source, "primary_obligation_id": "declared-body", "disposition": "represented", "requirement_refs": [ref],
                 "actor": "preparer", "action": "include", "target": "declaration text", "force": "required", "applicability": "applicable"},
                {"source_quote": source, "disposition": "source_content_verification_pending", "requirement_refs": [],
                 "actor": "author", "action": "verify", "target": "whether contributions are identified", "force": "required", "applicability": "applicable"}
            ]}]}
        return chunk, candidate, request, review

    def retry_request(self, candidate, request, review):
        prior = copy.deepcopy(request)
        prior.update(attempt=1, provider_attempt=1)
        current = copy.deepcopy(prior)
        current["provider_attempt"] = 2
        current["retry_feedback"] = {
            "code": native.PendingVerificationVerdictError.code, "clause_ids": ["C1"],
            "rejected_results": copy.deepcopy(review["results"]),
            "rejected_results_sha256": bridge.sha256_json(review["results"]),
            "rejected_request_sha256": bridge.sha256_json(prior),
            "checks_sha256": bridge.sha256_json(prior["checks"]),
            "candidate_response_sha256": bridge.sha256_json(candidate),
            "run_id": prior["run_id"], "provenance": copy.deepcopy(prior["provenance"]),
        }
        return current

    def test_retry_cannot_delete_change_add_or_reorder_human_duties(self):
        with tempfile.TemporaryDirectory() as td:
            _, candidate, request, mistaken = self.fixture(td)
            retry = self.retry_request(candidate, request, mistaken)
            self.assertTrue(native.pending_verification_retry_feedback_is_bound(retry))
            corrected = copy.deepcopy(mistaken)
            corrected["results"][0]["verdict"] = "source_content_verification_pending"
            native.validate_pending_verification_retry_result(corrected, retry)
            for mutation in ("delete", "change", "add", "reorder", "quotes", "machine", "consistent"):
                changed = copy.deepcopy(corrected)
                result = changed["results"][0]
                atoms = result["identified_obligations"]
                if mutation == "delete":
                    atoms.pop()
                    result["verdict"] = "consistent"
                    # The old stateless validator cannot remember the lost duty.
                    native.validate_obligation_coverage_response({"results": changed["results"]}, request["checks"])
                elif mutation == "change":
                    atoms[1]["target"] = "another factual question"
                elif mutation == "add":
                    atoms.append(copy.deepcopy(atoms[1]))
                elif mutation == "reorder":
                    atoms.reverse()
                elif mutation == "quotes":
                    result["evidence_quotes"] = []
                elif mutation == "machine":
                    result["machine_obligation_ids"] = ["forged"]
                else:
                    result["verdict"] = "consistent"
                with self.subTest(mutation=mutation), self.assertRaises(native.NativeSemanticReviewError):
                    native.validate_pending_verification_retry_result(changed, retry)

    def test_stale_or_forged_retry_authorization_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            _, candidate, request, mistaken = self.fixture(td)
            retry = self.retry_request(candidate, request, mistaken)
            for field in ("run_id", "provenance", "checks", "provider_attempt", "rejected_results", "rejected_request_sha256", "clause_ids"):
                forged = copy.deepcopy(retry)
                if field in ("rejected_results", "rejected_request_sha256", "clause_ids"):
                    forged["retry_feedback"][field] = [] if field != "rejected_request_sha256" else "0" * 64
                elif field == "provider_attempt":
                    forged[field] = 3
                elif field == "checks":
                    forged[field][0]["document_text"] += " changed"
                else:
                    forged[field] = "old run"
                with self.subTest(field=field):
                    self.assertFalse(native.pending_verification_retry_feedback_is_bound(forged))

    def test_bridge_rejects_inventory_loss_before_publishing_pointer(self):
        with tempfile.TemporaryDirectory() as td:
            chunk, candidate, _, mistaken = self.fixture(Path(td) / "packets")
            calls = []

            def reviewer(request, *, output_dir, **kwargs):
                calls.append(copy.deepcopy(request))
                result = copy.deepcopy(mistaken)
                if len(calls) == 2:
                    result["results"][0]["identified_obligations"].pop()
                bound = fixtures.bind_mock_review_to_source_spans(result, request, output_dir)
                native.validate_obligation_coverage_response({"results": bound["results"]}, request["checks"])
                return bound

            with patch.object(bridge, "run_native_semantic_review", side_effect=reviewer), patch.object(bridge.time, "sleep"):
                with self.assertRaises(bridge.IndependentObligationReviewError):
                    bridge._run_independent_obligation_coverage_review(candidate, chunk,
                        review_dir=Path(td) / "review", run_id=chunk["provenance"]["run_id"], chunk_index=1, attempt=1,
                        host_runtime="codex", model="gpt-6-luna", timeout=5, agent_id="main", runner="exec", binary="codex",
                        config_path=None, controller=bridge.RunController())
            self.assertEqual(len(calls), 2)

    def test_mixed_fixed_wording_and_truthfulness_cannot_be_consistent(self):
        with tempfile.TemporaryDirectory() as td:
            _, _, request, review = self.fixture(td)
            with self.assertRaises(native.PendingVerificationVerdictError):
                native.validate_obligation_coverage_response({"results": review["results"]}, request["checks"])
            self.assertEqual(review["results"][0]["verdict"], "consistent")
            corrected = copy.deepcopy(review)
            corrected["results"][0]["verdict"] = "source_content_verification_pending"
            out = native.validate_obligation_coverage_response({"results": corrected["results"]}, request["checks"])
            self.assertEqual(len(out[0]["identified_obligations"]), 2)
            self.assertEqual(out[0]["identified_obligations"][1]["disposition"], "source_content_verification_pending")
            for field, value in (("source_quote", "unrelated source"), ("requirement_refs", ["unrelated requirement"])):
                forged = copy.deepcopy(review)
                forged["results"][0]["identified_obligations"][1][field] = value
                with self.subTest(field=field), self.assertRaises(native.NativeSemanticReviewError) as caught:
                    native.validate_obligation_coverage_response({"results": forged["results"]}, request["checks"])
                self.assertNotIsInstance(caught.exception, native.PendingVerificationVerdictError)

    def test_bounded_rereview_preserves_candidate_and_pending_inventory(self):
        for succeeds in (True, False):
            with self.subTest(succeeds=succeeds), tempfile.TemporaryDirectory() as td:
                chunk, candidate, _, mistaken = self.fixture(Path(td) / "packets")
                original = copy.deepcopy(candidate)
                calls = []

                def reviewer(request, *, output_dir, **kwargs):
                    calls.append(copy.deepcopy(request))
                    result = copy.deepcopy(mistaken)
                    if succeeds and len(calls) == 2:
                        result["results"][0]["verdict"] = "source_content_verification_pending"
                    bound = fixtures.bind_mock_review_to_source_spans(result, request, output_dir)
                    native.validate_obligation_coverage_response({"results": bound["results"]}, request["checks"])
                    return bound

                with patch.object(bridge, "run_native_semantic_review", side_effect=reviewer), patch.object(bridge.time, "sleep"):
                    args = dict(review_dir=Path(td) / "review", run_id=chunk["provenance"]["run_id"], chunk_index=1, attempt=1,
                                host_runtime="codex", model="gpt-6-luna", timeout=5, agent_id="main", runner="exec", binary="codex",
                                config_path=None, controller=bridge.RunController())
                    if succeeds:
                        pointer = bridge._run_independent_obligation_coverage_review(candidate, chunk, **args)
                        self.assertEqual(pointer["status"], "completed")
                    else:
                        with self.assertRaises(bridge.IndependentObligationReviewError) as caught:
                            bridge._run_independent_obligation_coverage_review(candidate, chunk, **args)
                        self.assertFalse(caught.exception.retryable)
                self.assertEqual(len(calls), 2)
                self.assertEqual(candidate, original)
                self.assertEqual(calls[0]["checks"], calls[1]["checks"])
                self.assertEqual(calls[0]["provenance"], calls[1]["provenance"])
                feedback = calls[1]["retry_feedback"]
                self.assertEqual(feedback["code"], native.PendingVerificationVerdictError.code)
                self.assertEqual(feedback["clause_ids"], ["C1"])
                self.assertEqual(feedback["rejected_results"], mistaken["results"])
                self.assertTrue(native.pending_verification_retry_feedback_is_bound(calls[1]))
                self.assertEqual(feedback["candidate_response_sha256"], bridge.sha256_json(original))
                audit = json.loads((Path(td) / "review/independent-review-chunk-0001-attempt-01/coverage-audit.json").read_text())
                self.assertEqual(audit["status"], "rejected")
                self.assertTrue(audit["retryable"])
                self.assertEqual(audit["candidate_response_sha256"], bridge.sha256_json(original))
                prompt = native._prompt(calls[1])
                self.assertIn("same unchanged candidate", prompt)
                self.assertIn("does not prove its assertion true", prompt)


if __name__ == "__main__":
    unittest.main()
