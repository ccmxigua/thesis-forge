"""Compound local diagnostics authorize proposals, not source interpretations."""
import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path[:0] = [str(Path(__file__).resolve().parent),
               str(Path(__file__).resolve().parents[1] / "scripts")]
import host_agent_bridge as bridge
import requirements_engine as engine
from host_review_contract import contract_error_records, MIXED_INVENTORY_CODE
from local_atom_reassessment import POLICY
from semantic_contract import sha256_json
from test_source_inventory_composition import fixture, completion


def case():
    raw, chunk = fixture(3)
    parent = bridge.prepare_native_response_candidate(completion(raw, chunk), chunk)[0]
    parent["provenance"] = copy.deepcopy(chunk["provenance"])
    reviews = parent["clause_reviews"]
    reviews[0]["classification"] = "executable_with_external_check"
    reviews[0]["obligations"][0]["route"] = "automatic"
    reviews[1]["obligations"][0]["applicability"] = "unknown"
    reviews[2]["obligations"][0]["source_quote"] = "a stitched quotation absent from the source"
    records = contract_error_records(bridge.validate_host_agent_response(parent, chunk), response=parent, chunk=chunk)
    assert len(records) == 3
    proposal = copy.deepcopy(parent)
    added = copy.deepcopy(reviews[0]["obligations"][0])
    added.update(id="dynamic-extra-human-claim", status="unverifiable", route="human",
                 reason="A synthetic primary claim, not proof of a source duty")
    proposal["clause_reviews"][0]["obligations"].append(added)
    proposal["clause_reviews"][1]["obligations"][0]["applicability"] = "applicable"
    proposal["clause_reviews"][2]["obligations"][0]["source_quote"] = chunk["clauses"][2]["source_span"]["text"]
    proposal["clause_reviews"][1]["reason"] += " unrequested revision"
    proposal["clause_reviews"][2]["obligations"][0]["target"] = "unrequested broader scope"
    return parent, proposal, records, chunk


def project(parent, proposal, records, chunk):
    return bridge._project_validator_targeted_obligation_fields(parent, proposal, records, chunk=chunk)


def test_compound_projection_is_exact_current_source_bound_and_preserves_parent():
    parent, proposal, records, chunk = case()
    frozen = copy.deepcopy((parent, proposal, records, chunk))
    assert bridge._retry_semantic_change_error(parent, proposal, records, contract_version="3.0", chunk=chunk)[0]
    candidate, audit = project(parent, proposal, records, chunk)
    assert candidate is not None and audit["policy"] == POLICY and audit["status"] == "projected"
    assert candidate["requirements"] == parent["requirements"]
    assert candidate["clause_reviews"][0]["obligations"][:-1] == parent["clause_reviews"][0]["obligations"]
    assert candidate["clause_reviews"][1]["reason"] == parent["clause_reviews"][1]["reason"]
    assert candidate["clause_reviews"][2]["obligations"][0]["target"] == parent["clause_reviews"][2]["obligations"][0]["target"]
    assert len(audit["authorized_proposals"]) == len(audit["applied_paths"]) == 3
    assert audit["discarded_unrequested_paths"] == ["$.clause_reviews[1].reason", "$.clause_reviews[2].obligations[0].target"]
    assert audit["model_retry_response_sha256"] == sha256_json(proposal)
    assert not audit["mechanical_equivalence_claimed"] and not audit["submission_ready"]
    assert audit["independent_review_required"]
    for proof in audit["authorized_proposals"]:
        assert proof["primary_semantic_reassessment"] and proof["source_binding_complete"]
        assert proof["source_chunk_sha256"] == sha256_json(chunk)
    ledger = []
    assert bridge._retry_semantic_change_error(parent, candidate, records, contract_version="3.0", chunk=chunk, authorization_out=ledger)[0] is None
    assert ledger == audit["authorized_proposals"]
    assert bridge.validate_host_agent_response(candidate, chunk) == []
    assert (parent, proposal, records, chunk) == frozen


@pytest.mark.parametrize("wrong_code", ["contract_validation_error", "unknown", "primary_all_covered_classification_reassessment_required"])
def test_typed_inventory_diagnostic_is_not_a_generic_or_other_proposal_grant(wrong_code):
    parent, proposal, records, chunk = case()
    inventory = next(r for r in records if r["code"] == MIXED_INVENTORY_CODE)
    inventory["code"] = wrong_code
    assert project(parent, proposal, records, chunk)[0] is None


@pytest.mark.parametrize("field", ["applicability", "source_quote"])
@pytest.mark.parametrize("wrong_code", [MIXED_INVENTORY_CODE, "primary_all_covered_classification_reassessment_required"])
def test_inventory_or_classification_code_cannot_authorize_a_field_path(field, wrong_code):
    parent, proposal, records, chunk = case()
    record = next(r for r in records if r["json_pointer"].endswith("." + field))
    record["code"] = wrong_code
    assert project(parent, proposal, records, chunk)[0] is None


