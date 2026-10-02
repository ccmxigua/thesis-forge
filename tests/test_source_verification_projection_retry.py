"""Full candidate hashes and code-owned routes cannot authorize model drift."""
import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_cover_label_terminal_colon import packet
import host_agent_bridge as bridge
import requirements_engine as engine
from semantic_contract import sha256_json, sha256_bytes, attach_request_provenance


def fixture():
    original, _ = packet("", "unused", security=True)
    texts = ["关键词从论文中选取，在论文中有明确出处。", "关键词在摘要内容后另起一行，一般3～8个，之间用分号分开。"]
    clauses, evidence = [], {}
    for i, text in enumerate(texts):
        clause = copy.deepcopy(original["clauses"][i]); eid = clause["evidence_ids"][0]
        clause["text"] = text
        clause["source_span"].update(text=text, start_offset=0, end_offset=len(text), source_sha256=sha256_bytes(text.encode()))
        clauses.append(clause)
        evidence[eid] = {**original["evidence_context"][eid], "text": text}
    evidence_doc = {"evidence": list(evidence.values())}
    request = engine.build_llm_request([], clauses, evidence_doc, {}, "full", contract_version="3.0",
        runtime_context={"code_fingerprint_sha256": "f" * 64})
    request["case_id"] = "generic-keyword-verification"
    chunk = attach_request_provenance(request, source_sha256="a" * 64,
        evidence_doc=evidence_doc, clauses=clauses, run_id="current-generic-run")
    chunk["batch"] = {"index": 1}
    raw = {"contract_version": "3.0", "requirements": [{
        "role": "content_constraints", "properties": {"keywords_zh": {"separator": "semicolon"}},
        "clause_ids": [clauses[1]["id"]], "evidence_ids": clauses[1]["evidence_ids"],
        "reason": "Source-backed keyword separator.", "confidence": 0.9,
        "verification": {"mode": "manual", "checks": ["Check placement and advisory count."]}}],
        "clause_reviews": [{"clause_id": c["id"], "classification": "informational" if i == 0 else "executable",
            "reason": "Source analysis.", "normative_basis": "insufficient" if i == 0 else "explicit_normative_text",
            "obligations": [{"id": c["id"] + "-atom", "status": "covered", "reason": "Current source.",
                "source_quote": texts[i], "force": "optional" if i == 0 else "required",
                "applicability": "applicable", "route": "example" if i == 0 else "automatic"}]}
            for i, c in enumerate(clauses)], "unsupported_items": [], "reported_conflicts": []}
    return chunk, raw


def correction(parent, chunk):
    return {"code": "independent_obligation_review_incomplete", "clause_id": chunk["clauses"][0]["id"],
        "json_pointer": "$.clause_reviews[0].classification", "baseline_classification": "informational",
        "missing_source_quotes": [chunk["clauses"][0]["text"]], "evidence_ids": chunk["clauses"][0]["evidence_ids"],
        "candidate_response_sha256": sha256_json(bridge._bind_current_invocation_provenance(parent, chunk["provenance"])),
        "candidate_semantic_sha256": sha256_json(bridge._semantic_retry_view(parent)),
        "review_request_sha256": "1" * 64, "review_response_sha256": "2" * 64,
        "source_reference_compilation_sha256": "3" * 64, "primary_repairable": True,
        "primary_retry_authorization": "source_bound_existing_content_verification_reclassification_v1"}


def proposed(raw):
    current = copy.deepcopy(raw)
    current["clause_reviews"][0]["classification"] = "requires_source_verification"
    return current


def test_projection_preserves_model_atoms_and_records_code_owned_route():
    chunk, raw = fixture(); parent, _ = bridge.prepare_native_response_candidate(raw, chunk)
    assert parent != raw  # advisory keyword constraints are deterministically compiled
    current = proposed(raw); records = [correction(parent, chunk)]; ledger = []
    frozen = copy.deepcopy((raw, current, chunk, records))
    error, paths = bridge._retry_semantic_change_error(raw, current, records,
        contract_version="3.0", chunk=chunk, authorization_out=ledger)
    assert error is None and paths == ["$.clause_reviews[0].classification"]
    proof = ledger[0]["source_verification_projection_proof"]
    assert proof["code_owned_route_projection"][0]["old_value"] == "example"
    assert proof["code_owned_route_projection"][0]["new_value"] == "human"
    assert ledger[0]["independent_review_required"] and not ledger[0]["submission_ready"]
    assert (raw, current, chunk, records) == frozen


