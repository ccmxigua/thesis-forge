"""Source-bound target proposals: no equivalence projection or release proof."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import host_agent_bridge as bridge
import native_semantic_review as native
import requirements_engine as engine
from semantic_contract import sha256_json
from source_condition_reassessment import (
    condition_feedback, source_atom_feedback, condition_reassessment,
    condition_proposal_budget_receipt, TARGET_CODE, TARGET_RULE_ID,
)
from test_source_inventory_composition import fixture, completion


def case():
    raw, chunk = fixture(1)
    raw = completion(raw, chunk)
    raw["clause_reviews"][0]["obligations"][0]["actor"] = "document"
    raw["clause_reviews"][0]["obligations"][0]["target"] = "the complete abstract paragraph"
    candidate = bridge.prepare_native_response_candidate(raw, chunk)[0]
    candidate["provenance"] = copy.deepcopy(chunk["provenance"])
    request, rejected = disputed_review(candidate, chunk, 1)
    record = source_atom_feedback(candidate, chunk, request, rejected)
    assert record is not None
    record["response_sha256"] = sha256_json(raw)
    return raw, candidate, chunk, record


def disputed_review(candidate, chunk, attempt):
    request = native.build_obligation_coverage_request(candidate, chunk,
        run_id=chunk["provenance"]["run_id"], chunk_index=1)
    request.update(attempt=attempt, provider_attempt=2)
    results = []
    for check in request["checks"]:
        ctx = check["review_context"]
        atoms = []
        for primary in ctx["primary_obligations"]:
            atom = {k:copy.deepcopy(primary[k]) for k in
                    ("actor", "action", "target", "source_quote", "force", "applicability", "condition")
                    if k in primary}
            atom.update(target="the source sentence describing the abstract quality",
                primary_obligation_id=primary["id"], obligation_summary="synthetic scope disagreement",
                disposition="represented", requirement_refs=[r["requirement_ref"] for r in ctx["linked_requirements"]])
            atoms.append(atom)
        results.append({"check_id": check["check_id"], "verdict": "consistent",
            "rationale": "Synthetic reviewer considers the primary target broader than this sentence.",
            "identified_obligations": atoms, "evidence_quotes": [check["document_text"]],
            "machine_obligation_ids": ctx["machine_obligation_ids"]})
    return request, {"results": results}


def proposal(parent):
    current = copy.deepcopy(parent)
    # A primary proposal is not required to copy the rejected review's wording.
    current["clause_reviews"][0]["obligations"][0]["target"] = "this exact source-bound abstract quality statement"
    return current


class TargetReassessmentTests(unittest.TestCase):
    def proof(self, old, new, chunk, records):
        return condition_reassessment(old, new, records, bridge._retry_change_paths(old, new), chunk,
            prepare=bridge.prepare_native_response_candidate, validate=bridge.validate_host_agent_response)

    def test_target_feedback_is_distinct_from_condition_and_not_a_pass(self):
        raw, candidate, chunk, record = case()
        self.assertEqual(record["code"], TARGET_CODE)
        self.assertEqual(record["source_atoms"][0]["fields"], ["target"])
        self.assertNotIn("condition_atoms", record)
        self.assertIsNone(condition_feedback(candidate, chunk, record["review_request"], record["rejected_review"]))
        with self.assertRaises(native.TypedSourceAtomAlignmentError):
            native.validate_obligation_coverage_response(record["rejected_review"], record["review_request"]["checks"])
        frozen = copy.deepcopy((raw, candidate, chunk, record))
        for before in (raw, candidate):
            new = proposal(before)
            proofs = self.proof(before, new, chunk, [record])
            self.assertEqual(len(proofs), 1)
            self.assertEqual(proofs[0]["rule_id"], TARGET_RULE_ID)
            self.assertTrue(proofs[0]["independent_review_required"])
            self.assertFalse(proofs[0]["mechanical_equivalence_claimed"])
            ledger = []
            error, _ = bridge._retry_semantic_change_error(before, new, [record],
                contract_version="3.0", chunk=chunk, authorization_out=ledger)
            self.assertIsNone(error)
            self.assertEqual(ledger[0]["rule_id"], TARGET_RULE_ID)
            self.assertIn("target_reassessment", ledger[0])
        self.assertEqual((raw, candidate, chunk, record), frozen)

    def test_complete_rejected_review_and_current_source_are_required(self):
        _, candidate, chunk, record = case()
        for defect in ("quote", "foreign_ref", "empty_ref", "actor", "force", "missing_check", "duplicate_check", "source", "request"):
            source = copy.deepcopy(chunk); req = copy.deepcopy(record["review_request"])
            response = copy.deepcopy(record["rejected_review"])
            atom = response["results"][0]["identified_obligations"][0]
            if defect == "quote": atom["source_quote"] = "not current source"
            elif defect == "foreign_ref": atom["requirement_refs"] = ["foreign"]
            elif defect == "empty_ref": atom["requirement_refs"] = []
            elif defect == "actor": atom["actor"] = "different actor"
            elif defect == "force": atom["force"] = "optional"
            elif defect == "missing_check": response["results"] = []
            elif defect == "duplicate_check": response["results"].append(copy.deepcopy(response["results"][0]))
            elif defect == "source": source["clauses"][0]["source_span"]["source_sha256"] = "0" * 64
            else: req["run_id"] = "old-run"
            with self.subTest(defect=defect):
                self.assertIsNone(source_atom_feedback(candidate, source, req, response))

    def test_target_proposal_cannot_change_condition_or_other_semantics(self):
        for field in ("condition", "actor", "action", "force", "applicability", "source_quote", "route", "status", "id", "reason"):
            raw, _, chunk, record = case()
            new = proposal(raw)
            new["clause_reviews"][0]["obligations"][0][field] = "unauthorized"
            with self.subTest(field=field): self.assertIsNone(self.proof(raw, new, chunk, [record]))
        for defect in ("requirement", "classification", "delete", "blank_target", "unknown_target", "missing_target"):
            raw, _, chunk, record = case(); new = proposal(raw)
            if defect == "requirement": new["requirements"][0]["reason"] += " changed"
            elif defect == "classification": new["clause_reviews"][0]["classification"] = "informational"
            elif defect == "delete": new["clause_reviews"][0]["obligations"] = []
            elif defect == "blank_target": new["clause_reviews"][0]["obligations"][0]["target"] = " "
            elif defect == "unknown_target": new["clause_reviews"][0]["obligations"][0]["target"] = "unknown"
            else: new["clause_reviews"][0]["obligations"][0].pop("target")
            with self.subTest(defect=defect): self.assertIsNone(self.proof(raw, new, chunk, [record]))

    def test_mixed_target_and_condition_uses_only_each_disputed_field(self):
        raw, candidate, chunk, old = case()
        review = copy.deepcopy(old["rejected_review"])
        review["results"][0]["identified_obligations"][0]["condition"] = "source scope proposal"
        record = source_atom_feedback(candidate, chunk, old["review_request"], review)
        self.assertIsNotNone(record)
        self.assertEqual(record["source_atoms"][0]["fields"], ["target", "condition"])
        record["response_sha256"] = sha256_json(raw)
        current = proposal(raw)
        current["clause_reviews"][0]["obligations"][0]["condition"] = "a primary's new scope proposal"
        self.assertEqual(len(self.proof(raw, current, chunk, [record])), 2)

    def test_budget_rejects_partial_stale_or_unbound_feedback(self):
        raw, candidate, chunk, record = case()
        receipt = condition_proposal_budget_receipt(candidate, chunk, [record], raw)
        self.assertEqual(receipt["reassessment_code"], TARGET_CODE)
        self.assertEqual(receipt["proposal_limit"], 1)
        for defect in ("code_only", "old_run", "parent", "candidate", "source", "fields", "extra"):
            damaged = copy.deepcopy(record); records = [damaged]
            if defect == "code_only": records = [{"code": TARGET_CODE}]
            elif defect == "old_run": damaged["run_id"] = "old-run"
            elif defect == "parent": damaged["response_sha256"] = "0" * 64
            elif defect == "candidate": damaged["candidate_response_sha256"] = "0" * 64
            elif defect == "source": damaged["source_chunk_sha256"] = "0" * 64
            elif defect == "fields": damaged["source_atoms"][0]["fields"].append("actor")
            else: records.append({"code": "unrelated_error"})
            with self.subTest(defect=defect):
                self.assertIsNone(condition_proposal_budget_receipt(candidate, chunk, records, raw))
                self.assertIsNone(self.proof(raw, proposal(raw), chunk, records))

    def test_real_independent_exhaustion_proposes_primary_not_reviewer_copy(self):
        _, candidate, chunk, _ = case(); calls = []
        def reviewer(request, **kwargs):
            calls.append(request)
            _, rejected = disputed_review(candidate, chunk, 1)
            kwargs["output_dir"].mkdir(parents=True)
            bridge._write_json(kwargs["output_dir"] / "compiled-response.json", rejected)
            native.validate_obligation_coverage_response(rejected, request["checks"])
        with tempfile.TemporaryDirectory() as td, \
             patch.object(bridge, "run_native_semantic_review", side_effect=reviewer), patch.object(bridge.time, "sleep"):
            with self.assertRaises(bridge.IndependentObligationReviewError) as caught:
                bridge._run_independent_obligation_coverage_review(candidate, chunk, review_dir=Path(td),
                    run_id=chunk["provenance"]["run_id"], chunk_index=1, attempt=1, host_runtime="codex",
                    model="gpt-6-luna", timeout=5, agent_id="main", runner="exec", binary="mock-host",
                    config_path=None, controller=bridge.RunController())
            self.assertEqual(len(calls), 2)
            self.assertTrue(caught.exception.retryable)
            self.assertEqual(caught.exception.error_records[0]["code"], TARGET_CODE)
            self.assertEqual(list(Path(td).rglob("obligation-analysis-ledger.json")), [])

    def orchestrate(self, outcome):
        raw, _, seed, _ = case()
        source = seed["clauses"]; evidence = {"evidence": list(seed["evidence_context"].values())}
        primary_calls, reviews, prompts, captured = [], [], [], {}
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td) / "packets"; directory.mkdir()
            engine.prepare_host_agent_review_packets(seed, source, evidence, seed["provenance"]["source_sha256"],
                directory, chunk_size=100)
            current_chunk = json.loads((directory / "llm-request-chunks.json").read_text())[0]
            def independent(candidate, chunk, **kwargs):
                reviews.append(kwargs["attempt"])
                captured["controller"] = kwargs["controller"]
                if "record" not in captured:
                    request, rejected = disputed_review(candidate, chunk, kwargs["attempt"])
                    record = source_atom_feedback(candidate, chunk, request, rejected)
                    self.assertIsNotNone(record); captured["record"] = copy.deepcopy(record)
                    if outcome == "stale": record["source_chunk_sha256"] = "0" * 64
                    error = bridge.IndependentObligationReviewError("primary target proposal required")
                    error.retryable = True; error.error_records = [record]; raise error
                if outcome == "repeat":
                    error = bridge.IndependentObligationReviewError("second scope proposal must not reset budget")
                    error.retryable = True; error.error_records = [{"code": TARGET_CODE}]; raise error
                if outcome == "fresh_rejection":
                    error = bridge.IndependentObligationReviewError("fresh independent review rejects proposal")
                    error.retryable = False; raise error
                from test_host_agent_bridge import HostAgentBridgeTests
                return HostAgentBridgeTests._fake_independent_review(candidate, chunk, **kwargs)
            def primary(command, **kwargs):
                primary_calls.append(len(primary_calls) + 1)
                prompts.append(command)
                body = copy.deepcopy(raw) if "record" not in captured else proposal(raw)
                body["provenance"] = current_chunk["provenance"]
                if outcome == "unrelated" and "record" in captured: body["clause_reviews"][0]["reason"] += " unrequested"
                text = "invalid JSON" if len(primary_calls) == 1 else json.dumps(body)
                return subprocess.CompletedProcess(["mock-host"], 0, json.dumps({"status":"ok",
                    "provider":"openai", "model":"gpt-6-luna", "result":{"payloads":[{"text":text}]}}), "")
            real_reserve = bridge.condition_proposal_budget_receipt
            def reserve(*args):
                receipt = real_reserve(*args)
                if receipt and outcome == "cancel": captured["controller"].request_stop("cancel after reservation")
                return receipt
            output = Path(td) / "merged.json"
            with patch.dict(os.environ, {"THESIS_FORGE_HOST_RUNTIME":"openclaw"}), \
                 patch.object(bridge, "_run_command", side_effect=primary), \
                 patch.object(bridge, "_run_independent_obligation_coverage_review", side_effect=independent), \
                 patch.object(bridge, "condition_proposal_budget_receipt", side_effect=reserve):
                if outcome == "accepted":
                    bridge.run_bridge(directory, response_out=output, timeout=1, max_attempts=2,
                        agent_id="main", openclaw_bin="mock-host", model="openai/gpt-6-luna")
                else:
                    with self.assertRaises((ValueError, bridge.HostAgentCancelled, bridge.IndependentObligationReviewError)):
                        bridge.run_bridge(directory, response_out=output, timeout=1, max_attempts=2,
                            agent_id="main", openclaw_bin="mock-host", model="openai/gpt-6-luna")
            audit = json.loads((directory / "host-agent-run.json").read_text())
            self.assertEqual(output.exists(), outcome == "accepted")
            if len(primary_calls) == 3:
                prompt = (directory / "host-agent-prompts/prompt-0001-attempt-03.txt").read_text()
                self.assertIn("PRIMARY TARGET REASSESSMENT", prompt)
                self.assertIn("not an instruction to copy", prompt)
            return audit, primary_calls, reviews

    def test_target_proposal_works_after_regular_attempts_are_exhausted(self):
        audit, calls, reviews = self.orchestrate("accepted")
        self.assertEqual(calls, [1, 2, 3]); self.assertEqual(reviews, [2, 3])
        self.assertEqual(audit["status"], "merged")
        budget = audit["chunk_runs"][0]["retry_budget"]
        self.assertEqual(budget["scope_proposals_started"], 1)
        self.assertEqual(budget["scope_reassessment_code"], TARGET_CODE)
        self.assertEqual(budget["current_attempt_kind"], "target_proposal")

    def test_bad_feedback_cancel_new_disagreement_and_other_edits_fail_closed(self):
        for outcome in ("stale", "cancel", "repeat", "fresh_rejection", "unrelated"):
            with self.subTest(outcome=outcome):
                audit, calls, reviews = self.orchestrate(outcome)
                self.assertEqual(calls, [1, 2] if outcome in {"stale", "cancel"} else [1, 2, 3])
                self.assertFalse(audit["merged_response_written"])
                if outcome == "unrelated": self.assertEqual(reviews, [2])


if __name__ == "__main__": unittest.main()
