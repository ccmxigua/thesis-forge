"""Pure OOXML inspection helpers for section stories and PAGE fields.

This module deliberately does not mutate a DOCX.  It exposes a normalized,
JSON-serializable view that can be shared by format application, repair, and
submission audit code without depending on python-docx object identity.
"""
from __future__ import annotations

import hashlib
import json
import posixpath
import re
import zipfile
from pathlib import Path
from typing import Any, Iterator

from lxml import etree
from pipeline_finding import evidence as finding_evidence
from pipeline_finding import finding as pipeline_finding

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"w": W_NS, "r": R_NS}
W = f"{{{W_NS}}}"
R = f"{{{R_NS}}}"

VARIANTS = ("default", "first", "even")
STORY_KINDS = ("header", "footer")
FORMAT_TO_SWITCH = {
    "roman": "roman",
    "roman_upper": "ROMAN",
    "decimal": "ARABIC",
}
OOXML_TO_FORMAT = {
    "lowerRoman": "roman",
    "upperRoman": "roman_upper",
    "decimal": "decimal",
}


def finding(code: str, message: str, *, location: str, expected: Any = None,
            actual: Any = None, severity: str = "error", evidence: Any = None) -> dict[str, Any]:
    """Create a stable pipeline finding for section/page-field inspection."""
    items = [finding_evidence("location", location),
             finding_evidence("expected", expected),
             finding_evidence("actual", actual)]
    if evidence is not None:
        items.append(finding_evidence("details", evidence))
    return pipeline_finding(code, "section_plan", severity, severity == "error", message, items)


def _resolve_target(source_part: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(source_part), target))


def _rels_name(source_part: str) -> str:
    directory, name = posixpath.split(source_part)
    return posixpath.join(directory, "_rels", name + ".rels")


def _relationship_map(zf: zipfile.ZipFile, source_part: str) -> dict[str, str]:
    rel_name = _rels_name(source_part)
    if rel_name not in zf.namelist():
        return {}
    root = etree.fromstring(zf.read(rel_name))
    return {
        rel.get("Id", ""): _resolve_target(source_part, rel.get("Target", ""))
        for rel in root.findall(f"{{{PKG_REL_NS}}}Relationship")
        if rel.get("TargetMode") != "External"
    }


def story_relationship_references(root: etree._Element) -> Iterator[tuple[etree._Element, str, str]]:
    """Yield part-local relationship attributes, including legacy VML images."""
    legacy_relid = "{urn:schemas-microsoft-com:office:office}relid"
    for element in root.iter():
        for attribute, relationship_id in element.attrib.items():
            if attribute.startswith(R) or attribute == legacy_relid:
                yield element, attribute, relationship_id


def _story_relationship_findings(zf: zipfile.ZipFile, root: etree._Element,
                                 part: str) -> list[dict[str, Any]]:
    """Check serialized story references without fetching external targets."""
    rels_name = _rels_name(part)
    relationships = {}
    duplicates = set()
    names = set(zf.namelist())
    if rels_name in names:
        for relation in etree.fromstring(zf.read(rels_name)).findall(f"{{{PKG_REL_NS}}}Relationship"):
            rid = relation.get("Id")
            if rid in relationships:
                duplicates.add(rid)
            relationships[rid] = relation
    findings = []
    for rid in sorted({rid for _, _, rid in story_relationship_references(root)}):
        relation = relationships.get(rid)
        target = relation.get("Target") if relation is not None else None
        external = relation is not None and relation.get("TargetMode") == "External"
        resolved = _resolve_target(part, target) if target and not external else target
        if (rid and rid not in duplicates and target and relation.get("Type")
                and (external or resolved in names)):
            continue
        findings.append(finding(
            "broken_story_content_relationship",
            "Header/footer content references a missing, ambiguous or dangling relationship.",
            location=part, expected="unique relationship with an existing target",
            actual={"relationship_id": rid, "target": resolved, "external": external},
        ))
    return findings



def _semantic_element_path(element: etree._Element) -> list[tuple[str, int]]:
    """A prefix-independent location unaffected by font/paragraph properties."""
    path = []
    while element is not None:
        index = 1 + sum(sibling.tag == element.tag for sibling in element.itersiblings(preceding=True))
        path.append((element.tag, index))
        element = element.getparent()
    return list(reversed(path))


