"""Execute a compiled SectionPlan against a python-docx document.

This module is the only mutating implementation for section page-number
formatting and PAGE fields.  Callers must compile the format-spec with
``section_model.compile_section_plan`` first and audit the serialized output
with ``section_model.audit_plan_against_docx`` afterwards.
"""
from __future__ import annotations

import copy
from typing import Any, Mapping

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

from page_field_ops import FORMAT_TO_SWITCH, VARIANTS, parse_field_instruction

ALIGNMENTS = {
    "left": WD_ALIGN_PARAGRAPH.LEFT,
    "center": WD_ALIGN_PARAGRAPH.CENTER,
    "right": WD_ALIGN_PARAGRAPH.RIGHT,
}


def all_story_paragraphs(story) -> list[Paragraph]:
    """Return all story paragraphs, including paragraphs nested in SDTs."""
    return [Paragraph(node, story) for node in story._element.xpath(".//w:p")]


def _story(section, kind: str, variant: str):
    prefix = "first_page_" if variant == "first" else "even_page_" if variant == "even" else ""
    return getattr(section, prefix + kind)


def unlink_story_preserving_content(story, *, force_clone: bool = False) -> None:
    """Materialize an independent story part without losing inherited OOXML.

    Several sections may explicitly reference the same footer relationship.  In
    that case ``is_linked_to_previous=True`` is destructive: python-docx drops
    the document relationship even though an earlier section still names it.
    Clone by replacing only this section's reference and retain the shared old
    relationship for its other consumers.
    """
    if not story.is_linked_to_previous and not force_clone:
        return
    children = [copy.deepcopy(node) for node in story._element]
    if force_clone and not story.is_linked_to_previous:
        # Remove this section's reference without dropping the shared package
        # relationship, then create a fresh part of the same story kind.
        # _Footer and _Header intentionally expose parallel private adapters.
        if story.__class__.__name__ == "_Footer":
            story._sectPr.remove_footerReference(story._hdrftr_index)
        elif story.__class__.__name__ == "_Header":
            story._sectPr.remove_headerReference(story._hdrftr_index)
        else:
            raise TypeError(f"unsupported header/footer story: {type(story)!r}")
        story._add_definition()
    else:
        story.is_linked_to_previous = False
    target = story._element
    for node in list(target):
        target.remove(node)
    for node in children:
        target.append(node)


def _opcode(instruction: str) -> str | None:
    return parse_field_instruction(instruction).get("opcode")


def remove_page_fields(paragraph: Paragraph) -> int:
    """Remove complete simple/complex PAGE fields, preserving other fields/text."""
    removed = 0
    root = paragraph._p
    for field in list(root.findall(qn("w:fldSimple"))):
        if _opcode(field.get(qn("w:instr")) or "") == "PAGE":
            root.remove(field)
            removed += 1
    children = list(root)
    index = 0
    while index < len(children):
        child = children[index]
        begin = child.find(".//" + qn("w:fldChar"))
        if begin is not None and begin.get(qn("w:fldCharType")) == "begin":
            end_index = index
            instructions: list[str] = []
            while end_index < len(children):
                instructions.extend(node.text or "" for node in children[end_index].findall(".//" + qn("w:instrText")))
                end = children[end_index].find(".//" + qn("w:fldChar"))
                if end is not None and end.get(qn("w:fldCharType")) == "end":
                    break
                end_index += 1
            if end_index < len(children) and _opcode("".join(instructions)) == "PAGE":
                for node in children[index:end_index + 1]:
                    root.remove(node)
                removed += 1
                children = list(root)
                continue
        elif _opcode("".join(node.text or "" for node in child.findall(".//" + qn("w:instrText")))) == "PAGE":
            root.remove(child)
            removed += 1
            children = list(root)
            continue
        index += 1
    return removed


def add_page_field(paragraph: Paragraph, format_switch: str) -> None:
    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), f"PAGE \\* {format_switch}")
    run = OxmlElement("w:r")
    text = OxmlElement("w:t")
    text.text = "1"
    run.append(text)
    field.append(run)
    paragraph._p.append(field)


def _set_page_number_format(section, ooxml_format: str | None, start: int | None) -> None:
    node = section._sectPr.find(qn("w:pgNumType"))
    if ooxml_format is None:
        if node is not None:
            section._sectPr.remove(node)
        return
    if node is None:
        node = OxmlElement("w:pgNumType")
        section._sectPr.append(node)
    node.set(qn("w:fmt"), ooxml_format)
    if start is None:
        node.attrib.pop(qn("w:start"), None)
    else:
        node.set(qn("w:start"), str(start))


def normalize_section_property_order(doc: Document) -> int:
    """Put header/footer references before page setup children in CT_SectPr."""
    changed = 0
    reference_tags = {qn("w:headerReference"), qn("w:footerReference")}
    for section in doc.sections:
        sectpr = section._sectPr
        references = [child for child in list(sectpr) if child.tag in reference_tags]
        if not references or list(sectpr)[:len(references)] == references:
            continue
        for child in references:
            sectpr.remove(child)
        for index, child in enumerate(references):
            sectpr.insert(index, child)
        changed += 1
    return changed


