#!/usr/bin/env python3
"""Fail-closed import of DOCX body fragments into another OPC package.

The destination package remains authoritative.  This adapter copies selected
``w:body`` children from a source package and rewrites only dependencies that
are explicitly supported.  It currently supports style dependency closure,
embedded images, and external hyperlinks.  Source section properties are
rejected unless the caller explicitly chooses ``discard``; they are never
silently lost.

Footnote/endnote/comment stories, headers/footers, embedded objects, and
arbitrary relationship types deliberately remain unsupported.  Numbering is
imported with an explicit dependency closure and collision-safe id remapping.
Unsupported semantics still raise :class:`ValueError` before an output package
is written.
"""
from __future__ import annotations

import copy
import hashlib
import posixpath
import re
from pathlib import PurePosixPath
from typing import Any

from lxml import etree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
PIC = "http://schemas.openxmlformats.org/drawingml/2006/picture"
NS = {"w": W, "r": R, "rel": REL, "ct": CT, "wp": WP, "pic": PIC}

DOCUMENT_PART = "word/document.xml"
DOCUMENT_RELS = "word/_rels/document.xml.rels"
STYLES_PART = "word/styles.xml"
NUMBERING_PART = "word/numbering.xml"
SUPPORTED_RELATIONSHIP_KINDS = {"image", "hyperlink"}
STORY_REFERENCE_NAMES = {"footnoteReference", "endnoteReference", "commentReference"}
STYLE_REFERENCE_ELEMENTS = ("pStyle", "rStyle", "tblStyle")
STYLE_DEPENDENCY_ELEMENTS = ("basedOn", "next", "link")
IMPLICIT_BUILTIN_STYLE_DEPENDENCIES = {"TableNormal"}


def _relationship_kind(value: str) -> str:
    return value.rstrip("/").rsplit("/", 1)[-1].lower()


def _relationship_map(payload: bytes) -> dict[str, etree._Element]:
    root = etree.fromstring(payload)
    return {
        str(node.get("Id")): node
        for node in root.xpath("/*[local-name()='Relationships']/*[local-name()='Relationship']")
        if node.get("Id")
    }


def _next_relationship_id(root: etree._Element) -> str:
    used = {str(node.get("Id")) for node in root if node.get("Id")}
    number = 1
    while f"rId{number}" in used:
        number += 1
    return f"rId{number}"


def _resolve_target(source_part: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(source_part), target))


def _relative_target(source_part: str, target_part: str) -> str:
    return posixpath.relpath(target_part, posixpath.dirname(source_part))


def _unique_media_part(destination: dict[str, bytes], source_part: str, payload: bytes) -> str:
    suffix = PurePosixPath(source_part).suffix.lower() or ".bin"
    digest = hashlib.sha256(payload).hexdigest()[:16]
    base = f"word/media/import-{digest}{suffix}"
    if base not in destination or destination[base] == payload:
        return base
    number = 2
    while True:
        candidate = f"word/media/import-{digest}-{number}{suffix}"
        if candidate not in destination or destination[candidate] == payload:
            return candidate
        number += 1


def _content_type_for_part(source_members: dict[str, bytes], part: str) -> str | None:
    root = etree.fromstring(source_members["[Content_Types].xml"])
    absolute = "/" + part
    override = root.xpath("./ct:Override[@PartName=$name]", namespaces=NS, name=absolute)
    if override:
        return override[0].get("ContentType")
    extension = PurePosixPath(part).suffix.lstrip(".").lower()
    default = root.xpath("./ct:Default[translate(@Extension, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')=$ext]",
                         namespaces=NS, ext=extension)
    return default[0].get("ContentType") if default else None


