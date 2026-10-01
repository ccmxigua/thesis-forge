from __future__ import annotations

import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import host_agent_bridge as bridge
from context_relation_projection import POLICY, TABLE_POLICY, project_context_edges
from host_review_contract import contract_error_records, validate_response
from requirements_engine import build_llm_request
from semantic_contract import attach_request_provenance, sha256_json
from native_semantic_review import build_obligation_coverage_request


def date_fixture() -> tuple[dict, dict]:
    response, chunk = fixture()
    texts = {"clause-0": "年    月    日", "clause-1": "完成日期"}
    cells = []
    for column, cid in enumerate(("clause-1", "clause-0")):
        clause = next(c for c in chunk["clauses"] if c["id"] == cid)
        eid, text = clause["evidence_ids"][0], texts[cid]
        location = {"part": "document", "table_child_index": 7, "row": 2,
                    "column": column, "paragraph": 0, "order": column}
        clause.update(text=text, source_kind="table_cell", location=location)
        clause["source_span"].update(text=text, start_offset=0, end_offset=len(text),
            source_sha256=hashlib.sha256(text.encode()).hexdigest(), location=location)
        source = chunk["evidence_context"][eid]
        source.update(text=text, kind="table_cell", location=location)
        cells.append({"column": column, "grid_span": 1, "vertical_merge": None,
                      "horizontal_merge": None, "has_nested_table": False,
                      "paragraphs": [{"paragraph": 0, "text": text,
                          "text_sha256": hashlib.sha256(text.encode()).hexdigest(), "evidence_id": eid}]})
    row = {"protocol": "current-source-table-row/v1", "part": "document",
           "table_child_index": 7, "row": 2, "table_grid_columns": 2,
           "bidi_visual": False, "grid_before": 0, "grid_after": 0, "cells": cells}
    row["row_sha256"] = sha256_json(row)
    for eid in ("evidence-0", "evidence-1"):
        chunk["evidence_context"][eid]["table_row_context"] = copy.deepcopy(row)
    response["requirements"][0].update(role="cover", properties={"institution": "测试机构", "fields": [{
        "id": "completion_date", "label": "完成日期",
        "value_from": "thesis_profile.cover_metadata.completion_date",
        "display_policy": "required", "label_display_policy": "always", "order": 1}]})
    atom = response["clause_reviews"][1]["obligations"][0]
    atom.update(id="current-date-field", action="保留字段", target="论文日期字段", source_quote="完成日期")
    return response, chunk


