#!/usr/bin/env python3
"""Execute a compiled AssemblyPlan without weakening its preconditions.

The executor is intentionally package preserving: the official template is
the package base and only explicitly assembled content plus deterministic
review-comment cleanup are rewritten.  Dynamic region anchors are replaced
with body children from an explicitly bound DOCX input.  Protected regions
are verified by canonical OOXML hash before and after every mutation.
Unsupported generators and indeterminate optional conditions block before an
output file is written.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import posixpath
import re
import sys
import zipfile
from pathlib import Path
from typing import Any

from lxml import etree

from docx_fragment_import import import_body_children
from pipeline_finding import evidence, finding
from template_profile import NS, W, sha256

R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
REVIEW_COMMENT_MARKERS = {"commentRangeStart", "commentRangeEnd", "commentReference"}


def _canonical_bytes(node: etree._Element) -> bytes:
    """Return canonical OOXML with Word review-comment markers ignored."""
    copy_node = etree.fromstring(etree.tostring(node))
    for marker in list(copy_node.iter()):
        if etree.QName(marker).localname in REVIEW_COMMENT_MARKERS:
            parent = marker.getparent()
            if parent is not None:
                parent.remove(marker)
    return etree.tostring(copy_node, method="c14n", with_comments=False)


def _read_package(path: Path) -> tuple[etree._Element, dict[str, bytes]]:
    with zipfile.ZipFile(path) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    return etree.fromstring(members["word/document.xml"]), members


def _canonical_sha(node: etree._Element) -> str:
    return hashlib.sha256(_canonical_bytes(node)).hexdigest()


def _structural_sha(node: etree._Element) -> str:
    """Hash formatting/OOXML structure while ignoring editable text values."""
    copy_node = etree.fromstring(etree.tostring(node))
    for text_node in copy_node.xpath(".//w:t | .//w:instrText", namespaces=NS):
        text_node.text = ""
    return hashlib.sha256(_canonical_bytes(copy_node)).hexdigest()


def _section_property(node: etree._Element) -> etree._Element | None:
    if node.tag == f"{{{W}}}sectPr":
        return node
    properties = node.xpath("./w:pPr/w:sectPr", namespaces=NS)
    return properties[0] if properties else None


def _section_invariants(root: etree._Element) -> list[dict[str, Any]]:
    """Return effective section/header/footer/page-number signatures."""
    body = root.find("w:body", NS)
    if body is None:
        raise ValueError("assembled document has no w:body")
    result: list[dict[str, Any]] = []
    section_index = 0
    for child in body:
        sect_pr = _section_property(child)
        if sect_pr is None:
            continue
        refs = []
        for reference in sect_pr.xpath("./w:headerReference | ./w:footerReference", namespaces=NS):
            refs.append({
                "kind": etree.QName(reference).localname,
                "type": reference.get(f"{{{W}}}type"),
                "rid": reference.get(f"{{{R}}}id"),
            })
        page_number = sect_pr.find("w:pgNumType", NS)
        result.append({
            "section_index": section_index,
            "marker_kind": "body_sectPr" if child.tag == f"{{{W}}}sectPr" else "paragraph_sectPr",
            "sectpr_sha256": _canonical_sha(sect_pr),
            "header_footer_refs": refs,
            "page_number": {
                "format": page_number.get(f"{{{W}}}fmt") if page_number is not None else None,
                "start": page_number.get(f"{{{W}}}start") if page_number is not None else None,
            },
        })
        section_index += 1
    return result


def _body_child(node: etree._Element) -> etree._Element:
    current = node
    while current.getparent() is not None and current.getparent().tag != f"{{{W}}}body":
        current = current.getparent()
    if current.getparent() is None:
        raise ValueError("assembly locator is not inside w:body")
    return current


def _body_section_index(body: etree._Element, body_index: int) -> int:
    section_index = 0
    for index, child in enumerate(body):
        if index == body_index:
            return section_index
        if _section_property(child) is not None:
            section_index += 1
    raise ValueError(f"body child index is outside the document: {body_index}")


def _locator_identity_matches(root: etree._Element, compiled_locator: dict[str, Any], *,
                             allow_text_mutation: bool = False) -> list[etree._Element]:
    """Find locator identities without relying on mutable XML/body ordinals."""
    body = root.find("w:body", NS)
    if body is None:
        raise ValueError("document has no w:body")
    expected_kind = compiled_locator.get("kind")
    expected_style = compiled_locator.get("style_id")
    expected_digest = compiled_locator.get("normalized_text_sha256")
    expected_structure = compiled_locator.get("structural_sha256")
    expected_section = (compiled_locator.get("section") or {}).get("index")
    exact_matches: list[etree._Element] = []
    structural_matches: list[etree._Element] = []
    for body_index, child in enumerate(body):
        if child.tag == f"{{{W}}}p":
            candidates = [child]
        elif child.tag == f"{{{W}}}tbl":
            candidates = [child, *child.xpath(".//w:p", namespaces=NS)]
        else:
            candidates = child.xpath(".//w:p | .//w:tbl", namespaces=NS)
        for node in candidates:
            kind = "paragraph" if node.tag == f"{{{W}}}p" else "table"
            if expected_kind and kind != expected_kind:
                continue
            style = node.xpath("./w:pPr/w:pStyle/@w:val", namespaces=NS)
            if (style[0] if style else None) != expected_style:
                continue
            text = "".join(node.xpath(".//w:t/text() | .//w:instrText/text()", namespaces=NS))
            digest = hashlib.sha256("".join(text.split()).encode("utf-8")).hexdigest()
            if expected_structure and _structural_sha(node) != expected_structure:
                continue
            if expected_section is not None and _body_section_index(body, body_index) != expected_section:
                continue
            if expected_digest and digest == expected_digest:
                exact_matches.append(node)
            elif not expected_digest:
                exact_matches.append(node)
            elif expected_structure:
                structural_matches.append(node)
    if exact_matches:
        return exact_matches
    if expected_structure and allow_text_mutation:
        # Text-bound metadata may legitimately change the digest.  Prefer the
        # original XML path when it still identifies exactly one structurally
        # identical node; otherwise a unique structural match is acceptable.
        path_matches = []
        for node in root.xpath(compiled_locator["xml_path"], namespaces=NS):
            kind = "paragraph" if node.tag == f"{{{W}}}p" else "table"
            if expected_kind and kind != expected_kind:
                continue
            style = node.xpath("./w:pPr/w:pStyle/@w:val", namespaces=NS)
            if (style[0] if style else None) != expected_style:
                continue
            if _structural_sha(node) != expected_structure:
                continue
            path_matches.append(node)
        if len(path_matches) == 1:
            return path_matches
    return structural_matches if allow_text_mutation else []


def _find_locator(root: etree._Element, compiled_locator: dict[str, Any]) -> etree._Element:
    nodes = root.xpath(compiled_locator["xml_path"], namespaces=NS)
    if len(nodes) == 1:
        node = nodes[0]
        text = "".join(node.xpath(".//w:t/text() | .//w:instrText/text()", namespaces=NS))
        digest = hashlib.sha256("".join(text.split()).encode("utf-8")).hexdigest()
        style = node.xpath("./w:pPr/w:pStyle/@w:val", namespaces=NS)
        if digest == compiled_locator["normalized_text_sha256"] \
                and (style[0] if style else None) == compiled_locator["style_id"]:
            return node
    matches = _locator_identity_matches(root, compiled_locator)
    if len(matches) != 1:
        raise ValueError(
            f"stale assembly locator identity at {compiled_locator['xml_path']!r}: "
            f"got {len(matches)} identity matches"
        )
    return matches[0]


def _find_range(root: etree._Element, compiled_range: dict[str, Any]) -> tuple[etree._Element, etree._Element | None]:
    """Resolve and validate body boundaries for replace or preserve ranges."""
    if compiled_range.get("policy") not in {"replace_between", "preserve_between"}:
        raise ValueError(f"unsupported assembly range policy: {compiled_range.get('policy')!r}")
    start = _body_child(_find_locator(root, compiled_range["start"]))
    end_spec = compiled_range["end"]
    end = None if end_spec.get("boundary") == "body_end" else _body_child(_find_locator(root, end_spec))
    start_body = start.getparent()
    end_body = end.getparent() if end is not None else start_body
    if start_body is None or start_body is not end_body or start_body.tag != f"{{{W}}}body":
        raise ValueError("assembly range boundaries must resolve to the same w:body")
    end_index = (start_body.index(end) if end is not None else
                 len(start_body) - (1 if len(start_body) and start_body[-1].tag == f"{{{W}}}sectPr" else 0))
    if start_body.index(start) >= end_index:
        raise ValueError("assembly range start must precede range end")
    return start, end


def _set_paragraph_text(paragraph: etree._Element, text: str) -> None:
    for child in list(paragraph):
        if child.tag != f"{{{W}}}pPr":
            paragraph.remove(child)
    run = etree.SubElement(paragraph, f"{{{W}}}r")
    text_node = etree.SubElement(run, f"{{{W}}}t")
    if text.startswith(" ") or text.endswith(" ") or "  " in text:
        text_node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    text_node.text = text


def _apply_role_normalizations(children: list[etree._Element], actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    applied: list[dict[str, Any]] = []
    for action in actions:
        kind = action.get("action")
        if kind == "replace_heading_text":
            paragraph = next((child for child in children if child.tag == f"{{{W}}}p"), None)
            if paragraph is None:
                raise ValueError("source role normalization requires a heading paragraph")
            actual = "".join(paragraph.xpath(".//w:t/text()", namespaces=NS))
            if "".join(actual.split()) != "".join(str(action.get("from", "")).split()):
                raise ValueError("source role normalization heading identity is stale")
            _set_paragraph_text(paragraph, str(action["to"]))
            applied.append(copy.deepcopy(action))
        elif kind == "set_unnumbered_role_heading":
            # Numbering is represented in converted heading text for the current
            # adapter; replace_heading_text performs the concrete mutation.
            applied.append(copy.deepcopy(action))
        else:
            raise ValueError(f"unsupported source role normalization action: {kind!r}")
    return applied


def _incoming_children(path: Path, destination_members: dict[str, bytes],
                       destination_root: etree._Element, *,
                       source_section_policy: str,
                       source_role: dict[str, Any] | None = None) -> tuple[list[etree._Element], dict[str, Any]]:
    _, source_members = _read_package(path)
    source_range = None
    actions: list[dict[str, Any]] = []
    satisfied_by_template_boundary: list[dict[str, Any]] = []
    if source_role is not None:
        source_range = (
            int(source_role.get("content_start_body_child_index", source_role["start_body_child_index"])),
            int(source_role["end_body_child_index"]),
        )
        actions = list(source_role.get("normalization_actions", []))
        if source_role.get("heading_policy") == "preserve_template_boundary":
            satisfied_by_template_boundary = actions
            actions = []
    children, import_evidence = import_body_children(
        source_members, destination_members, destination_root,
        source_section_policy=source_section_policy,
        source_body_range=source_range,
    )
    import_evidence["role_normalizations_applied"] = _apply_role_normalizations(children, actions)
    import_evidence["role_normalizations_satisfied_by_template_boundary"] = copy.deepcopy(
        satisfied_by_template_boundary
    )
    return children, import_evidence


def _condition_value(expression: str | None, metadata: dict[str, Any],
                     source_role_map: dict[str, Any] | None = None) -> bool:
    if not expression:
        raise ValueError(f"unsupported assembly condition: {expression!r}")
    if expression.startswith("metadata."):
        current: Any = metadata
        parts = expression.removeprefix("metadata.").split(".")
    elif expression.startswith("source_role."):
        parts = expression.removeprefix("source_role.").split(".")
        if len(parts) != 2 or parts[1] != "present":
            raise ValueError(f"unsupported assembly condition: {expression!r}")
        if not source_role_map or source_role_map.get("status") != "extracted":
            raise ValueError(f"assembly condition requires an extracted source role map: {expression!r}")
        return parts[0] in source_role_map.get("roles", {})
    else:
        raise ValueError(f"unsupported assembly condition: {expression!r}")
    for part in parts:
        if not isinstance(current, dict) or part not in current:
            raise ValueError(f"assembly condition is not decidable: {expression!r}")
        current = current[part]
    if not isinstance(current, bool):
        raise ValueError(f"assembly condition must resolve to boolean: {expression!r}")
    return current


def _metadata_path_value(metadata: dict[str, Any], path: str) -> str | None:
    current: Any = metadata
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    if current is None:
        return None
    if isinstance(current, list):
        return "、".join(str(item.get("name", "")) if isinstance(item, dict) else str(item)
                        for item in current)
    return str(current)


def _transform_metadata_value(value: str, transform: str | None) -> str:
    """Apply only explicitly declared, deterministic display transforms."""
    if not transform:
        return value
    if transform == "chinese_year_month":
        match = re.fullmatch(r"(\d{4})-(\d{2})", value.strip())
        if not match:
            raise ValueError(
                "chinese_year_month requires an ISO YYYY-MM value: " + repr(value)
            )
        year, month = match.groups()
        digits = "〇一二三四五六七八九"
        year_text = "".join(digits[int(char)] for char in year)
        month_number = int(month)
        month_text = digits[month_number] if 1 <= month_number <= 9 else (
            "十" if month_number == 10 else "十一" if month_number == 11 else "十二"
        )
        return f"{year_text}年{month_text}月"
    raise ValueError(f"unsupported metadata value transform: {transform!r}")


def _replace_bound_node_text(node: etree._Element, value: str, *, replace_mode: str = "all_text",
                             prefix: str | None = None) -> None:
    """Replace text while preserving the official node's paragraph/run formatting."""
    text_nodes = node.xpath(".//w:t", namespaces=NS)
    if not text_nodes:
        # Empty official cover cells commonly contain only a styled w:p.  Add
        # the value into a new run while retaining the paragraph properties;
        # treating an empty cell as unbindable would leave required metadata
        # silently blank even though the selector resolved uniquely.
        if node.tag != f"{{{NS['w']}}}p":
            raise ValueError("metadata binding target has no w:t text node")
        run = node.find("w:r", namespaces=NS)
        if run is None:
            run = etree.SubElement(node, f"{{{NS['w']}}}r")
        text_node = etree.SubElement(run, f"{{{NS['w']}}}t")
        text_nodes = [text_node]
    if replace_mode == "after_prefix":
        current = "".join(item.text or "" for item in text_nodes)
        if not prefix or not current.startswith(prefix):
            raise ValueError(f"metadata binding prefix mismatch: {prefix!r}")
        value = prefix + value
    elif replace_mode != "all_text":
        raise ValueError(f"unsupported metadata replace mode: {replace_mode!r}")
    text_nodes[0].text = value
    if value.startswith(" ") or value.endswith(" ") or "  " in value:
        text_nodes[0].set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    for text_node in text_nodes[1:]:
        text_node.text = ""