def _ensure_content_type(destination: dict[str, bytes], source_members: dict[str, bytes],
                         source_part: str, destination_part: str) -> None:
    content_type = _content_type_for_part(source_members, source_part)
    if not content_type:
        raise ValueError(f"source content type is missing for relationship target: {source_part}")
    root = etree.fromstring(destination["[Content_Types].xml"])
    extension = PurePosixPath(destination_part).suffix.lstrip(".").lower()
    defaults = root.xpath("./ct:Default[translate(@Extension, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')=$ext]",
                          namespaces=NS, ext=extension)
    if defaults:
        if defaults[0].get("ContentType") != content_type:
            raise ValueError(f"content type collision for extension .{extension}")
    else:
        etree.SubElement(root, f"{{{CT}}}Default", Extension=extension, ContentType=content_type)
    destination["[Content_Types].xml"] = etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", standalone=True
    )


def _strip_or_reject_sections(children: list[etree._Element], policy: str) -> int:
    sections = [node for child in children for node in child.xpath(".//w:sectPr | self::w:sectPr", namespaces=NS)]
    if not sections:
        return 0
    if policy == "reject":
        raise ValueError("assembly input body may not contain section properties")
    if policy != "discard":
        raise ValueError(f"unsupported source section policy: {policy!r}")
    for node in sections:
        parent = node.getparent()
        if parent is None:
            raise ValueError("source section property has no parent")
        parent.remove(node)
    return len(sections)


def _reject_unsupported_semantics(children: list[etree._Element]) -> None:
    story_refs = []
    for child in children:
        for node in child.xpath(".//w:footnoteReference | .//w:endnoteReference | .//w:commentReference", namespaces=NS):
            story_refs.append(etree.QName(node).localname)
    if story_refs:
        raise ValueError("assembly input contains unsupported story references: " + ", ".join(sorted(set(story_refs))))


def _numbering_map(payload: bytes) -> tuple[etree._Element, dict[str, etree._Element], dict[str, etree._Element]]:
    root = etree.fromstring(payload)
    abstract = {
        str(node.get(f"{{{W}}}abstractNumId")): node
        for node in root.xpath("./w:abstractNum[@w:abstractNumId]", namespaces=NS)
    }
    nums = {
        str(node.get(f"{{{W}}}numId")): node
        for node in root.xpath("./w:num[@w:numId]", namespaces=NS)
    }
    return root, abstract, nums


def _next_ooxml_id(used: set[int]) -> int:
    value = max(used, default=-1) + 1
    used.add(value)
    return value


def _import_numbering_closure(children: list[etree._Element], source_members: dict[str, bytes],
                              destination_members: dict[str, bytes]) -> list[dict[str, str]]:
    references = sorted({
        str(value) for child in children
        for value in child.xpath(".//w:numPr/w:numId/@w:val", namespaces=NS)
    })
    if not references:
        return []
    if NUMBERING_PART not in source_members:
        raise ValueError("assembly input references numbering but source DOCX lacks word/numbering.xml")
    if NUMBERING_PART not in destination_members:
        raise ValueError("assembly input references numbering but destination DOCX lacks word/numbering.xml")
    _, source_abstract, source_nums = _numbering_map(source_members[NUMBERING_PART])
    destination_root, destination_abstract, destination_nums = _numbering_map(destination_members[NUMBERING_PART])
    used_num_ids = {int(value) for value in destination_nums if value.lstrip("-").isdigit()}
    used_abstract_ids = {int(value) for value in destination_abstract if value.lstrip("-").isdigit()}
    num_map: dict[str, str] = {}
    abstract_map: dict[str, str] = {}
    imported: list[dict[str, str]] = []
    for source_num_id in references:
        source_num = source_nums.get(source_num_id)
        if source_num is None:
            raise ValueError(f"assembly input references undefined numbering instance: {source_num_id}")
        abstract_ref = source_num.find("w:abstractNumId", NS)
        source_abstract_id = abstract_ref.get(f"{{{W}}}val") if abstract_ref is not None else None
        if source_abstract_id is None or source_abstract_id not in source_abstract:
            raise ValueError(f"assembly numbering instance {source_num_id} lacks a defined abstract numbering rule")
        if source_abstract_id not in abstract_map:
            new_abstract_id = str(_next_ooxml_id(used_abstract_ids))
            imported_abstract = copy.deepcopy(source_abstract[source_abstract_id])
            imported_abstract.set(f"{{{W}}}abstractNumId", new_abstract_id)
            destination_root.append(imported_abstract)
            abstract_map[source_abstract_id] = new_abstract_id
        new_num_id = str(_next_ooxml_id(used_num_ids))
        imported_num = copy.deepcopy(source_num)
        imported_num.set(f"{{{W}}}numId", new_num_id)
        imported_num.find("w:abstractNumId", NS).set(
            f"{{{W}}}val", abstract_map[source_abstract_id]
        )
        destination_root.append(imported_num)
        num_map[source_num_id] = new_num_id
        imported.append({
            "source_num_id": source_num_id,
            "destination_num_id": new_num_id,
            "source_abstract_num_id": source_abstract_id,
            "destination_abstract_num_id": abstract_map[source_abstract_id],
        })
    for child in children:
        for node in child.xpath(".//w:numPr/w:numId", namespaces=NS):
            source_num_id = node.get(f"{{{W}}}val")
            if source_num_id in num_map:
                node.set(f"{{{W}}}val", num_map[source_num_id])
    destination_members[NUMBERING_PART] = etree.tostring(
        destination_root, xml_declaration=True, encoding="UTF-8", standalone=True
    )
    return imported


