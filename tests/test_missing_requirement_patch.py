"""An authorized addition never accepts the model's replacement parent."""
import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_source_verification_projection_retry import fixture as source_fixture
import host_agent_bridge as bridge
import requirements_engine as engine
from semantic_contract import attach_request_provenance, sha256_bytes


def fixture():
    seed, _ = source_fixture()
    texts = ["研究目标说明", "实验方法说明"]
    clauses = copy.deepcopy(seed["clauses"])
    evidence = []
    for i, c in enumerate(clauses):
        c["text"] = texts[i]
        c["source_span"].update(text=texts[i], start_offset=0, end_offset=len(texts[i]),
                                source_sha256=sha256_bytes(texts[i].encode()))
        evidence.append({"id": c["evidence_ids"][0], "kind": "paragraph", "text": texts[i], "location": {}})
    evidence_doc = {"evidence": evidence}
    request = engine.build_llm_request([], clauses, evidence_doc, {}, "full", contract_version="3.0",
        runtime_context={"code_fingerprint_sha256": "f" * 64})
    request["case_id"] = "generic-missing-edge"
    chunk = attach_request_provenance(request, source_sha256="a" * 64,
        evidence_doc=evidence_doc, clauses=clauses, run_id="generic-edge-run")
    chunk["batch"] = {"index": 1}
    parent = {"contract_version": "3.0", "requirements": [{"role": "heading_2",
        "properties": {"text": texts[0]}, "clause_ids": [clauses[0]["id"]],
        "evidence_ids": clauses[0]["evidence_ids"], "reason": "Current source title.", "confidence": 0.8}],
        "clause_reviews": [{"clause_id": c["id"], "classification": "executable",
            "reason": "Printed section title.", "normative_basis": "template_structure",
            "obligations": [{"id": c["id"] + "-print", "status": "covered", "reason": "Print source title.",
                "source_quote": texts[i], "force": "required", "applicability": "applicable", "route": "automatic"}]}
            for i, c in enumerate(clauses)], "unsupported_items": [], "reported_conflicts": []}
    proposed = copy.deepcopy(parent)
    proposed["requirements"].append({**copy.deepcopy(parent["requirements"][0]),
        "properties": {"text": texts[1]}, "clause_ids": [clauses[1]["id"]],
        "evidence_ids": clauses[1]["evidence_ids"]})
    # A locally valid subspan is still an unauthorized semantic edit.
    proposed["clause_reviews"][0]["obligations"][0]["source_quote"] = texts[0][:2]
    records = bridge.contract_error_records(bridge.validate_host_agent_response(parent, chunk),
        response=parent, chunk=chunk)
    assert [r["code"] for r in records] == ["missing_derived_requirement"], records
    return chunk, parent, proposed, records


def test_preserves_parent_and_records_discarded_quote_and_old_payload():
    chunk, parent, proposed, records = fixture()
    proposed["requirements"][0]["properties"]["text"] = "unrequested old text"
    frozen = copy.deepcopy((parent, proposed, chunk, records))
    candidate, audit = bridge._project_validator_targeted_missing_requirements(parent, proposed, records, chunk=chunk)
    assert candidate is not None
    assert candidate["clause_reviews"] == parent["clause_reviews"]
    assert candidate["requirements"][:-1] == parent["requirements"]
    assert bridge.validate_host_agent_response(candidate, chunk) == []
    assert audit["independent_review_required"] and not audit["submission_ready"]
    assert "$.clause_reviews[0].obligations[0].source_quote" in audit["discarded_unrequested_paths"]
    assert "$.requirements[0].properties.text" in audit["discarded_unrequested_paths"]
    assert bridge._retry_semantic_change_error(parent, candidate, records, contract_version="3.0", chunk=chunk)[0] is None
    assert (parent, proposed, chunk, records) == frozen


def test_omitted_old_requirement_is_preserved_not_deleted():
    chunk, parent, proposed, records = fixture()
    proposed["requirements"].pop(0)
    candidate, audit = bridge._project_validator_targeted_missing_requirements(parent, proposed, records, chunk=chunk)
    assert candidate is not None
    assert candidate["requirements"][0] == parent["requirements"][0]
    assert candidate["clause_reviews"] == parent["clause_reviews"]


def test_multiple_missing_targets_require_complete_bundle_and_coverage():
    chunk, parent, proposed, _ = fixture()
    parent["requirements"] = []
    records = bridge.contract_error_records(bridge.validate_host_agent_response(parent, chunk), response=parent, chunk=chunk)
    assert len(records) == 2
    candidate, _ = bridge._project_validator_targeted_missing_requirements(parent, proposed, records, chunk=chunk)
    assert candidate is not None and len(candidate["requirements"]) == 2
    assert bridge._project_validator_targeted_missing_requirements(parent, proposed, records[:1], chunk=chunk)[0] is None
    proposed["requirements"].pop()
    assert bridge._project_validator_targeted_missing_requirements(parent, proposed, records, chunk=chunk)[0] is None


