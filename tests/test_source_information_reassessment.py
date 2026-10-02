"""Contradiction proposals preserve payloads and never constitute acceptance."""
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
from semantic_contract import attach_request_provenance
from source_information_reassessment import CODE, RULE_ID, feedback, reassessment


def atom(cid, source, target):
    return {"id": cid + "-printed-form", "status": "covered", "reason": "Printed source form.",
            "actor": None, "action": "display field", "target": target, "condition": None,
            "source_quote": source, "force": "required", "applicability": "applicable", "route": "automatic"}


def fixture():
    old, response = packet("", "年 月", security=True)
    evidence = {"evidence": list(old["evidence_context"].values())}
    request = engine.build_llm_request([], old["clauses"], evidence, {}, "full", contract_version="3.0",
                                       runtime_context={"code_fingerprint_sha256": "f" * 64})
    request["case_id"] = "another-case"
    chunk = attach_request_provenance(request, source_sha256="a" * 64, evidence_doc=evidence,
                                      clauses=old["clauses"], run_id="another-run")
    chunk["batch"] = {"index": 2}
    response["contract_version"] = "3.0"
    response["provenance"] = copy.deepcopy(chunk["provenance"])
    response["requirements"][0]["properties"]["fields"][0].update(
        id="completion_date", value_from="thesis_profile.cover_metadata.completion_date")
    for review in response["clause_reviews"]:
        review.pop("requirement_indexes")
    response["clause_reviews"][0].update(classification="informational", obligations=[], normative_basis="sample_content")
    response["clause_reviews"][1].update(normative_basis="template_structure",
        obligations=[atom("ADMIN-occurrence", "密级", "security marking field")])
    return chunk, response


def rejected(chunk, response):
    with pytest.raises(ValueError) as error:
        bridge.prepare_native_response_candidate(response, chunk,
            source_projection_validation_sha256=bridge._response_sha256(chunk))
    return error.value.repair_base_candidate, error.value.retry_authorizing_error_records


def proposal(parent):
    result = copy.deepcopy(parent)
    result["clause_reviews"][0].update(classification="executable", normative_basis="template_structure",
        reason="A blank printed date field, not a filled sample date.",
        obligations=[atom("TITLE-occurrence", "年 月", "completion_date field")])
    return result


def test_producer_and_authorization_preserve_graph_and_require_fresh_review():
    chunk, raw = fixture()
    before = copy.deepcopy(raw)
    parent, records = rejected(chunk, raw)
    assert raw == before
    envelope = [r for r in records if r["code"] == CODE][0]
    assert envelope["targets"][0]["clause_id"] == "TITLE-occurrence"
    assert not envelope["submission_ready"] and envelope["independent_review_required"]
    assert bridge._fresh_semantic_split_reason(records) is None
    candidate, audit = bridge.prepare_native_response_candidate(proposal(parent), chunk,
        source_projection_validation_sha256=bridge._response_sha256(chunk))
    paths = bridge._retry_change_paths(parent, candidate)
    assert bridge._retry_changes_allowed(records, paths, contract_version="3.0",
                                         previous_response=parent, current_response=candidate, chunk=chunk)
    ledger = bridge._retry_authorization_ledger(parent, candidate, records, paths, contract_version="3.0", chunk=chunk)
    assert ledger and all(item["rule_id"] == RULE_ID for item in ledger)
    assert all(item["independent_review_required"] and not item["mechanical_equivalence_claimed"] for item in ledger)
    assert candidate["clause_reviews"][1:] == parent["clause_reviews"][1:]
    assert len(candidate["requirements"]) == len(parent["requirements"])
    assert candidate["requirements"][0]["clause_ids"] == parent["requirements"][0]["clause_ids"]
    assert any(r["rule_id"] == "source_bound_cover_security_marking_migration_v1" for r in audit["mechanical_repairs"])
    for current in (proposal(parent), candidate):
        authorizations = []
        error, changes = bridge._retry_semantic_change_error(parent, current, records,
            contract_version="3.0", chunk=chunk, authorization_out=authorizations)
        assert error is None and changes
        assert len(authorizations) == len(changes)
        assert all(item["source_binding_complete"] for item in authorizations)


@pytest.mark.parametrize("change", ["external", "unresolved", "old_duty", "unrelated", "stale", "foreign_source", "ambiguous"])
def test_feedback_not_generic_semantic_permission(change):
    chunk, response = fixture()
    if change in {"external", "unresolved"}:
        response["clause_reviews"][0]["classification"] = "external_compliance" if change == "external" else "unresolved"
    elif change == "old_duty":
        response["clause_reviews"][0]["obligations"] = [atom("pending", "年 月", "existing duty")]
    elif change == "unrelated":
        response["requirements"][0]["properties"]["invented"] = True
    elif change == "foreign_source":
        chunk["clauses"][0]["source_span"]["source_sha256"] = "b" * 64
    elif change == "ambiguous":
        fields = response["requirements"][0]["properties"]["fields"]
        duplicate = copy.deepcopy(fields[0]); duplicate["order"] = 3
        fields.append(duplicate)
    ordinary = bridge.contract_error_records(bridge.validate_host_agent_response(response, chunk), response=response, chunk=chunk)
    if change == "stale":
        response["requirements"][0]["reason"] += " changed"
    assert feedback(response, chunk, ordinary, validate=bridge.validate_host_agent_response,
                    source_projection_validation_sha256=bridge._response_sha256(chunk)) is None


