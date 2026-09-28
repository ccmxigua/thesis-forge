"""Deterministic binding and composition for source-backed literal fragments.

The model may identify an ordered list of already extracted clause IDs.  This
module validates each exact source occurrence and derives any boundary text;
it never treats lexical ID order or mere proximity as proof of a join.
"""
from __future__ import annotations

import copy
import hashlib
import re
from typing import Any


_BOUNDARY_ONLY = re.compile(r"^[\s，,、：:;；。！？!?…“”‘’（）()【】\[\]{}]*$")
_NORMALIZE_CLAUSE = re.compile(r"\s+")
_CLAUSE_EDGE_PUNCTUATION = " \t\r\n，,、：:;；。！？!?…“”‘’（）()【】[]{}"

# These roles carry text through typed, role-native properties rather than a
# top-level ``properties.text`` field. Keep this list shared with the contract
# validator so deterministic source binding cannot inject a schema-invalid
# property into one of them.
TOP_LEVEL_NON_TEXT_ROLES = frozenset({
    "page", "table", "objects", "content_constraints", "conditional_constraints",
    "document_structure", "appendices", "equations", "cover", "declarations",
})


def normalize_clause_literal(value: str) -> str:
    """Normalize formatting whitespace and edge punctuation, not lexical text."""
    return _NORMALIZE_CLAUSE.sub(" ", value).strip(_CLAUSE_EDGE_PUNCTUATION)


class SourceFragmentBindingError(ValueError):
    """A selected source fragment cannot be bound to current source evidence."""

    code = "source_fragment_binding_invalid"


def evidence_map_for_clauses(
    clauses: list[dict[str, Any]], evidence_context: Any = None,
) -> dict[str, dict[str, Any]]:
    """Return evidence keyed by ID, deriving a copy only from bound clauses."""
    output: dict[str, dict[str, Any]] = {}
    if isinstance(evidence_context, dict):
        output.update({
            str(key): copy.deepcopy(value)
            for key, value in evidence_context.items()
            if isinstance(key, str) and isinstance(value, dict)
        })
    for clause in clauses:
        if not isinstance(clause, dict):
            continue
        span = clause.get("source_span")
        evidence_id = span.get("evidence_id") if isinstance(span, dict) else None
        source = clause.get("source_evidence_text")
        if not isinstance(evidence_id, str) or not isinstance(source, str):
            continue
        location = span.get("location") if isinstance(span.get("location"), dict) else (
            clause.get("location") if isinstance(clause.get("location"), dict) else {}
        )
        record = {
            "id": evidence_id,
            "text": source,
            "kind": clause.get("source_kind"),
            "location": copy.deepcopy(location),
        }
        previous = output.get(evidence_id)
        if previous is not None and previous.get("text") != source:
            raise SourceFragmentBindingError(
                f"conflicting_current_source_text:{evidence_id}"
            )
        output.setdefault(evidence_id, record)
    return output


