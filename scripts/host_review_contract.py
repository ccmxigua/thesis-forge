"""Shared, host-independent validation for contract-2.1 review responses."""
from __future__ import annotations

from typing import Any

from compliance import classification_requires_requirement
from evidence_context_guards import sample_content_guard
from format_spec_validation import validate_instance


def summarize_contract_errors(errors: list[str], *, limit: int = 12) -> str:
    """Make retry feedback bounded and deterministic without payload leakage."""
    unique: list[str] = []
    for error in errors:
        if error not in unique:
            unique.append(error)
    suffix = f"; ... ({len(unique) - limit} more)" if len(unique) > limit else ""
    return "; ".join(unique[:limit]) + suffix


def validate_response(response: Any, chunk: dict[str, Any]) -> list[str]:
    """Validate one response against the exact chunk contract.

    This function has no provider or host imports.  Both the automatic bridge
    and the offline merger call it so malformed indexes, references and role
    payloads cannot be accepted through one entry point but rejected through
    the other.
    """
    if not isinstance(response, dict):
        return ["response_must_be_object"]

    errors: list[str] = []
    response_schema = chunk.get("response_schema")
    if not isinstance(response_schema, dict) or not response_schema:
        errors.append("response_schema_missing")
    else:
        errors.extend(validate_instance(response, response_schema, response_schema))

    contract = chunk.get("requirement_contract")
    if not isinstance(contract, dict):
        errors.append("requirement_contract_missing")
        contract = {}
    contract_root = {"$defs": contract.get("$defs", {})}
    role_schemas = contract.get("role_properties_schema", {})
    allowed_roles = set(contract.get("allowed_roles", []))

    clauses = chunk.get("clauses")
    if not isinstance(clauses, list):
        clauses = []
    clause_map = {
        str(item.get("id")): item
        for item in clauses
        if isinstance(item, dict) and item.get("id")
    }
    evidence_context = chunk.get("evidence_context")
    if not isinstance(evidence_context, dict):
        evidence_context = {}
    evidence_ids = {str(key) for key in evidence_context}

    requirements = response.get("requirements")
    if not isinstance(requirements, list):
        return errors + ["requirements_must_be_array"]

    requirement_clause_sets: list[set[str]] = []
    for index, item in enumerate(requirements):
        if not isinstance(item, dict):
            requirement_clause_sets.append(set())
            errors.append(f"$.requirements[{index}]: must_be_object")
            continue
        role = item.get("role")
        if role not in allowed_roles:
            errors.append(f"$.requirements[{index}].role: unknown_or_disallowed_role")
        role_schema = role_schemas.get(role) if isinstance(role_schemas, dict) else None
        if isinstance(role_schema, dict):
            try:
                errors.extend(validate_instance(
                    item.get("properties"), role_schema, contract_root,
                    f"$.requirements[{index}].properties",
                ))
            except (KeyError, ValueError) as exc:
                errors.append(
                    f"$.requirements[{index}].properties: role_schema_resolution_failed:{exc}"
                )

        clause_ids = item.get("clause_ids")
        clause_set = {
            str(value) for value in clause_ids
        } if isinstance(clause_ids, list) else set()
        requirement_clause_sets.append(clause_set)
        if not clause_set:
            errors.append(f"$.requirements[{index}].clause_ids: must_be_non_empty")
        unknown_clauses = sorted(clause_set - set(clause_map))
        if unknown_clauses:
            errors.append(
                f"$.requirements[{index}].clause_ids: unknown:{','.join(unknown_clauses)}"
            )

        cited_evidence = {
            str(value) for value in item.get("evidence_ids", [])
        } if isinstance(item.get("evidence_ids"), list) else set()
        allowed_evidence = {
            str(evidence_id)
            for clause_id in clause_set
            for evidence_id in (clause_map.get(clause_id, {}).get("evidence_ids", []) or [])
        }
        if not cited_evidence:
            errors.append(f"$.requirements[{index}].evidence_ids: must_be_non_empty")
        unknown_evidence = sorted(cited_evidence - evidence_ids)
        if unknown_evidence:
            errors.append(
                f"$.requirements[{index}].evidence_ids: not_in_chunk:{','.join(unknown_evidence)}"
            )
        unrelated_evidence = sorted(cited_evidence - allowed_evidence)
        if unrelated_evidence:
            errors.append(
                f"$.requirements[{index}].evidence_ids: not_backed_by_clause:{','.join(unrelated_evidence)}"
            )

    expected_clause_ids = [
        str(item.get("id")) for item in clauses if isinstance(item, dict)
    ]
    reviews = response.get("clause_reviews")
    if not isinstance(reviews, list):
        return errors + ["clause_reviews_must_be_array"]
    actual_clause_ids = [
        str(item.get("clause_id")) for item in reviews if isinstance(item, dict)
    ]
    if (
        len(actual_clause_ids) != len(expected_clause_ids)
        or len(set(actual_clause_ids)) != len(actual_clause_ids)
        or set(actual_clause_ids) != set(expected_clause_ids)
    ):
        errors.append("clause_reviews_must_cover_each_chunk_clause_exactly_once")

    referenced_indexes: set[int] = set()
    for review_index, review in enumerate(reviews):
        if not isinstance(review, dict):
            errors.append(f"$.clause_reviews[{review_index}]: must_be_object")
            continue
        clause_id = str(review.get("clause_id"))
        classification = review.get("classification")
        indexes = review.get("requirement_indexes")
        clause = clause_map.get(clause_id)
        if (
            isinstance(classification, str)
            and classification_requires_requirement(classification)
            and isinstance(clause, dict)
        ):
            guard = sample_content_guard(clause, clauses)
            if guard:
                errors.append(
                    f"$.clause_reviews[{review_index}]:"
                    f"sample_content_cannot_be_executable:{guard['kind']}"
                )
        if not isinstance(indexes, list):
            errors.append(
                f"$.clause_reviews[{review_index}].requirement_indexes: must_be_array"
            )
            continue
        valid_indexes: list[int] = []
        for requirement_index in indexes:
            if (
                isinstance(requirement_index, bool)
                or not isinstance(requirement_index, int)
                or requirement_index < 0
                or requirement_index >= len(requirements)
            ):
                errors.append(
                    f"$.clause_reviews[{review_index}].requirement_indexes: invalid_integer"
                )
                continue
            valid_indexes.append(requirement_index)
            referenced_indexes.add(requirement_index)
            if (
                isinstance(classification, str)
                and classification_requires_requirement(classification)
                and clause_id not in requirement_clause_sets[requirement_index]
            ):
                errors.append(
                    f"$.clause_reviews[{review_index}]: requirement_index_not_backed_by_clause"
                )
        if isinstance(classification, str):
            if classification_requires_requirement(classification) and not valid_indexes:
                errors.append(
                    f"$.clause_reviews[{review_index}]: executable_review_requires_requirement_index"
                )
            elif not classification_requires_requirement(classification) and valid_indexes:
                errors.append(
                    f"$.clause_reviews[{review_index}]: nonexecutable_review_must_not_reference_requirement"
                )

    unused_indexes = sorted(set(range(len(requirements))) - referenced_indexes)
    if unused_indexes:
        errors.append(
            "requirements_not_referenced_by_clause_review:" + ",".join(map(str, unused_indexes))
        )
    return errors
