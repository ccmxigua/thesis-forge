"""One whole check per output, with historical native inputs unchanged."""
import copy
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from independent_review_partition import (
    POLICY, PREVIOUS_SINGLE_CHECK_POLICY, KEYED_TWO_POLICY,
    LEGACY_SINGLE_CHECK_POLICY, partition_packets, partition_schema,
)
from native_wire_schema import review_wire_schema
from semantic_contract import sha256_json
from semantic_source_references import build_source_reference_packet, source_reference_schema, source_inventory_generation_schema
from tests.test_keyed_partition_results import schema_for
from independent_review_partition import partition_generation_schema
import native_semantic_review as native


def test_captured_keyed_limit_preserves_old_inputs_and_whole_declaration_atoms():
    fixture = json.loads((Path(__file__).parent / "fixtures/keyed-declaration-output-limit-request.json").read_text())
    assert fixture["fixture_kind"] == "captured_request_for_offline_single_check_test_not_a_receipt"
    original = fixture["request"]
    before = copy.deepcopy(original)
    assert original["native_review_partition_policy"] == KEYED_TWO_POLICY
    old_child, old_schema = schema_for(original)
    old_wire = review_wire_schema(old_schema, original)
    old_bytes = (json.dumps(old_wire, ensure_ascii=False, indent=2) + "\n").encode()
    assert len(old_bytes) == fixture["failed_partition_provider_schema_bytes"] == 52494
    assert hashlib.sha256(old_bytes).hexdigest() == fixture["failed_partition_provider_schema_sha256"]
    assert hashlib.sha256(native._prompt(original, source_packet=old_child).encode()).hexdigest() == fixture["failed_partition_prompt_sha256"]

    # Only an offline request projection: no model answer or receipt is made.
    current = {**copy.deepcopy(original), "native_review_partition_policy": POLICY}
    packet = build_source_reference_packet(current)
    children = partition_packets(current, packet)
    assert len(children) == len(packet["checks"]) == 6
    assert [c["native_review_partition"]["check_ids"] for c in children] == [
        ["C00033"], ["C00034"], ["C00035"], ["C00036"], ["C00037"], ["C00038"]]
    assert [c["checks"][0] for c in children] == packet["checks"]
    for child in children:
        focus_ids = set(child["native_review_partition"]["check_ids"])
        assert child["orientation_only_checks"] == [
            check for check in packet["checks"] if check["check_id"] not in focus_ids]
        assert {check["check_id"] for check in child["checks"] + child["orientation_only_checks"]} == {
            check["check_id"] for check in packet["checks"]}
    full = source_inventory_generation_schema(source_reference_schema(native.OBLIGATION_COVERAGE_SCHEMA,
        packet, coverage=True, constrain_requirement_links=True))
    for child in children:
        assert child["native_review_partition"]["whole_request_sha256"] == sha256_json(current)
        ids = child["native_review_partition"]["check_ids"]
        array = partition_schema(full, ids)
        keyed = partition_generation_schema(full, ids, POLICY)
        assert keyed["properties"]["results"]["required"] == ids
        assert list(keyed["properties"]["results"]["properties"].values()) == array["properties"]["results"]["items"]["anyOf"]
        assert keyed["$defs"] == array["$defs"]
        assert "CLOSED OBJECT" in native._prompt(current, source_packet=child)
    mixed = children[1]["checks"][0]["review_context"]
    assert mixed["classification"] == "executable_with_external_check"
    assert [a["id"] for a in mixed["primary_obligations"]] == ["C00034-print", "C00034-truth"]
    assert mixed["source_clause_support"]
    assert original == before


