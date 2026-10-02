"""Captured ambiguity rejection and bounded unchanged-candidate source reread."""
import copy
import json
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest
import host_agent_bridge as bridge
import native_semantic_review as native
import thesis_format_pipeline as pipeline
from semantic_contract import sha256_json, request_body_sha256
from semantic_source_references import build_source_reference_packet, compile_source_reference_response, source_reference_schema
from independent_retry_scope import prepare_retry_scope, constrain_retry_schema, validate_retry_scope, validate_persisted_empty_inventory_scope
from tests import test_independent_retry_scope as scope_helpers
from tests import test_empty_inventory_verdict as transport_helpers
from tests import test_host_agent_bridge as bridge_helpers
from tests.test_empty_inventory_verdict import enveloped

CASE = Path(__file__).parent / "fixtures/unsafe-uncertainty-verdict-incident.json"


def retry_request(request, first, candidate_sha="a" * 64):
    with pytest.raises(native.UnsafeUncertaintyVerdictError) as caught:
        native.validate_obligation_coverage_response(copy.deepcopy(first), request["checks"])
    exc = caught.value
    return {**copy.deepcopy(request), "provider_attempt": 2, "retry_feedback": {
        "code": exc.code, "clause_ids": list(exc.clause_ids), "rejected_results": exc.rejected_results,
        "rejected_results_sha256": sha256_json(exc.rejected_results),
        "rejected_request_sha256": sha256_json(request), "checks_sha256": sha256_json(request["checks"]),
        "candidate_response_sha256": candidate_sha, "run_id": request["run_id"],
        "provenance": copy.deepcopy(request["provenance"])}}


def faithful_result(check):
    # Synthetic independent agreement, NOT an actual model pass for this incident.
    primary = check["review_context"]["primary_obligations"][0]
    atom = {k: v for k, v in primary.items() if k in {
        "actor", "action", "target", "condition", "source_quote", "force", "applicability"}}
    atom.update(primary_obligation_id=primary["id"], disposition="represented",
        requirement_refs=[check["review_context"]["linked_requirements"][0]["requirement_ref"]])
    return {"check_id": check["check_id"], "verdict": "consistent", "rationale": "Synthetic source agreement.",
        "evidence_quotes": [check["document_text"]], "machine_obligation_ids": [], "identified_obligations": [atom]}


def captured():
    case = json.loads(CASE.read_text())
    # Change identities to prove the production selector is not tied to BSU/C00009.
    case["request"]["run_id"] = case["request"]["provenance"]["run_id"] = "other-current-run"
    case["request"]["checks"][0]["check_id"] = "other-template-label"
    case["first_compiled"]["results"][0]["check_id"] = "other-template-label"
    return case


def parent_helper(case, base):
    helper = scope_helpers.RetryScopeTests(); helper.case = case; helper.request = case["request"]; helper.base = base
    helper.write_parent()
    return helper


def test_captured_compiled_rejection_and_safe_primary_uncertainty_still_valid():
    case = json.loads(CASE.read_text())
    # This is a selected-check fixture; old selectors bind the ORIGINAL full
    # request. Generate fresh selectors, never pretend they fit this excerpt.
    wire = scope_helpers.RetryScopeTests().wire(case["first_compiled"], case["request"])
    replay, _ = compile_source_reference_response(wire, case["request"],
        native.OBLIGATION_COVERAGE_SCHEMA, coverage=True, provider_nullable_optionals=True)
    assert replay == case["first_compiled"]
    original = copy.deepcopy(replay)
    with pytest.raises(native.UnsafeUncertaintyVerdictError) as caught:
        native.validate_obligation_coverage_response(replay, case["request"]["checks"])
    assert replay == original and caught.value.rejected_results == original["results"]
    safe = copy.deepcopy(case["request"]["checks"][0])
    safe["review_context"].update(classification="unresolved", requires_requirement=False, linked_requirements=[])
    safe["review_context"]["primary_obligations"][0].update(
        status="unresolved", force="unknown", applicability="unknown", route="unknown",
        actor=None, action=None, target=None)
    native.validate_obligation_coverage_response(copy.deepcopy(original), [safe])


