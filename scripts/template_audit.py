#!/usr/bin/env python3
"""Profile-driven structural and rendered-page audit for official DOCX templates."""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import zipfile
from pathlib import Path
from typing import Any

from lxml import etree

from docx_semantics import normalized_fixed_text
from template_profile import NS, W, _iter_candidates, _node_text, _selector_matches, _style_id, load_profile
from semantic_contract import strict_json_read

ROOT = Path(__file__).resolve().parents[1]
ROMAN = re.compile(r"^[IVXLCDM]+$", re.I)
DECIMAL = re.compile(r"^\d+$")


def normalize(text: str) -> str:
    return normalized_fixed_text(text)


def _matches(text: str, anchors: list[str]) -> bool:
    value = normalize(text)
    return any(value == normalize(anchor) or value.startswith(normalize(anchor)) for anchor in anchors)


def _candidate_evidence(node: etree._Element, body_index: int, section: int,
                        style_names: dict[str, str]) -> dict[str, Any]:
    style_id = _style_id(node)
    return {
        "paragraph": body_index,
        "body_child_index": body_index,
        "section_index": section,
        "text": _node_text(node),
        "style_id": style_id,
        "style": style_names.get(style_id, style_id),
        "xml_path": node.getroottree().getpath(node),
    }


def _selector_diagnostics(candidates: list[tuple[etree._Element, int, int]], selector: dict[str, Any],
                          style_names: dict[str, str], *, limit: int = 8) -> dict[str, Any]:
    """Build a bounded, evidence-only repair packet for humans or an LLM.

    The packet never changes a selector.  It ranks existing OOXML candidates,
    states which constraints disagree, and emits a candidate selector that a
    separately validated repair loop may choose.  This keeps an LLM from
    inventing XML paths or silently weakening the fail-closed audit.
    """
    wanted_texts = [selector.get("text", ""), *selector.get("accepted_texts", [])]
    wanted_texts = [normalize(value) for value in wanted_texts if value]
    accepted_styles = [selector.get("style_id"), *selector.get("accepted_style_ids", [])]
    accepted_styles = list(dict.fromkeys(accepted_styles))
    ranked: list[dict[str, Any]] = []
    for index, (node, body_index, section) in enumerate(candidates):
        kind = "paragraph" if node.tag == f"{{{W}}}p" else "table"
        text = _node_text(node)
        normalized = normalize(text)
        style_id = _style_id(node)
        similarities = [difflib.SequenceMatcher(None, wanted, normalized).ratio() for wanted in wanted_texts]
        text_similarity = max(similarities, default=0.0)
        mismatches: list[str] = []
        if selector.get("kind") and selector["kind"] != kind:
            mismatches.append("kind")
        if wanted_texts:
            mode = selector.get("match", "exact")
            text_ok = (mode == "exact" and normalized in wanted_texts
                       or mode == "starts_with" and any(normalized.startswith(value) for value in wanted_texts)
                       or mode == "contains" and any(value in normalized for value in wanted_texts))
            if not text_ok:
                mismatches.append("text")
        if "style_id" in selector and style_id not in accepted_styles:
            mismatches.append("style_id")
        if "body_child_index" in selector and selector["body_child_index"] != body_index:
            mismatches.append("body_child_index")
        if "section_index" in selector and selector["section_index"] != section:
            mismatches.append("section_index")
        kind_score = 1.0 if not selector.get("kind") or selector["kind"] == kind else 0.0
        style_score = 1.0 if "style_id" not in selector or style_id in accepted_styles else 0.0
        section_score = 1.0 if "section_index" not in selector or selector["section_index"] == section else 0.0
        index_distance = abs(body_index - selector.get("body_child_index", body_index))
        position_score = 1.0 / (1.0 + index_distance)
        score = round(0.55 * text_similarity + 0.10 * kind_score + 0.15 * style_score
                      + 0.15 * section_score + 0.05 * position_score, 6)
        previous_text = _node_text(candidates[index - 1][0]) if index else ""
        next_text = _node_text(candidates[index + 1][0]) if index + 1 < len(candidates) else ""
        evidence = _candidate_evidence(node, body_index, section, style_names)
        evidence.update({
            "score": score,
            "text_similarity": round(text_similarity, 6),
            "mismatches": mismatches,
            "context": {"previous_text": previous_text[:240], "next_text": next_text[:240]},
            "candidate_selector": {
                "kind": kind,
                "text": text,
                "match": "exact",
                "style_id": style_id,
                "body_child_index": body_index,
                "section_index": section,
            },
            "constraint_stability": {
                "kind": "stable",
                "text": "semantic_stable",
                "match": "stable",
                "style_id": "conditionally_stable",
                "section_index": "conditionally_stable",
                "body_child_index": "volatile_after_word_save",
            },
        })
        ranked.append(evidence)
    ranked.sort(key=lambda item: (-item["score"], item["body_child_index"]))
    top = ranked[:limit]
    margin = round(top[0]["score"] - top[1]["score"], 6) if len(top) > 1 else None
    return {
        "contract": "selector-repair-evidence-v1",
        "original_selector": selector,
        "candidate_count": len(candidates),
        "top_score_margin": margin,
        "candidates": top,
        "policy": {
            "llm_must_choose_listed_candidate": True,
            "automatic_patch_requires_unique_validation": True,
            "validate_against": ["official_docx", "generated_docx", "word_resaved_docx"],
        },
    }


