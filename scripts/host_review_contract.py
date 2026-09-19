"""Shared, host-independent validation for contract-2.1 review responses."""
from __future__ import annotations

import re
from typing import Any

from compliance import classification_requires_requirement
from evidence_context_guards import sample_content_guard
from format_spec_validation import validate_instance


_GENERIC_SIGNATURE_LINE_PATTERNS = (
    re.compile(r"^(?:作者|研究生)(?:姓名|签名|签字)$"),
    re.compile(r"^日期$"),
    re.compile(r"^年.{0,12}月.{0,12}日(?:于.*)?$"),
)


def _normalized_fixed_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip("：:")


def _is_generic_signature_line(value: Any) -> bool:
    text = _normalized_fixed_text(value)
    return bool(text) and any(pattern.fullmatch(text) for pattern in _GENERIC_SIGNATURE_LINE_PATTERNS)


def _declarations_are_signature_only(properties: Any) -> bool:
    """Reject generic author/date lines as executable fixed declarations.

    A declaration resource may contain signature placeholders, but a bare
    author/date/location line is metadata or an external signing duty.  It is
    not enough evidence to create a generated declaration block.
    """
    if not isinstance(properties, dict) or not isinstance(properties.get("items"), list):
        return False
    fixed_texts: list[Any] = []
    for item in properties["items"]:
        if not isinstance(item, dict):
            continue
        for key in ("heading", "body"):
            if isinstance(item.get(key), str) and item[key].strip():
                fixed_texts.append(item[key])
        body_parts = item.get("body_parts")
        if isinstance(body_parts, list):
            fixed_texts.extend(value for value in body_parts if isinstance(value, str) and value.strip())
    return bool(fixed_texts) and all(_is_generic_signature_line(value) for value in fixed_texts)


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
        if role == "declarations":
            properties = item.get("properties")
            if isinstance(properties, dict) and properties.get("before_role") not in {
                "document_start", "abstract_title_zh",
            }:
                errors.append(
                    f"$.requirements[{index}].properties.before_role: must be "
                    "document_start or abstract_title_zh"
                )
            preferred_anchor = chunk.get("declaration_anchor_preference")
            if (
                isinstance(properties, dict)
                and isinstance(preferred_anchor, str)
                and properties.get("before_role") != preferred_anchor
            ):
                errors.append(
                    f"$.requirements[{index}].properties.before_role: must equal "
                    f"the current target's declaration_anchor_preference {preferred_anchor!r}"
                )
            if isinstance(properties, dict) and isinstance(properties.get("items"), list):
                source_texts = {
                    _normalized_fixed_text(evidence_context.get(str(evidence_id), {}).get("text"))
                    for evidence_id in item.get("evidence_ids", [])
                    if isinstance(evidence_context.get(str(evidence_id)), dict)
                }
                for item_index, declaration in enumerate(properties["items"]):
                    if not isinstance(declaration, dict):
                        continue
                    heading = declaration.get("heading")
                    if isinstance(heading, str) and heading.strip() and _normalized_fixed_text(heading) not in source_texts:
                        errors.append(
                            f"$.requirements[{index}].properties.items[{item_index}].heading: "
                            "must equal a complete cited source-evidence text; do not shorten a source paragraph into a guessed heading"
                        )
                    body_parts = declaration.get("body_parts")
                    if isinstance(body_parts, list):
                        for body_index, body in enumerate(body_parts):
                            if (
                                isinstance(body, str)
                                and body.strip()
                                and _normalized_fixed_text(body) not in source_texts
                            ):
                                errors.append(
                                    f"$.requirements[{index}].properties.items[{item_index}].body_parts[{body_index}]: "
                                    "must equal a complete cited source-evidence text; do not paraphrase or shorten fixed declaration prose"
                                )
            if _declarations_are_signature_only(properties):
                errors.append(
                    f"$.requirements[{index}].properties.items: generic author/date/signature lines "
                    "cannot form an executable declarations requirement; classify the cited clauses "
                    "as non-executable with requirement_indexes: []"
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
        normative_basis = review.get("normative_basis")
        # ``normative_basis`` is an evidence-backed basis, never a copy of a
        # review classification.  The JSON schema rejects the known value
        # already, but keep this invariant in the shared semantic validator so
        # a future schema extension cannot silently re-open the old bug.
        if normative_basis in {
            "covered", "executable", "external_compliance", "ignored",
            "informational", "not_applicable", "requires_metadata",
            "requires_source_content", "unresolved", "unsupported",
            "unsupported_backend", "unverifiable", "verify_existing",
        }:
            errors.append(
                f"$.clause_reviews[{review_index}].normative_basis: "
                "must be a declared evidence basis, not a classification"
            )
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
