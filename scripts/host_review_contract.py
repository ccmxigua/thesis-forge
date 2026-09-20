"""Shared, host-independent validation for host review contracts 2.1 and 3.0."""
from __future__ import annotations

import re
import hashlib
import json
from typing import Any

from compliance import classification_requires_requirement
from evidence_context_guards import sample_content_guard
from format_spec_validation import schema_support_errors, validate_instance
from format_contract_guards import cover_binding_errors


_GENERIC_SIGNATURE_LINE_PATTERNS = (
    re.compile(r"^(?:作者|研究生)(?:姓名|签名|签字)$"),
    re.compile(r"^日期$"),
    re.compile(r"^年.{0,12}月.{0,12}日(?:于.*)?$"),
)

HOST_REVIEW_CONTRACT_V2 = "2.1"
HOST_REVIEW_CONTRACT_V3 = "3.0"
SUPPORTED_HOST_REVIEW_CONTRACTS = {
    HOST_REVIEW_CONTRACT_V2, HOST_REVIEW_CONTRACT_V3,
}


def requirement_payload_errors(item: Any, index: int) -> list[str]:
    """Reject requirements that contain no executable semantic payload.

    ``properties`` is intentionally a role-specific union in the native
    provider schema.  The provider can therefore return a structurally valid
    empty object, but an empty object cannot describe a style, text, layout,
    or cover requirement.  Keeping this check here makes the local contract
    the final authority instead of silently accepting an identity-only
    ``field_key`` or a model placeholder.
    """
    if not isinstance(item, dict):
        return []
    properties = item.get("properties")
    if isinstance(properties, dict) and properties:
        return []
    return [f"$.requirements[{index}].properties: must_include_semantic_payload"]