def _placement_paragraph(story, previous_field_paragraphs: list[Paragraph], section_index: int,
                         variant: str) -> Paragraph:
    # Official templates often place PAGE inside a narrow floating text box.
    # Reusing that nested paragraph preserves the box width, which can clip
    # multi-digit values (for example page 10 renders visibly as "1").  When
    # the complete footer story contains no non-PAGE text, replace the whole
    # page-only construction with one ordinary centered footer paragraph.
    # This is safe only for a text-empty story; mixed-content footers continue
    # through the conservative ambiguity checks below.
    visible_text = "".join(story._element.xpath(".//w:t/text()")) .strip()
    if not visible_text and story._element.xpath(".//w:drawing | .//w:pict | .//w:txbxContent"):
        for child in list(story._element):
            story._element.remove(child)
        return story.add_paragraph()
    pure = [paragraph for paragraph in previous_field_paragraphs
            if not "".join(paragraph._p.xpath(".//w:t/text()")) .strip()]
    if pure:
        return pure[0]
    paragraphs = all_story_paragraphs(story)
    substantive = [paragraph for paragraph in paragraphs
                   if "".join(paragraph._p.xpath(".//w:t/text()")) .strip()]
    if not substantive:
        return story.paragraphs[0] if story.paragraphs else story.add_paragraph()
    raise ValueError(
        f"section {section_index} {variant} footer contains non-page content but has no "
        "independent pure PAGE paragraph; automatic placement would be ambiguous"
    )


def execute_section_plan(doc: Document, plan: Mapping[str, Any], *, alignment: str | None = None) -> dict[str, Any]:
    """Apply an explicit SectionPlan.  No format-spec interpretation occurs here."""
    if plan.get("valid") is not True:
        raise ValueError("refusing to execute an invalid SectionPlan")
    expected = plan.get("sections")
    if not isinstance(expected, list) or len(expected) != len(doc.sections):
        raise ValueError("SectionPlan section count does not match the document")
    if alignment is not None and alignment not in ALIGNMENTS:
        raise ValueError(f"unsupported page-number alignment: {alignment}")
    preserve_existing = plan.get("page_properties_policy") == "preserve_existing"

    even_odd_values = {bool(section.get("evenAndOdd")) for section in expected}
    if len(even_odd_values) != 1:
        raise ValueError("SectionPlan evenAndOdd must be document-global and consistent")
    doc.settings.odd_and_even_pages_header_footer = even_odd_values.pop()

    stories: list[dict[str, Any]] = []
    # Clone only genuinely shared explicit parts.  Cloning every section on
    # every pass grows the package and breaks byte-level idempotence.
    seen_footer_parts: set[str] = set()
    for index, (section, section_plan) in enumerate(zip(doc.sections, expected), 1):
        if section_plan.get("section_index") != index:
            raise ValueError("SectionPlan sections are not in canonical order")
        section.different_first_page_header_footer = bool(section_plan.get("titlePg"))
        if preserve_existing:
            # The official DOCX already contains the authoritative pgNumType,
            # footer relationships, and PAGE fields.  Do not unlink stories or
            # normalize them: python-docx cannot represent this template's
            # two PAGE fields per active footer without losing OOXML.
            for variant in VARIANTS:
                expectation = section_plan["stories"]["footer"][variant]
                stories.append({
                    "section": index,
                    "variant": variant,
                    "active": bool(expectation.get("active")),
                    "required_page_fields": int(expectation.get("required_page_fields", 0)),
                    "removed_page_fields": 0,
                    "preserved_existing": True,
                })
            continue
        _set_page_number_format(
            section,
            section_plan.get("ooxml_format") if section_plan.get("numbered") else None,
            section_plan.get("start") if section_plan.get("restart") else None,
        )
        for variant in VARIANTS:
            story = _story(section, "footer", variant)
            part_key = str(story.part.partname)
            force_clone = not story.is_linked_to_previous and part_key in seen_footer_parts
            unlink_story_preserving_content(story, force_clone=force_clone)
            seen_footer_parts.add(str(story.part.partname))
            paragraphs = all_story_paragraphs(story)
            field_paragraphs: list[Paragraph] = []
            removed = 0
            for paragraph in paragraphs:
                count = remove_page_fields(paragraph)
                if count:
                    field_paragraphs.append(paragraph)
                    removed += count
            expectation = section_plan["stories"]["footer"][variant]
            required = int(expectation.get("required_page_fields", 0)) if expectation.get("active") else 0
            if required not in {0, 1}:
                raise ValueError("SectionPlan executor supports at most one PAGE field per footer story")
            if required:
                target = _placement_paragraph(story, field_paragraphs, index, variant)
                switch = expectation.get("expected_page_switch")
                if switch not in set(FORMAT_TO_SWITCH.values()):
                    raise ValueError(f"section {index} {variant} footer has no supported PAGE switch")
                add_page_field(target, switch)
                if alignment is not None:
                    target.alignment = ALIGNMENTS[alignment]
            stories.append({
                "section": index,
                "variant": variant,
                "active": bool(expectation.get("active")),
                "required_page_fields": required,
                "removed_page_fields": removed,
            })
    reordered = normalize_section_property_order(doc)
    return {
        "schema_version": "1.0",
        "status": "applied",
        "section_count": len(expected),
        "front_matter_start_section": plan.get("front_matter_start_section"),
        "body_start_section": plan.get("body_start_section"),
        "stories": stories,
        "section_properties_reordered": reordered,
    }
