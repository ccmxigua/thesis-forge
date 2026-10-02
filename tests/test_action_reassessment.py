"""Source-bound action proposals never certify equivalent wording or fulfilment."""
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
from source_condition_reassessment import (
    ACTION_CODE, ACTION_RULE_ID, source_atom_feedback, condition_proposal_budget_receipt,
)
from test_scope_field_patch import mixed_case
from test_target_reassessment import disputed_review
from test_target_reassessment import case as target_case


def rejected_action(candidate, chunk, attempt=1, action_only=False):
    request, rejected = disputed_review(candidate, chunk, attempt)
    for i, result in enumerate(rejected["results"]):
        atom = result["identified_obligations"][0]
        atom["target"] = candidate["clause_reviews"][i]["obligations"][0]["target"]
        if i == 0:
            atom["action"] = "describe the source's abstract quality duty"
            if not action_only: atom["target"] = "the exact abstract quality source sentence"
        else:
            atom["condition"] = "a disputed source context"
    return request, rejected


def case(action_only=False):
    raw, chunk, _, _ = mixed_case()
    candidate = bridge.prepare_native_response_candidate(raw, chunk)[0]
    candidate["provenance"] = copy.deepcopy(chunk["provenance"])
    request, rejected = rejected_action(candidate, chunk, action_only=action_only)
    record = source_atom_feedback(candidate, chunk, request, rejected)
    assert record and record["code"] == ACTION_CODE
    record["response_sha256"] = sha256_json(raw)
    proposal = copy.deepcopy(raw)
    proposal["clause_reviews"][0]["obligations"][0]["action"] = "write an academic abstract quality statement"
    if not action_only: proposal["clause_reviews"][0]["obligations"][0]["target"] = "this exact source-bound quality statement"
    proposal["clause_reviews"][1]["obligations"][0]["condition"] = "a new primary context proposal"
    return raw, candidate, chunk, record, proposal


@pytest.mark.parametrize("action_only", [False, True])
def test_action_and_mixed_grants_are_source_bound_not_reviewer_values(action_only):
    raw, candidate, chunk, record, proposal = case(action_only)
    frozen = copy.deepcopy((raw, candidate, chunk, record, proposal))
    assert record["source_atoms"][0]["fields"] == (["action"] if action_only else ["action", "target"])
    assert record["source_atoms"][1]["fields"] == ["condition"]
    receipt = condition_proposal_budget_receipt(candidate, chunk, [record], raw)
    assert receipt["proposal_limit"] == 1 and receipt["reassessment_code"] == ACTION_CODE
    ledger = []
    error, paths = bridge._retry_semantic_change_error(raw, proposal, [record], contract_version="3.0", chunk=chunk, authorization_out=ledger)
    assert error is None and len(paths) == (2 if action_only else 3)
    assert all(p["rule_id"] == ACTION_RULE_ID and p["independent_review_required"]
               and not p["mechanical_equivalence_claimed"] and "action_reassessment" in p for p in ledger)
    assert proposal["clause_reviews"][0]["obligations"][0]["action"] != record["rejected_review"]["results"][0]["identified_obligations"][0]["action"]
    assert (raw, candidate, chunk, record, proposal) == frozen


@pytest.mark.parametrize("damage", ["actor", "quote", "force", "status", "route", "classification", "requirement", "other_action", "blank_action", "unknown_action", "missing_action", "old_run", "old_source", "fields", "extra_error"])
def test_action_authority_cannot_change_other_semantics_or_trust_stale_feedback(damage):
    raw, _, chunk, record, proposal = case()
    records = [record]; atom = proposal["clause_reviews"][0]["obligations"][0]
    if damage in {"actor", "quote", "force", "status", "route"}: atom[{"quote": "source_quote"}.get(damage, damage)] = "unapproved"
    elif damage == "classification": proposal["clause_reviews"][0]["classification"] = "informational"
    elif damage == "requirement": proposal["requirements"][0]["reason"] += " changed"
    elif damage == "other_action": proposal["clause_reviews"][1]["obligations"][0]["action"] = "borrowed action grant"
    elif damage == "blank_action": atom["action"] = " "
    elif damage == "unknown_action": atom["action"] = "unknown"
    elif damage == "missing_action": atom.pop("action")
    elif damage == "old_run": record["run_id"] = "old"
    elif damage == "old_source": chunk["clauses"][0]["source_span"]["source_sha256"] = "0" * 64
    elif damage == "fields": record["source_atoms"][1]["fields"].append("action")
    else: records.append({"code": "other"})
    assert bridge._retry_semantic_change_error(raw, proposal, records, contract_version="3.0", chunk=chunk)[0] is not None


