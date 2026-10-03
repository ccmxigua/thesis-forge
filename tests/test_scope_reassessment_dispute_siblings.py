"""Pending inventory disagreements must not suppress unrelated bounded proposals.

Synthetic source-bound packets exercise real validation/routing with mocked
transport. Neither wording equivalence nor real-provider acceptance is asserted.
"""
import copy
import hashlib
import json
import os
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import host_agent_bridge as bridge
import native_semantic_review as native
import source_condition_reassessment as reassessment
from semantic_contract import attach_request_provenance, sha256_json
from requirements_engine import build_llm_request, prepare_host_agent_review_packets
from source_inventory_dispute import build_inventory_existence_disputes
from independent_retry_scope import (
    prepare_retry_scope, constrain_retry_schema, validate_retry_scope, validate_persisted_empty_inventory_scope,
)
from semantic_source_references import source_reference_schema, build_source_reference_packet
from test_action_reassessment import rejected_action
from test_scope_field_patch import mixed_case
import test_independent_retry_scope as retry_fixtures
import test_empty_inventory_verdict as transport


def packet(source="Reviewer code:", label_id="label-without-proven-duty"):
    raw, seed, _, _ = mixed_case()
    clauses = copy.deepcopy(seed["clauses"])
    evidence = copy.deepcopy(list(seed["evidence_context"].values()))
    eid = "label-evidence"
    location = {"part": "document", "child_index": len(clauses), "order": len(clauses)}
    evidence.append({"id": eid, "kind": "paragraph", "text": source, "location": location})
    clauses.append({"id": label_id, "text": source, "evidence_ids": [eid], "source_kind": "paragraph",
                    "location": location, "source_span": {"evidence_id": eid, "text": source,
                    "start_offset": 0, "end_offset": len(source),
                    "source_sha256": hashlib.sha256(source.encode()).hexdigest()}})
    chunk = build_llm_request([], clauses, {"evidence": evidence}, {}, "full", contract_version="3.0")
    chunk.update(case_id="generic-draft-proposal", batch={"index": 1, "count": 1},
                 runtime_context=copy.deepcopy(seed["runtime_context"]))
    chunk = attach_request_provenance(chunk, source_sha256=sha256_json(evidence),
        evidence_doc={"evidence": evidence}, clauses=clauses, run_id="fresh-offline-draft-proposal")
    raw["provenance"] = copy.deepcopy(chunk["provenance"])
    raw["clause_reviews"].append({"clause_id": label_id, "classification": "external_compliance",
        "normative_basis": "external_duty", "reason": "Primary proposes a pending duty.",
        "obligations": [{"id": "label-human-atom", "actor": "Author", "action": "Complete this field",
            "target": "Form field", "source_quote": source, "status": "unverifiable",
            "reason": "A person would supply the value.", "force": "required",
            "applicability": "applicable", "route": "human"}]})
    candidate = bridge.prepare_native_response_candidate(raw, chunk)[0]
    candidate["provenance"] = copy.deepcopy(chunk["provenance"])
    request, rejected = rejected_action(candidate, chunk, action_only=True)
    request.update(output_policy="review_draft", provider_attempt=1)
    rejected["results"][-1] = {"check_id": label_id, "verdict": "consistent",
        "rationale": "This label does not explicitly establish an action.",
        "identified_obligations": [], "evidence_quotes": [source], "machine_obligation_ids": []}
    return raw, candidate, chunk, request, rejected


