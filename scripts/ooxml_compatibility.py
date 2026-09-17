#!/usr/bin/env python3
"""Static, fail-closed WPS/Microsoft Word OOXML compatibility audit.

The audit never opens an office application.  It inspects package members,
relationships, field codes, drawing placement, fonts, and section page setup.
A parse/read error is a blocking finding rather than an implicit pass.
"""
from __future__ import annotations

import argparse
import hashlib
import posixpath
import json
import re
import sys
import zipfile
from pathlib import Path
from typing import Any

from lxml import etree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
V = "urn:schemas-microsoft-com:vml"
O = "urn:schemas-microsoft-com:office:office"
NS = {"w": W, "r": R, "rel": REL, "wp": WP, "v": V, "o": O}
FIELD_NAME = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_]*)")
WPS_MARKERS = ("www.wps.cn", "wpscustomdata")
MACRO_PART_MARKERS = ("vbaproject", "vbadata", "activex", "macrosheet")
OBJECT_REL_TYPES = {"oleobject", "package", "control", "attachedtemplate"}
DEFAULT_REQUIRED_FIELDS = ("PAGE", "TOC", "STYLEREF")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _finding(code: str, category: str, detail: str, **evidence: Any) -> dict[str, Any]:
    result = {"code": code, "category": category, "severity": "error", "detail": detail}
    if evidence:
        result["evidence"] = evidence
    return result


def _xml_parts(members: dict[str, bytes]) -> list[str]:
    return sorted(name for name in members if name.endswith((".xml", ".rels")))


def _field_codes(root: etree._Element) -> list[str]:
    values = [str(value) for value in root.xpath("//w:instrText/text()", namespaces=NS)]
    values.extend(str(value) for value in root.xpath("//w:fldSimple/@w:instr", namespaces=NS))
    return values


def _relationship_source_dir(rels_part: str) -> str:
    if rels_part == '_rels/.rels':
        return ''
    marker = '/_rels/'
    if marker not in rels_part:
        return posixpath.dirname(rels_part)
    return rels_part.split(marker, 1)[0]


def _relationship_target_part(rels_part: str, target: str) -> str:
    target = target.split('#', 1)[0]
    if target.startswith('/'):
        return posixpath.normpath(target.lstrip('/'))
    return posixpath.normpath(posixpath.join(_relationship_source_dir(rels_part), target))


def _relationship_findings(part: str, root: etree._Element, members: set[str]) -> list[dict[str, Any]]:
    findings = []
    relationship_ids: set[str] = set()
    for rel in root.xpath("/*[local-name()='Relationships']/*[local-name()='Relationship']"):
        rel_type = str(rel.get("Type", ""))
        target = str(rel.get("Target", ""))
        mode = str(rel.get("TargetMode", ""))
        evidence = {"part": part, "id": rel.get("Id"), "type": rel_type, "target": target}
        rel_id = rel.get('Id')
        if rel_id:
            if rel_id in relationship_ids:
                findings.append(_finding('duplicate_relationship_id', 'relationships',
                                         'Relationship Id is duplicated within one .rels part.', **evidence))
            relationship_ids.add(rel_id)
        lowered = rel_type.lower()
        relationship_kind = lowered.rstrip("/").rsplit("/", 1)[-1]
        if mode.lower() == "external" and relationship_kind != "hyperlink":
            findings.append(_finding("external_relationship", "external_objects",
                                     "External object/package relationships are not allowed in the compatibility edition.",
                                     **evidence))
        if relationship_kind in OBJECT_REL_TYPES:
            findings.append(_finding("embedded_or_linked_object_relationship", "objects",
                                     "Embedded/linked object or control relationship found.", **evidence))
        if any(marker in lowered or marker in target.lower() for marker in MACRO_PART_MARKERS):
            findings.append(_finding("macro_or_activex_relationship", "macros_activex",
                                     "Macro or ActiveX relationship found.", **evidence))
        if mode.lower() != 'external':
            resolved_target = _relationship_target_part(part, target)
            if resolved_target not in members:
                findings.append(_finding(
                    'missing_relationship_target', 'relationships',
                    'Internal relationship target is absent from the package.',
                    resolved_target=resolved_target, **evidence,
                ))
    return findings