@pytest.mark.parametrize("damage", ["stale", "feedback", "extra_error", "source", "duplicate_clause",
    "duplicate_review", "foreign_run", "foreign_evidence", "foreign_edge", "duplicate_addition", "empty", "bad_role", "bad_payload", "no_addition"])
def test_invalid_authority_or_addition_rejects(damage):
    chunk, parent, proposed, records = fixture()
    if damage == "stale": records[0]["response_sha256"] = "0" * 64
    elif damage == "feedback": records[0]["raw_error"] += " forged"
    elif damage == "extra_error": records.append({"code": "unrelated"})
    elif damage == "source": chunk["clauses"][1]["source_span"]["source_sha256"] = "0" * 64
    elif damage == "duplicate_clause": chunk["clauses"].append(copy.deepcopy(chunk["clauses"][1]))
    elif damage == "duplicate_review": parent["clause_reviews"].append(copy.deepcopy(parent["clause_reviews"][1]))
    elif damage == "foreign_run": proposed["provenance"] = {**chunk["provenance"], "run_id": "foreign"}
    elif damage == "foreign_evidence": proposed["requirements"][-1]["evidence_ids"] = ["foreign"]
    elif damage == "foreign_edge": proposed["requirements"][-1]["clause_ids"].append(chunk["clauses"][0]["id"])
    elif damage == "duplicate_addition": proposed["requirements"].append(copy.deepcopy(proposed["requirements"][-1]))
    elif damage == "empty": proposed["requirements"][-1]["properties"] = {}
    elif damage == "bad_role": proposed["requirements"][-1]["role"] = "invented_role"
    elif damage == "bad_payload": proposed["requirements"][-1]["properties"]["unknown"] = True
    elif damage == "no_addition": proposed["requirements"].pop()
    assert bridge._project_validator_targeted_missing_requirements(parent, proposed, records, chunk=chunk)[0] is None


@pytest.mark.parametrize("reject_independent", [False, True])
def test_native_missing_edge_retry_freezes_quote_and_requires_new_review(tmp_path, reject_independent):
    request, parent, proposed, _ = fixture()
    evidence = {"evidence": list(request["evidence_context"].values())}
    directory = tmp_path / "packet"
    engine.prepare_host_agent_review_packets(request, request["clauses"], evidence,
        request["provenance"]["source_sha256"], directory, chunk_size=100)
    calls, reviewed = [], []
    def primary(command, **kwargs):
        calls.append(command)
        final = json.dumps(parent if len(calls) == 1 else proposed)
        Path(command[command.index("--output-last-message") + 1]).write_text(final)
        events = [{"type": "thread.started", "thread_id": "offline-native"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": final}}, {"type": "turn.completed"}]
        return subprocess.CompletedProcess(command, 0, "\n".join(map(json.dumps, events)), "")
    def independent(candidate, chunk, **kwargs):
        reviewed.append(copy.deepcopy(candidate))
        assert candidate["clause_reviews"] == parent["clause_reviews"]
        if reject_independent:
            error = bridge.IndependentObligationReviewError("new requirement rejected by source review")
            error.retryable = False
            raise error
        from test_host_agent_bridge import HostAgentBridgeTests
        return HostAgentBridgeTests._fake_independent_review(candidate, chunk, **kwargs)
    output = tmp_path / "merged.json"
    with patch.dict(os.environ, {"THESIS_FORGE_HOST_RUNTIME": "codex"}), \
         patch.object(bridge, "_resolve_codex", return_value="/offline/mock-codex"), \
         patch.object(bridge.codex_adapter, "probe_capabilities", return_value={
            "output_schema_supported": True, "structured_output_mode": "native_schema"}), \
         patch.object(bridge, "_run_command", side_effect=primary), \
         patch.object(bridge, "_run_independent_obligation_coverage_review", side_effect=independent):
        if reject_independent:
            with pytest.raises(bridge.IndependentObligationReviewError):
                bridge.run_bridge(directory, response_out=output, host_runtime="codex", codex_bin="/offline/mock-codex", timeout=1, max_attempts=2)
        else:
            bridge.run_bridge(directory, response_out=output, host_runtime="codex", codex_bin="/offline/mock-codex", timeout=1, max_attempts=2)
    assert len(calls) == 2 and len(reviewed) == 1
    assert output.exists() is not reject_independent