@pytest.mark.parametrize("source", ["Reviewer code:", "联系电话："])
def test_complete_feedback_retains_dispute_but_grants_only_unrelated_named_fields(source):
    raw, candidate, chunk, request, rejected = packet(source)
    before = copy.deepcopy((raw, candidate, chunk, request, rejected))
    record = reassessment.source_atom_feedback(candidate, chunk, request, rejected)
    assert record is not None
    assert record["code"] == reassessment.ACTION_CODE
    assert [a["fields"] for a in record["source_atoms"]] == [["action"], ["condition"]]
    assert {a["clause_id"] for a in record["source_atoms"]} == {c["id"] for c in chunk["clauses"][:2]}
    disputes = build_inventory_existence_disputes(request["checks"], rejected["results"])
    assert record["retained_source_inventory_disputes"] == disputes
    assert record["semantic_review_required"] and not record["submission_ready"]
    assert (raw, candidate, chunk, request, rejected) == before
    record["response_sha256"] = sha256_json(raw)
    assert reassessment.condition_proposal_budget_receipt(candidate, chunk, [record], raw)["proposal_limit"] == 1
    proposal = copy.deepcopy(raw)
    proposal["clause_reviews"][0]["obligations"][0]["action"] = "A new primary source-grounded action proposal"
    assert bridge._retry_semantic_change_error(raw, proposal, [record], contract_version="3.0", chunk=chunk)[0] is None
    # The preserved disagreement is never a grant to delete/change that duty.
    proposal["clause_reviews"][-1]["obligations"] = []
    assert bridge._retry_semantic_change_error(raw, proposal, [record], contract_version="3.0", chunk=chunk)[0]
    record["retained_source_inventory_disputes"] = []
    assert reassessment.condition_proposal_budget_receipt(candidate, chunk, [record], raw) is None


@pytest.mark.parametrize("damage", ["submission", "no_policy", "other_policy", "source", "quote", "run",
    "missing_result", "duplicate", "known_fact", "actor", "force", "incomplete", "linked", "schema"])
def test_dispute_does_not_launder_invalid_or_incomplete_feedback(damage):
    raw, candidate, chunk, request, rejected = packet()
    if damage == "submission": request["output_policy"] = "submission"
    elif damage == "no_policy": request.pop("output_policy")
    elif damage == "other_policy": request["output_policy"] = "supported_subset"
    elif damage == "source": chunk["clauses"][-1]["source_span"]["source_sha256"] = "0" * 64
    elif damage == "quote": rejected["results"][-1]["evidence_quotes"] = ["forged"]
    elif damage == "run": request["run_id"] = "old"
    elif damage == "missing_result": rejected["results"].pop()
    elif damage == "duplicate": rejected["results"].append(copy.deepcopy(rejected["results"][-1]))
    elif damage == "known_fact": rejected["results"][-1]["machine_obligation_ids"] = ["invented"]
    elif damage in {"actor", "force"}: rejected["results"][0]["identified_obligations"][0][damage] = "different"
    elif damage == "incomplete": rejected["results"][-1]["verdict"] = "incomplete"
    elif damage == "linked": request["checks"][-1]["review_context"]["linked_requirements"] = [{"requirement_ref": "foreign"}]
    else: rejected["results"][-1]["unexpected"] = True
    frozen = copy.deepcopy((candidate, chunk, request, rejected))
    assert reassessment.source_atom_feedback(candidate, chunk, request, rejected) is None
    assert (candidate, chunk, request, rejected) == frozen


def test_validator_projection_is_not_silently_used_as_retry_authority():
    _, candidate, chunk, request, rejected = packet()
    original_validator = reassessment.validate_obligation_coverage_response
    def mutating_validator(response, checks, **kwargs):
        if checks[0]["check_id"] == request["checks"][-1]["check_id"]:
            response["results"][0]["rationale"] += " projected"
            return response["results"]
        return original_validator(response, checks, **kwargs)
    frozen = copy.deepcopy(rejected)
    with patch.object(reassessment, "validate_obligation_coverage_response", side_effect=mutating_validator):
        assert reassessment.source_atom_feedback(candidate, chunk, request, rejected) is None
    assert rejected == frozen


def test_other_draft_incomplete_result_does_not_gain_the_sibling_exception():
    _, candidate, chunk, request, rejected = packet()
    primary = request["checks"][-1]["review_context"]["primary_obligations"][0]
    rejected["results"][-1].update(verdict="incomplete", identified_obligations=[{
        **{k: primary[k] for k in ("actor", "action", "target", "source_quote", "force", "applicability")},
        "primary_obligation_id": primary["id"], "disposition": "unrepresented", "requirement_refs": []}])
    native.validate_obligation_coverage_response({"results": [copy.deepcopy(rejected["results"][-1])]},
        [request["checks"][-1]], allow_draft_disputes=True)
    assert reassessment.source_atom_feedback(candidate, chunk, request, rejected) is None


