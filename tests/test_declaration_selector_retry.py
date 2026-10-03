"""Captured selector correction must not disguise literal or graph edits."""
import copy
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import host_agent_bridge as bridge
from requirements_engine import build_llm_request, prepare_host_agent_review_packets
from semantic_contract import attach_request_provenance, sha256_json


def incident():
    data = json.loads((ROOT / "tests/fixtures/declaration-selector-retry-incident.json").read_text())
    source = data["source"]
    evidence = {"evidence": list(source["evidence_context"].values())}
    chunk = build_llm_request([], source["clauses"], evidence, {}, "full", contract_version="3.0")
    chunk.update(source)
    chunk.update(case_id="selector-offline", batch={"index": 2},
                 runtime_context={"code_fingerprint_sha256": sha256_json("selector-offline-code")})
    chunk = attach_request_provenance(chunk, source_sha256=sha256_json(evidence),
        evidence_doc=evidence, clauses=source["clauses"], run_id="selector-offline")
    parent, raw = data["parent"], data["model_retry"]
    records = bridge.contract_error_records(
        bridge.validate_host_agent_response(parent, chunk), response=parent, chunk=chunk)
    assert len(records) == 1
    return parent, raw, chunk, records


class DeclarationSelectorRetryTests(unittest.TestCase):
    def transition(self, parent, raw, chunk, records, *, candidate=None, comparison=None):
        projected = bridge._materialize_fixed_declaration_source_text(raw, chunk)[0]
        parent_projected = bridge._materialize_fixed_declaration_source_text(parent, chunk)[0]
        ledger = []
        error, paths = bridge._retry_semantic_change_error(
            parent, candidate if candidate is not None else projected, records,
            contract_version="3.0", chunk=chunk, authorization_out=ledger,
            comparison_previous_response=parent_projected,
            comparison_current_response=comparison if comparison is not None else projected,
            model_retry_response=raw)
        return error, paths, ledger

    def test_captured_two_raw_attempts_authorize_selector_and_code_literals_separately(self):
        parent, raw, chunk, records = incident()
        frozen = copy.deepcopy((parent, raw, chunk, records))
        self.assertEqual(bridge._retry_change_paths(parent, raw),
            ["$.requirements[0].properties.items[0].source_evidence_ids"])
        error, paths, ledger = self.transition(parent, raw, chunk, records)
        self.assertIsNone(error)
        self.assertEqual(len(paths), 4)
        self.assertEqual(len(ledger), 4)
        self.assertEqual(sum(a["change_owner"] == "model_selector" for a in ledger), 1)
        self.assertTrue(all(a["source_binding_complete"] and a["independent_review_required"]
                            and not a["submission_ready"] for a in ledger))
        projected = bridge.prepare_native_response_candidate(raw, chunk)[0]
        self.assertEqual(bridge.validate_host_agent_response(projected, chunk), [])
        self.assertEqual(projected["clause_reviews"],
                         bridge.normalize_native_response(parent, chunk["response_schema"])["clause_reviews"])
        self.assertEqual((parent, raw, chunk, records), frozen)

    def test_model_literal_atom_graph_and_diagnostic_edits_are_rejected(self):
        for field in ("heading", "body_parts", "source_signature_lines", "body", "reason",
                      "condition", "classification", "clause_ids", "evidence_ids"):
            with self.subTest(field=field):
                parent, raw, chunk, records = incident()
                item = raw["requirements"][0]["properties"]["items"][0]
                if field in {"heading", "body_parts", "source_signature_lines", "body"}:
                    item[field] = "model text" if field in {"heading", "body"} else ["model text"]
                elif field == "reason": raw["requirements"][0][field] += " changed"
                elif field == "classification":
                    next(r for r in raw["clause_reviews"] if r.get("obligations"))[field] = "informational"
                elif field == "condition":
                    next(r for r in raw["clause_reviews"] if r.get("obligations"))["obligations"][0][field] = "new scope"
                else: raw["requirements"][0][field] = []
                self.assertIsNotNone(self.transition(parent, raw, chunk, records)[0])

    def test_model_copied_exact_source_heading_is_not_code_projection(self):
        parent, raw, chunk, records = incident()
        projected = bridge._materialize_fixed_declaration_source_text(raw, chunk)[0]
        raw["requirements"][0]["properties"]["items"][0]["heading"] = projected["requirements"][0]["properties"]["items"][0]["heading"]
        self.assertIsNotNone(self.transition(parent, raw, chunk, records)[0])

    def test_stale_and_incomplete_feedback_and_source_are_rejected(self):
        for change in ("hash", "extra_error", "code_fingerprint", "source_span", "duplicate_clause",
                       "non_adjacent", "completed_signature", "old_run"):
            with self.subTest(change=change):
                parent, raw, chunk, records = incident()
                group = bridge._fixed_declaration_candidates(chunk["clauses"], chunk["evidence_context"],
                    anchor=chunk["declaration_anchor_preference"])[0]
                eid = group["signature_evidence_ids"][0]
                if change == "hash": records[0]["response_sha256"] = "0" * 64
                elif change == "extra_error": records.append(copy.deepcopy(records[0]))
                elif change == "code_fingerprint": chunk["runtime_context"] = {}
                elif change == "source_span": chunk["clauses"][0]["source_span"]["source_sha256"] = "0" * 64
                elif change == "duplicate_clause": chunk["clauses"].append(copy.deepcopy(chunk["clauses"][0]))
                elif change == "non_adjacent": chunk["evidence_context"][eid]["location"]["child_index"] += 10
                elif change == "completed_signature": chunk["evidence_context"][eid]["text"] += " 张三"
                else: raw["provenance"] = {"run_id": "old-run"}
                self.assertIsNotNone(self.transition(parent, raw, chunk, records)[0])

    def test_candidate_and_comparison_tampering_are_not_authorized(self):
        for target in ("candidate", "comparison"):
            parent, raw, chunk, records = incident()
            forged = bridge._materialize_fixed_declaration_source_text(raw, chunk)[0]
            forged["requirements"][0]["properties"]["items"][0]["body_parts"] = ["forged"]
            self.assertIsNotNone(self.transition(parent, raw, chunk, records, **{target: forged})[0])

    def test_different_current_ids_have_same_policy(self):
        parent, raw, chunk, _ = incident()
        encoded = json.dumps({"parent": parent, "raw": raw, "source": {
            k: chunk[k] for k in ("clauses", "evidence_context", "declaration_anchor_preference")}}, ensure_ascii=False)
        for clause in chunk["clauses"]:
            encoded = encoded.replace(clause["id"], "current-" + clause["id"])
        for eid in chunk["evidence_context"]:
            encoded = encoded.replace(eid, "current-" + eid)
        data = json.loads(encoded)
        source = data["source"]; evidence = {"evidence": list(source["evidence_context"].values())}
        rebuilt = build_llm_request([], source["clauses"], evidence, {}, "full", contract_version="3.0")
        rebuilt.update(source, case_id="different-case", batch={"index": 1},
            runtime_context={"code_fingerprint_sha256": sha256_json("different-code")})
        rebuilt = attach_request_provenance(rebuilt, source_sha256=sha256_json(evidence),
            evidence_doc=evidence, clauses=source["clauses"], run_id="different-run")
        records = bridge.contract_error_records(bridge.validate_host_agent_response(data["parent"], rebuilt),
            response=data["parent"], chunk=rebuilt)
        self.assertIsNone(self.transition(data["parent"], data["raw"], rebuilt, records)[0])

    def test_bridge_selector_retry_requires_new_independent_review_before_acceptance(self):
        parent, raw, chunk, _ = incident()
        evidence = {"evidence": list(chunk["evidence_context"].values())}
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td) / "packet"; directory.mkdir()
            prepare_host_agent_review_packets(chunk, chunk["clauses"], evidence,
                chunk["provenance"]["source_sha256"], directory, chunk_size=100)
            chunks = json.loads((directory / "llm-request-chunks.json").read_text())
            self.assertEqual(len(chunks), 1)
            calls = []
            def primary(*args, **kwargs):
                body = copy.deepcopy(parent if not calls else raw)
                body["requirements"][0]["properties"]["before_role"] = chunks[0]["declaration_anchor_preference"]
                calls.append("primary")
                body["provenance"] = chunks[0]["provenance"]
                return subprocess.CompletedProcess(["mock-native"], 0, json.dumps({
                    "runId": "offline-selector", "status": "ok", "provider": "openai",
                    "model": "gpt-5.6-luna", "result": {"payloads": [{"text": json.dumps(body)}]}}), "")
            def independent(candidate, source, **kwargs):
                calls.append("independent")
                self.assertEqual(kwargs["attempt"], 2)
                self.assertEqual(bridge.validate_host_agent_response(candidate, source), [])
                error = bridge.IndependentObligationReviewError("fresh review rejects candidate")
                error.retryable = False
                raise error
            output = Path(td) / "merged.json"
            with patch.dict(os.environ, {"THESIS_FORGE_HOST_RUNTIME": "openclaw"}), \
                    patch.object(bridge, "_run_command", side_effect=primary), \
                    patch.object(bridge, "_run_independent_obligation_coverage_review", side_effect=independent), \
                    patch.object(bridge.time, "sleep"):
                with self.assertRaisesRegex(bridge.IndependentObligationReviewError, "fresh review rejects"):
                    bridge.run_bridge(directory, response_out=output, agent_id="main", timeout=1,
                        max_attempts=2, openclaw_bin="mock-native", model="openai/gpt-5.6-luna")
            self.assertEqual(calls, ["primary", "primary", "independent"])
            self.assertFalse(output.exists())
            run = json.loads((directory / "host-agent-run.json").read_text())
            self.assertEqual(run["status"], "failed")


if __name__ == "__main__":
    unittest.main()
