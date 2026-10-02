from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import native_semantic_review as native
from independent_review_partition import (
    POLICY, partition_packets, partition_schema, join_partition_responses, validate_partition_receipt,
)
from semantic_contract import sha256_file, sha256_json
from semantic_source_references import build_source_reference_packet, source_reference_schema, source_inventory_generation_schema
from host_review_schema import native_schema_support_errors


def request(count=15):
    return {
        "protocol": native.OBLIGATION_COVERAGE_PROTOCOL, "run_id": "current-run",
        "chunk_index": 7, "case_id": "non-bsu-case", "attempt": 1, "provider_attempt": 1,
        "provenance": {"run_id": "current-run", "source_sha256": "a" * 64},
        **({"native_review_partition_policy": POLICY} if count > 8 else {}),
        "checks": [{"check_id": f"section-{index:02d}", "document_text": "本节为说明性标题。",
                    "review_context": {"classification": "informational", "requires_requirement": False,
                                       "primary_obligations": [], "linked_requirements": [],
                                       "machine_obligation_ids": [], "source_clause_support": []}}
                   for index in range(count)],
    }


def raw(packet):
    return {"results": [{"check_id": check["check_id"], "verdict": "consistent",
                         "rationale": "Exact source is an informational heading, without a duty.",
                         "evidence_refs": [check["source_spans"][0]["ref_id"]],
                         "identified_obligations": {"first": None, "remaining": []}}
                        for check in packet["checks"]]}


def fake_process(command, **kwargs):
    path = Path(command[command.index("--output-last-message") + 1])
    packet = json.loads((path.parent / "source-reference-packet.json").read_text())
    response = raw(packet)
    text = json.dumps(response, ensure_ascii=False)
    path.write_text(text)
    events = [{"type": "thread.started", "thread_id": path.parent.name},
              {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
              {"type": "turn.completed"}]
    return CompletedProcess(command, 0, "\n".join(json.dumps(event) for event in events), "")


def run(req, directory, process=fake_process):
    with patch.dict(os.environ, {"THESIS_FORGE_HOST_RUNTIME": "codex"}), \
            patch.object(native.codex_adapter, "resolve_binary", return_value="/mock/codex"), \
            patch.object(native.codex_adapter, "probe_capabilities", return_value={"output_schema_supported": True}), \
            patch.object(native, "run_process", side_effect=process):
        return native.run_native_semantic_review(req, output_dir=directory, host_runtime="codex", model="gpt-6-luna")


def reseal(root, audit, index, name):
    proof_path = root / "native-partition-projection.json"
    proof = json.loads(proof_path.read_text())
    proof["children"][index-1]["artifacts"][name] = sha256_file(root / f"native-batch-{index:04d}" / name)
    proof_path.write_text(json.dumps(proof))
    audit["native_partition_projection"]["sha256"] = sha256_file(proof_path)


def test_partition_is_lossless_and_preserves_whole_graph_selectors():
    req = request()
    req["checks"][0]["review_context"]["source_clause_support"] = [{"clause_id": "section-14", "document_text": "跨批支持"}]
    before = copy.deepcopy(req)
    packet = build_source_reference_packet(req)
    batches = partition_packets(req, packet)
    assert [len(item["checks"]) for item in batches] == [4, 4, 4, 3]
    assert [check for batch in batches for check in batch["checks"]] == packet["checks"]
    for batch in batches:
        assert batch["orientation_only_checks"] == packet["checks"]
        assert batch["source_reference_request_sha256"] == sha256_json(req)
    assert req == before


def test_schema_keeps_selected_constraints_and_prunes_only_unreachable_defs():
    req = request()
    packet = build_source_reference_packet(req)
    schema = source_inventory_generation_schema(source_reference_schema(native.OBLIGATION_COVERAGE_SCHEMA, packet, coverage=True, constrain_requirement_links=True))
    from host_review_schema import native_output_schema
    for batch in partition_packets(req, packet):
        ids = batch["native_review_partition"]["check_ids"]
        part = partition_schema(schema, ids)
        assert part["properties"]["results"]["items"]["anyOf"] == schema["properties"]["results"]["items"]["anyOf"][int(ids[0][-2:]):int(ids[-1][-2:])+1]
        assert not native_schema_support_errors(native_output_schema(part))


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "extra", "wrong-batch", "extra-key", "missing-batch"])
def test_exact_union_rejects_invalid_child_coverage(mutation):
    req = request()
    batches = partition_packets(req, build_source_reference_packet(req))
    responses = [raw(batch) for batch in batches]
    if mutation == "missing":
        responses[0]["results"].pop()
    elif mutation == "duplicate":
        responses[0]["results"][1] = responses[0]["results"][0]
    elif mutation == "extra":
        responses[0]["results"].append(responses[1]["results"][0])
    elif mutation == "wrong-batch":
        responses[0], responses[1] = responses[1], responses[0]
    elif mutation == "extra-key":
        responses[0]["trust"] = "completed"
    else:
        responses.pop()
    with pytest.raises(ValueError):
        join_partition_responses(batches, responses)


