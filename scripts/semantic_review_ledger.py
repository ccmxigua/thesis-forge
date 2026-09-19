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


def _stable_requirement_id(requirement: dict[str, Any]) -> str:
    """Derive an ID from the accepted contract, never from array position.

    The model may propose a legacy ``existing_requirement_id``, but that value
    is retained only as source metadata.  The ledger's relation graph uses a
    deterministic ID owned by this code so reordering a response cannot move
    an edge to a different requirement.
    """
    canonical = {
        "role": requirement.get("role"),
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
                "classification": review.get("classification"),
                "evidence_ids": copy.deepcopy(requirement.get("evidence_ids") or []),
            })

    clause_records: list[dict[str, Any]] = []
    for clause_id in sorted(clause_by_id):
        clause = clause_by_id[clause_id]
        review = review_by_id.get(clause_id)
        record: dict[str, Any] = {
            "clause_id": clause_id,
            "classification": review.get("classification") if isinstance(review, dict) else None,
            "requirement_indexes": list(reverse.get(clause_id, [])) if isinstance(review, dict) else [],
            "evidence_ids": copy.deepcopy(clause.get("evidence_ids") or []),
        }
        if isinstance(review, dict) and isinstance(review.get("obligations"), list):
            record["obligations"] = copy.deepcopy(review["obligations"])
            record["obligation_decomposition"] = "explicit_model_items_validated_by_contract"
        else:
            record["obligations"] = []
            record["obligation_decomposition"] = "not_supplied"
        clause_records.append(record)

    provenance = response.get("provenance") if isinstance(response.get("provenance"), dict) else None
    return {
        "schema_version": "1.0",
        "contract_version": response.get("contract_version"),
        "source": "accepted_host_review_response",
        "relationship_policy": {
            "authoritative_edge": "requirements[].clause_ids",
            "reverse_relation": "derived_by_code",
            "semantic_inference": "disabled",
            "cross_chunk_deduplication": "disabled",
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
            }
            for index, item in enumerate(requirements)
            if isinstance(item, dict)
        ],
        "edges": edges,
        "response_sha256": sha256_json(response),
    }
