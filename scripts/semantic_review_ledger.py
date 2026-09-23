"""Deterministic clause-to-requirement ledger for accepted host reviews.

This module records relationships that are present in the accepted response;
it never infers semantic identity, continuity, or missing obligations.
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from compliance import classification_requires_requirement
from host_review_contract import derived_requirement_indexes
from semantic_contract import sha256_json
from source_obligation_compiler import compile_known_source_obligations


def _stable_requirement_id(requirement: dict[str, Any]) -> str:
    """Derive an ID from the accepted contract, never from array position.

    The model may propose a legacy ``existing_requirement_id``, but that value
    is retained only as source metadata.  The ledger's relation graph uses a
    deterministic ID owned by this code so reordering a response cannot move
    an edge to a different requirement.
    """
    canonical = {
        "role": requirement.get("role"),
        "field_key": requirement.get("field_key"),
        "properties": requirement.get("properties") or {},
        "clause_ids": sorted(str(value) for value in (requirement.get("clause_ids") or [])),
        "evidence_ids": sorted(str(value) for value in (requirement.get("evidence_ids") or [])),
        "applicability": requirement.get("applicability"),
        "input_prerequisites": requirement.get("input_prerequisites"),
        "verification": requirement.get("verification"),
    }
    digest = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"R{digest[:16]}"


def _canonical_requirement(requirement: dict[str, Any]) -> str:
    """Return the exact code-owned identity used for duplicate detection."""
    return json.dumps(requirement, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def deduplicate_exact_requirements(
    response: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Remove only byte-for-byte semantic duplicates in contract 3.0.

    Cross-chunk semantic deduplication is unsafe because two similar clauses
    can intentionally produce different obligations.  The only automatic
    merge allowed here is an exact normalized requirement object, including
    its clause/evidence edges.  Anything less remains distinct and is recorded
    for review instead of being guessed together.
    """
    if response.get("contract_version") != "3.0":
        return copy.deepcopy(response), {
            "mode": "disabled_for_legacy_contract",
            "removed_indexes": [],
            "duplicate_groups": [],
        }
    output = copy.deepcopy(response)
    requirements = output.get("requirements")
    if not isinstance(requirements, list):
        return output, {"mode": "exact_only", "removed_indexes": [], "duplicate_groups": []}
    seen: dict[str, int] = {}
    kept: list[dict[str, Any]] = []
    removed: list[int] = []
    groups: list[dict[str, Any]] = []
    for index, requirement in enumerate(requirements):
        if not isinstance(requirement, dict):
            kept.append(requirement)
            continue
        key = _canonical_requirement(requirement)
        if key not in seen:
            seen[key] = len(kept)
            kept.append(requirement)
            continue
        removed.append(index)
        group = next((item for item in groups if item["kept_index"] == seen[key]), None)
        if group is None:
            group = {"kept_index": seen[key], "removed_indexes": []}
            groups.append(group)
        group["removed_indexes"].append(index)
    output["requirements"] = kept
    return output, {
        "mode": "exact_only",
        "removed_indexes": removed,
        "duplicate_groups": groups,
    }


