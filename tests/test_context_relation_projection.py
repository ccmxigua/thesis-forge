from __future__ import annotations

import copy
import hashlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import host_agent_bridge as bridge
from context_relation_projection import POLICY, project_context_edges
from host_review_contract import contract_error_records, validate_response
from requirements_engine import build_llm_request
from semantic_contract import attach_request_provenance, sha256_json


def fixture(shared: bool = False) -> tuple[dict, dict]:
    sources = ["章节排版说明", "正文必须左对齐", "须经部门批准"]
    clauses, evidence = [], {}
    combined = sources[0]
    for index, text in enumerate(sources):
        cid = f"clause-{index}"
        eid = "evidence-shared" if shared and index < 2 else f"evidence-{index}"
        source = combined if shared and index < 2 else text
        if shared and index == 1:
            text = combined
        location = {"part": "document", "child_index": index if not shared else (0 if index < 2 else index), "order": index if not shared else (0 if index < 2 else index)}
        evidence[eid] = {"id": eid, "kind": "paragraph", "text": source, "location": location}
        start = source.index(text)
        clauses.append({
            "id": cid, "text": text, "evidence_ids": [eid], "location": location,
            "source_span": {"evidence_id": eid, "start_offset": start,
                "end_offset": start + len(text), "text": text,
                "source_sha256": hashlib.sha256(source.encode()).hexdigest(), "location": location},
        })
    chunk = build_llm_request([], clauses, {"evidence": list(evidence.values())}, {}, "full", contract_version="3.0")
    chunk["batch"] = {"index": 1, "count": 1}
    chunk["case_id"] = "generic-source-test"
    chunk["runtime_context"] = {"code_fingerprint_sha256": sha256_json({"policy": POLICY})}
    chunk = attach_request_provenance(chunk, source_sha256=sha256_json(sources),
        evidence_doc={"evidence": list(evidence.values())}, clauses=clauses, run_id="test-current-run")
    response = {"contract_version": "3.0", "requirements": [{
        "role": "body_text", "properties": {"paragraph": {"alignment": "left"}},
        "clause_ids": ["clause-0", "clause-1"],
        "evidence_ids": list(dict.fromkeys(clauses[0]["evidence_ids"] + clauses[1]["evidence_ids"])),
        "confidence": .95, "reason": "current operative formatting clause",
    }], "clause_reviews": [
        {"clause_id": "clause-0", "classification": "informational", "normative_basis": "insufficient", "reason": "context only", "obligations": []},
        {"clause_id": "clause-1", "classification": "executable", "normative_basis": "explicit_normative_text", "reason": "local formatting", "obligations": [
            {"id": "left-align", "status": "covered", "reason": "represented",
             "action": "align", "target": "body text",
             "source_quote": combined if shared else sources[1], "force": "required", "applicability": "applicable", "route": "automatic"}]},
        {"clause_id": "clause-2", "classification": "external_compliance", "normative_basis": "external_duty", "reason": "pending real approval", "obligations": [
            {"id": "approval", "status": "unverifiable", "reason": "not established", "actor": "部门",
             "action": "批准", "target": "论文",
             "source_quote": sources[2], "force": "required", "applicability": "applicable", "route": "human"}]},
    ], "unsupported_items": [], "reported_conflicts": []}
    return response, chunk


def project(response, chunk, *, records=None, authority=True):
    errors = validate_response(response, chunk)
    records = contract_error_records(errors, response=response, chunk=chunk) if records is None else records
    return project_context_edges(response, chunk, records, validate=validate_response,
        source_projection_validation_sha256=sha256_json(chunk) if authority else None,
        invocation_fingerprints=bridge._retry_input_fingerprints(chunk))


