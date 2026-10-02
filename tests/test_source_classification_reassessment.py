"""Captured native failure, dynamic identities, bounded proposals and refusal."""
import copy
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

import host_agent_bridge as bridge
import requirements_engine as engine
from semantic_contract import attach_request_provenance
from source_classification_reassessment import CODE, ERROR_CODE, RULE_ID, feedback, reassessment

CASE = Path(__file__).parent / "fixtures/all-covered-mixed-classification-incident.json"


def fixture(attempt=1, *, rename=True):
    case = json.loads(CASE.read_text())
    chunk = case["chunk"]
    raw = case["raw_responses"][f"llm-response-chunk-0006.attempt-{attempt:02d}.raw.json"]
    if rename:
        # Not a rule keyed by school, clause ID, evidence ID or run identity.
        names = {c["id"]: f"different-clause-{i}" for i, c in enumerate(chunk["clauses"])}
        def rename_ids(value):
            if isinstance(value, dict): return {k: rename_ids(v) for k, v in value.items()}
            if isinstance(value, list): return [rename_ids(v) for v in value]
            return names.get(value, value) if isinstance(value, str) else value
        chunk, raw = rename_ids(chunk), rename_ids(raw)
        evidence = engine._evidence_doc_from_full_request(chunk)
        chunk = attach_request_provenance(chunk, source_sha256=chunk["provenance"]["source_sha256"],
            evidence_doc=evidence, clauses=chunk["clauses"], run_id="different-test-run")
        raw["provenance"] = copy.deepcopy(chunk["provenance"])
    return chunk, raw


def rejected(chunk, raw):
    with pytest.raises(ValueError) as caught:
        bridge.prepare_native_response_candidate(raw, chunk,
            source_projection_validation_sha256=bridge._response_sha256(chunk))
    return caught.value.repair_base_candidate, caught.value.retry_authorizing_error_records


def proposal(parent, classification="executable"):
    current = copy.deepcopy(parent)
    current["clause_reviews"][2]["classification"] = classification
    return current


def authorize(previous, current, records, chunk):
    return reassessment(previous, current, records, bridge._retry_change_paths(previous, current), chunk,
        prepare=bridge.prepare_native_response_candidate, validate=bridge.validate_host_agent_response,
        changed_paths=bridge._retry_change_paths)


@pytest.mark.parametrize("attempt", [1, 2])
@pytest.mark.parametrize("classification", ["covered", "executable"])
def test_captured_failure_replays_at_same_stage_and_preserves_every_other_field(attempt, classification):
    chunk, raw = fixture(attempt)
    original = copy.deepcopy(raw)
    parent, records = rejected(chunk, raw)
    assert [r["code"] for r in records] == [ERROR_CODE, CODE]
    assert raw == original
    envelope = records[-1]
    assert envelope["targets"][0]["clause_id"] == "different-clause-2"
    assert len(parent["clause_reviews"][2]["obligations"]) == 2
    assert parent["clause_reviews"][1]["classification"] == "external_compliance"
    assert len(parent["clause_reviews"][1]["obligations"]) == 3
    current = proposal(parent, classification)
    candidate, _ = bridge.prepare_native_response_candidate(current, chunk,
        source_projection_validation_sha256=bridge._response_sha256(chunk))
    assert bridge._retry_change_paths(parent, candidate) == ["$.clause_reviews[2].classification"]
    for previous in (raw, parent):
        proofs = authorize(previous, current, records, chunk)
        assert proofs and all(p["rule_id"] == RULE_ID for p in proofs)
        assert all(p["source_binding_complete"] and p["independent_review_required"]
                   and not p["mechanical_equivalence_claimed"] and not p["submission_ready"] for p in proofs)
        output = []
        error, paths = bridge._retry_semantic_change_error(previous, current, records,
            contract_version="3.0", chunk=chunk, authorization_out=output)
        assert error is None and len(output) == len(paths)
    assert authorize(parent, parent, records, chunk) is None
    with pytest.raises(ValueError):
        bridge.prepare_native_response_candidate(parent, chunk,
            source_projection_validation_sha256=bridge._response_sha256(chunk))


@pytest.mark.parametrize("change", ["drop", "add", "status", "route", "quote", "force", "target", "condition",
    "actor", "atom_reason", "review_reason", "normative", "order", "requirements", "edge", "property",
    "other_review", "confidence", "classification_unresolved", "classification_external", "classification_verify_existing", "old_run",
    "old_source", "span", "foreign_evidence", "forged_target", "forged_record", "partial_bundle"])