def test_single_check_v1_replays_historical_orientation_and_prompt():
    fixture = json.loads((Path(__file__).parent / "fixtures/keyed-declaration-output-limit-request.json").read_text())
    old = copy.deepcopy(fixture["request"])
    old["native_review_partition_policy"] = LEGACY_SINGLE_CHECK_POLICY
    old_packet = build_source_reference_packet(old)
    old_children = partition_packets(old, old_packet)
    old_focus = next(child for child in old_children
                     if child["native_review_partition"]["check_ids"] == ["C00034"])
    assert old_focus["orientation_only_checks"] == old_packet["checks"]
    old_prompt = native._prompt(old, source_packet=old_focus)
    assert "orientation_only_checks preserves the whole source group and graph for context" in old_prompt
    old_full_schema = source_inventory_generation_schema(source_reference_schema(
        native.OBLIGATION_COVERAGE_SCHEMA, old_packet, coverage=True, constrain_requirement_links=True))
    old_generation_schema = partition_generation_schema(
        old_full_schema, ["C00034"], LEGACY_SINGLE_CHECK_POLICY)
    old_wire = review_wire_schema(old_generation_schema, old)
    old_wire_bytes = (json.dumps(old_wire, ensure_ascii=False, indent=2) + "\n").encode()
    assert len(old_wire_bytes) == 37933
    assert hashlib.sha256(old_wire_bytes).hexdigest() == (
        "9ddc318c430749b04975f769a73d0cd3ed12f1aaf746449306268bb05a30171e"
    )
    assert len(old_prompt.encode("utf-8")) == 95632
    assert hashlib.sha256(old_prompt.encode("utf-8")).hexdigest() == (
        "91add2e1852c802124418c197d9c6c94355c55ed07840dc5bd7121846cab866c"
    )

    compact = {**copy.deepcopy(old), "native_review_partition_policy": POLICY}
    compact_packet = build_source_reference_packet(compact)
    compact_children = partition_packets(compact, compact_packet)
    compact_focus = next(child for child in compact_children
                         if child["native_review_partition"]["check_ids"] == ["C00034"])
    assert [check["check_id"] for check in compact_focus["checks"]] == ["C00034"]
    assert "C00034" not in {check["check_id"] for check in compact_focus["orientation_only_checks"]}
    compact_prompt = native._prompt(compact, source_packet=compact_focus)
    assert "without duplicating the selected checks" in compact_prompt
    assert len(compact_prompt.encode("utf-8")) < len(old_prompt.encode("utf-8"))


def test_v2_keeps_the_historical_pretty_prompt_encoding():
    fixture = json.loads((Path(__file__).parent / "fixtures/keyed-declaration-output-limit-request.json").read_text())
    request = {**copy.deepcopy(fixture["request"]),
               "native_review_partition_policy": PREVIOUS_SINGLE_CHECK_POLICY}
    packet = build_source_reference_packet(request)
    child = next(item for item in partition_packets(request, packet)
                 if item["native_review_partition"]["check_ids"] == ["C00034"])
    prompt = native._prompt(request, source_packet=child)
    payload = prompt.split("Current run-bound audit request:\n", 1)[1]
    assert payload == json.dumps(child, ensure_ascii=False, sort_keys=True, indent=2)


def test_v3_compacts_only_json_whitespace_and_preserves_complete_source_data():
    fixture = json.loads((Path(__file__).parent / "fixtures/keyed-declaration-output-limit-request.json").read_text())
    base = copy.deepcopy(fixture["request"])
    previous = {**copy.deepcopy(base),
                "native_review_partition_policy": PREVIOUS_SINGLE_CHECK_POLICY}
    current = {**copy.deepcopy(base), "native_review_partition_policy": POLICY}
    previous_packet = build_source_reference_packet(previous)
    current_packet = build_source_reference_packet(current)
    previous_child = next(item for item in partition_packets(previous, previous_packet)
                          if item["native_review_partition"]["check_ids"] == ["C00034"])
    current_child = next(item for item in partition_packets(current, current_packet)
                         if item["native_review_partition"]["check_ids"] == ["C00034"])

    assert previous_child["checks"][0]["document_text"] == current_child["checks"][0]["document_text"]
    assert previous_child["checks"][0]["review_context"] == current_child["checks"][0]["review_context"]
    assert previous_child["orientation_only_checks"] and current_child["orientation_only_checks"]
    assert [check["check_id"] for check in previous_child["orientation_only_checks"]] == [
        check["check_id"] for check in current_child["orientation_only_checks"]]

    def source_spans(check):
        return [(span["source_field"], span["start"], span["end"], span["text"], span["source_sha256"])
                for span in check["source_spans"]]

    assert source_spans(previous_child["checks"][0]) == source_spans(current_child["checks"][0])
    assert [source_spans(check) for check in previous_child["orientation_only_checks"]] == [
        source_spans(check) for check in current_child["orientation_only_checks"]]
    assert [atom["id"] for atom in current_child["checks"][0]["review_context"]["primary_obligations"]] == [
        "C00034-print", "C00034-truth"]

    previous_prompt = native._prompt(previous, source_packet=previous_child)
    current_prompt = native._prompt(current, source_packet=current_child)
    previous_prefix, previous_json = previous_prompt.split("Current run-bound audit request:\n", 1)
    current_prefix, current_json = current_prompt.split("Current run-bound audit request:\n", 1)
    assert previous_prefix == current_prefix
    assert previous_json == json.dumps(previous_child, ensure_ascii=False, sort_keys=True, indent=2)
    assert current_json == json.dumps(current_child, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert len(current_json.encode("utf-8")) < 0.8 * len(previous_json.encode("utf-8"))
