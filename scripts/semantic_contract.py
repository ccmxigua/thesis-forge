"""Canonical provenance helpers for semantic-review artifacts.

The semantic model is allowed to interpret requirements, but its response is
only usable when it is tied to the exact extraction and request that produced
it.  This module deliberately contains no network or filesystem mutation so it
can be reused by the requirement engine, offline review tools, and tests.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
from typing import Any


PROVENANCE_VERSION = "1.0"
HOST_AGENT_ORIGIN = "fresh_host_agent"


def canonical_json(value: Any) -> bytes:
    """Serialize JSON deterministically for hashing and audit comparison."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def strict_json_dumps(
    value: Any,
    *,
    ensure_ascii: bool = False,
    indent: int | None = None,
    sort_keys: bool = False,
    separators: tuple[str, str] | None = None,
) -> str:
    """Serialize standards-compliant JSON and reject NaN/Infinity values."""
    return json.dumps(
        value,
        ensure_ascii=ensure_ascii,
        indent=indent,
        sort_keys=sort_keys,
        separators=separators,
        allow_nan=False,
    )


def strict_json_loads(text: str) -> Any:
    """Parse a JSON boundary without duplicate keys or non-finite numbers."""
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON object key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> Any:
        raise ValueError(f"non-finite JSON number is not allowed: {value}")

    def finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError(f"non-finite JSON number is not allowed: {value}")
        return parsed

    return json.loads(
        text,
        object_pairs_hook=reject_duplicates,
        parse_constant=reject_constant,
        parse_float=finite_float,
    )


def strict_json_read(path: str | Path) -> Any:
    """Read a UTF-8 JSON file using the duplicate-key/finite-number boundary."""
    return strict_json_loads(Path(path).read_text(encoding="utf-8"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value))


def request_without_provenance(request: dict[str, Any]) -> dict[str, Any]:
    """Return the canonical semantic request body without its identity block.

    The response contract historically called this value ``request_sha256``.
    Keep that field as a compatibility alias, but expose the domain explicitly
    so callers cannot accidentally compare it with a hash of the full request
    envelope.
    """
    result = copy.deepcopy(request)
    result.pop("provenance", None)
    return result


def request_body_sha256(request: dict[str, Any]) -> str:
    """Hash the semantic request body, excluding non-semantic provenance."""
    return sha256_json(request_without_provenance(request))


def request_envelope_sha256(request: dict[str, Any]) -> str:
    """Hash the complete request envelope, including its provenance block."""
    return sha256_json(request)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def evidence_payload(evidence_doc: dict[str, Any]) -> dict[str, Any]:
    """Return the complete semantic evidence identity.

    The source path is intentionally excluded: moving an unchanged evidence
    artifact must not change its semantic identity.  The structure and page
    evidence are included because they affect interpretation of examples,
    headers/footers, sections, and page-number clauses.
    """
    return {
        "evidence": evidence_doc.get("evidence", []),
        "page_evidence": evidence_doc.get("page_evidence", {}),
        "structure_evidence": evidence_doc.get("structure_evidence", {}),
        "structure_page_evidence": evidence_doc.get("structure_page_evidence", {}),
    }


def build_provenance(
    *,
    source_sha256: str,
    evidence_doc: dict[str, Any],
    clauses: list[dict[str, Any]],
    request_without_provenance: dict[str, Any],
    run_id: str | None = None,
    origin: str = HOST_AGENT_ORIGIN,
) -> dict[str, Any]:
    """Build the non-circular identity fields embedded in an LLM request."""
    result = {
        "version": PROVENANCE_VERSION,
        "origin": origin,
        "source_sha256": source_sha256,
        "evidence_sha256": sha256_json(evidence_payload(evidence_doc)),
        "clause_sha256": sha256_json(clauses),
        # ``request_sha256`` remains the contract-2.1 compatibility name.
        # New manifests and receipts additionally label this domain as
        # ``request_body_sha256``.
        "request_sha256": request_body_sha256(request_without_provenance),
    }
    if run_id is not None:
        result["run_id"] = run_id
    return result


def attach_request_provenance(
    request: dict[str, Any],
    *,
    source_sha256: str,
    evidence_doc: dict[str, Any],
    clauses: list[dict[str, Any]],
    run_id: str | None = None,
    origin: str = HOST_AGENT_ORIGIN,
) -> dict[str, Any]:
    """Attach a provenance block without hashing the block itself."""
    result = copy.deepcopy(request)
    result["provenance"] = build_provenance(
        source_sha256=source_sha256,
        evidence_doc=evidence_doc,
        clauses=clauses,
        run_id=run_id,
        request_without_provenance=request,
        origin=origin,
    )
    return result


def validate_response_provenance(
    response: Any,
    expected: dict[str, Any],
    *,
    require_fresh_origin: bool = True,
) -> list[str]:
    """Return stable contract errors for a response bound to another run."""
    if not isinstance(response, dict):
        return ["response_must_be_object"]
    actual = response.get("provenance")
    if not isinstance(actual, dict):
        return ["response_provenance_missing"]
    errors: list[str] = []
    keys = [
        "version", "source_sha256", "evidence_sha256", "clause_sha256", "request_sha256",
    ]
    if "run_id" in expected:
        keys.append("run_id")
    for key in keys:
        if actual.get(key) != expected.get(key):
            errors.append(f"provenance_{key}_mismatch")
    if actual.get("origin") != expected.get("origin"):
        errors.append("provenance_origin_mismatch")
    if require_fresh_origin and actual.get("origin") not in {
        "fresh_llm", "fresh_review", HOST_AGENT_ORIGIN,
    }:
        errors.append("provenance_origin_not_fresh")
    return errors