def test_no_new_duties_no_payload_or_source_edits_and_no_stale_authority(change):
    chunk, raw = fixture(); parent, records = rejected(chunk, raw); current = proposal(parent)
    review = current["clause_reviews"][2]
    atom = review["obligations"][0]
    if change == "drop": review["obligations"].pop()
    elif change == "add": review["obligations"].append(copy.deepcopy(parent["clause_reviews"][1]["obligations"][0]))
    elif change == "status": atom["status"] = "unverifiable"
    elif change == "route": atom["route"] = "human"
    elif change == "quote": atom["source_quote"] = parent["clause_reviews"][1]["obligations"][0]["source_quote"]
    elif change in {"force", "target", "condition", "actor"}: atom[change] = "changed"
    elif change == "atom_reason": atom["reason"] += "changed"
    elif change == "review_reason": review["reason"] += "changed"
    elif change == "normative": review["normative_basis"] = "external_duty"
    elif change == "order": review["obligations"].reverse()
    elif change == "requirements": current["requirements"].clear()
    elif change == "edge": current["requirements"][0]["clause_ids"].reverse()
    elif change == "property": current["requirements"][0]["properties"]["non_public_administration"]["public_policy"] = "placeholder"
    elif change == "other_review": current["clause_reviews"][1]["reason"] += "changed"
    elif change == "confidence": current["requirements"][0]["confidence"] = 0.99
    elif change.startswith("classification_"): review["classification"] = change.removeprefix("classification_")
    elif change == "old_run": chunk["provenance"]["run_id"] = "old-run"
    elif change == "old_source": chunk["clauses"][2]["source_span"]["source_sha256"] = "0" * 64
    elif change == "span": chunk["clauses"][2]["source_span"]["start_offset"] -= 1
    elif change == "foreign_evidence": chunk["clauses"][2]["source_span"]["evidence_id"] = "foreign"
    elif change == "forged_target": records[-1]["targets"][0]["review_index"] = 1
    elif change == "forged_record": records[0]["clause_id"] = chunk["clauses"][1]["id"]
    elif change == "partial_bundle": records.pop(0)
    assert authorize(parent, current, records, chunk) is None
    error, _ = bridge._retry_semantic_change_error(parent, current, records,
        contract_version="3.0", chunk=chunk)
    assert error is not None


@pytest.mark.parametrize("change", ["empty", "unverifiable", "real_mixed", "unknown", "unrelated", "context_quote"])
def test_dispatch_does_not_guess_or_fix_other_inventory_classes(change):
    chunk, raw = fixture(); parent, _ = rejected(chunk, raw)
    atoms = parent["clause_reviews"][2]["obligations"]
    if change == "empty": atoms.clear()
    elif change == "unverifiable":
        for atom in atoms: atom.update(status="unverifiable", route="human")
    elif change == "real_mixed": atoms[0].update(status="unverifiable", route="human")
    elif change == "unknown": atoms[0]["applicability"] = "unknown"
    elif change == "unrelated": parent["requirements"][0]["properties"]["invented"] = True
    elif change == "context_quote":
        # The complete shared evidence contains the current span plus sibling duties.
        atoms[0]["source_quote"] = chunk["evidence_context"][chunk["clauses"][2]["source_span"]["evidence_id"]]["text"]
    records = bridge.contract_error_records(bridge.validate_host_agent_response(parent, chunk), response=parent, chunk=chunk)
    assert feedback(parent, chunk, records, validate=bridge.validate_host_agent_response,
        source_projection_validation_sha256=bridge._response_sha256(chunk)) is None


def test_prompt_preserves_candidate_inventory_and_does_not_instruct_repairing_status(tmp_path):
    chunk, raw = fixture(); parent, records = rejected(chunk, raw)
    request = tmp_path / "chunk.json"; baseline = tmp_path / "parent.json"
    bridge._write_json(request, chunk); bridge._write_json(baseline, parent)
    prompt = bridge._host_prompt(request_path=request, chunk_path=request, response_path=tmp_path / "out.json",
        run_id=chunk["provenance"]["run_id"], chunk_index=6, chunk_count=61, attempt=2,
        retry_hint="mixed inventory contradiction", retry_parent_response_path=baseline, retry_error_records=records)
    assert "CLASSIFICATION-ONLY REASSESSMENT" in prompt
    assert "Freeze every obligation" in prompt
    assert "never invent an external duty" in prompt.lower()
    assert "bounded primary semantic proposal" in prompt
    assert "do not reclassify a clause." not in prompt
    assert "copy every clause_review classification" not in prompt
    assert "FINAL RETRY INVARIANT: bounded CLASSIFICATION-ONLY REASSESSMENT" in prompt
    assert "Fresh independent source review is mandatory" in prompt


