"""High-confidence semantic guards backed by bounded source evidence.

These guards do not invent executable DOCX properties.  They only refine a
review when the extracted clause is an isolated annotation whose nearby source
evidence identifies its physical meaning.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any


SPINE_CLEARANCE_REASON = (
    "原始模板的书脊示范图以箭头标注书脊上、下端各约3cm；这是实体装订/书脊制作要求，"
    "不能由普通DOCX正文排版后端生成或验证。"
)

SAMPLE_CONTENT_REASON = (
    "该段落是输入模板中的样例内容；证据只表明样例文本在模板中出现，"
    "没有表明提交论文必须复制该内容，因此不能据此生成DOCX格式义务。"
)

_NORMATIVE_MARKERS = re.compile(
    r"(?:必须|应当|须(?:要)?|要求|规定|格式(?:为|要求)|采用|使用|设置|填写|不得|不能|"
    r"按照|按本|统一为|标题(?:为|应)|正文(?:为|使用)|参考文献(?:按|采用|格式))"
)
_NUMBERED_ENTRY = re.compile(r"^\s*(?:\[\s*\d+\s*\]|[（(]\s*\d+\s*[）)])\s*")
_APPENDIX_HEADING = re.compile(r"^\s*附录\s*[A-ZＡ-Ｚ0-9一二三四五六七八九十]+\s*$", re.I)
_EDUCATION_ENTRY = re.compile(r"^\s*(?:\d{4}|YYYY-MM).*(?:大学|学士|硕士|博士|学位)")


def _has_normative_marker(text: str) -> bool:
    """Return whether the clause itself contains an explicit norm cue.

    The check intentionally does not inspect all neighboring context.  A
    nearby heading may contain words such as ``要求`` while the current
    paragraph is still a concrete sample value; only the cited text can
    establish a direct normative statement.
    """
    return bool(_NORMATIVE_MARKERS.search(text))


def sample_content_guard(
    clause: dict[str, Any], clauses: list[dict[str, Any]] | None = None,
) -> dict[str, str] | None:
    """Identify high-confidence sample content that cannot be executable.

    Requirements DOCX files often combine instructions with a filled thesis
    example.  Presence of a numbered reference, an appendix data label, or a
    placeholder education/publication entry is not, by itself, a formatting
    requirement.  This guard is deliberately narrow and fail-open for
    ambiguous prose: it only fires when bounded source context identifies a
    known sample-content section and the cited clause has no explicit
    normative marker.

    The result is metadata for an audit/normalization layer; it never invents
    a replacement requirement.
    """
    text = unicodedata.normalize("NFKC", str(clause.get("text") or "")).strip()
    if not text or _has_normative_marker(text):
        return None
    source_kind = str(clause.get("source_kind") or clause.get("evidence_context", {}).get("kind") or "")
    if source_kind not in {"paragraph", "table_cell", "table"}:
        return None
    context = unicodedata.normalize(
        "NFKC", bounded_clause_context(clause, clauses or []),
    )

    if _NUMBERED_ENTRY.match(text) and re.search(r"参考文献|发表.*(?:论文|成果)|科研成果", context):
        return {
            "kind": "numbered_bibliography_entry",
            "reason": SAMPLE_CONTENT_REASON,
        }

    # Appendix headings themselves can be genuine structural requirements.
    # Only classify their concrete title/data/cell contents as sample content.
    if "附录" in context and not _APPENDIX_HEADING.fullmatch(text):
        appendix_data = (
            source_kind == "table_cell"
            or bool(re.match(r"^\s*表\s*[A-ZＡ-Ｚ]?\d+\b", text, re.I))
            or bool(re.search(r"\d{4}年度.*(?:数据|统计)", text))
            or bool(re.search(r"(?:投资|税收)增长速度", text))
        )
        if appendix_data:
            return {
                "kind": "appendix_sample_content",
                "reason": SAMPLE_CONTENT_REASON,
            }

    if "教育经历" in context and _EDUCATION_ENTRY.fullmatch(text):
        return {
            "kind": "education_sample_content",
            "reason": SAMPLE_CONTENT_REASON,
        }

    return None


def bounded_clause_context(clause: dict[str, Any], clauses: list[dict[str, Any]]) -> str:
    """Collect direct and structurally nearby evidence without fuzzy matching.

    ``location.order`` is a flattened evidence order, not a document-layout
    boundary: a table can move the next paragraph many order units away while
    an unrelated appendix heading can still fall inside a broad numeric
    window.  Use the native document/table position when it is available and
    keep the order fallback deliberately narrow.  This prevents a distant
    ``附录`` instruction from reclassifying an unrelated cover or spine sample
    while retaining context for a sample cell immediately under an appendix
    heading.
    """
    pieces = [
        str(clause.get("text") or ""),
        str(clause.get("source_text_full") or ""),
        *[str(value) for value in clause.get("context_before", [])],
        *[str(value) for value in clause.get("context_after", [])],
    ]
    location = clause.get("location") or {}
    part = location.get("part")
    order = location.get("order")
    structural_positions = [
        value for key in ("child_index", "table_child_index")
        if isinstance((value := location.get(key)), int)
    ]
    for candidate in clauses:
        candidate_location = candidate.get("location") or {}
        if candidate_location.get("part") != part:
            continue
        candidate_positions = [
            value for key in ("child_index", "table_child_index")
            if isinstance((value := candidate_location.get(key)), int)
        ]
        if structural_positions and candidate_positions:
            if min(
                abs(left - right)
                for left in structural_positions
                for right in candidate_positions
            ) > 12:
                continue
        elif isinstance(order, int):
            candidate_order = candidate_location.get("order")
            if not isinstance(candidate_order, int) or abs(candidate_order - order) > 8:
                continue
        else:
            continue
        pieces.extend([
            str(candidate.get("text") or ""),
            str(candidate.get("source_text_full") or ""),
        ])
    return " ".join(piece for piece in pieces if piece)


def spine_clearance_external(clause: dict[str, Any], clauses: list[dict[str, Any]]) -> bool:
    """Whether an isolated 3 cm label is evidenced as a physical spine note."""
    text = unicodedata.normalize("NFKC", str(clause.get("text") or "")).strip()
    context = unicodedata.normalize("NFKC", bounded_clause_context(clause, clauses))
    return bool(
        re.fullmatch(r"3\s*(?:cm|厘米)\s*左右(?:3\s*(?:cm|厘米)\s*左右)?", text)
        and "书脊" in context
        and re.search(r"示范|印刷|装订", context)
    )
