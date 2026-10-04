"""Captured omission plus generic, bounded primary scope proposals (no provider)."""
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
from format_spec_validation import validate_instance
from host_review_contract import _validate_obligations
from host_review_schema import primary_generation_schema, native_output_schema, normalize_native_response
from semantic_contract import attach_request_provenance, sha256_json
from source_condition_reassessment import (
    source_atom_feedback, condition_feedback, condition_reassessment,
    condition_proposal_budget_receipt, APPLICABILITY_CODE, APPLICABILITY_RULE_ID,
)


def captured_case(renamed=False):
    data = json.loads((ROOT / "tests/fixtures/primary-applicability-incident.json").read_text())
    if renamed:
        replacements = {data["clause"]["id"]: "another-school-label",
                        next(iter(data["evidence_context"])): "another-source-occurrence"}
        def rename(value):
            if isinstance(value, str): return replacements.get(value, value)
            if isinstance(value, list): return [rename(v) for v in value]
            if isinstance(value, dict): return {replacements.get(k, k): rename(v) for k, v in value.items()}
            return value
        data = rename(data)
    clauses = [data["clause"]]; evidence = {"evidence": list(data["evidence_context"].values())}
    chunk = engine.build_llm_request([], clauses, evidence, {}, "full", contract_version="3.0")
    chunk.update(batch={"index": 1, "count": 1}, case_id="captured-scope-regression",
                 runtime_context={"code_fingerprint_sha256": sha256_json("offline-applicability-code")})
    chunk = attach_request_provenance(chunk, source_sha256=sha256_json(evidence),
        evidence_doc=evidence, clauses=clauses, run_id="fresh-applicability-test")
    raw = normalize_native_response({"contract_version": "3.0", "requirements": data["requirements"],
        "clause_reviews": [data["review"]], "unsupported_items": [], "reported_conflicts": []},
        chunk["response_schema"])
    raw["provenance"] = copy.deepcopy(chunk["provenance"])
    candidate = bridge.prepare_native_response_candidate(raw, chunk)[0]
    candidate["provenance"] = copy.deepcopy(chunk["provenance"])
    request, rejected = rejected_review(candidate, chunk)
    record = source_atom_feedback(candidate, chunk, request, rejected)
    assert record is not None
    record["response_sha256"] = sha256_json(raw)
    return raw, candidate, chunk, record


def rejected_review(candidate, chunk, attempt=1):
    request = native.build_obligation_coverage_request(candidate, chunk,
        run_id=chunk["provenance"]["run_id"], chunk_index=1)
    request.update(attempt=attempt, provider_attempt=2)
    data = json.loads((ROOT / "tests/fixtures/primary-applicability-incident.json").read_text())
    result = copy.deepcopy(data["independent_result"])
    check = request["checks"][0]; ctx = check["review_context"]
    result["check_id"] = check["check_id"]
    atom = result["identified_obligations"][0]
    atom["primary_obligation_id"] = ctx["primary_obligations"][0]["id"]
    atom["requirement_refs"] = [r["requirement_ref"] for r in ctx["linked_requirements"]]
    result["machine_obligation_ids"] = ctx["machine_obligation_ids"]
    return request, {"results": [result]}


def proposal(parent, value="applicable"):
    current = copy.deepcopy(parent)
    current["clause_reviews"][0]["obligations"][0]["applicability"] = value
    return current