def build_semantic_review_ledger(
    response: dict[str, Any], clauses: list[dict[str, Any]],
) -> dict[str, Any]:
    requirements = response.get("requirements") if isinstance(response, dict) else []
    reviews = response.get("clause_reviews") if isinstance(response, dict) else []
    requirements = requirements if isinstance(requirements, list) else []
    reviews = reviews if isinstance(reviews, list) else []
    clause_by_id = {
        str(item.get("id")): item for item in clauses
        if isinstance(item, dict) and item.get("id")
    }
    review_by_id = {
        str(item.get("clause_id")): item for item in reviews
        if isinstance(item, dict) and item.get("clause_id")
    }
    reverse = derived_requirement_indexes(response, clauses)
    requirement_ids = {
        index: _stable_requirement_id(item)
        for index, item in enumerate(requirements)
        if isinstance(item, dict)
    }
    edges: list[dict[str, Any]] = []
    for clause_id in sorted(reverse):
        review = review_by_id.get(clause_id)
        if not isinstance(review, dict) or not classification_requires_requirement(
            str(review.get("classification"))
        ):
            continue
        for requirement_index in reverse[clause_id]:
            requirement = requirements[requirement_index]
            if not isinstance(requirement, dict):
                continue
            edges.append({
                "clause_id": clause_id,
                "requirement_index": requirement_index,
                "requirement_id": requirement_ids.get(requirement_index),
                "edge_id": hashlib.sha256(
                    f"{clause_id}\0{requirement_ids.get(requirement_index)}".encode("utf-8")
                ).hexdigest()[:16],
                "classification": review.get("classification"),
                "evidence_ids": copy.deepcopy(requirement.get("evidence_ids") or []),
            })

    clause_records: list[dict[str, Any]] = []
    duplicate_groups: dict[str, list[int]] = {}
    source_aliases: list[dict[str, Any]] = []
    for index, item in enumerate(requirements):
        if not isinstance(item, dict):
            continue
        requirement_id = requirement_ids.get(index)
        if requirement_id:
            duplicate_groups.setdefault(requirement_id, []).append(index)
        source_id = item.get("id") or item.get("existing_requirement_id")
        if isinstance(source_id, str) and source_id.strip() and requirement_id:
            source_aliases.append({
                "source_requirement_id": source_id,
                "requirement_id": requirement_id,
                "response_index": index,
                "resolution": "code_owned_exact_source_alias",
            })
    for clause_id in sorted(clause_by_id):
        clause = clause_by_id[clause_id]
        review = review_by_id.get(clause_id)
        clause_requirement_indexes = list(reverse.get(clause_id, []))
        source_obligation_inventory: list[dict[str, Any]] = []
        source_text = str(clause.get("text") or clause.get("source_text_full") or "")
        source_text_sha256 = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        for fact in compile_known_source_obligations(source_text):
            candidates: list[dict[str, Any]] = []
            for requirement_index in clause_requirement_indexes:
                requirement = requirements[requirement_index]
                if (
                    not isinstance(requirement, dict)
                    or requirement.get("role") not in fact["roles"]
                ):
                    continue
                value: Any = requirement.get("properties")
                for path_part in str(fact["property_path"]).split(".")[1:]:
                    value = value.get(path_part) if isinstance(value, dict) else None
                candidates.append({
                    "requirement_index": requirement_index,
                    "requirement_id": requirement_ids.get(requirement_index),
                    "observed_value": copy.deepcopy(value),
                    "declared_checker_ids": copy.deepcopy(
                        requirement.get("verification", {}).get("checker_ids", [])
                        if isinstance(requirement.get("verification"), dict) else []
                    ),
                })
            expected = fact["expected_value"]
            matched_candidates = [
                item for item in candidates
                if item.get("observed_value") == expected
                and type(item.get("observed_value")) is type(expected)
            ]
            required_checker_ids = list(fact.get("required_checker_ids") or [])
            declared_checker_ids = sorted({
                checker_id
                for item in matched_candidates
                for checker_id in item.get("declared_checker_ids", [])
                if isinstance(checker_id, str)
            })
            missing_checker_ids = sorted(set(required_checker_ids) - set(declared_checker_ids))
            source_obligation_inventory.append({
                "id": fact["id"],
                "source_clause_sha256": source_text_sha256,
                "allowed_roles": copy.deepcopy(fact["roles"]),
                "property_path": fact["property_path"],
                "expected_value": copy.deepcopy(fact["expected_value"]),
                "candidate_requirement_bindings": candidates,
                "required_checker_ids": required_checker_ids,
                "declared_checker_ids": declared_checker_ids,
                "missing_checker_ids": missing_checker_ids,
                "checker_binding_status": "bound" if not missing_checker_ids else "missing_required_checker",
                "execution_receipt_status": "pending_generation",
                "render_status": (
                    "pending_word_render"
                    if "docx.word_render" in required_checker_ids else "not_required"
                ),
                "compiled_by": "source_obligation_compiler",
                "coverage_gate": "host_review_contract_role_property_check",
                "model_echo_required": False,
            })
        record: dict[str, Any] = {
            "clause_id": clause_id,
            "source_text_sha256": source_text_sha256,
            "classification": review.get("classification") if isinstance(review, dict) else None,
            "requirement_indexes": clause_requirement_indexes if isinstance(review, dict) else [],
            "evidence_ids": copy.deepcopy(clause.get("evidence_ids") or []),
            "source_obligation_inventory": source_obligation_inventory,
            "source_obligation_inventory_complete": False,
        }
        if isinstance(review, dict) and isinstance(review.get("obligations"), list):
            record["obligations"] = copy.deepcopy(review["obligations"])
            record["obligation_decomposition"] = "model_semantic_items_plus_code_compiled_source_facts"
        else:
            record["obligations"] = []
            record["obligation_decomposition"] = "not_supplied"
        clause_records.append(record)

    provenance = response.get("provenance") if isinstance(response.get("provenance"), dict) else None
    return {
        "schema_version": "1.2",
        "contract_version": response.get("contract_version"),
        "source_obligation_inventory_scope": "partial_machine_recognized_supplement",
        "source": "accepted_host_review_response",
        "relationship_policy": {
            "authoritative_edge": "requirements[].clause_ids",
            "reverse_relation": "derived_by_code",
            "semantic_inference": "disabled",
            "cross_chunk_deduplication": "exact_only_code_owned",
            "semantic_aliasing": "disabled",
            "duplicate_node_policy": "same_exact_node_id_and_record_group",
        },
        "provenance": copy.deepcopy(provenance),
        "clauses": clause_records,
        "requirements": [
            {
                "index": index,
                "requirement_id": requirement_ids.get(index),
                "source_requirement_id": item.get("id") or item.get("existing_requirement_id"),
                "role": item.get("role"),
                "clause_ids": copy.deepcopy(item.get("clause_ids") or []),
                "evidence_ids": copy.deepcopy(item.get("evidence_ids") or []),
                "canonical_requirement_sha256": hashlib.sha256(
                    _canonical_requirement(item).encode("utf-8")
                ).hexdigest(),
            }
            for index, item in enumerate(requirements)
            if isinstance(item, dict)
        ],
        "edges": edges,
        "source_aliases": source_aliases,
        "duplicate_requirement_groups": [
            {"requirement_id": requirement_id, "response_indexes": indexes}
            for requirement_id, indexes in sorted(duplicate_groups.items())
            if len(indexes) > 1
        ],
        "obligation_ledger": [
            {
                "clause_id": item["clause_id"],
                "obligations": copy.deepcopy(item.get("obligations") or []),
                "source_obligation_inventory": copy.deepcopy(
                    item.get("source_obligation_inventory") or []
                ),
                "status": item.get("obligation_decomposition"),
            }
            for item in clause_records
        ],
        "response_sha256": sha256_json(response),
    }
