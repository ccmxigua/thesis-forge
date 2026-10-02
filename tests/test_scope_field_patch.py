"""An atom-specific condition proposal cannot inherit a neighbour's target grant."""
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
from semantic_contract import sha256_json
from source_condition_reassessment import source_atom_feedback
from test_target_reassessment import disputed_review
from test_source_inventory_composition import fixture, completion


def mixed_case():
    seed, chunk = fixture(2)
    parent = completion(seed, chunk)
    for review in parent["clause_reviews"]:
        review["obligations"][0]["actor"] = "document"
        review["obligations"][0]["condition"] = None
    candidate = bridge.prepare_native_response_candidate(parent, chunk)[0]
    candidate["provenance"] = copy.deepcopy(chunk["provenance"])
    request, rejected = disputed_review(candidate, chunk, 1)
    second = rejected["results"][1]["identified_obligations"][0]
    second["target"] = parent["clause_reviews"][1]["obligations"][0]["target"]
    second["condition"] = "only in the indicated source context"
    record = source_atom_feedback(candidate, chunk, request, rejected)
    assert [e["fields"] for e in record["source_atoms"]] == [["target"], ["condition"]]
    record["response_sha256"] = sha256_json(parent)
    proposal = copy.deepcopy(parent)
    proposal["clause_reviews"][0]["obligations"][0]["target"] = "this source-bound abstract quality sentence"
    proposal["clause_reviews"][1]["obligations"][0].update(
        condition="a new primary scope proposal", target="unrequested broader target")
    return parent, chunk, record, proposal


def project(parent, proposal, records, chunk):
    return bridge._project_validator_targeted_obligation_fields(parent, proposal, records, chunk=chunk)


def test_exact_atom_field_grants_parent_preservation_and_authentication():
    parent, chunk, record, proposal = mixed_case()
    frozen = copy.deepcopy((parent, chunk, record, proposal))
    assert bridge._retry_semantic_change_error(parent, proposal, [record], contract_version="3.0", chunk=chunk)[0]
    result, audit = project(parent, proposal, [record], chunk)
    assert result is not None
    assert result["clause_reviews"][1]["obligations"][0]["target"] == parent["clause_reviews"][1]["obligations"][0]["target"]
    assert result["clause_reviews"][1]["obligations"][0]["condition"] == "a new primary scope proposal"
    assert result["requirements"] == parent["requirements"]
    assert audit["policy"] == "source_bound_scope_field_patch_v1"
    assert audit["discarded_unrequested_paths"] == ["$.clause_reviews[1].obligations[0].target"]
    assert len(audit["authorized_scope_proposals"]) == 2
    assert audit["independent_review_required"] and not audit["submission_ready"]
    assert audit["model_retry_response_sha256"] == sha256_json(proposal)
    assert bridge._retry_semantic_change_error(parent, result, [record], contract_version="3.0", chunk=chunk)[0] is None
    assert bridge.validate_host_agent_response(bridge.prepare_native_response_candidate(result, chunk)[0], chunk) == []
    assert (parent, chunk, record, proposal) == frozen


def test_authorized_only_proposal_uses_original_guard_not_surplus_projector():
    parent, chunk, record, proposal = mixed_case()
    proposal["clause_reviews"][1]["obligations"][0]["target"] = parent["clause_reviews"][1]["obligations"][0]["target"]
    assert project(parent, proposal, [record], chunk)[0] is None
    assert bridge._retry_semantic_change_error(parent, proposal, [record], contract_version="3.0", chunk=chunk)[0] is None


def test_compiled_parent_requires_same_complete_feedback_and_stage():
    parent, chunk, record, proposal = mixed_case()
    candidate = bridge.prepare_native_response_candidate(parent, chunk)[0]
    candidate["provenance"] = copy.deepcopy(chunk["provenance"])
    proposal = bridge.prepare_native_response_candidate(proposal, chunk)[0]
    proposal["provenance"] = copy.deepcopy(chunk["provenance"])
    result, audit = project(candidate, proposal, [record], chunk)
    assert result is not None and audit["parent_response_sha256"] == record["candidate_response_sha256"]
    assert bridge._retry_semantic_change_error(candidate, result, [record], contract_version="3.0", chunk=chunk)[0] is None


