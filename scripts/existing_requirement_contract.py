"""Code-owned existing-requirement references, shared by review and merge.

References select an exact current-input occurrence, not a sequence number
the model may allocate. Only properties may be projected, after identity,
source and evidence have all matched. No identity is repaired here.
"""
from __future__ import annotations

import copy
import re
import unicodedata
from typing import Any

from semantic_contract import sha256_json


def normalized_source_text(text: Any) -> str:
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = re.sub(r"\s+", "", value)
    return value.strip(" ：:。；;，,、.!！？?\t\r\n")


def _id_set(value: Any) -> set[str] | None:
    if not isinstance(value, list) or not value:
        return None
    if any(not isinstance(item, str) or not item for item in value):
        return None
    result = set(value)
    return result if len(result) == len(value) else None


def existing_reference_errors(
    item: dict[str, Any], existing_map: dict[str, dict[str, Any]],
    clause_map: dict[str, dict[str, Any]], *, check_properties: bool = True,
) -> list[str]:
    reference = item.get("existing_requirement_id")
    if reference is None:
        return []  # New requirements receive IDs from the deterministic merger.
    if not isinstance(reference, str) or not reference:
        return ["invalid_existing_requirement_id"]
    existing = existing_map.get(reference)
    if not isinstance(existing, dict):
        return ["unknown_existing_requirement_id"]
    errors: list[str] = []
    if item.get("role") != existing.get("role") or (
        check_properties and item.get("properties") != existing.get("properties")
    ):
        errors.append("existing_requirement_payload_mismatch")
    clause_ids = _id_set(item.get("clause_ids"))
    if clause_ids is None or clause_ids != _id_set(existing.get("clause_ids")):
        errors.append("existing_requirement_clause_mismatch")
    evidence_ids = _id_set(item.get("evidence_ids"))
    if evidence_ids is None or evidence_ids != _id_set(existing.get("evidence_ids")):
        errors.append("existing_requirement_evidence_mismatch")
    parts = [clause_map.get(cid, {}).get("text") for cid in sorted(clause_ids or [])]
    if (
        not parts or any(not isinstance(part, str) or not part.strip() for part in parts)
        or normalized_source_text(existing.get("source_text"))
        != normalized_source_text(" | ".join(parts))
    ):
        errors.append("existing_requirement_source_text_mismatch")
    return errors


def project_authoritative_existing_payloads(
    response: dict[str, Any], existing_map: dict[str, dict[str, Any]],
    clause_map: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return a copy plus audit facts; never change raw input or a bad reference."""
    projected = copy.deepcopy(response)
    requirements = projected.get("requirements")
    repairs: list[dict[str, Any]] = []
    if not isinstance(requirements, list):
        return projected, repairs
    for index, item in enumerate(requirements):
        if not isinstance(item, dict) or item.get("existing_requirement_id") is None:
            continue
        if existing_reference_errors(item, existing_map, clause_map, check_properties=False):
            continue
        existing = existing_map[item["existing_requirement_id"]]
        before, after = item.get("properties"), existing.get("properties")
        if not isinstance(before, dict) or not before or not isinstance(after, dict) or not after:
            continue  # Do not turn an empty/invalid model payload into an executable one.
        if before == after:
            continue
        item["properties"] = copy.deepcopy(after)
        repairs.append({
            "response_index": index,
            "existing_requirement_id": item["existing_requirement_id"],
            "role": existing.get("role"),
            "clause_ids": sorted(item["clause_ids"]),
            "evidence_ids": sorted(item["evidence_ids"]),
            "before_properties_sha256": sha256_json(before),
            "after_properties_sha256": sha256_json(after),
            "rule_id": "authoritative_existing_requirement_payload",
        })
    return projected, repairs
