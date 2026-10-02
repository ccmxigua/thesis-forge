"""Exact source delimiters compose with independent cover repair errors."""
import copy
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import host_agent_bridge as bridge
import requirements_engine as engine
from semantic_contract import attach_request_provenance


def packet(delimiter="：", title="测试论文题名", *, security=False):
    texts = [("TITLE-occurrence", "TITLE-evidence", title + delimiter)]
    if security:
        texts.append(("ADMIN-occurrence", "ADMIN-evidence", "密级："))
    clauses, evidence = [], {"evidence": []}
    for cid, eid, source in texts:
        clauses.append({
            "id": cid, "text": source.rstrip(":："), "evidence_ids": [eid],
            "source_kind": "paragraph", "location": {}, "part_index": 0,
            "source_span": {"evidence_id": eid, "start_offset": 0,
                            "end_offset": len(source.rstrip(":：")),
                            "text": source.rstrip(":："),
                            "source_sha256": hashlib.sha256(source.encode()).hexdigest()},
        })
        evidence["evidence"].append({"id": eid, "text": source, "kind": "paragraph", "location": {}})
    request = engine.build_llm_request([], clauses, evidence, {}, "full",
                                       runtime_context={"code_fingerprint_sha256": "f" * 64})
    request["case_id"] = "different-school"
    chunk = attach_request_provenance(request, source_sha256="a" * 64,
                                      evidence_doc=evidence, clauses=clauses, run_id="different-run")
    chunk["batch"] = {"index": 1}
    chunk["evidence_context"] = {item["id"]: item for item in evidence["evidence"]}
    fields = [{"id": "title_zh", "label": title, "label_display_policy": "always",
               "value_from": "thesis_profile.cover_metadata.title_zh", "display_policy": "required", "order": 1}]
    if security:
        fields.append({"id": "security_marking", "label": "密级", "label_display_policy": "always",
                       "value_from": "thesis_profile.cover_metadata.security_marking",
                       "display_policy": "if_present", "order": 2})
    response = {"contract_version": "2.1", "provenance": copy.deepcopy(chunk["provenance"]),
                "requirements": [{"role": "cover", "properties": {"fields": fields, "institution": "测试大学"},
                                  "clause_ids": [c["id"] for c in clauses],
                                  "evidence_ids": [e["id"] for e in evidence["evidence"]],
                                  "confidence": 0.8, "reason": "Source-backed printed form labels.",
                                  "verification": {"mode": "static_docx", "checks": ["printed form labels"]}}],
                "clause_reviews": [{"clause_id": c["id"], "classification": "executable",
                                    "requirement_indexes": [0], "reason": "Printed form field."} for c in clauses],
                "unsupported_items": [], "reported_conflicts": []}
    return chunk, response


def records(response, chunk):
    return bridge.contract_error_records(bridge.validate_host_agent_response(response, chunk),
                                         response=response, chunk=chunk)


def project(chunk, response, *, cookie=True, feedback=None):
    return bridge._project_exact_cover_label_terminal_colon(
        response, records(response, chunk) if feedback is None else feedback, chunk,
        source_projection_validation_sha256=bridge._response_sha256(chunk) if cookie else "0" * 64)


@pytest.mark.parametrize("delimiter,title", [("：", "论文标题"), (":", "Research title")])
def test_exact_delimiter_only_changes_one_label(delimiter, title):
    chunk, original = packet(delimiter, title)
    before = copy.deepcopy(original)
    candidate, audit = project(chunk, original)
    assert candidate is not None
    expected = copy.deepcopy(before)
    expected["requirements"][0]["properties"]["fields"][0]["label"] = title + delimiter
    assert candidate == expected and original == before
    assert bridge.validate_host_agent_response(candidate, chunk) == []
    assert audit[0]["source_binding"]["source_fragments"][0]["clause_id"] == "TITLE-occurrence"
    assert audit[0]["partial_candidate_only"] and not audit[0]["submission_ready"]
    assert project(chunk, candidate) == (None, [])


@pytest.mark.parametrize("mutation", ["cookie", "hash", "missing_run", "foreign_evidence",
                                     "ambiguous", "wrong_label", "whitespace", "wrong_policy", "stale_feedback"])
def test_unsafe_or_nonexact_repairs_reject(mutation):
    chunk, response = packet()
    feedback = records(response, chunk)
    if mutation == "hash":
        chunk["clauses"][0]["source_span"]["source_sha256"] = "b" * 64
    elif mutation == "missing_run":
        chunk["provenance"].pop("run_id")
    elif mutation == "foreign_evidence":
        response["requirements"][0]["evidence_ids"] = ["foreign"]
    elif mutation == "ambiguous":
        duplicate = copy.deepcopy(chunk["clauses"][0]); duplicate["id"] = "second-occurrence"
        chunk["clauses"].append(duplicate)
        response["requirements"][0]["clause_ids"].append(duplicate["id"])
    elif mutation in {"wrong_label", "whitespace"}:
        response["requirements"][0]["properties"]["fields"][0]["label"] += "x" if mutation == "wrong_label" else " "
    elif mutation == "wrong_policy":
        response["requirements"][0]["properties"]["fields"][0]["label_display_policy"] = "never"
    elif mutation == "stale_feedback":
        response["requirements"][0]["reason"] += " changed"
    frozen = copy.deepcopy(response)
    candidate, audit = project(chunk, response, cookie=mutation != "cookie",
                               feedback=feedback if mutation == "stale_feedback" else None)
    assert candidate is None and audit == [] and response == frozen


def test_combined_title_and_administration_repairs_revalidate_between_steps():
    chunk, response = packet(security=True)
    original_reviews = copy.deepcopy(response["clause_reviews"])
    audit = []
    for _ in range(4):
        candidate, step = bridge._apply_safe_mechanical_repairs(
            response, records(response, chunk), chunk=chunk,
            source_projection_validation_sha256=bridge._response_sha256(chunk))
        if candidate is None:
            break
        response = candidate; audit.extend(step)
    assert bridge.validate_host_agent_response(response, chunk) == []
    assert {r["rule_id"] for r in audit} == {
        "exact_cover_label_terminal_colon_v1", "source_bound_cover_security_marking_migration_v1"}
    assert response["clause_reviews"] == original_reviews
    assert len(response["requirements"]) == 1


def test_empty_retry_is_not_restored_or_accepted():
    chunk, response = packet()
    response.update(requirements=[], clause_reviews=[])
    assert bridge.validate_host_agent_response(response, chunk)
    assert project(chunk, response) == (None, [])
