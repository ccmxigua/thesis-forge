"""Compile and independently audit source-scoped DOCX header rules.

Header text is not a document-wide singleton when the accepted source clauses
limit it to a document region.  This module preserves those clause scopes and
binds them to concrete section anchors in the supplied thesis DOCX.  Header
execution and OOXML relationship auditing live in ``apply_format_spec`` and
``header_scope_audit`` respectively; this file contains the shared source
anchor interpretation only.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Mapping

from docx import Document
from docx.oxml.ns import qn


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schema" / "format-spec.schema.json"

# These are registered applicability facts in the existing semantic contract,
# not clause IDs or thesis-specific section numbers.
_APPLICABILITY_SCOPES = {
    ("source_inventory.table_of_contents", "present", None): ("toc", "toc_heading"),
    ("thesis_profile.has_appendices", "equals", True): ("appendix", "appendix_heading_1"),
}


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()


def _compact_text(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()


def _heading1(paragraph: Any) -> bool:
    style = _normalized_text(getattr(getattr(paragraph, "style", None), "name", "")).casefold()
    return style in {"heading 1", "heading1", "标题 1", "标题1"}


def _toc_heading(paragraph: Any) -> bool:
    style = _normalized_text(getattr(getattr(paragraph, "style", None), "name", "")).casefold()
    text = _compact_text(getattr(paragraph, "text", ""))
    return "toc" in style or "目录标题" in style or (
        text in {"目录", "目錄"} and style in {"heading 1", "heading1", "标题 1", "标题1"}
    )


def _appendix_heading(text: str) -> bool:
    return _compact_text(text).startswith("附录")


_CHAPTER_PREFIX = re.compile(
    r"^(?:第\s*[0-9一二三四五六七八九十百千〇零]+\s*章|"
    r"chapter\s+[0-9ivxlcdm]+|[0-9]+(?:[.．][0-9]+)*\s+\S)",
    re.IGNORECASE,
)


def _chapter_heading(text: str) -> bool:
    return bool(_CHAPTER_PREFIX.search(_normalized_text(text)))


def _condition_scope(condition: Mapping[str, Any]) -> tuple[str, str] | None:
    key = (
        condition.get("fact"), condition.get("operator"), condition.get("value"),
    )
    return _APPLICABILITY_SCOPES.get(key)


def _rule_signature(rule: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        rule.get("scope"), rule.get("selector"), rule.get("content_mode"),
        rule.get("header_content"), tuple(rule.get("requirement_ids", [])),
        tuple(rule.get("clause_ids", [])), tuple(rule.get("evidence_ids", [])),
    )


def compile_header_scope_rules(spec: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Compile accepted clause applicability into typed header-scope rules.

    A legacy singleton ``roles.header.header_content`` is intentionally not
    treated as a scoped rule.  Conditional header requirements must identify a
    registered spatial scope; unknown or compound conditions fail closed.
    """
    findings: list[dict[str, Any]] = []
    requirements = spec.get("requirements", [])
    if not isinstance(requirements, list):
        return [], [{"code": "header_scope_requirements_invalid", "severity": "error",
                     "message": "format-spec requirements must be an array before header scopes can be compiled."}]

    by_scope: dict[str, dict[str, Any]] = {}
    unscoped_header_requirements: list[str] = []
    for requirement in requirements:
        if not isinstance(requirement, Mapping) or requirement.get("role") != "header":
            continue
        props = requirement.get("properties")
        content = props.get("header_content") if isinstance(props, Mapping) else None
        if not isinstance(content, Mapping) or not content:
            continue
        applicability = requirement.get("applicability")
        if not isinstance(applicability, Mapping):
            unscoped_header_requirements.append(str(requirement.get("id", "<missing-id>")))
            continue
        conditions = applicability.get("conditions")
        if applicability.get("status") != "conditional" or not isinstance(conditions, list) or not conditions:
            findings.append({
                "code": "header_scope_applicability_unresolved", "severity": "error",
                "requirement_id": requirement.get("id"),
                "message": "A header requirement has applicability but no supported conditional source scope.",
                "evidence": {"applicability": copy.deepcopy(applicability)},
            })
            continue
        resolved = [_condition_scope(item) if isinstance(item, Mapping) else None for item in conditions]
        if any(item is None for item in resolved) or len(set(resolved)) != 1:
            findings.append({
                "code": "header_scope_applicability_unmapped", "severity": "error",
                "requirement_id": requirement.get("id"),
                "message": "Header applicability conditions do not identify exactly one supported section scope.",
                "evidence": {"conditions": copy.deepcopy(conditions)},
            })
            continue
        scope, selector_kind = resolved[0]  # type: ignore[misc]
        clause_ids = requirement.get("clause_ids") if isinstance(requirement.get("clause_ids"), list) else []
        evidence_ids = requirement.get("evidence_ids") if isinstance(requirement.get("evidence_ids"), list) else []
        requirement_id = requirement.get("id")
        if not isinstance(requirement_id, str) or not clause_ids or not evidence_ids:
            findings.append({
                "code": "header_scope_provenance_missing", "severity": "error",
                "requirement_id": requirement_id,
                "message": "A scoped header rule must retain its requirement, clause, and evidence references.",
            })
            continue
        rule = {
            "scope": scope,
            "selector": {"kind": selector_kind},
            "content_mode": "fixed",
            "header_content": copy.deepcopy(dict(content)),
            "requirement_ids": [requirement_id],
            "clause_ids": sorted({str(item) for item in clause_ids}),
            "evidence_ids": sorted({str(item) for item in evidence_ids}),
        }
        prior = by_scope.get(scope)
        if prior is None:
            by_scope[scope] = rule
        elif prior["header_content"] == rule["header_content"] and prior["selector"] == rule["selector"]:
            for key in ("requirement_ids", "clause_ids", "evidence_ids"):
                prior[key] = sorted(set(prior[key]) | set(rule[key]))
        else:
            findings.append({
                "code": "header_scope_conflict", "severity": "error",
                "scope": scope,
                "message": "Multiple accepted header requirements provide conflicting content for the same source scope.",
                "evidence": {"requirements": [prior, rule]},
            })

    if by_scope and unscoped_header_requirements:
        findings.append({
            "code": "header_scope_unscoped_requirement", "severity": "error",
            "message": "Scoped and unscoped header content coexist; the unscoped requirement cannot be safely applied by section.",
            "evidence": {"requirement_ids": sorted(unscoped_header_requirements)},
        })

    derived = [by_scope[key] for key in sorted(by_scope)]
    explicit = spec.get("header_scope_rules")
    if explicit is not None:
        if not isinstance(explicit, list):
            findings.append({"code": "header_scope_rules_invalid", "severity": "error",
                             "message": "header_scope_rules must be an array."})
        else:
            explicit_by_scope = {str(item.get("scope")): item for item in explicit if isinstance(item, Mapping)}
            derived_by_scope = {item["scope"]: item for item in derived}
            if set(explicit_by_scope) != set(derived_by_scope) or any(
                _rule_signature(explicit_by_scope[key]) != _rule_signature(derived_by_scope[key])
                for key in set(explicit_by_scope) & set(derived_by_scope)
            ):
                findings.append({
                    "code": "header_scope_rule_source_mismatch", "severity": "error",
                    "message": "Persisted header_scope_rules differ from deterministic compilation of current accepted requirements.",
                    "evidence": {"persisted": explicit, "compiled": derived},
                })
            else:
                derived = [copy.deepcopy(explicit_by_scope[key]) for key in sorted(explicit_by_scope)]
    if any(item.get("severity") == "error" for item in findings):
        # A valid subset is not safe to execute when another accepted header
        # requirement is unmapped. Return no executable rules in that case.
        return [], findings
    return derived, findings