def _integrity_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def _story_integrity(zf: zipfile.ZipFile, root: etree._Element | None,
                     part: str | None) -> dict[str, Any]:
    """Bind field-code markup and directly referenced targets, not typography.

    Cached field results are not field instructions. Relationship IDs may be
    remapped during safe isolation, but the reference location, type, target
    path/URL and internal target bytes must remain identical. External targets
    are never fetched. This does not attest arbitrary transitive package data.
    """
    fields, references = [], []
    if root is not None and part:
        fields = [
            {"path": _semantic_element_path(node), "attributes": dict(node.attrib),
             "text": node.text if node.tag == f"{W}instrText" else None}
            for node in root.iter()
            if node.tag in {f"{W}fldSimple", f"{W}fldChar", f"{W}instrText"}
        ]
        names = set(zf.namelist())
        rels_name = _rels_name(part)
        relationships = ({node.get("Id"): node for node in etree.fromstring(zf.read(rels_name))
                          .findall(f"{{{PKG_REL_NS}}}Relationship")}
                         if rels_name in names else {})
        for node, attribute, rid in story_relationship_references(root):
            relation = relationships.get(rid)
            target = relation.get("Target") if relation is not None else None
            external = relation is not None and relation.get("TargetMode") == "External"
            resolved = _resolve_target(part, target) if target and not external else target
            references.append({
                "path": _semantic_element_path(node), "attribute": attribute,
                "type": relation.get("Type") if relation is not None else None,
                "external": external, "target": resolved,
                "target_sha256": (hashlib.sha256(zf.read(resolved)).hexdigest()
                                  if not external and resolved in names else None),
            })
    return {"present": root is not None,
            "field_markup_sha256": _integrity_digest(fields),
            "relationship_targets_sha256": _integrity_digest(references)}


def parse_field_instruction(instruction: str) -> dict[str, Any]:
    """Parse an instruction sufficiently to audit PAGE and its format switch.

    The raw instruction is retained.  PAGE is recognized only as the first
    opcode, so PAGEREF/NUMPAGES and visible text containing PAGE are excluded.
    """
    raw = instruction or ""
    opcode_match = re.match(r"\s*([A-Za-z]+)\b", raw)
    opcode = opcode_match.group(1).upper() if opcode_match else None
    switches = re.findall(r"\\\*\s+(\S+)", raw)
    format_switch = switches[-1] if switches else None
    return {
        "instruction": raw,
        "opcode": opcode,
        "switches": switches,
        "format_switch": format_switch,
        "is_page": opcode == "PAGE",
    }


def _field_instructions(root: etree._Element) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    fields: list[dict[str, Any]] = []
    malformed: list[dict[str, Any]] = []
    consumed_instr_nodes: set[etree._Element] = set()

    for node in root.xpath(".//w:fldSimple", namespaces=NS):
        parsed = parse_field_instruction(node.get(f"{W}instr", ""))
        parsed["representation"] = "simple"
        fields.append(parsed)

    stack: list[dict[str, Any]] = []
    for node in root.iter():
        if node.tag == f"{W}fldChar":
            kind = node.get(f"{W}fldCharType")
            if kind == "begin":
                stack.append({"parts": [], "separated": False})
            elif kind == "separate" and stack:
                stack[-1]["separated"] = True
            elif kind == "end":
                if not stack:
                    malformed.append({"reason": "orphan_field_end"})
                    continue
                state = stack.pop()
                parsed = parse_field_instruction("".join(state["parts"]))
                parsed["representation"] = "complex"
                fields.append(parsed)
        elif node.tag == f"{W}instrText" and stack and not stack[-1]["separated"]:
            stack[-1]["parts"].append(node.text or "")
            consumed_instr_nodes.add(node)

    for state in stack:
        malformed.append({"reason": "unterminated_complex_field", "instruction": "".join(state["parts"])})
    for node in root.xpath(".//w:instrText", namespaces=NS):
        if node not in consumed_instr_nodes:
            parsed = parse_field_instruction(node.text or "")
            if parsed["opcode"]:
                malformed.append({"reason": "orphan_instruction_text", "instruction": node.text or ""})
    return fields, malformed


