"""Constrain a corrective read without rewriting any provider response.

Only a reproduced inventory rejection of the identical current
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
    decode_source_inventory_envelopes,
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
        EmptyInventoryVerdictError, empty_inventory_retry_feedback_is_bound,
        UnsafeUncertaintyVerdictError, unsafe_uncertainty_retry_feedback_is_bound,
        TypedSourceAtomAlignmentError, typed_alignment_retry_feedback_is_bound,
        validate_obligation_coverage_response,
    )
    feedback = request.get("retry_feedback")
    if (request.get("provider_attempt") != 2 or not isinstance(feedback, dict)
            or feedback.get("code") not in {MissingSourceObligationInventoryError.code,
                                            EmptyInventoryVerdictError.code,
                                            UnsafeUncertaintyVerdictError.code,
                                            TypedSourceAtomAlignmentError.code}):
        return {}, None
    empty_verdict = feedback["code"] == EmptyInventoryVerdictError.code
    unsafe_uncertainty = feedback["code"] == UnsafeUncertaintyVerdictError.code
    typed_alignment = feedback["code"] == TypedSourceAtomAlignmentError.code
    if typed_alignment and not typed_alignment_retry_feedback_is_bound(request):
        raise NativeSemanticReviewError("typed-alignment feedback is not current-source bound")
    if empty_verdict and not empty_inventory_retry_feedback_is_bound(request):
        raise NativeSemanticReviewError("empty-inventory feedback is not current-source bound")
    if unsafe_uncertainty and not unsafe_uncertainty_retry_feedback_is_bound(request):
        raise NativeSemanticReviewError("unsafe-uncertainty feedback is not current-source bound")
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
        if empty_verdict or unsafe_uncertainty or typed_alignment:
            raise NativeSemanticReviewError("empty-inventory retry lacks captured parent evidence")
        return {}, None
    if any(not path.is_file() or path.is_symlink() for path in paths):
        raise NativeSemanticReviewError("missing-inventory parent evidence is incomplete")
    data = [strict_json_loads(path.read_text(encoding="utf-8")) for path in paths]
    prior, raw, compiled, packet, compilation = data
    if provider_nullable_optionals and prior.get("native_review_partition_policy") is not None:
        from independent_review_partition import PROTOCOL, validate_partition_receipt
        partition_path = parent_dir / "native-partition-projection.json"
        if not partition_path.is_file():
            raise NativeSemanticReviewError("partitioned retry parent lacks native projection proof")
        validate_partition_receipt(prior, parent_dir, raw, {
            "adapter_id": "codex", "native_partition_projection": {
                "protocol": PROTOCOL, "path": str(partition_path.resolve()),
                "sha256": hashlib.sha256(partition_path.read_bytes()).hexdigest(),
            },
        })
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
    except (MissingSourceObligationInventoryError, EmptyInventoryVerdictError,
            UnsafeUncertaintyVerdictError, TypedSourceAtomAlignmentError) as exc:
        targets = list(exc.clause_ids)
        if exc.code != feedback["code"] or targets != feedback.get("clause_ids"):
            raise NativeSemanticReviewError("missing-inventory retry scope disagrees with actual rejection")
        if (empty_verdict or unsafe_uncertainty) and exc.rejected_results != feedback.get("rejected_results"):
            raise NativeSemanticReviewError("empty-inventory rejection differs from captured invocation")
        if typed_alignment and exc.disagreements != feedback.get("disagreements"):
            raise NativeSemanticReviewError("typed-alignment rejection differs from captured invocation")
    else:
        raise NativeSemanticReviewError("missing-inventory parent did not reproduce the claimed rejection")
    by_check = {check["check_id"]: check for check in prior["checks"]}
    by_result = {result["check_id"]: result for result in compiled["results"]}
    retained = {}
    additional_rejections = []
    for cid, check in by_check.items():
        if cid in targets:
            continue
        original = copy.deepcopy(by_result[cid])
        validated = copy.deepcopy(original)
        # A hidden second error must not be frozen merely because the full
        # validator reported the missing-inventory error first.
        try:
            validate_obligation_coverage_response(
                {"results": [validated]}, [check],
                allow_draft_disputes=prior.get("output_policy") == "review_draft",
            )
        except (MissingSourceObligationInventoryError, EmptyInventoryVerdictError,
                UnsafeUncertaintyVerdictError, TypedSourceAtomAlignmentError) as exc:
            # The full validator reports one error class first, not an
            # exhaustive failure set. A reproduced, check-local semantic
            # rejection must receive a fresh read, never become a frozen pass.
            # This grants no primary edit or interpretation; the unchanged
            # full validator still rejects any persistent disagreement.
            if list(exc.clause_ids) != [cid] or validated != original:
                raise NativeSemanticReviewError("additional retry rejection is not check-local and unchanged") from exc
            additional_rejections.append({"check_id": cid, "code": exc.code,
                "error_type": type(exc).__name__, "message": str(exc),
                "rejected_result_sha256": sha256_json(original)})
            continue
        if validated != original or original.get("verdict") == "incomplete":
            raise NativeSemanticReviewError("missing-inventory sibling is not an unchanged validated result")
        retained[cid] = original
    old_checks = {check["check_id"]: check for check in packet["checks"]}
    new_packet = build_source_reference_packet(request)
    new_checks = {check["check_id"]: check for check in new_packet["checks"]}
    old_wire_schema = source_reference_schema(canonical_schema, packet, coverage=True)
    decoded, _ = decode_source_inventory_envelopes(raw)
    wire = (normalize_native_response(decoded, old_wire_schema)
            if provider_nullable_optionals else decoded)
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
        "policy": ("validated_typed_alignment_retry_scope_v1" if typed_alignment else
                   "validated_unsafe_uncertainty_retry_scope_v1" if unsafe_uncertainty else
                   "validated_empty_inventory_verdict_retry_scope_v1" if empty_verdict else POLICY),
        "run_id": request.get("run_id"),
        "provenance": copy.deepcopy(request.get("provenance")),
        "request_sha256": sha256_json(request), "parent_request_sha256": sha256_json(prior),
        "checks_sha256": sha256_json(request["checks"]),
        "provider_nullable_optionals": provider_nullable_optionals,
        "fresh_review_check_ids": sorted(set(targets) | {r["check_id"] for r in additional_rejections}),
        "retained_check_ids": sorted(locks),
        "retained_result_sha256": {cid: sha256_json(value) for cid, value in retained.items()},
        "parent_artifacts": [{"path": str(path.resolve()),
                              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                             for path in paths],
        "retention_is_not_submission_approval": True,
    }
    if additional_rejections:
        proof["additional_reproduced_rejections"] = sorted(additional_rejections, key=lambda r: r["check_id"])
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
    decoded, _ = decode_source_inventory_envelopes(response)
    canonical = normalize_native_response(decoded, schema) if native else decoded
    if validate_instance(canonical, schema):
        raise NativeSemanticReviewError("corrective review changed its validated sibling scope")
    for cid, locked in locks.items():
        matches = [item for item in canonical["results"] if item.get("check_id") == cid]
        if matches != [locked]:
            raise NativeSemanticReviewError("corrective review changed a retained current-source result")


def validate_persisted_empty_inventory_scope(request: dict[str, Any], output_dir: Path,
                                           raw: dict[str, Any], audit: dict[str, Any]) -> None:
    """Consumers replay the same scope proof; resealed success tags are insufficient."""
    from native_semantic_review import EmptyInventoryVerdictError, UnsafeUncertaintyVerdictError, TypedSourceAtomAlignmentError, OBLIGATION_COVERAGE_SCHEMA
    feedback = request.get("retry_feedback")
    if not isinstance(feedback, dict) or feedback.get("code") not in {
            EmptyInventoryVerdictError.code, UnsafeUncertaintyVerdictError.code,
            TypedSourceAtomAlignmentError.code}:
        return
    native = audit.get("adapter_id") == "codex"
    locks, proof = prepare_retry_scope(request, output_dir, OBLIGATION_COVERAGE_SCHEMA,
                                     provider_nullable_optionals=native)
    path = output_dir / "validated-retry-scope.json"
    if proof is None or not path.is_file() or path.is_symlink():
        raise ValueError("empty-inventory correction lacks its replayed scope proof")
    expected = {"policy": proof["policy"], "proof_path": str(path.resolve()),
                "proof_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "fresh_review_check_ids": proof["fresh_review_check_ids"],
                "retained_check_ids": proof["retained_check_ids"]}
    if strict_json_loads(path.read_text(encoding="utf-8")) != proof or audit.get("corrective_review_scope") != expected:
        raise ValueError("empty-inventory correction scope does not reproduce")
    schema = constrain_retry_schema(source_reference_schema(OBLIGATION_COVERAGE_SCHEMA,
        build_source_reference_packet(request), coverage=True, constrain_requirement_links=True), locks)
    validate_retry_scope(raw, schema, locks, native=native)
