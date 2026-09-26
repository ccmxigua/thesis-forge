"""Shared, host-independent validation for host review contracts 2.1 and 3.0."""
from __future__ import annotations

import re
import hashlib
import json
import copy
from typing import Any

from compliance import classification_requires_requirement
from evidence_context_guards import sample_content_guard
from format_spec_validation import schema_support_errors, validate_instance
from format_contract_guards import cover_binding_errors
from semantic_contract import strict_json_loads
from existing_requirement_contract import (
    existing_reference_errors, project_authoritative_existing_payloads,
)
from source_obligation_compiler import (
    SECURITY_MARKING_SHORTER_ALLOWANCE_OBLIGATION_ID,
    compile_known_source_obligations,
    source_fact_value_matches,
    compile_soft_keyword_count_guidance,
    compile_explicit_keyword_count_range,
    has_explicit_keyword_count_signal,
    source_text_candidates,
    exact_clause_source_text,
    materialize_complete_abstract_source_constraints,
    materialize_soft_keyword_count_guidance,
    materialize_known_source_verification,
)


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


def analyze_requirement_relations(
    response: dict[str, Any], clauses: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Classify every requirement-to-clause edge deterministically.

    Contract 3.0 has one authored direction: ``requirements[].clause_ids``.
    This helper does not infer missing edges.  It is shared by validation,
    structured error records, and the bridge's narrow mechanical projection so
    those layers cannot disagree about which requirements are safe to remove.
    """
    requirements = response.get("requirements", []) if isinstance(response, dict) else []
    if not isinstance(requirements, list):
        return []
    authoritative_clauses = clauses if isinstance(clauses, list) else []
    clause_id_counts: dict[str, int] = {}
    for clause in authoritative_clauses:
        if isinstance(clause, dict) and clause.get("id"):
            clause_id = str(clause["id"])
            clause_id_counts[clause_id] = clause_id_counts.get(clause_id, 0) + 1
    reviews = response.get("clause_reviews", []) if isinstance(response, dict) else []
    if not isinstance(reviews, list):
        reviews = []
    review_counts: dict[str, int] = {}
    review_classifications: dict[str, list[str]] = {}
    for review in reviews:
        if not isinstance(review, dict) or not review.get("clause_id"):
            continue
        clause_id = str(review["clause_id"])
        review_counts[clause_id] = review_counts.get(clause_id, 0) + 1
        review_classifications.setdefault(clause_id, []).append(
            str(review.get("classification") or "")
        )
    if not clause_id_counts:
        # Unit-level callers may only provide the response.  This fallback can
        # classify an informational-only relation, but cannot claim that a
        # clause is known outside the response; production callers always pass
        # the authoritative current chunk.
        clause_id_counts = {clause_id: 1 for clause_id in review_counts}

    facts: list[dict[str, Any]] = []
    for index, requirement in enumerate(requirements):
        raw_clause_ids = requirement.get("clause_ids") if isinstance(requirement, dict) else None
        clause_ids = [str(value) for value in raw_clause_ids] if isinstance(raw_clause_ids, list) else []
        unique_clause_ids = list(dict.fromkeys(clause_ids))
        unknown_clause_ids = sorted(
            clause_id for clause_id in unique_clause_ids if clause_id not in clause_id_counts
        )
        missing_review_ids = sorted(
            clause_id for clause_id in unique_clause_ids if review_counts.get(clause_id, 0) == 0
        )
        duplicate_review_ids = sorted(
            clause_id for clause_id in unique_clause_ids if review_counts.get(clause_id, 0) != 1
            and clause_id not in missing_review_ids
        )
        classifications = {
            clause_id: list(review_classifications.get(clause_id, []))
            for clause_id in unique_clause_ids
        }
        if not clause_ids:
            category = "missing_clause_relation"
        elif unknown_clause_ids:
            category = "unknown_clause_relation"
        elif missing_review_ids or duplicate_review_ids:
            category = "missing_clause_review"
        else:
            values = [classifications[clause_id][0] for clause_id in unique_clause_ids]
            requires = [classification_requires_requirement(value) for value in values]
            if values and all(value == "informational" for value in values):
                category = "informational_only"
            elif any(requires) and not all(requires):
                category = "mixed_execution_classification"
            elif values and all(requires):
                category = "execution"
            else:
                category = "non_requirement_classification"
        facts.append({
            "requirement_index": index,
            "clause_ids": unique_clause_ids,
            "clause_classifications": classifications,
            "category": category,
            "unknown_clause_ids": unknown_clause_ids,
            "missing_review_ids": missing_review_ids,
            "duplicate_review_ids": duplicate_review_ids,
        })
    return facts


def project_compatibility_indexes(
    response: dict[str, Any], clauses: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return a local compatibility view without changing the model response."""
    projected = copy.deepcopy(response)
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


_RELATION_CATEGORY_CODES = {
    "informational_only": "informational_requirement_forbidden",
    "mixed_execution_classification": "mixed_execution_classification_relation",
    "missing_clause_review": "missing_clause_review",
    "unknown_clause_relation": "unknown_clause_relation",
    "non_requirement_classification": "non_requirement_classification_relation",
    "execution": "unused_executable_requirement",
}


def _relation_record(
    *,
    code: str,
    raw_error: str,
    response: Any,
    requirements: list[Any] | None,
    fact: dict[str, Any] | None = None,
    json_pointer: str | None = None,
    clause_id: str | None = None,
    matching_requirement_indexes: list[int] | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "code": code,
        "json_pointer": json_pointer,
        "schema_pointer": json_pointer,
        "clause_id": clause_id,
        "raw_error": raw_error,
        "response_sha256": _response_sha256(response) if response is not None else None,
        "allowed_values": None,
        "matching_requirement_indexes": matching_requirement_indexes,
        "requirement_count": len(requirements) if isinstance(requirements, list) else None,
        "semantic_review_required": True,
    }
    if fact is not None:
        record.update({
            "requirement_index": fact.get("requirement_index"),
            "clause_ids": list(fact.get("clause_ids", [])),
            "clause_classifications": fact.get("clause_classifications", {}),
            "relation_category": fact.get("category"),
            "unknown_clause_ids": list(fact.get("unknown_clause_ids", [])),
            "missing_review_ids": list(fact.get("missing_review_ids", [])),
            "duplicate_review_ids": list(fact.get("duplicate_review_ids", [])),
            "mechanically_removable": code == "informational_requirement_forbidden",
        })
    return record


def contract_error_records(
    errors: list[str], *, response: Any = None, chunk: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Convert validator strings into bounded, machine-readable failure facts."""
    records: list[dict[str, Any]] = []
    requirements = response.get("requirements", []) if isinstance(response, dict) else []
    clauses = chunk.get("clauses", []) if isinstance(chunk, dict) else []
    analysis_clauses = clauses if isinstance(clauses, list) else []
    if isinstance(response, dict) and not analysis_clauses:
        analysis_clauses = [
            {"id": review.get("clause_id")}
            for review in response.get("clause_reviews", [])
            if isinstance(review, dict) and review.get("clause_id")
        ]
    relation_analysis = (
        analyze_requirement_relations(response, analysis_clauses)
        if isinstance(response, dict) else []
    )
    relation_by_index = {
        int(item["requirement_index"]): item
        for item in relation_analysis
        if isinstance(item, dict) and isinstance(item.get("requirement_index"), int)
    }
    relation_facts: dict[str, list[int]] = {}
    if isinstance(response, dict) and isinstance(analysis_clauses, list):
        relation_facts = derived_requirement_indexes(response, analysis_clauses)

    def empty_cover_institution(pointer: str | None) -> bool:
        match = re.fullmatch(
            r"\$\.requirements\[(\d+)\]\.properties(?:\.institution)?",
            str(pointer or ""),
        )
        if match is None or not isinstance(requirements, list):
            return False
        index = int(match.group(1))
        if index >= len(requirements) or not isinstance(requirements[index], dict):
            return False
        requirement = requirements[index]
        properties = requirement.get("properties")
        return (
            requirement.get("role") == "cover"
            and isinstance(properties, dict)
            and not str(properties.get("institution") or "").strip()
        )

    def append_generic(
        *, code: str, text: str, pointer: str | None, clause_id: str | None,
        matching: list[int] | None = None,
    ) -> None:
        records.append({
            "code": code,
            "json_pointer": pointer,
            "schema_pointer": pointer,
            "clause_id": clause_id,
            "raw_error": text,
            "response_sha256": _response_sha256(response) if response is not None else None,
            "allowed_values": (
                [
                    "explicit_normative_text", "template_structure", "fixed_statement",
                    "sample_content", "source_content", "external_duty", "insufficient",
                ] if "normative_basis" in text.lower() else None
            ),
            "matching_requirement_indexes": matching,
            "requirement_count": len(requirements) if isinstance(requirements, list) else None,
            "semantic_review_required": code in {
                "partial_clause_coverage", "requirement_relation_mismatch",
                "missing_derived_requirement", "informational_requirement_forbidden",
                "mixed_execution_classification_relation", "missing_clause_review",
                "unknown_clause_relation", "non_requirement_classification_relation",
                "unused_executable_requirement",
                "executable_review_obligations_missing",
                "executable_review_obligations_uncovered",
                "non_public_administration_fields_missing",
            },
        })

    for raw in errors:
        text = str(raw)
        lowered = text.lower()
        pointer_match = re.match(r"(\$[^:]+)", text)
        clause_match = re.search(r"clause_id=([^:;]+)", text)
        pointer = pointer_match.group(1) if pointer_match else None
        derived_clause_id = clause_match.group(1) if clause_match else None
        if derived_clause_id is None and pointer:
            review_match = re.fullmatch(r"\$\.clause_reviews\[(\d+)\](?:\..*)?", pointer)
            if review_match and isinstance(response, dict):
                review_index = int(review_match.group(1))
                reviews = response.get("clause_reviews")
                if isinstance(reviews, list) and review_index < len(reviews):
                    review = reviews[review_index]
                    if isinstance(review, dict) and review.get("clause_id"):
                        derived_clause_id = str(review["clause_id"])

        aggregate_match = re.fullmatch(
            r"requirements_not_referenced_by_clause_review:(\d+(?:,\d+)*)",
            text,
        )
        if aggregate_match and relation_by_index:
            for index_text in aggregate_match.group(1).split(","):
                requirement_index = int(index_text)
                fact = relation_by_index.get(requirement_index)
                if fact is None:
                    append_generic(
                        code="requirement_relation_mismatch", text=text,
                        pointer=f"$.requirements[{requirement_index}]", clause_id=None,
                    )
                    continue
                code = _RELATION_CATEGORY_CODES.get(
                    str(fact.get("category")), "requirement_relation_mismatch"
                )
                matching = sorted({
                    matching_index
                    for clause_id in fact.get("clause_ids", [])
                    for matching_index in relation_facts.get(str(clause_id), [])
                })
                records.append(_relation_record(
                    code=code,
                    raw_error=text,
                    response=response,
                    requirements=requirements,
                    fact=fact,
                    json_pointer=f"$.requirements[{requirement_index}]",
                    matching_requirement_indexes=matching,
                ))
            continue

        if pointer and pointer.endswith(".existing_requirement_id"):
            code = text.rsplit(": ", 1)[-1]
            if not (code.startswith("existing_requirement_") or code in {
                "unknown_existing_requirement_id", "invalid_existing_requirement_id",
            }):
                code = "existing_requirement_reference_invalid"
            append_generic(code=code, text=text, pointer=pointer, clause_id=None)
            candidates = (chunk or {}).get("rule_spec", {}).get("requirements", [])
            records[-1].update({
                "allowed_values": sorted({
                    item["id"] for item in candidates
                    if isinstance(item, dict) and isinstance(item.get("id"), str)
                }),
                "semantic_review_required": True,
                "identity_repair_allowed": False,
            })
            continue
        if "informational_requirement_forbidden" in lowered:
            pointer = pointer_match.group(1) if pointer_match else None
            index_match = re.fullmatch(r"\$\.requirements\[(\d+)\]", str(pointer or ""))
            fact = relation_by_index.get(int(index_match.group(1))) if index_match else None
            records.append(_relation_record(
                code="informational_requirement_forbidden", raw_error=text,
                response=response, requirements=requirements, fact=fact,
                json_pointer=pointer,
                matching_requirement_indexes=(
                    relation_facts.get(clause_match.group(1), [])
                    if clause_match else None
                ),
            ))
            continue
        if "non_requirement_classification_relation" in lowered:
            pointer = pointer_match.group(1) if pointer_match else None
            index_match = re.fullmatch(r"\$\.requirements\[(\d+)\]", str(pointer or ""))
            fact = relation_by_index.get(int(index_match.group(1))) if index_match else None
            records.append(_relation_record(
                code="non_requirement_classification_relation", raw_error=text,
                response=response, requirements=requirements, fact=fact,
                json_pointer=pointer,
                matching_requirement_indexes=(
                    sorted({
                        matching_index
                        for clause_id in (fact.get("clause_ids", []) if fact else [])
                        for matching_index in relation_facts.get(str(clause_id), [])
                    })
                    if fact else None
                ),
            ))
            continue
        if (
            ("must match at least one schema in anyof" in lowered
             or "is shorter than 1 characters" in lowered)
            and empty_cover_institution(pointer_match.group(1) if pointer_match else None)
        ):
            code = "cover_institution_placeholder"
        elif "normative_basis" in lowered:
            code = "normative_basis_invalid"
        elif "requirement_index_not_backed_by_clause" in lowered:
            code = "requirement_relation_mismatch"
        elif "executable_review_requires_derived_requirement" in lowered:
            code = "missing_derived_requirement"
        elif (
            "executable_review_requires_non_empty_inventory" in lowered
            or "review_requires_non_empty_source_inventory" in lowered
        ):
            code = "executable_review_obligations_missing"
        elif "executable_review_requires_all_obligations_covered" in lowered:
            code = "executable_review_obligations_uncovered"
        elif "mixed_execution_classification_relation" in lowered:
            code = "mixed_execution_classification_relation"
        elif "missing_clause_review" in lowered:
            code = "missing_clause_review"
        elif "unknown_clause_relation" in lowered:
            code = "unknown_clause_relation"
        elif "non_requirement_classification_relation" in lowered:
            code = "non_requirement_classification_relation"
        elif "unused_executable_requirement" in lowered:
            code = "unused_executable_requirement"
        elif "requirements_not_referenced_by_clause_review" in lowered:
            code = "requirement_relation_mismatch"
        elif re.fullmatch(r"\$\.requirements\[\d+\]\.evidence_ids: duplicate", lowered):
            code = "duplicate_evidence_ids"
        elif "not_backed_by_clause:" in lowered:
            code = "evidence_relation_mismatch"
        elif "partial_clause_coverage" in lowered:
            code = "partial_clause_coverage"
        elif "must_include_semantic_payload" in lowered:
            code = "empty_requirement_properties"
        elif "must equal a complete cited source-evidence text" in lowered:
            code = "fixed_text_evidence_mismatch"
        elif (
            ".applicability.conditions" in lowered
            and ".fact" in lowered
            and "does not match" in lowered
        ):
            code = "applicability_fact_namespace"
        elif (
            ".non_public_administration.fields" in lowered
            and (
                "requires at least 1 items" in lowered
                or "minitems" in lowered
            )
        ):
            code = "non_public_administration_fields_missing"
        elif "non_public_administration" in lowered:
            code = "cover_binding_violation"
        elif "input_prerequisites" in lowered and (
            "pattern" in lowered
            or "namespace" in lowered
            or "runtime_context" in lowered
            or "does not match" in lowered
        ):
            code = "input_prerequisite_namespace"
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
        append_generic(
            code=code, text=text,
            pointer=pointer,
            clause_id=derived_clause_id,
            matching=(
                relation_facts.get(derived_clause_id, [])
                if derived_clause_id else None
            ),
        )
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
    text = re.sub(r"\s+", "", exact_clause_source_text(clause))
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
    abstract_rule: dict[str, Any] = {}
    for index in indexes:
        if 0 <= index < len(requirements) and requirements[index].get("role") == "content_constraints":
            nested = (requirements[index].get("properties") or {}).get("abstract_zh")
            if isinstance(nested, dict):
                abstract_rule.update(nested)
    soft_length_qualifier = bool(
        re.search(r"(?:一般|通常|generally|usually).{0,40}300.{0,40}1000", text, re.I)
        or re.search(r"300.{0,40}1000.{0,80}(?:特殊需要|特殊情况|may be slightly extended|if special)", text, re.I)
    )
    third_person_soft = bool(re.search(r"(?:一般|通常).{0,12}第三人称|(?:usually|generally).{0,30}thirdperson", text, re.I))
    if re.search(r"300(?:字|字符|words?).{0,40}1000|300.{0,40}1000(?:字|字符|words?)", text, re.I):
        if soft_length_qualifier:
            guidance = abstract_rule.get("length_guidance")
            if not isinstance(guidance, dict) or guidance.get("strength") != "general_guidance":
                gaps.append("abstract_zh.length_guidance")
        else:
            if "abstract_zh.min_chars" not in properties and "abstract_zh.min_words" not in properties:
                gaps.append("abstract_zh.min_length")
            if "abstract_zh.max_chars" not in properties and "abstract_zh.max_words" not in properties:
                gaps.append("abstract_zh.max_length")
    if "第三人称" in text:
        if third_person_soft:
            if abstract_rule.get("third_person_guidance") != "general_guidance":
                gaps.append("abstract_zh.third_person_guidance")
        elif abstract_rule.get("require_third_person") is not True:
            gaps.append("abstract_zh.require_third_person")
    # Do not treat every occurrence of ``方法`` as a required abstract
    # section.  Template prose often says that a thesis presents a "新方法"
    # (new method) while the independent section obligation is stated later
    # in a phrase such as ``其内容包括：目的意义、研究方法、研究成果和结论``.
    # The former is a substantive/novelty description, not a section list.
    section_list_signal = (
        bool(re.search(r"(?:其)?内容包括", text))
        or bool(re.search(
            r"(?:包含|包括).{0,80}目的.{0,80}方法.{0,80}成果.{0,80}结论",
            text,
        ))
    )
    if section_list_signal:
        required_sections = abstract_rule.get("required_sections")
        section_text = set(required_sections) if isinstance(required_sections, list) else set()
        expected_sections = {
            "目的": "purpose", "方法": "methods", "成果": "results",
            "结论": "conclusions", "创新性": "innovation",
        }
        for marker, section in expected_sections.items():
            if marker in text and section not in section_text:
                gaps.append(f"abstract_zh.required_sections:{section}")
    if re.search(r"不加评论和解释|不得?加评论|不应?加评论|不含评论", text) and abstract_rule.get("prohibit_commentary") is not True:
        gaps.append("abstract_zh.prohibit_commentary")
    quality_checks = {
        "brief_statement_of_thesis_content": "论文内容的简要陈述",
        "independent_and_complete": "独立性和完整性",
        "reflects_central_idea": "准确反映论文的中心思想",
        "academic_language": "规范的学术用语",
        "logical_structure": "逻辑性强",
        "highlight_innovation": "创造性成果",
        "new_theory_method_technology": "新理论、新方法、新技术",
        "main_information_equivalent_to_thesis": "与论文等同的主要信息",
    }
    quality_guidance = set(abstract_rule.get("quality_guidance") or [])
    for key, marker in quality_checks.items():
        if marker in text and key not in quality_guidance:
            gaps.append(f"abstract_zh.quality_guidance:{key}")
    prohibited_objects = set(abstract_rule.get("prohibited_objects") or [])
    if "图表" in text or re.search(r"不可出现.{0,12}图", text):
        if not {"figures", "tables"}.issubset(prohibited_objects):
            gaps.append("abstract_zh.prohibited_objects:figures_or_tables")
    if "方程式" in text and "chemical_equations" not in prohibited_objects:
        gaps.append("abstract_zh.prohibited_objects:chemical_equations")
    if re.search(r"非公知|非公开.*术语|公知.*符号", text) and "nonpublic_symbols_and_terminology" not in prohibited_objects:
        gaps.append("abstract_zh.prohibited_objects:nonpublic_symbols_and_terminology")
    return gaps


def _keyword_obligation_gaps(
    clause: dict[str, Any], requirements: list[dict[str, Any]], indexes: list[int],
) -> list[str]:
    candidates = source_text_candidates(clause)
    exact_text = candidates[0] if candidates else ""
    keyword_subject = re.compile(r"关键词|关键字|\bkey\s*words?\b", re.I)
    obligation_text = exact_text
    if not keyword_subject.search(exact_text) and "source_span" in clause:
        # A split clause may inherit its target noun from the same, already
        # hash-validated evidence paragraph. Use that context only to identify
        # one unambiguous language target; never parse numeric bounds from the
        # wider paragraph, where unrelated limits may coexist.
        contexts = [value for value in candidates[1:] if keyword_subject.search(value)]
        languages = {
            "keywords_en" if re.search(r"\bkey\s*words?\b", value, re.I) else "keywords_zh"
            for value in contexts
        }
        if len(languages) == 1:
            subject = "Key Words" if languages == {"keywords_en"} else "关键词"
            obligation_text = f"{subject} {exact_text}"
    text = re.sub(r"\s+", "", obligation_text)
    if not keyword_subject.search(obligation_text):
        return []
    has_chinese_character_limit = bool(
        re.search(r"Chinesecharacters|汉字|中文字符", text, re.I)
    )
    properties: dict[str, Any] = {}
    for index in indexes:
        if 0 <= index < len(requirements) and requirements[index].get("role") == "content_constraints":
            properties.update(_flatten_property_paths(requirements[index].get("properties") or {}))
    key = "keywords_en" if re.search(
        r"英文关键词|\benglish\s+keywords?\b|\bkey\s*words?\b",
        obligation_text, re.I,
    ) else "keywords_zh"
    gaps: list[str] = []
    if has_chinese_character_limit:
        if f"{key}.max_item_chars" not in properties:
            gaps.append(f"{key}.max_item_chars")
        elif properties.get(f"{key}.item_length_metric") != "cjk_characters":
            gaps.append(f"{key}.item_length_metric:cjk_characters")
    guidance = compile_soft_keyword_count_guidance(obligation_text)
    guidance_candidates = [guidance] if guidance is not None else []
    unique_guidance = {
        (item["language_key"], item["min_count"], item["max_count"], item["strength"])
        for item in guidance_candidates
    }
    if len(unique_guidance) == 1:
        guidance = guidance_candidates[0]
        expected_guidance = {
            f"{key}.count_guidance.{field}": guidance[field]
            for field in ("min_count", "max_count", "strength")
        }
        if any(properties.get(path) != value for path, value in expected_guidance.items()):
            gaps.append(f"{key}.count_guidance")
    elif len(unique_guidance) > 1:
        gaps.append(f"{key}.count_guidance:ambiguous_source")

    hard_range = compile_explicit_keyword_count_range(obligation_text)
    hard_ranges = [hard_range] if hard_range is not None else []
    unique_hard_ranges = {
        (item["min_count"], item["max_count"]) for item in hard_ranges
    }
    if len(unique_hard_ranges) == 1:
        hard_range = hard_ranges[0]
        for field in ("min_count", "max_count"):
            if properties.get(f"{key}.{field}") != hard_range[field]:
                gaps.append(f"{key}.{field}")
    elif len(unique_hard_ranges) > 1:
        gaps.append(f"{key}.hard_count_range:conflicting_source")
    elif has_explicit_keyword_count_signal(obligation_text):
        gaps.append(f"{key}.hard_count_range:unresolved_source")
    return gaps


def validate_clause_source_spans(
    clauses: Any,
    evidence_context: Any,
    *,
    required: bool,
) -> list[str]:
    """Validate code-owned clause spans against the exact evidence packet.

    Contract 3.0 relies on exact source text for deterministic obligation
    checks. A span is therefore not trusted merely because it contains text
    and a plausible hash: its evidence id, offsets, source digest, slice, and
    normalized clause text must all agree with the current evidence packet.
    """
    if not isinstance(clauses, list):
        return ["clauses_must_be_array_for_source_binding"] if required else []
    evidence_map = evidence_context if isinstance(evidence_context, dict) else {}
    errors: list[str] = []
    for index, clause in enumerate(clauses):
        prefix = f"$.clauses[{index}].source_span"
        if not isinstance(clause, dict):
            if required:
                errors.append(f"{prefix}: clause_must_be_object")
            continue
        span = clause.get("source_span")
        if span is None:
            if required:
                errors.append(f"{prefix}: required_for_contract_3.0")
            continue
        if not isinstance(span, dict):
            errors.append(f"{prefix}: must_be_object")
            continue
        evidence_id = span.get("evidence_id")
        clause_evidence_ids = clause.get("evidence_ids")
        source_record = evidence_map.get(str(evidence_id)) if isinstance(evidence_id, str) else None
        source_text = source_record.get("text") if isinstance(source_record, dict) else None
        start = span.get("start_offset")
        end = span.get("end_offset")
        span_text = span.get("text")
        digest = span.get("source_sha256")
        if not isinstance(evidence_id, str) or not evidence_id:
            errors.append(f"{prefix}.evidence_id: must_be_nonempty_string")
            continue
        if (
            not isinstance(clause_evidence_ids, list)
            or evidence_id not in {str(value) for value in clause_evidence_ids}
        ):
            errors.append(f"{prefix}.evidence_id: not_referenced_by_clause")
        if not isinstance(source_text, str):
            errors.append(f"{prefix}.evidence_id: missing_current_source_text")
            continue
        if (
            isinstance(start, bool) or not isinstance(start, int) or start < 0
            or isinstance(end, bool) or not isinstance(end, int)
            or end <= start or end > len(source_text)
        ):
            errors.append(f"{prefix}: invalid_source_offsets")
            continue
        if not isinstance(span_text, str) or not span_text:
            errors.append(f"{prefix}.text: must_be_nonempty_string")
            continue
        if source_text[start:end] != span_text:
            errors.append(f"{prefix}.text: does_not_match_current_evidence_slice")
        if (
            not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or hashlib.sha256(source_text.encode("utf-8")).hexdigest() != digest
        ):
            errors.append(f"{prefix}.source_sha256: does_not_match_current_evidence")
        clause_text = clause.get("text")
        normalized_span = re.sub(r"\s+", " ", span_text).strip(" ，,、：:;；。和及")
        normalized_clause = (
            re.sub(r"\s+", " ", clause_text).strip(" ，,、：:;；。和及")
            if isinstance(clause_text, str) else None
        )
        if normalized_clause != normalized_span:
            errors.append(f"{prefix}.text: does_not_match_clause_text")
        full_context = clause.get("source_text_full")
        if isinstance(full_context, str) and re.sub(r"\s+", " ", source_text).strip() != full_context:
            errors.append(f"$.clauses[{index}].source_text_full: does_not_match_current_evidence")
        exact_context = clause.get("source_evidence_text")
        if isinstance(exact_context, str) and exact_context != source_text:
            errors.append(f"$.clauses[{index}].source_evidence_text: does_not_match_current_evidence")
    return errors


_TOP_LEVEL_NON_TEXT_ROLES = {
    "page", "table", "objects", "content_constraints", "conditional_constraints",
    "document_structure", "appendices", "equations", "cover", "declarations",
}


def _resolve_contract_schema(schema: Any, contract_root: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve the local role-schema reference used by the request contract."""
    seen: set[str] = set()
    while isinstance(schema, dict) and isinstance(schema.get("$ref"), str):
        reference = schema["$ref"]
        if not reference.startswith("#/$defs/") or reference in seen:
            return None
        seen.add(reference)
        schema = contract_root.get("$defs", {}).get(reference.removeprefix("#/$defs/"))
    return schema if isinstance(schema, dict) else None


def _literal_text_source_binding_error(
    item: dict[str, Any], index: int, clause_map: dict[str, dict[str, Any]],
    evidence_context: dict[str, Any],
) -> str | None:
    """Require literal text properties to be exact text at a cited source span.

    Segmentation may trim boundary punctuation from source_span. Permit only
    such punctuation immediately adjacent to the bound span; never normalize
    internal whitespace or replace punctuation inside the literal.
    """
    properties = item.get("properties")
    text = properties.get("text") if isinstance(properties, dict) else None
    if not isinstance(text, str) or not text.strip():
        return None  # The role schema reports malformed or missing values.
    clause_ids = item.get("clause_ids")
    evidence_ids = item.get("evidence_ids")
    if not isinstance(clause_ids, list) or not isinstance(evidence_ids, list):
        return f"$.requirements[{index}].properties.text: must_be_exact_substring_of_cited_source_span"
    cited_evidence = {str(value) for value in evidence_ids}
    punctuation = re.compile(r"^[\s，,、：:;；。！？!?…“”‘’（）()【】\[\]{}]*$")
    source_spans: list[tuple[str, str, int, int]] = []
    # Validate every linked clause before looking for a matching literal.  An
    # early match must not let a later clause's primary evidence disappear
    # merely because the model changed the order of clause_ids.
    for clause_id in clause_ids:
        clause = clause_map.get(str(clause_id))
        span = clause.get("source_span") if isinstance(clause, dict) else None
        if not isinstance(span, dict):
            continue
        evidence_id = span.get("evidence_id")
        if not isinstance(evidence_id, str) or not evidence_id:
            return (
                f"$.requirements[{index}].properties.text: "
                f"missing_primary_source_span_evidence_id:{clause_id}"
            )
        if evidence_id not in cited_evidence:
            return (
                f"$.requirements[{index}].evidence_ids: "
                f"must_cite_primary_source_span_evidence:{clause_id}"
            )
        source_spans.append((str(clause_id), evidence_id, span.get("start_offset"), span.get("end_offset")))

    for clause_id, evidence_id, start, end in source_spans:
        evidence = evidence_context.get(evidence_id)
        source_text = evidence.get("text") if isinstance(evidence, dict) else None
        if (
            not isinstance(source_text, str)
            or isinstance(start, bool) or not isinstance(start, int)
            or isinstance(end, bool) or not isinstance(end, int)
        ):
            continue
        search_from = 0
        matched = False
        while True:
            occurrence = source_text.find(text, search_from)
            if occurrence < 0:
                break
            occurrence_end = occurrence + len(text)
            overlaps_span = occurrence < end and occurrence_end > start
            left_fringe = source_text[occurrence:start] if occurrence < start else ""
            right_fringe = source_text[end:occurrence_end] if occurrence_end > end else ""
            bounded_fringe = len(left_fringe) <= 8 and len(right_fringe) <= 8
            if (
                overlaps_span and bounded_fringe
                and punctuation.fullmatch(left_fringe)
                and punctuation.fullmatch(right_fringe)
            ):
                matched = True
                break
            search_from = occurrence + 1
        if matched:
            # A literal may be supported by one of several linked clauses, but
            # the primary evidence for every linked clause has already been
            # checked above.
            return None
    return f"$.requirements[{index}].properties.text: must_be_exact_substring_of_cited_source_span"


def _table_obligation_gaps(
    clause: dict[str, Any], requirements: list[dict[str, Any]], indexes: list[int],
) -> list[str]:
    """Validate each compiled table fact against its role-specific payload.

    The model-authored obligation IDs are intentionally irrelevant here.  This
    deterministic inventory is checked against actual linked requirement
    properties so a prose ID mismatch cannot hide a missing property, and a
    model is not required to echo code-owned IDs.
    """
    source_text = exact_clause_source_text(clause)
    selected = [
        requirements[index] for index in indexes
        if isinstance(index, int) and 0 <= index < len(requirements)
        and isinstance(requirements[index], dict)
    ]

    def read_property(value: Any, path: str) -> Any:
        current = value
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                return None
            current = current[part]
        return current

    gaps: list[str] = []
    for fact in compile_known_source_obligations(source_text):
        candidates = [
            item for item in selected
            if item.get("role") in fact["roles"]
        ]
        payload_path = str(fact["property_path"])
        if payload_path.startswith("properties."):
            payload_path = payload_path[len("properties."):]
        values = [
            read_property(item.get("properties"), payload_path)
            for item in candidates
        ]
        explicit_values = [value for value in values if value is not None]
        expected = fact["expected_value"]
        matched_requirements = [
            item for item, value in zip(candidates, values)
            if value is not None and source_fact_value_matches(
                value, expected, fact.get("match_mode"),
            )
        ]
        if not matched_requirements or any(
            not source_fact_value_matches(
                value, expected, fact.get("match_mode"),
            ) for value in explicit_values
        ):
            gaps.append(str(fact["id"]))
            continue
        for checker_id in fact.get("required_checker_ids", []):
            if not any(
                isinstance(item.get("verification"), dict)
                and checker_id in (item["verification"].get("checker_ids") or [])
                for item in matched_requirements
            ):
                gaps.append(f"{fact['id']}:missing_checker:{checker_id}")
    return gaps


def _security_marking_qualifier_binding_errors(
    response: dict[str, Any], clauses: Any,
) -> list[str]:
    """Reject model-authored shorter-term flags without a linked source fact."""
    requirements = response.get("requirements")
    if not isinstance(requirements, list) or not isinstance(clauses, list):
        return []
    clauses_by_id = {
        item.get("id"): item for item in clauses
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    errors: list[str] = []
    for requirement_index, requirement in enumerate(requirements):
        if not isinstance(requirement, dict) or requirement.get("role") != "cover":
            continue
        properties = requirement.get("properties")
        administration = (
            properties.get("non_public_administration")
            if isinstance(properties, dict) else None
        )
        options = (
            administration.get("security_marking_options")
            if isinstance(administration, dict) else None
        )
        if not isinstance(options, list):
            continue
        flagged = [
            (option_index, option) for option_index, option in enumerate(options)
            if isinstance(option, dict) and "shorter_duration_allowed" in option
        ]
        if not flagged:
            continue
        clause_ids = requirement.get("clause_ids")
        linked_clauses = []
        if isinstance(clause_ids, list):
            linked_clauses = [
                clauses_by_id[clause_id]
                for clause_id in clause_ids
                if isinstance(clause_id, str) and clause_id in clauses_by_id
            ]
        source_facts = [
            fact for clause in linked_clauses
            for fact in compile_known_source_obligations(
                exact_clause_source_text(clause)
            )
            if fact.get("id") == SECURITY_MARKING_SHORTER_ALLOWANCE_OBLIGATION_ID
        ]
        for option_index, option in flagged:
            pointer = (
                f"$.requirements[{requirement_index}].properties.non_public_administration"
                f".security_marking_options[{option_index}].shorter_duration_allowed"
            )
            if option.get("shorter_duration_allowed") is not True:
                errors.append(f"{pointer}: must_be_source_compiled_true")
                continue
            source_backed = any(
                source_fact_value_matches(
                    options, [expected_option], "security_marking_option",
                )
                for fact in source_facts
                for expected_option in fact.get("expected_value", [])
            )
            if not source_backed:
                errors.append(f"{pointer}: must_be_bound_to_linked_source_clause")
    return errors


def _validate_obligations(
    review: dict[str, Any], review_index: int, *,
    require_semantic_decomposition: bool = False,
) -> list[str]:
    classification = str(review.get("classification"))
    requires_inventory = classification not in {"informational", "not_applicable"}
    obligations = review.get("obligations")
    if obligations is None:
        if require_semantic_decomposition and requires_inventory:
            return [
                f"$.clause_reviews[{review_index}].obligations: review_requires_non_empty_source_inventory"
            ]
        return []
    if not isinstance(obligations, list):
        return [f"$.clause_reviews[{review_index}].obligations: must_be_array"]
    errors: list[str] = []
    if require_semantic_decomposition and requires_inventory and not obligations:
        errors.append(
            f"$.clause_reviews[{review_index}].obligations: review_requires_non_empty_source_inventory"
        )
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
    # ``obligations[].id`` and text are model-owned semantic decomposition.
    # Code-owned, mechanically recognizable facts are checked independently
    # against linked requirement payloads by _table_obligation_gaps (and the
    # abstract/keyword guards); requiring the model to echo compiler IDs would
    # duplicate a machine fact in a fragile output field without adding proof.
    return errors


def _conflict_target_property_known(
    target: Any, role_schemas: Any, contract_root: dict[str, Any],
) -> bool:
    return _conflict_target_property_schema(
        target, role_schemas, contract_root,
    ) is not None


def _conflict_target_property_schema(
    target: Any, role_schemas: Any, contract_root: dict[str, Any],
) -> dict[str, Any] | None:
    if not isinstance(target, dict):
        return None
    role = target.get("role")
    property_name = target.get("property")
    if not isinstance(role, str) or not isinstance(property_name, str):
        return None
    if property_name.startswith("properties."):
        property_name = property_name.removeprefix("properties.")
    # Permit nested role properties and identity-selected array members such
    # as ``font.size_pt`` and ``fields[classification_number].label``.  The
    # selector is accepted only when the item schema explicitly enumerates
    # that identity; it is never treated as an arbitrary array index.
    tokens = [
        (name, selector or None)
        for name, selector in re.findall(
            r"([A-Za-z_][A-Za-z0-9_-]*)(?:\[([^\]]+)\])?", property_name,
        )
    ]
    if not tokens or ".".join(
        name + (f"[{selector}]" if selector else "") for name, selector in tokens
    ) != property_name:
        return None

    def dereference(value: Any) -> dict[str, Any] | None:
        seen: set[str] = set()
        while isinstance(value, dict) and isinstance(value.get("$ref"), str):
            reference = value["$ref"]
            if reference in seen or not reference.startswith("#/$defs/"):
                return None
            seen.add(reference)
            value = contract_root.get("$defs", {}).get(reference.removeprefix("#/$defs/"))
        return value if isinstance(value, dict) else None

    schema: Any = role_schemas.get(role) if isinstance(role_schemas, dict) else None
    for name, selector in tokens:
        schema = dereference(schema)
        properties = schema.get("properties") if isinstance(schema, dict) else None
        schema = properties.get(name) if isinstance(properties, dict) else None
        if not isinstance(schema, dict):
            return None
        if selector is not None:
            array_schema = dereference(schema)
            if not isinstance(array_schema, dict) or array_schema.get("type") != "array":
                return None
            item_schema = dereference(array_schema.get("items"))
            if not isinstance(item_schema, dict) or item_schema.get("type") != "object":
                return None
            identity_schema = item_schema.get("properties", {}).get("id")
            identity_schema = dereference(identity_schema)
            allowed_ids = identity_schema.get("enum") if isinstance(identity_schema, dict) else None
            if not isinstance(allowed_ids, list) or selector not in allowed_ids:
                return None
            schema = item_schema
    return dereference(schema)


def _conflict_candidate_value(candidate: dict[str, Any]) -> tuple[bool, Any, str | None]:
    """Decode the closed scalar or strict-JSON conflict candidate form."""
    if "value_json" not in candidate:
        if "value" not in candidate:
            return False, None, "missing_value"
        value = candidate["value"]
        if isinstance(value, (dict, list)):
            return False, None, "structured_value_requires_value_json"
        return True, value, None
    raw = candidate.get("value_json")
    if not isinstance(raw, str):
        return False, None, "value_json_must_be_string"
    try:
        value = strict_json_loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return False, None, f"value_json_invalid:{exc}"
    expected_type = candidate.get("value_type")
    actual_type = (
        "null" if value is None else "boolean" if isinstance(value, bool)
        else "object" if isinstance(value, dict) else "array" if isinstance(value, list)
        else "number" if isinstance(value, (int, float)) else "string" if isinstance(value, str)
        else "unsupported"
    )
    if expected_type != actual_type:
        return False, None, f"value_type_mismatch:expected_{expected_type}_got_{actual_type}"
    return True, value, None


def _reported_conflict_errors(
    response: dict[str, Any], *, clause_map: dict[str, dict[str, Any]],
    evidence_context: dict[str, Any], role_schemas: Any, contract_root: dict[str, Any],
) -> list[str]:
    """Validate that reported conflicts refer only to this chunk's bound facts."""
    conflicts = response.get("reported_conflicts")
    if not isinstance(conflicts, list):
        return ["$.reported_conflicts: must_be_array"]
    evidence_ids = {str(key) for key in evidence_context}
    errors: list[str] = []
    for index, conflict in enumerate(conflicts):
        if not isinstance(conflict, dict):
            errors.append(f"$.reported_conflicts[{index}]: must_be_object")
            continue
        clause_values = conflict.get("clause_ids")
        evidence_values = conflict.get("evidence_ids")
        if not isinstance(clause_values, list) or not clause_values:
            errors.append(f"$.reported_conflicts[{index}].clause_ids: must_be_non_empty_array")
            clause_values = []
        if not isinstance(evidence_values, list) or not evidence_values:
            errors.append(f"$.reported_conflicts[{index}].evidence_ids: must_be_non_empty_array")
            evidence_values = []
        clause_refs = [str(value) for value in clause_values]
        evidence_refs = [str(value) for value in evidence_values]
        if len(clause_refs) != len(set(clause_refs)):
            errors.append(f"$.reported_conflicts[{index}].clause_ids: duplicate")
        if len(evidence_refs) != len(set(evidence_refs)):
            errors.append(f"$.reported_conflicts[{index}].evidence_ids: duplicate")
        unknown_clauses = sorted(set(clause_refs) - set(clause_map))
        if unknown_clauses:
            errors.append(
                f"$.reported_conflicts[{index}].clause_ids: unknown:{','.join(unknown_clauses)}"
            )
        unknown_evidence = sorted(set(evidence_refs) - evidence_ids)
        if unknown_evidence:
            errors.append(
                f"$.reported_conflicts[{index}].evidence_ids: not_in_chunk:{','.join(unknown_evidence)}"
            )
        clause_evidence = {
            str(evidence_id)
            for clause_id in clause_refs
            for evidence_id in (clause_map.get(clause_id, {}).get("evidence_ids") or [])
        }
        unbound_evidence = sorted(set(evidence_refs) - clause_evidence)
        if unbound_evidence:
            errors.append(
                f"$.reported_conflicts[{index}].evidence_ids: not_backed_by_conflict_clause:"
                + ",".join(unbound_evidence)
            )
        target = conflict.get("target")
        if target is not None and not _conflict_target_property_known(
            target, role_schemas, contract_root,
        ):
            errors.append(
                f"$.reported_conflicts[{index}].target: unknown_registered_role_property"
            )
        candidates = conflict.get("candidates")
        if candidates is not None:
            if not isinstance(candidates, list):
                errors.append(f"$.reported_conflicts[{index}].candidates: must_be_array")
                continue
            for candidate_index, candidate in enumerate(candidates):
                if not isinstance(candidate, dict):
                    continue  # The closed schema reports the shape error.
                candidate_evidence = str(candidate.get("evidence_id"))
                if candidate_evidence not in evidence_refs:
                    errors.append(
                        f"$.reported_conflicts[{index}].candidates[{candidate_index}].evidence_id: "
                        "must_reference_conflict_evidence"
                    )
                decoded, candidate_value, candidate_error = _conflict_candidate_value(candidate)
                if not decoded:
                    errors.append(
                        f"$.reported_conflicts[{index}].candidates[{candidate_index}]:{candidate_error}"
                    )
                    continue
                if target is not None:
                    target_schema = _conflict_target_property_schema(
                        target, role_schemas, contract_root,
                    )
                    if target_schema is not None:
                        value_errors = validate_instance(
                            candidate_value, target_schema, contract_root,
                            f"$.reported_conflicts[{index}].candidates[{candidate_index}].value",
                        )
                        if value_errors:
                            errors.append(
                                f"$.reported_conflicts[{index}].candidates[{candidate_index}].value: "
                                "does_not_match_target_property_schema"
                            )
        conditions = conflict.get("conditions")
        if conditions is not None:
            if not isinstance(conditions, list):
                errors.append(f"$.reported_conflicts[{index}].conditions: must_be_array")
                continue
            for condition_index, condition in enumerate(conditions):
                if not isinstance(condition, dict):
                    continue  # The closed schema reports the shape error.
                condition_evidence = str(condition.get("evidence_id") or "")
                if condition_evidence not in evidence_refs:
                    errors.append(
                        f"$.reported_conflicts[{index}].conditions[{condition_index}].evidence_id: "
                        "must_reference_conflict_evidence"
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

    contract_version = response.get("contract_version")
    clauses_value = chunk.get("clauses")
    evidence_context = chunk.get("evidence_context")
    span_errors = validate_clause_source_spans(
        clauses_value,
        evidence_context,
        required=contract_version == HOST_REVIEW_CONTRACT_V3,
    )
    if span_errors:
        return span_errors
    clauses = clauses_value if isinstance(clauses_value, list) else []

    clause_map_for_binding = {
        item["id"]: item for item in clauses
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    existing_map = {
        item["id"]: item for item in chunk.get("rule_spec", {}).get("requirements", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    # Use exactly the merger's safe deterministic payload projection. This is
    # a validation view, not a rewrite of the raw model response. The merger
    # persists its before/after hashes; neither stage changes identity.
    response, _ = project_authoritative_existing_payloads(
        response, existing_map, clause_map_for_binding,
    )
    response, _ = materialize_known_source_verification(
        response, chunk.get("clauses"),
    )
    response, _ = materialize_complete_abstract_source_constraints(
        response, chunk.get("clauses"),
    )
    response, _ = materialize_soft_keyword_count_guidance(
        response, chunk.get("clauses"),
    )
    errors: list[str] = []
    errors.extend(_security_marking_qualifier_binding_errors(
        response, chunk.get("clauses"),
    ))
    if contract_version not in SUPPORTED_HOST_REVIEW_CONTRACTS:
        errors.append(f"contract_version_unsupported:{contract_version!r}")
    response_schema = chunk.get("response_schema")
    if not isinstance(response_schema, dict) or not response_schema:
        errors.append("response_schema_missing")
    else:
        errors.extend(schema_support_errors(response_schema))
        errors.extend(validate_instance(response, response_schema, response_schema))
        # Emit a field-level diagnostic for the discriminated role union.
        # Generic anyOf failures otherwise hide which role-specific payload
        # field the provider got wrong, making retry feedback needlessly vague.
        item_schema = (
            response_schema.get("properties", {}).get("requirements", {}).get("items")
            if isinstance(response_schema.get("properties"), dict) else None
        )
        branches = item_schema.get("anyOf") if isinstance(item_schema, dict) else None
        if isinstance(branches, list) and isinstance(response.get("requirements"), list):
            for index, item in enumerate(response["requirements"]):
                if not isinstance(item, dict):
                    continue
                role = item.get("role")
                branch = next((
                    candidate for candidate in branches
                    if isinstance(candidate, dict)
                    and isinstance(candidate.get("properties"), dict)
                    and candidate["properties"].get("role", {}).get("enum") == [role]
                ), None)
                if isinstance(branch, dict):
                    errors.extend(validate_instance(
                        item, branch, response_schema, f"$.requirements[{index}]",
                    ))

    contract = chunk.get("requirement_contract")
    if not isinstance(contract, dict):
        errors.append("requirement_contract_missing")
        contract = {}
    contract_root = {"$defs": contract.get("$defs", {})}
    role_schemas = contract.get("role_properties_schema", {})
    allowed_roles = set(contract.get("allowed_roles", []))

    if not isinstance(clauses, list):
        clauses = []
    clause_map = {
        str(item.get("id")): item
        for item in clauses
        if isinstance(item, dict) and item.get("id")
    }
    if not isinstance(evidence_context, dict):
        evidence_context = {}
    evidence_ids = {str(key) for key in evidence_context}
    errors.extend(_reported_conflict_errors(
        response,
        clause_map=clause_map,
        evidence_context=evidence_context,
        role_schemas=role_schemas,
        contract_root=contract_root,
    ))

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
        errors.extend(
            f"$.requirements[{index}].existing_requirement_id: {reason}"
            for reason in existing_reference_errors(item, existing_map, clause_map)
        )
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
                    evidence_context.get(str(evidence_id), {}).get("text")
                    for evidence_id in item.get("evidence_ids", [])
                    if isinstance(evidence_context.get(str(evidence_id)), dict)
                    and isinstance(evidence_context.get(str(evidence_id), {}).get("text"), str)
                }
                for item_index, declaration in enumerate(properties["items"]):
                    if not isinstance(declaration, dict):
                        continue
                    fixed_text_atoms: list[tuple[str, str]] = []
                    for field in ("heading", "body"):
                        value = declaration.get(field)
                        if isinstance(value, str) and value.strip():
                            fixed_text_atoms.append((field, value))
                    body_parts = declaration.get("body_parts")
                    if isinstance(body_parts, list):
                        fixed_text_atoms.extend(
                            (f"body_parts[{body_index}]", body)
                            for body_index, body in enumerate(body_parts)
                            if isinstance(body, str) and body.strip()
                        )
                    for field_path, value in fixed_text_atoms:
                        if value not in source_texts:
                            errors.append(
                                f"$.requirements[{index}].properties.items[{item_index}].{field_path}: "
                                "must exactly equal a complete cited source-evidence text; do not normalize, paraphrase, or shorten fixed declaration prose"
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
        elif isinstance(clause_ids, list) and len(clause_ids) != len(clause_set):
            errors.append(f"$.requirements[{index}].clause_ids: duplicate")
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
        raw_evidence_ids = item.get("evidence_ids")
        if isinstance(raw_evidence_ids, list) and len(raw_evidence_ids) != len(cited_evidence):
            errors.append(f"$.requirements[{index}].evidence_ids: duplicate")
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
        if contract_version == HOST_REVIEW_CONTRACT_V3 and isinstance(role, str):
            raw_role_schema = role_schemas.get(role) if isinstance(role_schemas, dict) else None
            resolved_role_schema = _resolve_contract_schema(raw_role_schema, contract_root)
            role_properties = (
                resolved_role_schema.get("properties")
                if isinstance(resolved_role_schema, dict) else None
            )
            text_schema = role_properties.get("text") if isinstance(role_properties, dict) else None
            if (
                role not in _TOP_LEVEL_NON_TEXT_ROLES
                and isinstance(text_schema, dict)
                and text_schema.get("type") == "string"
                and isinstance(item.get("properties"), dict)
                and "text" in item["properties"]
            ):
                source_binding_error = _literal_text_source_binding_error(
                    item, index, clause_map, evidence_context,
                )
                if source_binding_error:
                    errors.append(source_binding_error)

    matching_requirement_indexes: dict[str, list[int]] = {
        clause_id: [
            index
            for index, clause_ids in enumerate(requirement_clause_sets)
            if clause_id in clause_ids
        ]
        for clause_id in clause_map
    }

    relation_analysis = (
        analyze_requirement_relations(response, clauses)
        if contract_version == HOST_REVIEW_CONTRACT_V3 else []
    )
    relation_by_index = {
        int(item["requirement_index"]): item
        for item in relation_analysis
        if isinstance(item, dict) and isinstance(item.get("requirement_index"), int)
    }
    for fact in relation_analysis:
        category = str(fact.get("category") or "")
        code = _RELATION_CATEGORY_CODES.get(category)
        if code is None or category == "execution":
            continue
        requirement_index = int(fact["requirement_index"])
        details = ",".join(str(value) for value in fact.get("clause_ids", [])) or "none"
        errors.append(
            f"$.requirements[{requirement_index}]:{code}:clause_ids={details}"
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
        clause = clause_map.get(clause_id)
        indexes = review.get("requirement_indexes")
        errors.extend(_validate_obligations(
            review, review_index,
            require_semantic_decomposition=(contract_version == HOST_REVIEW_CONTRACT_V3),
        ))
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
            valid_indexes = [
                index for index in matching_requirement_indexes.get(clause_id, [])
                if relation_by_index.get(index, {}).get("category") == "execution"
            ]
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
    if contract_version == HOST_REVIEW_CONTRACT_V3:
        # Relation-specific errors above are authoritative for every known
        # edge category.  Keep the legacy aggregate only for an unclassified
        # relation (for example a requirement with no clause_ids), or for an
        # executable relation that somehow remained unreferenced.  This keeps
        # one machine-readable record per informational-only requirement
        # instead of re-emitting the same comma-separated list.
        unused_indexes = [
            index for index in unused_indexes
            if relation_by_index.get(index, {}).get("category") in {
                None, "execution", "missing_clause_relation",
            }
        ]
    if unused_indexes:
        errors.append(
            "requirements_not_referenced_by_clause_review:" + ",".join(map(str, unused_indexes))
        )
    return errors
