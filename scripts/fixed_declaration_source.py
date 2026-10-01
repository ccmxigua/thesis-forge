"""Shared source-only boundaries for fixed declaration review packets.

These predicates identify where one exact source declaration may continue. They
do not classify clauses or authorize a declaration requirement on their own.
"""
from __future__ import annotations

import re
import hashlib
from typing import Any


_HEADING = re.compile(
    r"(?:原创性|独创性|诚信|使用授权|版权授权|公开授权).{0,12}(?:声明|说明|书)$"
    r"|^(?:声明|授权书)$"
)
_ADMINISTRATIVE_HEADING = re.compile(
    r"^(?:非公开|不公开)学位论文(?:标注|审批|保密)(?:说明|表)$"
)
_SECTION_BOUNDARY = re.compile(
    r"^(?:摘要|ABSTRACT|目录|参考文献|致谢|后记"
    r"|附录(?:[A-Z\d一二三四五六七八九十]+)?"
    r"|在学期间发表的学术论文(?:与研究成果)?"
    r"|攻读(?:硕士|博士)?学位期间(?:取得|发表)的?(?:学术论文|研究成果|成果)"
    r"|第[一二三四五六七八九十\d]+章)$",
    re.IGNORECASE,
)


def _compact(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def is_fixed_declaration_heading(value: Any) -> bool:
    text = _compact(value)
    return len(text) <= 50 and bool(_HEADING.search(text))


def is_administrative_region_heading(value: Any) -> bool:
    """Keep a source region together without calling approval a declaration."""
    text = _compact(value)
    return len(text) <= 50 and bool(_ADMINISTRATIVE_HEADING.search(text))


def is_source_region_heading(value: Any) -> bool:
    return is_fixed_declaration_heading(value) or is_administrative_region_heading(value)


def is_fixed_declaration_boundary(value: Any) -> bool:
    """Stop before a second declaration or an ordinary document section."""
    text = _compact(value)
    return is_source_region_heading(text) or bool(_SECTION_BOUNDARY.match(text))


def is_blank_signature_line(value: Any) -> bool:
    """Recognize a blank printed line, never a person's completed signature."""
    if not isinstance(value, str):
        return False
    blank = r"[\s_＿]*"
    date = rf"(?:日期[：:]?{blank})?(?:年{blank}月{blank}日{blank})?"
    return bool(re.fullmatch(
        rf"(?:(?:学位论文)?(?:作者|研究生|导师|指导教师)(?:签名|签字)[：:]?{blank}{date}"
        rf"|日期[：:]?{blank}|年{blank}月{blank}日{blank})", value.strip()))


def bound_signature_lines(group: dict, evidence_context: dict) -> list[dict]:
    """Exact adjacent blank paragraphs attached to one complete source group."""
    previous = evidence_context.get((group.get("body_evidence_ids") or [None])[-1], {})
    lines = []
    for eid in group.get("signature_evidence_ids", []):
        record = evidence_context.get(eid, {})
        location, prior = record.get("location", {}), previous.get("location", {})
        if (record.get("kind") != "paragraph" or previous.get("kind") != "paragraph"
                or not is_blank_signature_line(record.get("text"))
                or location.get("part") != prior.get("part")
                or type(prior.get("child_index")) is not int
                or location.get("child_index") != prior["child_index"] + 1):
            return []
        lines.append({"text": record["text"], "source_evidence_id": eid,
            "source_sha256": hashlib.sha256(record["text"].encode("utf-8")).hexdigest(),
            "attestation_scope": "placeholder_presence_only"})
        previous = record
    return lines


def matches_declaration_render_selection(group: dict, edges: Any, source_ids: Any) -> bool:
    """Match physical print sources separately from nonempty execution edges.

    This predicate grants no semantic classification or source freshness. Its
    callers still validate exact current spans, inventories and local contracts.
    A heading may be informational without being an executable requirement edge.
    """
    if (not isinstance(edges, list) or not edges
            or not isinstance(source_ids, list) or not source_ids
            or any(not isinstance(value, str) or not value for value in [*edges, *source_ids])
            or len(set(edges)) != len(edges) or len(set(source_ids)) != len(source_ids)):
        return False
    return (group.get("evidence_ids") == source_ids
            and [cid for cid in group.get("clause_ids", []) if cid in edges] == edges)


def derive_fixed_declaration_candidates(
    clauses: Any, evidence_context: dict[str, Any], *, anchor: Any,
) -> list[dict[str, Any]]:
    """Group exact source paragraphs; never classify their obligations."""
    if not isinstance(clauses, list) or not isinstance(evidence_context, dict):
        return []

    signature_pattern = re.compile(
        r"(?:签名|签字)|日期.{0,12}年?.{0,6}月?.{0,6}日?|^\s*年\s*月\s*日\s*$"
    )
    candidates: list[dict[str, Any]] = []
    for start, clause in enumerate(clauses):
        if not isinstance(clause, dict) or not is_fixed_declaration_heading(clause.get("text")):
            continue
        heading_kind = clause.get("source_kind")
        heading_location = clause.get("location") if isinstance(clause.get("location"), dict) else {}
        body_clauses: list[dict[str, Any]] = []
        signature_clauses: list[dict[str, Any]] = []
        for following in clauses[start + 1:]:
            if not isinstance(following, dict):
                continue
            text = str(following.get("text") or "")
            if is_fixed_declaration_boundary(text):
                break
            following_kind = following.get("source_kind")
            following_location = (
                following.get("location") if isinstance(following.get("location"), dict) else {}
            )
            if heading_location.get("part") and following_location.get("part") != heading_location.get("part"):
                break
            if heading_kind and following_kind and ("table" in str(heading_kind)) != ("table" in str(following_kind)):
                break
            if "table" in str(heading_kind) and (
                heading_location.get("child_index") is not None
                and following_location.get("child_index") != heading_location.get("child_index")
            ):
                break
            evidence_ids = [str(value) for value in following.get("evidence_ids", [])]
            evidence_texts = [
                str(evidence_context[evidence_id].get("text") or "")
                for evidence_id in evidence_ids
                if isinstance(evidence_context.get(evidence_id), dict)
            ]
            source_text = evidence_texts[0] if evidence_texts else text
            if signature_pattern.search(source_text):
                if body_clauses and is_blank_signature_line(source_text):
                    signature_clauses.append(following)
                    continue
                break
            if signature_clauses:
                break
            if text.strip():
                body_clauses.append(following)
        if not body_clauses:
            continue
        grouped = [clause, *body_clauses]
        evidence_ids = [
            str(value) for item in grouped for value in item.get("evidence_ids", [])
        ]
        candidates.append({
            "kind": "exact_fixed_declaration",
            "heading_clause_id": clause.get("id"),
            "body_clause_ids": [item.get("id") for item in body_clauses],
            "clause_ids": [item.get("id") for item in grouped],
            "heading_evidence_ids": [str(value) for value in clause.get("evidence_ids", [])],
            "body_evidence_ids": list(dict.fromkeys(
                str(value) for item in body_clauses for value in item.get("evidence_ids", [])
            )),
            "evidence_ids": list(dict.fromkeys(evidence_ids)),
            "signature_clause_ids": [item.get("id") for item in signature_clauses],
            "signature_evidence_ids": list(dict.fromkeys(
                str(value) for item in signature_clauses for value in item.get("evidence_ids", []))),
            "before_role": anchor,
            "policy": "source grouping only; host materializes each unique cited paragraph once",
        })
    return candidates
