#!/usr/bin/env python3
"""Compare a generated DOCX with written requirements and an official Word template.

The written format-spec is authoritative.  The official template is an
independent baseline and may itself conflict with the written rules; such
conflicts are reported instead of silently treating either source as correct.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

from artifact_io import atomic_write_text
from typing import Any

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml.ns import qn

ALIGNMENT = {
    WD_ALIGN_PARAGRAPH.LEFT: "left",
    WD_ALIGN_PARAGRAPH.CENTER: "center",
    WD_ALIGN_PARAGRAPH.RIGHT: "right",
    WD_ALIGN_PARAGRAPH.JUSTIFY: "justify",
    WD_ALIGN_PARAGRAPH.DISTRIBUTE: "distributed",
}
LINE_SPACING = {
    WD_LINE_SPACING.SINGLE: "single",
    WD_LINE_SPACING.ONE_POINT_FIVE: "one_point_five",
    WD_LINE_SPACING.DOUBLE: "double",
    WD_LINE_SPACING.EXACTLY: "exact",
    WD_LINE_SPACING.AT_LEAST: "at_least",
    WD_LINE_SPACING.MULTIPLE: "multiple",
}
FONT_ALIASES = {
    "宋体": "simsun", "simsun": "simsun",
    "黑体": "simhei", "simhei": "simhei",
    "楷体": "kaiti", "kaiti": "kaiti", "楷体_gb2312": "kaiti",
    "仿宋": "fangsong", "fangsong": "fangsong", "仿宋_gb2312": "fangsong",
    "timesnewroman": "timesnewroman",
}
STYLE_PROPERTIES = {
    "font.cjk", "font.latin", "font.size_pt", "font.bold", "font.italic",
    "paragraph.alignment", "paragraph.first_line_indent_pt",
    "paragraph.left_indent_pt", "paragraph.right_indent_pt",
    "paragraph.space_before_pt", "paragraph.space_after_pt",
    "paragraph.line_spacing.type", "paragraph.line_spacing.value",
    "paragraph.line_spacing.unit", "paragraph.outline_level", "paragraph.page_break_before",
    "paragraph.keep_with_next",
}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inherited(style: Any, getter: Any) -> Any:
    seen: set[str] = set()
    current = style
    while current is not None and current.style_id not in seen:
        seen.add(current.style_id)
        value = getter(current)
        if value is not None:
            return value
        current = current.base_style
    return None


def _points(value: Any) -> float | None:
    return round(value.pt, 3) if value is not None else None


def style_snapshot(style: Any) -> dict[str, Any]:
    def cjk(item: Any) -> str | None:
        rpr = item.element.rPr
        fonts = rpr.rFonts if rpr is not None else None
        return fonts.get(qn("w:eastAsia")) if fonts is not None else None

    size = inherited(style, lambda item: item.font.size)
    alignment = inherited(style, lambda item: item.paragraph_format.alignment)
    spacing_rule = inherited(style, lambda item: item.paragraph_format.line_spacing_rule)
    spacing = inherited(style, lambda item: item.paragraph_format.line_spacing)
    outline_level = inherited(
        style,
        lambda item: (
            int(item.element.pPr.outlineLvl.get(qn("w:val"))) + 1
            if item.element.pPr is not None and item.element.pPr.outlineLvl is not None
            else None
        ),
    )
    line_spacing = None
    if spacing is not None:
        spacing_type = LINE_SPACING.get(spacing_rule)
        if spacing_type in {"exact", "at_least"}:
            line_spacing = {"type": spacing_type, "value": round(spacing.pt, 3), "unit": "pt"}
        elif spacing_type:
            line_spacing = {"type": spacing_type, "value": float(spacing), "unit": "multiple"}
    return {
        "font": {
            "latin": inherited(style, lambda item: item.font.name),
            "cjk": inherited(style, cjk),
            "size_pt": round(size.pt, 3) if size else None,
            "bold": inherited(style, lambda item: item.font.bold),
            "italic": inherited(style, lambda item: item.font.italic),
        },
        "paragraph": {
            "alignment": ALIGNMENT.get(alignment),
            "first_line_indent_pt": _points(inherited(style, lambda item: item.paragraph_format.first_line_indent)),
            "left_indent_pt": _points(inherited(style, lambda item: item.paragraph_format.left_indent)),
            "right_indent_pt": _points(inherited(style, lambda item: item.paragraph_format.right_indent)),
            "space_before_pt": _points(inherited(style, lambda item: item.paragraph_format.space_before)) or 0,
            "space_after_pt": _points(inherited(style, lambda item: item.paragraph_format.space_after)) or 0,
            "line_spacing": line_spacing,
            "outline_level": outline_level,
            "page_break_before": inherited(style, lambda item: item.paragraph_format.page_break_before),
            "keep_with_next": inherited(style, lambda item: item.paragraph_format.keep_with_next),
        },
    }


def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else key
            result.update(flatten(child, path))
    else:
        result[prefix] = value
    return result


def normalized_requirement(role_spec: dict[str, Any]) -> dict[str, Any]:
    font = dict(role_spec.get("font", {}))
    paragraph = dict(role_spec.get("paragraph", {}))
    size = float(font.get("size_pt") or 12)
    line_height = float(paragraph.pop("spacing_line_height_pt", size))
    if "first_line_indent_chars" in paragraph:
        paragraph["first_line_indent_pt"] = float(paragraph.pop("first_line_indent_chars")) * size
    if "space_before_lines" in paragraph:
        paragraph["space_before_pt"] = float(paragraph.pop("space_before_lines")) * line_height
    if "space_after_lines" in paragraph:
        paragraph["space_after_pt"] = float(paragraph.pop("space_after_lines")) * line_height
    return {"font": font, "paragraph": paragraph}


def normalize_value(path: str, value: Any) -> Any:
    if value is None:
        return None
    if path in {"font.cjk", "font.latin"} and isinstance(value, str):
        compact = value.strip().casefold().replace(" ", "")
        return FONT_ALIASES.get(compact, compact)
    if isinstance(value, float):
        return round(value, 3)
    return value


def equal(path: str, left: Any, right: Any) -> bool:
    left = normalize_value(path, left)
    right = normalize_value(path, right)
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return abs(float(left) - float(right)) <= 0.05
    return left == right


def mapping_names(raw: dict[str, Any]) -> dict[str, str]:
    source = raw.get("mappings", raw)
    return {
        role: value.get("style_name") if isinstance(value, dict) else value
        for role, value in source.items()
        if (value.get("style_name") if isinstance(value, dict) else value)
    }


def role_snapshots(docx: Path, names: dict[str, str]) -> dict[str, dict[str, Any]]:
    doc = Document(docx)
    available = {style.name for style in doc.styles}
    return {
        role: style_snapshot(doc.styles[name])
        for role, name in names.items()
        if name in available
    }


def page_snapshot(docx: Path) -> dict[str, Any]:
    doc = Document(docx)
    if not doc.sections:
        return {}
    section = doc.sections[0]
    return {
        "size": "A4" if abs(section.page_width.mm - 210) < 1 and abs(section.page_height.mm - 297) < 1 else None,
        "orientation": "landscape" if section.page_width > section.page_height else "portrait",
        "margins_pt": {
            "top": _points(section.top_margin), "bottom": _points(section.bottom_margin),
            "left": _points(section.left_margin), "right": _points(section.right_margin),
            "header": _points(section.header_distance), "footer": _points(section.footer_distance),
            "gutter": _points(section.gutter),
        },
    }


def comparison_status(path: str, requirement: Any, official: Any, generated: Any) -> tuple[str, bool]:
    has_rule = requirement is not None
    has_official = official is not None
    if generated is None:
        return "not_observable", True
    if has_rule and has_official and not equal(path, official, requirement):
        # Preserve provenance conflicts in the report, but do not reject an
        # output that correctly follows the documented precedence rule:
        # written requirement > official template.
        return "conflict", not equal(path, generated, requirement)
    target = requirement if has_rule else official
    if target is None:
        return "manual_review", False
    return ("pass", False) if equal(path, generated, target) else ("fail", True)


def compare_role(role: str, requirement: dict[str, Any], official: dict[str, Any] | None,
                 generated: dict[str, Any] | None) -> list[dict[str, Any]]:
    required = flatten(normalized_requirement(requirement))
    official_values = flatten(official or {})
    generated_values = flatten(generated or {})
    properties = sorted((set(required) | set(official_values)) & STYLE_PROPERTIES)
    rows = []
    for path in properties:
        rule_value = required.get(path)
        official_value = official_values.get(path)
        # A property that is unspecified by the written rules and absent from the
        # official template is not a meaningful contract row.
        if rule_value is None and official_value is None:
            continue
        generated_value = generated_values.get(path)
        status, blocking = comparison_status(path, rule_value, official_value, generated_value)
        rows.append({
            "role": role, "property": path, "written_requirement": rule_value,
            "official_template_value": official_value, "generated_value": generated_value,
            "authoritative_source": "written_requirement" if rule_value is not None else "official_template",
            "status": status, "blocking": blocking,
        })
    unsupported = sorted(set(requirement) - {"font", "paragraph", "coverage", "required"})
    for key in unsupported:
        rows.append({
            "role": role, "property": key, "written_requirement": requirement[key],
            "official_template_value": None, "generated_value": None,
            "authoritative_source": "written_requirement", "status": "manual_review", "blocking": False,
            "note": "This property is validated elsewhere or requires structural/visual review.",
        })
    return rows


def compare_page(requirement: dict[str, Any], official_docx: Path, generated_docx: Path) -> list[dict[str, Any]]:
    official = flatten(page_snapshot(official_docx))
    generated = flatten(page_snapshot(generated_docx))
    required = flatten({key: value for key, value in requirement.items() if key in {"size", "orientation", "margins_pt"}})
    rows = []
    for raw_path in sorted(set(required) | set(official)):
        path = raw_path
        rule_value, official_value, generated_value = required.get(path), official.get(path), generated.get(path)
        if rule_value is None and official_value is None:
            continue
        status, blocking = comparison_status(path, rule_value, official_value, generated_value)
        rows.append({
            "role": "page", "property": path, "written_requirement": rule_value,
            "official_template_value": official_value, "generated_value": generated_value,
            "authoritative_source": "written_requirement" if rule_value is not None else "official_template",
            "status": status, "blocking": blocking,
        })
    if requirement.get("page_number"):
        rows.append({
            "role": "page", "property": "page_number", "written_requirement": requirement["page_number"],
            "official_template_value": None, "generated_value": None,
            "authoritative_source": "written_requirement", "status": "manual_review", "blocking": False,
            "note": "Page-number fields and rendered sequences are validated by submission/render audits.",
        })
    return rows


def build_report(generated_docx: Path, official_docx: Path, format_spec: Path,
                 official_style_map: Path, generated_style_map: Path,
                 *, requirements_only: bool = False) -> dict[str, Any]:
    spec = read_json(format_spec)
    official_names = mapping_names(read_json(official_style_map))
    generated_names = mapping_names(read_json(generated_style_map))
    official_roles = {} if requirements_only else role_snapshots(official_docx, official_names)
    generated_roles = role_snapshots(generated_docx, generated_names)
    rows: list[dict[str, Any]] = []
    # The written requirements define the executable comparison scope.  The
    # style analyzer may identify additional incidental built-in styles in the
    # official file; those are not school requirements by themselves.
    all_roles = sorted(spec.get("roles", {}))
    for role in all_roles:
        rows.extend(compare_role(role, spec.get("roles", {}).get(role, {}),
                                 official_roles.get(role), generated_roles.get(role)))
    if requirements_only:
        # Use an empty disposable official baseline logically; compare_page is
        # expanded inline so input/template values are not misrepresented as
        # official requirements.
        generated_page = flatten(page_snapshot(generated_docx))
        required_page = flatten({key: value for key, value in spec.get("page", {}).items()
                                 if key in {"size", "orientation", "margins_pt"}})
        for path in sorted(required_page):
            status, blocking = comparison_status(path, required_page[path], None, generated_page.get(path))
            rows.append({"role": "page", "property": path,
                         "written_requirement": required_page[path], "official_template_value": None,
                         "generated_value": generated_page.get(path),
                         "authoritative_source": "written_requirement", "status": status, "blocking": blocking})
        if spec.get("page", {}).get("page_number"):
            rows.append({"role": "page", "property": "page_number",
                         "written_requirement": spec["page"]["page_number"],
                         "official_template_value": None, "generated_value": None,
                         "authoritative_source": "written_requirement", "status": "manual_review",
                         "blocking": False,
                         "note": "Page-number fields and rendered sequences are validated by submission/render audits."})
    else:
        rows.extend(compare_page(spec.get("page", {}), official_docx, generated_docx))
    counts = Counter(row["status"] for row in rows)
    blocking = [row for row in rows if row.get("blocking")]
    return {
        "schema_version": "1.0",
        "status": "failed" if blocking else "passed",
        "comparison_policy": {
            "precedence": ["written_requirement", "official_template", "profile_default"],
            "official_template_baseline_available": not requirements_only,
            "blocking_statuses": ["fail", "conflict", "not_observable"],
            "non_blocking_statuses": ["pass", "manual_review", "not_applicable"],
        },
        "inputs": {
            "generated_docx": str(generated_docx), "generated_docx_sha256": sha256(generated_docx),
            "official_template": str(official_docx), "official_template_sha256": sha256(official_docx),
            "format_spec": str(format_spec), "official_style_map": str(official_style_map),
            "generated_style_map": str(generated_style_map),
        },
        "summary": {"total": len(rows), **{name: counts.get(name, 0) for name in
                    ("pass", "fail", "conflict", "not_observable", "manual_review", "not_applicable")},
                    "blocking": len(blocking)},
        "comparisons": rows,
    }


def markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# Post-generation format comparison",
        "",
        f"- Status: **{report['status']}**",
        f"- Total checks: {summary['total']}",
        f"- Pass: {summary['pass']}",
        f"- Fail: {summary['fail']}",
        f"- Official-source conflicts: {summary['conflict']}",
        f"- Not observable: {summary['not_observable']}",
        f"- Manual review: {summary['manual_review']}",
        "",
        "Written requirements take precedence over the official Word template. A mismatch between those two official sources is reported as `conflict`.",
        "",
        "| Role | Property | Written requirement | Official template | Generated DOCX | Result |",
        "|---|---|---|---|---|---|",
    ]
    def value(item: Any) -> str:
        if item is None:
            return "—"
        text = json.dumps(item, ensure_ascii=False) if isinstance(item, (dict, list)) else str(item)
        return text.replace("|", "\\|").replace("\n", " ")
    for row in report["comparisons"]:
        lines.append("| {role} | {property} | {written} | {official} | {generated} | **{status}** |".format(
            role=row["role"], property=row["property"], written=value(row["written_requirement"]),
            official=value(row["official_template_value"]), generated=value(row["generated_value"]),
            status=row["status"],
        ))
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("generated_docx", type=Path)
    parser.add_argument("--official-template", type=Path, required=True)
    parser.add_argument("--format-spec", type=Path, required=True)
    parser.add_argument("--official-style-map", type=Path, required=True)
    parser.add_argument("--generated-style-map", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--strict", action="store_true", help="return non-zero for blocking comparisons")
    parser.add_argument("--requirements-only", action="store_true",
                        help="do not treat the style-analysis DOCX as an official baseline")
    args = parser.parse_args(argv)
    report = build_report(args.generated_docx.resolve(), args.official_template.resolve(),
                          args.format_spec.resolve(), args.official_style_map.resolve(),
                          args.generated_style_map.resolve(), requirements_only=args.requirements_only)
    atomic_write_text(args.out, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if args.markdown:
        atomic_write_text(args.markdown, markdown(report))
    print(json.dumps({"status": report["status"], "summary": report["summary"], "output": str(args.out)}, ensure_ascii=False))
    return 1 if args.strict and report["status"] != "passed" else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
