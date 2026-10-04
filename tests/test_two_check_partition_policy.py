"""Bound new output units without reinterpreting historical native receipts."""
import copy
import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import native_semantic_review as native
import host_agent_bridge as bridge
import thesis_format_pipeline as pipeline
from independent_review_partition import POLICY, KEYED_TWO_POLICY, ARRAY_TWO_POLICY, LEGACY_POLICY, partition_packets, partition_schema
from native_wire_schema import POLICY as WIRE_POLICY, review_wire_schema
from semantic_source_references import build_source_reference_packet, source_reference_schema, source_inventory_generation_schema
from semantic_contract import sha256_json, sha256_file, request_body_sha256, request_envelope_sha256
from tests import test_independent_review_partition as integration


def test_captured_shared_schema_failure_is_split_without_rewriting_its_request():
    fixture = json.loads((ROOT / "tests/fixtures/shared-schema-output-limit-request.json").read_text())
    original = fixture["request"]
    before = copy.deepcopy(original)
    assert fixture["fixture_kind"] == "captured_request_for_offline_partition_test_not_a_receipt"
    old_packet = build_source_reference_packet(original)
    old_children = partition_packets(original, old_packet)
    schema = source_inventory_generation_schema(source_reference_schema(native.OBLIGATION_COVERAGE_SCHEMA,
        old_packet, coverage=True, constrain_requirement_links=True))
    old_schema = review_wire_schema(partition_schema(schema, old_children[0]["native_review_partition"]["check_ids"]), original)
    encoded = (json.dumps(old_schema, ensure_ascii=False, indent=2) + "\n").encode()
    assert hashlib.sha256(encoded).hexdigest() == fixture["failed_partition_provider_schema_sha256"]
    assert len(encoded) == fixture["failed_partition_provider_schema_bytes"] == 89089
    current = copy.deepcopy(original)
    current["native_review_partition_policy"] = KEYED_TWO_POLICY
    # Offline projection only. There is no copied response, new run identity,
    # native terminal event or production receipt in this fixture/test.
    packet = build_source_reference_packet(current)
    for new_check, old_check in zip(packet["checks"], old_packet["checks"]):
        # Source selectors bind the whole request, so a different policy must
        # get different selectors rather than laundering historical bindings.
        normalized_new, normalized_old = copy.deepcopy(new_check), copy.deepcopy(old_check)
        for new_span, old_span in zip(normalized_new["source_spans"], normalized_old["source_spans"]):
            assert new_span.pop("ref_id") != old_span.pop("ref_id")
        assert normalized_new == normalized_old
    schema = source_inventory_generation_schema(source_reference_schema(native.OBLIGATION_COVERAGE_SCHEMA,
        packet, coverage=True, constrain_requirement_links=True))
    children = partition_packets(current, packet)
    assert [child["native_review_partition"]["check_ids"] for child in children] == [
        ["C00009", "C00010"], ["C00011", "C00012"], ["C00013", "C00014"], ["C00015", "C00016"]]
    assert all(c["orientation_only_checks"] == packet["checks"] for c in children)
    assert [check for c in children for check in c["checks"]] == packet["checks"]
    for child in children[:2]:
        ids = child["native_review_partition"]["check_ids"]
        focused = partition_schema(schema, ids)
        wire = review_wire_schema(focused, current)
        assert len((json.dumps(wire, ensure_ascii=False, indent=2) + "\n").encode()) < len(encoded)
        assert focused["properties"]["results"]["minItems"] == 2
        assert focused["properties"]["results"]["maxItems"] == 2
        assert child["source_reference_request_sha256"] == sha256_json(current)
    assert original == before