def rehash_rows(chunk):
    for eid in ("evidence-0", "evidence-1"):
        row = chunk["evidence_context"][eid]["table_row_context"]
        row["row_sha256"] = sha256_json({k: v for k, v in row.items() if k != "row_sha256"})


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
    def test_captured_incident_raw_composes_atom_repairs_and_context_without_id_rules(self):
        incident = json.loads((ROOT / "tests/fixtures/blank-date-context-edge-incident.json").read_text())
        for renamed in (False, True):
            with self.subTest(renamed=renamed):
                data = copy.deepcopy(incident)
                if renamed:
                    replacements = {c["id"]: f"fresh-clause-{i}" for i, c in enumerate(data["clauses"])}
                    replacements.update({eid: f"fresh-evidence-{i}" for i, eid in enumerate(data["evidence_context"])})
                    def rename(value):
                        if isinstance(value, str): return replacements.get(value, value)
                        if isinstance(value, list): return [rename(v) for v in value]
                        if isinstance(value, dict): return {replacements.get(k, k): rename(v) for k, v in value.items()}
                        return value
                    data = rename(data)
                    for evidence in data["evidence_context"].values():
                        row = evidence.get("table_row_context")
                        if row:
                            row["row_sha256"] = sha256_json({k: v for k, v in row.items() if k != "row_sha256"})
                clauses = data["clauses"]
                evidence_doc = {"evidence": list(data["evidence_context"].values())}
                chunk = build_llm_request([], clauses, evidence_doc, {}, "full", contract_version="3.0")
                chunk["batch"] = {"index": 3, "count": 24}
                chunk["case_id"] = "new-generic-case" if renamed else "captured-case"
                chunk["runtime_context"] = {"code_fingerprint_sha256": sha256_json({"policy": TABLE_POLICY})}
                chunk = attach_request_provenance(chunk, source_sha256=sha256_json(evidence_doc),
                    evidence_doc=evidence_doc, clauses=clauses, run_id="fresh-test-run")
                raw = data["response"]
                original = copy.deepcopy(raw)
                candidate, audit = bridge.prepare_native_response_candidate(raw, chunk,
                    source_projection_validation_sha256=sha256_json(chunk))
                self.assertEqual(validate_response(candidate, chunk), [])
                self.assertEqual(raw, original)
                proofs = [p for r in audit["mechanical_repairs"] if r.get("rule_id") == POLICY for p in r["proofs"]]
                self.assertEqual(len(proofs), 1)
                context = proofs[0]["context_edges"][0]
                self.assertEqual(context["context_basis"]["policy"], TABLE_POLICY)
                cid = context["clause_id"]
                self.assertNotIn(cid, candidate["requirements"][0]["clause_ids"])
                self.assertEqual(candidate["requirements"][0]["properties"], proofs[0]["original_requirement"]["properties"])
                self.assertEqual(next(r for r in candidate["clause_reviews"] if r["clause_id"] == cid), context["review"])
                self.assertFalse(audit["repair_transaction"]["submission_ready"])
                self.assertEqual(audit["repair_transaction"]["status"], "projected_pending_independent_review")
                second, second_audit = bridge.prepare_native_response_candidate(candidate, chunk,
                    source_projection_validation_sha256=sha256_json(chunk))
                self.assertEqual(second, candidate)
                self.assertEqual(second_audit["mechanical_repairs"], [])

    def test_blank_date_context_requires_unique_current_table_field_and_keeps_source(self):
        response, chunk = date_fixture()
        original = copy.deepcopy(response)
        candidate, audit = project(response, chunk)
        self.assertIsNotNone(candidate, validate_response(response, chunk))
        self.assertEqual(validate_response(candidate, chunk), [])
        self.assertEqual(candidate["requirements"][0]["clause_ids"], ["clause-1"])
        self.assertEqual(candidate["requirements"][0]["properties"], original["requirements"][0]["properties"])
        self.assertEqual(candidate["clause_reviews"], original["clause_reviews"])
        proof = audit[0]["proofs"][0]["context_edges"][0]
        self.assertEqual(proof["clause"]["source_span"]["text"], "年    月    日")
        self.assertEqual(proof["context_basis"]["policy"], TABLE_POLICY)
        self.assertEqual(proof["context_basis"]["owner_clause_id"], "clause-1")
        self.assertFalse(proof["context_basis"]["date_format_confirmed"])
        self.assertFalse(proof["context_basis"]["external_action_confirmed"])
        request = build_obligation_coverage_request(candidate, chunk,
            run_id=chunk["provenance"]["run_id"], chunk_index=1)
        check = next(c for c in request["checks"] if c["check_id"] == "clause-0")
        self.assertEqual(check["document_text"], "年    月    日")
        self.assertEqual(check["review_context"]["classification"], "informational")
        self.assertEqual(check["review_context"]["linked_requirements"], [])
        self.assertEqual(check["review_context"]["table_structure_context"]["relationship"],
                         "same_row_immediate_left_unmerged")
        self.assertEqual(response, original)

    def test_blank_date_context_rejects_ambiguous_geometry_or_field_ownership(self):
        for shape in ("missing_row", "old_row_hash", "merged", "rtl", "multi_label", "duplicate_field",
                      "wrong_label", "missing_owner", "duplicate_owner", "filled_date", "wrong_part",
                      "wrong_row", "wrong_column", "missing_field_binding", "wrong_role", "new_obligation"):
            with self.subTest(shape=shape):
                response, chunk = date_fixture()
                source = chunk["evidence_context"]["evidence-0"]
                req = response["requirements"][0]
                if shape == "missing_row": del source["table_row_context"]
                elif shape == "old_row_hash": source["table_row_context"]["row_sha256"] = "0" * 64
                elif shape in {"merged", "rtl", "multi_label"}:
                    for eid in ("evidence-0", "evidence-1"):
                        row = chunk["evidence_context"][eid]["table_row_context"]
                        if shape == "merged": row["cells"][1]["vertical_merge"] = "restart"
                        elif shape == "rtl": row["bidi_visual"] = True
                        else: row["cells"][0]["paragraphs"].append({"paragraph": 1, "text": "其他标签",
                            "text_sha256": hashlib.sha256("其他标签".encode()).hexdigest(), "evidence_id": "extra-label"})
                    rehash_rows(chunk)
                elif shape == "duplicate_field": req["properties"]["fields"].append(copy.deepcopy(req["properties"]["fields"][0]))
                elif shape == "wrong_label": req["properties"]["fields"][0]["label"] = "另一日期"
                elif shape == "missing_owner": req["clause_ids"] = ["clause-0"]
                elif shape == "duplicate_owner":
                    owner = copy.deepcopy(chunk["clauses"][1]); owner["id"] = "duplicate-owner"
                    chunk["clauses"].append(owner)
                    review = copy.deepcopy(response["clause_reviews"][1]); review["clause_id"] = "duplicate-owner"
                    response["clause_reviews"].append(review)
                    req["clause_ids"].append("duplicate-owner")
                elif shape == "filled_date":
                    text = "2026年10月1日"
                    source["text"] = text
                    chunk["clauses"][0]["source_span"].update(text=text, end_offset=len(text), source_sha256=hashlib.sha256(text.encode()).hexdigest())
                elif shape.startswith("wrong_") and shape != "wrong_role":
                    key = shape.removeprefix("wrong_")
                    if key == "column": source["table_row_context"]["cells"][1]["column"] = 99
                    else: source["table_row_context"][key] = "other" if key == "part" else 99
                    rehash_rows(chunk)
                elif shape == "missing_field_binding": del req["properties"]["fields"][0]["value_from"]
                elif shape == "wrong_role": req["role"] = "body_text"
                elif shape == "new_obligation": response["clause_reviews"][0]["obligations"] = [copy.deepcopy(response["clause_reviews"][1]["obligations"][0])]
                self.assertEqual(project(response, chunk), (None, []))

    def test_blank_date_context_cannot_rebind_owner_to_non_date_or_other_metadata(self):
        for field_id, binding in (("completion_date", "approval_date"), ("completion_date", "author_name"),
                                  ("author_name", "author_name"), ("title_zh", "title_zh")):
            with self.subTest(field_id=field_id, binding=binding):
                response, chunk = date_fixture()
                field = response["requirements"][0]["properties"]["fields"][0]
                field.update(id=field_id, value_from=f"thesis_profile.cover_metadata.{binding}")
                self.assertEqual(project(response, chunk), (None, []))

    def test_blank_date_context_requires_complete_current_authority_and_error_bundle(self):
        response, chunk = date_fixture()
        records = contract_error_records(validate_response(response, chunk), response=response, chunk=chunk)
        self.assertEqual(project(response, chunk, authority=False), (None, []))
        self.assertEqual(project(response, chunk, records=records[:1]), (None, []))
        stale = copy.deepcopy(records); stale[0]["response_sha256"] = "0" * 64
        self.assertEqual(project(response, chunk, records=stale), (None, []))

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
