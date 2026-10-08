"""Validate current-session semantic review input and mint an honest receipt.

The receipt binds a declared current-session review to exact current inputs. It
does not authenticate a provider/model or claim that a native CLI was used.
"""
from __future__ import annotations

import copy
import hashlib
import re
from pathlib import Path
from typing import Any

from format_spec_validation import validate_instance
from native_semantic_review import (
    NativeSemanticReviewError,
    validate_response as validate_semantic_response,
)
from semantic_contract import sha256_file, sha256_json, strict_json_read


ROOT = Path(__file__).resolve().parents[1]
RESPONSE_SCHEMA_PATH = ROOT / "schema" / "current-session-semantic-review-response.schema.json"
RECEIPT_SCHEMA_PATH = ROOT / "schema" / "current-session-semantic-review-receipt.schema.json"
CURRENT_SESSION_PROTOCOL = "current_session_semantic_content_review_v1"
RECEIPT_PROTOCOL = "current_session_semantic_review_receipt_v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class CurrentSessionSemanticReviewError(ValueError):
    """A current-session response is unbound, malformed, or outside scope."""


def _schema(path: Path) -> dict[str, Any]:
    value = strict_json_read(path)
    if not isinstance(value, dict):
        raise CurrentSessionSemanticReviewError(f"schema is not an object: {path.name}")
    from format_spec_validation import schema_support_errors
    unsupported = schema_support_errors(value)
    if unsupported:
        raise CurrentSessionSemanticReviewError(
            f"unsupported {path.name}: " + "; ".join(unsupported[:8])
        )
    return value


def _require_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise CurrentSessionSemanticReviewError(f"{label} must be a lowercase SHA-256")
    return value


def current_session_binding(
    request: dict[str, Any], *, code_fingerprint_sha256: str,
) -> dict[str, Any]:
    """Return exact runtime identity the conversation response must echo."""
    checks = request.get("checks")
    if not isinstance(checks, list) or not checks:
        raise CurrentSessionSemanticReviewError("current-session review requires non-empty checks")
    check_ids = [item.get("check_id") if isinstance(item, dict) else None for item in checks]
    if (any(not isinstance(value, str) or not value for value in check_ids)
            or len(set(check_ids)) != len(check_ids)):
        raise CurrentSessionSemanticReviewError("semantic request has missing or duplicate check IDs")
    for name in ("case_id", "run_id"):
        if not isinstance(request.get(name), str) or not request[name]:
            raise CurrentSessionSemanticReviewError(f"semantic request {name} is missing")
    document_projection = [
        [item["check_id"], item.get("document_text", "")]
        for item in checks
    ]
    if request.get("document_text_sha256") != sha256_json(document_projection):
        raise CurrentSessionSemanticReviewError(
            "document_text_sha256 does not match the current ordered check text"
        )
    return {
        "case_id": request["case_id"],
        "run_id": request["run_id"],
        "source_sha256": _require_digest(request.get("source_sha256"), "source_sha256"),
        "format_spec_sha256": _require_digest(request.get("format_spec_sha256"), "format_spec_sha256"),
        "document_text_sha256": _require_digest(request.get("document_text_sha256"), "document_text_sha256"),
        "request_sha256": sha256_json(request),
        "checks_sha256": sha256_json(checks),
        "check_ids": check_ids,
        "code_fingerprint_sha256": _require_digest(code_fingerprint_sha256, "code_fingerprint_sha256"),
    }


def validate_current_session_semantic_review_response(
    response_path: Path,
    request: dict[str, Any],
    *,
    code_fingerprint_sha256: str,
    output_policy: str,
    native_runtime: str | None = None,
    native_model: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate exact response coverage and return audit plus code-minted receipt."""
    if output_policy != "review_draft":
        raise CurrentSessionSemanticReviewError(
            "current-session semantic review is restricted to review_draft"
        )
    if native_runtime is not None or native_model is not None:
        raise CurrentSessionSemanticReviewError(
            "current-session semantic review cannot be mixed with a native runtime/model"
        )
    path = response_path.expanduser().resolve()
    if not path.is_file():
        raise CurrentSessionSemanticReviewError(f"current-session response is missing: {path}")
    response = strict_json_read(path)
    schema = _schema(RESPONSE_SCHEMA_PATH)
    errors = validate_instance(response, schema)
    if errors:
        raise CurrentSessionSemanticReviewError(
            "current-session response violates its schema: " + "; ".join(errors[:8])
        )
    expected_binding = current_session_binding(
        request, code_fingerprint_sha256=code_fingerprint_sha256,
    )
    if response.get("binding") != expected_binding:
        raise CurrentSessionSemanticReviewError(
            "current-session response binding differs from current request/input/code"
        )
    try:
        normalized_results = validate_semantic_response(
            {"results": response["results"]}, request["checks"],
        )
    except (NativeSemanticReviewError, ValueError, TypeError, KeyError) as exc:
        raise CurrentSessionSemanticReviewError(
            f"current-session result validation failed: {exc}"
        ) from exc

    response_sha256 = sha256_file(path)
    verdict_counts = {
        verdict: sum(1 for result in normalized_results if result["verdict"] == verdict)
        for verdict in ("satisfied", "noncompliant", "uncertain")
    }
    binding = copy.deepcopy(expected_binding)
    review = {
        "schema_version": "1.0",
        "protocol": CURRENT_SESSION_PROTOCOL,
        "status": "completed",
        "review_mode": "current_session",
        "review_origin_attestation": "current_session_declared",
        "origin_identity_verified": False,
        "provider_model_verified": False,
        "native_invocation": False,
        "external_model_request_made_by_pipeline": False,
        "case_id": binding["case_id"],
        "run_id": binding["run_id"],
        "source_sha256": binding["source_sha256"],
        "format_spec_sha256": binding["format_spec_sha256"],
        "document_text_sha256": binding["document_text_sha256"],
        "request_sha256": binding["request_sha256"],
        "response_sha256": response_sha256,
        "binding": binding,
        "checks": copy.deepcopy(request["checks"]),
        "results": normalized_results,
        "summary": {"check_count": len(normalized_results), **verdict_counts},
        "submission_ready": False,
    }
    receipt = {
        "schema_version": "1.0",
        "protocol": RECEIPT_PROTOCOL,
        "status": "completed",
        "review_mode": "current_session",
        "review_origin_attestation": "current_session_declared",
        "origin_identity_verified": False,
        "provider_model_verified": False,
        "native_invocation": False,
        "external_model_request_made_by_pipeline": False,
        "submission_ready": False,
        "binding": binding,
        "response_file": {
            "path": str(path),
            "bytes": path.stat().st_size,
            "sha256": response_sha256,
        },
        "results_sha256": sha256_json(normalized_results),
        "verdict_counts": verdict_counts,
    }
    receipt_schema = _schema(RECEIPT_SCHEMA_PATH)
    receipt_errors = validate_instance(receipt, receipt_schema)
    if receipt_errors:
        raise CurrentSessionSemanticReviewError(
            "generated current-session receipt violates its schema: "
            + "; ".join(receipt_errors[:8])
        )
    return review, receipt