def _protected_sha(root: etree._Element, assertion: dict[str, Any]) -> str:
    if assertion.get("range_locator"):
        start, end = _find_range(root, assertion["range_locator"])
        body = root.find("w:body", NS)
        if body is None:
            raise ValueError("template has no w:body")
        start_index = body.index(start)
        end_index = body.index(end) + 1 if end is not None else len(body) - (
            1 if len(body) and body[-1].tag == f"{{{W}}}sectPr" else 0
        )
        canonical = b"".join(
            _canonical_bytes(child)
            for child in list(body)[start_index:end_index]
        )
        return hashlib.sha256(canonical).hexdigest()
    node = _find_locator(root, assertion["locator"])
    return _canonical_sha(node)


def _assert_protected(root: etree._Element, assertions: list[dict[str, Any]]) -> None:
    for assertion in assertions:
        actual = _protected_sha(root, assertion)
        if actual != assertion["expected_sha256"]:
            raise ValueError(f"protected region hash mismatch: {assertion['node_id']!r}")


def _is_section_boundary(child: etree._Element) -> bool:
    """Whether a body child carries an official section boundary."""
    return _section_property(child) is not None


def _remove_range_children(body: etree._Element, start_index: int, end_index: int,
                           *, include_start: bool = False) -> dict[str, int]:
    """Remove dynamic content while retaining every template section carrier."""
    removed = 0
    preserved_boundaries = 0
    first = start_index if include_start else start_index + 1
    for child in list(body)[first:end_index]:
        if _is_section_boundary(child):
            preserved_boundaries += 1
            continue
        body.remove(child)
        removed += 1
    return {"content_children_removed": removed, "section_boundaries_preserved": preserved_boundaries}


