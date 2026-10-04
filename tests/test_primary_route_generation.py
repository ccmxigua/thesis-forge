"""Captured conflicts and declaration output guard fresh generation routing."""
import copy
import hashlib
import itertools
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import host_agent_bridge as bridge
import requirements_engine as engine
from format_spec_validation import validate_instance
from host_review_schema import (
    PRIMARY_CLAUSE_REVIEW_WIRE_FORMAT, native_output_schema,
    native_schema_support_errors, normalize_native_response,
    primary_clause_review_wire_schema, primary_generation_schema,
)
from native_wire_schema import compact_native_schema, expand_shared_schema
from responsibility_ledger import ROUTES, route_for_obligation
from semantic_contract import attach_request_provenance, sha256_json


def captured_conflict():
    data = json.loads((ROOT / "tests/fixtures/primary-route-conflict-incident.json").read_text())
    assert data["fixture_kind"] == "captured_primary_route_conflict_for_offline_tests_not_a_receipt"
    evidence = {"evidence": list(data["evidence_context"].values())}
    clauses = [copy.deepcopy(data["source_clause"])]
    chunk = engine.build_llm_request([], clauses, evidence, {}, "full", contract_version="3.0")
    chunk = attach_request_provenance(chunk, source_sha256=sha256_json(evidence),
        evidence_doc=evidence, clauses=clauses, run_id="offline-route-generation")
    raw = {"contract_version": "3.0", "requirements": [],
        "clause_reviews": [copy.deepcopy(data["review"])],
        "unsupported_items": [], "reported_conflicts": []}
    return data, chunk, normalize_native_response(raw, chunk["response_schema"])


def test_captured_conflicting_route_is_rejected_without_reinterpreting_the_source():
    data, chunk, response = captured_conflict()
    before = copy.deepcopy((data, chunk, response))
    assert validate_instance(response, chunk["response_schema"]) == []
    assert bridge.validate_host_agent_response(response, chunk) == [
        "$.clause_reviews[0].obligations[0].route: responsibility_route_conflict",
    ]
    generated = primary_generation_schema(chunk["response_schema"])
    assert validate_instance(response, generated)
    shape_witness = copy.deepcopy(response)
    shape_witness["clause_reviews"][0]["obligations"][0]["route"] = "unknown"
    assert validate_instance(shape_witness, generated) == []
    # A representability witness is not a corrected model response or receipt.
    assert (data, chunk, response) == before


def test_generation_routes_follow_every_classification_status_pair_in_both_schemas():
    _, chunk, _ = captured_conflict()
    generated = primary_generation_schema(chunk["response_schema"])
    branches = generated["properties"]["clause_reviews"]["items"]["anyOf"]
    seen = set()
    for branch in branches:
        wire = native_output_schema(branch)
        assert native_schema_support_errors(wire) == []
        atom_schema = branch["properties"]["obligations"]["items"]
        assert "route" in atom_schema["required"]
        statuses = atom_schema["properties"]["status"]["enum"]
        for classification, status, route in itertools.product(
                branch["properties"]["classification"]["enum"], statuses, sorted(ROUTES)):
            atom = {"id": "shape-witness", "status": status, "reason": "Offline shape only.",
                "force": "unknown", "applicability": "applicable", "route": route}
            review = {"clause_id": "C00070", "classification": classification,
                "reason": "Offline shape only.", "obligations": [atom]}
            expected_invalid = route != route_for_obligation(classification, status)
            assert bool(validate_instance(review, branch)) == expected_invalid
            native_atom = {name: copy.deepcopy(atom.get(name))
                for name in atom_schema["properties"]}
            native_review = {name: copy.deepcopy(review.get(name)) for name in wire["properties"]}
            native_review["obligations"] = [native_atom]
            assert bool(validate_instance(native_review, wire)) == expected_invalid
            if not expected_invalid:
                assert normalize_native_response(native_review, branch) == review
                seen.add((classification, status))
    canonical = chunk["response_schema"]["properties"]["clause_reviews"]["items"]["anyOf"]
    expected = {(classification, status) for branch in canonical
        for classification in branch["properties"]["classification"]["enum"]
        for status in branch["properties"]["obligations"]["items"]["properties"]["status"]["enum"]}
    assert seen == expected


def test_routes_cannot_be_omitted_or_hide_unknown_covered_scope():
    _, chunk, response = captured_conflict()
    generated = primary_generation_schema(chunk["response_schema"])
    atom = response["clause_reviews"][0]["obligations"][0]
    atom.pop("route")
    assert validate_instance(response, generated)
    atom["route"] = "unknown"
    assert validate_instance(response, generated) == []
    response["clause_reviews"][0]["classification"] = "executable"
    atom.update(status="covered", route="automatic", applicability="unknown")
    assert validate_instance(response, generated)


