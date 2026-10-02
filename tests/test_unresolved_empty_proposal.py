"""Empty proposals are separate from unresolved source obligations, not waived."""
import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import host_agent_bridge as bridge
import requirements_engine as engine
from semantic_contract import attach_request_provenance, sha256_json
from native_semantic_review import (
    MissingSourceObligationInventoryError, build_obligation_coverage_request,
    validate_obligation_coverage_response,
)
from compliance import build_clause_records, summarize
from test_host_agent_bridge import exact_source_clause
import test_host_agent_bridge as bridge_fixtures


def fixture(count=1, source="7 相关研究"):
    clauses = [exact_source_clause(f"heading-{i}", source, f"evidence-{i}") for i in range(count)]
    evidence = [{"id": c["evidence_ids"][0], "text": source, "kind": "paragraph"} for c in clauses]
    chunk = engine.build_llm_request([], clauses, {"evidence": evidence}, {}, "full", contract_version="3.0")
    chunk.update(batch={"index": 1, "count": 1}, case_id="dynamic-heading-fixture",
                 runtime_context={"code_fingerprint_sha256": sha256_json({"fixture": "heading"})})
    chunk = attach_request_provenance(chunk, source_sha256=sha256_json(evidence),
        evidence_doc={"evidence": evidence}, clauses=clauses, run_id="current-unresolved-run")
    raw = {"contract_version": "3.0", "requirements": [{
        "role": "body_text", "properties": {"font": {"cjk": None}, "paragraph": {}},
        "clause_ids": [c["id"]], "evidence_ids": c["evidence_ids"], "existing_requirement_id": None,
        "field_key": None, "confidence": .72, "reason": "Heading level remains unknown.",
    } for c in clauses], "clause_reviews": [{"clause_id": c["id"], "classification": "unresolved",
        "normative_basis": "insufficient", "reason": "Do not guess the heading level."} for c in clauses],
        "unsupported_items": [], "reported_conflicts": []}
    return raw, chunk


def records(response, chunk):
    return bridge.contract_error_records(bridge.validate_host_agent_response(response, chunk), response=response, chunk=chunk)


def project(response, chunk, feedback=None, validation=None):
    return bridge._project_unresolved_empty_requirements(response, records(response, chunk) if feedback is None else feedback,
        chunk, source_projection_validation_sha256=sha256_json(chunk) if validation is None else validation)


@pytest.mark.parametrize("count", [1, 2, 4])
def test_separation_preserves_full_uncertainty_source_and_rejected_proposals(count):
    raw, chunk = fixture(count); original = copy.deepcopy((raw, chunk))
    candidate, audit = bridge.prepare_native_response_candidate(raw, chunk, source_projection_validation_sha256=sha256_json(chunk))
    assert candidate["requirements"] == [] and candidate["clause_reviews"] == raw["clause_reviews"]
    proof = audit["mechanical_repairs"][0]
    assert proof["rule_id"] == "unresolved_empty_proposal_separation_v1"
    assert len(proof["removed_requirements"]) == count and len(proof["source_bindings"]) == count
    assert all(r["reason"] == "Heading level remains unknown." for r in proof["removed_requirements"])
    assert proof["independent_review_required"] and not proof["submission_ready"]
    assert proof["input_fingerprints"] == bridge._retry_input_fingerprints(chunk)
    assert audit["repair_transaction"]["status"] == "projected_pending_independent_review"
    assert not bridge.validate_host_agent_response(candidate, chunk)
    second, second_audit = bridge.prepare_native_response_candidate(candidate, chunk, source_projection_validation_sha256=sha256_json(chunk))
    assert second == candidate and second_audit["mechanical_repairs"] == []
    assert (raw, chunk) == original


@pytest.mark.parametrize("damage", ["payload", "unknown_payload", "existing", "field", "check", "condition", "prerequisite", "fragment",
    "obligations", "classification", "mixed", "unknown_clause", "wrong_evidence", "wrong_span", "stale_span", "stale_run",
    "duplicate_review", "conflict", "missing_feedback", "duplicate_feedback", "stale_feedback", "extra_error", "no_validation", "old_validation"])
