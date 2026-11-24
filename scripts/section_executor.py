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
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.shared import Pt
from docx.text.paragraph import Paragraph
from docx.text.run import Run

from font_declarations import THEME_ATTRIBUTE, set_explicit_faces

from page_field_ops import FORMAT_TO_SWITCH, VARIANTS, parse_field_instruction, story_relationship_references

ALIGNMENTS = {
    "left": WD_ALIGN_PARAGRAPH.LEFT,
    "center": WD_ALIGN_PARAGRAPH.CENTER,
    "right": WD_ALIGN_PARAGRAPH.RIGHT,
}


def all_story_paragraphs(story) -> list[Paragraph]:
    """Read existing story paragraphs, including SDTs, without creating parts.

    python-docx's ``_element`` accessor creates a header/footer when neither
    this section nor its ancestors defines one.  Inventory and coverage calls
    must not change a document merely by reading it.
    """
    definition = story
    while definition.is_linked_to_previous:
        definition = definition._prior_headerfooter
        if definition is None:
            return []
    return [Paragraph(node, story) for node in definition._definition.element.xpath(".//w:p")]


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
    definition = story
    while definition.is_linked_to_previous:
        definition = definition._prior_headerfooter
        if definition is None:
            # Reading _element first would materialize a definition in an
            # earlier section. A missing inherited story belongs only here.
            story.is_linked_to_previous = False
            return
    source_part = definition._definition
    children = [copy.deepcopy(node) for node in source_part.element]
    references = [reference for child in children for reference in story_relationship_references(child)]
    relationships = {}
    # Validate copied relationships before changing section links. Other
    # payload types require a preservation contract instead of broken XML.
    for _, _, rid in references:
        relation = source_part.rels.get(rid)
        if relation is None:
            raise ValueError(f"source story has a missing content relationship: {rid!r}")
        if relation.reltype not in {RT.IMAGE, RT.HYPERLINK}:
            raise ValueError(f"unsupported story content relationship: {relation.reltype}")
        relationships[rid] = relation
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
    target_part = story._definition
    remapped = {
        rid: target_part.relate_to(
            relation.target_ref if relation.is_external else relation.target_part,
            relation.reltype, is_external=relation.is_external,
        )
        for rid, relation in relationships.items()
    }
    for element, attribute, rid in references:
        element.set(attribute, remapped[rid])
    target = story._element
    for node in list(target):
        target.remove(node)
    for node in children:
        target.append(node)


def _opcode(instruction: str) -> str | None:
    return parse_field_instruction(instruction).get("opcode")


def _page_field_runs(paragraph: Paragraph) -> list[list[Any]]:
    """Inventory complete PAGE fields owned by this paragraph, without writes."""
    fields: list[list[Any]] = []
    stack: list[dict[str, Any]] = []
    for node in paragraph._p.iter():
        if next(node.iterancestors(qn("w:p")), None) is not paragraph._p:
            continue
        if node.tag == qn("w:fldSimple"):
            if _opcode(node.get(qn("w:instr")) or "") == "PAGE":
                fields.append(list(node.iter(qn("w:r"))))
        elif node.tag == qn("w:r"):
            for state in stack:
                state["runs"].append(node)
        elif node.tag == qn("w:fldChar"):
            kind = node.get(qn("w:fldCharType"))
            if kind == "begin":
                stack.append({"instructions": [], "runs": [node.getparent()], "separated": False})
            elif kind == "separate" and stack:
                stack[-1]["separated"] = True
            elif kind == "end" and stack:
                state = stack.pop()
                if _opcode("".join(state["instructions"])) == "PAGE":
                    fields.append(state["runs"])
        elif node.tag == qn("w:instrText") and stack and not stack[-1]["separated"]:
            stack[-1]["instructions"].append(node.text or "")
    return fields