def test_native_retry_exhaustion_retains_sibling_and_routes_bounded_proposal(tmp_path):
    raw, candidate, chunk, request, rejected = packet()
    before = copy.deepcopy(candidate)
    wire = retry_fixtures.RetryScopeTests()
    with pytest.raises(native.TypedSourceAtomAlignmentError) as caught:
        native.validate_obligation_coverage_response(copy.deepcopy(rejected), request["checks"], allow_draft_disputes=True)
    error = caught.value
    retry = {**copy.deepcopy(request), "provider_attempt": 2, "retry_feedback": {
        "code": error.code, "clause_ids": list(error.clause_ids), "disagreements": error.disagreements,
        "checks_sha256": sha256_json(request["checks"]), "candidate_response_sha256": sha256_json(candidate),
        "run_id": request["run_id"], "provenance": copy.deepcopy(request["provenance"])}}
    raws = [transport.enveloped(wire.wire(rejected, r)) for r in (request, retry)]
    with ExitStack() as stack:
        transport.EmptyInventoryVerdictTests().mock_transport(stack, raws)
        stack.enter_context(patch.object(bridge, "INDEPENDENT_REVIEW_RETRY_BACKOFF_SECONDS", 0))
        with pytest.raises(bridge.IndependentObligationReviewError) as caught:
            bridge._run_independent_obligation_coverage_review(candidate, chunk, review_dir=tmp_path,
                run_id=request["run_id"], chunk_index=1, attempt=1, host_runtime="codex", model="gpt-6-luna",
                timeout=5, agent_id="main", runner="exec", binary="codex", config_path=None,
                controller=bridge.RunController(), output_policy="review_draft")
    assert candidate == before
    exc = caught.value
    assert exc.retryable
    assert exc.error_records[0]["code"] == reassessment.ACTION_CODE
    assert len(exc.error_records[0]["source_atoms"]) == 2
    assert len(exc.error_records[0]["retained_source_inventory_disputes"]) == 1
    second = tmp_path / "independent-review-chunk-0001-attempt-01-provider-attempt-02"
    proof = json.loads((second / "validated-retry-scope.json").read_text())
    assert proof["retained_check_ids"] == [chunk["clauses"][-1]["id"]]
    assert proof["fresh_review_check_ids"] == sorted(c["id"] for c in chunk["clauses"][:2])
    assert json.loads((second / "coverage-audit.json").read_text())["status"] == "rejected"
    assert not (second / "obligation-analysis-ledger.json").exists()
    locks, _ = prepare_retry_scope(retry, second, native.OBLIGATION_COVERAGE_SCHEMA,
                                  provider_nullable_optionals=True)
    locked_schema = constrain_retry_schema(source_reference_schema(native.OBLIGATION_COVERAGE_SCHEMA,
        build_source_reference_packet(retry), coverage=True, constrain_requirement_links=True), locks)
    wire_rejected = wire.wire(rejected, retry)
    validate_retry_scope(wire_rejected, locked_schema, locks, native=True)
    wire_rejected["results"][-1]["rationale"] += " changed"
    with pytest.raises(native.NativeSemanticReviewError):
        validate_retry_scope(wire_rejected, locked_schema, locks, native=True)
    proof_path = second / "validated-retry-scope.json"
    proof_audit = {"adapter_id": "codex", "corrective_review_scope": {
        "policy": proof["policy"], "proof_path": str(proof_path.resolve()),
        "proof_sha256": bridge.sha256_file(proof_path),
        "retained_check_ids": proof["retained_check_ids"], "fresh_review_check_ids": proof["fresh_review_check_ids"]}}
    validate_persisted_empty_inventory_scope(retry, second, raws[1], proof_audit)
    forged = copy.deepcopy(proof); forged["retained_check_ids"] = []
    bridge._write_json(proof_path, forged)
    resealed = copy.deepcopy(proof_audit)
    resealed["corrective_review_scope"].update(proof_sha256=bridge.sha256_file(proof_path), retained_check_ids=[])
    with pytest.raises(ValueError):
        validate_persisted_empty_inventory_scope(retry, second, raws[1], resealed)
    bridge._write_json(proof_path, proof)
    forged_audit = copy.deepcopy(proof_audit); forged_audit["corrective_review_scope"]["fresh_review_check_ids"] = []
    with pytest.raises(ValueError):
        validate_persisted_empty_inventory_scope(retry, second, raws[1], forged_audit)
    parent_path = tmp_path / "independent-review-chunk-0001-attempt-01/raw-response.json"
    original_parent = json.loads(parent_path.read_text())
    damaged_parent = copy.deepcopy(original_parent); damaged_parent["results"][-1]["rationale"] += " changed"
    bridge._write_json(parent_path, damaged_parent)
    with pytest.raises((ValueError, native.NativeSemanticReviewError)):
        validate_persisted_empty_inventory_scope(retry, second, raws[1], proof_audit)
    bridge._write_json(parent_path, original_parent)

    # A separate scripted primary proposal is still not an acceptance. Run the
    # real fresh-review/ledger path with mocked agreement on its named fields.
    record = exc.error_records[0]
    record["response_sha256"] = sha256_json(raw)
    assert reassessment.condition_proposal_budget_receipt(candidate, chunk, [record], raw)["proposal_limit"] == 1
    proposal = copy.deepcopy(raw)
    proposal["clause_reviews"][0]["obligations"][0]["action"] = "A newly assessed primary action"
    proposal["clause_reviews"][1]["obligations"][0]["condition"] = "A newly assessed primary condition"
    assert bridge._retry_semantic_change_error(raw, proposal, [record], contract_version="3.0", chunk=chunk)[0] is None
    new_candidate = bridge.prepare_native_response_candidate(proposal, chunk)[0]
    new_candidate["provenance"] = copy.deepcopy(chunk["provenance"])
    fresh = native.build_obligation_coverage_request(new_candidate, chunk, run_id=request["run_id"], chunk_index=1)
    fresh.update(attempt=2, provider_attempt=1, output_policy="review_draft")
    agreed = copy.deepcopy(rejected)
    for i in range(2):
        primary = fresh["checks"][i]["review_context"]["primary_obligations"][0]
        for field in ("action", "condition"):
            if field in primary:
                agreed["results"][i]["identified_obligations"][0][field] = primary[field]
            else:
                agreed["results"][i]["identified_obligations"][0].pop(field, None)
    with ExitStack() as stack:
        transport.EmptyInventoryVerdictTests().mock_transport(stack, [transport.enveloped(wire.wire(agreed, fresh))])
        pointer = bridge._run_independent_obligation_coverage_review(new_candidate, chunk, review_dir=tmp_path,
            run_id=request["run_id"], chunk_index=1, attempt=2, host_runtime="codex", model="gpt-6-luna",
            timeout=5, agent_id="main", runner="exec", binary="codex", config_path=None,
            controller=bridge.RunController(), output_policy="review_draft")
    envelope = json.loads((tmp_path / pointer["audit_path"]).read_text())
    assert envelope["status"] == "completed_with_disputes"
    assert envelope["coverage_complete"] is False and envelope["submission_ready"] is False
    assert envelope["source_inventory_disputes"][0]["independent_result"] == rejected["results"][-1]
    bridge._validate_completed_obligation_ledger_chain(tmp_path, envelope, pointer, new_candidate, chunk,
        chunk_index=1, attempt=2, output_policy="review_draft")
    with pytest.raises(ValueError):
        bridge._validate_completed_obligation_ledger_chain(tmp_path, envelope, pointer, new_candidate, chunk,
            chunk_index=1, attempt=2, output_policy="submission")