def _style_map(payload: bytes) -> tuple[etree._Element, dict[str, etree._Element]]:
    root = etree.fromstring(payload)
    return root, {
        str(node.get(f"{{{W}}}styleId")): node
        for node in root.xpath("./w:style[@w:styleId]", namespaces=NS)
    }


def _style_references(children: list[etree._Element]) -> set[str]:
    references: set[str] = set()
    expression = " | ".join(f".//w:{name}/@w:val" for name in STYLE_REFERENCE_ELEMENTS)
    for child in children:
        references.update(str(value) for value in child.xpath(expression, namespaces=NS) if value)
    return references


def _import_style_closure(children: list[etree._Element], source_members: dict[str, bytes],
                          destination_members: dict[str, bytes]) -> tuple[list[str], list[dict[str, str]]]:
    """Import missing styles without replacing destination-owned definitions."""
    if STYLES_PART not in source_members:
        raise ValueError("source DOCX lacks word/styles.xml required by imported content")
    if STYLES_PART not in destination_members:
        raise ValueError("destination DOCX lacks word/styles.xml required by imported content")
    source_root, source_styles = _style_map(source_members[STYLES_PART])
    destination_root, destination_styles = _style_map(destination_members[STYLES_PART])
    del source_root  # The map owns the source nodes for the duration of this call.

    pending = sorted(_style_references(children))
    visited: set[str] = set()
    to_import: list[str] = []
    normalized_dependencies: list[dict[str, str]] = []
    while pending:
        style_id = pending.pop(0)
        if style_id in visited:
            continue
        visited.add(style_id)
        if style_id in destination_styles:
            continue
        style = source_styles.get(style_id)
        if style is None:
            raise ValueError(f"assembly input references undefined style: {style_id}")
        if style.xpath(".//w:numPr/w:numId", namespaces=NS):
            raise ValueError(
                f"assembly style {style_id!r} contains numbering references; "
                "numbering import adapter does not support style-level numbering"
            )
        to_import.append(style_id)
        dependency_expression = " | ".join(
            f"./w:{name}/@w:val" for name in STYLE_DEPENDENCY_ELEMENTS
        )
        for value in style.xpath(dependency_expression, namespaces=NS):
            dependency = str(value)
            if not dependency or dependency in visited:
                continue
            if dependency not in source_styles and dependency not in destination_styles:
                if dependency not in IMPLICIT_BUILTIN_STYLE_DEPENDENCIES:
                    raise ValueError(
                        f"assembly style {style_id!r} references undefined style: {dependency}"
                    )
                normalized_dependencies.append({
                    "style_id": style_id,
                    "dependency": dependency,
                    "action": "remove_implicit_builtin_reference",
                })
                continue
            pending.append(dependency)
        pending.sort()

    existing_defaults = {
        str(node.get(f"{{{W}}}type"))
        for node in destination_root.xpath("./w:style[@w:default='1']", namespaces=NS)
        if node.get(f"{{{W}}}type")
    }
    for style_id in to_import:
        imported = copy.deepcopy(source_styles[style_id])
        for dependency in imported.xpath(
            "./w:basedOn | ./w:next | ./w:link", namespaces=NS
        ):
            value = dependency.get(f"{{{W}}}val")
            if value in IMPLICIT_BUILTIN_STYLE_DEPENDENCIES \
                    and value not in source_styles and value not in destination_styles:
                imported.remove(dependency)
        style_type = imported.get(f"{{{W}}}type")
        if imported.get(f"{{{W}}}default") == "1" and style_type in existing_defaults:
            imported.attrib.pop(f"{{{W}}}default", None)
        elif imported.get(f"{{{W}}}default") == "1" and style_type:
            existing_defaults.add(style_type)
        destination_root.append(imported)
    if to_import:
        destination_members[STYLES_PART] = etree.tostring(
            destination_root, xml_declaration=True, encoding="UTF-8", standalone=True
        )
    return to_import, normalized_dependencies


