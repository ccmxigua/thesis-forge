"""Fail-closed binding for explicit source ranges used by format requirements.

The module does not interpret prose such as “the above rule”.  A reviewer must
select exact clause IDs; range forms are accepted only when their endpoints and
all clauses between them are present in the same extracted paragraph or table
row.  This keeps semantic ambiguity and parser limitations visible.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any


_ANAPHORA = re.compile(r"(?:上述|前述|上文|前文|前项|前一项|本条|该条|前款|上述规定|前述要求)")
_ORDINAL = re.compile(r"第[一二三四五六七八九十百零〇两\d]+(?:条|款|项)")
_FORWARD_REFERENCE = re.compile(r"(?:如下|下表|下图|下文|后文|见下|参见后)")


def diagnose_source_scope_reference(
    clause: dict[str, Any], clauses: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Describe an unresolved prose reference without selecting its target."""
    text = str(clause.get("text") or "")
    ordinal_terms = list(dict.fromkeys(_ORDINAL.findall(text)))
    anaphoric = bool(_ANAPHORA.search(text))
    forward = bool(_FORWARD_REFERENCE.search(text))
    if not (ordinal_terms or anaphoric or forward):
        return None

    current_index = next((i for i, item in enumerate(clauses) if item is clause), None)
    candidates: list[dict[str, Any]] = []
    if ordinal_terms:
        for item in clauses:
            if item is clause:
                continue
            item_text = str(item.get("text") or "")
            matched = [term for term in ordinal_terms if term in item_text]
            if matched:
                candidates.append({
                    "clause_id": item.get("id"),
                    "text": item_text,
                    "evidence_ids": list(item.get("evidence_ids") or []),
                    "location": dict(item.get("location") or {}),
                    "matched_terms": matched,
                })
    elif anaphoric and current_index is not None:
        # List nearby preceding source clauses as candidates for a human.  The
        # list is diagnostic only; proximity never resolves an anaphor.
        for item in clauses[max(0, current_index - 6):current_index]:
            candidates.append({
                "clause_id": item.get("id"),
                "text": str(item.get("text") or ""),
                "evidence_ids": list(item.get("evidence_ids") or []),
                "location": dict(item.get("location") or {}),
            })
    elif forward and current_index is not None:
        for item in clauses[current_index + 1:current_index + 7]:
            location = item.get("location") if isinstance(item.get("location"), dict) else {}
            if item.get("source_kind") == "table_cell" or location.get("table_child_index") is not None:
                candidates.append({
                    "clause_id": item.get("id"),
                    "text": str(item.get("text") or ""),
                    "evidence_ids": list(item.get("evidence_ids") or []),
                    "location": dict(location),
                })

    if len(candidates) > 1:
        classification = "semantic_ambiguity"
        reason = "multiple_source_anchors_match_reference"
    elif forward and not candidates:
        classification = "parser_capability_gap"
        reason = "forward_target_not_structurally_observable"
    elif anaphoric:
        classification = "human_confirmation_required"
        reason = "anaphoric_reference_requires_explicit_anchor"
    elif ordinal_terms and len(candidates) == 1:
        classification = "human_confirmation_required"
        reason = "ordinal_reference_requires_explicit_anchor"
    else:
        classification = "parser_capability_gap"
        reason = "source_reference_not_resolved_by_supported_scope_grammar"
    return {
        "status": "pending_input",
        "classification": classification,
        "reason": reason,
        "source_clause_id": clause.get("id"),
        "source_text": text,
        "reference_terms": ordinal_terms + (["anaphora"] if anaphoric else [])
        + (["forward_reference"] if forward else []),
        "candidate_clause_ids": [str(item["clause_id"]) for item in candidates
                                 if isinstance(item.get("clause_id"), str)],
        "candidates": candidates,
        "resolution_policy": "human_selects_exact_clause_ids_before_execution",
    }


def _clause_span(clause: dict[str, Any]) -> tuple[str, int, int, str, dict[str, Any]] | None:
    span = clause.get("source_span")
    if not isinstance(span, dict):
        return None
    evidence_id = span.get("evidence_id")
    start, end = span.get("start_offset"), span.get("end_offset")
    source_hash, source_text = span.get("source_sha256"), span.get("text")
    location = span.get("location") or clause.get("location")
    if (not isinstance(evidence_id, str) or type(start) is not int or type(end) is not int
            or start < 0 or end <= start or not isinstance(source_hash, str)
            or not isinstance(source_text, str) or not isinstance(location, dict)):
        return None
    full_text = clause.get("source_evidence_text")
    if not isinstance(full_text, str) or full_text[start:end] != source_text:
        return None
    digest = hashlib.sha256(full_text.encode("utf-8")).hexdigest()
    if digest != source_hash:
        return None
    return evidence_id, start, end, source_hash, dict(location)