@pytest.mark.parametrize("outcome", ["accept_draft", "repeat_disagreement"])
def test_full_bridge_routes_one_proposal_and_requires_fresh_native_review(tmp_path, outcome):
    """Only CLI resolution/probe and subprocess I/O are fake, not orchestration."""
    raw, _, seed, _, _ = packet()
    directory = tmp_path / "packets"
    prepare_host_agent_review_packets(seed, seed["clauses"],
        {"evidence": list(seed["evidence_context"].values())}, seed["provenance"]["source_sha256"],
        directory, chunk_size=100)
    chunk = json.loads((directory / "llm-request-chunks.json").read_text())[0]
    calls, reviews = [], []
    wire = retry_fixtures.RetryScopeTests()

    def finish(command, response):
        text = json.dumps(response, ensure_ascii=False)
        Path(command[command.index("--output-last-message") + 1]).write_text(text, encoding="utf-8")
        events = [{"type": "thread.started", "thread_id": "offline-only-draft-scope"},
                  {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
                  {"type": "turn.completed"}]
        return subprocess.CompletedProcess(command, 0, "\n".join(json.dumps(e) for e in events), "")

    def primary(command, **kwargs):
        calls.append(command)
        body = copy.deepcopy(raw)
        body["provenance"] = copy.deepcopy(chunk["provenance"])
        if len(calls) == 2:
            body["clause_reviews"][0]["obligations"][0]["action"] = "A primary's newly assessed source action"
            body["clause_reviews"][1]["obligations"][0]["condition"] = "A primary's newly assessed condition"
        return finish(command, body)

    def independent(command, **kwargs):
        path = Path(command[command.index("--output-last-message") + 1]).parent
        request = json.loads((path / "request.json").read_text())
        reviews.append(request)
        results = []
        for i, check in enumerate(request["checks"]):
            context = check["review_context"]
            item = {"check_id": check["check_id"], "verdict": "consistent",
                "rationale": "Scripted offline review, not independent semantic truth.",
                "evidence_quotes": [check["document_text"]], "machine_obligation_ids": context["machine_obligation_ids"],
                "identified_obligations": []}
            if i < 2:
                atom = context["primary_obligations"][0]
                reviewed = {k: copy.deepcopy(atom[k]) for k in
                    ("actor", "action", "target", "source_quote", "force", "applicability", "condition") if k in atom}
                reviewed.update(primary_obligation_id=atom["id"], disposition="represented",
                    requirement_refs=[r["requirement_ref"] for r in context["linked_requirements"]])
                if len(calls) == 1 or outcome == "repeat_disagreement":
                    reviewed["action" if i == 0 else "condition"] = "A different scripted reviewer interpretation"
                item["identified_obligations"] = [reviewed]
            results.append(item)
        return finish(command, transport.enveloped(wire.wire({"results": results}, request)))

    output = tmp_path / "merged.json"
    with patch.dict(os.environ, {"THESIS_FORGE_HOST_RUNTIME": "codex"}), \
         patch.object(bridge, "_resolve_codex", return_value="/offline/mock-codex"), \
         patch.object(native.codex_adapter, "resolve_binary", return_value="/offline/mock-codex"), \
         patch.object(native.codex_adapter, "probe_capabilities", return_value={"output_schema_supported": True}), \
         patch.object(bridge, "INDEPENDENT_REVIEW_RETRY_BACKOFF_SECONDS", 0), \
         patch.object(bridge, "_run_command", side_effect=primary), \
         patch.object(native, "run_process", side_effect=independent):
        if outcome == "accept_draft":
            bridge.run_bridge(directory, response_out=output, host_runtime="codex", codex_bin="/offline/mock-codex",
                              timeout=5, max_attempts=1, output_policy="review_draft")
        else:
            with pytest.raises((bridge.IndependentObligationReviewError, ValueError)):
                bridge.run_bridge(directory, response_out=output, host_runtime="codex", codex_bin="/offline/mock-codex",
                                  timeout=5, max_attempts=1, output_policy="review_draft")
    assert len(calls) == 2  # ordinary budget + one authenticated scope proposal
    assert [r["attempt"] for r in reviews] == ([1, 1, 2] if outcome == "accept_draft" else [1, 1, 2, 2])
    assert output.exists() is (outcome == "accept_draft")
    audit = json.loads((directory / "host-agent-run.json").read_text())
    attempts = audit["chunk_lifecycle"][0]["attempts"]
    assert attempts[-1]["retry_budget"]["scope_proposals_started"] == 1
    if outcome == "accept_draft":
        pointer = audit["chunk_runs"][0]["independent_obligation_review"]
        envelope = json.loads((directory / pointer["audit_path"]).read_text())
        assert envelope["status"] == "completed_with_disputes" and not envelope["submission_ready"]
        assert len(envelope["source_inventory_disputes"]) == 1