def _section_paragraphs(doc: Any) -> list[list[Any]]:
    sections: list[list[Any]] = [[] for _ in doc.sections]
    index = 0
    # Walk the body tree, not Document.paragraphs: the latter silently omits
    # table-cell paragraphs, and a heading in a table still belongs to the
    # section whose header will render it. A section break applies after its
    # paragraph, so record the paragraph before advancing the section index.
    from docx.text.paragraph import Paragraph

    body = doc._body._element
    for child in list(body):
        for element in child.iter(qn("w:p")):
            if index >= len(sections):
                return []
            paragraph = Paragraph(element, doc._body)
            sections[index].append(paragraph)
            ppr = element.find(qn("w:pPr"))
            sectpr = ppr.find(qn("w:sectPr")) if ppr is not None else None
            if sectpr is not None:
                # OOXML section boundaries in table-cell paragraphs are not a
                # supported source topology. Do not guess which table row is
                # the boundary for the body-level section properties.
                if child.tag != qn("w:p") or element.getparent() is not body:
                    return []
                index += 1
    if index != len(sections) - 1:
        return []
    return sections


def _story_details(story: Any) -> dict[str, Any]:
    text = "\n".join(paragraph.text for paragraph in story.paragraphs)
    instructions: list[str] = []
    for paragraph in story.paragraphs:
        instructions.extend(
            str(value).strip() for value in paragraph._p.xpath(".//w:instrText/text()")
        )
        instructions.extend(
            str(value).strip() for value in paragraph._p.xpath(".//w:fldSimple/@w:instr")
        )
    return {"text": text, "instructions": instructions, "linked_to_previous": bool(story.is_linked_to_previous)}