def test_real_native_entrypoint_aggregate_and_replay(tmp_path):
    req = request()
    audit = run(req, tmp_path)
    assert len(audit["results"]) == 15
    assert audit["result_event_types"] is None  # Deliberately not a fabricated native turn.
    aggregate = json.loads((tmp_path / "raw-response.json").read_text())
    validate_partition_receipt(req, tmp_path, aggregate, audit)
    assert len(list(tmp_path.glob("native-batch-*/stdout.jsonl"))) == 4


@pytest.mark.parametrize("name", ["source-reference-packet.json", "prompt.txt", "provider-response-schema.json", "stdout.jsonl", "last-message.txt", "raw-response.json"])
def test_resealed_child_tampering_is_rejected(tmp_path, name):
    req = request()
    audit = run(req, tmp_path)
    aggregate = json.loads((tmp_path / "raw-response.json").read_text())
    path = tmp_path / "native-batch-0001" / name
    if name == "prompt.txt":
        path.write_text(path.read_text() + " forged")
    elif name == "stdout.jsonl":
        path.write_text('{"type":"turn.failed"}\n')
    else:
        value = json.loads(path.read_text())
        value["forged"] = True
        path.write_text(json.dumps(value))
    reseal(tmp_path, audit, 1, name)
    with pytest.raises(ValueError):
        validate_partition_receipt(req, tmp_path, aggregate, audit)


@pytest.mark.parametrize("field", ["run_id", "source_sha256", "checks", "native_review_partition_policy"])
def test_old_or_changed_whole_request_is_rejected(tmp_path, field):
    req = request()
    audit = run(req, tmp_path)
    aggregate = json.loads((tmp_path / "raw-response.json").read_text())
    req[field] = "changed" if field != "checks" else req["checks"][:-1]
    with pytest.raises(ValueError):
        validate_partition_receipt(req, tmp_path, aggregate, audit)


def test_missing_projection_pointer_cannot_be_hidden(tmp_path):
    req = request()
    audit = run(req, tmp_path)
    aggregate = json.loads((tmp_path / "raw-response.json").read_text())
    del audit["native_partition_projection"]
    with pytest.raises(ValueError, match="missing"):
        validate_partition_receipt(req, tmp_path, aggregate, audit)