def _used_numeric_ids(root: etree._Element, xpath: str, attribute: str) -> set[int]:
    values: set[int] = set()
    for value in root.xpath(xpath, namespaces=NS):
        try:
            values.add(int(value))
        except (TypeError, ValueError):
            continue
    return values


def _remap_local_ids(children: list[etree._Element], destination_document: etree._Element) -> dict[str, int]:
    """Avoid collisions for document-scoped drawing and bookmark identifiers."""
    destination_docpr = _used_numeric_ids(destination_document, "//wp:docPr/@id", "id")
    destination_pic = _used_numeric_ids(destination_document, "//pic:cNvPr/@id", "id")
    destination_bookmarks = _used_numeric_ids(destination_document, "//w:bookmarkStart/@w:id | //w:bookmarkEnd/@w:id", "id")

    def next_id(used: set[int]) -> int:
        value = max(used, default=0) + 1
        used.add(value)
        return value

    counts = {"drawing_ids": 0, "picture_ids": 0, "bookmark_ids": 0}
    bookmark_map: dict[str, str] = {}
    for child in children:
        for node in child.xpath(".//wp:docPr", namespaces=NS):
            node.set("id", str(next_id(destination_docpr)))
            counts["drawing_ids"] += 1
        for node in child.xpath(".//pic:cNvPr", namespaces=NS):
            node.set("id", str(next_id(destination_pic)))
            counts["picture_ids"] += 1
        for node in child.xpath(".//w:bookmarkStart", namespaces=NS):
            old = node.get(f"{{{W}}}id")
            if old is None:
                continue
            new = str(next_id(destination_bookmarks))
            bookmark_map[old] = new
            node.set(f"{{{W}}}id", new)
            counts["bookmark_ids"] += 1
    for child in children:
        for node in child.xpath(".//w:bookmarkEnd", namespaces=NS):
            old = node.get(f"{{{W}}}id")
            if old in bookmark_map:
                node.set(f"{{{W}}}id", bookmark_map[old])
    return counts


