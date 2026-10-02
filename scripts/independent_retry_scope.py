"""Constrain a corrective read without rewriting any provider response.

Only a reproduced missing-inventory rejection of the identical current
candidate can retain individually validated siblings. This is generation
scope, not a semantic pass projection or a source/quotation normalizer.
"""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

from format_spec_validation import validate_instance
from host_review_schema import normalize_native_response
from semantic_contract import sha256_json, strict_json_loads
from semantic_source_references import (
    build_source_reference_packet, compile_source_reference_response,
    source_reference_schema,
)

POLICY = "validated_missing_inventory_retry_scope_v1"


def _exact_shape(value: Any) -> dict[str, Any]:
    """Portable scalar enums; the local equality check also fixes array order."""
    if isinstance(value, dict):
        props = {key: _exact_shape(item) for key, item in value.items()}
        return {"type": "object", "properties": props, "required": list(props),
                "additionalProperties": False}
    if isinstance(value, list):
        shapes = [_exact_shape(item) for item in value]
        return {"type": "array", "minItems": len(value), "maxItems": len(value),
                "items": {"anyOf": shapes} if shapes else {"type": "null"}}
    return {"enum": [value]}


def prepare_retry_scope(
    request: dict[str, Any], output_dir: Path, canonical_schema: dict[str, Any],
    *, provider_nullable_optionals: bool = False,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:
    """Replay the immediately preceding invocation, never a caller-selected file."""
    from native_semantic_review import (
        MissingSourceObligationInventoryError, NativeSemanticReviewError,
        validate_obligation_coverage_response,
    )
    feedback = request.get("retry_feedback")
    if (request.get("provider_attempt") != 2 or not isinstance(feedback, dict)
            or feedback.get("code") != MissingSourceObligationInventoryError.code):
        return {}, None
    suffix = "-provider-attempt-02"
    if not output_dir.name.endswith(suffix):
        raise NativeSemanticReviewError("missing-inventory retry directory is not invocation-scoped")
    parent_dir = output_dir.with_name(output_dir.name[:-len(suffix)])
    if parent_dir.is_symlink():
        raise NativeSemanticReviewError("missing-inventory parent directory is a symlink")
    names = ("request.json", "raw-response.json", "compiled-response.json",
             "source-reference-packet.json", "source-reference-compilation.json")
    paths = [parent_dir / name for name in names]
    # Test/fault-injection callers without captured invocation artifacts cannot
    # claim sibling retention. They still use the unchanged full review path.
    if not any(path.exists() for path in paths):
        return {}, None
    if any(not path.is_file() or path.is_symlink() for path in paths):
        raise NativeSemanticReviewError("missing-inventory parent evidence is incomplete")
    data = [strict_json_loads(path.read_text(encoding="utf-8")) for path in paths]
    prior, raw, compiled, packet, compilation = data
    expected = copy.deepcopy(request)
    expected.pop("retry_feedback", None)
    expected["provider_attempt"] = 1
    if prior != expected or packet != build_source_reference_packet(prior):
        raise NativeSemanticReviewError("missing-inventory parent is not the unchanged current request")
    replay, replay_compilation = compile_source_reference_response(
        raw, prior, canonical_schema, coverage=True,
        provider_nullable_optionals=provider_nullable_optionals,
    )
    if replay != compiled or replay_compilation != compilation:
        raise NativeSemanticReviewError("missing-inventory parent compilation does not reproduce")
    try:
        validate_obligation_coverage_response(
            copy.deepcopy(compiled), prior["checks"],
            allow_draft_disputes=prior.get("output_policy") == "review_draft",
        )
    except MissingSourceObligationInventoryError as exc:
        targets = list(exc.clause_ids)
        if targets != feedback.get("clause_ids"):
            raise NativeSemanticReviewError("missing-inventory retry scope disagrees with actual rejection")
    else:
        raise NativeSemanticReviewError("missing-inventory parent did not reproduce the claimed rejection")
    by_check = {check["check_id"]: check for check in prior["checks"]}
    by_result = {result["check_id"]: result for result in compiled["results"]}
    retained = {}
    for cid, check in by_check.items():
        if cid in targets:
            continue
        original = copy.deepcopy(by_result[cid])
        validated = copy.deepcopy(original)
        # A hidden second error must not be frozen merely because the full
        # validator reported the missing-inventory error first.
        validate_obligation_coverage_response(
            {"results": [validated]}, [check],
            allow_draft_disputes=prior.get("output_policy") == "review_draft",
        )
        if validated != original or original.get("verdict") == "incomplete":
            raise NativeSemanticReviewError("missing-inventory sibling is not an unchanged validated result")
        retained[cid] = original
    old_checks = {check["check_id"]: check for check in packet["checks"]}
    new_packet = build_source_reference_packet(request)
    new_checks = {check["check_id"]: check for check in new_packet["checks"]}
    old_wire_schema = source_reference_schema(canonical_schema, packet, coverage=True)
    wire = (normalize_native_response(raw, old_wire_schema)
            if provider_nullable_optionals else copy.deepcopy(raw))
    locks = {}
    for result in wire["results"]:
        cid = result["check_id"]
        if cid not in retained:
            continue
        old_spans = {span["ref_id"]: span for span in old_checks[cid]["source_spans"]}
        new_spans = new_checks[cid]["source_spans"]

        def current_ref(ref: str) -> str:
            old = copy.deepcopy(old_spans[ref]); old.pop("ref_id")
            matches = [span["ref_id"] for span in new_spans
                       if {k: v for k, v in span.items() if k != "ref_id"} == old]
            if len(matches) != 1:
                raise NativeSemanticReviewError("retained source reference has no unique current occurrence")
            return matches[0]

        value = copy.deepcopy(result)
        value["evidence_refs"] = [current_ref(ref) for ref in value["evidence_refs"]]
        for atom in value["identified_obligations"]:
            atom["source_ref"] = current_ref(atom["source_ref"])
        locks[cid] = value
    proof = {
        "policy": POLICY, "run_id": request.get("run_id"),
        "provenance": copy.deepcopy(request.get("provenance")),
        "request_sha256": sha256_json(request), "parent_request_sha256": sha256_json(prior),
        "checks_sha256": sha256_json(request["checks"]),
        "provider_nullable_optionals": provider_nullable_optionals,
        "fresh_review_check_ids": targets, "retained_check_ids": sorted(locks),
        "retained_result_sha256": {cid: sha256_json(value) for cid, value in retained.items()},
        "parent_artifacts": [{"path": str(path.resolve()),
                              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                             for path in paths],
        "retention_is_not_submission_approval": True,
    }
    return locks, proof


def constrain_retry_schema(schema: dict[str, Any], locks: dict[str, dict[str, Any]]) -> dict[str, Any]:
    scoped = copy.deepcopy(schema)
    branches = scoped["properties"]["results"]["items"]["anyOf"]
    for index, branch in enumerate(branches):
        cid = branch["properties"]["check_id"]["enum"][0]
        if cid in locks:
            branches[index] = _exact_shape(locks[cid])
    return scoped


def validate_retry_scope(response: dict[str, Any], schema: dict[str, Any],
                         locks: dict[str, dict[str, Any]], *, native: bool) -> None:
    from native_semantic_review import NativeSemanticReviewError
    canonical = normalize_native_response(response, schema) if native else copy.deepcopy(response)
    if validate_instance(canonical, schema):
        raise NativeSemanticReviewError("corrective review changed its validated sibling scope")
    for cid, locked in locks.items():
        matches = [item for item in canonical["results"] if item.get("check_id") == cid]
        if matches != [locked]:
            raise NativeSemanticReviewError("corrective review changed a retained current-source result")