@pytest.mark.parametrize("fresh_review_rejects", [False, True])
def test_native_bridge_requires_new_independent_review_before_merge(tmp_path, fresh_review_rejects):
    request, raw = fixture(rename=False)
    directory = tmp_path / "packet"
    evidence = engine._evidence_doc_from_full_request(request)
    engine.prepare_host_agent_review_packets(request, request["clauses"], evidence,
        request["provenance"]["source_sha256"], directory, chunk_size=100)
    chunk = json.loads((directory / "llm-request-chunks.json").read_text())[0]
    raw["provenance"] = copy.deepcopy(chunk["provenance"])
    parent, _ = rejected(chunk, raw); current = proposal(parent)
    calls, reviews = [], []

    def primary(command, **kwargs):
        calls.append(command)
        final = json.dumps(raw if len(calls) == 1 else current, ensure_ascii=False)
        Path(command[command.index("--output-last-message") + 1]).write_text(final)
        events = [{"type": "thread.started", "thread_id": "offline-native"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": final}},
            {"type": "turn.completed", "usage": {}}]
        return subprocess.CompletedProcess(command, 0, "\n".join(map(json.dumps, events)), "")

    def independent(candidate, current_chunk, **kwargs):
        reviews.append(kwargs["attempt"])
        assert not bridge.validate_host_agent_response(candidate, current_chunk)
        assert len(candidate["clause_reviews"][1]["obligations"]) == 3
        if fresh_review_rejects:
            error = bridge.IndependentObligationReviewError("fresh source review rejects proposal")
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
        if fresh_review_rejects:
            with pytest.raises(bridge.IndependentObligationReviewError):
                bridge.run_bridge(directory, response_out=output, host_runtime="codex",
                    codex_bin="/offline/mock-codex", codex_model="gpt-6-luna", timeout=1, max_attempts=2)
        else:
            bridge.run_bridge(directory, response_out=output, host_runtime="codex",
                codex_bin="/offline/mock-codex", codex_model="gpt-6-luna", timeout=1, max_attempts=2)
    assert len(calls) == 2 and reviews == [2]
    assert output.exists() is not fresh_review_rejects
    audit = json.loads((directory / "host-agent-run.json").read_text())
    assert (audit["status"] == "merged") is not fresh_review_rejects
    assert audit["chunk_lifecycle"][0]["retry_parent_stage"] == "unaccepted_repair_base"
    replay_proof = audit["chunk_lifecycle"][0]["retry_repair_base_replay_proof"]
    assert replay_proof["protocol"] == "retry_repair_base_replay_v1"
    assert replay_proof["accepted"] is False
    assert replay_proof["raw_sha256"] != replay_proof["repair_base_sha256"]
    assert (directory / "llm-response-chunk-0001.attempt-01.repair-base.json").is_file()
    assert (directory / "llm-response-chunk-0001.attempt-01.raw.json").is_file()
    assert (directory / "llm-response-chunk-0001.attempt-02.raw.json").is_file()


def test_multiple_targets_at_other_positions_keep_graph_and_inventories_frozen():
    chunk, raw = fixture()
    # Make an additional existing executable review contradictory, not a new duty.
    raw["clause_reviews"][4]["classification"] = "executable_with_external_check"
    raw["clause_reviews"].reverse()
    parent, records = rejected(chunk, raw)
    envelope = records[-1]
    assert envelope["code"] == CODE and len(envelope["targets"]) == 2
    current = copy.deepcopy(parent)
    for target in envelope["targets"]:
        current["clause_reviews"][target["review_index"]]["classification"] = "executable"
    proofs = authorize(parent, current, records, chunk)
    assert proofs and len(proofs) == 2
    assert {p["json_pointer"] for p in proofs} == {
        f"$.clause_reviews[{t['review_index']}].classification" for t in envelope["targets"]}
    assert current["requirements"] == parent["requirements"]
    for old, new in zip(parent["clause_reviews"], current["clause_reviews"]):
        assert {k: v for k, v in old.items() if k != "classification"} == {
            k: v for k, v in new.items() if k != "classification"}