def test_surplus_action_grant_cannot_leak_to_another_atom():
    raw, _, chunk, record, proposal = case()
    proposal["clause_reviews"][1]["obligations"][0]["action"] = "unrequested action"
    result, audit = bridge._project_validator_targeted_obligation_fields(raw, proposal, [record], chunk=chunk)
    assert result is not None
    assert result["clause_reviews"][1]["obligations"][0]["action"] == raw["clause_reviews"][1]["obligations"][0]["action"]
    assert audit["discarded_unrequested_paths"] == ["$.clause_reviews[1].obligations[0].action"]
    assert audit["independent_review_required"] and not audit["submission_ready"]


def test_existing_target_only_grant_never_authorizes_action():
    raw, _, chunk, record = target_case()
    proposal = copy.deepcopy(raw)
    proposal["clause_reviews"][0]["obligations"][0].update(
        target="this source-bound quality sentence", action="an unrequested action")
    assert bridge._retry_semantic_change_error(raw, proposal, [record], contract_version="3.0", chunk=chunk)[0] is not None
    result, audit = bridge._project_validator_targeted_obligation_fields(raw, proposal, [record], chunk=chunk)
    assert result is not None
    assert result["clause_reviews"][0]["obligations"][0]["action"] == raw["clause_reviews"][0]["obligations"][0]["action"]
    assert all(p["authorized_atom_fields"] == ["target"] for p in audit["authorized_scope_proposals"])


@pytest.mark.parametrize("outcome", ["accept", "reject", "repeat", "stale"])
def test_native_one_shot_action_proposal_after_ordinary_budget_requires_fresh_review(tmp_path, outcome):
    raw, _, seed, _, proposal = case()
    directory = tmp_path / "packets"
    engine.prepare_host_agent_review_packets(seed, seed["clauses"], {"evidence": list(seed["evidence_context"].values())},
        seed["provenance"]["source_sha256"], directory, chunk_size=100)
    chunk = json.loads((directory / "llm-request-chunks.json").read_text())[0]
    calls, reviews = [], []
    def primary(command, **kwargs):
        calls.append(command)
        body = copy.deepcopy(raw if len(calls) <= 2 else proposal)
        body["provenance"] = copy.deepcopy(chunk["provenance"])
        final = "invalid JSON" if len(calls) == 1 else json.dumps(body)
        Path(command[command.index("--output-last-message") + 1]).write_text(final)
        events = [{"type": "thread.started", "thread_id": "offline-action"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": final}}, {"type": "turn.completed"}]
        return subprocess.CompletedProcess(command, 0, "\n".join(map(json.dumps, events)), "")
    def independent(candidate, source, **kwargs):
        reviews.append(kwargs["attempt"])
        if len(reviews) == 1 or outcome == "repeat":
            request, rejected = rejected_action(candidate, source, kwargs["attempt"])
            record = source_atom_feedback(candidate, source, request, rejected)
            assert record is not None
            if outcome == "stale": record["source_chunk_sha256"] = "0" * 64
            error = bridge.IndependentObligationReviewError("action and target primary proposal required")
            error.retryable = True; error.error_records = [record]
            raise error
        if outcome == "reject":
            error = bridge.IndependentObligationReviewError("fresh independent review rejects new action")
            error.retryable = False
            raise error
        from test_host_agent_bridge import HostAgentBridgeTests
        return HostAgentBridgeTests._fake_independent_review(candidate, source, **kwargs)
    output = tmp_path / "merged.json"
    with patch.dict(os.environ, {"THESIS_FORGE_HOST_RUNTIME": "codex"}), \
         patch.object(bridge, "_resolve_codex", return_value="/offline/mock-codex"), \
         patch.object(bridge.codex_adapter, "probe_capabilities", return_value={"output_schema_supported": True, "structured_output_mode": "native_schema"}), \
         patch.object(bridge, "_run_command", side_effect=primary), \
         patch.object(bridge, "_run_independent_obligation_coverage_review", side_effect=independent):
        if outcome == "accept":
            bridge.run_bridge(directory, response_out=output, host_runtime="codex", codex_bin="/offline/mock-codex", timeout=1, max_attempts=2)
        else:
            with pytest.raises((ValueError, bridge.IndependentObligationReviewError)):
                bridge.run_bridge(directory, response_out=output, host_runtime="codex", codex_bin="/offline/mock-codex", timeout=1, max_attempts=2)
    assert len(calls) == (2 if outcome == "stale" else 3)
    assert reviews == ([2] if outcome == "stale" else [2, 3])
    assert output.exists() is (outcome == "accept")
    audit = json.loads((directory / "host-agent-run.json").read_text())
    if outcome != "stale":
        prompt = (directory / "host-agent-prompts/prompt-0001-attempt-03.txt").read_text()
        assert "PRIMARY ACTION REASSESSMENT" in prompt and "Do not copy" in prompt
        last = audit["chunk_lifecycle"][0]["attempts"][-1]["retry_budget"]
        assert last["scope_proposals_started"] == 1 and last["current_attempt_kind"] == "action_proposal"