def import_body_children(source_members: dict[str, bytes], destination_members: dict[str, bytes],
                         destination_document: etree._Element, *,
                         source_section_policy: str = "reject",
                         source_body_range: tuple[int, int] | None = None) -> tuple[list[etree._Element], dict[str, Any]]:
    """Return imported body children and mutate a destination-member copy.

    Callers should operate on private package dictionaries.  Any exception can
    therefore be handled atomically without touching the final output path.
    """
    required = {"[Content_Types].xml", DOCUMENT_PART, DOCUMENT_RELS, STYLES_PART}
    missing_source = sorted(required - set(source_members))
    missing_destination = sorted(required - set(destination_members))
    if missing_source:
        raise ValueError(f"source DOCX lacks required package members: {missing_source}")
    if missing_destination:
        raise ValueError(f"destination DOCX lacks required package members: {missing_destination}")

    source_document = etree.fromstring(source_members[DOCUMENT_PART])
    body = source_document.find("w:body", NS)
    if body is None:
        raise ValueError("assembly input has no body")
    raw_children = list(body)
    content_end = len(raw_children)
    if raw_children and raw_children[-1].tag == f"{{{W}}}sectPr":
        content_end -= 1
    if source_body_range is None:
        start, end = 0, content_end
    else:
        start, end = source_body_range
        if isinstance(start, bool) or isinstance(end, bool) \
                or not isinstance(start, int) or not isinstance(end, int):
            raise ValueError("source body range indices must be integers")
        if start < 0 or end > content_end or start >= end:
            raise ValueError(
                f"source body range is outside content bounds: [{start}, {end}) of [0, {content_end})"
            )
    children = [copy.deepcopy(child) for child in raw_children[start:end]]
    sections_discarded = _strip_or_reject_sections(children, source_section_policy)
    _reject_unsupported_semantics(children)
    imported_numbering = _import_numbering_closure(children, source_members, destination_members)
    imported_styles, normalized_style_dependencies = _import_style_closure(
        children, source_members, destination_members
    )

    source_relationships = _relationship_map(source_members[DOCUMENT_RELS])
    destination_rels_root = etree.fromstring(destination_members[DOCUMENT_RELS])
    remapped: dict[str, str] = {}
    imported_parts: list[str] = []
    imported_relationships: list[dict[str, Any]] = []

    for child in children:
        for node in child.iter():
            for key, old_rid in list(node.attrib.items()):
                if etree.QName(key).namespace != R:
                    continue
                relationship = source_relationships.get(old_rid)
                if relationship is None:
                    raise ValueError(f"assembly input has dangling relationship: {old_rid}")
                rel_type = str(relationship.get("Type", ""))
                kind = _relationship_kind(rel_type)
                if kind not in SUPPORTED_RELATIONSHIP_KINDS:
                    raise ValueError(f"unsupported assembly relationship type: {kind or rel_type}")
                if old_rid not in remapped:
                    target = str(relationship.get("Target", ""))
                    target_mode = str(relationship.get("TargetMode", ""))
                    new_rid = _next_relationship_id(destination_rels_root)
                    attributes = {"Id": new_rid, "Type": rel_type}
                    if kind == "hyperlink":
                        if target_mode.lower() != "external":
                            raise ValueError("only external hyperlink relationships are supported")
                        attributes.update({"Target": target, "TargetMode": "External"})
                    else:
                        if target_mode:
                            raise ValueError("external image relationships are not supported")
                        source_part = _resolve_target(DOCUMENT_PART, target)
                        if source_part not in source_members:
                            raise ValueError(f"relationship target is missing from source package: {source_part}")
                        payload = source_members[source_part]
                        destination_part = _unique_media_part(destination_members, source_part, payload)
                        destination_members[destination_part] = payload
                        _ensure_content_type(destination_members, source_members, source_part, destination_part)
                        attributes["Target"] = _relative_target(DOCUMENT_PART, destination_part)
                        imported_parts.append(destination_part)
                    etree.SubElement(destination_rels_root, f"{{{REL}}}Relationship", **attributes)
                    remapped[old_rid] = new_rid
                    imported_relationships.append({
                        "source_id": old_rid, "destination_id": new_rid,
                        "kind": kind, "target": attributes["Target"],
                    })
                node.set(key, remapped[old_rid])

    destination_members[DOCUMENT_RELS] = etree.tostring(
        destination_rels_root, xml_declaration=True, encoding="UTF-8", standalone=True
    )
    id_counts = _remap_local_ids(children, destination_document)
    return children, {
        "source_section_policy": source_section_policy,
        "source_body_range": {"start": start, "end": end, "policy": "half_open"},
        "source_sections_discarded": sections_discarded,
        "styles_imported": imported_styles,
        "numbering_imported": imported_numbering,
        "style_dependencies_normalized": normalized_style_dependencies,
        "relationships_imported": imported_relationships,
        "parts_imported": sorted(set(imported_parts)),
        "ids_remapped": id_counts,
    }