def _variant_selector(rule: dict[str, Any], thesis_profile: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    if "selector" in rule:
        return rule["selector"], None
    if "selector_variants" not in rule:
        return None, None
    variants = rule["selector_variants"]
    matched = []
    for variant in variants:
        if all(_metadata_value(thesis_profile, dotted) == expected for dotted, expected in variant["when"].items()):
            matched.append(variant["selector"])
    if len(matched) != 1:
        return None, f"expected exactly one metadata selector variant, got {len(matched)}"
    return matched[0], None


def _is_toc_cache_entry(node: etree._Element, style_names: dict[str, str]) -> bool:
    """Return true for generated TOC result paragraphs, not TOC headings.

    Word materializes TOC entries as ordinary body paragraphs.  Their cached
    text can repeat structural anchors such as references or appendix titles,
    so treating them as document-role occurrences creates false duplicates
    after a legitimate field refresh.  Built-in TOC style IDs/names are the
    stable semantic signal; PAGEREF is an additional defensive signal for
    templates whose style names were normalized during a Word save.
    """
    style_id = _style_id(node) or ""
    style_name = style_names.get(style_id, style_id)
    if re.fullmatch(r"TOC\s*\d+", style_id, re.I) or re.fullmatch(r"toc\s*\d+|目录\s*\d+", style_name, re.I):
        return True
    instructions = " ".join(node.xpath(".//w:instrText/text() | .//w:fldSimple/@w:instr", namespaces=NS))
    return bool(re.search(r"\bPAGEREF\b", instructions, re.I))


def _role_hits(candidates: list[tuple[etree._Element, int, int]], profile: dict[str, Any],
               thesis_profile: dict[str, Any], style_names: dict[str, str]) -> tuple[dict[str, list[dict[str, Any]]],
                                                                                  list[dict[str, Any]]]:
    hits: dict[str, list[dict[str, Any]]] = {}
    failures: list[dict[str, Any]] = []
    structural_candidates = [
        candidate for candidate in candidates
        if not _is_toc_cache_entry(candidate[0], style_names)
    ]
    for rule in profile["structure"]["ordered_roles"]:
        selector, variant_error = _variant_selector(rule, thesis_profile)
        current: list[dict[str, Any]] = []
        if variant_error:
            failures.append({"code": "template_role_selector_variant_mismatch", "role": rule["role"],
                             "detail": variant_error})
        elif selector is not None:
            current = [_candidate_evidence(node, body_index, section, style_names)
                       for node, body_index, section in structural_candidates
                       if _selector_matches(node, selector, body_index=body_index, section_index=section)]
            if rule.get("required") and len(current) != 1:
                failures.append({"code": "template_required_role_selector_not_unique", "role": rule["role"],
                                 "selector": selector, "actual": len(current), "evidence": current[:10],
                                 "selector_diagnostics": _selector_diagnostics(
                                     candidates, selector, style_names
                                 )})
        else:
            for node, body_index, section in structural_candidates:
                if _matches(_node_text(node), rule.get("anchors", [])):
                    current.append(_candidate_evidence(node, body_index, section, style_names))
        hits[rule["role"]] = current
    return hits, failures


def _metadata_value(metadata: dict[str, Any], dotted: str) -> Any:
    current: Any = metadata
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _metadata_findings(profile: dict[str, Any], thesis_profile: dict[str, Any] | None) -> tuple[list[dict[str, Any]], list[str]]:
    failures: list[dict[str, Any]] = []
    missing: list[str] = []
    thesis_profile = thesis_profile or {}
    variant = profile.get("variants", {})
    selector = variant.get("selector")
    if selector:
        actual = _metadata_value({"thesis_profile": thesis_profile}, selector)
        if variant.get("required") and actual not in variant.get("allowed", []):
            failures.append({"code": "template_variant_missing_or_invalid", "selector": selector,
                             "allowed": variant.get("allowed", []), "actual": actual})
    for dotted in profile.get("required_metadata", []):
        value = _metadata_value(thesis_profile, dotted)
        if value in (None, "", []):
            missing.append(dotted)
    if missing:
        failures.append({"code": "template_required_metadata_missing", "fields": missing})
    return failures, missing


def _heading_level(style_id: str | None, style_name: str | None) -> int | None:
    values = [value for value in (style_name, style_id) if value]
    match = next((re.search(r"(?:Heading|标题)\s*([1-9])", value, re.I) for value in values
                  if re.search(r"(?:Heading|标题)\s*([1-9])", value, re.I)), None)
    return int(match.group(1)) if match else None


def _read_ooxml(docx_path: Path) -> tuple[etree._Element, dict[str, str]]:
    with zipfile.ZipFile(docx_path) as archive:
        root = etree.fromstring(archive.read("word/document.xml"))
        styles_root = etree.fromstring(archive.read("word/styles.xml"))
    style_names = {}
    for style in styles_root.xpath("//w:style", namespaces=NS):
        style_id = style.get(f"{{{W}}}styleId")
        names = style.xpath("./w:name/@w:val", namespaces=NS)
        if style_id:
            style_names[style_id] = names[0] if names else style_id
    return root, style_names


def _page_role_map(pages: list[dict[str, Any]], profile: dict[str, Any]) -> tuple[dict[str, list[int]], dict[int, list[str]]]:
    role_pages: dict[str, list[int]] = {}
    page_roles: dict[int, list[str]] = {}
    header_markers = [normalize(value) for value in profile.get("render_rules", {}).get("header_markers", [])]
    for rule in profile["structure"]["ordered_roles"]:
        anchors = list(rule.get("anchors", []))
        if not anchors and rule.get("selector", {}).get("text"):
            anchors.append(rule["selector"]["text"])
        selector_match = rule.get("selector", {}).get("match", "contains")
        found = []
        for page in pages:
            raw_text = str(page.get("full_text", ""))
            # Render extraction includes repeated header text in full_text.  A
            # cover anchor such as "某大学硕士学位论文" must not therefore make
            # every numbered body page look like another cover page.
            for marker in header_markers:
                raw_text = raw_text.replace(marker, "")
            lines = [normalize(line) for line in raw_text.splitlines() if normalize(line)]
            normalized_anchors = [normalize(anchor) for anchor in anchors]
            if selector_match == "exact":
                matched = any(anchor == line for anchor in normalized_anchors for line in lines)
            elif selector_match == "starts_with":
                matched = any(line.startswith(anchor) for anchor in normalized_anchors for line in lines)
            else:
                matched = any(anchor in line for anchor in normalized_anchors for line in lines)
            if matched:
                found.append(page["page"])
                page_roles.setdefault(page["page"], []).append(rule["role"])
        role_pages[rule["role"]] = found
    return role_pages, page_roles


def _candidate(page: dict[str, Any]) -> dict[str, Any] | None:
    values = page.get("footer_page_number_candidates") or []
    return values[0] if len(values) == 1 else None


def _section_numbering(root: etree._Element) -> list[dict[str, Any]]:
    """Return effective page-number declarations for each OOXML section.

    Word may omit w:fmt="decimal" after a save because decimal is the OOXML
    default.  Other omitted values inherit from the previous section.
    """
    sections = root.xpath(".//w:p/w:pPr/w:sectPr | ./w:body/w:sectPr", namespaces=NS)
    result: list[dict[str, Any]] = []
    previous_format = "decimal"
    for index, section in enumerate(sections):
        nodes = section.xpath("./w:pgNumType", namespaces=NS)
        node = nodes[0] if nodes else None
        explicit_format = node.get(f"{{{W}}}fmt") if node is not None else None
        start_raw = node.get(f"{{{W}}}start") if node is not None else None
        effective_format = (explicit_format or "decimal") if node is not None else previous_format
        if node is not None:
            previous_format = effective_format
        result.append({
            "section_index": index,
            "format": effective_format,
            "start": int(start_raw) if start_raw is not None else None,
            "explicit_format": explicit_format,
            "source": "word/document.xml:w:sectPr/w:pgNumType",
        })
    return result


def _profile_number_format(value: str) -> str:
    return {"roman": "lowerRoman", "roman_upper": "upperRoman", "decimal": "decimal"}.get(value, value)


def audit_template(docx_path: Path, profile_path: Path, *, thesis_profile: dict[str, Any] | None = None,
                   render_layout: dict[str, Any] | None = None) -> dict[str, Any]:
    profile = load_profile(profile_path)
    root, style_names = _read_ooxml(docx_path)
    section_numbering = _section_numbering(root)
    candidates = list(_iter_candidates(root))
    body_text = "\n".join(_node_text(node) for node, _, _ in candidates)
    role_hits, selector_failures = _role_hits(candidates, profile, thesis_profile or {}, style_names)
    failures, missing_fields = _metadata_findings(profile, thesis_profile)
    failures.extend(selector_failures)
    observations: list[dict[str, Any]] = []

    previous = -1
    for rule in profile["structure"]["ordered_roles"]:
        role = rule["role"]
        hits = role_hits[role]
        required = bool(rule.get("required"))
        if required and not hits:
            failures.append({"code": "template_required_role_missing", "role": role,
                             "anchors": rule.get("anchors", []), "selector": rule.get("selector")})
            continue
        minimum = rule.get("min_occurrences", 1 if required else 0)
        maximum = rule.get("max_occurrences")
        if len(hits) < minimum or maximum is not None and len(hits) > maximum:
            failures.append({"code": "template_role_occurrence_mismatch", "role": role,
                             "minimum": minimum, "maximum": maximum, "actual": len(hits), "evidence": hits})
        if hits:
            position = hits[0]["paragraph"]
            if position < previous:
                failures.append({"code": "template_role_order_mismatch", "role": role,
                                 "position": position, "previous_position": previous})
            previous = max(previous, position)
        expected_level = rule.get("heading_level")
        if expected_level is not None:
            for hit in hits:
                actual_level = _heading_level(hit.get("style_id"), hit.get("style"))
                if actual_level != expected_level:
                    failures.append({"code": "template_heading_level_mismatch", "role": role,
                                     "expected": expected_level, "actual": actual_level, "evidence": hit})

    normalized_body = normalize(body_text)
    for item in profile.get("fixed_texts", []):
        if item.get("required", True) and normalize(item["text"]) not in normalized_body:
            failures.append({"code": "template_fixed_text_missing_or_modified", "fixed_text_id": item["id"]})

    forbidden_numbered = profile["structure"].get("forbidden_numbered_titles", [])
    for title in forbidden_numbered:
        pattern = re.compile(rf"(?:^|\n)\s*(?:第?\d+(?:\.\d+)+|\d+(?:\.\d+)+)\s*{re.escape(title)}\s*(?:\n|$)")
        if pattern.search(body_text):
            failures.append({"code": "template_forbidden_numbered_title", "title": title})

    render_status = "not_run"
    role_pages: dict[str, list[int]] = {}
    if render_layout and isinstance(render_layout.get("pages"), list):
        pages = render_layout["pages"]
        role_pages, page_roles = _page_role_map(pages, profile)
        render_status = "passed"
        body_candidates = [page for role in ("body",) for page in role_pages.get(role, [])]
        body_start = min(body_candidates) if body_candidates else None
        rules = profile.get("render_rules", {})
        if body_start and rules.get("no_header_before_body"):
            markers = rules.get("header_markers", [])
            leaking = [
                {"page": page["page"], "top_text": page.get("top_text", "")}
                for page in pages if page["page"] < body_start
                and any(normalize(marker) in normalize(str(page.get("top_text", ""))) for marker in markers)
            ]
            if leaking:
                failures.append({"code": "template_header_before_body", "body_start_page": body_start,
                                 "evidence": leaking})
        for role in rules.get("unnumbered_roles", []):
            for page_number in role_pages.get(role, []):
                page = pages[page_number - 1]
                if page.get("footer_page_number_candidates"):
                    failures.append({"code": "template_page_number_on_unnumbered_role", "role": role,
                                     "page": page_number, "evidence": page["footer_page_number_candidates"]})
        phases = rules.get("numbering_phases", [])
        for index, phase in enumerate(phases):
            role_evidence = role_hits.get(phase["start_role"], [])
            starts = role_pages.get(phase["start_role"], [])
            if not role_evidence:
                failures.append({"code": "template_numbering_phase_start_role_missing", "phase": phase})
                continue
            section_index = role_evidence[0]["section_index"]
            declaration = (section_numbering[section_index]
                           if 0 <= section_index < len(section_numbering) else None)
            start_page = min(starts) if starts else None
            end_page = len(pages)
            if index + 1 < len(phases):
                next_pages = role_pages.get(phases[index + 1]["start_role"], [])
                if next_pages:
                    end_page = min(next_pages) - 1
            expected_format = _profile_number_format(phase["format"])
            format_ok = declaration and declaration.get("format") == expected_format
            value_ok = declaration and declaration.get("start") == phase["start"]
            if not format_ok or not value_ok:
                failures.append({"code": "template_numbering_phase_start_mismatch", "phase": phase,
                                 "page": start_page, "section_index": section_index,
                                 "actual": declaration})
            observations.append({"code": "template_numbering_phase", "phase": phase,
                                 "section_index": section_index, "page_span": [start_page, end_page],
                                 "ooxml_evidence": declaration})
        for pair in rules.get("same_page_roles", []):
            left, right = pair
            left_pages, right_pages = role_pages.get(left, []), role_pages.get(right, [])
            if left_pages and right_pages and not set(left_pages) & set(right_pages):
                failures.append({"code": "template_roles_not_on_same_page", "roles": pair,
                                 "pages": {left: left_pages, right: right_pages}})
        toc_pages = role_pages.get("toc", [])
        forbidden = [normalize(title) for title in profile["structure"].get("forbidden_toc_titles", [])]
        for page_number in toc_pages:
            page = pages[page_number - 1]
            toc_entries = page.get("toc_entry_candidates") or []
            entry_titles = [normalize(str(entry.get("title", ""))) for entry in toc_entries]
            for title in forbidden:
                if title in entry_titles:
                    failures.append({"code": "template_toc_contains_forbidden_title", "title": title,
                                     "page": page_number})
        render_status = "failed" if any(item["code"].startswith("template_") for item in failures) else "passed"

    valid = not failures
    return {
        "schema_version": "1.0",
        "profile_id": profile["profile_id"],
        "profile_path": str(profile_path.resolve()),
        "artifact": str(docx_path.resolve()),
        "official_template_structure_valid": valid,
        "render_status": render_status,
        "manual_required_fields": missing_fields,
        "failures": failures,
        "observations": observations,
        "evidence": {"role_hits": role_hits, "role_pages": role_pages},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("docx", type=Path)
    parser.add_argument("profile", type=Path)
    parser.add_argument("--thesis-profile", type=Path)
    parser.add_argument("--render-layout", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    metadata = strict_json_read(args.thesis_profile) if args.thesis_profile else None
    layout = strict_json_read(args.render_layout) if args.render_layout else None
    result = audit_template(args.docx, args.profile, thesis_profile=metadata, render_layout=layout)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["official_template_structure_valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
