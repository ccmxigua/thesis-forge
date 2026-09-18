#!/usr/bin/env python3
"""Map DOCX paragraph styles to thesis semantic roles using auditable rules."""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from docx import Document
from docx.oxml.ns import qn

from docx_semantics import all_body_paragraphs, all_story_paragraphs, all_table_paragraphs, is_display_equation
from role_registry import role_names, style_patterns

ROLE_RULES = {role: style_patterns(role) for role in role_names()}
SAMPLE_RULES = {
    "heading_1": r"^第[一二三四五六七八九十\d]+章(?:\s|　)|^\d+\s+[^。；;]{1,40}$|^附录[A-Z一二三四五六七八九十](?:\s|　)",
    "heading_2": r"^\d+\.\d+\s+|^[A-Z]\.\d+\s+",
    "heading_3": r"^\d+\.\d+\.\d+\s+|^[A-Z]\.\d+\.\d+\s+",
    "figure_caption": r"^(图|Figure)\s*\d+",
    "table_caption": r"^(表|Table)\s*\d+",
    "abstract_title_zh": r"^摘\s*要$",
    "abstract_title_en": r"^Abstract$",
    "bibliography_heading": r"^参\s*考\s*文\s*献$",
}

# These roles are distinguished by paragraph text/OOXML context at apply time,
# and official templates commonly (and legitimately) assign them the same
# paragraph style.  Reuse is safe only when the current role has positive
# sample-text evidence; a matching style name by itself is not enough.
TEXT_DISAMBIGUATED_REUSE_GROUPS = (
    {"heading_1", "heading_2", "heading_3", "body_text", "abstract_title_zh", "abstract_title_en",
     "bibliography_heading", "toc"},
    {"figure_caption", "table_caption"},
)


def _inherited(style, getter):
    seen = set(); current = style
    while current is not None and current.style_id not in seen:
        seen.add(current.style_id); value = getter(current)
        if value is not None: return value
        current = current.base_style
    return None


def style_attributes(style) -> dict[str, Any]:
    def cjk(s):
        rpr = s.element.rPr; fonts = rpr.rFonts if rpr is not None else None
        return fonts.get(qn("w:eastAsia")) if fonts is not None else None
    f, p = style.font, style.paragraph_format
    size = _inherited(style, lambda s: s.font.size)
    return {"font": {"latin": _inherited(style, lambda s: s.font.name), "cjk": _inherited(style, cjk),
                     "size_pt": round(size.pt, 3) if size else None,
                     "bold": _inherited(style, lambda s: s.font.bold), "italic": _inherited(style, lambda s: s.font.italic)},
            "paragraph": {"alignment": str(_inherited(style, lambda s: s.paragraph_format.alignment)) if _inherited(style, lambda s: s.paragraph_format.alignment) is not None else None,
                          "first_line_indent_pt": round(_inherited(style, lambda s: s.paragraph_format.first_line_indent).pt, 3) if _inherited(style, lambda s: s.paragraph_format.first_line_indent) else None,
                          "space_before_pt": round(_inherited(style, lambda s: s.paragraph_format.space_before).pt, 3) if _inherited(style, lambda s: s.paragraph_format.space_before) else 0,
                          "space_after_pt": round(_inherited(style, lambda s: s.paragraph_format.space_after).pt, 3) if _inherited(style, lambda s: s.paragraph_format.space_after) else 0}}


def all_paragraphs(doc):
    yield from all_body_paragraphs(doc)
    for section in doc.sections:
        for container in (section.header, section.footer, section.first_page_header, section.first_page_footer,
                          section.even_page_header, section.even_page_footer):
            yield from all_story_paragraphs(container)