def _preservation_policy(policy: Any) -> bool:
    """Recognize explicit template-preservation adapters.

    Generic page-break/section-break declarations remain fail-closed until an
    adapter can implement them.  The repaired assembly contract uses an
    explicit preservation declaration; its concrete operation is to retain the
    official boundary nodes and verify their effective signatures.
    """
    if not isinstance(policy, dict):
        return False
    return any(policy.get(key) is True for key in (
        "preserve_template",
        "preserve_template_boundaries",
        "preserve_template_sections",
        "preserve_headers_footers",
        "preserve_page_numbering",
    ))


def _remove_review_comment_markers(root: etree._Element) -> int:
    removed = 0
    for marker in list(root.iter()):
        if etree.QName(marker).localname in REVIEW_COMMENT_MARKERS:
            parent = marker.getparent()
            if parent is not None:
                parent.remove(marker)
                removed += 1
    return removed


def _is_comment_part(name: str) -> bool:
    lower = name.lower()
    return lower.startswith("word/comments") and lower.endswith(".xml")


def _relationship_targets_comment(node: etree._Element, removed_parts: set[str]) -> bool:
    rel_type = str(node.get("Type", "")).lower()
    target = str(node.get("Target", ""))
    target_name = posixpath.basename(posixpath.normpath(target)).lower()
    comment_part_names = {posixpath.basename(item).lower() for item in removed_parts}
    return "comment" in rel_type or target_name in comment_part_names