def test_failed_child_preserves_failure_and_never_writes_accepted_aggregate(tmp_path):
    calls = 0
    def fail_second(command, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            return CompletedProcess(command, 1, '{"type":"turn.failed","error":{"message":"Incomplete response returned, reason: max_output_tokens"}}', "limit")
        return fake_process(command, **kwargs)
    with pytest.raises(native.NativeOutputLimitError):
        run(request(), tmp_path, fail_second)
    assert calls == 2
    assert (tmp_path / "native-batch-0002/stdout.jsonl").is_file()
    assert not (tmp_path / "response.json").exists()
    assert not (tmp_path / "raw-response.json").exists()
    assert not (tmp_path / "native-partition-projection.json").exists()


def test_legacy_nonpartition_receipts_are_unchanged(tmp_path):
    validate_partition_receipt(request(4), tmp_path, {}, {"adapter_id": "codex"})
    with pytest.raises(ValueError):
        partition_packets(request(4), build_source_reference_packet(request(4)))


def test_preexisting_child_is_rejected_before_top_level_writes(tmp_path):
    (tmp_path / "native-batch-0002").mkdir()
    with pytest.raises(native.NativeSemanticReviewError, match="reuse"):
        run(request(), tmp_path)
    assert not (tmp_path / "request.json").exists()


def test_cancellation_has_unknown_not_fake_success_and_no_partial_aggregate(tmp_path):
    def cancel(command, **kwargs):
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        run(request(), tmp_path, cancel)
    assert "unknown" in (tmp_path / "native-batch-0001/stderr.txt").read_text()
    assert not (tmp_path / "native-partition-projection.json").exists()
    assert not (tmp_path / "response.json").exists()


def test_deadline_is_shared_not_restarted_per_child(tmp_path):
    budgets = []
    def observe(command, **kwargs):
        budgets.append(kwargs["timeout"])
        return fake_process(command, **kwargs)
    with patch.object(native.time, "monotonic", side_effect=[0, 10, 20, 30, 40]):
        run(request(), tmp_path, observe)
    assert budgets == [890, 880, 870, 860]


def test_partitioned_corrective_retry_replays_parent_and_locks_all_siblings(tmp_path):
    from tests import test_unsafe_uncertainty_retry as uncertainty
    from tests.test_independent_retry_scope import RetryScopeTests
    from tests.test_empty_inventory_verdict import enveloped
    from independent_retry_scope import prepare_retry_scope
    from semantic_source_references import compile_source_reference_response
    req = request()
    case = uncertainty.captured()
    req["checks"][0] = case["request"]["checks"][0]
    compiled, _ = compile_source_reference_response(raw(build_source_reference_packet(req)), req,
        native.OBLIGATION_COVERAGE_SCHEMA, coverage=True, provider_nullable_optionals=True)
    compiled["results"][0] = case["first_compiled"]["results"][0]
    helper = RetryScopeTests()
    def transport_for(current, values):
        wire = enveloped(helper.wire(values, current))
        def process(command, **kwargs):
            path = Path(command[command.index("--output-last-message") + 1])
            packet = json.loads((path.parent / "source-reference-packet.json").read_text())
            ids = packet["native_review_partition"]["check_ids"]
            response = {"results": [item for item in wire["results"] if item["check_id"] in ids]}
            text = json.dumps(response, ensure_ascii=False)
            path.write_text(text)
            events = [{"type": "item.completed", "item": {"type": "agent_message", "text": text}}, {"type": "turn.completed"}]
            return CompletedProcess(command, 0, "\n".join(json.dumps(event) for event in events), "")
        return process
    parent = tmp_path / "first"
    with pytest.raises(native.UnsafeUncertaintyVerdictError):
        run(req, parent, transport_for(req, compiled))
    retry = uncertainty.retry_request(req, compiled)
    corrected = copy.deepcopy(compiled)
    corrected["results"][0] = uncertainty.faithful_result(req["checks"][0])
    output = tmp_path / "first-provider-attempt-02"
    audit = run(retry, output, transport_for(retry, corrected))
    assert len(audit["corrective_review_scope"]["retained_check_ids"]) == 14
    validate_partition_receipt(retry, output, json.loads((output / "raw-response.json").read_text()), audit)
    parent_proof = parent / "native-partition-projection.json"
    parent_proof.unlink()
    with pytest.raises(native.NativeSemanticReviewError, match="parent lacks"):
        prepare_retry_scope(retry, output, native.OBLIGATION_COVERAGE_SCHEMA, provider_nullable_optionals=True)


def committed_partition(tmp_path):
    """Drive actual producer plus both receipt consumers, not extracted funcs."""
    import hashlib
    import requirements_engine as engine
    import host_agent_bridge as bridge
    from semantic_contract import attach_request_provenance
    clauses, evidence = [], {"evidence": []}
    for index in range(15):
        text = f"本节为说明性标题{index}。"
        eid, cid = f"evidence-{index}", f"clause-{index:02d}"
        clauses.append({"id": cid, "text": text, "evidence_ids": [eid],
                        "source_kind": "paragraph", "location": {"part": "document", "order": index},
                        "source_span": {"evidence_id": eid, "start_offset": 0, "end_offset": len(text),
                                        "text": text, "source_sha256": hashlib.sha256(text.encode()).hexdigest()}})
        evidence["evidence"].append({"id": eid, "text": text, "kind": "paragraph"})
    req = engine.build_llm_request([], clauses, evidence, {}, "full")
    req["case_id"] = "different-case"
    req = attach_request_provenance(req, source_sha256="a" * 64, evidence_doc=evidence, clauses=clauses, run_id="current-run")
    review_dir = tmp_path / "requirements"
    engine.prepare_host_agent_review_packets(req, clauses, evidence, "a" * 64, review_dir, chunk_size=64)
    chunks = json.loads((review_dir / "llm-request-chunks.json").read_text())
    assert len(chunks) == 1
    chunk = chunks[0]
    candidate = {"contract_version": "2.1", "provenance": chunk["provenance"], "requirements": [],
                 "clause_reviews": [{"clause_id": clause["id"], "classification": "informational", "requirement_indexes": [], "reason": "说明性标题。"} for clause in clauses],
                 "unsupported_items": [], "reported_conflicts": []}
    candidate_path = review_dir / chunk["batch"]["response_filename"]
    candidate_path.write_text(json.dumps(candidate, ensure_ascii=False))
    with patch.dict(os.environ, {"THESIS_FORGE_HOST_RUNTIME": "codex"}), \
            patch.object(native.codex_adapter, "resolve_binary", return_value="/mock/codex"), \
            patch.object(native.codex_adapter, "probe_capabilities", return_value={"output_schema_supported": True}), \
            patch.object(native, "run_process", side_effect=fake_process):
        pointer = bridge._run_independent_obligation_coverage_review(candidate, chunk,
            review_dir=review_dir, run_id="current-run", chunk_index=1, attempt=1,
            host_runtime="codex", model="gpt-6-luna", timeout=900, agent_id="main", runner="exec",
            binary="/mock/codex", config_path=None, controller=SimpleNamespace(check=lambda: None))
    envelope_path = review_dir / pointer["audit_path"]
    envelope = json.loads(envelope_path.read_text())
    audit = {"adapter_id": "codex", "host_runtime": "codex", "chunk_count": 1,
             "chunk_lifecycle": [{"chunk_index": 1, "status": "completed", "remote_operation_state": "completed"}],
             "chunk_runs": [{"chunk_index": 1, "response_path": str(candidate_path), "accepted_response_sha256": sha256_json(candidate), "independent_obligation_review": pointer}]}
    return req, chunk, candidate, review_dir, audit, envelope, envelope_path


@pytest.mark.parametrize("tamper", [False, True])
def test_both_actual_receipt_consumers_replay_native_children(tmp_path, tamper):
    import host_agent_bridge as bridge
    import thesis_format_pipeline as pipeline
    from semantic_contract import request_body_sha256, request_envelope_sha256
    req, chunk, candidate, review_dir, audit, envelope, envelope_path = committed_partition(tmp_path)
    pointer = audit["chunk_runs"][0]["independent_obligation_review"]
    if tamper:
        child_root = envelope_path.parent
        path = child_root / "native-batch-0001/prompt.txt"
        path.write_text(path.read_text() + " forced pass")
        reseal(child_root, envelope["review_audit"], 1, "prompt.txt")
        envelope_path.write_text(json.dumps(envelope, ensure_ascii=False))
        pointer["audit_sha256"] = sha256_file(envelope_path)
    callbacks = [
        lambda: bridge._validate_completed_obligation_ledger_chain(review_dir, envelope, pointer, candidate, chunk, chunk_index=1, attempt=1),
        lambda: pipeline._validate_independent_obligation_receipts(audit=audit, review_root=review_dir, expected_run_id="current-run", expected_request_body_sha=request_body_sha256(req), expected_request_envelope_sha=request_envelope_sha256(req), expected_request_file_sha=sha256_file(review_dir / "llm-request.json")),
    ]
    for callback in callbacks:
        if tamper:
            with pytest.raises(ValueError, match="partition input"):
                callback()
        else:
            callback()