def score(role: str, name: str, samples: list[str], structural: dict[str, int] | None = None) -> tuple[int, list[str]]:
    # A TOC style can contain chapter-looking text, but that does not make it a
    # body heading.  Fail closed instead of letting sample text override the
    # explicit TOC identity.
    if role in {"heading_1", "heading_2", "heading_3"} and re.search(r"\bTOC\b|目录", name, re.I):
        return 0, []
    # Word's built-in ``Table of Figures`` style contains entries beginning
    # with 图/Figure and 表/Table.  Those are list entries, not captions in the
    # document body, so sample text must not let a TOC/list style tie with the
    # real Caption style.
    if role in {"figure_caption", "table_caption"} and re.search(
            r"\bTOC\b|目录|table\s+of\s+figures", name, re.I):
        return 0, []
    # Name rules are alternatives, not cumulative bonuses. Otherwise a generic
    # and an exact regex can make an unused style outrank a style used in text.
    matched = [(value, pattern) for pattern, value in ROLE_RULES.get(role, []) if re.search(pattern, name, re.I)]
    points = max((value for value, _ in matched), default=0)
    reasons = [f"style-name:{pattern}" for value, pattern in matched if value == points]
    structural = structural or {}
    pat = SAMPLE_RULES.get(role)
    sample_match = bool(pat and any(re.search(pat, s, re.I) for s in samples))
    # Cover tables often reuse ``Normal`` and contain date/metadata strings
    # such as ``20 年 月 日``. Those strings can accidentally satisfy the
    # generic numbered-heading sample rule; table-only evidence must not
    # outrank Word's canonical Heading N style.
    if role in {"heading_1", "heading_2", "heading_3", "heading_4"} and structural.get("table_paragraphs", 0):
        sample_match = False
    if sample_match:
        # Repeated observed semantic text is stronger evidence than an unused
        # built-in style whose name happens to match exactly.
        points += 90; reasons.append(f"sample-text:{pat}")
    if samples and (matched or sample_match):
        points += min(30, 10 + len(samples) * 3); reasons.append(f"used-by:{len(samples)}-paragraphs")
    if role == "table_text" and structural.get("table_paragraphs", 0):
        count = structural["table_paragraphs"]
        points += 120 + min(30, count); reasons.append(f"inside-table:{count}-paragraphs")
    if role == "equation" and structural.get("display_math_paragraphs", 0):
        count = structural["display_math_paragraphs"]
        points += 140 + min(30, count); reasons.append(f"contains-oMathPara:{count}-paragraphs")
    if role == "thesis_title_zh" and samples and any(len(s) >= 8 for s in samples) and re.search(r"title|题目", name, re.I): points += 10
    if role == "thesis_title_zh" and re.fullmatch(r"论文题目|中文题目", name, re.I):
        points += 20; reasons.append("locale-specific:zh-title")
    if role == "thesis_title_en" and re.fullmatch(r"EnglishTitle|英文题目", name, re.I):
        points += 20; reasons.append("locale-specific:en-title")
    return points, reasons


def safe_text_disambiguated_reuse(role: str, prior_roles: set[str], candidate: dict[str, Any]) -> bool:
    """Allow a shared style only when exact paragraph text disambiguates it."""
    if not any(reason.startswith("sample-text:") for reason in candidate.get("reasons", [])):
        return False
    semantic_prior = prior_roles - {"header", "footer", "table_text", "equation"}
    return any(role in group and semantic_prior <= group for group in TEXT_DISAMBIGUATED_REUSE_GROUPS)


