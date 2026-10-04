"""Provider representation changes must be exactly reversible, never semantic."""
import copy
import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from native_wire_schema import POLICY, POLICY_KEY, compact_native_schema, expand_shared_schema, review_wire_schema
from host_review_schema import native_output_schema, native_schema_support_errors, normalize_native_response
from format_spec_validation import validate_instance
from independent_review_partition import partition_packets, partition_schema, validate_partition_receipt
from semantic_source_references import build_source_reference_packet, source_reference_schema, source_inventory_generation_schema
import native_semantic_review as native
import tests.test_independent_review_partition as integration


def example():
    atom = {"type": "object", "properties": {
        "target": {"type": "string", "enum": ["exact source"], "description": "Current-source binding. " * 8},
        "reason": {"type": "string", "minLength": 1},
    }, "required": ["target"], "additionalProperties": False}
    return {"type": "object", "properties": {name: copy.deepcopy(atom) for name in ["left", "right", "third"]},
            "required": ["left", "right", "third"], "additionalProperties": False}


def test_exact_expansion_native_support_and_acceptance_language():
    local = example()
    before = copy.deepcopy(local)
    old = native_output_schema(local)
    shared = review_wire_schema(local, {POLICY_KEY: POLICY})
    assert expand_shared_schema(shared, set(shared["$defs"])) == {**old, "$defs": {}}
    assert compact_native_schema(shared) == shared
    assert native_schema_support_errors(shared) == []
    assert local == before
    valid = {name: {"target": "exact source", "reason": None} for name in ["left", "right", "third"]}
    mutations = [valid, {}, {**valid, "foreign": {}}, {**valid, "left": {"target": "wrong", "reason": None}},
                 {**valid, "left": {"target": "exact source"}}, {**valid, "left": None}]
    assert not validate_instance(valid, shared)
    for value in mutations:
        assert bool(validate_instance(value, old)) == bool(validate_instance(value, shared))
    normalized = normalize_native_response(valid, local)
    assert normalized == {name: {"target": "exact source"} for name in valid}
    assert validate_instance(normalized, local) == []


def test_definition_name_collisions_and_enum_data_are_preserved():
    old = native_output_schema(example())
    compacted = compact_native_schema(old)
    collision = next(iter(compacted["$defs"]))
    old["$defs"] = {collision: {"type": "string", "enum": ["original definition"]}}
    # A $ref or $id inside enum is data, not a schema or a scope declaration.
    old["properties"]["literal"] = {"enum": [{"$id": "literal-id", "$ref": "literal-ref"}]}
    compacted = compact_native_schema(old)
    added = set(compacted["$defs"]) - set(old["$defs"])
    assert added
    assert expand_shared_schema(compacted, added) == old
    assert compacted["$defs"][collision] == old["$defs"][collision]


@pytest.mark.parametrize("bad", [
    {"$id": "scoped"}, {"$anchor": "anchor"}, {"$dynamicRef": "#anchor"},
    {"$ref": "other.json"}, {"$ref": "#/$defs/missing"},
    {"properties": {"x": {"type": "string", "$defs": {}}}},
])
def test_unsupported_scope_never_silently_rewritten(bad):
    with pytest.raises(ValueError):
        compact_native_schema({"type": "object", **bad})


@pytest.mark.parametrize("keyword,mapping", [
    ("dependentSchemas", True), ("dependencies", True), ("patternProperties", True),
    ("definitions", True), ("contains", False), ("propertyNames", False),
    ("unevaluatedItems", False), ("unevaluatedProperties", False), ("contentSchema", False),
])
def test_scopes_in_other_schema_positions_are_rejected(keyword, mapping):
    for denied in [{"$id": "relative-scope", "type": "string"}, {"$ref": "external.json"}]:
        child = {"field": denied} if mapping else denied
        with pytest.raises(ValueError):
            compact_native_schema({"type": "object", keyword: child})