@pytest.mark.parametrize("damage", ["stale_parent", "missing_error", "duplicate_error", "invented_error", "wrong_clause",
    "wrong_pointer", "old_run", "unbound_parent", "source", "evidence", "missing_code", "missing_schema",
    "reorder", "missing_review", "duplicate_review", "existing_atom_edit", "inventory_delete", "duplicate_atom",
    "wrong_status", "wrong_route", "invented_quote", "neighbour_quote", "empty_quote", "conflict",
    "new_atom_schema", "not_applicable", "unchanged", "other_parent_error"])
def test_incomplete_feedback_stale_binding_and_wider_edits_cannot_be_authorized(damage):
    parent, proposal, records, chunk = case()
    new = proposal["clause_reviews"][0]["obligations"][-1]
    if damage == "stale_parent": records[0]["response_sha256"] = "0" * 64
    elif damage == "missing_error": records.pop()
    elif damage == "duplicate_error": records.append(copy.deepcopy(records[0]))
    elif damage == "invented_error": records.append({"code": "contract_validation_error"})
    elif damage == "wrong_clause": records[0]["clause_id"] = "unlinked-clause"
    elif damage == "wrong_pointer": records[0]["json_pointer"] = "$.requirements[0]"
    elif damage == "old_run": proposal["provenance"]["run_id"] = "other"
    elif damage == "unbound_parent": parent.pop("provenance")
    elif damage == "source": chunk["clauses"][0]["source_span"]["source_sha256"] = "0" * 64
    elif damage == "evidence": chunk["evidence_context"][chunk["clauses"][0]["evidence_ids"][0]]["text"] += " changed"
    elif damage == "missing_code": chunk["runtime_context"] = {}
    elif damage == "missing_schema": chunk["response_schema"] = {}
    elif damage == "reorder": proposal["clause_reviews"].reverse()
    elif damage == "missing_review": proposal["clause_reviews"].pop()
    elif damage == "duplicate_review": proposal["clause_reviews"].append(copy.deepcopy(proposal["clause_reviews"][0]))
    elif damage == "existing_atom_edit": proposal["clause_reviews"][0]["obligations"][0]["reason"] += " changed"
    elif damage == "inventory_delete": proposal["clause_reviews"][0]["obligations"] = [new]
    elif damage == "duplicate_atom": new["id"] = proposal["clause_reviews"][0]["obligations"][0]["id"]
    elif damage == "wrong_status": new["status"] = "covered"
    elif damage == "wrong_route": new["route"] = "automatic"
    elif damage == "invented_quote": new["source_quote"] = "not source text"
    elif damage == "neighbour_quote":
        new["source_quote"] = "another evidence only"
        chunk["evidence_context"][chunk["clauses"][2]["evidence_ids"][0]]["text"] = new["source_quote"]
    elif damage == "empty_quote": proposal["clause_reviews"][2]["obligations"][0]["source_quote"] = ""
    elif damage == "conflict": proposal["reported_conflicts"] = [{"reason": "a new conflict must not be suppressed"}]
    elif damage == "new_atom_schema": new["invented_field"] = True
    elif damage == "not_applicable": proposal["clause_reviews"][1]["obligations"][0]["applicability"] = "not_applicable"
    elif damage == "unchanged": proposal = copy.deepcopy(parent)
    else:
        parent["requirements"][0]["properties"]["unsupported"] = True
        records = contract_error_records(bridge.validate_host_agent_response(parent, chunk), response=parent, chunk=chunk)
    assert project(parent, proposal, records, chunk)[0] is None


def test_authorization_cannot_be_reused_for_a_different_candidate():
    parent, proposal, records, chunk = case()
    candidate, _ = project(parent, proposal, records, chunk)
    candidate["clause_reviews"][0]["reason"] += " extra"
    assert bridge._retry_semantic_change_error(parent, candidate, records, contract_version="3.0", chunk=chunk)[0]