def test_nonempty_semantics_identity_source_and_complete_bundle_cannot_be_discarded(damage):
    raw, chunk = fixture(2); raw = bridge.normalize_native_response(raw, chunk["response_schema"])
    feedback = records(raw, chunk); validation = sha256_json(chunk); req = raw["requirements"][0]
    if damage == "payload": req["properties"] = {"font": {"size_pt": 12}}
    elif damage == "unknown_payload": req["properties"] = {"bogus": None}
    elif damage == "existing": req["existing_requirement_id"] = "R-original"
    elif damage == "field": req["field_key"] = "title"
    elif damage == "check": req["verification"] = {"mode": "external", "checks": ["must verify"]}
    elif damage == "condition": req["applicability"] = {"status": "conditional", "conditions": []}
    elif damage == "prerequisite": req["input_prerequisites"] = [{"input_key": "degree"}]
    elif damage == "fragment": req["source_fragment_clause_ids"] = ["heading-0"]
    elif damage == "obligations": raw["clause_reviews"][0]["obligations"] = [{"id": "pending", "status": "unresolved", "reason": "Keep me"}]
    elif damage == "classification": raw["clause_reviews"][0]["classification"] = "external_compliance"
    elif damage == "mixed": req["clause_ids"].append("heading-1")
    elif damage == "unknown_clause": req["clause_ids"] = ["absent"]
    elif damage == "wrong_evidence": req["evidence_ids"] = ["evidence-1"]
    elif damage == "wrong_span": chunk["clauses"][0]["source_span"]["text"] += "改"
    elif damage == "stale_span": chunk["clauses"][0]["source_span"]["source_sha256"] = "0" * 64
    elif damage == "stale_run": raw["provenance"] = {**chunk["provenance"], "run_id": "old"}
    elif damage == "duplicate_review": raw["clause_reviews"].append(copy.deepcopy(raw["clause_reviews"][0]))
    elif damage == "conflict": raw["reported_conflicts"] = [{"reason": "not resolved"}]
    elif damage == "missing_feedback": feedback.pop()
    elif damage == "duplicate_feedback": feedback.append(copy.deepcopy(feedback[0]))
    elif damage == "stale_feedback": feedback[0]["response_sha256"] = "0" * 64
    elif damage == "extra_error": feedback.append({"code": "other_error"})
    elif damage == "no_validation": validation = None
    else: validation = "0" * 64
    if damage not in {"missing_feedback", "duplicate_feedback", "stale_feedback", "extra_error"}:
        feedback = records(raw, chunk)
    # Bind fresh chunk marker even for a damaged exact span: exact-source
    # verification must reject it independently of the invocation marker.
    if damage in {"wrong_span", "stale_span"}: validation = sha256_json(chunk)
    assert bridge._project_unresolved_empty_requirements(raw, feedback, chunk,
        source_projection_validation_sha256=validation)[0] is None


def test_registered_executable_source_fact_is_not_deferred_through_a_shell():
    raw, chunk = fixture(source="未经批准的均为公开学位论文（公开的学位论文本项为空白）")
    assert project(bridge.normalize_native_response(raw, chunk["response_schema"]), chunk)[0] is None


def test_other_requirement_and_review_remain_byte_for_byte():
    raw, chunk = fixture(2)
    raw["requirements"][1]["properties"] = {"text": "7 相关研究"}
    raw["clause_reviews"][1].update(classification="executable", normative_basis="template_structure",
        obligations=[{"id": "fixed-text", "status": "covered", "reason": "literal preserved"}])
    raw = bridge.normalize_native_response(raw, chunk["response_schema"])
    result, _ = project(raw, chunk)
    assert result is not None and result["requirements"] == [raw["requirements"][1]]
    assert result["clause_reviews"] == raw["clause_reviews"]


