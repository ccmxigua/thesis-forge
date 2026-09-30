"""Code-owned table geometry, not a semantic label-to-field inference engine."""
from __future__ import annotations

import copy
import hashlib
import re
from typing import Any

from semantic_contract import sha256_json

TABLE_CONTEXT_PROTOCOL = "current-source-table-row/v1"
TABLE_CONTEXT_RETRY_CODE = "table_structure_context_uncertainty"


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_table_structure_context(clause: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any] | None:
    """Validate extracted row facts against the exact current target evidence.

    Row snapshots retain blank/merged cells and neighboring source paragraphs
    even across chunk boundaries. Older evidence without geometry is not
    upgraded by interpreting context_before or by guessing physical columns.
    The row is part of the canonical evidence hash, not supplied by a model.
    """
    span = clause.get("source_span")
    if not isinstance(span, dict):
        return None
    target = evidence.get(span.get("evidence_id"))
    if not isinstance(target, dict) or target.get("kind") != "table_cell":
        return None
    row = target.get("table_row_context")
    if row is None:
        return None
    if not isinstance(row, dict) or row.get("protocol") != TABLE_CONTEXT_PROTOCOL:
        raise ValueError("malformed current-source table row context")
    payload = {key: value for key, value in row.items() if key != "row_sha256"}
    if sha256_json(payload) != row.get("row_sha256"):
        raise ValueError("stale current-source table row hash")
    location = target.get("location") or {}
    if not isinstance(location, dict) or any(
        type(row.get(key)) is not int or row[key] < 0 for key in ("table_child_index", "row")
    ):
        raise ValueError("invalid table source coordinates")
    if any(row.get(key) != location.get(key) for key in ("part", "table_child_index", "row")):
        raise ValueError("table row context belongs to a different source location")
    cells = row.get("cells")
    if not isinstance(cells, list) or not cells:
        raise ValueError("table row context has no physical cells")
    matches = []
    evidence_ids = set()
    for column, cell in enumerate(cells):
        if not isinstance(cell, dict) or type(cell.get("column")) is not int or cell["column"] != column:
            raise ValueError("table row physical column inventory is invalid")
        paragraphs = cell.get("paragraphs")
        if not isinstance(paragraphs, list):
            raise ValueError("table row paragraph inventory is invalid")
        for paragraph_index, paragraph in enumerate(paragraphs):
            if (not isinstance(paragraph, dict) or type(paragraph.get("paragraph")) is not int
                    or paragraph["paragraph"] != paragraph_index):
                raise ValueError("table row source paragraph is invalid")
            text = paragraph.get("text")
            if not isinstance(text, str) or paragraph.get("text_sha256") != text_sha256(text):
                raise ValueError("table row source text hash is invalid")
            eid = paragraph.get("evidence_id")
            if (text and (not isinstance(eid, str) or not eid)) or (not text and eid is not None):
                raise ValueError("table row text lacks an exact evidence identity")
            if eid in evidence_ids:
                raise ValueError("table row duplicates source evidence")
            if eid is not None:
                evidence_ids.add(eid)
                available = evidence.get(eid)
                expected_location = {"part": row["part"], "table_child_index": row["table_child_index"],
                                     "row": row["row"], "column": column,
                                     "paragraph": paragraph.get("paragraph")}
                if available is not None and (
                    not isinstance(available, dict) or available.get("text") != text
                    or any((available.get("location") or {}).get(k) != v for k, v in expected_location.items())
                ):
                    raise ValueError("table row context disagrees with current evidence")
            if eid == span.get("evidence_id"):
                if (column != location.get("column") or paragraph.get("paragraph") != location.get("paragraph")
                        or text != target.get("text") or text_sha256(text) != span.get("source_sha256")):
                    raise ValueError("table target source identity is stale")
                matches.append(column)
    if len(matches) != 1:
        raise ValueError("table target does not have a unique row occurrence")
    target_column = matches[0]
    # Only a complete, unmerged row can authorize a corrective *review*.
    # This never asserts the semantic meaning of its left-hand label.
    unmerged = (
        row.get("grid_before") == 0 and row.get("grid_after") == 0
        and row.get("bidi_visual") is False
        and type(row.get("table_grid_columns")) is int
        and row["table_grid_columns"] == len(cells)
        and type(row.get("grid_before")) is int and type(row.get("grid_after")) is int
        and all(type(c.get("grid_span")) is int and c["grid_span"] == 1
                and c.get("vertical_merge") is None and c.get("horizontal_merge") is None
                and c.get("has_nested_table") is False
                for c in cells)
    )
    left = cells[target_column - 1] if target_column else None
    single_text = lambda c: len([p for p in c["paragraphs"] if p["text"].strip()]) == 1
    left_relation = bool(unmerged and left and single_text(left) and single_text(cells[target_column]))
    return {"protocol": TABLE_CONTEXT_PROTOCOL, "source_row": copy.deepcopy(row),
            "target_evidence_id": span["evidence_id"], "target_column": target_column,
            "immediate_left_column": target_column - 1 if left_relation else None,
            "relationship": "same_row_immediate_left_unmerged" if left_relation else "geometry_not_unique",
            "meaning": "Source geometry only; field meaning and candidate coverage require independent semantic review."}


def table_context_retry_is_source_bound(check: dict[str, Any]) -> bool:
    """Narrow date-placeholder rereview; no new requirement or pass authority."""
    text = check.get("document_text")
    context = check.get("review_context")
    if not isinstance(context, dict):
        return False
    table = context.get("table_structure_context")
    if not (isinstance(text, str) and re.fullmatch(r"\s*年\s*月\s*日\s*", text)
            and isinstance(context, dict) and context.get("classification") == "executable"
            and context.get("requires_requirement") is True and context.get("linked_requirements")
            and isinstance(table, dict) and table.get("relationship") == "same_row_immediate_left_unmerged"):
        return False
    cited = context.get("cited_evidence")
    target_id = table.get("target_evidence_id")
    if not isinstance(cited, dict) or not isinstance(target_id, str):
        return False
    target = cited.get(target_id)
    if not isinstance(target, dict):
        return False
    source_text = target.get("text")
    if not isinstance(source_text, str) or source_text != text:
        return False
    try:
        rebuilt = build_table_structure_context(
            {"source_span": {"evidence_id": table["target_evidence_id"],
                             "source_sha256": text_sha256(source_text)}}, context["cited_evidence"],
        )
    except (ValueError, KeyError, TypeError):
        return False
    return rebuilt == table


def table_retry_feedback_is_source_bound(request: dict[str, Any]) -> bool:
    """Recheck the authorization when consuming/replaying a corrective receipt."""
    feedback = request.get("retry_feedback")
    if not isinstance(feedback, dict) or feedback.get("code") != TABLE_CONTEXT_RETRY_CODE:
        return False
    ids = feedback.get("clause_ids")
    if (type(request.get("provider_attempt")) is not int or request["provider_attempt"] != 2
            or not isinstance(ids, list) or not ids or any(not isinstance(i, str) or not i for i in ids)
            or len(set(ids)) != len(ids)):
        return False
    checks = request.get("checks")
    if not isinstance(checks, list):
        return False
    selected = [c for c in checks if isinstance(c, dict) and c.get("check_id") in ids]
    return len(selected) == len(ids) and all(table_context_retry_is_source_bound(c) for c in selected)