@pytest.mark.parametrize("reject", [False, True])
@pytest.mark.parametrize("omit_model_provenance", [False, True])
def test_native_compound_proposal_requires_fresh_review_with_unchanged_attempt_budget(tmp_path, reject, omit_model_provenance):
    parent, proposal, _, seed = case()
    directory = tmp_path / "packets"
    engine.prepare_host_agent_review_packets(seed, seed["clauses"], {"evidence": list(seed["evidence_context"].values())},
        seed["provenance"]["source_sha256"], directory, chunk_size=100)
    chunk = json.loads((directory / "llm-request-chunks.json").read_text())[0]
    calls, reviews = [], []
    def primary(command, **kwargs):
        calls.append(command)
        body = copy.deepcopy(parent if len(calls) == 1 else proposal)
        body["provenance"] = copy.deepcopy(chunk["provenance"])
        if omit_model_provenance:
            body.pop("provenance")
        final = json.dumps(body)
        Path(command[command.index("--output-last-message") + 1]).write_text(final)
        events = [{"type": "thread.started", "thread_id": "offline-compound"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": final}}, {"type": "turn.completed"}]
        return subprocess.CompletedProcess(command, 0, "\n".join(map(json.dumps, events)), "")
    def independent(candidate, current, **kwargs):
        reviews.append(copy.deepcopy(candidate))
        assert len(calls) == 2
        assert candidate["clause_reviews"][1]["reason"] == parent["clause_reviews"][1]["reason"]
        assert candidate["clause_reviews"][2]["obligations"][0]["target"] == parent["clause_reviews"][2]["obligations"][0]["target"]
        if reject:
            error = bridge.IndependentObligationReviewError("Synthetic added human duty has no source proof")
            error.retryable = False
            raise error
        from test_host_agent_bridge import HostAgentBridgeTests
        return HostAgentBridgeTests._fake_independent_review(candidate, current, **kwargs)
    output = tmp_path / "merged.json"
    with patch.dict(os.environ, {"THESIS_FORGE_HOST_RUNTIME": "codex"}), \
         patch.object(bridge, "_resolve_codex", return_value="/offline/mock-codex"), \
         patch.object(bridge.codex_adapter, "probe_capabilities", return_value={"output_schema_supported": True, "structured_output_mode": "native_schema"}), \
         patch.object(bridge, "_run_command", side_effect=primary), \
         patch.object(bridge, "_run_independent_obligation_coverage_review", side_effect=independent):
        if reject:
            with pytest.raises(bridge.IndependentObligationReviewError):
                bridge.run_bridge(directory, response_out=output, host_runtime="codex", codex_bin="/offline/mock-codex", timeout=1, max_attempts=2)
        else:
            bridge.run_bridge(directory, response_out=output, host_runtime="codex", codex_bin="/offline/mock-codex", timeout=1, max_attempts=2)
    assert len(calls) == 2 and len(reviews) == 1
    assert output.exists() is not reject
    artifact = directory / "llm-response-chunk-0001.attempt-02.raw.json"
    saved = json.loads(artifact.read_text())
    assert saved["clause_reviews"][1]["reason"] == proposal["clause_reviews"][1]["reason"]
    run = json.loads((directory / "host-agent-run.json").read_text())
    assert len(run["chunk_lifecycle"][0]["attempts"]) == 2
    attempt = run["chunk_lifecycle"][0]["attempts"][0]
    receipt = bridge._retry_semantic_parent_receipt([
        bridge._validate_retry_attempt_artifact(directory / "llm-response-chunk-0001.json", 1, attempt)])
    stored = json.loads(Path(receipt["path"]).read_text())
    records = contract_error_records(bridge.validate_host_agent_response(stored, chunk), response=stored, chunk=chunk)
    proof = bridge._verified_local_atom_parent_binding(receipt, attempt, stored, chunk, records)
    assert proof and proof["accepted"] is False
    assert proof["input_fingerprints"] == bridge._retry_input_fingerprints(chunk)
    assert stored.get("provenance") is None if omit_model_provenance else stored.get("provenance") == chunk["provenance"]
    for field in ("sha256", "canonical_json_sha256"):
        stale = copy.deepcopy(receipt); stale[field] = "0" * 64
        assert bridge._verified_local_atom_parent_binding(stale, attempt, stored, chunk, records) is None
    old_attempt = copy.deepcopy(attempt)
    old_attempt["retry_input_fingerprints"]["run_id"] = "old"
    assert bridge._verified_local_atom_parent_binding(receipt, old_attempt, stored, chunk, records) is None
    stale_raw = copy.deepcopy(receipt)
    stale_raw["decoded_raw_receipt"]["sha256"] = "0" * 64
    assert bridge._verified_local_atom_parent_binding(stale_raw, attempt, stored, chunk, records) is None
    assert bridge._verified_local_atom_parent_binding(receipt, attempt, stored, chunk, records[:-1]) is None


def test_reverse_inventory_adds_automatic_proposal_without_claiming_external_completion():
    parent, proposal, _, chunk = case()
    parent["clause_reviews"][0]["obligations"] = copy.deepcopy(proposal["clause_reviews"][0]["obligations"][-1:])
    proposal = copy.deepcopy(parent)
    added = copy.deepcopy(parent["clause_reviews"][0]["obligations"][0])
    added.update(id="new-automatic-proposal", status="covered", route="automatic")
    proposal["clause_reviews"][0]["obligations"].append(added)
    proposal["clause_reviews"][1]["obligations"][0]["applicability"] = "applicable"
    proposal["clause_reviews"][2]["obligations"][0]["source_quote"] = chunk["clauses"][2]["source_span"]["text"]
    records = contract_error_records(bridge.validate_host_agent_response(parent, chunk), response=parent, chunk=chunk)
    candidate, audit = project(parent, proposal, records, chunk)
    assert candidate is not None and audit["independent_review_required"]
    assert candidate["clause_reviews"][0]["obligations"][0] == parent["clause_reviews"][0]["obligations"][0]
    assert candidate["clause_reviews"][0]["obligations"][0]["status"] == "unverifiable"