class ApplicabilityReassessmentTests(unittest.TestCase):
    def proof(self, before, after, chunk, records):
        return condition_reassessment(before, after, records, bridge._retry_change_paths(before, after), chunk,
            prepare=bridge.prepare_native_response_candidate, validate=bridge.validate_host_agent_response)

    def test_captured_omission_is_not_automatically_interpreted_or_accepted(self):
        for renamed in (False, True):
            raw, candidate, chunk, record = captured_case(renamed)
            self.assertNotIn("applicability", raw["clause_reviews"][0]["obligations"][0])
            self.assertEqual(bridge.validate_host_agent_response(candidate, chunk), [])
            with self.assertRaisesRegex(native.TypedSourceAtomAlignmentError, "applicability"):
                native.validate_obligation_coverage_response(record["rejected_review"], record["review_request"]["checks"])
            self.assertEqual(record["code"], APPLICABILITY_CODE)
            self.assertEqual(record["source_atoms"][0]["fields"], ["applicability"])
            self.assertIsNone(condition_feedback(candidate, chunk, record["review_request"], record["rejected_review"]))
            original = copy.deepcopy((raw, candidate, chunk, record))
            for before in (raw, candidate):
                proofs = self.proof(before, proposal(before), chunk, [record])
                self.assertEqual(proofs[0]["rule_id"], APPLICABILITY_RULE_ID)
                self.assertTrue(proofs[0]["independent_review_required"])
                self.assertFalse(proofs[0]["mechanical_equivalence_claimed"])
                self.assertFalse(proofs[0]["submission_ready"])
                ledger = []
                error, _ = bridge._retry_semantic_change_error(before, proposal(before), [record],
                    contract_version="3.0", chunk=chunk, authorization_out=ledger)
                self.assertIsNone(error)
                self.assertIn("applicability_reassessment", ledger[0])
            self.assertEqual((raw, candidate, chunk, record), original)

    def test_no_review_value_is_copied_and_scope_is_exact(self):
        raw, _, chunk, record = captured_case()
        # A different valid enum is a PROPOSAL, not an accepted interpretation.
        for value in ("applicable", "not_applicable"):
            self.assertIsNotNone(self.proof(raw, proposal(raw, value), chunk, [record]))
        for value in (None, "", "informational", True, {}, [], "unknown", "conflicted"):
            self.assertIsNone(self.proof(raw, proposal(raw, value), chunk, [record]))
        for key in ("force", "actor", "action", "target", "condition", "source_quote", "status", "route", "reason", "id"):
            changed = proposal(raw); changed["clause_reviews"][0]["obligations"][0][key] = "unrequested"
            with self.subTest(key=key): self.assertIsNone(self.proof(raw, changed, chunk, [record]))
        for defect in ("delete", "requirement", "classification"):
            changed = proposal(raw)
            if defect == "delete": changed["clause_reviews"][0]["obligations"] = []
            elif defect == "requirement": changed["requirements"][0]["reason"] += " changed"
            else: changed["clause_reviews"][0]["classification"] = "informational"
            self.assertIsNone(self.proof(raw, changed, chunk, [record]))

    def test_partial_stale_duplicate_and_other_invalid_feedback_rejected(self):
        raw, candidate, chunk, record = captured_case()
        self.assertEqual(condition_proposal_budget_receipt(candidate, chunk, [record], raw)["proposal_limit"], 1)
        for defect in ("source", "run", "quote", "foreign_ref", "missing_ref", "duplicate", "missing_check", "force", "actor"):
            source = copy.deepcopy(chunk); req = copy.deepcopy(record["review_request"])
            review = copy.deepcopy(record["rejected_review"]); atom = review["results"][0]["identified_obligations"][0]
            if defect == "source": source["clauses"][0]["source_span"]["source_sha256"] = "0" * 64
            elif defect == "run": req["run_id"] = "old-run"
            elif defect == "quote": atom["source_quote"] = "foreign source"
            elif defect == "foreign_ref": atom["requirement_refs"] = ["foreign"]
            elif defect == "missing_ref": atom["requirement_refs"] = []
            elif defect == "duplicate": review["results"].append(copy.deepcopy(review["results"][0]))
            elif defect == "missing_check": review["results"] = []
            else: atom[defect] = "optional" if defect == "force" else "another actor"
            with self.subTest(defect=defect): self.assertIsNone(source_atom_feedback(candidate, source, req, review))
        for defect in ("code_only", "fields", "parent", "extra"):
            damaged = copy.deepcopy(record); records = [damaged]
            if defect == "code_only": records = [{"code": APPLICABILITY_CODE}]
            elif defect == "fields": damaged["source_atoms"][0]["fields"].append("force")
            elif defect == "parent": damaged["response_sha256"] = "0" * 64
            else: records.append({"code": "unrelated"})
            self.assertIsNone(condition_proposal_budget_receipt(candidate, chunk, records, raw))
            self.assertIsNone(self.proof(raw, proposal(raw), chunk, records))

    def test_complete_bundle_can_name_multiple_scope_fields_but_no_others(self):
        raw, candidate, chunk, record = captured_case()
        review = copy.deepcopy(record["rejected_review"])
        review["results"][0]["identified_obligations"][0].update(target="this printed template label", condition="when shown")
        feedback = source_atom_feedback(candidate, chunk, record["review_request"], review)
        self.assertEqual(feedback["source_atoms"][0]["fields"], ["target", "applicability", "condition"])
        feedback["response_sha256"] = sha256_json(raw)
        current = proposal(raw); current["clause_reviews"][0]["obligations"][0].update(target="this exact source label", condition="when present")
        self.assertEqual(len(self.proof(raw, current, chunk, [feedback])), 3)

    def test_new_generation_schema_is_host_neutral_without_changing_history_or_retries(self):
        raw, _, chunk, _ = captured_case(); canonical = copy.deepcopy(chunk["response_schema"])
        fresh = primary_generation_schema(canonical)
        native_packet_schema = bridge.compact_model_packet(chunk)["response_schema"]
        self.assertEqual(
            native_packet_schema["properties"]["response_wire_format"]["enum"],
            [bridge.PRIMARY_CLAUSE_REVIEW_WIRE_FORMAT],
        )
        self.assertEqual(
            set(native_packet_schema["properties"]["clause_reviews"]["required"]),
            {item["id"] for item in chunk["clauses"]},
        )
        self.assertEqual(bridge.compact_model_packet(chunk, fresh_primary=False)["response_schema"], canonical)
        for schema in (fresh, native_output_schema(fresh)):
            branches = schema["properties"]["clause_reviews"]["items"]["anyOf"]
            for branch in branches:
                duties = branch["properties"]["obligations"]
                if "anyOf" in duties: duties = next(v for v in duties["anyOf"] if v.get("type") == "array")
                atom_schema = duties["items"]
                self.assertIn("applicability", atom_schema["required"])
                self.assertIn("force", atom_schema["required"])
                self.assertNotEqual(validate_instance(raw["clause_reviews"][0]["obligations"][0], atom_schema), [])
                for value in (None, "invalid"):
                    self.assertNotEqual(validate_instance(proposal(raw, value)["clause_reviews"][0]["obligations"][0], atom_schema), [])
        self.assertEqual(chunk["response_schema"], canonical)
        self.assertEqual(normalize_native_response(raw, canonical), raw)
        self.assertEqual(primary_generation_schema(native.OBLIGATION_COVERAGE_SCHEMA), native.OBLIGATION_COVERAGE_SCHEMA)

    def test_undecided_scope_cannot_claim_covered_on_any_validation_entry(self):
        raw, candidate, chunk, _ = captured_case()
        for value in ("unknown", "conflicted"):
            response = proposal(candidate, value)
            self.assertTrue(any("undecided_scope_cannot_be_covered" in e
                for e in bridge.validate_host_agent_response(response, chunk)))
            pending = copy.deepcopy(response["clause_reviews"][0])
            pending["classification"] = "unresolved"
            pending["obligations"][0]["status"] = "unresolved"
            self.assertEqual(_validate_obligations(pending, 0, require_semantic_decomposition=True), [])

    def test_real_independent_review_exhaustion_routes_to_one_primary_proposal(self):
        _, candidate, chunk, _ = captured_case(); calls = []
        def reviewer(request, **kwargs):
            calls.append(request); _, rejected = rejected_review(candidate, chunk)
            kwargs["output_dir"].mkdir(parents=True)
            bridge._write_json(kwargs["output_dir"] / "compiled-response.json", rejected)
            native.validate_obligation_coverage_response(rejected, request["checks"])
        with tempfile.TemporaryDirectory() as td, patch.object(bridge, "run_native_semantic_review", side_effect=reviewer), patch.object(bridge.time, "sleep"):
            with self.assertRaises(bridge.IndependentObligationReviewError) as caught:
                bridge._run_independent_obligation_coverage_review(candidate, chunk, review_dir=Path(td),
                    run_id=chunk["provenance"]["run_id"], chunk_index=1, attempt=1, host_runtime="codex",
                    model="gpt-6-luna", timeout=5, agent_id="main", runner="exec", binary="mock-host",
                    config_path=None, controller=bridge.RunController())
            self.assertEqual(len(calls), 2)
            self.assertTrue(caught.exception.retryable)
            self.assertEqual(caught.exception.error_records[0]["code"], APPLICABILITY_CODE)
            self.assertEqual(list(Path(td).rglob("obligation-analysis-ledger.json")), [])

    def test_bridge_rechecks_proposal_and_does_not_reset_budget_or_publish_rejection(self):
        for outcome in ("accepted", "repeat", "fresh_rejection", "unrelated", "stale"):
            raw, _, seed, _ = captured_case(); calls, reviews = [], []
            evidence = {"evidence": list(seed["evidence_context"].values())}
            with tempfile.TemporaryDirectory() as td:
                directory = Path(td) / "packets"; directory.mkdir()
                engine.prepare_host_agent_review_packets(seed, seed["clauses"], evidence,
                    seed["provenance"]["source_sha256"], directory, chunk_size=100)
                current_chunk = json.loads((directory / "llm-request-chunks.json").read_text())[0]
                def primary(command, **kwargs):
                    calls.append(len(calls) + 1)
                    body = copy.deepcopy(raw) if len(calls) == 1 else proposal(raw)
                    body["provenance"] = current_chunk["provenance"]
                    if len(calls) > 1 and outcome == "unrelated": body["clause_reviews"][0]["reason"] += " unrequested"
                    return subprocess.CompletedProcess(["mock"], 0, json.dumps({"status": "ok", "provider": "openai",
                        "model": "gpt-6-luna", "result": {"payloads": [{"text": json.dumps(body)}]}}), "")
                def independent(candidate, chunk, **kwargs):
                    reviews.append(kwargs["attempt"])
                    if len(reviews) == 1:
                        request, rejected = rejected_review(candidate, chunk, kwargs["attempt"])
                        record = source_atom_feedback(candidate, chunk, request, rejected)
                        self.assertIsNotNone(record)
                        if outcome == "stale": record["source_chunk_sha256"] = "0" * 64
                        error = bridge.IndependentObligationReviewError("scope proposal needed")
                        error.retryable = True; error.error_records = [record]; raise error
                    if outcome in {"repeat", "fresh_rejection"}:
                        error = bridge.IndependentObligationReviewError("fresh review refused")
                        error.retryable = outcome == "repeat"; error.error_records = [{"code": APPLICABILITY_CODE}]; raise error
                    from test_host_agent_bridge import HostAgentBridgeTests
                    return HostAgentBridgeTests._fake_independent_review(candidate, chunk, **kwargs)
                out = Path(td) / "merged.json"
                with patch.dict(os.environ, {"THESIS_FORGE_HOST_RUNTIME": "openclaw"}), patch.object(bridge, "_run_command", side_effect=primary), patch.object(bridge, "_run_independent_obligation_coverage_review", side_effect=independent):
                    if outcome == "accepted": bridge.run_bridge(directory, response_out=out, timeout=1,
                        max_attempts=1, agent_id="main", openclaw_bin="mock", model="openai/gpt-6-luna")
                    else:
                        with self.assertRaises((ValueError, bridge.IndependentObligationReviewError)):
                            bridge.run_bridge(directory, response_out=out, timeout=1, max_attempts=1,
                                agent_id="main", openclaw_bin="mock", model="openai/gpt-6-luna")
                self.assertEqual(out.exists(), outcome == "accepted")
                self.assertEqual(calls, [1] if outcome == "stale" else [1, 2])
                if outcome == "unrelated": self.assertEqual(reviews, [1])
                if len(calls) == 2:
                    self.assertIn("PRIMARY APPLICABILITY REASSESSMENT", (directory / "host-agent-prompts/prompt-0001-attempt-02.txt").read_text())
                if outcome == "accepted":
                    audit = json.loads((directory / "host-agent-run.json").read_text())
                    budget = audit["chunk_runs"][0]["retry_budget"]
                    self.assertEqual(budget["scope_proposals_started"], 1)
                    self.assertEqual(budget["current_attempt_kind"], "applicability_proposal")


if __name__ == "__main__": unittest.main()