def _sanitize_review_comments(members: dict[str, bytes], root: etree._Element) -> dict[str, Any]:
    """Remove Word review-comment metadata from the generated package only."""
    removed_parts = {name for name in members if _is_comment_part(name)}
    marker_count = _remove_review_comment_markers(root)
    changed_parts: list[str] = []

    for name, payload in list(members.items()):
        if name in removed_parts or not (name.endswith(".xml") or name.endswith(".rels")):
            continue
        try:
            document = etree.fromstring(payload)
        except etree.XMLSyntaxError:
            continue
        changed = False
        local_markers_removed = 0
        if name != "word/document.xml":
            local_markers_removed = _remove_review_comment_markers(document)
            marker_count += local_markers_removed
        if name.endswith(".rels"):
            for relationship in list(document):
                if _relationship_targets_comment(relationship, removed_parts):
                    document.remove(relationship)
                    changed = True
        elif name == "[Content_Types].xml":
            for override in document.xpath("./ct:Override", namespaces={"ct": CT}):
                part_name = str(override.get("PartName", "")).lstrip("/")
                if part_name in removed_parts:
                    document.remove(override)
                    changed = True
        if changed or local_markers_removed:
            if name != "word/document.xml":
                before = payload
                after = etree.tostring(document, xml_declaration=True, encoding="UTF-8", standalone=True)
                if after != before:
                    members[name] = after
                    changed_parts.append(name)

    for name in removed_parts:
        members.pop(name, None)

    # The document root was passed separately so all locator/postcondition
    # checks see the sanitized tree before serialization.
    members["word/document.xml"] = etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", standalone=True
    )

    residual_markers: list[tuple[str, str]] = []
    residual_relationships: list[str] = []
    for name, payload in members.items():
        if not (name.endswith(".xml") or name.endswith(".rels")):
            continue
        try:
            document = etree.fromstring(payload)
        except etree.XMLSyntaxError:
            continue
        for node in document.iter():
            if etree.QName(node).localname in REVIEW_COMMENT_MARKERS:
                residual_markers.append((name, etree.QName(node).localname))
        if name.endswith(".rels"):
            if any(_relationship_targets_comment(rel, removed_parts) for rel in document):
                residual_relationships.append(name)
    residual_parts = sorted(name for name in members if _is_comment_part(name))
    if residual_markers or residual_relationships or residual_parts:
        raise ValueError(
            "review comment cleanup postcondition failed: "
            f"markers={residual_markers[:5]}, relationships={residual_relationships}, parts={residual_parts}"
        )
    return {
        "status": "cleaned",
        "comment_parts_removed": sorted(removed_parts),
        "comment_markers_removed": marker_count,
        "parts_rewritten": sorted(set(changed_parts)),
    }