def inspect_story(root: etree._Element, *, part: str, kind: str) -> dict[str, Any]:
    fields, malformed = _field_instructions(root)
    return {
        "part": part,
        "kind": kind,
        "page_fields": [field for field in fields if field["is_page"]],
        "fields": fields,
        "malformed_fields": malformed,
        "paragraph_count": len(root.xpath(".//w:p", namespaces=NS)),
    }


def _style_heading_ids(styles: etree._Element | None) -> set[str]:
    if styles is None:
        return set()
    heading_ids: set[str] = set()
    parents: dict[str, str] = {}
    names: dict[str, str] = {}
    outlines: dict[str, str] = {}
    for style in styles.xpath("./w:style[@w:type='paragraph']", namespaces=NS):
        style_id = style.get(f"{W}styleId", "")
        name = style.find("w:name", namespaces=NS)
        based = style.find("w:basedOn", namespaces=NS)
        outline = style.find("w:pPr/w:outlineLvl", namespaces=NS)
        names[style_id] = name.get(f"{W}val", "") if name is not None else ""
        if based is not None:
            parents[style_id] = based.get(f"{W}val", "")
        if outline is not None:
            outlines[style_id] = outline.get(f"{W}val", "")
    def normalized(value: str) -> str:
        return re.sub(r"[\s_-]", "", value).casefold()
    for style_id in names:
        lineage: set[str] = set()
        current = style_id
        while current and current not in lineage:
            lineage.add(current)
            if outlines.get(current) == "0" or normalized(current) in {"heading1", "标题1"} or normalized(names.get(current, "")) in {"heading1", "标题1"}:
                heading_ids.add(style_id)
                break
            current = parents.get(current, "")
    return heading_ids


def _style_toc_ids(styles: etree._Element | None) -> set[str]:
    """Recognize explicit TOC paragraph styles, including basedOn inheritance.

    Do not infer TOC membership from text, page-number suffixes or _Toc
    bookmarks: genuine chapter headings can have all of those properties.
    Unknown/unstyled duplicates therefore continue to fail closed.
    """
    if styles is None:
        return set()
    names, parents = {}, {}
    for style in styles.xpath("./w:style[@w:type='paragraph']", namespaces=NS):
        sid = style.get(f"{W}styleId", "")
        name = style.find("w:name", namespaces=NS)
        based = style.find("w:basedOn", namespaces=NS)
        names[sid] = name.get(f"{W}val", "") if name is not None else ""
        parents[sid] = based.get(f"{W}val", "") if based is not None else ""
    result = set()
    for sid in names:
        current, seen = sid, set()
        while current and current not in seen:
            seen.add(current)
            if re.fullmatch(r"toc\s+[1-9]", names.get(current, "").strip(), re.IGNORECASE):
                result.add(sid)
                break
            current = parents.get(current, "")
    return result


