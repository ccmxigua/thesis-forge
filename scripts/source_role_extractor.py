#!/usr/bin/env python3
"""Extract deterministic semantic body ranges from a converted thesis DOCX.

The extractor does not mutate the source.  It identifies role headings from
OOXML paragraph text and style, then emits half-open body-child ranges suitable
for role-specific package import.  Missing/ambiguous required roles and
intersecting ranges fail closed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path
from typing import Any

from lxml import etree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}
HEADING_STYLES = {"Heading1", "Heading2", "Heading3", "TOCHeading"}
ABSTRACT_TITLE_STYLES = {"AbstractTitleCN", "AbstractTitleEN", "Abstract Title CN", "Abstract Title EN"}
ROLE_ORDER = ("body", "references", "appendices", "acknowledgments", "academic_outputs")
ACADEMIC_OUTPUT_TITLES = {
    "攻读硕士学位期间的学术成果",
    "攻读学位期间取得的研究成果",
    "在学期间发表的学术论文与研究成果",
    "在学期间取得的科研成果",
}


def _normalized(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _paragraph_text(node: etree._Element) -> str:
    return "".join(node.xpath(".//w:t/text()", namespaces=NS)).strip()


def _style_id(node: etree._Element) -> str | None:
    values = node.xpath("./w:pPr/w:pStyle/@w:val", namespaces=NS)
    return str(values[0]) if values else None


def _section_indices(children: list[etree._Element]) -> dict[int, int]:
    section = 0
    result: dict[int, int] = {}
    for index, child in enumerate(children):
        result[index] = section
        if child.xpath(".//w:sectPr | self::w:sectPr", namespaces=NS):
            section += 1
    return result


def _heading_record(index: int, node: etree._Element, section_index: int) -> dict[str, Any]:
    text = _paragraph_text(node)
    return {
        "body_child_index": index,
        "section_index": section_index,
        "text": text,
        "normalized_text_sha256": hashlib.sha256(_normalized(text).encode("utf-8")).hexdigest(),
        "style_id": _style_id(node),
    }


def _source_document_content_start(children: list[etree._Element], content_end: int) -> int:
    """Find the first source paragraph that belongs in a whole-body import.

    Converted LaTeX DOCX files commonly contain a source-specific cover and
    title-page material before the Chinese abstract.  A profile with one
    whole-body dynamic region must discard that source cover but retain the
    abstract, TOC, chapters, references, appendices, and trailing sections.
    """
    fallback_heading: int | None = None
    for index, child in enumerate(children[:content_end]):
        if child.tag != f"{{{W}}}p":
            continue
        text = _paragraph_text(child)
        style = _style_id(child)
        compact = _normalized(text).lower()
        if style in ABSTRACT_TITLE_STYLES or compact in {"摘要", "摘 要", "abstract"}:
            return index
        if fallback_heading is None and _classify_heading(text, style) == "body":
            fallback_heading = index
    return fallback_heading if fallback_heading is not None else 0


def _is_body_heading(text: str, style_id: str | None) -> bool:
    if style_id != "Heading1":
        return False
    value = " ".join(text.split())
    return bool(
        re.match(r"^第?1章(?:\s|$)", value)
        or re.match(r"^1(?:[.．、]?\s+)引言$", value)
        or value in {"引言", "绪论"}
    )


def _strip_heading_number(text: str) -> str:
    value = " ".join(text.split())
    value = re.sub(r"^第?\d+章\s*", "", value)
    value = re.sub(r"^\d+(?:[.．]\d+)*[.．、]?\s*", "", value)
    return _normalized(value)


def _classify_heading(text: str, style_id: str | None) -> str | None:
    compact = _normalized(text)
    stripped = _strip_heading_number(text)
    if _is_body_heading(text, style_id):
        return "body"
    if style_id in HEADING_STYLES and stripped == "参考文献":
        return "references"
    if style_id == "Heading1" and re.match(r"^附录[A-ZＡ-Ｚ一二三四五六七八九十]", compact):
        return "appendices"
    if style_id in HEADING_STYLES and compact in {"致谢", "后记"}:
        return "acknowledgments"
    if style_id in HEADING_STYLES and compact in {_normalized(item) for item in ACADEMIC_OUTPUT_TITLES}:
        return "academic_outputs"
    return None


def _normalization_actions(role: str, heading: dict[str, Any],
                           target_headings: dict[str, str] | None) -> list[dict[str, str]]:
    actions: list[dict[str, str]] = []
    text = heading["text"]
    target = target_headings.get(role) if target_headings is not None else None
    if target and _normalized(text) != _normalized(target):
        actions.append({"action": "replace_heading_text", "from": text, "to": target})
    if role == "references" and target and _normalized(text) != _normalized(target):
        actions.append({"action": "set_unnumbered_role_heading", "role": role})
    return actions


def _heading_policy(role: str, target_headings: dict[str, str] | None) -> str:
    """Choose whether the source role heading is already present in target.

    Most official profiles expose a heading as the protected start boundary;
    the source role therefore contributes content after that heading.  A
    whole-body profile may instead use an empty marker (there is no target
    heading to preserve), in which case the source boundary heading must be
    imported with the role payload.
    """
    # A missing target profile means the caller is using the historical
    # generic role-map contract: headings are supplied by the destination
    # regions.  Only an explicit profile that omits a role indicates that the
    # destination has no corresponding heading boundary.
    return "preserve_template_boundary" if target_headings is None or role in target_headings else "include_source_boundary"


def target_headings_from_profile(profile_path: Path) -> dict[str, str]:
    """Derive target boundary text from a profile without school-specific rules."""
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    result: dict[str, str] = {}
    for node in profile.get("regions", {}).get("nodes", []):
        role = node.get("content_role")
        selector = node.get("start_selector") or node.get("selector") or {}
        text = selector.get("text")
        if role in ROLE_ORDER and isinstance(text, str) and text:
            result[str(role)] = text
    return result


def extract_source_roles(path: Path, *, required_roles: set[str] | None = None,
                         target_headings: dict[str, str] | None = None,
                         whole_document_roles: set[str] | None = None) -> dict[str, Any]:
    required = required_roles or {"body", "references", "acknowledgments", "academic_outputs"}
    targets = target_headings
    with zipfile.ZipFile(path) as archive:
        root = etree.fromstring(archive.read("word/document.xml"))
    body = root.find("w:body", NS)
    if body is None:
        raise ValueError("source DOCX has no w:body")
    children = list(body)
    content_end = len(children) - 1 if children and children[-1].tag == f"{{{W}}}sectPr" else len(children)
    sections = _section_indices(children)
    whole_roles = set(whole_document_roles or ())
    document_content_start = _source_document_content_start(children, content_end)

    candidates: dict[str, list[dict[str, Any]]] = {role: [] for role in ROLE_ORDER}
    for index, child in enumerate(children[:content_end]):
        if child.tag != f"{{{W}}}p":
            continue
        text = _paragraph_text(child)
        role = _classify_heading(text, _style_id(child))
        if role:
            candidates[role].append(_heading_record(index, child, sections[index]))

    findings: list[dict[str, Any]] = []
    selected: dict[str, dict[str, Any]] = {}
    for role in ROLE_ORDER:
        matches = candidates[role]
        # Multiple appendix chapter headings form one appendices role; use the first.
        if role == "appendices" and matches:
            selected[role] = matches[0]
            continue
        if len(matches) == 1:
            selected[role] = matches[0]
        elif len(matches) == 0 and role in required:
            # A whole-document dynamic region does not need a semantic
            # heading in the converted source.  Minimal DOCX fixtures and
            # short source documents may contain only a title plus body prose.
            # Anchor that region at the first content paragraph instead of
            # rejecting an otherwise importable document.  Role-specific
            # regions retain the original fail-closed requirement.
            if role in whole_roles and document_content_start < content_end:
                anchor = children[document_content_start]
                if anchor.tag == f"{{{W}}}p":
                    selected[role] = _heading_record(
                        document_content_start, anchor, sections[document_content_start]
                    )
                    selected[role]["synthetic_source_anchor"] = True
                else:
                    findings.append({
                        "code": "source_role.required_missing",
                        "role": role,
                        "message": f"required source role is missing: {role}",
                    })
            else:
                findings.append({
                    "code": "source_role.required_missing",
                    "role": role,
                    "message": f"required source role is missing: {role}",
                })
        elif len(matches) > 1:
            findings.append({
                "code": "source_role.heading_not_unique",
                "role": role,
                "message": f"source role heading must be unique: {role}",
                "candidates": matches,
            })

    unknown_whole_roles = sorted(whole_roles - set(selected))
    if unknown_whole_roles:
        findings.extend({
            "code": "source_role.whole_document_role_missing",
            "role": role,
            "message": f"whole-document source role is missing: {role}",
        } for role in unknown_whole_roles)
    if whole_roles:
        # A whole-document role consumes the complete dynamic payload.  Other
        # semantic ranges would overlap by definition and must not be emitted
        # as if they were independently importable regions.
        selected = {role: selected[role] for role in whole_roles if role in selected}

    ordered_starts = sorted(
        ((heading["body_child_index"], role) for role, heading in selected.items()),
        key=lambda item: item[0],
    )
    roles: dict[str, Any] = {}
    for position, (start, role) in enumerate(ordered_starts):
        if role in whole_roles:
            range_start = document_content_start
            end = content_end
        else:
            range_start = start
            end = ordered_starts[position + 1][0] if position + 1 < len(ordered_starts) else content_end
        if range_start >= end:
            findings.append({
                "code": "source_role.range_invalid", "role": role,
                "message": f"source role range is empty or inverted: {role}",
                "start_body_child_index": range_start, "end_body_child_index": end,
            })
            continue
        heading_policy = (
            "include_source_boundary" if role in whole_roles
            else _heading_policy(role, targets)
        )
        roles[role] = {
            "role": role,
            "required": role in required,
            "start_body_child_index": range_start,
            "content_start_body_child_index": (
                range_start + 1 if heading_policy == "preserve_template_boundary" else range_start
            ),
            "end_body_child_index": end,
            "range_policy": "half_open",
            "heading_policy": heading_policy,
            "heading": selected[role],
            "normalization_actions": (
                [] if selected[role].get("synthetic_source_anchor")
                else _normalization_actions(role, selected[role], targets)
            ),
            **({"source_document_content_start": document_content_start} if role in whole_roles else {}),
        }

    ranges = sorted(
        (item["start_body_child_index"], item["end_body_child_index"], role)
        for role, item in roles.items()
    )
    for (_, left_end, left_role), (right_start, _, right_role) in zip(ranges, ranges[1:]):
        if left_end > right_start:
            findings.append({
                "code": "source_role.range_overlap",
                "message": "source role ranges overlap",
                "left_role": left_role, "right_role": right_role,
            })

    return {
        "schema_version": "1.0",
        "artifact": str(path.resolve()),
        "status": "blocked" if findings else "extracted",
        "required_roles": sorted(required),
        "roles": roles,
        "candidates": candidates,
        "findings": findings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("docx", type=Path)
    parser.add_argument("--required-role", action="append", default=[])
    parser.add_argument("--template-profile", type=Path,
                        help="derive target role-heading text from this profile's region boundaries")
    parser.add_argument("--whole-document-role", action="append", default=[],
                        help="import the source document content range for this role (repeatable)")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    required = set(args.required_role) if args.required_role else None
    try:
        target_headings = target_headings_from_profile(args.template_profile) if args.template_profile else None
        result = extract_source_roles(
            args.docx, required_roles=required, target_headings=target_headings,
            whole_document_roles=set(args.whole_document_role),
        )
        rc = 0 if result["status"] == "extracted" else 2
    except (OSError, KeyError, ValueError, zipfile.BadZipFile, etree.XMLSyntaxError) as exc:
        result = {
            "schema_version": "1.0", "artifact": str(args.docx.resolve()),
            "status": "blocked", "required_roles": sorted(required or []),
            "roles": {}, "candidates": {},
            "findings": [{"code": "source_role.read_failed", "message": str(exc)}],
        }
        rc = 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": result["status"], "role_count": len(result["roles"]),
        "finding_count": len(result["findings"]), "out": str(args.out),
    }, ensure_ascii=False))
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