def _effective_page_rpr(paragraph: Paragraph, runs: list[Any]):
    """Snapshot run typography before PAGE replacement can remove its style context.

    Prefer the cached result over instruction/control runs. Resolve paragraph
    and character style chains, including script-specific font slots, so a
    page-only text box can be removed without reverting to Normal's font.
    """
    node = next((run for run in runs if run.find(qn("w:t")) is not None),
                next((run for run in runs if run.find(qn("w:rPr")) is not None),
                     runs[0] if runs else None))
    # An empty cached field still uses the default character style. The
    # detached placeholder resolves that same context without changing XML.
    run = Run(node if node is not None else OxmlElement("w:r"), paragraph)
    if next(paragraph._p.iterancestors(qn("w:tbl")), None) is not None:
        # Table/conditional styles are a separate cascade layer. Keep the
        # original direct properties when staying in the same cell instead of
        # freezing docDefaults over that still-authoritative table context.
        return copy.deepcopy(run._r.rPr) if run is not None and run._r.rPr is not None else OxmlElement("w:rPr")
    styles = paragraph.part.package.main_document_part.styles
    defaults = styles.element.find(qn("w:docDefaults") + "/" + qn("w:rPrDefault") + "/" + qn("w:rPr"))
    layers = [(defaults, False)]
    for initial in (paragraph.style, run.style if run is not None else None):
        chain, seen = [], set()
        style = initial
        while style is not None and style.style_id not in seen:
            seen.add(style.style_id)
            chain.append(style.element.rPr)
            style = style.base_style
        layers.extend((properties, True) for properties in reversed(chain))
    if run is not None:
        layers.append((run._r.rPr, False))
    result = OxmlElement("w:rPr")
    toggles = {qn("w:" + name) for name in (
        "b", "bCs", "i", "iCs", "caps", "smallCaps", "strike", "outline",
        "shadow", "emboss", "imprint", "vanish",
    )}
    for properties, is_style in layers:
        if properties is None:
            continue
        for child in properties:
            if child.tag in {qn("w:rStyle"), qn("w:rPrChange")}:
                continue
            old = result.find(child.tag)
            if child.tag == qn("w:rFonts"):
                if old is None:
                    old = OxmlElement("w:rFonts")
                    result.append(old)
                for slot, theme in THEME_ATTRIBUTE.items():
                    if child.get(qn("w:" + slot)) is not None or child.get(qn("w:" + theme)) is not None:
                        old.attrib.pop(qn("w:" + slot), None)
                        old.attrib.pop(qn("w:" + theme), None)
                old.attrib.update(child.attrib)
                continue
            replacement = copy.deepcopy(child)
            if is_style and child.tag in toggles:
                if child.get(qn("w:val"), "true") in {"0", "false", "off"}:
                    continue
                previous = old is not None and old.get(qn("w:val"), "true") not in {"0", "false", "off"}
                replacement.set(qn("w:val"), "0" if previous else "1")
            if old is not None:
                result.remove(old)
            result.append(replacement)
    return result


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


def _apply_page_font(run: Run, font: Mapping[str, Any]) -> None:
    """Apply only requested typography; never mutate paragraph layout or text."""
    if font.get("latin") or font.get("cjk"):
        fonts = run._r.get_or_add_rPr().get_or_add_rFonts()
        set_explicit_faces(fonts, latin=font.get("latin"), cjk=font.get("cjk"))
    if font.get("size_pt") is not None:
        run.font.size = Pt(float(font["size_pt"]))
        rpr = run._r.get_or_add_rPr()
        complex_size = rpr.find(qn("w:szCs"))
        if complex_size is None:
            complex_size = OxmlElement("w:szCs")
            rpr.append(complex_size)
        complex_size.set(qn("w:val"), rpr.sz.get(qn("w:val")))
    if font.get("bold") is not None:
        run.font.bold = bool(font["bold"])
        run.font.cs_bold = bool(font["bold"])
    if font.get("italic") is not None:
        run.font.italic = bool(font["italic"])
        run.font.cs_italic = bool(font["italic"])


def add_page_field(paragraph: Paragraph, format_switch: str, *, run_properties=None,
                   font: Mapping[str, Any] | None = None) -> None:
    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), f"PAGE \\* {format_switch}")
    run = OxmlElement("w:r")
    if run_properties is not None:
        run.append(copy.deepcopy(run_properties))
    text = OxmlElement("w:t")
    text.text = "1"
    run.append(text)
    field.append(run)
    paragraph._p.append(field)
    if font:
        _apply_page_font(Run(run, paragraph), font)


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