def test_collects_multiple_dynamic_targets_but_empty_or_unrepresented_inventory_is_not_authorized():
    case = captured()
    second = copy.deepcopy(case["request"]["checks"][0]); second["check_id"] = "different-second-target"
    case["request"]["checks"].append(second)
    result = copy.deepcopy(case["first_compiled"]["results"][0]); result["check_id"] = second["check_id"]
    case["first_compiled"]["results"].append(result)
    retry = retry_request(case["request"], case["first_compiled"])
    assert retry["retry_feedback"]["clause_ids"] == ["different-second-target", "other-template-label"]
    assert native.unsafe_uncertainty_retry_feedback_is_bound(retry)
    for disposition in ("unrepresented", "represented", "external_action_pending"):
        invalid = copy.deepcopy(case["first_compiled"])
        invalid["results"][0]["identified_obligations"][0]["disposition"] = disposition
        with pytest.raises(native.NativeSemanticReviewError) as caught:
            native.validate_obligation_coverage_response(invalid, case["request"]["checks"])
        assert not isinstance(caught.value, native.UnsafeUncertaintyVerdictError)


@pytest.mark.parametrize("change", ["old_run", "old_hash", "float_attempt", "attempt3", "unknown_id", "duplicate_id",
    "foreign_quote", "extra_authority", "resealed_consistent", "resealed_missing", "unrepresented", "known_fact"])
def test_no_stale_or_invalid_feedback_can_authorize_reread(change):
    case = captured(); retry = retry_request(case["request"], case["first_compiled"])
    assert native.unsafe_uncertainty_retry_feedback_is_bound(retry)
    f = retry["retry_feedback"]
    if change == "old_run": retry["run_id"] = "old"
    elif change == "old_hash": f["checks_sha256"] = "0" * 64
    elif change == "float_attempt": retry["provider_attempt"] = 2.0
    elif change == "attempt3": retry["provider_attempt"] = 3
    elif change == "unknown_id": f["clause_ids"] = ["foreign"]
    elif change == "duplicate_id": f["clause_ids"] *= 2
    elif change == "extra_authority": f["allow_primary_edit"] = True
    elif change == "known_fact": retry["checks"][0]["review_context"]["machine_obligation_ids"] = ["invented"]
    else:
        result = f["rejected_results"][0]
        if change == "foreign_quote": result["identified_obligations"][0]["source_quote"] = "foreign"
        elif change == "resealed_consistent": result["verdict"] = "consistent"
        elif change == "resealed_missing": result["identified_obligations"] = []
        elif change == "unrepresented": result["identified_obligations"][0]["disposition"] = "unrepresented"
        f["rejected_results_sha256"] = sha256_json(f["rejected_results"])
    assert not native.unsafe_uncertainty_retry_feedback_is_bound(retry)


def test_replayed_scope_locks_valid_siblings_but_not_hidden_errors(tmp_path):
    case = captured()
    sibling = copy.deepcopy(case["request"]["checks"][0]); sibling["check_id"] = "valid-sibling"
    case["request"]["checks"].append(sibling)
    case["first_compiled"]["results"].append(faithful_result(sibling))
    base = tmp_path / "review"; helper = parent_helper(case, base)
    retry = retry_request(case["request"], case["first_compiled"])
    output = tmp_path / "review-provider-attempt-02"
    locks, proof = prepare_retry_scope(retry, output, native.OBLIGATION_COVERAGE_SCHEMA, provider_nullable_optionals=True)
    assert list(locks) == ["valid-sibling"] and proof["fresh_review_check_ids"] == ["other-template-label"]
    corrected = copy.deepcopy(case["first_compiled"])
    corrected["results"][0] = faithful_result(case["request"]["checks"][0])
    wire = helper.wire(corrected, retry)
    schema = constrain_retry_schema(source_reference_schema(native.OBLIGATION_COVERAGE_SCHEMA,
        build_source_reference_packet(retry), coverage=True, constrain_requirement_links=True), locks)
    validate_retry_scope(wire, schema, locks, native=True)
    changed = copy.deepcopy(wire); changed["results"][1]["rationale"] += " changed"
    with pytest.raises(native.NativeSemanticReviewError): validate_retry_scope(changed, schema, locks, native=True)
    case["first_compiled"]["results"][1]["identified_obligations"][0]["target"] = "wrong target"
    helper.write_parent()
    locks, proof = prepare_retry_scope(retry, output, native.OBLIGATION_COVERAGE_SCHEMA, provider_nullable_optionals=True)
    assert locks == {}
    assert proof["fresh_review_check_ids"] == ["other-template-label", "valid-sibling"]
    assert proof["additional_reproduced_rejections"][0]["check_id"] == "valid-sibling"


def test_missing_parent_and_persistent_ambiguity_fail_closed(tmp_path):
    case = captured(); retry = retry_request(case["request"], case["first_compiled"])
    with pytest.raises(native.NativeSemanticReviewError):
        prepare_retry_scope(retry, tmp_path / "missing-provider-attempt-02", native.OBLIGATION_COVERAGE_SCHEMA)
    with pytest.raises(native.UnsafeUncertaintyVerdictError):
        native.validate_obligation_coverage_response(copy.deepcopy(case["first_compiled"]), retry["checks"])
    prompt = native._prompt(retry)
    assert "not authority to change the candidate" in prompt
    assert "Preserve any real ambiguity" in prompt
    assert "NOT proof of a mandate" in prompt