def validate_source_scope_binding(
    scope: dict[str, Any], clauses: list[dict[str, Any]], *,
    cited_clause_ids: set[str] | None = None,
) -> tuple[list[str], dict[str, Any] | None]:
    """Validate an explicit source selector and return its code-owned projection."""
    if not isinstance(scope, dict):
        return ["source_scope_must_be_object"], None
    if "anchor_clause_ids" not in scope:
        return ["source_scope_anchor_clause_ids_required"], None
    kind = scope.get("kind")
    targets = scope.get("target_clause_ids")
    anchors = scope.get("anchor_clause_ids", [])
    if kind not in {"explicit_clause_set", "same_paragraph_range", "same_table_row_range"}:
        return ["source_scope_kind_unsupported"], None
    if (not isinstance(targets, list) or not targets
            or any(not isinstance(value, str) or not value for value in targets)
            or len(targets) != len(set(targets))):
        return ["source_scope_target_clause_ids_invalid"], None
    if (not isinstance(anchors, list)
            or any(not isinstance(value, str) or not value for value in anchors)
            or len(anchors) != len(set(anchors))):
        return ["source_scope_anchor_clause_ids_invalid"], None
    if kind == "explicit_clause_set" and anchors:
        return ["explicit_clause_set_must_not_claim_range_anchors"], None
    if kind != "explicit_clause_set" and len(anchors) != 2:
        return ["source_scope_range_requires_two_anchors"], None
    by_id = {
        str(item.get("id")): item for item in clauses
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    referenced = set(targets) | set(anchors)
    errors: list[str] = []
    if not referenced <= set(by_id):
        errors.append("source_scope_references_unknown_clause")
    if cited_clause_ids is not None and not referenced <= cited_clause_ids:
        errors.append("source_scope_references_uncited_clause")
    if errors:
        return errors, None
    spans: dict[str, tuple[str, int, int, str, dict[str, Any]]] = {}
    for clause_id in referenced:
        span = _clause_span(by_id[clause_id])
        if span is None:
            errors.append(f"source_scope_clause_span_unverifiable:{clause_id}")
        else:
            spans[clause_id] = span
    if errors:
        return errors, None

    resolved_targets = list(targets)
    if kind == "same_paragraph_range":
        start_anchor, end_anchor = anchors
        start_span, end_span = spans[start_anchor], spans[end_anchor]
        if (start_span[0] != end_span[0] or start_span[4] != end_span[4]
                or start_span[1] > end_span[1]):
            return ["source_scope_paragraph_range_anchors_do_not_share_ordered_paragraph"], None
        expected = [
            str(item["id"]) for item in clauses
            if isinstance(item, dict) and isinstance(item.get("id"), str)
            and (candidate := _clause_span(item)) is not None
            and candidate[0] == start_span[0]
            and candidate[4] == start_span[4]
            and start_span[1] <= candidate[1] and candidate[2] <= end_span[2]
        ]
        if resolved_targets != expected or not expected:
            return ["source_scope_paragraph_range_target_set_mismatch"], None
    elif kind == "same_table_row_range":
        start_anchor, end_anchor = anchors
        start_span, end_span = spans[start_anchor], spans[end_anchor]
        start_loc, end_loc = start_span[4], end_span[4]
        row_keys = ("table_child_index", "row")
        if (not all(key in start_loc and key in end_loc for key in row_keys)
                or any(start_loc[key] != end_loc[key] for key in row_keys)
                or not all(type(start_loc.get(key)) is int for key in ("column", "paragraph"))
                or not all(type(end_loc.get(key)) is int for key in ("column", "paragraph"))):
            return ["source_scope_table_row_range_anchor_location_unverifiable"], None
        start_position = (start_loc["column"], start_loc["paragraph"], start_span[1])
        end_position = (end_loc["column"], end_loc["paragraph"], end_span[2])
        if start_position > end_position:
            return ["source_scope_table_row_range_anchors_not_ordered"], None
        selected = []
        for item in clauses:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            candidate = _clause_span(item)
            if candidate is None or candidate[0] != start_span[0]:
                # Evidence IDs differ per cell, so row membership is determined
                # by the extracted table/row locator below.
                candidate = _clause_span(item)
            if candidate is None:
                continue
            loc = candidate[4]
            if any(loc.get(key) != start_loc.get(key) for key in row_keys):
                continue
            if not all(type(loc.get(key)) is int for key in ("column", "paragraph")):
                return ["source_scope_table_row_target_location_unverifiable"], None
            position = (loc["column"], loc["paragraph"], candidate[1])
            candidate_end_position = (loc["column"], loc["paragraph"], candidate[2])
            if start_position <= position and candidate_end_position <= end_position:
                selected.append(str(item["id"]))
        if resolved_targets != selected or not selected:
            return ["source_scope_table_row_range_target_set_mismatch"], None

    source_spans = [
        {"clause_id": clause_id, "evidence_id": spans[clause_id][0],
         "start_offset": spans[clause_id][1], "end_offset": spans[clause_id][2],
         "source_sha256": spans[clause_id][3], "location": spans[clause_id][4]}
        for clause_id in resolved_targets
    ]
    return [], {
        "kind": kind,
        "anchor_clause_ids": list(anchors),
        "target_clause_ids": resolved_targets,
        "target_spans": source_spans,
    }