@pytest.mark.parametrize("damage", ["stale", "source", "fields", "review_quote", "extra_error", "run",
    "quote", "actor", "route", "classification", "requirements", "duplicate", "reorder", "missing", "bad_target", "bad_condition", "no_authorized_change"])
def test_unbound_feedback_identity_payload_and_non_scope_changes_fail(damage):
    parent, chunk, record, proposal = mixed_case()
    records = [record]
    atom = proposal["clause_reviews"][0]["obligations"][0]
    if damage == "stale": record["response_sha256"] = "0" * 64
    elif damage == "source": chunk["clauses"][0]["source_span"]["source_sha256"] = "0" * 64
    elif damage == "fields": record["source_atoms"][1]["fields"].append("target")
    elif damage == "review_quote": record["rejected_review"]["results"][0]["identified_obligations"][0]["source_quote"] = "forged"
    elif damage == "extra_error": records.append({"code": "other"})
    elif damage == "run": proposal["provenance"]["run_id"] = "old"
    elif damage in {"quote", "actor", "route"}: atom[{"quote": "source_quote"}.get(damage, damage)] = "unrequested"
    elif damage == "classification": proposal["clause_reviews"][0]["classification"] = "informational"
    elif damage == "requirements": proposal["requirements"][0]["reason"] += " changed"
    elif damage == "duplicate": proposal["clause_reviews"][0]["obligations"].append(copy.deepcopy(atom))
    elif damage == "reorder": proposal["clause_reviews"].reverse()
    elif damage == "missing": proposal["clause_reviews"][0]["obligations"] = []
    elif damage == "bad_target": atom["target"] = "unknown"
    elif damage == "bad_condition": proposal["clause_reviews"][1]["obligations"][0]["condition"] = 42
    else:
        atom["target"] = parent["clause_reviews"][0]["obligations"][0]["target"]
        proposal["clause_reviews"][1]["obligations"][0]["condition"] = None
    assert project(parent, proposal, records, chunk)[0] is None


@pytest.mark.parametrize("reject", [False, True])
def test_native_retry_patch_requires_fresh_review_and_does_not_reset_budget(tmp_path, reject):
    parent, seed, _, proposal = mixed_case()
    directory = tmp_path / "packets"
    engine.prepare_host_agent_review_packets(seed, seed["clauses"],
        {"evidence": list(seed["evidence_context"].values())}, seed["provenance"]["source_sha256"], directory, chunk_size=100)
    current_chunk = json.loads((directory / "llm-request-chunks.json").read_text())[0]
    calls, reviews = [], []
    def primary(command, **kwargs):
        calls.append(command)
        body = copy.deepcopy(parent if len(calls) == 1 else proposal)
        body["provenance"] = copy.deepcopy(current_chunk["provenance"])
        final = json.dumps(body)
        Path(command[command.index("--output-last-message") + 1]).write_text(final)
        events = [{"type": "thread.started", "thread_id": "offline-scope"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": final}}, {"type": "turn.completed"}]
        return subprocess.CompletedProcess(command, 0, "\n".join(map(json.dumps, events)), "")
    def independent(candidate, chunk, **kwargs):
        reviews.append(copy.deepcopy(candidate))
        if len(reviews) == 1:
            request, rejected = disputed_review(candidate, chunk, kwargs["attempt"])
            atom = rejected["results"][1]["identified_obligations"][0]
            atom["target"] = candidate["clause_reviews"][1]["obligations"][0]["target"]
            atom["condition"] = "source scope dispute"
            record = source_atom_feedback(candidate, chunk, request, rejected)
            error = bridge.IndependentObligationReviewError("new primary scope proposal required")
            error.retryable = True; error.error_records = [record]
            raise error
        assert candidate["clause_reviews"][1]["obligations"][0]["target"] == parent["clause_reviews"][1]["obligations"][0]["target"]
        if reject:
            error = bridge.IndependentObligationReviewError("fresh independent source review rejects scope")
            error.retryable = False
            raise error
        from test_host_agent_bridge import HostAgentBridgeTests
        return HostAgentBridgeTests._fake_independent_review(candidate, chunk, **kwargs)
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
    assert len(calls) == 2 and len(reviews) == 2
    assert output.exists() is not reject
    run = json.loads((directory / "host-agent-run.json").read_text())
    assert all(a["retry_budget"]["scope_proposals_started"] <= 1
               for c in run["chunk_lifecycle"] for a in c["attempts"])