@pytest.mark.parametrize("success", [True, False])
def test_real_bridge_native_artifacts_two_call_limit_and_both_consumer_checks(tmp_path, success):
    directory = tmp_path / "packet"
    _, chunk = bridge_helpers.HostAgentBridgeTests()._packet(directory, source="培养单位", contract_version="3.0")
    check = captured()["request"]["checks"][0]
    primary = copy.deepcopy(check["review_context"]["primary_obligations"])
    candidate = {"contract_version": "3.0", "provenance": chunk["provenance"],
        "clause_reviews": [{"clause_id": "C1", "classification": "executable", "reason": "Source label.",
            "normative_basis": "template_structure", "obligations": primary}],
        "requirements": [{"role": "cover", "properties": check["review_context"]["linked_requirements"][0]["properties"],
            "clause_ids": ["C1"], "evidence_ids": ["E1"]}], "unsupported_items": [], "reported_conflicts": []}
    before = copy.deepcopy(candidate)
    request = native.build_obligation_coverage_request(candidate, chunk, run_id=chunk["provenance"]["run_id"], chunk_index=1)
    request.update(attempt=1, provider_attempt=1)
    assert request["checks"][0]["review_context"]["primary_normative_basis"] == "template_structure"
    first = copy.deepcopy(captured()["first_compiled"]); first["results"][0]["check_id"] = "C1"
    retry = retry_request(request, first, sha256_json(candidate))
    second = {"results": [faithful_result(request["checks"][0])]} if success else first
    helper = scope_helpers.RetryScopeTests()
    raws = [enveloped(helper.wire(first, request)), enveloped(helper.wire(second, retry))]
    with ExitStack() as stack:
        transport_helpers.EmptyInventoryVerdictTests().mock_transport(stack, raws)
        stack.enter_context(patch.object(bridge.time, "sleep"))
        args = dict(review_dir=directory, run_id=request["run_id"], chunk_index=1, attempt=1, host_runtime="codex",
            model="gpt-6-luna", timeout=5, agent_id="main", runner="exec", binary="codex", config_path=None,
            controller=bridge.RunController())
        if not success:
            with pytest.raises(bridge.IndependentObligationReviewError) as caught:
                bridge._run_independent_obligation_coverage_review(candidate, chunk, **args)
            assert not caught.value.retryable and caught.value.error_records[0]["provider_attempts"] == 2
        else:
            pointer = bridge._run_independent_obligation_coverage_review(candidate, chunk, **args)
            assert pointer["provider_attempt"] == 2
            envelope = json.loads((directory / pointer["audit_path"]).read_text())
            bridge._validate_completed_obligation_ledger_chain(directory, envelope, pointer, candidate, chunk, chunk_index=1, attempt=1)
            full = json.loads((directory / "llm-request.json").read_text())
            candidate_path = directory / "accepted-candidate.json"; bridge._write_json(candidate_path, candidate)
            audit = {"chunk_count": 1, "adapter_id": "codex", "host_runtime": "codex",
                "chunk_lifecycle": [{"chunk_index": 1, "status": "completed", "remote_operation_state": "completed"}],
                "chunk_runs": [{"chunk_index": 1, "response_path": str(candidate_path),
                    "accepted_response_sha256": sha256_json(candidate), "independent_obligation_review": pointer}]}
            def consume():
                return pipeline._validate_independent_obligation_receipts(audit=audit, review_root=directory,
                    expected_run_id=request["run_id"], expected_request_body_sha=request_body_sha256(full),
                    expected_request_envelope_sha=None, expected_request_file_sha=None)
            assert len(consume()) == 1
            scope_path = Path(envelope["review_audit"]["corrective_review_scope"]["proof_path"])
            scope = json.loads(scope_path.read_text()); scope["fresh_review_check_ids"] = []
            bridge._write_json(scope_path, scope)
            envelope["review_audit"]["corrective_review_scope"]["proof_sha256"] = bridge.sha256_file(scope_path)
            bridge._write_json(directory / pointer["audit_path"], envelope)
            pointer["audit_sha256"] = bridge.sha256_file(directory / pointer["audit_path"])
            with pytest.raises(ValueError): consume()
            with pytest.raises(ValueError):
                bridge._validate_completed_obligation_ledger_chain(directory, envelope, pointer, candidate, chunk, chunk_index=1, attempt=1)
    assert candidate == before