def _active_variants(doc: Any, section: Any, spec: Mapping[str, Any]) -> dict[str, bool]:
    settings = doc.settings.element
    even_odd = settings.find(qn("w:evenAndOddHeaders")) is not None
    page = spec.get("page") if isinstance(spec.get("page"), Mapping) else {}
    desired_first = page.get("different_first_page") if isinstance(page, Mapping) else None
    desired_even = page.get("different_odd_even") if isinstance(page, Mapping) else None
    return {
        "default": True,
        "first": bool(section.different_first_page_header_footer if desired_first is None else desired_first),
        "even": bool(even_odd if desired_even is None else desired_even),
    }


def _source_anchor(paragraph: Any, paragraph_index: int) -> dict[str, Any]:
    text = str(paragraph.text)
    style = getattr(getattr(paragraph, "style", None), "name", "")
    return {
        "kind": "toc_heading" if _toc_heading(paragraph) else "heading_1",
        "paragraph_index": paragraph_index,
        "style": str(style),
        "text": text,
        "text_sha256": _sha256_text(text),
    }


def compile_header_scope_plan(source_docx: str | Path, spec: Mapping[str, Any],
                             format_spec_path: str | Path | None = None) -> dict[str, Any]:
    """Resolve compiled header scope rules against the actual input DOCX."""
    path = Path(source_docx)
    source_bytes = path.read_bytes()
    source_sha = hashlib.sha256(source_bytes).hexdigest()
    spec_bytes = (
        Path(format_spec_path).read_bytes() if format_spec_path is not None
        else json.dumps(dict(spec), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    rules, findings = compile_header_scope_rules(spec)
    doc = Document(path)
    if not rules:
        return {
            "schema_version": "1.0", "valid": not any(x.get("severity") == "error" for x in findings),
            "scoped": False, "source_sha256": source_sha,
            "format_spec_sha256": hashlib.sha256(spec_bytes).hexdigest(),
            "scope_rules": [], "sections": [], "findings": findings,
        }

    sections = _section_paragraphs(doc)
    if len(sections) != len(doc.sections):
        findings.append({"code": "header_scope_section_parse_failed", "severity": "error",
                         "message": "Could not map source paragraphs to every DOCX section."})
        sections = [[] for _ in doc.sections]
    rules_by_scope = {str(item["scope"]): item for item in rules}
    toc_locations: list[tuple[int, Any, int]] = []
    appendix_locations: list[tuple[int, Any, int]] = []
    plan_sections: list[dict[str, Any]] = []
    profile = spec.get("thesis_profile") if isinstance(spec.get("thesis_profile"), Mapping) else {}

    for section_index, (section, paragraphs) in enumerate(zip(doc.sections, sections), 1):
        nonblank = [(i, p) for i, p in enumerate(paragraphs) if str(p.text).strip()]
        toc = [(i, p) for i, p in nonblank if _toc_heading(p)]
        headings = [(i, p) for i, p in nonblank if _heading1(p)]
        default_story = _story_details(section.header)
        active = _active_variants(doc, section, spec)
        source_anchor = None
        scope = "unscoped"
        content_mode = "preserve_source"
        fixed_content = None
        requirement_ids: list[str] = []
        clause_ids: list[str] = []
        evidence_ids: list[str] = []
        error = False

        if toc:
            if len(toc) != 1:
                findings.append({"code": "header_scope_toc_anchor_ambiguous", "severity": "error",
                                 "section_index": section_index,
                                 "message": "The source section contains more than one table-of-contents title anchor.",
                                 "evidence": {"paragraphs": [p.text for _, p in toc]}})
                error = True
            if headings:
                findings.append({"code": "header_scope_mixed_toc_and_heading", "severity": "error",
                                 "section_index": section_index,
                                 "message": "A single source section combines a table-of-contents anchor with Heading 1 scope; a section-level header cannot safely serve both.",
                                 "evidence": {"toc": [p.text for _, p in toc], "heading_1": [p.text for _, p in headings]}})
                error = True
            rule = rules_by_scope.get("toc")
            if rule is None:
                findings.append({"code": "header_scope_toc_rule_missing", "severity": "error",
                                 "section_index": section_index,
                                 "message": "A source table-of-contents anchor has no accepted TOC-scoped header rule."})
                error = True
            elif len(toc) == 1:
                scope = "toc"
                anchor_index, anchor_paragraph = toc[0]
                source_anchor = _source_anchor(anchor_paragraph, anchor_index)
                content_mode = str(rule["content_mode"])
                fixed_content = copy.deepcopy(rule["header_content"])
                requirement_ids = list(rule["requirement_ids"])
                clause_ids = list(rule["clause_ids"])
                evidence_ids = list(rule["evidence_ids"])
                toc_locations.append((section_index, anchor_paragraph, anchor_index))
        else:
            appendix_heads = [(i, p) for i, p in headings if _appendix_heading(p.text)]
            chapter_heads = [(i, p) for i, p in headings if _chapter_heading(p.text)]
            if appendix_heads:
                appendix_locations.extend((section_index, p, i) for i, p in appendix_heads)
                other_headings = [(i, p) for i, p in headings if not _appendix_heading(p.text)]
                if other_headings:
                    findings.append({"code": "header_scope_mixed_appendix_and_body", "severity": "error",
                                     "section_index": section_index,
                                     "message": "A source section combines appendix and non-appendix Heading 1 anchors, so their header scopes are not isolated.",
                                     "evidence": {"appendix": [p.text for _, p in appendix_heads],
                                                  "other_heading_1": [p.text for _, p in other_headings]}})
                    error = True
                rule = rules_by_scope.get("appendix")
                if rule is None:
                    findings.append({"code": "header_scope_appendix_rule_missing", "severity": "error",
                                     "section_index": section_index,
                                     "message": "An appendix source anchor has no accepted appendix-scoped header rule.",
                                     "evidence": {"anchors": [p.text for _, p in appendix_heads]}})
                    error = True
                else:
                    scope = "appendix"
                    anchor_index, anchor_paragraph = appendix_heads[0]
                    source_anchor = _source_anchor(anchor_paragraph, anchor_index)
                    content_mode = str(rule["content_mode"])
                    fixed_content = copy.deepcopy(rule["header_content"])
                    requirement_ids = list(rule["requirement_ids"])
                    clause_ids = list(rule["clause_ids"])
                    evidence_ids = list(rule["evidence_ids"])
            elif chapter_heads:
                if len(chapter_heads) != 1:
                    findings.append({"code": "header_scope_chapter_anchor_ambiguous", "severity": "error",
                                     "section_index": section_index,
                                     "message": "A static source section contains multiple chapter Heading 1 anchors; a single section header cannot be mapped to one chapter.",
                                     "evidence": {"anchors": [p.text for _, p in chapter_heads]}})
                    error = True
                anchor_index, anchor_paragraph = chapter_heads[0]
                source_anchor = _source_anchor(anchor_paragraph, anchor_index)
                scope = "chapter"
                rule = rules_by_scope.get("chapter")
                if rule is not None:
                    content_mode = str(rule["content_mode"])
                    fixed_content = copy.deepcopy(rule["header_content"])
                    requirement_ids = list(rule["requirement_ids"])
                    clause_ids = list(rule["clause_ids"])
                    evidence_ids = list(rule["evidence_ids"])
                else:
                    header_text = _normalized_text(default_story["text"])
                    heading_text = _normalized_text(anchor_paragraph.text)
                    if not header_text:
                        findings.append({"code": "header_scope_chapter_source_missing", "severity": "error",
                                         "section_index": section_index,
                                         "message": "The chapter anchor has no source-authored header and no accepted chapter header rule.",
                                         "evidence": {"anchor": anchor_paragraph.text}})
                        error = True
                    elif header_text != heading_text:
                        findings.append({"code": "header_scope_chapter_source_mismatch", "severity": "error",
                                         "section_index": section_index,
                                         "message": "The source chapter header does not match its Heading 1 anchor; no chapter title will be guessed.",
                                         "evidence": {"anchor": anchor_paragraph.text, "source_header": default_story["text"]}})
                        error = True
            elif headings:
                anchor_index, anchor_paragraph = headings[0]
                source_anchor = _source_anchor(anchor_paragraph, anchor_index)
                header_text = _normalized_text(default_story["text"])
                if header_text:
                    if header_text != _normalized_text(anchor_paragraph.text):
                        findings.append({"code": "header_scope_source_heading_mismatch", "severity": "error",
                                         "section_index": section_index,
                                         "message": "A source header is not anchored to the section's Heading 1; the mapping is unresolved.",
                                         "evidence": {"anchor": anchor_paragraph.text, "source_header": default_story["text"]}})
                        error = True
                    else:
                        scope = "source_heading"
                else:
                    scope = "unscoped"
            elif default_story["text"].strip():
                findings.append({"code": "header_scope_source_anchor_missing", "severity": "error",
                                 "section_index": section_index,
                                 "message": "The source contains header text but no TOC or Heading 1 anchor can bind it to a section."})
                error = True

        if error:
            scope = "unresolved"
        if fixed_content is not None and not isinstance(fixed_content.get("left_text"), str):
            findings.append({"code": "header_scope_fixed_text_missing", "severity": "error",
                             "section_index": section_index,
                             "scope": scope,
                             "message": "The scoped fixed header rule has no left_text value."})
            error = True
            scope = "unresolved"

        variant_contracts: dict[str, dict[str, Any]] = {}
        for variant, is_active in active.items():
            source_story = _story_details(getattr(section, f"{variant}_page_header") if variant == "first" else
                                          getattr(section, "even_page_header") if variant == "even" else section.header)
            if content_mode == "fixed" and fixed_content is not None:
                expected = copy.deepcopy(fixed_content)
                expected_text = str(expected.get("left_text", ""))
                mode = "fixed"
            else:
                expected_text = source_story["text"]
                expected = {"left_text": expected_text}
                mode = "preserve_source"
            variant_contracts[variant] = {
                "active": bool(is_active), "content_mode": mode,
                "header_content": expected, "expected_text": expected_text,
                "source_instructions": list(source_story["instructions"]),
                "source_linked_to_previous": bool(source_story["linked_to_previous"]),
            }

        plan_sections.append({
            "section_index": section_index,
            "source_section_index": section_index,
            "scope": scope,
            "source_anchor": source_anchor,
            "content_mode": "fixed" if fixed_content is not None else "preserve_source",
            "header_content": copy.deepcopy(fixed_content) if fixed_content is not None else None,
            "requirement_ids": requirement_ids,
            "clause_ids": clause_ids,
            "evidence_ids": evidence_ids,
            "source_default_header_sha256": _sha256_text(default_story["text"]),
            "variants": variant_contracts,
        })

    toc_rule = rules_by_scope.get("toc")
    if toc_rule and not toc_locations:
        findings.append({"code": "header_scope_toc_anchor_missing", "severity": "error",
                         "message": "The accepted TOC-scoped header rule matched no table-of-contents anchor in the source DOCX.",
                         "evidence": {"requirement_ids": toc_rule["requirement_ids"],
                                      "clause_ids": toc_rule["clause_ids"]}})
    appendix_rule = rules_by_scope.get("appendix")
    has_appendices = profile.get("has_appendices")
    if appendix_rule and not appendix_locations:
        findings.append({"code": "header_scope_appendix_anchor_missing", "severity": "error",
                         "message": "The accepted appendix-scoped header rule matched no appendix Heading 1 anchor in the source DOCX.",
                         "evidence": {"requirement_ids": appendix_rule["requirement_ids"],
                                      "clause_ids": appendix_rule["clause_ids"]}})
    if appendix_locations and has_appendices is False:
        findings.append({"code": "header_scope_appendix_profile_conflict", "severity": "error",
                         "message": "The source contains appendix anchors while the bound thesis profile says has_appendices=false.",
                         "evidence": {"anchors": [p.text for _, p, _ in appendix_locations]}})
    if has_appendices is True and appendix_locations and not appendix_rule:
        findings.append({"code": "header_scope_appendix_rule_missing", "severity": "error",
                         "message": "The profile and source show appendices, but no accepted appendix header rule exists."})

    valid = not any(item.get("severity") == "error" for item in findings)
    return {
        "schema_version": "1.0", "valid": valid, "scoped": True,
        "source_sha256": source_sha,
        "format_spec_sha256": hashlib.sha256(spec_bytes).hexdigest(),
        "scope_rules": rules,
        "sections": plan_sections,
        "findings": findings,
    }


def strip_flat_header_content(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Return style-only header roles when scoped rules are active."""
    roles = spec.get("roles") if isinstance(spec.get("roles"), Mapping) else {}
    result = copy.deepcopy(dict(roles))
    header = result.get("header")
    if isinstance(header, dict):
        header.pop("header_content", None)
        if not header:
            result.pop("header", None)
    return result