def test_fresh_review_requires_source_inventory_and_full_remains_blocked():
    raw, chunk = fixture()
    candidate, _ = bridge.prepare_native_response_candidate(raw, chunk, source_projection_validation_sha256=sha256_json(chunk))
    candidate["provenance"] = copy.deepcopy(chunk["provenance"])
    request = build_obligation_coverage_request(candidate, chunk, run_id=chunk["provenance"]["run_id"], chunk_index=1)
    check = request["checks"][0]
    response = {"results": [{"check_id": "heading-0", "verdict": "consistent", "rationale": "Not established.",
        "evidence_quotes": ["7 相关研究"], "machine_obligation_ids": [], "identified_obligations": []}]}
    with pytest.raises(MissingSourceObligationInventoryError):
        validate_obligation_coverage_response(response, [check])
    response["results"][0].update(verdict="uncertain", identified_obligations=[{
        "source_quote": "7 相关研究", "disposition": "ambiguous", "requirement_refs": []}])
    assert validate_obligation_coverage_response(response, [check])[0]["verdict"] == "uncertain"
    clause_records = build_clause_records(chunk["clauses"], {r["clause_id"]: r for r in candidate["clause_reviews"]}, {})
    summary = summarize(clause_records)
    assert summary["docx_compliance"]["blocking_clause_ids"] == ["heading-0"]
    assert not summary["execution_ready"] and not summary["docx_fully_compliant"]


@pytest.mark.parametrize("outcome", ["uncertain", "missing_inventory"])
def test_native_candidate_separation_must_reach_fresh_independent_review(tmp_path, outcome):
    raw, seed = fixture(); directory = tmp_path / "packets"
    engine.prepare_host_agent_review_packets(seed, seed["clauses"], {"evidence": list(seed["evidence_context"].values())},
        seed["provenance"]["source_sha256"], directory, chunk_size=100)
    chunk = json.loads((directory / "llm-request-chunks.json").read_text())[0]
    calls, reviews = [], []
    def primary(command, **kwargs):
        calls.append(command); final = json.dumps({**raw, "provenance": chunk["provenance"]})
        Path(command[command.index("--output-last-message") + 1]).write_text(final)
        events = [{"type": "thread.started", "thread_id": "offline-unresolved"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": final}}, {"type": "turn.completed"}]
        return subprocess.CompletedProcess(command, 0, "\n".join(map(json.dumps, events)), "")
    def independent(candidate, source, **kwargs):
        reviews.append(candidate)
        assert candidate["requirements"] == [] and candidate["clause_reviews"] == raw["clause_reviews"]
        def builder(check, source_ref):
            return {"check_id": check["check_id"], "verdict": "uncertain" if outcome == "uncertain" else "consistent",
                "rationale": "The source heading level remains unknown.", "evidence_refs": [source_ref],
                "identified_obligations": [{"source_ref": source_ref, "disposition": "ambiguous", "requirement_refs": []}]
                    if outcome == "uncertain" else []}
        return bridge_fixtures.HostAgentBridgeTests._fake_independent_review(candidate, source, result_builder=builder, **kwargs)
    output = tmp_path / "merged.json"
    with patch.dict(os.environ, {"THESIS_FORGE_HOST_RUNTIME": "codex"}), \
         patch.object(bridge, "_resolve_codex", return_value="/offline/mock-codex"), \
         patch.object(bridge.codex_adapter, "probe_capabilities", return_value={"output_schema_supported": True, "structured_output_mode": "native_schema"}), \
         patch.object(bridge, "_run_command", side_effect=primary), \
         patch.object(bridge, "_run_independent_obligation_coverage_review", side_effect=independent):
        if outcome == "uncertain":
            bridge.run_bridge(directory, response_out=output, host_runtime="codex", codex_bin="/offline/mock-codex", timeout=1, max_attempts=1, output_policy="review_draft")
        else:
            with pytest.raises(ValueError, match="no source-obligation inventory"):
                bridge.run_bridge(directory, response_out=output, host_runtime="codex", codex_bin="/offline/mock-codex", timeout=1, max_attempts=1, output_policy="review_draft")
    assert len(calls) == len(reviews) == 1 and output.exists() is (outcome == "uncertain")
    audit = json.loads((directory / "host-agent-run.json").read_text())
    if outcome == "uncertain":
        assert audit["chunk_runs"][0]["mechanical_repairs"][0]["unresolved_semantics_preserved"]
        merged = json.loads(output.read_text())
        assert merged["clause_reviews"][0]["classification"] == "unresolved"