def inspect_docx(docx_path: str | Path) -> dict[str, Any]:
    """Return normalized section, inheritance, heading, and field evidence."""
    path = Path(docx_path)
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        if "word/document.xml" not in names:
            raise ValueError("DOCX is missing word/document.xml")
        document = etree.fromstring(zf.read("word/document.xml"))
        if etree.QName(document).namespace != W_NS:
            raise ValueError("unsupported WordprocessingML namespace")
        styles = etree.fromstring(zf.read("word/styles.xml")) if "word/styles.xml" in names else None
        settings = etree.fromstring(zf.read("word/settings.xml")) if "word/settings.xml" in names else None
        even_and_odd = bool(settings is not None and settings.find("w:evenAndOddHeaders", namespaces=NS) is not None)
        rels = _relationship_map(zf, "word/document.xml")
        heading_ids = _style_heading_ids(styles)
        toc_ids = _style_toc_ids(styles)

        story_cache: dict[tuple[str, str], dict[str, Any]] = {}
        package_findings: list[dict[str, Any]] = []
        inherited: dict[str, dict[str, str | None]] = {
            kind: {variant: None for variant in VARIANTS} for kind in STORY_KINDS
        }
        sections: list[dict[str, Any]] = []
        sectprs = document.xpath("//w:body//w:sectPr", namespaces=NS)
        for section_index, sectpr in enumerate(sectprs, 1):
            pg = sectpr.find("w:pgNumType", namespaces=NS)
            ooxml_format = pg.get(f"{W}fmt") if pg is not None else None
            start_raw = pg.get(f"{W}start") if pg is not None else None
            section: dict[str, Any] = {
                "index": section_index,
                "page_number": {
                    "ooxml_format": ooxml_format,
                    "format": OOXML_TO_FORMAT.get(ooxml_format, ooxml_format),
                    "start": int(start_raw) if start_raw and start_raw.isdigit() else start_raw,
                    "restart": start_raw is not None,
                },
                "titlePg": sectpr.find("w:titlePg", namespaces=NS) is not None,
                "evenAndOdd": even_and_odd,
                "stories": {kind: {} for kind in STORY_KINDS},
            }
            for kind in STORY_KINDS:
                explicit: dict[str, etree._Element] = {}
                for ref in sectpr.findall(f"w:{kind}Reference", namespaces=NS):
                    explicit[ref.get(f"{W}type", "default")] = ref
                for variant in VARIANTS:
                    ref = explicit.get(variant)
                    linked = ref is None and inherited[kind][variant] is not None
                    rid = ref.get(f"{R}id") if ref is not None else None
                    part = rels.get(rid or "") if ref is not None else inherited[kind][variant]
                    broken = bool(ref is not None and (not part or part not in names))
                    if ref is not None and not broken:
                        inherited[kind][variant] = part
                    active = variant == "default" or (variant == "first" and section["titlePg"]) or (variant == "even" and even_and_odd)
                    story: dict[str, Any] = {
                        "variant": variant,
                        "active": active,
                        "explicit": ref is not None,
                        "linked_to_previous": linked,
                        "relationship_id": rid,
                        "part": part,
                        "broken_relationship": broken,
                        "page_fields": [],
                        "malformed_fields": [],
                        "integrity": _story_integrity(zf, None, None),
                    }
                    if broken:
                        package_findings.append(finding(
                            "broken_story_relationship",
                            f"Section {section_index} has a broken {variant} {kind} relationship.",
                            location=f"sections[{section_index}].{kind}.{variant}",
                            expected="valid internal relationship", actual={"relationship_id": rid, "part": part},
                        ))
                    elif part:
                        key = (part, kind)
                        if key not in story_cache:
                            root = etree.fromstring(zf.read(part))
                            story_cache[key] = inspect_story(root, part=part, kind=kind)
                            package_findings.extend(_story_relationship_findings(zf, root, part))
                            story_cache[key]["integrity"] = _story_integrity(zf, root, part)
                        story.update({
                            "page_fields": story_cache[key]["page_fields"],
                            "malformed_fields": story_cache[key]["malformed_fields"],
                            "integrity": story_cache[key]["integrity"],
                        })
                    section["stories"][kind][variant] = story
            sections.append(section)

        paragraphs: list[dict[str, Any]] = []
        current_section = 1
        body = document.find("w:body", namespaces=NS)
        for child in body if body is not None else ():
            if child.tag != f"{W}p":
                continue
            text = "".join(child.xpath(".//w:t/text()", namespaces=NS)).strip()
            style_node = child.find("w:pPr/w:pStyle", namespaces=NS)
            style_id = style_node.get(f"{W}val", "") if style_node is not None else ""
            direct_outline = child.find("w:pPr/w:outlineLvl", namespaces=NS)
            paragraphs.append({
                "text": text,
                "style_id": style_id,
                "is_toc_entry": style_id in toc_ids,
                "is_heading_1": style_id in heading_ids or bool(direct_outline is not None and direct_outline.get(f"{W}val") == "0"),
                "section_index": current_section,
            })
            if child.find("w:pPr/w:sectPr", namespaces=NS) is not None:
                current_section += 1

        return {
            "source": str(path),
            "section_count": len(sections),
            "evenAndOdd": even_and_odd,
            "sections": sections,
            "paragraphs": paragraphs,
            "stories": {part: value for (part, _), value in sorted(story_cache.items())},
            "findings": package_findings,
        }
