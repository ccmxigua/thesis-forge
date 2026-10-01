"""Captured primary retry, authorization ledger, and real bridge orchestration."""
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
import host_agent_bridge as bridge
import requirements_engine as engine
from semantic_contract import attach_request_provenance, sha256_json
from source_quote_reassessment import quote_context_reassessment, RULE_ID


def incident():
    data = json.loads((ROOT / "tests/fixtures/ambiguous-quote-context-incident.json").read_text())
    source = data["chunk"]
    evidence = {"evidence": list(source["evidence_context"].values())}
    chunk = engine.build_llm_request([], source["clauses"], evidence, source.get("rule_spec", {}),
                                    "full", contract_version="3.0")
    chunk.update({k:v for k,v in source.items() if k != "rule_spec"})
    chunk.update(case_id="offline-quote-context", batch={"index":1},
                 runtime_context={"code_fingerprint_sha256":sha256_json("offline-code-fixture")})
    chunk = attach_request_provenance(chunk, source_sha256=sha256_json(evidence), evidence_doc=evidence,
                                     clauses=source["clauses"], run_id="offline-quote-context")
    current = bridge.normalize_native_response(data["model_retry"], chunk["response_schema"])
    return data["baseline"], current, chunk


class QuoteContextReassessmentTests(unittest.TestCase):
    def proofs(self, old, new, chunk, records=None):
        records = records if records is not None else bridge.contract_error_records(
            bridge.validate_host_agent_response(old, chunk), response=old, chunk=chunk)
        return quote_context_reassessment(old, new, records, bridge._retry_change_paths(old, new),
                                          chunk, validate=bridge.validate_host_agent_response)

    def test_captured_pair_is_reassessment_not_mechanical_equivalence(self):
        old, new, chunk = incident()
        frozen = copy.deepcopy((old, new, chunk))
        records = bridge.contract_error_records(bridge.validate_host_agent_response(old, chunk), response=old, chunk=chunk)
        paths = bridge._retry_change_paths(old, new)
        self.assertEqual(len(paths), 2)
        proofs = self.proofs(old, new, chunk, records)
        self.assertEqual(len(proofs), 2)
        for proof in proofs:
            self.assertEqual(proof["ambiguous_match_count"], 2)
            self.assertFalse(proof["mechanical_equivalence_claimed"])
            self.assertFalse(proof["submission_ready"])
            self.assertTrue(proof["independent_review_required"])
        authorizations = []
        error, changes = bridge._retry_semantic_change_error(old, new, records, contract_version="3.0",
            chunk=chunk, authorization_out=authorizations)
        self.assertIsNone(error)
        self.assertEqual(changes, paths)
        self.assertEqual(len(authorizations), 2)
        self.assertTrue(all(a["rule_id"] == RULE_ID and a["source_binding_complete"]
                            and a["semantic_review_required"] for a in authorizations))
        self.assertEqual(bridge.validate_host_agent_response(new, chunk), [])
        self.assertEqual((old, new, chunk), frozen)

    def test_semantic_fields_order_and_requirements_cannot_change(self):
        for field, value in (("actor", "new actor"), ("action", "omit"), ("target", "other target"),
            ("condition", "unconditional"), ("force", "optional"), ("status", "unverifiable"),
            ("route", "human"), ("id", "replacement"), ("applicability", "unknown")):
            old, new, chunk = incident()
            review = next(r for r in new["clause_reviews"] if r["clause_id"] == "C00046")
            review["obligations"][0][field] = value
            with self.subTest(field=field): self.assertIsNone(self.proofs(old, new, chunk))
        for change in ("atom_order", "classification", "requirement", "drop_atom", "reason"):
            old, new, chunk = incident()
            review = next(r for r in new["clause_reviews"] if r["clause_id"] == "C00046")
            if change == "atom_order": review["obligations"].reverse()
            elif change == "classification": review["classification"] = "informational"
            elif change == "requirement": new["requirements"] = []
            elif change == "drop_atom": review["obligations"].pop()
            else: review["reason"] += " improved"
            with self.subTest(change=change): self.assertIsNone(self.proofs(old, new, chunk))

    def test_stale_feedback_source_and_nonquote_errors_fail_closed(self):
        for change in ("feedback_hash", "feedback_pointer", "missing_record", "duplicate_record",
                       "source_hash", "source_location", "unknown_evidence", "foreign_quote", "unrelated_error"):
            old, new, chunk = incident()
            records = bridge.contract_error_records(bridge.validate_host_agent_response(old, chunk), response=old, chunk=chunk)
            cid = records[0]["clause_id"]
            clause = next(c for c in chunk["clauses"] if c["id"] == cid)
            if change == "feedback_hash": records[0]["response_sha256"] = "0" * 64
            elif change == "feedback_pointer": records[0]["clause_id"] = "foreign-clause"
            elif change == "missing_record": records.pop()
            elif change == "duplicate_record": records.append(copy.deepcopy(records[0]))
            elif change == "source_hash": clause["source_span"]["source_sha256"] = "0" * 64
            elif change == "source_location": clause["source_span"]["location"]["row"] += 1
            elif change == "unknown_evidence": clause["evidence_ids"] = ["foreign-evidence"]
            elif change == "foreign_quote": new["clause_reviews"][7]["obligations"][0]["source_quote"] = "foreign text"
            else: old["requirements"][0]["properties"]["invented_operation"] = True
            with self.subTest(change=change): self.assertIsNone(self.proofs(old, new, chunk, records))

    def test_a_guessed_first_occurrence_or_invented_old_text_is_ineligible(self):
        old, new, chunk = incident()
        new["clause_reviews"][7]["obligations"][0]["source_quote"] = "年    月    日"
        self.assertIsNone(self.proofs(old, new, chunk))
        old, new, chunk = incident()
        old["clause_reviews"][7]["obligations"][0]["source_quote"] = "日期待审核"
        self.assertIsNone(self.proofs(old, new, chunk))
        old, new, chunk = incident()
        # A valid precise occurrence is not a validator-rejected ambiguity.
        old["clause_reviews"][7]["obligations"][0]["source_quote"] = "年    月    日"
        self.assertIsNone(self.proofs(old, new, chunk))

    def test_current_ids_are_not_hardcoded(self):
        old, new, chunk = incident()
        encoded = json.dumps({"old":old,"new":new,"source":{
            k:v for k,v in chunk.items() if k not in ("response_schema","requirement_contract")}}, ensure_ascii=False)
        for c in chunk["clauses"]:
            encoded = encoded.replace(json.dumps(c["id"]), json.dumps("current-" + c["id"]))
        for eid in chunk["evidence_context"]:
            encoded = encoded.replace(json.dumps(eid), json.dumps("current-" + eid))
        data = json.loads(encoded)
        fresh = engine.build_llm_request([], data["source"]["clauses"],
            {"evidence":list(data["source"]["evidence_context"].values())}, {}, "full", contract_version="3.0")
        fresh.update(data["source"])
        self.assertEqual(len(self.proofs(data["old"], data["new"], fresh)), 2)

    def test_bridge_still_runs_fresh_independent_review_and_rejects_its_disagreement(self):
        old, new, source = incident()
        evidence = {"evidence":list(source["evidence_context"].values())}
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td) / "requirements"
            request = engine.build_llm_request([], source["clauses"], evidence, {}, "full", contract_version="3.0",
                runtime_context={"code_fingerprint_sha256":"f" * 64})
            request["case_id"] = "standalone"
            request = attach_request_provenance(request, source_sha256="a" * 64, evidence_doc=evidence,
                clauses=source["clauses"], run_id="quote-context-orchestration")
            engine.prepare_host_agent_review_packets(request, source["clauses"], evidence, "a" * 64,
                directory, chunk_size=100)
            chunks = json.loads((directory / "llm-request-chunks.json").read_text())
            self.assertEqual(len(chunks), 1)
            results = []
            for i, response in enumerate((old, new), 1):
                response["provenance"] = chunks[0]["provenance"]
                envelope = {"runId":f"quote-primary-{i}","status":"ok","provider":"openai","model":"gpt-5.6-luna",
                    "result":{"payloads":[{"text":json.dumps(response)}]}}
                results.append(subprocess.CompletedProcess(["mock-native-host"],0,json.dumps(envelope),""))
            def reject(candidate, chunk, **kwargs):
                self.assertEqual(bridge.validate_host_agent_response(candidate, chunk), [])
                self.assertEqual(kwargs["attempt"], 2)
                error = bridge.IndependentObligationReviewError("source does not support the proposed atom")
                error.retryable = False
                raise error
            output = Path(td) / "merged.json"
            with patch.dict(os.environ, {"THESIS_FORGE_HOST_RUNTIME":"openclaw"}), \
                 patch.object(bridge, "_run_command", side_effect=results) as primary, \
                 patch.object(bridge, "_run_independent_obligation_coverage_review", side_effect=reject) as independent:
                with self.assertRaises(bridge.IndependentObligationReviewError):
                    bridge.run_bridge(directory,response_out=output,agent_id="main",timeout=1,max_attempts=2,
                        openclaw_bin="mock-native-host",model="openai/gpt-5.6-luna")
            self.assertEqual(primary.call_count, 2)
            self.assertEqual(independent.call_count, 1)
            self.assertFalse(output.exists())
            self.assertEqual(json.loads((directory / "host-agent-run.json").read_text())["status"], "failed")

if __name__ == "__main__": unittest.main()
