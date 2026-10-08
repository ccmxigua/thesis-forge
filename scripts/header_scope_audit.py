"""Independent OOXML audit for section-scoped document headers."""
from __future__ import annotations

import hashlib
import json
import posixpath
import re
import unicodedata
import zipfile
from pathlib import Path
from typing import Any, Mapping

from lxml import etree
from docx import Document
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

from header_scope import compile_header_scope_plan


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"w": W, "r": R, "rel": REL}


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normal(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _anchor_normal(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()


def _part_for_target(target: str) -> str:
    if target.startswith("/"):
        return posixpath.normpath(target.lstrip("/"))
    return posixpath.normpath(posixpath.join("word", target))


def _visible_story(zf: zipfile.ZipFile, part: str) -> tuple[str, list[str]]:
    root = etree.fromstring(zf.read(part))
    paragraphs: list[str] = []
    instructions: list[str] = []
    for paragraph in root.xpath(".//w:p", namespaces=NS):
        paragraphs.append("".join(paragraph.xpath(".//w:t/text()", namespaces=NS)))
        instructions.extend(
            str(value).strip() for value in paragraph.xpath(".//w:instrText/text()", namespaces=NS)
        )
        instructions.extend(
            str(value).strip() for value in paragraph.xpath(".//w:fldSimple/@w:instr", namespaces=NS)
        )
    return "\n".join(paragraphs), instructions


def _section_anchor_in_output(paragraphs: list[Mapping[str, Any]], section_index: int,
                              anchor: Mapping[str, Any]) -> bool:
    expected_text = str(anchor.get("text", ""))
    kind = anchor.get("kind")
    for paragraph in paragraphs:
        if int(paragraph.get("section_index", 0)) != section_index:
            continue
        if _anchor_normal(str(paragraph.get("text", ""))) != _anchor_normal(expected_text):
            continue
        if kind == "toc_heading" and not (
            "toc" in _normal(str(paragraph.get("style_name", ""))).casefold()
            or _anchor_normal(expected_text) in {"目录", "目錄"}
        ):
            continue
        return True
    return False


def _output_paragraph_evidence(path: Path) -> list[dict[str, Any]]:
    """Read output anchors in body order, including paragraphs inside tables."""
    doc = Document(path)
    body = doc._body._element
    current_section = 1
    paragraphs: list[dict[str, Any]] = []
    for child in list(body):
        for element in child.iter(qn("w:p")):
            paragraph = Paragraph(element, doc._body)
            paragraphs.append({
                "text": paragraph.text,
                "style_name": getattr(paragraph.style, "name", ""),
                "section_index": current_section,
            })
            ppr = element.find(qn("w:pPr"))
            sectpr = ppr.find(qn("w:sectPr")) if ppr is not None else None
            if sectpr is not None and child.tag == qn("w:p") and element.getparent() is body:
                current_section += 1
    return paragraphs


def _header_relationships(zf: zipfile.ZipFile) -> dict[str, dict[str, str]]:
    names = set(zf.namelist())
    rel_path = "word/_rels/document.xml.rels"
    if rel_path not in names:
        return {}
    root = etree.fromstring(zf.read(rel_path))
    result: dict[str, dict[str, str]] = {}
    for rel in root:
        rel_id = rel.get("Id")
        if not rel_id:
            continue
        result[rel_id] = {
            "target": rel.get("Target", ""),
            "type": rel.get("Type", ""),
            "target_mode": rel.get("TargetMode", ""),
        }
    return result


def audit_scoped_headers(source_docx: str | Path, output_docx: str | Path,
                         spec: Mapping[str, Any],
                         format_spec_path: str | Path | None = None) -> dict[str, Any]:
    """Recompile from immutable inputs and audit section anchors, active refs and story text.

    The audit never consumes the writer's serialized scope plan. It independently
    resolves the current source, then follows every active headerReference through
    document.xml.rels and the effective linked-to-previous chain.
    """
    source = Path(source_docx)
    output = Path(output_docx)
    plan = compile_header_scope_plan(source, spec, format_spec_path)
    findings = list(plan.get("findings", []))
    if not plan.get("scoped"):
        return {
            "schema_version": "1.0", "valid": not any(x.get("severity") == "error" for x in findings),
            "scoped": False, "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest() if output.exists() else None,
            "sections_checked": 0, "findings": findings,
        }
    if not output.is_file():
        findings.append({"code": "header_scope_output_missing", "severity": "error",
                         "message": "The scoped-header output DOCX does not exist."})
        return {"schema_version": "1.0", "valid": False, "scoped": True,
                "source_sha256": plan["source_sha256"], "output_sha256": None,
                "sections_checked": 0, "findings": findings}

    output_document = Document(output)
    output_paragraphs = _output_paragraph_evidence(output)
    if len(output_document.sections) != len(plan.get("sections", [])):
        findings.append({"code": "header_scope_section_count_mismatch", "severity": "error",
                         "message": "Serialized output section count does not match the independently resolved source section map.",
                         "evidence": {"expected": len(plan.get("sections", [])),
                                      "actual": len(output_document.sections)}})

    document_root = None
    rels: dict[str, dict[str, str]] = {}
    names: set[str] = set()
    with zipfile.ZipFile(output) as zf:
        names = set(zf.namelist())
        if "word/document.xml" not in names:
            findings.append({"code": "header_scope_document_xml_missing", "severity": "error",
                             "message": "The output package has no word/document.xml."})
        else:
            document_root = etree.fromstring(zf.read("word/document.xml"))
        rels = _header_relationships(zf)
        sectprs = document_root.xpath("//w:sectPr", namespaces=NS) if document_root is not None else []
        if len(sectprs) != len(plan.get("sections", [])):
            findings.append({"code": "header_scope_serialized_sectpr_count_mismatch", "severity": "error",
                             "message": "The number of serialized sectPr elements differs from the source-anchored plan.",
                             "evidence": {"expected": len(plan.get("sections", [])), "actual": len(sectprs)}})
        settings = etree.fromstring(zf.read("word/settings.xml")) if "word/settings.xml" in names else None
        output_even_odd = bool(settings is not None and settings.find("w:evenAndOddHeaders", namespaces=NS) is not None)
        expected_text_by_part: dict[str, list[dict[str, Any]]] = {}
        effective_parts: dict[str, str | None] = {"default": None, "first": None, "even": None}
        section_evidence: list[dict[str, Any]] = []

        for index, expected_section in enumerate(plan.get("sections", []), 1):
            section_record: dict[str, Any] = {
                "section_index": index,
                "scope": expected_section.get("scope"),
                "source_anchor": expected_section.get("source_anchor"),
                "requirement_ids": expected_section.get("requirement_ids", []),
                "clause_ids": expected_section.get("clause_ids", []),
                "evidence_ids": expected_section.get("evidence_ids", []),
                "source_default_header_sha256": expected_section.get("source_default_header_sha256"),
                "variants": {},
            }
            section_evidence.append(section_record)
            if index > len(sectprs):
                continue
            anchor = expected_section.get("source_anchor")
            if isinstance(anchor, Mapping) and not _section_anchor_in_output(output_paragraphs, index, anchor):
                findings.append({"code": "header_scope_anchor_not_in_output_section", "severity": "error",
                                 "section_index": index,
                                 "message": "The source anchor mapped to this section is missing or moved to another output section.",
                                 "evidence": {"source_anchor": anchor}})
            if isinstance(anchor, Mapping):
                matching_other_sections = [
                    other_index for other_index in range(1, len(output_document.sections) + 1)
                    if other_index != index and _section_anchor_in_output(output_paragraphs, other_index, anchor)
                ]
                if matching_other_sections:
                    findings.append({"code": "header_scope_anchor_duplicated", "severity": "error",
                                     "section_index": index,
                                     "message": "A source header anchor also appears in another output section.",
                                     "evidence": {"other_sections": matching_other_sections, "anchor": anchor}})

            sectpr = sectprs[index - 1]
            title_pg = sectpr.find("w:titlePg", namespaces=NS) is not None
            expected_variants = expected_section.get("variants", {})
            expected_active = {
                "default": True,
                "first": bool(expected_variants.get("first", {}).get("active")),
                "even": bool(expected_variants.get("even", {}).get("active")),
            }
            actual_active = {"default": True, "first": title_pg, "even": output_even_odd}
            for variant in ("first", "even"):
                if expected_active[variant] != actual_active[variant]:
                    findings.append({"code": "header_scope_active_variant_mismatch", "severity": "error",
                                     "section_index": index,
                                     "message": f"Serialized {variant} header activation differs from the source-bound format plan.",
                                     "evidence": {"expected_active": expected_active[variant],
                                                  "actual_active": actual_active[variant]}})

            refs_by_type: dict[str, list[Any]] = {"default": [], "first": [], "even": []}
            for ref in sectpr.xpath("./w:headerReference", namespaces=NS):
                kind = ref.get(qn("w:type"), "default")
                if kind in refs_by_type:
                    refs_by_type[kind].append(ref)

            for variant in ("default", "first", "even"):
                contract = expected_variants.get(variant, {})
                if not contract.get("active"):
                    continue
                refs = refs_by_type[variant]
                if len(refs) > 1:
                    findings.append({"code": "header_scope_duplicate_active_reference", "severity": "error",
                                     "section_index": index, "variant": variant,
                                     "message": f"More than one active {variant} headerReference exists."})
                ref = refs[0] if refs else None
                part: str | None = None
                if ref is not None:
                    rel_id = ref.get(qn("r:id"))
                    rel = rels.get(str(rel_id)) if rel_id else None
                    if rel is None:
                        findings.append({"code": "header_scope_relationship_missing", "severity": "error",
                                         "section_index": index,
                                         "variant": variant,
                                         "message": f"Active {variant} headerReference has no document relationship.",
                                         "evidence": {"relationship_id": rel_id}})
                    elif rel.get("target_mode") == "External" or not rel.get("target"):
                        findings.append({"code": "header_scope_relationship_target_invalid", "severity": "error",
                                         "section_index": index,
                                         "variant": variant,
                                         "message": f"Active {variant} header relationship does not target an internal header part.",
                                         "evidence": rel})
                    elif not rel.get("type", "").endswith("/header"):
                        findings.append({"code": "header_scope_relationship_type_invalid", "severity": "error",
                                         "section_index": index,
                                         "variant": variant,
                                         "message": f"Active {variant} header relationship has a non-header relationship type.",
                                         "evidence": rel})
                    else:
                        part = _part_for_target(rel["target"])
                        if part not in names:
                            findings.append({"code": "header_scope_part_missing", "severity": "error",
                                             "section_index": index,
                                             "variant": variant,
                                             "message": f"Active {variant} header relationship points to a missing package part.",
                                             "evidence": {"part": part, "relationship_id": rel_id}})
                            part = None
                elif index > 1:
                    part = effective_parts.get(variant)
                effective_parts[variant] = part

                expected_text = str(contract.get("expected_text", ""))
                relationship = None
                relationship_id = None
                if ref is not None:
                    relationship_id = ref.get(qn("r:id"))
                    relationship = rels.get(str(relationship_id)) if relationship_id else None
                if expected_text and part is None:
                    findings.append({"code": "header_scope_active_reference_missing", "severity": "error",
                                     "section_index": index,
                                     "variant": variant,
                                     "message": f"The active {variant} header has non-empty expected content but no resolvable headerReference.",
                                     "evidence": {"scope": expected_section.get("scope"),
                                                  "expected_text": expected_text}})
                    section_record["variants"][variant] = {
                        "active": True,
                        "expected_text": expected_text,
                        "actual_text": "",
                        "relationship_id": relationship_id,
                        "relationship_target": relationship.get("target") if relationship else None,
                        "relationship_type": relationship.get("type") if relationship else None,
                        "part": None,
                        "linked_to_previous": bool(ref is None and index > 1),
                        "source_linked_to_previous": bool(contract.get("source_linked_to_previous")),
                    }
                    continue
                actual_text, actual_instructions = _visible_story(zf, part) if part else ("", [])
                section_record["variants"][variant] = {
                    "active": True,
                    "expected_text": expected_text,
                    "actual_text": actual_text,
                    "relationship_id": relationship_id,
                    "relationship_target": relationship.get("target") if relationship else None,
                    "relationship_type": relationship.get("type") if relationship else None,
                    "part": part,
                    "linked_to_previous": bool(ref is None and index > 1),
                    "source_linked_to_previous": bool(contract.get("source_linked_to_previous")),
                }
                if actual_text != expected_text:
                    findings.append({"code": "header_scope_text_mismatch", "severity": "error",
                                     "section_index": index,
                                     "variant": variant,
                                     "message": f"Serialized {variant} header text does not match the section's source scope.",
                                     "evidence": {"scope": expected_section.get("scope"),
                                                  "source_anchor": anchor,
                                                  "expected_text": expected_text,
                                                  "actual_text": actual_text,
                                                  "header_part": part}})
                expected_instructions = list(contract.get("source_instructions", []))
                expected_content = contract.get("header_content", {})
                if expected_content.get("right_field") == "styleref_heading_1":
                    if not any(value.upper().startswith("STYLEREF ") for value in actual_instructions):
                        findings.append({"code": "header_scope_styleref_missing", "severity": "error",
                                         "section_index": index,
                                         "variant": variant,
                                         "message": f"The active {variant} header omits the required STYLEREF field."})
                elif contract.get("content_mode") == "preserve_source" and actual_instructions != expected_instructions:
                    findings.append({"code": "header_scope_source_field_mismatch", "severity": "error",
                                     "section_index": index,
                                     "variant": variant,
                                     "message": f"The active {variant} source header field instructions were not preserved.",
                                     "evidence": {"expected": expected_instructions,
                                                  "actual": actual_instructions}})
                if part:
                    expected_text_by_part.setdefault(part, []).append({
                        "section_index": index, "variant": variant,
                        "scope": expected_section.get("scope"), "text": expected_text,
                    })

        for part, uses in expected_text_by_part.items():
            distinct = {(item["scope"], item["text"]) for item in uses}
            if len(distinct) > 1:
                findings.append({"code": "header_scope_shared_part_collision", "severity": "error",
                                 "message": "One shared header part is referenced by sections with different source scopes or expected text.",
                                 "evidence": {"header_part": part, "uses": uses}})

    valid = not any(item.get("severity") == "error" for item in findings)
    return {
        "schema_version": "1.0", "valid": valid, "scoped": True,
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "format_spec_sha256": plan.get("format_spec_sha256"),
        "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "sections_checked": len(plan.get("sections", [])),
        "sections": section_evidence,
        "findings": findings,
    }


def build_header_scope_review_ledger(audit: Mapping[str, Any]) -> dict[str, Any]:
    """Project independent serialized-header errors into a source-bound ledger.

    This is a technical failure ledger, not a human-decision marker list. It
    preserves the exact section/scope/anchor/provenance and OOXML finding so a
    failed header cannot disappear into a generic manual-review marker.
    """
    sections = {
        int(item.get("section_index")): item
        for item in audit.get("sections", [])
        if isinstance(item, Mapping) and isinstance(item.get("section_index"), int)
    }
    items: list[dict[str, Any]] = []
    for finding in audit.get("findings", []):
        if not isinstance(finding, Mapping) or finding.get("severity") != "error":
            continue
        section_index = finding.get("section_index")
        section = sections.get(section_index) if isinstance(section_index, int) else None
        evidence = finding.get("evidence") if isinstance(finding.get("evidence"), Mapping) else {}
        variant = finding.get("variant")
        variant_evidence = (
            section.get("variants", {}).get(variant)
            if isinstance(section, Mapping) and isinstance(variant, str)
            and isinstance(section.get("variants"), Mapping) else None
        )
        items.append({
            "finding_id": hashlib.sha256(json.dumps(
                dict(finding), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")).hexdigest(),
            "status": "pending_resolution",
            "code": str(finding.get("code") or "header_scope_audit_error"),
            "severity": "error",
            "message": str(finding.get("message") or "Source-scoped header audit failed."),
            "section_index": section_index if isinstance(section_index, int) else None,
            "scope": section.get("scope") if isinstance(section, Mapping) else finding.get("scope"),
            "variant": variant if isinstance(variant, str) else None,
            "source_anchor": section.get("source_anchor") if isinstance(section, Mapping) else None,
            "requirement_ids": list(section.get("requirement_ids", [])) if isinstance(section, Mapping) else [],
            "clause_ids": list(section.get("clause_ids", [])) if isinstance(section, Mapping) else [],
            "evidence_ids": list(section.get("evidence_ids", [])) if isinstance(section, Mapping) else [],
            "expected_text": evidence.get("expected_text") if isinstance(evidence.get("expected_text"), str) else (
                variant_evidence.get("expected_text") if isinstance(variant_evidence, Mapping) else None
            ),
            "actual_text": evidence.get("actual_text") if isinstance(evidence.get("actual_text"), str) else (
                variant_evidence.get("actual_text") if isinstance(variant_evidence, Mapping) else None
            ),
            "relationship_id": variant_evidence.get("relationship_id") if isinstance(variant_evidence, Mapping) else evidence.get("relationship_id"),
            "relationship_target": variant_evidence.get("relationship_target") if isinstance(variant_evidence, Mapping) else evidence.get("target"),
            "header_part": variant_evidence.get("part") if isinstance(variant_evidence, Mapping) else evidence.get("header_part"),
            "linked_to_previous": variant_evidence.get("linked_to_previous") if isinstance(variant_evidence, Mapping) else None,
            "finding": dict(finding),
            "action": "修复来源适用范围或活动页眉关系；在独立 source-to-output 审计通过前不得提交。",
        })
    return {
        "schema_version": "1.0",
        "protocol": "source_scoped_header_review_ledger_v1",
        "status": "blocked" if items else "passed",
        "submission_ready": False,
        "source_sha256": audit.get("source_sha256"),
        "format_spec_sha256": audit.get("format_spec_sha256"),
        "output_sha256": audit.get("output_sha256"),
        "sections_checked": audit.get("sections_checked", 0),
        "summary": {"total": len(items), "pending_resolution": len(items)},
        "items": items,
    }
