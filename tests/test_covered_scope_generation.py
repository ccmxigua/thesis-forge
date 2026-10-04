"""Mirror an existing semantic invariant at generation, without choosing scope."""
import copy
import itertools
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import pytest
import host_agent_bridge as bridge
import requirements_engine as engine
from format_spec_validation import validate_instance
from host_review_contract import _validate_obligations, contract_error_records
from host_review_schema import (
    primary_generation_schema, native_output_schema, native_schema_support_errors,
    normalize_native_response,
)
from semantic_contract import attach_request_provenance, sha256_json
from responsibility_ledger import route_for_obligation


def captured():
    data = json.loads((ROOT / "tests/fixtures/covered-undecided-scope-incident.json").read_text())
    assert data["fixture_kind"] == "captured_source_and_invalid_primary_proposals_not_a_receipt"
    clauses = [copy.deepcopy(data["source_clause"])]
    evidence = {"evidence": list(data["evidence_context"].values())}
    chunk = engine.build_llm_request([], clauses, evidence, {}, "full", contract_version="3.0")
    chunk = attach_request_provenance(chunk, source_sha256=sha256_json(evidence),
        evidence_doc=evidence, clauses=clauses, run_id="offline-covered-scope-reproduction")
    response = {"contract_version": "3.0", "provenance": chunk["provenance"], "requirements": [],
        "clause_reviews": [copy.deepcopy(data["first_review"])],
        "unsupported_items": [], "reported_conflicts": []}
    response = normalize_native_response(response, chunk["response_schema"])
    return data, chunk, response


def atom_schemas(chunk):
    fresh = primary_generation_schema(chunk["response_schema"])
    branches = fresh["properties"]["clause_reviews"]["items"]["anyOf"]
    for branch in branches:
        atom = branch["properties"]["obligations"]["items"]
        yield branch["properties"]["classification"]["enum"][0], atom, native_output_schema(atom)


def native_atom(value, schema):
    # Only provider-required null sentinels, no semantic choices or fillers.
    return {name: copy.deepcopy(value.get(name)) for name in schema["properties"]}


def test_actual_first_proposal_is_rejected_during_generation_without_rewriting_it():
    data, chunk, raw = captured()
    before = copy.deepcopy((data, chunk, raw))
    assert raw["clause_reviews"][0]["clause_id"] == "C00003"
    assert chunk["clauses"][0]["text"] == "10043"
    errors = bridge.validate_host_agent_response(raw, chunk)
    assert errors == ["$.clause_reviews[0].obligations[0].applicability: undecided_scope_cannot_be_covered"]
    # Historical schema still parses the old response; semantic validation
    # continues to reject it. Only fresh generation is tightened.
    assert validate_instance(raw, chunk["response_schema"]) == []
    fresh = primary_generation_schema(chunk["response_schema"])
    assert validate_instance(raw, fresh)
    for _, local, wire in atom_schemas(chunk):
        atom = raw["clause_reviews"][0]["obligations"][0]
        assert validate_instance(atom, local)
        assert validate_instance(native_atom(atom, wire), wire)
        assert native_schema_support_errors(wire) == []
    assert (data, chunk, raw) == before


def test_every_status_scope_force_condition_combination_matches_existing_invariant():
    _, chunk, raw = captured()
    for classification, local, wire in atom_schemas(chunk):
        fields = local["properties"]
        combinations = itertools.product(fields["status"]["enum"], fields["applicability"]["enum"],
                                          fields["force"]["enum"], [False, True])
        for status, applicability, force, conditional in combinations:
            atom = {"id": "source-atom", "reason": "Offline shape witness only.",
                    "status": status, "applicability": applicability, "force": force,
                    "route": route_for_obligation(classification, status)}
            if conditional: atom["condition"] = "when the exact source condition holds"
            expected_invalid = status == "covered" and applicability in {"unknown", "conflicted"}
            review = {"classification": "informational", "obligations": [atom]}
            assert bool(_validate_obligations(review, 0)) == expected_invalid
            assert bool(validate_instance(atom, local)) == expected_invalid
            assert bool(validate_instance(native_atom(atom, wire), wire)) == expected_invalid
            assert normalize_native_response(native_atom(atom, wire), local) == atom


def test_empty_sample_inventory_and_pending_unknown_remain_available_as_model_proposals():
    _, chunk, raw = captured()
    fresh = primary_generation_schema(chunk["response_schema"])
    sample = copy.deepcopy(raw)
    sample["clause_reviews"][0]["obligations"] = []
    assert validate_instance(sample, fresh) == []
    assert bridge.validate_host_agent_response(sample, chunk) == []
    pending = copy.deepcopy(raw)
    pending["clause_reviews"][0]["classification"] = "unresolved"
    atom = pending["clause_reviews"][0]["obligations"][0]
    atom.update(status="unresolved", route="unknown")
    assert validate_instance(pending, fresh) == []
    assert _validate_obligations(pending["clause_reviews"][0], 0) == []
    # These examples establish representability, not the correct interpretation
    # of 10043. Neither becomes a runtime response or replaces the old proposal.
    assert raw["clause_reviews"][0]["obligations"][0]["status"] == "covered"


def test_generation_does_not_authorize_the_captured_retry_semantic_change():
    data, chunk, raw = captured()
    retry = copy.deepcopy(raw)
    retry["clause_reviews"][0]["obligations"][0]["applicability"] = data["retry_review"]["obligations"][0]["applicability"]
    assert retry["clause_reviews"][0]["obligations"][0]["applicability"] == "not_applicable"
    records = contract_error_records(bridge.validate_host_agent_response(raw, chunk), response=raw, chunk=chunk)
    error, _ = bridge._retry_semantic_change_error(raw, retry, records, contract_version="3.0", chunk=chunk)
    assert error and "applicability" in str(error)
    assert bridge.compact_model_packet(chunk, fresh_primary=False)["response_schema"] == chunk["response_schema"]


def test_projection_is_idempotent_and_does_not_modify_legacy_or_independent_contracts():
    _, chunk, _ = captured()
    fresh = primary_generation_schema(chunk["response_schema"])
    assert primary_generation_schema(fresh) == fresh
    legacy = copy.deepcopy(chunk["response_schema"])
    legacy["properties"]["contract_version"]["const"] = "2.1"
    assert primary_generation_schema(legacy) == legacy
    from native_semantic_review import OBLIGATION_COVERAGE_SCHEMA
    assert primary_generation_schema(OBLIGATION_COVERAGE_SCHEMA) == OBLIGATION_COVERAGE_SCHEMA