def test_real_declaration_body_and_blank_signature_sources_are_preserved():
    data = json.loads((ROOT / "tests/fixtures/primary-route-declaration-incident.json").read_text())
    assert data["fixture_kind"] == "captured_fixed_declaration_materialization_for_offline_tests_not_a_receipt"
    source = copy.deepcopy(data["request"])
    evidence = {"evidence": list(source["evidence_context"].values())}
    chunk = engine.build_llm_request([], source["clauses"], evidence, {}, "full", contract_version="3.0")
    response = normalize_native_response(data["raw_response"], chunk["response_schema"])
    before = copy.deepcopy((source, response))
    generated = primary_generation_schema(chunk["response_schema"])
    review_schema = generated["properties"]["clause_reviews"]["items"]
    for review in response["clause_reviews"]:
        assert validate_instance(review, review_schema) == []
    projected, audits = bridge._materialize_fixed_declaration_source_text(response, source)
    assert len(audits) == 1
    item = projected["requirements"][0]["properties"]["items"][0]
    assert item["heading"] == source["evidence_context"]["E00033"]["text"]
    assert item["body_parts"] == [source["evidence_context"]["E00034"]["text"]]
    lines = item["source_signature_lines"]
    assert len(lines) == 1
    assert lines[0]["text"] == source["evidence_context"]["E00035"]["text"]
    assert lines[0]["source_sha256"] == hashlib.sha256(lines[0]["text"].encode()).hexdigest()
    assert lines[0]["attestation_scope"] == "placeholder_presence_only"
    mixed = next(review for review in response["clause_reviews"] if review["clause_id"] == "C00034")
    assert {(atom["status"], atom["route"]) for atom in mixed["obligations"]} == {
        ("covered", "automatic"), ("unverifiable", "human"),
    }
    # This is the real-input counterexample that rejected the omission design.
    omitted = copy.deepcopy(response)
    for review in omitted["clause_reviews"]:
        for atom in review.get("obligations", []):
            atom.pop("route", None)
    assert any(validate_instance(review, review_schema) for review in omitted["clause_reviews"])
    _, omitted_audits = bridge._materialize_fixed_declaration_source_text(omitted, source)
    assert omitted_audits == []
    assert (source, response) == before


def test_projection_is_lossless_on_wire_and_preserves_historical_retry_contracts():
    _, chunk, _ = captured_conflict()
    canonical = copy.deepcopy(chunk["response_schema"])
    generated = primary_generation_schema(canonical)
    assert primary_generation_schema(generated) == generated
    assert canonical == chunk["response_schema"]
    assert bridge.compact_model_packet(chunk, fresh_primary=False)["response_schema"] == canonical
    legacy = copy.deepcopy(canonical)
    legacy["properties"]["contract_version"]["const"] = "2.1"
    assert primary_generation_schema(legacy) == legacy
    from native_semantic_review import OBLIGATION_COVERAGE_SCHEMA
    assert primary_generation_schema(OBLIGATION_COVERAGE_SCHEMA) == OBLIGATION_COVERAGE_SCHEMA
    native = native_output_schema(generated)
    wire = compact_native_schema(native)
    assert native_schema_support_errors(wire) == []
    added = set(wire.get("$defs", {})) - set(native.get("$defs", {}))
    assert expand_shared_schema(wire, added) == native


def test_fresh_primary_clause_map_requires_exact_chunk_keys_and_decodes_legacy_arrays():
    _, chunk, response = captured_conflict()
    canonical = chunk["response_schema"]
    fresh = bridge.compact_model_packet(chunk)["response_schema"]
    wire = fresh["properties"]["clause_reviews"]
    clause_id = chunk["clauses"][0]["id"]
    assert fresh["properties"]["response_wire_format"]["enum"] == [
        PRIMARY_CLAUSE_REVIEW_WIRE_FORMAT,
    ]
    assert fresh["required"][-1] == "response_wire_format"
    assert wire["required"] == [clause_id]
    assert set(wire["properties"]) == {clause_id}
    assert wire["properties"][clause_id] == {"$ref": "#/$defs/primaryClauseReviewByIdV1"}
    assert "publication_default_policy" in canonical["$defs"]["nonPublicAdministrationSpec"]["properties"]
    assert "publication_default_policy" not in fresh["$defs"]["nonPublicAdministrationSpec"]["properties"]

    # Eight distinct supplied IDs become eight required object properties; no
    # array cardinality keyword or semantic field is used as a substitute.
    eight_ids = [f"C{i:05d}" for i in range(25, 33)]
    eight_schema = copy.deepcopy(primary_generation_schema(canonical))
    for branch in eight_schema["properties"]["clause_reviews"]["items"]["anyOf"]:
        branch["properties"]["clause_id"]["enum"] = eight_ids
    eight_wire = primary_clause_review_wire_schema(eight_schema, eight_ids)
    assert eight_wire["properties"]["clause_reviews"]["required"] == eight_ids
    assert set(eight_wire["properties"]["clause_reviews"]["properties"]) == set(eight_ids)
    native = native_output_schema(eight_wire)
    assert native_schema_support_errors(native) == []

    complete = copy.deepcopy(response)
    complete["clause_reviews"][0]["obligations"][0]["route"] = "unknown"
    keyed = copy.deepcopy(complete)
    keyed["response_wire_format"] = PRIMARY_CLAUSE_REVIEW_WIRE_FORMAT
    keyed["clause_reviews"] = {clause_id: copy.deepcopy(complete["clause_reviews"][0])}
    assert validate_instance(keyed, fresh) == []
    assert normalize_native_response(keyed, canonical) == complete
    assert bridge.validate_host_agent_response(
        normalize_native_response(keyed, canonical), chunk,
    ) == []

    # Existing captured array receipts remain in their original form.
    assert normalize_native_response(response, canonical) == response
    assert bridge.compact_model_packet(chunk, fresh_primary=False)["response_schema"] == canonical

    malformed = copy.deepcopy(keyed)
    malformed["clause_reviews"][clause_id]["clause_id"] = "C99999"
    unmarked = copy.deepcopy(keyed)
    unmarked.pop("response_wire_format")
    missing_key = copy.deepcopy(keyed)
    missing_key["clause_reviews"].pop(clause_id)
    extra_key = copy.deepcopy(keyed)
    extra_key["clause_reviews"]["C99999"] = copy.deepcopy(complete["clause_reviews"][0])
    wrong_format = copy.deepcopy(keyed)
    wrong_format["response_wire_format"] = "unknown"
    for invalid in (malformed, unmarked, missing_key, extra_key, wrong_format):
        normalized = normalize_native_response(invalid, canonical)
        assert normalized == invalid
        assert validate_instance(normalized, canonical)