@pytest.mark.parametrize("change", ["delete_field", "value_binding", "other_review", "source_edge", "external_atom", "quote", "old_run", "foreign_provenance", "forged_envelope"])
def test_reassessment_rejects_unrequested_changes(change):
    chunk, raw = fixture(); parent, records = rejected(chunk, raw); current = proposal(parent)
    if change == "delete_field":
        current["requirements"][0]["properties"]["fields"].pop(0)
    elif change == "value_binding":
        current["requirements"][0]["properties"]["fields"][0]["value_from"] = "thesis_profile.cover_metadata.approval_date"
    elif change == "other_review":
        current["clause_reviews"][1]["reason"] = "unrequested change"
    elif change == "source_edge":
        current["requirements"][0]["clause_ids"].pop(0)
    elif change == "external_atom":
        current["clause_reviews"][0]["obligations"][0].update(status="unverifiable", route="human_verification")
    elif change == "quote":
        current["clause_reviews"][0]["obligations"][0]["source_quote"] = "审批日期"
    elif change == "old_run":
        chunk["provenance"]["run_id"] = "old-run"
    elif change == "foreign_provenance":
        current["provenance"]["run_id"] = "old-run"
    elif change == "forged_envelope":
        records[-1]["targets"][0]["printed_field"]["id"] = "approval_date"
    assert reassessment(parent, current, records, bridge._retry_change_paths(parent, current), chunk,
                        prepare=bridge.prepare_native_response_candidate, validate=bridge.validate_host_agent_response) is None


def test_without_current_source_projection_no_proposal_is_authorized():
    chunk, response = fixture()
    with pytest.raises(ValueError) as error:
        bridge.prepare_native_response_candidate(response, chunk, source_projection_validation_sha256="0" * 64)
    assert all(r["code"] != CODE for r in error.value.retry_authorizing_error_records)
    assert bridge._fresh_semantic_split_reason(error.value.retry_authorizing_error_records) == "mixed_executable_external_relation"


def test_prompt_freezes_payload_and_names_only_current_review(tmp_path):
    chunk, raw = fixture(); parent, records = rejected(chunk, raw)
    chunk_file = tmp_path / "chunk.json"; parent_file = tmp_path / "parent.json"
    bridge._write_json(chunk_file, chunk); bridge._write_json(parent_file, parent)
    prompt = bridge._host_prompt(request_path=chunk_file, chunk_path=chunk_file,
        response_path=tmp_path / "out.json", run_id=chunk["provenance"]["run_id"], chunk_index=2, chunk_count=2,
        attempt=2, retry_hint="source-bound contradiction", retry_parent_response_path=parent_file,
        retry_error_records=records)
    assert "PRIMARY PRINTED-FIELD INFORMATION REASSESSMENT" in prompt
    assert "Freeze every requirement/property/value/source edge" in prompt
    assert "Independent source-first review must approve" in prompt
    guidance = bridge._structured_contract_repair_guidance(records, contract_version="3.0")
    assert "Preserve every requirement" in guidance
    assert "Stop for a fresh source-bound semantic review of the requirement edges" not in guidance
    assert "Correct the source-bound semantic split in a fresh review" not in guidance
    assert '"clause_id": "TITLE-occurrence"' in prompt
    assert '"source_text": "年 月"' in prompt
    assert "the retry must add one evidence-backed requirement for that clause" not in prompt
    assert "bounded primary semantic proposal" in prompt
    # Companion hard errors remain in the immutable authorization bundle.
    assert any(r["code"] == "mixed_execution_classification_relation" for r in records)


@pytest.mark.parametrize("fresh_review_rejects", [False, True])
def test_native_bridge_replays_parent_and_cannot_bypass_independent_review(tmp_path, fresh_review_rejects):
    """Native-adapter orchestration with mocked provider, not a live BSU run."""
    request, raw = fixture()
    evidence = {"evidence": list(request["evidence_context"].values())}
    directory = tmp_path / "packet"
    engine.prepare_host_agent_review_packets(request, request["clauses"], evidence,
        request["provenance"]["source_sha256"], directory, chunk_size=100)
    chunk = json.loads((directory / "llm-request-chunks.json").read_text())[0]
    raw["provenance"] = copy.deepcopy(chunk["provenance"])
    parent, records = rejected(chunk, raw)
    proposed = proposal(parent)
    calls, reviews = [], []

    def primary(command, **kwargs):
        calls.append(command)
        body = raw if len(calls) == 1 else proposed
        final = json.dumps(body, ensure_ascii=False)
        Path(command[command.index("--output-last-message") + 1]).write_text(final)
        events = [{"type": "thread.started", "thread_id": "offline-native"},
                  {"type": "item.completed", "item": {"type": "agent_message", "text": final}},
                  {"type": "turn.completed", "usage": {}}]
        return subprocess.CompletedProcess(command, 0, "\n".join(map(json.dumps, events)), "")

    def independent(candidate, current_chunk, **kwargs):
        reviews.append(kwargs["attempt"])
        assert not bridge.validate_host_agent_response(candidate, current_chunk)
        if fresh_review_rejects:
            error = bridge.IndependentObligationReviewError("independent source interpretation rejects proposal")
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
    if fresh_review_rejects:
        assert audit["merged_response_written"] is False
    assert audit["chunk_lifecycle"][0]["retry_parent_stage"] == "unaccepted_repair_base"