def analyze(path: Path) -> dict[str, Any]:
    doc = Document(path); samples: dict[str, list[str]] = defaultdict(list)
    structural: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for p in all_paragraphs(doc):
        text = p.text.strip()
        if text and len(samples[p.style.name]) < 8: samples[p.style.name].append(text)
        if is_display_equation(p):
            structural[p.style.name]["display_math_paragraphs"] += 1
    for p in all_table_paragraphs(doc):
        if p.text.strip() or p._p.xpath(".//w:drawing|.//m:oMath|.//m:oMathPara"):
            structural[p.style.name]["table_paragraphs"] += 1
    styles = {}
    for s in doc.styles:
        if s.type == 1: styles[s.name] = {"attributes": style_attributes(s), "samples": samples.get(s.name, [])}
    mappings = {}; questions = []; used_by: dict[str, set[str]] = defaultdict(set)
    for role in ROLE_RULES:
        candidates = []
        for name, evidence in styles.items():
            value, reasons = score(role, name, evidence["samples"], structural.get(name))
            if value > 0: candidates.append({"style_name": name, "score": value, "reasons": reasons})
        candidates.sort(key=lambda x: (-x["score"], x["style_name"]))
        if not candidates: continue
        best = candidates[0]
        tied = [x for x in candidates if x["score"] == best["score"]]
        if role == "table_text" and len(tied) > 1:
            # Table cells are formatted by OOXML context, but a template may use
            # both Normal and one table-specific style equally often.  Prefer a
            # unique non-generic style; this is deterministic and does not claim
            # that arbitrary custom-style ties have been semantically resolved.
            specific = [x for x in tied if x["style_name"].casefold() not in
                        {"normal", "body text", "正文", "bodytext"}]
            if len(specific) == 1:
                best = specific[0]
                tied = [best]
        if role in {"header", "footer"} and len(tied) > 1:
            # Prefer the canonical built-in story style when it is tied with
            # template-specific variants. The semantic role is already proven
            # by the header/footer story container, so this does not guess from
            # body text or silently merge distinct content roles.
            canonical = role.capitalize()
            built_in = [item for item in tied if item["style_name"].casefold() == canonical.casefold()]
            if len(built_in) == 1:
                best = built_in[0]
                tied = [best]
        if role == "footnote" and len(tied) > 1:
            # Word's built-in Footnote Text style is the semantic anchor used
            # by real footnote stories.  Templates may also carry an unused
            # custom ``脚注`` style with the same name score; prefer the exact
            # canonical built-in name instead of escalating a false tie.
            canonical = [item for item in tied
                         if item["style_name"].casefold() == "footnote text"]
            if len(canonical) == 1:
                best = canonical[0]
                tied = [best]
        if role in {"heading_1", "heading_2", "heading_3", "heading_4"} and len(tied) > 1:
            # Some official templates carry both Word's canonical built-in
            # ``Heading N`` style and a legacy lowercase alias such as
            # ``heading1``.  They are semantically equivalent, but leaving the
            # tie unresolved blocks deterministic profile assembly.  Prefer
            # the canonical built-in name; content-specific evidence still
            # controls all non-tied and shared-style decisions.
            level = role.rsplit("_", 1)[-1]
            canonical = [item for item in tied
                         if item["style_name"].casefold() == f"heading {level}".casefold()]
            if len(canonical) == 1:
                best = canonical[0]
                tied = [best]
        if len(tied) > 1 or best["score"] < 70:
            questions.append({"id": f"Q{len(questions)+1:04d}", "role": role,
                              "question": "哪个 Word 样式对应此语义角色？", "candidates": candidates[:5]})
            continue
        # Structural roles may legitimately share a generic paragraph style:
        # OOXML context, not the style name alone, identifies them at apply time.
        prior_roles = used_by.get(best["style_name"], set())
        if (prior_roles and role not in {"header", "footer", "table_text", "equation"}
                and not safe_text_disambiguated_reuse(role, prior_roles, best)):
            questions.append({"id": f"Q{len(questions)+1:04d}", "role": role,
                              "question": "候选样式已映射到其他角色，是否复用？", "candidates": candidates[:5]})
            continue
        used_by[best["style_name"]].add(role)
        mappings[role] = {**best, "attributes": styles[best["style_name"]]["attributes"],
                          "sample_texts": styles[best["style_name"]]["samples"], "resolved_by": "rule"}
    return {"schema_version": "1.0", "source_document": str(path),
            "status": "needs_clarification" if questions else "rule_resolved",
            "mappings": mappings, "questions": questions,
            "style_evidence": styles}


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__); p.add_argument("input", type=Path); p.add_argument("--out", type=Path, required=True)
    args = p.parse_args(argv); result = analyze(args.input.resolve()); args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "mappings": len(result["mappings"]), "questions": len(result["questions"]), "output": str(args.out)}, ensure_ascii=False))
    return 0

if __name__ == "__main__": raise SystemExit(main(sys.argv[1:]))
