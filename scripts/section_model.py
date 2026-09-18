"""Deterministic SectionPlan compiler and pure DOCX auditor.

The compiler turns the compact ``page.page_number`` format-spec rule plus
serialized DOCX structure into an explicit, section-by-section contract.  It
never edits a document and never guesses when a selector cannot be resolved.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

from page_field_ops import FORMAT_TO_SWITCH, VARIANTS, finding, inspect_docx

SUPPORTED_FORMATS = {"none", "roman", "roman_upper", "decimal"}


class SectionPlanError(ValueError):
    """Raised only for unusable API inputs; semantic failures are findings."""


def _coerce_evidence(docx: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(docx, Mapping):
        return dict(docx)
    return inspect_docx(docx)


def _selector_section(selector: Mapping[str, Any], evidence: Mapping[str, Any], *,
                      location: str = "page.page_number.body_start_selector",
                      code_prefix: str = "body_selector") -> tuple[int | None, list[dict[str, Any]]]:
    findings: list[dict[str, Any]] = []
    count = int(evidence.get("section_count", 0))
    strategy = selector.get("strategy")
    if strategy == "section_index":
        value = selector.get("section_index")
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= count:
            findings.append(finding(
                f"{code_prefix}_out_of_range", "Section-boundary selector does not identify a DOCX section.",
                location=location, expected=f"integer in 1..{count}", actual=value,
            ))
            return None, findings
        return value, findings
    if strategy == "first_heading_1":
        matches = [p for p in evidence.get("paragraphs", []) if p.get("is_heading_1") and str(p.get("text", "")).strip()]
        if not matches:
            findings.append(finding(
                f"{code_prefix}_no_match", "Section-boundary selector matched no Heading 1 paragraph.",
                location=location, expected="at least one Heading 1", actual=0,
            ))
            return None, findings
        if len(matches) != 1:
            findings.append(finding(
                f"{code_prefix}_ambiguous", "Section-boundary selector matched more than one Heading 1 paragraph.",
                location=location, expected="exactly one Heading 1", actual=len(matches),
                evidence={"matches": [
                    {"section_index": item.get("section_index"), "text": item.get("text", "")[:160]}
                    for item in matches[:20]
                ]},
            ))
            return None, findings
        return int(matches[0]["section_index"]), findings
    if strategy == "heading_text":
        pattern = selector.get("heading_text_pattern")
        if not isinstance(pattern, str) or not pattern:
            findings.append(finding(
                f"{code_prefix}_pattern_missing", "heading_text selector requires heading_text_pattern.",
                location=f"{location}.heading_text_pattern", expected="non-empty regex", actual=pattern,
            ))
            return None, findings
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            findings.append(finding(
                f"{code_prefix}_pattern_invalid", "heading_text_pattern is not a valid regular expression.",
                location=f"{location}.heading_text_pattern", expected="valid regex", actual=pattern,
                evidence={"error": str(exc)},
            ))
            return None, findings
        matches = [p for p in evidence.get("paragraphs", []) if regex.search(str(p.get("text", "")).strip())]
        if not matches:
            findings.append(finding(
                f"{code_prefix}_no_match", "heading_text selector matched no paragraph.",
                location=location, expected=pattern, actual=0,
            ))
            return None, findings
        if len(matches) != 1:
            findings.append(finding(
                f"{code_prefix}_ambiguous", "heading_text selector matched more than one paragraph.",
                location=location, expected="exactly one matching paragraph", actual=len(matches),
                evidence={"matches": [
                    {"section_index": item.get("section_index"), "text": item.get("text", "")[:160]}
                    for item in matches[:20]
                ]},
            ))
            return None, findings
        return int(matches[0]["section_index"]), findings
    findings.append(finding(
        f"{code_prefix}_strategy_unsupported", "Unsupported section-boundary selector strategy.",
        location=f"{location}.strategy",
        expected=["section_index", "first_heading_1", "heading_text"], actual=strategy,
    ))
    return None, findings


def _positive_start(value: Any, location: str, findings: list[dict[str, Any]]) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        findings.append(finding(
            "page_number_start_invalid", "Page-number start must be a positive integer.",
            location=location, expected="integer >= 1", actual=value,
        ))
        return None
    return value


def compile_section_plan(format_spec: Mapping[str, Any], docx: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    """Compile a JSON-serializable SectionPlan.

    ``docx`` may be a path or a prior :func:`page_field_ops.inspect_docx`
    result, which makes this function deterministic and easy to compose.
    Semantic/configuration errors are represented in ``findings`` and make the
    plan invalid; unresolved values are not silently defaulted.
    """
    evidence = _coerce_evidence(docx)
    section_count = int(evidence.get("section_count", 0))
    if section_count < 1:
        raise SectionPlanError("DOCX evidence contains no sections")
    page = format_spec.get("page", {}) if isinstance(format_spec.get("page", {}), Mapping) else {}
    rule = page.get("page_number")
    findings = list(evidence.get("findings", []))
    preserve_existing = False

    if rule is None:
        rule = {}
        formats = {"front": "none", "body": "none"}
        body_start = 1
    elif not isinstance(rule, Mapping):
        findings.append(finding(
            "page_number_rule_invalid", "page.page_number must be an object.",
            location="page.page_number", expected="object", actual=type(rule).__name__,
        ))
        rule = {}
        formats = {"front": "none", "body": "none"}
        body_start = 1
    else:
        front = rule.get("front_matter_format")
        body = rule.get("body_format")
        preserve_existing = bool(rule.get("preserve_existing_locations")) and front is None and body is None
        if preserve_existing:
            # The official template is the executable authority when the
            # requirements do not declare a page-number rule.  Derive only a
            # diagnostic front/body split; each section below copies its exact
            # serialized pgNumType and footer PAGE-field contract.
            body_start = next(
                (
                    index
                    for index, section in enumerate(evidence.get("sections", []), 1)
                    if section.get("page_number", {}).get("format") == "decimal"
                    and any(
                        prior.get("page_number", {}).get("format") not in {None, "decimal"}
                        for prior in evidence.get("sections", [])[: index - 1]
                    )
                ),
                1,
            )
            formats = {"front": "none", "body": "none"}
        elif front is None and body is None:
            # Alignment and other presentation hints can be extracted without
            # any evidence that PAGE fields are required.  Compile that case
            # to an explicit unnumbered plan rather than leaking JSON nulls or
            # inventing a numbering format.
            front = body = "none"
        if preserve_existing:
            selector = None
        else:
            body = body if body is not None else front
            front = front if front is not None else ("none" if rule.get("body_start_selector") else body)
            formats = {"front": front, "body": body}
            selector = rule.get("body_start_selector")
            if selector is None:
                if front != body:
                    findings.append(finding(
                        "body_selector_required", "Different front/body formats require body_start_selector.",
                        location="page.page_number.body_start_selector", expected="selector object", actual=None,
                    ))
                    body_start = None
                else:
                    body_start = 1
            elif not isinstance(selector, Mapping):
                findings.append(finding(
                    "body_selector_invalid", "body_start_selector must be an object.",
                    location="page.page_number.body_start_selector", expected="object", actual=type(selector).__name__,
                ))
                body_start = None
            else:
                body_start, selector_findings = _selector_section(selector, evidence)
                findings.extend(selector_findings)

    front_selector = None if preserve_existing else rule.get("front_matter_start_selector")
    if front_selector is None:
        front_start_section = 1
    elif not isinstance(front_selector, Mapping):
        findings.append(finding(
            "front_selector_invalid", "front_matter_start_selector must be an object.",
            location="page.page_number.front_matter_start_selector", expected="object",
            actual=type(front_selector).__name__,
        ))
        front_start_section = None
    else:
        front_start_section, selector_findings = _selector_section(
            front_selector, evidence,
            location="page.page_number.front_matter_start_selector",
            code_prefix="front_selector",
        )
        findings.extend(selector_findings)
    if (front_selector is not None and front_start_section is not None and body_start is not None
            and front_start_section >= body_start):
        findings.append(finding(
            "page_zone_order_invalid", "Explicit front matter must begin before body matter.",
            location="page.page_number", expected="front_matter_start_section < body_start_section",
            actual={"front": front_start_section, "body": body_start},
        ))

    for zone, value in formats.items():
        if value not in SUPPORTED_FORMATS:
            findings.append(finding(
                "page_number_format_unsupported", f"Unsupported {zone} page-number format.",
                location=f"page.page_number.{zone}_format", expected=sorted(SUPPORTED_FORMATS), actual=value,
            ))

    desired_first = bool(page.get("different_first_page", False)) if "different_first_page" in page else None
    desired_even = bool(page.get("different_odd_even", False)) if "different_odd_even" in page else None
    front_start = _positive_start(rule.get("front_matter_start", 1), "page.page_number.front_matter_start", findings)
    body_value = _positive_start(rule.get("body_start", 1), "page.page_number.body_start", findings)

    sections: list[dict[str, Any]] = []
    for index in range(1, section_count + 1):
        source = evidence["sections"][index - 1]
        if body_start is not None and index >= body_start:
            zone = "body"
        elif front_start_section is not None and index >= front_start_section:
            zone = "front"
        else:
            zone = "cover"
        if preserve_existing:
            source_page = source.get("page_number", {})
            fmt = source_page.get("format") or "none"
            ooxml_format = source_page.get("ooxml_format")
            numbered = bool(ooxml_format) or fmt != "none"
            restart = bool(source_page.get("restart"))
            start = source_page.get("start")
        else:
            fmt = "none" if zone == "cover" else formats[zone]
            ooxml_format = {"roman": "lowerRoman", "roman_upper": "upperRoman", "decimal": "decimal"}.get(fmt)
            numbered = fmt in SUPPORTED_FORMATS and fmt != "none"
            zone_first = index == body_start if zone == "body" else index == front_start_section
            # A front zone can be empty when body starts in section 1.
            restart = bool(numbered and zone_first)
            start = (body_value if zone == "body" else front_start) if restart else None
        title_pg = desired_first if desired_first is not None else bool(source.get("titlePg"))
        even_odd = desired_even if desired_even is not None else bool(source.get("evenAndOdd"))
        stories: dict[str, dict[str, dict[str, Any]]] = {"header": {}, "footer": {}}
        for kind in ("header", "footer"):
            for variant in VARIANTS:
                active = variant == "default" or (variant == "first" and title_pg) or (variant == "even" and even_odd)
                source_story = source.get("stories", {}).get(kind, {}).get(variant, {})
                stories[kind][variant] = {
                    "active": active,
                    "required_page_fields": (
                        len(source_story.get("page_fields", []))
                        if preserve_existing
                        else (1 if kind == "footer" and active and numbered else 0)
                    ),
                    "expected_page_switch": (
                        None if preserve_existing
                        else (FORMAT_TO_SWITCH.get(fmt) if kind == "footer" and active and numbered else None)
                    ),
                }
        sections.append({
            "section_index": index,
            "zone": zone,
            "format": fmt,
            "ooxml_format": ooxml_format,
            "numbered": numbered,
            "restart": restart,
            "start": start,
            "titlePg": title_pg,
            "evenAndOdd": even_odd,
            "stories": stories,
        })

    return {
        "schema_version": "1.0",
        "source": evidence.get("source"),
        "valid": not any(item.get("severity") == "error" for item in findings),
        "front_matter_start_section": front_start_section,
        "body_start_section": body_start,
        "page_properties_policy": "preserve_existing" if preserve_existing else "apply_plan",
        "section_count": section_count,
        "sections": sections,
        "findings": findings,
    }


def _switch_matches(actual: Any, expected: str) -> bool:
    if not isinstance(actual, str):
        return False
    # Word uses switch case to distinguish lower- and upper-case Roman output.
    # Requiring the canonical spelling also keeps the contract deterministic.
    return actual == expected


def audit_plan_against_docx(plan: Mapping[str, Any], docx: str | Path | Mapping[str, Any]) -> list[dict[str, Any]]:
    """Purely compare a compiled plan with serialized DOCX evidence."""
    evidence = _coerce_evidence(docx)
    findings = list(evidence.get("findings", []))
    expected_sections = plan.get("sections", [])
    actual_sections = evidence.get("sections", [])
    preserve_existing = plan.get("page_properties_policy") == "preserve_existing"
    if len(expected_sections) != len(actual_sections):
        findings.append(finding(
            "section_count_mismatch", "DOCX section count differs from SectionPlan.",
            location="sections", expected=len(expected_sections), actual=len(actual_sections),
        ))
        return findings

    for expected, actual in zip(expected_sections, actual_sections):
        index = expected["section_index"]
        base = f"sections[{index}]"
        actual_page = actual.get("page_number", {})
        expected_fmt = expected.get("ooxml_format")
        actual_fmt = actual_page.get("ooxml_format")
        if preserve_existing:
            expected_start = expected.get("start") if expected.get("restart") else None
            actual_start = actual_page.get("start") if actual_page.get("restart") else None
            if actual_fmt != expected_fmt or actual_start != expected_start:
                findings.append(finding(
                    "preserved_page_contract_mismatch",
                    "Official page-number properties changed despite preserve-existing policy.",
                    location=f"{base}.page_number",
                    expected={"format": expected_fmt, "start": expected_start},
                    actual={"format": actual_fmt, "start": actual_start},
                ))
        elif expected.get("numbered"):
            if actual_fmt != expected_fmt:
                findings.append(finding(
                    "section_page_format_mismatch", "Section page-number format does not match the plan.",
                    location=f"{base}.page_number.format", expected=expected_fmt, actual=actual_fmt,
                ))
            expected_start = expected.get("start") if expected.get("restart") else None
            actual_start = actual_page.get("start")
            if actual_start != expected_start:
                findings.append(finding(
                    "section_page_restart_mismatch", "Section page-number restart/start does not match the plan.",
                    location=f"{base}.page_number.start", expected=expected_start, actual=actual_start,
                ))
        elif not preserve_existing:
            if actual_fmt is not None or actual_page.get("start") is not None:
                findings.append(finding(
                    "section_page_format_present_on_unnumbered_zone",
                    "An unnumbered section retains a page-number format or restart.",
                    location=f"{base}.page_number", expected={"format": None, "start": None},
                    actual={"format": actual_fmt, "start": actual_page.get("start")},
                ))
            for variant in VARIANTS:
                story = actual["stories"]["footer"][variant]
                if story.get("active") and story.get("page_fields"):
                    findings.append(finding(
                        "page_field_on_unnumbered_section", "An active footer contains PAGE on an unnumbered section.",
                        location=f"{base}.footer.{variant}", expected=0, actual=len(story["page_fields"]),
                        evidence={"part": story.get("part")},
                    ))

        if bool(actual.get("titlePg")) != bool(expected.get("titlePg")):
            findings.append(finding(
                "title_page_setting_mismatch", "Section titlePg setting differs from the plan.",
                location=f"{base}.titlePg", expected=expected.get("titlePg"), actual=actual.get("titlePg"),
            ))
        if bool(actual.get("evenAndOdd")) != bool(expected.get("evenAndOdd")):
            findings.append(finding(
                "even_odd_setting_mismatch", "Document evenAndOdd setting differs from the plan.",
                location=f"{base}.evenAndOdd", expected=expected.get("evenAndOdd"), actual=actual.get("evenAndOdd"),
            ))

        for kind in ("header", "footer"):
            for variant in VARIANTS:
                expected_story = expected["stories"][kind][variant]
                actual_story = actual["stories"][kind][variant]
                if bool(actual_story.get("active")) != bool(expected_story.get("active")):
                    continue  # setting mismatch above is the actionable root cause
                if actual_story.get("broken_relationship"):
                    continue  # inspect_docx already emitted the package finding
                malformed = actual_story.get("malformed_fields", [])
                if malformed:
                    findings.append(finding(
                        "malformed_story_field", "Header/footer contains malformed field markup.",
                        location=f"{base}.{kind}.{variant}", expected="well-formed fields", actual=malformed,
                        evidence={"part": actual_story.get("part")},
                    ))
                if kind != "footer":
                    continue
                fields = actual_story.get("page_fields", [])
                required = int(expected_story.get("required_page_fields", 0))
                if preserve_existing:
                    if len(fields) != required:
                        findings.append(finding(
                            "preserved_page_field_count_mismatch",
                            "Footer PAGE-field count changed despite preserve-existing policy.",
                            location=f"{base}.footer.{variant}", expected=required, actual=len(fields),
                            evidence={"part": actual_story.get("part")},
                        ))
                    continue
                if not expected_story.get("active"):
                    continue
                if len(fields) != required:
                    findings.append(finding(
                        "page_field_count_mismatch", "Active footer PAGE-field count differs from the plan.",
                        location=f"{base}.footer.{variant}", expected=required, actual=len(fields),
                        evidence={"part": actual_story.get("part"), "linked_to_previous": actual_story.get("linked_to_previous")},
                    ))
                expected_switch = expected_story.get("expected_page_switch")
                if expected_switch:
                    bad = [field for field in fields if not _switch_matches(field.get("format_switch"), expected_switch)]
                    if bad:
                        findings.append(finding(
                            "page_field_switch_mismatch", "PAGE field switch is inconsistent with the section format.",
                            location=f"{base}.footer.{variant}", expected=expected_switch,
                            actual=[field.get("format_switch") for field in fields],
                            evidence={"part": actual_story.get("part"), "instructions": [field.get("instruction") for field in fields]},
                        ))
    return findings