class ContextRelationProjectionTests(unittest.TestCase):
    def test_context_is_preserved_and_all_payloads_and_external_duties_unchanged(self):
        response, chunk = fixture()
        original, original_chunk = copy.deepcopy(response), copy.deepcopy(chunk)
        errors = validate_response(response, chunk)
        self.assertTrue(any("mixed_execution_classification_relation" in e for e in errors), errors)
        candidate, audit = project(response, chunk)
        self.assertIsNotNone(candidate, errors)
        self.assertEqual(validate_response(candidate, chunk), [])
        self.assertEqual(candidate["requirements"][0]["clause_ids"], ["clause-1"])
        self.assertEqual(candidate["requirements"][0]["evidence_ids"], ["evidence-1"])
        self.assertEqual(candidate["clause_reviews"], original["clause_reviews"])
        self.assertEqual(candidate["requirements"][0]["properties"], original["requirements"][0]["properties"])
        context = audit[0]["proofs"][0]["context_edges"][0]
        self.assertEqual(context["clause"], chunk["clauses"][0])
        self.assertEqual(context["review"], original["clause_reviews"][0])
        self.assertEqual(context["evidence"], [chunk["evidence_context"]["evidence-0"]])
        self.assertTrue(audit[0]["independent_review_required"])
        self.assertFalse(audit[0]["submission_ready"])
        self.assertEqual(response, original)
        self.assertEqual(chunk, original_chunk)

    def test_shared_evidence_is_never_removed(self):
        response, chunk = fixture(shared=True)
        candidate, audit = project(response, chunk)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["requirements"][0]["evidence_ids"], ["evidence-shared"])
        self.assertEqual(audit[0]["proofs"][0]["detached_requirement_evidence_ids"], [])

    def test_ineligible_shapes_remain_fail_closed(self):
        for shape in ("external", "unresolved", "missing_inventory", "normative", "selector", "literal", "existing", "referenced", "embedded_reference", "conflict", "unknown", "duplicate_review", "wrong_span", "extra_evidence", "other_error"):
            with self.subTest(shape=shape):
                response, chunk = fixture()
                req, review = response["requirements"][0], response["clause_reviews"][0]
                if shape in {"external", "unresolved"}:
                    review["classification"] = "external_compliance" if shape == "external" else "unresolved"
                elif shape == "missing_inventory": del review["obligations"]
                elif shape == "normative": review["normative_basis"] = "explicit_normative_text"
                elif shape == "selector": req["source_fragment_clause_ids"] = ["clause-0"]
                elif shape == "literal": req["properties"]["text"] = "章节排版说明"
                elif shape == "existing": req["existing_requirement_id"] = "previous-rule"
                elif shape == "referenced": req["reason"] = "clause-0"
                elif shape == "embedded_reference": req["reason"] = "refer to clause-0 for the literal"
                elif shape == "conflict": response["reported_conflicts"] = [{"reason": "conflict"}]
                elif shape == "unknown": req["clause_ids"].append("unknown")
                elif shape == "duplicate_review": response["clause_reviews"].append(copy.deepcopy(review))
                elif shape == "wrong_span": chunk["clauses"][0]["source_span"]["source_sha256"] = "0" * 64
                elif shape == "extra_evidence": req["evidence_ids"].append("evidence-2")
                elif shape == "other_error": req["properties"]["unknown"] = True
                self.assertEqual(project(response, chunk), (None, []))

    def test_missing_source_authority_stale_and_partial_feedback_rejected(self):
        response, chunk = fixture()
        records = contract_error_records(validate_response(response, chunk), response=response, chunk=chunk)
        self.assertGreater(len(records), 1)
        self.assertEqual(project(response, chunk, authority=False), (None, []))
        self.assertEqual(project(response, chunk, records=records[:1]), (None, []))
        stale = copy.deepcopy(records)
        stale[0]["response_sha256"] = "0" * 64
        self.assertEqual(project(response, chunk, records=stale), (None, []))
        response["requirements"][0]["properties"]["paragraph"]["alignment"] = "right"
        self.assertEqual(project(response, chunk, records=records), (None, []))

    def test_bridge_requires_full_invocation_and_source_authentication(self):
        response, chunk = fixture()
        records = contract_error_records(validate_response(response, chunk), response=response, chunk=chunk)
        self.assertEqual(bridge._apply_safe_mechanical_repairs(response, records, chunk=chunk), (None, []))
        repaired, _ = bridge._apply_safe_mechanical_repairs(response, records, chunk=chunk,
            source_projection_validation_sha256=sha256_json(chunk))
        self.assertIsNotNone(repaired)
        del chunk["runtime_context"]
        self.assertEqual(bridge._apply_safe_mechanical_repairs(response, records, chunk=chunk,
            source_projection_validation_sha256=sha256_json(chunk)), (None, []))

    def test_model_cannot_relabel_an_external_duty_to_use_projection(self):
        response, chunk = fixture()
        req = response["requirements"][0]
        req["clause_ids"].append("clause-2")
        req["evidence_ids"].append("evidence-2")
        self.assertEqual(project(response, chunk), (None, []))

    def test_known_source_duty_cannot_hide_as_zero_duty_context(self):
        response, chunk = fixture()
        text = "关键词在摘要内容后另起一行，一般3～8个，之间用分号分开"
        clause = chunk["clauses"][0]
        clause["text"] = text
        clause["source_span"].update(text=text, end_offset=len(text), source_sha256=hashlib.sha256(text.encode()).hexdigest())
        chunk["evidence_context"]["evidence-0"]["text"] = text
        self.assertEqual(project(response, chunk), (None, []))

    def test_unknown_duties_and_normative_prose_never_become_context(self):
        for text in ("须经导师批准后方可提交", "须经导师批准的说明", "正文应左对齐的要求",
                     "Keywords must originate in the thesis", "A previously unknown obligation",
                     "摘要最多1000字的说明"):
            with self.subTest(text=text):
                response, chunk = fixture()
                clause = chunk["clauses"][0]
                clause["text"] = text
                clause["source_span"].update(text=text, end_offset=len(text), source_sha256=hashlib.sha256(text.encode()).hexdigest())
                chunk["evidence_context"]["evidence-0"]["text"] = text
                self.assertEqual(project(response, chunk), (None, []))

    def test_candidate_boundary_emits_bound_transaction_and_is_idempotent(self):
        response, chunk = fixture()
        candidate, audit = bridge.prepare_native_response_candidate(response, chunk,
            source_projection_validation_sha256=sha256_json(chunk))
        receipt = audit["repair_transaction"]
        self.assertEqual(receipt["status"], "projected_pending_independent_review")
        self.assertEqual(receipt["binding"], chunk["provenance"])
        self.assertEqual(receipt["proofs"][0]["rule_id"], POLICY)
        self.assertFalse(receipt["submission_ready"])
        second, second_audit = bridge.prepare_native_response_candidate(candidate, chunk,
            source_projection_validation_sha256=sha256_json(chunk))
        self.assertEqual(second, candidate)
        self.assertEqual(second_audit["mechanical_repairs"], [])

    def test_atom_quote_metadata_and_context_relation_compose_with_revalidation(self):
        response, chunk = fixture()
        # Normalized quote punctuation is a separately bounded representation repair.
        response["clause_reviews"][1]["obligations"][0]["source_quote"] = "正文必须左对齐。"
        candidate, audit = bridge.prepare_native_response_candidate(response, chunk,
            source_projection_validation_sha256=sha256_json(chunk))
        self.assertEqual(validate_response(candidate, chunk), [])
        self.assertEqual(candidate["clause_reviews"][1]["obligations"][0]["source_quote"], "正文必须左对齐")
        rules = [r.get("rule_id") for r in audit["mechanical_repairs"]]
        self.assertIn("current_source_atom_metadata_v1", rules)
        self.assertIn(POLICY, rules)


if __name__ == "__main__":
    unittest.main()