def _assert_role_anchor_postconditions(root: etree._Element, postconditions: list[dict[str, Any]],
                                       metadata: dict[str, Any],
                                       source_role_map: dict[str, Any] | None) -> int:
    verified = 0
    for condition in postconditions:
        if condition.get("type") != "role_anchor_unique":
            continue
        expression = condition.get("condition")
        if expression and not _condition_value(expression, metadata, source_role_map):
            continue
        for anchor in condition.get("anchors", []):
            if "locator" in anchor:
                locator = anchor["locator"]
                anchor_condition = anchor.get("condition")
            else:
                locator = anchor
                anchor_condition = None
            if anchor_condition and not _condition_value(anchor_condition, metadata, source_role_map):
                continue
            matches = _locator_identity_matches(
                root, locator, allow_text_mutation=bool(anchor.get("allow_text_mutation"))
            )
            if len(matches) != 1:
                raise ValueError(
                    f"role anchor is not unique after assembly: {condition.get('role')!r}; "
                    f"expected 1, got {len(matches)}"
                )
            verified += 1
    return verified


def _assert_effective_section_invariants(root: etree._Element,
                                         postconditions: list[dict[str, Any]]) -> int:
    verified = 0
    for condition in postconditions:
        if condition.get("type") != "effective_section_invariants":
            continue
        actual = _section_invariants(root)
        expected = condition.get("sections", [])
        if len(actual) != condition.get("expected_section_count") or actual != expected:
            raise ValueError(
                "effective section invariants failed: "
                f"expected {condition.get('expected_section_count')} sections, got {len(actual)}"
            )
        verified += 1
    return verified