def audit_docx(docx: Path, *, required_fields: tuple[str, ...] = ()) -> dict[str, Any]:
    docx = docx.resolve()
    findings: list[dict[str, Any]] = []
    evidence: dict[str, Any] = {
        "package_parts": [], "field_counts": {}, "fonts": {}, "sections": [],
        "floating_objects": [], "wps_parts": [],
    }
    try:
        with zipfile.ZipFile(docx) as archive:
            bad_member = archive.testzip()
            if bad_member:
                findings.append(_finding("zip_member_crc_failure", "package", "ZIP member CRC check failed.",
                                         part=bad_member))
            names = archive.namelist()
            if len(names) != len(set(names)):
                findings.append(_finding('duplicate_package_member', 'package',
                                         'ZIP package contains duplicate member names.'))
            members = {name: archive.read(name) for name in names}
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        findings.append(_finding("docx_package_unreadable", "package", str(exc)))
        return _result(docx, findings, evidence)

    evidence["package_parts"] = sorted(members)
    required_package_parts = {"[Content_Types].xml", "_rels/.rels", "word/document.xml", "word/styles.xml"}
    for missing in sorted(required_package_parts - set(members)):
        findings.append(_finding("required_package_part_missing", "package", "Required DOCX part is missing.",
                                 part=missing))

    lowered_parts = {name.lower(): name for name in members}
    for lowered, original in lowered_parts.items():
        if any(marker in lowered for marker in MACRO_PART_MARKERS):
            findings.append(_finding("macro_or_activex_part", "macros_activex",
                                     "Macro or ActiveX package part found.", part=original))
        if any(marker in lowered for marker in WPS_MARKERS):
            evidence["wps_parts"].append(original)
            findings.append(_finding("wps_proprietary_part", "wps_extensions",
                                     "WPS-specific package part found.", part=original))

    parsed: dict[str, etree._Element] = {}
    for part in _xml_parts(members):
        try:
            root = etree.fromstring(members[part])
            parsed[part] = root
        except etree.XMLSyntaxError as exc:
            findings.append(_finding("xml_part_parse_failed", "package", str(exc), part=part))
            continue
        raw_lower = members[part].lower()
        wps_elements = []
        for element in root.iter():
            namespace = etree.QName(element).namespace or ""
            if any(marker in namespace.lower() for marker in WPS_MARKERS):
                wps_elements.append(root.getroottree().getpath(element))
        if wps_elements:
            evidence["wps_parts"].append(part)
            findings.append(_finding("wps_proprietary_xml", "wps_extensions",
                                     "WPS-specific XML elements found.", part=part, paths=wps_elements[:20]))
        if part.endswith(".rels"):
            findings.extend(_relationship_findings(part, root, set(members)))
        if b"macroenabled" in raw_lower or b"vbaProject".lower() in raw_lower:
            findings.append(_finding("macro_enabled_content_type", "macros_activex",
                                     "Macro-enabled content type found.", part=part))

    word_parts = [name for name in parsed if name == "word/document.xml" or
                  re.fullmatch(r"word/(?:header|footer)\d+\.xml", name)]
    all_fields: list[tuple[str, str]] = []
    for part in word_parts:
        root = parsed[part]
        for code in _field_codes(root):
            all_fields.append((part, code))
            match = FIELD_NAME.match(code)
            name = match.group(1).upper() if match else "UNKNOWN"
            evidence["field_counts"][name] = evidence["field_counts"].get(name, 0) + 1
            if name == "MACROBUTTON":
                findings.append(_finding("macrobutton_field", "fields",
                                         "MACROBUTTON field remains in the document.", part=part,
                                         instruction=code.strip()[:240]))
            if (name in {"INCLUDEPICTURE", "INCLUDETEXT", "LINK", "DDE", "DDEAUTO"}
                    and re.search(r"(?:https?://|file:|\\\\)", code, re.I)):
                findings.append(_finding("external_link_field", "external_objects",
                                         "Field instruction references an external resource.", part=part,
                                         instruction=code.strip()[:240]))
        anchors = root.xpath("//wp:anchor", namespaces=NS)
        picts = root.xpath("//w:object | //w:pict[.//o:OLEObject]", namespaces=NS)
        for node in anchors:
            item = {"part": part, "path": root.getroottree().getpath(node)}
            evidence["floating_objects"].append(item)
            findings.append(_finding("floating_drawing", "objects",
                                     "Floating drawing found; inline drawings are required.", **item))
        for node in picts:
            findings.append(_finding("legacy_or_embedded_object", "objects",
                                     "Legacy VML/OLE object found.", part=part,
                                     path=root.getroottree().getpath(node)))

    for required in required_fields:
        count = evidence["field_counts"].get(required.upper(), 0)
        if count == 0:
            findings.append(_finding("required_standard_field_missing", "fields",
                                     "Required standard Word field is missing.", field=required.upper()))

    font_root = parsed.get("word/fontTable.xml")
    declared = set(font_root.xpath("//w:font/@w:name", namespaces=NS)) if font_root is not None else set()
    referenced: set[str] = set()
    for part, root in parsed.items():
        if part.startswith("word/") and part.endswith(".xml"):
            for attr in ("ascii", "hAnsi", "eastAsia", "cs"):
                referenced.update(root.xpath(f"//w:rFonts/@w:{attr}", namespaces=NS))
    missing_fonts = sorted(font for font in referenced if font and not font.startswith("+") and font not in declared)
    evidence["fonts"] = {"declared": sorted(declared), "referenced": sorted(referenced),
                         "referenced_but_undeclared": missing_fonts}
    if font_root is None:
        findings.append(_finding("font_table_missing", "fonts", "word/fontTable.xml is missing."))
    for font in missing_fonts:
        findings.append(_finding("referenced_font_undeclared", "fonts",
                                 "Referenced font is absent from the font table.", font=font))

    document = parsed.get("word/document.xml")
    if document is not None:
        sections = document.xpath("//w:sectPr", namespaces=NS)
        if not sections:
            findings.append(_finding("section_properties_missing", "page_setup",
                                     "No section properties were found."))
        for index, section in enumerate(sections):
            sizes = section.xpath("./w:pgSz", namespaces=NS)
            margins = section.xpath("./w:pgMar", namespaces=NS)
            item = {"index": index, "page_size": dict(sizes[0].attrib) if sizes else None,
                    "margins": dict(margins[0].attrib) if margins else None}
            evidence["sections"].append(item)
            if len(sizes) != 1 or len(margins) != 1:
                findings.append(_finding("section_page_setup_incomplete", "page_setup",
                                         "Every section must declare exactly one page size and margin set.",
                                         section=index, page_size_count=len(sizes), margin_count=len(margins)))
            elif any(margins[0].get(f"{{{W}}}{key}") is None for key in ("top", "right", "bottom", "left")):
                findings.append(_finding("section_margin_incomplete", "page_setup",
                                         "Section margin set lacks a required edge.", section=index))

    content_types = parsed.get('[Content_Types].xml')
    if content_types is not None:
        for override in content_types.xpath("//*[local-name()='Override']"):
            part_name = str(override.get('PartName', ''))
            resolved = part_name.lstrip('/')
            if resolved and resolved not in members:
                findings.append(_finding(
                    'missing_content_type_part', 'relationships',
                    'Content type override points to an absent package part.',
                    part=resolved, content_type=override.get('ContentType'),
                ))

    return _result(docx, findings, evidence)


def _result(docx: Path, findings: list[dict[str, Any]], evidence: dict[str, Any]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding["category"]] = counts.get(finding["category"], 0) + 1
    return {
        "schema_version": "1.0", "audit_type": "static_ooxml_wps_word_compatibility",
        "artifact": str(docx), "artifact_sha256": sha256(docx) if docx.is_file() else None,
        "compatible": not findings, "failure_mode": "closed", "finding_count": len(findings),
        "finding_counts_by_category": counts, "findings": findings, "evidence": evidence,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("docx", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--require-field", action="append", default=[], metavar="NAME")
    parser.add_argument("--require-template-fields", action="store_true",
                        help="require PAGE, TOC, and STYLEREF fields")
    args = parser.parse_args(argv)
    required = tuple(args.require_field) + (DEFAULT_REQUIRED_FIELDS if args.require_template_fields else ())
    result = audit_docx(args.docx, required_fields=required)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["compatible"] else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