@pytest.mark.parametrize("damage", ["route", "atom", "quote", "requirement", "other_review", "stale", "source", "run", "extra_error", "confidence"])
def test_no_model_route_payload_or_stale_identity_permission(damage):
    chunk, raw = fixture(); parent, _ = bridge.prepare_native_response_candidate(raw, chunk)
    records = [correction(parent, chunk)]; current = proposed(raw)
    if damage == "route": current["clause_reviews"][0]["obligations"][0]["route"] = "human"
    elif damage == "atom": current["clause_reviews"][0]["obligations"][0]["force"] = "required"
    elif damage == "quote": current["clause_reviews"][0]["obligations"][0]["source_quote"] = "伪造出处"
    elif damage == "requirement": current["requirements"][0]["properties"]["keywords_zh"]["separator"] = "chinese_comma"
    elif damage == "other_review": current["clause_reviews"][1]["reason"] = "edited"
    elif damage == "stale": records[0]["candidate_response_sha256"] = "0" * 64
    elif damage == "source": chunk["clauses"][0]["source_span"]["source_sha256"] = "0" * 64
    elif damage == "run": current["provenance"] = {**chunk["provenance"], "run_id": "foreign"}
    elif damage == "extra_error": records.append({"code": "unrelated"})
    elif damage == "confidence": current["requirements"][0]["confidence"] = 1
    assert bridge._source_verification_projection_retry_ledger(
        raw, current, records, chunk, bridge._retry_change_paths(raw, current)) is None


@pytest.mark.parametrize("reject_independent", [False, True])
def test_native_retry_replays_and_requires_fresh_review(tmp_path, reject_independent):
    request, raw = fixture()
    evidence = {"evidence": list(request["evidence_context"].values())}
    directory = tmp_path / "packet"
    engine.prepare_host_agent_review_packets(request, request["clauses"], evidence,
        request["provenance"]["source_sha256"], directory, chunk_size=100)
    chunk = json.loads((directory / "llm-request-chunks.json").read_text())[0]
    calls, reviews = [], []
    def primary(command, **kwargs):
        calls.append(command)
        final = json.dumps(raw if len(calls) == 1 else proposed(raw))
        Path(command[command.index("--output-last-message") + 1]).write_text(final)
        events = [{"type": "thread.started", "thread_id": "offline-native"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": final}},
            {"type": "turn.completed"}]
        return subprocess.CompletedProcess(command, 0, "\n".join(map(json.dumps, events)), "")
    def independent(candidate, current_chunk, **kwargs):
        reviews.append(kwargs["attempt"])
        if kwargs["attempt"] == 1:
            error = bridge.IndependentObligationReviewError("source verification required")
            error.retryable = True; error.error_records = [correction(candidate, current_chunk)]
            raise error
        if reject_independent:
            error = bridge.IndependentObligationReviewError("fresh source review rejects")
            error.retryable = False
            raise error
        from test_host_agent_bridge import HostAgentBridgeTests
        return HostAgentBridgeTests._fake_independent_review(candidate, current_chunk, **kwargs)
    output = tmp_path / "merged.json"
    with patch.dict(os.environ, {"THESIS_FORGE_HOST_RUNTIME": "codex"}), \
         patch.object(bridge, "_resolve_codex", return_value="/offline/mock-codex"), \
         patch.object(bridge.codex_adapter, "probe_capabilities", return_value={
            "output_schema_supported": True, "structured_output_mode": "native_schema"}), \
         patch.object(bridge, "_run_command", side_effect=primary), \
         patch.object(bridge, "_run_independent_obligation_coverage_review", side_effect=independent):
        if reject_independent:
            with pytest.raises(bridge.IndependentObligationReviewError):
                bridge.run_bridge(directory, response_out=output, host_runtime="codex",
                    codex_bin="/offline/mock-codex", timeout=1, max_attempts=2)
        else:
            bridge.run_bridge(directory, response_out=output, host_runtime="codex",
                codex_bin="/offline/mock-codex", timeout=1, max_attempts=2)
    assert len(calls) == 2 and reviews == [1, 2]
    assert output.exists() is not reject_independent
    if not reject_independent:
        merged = json.loads(output.read_text())
        assert merged["clause_reviews"][0]["classification"] == "requires_source_verification"
        assert merged["clause_reviews"][0]["obligations"][0]["route"] == "human"