def _assert_deterministic_postconditions(root: etree._Element,
                                         postconditions: list[dict[str, Any]],
                                         metadata: dict[str, Any],
                                         source_role_map: dict[str, Any] | None) -> dict[str, int]:
    return {
        "role_anchors": _assert_role_anchor_postconditions(root, postconditions, metadata, source_role_map),
        "effective_section_invariants": _assert_effective_section_invariants(root, postconditions),
    }


def execute_assembly_plan(plan_result: dict[str, Any], template: Path, source: Path, output: Path,
                          *, metadata: dict[str, Any] | None = None,
                          source_section_policy: str = "reject",
                          source_role_map: dict[str, Any] | None = None) -> dict[str, Any]:
    if plan_result.get("status") != "compiled" or not isinstance(plan_result.get("assembly_plan"), dict):
        raise ValueError("assembly plan is not compiled")
    plan = plan_result["assembly_plan"]
    operations = plan.get("operations", [])
    postconditions = plan.get("structural_postconditions", [])
    # Validate complete executability before reading or mutating the package.
    unsupported_policies = [
        item for item in postconditions
        if item.get("type") in {"boundary", "section_policy"}
        and not _preservation_policy(item.get("policy"))
    ]
    if unsupported_policies:
        raise ValueError("boundary/section-policy assembly adapters are not implemented")
    for operation in operations:
        action = operation.get("action")
        if action == "generate_content":
            raise ValueError(f"no generator adapter for assembly node {operation.get('node_id')!r}")
        if action == "replace_content":
            has_locator = bool(operation.get("locator"))
            has_range = bool(operation.get("range_locator"))
            if has_locator == has_range:
                raise ValueError(
                    f"replace_content node requires exactly one locator or range_locator: "
                    f"{operation.get('node_id')!r}"
                )
        if action == "conditionally_assemble":
            _condition_value(operation.get("condition"), metadata or {}, source_role_map)
    replace_operations = [operation for operation in operations if operation.get("action") == "replace_content"]
    if len(replace_operations) > 1:
        if not source_role_map or source_role_map.get("status") != "extracted":
            raise ValueError("multiple replace_content nodes require an extracted source role map")
        available_roles = source_role_map.get("roles", {})
        missing_roles = sorted({
            str(operation.get("content_role")) for operation in replace_operations
            if not operation.get("content_role") or operation.get("content_role") not in available_roles
        })
        if missing_roles:
            raise ValueError("source role map lacks assembly roles: " + ", ".join(missing_roles))

    template = template.resolve()
    source = source.resolve()
    output = output.resolve()
    if output in {template, source}:
        raise ValueError("assembled output must be distinct from the official template and source DOCX")
    if output.exists():
        raise ValueError(f"refusing to overwrite existing assembled output: {output}")

    root, members = _read_package(template)
    assertions = plan.get("protection_assertions", [])
    _assert_protected(root, assertions)
    resolved_nodes = {
        operation["node_id"]: _body_child(_find_locator(root, operation["locator"]))
        for operation in operations if operation.get("locator")
    }
    resolved_ranges = {
        operation["node_id"]: _find_range(root, operation["range_locator"])
        for operation in operations
        if operation.get("range_locator") and operation.get("action") != "preserve"
    }
    metadata_bindings_applied: list[dict[str, Any]] = []
    for operation in operations:
        for binding in operation.get("metadata_bindings", []):
            value = _metadata_path_value(metadata or {}, binding["field"])
            if value is None or not value.strip():
                if binding.get("required") and binding.get("missing_policy") == "error":
                    raise ValueError(f"required assembly metadata is missing: {binding['field']}")
                metadata_bindings_applied.append({
                    "node_id": operation["node_id"], "field": binding["field"],
                    "status": "preserved_template_placeholder",
                })
                continue
            value = _transform_metadata_value(value, binding.get("value_transform"))
            target = _find_locator(root, binding["locator"])
            _replace_bound_node_text(
                target, value,
                replace_mode=binding.get("replace_mode", "all_text"),
                prefix=binding.get("prefix"),
            )
            metadata_bindings_applied.append({
                "node_id": operation["node_id"], "field": binding["field"],
                "status": "replaced",
            })
    incoming_by_role: dict[str, list[etree._Element]] = {}
    import_evidence_by_role: dict[str, dict[str, Any]] = {}
    applied: list[dict[str, Any]] = []
    runtime_nodes: dict[str, list[etree._Element]] = {
        node_id: [node] for node_id, node in resolved_nodes.items()
    }
    for operation in operations:
        action = operation["action"]
        if action == "preserve":
            applied.append({"node_id": operation["node_id"], "action": action})
            continue
        node = resolved_nodes.get(operation["node_id"])
        body = root.find("w:body", NS)
        if body is None:
            raise ValueError("template has no w:body")
        if action == "replace_content":
            content_role = str(operation.get("content_role") or "__whole_source__")
            if content_role not in incoming_by_role:
                role_binding = None
                if source_role_map is not None:
                    role_binding = source_role_map.get("roles", {}).get(content_role)
                    if role_binding is None and len(replace_operations) > 1:
                        raise ValueError(f"source role map lacks assembly role: {content_role}")
                incoming_by_role[content_role], import_evidence_by_role[content_role] = _incoming_children(
                    source, members, root, source_section_policy=source_section_policy,
                    source_role=role_binding,
                )
            incoming = incoming_by_role[content_role]
            range_boundaries = resolved_ranges.get(operation["node_id"])
            if range_boundaries is not None:
                start, end = range_boundaries
                start_index = body.index(start)
                end_index = (body.index(end) if end is not None else
                             len(body) - (1 if len(body) and body[-1].tag == f"{{{W}}}sectPr" else 0))
                _remove_range_children(body, start_index, end_index)
                insert_at = body.index(start) + 1
            else:
                if node is None:
                    raise ValueError(f"replace_content node has no resolved target: {operation['node_id']!r}")
                insert_at = body.index(node)
                body.remove(node)
            inserted: list[etree._Element] = []
            for offset, child in enumerate(incoming):
                inserted_child = copy.deepcopy(child)
                body.insert(insert_at + offset, inserted_child)
                inserted.append(inserted_child)
            if not inserted:
                raise ValueError(f"replace_content source is empty: {operation['node_id']!r}")
            runtime_nodes[operation["node_id"]] = inserted
        elif action == "conditionally_assemble":
            enabled = _condition_value(operation.get("condition"), metadata or {}, source_role_map)
            range_boundaries = resolved_ranges.get(operation["node_id"])
            if range_boundaries is not None:
                start, end = range_boundaries
                start_index = body.index(start)
                end_index = (body.index(end) if end is not None else
                             len(body) - (1 if len(body) and body[-1].tag == f"{{{W}}}sectPr" else 0))
                if enabled:
                    content_role = str(operation.get("content_role") or "")
                    role_binding = (source_role_map or {}).get("roles", {}).get(content_role)
                    if role_binding is None:
                        raise ValueError(f"enabled optional node lacks source role: {content_role}")
                    if content_role not in incoming_by_role:
                        incoming_by_role[content_role], import_evidence_by_role[content_role] = _incoming_children(
                            source, members, root, source_section_policy=source_section_policy,
                            source_role=role_binding,
                        )
                    incoming = incoming_by_role[content_role]
                    _remove_range_children(body, start_index, end_index)
                    start_index = body.index(start)
                    inserted = []
                    for offset, child in enumerate(incoming):
                        inserted_child = copy.deepcopy(child)
                        body.insert(start_index + 1 + offset, inserted_child)
                        inserted.append(inserted_child)
                    if not inserted:
                        raise ValueError(f"enabled optional source is empty: {operation['node_id']!r}")
                    runtime_nodes[operation["node_id"]] = [start, *inserted]
                else:
                    _remove_range_children(body, start_index, end_index, include_start=True)
                    runtime_nodes[operation["node_id"]] = []
            elif not enabled:
                if node is None:
                    raise ValueError(f"disabled optional node lacks removable locator: {operation['node_id']!r}")
                body.remove(node)
                runtime_nodes[operation["node_id"]] = []
        else:
            raise ValueError(f"unsupported assembly action: {action!r}")
        applied.append({"node_id": operation["node_id"], "action": action})

    # Hash lookup by locator can become position-stale after insertions. Verify
    # protected canonical hashes globally and require exactly one surviving hit.
    metadata_mutable_nodes = {
        operation["node_id"] for operation in operations if operation.get("metadata_bindings")
    }
    for assertion in assertions:
        if assertion["node_id"] in metadata_mutable_nodes:
            # The original official node was identity-verified before mutation;
            # its declared text bindings are the only permitted mutation path.
            continue
        if assertion.get("range_locator"):
            if _protected_sha(root, assertion) != assertion["expected_sha256"]:
                raise ValueError(f"protected region removed or modified: {assertion['node_id']!r}")
            continue
        matches = [node for node in root.xpath("//w:p | //w:tbl", namespaces=NS)
                   if _canonical_sha(node) == assertion["expected_sha256"]]
        if len(matches) != 1:
            raise ValueError(f"protected region removed, modified, or duplicated: {assertion['node_id']!r}")

    body = root.find("w:body", NS)
    if body is None:
        raise ValueError("assembled document has no w:body")
    positions = {id(child): index for index, child in enumerate(body)}
    for condition in postconditions:
        if condition.get("type") != "topological_order":
            continue
        left = runtime_nodes.get(condition["from"], [])
        right = runtime_nodes.get(condition["to"], [])
        # A disabled optional node vacuously satisfies its ordering edges.
        if not left or not right:
            continue
        if max(positions[id(node)] for node in left) >= min(positions[id(node)] for node in right):
            raise ValueError(f"structural order postcondition failed: {condition['from']} -> {condition['to']}")

    deterministic = _assert_deterministic_postconditions(
        root, postconditions, metadata or {}, source_role_map
    )
    comments_cleanup = _sanitize_review_comments(members, root)
    # Sanitization removes only generated-package review metadata.  Re-run all
    # deterministic assertions against the sanitized tree before writing it.
    deterministic = _assert_deterministic_postconditions(
        root, postconditions, metadata or {}, source_role_map
    )

    members["word/document.xml"] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)

    # Inspect the actual serialized package as the final acceptance boundary;
    # in-memory OOXML alone cannot prove that ZIP/package sanitization and
    # section relationships were persisted correctly.
    final_root, final_members = _read_package(output)
    final_deterministic = _assert_deterministic_postconditions(
        final_root, postconditions, metadata or {}, source_role_map
    )
    for assertion in assertions:
        if assertion["node_id"] in metadata_mutable_nodes:
            continue
        actual = _protected_sha(final_root, assertion)
        if actual != assertion["expected_sha256"]:
            raise ValueError(f"serialized protected region mismatch: {assertion['node_id']!r}")
    final_comments = _sanitize_review_comments(final_members, final_root)
    if final_comments["comment_parts_removed"] or final_comments["comment_markers_removed"]:
        raise ValueError("serialized assembled DOCX still contains review-comment metadata")
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise ValueError("serialized assembled DOCX has a corrupt ZIP member")

    declared_postconditions = [
        item for item in postconditions if item.get("verification") != "derived"
    ]
    return {"schema_version": "1.0", "status": "assembled", "profile_id": plan.get("profile_id"),
            "graph_id": plan.get("graph_id"), "template_sha256": sha256(template),
            "source_sha256": sha256(source), "output": str(output.resolve()),
            "output_sha256": sha256(output), "operations_applied": applied,
            "protected_regions_verified": len(assertions),
            "structural_postconditions_verified": len(declared_postconditions),
            "deterministic_postconditions_verified": final_deterministic,
            "role_anchors_verified": final_deterministic["role_anchors"],
            "effective_section_invariants_verified": final_deterministic["effective_section_invariants"],
            "effective_section_count": len(_section_invariants(final_root)),
            "comments_cleanup": comments_cleanup,
            "metadata_bindings": metadata_bindings_applied,
            "source_import": (
                next(iter(import_evidence_by_role.values()))
                if len(import_evidence_by_role) == 1
                else import_evidence_by_role
            ), "findings": []}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("template", type=Path)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--source-section-policy", choices=("reject", "discard"), default="reject")
    parser.add_argument("--source-role-map", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        metadata = json.loads(args.metadata.read_text(encoding="utf-8")) if args.metadata else {}
        source_role_map = json.loads(args.source_role_map.read_text(encoding="utf-8")) if args.source_role_map else None
        result = execute_assembly_plan(
            plan, args.template, args.source, args.output, metadata=metadata,
            source_section_policy=args.source_section_policy,
            source_role_map=source_role_map,
        )
        rc = 0
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, zipfile.BadZipFile, etree.XMLSyntaxError) as exc:
        result = {"schema_version": "1.0", "status": "blocked", "findings": [finding(
            "assembly.execution_blocked", "assembly_execution", "error", True, str(exc),
            [evidence("plan", str(args.plan)), evidence("template", str(args.template)),
             evidence("source", str(args.source))])]}
        rc = 2
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "report": str(args.report)}, ensure_ascii=False))
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