def _response_sha256(response: Any) -> str:
    return hashlib.sha256(
        json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def derived_requirement_indexes(
    response: dict[str, Any], clauses: list[dict[str, Any]],
) -> dict[str, list[int]]:
    """Derive the reverse relation from the authoritative requirement edges.

    ``requirements[].clause_ids`` is the only relation the model may author in
    contract 3.0.  This helper is deterministic and intentionally does not
    guess an edge when the clause id is missing or unknown.
    """
    requirements = response.get("requirements") if isinstance(response, dict) else []
    if not isinstance(requirements, list):
        return {}
    clause_ids = [
        str(item.get("id")) for item in clauses
        if isinstance(item, dict) and item.get("id")
    ]
    result: dict[str, list[int]] = {clause_id: [] for clause_id in clause_ids}
    for index, requirement in enumerate(requirements):
        if not isinstance(requirement, dict) or not isinstance(requirement.get("clause_ids"), list):
            continue
        for value in requirement["clause_ids"]:
            clause_id = str(value)
            if clause_id in result:
                result[clause_id].append(index)
    return result


def project_compatibility_indexes(
    response: dict[str, Any], clauses: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return a local compatibility view without changing the model response."""
    projected = json.loads(json.dumps(response, ensure_ascii=False))
    mapping = derived_requirement_indexes(projected, clauses)
    for review in projected.get("clause_reviews", []) if isinstance(projected.get("clause_reviews"), list) else []:
        if not isinstance(review, dict):
            continue
        clause_id = str(review.get("clause_id"))
        if classification_requires_requirement(str(review.get("classification"))):
            review["requirement_indexes"] = list(mapping.get(clause_id, []))
        else:
            review["requirement_indexes"] = []
    return projected


def contract_error_records(
    errors: list[str], *, response: Any = None, chunk: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Convert validator strings into bounded, machine-readable failure facts."""
    records: list[dict[str, Any]] = []
    requirements = response.get("requirements", []) if isinstance(response, dict) else []
    clauses = chunk.get("clauses", []) if isinstance(chunk, dict) else []
    relation_facts: dict[str, list[int]] = {}
    if isinstance(response, dict) and isinstance(clauses, list):
        relation_facts = derived_requirement_indexes(response, clauses)
    for raw in errors:
        text = str(raw)
        lowered = text.lower()
        if "normative_basis" in lowered:
            code = "normative_basis_invalid"
        elif "requirement_index_not_backed_by_clause" in lowered:
            code = "requirement_relation_mismatch"
        elif "partial_clause_coverage" in lowered:
            code = "partial_clause_coverage"
        elif "must_include_semantic_payload" in lowered:
            code = "empty_requirement_properties"
        elif "unknown property" in lowered:
            code = "unknown_property"
        elif "unsupported_schema_keyword" in lowered:
            code = "validator_capability_gap"
        elif (
            "missing required" in lowered
            or "must_be" in lowered
            or "expected " in lowered
            or "type" in lowered
        ):
            code = "schema_contract_violation"
        else:
            code = "contract_validation_error"
        pointer_match = re.match(r"(\$[^:]+)", text)
        clause_match = re.search(r"clause_id=([^:;]+)", text)
        records.append({
            "code": code,
            "json_pointer": pointer_match.group(1) if pointer_match else None,
            "schema_pointer": pointer_match.group(1) if pointer_match else None,
            "clause_id": clause_match.group(1) if clause_match else None,
            "raw_error": text,
            "response_sha256": _response_sha256(response) if response is not None else None,
            "allowed_values": (
                [
                    "explicit_normative_text", "template_structure", "fixed_statement",
                    "sample_content", "source_content", "external_duty", "insufficient",
                ] if "normative_basis" in lowered else None
            ),
            "matching_requirement_indexes": (
                relation_facts.get(clause_match.group(1), [])
                if clause_match else None
            ),
            "requirement_count": len(requirements) if isinstance(requirements, list) else None,
            "semantic_review_required": code in {
                "partial_clause_coverage", "requirement_relation_mismatch",
            },
        })
    return records


def provenance_error_records(
    errors: list[str], *, response: Any = None,
) -> list[dict[str, Any]]:
    """Convert provenance failures into the same machine-readable ledger.

    Provenance failures are invocation-integrity failures, not semantic repair
    requests.  They still need structured terminal evidence so a retry or a
    stopped batch cannot collapse into a free-text summary.
    """
    response_hash = _response_sha256(response) if response is not None else None
    return [{
        "code": "provenance_validation_error",
        "json_pointer": "$.provenance",
        "schema_pointer": "$.provenance",
        "clause_id": None,
        "raw_error": str(error),
        "response_sha256": response_hash,
        "allowed_values": None,
        "matching_requirement_indexes": None,
        "requirement_count": (
            len(response.get("requirements", []))
            if isinstance(response, dict) and isinstance(response.get("requirements"), list)
            else None
        ),
        "semantic_review_required": False,
        "invocation_integrity_required": True,
    } for error in errors]


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


def _flatten_property_paths(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            result.update(_flatten_property_paths(child, path))
        return result
    if isinstance(value, list):
        return {prefix: value}
    return {prefix: value}


def _abstract_obligation_gaps(
    clause: dict[str, Any], requirements: list[dict[str, Any]], indexes: list[int],
) -> list[str]:
    """Reject a known partial abstract contract before it becomes executable.

    This is intentionally a narrow fail-closed detector for obligations that
    have an unambiguous lexical signal in the source clause.  It does not
    rewrite a response or decide whether prose is semantically good; it only
    prevents a short property projection from claiming to cover an entire
    clause that explicitly contains additional independent constraints.
    """
    text = re.sub(r"\s+", "", str(clause.get("text") or clause.get("source_text_full") or ""))
    if not re.search(r"中文摘要|摘要|chineseabstract|englishabstract|abstract", text, re.I):
        return []
    properties: dict[str, Any] = {}
    for index in indexes:
        if 0 <= index < len(requirements):
            item = requirements[index]
            if item.get("role") == "content_constraints":
                properties.update(_flatten_property_paths(item.get("properties") or {}))
    gaps: list[str] = []
    if re.search(r"thefollowingenglishisnotcorrect|thechineseabstract|英文.{0,20}中文摘要", text, re.I):
        gaps.append("abstract_target_or_translation_ambiguous")
    if re.search(r"300(?:字|字符).{0,24}1000(?:字|字符)|300.{0,24}1000(?:字|字符)", text):
        if "abstract_zh.min_chars" not in properties:
            gaps.append("abstract_zh.min_chars")
        if "abstract_zh.max_chars" not in properties:
            gaps.append("abstract_zh.max_chars")
    if "第三人称" in text and properties.get("abstract_zh.require_third_person") is not True:
        gaps.append("abstract_zh.require_third_person")
    if re.search(r"目的|方法|成果|结论|创新性", text):
        required_sections = properties.get("abstract_zh.required_sections")
        section_text = set(required_sections) if isinstance(required_sections, list) else set()
        expected_sections = {
            "目的": "purpose", "方法": "methods", "成果": "results",
            "结论": "conclusions", "创新性": "innovation",
        }
        for marker, section in expected_sections.items():
            if marker in text and section not in section_text:
                gaps.append(f"abstract_zh.required_sections:{section}")
    if re.search(r"不得?加评论|不应?加评论|不含评论", text) and properties.get("abstract_zh.prohibit_comments") is not True:
        gaps.append("abstract_zh.prohibit_comments")
    if "图表" in text and "abstract_zh.prohibited_objects" not in properties:
        gaps.append("abstract_zh.prohibited_objects:figures_or_tables")
    if "方程式" in text and "abstract_zh.prohibited_objects" not in properties:
        gaps.append("abstract_zh.prohibited_objects:chemical_equations")
    if re.search(r"非公知|非公开.*术语|公知.*符号", text) and "abstract_zh.prohibited_objects" not in properties:
        gaps.append("abstract_zh.prohibited_objects:nonpublic_symbols_and_terminology")
    return gaps


def _keyword_obligation_gaps(
    clause: dict[str, Any], requirements: list[dict[str, Any]], indexes: list[int],
) -> list[str]:
    text = re.sub(r"\s+", "", str(clause.get("text") or clause.get("source_text_full") or ""))
    if not re.search(r"关键词|keywords?", text, re.I):
        return []
    if not re.search(r"Chinesecharacters|汉字|中文字符", text, re.I):
        return []
    properties: dict[str, Any] = {}
    for index in indexes:
        if 0 <= index < len(requirements) and requirements[index].get("role") == "content_constraints":
            properties.update(_flatten_property_paths(requirements[index].get("properties") or {}))
    key = "keywords_en" if re.search(r"英文关键词|englishkeywords|english.*keywords", text, re.I) else "keywords_zh"
    gaps: list[str] = []
    if f"{key}.max_item_chars" not in properties:
        gaps.append(f"{key}.max_item_chars")
    elif properties.get(f"{key}.item_length_metric") != "cjk_characters":
        gaps.append(f"{key}.item_length_metric:cjk_characters")
    return gaps


def _table_obligation_gaps(
    clause: dict[str, Any], requirements: list[dict[str, Any]], indexes: list[int],
) -> list[str]:
    """Keep continuation-table obligations atomic and evidence-backed.

    A continuation/table clause commonly combines the marker, repeated header,
    caption position and caption alignment.  A table-level continuation object
    cannot silently stand in for a separate caption requirement.  This guard
    only checks explicit, mechanically representable obligations; it does not
    infer an omitted semantic rule.
    """
    text = re.sub(r"\s+", "", str(clause.get("text") or clause.get("source_text_full") or ""))
    if not re.search(r"表|table", text, re.I):
        return []
    selected = [
        requirements[index] for index in indexes
        if isinstance(index, int) and 0 <= index < len(requirements)
        and isinstance(requirements[index], dict)
    ]
    table_requirements = [item for item in selected if item.get("role") == "table"]
    caption_requirements = [item for item in selected if item.get("role") in {"table_caption", "figure_table_title"}]
    gaps: list[str] = []
    continuation_values = [
        item.get("properties", {}).get("continuation")
        for item in table_requirements
        if isinstance(item.get("properties"), dict)
    ]
    continuation_values = [item for item in continuation_values if isinstance(item, dict)]
    if "续" in text or "continuation" in text.lower():
        if not any(item.get("caption_suffix") == "(续)" for item in continuation_values):
            gaps.append("table.continuation.caption_suffix")
        if "重复表头" in text or "repeatheader" in text.lower():
            if not any(item.get("repeat_header_row") is True for item in continuation_values):
                gaps.append("table.continuation.repeat_header_row")
        if re.search(r"可省略|可略", text) and any(
            item.get("caption_required_on_continuation") is True for item in continuation_values
        ):
            gaps.append("table.continuation.optional_caption_marked_required")
    if re.search(r"表上方|置于表上|表上.*居中|居中.*表上", text):
        if not any(item.get("properties", {}).get("position") == "above" for item in caption_requirements):
            gaps.append("table_caption.position:above")
    if "居中" in text:
        if not any(
            (item.get("properties", {}).get("paragraph") or {}).get("alignment") == "center"
            for item in caption_requirements
        ):
            gaps.append("table_caption.paragraph.alignment:center")
    return gaps


def _validate_obligations(review: dict[str, Any], review_index: int) -> list[str]:
    obligations = review.get("obligations")
    if obligations is None:
        return []
    if not isinstance(obligations, list):
        return [f"$.clause_reviews[{review_index}].obligations: must_be_array"]
    errors: list[str] = []
    seen: set[str] = set()
    for index, obligation in enumerate(obligations):
        if not isinstance(obligation, dict):
            errors.append(f"$.clause_reviews[{review_index}].obligations[{index}]: must_be_object")
            continue
        identifier = obligation.get("id")
        if not isinstance(identifier, str) or not identifier.strip():
            errors.append(f"$.clause_reviews[{review_index}].obligations[{index}].id: must_be_non_empty")
        elif identifier in seen:
            errors.append(f"$.clause_reviews[{review_index}].obligations[{index}].id: duplicate")
        else:
            seen.add(identifier)
        if not isinstance(obligation.get("reason"), str) or not obligation["reason"].strip():
            errors.append(f"$.clause_reviews[{review_index}].obligations[{index}].reason: must_be_non_empty")
    if classification_requires_requirement(str(review.get("classification"))) and any(
        isinstance(item, dict) and item.get("status") != "covered" for item in obligations
    ):
        errors.append(
            f"$.clause_reviews[{review_index}].obligations: executable_review_requires_all_obligations_covered"
        )
    return errors


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
    contract_version = response.get("contract_version")
    if contract_version not in SUPPORTED_HOST_REVIEW_CONTRACTS:
        errors.append(f"contract_version_unsupported:{contract_version!r}")
    response_schema = chunk.get("response_schema")
    if not isinstance(response_schema, dict) or not response_schema:
        errors.append("response_schema_missing")
    else:
        errors.extend(schema_support_errors(response_schema))
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
        errors.extend(requirement_payload_errors(item, index))
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
        if role == "cover":
            for guard_error in cover_binding_errors({"cover": item.get("properties")}):
                errors.append(
                    f"$.requirements[{index}].properties{guard_error.removeprefix('$.cover')}"
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

    matching_requirement_indexes: dict[str, list[int]] = {
        clause_id: [
            index
            for index, clause_ids in enumerate(requirement_clause_sets)
            if clause_id in clause_ids
        ]
        for clause_id in clause_map
    }

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
        errors.extend(_validate_obligations(review, review_index))
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
        if contract_version == HOST_REVIEW_CONTRACT_V3:
            if "requirement_indexes" in review:
                errors.append(
                    f"$.clause_reviews[{review_index}].requirement_indexes: forbidden_in_contract_3.0"
                )
            valid_indexes = matching_requirement_indexes.get(clause_id, [])
            if isinstance(classification, str) and classification_requires_requirement(classification):
                referenced_indexes.update(valid_indexes)
                if not valid_indexes:
                    errors.append(
                        f"$.clause_reviews[{review_index}]: executable_review_requires_derived_requirement"
                    )
                if valid_indexes:
                    gaps = _abstract_obligation_gaps(clause or {}, requirements, valid_indexes)
                    gaps.extend(_keyword_obligation_gaps(clause or {}, requirements, valid_indexes))
                    gaps.extend(_table_obligation_gaps(clause or {}, requirements, valid_indexes))
                    if gaps:
                        errors.append(
                            f"$.clause_reviews[{review_index}]: partial_clause_coverage:"
                            + ",".join(gaps)
                        )
            continue
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
                    f"$.clause_reviews[{review_index}]: "
                    "requirement_index_not_backed_by_clause:"
                    f"clause_id={clause_id}:"
                    f"requirement_index={requirement_index}:"
                    "matching_indexes="
                    f"{matching_requirement_indexes.get(clause_id, [])}"
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
            if classification_requires_requirement(classification) and valid_indexes:
                gaps = _abstract_obligation_gaps(clause or {}, requirements, valid_indexes)
                gaps.extend(_keyword_obligation_gaps(clause or {}, requirements, valid_indexes))
                gaps.extend(_table_obligation_gaps(clause or {}, requirements, valid_indexes))
                if gaps:
                    errors.append(
                        f"$.clause_reviews[{review_index}]: partial_clause_coverage:"
                        + ",".join(gaps)
                    )

    unused_indexes = sorted(set(range(len(requirements))) - referenced_indexes)
    if unused_indexes:
        errors.append(
            "requirements_not_referenced_by_clause_review:" + ",".join(map(str, unused_indexes))
        )
    return errors