def _location_for(clause: dict[str, Any], span: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    span_location = span.get("location")
    evidence_location = evidence.get("location")
    clause_location = clause.get("location")
    location = next((candidate for candidate in (span_location, evidence_location, clause_location)
                     if isinstance(candidate, dict)), {})
    if isinstance(span_location, dict) and isinstance(evidence_location, dict):
        if span_location != evidence_location:
            raise SourceFragmentBindingError(
                f"source_location_mismatch:{span.get('evidence_id')}"
            )
    if isinstance(clause_location, dict) and isinstance(evidence_location, dict):
        # Some compact clause packets omit a few non-authoritative location
        # fields; compare only keys explicitly present on the clause.
        if any(evidence_location.get(key) != value for key, value in clause_location.items()):
            raise SourceFragmentBindingError(
                f"clause_location_mismatch:{span.get('evidence_id')}"
            )
    return copy.deepcopy(location)


def _sort_key(fragment: dict[str, Any]) -> tuple[int, int, int]:
    location = fragment["location"]
    order = location.get("order")
    if isinstance(order, bool) or not isinstance(order, int):
        # Multiple clauses in one source paragraph can still be ordered by
        # exact offsets. Cross-evidence composition requires a reliable order.
        order = 0
    return (order, fragment["start_offset"], fragment["end_offset"])


def _structurally_adjacent(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    left = previous["location"]
    right = current["location"]
    left_kind = previous.get("kind")
    right_kind = current.get("kind")
    if left_kind != right_kind or not isinstance(left, dict) or not isinstance(right, dict):
        return False
    if left.get("part") != right.get("part"):
        return False
    if left_kind == "paragraph" and left.get("part") == "document":
        return (
            isinstance(left.get("child_index"), int)
            and not isinstance(left.get("child_index"), bool)
            and right.get("child_index") == left["child_index"] + 1
        )
    if left_kind == "table_cell":
        return (
            all(left.get(key) == right.get(key) for key in ("table_child_index", "row", "column"))
            and isinstance(left.get("paragraph"), int)
            and not isinstance(left.get("paragraph"), bool)
            and right.get("paragraph") == left["paragraph"] + 1
        )
    if left_kind == "header_footer":
        return (
            isinstance(left.get("paragraph"), int)
            and not isinstance(left.get("paragraph"), bool)
            and right.get("paragraph") == left["paragraph"] + 1
        )
    if left_kind == "textbox":
        return (
            all(left.get(key) == right.get(key) for key in (
                "textbox", "alternate_content_index", "alternate_branch",
            ))
            and isinstance(left.get("paragraph"), int)
            and not isinstance(left.get("paragraph"), bool)
            and right.get("paragraph") == left["paragraph"] + 1
        )
    return False


def _declaration_fragment_payload_error(
    requirement: dict[str, Any], binding: dict[str, Any],
    evidence_map: dict[str, dict[str, Any]],
) -> str | None:
    """Bind declaration fragments to their existing role-native text atoms.

    Declarations store fixed wording under ``items[].heading``, ``body``, or
    ``body_parts``. They do not accept a top-level ``properties.text`` field.
    Require every selected evidence span to cover its complete substantive
    source text, and require the selected evidence texts to appear in order in
    role-native fields. Other declaration atoms remain independently checked
    by the full declaration contract and are immutable across this retry.
    """
    properties = requirement.get("properties")
    declaration_items = properties.get("items") if isinstance(properties, dict) else None
    if not isinstance(declaration_items, list):
        return "declaration_role_native_items_missing"

    atoms_by_item: list[list[str]] = []
    for declaration in declaration_items:
        if not isinstance(declaration, dict):
            continue
        atoms: list[str] = []
        for field in ("heading", "body"):
            value = declaration.get(field)
            if isinstance(value, str) and value:
                atoms.append(value)
        body_parts = declaration.get("body_parts")
        if isinstance(body_parts, list):
            atoms.extend(value for value in body_parts if isinstance(value, str) and value)
        if atoms:
            atoms_by_item.append(atoms)

    selected_evidence_ids: list[str] = []
    spans_by_evidence: dict[str, list[tuple[int, int]]] = {}
    for fragment in binding.get("source_fragments", []):
        evidence_id = fragment.get("evidence_id") if isinstance(fragment, dict) else None
        if not isinstance(evidence_id, str) or not evidence_id:
            return "declaration_source_fragment_evidence_missing"
        start = fragment.get("start_offset")
        end = fragment.get("end_offset")
        if (
            isinstance(start, bool) or not isinstance(start, int)
            or isinstance(end, bool) or not isinstance(end, int)
            or start < 0 or end <= start
        ):
            return f"declaration_source_fragment_span_invalid:{evidence_id}"
        spans_by_evidence.setdefault(evidence_id, []).append((start, end))
        if not selected_evidence_ids or selected_evidence_ids[-1] != evidence_id:
            selected_evidence_ids.append(evidence_id)

    expected_texts: list[str] = []
    for evidence_id in selected_evidence_ids:
        evidence = evidence_map.get(evidence_id)
        source_text = evidence.get("text") if isinstance(evidence, dict) else None
        if not isinstance(source_text, str) or not source_text:
            return f"declaration_source_fragment_text_missing:{evidence_id}"
        cursor = 0
        for start, end in spans_by_evidence.get(evidence_id, []):
            if end > len(source_text) or start < cursor:
                return f"declaration_source_fragment_span_invalid:{evidence_id}"
            if not _BOUNDARY_ONLY.fullmatch(source_text[cursor:start]):
                return f"declaration_source_fragment_selector_omits_evidence_text:{evidence_id}"
            cursor = end
        if not _BOUNDARY_ONLY.fullmatch(source_text[cursor:]):
            return f"declaration_source_fragment_selector_omits_evidence_text:{evidence_id}"
        expected_texts.append(source_text)

    if not expected_texts:
        return "declaration_source_fragment_evidence_missing"

    ordered_atoms = [atom for atoms in atoms_by_item for atom in atoms]
    for start in range(len(ordered_atoms) - len(expected_texts) + 1):
        if ordered_atoms[start:start + len(expected_texts)] == expected_texts:
            return None
    return "declaration_source_fragments_not_represented_in_role_native_text"


def compose_source_fragments(
    source_fragment_clause_ids: Any,
    clause_map: dict[str, dict[str, Any]],
    evidence_context: Any = None,
    *,
    requirement_clause_ids: Any = None,
    requirement_evidence_ids: Any = None,
) -> dict[str, Any]:
    """Validate an ordered source-fragment selection and derive its exact text.

    Same-evidence spans may be joined only across source gaps consisting
    exclusively of whitespace/boundary punctuation. Different evidence items
    may be joined only when their code-owned document locations prove direct
    structural adjacency; the separator is then an explicit paragraph break.
    """
    if (
        not isinstance(source_fragment_clause_ids, list)
        or not source_fragment_clause_ids
        or any(not isinstance(value, str) or not value for value in source_fragment_clause_ids)
    ):
        raise SourceFragmentBindingError("clause_ids_must_be_nonempty_string_array")
    if len(set(source_fragment_clause_ids)) != len(source_fragment_clause_ids):
        raise SourceFragmentBindingError("duplicate_clause_id")
    if isinstance(requirement_clause_ids, list) and not set(source_fragment_clause_ids) <= set(requirement_clause_ids):
        raise SourceFragmentBindingError("fragment_clause_not_cited_by_requirement")
    evidence_map = evidence_map_for_clauses(list(clause_map.values()), evidence_context)
    cited_evidence = set(requirement_evidence_ids) if isinstance(requirement_evidence_ids, list) else None
    fragments: list[dict[str, Any]] = []
    for clause_id in source_fragment_clause_ids:
        clause = clause_map.get(clause_id)
        if not isinstance(clause, dict):
            raise SourceFragmentBindingError(f"unknown_clause:{clause_id}")
        span = clause.get("source_span")
        if not isinstance(span, dict):
            raise SourceFragmentBindingError(f"missing_source_span:{clause_id}")
        evidence_id = span.get("evidence_id")
        if not isinstance(evidence_id, str) or not evidence_id:
            raise SourceFragmentBindingError(f"missing_evidence_id:{clause_id}")
        clause_evidence_ids = clause.get("evidence_ids")
        if not isinstance(clause_evidence_ids, list) or evidence_id not in clause_evidence_ids:
            raise SourceFragmentBindingError(f"primary_evidence_not_in_clause:{clause_id}")
        if cited_evidence is not None and evidence_id not in cited_evidence:
            raise SourceFragmentBindingError(f"primary_evidence_not_cited:{clause_id}")
        evidence = evidence_map.get(evidence_id)
        source = evidence.get("text") if isinstance(evidence, dict) else None
        start, end = span.get("start_offset"), span.get("end_offset")
        span_text, source_digest = span.get("text"), span.get("source_sha256")
        if not isinstance(source, str):
            raise SourceFragmentBindingError(f"missing_current_source_text:{evidence_id}")
        if (
            isinstance(start, bool) or not isinstance(start, int) or start < 0
            or isinstance(end, bool) or not isinstance(end, int)
            or end <= start or end > len(source)
        ):
            raise SourceFragmentBindingError(f"invalid_offsets:{clause_id}")
        if not isinstance(span_text, str) or not span_text or source[start:end] != span_text:
            raise SourceFragmentBindingError(f"span_text_mismatch:{clause_id}")
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        if source_digest != digest:
            raise SourceFragmentBindingError(f"source_hash_mismatch:{clause_id}")
        clause_text = clause.get("text")
        if (
            not isinstance(clause_text, str)
            or normalize_clause_literal(span_text) != normalize_clause_literal(clause_text)
        ):
            raise SourceFragmentBindingError(f"clause_text_mismatch:{clause_id}")
        full_source = clause.get("source_evidence_text")
        if isinstance(full_source, str) and full_source != source:
            raise SourceFragmentBindingError(f"clause_current_evidence_mismatch:{clause_id}")
        location = _location_for(clause, span, evidence)
        fragments.append({
            "clause_id": clause_id,
            "evidence_id": evidence_id,
            "start_offset": start,
            "end_offset": end,
            "source_sha256": digest,
            "text": span_text,
            "source_kind": evidence.get("kind") if isinstance(evidence, dict) else None,
            "separator_before": "",
            "location": location,
            "kind": evidence.get("kind") if isinstance(evidence, dict) else None,
        })

    if len(fragments) > 1:
        if fragments != sorted(fragments, key=_sort_key):
            raise SourceFragmentBindingError("fragments_not_in_source_order")
    for index in range(1, len(fragments)):
        previous, current = fragments[index - 1], fragments[index]
        if previous["evidence_id"] == current["evidence_id"]:
            if current["start_offset"] < previous["end_offset"]:
                raise SourceFragmentBindingError("overlapping_source_spans")
            gap = evidence_map[previous["evidence_id"]]["text"][
                previous["end_offset"]:current["start_offset"]
            ]
            if not _BOUNDARY_ONLY.fullmatch(gap):
                raise SourceFragmentBindingError("unselected_source_text_between_fragments")
            current["separator_before"] = gap
        else:
            previous_order = previous["location"].get("order")
            current_order = current["location"].get("order")
            if (
                isinstance(previous_order, bool) or not isinstance(previous_order, int)
                or isinstance(current_order, bool) or not isinstance(current_order, int)
                or current_order != previous_order + 1
                or not _structurally_adjacent(previous, current)
            ):
                raise SourceFragmentBindingError("cross_evidence_boundary_unproven")
            current["separator_before"] = "\n"

    materialized = "".join(item["separator_before"] + item["text"] for item in fragments)
    public_fragments = [
        {key: copy.deepcopy(value) for key, value in item.items() if key != "kind"}
        for item in fragments
    ]
    return {"text": materialized, "source_fragments": public_fragments}


def materialize_source_fragment_literals(
    response: Any,
    clauses: list[dict[str, Any]],
    evidence_context: Any = None,
) -> tuple[Any, list[dict[str, Any]], list[str]]:
    """Materialize selected source fragments into literal fields without guessing."""
    output = copy.deepcopy(response)
    if not isinstance(output, dict) or not isinstance(output.get("requirements"), list):
        return output, [], []
    clause_map = {
        str(item.get("id")): item for item in clauses
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    audits: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        evidence_map = evidence_map_for_clauses(clauses, evidence_context)
    except SourceFragmentBindingError as exc:
        return output, [], [f"source_fragment_evidence_map:{exc}"]
    for index, item in enumerate(output["requirements"]):
        if not isinstance(item, dict) or "source_fragment_clause_ids" not in item:
            continue
        pointer = f"$.requirements[{index}]"
        try:
            binding = compose_source_fragments(
                item.get("source_fragment_clause_ids"), clause_map, evidence_context,
                requirement_clause_ids=item.get("clause_ids"),
                requirement_evidence_ids=item.get("evidence_ids"),
            )
        except SourceFragmentBindingError as exc:
            errors.append(f"{pointer}.source_fragment_clause_ids:{exc}")
            continue
        properties = item.get("properties")
        if not isinstance(properties, dict):
            errors.append(f"{pointer}.properties:must_be_object_for_source_fragments")
            continue
        role = item.get("role")
        if role == "declarations":
            declaration_error = _declaration_fragment_payload_error(
                item, binding, evidence_map,
            )
            if declaration_error:
                errors.append(
                    f"{pointer}.source_fragment_clause_ids:{declaration_error}"
                )
                continue
            audits.append({
                "requirement_index": index,
                "clause_ids": copy.deepcopy(item["source_fragment_clause_ids"]),
                "source_fragments": copy.deepcopy(binding["source_fragments"]),
                "materialized_text_sha256": hashlib.sha256(
                    binding["text"].encode("utf-8")
                ).hexdigest(),
                "action": "verified_against_role_native_declaration_text",
            })
            continue
        if role in TOP_LEVEL_NON_TEXT_ROLES:
            errors.append(
                f"{pointer}.source_fragment_clause_ids:role_has_no_top_level_text_target:{role}"
            )
            continue
        supplied_text = properties.get("text")
        if supplied_text is not None and supplied_text != binding["text"]:
            errors.append(f"{pointer}.properties.text:source_fragment_literal_conflict")
            continue
        if supplied_text is None:
            properties["text"] = binding["text"]
            action = "materialized_from_current_bound_source_fragments"
        else:
            action = "verified_against_current_bound_source_fragments"
        audits.append({
            "requirement_index": index,
            "clause_ids": copy.deepcopy(item["source_fragment_clause_ids"]),
            "source_fragments": copy.deepcopy(binding["source_fragments"]),
            "materialized_text_sha256": hashlib.sha256(binding["text"].encode("utf-8")).hexdigest(),
            "action": action,
        })
    return output, audits, errors
