#!/usr/bin/env python3
"""Compile declarative template regions into a fail-closed assembly plan.

This module is deliberately model/compiler-only: it resolves selectors against
an official DOCX and describes assembly operations, but never mutates OOXML.
Selectors use :mod:`template_profile`'s locator matching semantics.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path
from typing import Any, Iterable

from lxml import etree

from format_spec_validation import load_and_validate
from pipeline_finding import evidence, finding
from template_profile import (
    ROOT,
    NS,
    build_locator,
    iter_candidates,
    node_text,
    normalize,
    resource,
    selector_matches,
)
from semantic_contract import strict_json_read

NODE_KINDS = {"fixed_protected", "dynamic_content", "generated", "optional"}
EDGE_KINDS = {"order", "boundary", "section_policy"}
REVIEW_COMMENT_MARKERS = {"commentRangeStart", "commentRangeEnd", "commentReference"}
CANONICAL_OOXML_HASH_POLICY = {
    "algorithm": "sha256",
    "canonicalization": "C14N-1.0",
    "scope": "resolved_node",
    "comments": False,
    "verification_phase": "pre_and_post_assembly",
}


def _finding(code: str, message: str, path: str, **details: Any) -> dict[str, Any]:
    items = [evidence("path", path)]
    items.extend(evidence(key, value) for key, value in details.items())
    return finding(code, "region_graph", "error", True, message, items)


def _validate_contract(declaration: dict[str, Any]) -> list[dict[str, Any]]:
    errors = load_and_validate(declaration, ROOT / "schema" / "region-graph.schema.json")
    return [_finding("region_graph.schema_invalid", error, "$") for error in errors]


def _topological_order(node_ids: Iterable[str], edges: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    # Every edge is a dependency. Boundary and section-policy edges therefore
    # constrain execution just like explicit order edges.
    ids = list(node_ids)
    position = {node_id: index for index, node_id in enumerate(ids)}
    outgoing = {node_id: set() for node_id in ids}
    indegree = {node_id: 0 for node_id in ids}
    for edge in edges:
        source, target = edge["from"], edge["to"]
        if source in outgoing and target in indegree and target not in outgoing[source]:
            outgoing[source].add(target)
            indegree[target] += 1
    ready = sorted((node_id for node_id in ids if indegree[node_id] == 0), key=position.get)
    ordered: list[str] = []
    while ready:
        current = ready.pop(0)
        ordered.append(current)
        for target in sorted(outgoing[current], key=position.get):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
                ready.sort(key=position.get)
    cyclic = [node_id for node_id in ids if indegree[node_id] > 0]
    return ordered, cyclic


def _selector_matches_in_template(
    root: etree._Element,
    candidates: list[tuple[etree._Element, int, int]],
    selector: dict[str, Any],
) -> list[tuple[etree._Element, dict[str, Any]]]:
    matches = []
    for node, body_index, section_index in candidates:
        if not selector_matches(node, selector, body_index=body_index, section_index=section_index):
            continue
        locator = build_locator(node, root, body_index, section_index)
        locator["structural_sha256"] = _structural_hash(node)
        matches.append((node, locator))
    return matches


def _canonical_bytes(node: etree._Element) -> bytes:
    """Return canonical OOXML with review-comment markers ignored."""
    copy_node = etree.fromstring(etree.tostring(node))
    for marker in list(copy_node.iter()):
        if etree.QName(marker).localname in REVIEW_COMMENT_MARKERS:
            parent = marker.getparent()
            if parent is not None:
                parent.remove(marker)
    return etree.tostring(copy_node, method="c14n", with_comments=False)


def _canonical_hash(node: etree._Element) -> str:
    # Review-comment markers are sanitization metadata, not user content.  The
    # hash contract must therefore remain stable when the generated output
    # removes those markers (the official template itself is never mutated).
    canonical = _canonical_bytes(node)
    return hashlib.sha256(canonical).hexdigest()


def _structural_hash(node: etree._Element) -> str:
    """Hash formatting/OOXML structure while ignoring editable text values."""
    copy_node = etree.fromstring(etree.tostring(node))
    for text_node in copy_node.xpath(".//w:t | .//w:instrText", namespaces=NS):
        text_node.text = ""
    return hashlib.sha256(_canonical_bytes(copy_node)).hexdigest()


def _section_property(node: etree._Element) -> etree._Element | None:
    if node.tag == f"{{{NS['w']}}}sectPr":
        return node
    properties = node.xpath("./w:pPr/w:sectPr", namespaces=NS)
    return properties[0] if properties else None


def _section_invariants(root: etree._Element) -> list[dict[str, Any]]:
    """Describe effective template section boundaries without body ordinals.

    Body-child positions are expected to move when content is assembled.  The
    section properties, their header/footer relationships, and page-number
    settings are the stable contract that must survive that movement.
    """
    body = root.find("w:body", NS)
    if body is None:
        raise ValueError("template has no w:body")
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
                "type": reference.get(f"{{{NS['w']}}}type"),
                "rid": reference.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"),
            })
        page_number = sect_pr.find("w:pgNumType", NS)
        result.append({
            "section_index": section_index,
            "marker_kind": "body_sectPr" if child.tag == f"{{{NS['w']}}}sectPr" else "paragraph_sectPr",
            "sectpr_sha256": _canonical_hash(sect_pr),
            "header_footer_refs": refs,
            "page_number": {
                "format": page_number.get(f"{{{NS['w']}}}fmt") if page_number is not None else None,
                "start": page_number.get(f"{{{NS['w']}}}start") if page_number is not None else None,
            },
        })
        section_index += 1
    return result


def _deterministic_postconditions(
    nodes: list[dict[str, Any]], compiled_nodes: dict[str, dict[str, Any]],
    root: etree._Element | None,
) -> list[dict[str, Any]]:
    """Emit postconditions that can be checked against the serialized DOCX.

    These are deliberately based on stable text/style identities and effective
    section properties rather than body ordinals.  Insertion of dynamic
    content may shift XML paths and body indices, but it must not make a role
    boundary ambiguous or change the official section contract.
    """
    postconditions: list[dict[str, Any]] = []
    range_starts: dict[tuple[str, str], dict[str, Any]] = {}
    for node in nodes:
        range_locator = compiled_nodes.get(node["id"], {}).get("range_locator")
        if range_locator:
            start = range_locator["start"]
            range_starts[(start.get("part"), start.get("xml_path"))] = node

    for node in nodes:
        compiled = compiled_nodes.get(node["id"], {})
        anchors: list[dict[str, Any]] = []
        if compiled.get("locator") and node.get("kind") == "fixed_protected":
            anchors.append({
                "locator": compiled["locator"],
                "allow_text_mutation": bool(node.get("metadata_bindings")),
            })
        range_locator = compiled.get("range_locator")
        if range_locator:
            anchors.append({"locator": range_locator["start"], "condition": node.get("condition")})
            if range_locator["end"].get("boundary") != "body_end":
                end = range_locator["end"]
                boundary_owner = range_starts.get((end.get("part"), end.get("xml_path")))
                anchors.append({
                    "locator": end,
                    "condition": boundary_owner.get("condition") if boundary_owner else None,
                })
        if anchors:
            postconditions.append({
                "type": "role_anchor_unique",
                "verification": "derived",
                "node_id": node["id"],
                "role": node.get("content_role") or node["id"],
                "required": node.get("kind") != "optional",
                "condition": node.get("condition"),
                "anchors": anchors,
            })
    if root is not None:
        sections = _section_invariants(root)
        postconditions.append({
            "type": "effective_section_invariants",
            "verification": "derived",
            "expected_section_count": len(sections),
            "sections": sections,
        })
    return postconditions


def compile_region_graph(
    profile: dict[str, Any],
    *,
    template_path: Path | None = None,
    validate_schema: bool = True,
) -> dict[str, Any]:
    """Compile ``profile['regions']`` into an AssemblyPlan and findings.

    ``template_path`` is optional only when all nodes are generated and have no
    selector. Selector-bearing nodes are preflighted against the template and
    must resolve exactly once. Errors are returned as machine findings; no
    assembly operation is emitted when compilation is invalid.
    """
    declaration = profile.get("regions")
    findings: list[dict[str, Any]] = []
    if not isinstance(declaration, dict):
        findings.append(_finding("region_graph.missing", "profile has no regions declaration", "$.regions"))
        return _result(profile, None, [], findings)

    if validate_schema:
        findings.extend(_validate_contract(declaration))
        if findings:
            return _result(profile, declaration, [], findings)

    nodes = declaration.get("nodes", [])
    edges = declaration.get("edges", [])
    node_ids = [node.get("id") for node in nodes if isinstance(node.get("id"), str)]
    seen: set[str] = set()
    for index, node_id in enumerate(node_ids):
        if node_id in seen:
            findings.append(_finding("region_graph.node_duplicate", f"duplicate region node: {node_id}",
                                     f"$.regions.nodes[{index}].id", node_id=node_id))
        seen.add(node_id)

    known = set(node_ids)
    seen_edges: set[tuple[str, str, str]] = set()
    valid_edges: list[dict[str, Any]] = []
    for index, edge in enumerate(edges):
        key = (edge.get("kind"), edge.get("from"), edge.get("to"))
        if key in seen_edges:
            findings.append(_finding("region_graph.edge_duplicate", "duplicate region edge",
                                     f"$.regions.edges[{index}]", edge=list(key)))
        seen_edges.add(key)
        missing = [endpoint for endpoint in (edge.get("from"), edge.get("to")) if endpoint not in known]
        if missing:
            findings.append(_finding("region_graph.edge_node_missing", "edge references missing node",
                                     f"$.regions.edges[{index}]", missing=missing, edge=edge))
        else:
            valid_edges.append(edge)

    order, cyclic = _topological_order(node_ids, valid_edges)
    if cyclic:
        findings.append(_finding("region_graph.cycle", "region graph contains a cycle", "$.regions.edges",
                                 nodes=cyclic))

    selector_nodes = [
        node for node in nodes
        if (node.get("selector") or node.get("start_selector") or node.get("end_selector")
            or node.get("metadata_bindings"))
    ]
    root: etree._Element | None = None
    candidates: list[tuple[etree._Element, int, int]] = []
    resolved_template: Path | None = template_path
    if selector_nodes and resolved_template is None:
        resolved_template = resource(profile, "official_docx")
    if selector_nodes:
        if resolved_template is None:
                findings.append(_finding("region_graph.template_missing", "selector preflight requires official DOCX",
                                     "$.resources"))
        else:
            try:
                with zipfile.ZipFile(resolved_template) as archive:
                    root = etree.fromstring(archive.read("word/document.xml"))
                candidates = list(iter_candidates(root))
            except (OSError, KeyError, zipfile.BadZipFile, etree.XMLSyntaxError) as exc:
                findings.append(_finding("region_graph.template_invalid", str(exc), "$.resources",
                                         template_path=str(resolved_template)))

    compiled_nodes: dict[str, dict[str, Any]] = {}
    selector_identity: dict[str, list[tuple[str, str]]] = {}
    resolved_ranges: list[tuple[str, int, int]] = []
    protected_positions: list[tuple[str, int]] = []
    if root is not None:
        for index, node in enumerate(nodes):
            resolved: dict[str, tuple[etree._Element, dict[str, Any]]] = {}
            compiled_bindings: list[dict[str, Any]] = []
            for binding_index, binding in enumerate(node.get("metadata_bindings", [])):
                matches = _selector_matches_in_template(root, candidates, binding["selector"])
                if len(matches) != 1:
                    findings.append(_finding(
                        "region_graph.metadata_selector_not_unique",
                        f"metadata selector must resolve exactly once; got {len(matches)}",
                        f"$.regions.nodes[{index}].metadata_bindings[{binding_index}].selector",
                        node_id=node["id"], field=binding["field"],
                    ))
                    continue
                _element, binding_locator = matches[0]
                compiled_bindings.append({
                    "field": binding["field"],
                    "locator": binding_locator,
                    "required": binding.get("required", False),
                    "missing_policy": binding.get("missing_policy", "preserve"),
                    "replace_mode": binding.get("replace_mode", "all_text"),
                    "prefix": binding.get("prefix"),
                    "value_transform": binding.get("value_transform"),
                })
            selector_fields = [
                field for field in ("selector", "start_selector", "end_selector") if node.get(field)
            ]
            for field in selector_fields:
                matches = _selector_matches_in_template(root, candidates, node[field])
                match_evidence = [
                    {"body_child_index": locator["body_child_index"],
                     "style_id": locator["style_id"], "text": normalize(node_text(element))[:120]}
                    for element, locator in matches[:10]
                ]
                if len(matches) != 1:
                    findings.append(_finding("region_graph.selector_not_unique",
                                             f"selector must resolve exactly once; got {len(matches)}",
                                             f"$.regions.nodes[{index}].{field}", node_id=node["id"],
                                             selector_field=field, matches=match_evidence))
                    continue
                resolved[field] = matches[0]

            if len(resolved) != len(selector_fields):
                continue
            if not selector_fields:
                compiled_nodes[node["id"]] = {"metadata_bindings": compiled_bindings}
            elif "selector" in resolved:
                element, locator = resolved["selector"]
                identity = f"{locator['part']}:{locator['xml_path']}"
                prior_uses = selector_identity.get(identity, [])
                if prior_uses:
                    findings.append(_finding("region_graph.selector_collision", "two regions resolve to the same OOXML node",
                                             f"$.regions.nodes[{index}].selector", node_id=node["id"],
                                             other_node_id=prior_uses[0][0], identity=identity))
                selector_identity.setdefault(identity, []).append((node["id"], "selector"))
                compiled_nodes[node["id"]] = {"locator": locator, "metadata_bindings": compiled_bindings}
                if node["kind"] == "fixed_protected":
                    compiled_nodes[node["id"]]["canonical_ooxml_sha256"] = _canonical_hash(element)
                    protected_positions.append((node["id"], locator["body_child_index"]))
            elif "start_selector" in resolved and ("end_selector" in resolved or node.get("end_boundary") == "body_end"):
                _, start = resolved["start_selector"]
                end = resolved.get("end_selector", (None, {"boundary": "body_end"}))[1]
                start_index = start["body_child_index"]
                body = root.find("w:body", namespaces=NS)
                if body is None:
                    findings.append(_finding("region_graph.template_invalid", "template has no w:body",
                                             f"$.regions.nodes[{index}]", node_id=node["id"]))
                    continue
                body_children = list(body)
                end_index = (len(body_children) - 1
                             if body_children and body_children[-1].tag == f"{{{NS['w']}}}sectPr"
                             else len(body_children))
                if "end_selector" in resolved:
                    end_index = end["body_child_index"]
                if start_index >= end_index:
                    findings.append(_finding(
                        "region_graph.range_invalid", "range start must precede range end",
                        f"$.regions.nodes[{index}]", node_id=node["id"],
                        start_body_child_index=start_index, end_body_child_index=end_index,
                    ))
                    continue
                boundary_locators = [("start_selector", start)]
                if "end_selector" in resolved:
                    boundary_locators.append(("end_selector", end))
                for field, locator in boundary_locators:
                    identity = f"{locator['part']}:{locator['xml_path']}"
                    prior_uses = selector_identity.get(identity, [])
                    compatible_shared_boundary = (
                        len(prior_uses) == 1
                        and prior_uses[0][0] != node["id"]
                        and {prior_uses[0][1], field} == {"start_selector", "end_selector"}
                    )
                    if prior_uses and not compatible_shared_boundary:
                        findings.append(_finding(
                            "region_graph.selector_collision", "two region boundaries resolve to the same OOXML node",
                            f"$.regions.nodes[{index}].{field}", node_id=node["id"],
                            other_node_id=prior_uses[0][0], identity=identity,
                        ))
                    selector_identity.setdefault(identity, []).append((node["id"], field))
                compiled_nodes[node["id"]] = {
                    "range_locator": {"start": start, "end": end, "policy": node["range_policy"]},
                    "metadata_bindings": compiled_bindings,
                }
                if node["range_policy"] == "preserve_between":
                    start_element = resolved["start_selector"][0]
                    end_element = resolved.get("end_selector", (None, None))[0]
                    protected_children = body_children[start_index:(end_index + 1 if end_element is not None else end_index)]
                    canonical = b"".join(
                        _canonical_bytes(child) for child in protected_children
                    )
                    compiled_nodes[node["id"]]["canonical_ooxml_sha256"] = hashlib.sha256(canonical).hexdigest()
                else:
                    resolved_ranges.append((node["id"], start_index, end_index))

        for left_index, (left_id, left_start, left_end) in enumerate(resolved_ranges):
            for right_id, right_start, right_end in resolved_ranges[left_index + 1:]:
                if max(left_start, right_start) < min(left_end, right_end):
                    findings.append(_finding(
                        "region_graph.range_overlap", "dynamic region ranges may not overlap",
                        "$.regions.nodes", left_node_id=left_id, right_node_id=right_id,
                    ))
            for protected_id, position in protected_positions:
                if left_start <= position <= left_end:
                    findings.append(_finding(
                        "region_graph.range_protected_overlap", "dynamic range includes a protected region",
                        "$.regions.nodes", node_id=left_id, protected_node_id=protected_id,
                    ))

    if findings:
        return _result(profile, declaration, [], findings, resolved_template)

    by_id = {node["id"]: node for node in nodes}
    incoming: dict[str, list[dict[str, Any]]] = {node_id: [] for node_id in node_ids}
    outgoing: dict[str, list[dict[str, Any]]] = {node_id: [] for node_id in node_ids}
    for edge in valid_edges:
        incoming[edge["to"]].append(edge)
        outgoing[edge["from"]].append(edge)

    operations = []
    protection_assertions = []
    for sequence, node_id in enumerate(order, 1):
        node = by_id[node_id]
        compiled = compiled_nodes.get(node_id, {})
        prerequisites = [
            {"node_id": edge["from"], "relation": edge["kind"], "policy": edge.get("policy")}
            for edge in incoming[node_id]
        ]
        operation = {
            "sequence": sequence,
            "node_id": node_id,
            "kind": node["kind"],
            "action": {
                "fixed_protected": "preserve",
                "dynamic_content": "replace_content",
                "generated": "generate_content",
                "optional": "conditionally_assemble",
            }[node["kind"]],
            "prerequisites": prerequisites,
            "locator": compiled.get("locator"),
            "range_locator": compiled.get("range_locator"),
            "content_role": node.get("content_role"),
            "condition": node.get("condition"),
            "metadata_bindings": compiled.get("metadata_bindings", []),
        }
        operations.append(operation)
        if node["kind"] == "fixed_protected":
            assertion = {
                "node_id": node_id,
                "locator": compiled.get("locator"),
                "range_locator": compiled.get("range_locator"),
                "hash_policy": CANONICAL_OOXML_HASH_POLICY,
                "expected_sha256": compiled["canonical_ooxml_sha256"],
            }
            protection_assertions.append(assertion)

    postconditions = [
        {"type": "topological_order", "from": edge["from"], "to": edge["to"], "relation": edge["kind"]}
        for edge in valid_edges
    ]
    for node_id in order:
        for edge in outgoing[node_id]:
            if edge["kind"] in {"boundary", "section_policy"}:
                postconditions.append({
                    "type": edge["kind"], "from": edge["from"], "to": edge["to"],
                    "policy": edge.get("policy", {}),
                })
    postconditions.extend(_deterministic_postconditions(nodes, compiled_nodes, root))

    plan = {
        "schema_version": "1.0",
        "profile_id": profile.get("profile_id"),
        "graph_id": declaration.get("graph_id"),
        "source_section_policy": declaration.get("source_section_policy", "reject"),
        "operations": operations,
        "preconditions": [
            {"type": "selector_unique", "node_id": node["id"]}
            for node in nodes
            if node.get("selector") or node.get("start_selector") or node.get("end_selector")
        ] + [{"type": "graph_acyclic"}, {"type": "edge_endpoints_exist"}],
        "protection_assertions": protection_assertions,
        "structural_postconditions": postconditions,
    }
    return _result(profile, declaration, [plan], findings, resolved_template)


def _result(profile: dict[str, Any], declaration: dict[str, Any] | None,
            plans: list[dict[str, Any]], findings: list[dict[str, Any]],
            template_path: Path | None = None) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "profile_id": profile.get("profile_id"),
        "graph_id": declaration.get("graph_id") if declaration else None,
        "status": "invalid" if findings else "compiled",
        "template_path": str(template_path) if template_path else None,
        "assembly_plan": plans[0] if plans else None,
        "findings": findings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path)
    parser.add_argument("--template", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    profile_path = args.profile.resolve()
    profile = strict_json_read(profile_path)
    template_path = args.template
    if template_path is None:
        official = resource(profile, "official_docx")
        if official is not None:
            template_path = official if official.is_absolute() else profile_path.parent / official
    result = compile_region_graph(profile, template_path=template_path)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "out": str(args.out),
                      "finding_count": len(result["findings"])}, ensure_ascii=False))
    return 0 if result["status"] == "compiled" else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