def execute_section_plan(doc: Document, plan: Mapping[str, Any], *, alignment: str | None = None,
                         footer_font: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Apply a SectionPlan, with an optional explicit font for active PAGE runs.

    ``footer_font`` accepts latin/cjk/size_pt/bold/italic, independently of
    paragraph layout. Without it, replacements retain source typography. A
    preserve-existing plan changes only requested font properties on existing
    active PAGE runs, without creating parts or unlinking inherited stories.
    No format-spec interpretation occurs here.
    """
    if plan.get("valid") is not True:
        raise ValueError("refusing to execute an invalid SectionPlan")
    expected = plan.get("sections")
    if not isinstance(expected, list) or len(expected) != len(doc.sections):
        raise ValueError("SectionPlan section count does not match the document")
    if alignment is not None and alignment not in ALIGNMENTS:
        raise ValueError(f"unsupported page-number alignment: {alignment}")
    preserve_existing = plan.get("page_properties_policy") == "preserve_existing"
    if preserve_existing and alignment is not None:
        raise ValueError("page-number alignment requires an explicit numbering format")

    even_odd_values = {bool(section.get("evenAndOdd")) for section in expected}
    if len(even_odd_values) != 1:
        raise ValueError("SectionPlan evenAndOdd must be document-global and consistent")

    # Snapshot before touching any section: removing PAGE on an unnumbered
    # cover must not erase the font inherited by a later numbered section.
    source_fonts: dict[tuple[int, str], dict[str, tuple[Any, str | None]]] = {}
    if not preserve_existing:
        for index, section in enumerate(doc.sections, 1):
            if expected[index - 1].get("page_properties_policy") == "preserve_existing":
                continue
            for variant in VARIANTS:
                properties = {}
                paragraphs = all_story_paragraphs(_story(section, "footer", variant))
                page_runs = set()
                for paragraph in paragraphs:
                    fields = _page_field_runs(paragraph)
                    if fields:
                        tree = paragraph._p.getroottree()
                        cell = next(paragraph._p.iterancestors(qn("w:tc")), None)
                        properties[tree.getpath(paragraph._p)] = (
                            _effective_page_rpr(paragraph, fields[0]),
                            tree.getpath(cell) if cell is not None else None,
                        )
                        page_runs.update(run for runs in fields for run in runs)
                expectation = expected[index - 1]["stories"]["footer"][variant]
                if (any(cell is not None for _, cell in properties.values())
                        and expectation.get("active") and expectation.get("required_page_fields")):
                    root = paragraphs[0]._p.getroottree().getroot()
                    non_page_text = "".join(node.text or "" for node in root.iter(qn("w:t"))
                                            if node.getparent() not in page_runs).strip()
                    if not non_page_text and root.xpath(".//w:drawing | .//w:pict | .//w:txbxContent"):
                        raise ValueError(
                            f"section {index} {variant} PAGE would leave its table context; "
                            "table-style typography is unresolved"
                        )
                source_fonts[index, variant] = properties

    doc.settings.odd_and_even_pages_header_footer = even_odd_values.pop()
    stories: list[dict[str, Any]] = []
    # Clone only genuinely shared explicit parts.  Cloning every section on
    # every pass grows the package and breaks byte-level idempotence.
    seen_footer_parts: set[str] = set()
    seen_font_runs: set[Any] = set()
    for index, (section, section_plan) in enumerate(zip(doc.sections, expected), 1):
        if section_plan.get("section_index") != index:
            raise ValueError("SectionPlan sections are not in canonical order")
        section.different_first_page_header_footer = bool(section_plan.get("titlePg"))
        if preserve_existing or section_plan.get("page_properties_policy") == "preserve_existing":
            # The official DOCX already contains the authoritative pgNumType,
            # footer relationships, and PAGE fields.  Do not unlink stories or
            # normalize them: python-docx cannot represent this template's
            # two PAGE fields per active footer without losing OOXML.
            for variant in VARIANTS:
                expectation = section_plan["stories"]["footer"][variant]
                # A later body section may explicitly share this part, so
                # preserved definitions also participate in clone isolation.
                for paragraph in all_story_paragraphs(_story(section, "footer", variant)):
                    seen_footer_parts.add(str(paragraph.part.partname))
                if footer_font and expectation.get("active"):
                    for paragraph in all_story_paragraphs(_story(section, "footer", variant)):
                        for runs in _page_field_runs(paragraph):
                            for run in runs:
                                if run not in seen_font_runs:
                                    _apply_page_font(Run(run, paragraph), footer_font)
                                    seen_font_runs.add(run)
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
            inherited = all_story_paragraphs(story)
            part_key = str(inherited[0].part.partname) if inherited else None
            force_clone = not story.is_linked_to_previous and part_key in seen_footer_parts
            unlink_story_preserving_content(story, force_clone=force_clone)
            seen_footer_parts.add(str(story.part.partname))
            paragraphs = all_story_paragraphs(story)
            field_paragraphs: list[Paragraph] = []
            source_properties = source_fonts[index, variant]
            removed = 0
            for paragraph in paragraphs:
                count = remove_page_fields(paragraph)
                if count or paragraph._p.getroottree().getpath(paragraph._p) in source_properties:
                    # A prior unnumbered section may already have removed an
                    # inherited PAGE. Keep its source paragraph/cell as the
                    # placement candidate after cloning the linked story.
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
                tree = target._p.getroottree()
                path = tree.getpath(target._p)
                source = path if path in source_properties else next(iter(source_properties), None)
                properties, source_cell = source_properties.get(source, (None, None))
                if properties is None:
                    # New fields must have the same resolved typography on the
                    # first and later passes, not gain docDefaults on rerun.
                    properties = _effective_page_rpr(target, [])
                target_cell = next(target._p.iterancestors(qn("w:tc")), None)
                if source_cell is not None and (target_cell is None or tree.getpath(target_cell) != source_cell):
                    raise ValueError(
                        f"section {index} {variant} PAGE would leave its table context; "
                        "table-style typography is unresolved"
                    )
                add_page_field(target, switch, run_properties=properties, font=footer_font)
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
