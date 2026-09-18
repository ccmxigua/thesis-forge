#!/usr/bin/env python3
"""Load, verify, and compile official DOCX template profiles.

The compiler emits fail-closed OOXML locators.  Paragraph ordinals are retained
only as diagnostic evidence; identity is bound to the package part, XML path,
style, normalized text digest, table/cell coordinates, bookmarks/content
controls, and the section containing the node.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path
from typing import Any, Iterable

from lxml import etree

from format_spec_validation import load_and_validate

ROOT = Path(__file__).resolve().parents[1]
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def text_sha256(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def load_profile(path: Path, *, verify_resources: bool = True) -> dict[str, Any]:
    path = path.resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    errors = load_and_validate(data, ROOT / "schema" / "template-profile.schema.json")
    if errors:
        raise ValueError("invalid template profile:\n" + "\n".join(errors))
    seen: set[str] = set()
    for resource_item in data["resources"]:
        resource_id = resource_item["id"]
        if resource_id in seen:
            raise ValueError(f"duplicate template resource id: {resource_id}")
        seen.add(resource_id)
        resolved = (path.parent / resource_item["path"]).resolve()
        resource_item["resolved_path"] = str(resolved)
        if verify_resources:
            if not resolved.is_file():
                raise ValueError(f"template resource does not exist: {resolved}")
            actual = sha256(resolved)
            if actual != resource_item["sha256"]:
                raise ValueError(
                    f"template resource hash mismatch for {resource_id}: expected {resource_item['sha256']}, got {actual}"
                )
    return data


def resource(profile: dict[str, Any], kind: str) -> Path | None:
    for item in profile.get("resources", []):
        if item.get("kind") == kind:
            raw = item.get("resolved_path") or item.get("path")
            return Path(raw) if raw else None
    return None


def _node_text(node: etree._Element) -> str:
    # instrText matters: official templates often place placeholder anchors in
    # MACROBUTTON fields rather than ordinary w:t nodes.
    return "".join(node.xpath(".//w:t/text() | .//w:instrText/text()", namespaces=NS))


def _style_id(node: etree._Element) -> str | None:
    values = node.xpath("./w:pPr/w:pStyle/@w:val", namespaces=NS)
    return values[0] if values else None


def _xml_path(node: etree._Element, root: etree._Element) -> str:
    return root.getroottree().getpath(node)


def _table_coordinates(node: etree._Element) -> dict[str, Any] | None:
    cell = next(iter(node.iterancestors(f"{{{W}}}tc")), None)
    table = node if node.tag == f"{{{W}}}tbl" else next(iter(node.iterancestors(f"{{{W}}}tbl")), None)
    if table is None:
        return None
    body = next(iter(table.iterancestors(f"{{{W}}}body")), None)
    tables = body.xpath("./w:tbl", namespaces=NS) if body is not None else []
    result: dict[str, Any] = {"table_index": tables.index(table) if table in tables else None}
    if cell is not None:
        row = cell.getparent()
        result["row_index"] = list(table).index(row)
        result["cell_index"] = list(row).index(cell)
        paragraphs = cell.xpath(".//w:p", namespaces=NS)
        result["cell_paragraph_index"] = paragraphs.index(node) if node in paragraphs else None
    return result


def _bookmark_info(node: etree._Element) -> list[dict[str, str | None]]:
    return [
        {"id": item.get(f"{{{W}}}id"), "name": item.get(f"{{{W}}}name")}
        for item in node.xpath(".//w:bookmarkStart", namespaces=NS)
    ]


def _content_controls(node: etree._Element) -> list[dict[str, str | None]]:
    controls = list(node.xpath("ancestor-or-self::w:sdt", namespaces=NS)) + list(node.xpath(".//w:sdt", namespaces=NS))
    seen: set[int] = set()
    result = []
    for control in controls:
        if id(control) in seen:
            continue
        seen.add(id(control))
        tags = control.xpath("./w:sdtPr/w:tag/@w:val", namespaces=NS)
        aliases = control.xpath("./w:sdtPr/w:alias/@w:val", namespaces=NS)
        ids = control.xpath("./w:sdtPr/w:id/@w:val", namespaces=NS)
        result.append({"id": ids[0] if ids else None, "tag": tags[0] if tags else None,
                       "alias": aliases[0] if aliases else None})
    return result


def _section_map(body: etree._Element) -> dict[int, int]:
    section = 0
    result: dict[int, int] = {}
    for index, child in enumerate(body):
        result[index] = section
        if child.xpath("./w:pPr/w:sectPr | self::w:sectPr", namespaces=NS):
            section += 1
    return result


def _iter_candidates(root: etree._Element) -> Iterable[tuple[etree._Element, int, int]]:
    body = root.find("w:body", NS)
    if body is None:
        return
    sections = _section_map(body)
    paragraph_ordinal = 0
    for child_index, child in enumerate(body):
        nodes = [child] if child.tag == f"{{{W}}}p" else child.xpath(".//w:p", namespaces=NS)
        for node in nodes:
            yield node, child_index, sections[child_index]
            paragraph_ordinal += 1
        if child.tag == f"{{{W}}}tbl":
            yield child, child_index, sections[child_index]


def _locator(node: etree._Element, root: etree._Element, body_index: int, section_index: int) -> dict[str, Any]:
    text = _node_text(node)
    local = etree.QName(node).localname
    parent = node.getparent()
    result = {
        "part": "word/document.xml",
        "kind": "paragraph" if local == "p" else "table",
        "xml_path": _xml_path(node, root),
        "body_child_index": body_index,
        "body_child_path": f"/w:document/w:body/*[{body_index + 1}]",
        "sibling_index": list(parent).index(node) if parent is not None else 0,
        "style_id": _style_id(node),
        "normalized_text_sha256": text_sha256(text),
        "normalized_text_length": len(normalize(text)),
        "table_cell_path": _table_coordinates(node),
        "bookmarks": _bookmark_info(node),
        "content_controls": _content_controls(node),
        "section": {
            "index": section_index,
            "boundary_after": bool(node.xpath("./w:pPr/w:sectPr | self::w:sectPr", namespaces=NS)),
        },
    }
    return result


def _selector_matches(node: etree._Element, selector: dict[str, Any], *, body_index: int | None = None,
                      section_index: int | None = None) -> bool:
    if selector.get("kind") and selector["kind"] != ("paragraph" if node.tag == f"{{{W}}}p" else "table"):
        return False
    text = normalize(_node_text(node))
    wanted_values = [selector.get("text", ""), *selector.get("accepted_texts", [])]
    wanted_values = [normalize(value) for value in wanted_values if value]
    mode = selector.get("match", "exact")
    if wanted_values:
        if mode == "exact" and not any(text == wanted for wanted in wanted_values):
            return False
        if mode == "starts_with" and not any(text.startswith(wanted) for wanted in wanted_values):
            return False
        if mode == "contains" and not any(wanted in text for wanted in wanted_values):
            return False
    if selector.get("normalized_text_sha256") and text_sha256(text) != selector["normalized_text_sha256"]:
        return False
    if "style_id" in selector:
        accepted_style_ids = [selector["style_id"], *selector.get("accepted_style_ids", [])]
        if _style_id(node) not in accepted_style_ids:
            return False
    if "body_child_index" in selector and selector["body_child_index"] != body_index:
        return False
    if "section_index" in selector and selector["section_index"] != section_index:
        return False
    # A body-child index identifies the table container, not the paragraph
    # inside it.  Cell coordinates are therefore a first-class identity part
    # of selectors used for deterministic cover-field bindings.  Without this
    # check, two empty/identical cells in an official cover can resolve to the
    # wrong paragraph while still appearing "unique" to the compiler.
    if "table_cell_path" in selector:
        if _table_coordinates(node) != selector["table_cell_path"]:
            return False
    return True


def _unique_selector_match(root: etree._Element, candidates: list[tuple[etree._Element, int, int]],
                           selector: dict[str, Any], label: str) -> tuple[dict[str, Any], str]:
    matches = [(_locator(node, root, body_index, section), _node_text(node))
               for node, body_index, section in candidates
               if _selector_matches(node, selector, body_index=body_index, section_index=section)]
    if len(matches) != 1:
        evidence = [{"body_child_index": item[0]["body_child_index"], "text": normalize(item[1])[:120],
                     "style_id": item[0]["style_id"]} for item in matches[:10]]
        raise ValueError(f"{label} is ambiguous: expected exactly 1 match, got {len(matches)}; {evidence}")
    return matches[0]


# Public selector/locator API shared by profile compilation and region-graph
# preflight.  Keep the historical private helpers above as implementation
# details so existing callers remain compatible while new components no
# longer import underscored names.
def node_text(node: etree._Element) -> str:
    return _node_text(node)


def iter_candidates(root: etree._Element) -> Iterable[tuple[etree._Element, int, int]]:
    return _iter_candidates(root)


def build_locator(node: etree._Element, root: etree._Element,
                  body_index: int, section_index: int) -> dict[str, Any]:
    return _locator(node, root, body_index, section_index)


def selector_matches(node: etree._Element, selector: dict[str, Any], *,
                     body_index: int | None = None,
                     section_index: int | None = None) -> bool:
    return _selector_matches(node, selector, body_index=body_index,
                             section_index=section_index)


def unique_selector_match(root: etree._Element,
                          candidates: list[tuple[etree._Element, int, int]],
                          selector: dict[str, Any], label: str) -> tuple[dict[str, Any], str]:
    return _unique_selector_match(root, candidates, selector, label)


def _resolve_selectors(root: etree._Element, definitions: dict[str, Any]) -> dict[str, Any]:
    candidates = list(_iter_candidates(root))
    compiled: dict[str, Any] = {}
    for locator_id, selector in definitions.items():
        match = _unique_selector_match(root, candidates, selector, f"build locator {locator_id!r}")
        compiled[locator_id] = {"selector": selector, "locator": match[0]}
    return compiled


def compile_profile(profile_path: Path) -> dict[str, Any]:
    profile = load_profile(profile_path)
    official = resource(profile, "official_docx")
    if official is None:
        raise ValueError("template profile has no official_docx resource")
    with zipfile.ZipFile(official) as archive:
        package_parts = sorted(archive.namelist())
        root = etree.fromstring(archive.read("word/document.xml"))
    body = root.find("w:body", NS)
    if body is None:
        raise ValueError("official template has no word/document.xml body")

    candidates = list(_iter_candidates(root))
    roles: dict[str, Any] = {}
    for rule in profile["structure"]["ordered_roles"]:
        evidence: dict[str, Any] = {"required": rule.get("required", False)}
        if "selector" in rule:
            match = _unique_selector_match(root, candidates, rule["selector"],
                                           f"role selector {rule['role']!r}")
            evidence.update({"selector": rule["selector"], "locator": match[0], "locators": [match[0]]})
        elif "selector_variants" in rule:
            variants = []
            for index, variant in enumerate(rule["selector_variants"]):
                match = _unique_selector_match(
                    root, candidates, variant["selector"],
                    f"role selector variant {rule['role']!r}[{index}]",
                )
                variants.append({"when": variant["when"], "selector": variant["selector"], "locator": match[0]})
            evidence.update({"selector_variants": variants, "locators": [item["locator"] for item in variants]})
        else:
            hits = []
            for node, body_index, section in candidates:
                text = normalize(_node_text(node))
                if any(text == normalize(anchor) or text.startswith(normalize(anchor))
                       for anchor in rule.get("anchors", [])):
                    hits.append(_locator(node, root, body_index, section))
            evidence["locators"] = hits
        roles[rule["role"]] = evidence

    fixed = {}
    for item in profile.get("fixed_texts", []):
        needle = normalize(item["text"])
        matching = []
        for node, body_index, section in candidates:
            if needle and needle in normalize(_node_text(node)):
                loc = _locator(node, root, body_index, section)
                loc["ooxml_sha256"] = hashlib.sha256(etree.tostring(node, method="c14n")).hexdigest()
                matching.append(loc)
        fixed[item["id"]] = {
            "required": item.get("required", True),
            "present_in_official_template": bool(matching),
            "normalized_sha256": hashlib.sha256(needle.encode("utf-8")).hexdigest(),
            "locators": matching,
        }
        if item.get("required", True) and len(matching) != 1:
            raise ValueError(f"fixed text {item['id']!r} must resolve uniquely; got {len(matching)} matches")

    build = profile.get("build", {})
    build_locators = _resolve_selectors(root, build.get("locators", {}))
    return {
        "schema_version": "1.1",
        "profile_id": profile["profile_id"],
        "profile_path": str(profile_path.resolve()),
        "official_docx": str(official),
        "official_docx_sha256": sha256(official),
        "body_child_count": len(body),
        "section_count": 1 + sum(bool(child.xpath("./w:pPr/w:sectPr | self::w:sectPr", namespaces=NS)) for child in body[:-1]),
        "package_part_count": len(package_parts),
        "role_evidence": roles,
        "fixed_text_evidence": fixed,
        "build": {"locators": build_locators, "operations": build.get("operations", [])},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        compiled = compile_profile(args.profile)
    except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile, etree.XMLSyntaxError) as exc:
        parser.error(str(exc))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(compiled, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "compiled", "profile_id": compiled["profile_id"], "out": str(args.out)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