def test_legacy_policy_reconstructs_through_both_complete_consumers(tmp_path):
    import host_agent_bridge as bridge
    import thesis_format_pipeline as pipeline
    from semantic_contract import request_body_sha256, request_envelope_sha256, sha256_file
    build = bridge.build_obligation_coverage_request

    def legacy(*args, **kwargs):
        kwargs.setdefault("wire_schema_policy", None)
        return build(*args, **kwargs)

    # Produce a genuine legacy wire representation through the production
    # path with a mocked transport, then restore the current builders before
    # invoking both complete receipt consumers.
    with patch.object(bridge, "build_obligation_coverage_request", side_effect=legacy):
        request, chunk, candidate, review_dir, audit, envelope, envelope_path = integration.committed_partition(tmp_path, 8)
    saved_request = json.loads((envelope_path.parent / "request.json").read_text())
    assert POLICY_KEY not in saved_request
    old = build(candidate, chunk, run_id="current-run", chunk_index=1, wire_schema_policy=None)
    new = build(candidate, chunk, run_id="current-run", chunk_index=1)
    assert new.pop(POLICY_KEY) == POLICY
    assert new == old  # Source fields, selectors and candidate context unchanged.
    with pytest.raises(ValueError, match="unknown"):
        build(candidate, chunk, run_id="current-run", chunk_index=1, wire_schema_policy="unknown")
    pointer = audit["chunk_runs"][0]["independent_obligation_review"]
    bridge._validate_completed_obligation_ledger_chain(review_dir, envelope, pointer, candidate, chunk, chunk_index=1, attempt=1)
    pipeline._validate_independent_obligation_receipts(audit=audit, review_root=review_dir,
        expected_run_id="current-run", expected_request_body_sha=request_body_sha256(request),
        expected_request_envelope_sha=request_envelope_sha256(request),
        expected_request_file_sha=sha256_file(review_dir / "llm-request.json"))


def test_actual_failed_schema_is_losslessly_smaller_and_old_request_stays_old():
    captured = json.loads((ROOT / "tests/fixtures/four-check-output-limit-request.json").read_text())
    request = captured["request"]
    before = copy.deepcopy(request)
    packet = build_source_reference_packet(request)
    schema = source_inventory_generation_schema(source_reference_schema(native.OBLIGATION_COVERAGE_SCHEMA,
        packet, coverage=True, constrain_requirement_links=True))
    ids = partition_packets(request, packet)[0]["native_review_partition"]["check_ids"]
    assert ids == ["C00009", "C00010", "C00011", "C00012"]
    local = partition_schema(schema, ids)
    old = review_wire_schema(local, request)
    encoded = (json.dumps(old, ensure_ascii=False, indent=2) + "\n").encode()
    assert hashlib.sha256(encoded).hexdigest() == captured["first_partition_provider_schema_sha256"]
    assert len(encoded) == captured["first_partition_provider_schema_bytes"]
    shared = review_wire_schema(local, {**request, POLICY_KEY: POLICY})
    added = set(shared["$defs"]) - set(old["$defs"])
    assert expand_shared_schema(shared, added) == old
    assert len(json.dumps(shared, ensure_ascii=False, indent=2).encode()) < 0.60 * len(encoded)
    assert native_schema_support_errors(shared) == []
    assert request == before and POLICY_KEY not in request
    with pytest.raises(ValueError, match="unknown"):
        review_wire_schema(local, {POLICY_KEY: "unrecognized"})


@pytest.mark.parametrize("policy", [None, POLICY])
def test_real_producer_replay_and_resealed_definition_tamper(tmp_path, policy):
    request = integration.request(8)
    if policy is not None:
        request[POLICY_KEY] = policy
    audit = integration.run(request, tmp_path)
    raw = json.loads((tmp_path / "raw-response.json").read_text())
    validate_partition_receipt(request, tmp_path, raw, audit)
    path = tmp_path / "native-batch-0001/provider-response-schema.json"
    schema = json.loads(path.read_text())
    shared_names = [name for name in schema.get("$defs", {}) if name.startswith("shared_")]
    assert bool(shared_names) == (policy == POLICY)
    if shared_names:
        schema["$defs"][shared_names[0]] = {"type": "null"}
    else:
        schema["properties"]["results"] = {"type": "null"}
    path.write_text(json.dumps(schema))
    integration.reseal(tmp_path, audit, 1, path.name)
    with pytest.raises(ValueError, match="input is not reconstructed"):
        validate_partition_receipt(request, tmp_path, raw, audit)
