#!/usr/bin/env python3
"""Build a DOCX from an official package using compiled, fail-closed rules.

This is deliberately a package-preserving builder: every ZIP member is copied
verbatim except word/document.xml.  Fixed declaration blocks and all other
untouched XML therefore remain byte-for-byte identical inside document.xml's
canonical subtrees.  Supported operations are intentionally small:

* keep_range: delete body siblings outside an inclusive locator range;
* delete_range: delete an inclusive locator range;
* replace_range_from_docx: replace a range with body children from another DOCX.

Unknown operators, stale locators, ambiguous selectors, attempts to remove a
required fixed block, or replacement content containing section properties all
fail closed.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
import zipfile
from pathlib import Path
from typing import Any

from lxml import etree

from template_profile import NS, W, compile_profile, load_profile, resource, sha256
from semantic_contract import strict_json_read


def _read_part(docx: Path, part: str = "word/document.xml") -> tuple[etree._Element, dict[str, bytes]]:
    with zipfile.ZipFile(docx) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    return etree.fromstring(members[part]), members


def _canonical_sha(node: etree._Element) -> str:
    return hashlib.sha256(etree.tostring(node, method="c14n")).hexdigest()


def _find_locator(root: etree._Element, compiled_locator: dict[str, Any]) -> etree._Element:
    locator = compiled_locator["locator"]
    nodes = root.xpath(locator["xml_path"], namespaces=NS)
    if len(nodes) != 1:
        raise ValueError(f"stale locator path {locator['xml_path']!r}: got {len(nodes)} nodes")
    node = nodes[0]
    text = "".join(node.xpath(".//w:t/text() | .//w:instrText/text()", namespaces=NS))
    digest = hashlib.sha256("".join(text.split()).encode("utf-8")).hexdigest()
    style = node.xpath("./w:pPr/w:pStyle/@w:val", namespaces=NS)
    style_id = style[0] if style else None
    if digest != locator["normalized_text_sha256"] or style_id != locator["style_id"]:
        raise ValueError(f"stale locator identity at {locator['xml_path']!r}")
    return node


def _body_child(node: etree._Element) -> etree._Element:
    current = node
    while current.getparent() is not None and current.getparent().tag != f"{{{W}}}body":
        current = current.getparent()
    if current.getparent() is None:
        raise ValueError("locator is not inside w:body")
    return current


def _indexes(root: etree._Element, locators: dict[str, Any], start: str, end: str) -> tuple[int, int]:
    body = root.find("w:body", NS)
    if body is None:
        raise ValueError("document has no w:body")
    if start not in locators or end not in locators:
        raise ValueError(f"unknown locator in range: {start!r}, {end!r}")
    start_node = _body_child(_find_locator(root, locators[start]))
    end_node = _body_child(_find_locator(root, locators[end]))
    values = list(body)
    left, right = values.index(start_node), values.index(end_node)
    if left > right:
        raise ValueError(f"range is reversed: {start!r} occurs after {end!r}")
    return left, right


def _incoming_children(path: Path) -> list[etree._Element]:
    root, _ = _read_part(path)
    body = root.find("w:body", NS)
    if body is None:
        raise ValueError(f"replacement DOCX has no body: {path}")
    children = list(body)
    if children and children[-1].tag == f"{{{W}}}sectPr":
        children = children[:-1]
    if any(child.xpath(".//w:sectPr | self::w:sectPr", namespaces=NS) for child in children):
        raise ValueError("replacement body may not contain section properties")
    return [copy.deepcopy(child) for child in children]


def _apply_operation(root: etree._Element, locators: dict[str, Any], operation: dict[str, Any], inputs: dict[str, Path]) -> None:
    body = root.find("w:body", NS)
    if body is None:
        raise ValueError("document has no w:body")
    operator = operation["op"]
    left, right = _indexes(root, locators, operation["start"], operation["end"])
    children = list(body)
    terminal = children[-1] if children and children[-1].tag == f"{{{W}}}sectPr" else None
    if operator == "keep_range":
        keep = set(children[left:right + 1])
        if terminal is not None:
            keep.add(terminal)
        for child in children:
            if child not in keep:
                body.remove(child)
    elif operator == "delete_range":
        for child in children[left:right + 1]:
            if child is terminal:
                raise ValueError("delete_range may not remove terminal sectPr")
            body.remove(child)
    elif operator == "replace_range_from_docx":
        input_id = operation.get("input")
        if input_id not in inputs:
            raise ValueError(f"missing replacement input {input_id!r}")
        incoming = _incoming_children(inputs[input_id])
        anchor = children[left]
        insert_at = body.index(anchor)
        for child in children[left:right + 1]:
            if child is terminal:
                raise ValueError("replace_range_from_docx may not replace terminal sectPr")
            body.remove(child)
        for offset, child in enumerate(incoming):
            body.insert(insert_at + offset, child)
    else:
        raise ValueError(f"unknown template build operator: {operator!r}")


def build_template(profile_path: Path, output: Path, *, inputs: dict[str, Path] | None = None,
                   compiled_path: Path | None = None) -> dict[str, Any]:
    profile = load_profile(profile_path)
    official = resource(profile, "official_docx")
    if official is None:
        raise ValueError("profile has no official_docx")
    compiled = strict_json_read(compiled_path) if compiled_path else compile_profile(profile_path)
    if compiled.get("official_docx_sha256") != sha256(official):
        raise ValueError("compiled profile is not bound to the current official template")
    root, members = _read_part(official)
    locators = compiled.get("build", {}).get("locators", {})
    for operation in compiled.get("build", {}).get("operations", []):
        _apply_operation(root, locators, operation, inputs or {})

    # Required fixed blocks must still exist and be canonical-identical.
    for fixed_id, evidence in compiled["fixed_text_evidence"].items():
        if not evidence.get("required"):
            continue
        expected = evidence.get("locators", [])
        if len(expected) != 1:
            raise ValueError(f"required fixed block {fixed_id!r} lacks a unique compiled locator")
        expected_hash = expected[0]["ooxml_sha256"]
        matching = [node for node in root.xpath("//w:p | //w:tbl", namespaces=NS)
                    if _canonical_sha(node) == expected_hash]
        if len(matching) != 1:
            raise ValueError(f"build would remove, modify, or duplicate required fixed block {fixed_id!r}")

    members["word/document.xml"] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return {"status": "built", "profile_id": profile["profile_id"], "output": str(output.resolve()),
            "output_sha256": sha256(output), "operations_applied": len(compiled.get("build", {}).get("operations", []))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--compiled", type=Path)
    parser.add_argument("--input", action="append", default=[], metavar="ID=DOCX")
    args = parser.parse_args(argv)
    inputs: dict[str, Path] = {}
    for raw in args.input:
        if "=" not in raw:
            parser.error("--input must be ID=DOCX")
        key, value = raw.split("=", 1)
        if not key or key in inputs:
            parser.error(f"invalid or duplicate input id: {key!r}")
        inputs[key] = Path(value).resolve()
    try:
        result = build_template(args.profile, args.output, inputs=inputs, compiled_path=args.compiled)
    except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile, etree.XMLSyntaxError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
