"""Generation cardinality is structural; semantic payload stays unchanged."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from tests import test_independent_review_partition as integration
from independent_review_partition import (
    POLICY, KEYED_TWO_POLICY, ARRAY_TWO_POLICY, partition_packets, partition_generation_schema,
    partition_schema, join_partition_responses, validate_partition_receipt,
)
from semantic_source_references import (
    build_source_reference_packet, source_reference_schema, source_inventory_generation_schema,
)
from native_wire_schema import review_wire_schema, expand_shared_schema
from format_spec_validation import validate_instance
from semantic_contract import strict_json_loads
import native_semantic_review as native


def schema_for(request):
    packet = build_source_reference_packet(request)
    schema = source_inventory_generation_schema(source_reference_schema(
        native.OBLIGATION_COVERAGE_SCHEMA, packet, coverage=True, constrain_requirement_links=True))
    child = partition_packets(request, packet)[0]
    ids = child["native_review_partition"]["check_ids"]
    return child, partition_generation_schema(schema, ids, request["native_review_partition_policy"])


def test_native_array_gap_is_reproduced_and_keyed_shape_closes_it():
    old = integration.request(3, ARRAY_TWO_POLICY)
    child, schema = schema_for(old)
    wire = review_wire_schema(schema, old)
    valid = integration.raw(child)
    assert validate_instance(valid, wire) == []
    # These are syntactic witnesses, not source judgments or native responses.
    for rows in ([], valid["results"] * 3, [valid["results"][0]] * 2):
        assert validate_instance({"results": rows}, wire) == []
    request = integration.request(3)
    child, schema = schema_for(request)
    wire = review_wire_schema(schema, request)
    valid = integration.raw(child)
    assert validate_instance(valid, wire) == []
    missing = copy.deepcopy(valid); missing["results"].pop("section-00")
    extra = copy.deepcopy(valid); extra["results"]["foreign"] = extra["results"]["section-00"]
    wrong = copy.deepcopy(valid); wrong["results"]["section-00"]["check_id"] = "section-01"
    for value in (missing, extra, wrong, {"results": list(valid["results"].values())}):
        assert validate_instance(value, wire)
    with pytest.raises(ValueError):
        strict_json_loads('{"results":{"section-00":{},"section-00":{}}}')


def test_failed_declaration_request_retains_every_field_and_old_schema_hash():
    fixture = json.loads((Path(__file__).parent / "fixtures/two-check-declaration-output-limit-request.json").read_text())
    assert fixture["fixture_kind"] == "captured_request_for_offline_keyed_test_not_a_receipt"
    old = fixture["request"]
    before = copy.deepcopy(old)
    old_child, old_schema = schema_for(old)
    old_wire = review_wire_schema(old_schema, old)
    encoded = (json.dumps(old_wire, ensure_ascii=False, indent=2) + "\n").encode()
    assert len(encoded) == fixture["failed_partition_provider_schema_bytes"] == 53525
    assert hashlib.sha256(encoded).hexdigest() == fixture["failed_partition_provider_schema_sha256"]
    current = {**copy.deepcopy(old), "native_review_partition_policy": KEYED_TWO_POLICY}
    child, schema = schema_for(current)
    assert child["native_review_partition"]["check_ids"] == ["C00033", "C00034"]
    packet = build_source_reference_packet(current)
    full = source_inventory_generation_schema(source_reference_schema(native.OBLIGATION_COVERAGE_SCHEMA,
        packet, coverage=True, constrain_requirement_links=True))
    array_schema = partition_schema(full, ["C00033", "C00034"])
    # Prove structural projection only: all per-check constraints and defs
    # compare exactly, including both obligations on the mixed declaration.
    assert list(schema["properties"]["results"]["properties"].values()) == array_schema["properties"]["results"]["items"]["anyOf"]
    assert schema["$defs"] == array_schema["$defs"]
    assert child["checks"][1]["review_context"]["classification"] == "executable_with_external_check"
    assert len(child["checks"][1]["review_context"]["primary_obligations"]) == 2
    assert old == before
    assert "CLOSED OBJECT" in native._prompt(current, source_packet=child)
    assert "CLOSED OBJECT" not in native._prompt(old, source_packet=old_child)


@pytest.mark.parametrize("damage", ["missing", "foreign", "identity", "array", "wrong-batch"])
def test_keyed_projection_never_repairs_incomplete_or_misbound_native_output(damage):
    request = integration.request(3)
    packets = partition_packets(request, build_source_reference_packet(request))
    responses = [integration.raw(p) for p in packets]
    if damage == "missing": responses[0]["results"].pop("section-00")
    elif damage == "foreign": responses[0]["results"]["foreign"] = {}
    elif damage == "identity": responses[0]["results"]["section-00"]["check_id"] = "section-01"
    elif damage == "array": responses[0]["results"] = list(responses[0]["results"].values())
    else: responses.reverse()
    with pytest.raises(ValueError): join_partition_responses(packets, responses)


@pytest.mark.parametrize("policy", [KEYED_TWO_POLICY, POLICY])
def test_keyed_projection_reorders_only_slots_and_never_changes_inner_payload(policy):
    request = integration.request(3, policy)
    packets = partition_packets(request, build_source_reference_packet(request))
    responses = [integration.raw(p) for p in packets]
    expected = [copy.deepcopy(v) for r in responses for v in r["results"].values()]
    responses[0]["results"] = dict(reversed(list(responses[0]["results"].items())))
    before = copy.deepcopy(responses)
    assert join_partition_responses(packets, responses) == {"results": expected}
    assert responses == before


def test_resealed_array_payload_cannot_masquerade_as_new_native_keyed_response(tmp_path):
    request = integration.request(3)
    audit = integration.run(request, tmp_path)
    aggregate = json.loads((tmp_path / "raw-response.json").read_text())
    path = tmp_path / "native-batch-0001/raw-response.json"
    value = json.loads(path.read_text())
    value["results"] = list(value["results"].values())
    path.write_text(json.dumps(value))
    integration.reseal(tmp_path, audit, 1, path.name)
    with pytest.raises(ValueError, match="native terminal"):
        validate_partition_receipt(request, tmp_path, aggregate, audit)
