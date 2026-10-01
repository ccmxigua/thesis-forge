"""Atomic matching and bounded independent corrections; never a pass projection."""
from __future__ import annotations

import copy
import itertools
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import native_semantic_review as native
import host_agent_bridge as bridge
from test_host_agent_bridge import bind_mock_review_to_source_spans


class SourceAtomReviewAlignmentTests(unittest.TestCase):
    def incident(self):
        return json.loads((ROOT / "tests/fixtures/publication-coverage-incident.json").read_text())

    def fixture(self):
        case = self.incident()
        provenance = {"run_id": "fresh-alignment-test", "source_sha256": "a" * 64,
                      "evidence_sha256": "b" * 64, "clause_sha256": "c" * 64,
                      "request_sha256": "d" * 64}
        chunk = {"clauses": [case["clause"]], "evidence_context": case["evidence_context"],
                 "provenance": provenance, "case_id": "dynamic-test-case"}
        candidate = {"contract_version": "3.0", "provenance": provenance,
                     "requirements": case["requirements"], "clause_reviews": [case["review"]]}
        return case, chunk, candidate

    def test_overlapping_quotes_use_complete_assignment_in_both_orders(self):
        case = self.incident()
        check = copy.deepcopy(case["check"])
        check["review_context"]["primary_obligations"] = []  # Test assignment independently of typed checks.
        atoms = case["independent_result"]["identified_obligations"]
        for order in itertools.permutations(atoms):
            response = {"results": [{**case["independent_result"], "identified_obligations": list(order)}]}
            self.assertEqual(native.validate_obligation_coverage_response(response, [check])[0]["verdict"], "consistent")
        for remaining in atoms:
            response = {"results": [{**case["independent_result"], "identified_obligations": [remaining]}]}
            with self.assertRaisesRegex(native.NativeSemanticReviewError, "separate represented"):
                native.validate_obligation_coverage_response(response, [check])

    def test_assignment_generalizes_to_three_facts_and_never_counts_one_twice(self):
        facts = [{"evidence_text": value} for value in ("alpha", "beta", "gamma")]
        atoms = [{"source_quote": value, "disposition": "represented", "requirement_refs": ["current"]}
                 for value in ("alpha beta gamma", "alpha beta", "alpha")]
        for order in itertools.permutations(atoms):
            self.assertTrue(native._distinct_source_atom_matching(facts, list(order)))
        self.assertFalse(native._distinct_source_atom_matching(facts, atoms[:2]))
        atoms[2]["disposition"] = "unrepresented"
        self.assertFalse(native._distinct_source_atom_matching(facts, atoms))

    def test_real_incident_reaches_precise_disagreements_without_mutation(self):
        case = self.incident()
        response = {"results": [case["independent_result"]]}
        original = copy.deepcopy(response)
        with self.assertRaises(native.TypedSourceAtomAlignmentError) as caught:
            native.validate_obligation_coverage_response(response, [case["check"]])
        self.assertEqual([d["fields"] for d in caught.exception.disagreements], [["source_quote"], ["condition"]])
        self.assertEqual(response, original)
        # Offline simulation only: a fresh reviewer must establish any actual agreement.
        for atom, primary in zip(response["results"][0]["identified_obligations"],
                                 case["check"]["review_context"]["primary_obligations"]):
            atom.update({k: primary.get(k) for k in ("source_quote", "condition")})
        self.assertEqual(native.validate_obligation_coverage_response(response, [case["check"]])[0]["verdict"], "consistent")

    def run_review(self, candidate, chunk, output, corrected, calls):
        case = self.incident()
        def reviewer(request, **kwargs):
            calls.append(copy.deepcopy(request))
            result = copy.deepcopy(case["independent_result"])
            context = request["checks"][0]["review_context"]
            current_ref = context["linked_requirements"][0]["requirement_ref"]
            for atom in result["identified_obligations"]:
                atom["requirement_refs"] = [current_ref]
            if corrected and len(calls) == 2:
                for atom, primary in zip(result["identified_obligations"], context["primary_obligations"]):
                    atom.update({k: primary.get(k) for k in ("source_quote", "condition")})
            bound = bind_mock_review_to_source_spans(
                {"protocol": native.OBLIGATION_COVERAGE_PROTOCOL, "status": "completed", "results": [result], "summary": {}},
                request, kwargs["output_dir"],
            )
            native.validate_obligation_coverage_response({"results": bound["results"]}, request["checks"])
            return bound
        with patch.object(bridge, "run_native_semantic_review", side_effect=reviewer), patch.object(bridge.time, "sleep"):
            return bridge._run_independent_obligation_coverage_review(
                candidate, chunk, review_dir=Path(output), run_id="fresh-alignment-test", chunk_index=1,
                attempt=1, host_runtime="codex", model="gpt-5.6-luna", timeout=5, agent_id="main",
                runner="exec", binary="codex", config_path=None, controller=bridge.RunController(),
            )

    def test_one_fresh_correction_keeps_candidate_source_inventory_and_audit(self):
        case, chunk, candidate = self.fixture()
        original = copy.deepcopy(candidate)
        calls = []
        with tempfile.TemporaryDirectory() as td:
            pointer = self.run_review(candidate, chunk, td, True, calls)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0]["checks"], calls[1]["checks"])
            self.assertEqual(pointer["provider_attempt"], 2)
            self.assertTrue(native.typed_alignment_retry_feedback_is_bound(calls[1]))
            self.assertEqual(pointer["candidate_response_sha256"], bridge._response_sha256(original))
            ledger = json.loads((Path(td) / pointer["obligation_analysis_ledger_path"]).read_text())
            self.assertEqual(len(ledger["obligations"]), 2)
            self.assertFalse(ledger["submission_ready"])
            rejected = json.loads(next(Path(td).glob("*-attempt-01/coverage-audit.json")).read_text())
            self.assertEqual(rejected["status"], "rejected")
            self.assertEqual(rejected["retry_code"], native.TypedSourceAtomAlignmentError.code)
        self.assertEqual(candidate, original)

    def test_persistent_disagreement_exhausts_and_never_publishes_ledger(self):
        case, chunk, candidate = self.fixture()
        calls = []
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(bridge.IndependentObligationReviewError) as caught:
                self.run_review(candidate, chunk, td, False, calls)
            self.assertEqual(len(calls), bridge.INDEPENDENT_REVIEW_PROVIDER_MAX_ATTEMPTS)
            self.assertFalse(caught.exception.retryable)
            self.assertEqual(caught.exception.error_records[0]["code"], "independent_obligation_review_correction_exhausted")
            self.assertEqual(list(Path(td).rglob("obligation-analysis-ledger.json")), [])

    def test_feedback_is_bound_to_source_primary_ids_fields_and_hashes(self):
        case, chunk, candidate = self.fixture()
        calls = []
        with tempfile.TemporaryDirectory() as td:
            self.run_review(candidate, chunk, td, True, calls)
        request = calls[1]
        for field, value in (("checks_sha256", "0" * 64), ("run_id", "old-run"),
                             ("clause_ids", ["foreign"]), ("disagreements", [])):
            modified = copy.deepcopy(request); modified["retry_feedback"][field] = value
            with self.subTest(field=field):
                self.assertFalse(native.typed_alignment_retry_feedback_is_bound(modified))
                with self.assertRaises(native.NativeSemanticReviewError):
                    native._prompt(modified)
        for field, value in (("primary_obligation_id", "foreign"), ("primary_sha256", "0" * 64),
                             ("fields", ["classification"]), ("fields", [{}])):
            modified = copy.deepcopy(request); modified["retry_feedback"]["disagreements"][0][field] = value
            with self.subTest(field=field, value=value):
                self.assertFalse(native.typed_alignment_retry_feedback_is_bound(modified))
        modified = copy.deepcopy(request); modified["retry_feedback"]["candidate_response_sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as td, patch.object(bridge, "run_native_semantic_review") as reviewer:
            with self.assertRaisesRegex(bridge.IndependentObligationReviewError, "immutable candidate"):
                bridge._run_independent_obligation_coverage_review(
                    candidate, chunk, review_dir=Path(td), run_id="fresh-alignment-test", chunk_index=1,
                    attempt=1, host_runtime="codex", model="gpt-5.6-luna", timeout=5, agent_id="main",
                    runner="exec", binary="codex", config_path=None, controller=bridge.RunController(),
                    _provider_attempt=2, _retry_feedback=modified["retry_feedback"])
            reviewer.assert_not_called()
        prompt = native._prompt(request)
        self.assertIn("not by copying to pass", prompt)
        self.assertIn("unresolved disagreements still block", prompt)


if __name__ == "__main__":
    unittest.main()