@pytest.mark.parametrize("policy", [None, LEGACY_POLICY, ARRAY_TWO_POLICY, KEYED_TWO_POLICY, POLICY])
@pytest.mark.parametrize("wire_policy", [None, WIRE_POLICY])
def test_policy_versions_replay_through_both_complete_consumers(tmp_path, policy, wire_policy):
    build = bridge.build_obligation_coverage_request

    def selected(*args, **kwargs):
        kwargs.setdefault("partition_policy", policy)
        kwargs.setdefault("wire_schema_policy", wire_policy)
        return build(*args, **kwargs)

    with patch.object(bridge, "build_obligation_coverage_request", side_effect=selected):
        req, chunk, candidate, review_dir, audit, envelope, envelope_path = integration.committed_partition(tmp_path, 8)
    saved = json.loads((envelope_path.parent / "request.json").read_text())
    assert saved.get("native_review_partition_policy") == policy
    assert saved.get("native_wire_schema_policy") == wire_policy
    assert len(list(envelope_path.parent.glob("native-batch-*"))) == {None: 0, LEGACY_POLICY: 2, ARRAY_TWO_POLICY: 4, KEYED_TWO_POLICY: 4, POLICY: 8}[policy]
    pointer = audit["chunk_runs"][0]["independent_obligation_review"]
    bridge._validate_completed_obligation_ledger_chain(review_dir, envelope, pointer, candidate, chunk, chunk_index=1, attempt=1)
    pipeline._validate_independent_obligation_receipts(audit=audit, review_root=review_dir,
        expected_run_id="current-run", expected_request_body_sha=request_body_sha256(req),
        expected_request_envelope_sha=request_envelope_sha256(req),
        expected_request_file_sha=sha256_file(review_dir / "llm-request.json"))
    legacy = build(candidate, chunk, run_id="current-run", chunk_index=1, partition_policy=None)
    for version in [LEGACY_POLICY, ARRAY_TWO_POLICY, KEYED_TWO_POLICY, POLICY]:
        current = build(candidate, chunk, run_id="current-run", chunk_index=1, partition_policy=version)
        assert current.pop("native_review_partition_policy") == version
        assert current == legacy


@pytest.mark.parametrize("count,partitioned", [(1, False), (2, True), (3, True), (4, True), (7, True)])
def test_fresh_builder_handles_smaller_source_groups_without_changing_primary_packing(tmp_path, count, partitioned):
    req, chunk, candidate, review_dir, audit, envelope, envelope_path = integration.committed_partition(tmp_path, count)
    saved = json.loads((envelope_path.parent / "request.json").read_text())
    assert ("native_review_partition_policy" in saved) is partitioned
    assert len(saved["checks"]) == len(chunk["clauses"]) == count
    assert len(json.loads((review_dir / "llm-request-chunks.json").read_text())) == 1


def test_unknown_partition_policy_rejected_before_dispatch_or_artifact_writes(tmp_path):
    request = integration.request(8)
    request["native_review_partition_policy"] = "unrecognized"
    with patch.object(native, "run_process") as process:
        with pytest.raises(native.NativeSemanticReviewError, match="unknown native review partition policy"):
            integration.run(request, tmp_path, process)
        process.assert_not_called()
    assert not list(tmp_path.iterdir())
    with pytest.raises(ValueError, match="unknown native review partition policy"):
        native.build_obligation_coverage_request({}, {}, run_id="offline", chunk_index=1, partition_policy="unknown")


def test_whole_deadline_expiry_preserves_child_evidence_but_never_accepts_aggregate(tmp_path):
    with patch.object(native.time, "monotonic", side_effect=[0, 10, 901]):
        with pytest.raises(native.NativeSemanticReviewError, match="whole-invocation timeout"):
            integration.run(integration.request(8), tmp_path)
    assert (tmp_path / "native-batch-0001/raw-response.json").exists()
    assert not (tmp_path / "native-batch-0002").exists()
    assert not (tmp_path / "native-partition-projection.json").exists()
    assert not (tmp_path / "raw-response.json").exists()


@pytest.mark.parametrize("policy", [None, LEGACY_POLICY, ARRAY_TWO_POLICY, KEYED_TWO_POLICY, POLICY])
@pytest.mark.parametrize("wire_policy", [None, WIRE_POLICY])
def test_scoped_reassessment_reconstructs_saved_versions_and_keeps_budget(policy, wire_policy):
    from tests.test_condition_reassessment import incident
    from source_condition_reassessment import condition_feedback, condition_proposal_budget_receipt
    data, raw, candidate, chunk, original = incident()
    request = native.build_obligation_coverage_request(candidate, chunk,
        run_id=chunk["provenance"]["run_id"], chunk_index=1,
        partition_policy=policy, wire_schema_policy=wire_policy)
    request.update(attempt=1, provider_attempt=2)
    assert request.get("native_review_partition_policy") == policy
    rebuilt = condition_feedback(candidate, chunk, request, data["independent"])
    assert rebuilt is not None
    assert rebuilt["condition_atoms"] == original["condition_atoms"]
    assert rebuilt["submission_ready"] is False
    rebuilt["response_sha256"] = sha256_json(raw)
    assert condition_proposal_budget_receipt(candidate, chunk, [rebuilt], raw)["proposal_limit"] == 1
    damaged = copy.deepcopy(request)
    damaged["native_review_partition_policy"] = "unknown"
    assert condition_feedback(candidate, chunk, damaged, data["independent"]) is None
