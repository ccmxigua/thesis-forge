#!/usr/bin/env python3
"""Export a concise review draft plus a separately editable full audit ledger.

The split removes only the scorecard display block whose complete text is
bound to the supplied scorecard JSON. Manual-review markers remain in the
thesis. Full current/historical entries are retained in an editable DOCX and a
machine-readable JSON ledger. Nothing in this module promotes readiness.
"""
from __future__ import annotations

import argparse
import collections
import copy
import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.shared import Inches, Pt, RGBColor
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from lxml import etree

from docx_semantics import all_body_paragraphs
from draft_scorecard import (PREFIX, STATUSES, _scorecard_semantics_valid, scorecard_lines,
                             audit_external_scorecard)
from manual_review_display import _text as _full_manual_marker_text
from semantic_contract import sha256_json, strict_json_read

NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
MR_START = re.compile(r"^【(MR-\d{4})｜人工待审】")
SC_START = re.compile(r"^【(SC-[0-9A-Za-z-]+)｜")


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    import os
    os.close(fd)
    tmp = Path(raw)
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def _ids_in_docx(path: Path) -> tuple[list[str], list[str]]:
    doc = Document(path)
    scorecard_ids: list[str] = []
    manual_ids: list[str] = []
    for paragraph in all_body_paragraphs(doc):
        match = SC_START.match(paragraph.text)
        if match:
            scorecard_ids.append(match.group(1))
        match = MR_START.match(paragraph.text)
        if match:
            manual_ids.append(match.group(1))
    return scorecard_ids, manual_ids


def _human_marker_ids(card: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for entry in card.get("entries", []):
        if entry.get("kind") != "human_review":
            continue
        marker_id = (entry.get("detail") or {}).get("marker_id")
        if not isinstance(marker_id, str) or not MR_START.match(f"【{marker_id}｜人工待审】"):
            raise ValueError("human-review scorecard entry lacks a valid marker_id")
        result.append(marker_id)
    if len(result) != len(set(result)):
        raise ValueError("duplicate human-review marker ids in scorecard")
    return result


def _manual_marker_projections(card: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Return exact ledger-bound source marker text and its concise paper form."""
    result: dict[str, dict[str, str]] = {}
    for entry in card.get("entries", []):
        if entry.get("kind") != "human_review":
            continue
        detail = entry.get("detail")
        if not isinstance(detail, dict):
            raise ValueError("human-review scorecard entry lacks its full source detail")
        marker_id = detail.get("marker_id")
        if not isinstance(marker_id, str) or not MR_START.match(f"【{marker_id}｜人工待审】"):
            raise ValueError("human-review scorecard entry lacks a valid marker_id")
        if marker_id in result:
            raise ValueError("duplicate human-review marker ids in scorecard")
        try:
            full_text = _full_manual_marker_text(detail)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"human-review marker {marker_id} cannot be reconstructed from its ledger") from exc
        result[marker_id] = {
            "full_text": full_text,
            "paper_text": f"【{marker_id}｜人工待审】详见同编号独立审查台账。",
        }
    return result


def _package_part_hashes(path: Path) -> dict[str, str]:
    with ZipFile(path) as archive:
        return {info.filename: hashlib.sha256(archive.read(info.filename)).hexdigest()
                for info in archive.infolist()}


def _xml_paragraph_text(node: etree._Element) -> str:
    parts: list[str] = []
    for child in node.iter():
        if child.tag == f"{{{NS['w']}}}t":
            parts.append(child.text or "")
        elif child.tag == f"{{{NS['w']}}}br":
            parts.append("\n")
        elif child.tag == f"{{{NS['w']}}}tab":
            parts.append("\t")
    return "".join(parts)


def _replace_marker_paragraph_text(node: etree._Element, value: str) -> None:
    """Replace visible marker text while retaining its paragraph/run formatting."""
    allowed_paragraph_children = {f"{{{NS['w']}}}pPr", f"{{{NS['w']}}}r"}
    if any(child.tag not in allowed_paragraph_children for child in node):
        raise ValueError("manual-review marker paragraph has unsupported non-run content")
    text_nodes = node.xpath(".//w:t", namespaces=NS)
    if not text_nodes:
        raise ValueError("manual-review marker paragraph has no text run")
    for run in node.xpath(".//w:r", namespaces=NS):
        if any(child.tag not in {f"{{{NS['w']}}}rPr", f"{{{NS['w']}}}t", f"{{{NS['w']}}}br"}
               for child in run):
            raise ValueError("manual-review marker run has unsupported content")
    for line_break in node.xpath(".//w:br", namespaces=NS):
        line_break.getparent().remove(line_break)
    text_nodes = node.xpath(".//w:t", namespaces=NS)
    text_nodes[0].text = value
    if value[:1].isspace() or value[-1:].isspace():
        text_nodes[0].set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    for text_node in text_nodes[1:]:
        text_node.text = ""


def strip_scorecard_display(source_docx: Path, baseline_docx: Path,
                            scorecard_path: Path, output_docx: Path,
                            report_path: Path) -> dict[str, Any]:
    """Remove the exact scorecard block and compact exact ledger-bound MR markers."""
    paths = [source_docx.resolve(), baseline_docx.resolve(), scorecard_path.resolve(),
             output_docx.resolve(), report_path.resolve()]
    if len(set(paths)) != len(paths):
        raise ValueError("scorecard split inputs and outputs must be distinct files")
    card = strict_json_read(scorecard_path)
    if not isinstance(card, dict) or not _scorecard_semantics_valid(card):
        raise ValueError("scorecard JSON fails its v3 semantic/status-count contract")
    if card.get("submission_ready") is not False:
        raise ValueError("review package exporter requires submission_ready=false")
    source_sha = digest(source_docx)
    binding = card.get("binding")
    if not isinstance(binding, dict) or binding.get("input_source_sha256") != source_sha:
        raise ValueError("scorecard input-source binding does not match source DOCX bytes")
    if binding.get("run_id") is None or binding.get("case_id") is None:
        raise ValueError("scorecard lacks a run/case identity")

    expected_lines = scorecard_lines(card)
    doc = Document(baseline_docx)
    paragraphs = list(doc.paragraphs)
    if len(paragraphs) < len(expected_lines):
        raise ValueError("baseline DOCX is shorter than its bound scorecard display")
    if [paragraph.text for paragraph in paragraphs[:len(expected_lines)]] != expected_lines:
        raise ValueError("baseline scorecard front block differs from the exact scorecard JSON")
    visible_sc_ids = [paragraph.text.split("｜", 1)[0][1:]
                      for paragraph in paragraphs[1:len(expected_lines)]]
    expected_sc_ids = [entry["item_id"] for entry in card["entries"]]
    if visible_sc_ids != expected_sc_ids:
        raise ValueError("baseline scorecard ID order differs from scorecard JSON")

    expected_mr_ids = _human_marker_ids(card)
    marker_projections = _manual_marker_projections(card)
    actual_sc_ids, actual_mr_ids = _ids_in_docx(baseline_docx)
    if actual_sc_ids != expected_sc_ids:
        raise ValueError("scorecard item IDs occur outside or differ from the declared display block")
    if len(actual_mr_ids) != len(set(actual_mr_ids)) or set(actual_mr_ids) != set(expected_mr_ids):
        raise ValueError("baseline manual-review markers do not exactly match scorecard marker IDs")
    baseline_doc = Document(baseline_docx)
    baseline_marker_texts = {
        match.group(1): paragraph.text
        for paragraph in all_body_paragraphs(baseline_doc)
        if (match := MR_START.match(paragraph.text))
    }
    if any(baseline_marker_texts.get(marker_id) != marker["full_text"]
           for marker_id, marker in marker_projections.items()):
        raise ValueError("baseline manual-review marker text is not an exact projection of the bound ledger")

    before_signature = sha256_json([p.text for p in paragraphs])
    # All scorecard paragraphs are the leading document-body block. Remove
    # these exact nodes from OOXML and copy every other package part bytewise.
    with ZipFile(baseline_docx) as source_zip:
        document_root = etree.fromstring(source_zip.read("word/document.xml"))
        body_paragraphs = document_root.xpath("./w:body/w:p", namespaces=NS)
        if len(body_paragraphs) < len(expected_lines):
            raise ValueError("scorecard paragraphs are not direct body paragraphs")
        xml_texts = [_xml_paragraph_text(node) for node in body_paragraphs[:len(expected_lines)]]
        if xml_texts != expected_lines:
            raise ValueError("OOXML scorecard block differs from its parsed DOCX text")
        compacted_ids: list[str] = []
        for node in document_root.xpath(".//w:p", namespaces=NS):
            text = _xml_paragraph_text(node)
            match = MR_START.match(text)
            if not match:
                continue
            marker_id = match.group(1)
            expected = marker_projections.get(marker_id)
            if expected is None or text != expected["full_text"]:
                raise ValueError("serialized manual-review marker differs from exact bound ledger text")
            _replace_marker_paragraph_text(node, expected["paper_text"])
            compacted_ids.append(marker_id)
        if (len(compacted_ids) != len(set(compacted_ids))
                or set(compacted_ids) != set(expected_mr_ids)):
            raise ValueError("serialized marker projection inventory differs from the scorecard")
        body = document_root.find("w:body", namespaces=NS)
        if body is None:
            raise ValueError("baseline DOCX has no body")
        for node in body_paragraphs[:len(expected_lines)]:
            body.remove(node)
        rewritten_document_xml = etree.tostring(
            document_root, encoding="UTF-8", xml_declaration=True, standalone=True,
        )
        output_docx.parent.mkdir(parents=True, exist_ok=True)
        with ZipFile(output_docx, "w", ZIP_DEFLATED) as output_zip:
            for info in source_zip.infolist():
                data = (rewritten_document_xml if info.filename == "word/document.xml"
                        else source_zip.read(info.filename))
                output_zip.writestr(info, data)

    output_sc_ids, output_mr_ids = _ids_in_docx(output_docx)
    if output_sc_ids:
        raise ValueError("scorecard item paragraphs remain in the thesis DOCX")
    if output_mr_ids != actual_mr_ids:
        raise ValueError("manual-review markers changed while compacting the scorecard block")
    output_paragraphs = list(Document(output_docx).paragraphs)
    expected_remaining = [paragraph.text for paragraph in paragraphs[len(expected_lines):]]
    expected_remaining = [
        marker_projections[match.group(1)]["paper_text"] if (match := MR_START.match(text)) else text
        for text in expected_remaining
    ]
    if [paragraph.text for paragraph in output_paragraphs] != expected_remaining:
        raise ValueError("document text differs from exact scorecard removal and marker compaction")
    after_parts = _package_part_hashes(output_docx)
    before_parts = _package_part_hashes(baseline_docx)
    changed_parts = sorted(name for name in before_parts
                           if before_parts[name] != after_parts.get(name))
    if changed_parts != ["word/document.xml"]:
        raise ValueError("scorecard split changed unexpected DOCX package parts")

    report = {
        "schema_version": "1.0", "protocol": "review_package_scorecard_split_v1",
        "status": "complete", "submission_ready": False,
        "model_request_made": False, "semantic_review_is_new": False,
        "source_docx": {"path": str(source_docx.resolve()), "bytes": source_docx.stat().st_size,
                        "sha256": source_sha},
        "baseline_docx": {"path": str(baseline_docx.resolve()), "bytes": baseline_docx.stat().st_size,
                          "sha256": digest(baseline_docx)},
        "scorecard": {"path": str(scorecard_path.resolve()), "sha256": digest(scorecard_path),
                      "entry_count": len(card["entries"]),
                      "status_counts": copy.deepcopy(card["status_counts"]),
                      "parent_run_id": binding["run_id"], "case_id": binding["case_id"]},
        "output_docx": {"path": str(output_docx.resolve()), "bytes": output_docx.stat().st_size,
                        "sha256": digest(output_docx)},
        "removed_scorecard_display_paragraphs": len(expected_lines),
        "removed_scorecard_item_ids": expected_sc_ids,
        "retained_manual_review_marker_count": len(output_mr_ids),
        "retained_manual_review_marker_ids": output_mr_ids,
        "compacted_manual_review_marker_count": len(compacted_ids),
        "compacted_manual_review_marker_ids": compacted_ids,
        "manual_review_marker_projection_sha256": sha256_json({
            marker_id: {"full_text_sha256": hashlib.sha256(marker["full_text"].encode("utf-8")).hexdigest(),
                        "paper_text": marker["paper_text"]}
            for marker_id, marker in marker_projections.items()
        }),
        "body_paragraph_text_sha256_before_and_after_removal": sha256_json(expected_remaining),
        "baseline_all_paragraph_text_sha256": before_signature,
        "changed_ooxml_parts": changed_parts,
        "submission_ready": False,
    }
    report["audit_sha256"] = sha256_json(report)
    _write_json(report_path, report)
    return report


def _source_refs(entry: dict[str, Any]) -> dict[str, Any]:
    detail = entry.get("detail") if isinstance(entry.get("detail"), dict) else {}
    keys = (
        "requirement_id", "clause_ids", "evidence_ids", "question_ids",
        "manual_obligation_id", "marker_id", "evaluation_unit_id",
        "evaluation_unit_ids", "canonical_obligation_key", "source_sha256",
        "source_span_sha256", "role", "property_path", "target_locator",
        "source_code", "source_text", "category", "kind", "code",
    )
    return {key: copy.deepcopy(detail[key]) for key in keys if key in detail}


def _add_table_header_repeat(row: Any) -> None:
    tr_pr = row._tr.get_or_add_trPr()
    repeat = OxmlElement("w:tblHeader")
    repeat.set(qn("w:val"), "true")
    tr_pr.append(repeat)


def _set_cell(cell: Any, text: str, *, bold: bool = False, size: float = 7.0,
              color: str | None = None) -> None:
    cell.text = str(text)
    for paragraph in cell.paragraphs:
        paragraph.paragraph_format.space_after = Pt(0)
        paragraph.paragraph_format.space_before = Pt(0)
        for run in paragraph.runs:
            run.font.name = "Noto Sans CJK SC"
            run.font.size = Pt(size)
            run.bold = bold
            if color:
                run.font.color.rgb = RGBColor.from_string(color)


def _detail_text(entry: dict[str, Any]) -> str:
    return json.dumps(entry.get("detail", {}), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


_ROLE_LABELS_ZH = {
    "abstract_body_en": "英文摘要正文", "abstract_body_zh": "中文摘要正文",
    "abstract_title_en": "英文摘要标题", "abstract_title_zh": "中文摘要标题",
    "bibliography_entry": "参考文献条目", "bibliography_heading": "参考文献标题",
    "body_text": "论文正文", "equation": "公式", "figure_caption": "图题",
    "footer": "页脚", "footnote": "脚注", "header": "页眉",
    "cover_field_value": "封面字段",
    "heading_1": "一级标题", "heading_2": "二级标题", "heading_3": "三级标题",
    "heading_4": "四级标题", "heading_acknowledgments": "致谢/后记标题",
    "heading_appendix": "附录标题", "heading_conclusion": "结论标题",
    "heading_publications": "学术成果标题", "heading_references": "参考文献标题",
    "keywords_en": "英文关键词", "keywords_zh": "中文关键词", "table_caption": "表题",
    "table_text": "表格正文", "thesis_affiliation_date": "论文单位与日期栏",
    "thesis_author": "中文作者栏", "thesis_author_en": "英文作者栏",
    "thesis_classified_index": "分类号栏", "thesis_title_en": "英文论文题名",
    "thesis_title_zh": "中文论文题名", "thesis_type_en": "英文学位类型",
    "thesis_type_zh": "中文学位类型", "toc": "目录正文", "toc_title": "目录标题",
}


def _role_label(role: Any) -> str | None:
    return _ROLE_LABELS_ZH.get(role) if isinstance(role, str) else None


def _load_source_review_bundle(requirements_dir: Path,
                               requirements_docx_path: Path) -> dict[str, Any]:
    """Load and verify the existing source-bound requirement artifacts.

    This validates byte bindings only.  A false semantic provenance flag is
    retained and surfaced; it is never repaired or promoted here.
    """
    paths = {
        "format_spec": requirements_dir / "format-spec.json",
        "requirement_clauses": requirements_dir / "requirement-clauses.json",
        "extraction_manifest": requirements_dir / "extraction-manifest.json",
        "requirements_input_manifest": requirements_dir / "requirements-input-manifest.json",
    }
    objects = {key: strict_json_read(path) for key, path in paths.items()}
    spec = objects["format_spec"]
    clauses = objects["requirement_clauses"]
    extraction = objects["extraction_manifest"]
    input_manifest = objects["requirements_input_manifest"]
    if (not isinstance(spec, dict) or not isinstance(clauses, list)
            or not isinstance(extraction, dict) or not isinstance(input_manifest, dict)):
        raise ValueError("source requirement bundle has an invalid JSON shape")

    source_sha = digest(requirements_docx_path)
    original = input_manifest.get("original", {})
    normalized = input_manifest.get("normalized", {})
    if (source_sha != extraction.get("source_sha256")
            or source_sha != original.get("sha256")
            or source_sha != normalized.get("sha256")):
        raise ValueError("requirements DOCX bytes do not match the bound extraction inputs")
    if (extraction.get("run_id") != spec.get("run_id")
            or extraction.get("source_document") != spec.get("source_document")):
        raise ValueError("format spec does not match the source extraction run")
    if (extraction.get("format_spec_sha256") != digest(paths["format_spec"])
            or extraction.get("requirement_clauses_sha256") != digest(paths["requirement_clauses"])):
        raise ValueError("source requirement bundle hashes do not match extraction manifest")
    expected_input = extraction.get("requirements_input_normalization", {})
    if (expected_input.get("normalization_id") != input_manifest.get("normalization_id")
            or expected_input.get("original", {}).get("sha256") != source_sha):
        raise ValueError("requirements input manifest does not match extraction manifest")
    if spec.get("semantic_review_provenance") != extraction.get("semantic_review_provenance"):
        raise ValueError("format spec semantic provenance differs from its extraction manifest")

    reqs = spec.get("requirements")
    if not isinstance(reqs, list) or any(not isinstance(row, dict) for row in reqs):
        raise ValueError("format spec requirements are not a list of objects")
    req_ids = [row.get("id") for row in reqs]
    if len(req_ids) != len(set(req_ids)):
        raise ValueError("format spec contains duplicate requirement ids")
    clause_ids = [row.get("id") for row in clauses if isinstance(row, dict)]
    if len(clause_ids) != len(clauses) or len(clause_ids) != len(set(clause_ids)):
        raise ValueError("source clause catalog contains duplicate or invalid ids")

    source_paragraphs = {
        _normalize_source_text(paragraph.text)
        for paragraph in all_body_paragraphs(Document(requirements_docx_path))
        if _normalize_source_text(paragraph.text)
    }

    return {
        "requirements_docx_path": requirements_docx_path.resolve(),
        "requirements_docx_sha256": source_sha,
        "paths": paths,
        "hashes": {key: digest(path) for key, path in paths.items()},
        "format_spec": spec,
        "clauses": clauses,
        "extraction_manifest": extraction,
        "requirements_input_manifest": input_manifest,
        "semantic_review_provenance_valid": spec.get("semantic_review_provenance_valid") is True,
        "source_paragraphs": source_paragraphs,
    }


def _normalize_source_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _clause_is_source_bound(clause: dict[str, Any], bundle: dict[str, Any]) -> bool:
    full_text = clause.get("source_text_full") or clause.get("source_evidence_text")
    span = clause.get("source_span") or {}
    if not isinstance(full_text, str) or not full_text:
        return False
    if hashlib.sha256(full_text.encode("utf-8")).hexdigest() != span.get("source_sha256"):
        return False
    return _normalize_source_text(full_text) in bundle["source_paragraphs"]


def _build_source_crosswalk(card: dict[str, Any], bundle: dict[str, Any] | None
                            ) -> list[dict[str, Any]]:
    """Resolve human review entries only through explicit bound requirement ids."""
    reqs_by_id: dict[str, dict[str, Any]] = {}
    clauses_by_id: dict[str, dict[str, Any]] = {}
    if bundle:
        reqs_by_id = {row["id"]: row for row in bundle["format_spec"]["requirements"]}
        clauses_by_id = {row["id"]: row for row in bundle["clauses"]}
    result: list[dict[str, Any]] = []
    for entry in card.get("entries", []):
        if entry.get("kind") != "human_review":
            continue
        detail = entry.get("detail") or {}
        requirement_ids = list(detail.get("requirement_ids") or [])
        explicit_clause_ids = list(detail.get("clause_ids") or [])
        explicit_evidence_ids = list(detail.get("evidence_ids") or [])
        resolved: list[dict[str, Any]] = []
        problems: list[str] = []
        current_output_instance = copy.deepcopy(detail.get("current_output_instance") or {})
        paper_location_present = any(
            current_output_instance.get(key)
            for key in ("location", "paragraph_location", "page", "paragraph", "target_locator")
        )
        if bundle:
            if requirement_ids:
                for requirement_id in requirement_ids:
                    requirement = reqs_by_id.get(requirement_id)
                    if requirement is None:
                        problems.append("requirement_id_not_found")
                        continue
                    requirement_clauses = list(requirement.get("clause_ids") or [])
                    requirement_evidence = list(requirement.get("evidence_ids") or [])
                    if not requirement_clauses or not requirement_evidence:
                        problems.append("requirement_source_links_missing")
                    for clause_id in requirement_clauses:
                        clause = clauses_by_id.get(clause_id)
                        if clause is None:
                            problems.append("clause_id_not_found")
                            continue
                        clause_evidence = set(clause.get("evidence_ids") or [])
                        if not set(requirement_evidence).issubset(clause_evidence):
                            problems.append("requirement_evidence_not_backed_by_clause")
                            continue
                        span = clause.get("source_span") or {}
                        evidence_id = span.get("evidence_id")
                        if evidence_id and evidence_id not in clause_evidence:
                            problems.append("clause_span_evidence_mismatch")
                            continue
                        if not _clause_is_source_bound(clause, bundle):
                            problems.append("clause_text_not_bound_to_source_docx")
                            continue
                        resolved.append({
                            "requirement_id": requirement_id,
                            "role": requirement.get("role"),
                            "properties": copy.deepcopy(requirement.get("properties") or {}),
                            "applicability": copy.deepcopy(requirement.get("applicability") or {}),
                            "requirement_source_text": requirement.get("source_text"),
                            "clause_id": clause_id,
                            "evidence_ids": requirement_evidence,
                            "clause_text": clause.get("text"),
                            "clause_source_text_full": clause.get("source_text_full"),
                            "source_span": copy.deepcopy(span),
                            "context_before": copy.deepcopy(clause.get("context_before") or []),
                            "context_after": copy.deepcopy(clause.get("context_after") or []),
                            "clause_location": copy.deepcopy(clause.get("location") or {}),
                        })
            elif explicit_clause_ids:
                for clause_id in explicit_clause_ids:
                    clause = clauses_by_id.get(clause_id)
                    if clause is None:
                        problems.append("clause_id_not_found")
                        continue
                    if explicit_evidence_ids and not set(explicit_evidence_ids).issubset(
                            set(clause.get("evidence_ids") or [])):
                        problems.append("explicit_evidence_not_backed_by_clause")
                        continue
                    if not _clause_is_source_bound(clause, bundle):
                        problems.append("clause_text_not_bound_to_source_docx")
                        continue
                    span = clause.get("source_span") or {}
                    resolved.append({
                        "requirement_id": None, "role": None, "properties": {},
                        "applicability": {}, "requirement_source_text": None,
                        "clause_id": clause_id,
                        "evidence_ids": explicit_evidence_ids or list(clause.get("evidence_ids") or []),
                        "clause_text": clause.get("text"),
                        "clause_source_text_full": clause.get("source_text_full"),
                        "source_span": copy.deepcopy(span),
                        "context_before": copy.deepcopy(clause.get("context_before") or []),
                        "context_after": copy.deepcopy(clause.get("context_after") or []),
                        "clause_location": copy.deepcopy(clause.get("location") or {}),
                    })
            if explicit_clause_ids and resolved:
                derived_clause_ids = {row["clause_id"] for row in resolved}
                if not set(explicit_clause_ids).issubset(derived_clause_ids):
                    problems.append("scorecard_clause_refs_conflict_with_requirement")
            if explicit_evidence_ids and resolved:
                derived_evidence_ids = {
                    evidence_id for row in resolved for evidence_id in row["evidence_ids"]
                }
                if not set(explicit_evidence_ids).issubset(derived_evidence_ids):
                    problems.append("scorecard_evidence_refs_conflict_with_requirement")

        state = ("resolved" if resolved and not problems else
                 "conflict" if problems else "unlinked")
        result.append({
            "scorecard_item_id": entry.get("item_id"),
            "marker_id": detail.get("marker_id"),
            "status": entry.get("status"),
            "source_type": detail.get("source_type"),
            "source_code": detail.get("source_code"),
            "category": detail.get("category"),
            "requirement_ids": requirement_ids,
            "explicit_clause_ids": explicit_clause_ids,
            "explicit_evidence_ids": explicit_evidence_ids,
            "question_ids": list(detail.get("question_ids") or []),
            "source_text": detail.get("source_text"),
            "reason": detail.get("reason"),
            "action": detail.get("action"),
            "current_output_instance": current_output_instance,
            "paper_location_present": bool(paper_location_present),
            "original_blocking": detail.get("original_blocking"),
            "crosswalk_status": state,
            "crosswalk_problems": sorted(set(problems)),
            "resolved_source_records": resolved if state == "resolved" else [],
        })
    return result


def _is_conditional_english_font_rule(record: dict[str, Any]) -> bool:
    clause_text = " ".join(str(record.get("clause_text") or "").split())
    applicability = record.get("applicability") or {}
    return (
        "Times New Roman" in clause_text
        and ("出现英文" in clause_text or _plain_applicability(applicability) == "论文中出现英文时")
    )


def _actionable_manual_text(item: dict[str, Any]) -> tuple[str, str, str]:
    source = _clean_user_text(item.get("source_text"))
    reason = _clean_user_text(item.get("reason"))
    action = _clean_user_text(item.get("action"))
    combined = " ".join((source, reason, action)).casefold()

    spine_cover_fields = (
        "cover_field_value" in combined
        and any(term in combined for term in ("论文题目", "研究生姓名", "题名", "姓名"))
    ) or (
        "3cm" in combined
        and any(term in combined for term in ("论文题目", "研究生姓名", "题名", "姓名"))
    )
    if spine_cover_fields:
        return (
            "模板文本框的封面或书脊归属待核",
            "模板把论文题目、研究生姓名和约3cm标注放在独立文本框中；现有记录没有确认这些框属于封面还是书脊。",
            "查看模板原页，核对这些文本框的位置和方向，确认封面或书脊归属后再评估对应格式；确认前保持待核验。",
        )

    if "unit_code" in combined or "培养单位代码" in combined:
        return (
            "封面培养单位代码待确认",
            "输入资料没有用户确认值，生成稿目前使用中性占位符。",
            "提供或确认论文封面的培养单位/院系代码，再检查封面字段。",
        )

    if "中文摘要" in source and "agent无法可靠判断" in action.casefold():
        evidence = ""
        if "原文证据：" in reason:
            evidence = reason.split("原文证据：", 1)[1].strip(" ；。")
        excerpts = [part.strip() for part in evidence.split(" / ") if part.strip()]
        if "全文包含三章编号正文" in reason:
            searchable = " / ".join(f"“{part}”" for part in excerpts)
            return (
                "中文摘要内容与凝练度",
                f"可在论文中搜索这些摘要原句：{searchable}。判断“全文包含三章编号正文……”是否属于摘要核心内容，以及是否影响摘要凝练度。",
                "核对摘要是否清楚交代研究目的、方法和主要结果，是否有独立结论句；判断结构说明是否挤占这些必要信息，并记录具体修改句。",
            )
        if "独立的结论句" in reason:
            searchable = " / ".join(f"“{part}”" for part in excerpts)
            return (
                "中文摘要结论表达",
                f"可在论文中搜索这些摘要原句：{searchable}。现有记录指出末段列出创新点与训练策略，但没有识别到清楚、独立的结论句。",
                "检查摘要末段是否有清楚、独立的结论句总结研究发现，并与正文结论段对照；记录摘要和正文对应位置及判断依据。",
            )

    if "keywords_zh.max_item_chars" in combined or "keywords_en.max_item_chars" in combined:
        return (
            "关键词长度规则的适用对象待核",
            "原文写作“up to 7 Chinese characters”；当前台账无法确定这条限制对应哪组关键词。",
            "对照模板原页的中英文关键词标题，确认限制对象；保留原文“Chinese characters”的度量，确认前不换算为英文字符数。",
        )

    if "关键词是为了便于" in source or "关键词从论文中选取" in source:
        return (
            "关键词须能代表论文主题并有文内出处",
            "该语义要求需要逐个核对关键词与论文正文的对应关系，自动检查无法判定。",
            "在论文中逐个搜索关键词，确认其能代表全文主题并标出对应内容位置；无法找到出处的词保持待核。",
        )

    if "the following english is not correct" in combined:
        return (
            "英文摘要条文的语言与计量单位待核",
            "原文先称后续英文不正确，后续内容却描述中国摘要并用 words 计量；需按模板原页确认适用语言和单位。",
            "核对该段标题、语言和长度单位，记录应遵循的原文条款后再检查英文摘要。",
        )

    if source.casefold().startswith("the chinese abstract is a brief statement of the content"):
        return (
            "摘要语言与长度计量待核",
            "来源段落称其对象为 Chinese abstract，却使用 words 计数；具体原文和相邻说明见技术附件。",
            "查看模板中该段标题和完整段落，确认它适用于中文摘要还是英文摘要，以及长度按字、字符还是单词计算；确认前不套用该限制。",
        )

    if "agent无法可靠判断" in action.casefold():
        return (
            source,
            reason,
            "对照来源条文核验对应论文内容，记录论文位置与具体依据；证据不足时保持待人工。",
        )
    return source, reason, action


def _is_reference_sample_numbering_issue(item: dict[str, Any]) -> bool:
    reason = _clean_user_text(item.get("reason"))
    source = _clean_user_text(item.get("source_text"))
    return (
        item.get("source_type") == "release_gate"
        and "编号" in reason
        and "类别" in reason
        and "示例" in reason
        and any(token in source for token in ("[1]", "[2]", "[3]", "[4]"))
    )


def _render_human_review_section(card: dict[str, Any], bundle: dict[str, Any] | None,
                                 crosswalk: list[dict[str, Any]],
                                 technical_json_path: Path | None) -> list[str]:
    manual = [entry for entry in card.get("entries", []) if entry.get("kind") == "human_review"]
    lines = [
        "## 人工复核指引",
        "",
        "以下事项均未完成核验。未核验或待人工不表示通过；自动检查器没有能力执行某项检查，也不代表已经失败。",
        "",
    ]
    if technical_json_path:
        lines.append(
            f"条款与标记的逐项编号映射见 [{technical_json_path.name}]({technical_json_path.name})；"
            "完整机器台账另见上方链接。"
        )
        lines.append("")
    if not manual:
        lines.extend(["当前没有人工复核条目。", ""])
        return lines

    counts = collections.Counter(row.get("source_type") or "unknown" for row in crosswalk)
    lines.extend([
        f"当前共有 {len(manual)} 项人工待办，其中 {counts.get('capability_finding', 0)} 项是自动检查能力不足，"
        f"{counts.get('open_question', 0)} 项需要澄清条款，"
        f"{counts.get('current_session_semantic_review', 0)} 项需要作者判断，"
        f"{counts.get('input_prerequisite', 0) + counts.get('release_gate', 0)} 项需要补齐材料或门槛。",
        "",
    ])

    capability = [row for row in crosswalk if row.get("source_type") == "capability_finding"]
    if capability:
        mapped = [row for row in capability if row.get("crosswalk_status") == "resolved"]
        unmapped = len(capability) - len(mapped)
        target_unmapped = [
            row for row in mapped
            if not any(record.get("role") for record in row.get("resolved_source_records", []))
        ]
        lines.extend([
            "### 自动检查器无法判定的格式项目",
            "",
            f"{len(capability)} 项标记为未能自动核验；其中 {len(mapped)} 项能回查到来源条款，"
            f"{unmapped} 项的来源交叉引用缺失或冲突。它们都没有精确的论文段落/页码核验回执；"
            "当前记录中的定位字段只有‘人工复核仍待处理’。"
            "这类状态只表示工具没有完成检查，不表示论文内容错误，也不表示已经通过。",
            "",
        ])
        groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
        target_unmapped_groups: dict[str, list[dict[str, Any]]] = {}
        for item in capability:
            records = item.get("resolved_source_records") or []
            if item.get("crosswalk_status") != "resolved" or not records:
                groups.setdefault(("unlinked", "", "", ""), []).append(item)
                continue
            for record in records:
                if not record.get("role"):
                    quote = " ".join(str(record.get("clause_text") or "").split())
                    target_unmapped_groups.setdefault(quote, []).append(item)
                    continue
                quote = " ".join(str(record.get("clause_text") or "").split())
                property_key = json.dumps(record.get("properties") or {}, ensure_ascii=False, sort_keys=True)
                condition_key = json.dumps(record.get("applicability") or {}, ensure_ascii=False, sort_keys=True)
                groups.setdefault(("resolved", quote, property_key, condition_key), []).append(
                    {**item, "record": record}
                )

        source_provenance_valid = bool(bundle and bundle.get("semantic_review_provenance_valid"))
        for (state, quote, properties_key, condition_key), group in groups.items():
            if state == "unlinked":
                lines.extend([
                    f"- **来源条款尚未关联：{len(group)} 项。** 当前记录无法绑定到唯一来源条款；请在学校原始要求中找到对应条款，"
                    "再对照论文位置核验并记录依据。找不到唯一条款时，保持待确认。",
                ])
                continue
            first = group[0]["record"]
            roles = sorted({
                _role_label(item["record"].get("role")) or "未识别的格式区域"
                for item in group
            })
            lines.append(f"- **同一来源要求的人工核对：** {len(group)} 条记录映射到 {len(roles)} 个格式区域。来源原文：“{quote}”")
            if first.get("properties"):
                lines.append(f"  - 格式记录：{_plain_properties(first['properties'])}。")
            applicability = _plain_applicability(first.get("applicability"))
            if applicability:
                lines.append(f"  - 契约记录的适用条件：{applicability}。")
            lines.append("  - 被映射的区域：" + "、".join(roles) + "。")
            if _is_conditional_english_font_rule(first):
                lines.append(
                    "  - **按来源原文核对：** 这条文字明确规定论文中出现英文时使用 Times New Roman。"
                    "请逐一检查上述区域中实际出现的英文，记录论文位置、实测字体和依据；当前缺少逐区域位置与实测回执，相关项保持未核验。"
                )
            elif not source_provenance_valid:
                lines.append("  - **契约来源校验限制：** 这里只展示绑定原文和现有区域映射；校验标记无效不改变原文，也不代表论文已通过核验。")
            source_full_texts = list(dict.fromkeys(
                _clean_user_text(item["record"].get("clause_source_text_full"))
                for item in group if item["record"].get("clause_source_text_full")
            ))
            if source_full_texts:
                # The full source paragraphs reveal when repeated wording is part
                # of a sample or dummy text; keep those passages visible.
                sample_contexts = [text for text in source_full_texts if "编的" in text or "示例" in text]
                if _is_conditional_english_font_rule(first):
                    sample_contexts = []
                if sample_contexts:
                    lines.append(f"  - 原文同段还注明：“{_compact_plain(sample_contexts[0], 280)}”")
            if not _is_conditional_english_font_rule(first):
                lines.append("  - 按来源要求检查对应论文区域，记录位置、实际格式和依据；缺少定位或实测回执时保持未核验。")
        if target_unmapped:
            lines.append(
                f"- **论文位置尚未建立：{len(target_unmapped)} 项来源条款已找到，但没有指向论文区域的格式角色。** "
                "需补充具体章节/段落或属性定位后才能检查；当前缺少论文定位证据。"
            )
            samples = list(target_unmapped_groups)
            if samples:
                lines.append("  - 来源文本示例：“" + "”；“".join(
                    _clean_user_text(value) for value in samples[:4]
                ) + "”。")
        lines.append("")

    reference_sample_issues = [row for row in crosswalk if _is_reference_sample_numbering_issue(row)]
    other = [
        row for row in crosswalk
        if row.get("source_type") != "capability_finding"
        and not _is_reference_sample_numbering_issue(row)
    ]
    if other:
        lines.extend(["### 内容、来源疑问和输入缺项", ""])
        labels = {
            "open_question": "需澄清来源条款",
            "current_session_semantic_review": "需作者判断论文内容",
            "input_prerequisite": "需补充输入信息",
        }
        for item in other:
            source_type = item.get("source_type")
            if source_type == "release_gate" and item.get("category") == "semantic_content_review":
                label = "发布前需完成人工内容或来源核验"
            elif source_type == "release_gate":
                label = "需补齐发布材料"
            else:
                label = labels.get(source_type, "待人工处理")
            source, reason, action = _actionable_manual_text(item)
            source = _compact_plain(source, 360)
            reason = _compact_plain(reason, 440)
            action = _compact_plain(action, 320)
            lines.append(f"- **{label}。** {source}")
            if reason and reason != source:
                lines.append(f"  - 为什么待办：{reason}")
            if action:
                if "submission 模式" in action:
                    action = "补齐或确认所述材料；当前草稿仍不可提交。"
                lines.append(f"  - 下一步：{action}")
            records = item.get("resolved_source_records") or []
            hide_full_source_quote = (
                (
                    source_type == "current_session_semantic_review"
                    and str(item.get("source_code") or "").startswith("semantic_review:abstract_zh.")
                )
                or _clean_user_text(item.get("source_text")).casefold().startswith(
                    "the chinese abstract is a brief statement of the content"
                )
                or _clean_user_text(item.get("source_text")).startswith("关键词是为了便于")
            )
            if records and not hide_full_source_quote:
                quoted = "；".join(dict.fromkeys(
                    _clean_user_text(record.get("clause_text")) for record in records
                    if record.get("clause_text")
                ))
                if quoted and quoted != source:
                    lines.append(f"  - 来源条文：“{_compact_plain(quoted, 300)}”")
        lines.append("")
    if reference_sample_issues:
        lines.extend([
            f"- **参考文献示例编号：** 有 {len(reference_sample_issues)} 条既有待办仅因分类示例从 [1] 重新编号而提出疑问；示例编号本身不能证明最终参考文献应按类别重启，也不足以确认存在来源冲突。本指引不把它们列为已证实冲突；原始条目和证据保留在技术附件。",
            "",
        ])
    if bundle and not bundle.get("semantic_review_provenance_valid"):
        lines.extend([
            "### 来源解释的边界",
            "",
            "原始要求 DOCX、抽取清单、条款目录和格式契约的文件哈希互相匹配。来源原文按原文展示；格式契约自身的语义来源校验标记无效，"
            "所以本报告不把契约映射或自动检查条目当作论文已核验。该标记本身不表示来源原文不清楚，也不改变来源条款。",
            "",
        ])
    return lines


def _compact_plain(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit] + "…（完整内容见技术附件）"


def _plain_properties(value: Any) -> str:
    if not isinstance(value, dict):
        return "格式细节见技术附件"
    flattened: list[str] = []
    def walk(item: Any, prefix: str = "") -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                walk(child, f"{prefix}.{key}" if prefix else str(key))
        elif isinstance(item, list):
            flattened.append(f"{prefix}：{', '.join(str(child) for child in item)}")
        elif item is not None:
            flattened.append(f"{prefix}：{item}")
    walk(value)
    def label(path: str) -> str:
        parts = path.split(".")
        if parts[0] == "font":
            return {"latin": "英文字体", "cjk": "中文字体"}.get(parts[-1], "字体")
        if parts[0] == "paragraph":
            return {"size_pt": "字号", "line_spacing": "行距", "alignment": "对齐方式"}.get(parts[-1], "段落格式")
        if parts[0] == "page":
            return {"width": "页面宽度", "height": "页面高度"}.get(parts[-1], "页面格式")
        return parts[-1]
    return "；".join(f"{label(entry.split('：', 1)[0])} {entry.split('：', 1)[1]}" for entry in flattened)


def _plain_applicability(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    status = value.get("status")
    conditions = value.get("conditions")
    if status == "conditional" and isinstance(conditions, list):
        readable = []
        for condition in conditions:
            if not isinstance(condition, dict):
                continue
            fact = condition.get("fact")
            operator = condition.get("operator")
            if fact == "source_inventory.english_text" and operator == "present":
                readable.append("论文中出现英文时")
            elif isinstance(fact, str) and fact:
                readable.append("条件需从技术附件核实")
        return "且".join(readable) or "条件尚需核实"
    if status == "always":
        return "始终适用"
    return str(status or "")


def _technical_appendix_payload(
    card: dict[str, Any], source_docx: Path, paper_docx: Path,
    scorecard_path: Path, reconciliation_path: Path, report_path: Path,
    source_bundle: dict[str, Any] | None, source_crosswalk: list[dict[str, Any]],
    code_identity_path: Path | None,
) -> dict[str, Any]:
    source_records = [
        record
        for entry in source_crosswalk
        for record in entry.get("resolved_source_records", [])
    ]
    repeated: dict[str, dict[str, Any]] = {}
    for record in source_records:
        quote = " ".join(str(record.get("clause_text") or "").split())
        if not quote:
            continue
        group = repeated.setdefault(quote, {
            "clause_text": quote, "occurrence_count": 0,
            "clause_ids": [], "requirement_ids": [], "roles": [],
        })
        group["occurrence_count"] += 1
        if record.get("clause_id"):
            group["clause_ids"].append(record["clause_id"])
        if record.get("requirement_id"):
            group["requirement_ids"].append(record["requirement_id"])
        if record.get("role"):
            group["roles"].append(record["role"])
    for group in repeated.values():
        for key in ("clause_ids", "requirement_ids", "roles"):
            group[key] = sorted(set(group[key]))

    crosswalk_counts = collections.Counter(row.get("crosswalk_status") for row in source_crosswalk)
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "protocol": "source_bound_human_review_guide_technical_appendix_v1",
        "status": "complete",
        "submission_ready": False,
        "model_request_made": False,
        "semantic_review_is_new": False,
        "paper_docx": {"path": str(paper_docx.resolve()), "bytes": paper_docx.stat().st_size,
                       "sha256": digest(paper_docx)},
        "paper_source_docx": {"path": str(source_docx.resolve()), "bytes": source_docx.stat().st_size,
                              "sha256": digest(source_docx)},
        "scorecard": {"path": str(scorecard_path.resolve()), "sha256": digest(scorecard_path)},
        "reconciliation": {"path": str(reconciliation_path.resolve()),
                           "sha256": digest(reconciliation_path)},
        "reader_report": {"path": str(report_path.resolve()), "sha256": digest(report_path)},
        "code_identity": ({"path": str(code_identity_path.resolve()), "sha256": digest(code_identity_path)}
                          if code_identity_path else None),
        "lineage": {"parent_run_id": card.get("binding", {}).get("run_id"),
                    "case_id": card.get("binding", {}).get("case_id"),
                    "source_docx_sha256": card.get("binding", {}).get("input_source_sha256"),
                    "semantic_review_reperformed": False,
                    "response_copied_or_rebound": False},
        "source_requirement_bundle": None,
        "crosswalk_summary": {
            "human_review_count": len(source_crosswalk),
            "resolved": crosswalk_counts.get("resolved", 0),
            "unlinked": crosswalk_counts.get("unlinked", 0),
            "conflict": crosswalk_counts.get("conflict", 0),
            "paper_location_present": sum(bool(row.get("paper_location_present")) for row in source_crosswalk),
        },
        "human_review_crosswalk": source_crosswalk,
        "repeated_source_text_diagnostics": sorted(
            repeated.values(), key=lambda row: (-row["occurrence_count"], row["clause_text"])
        ),
    }
    if source_bundle:
        extraction = source_bundle["extraction_manifest"]
        spec = source_bundle["format_spec"]
        payload["source_requirement_bundle"] = {
            "requirements_docx": {
                "path": str(source_bundle["requirements_docx_path"]),
                "bytes": source_bundle["requirements_docx_path"].stat().st_size,
                "sha256": source_bundle["requirements_docx_sha256"],
            },
            "source_run_id": extraction.get("run_id"),
            "source_sha256": extraction.get("source_sha256"),
            "format_spec": {"sha256": source_bundle["hashes"]["format_spec"],
                            "semantic_review_provenance_valid": spec.get("semantic_review_provenance_valid")},
            "requirement_clauses_sha256": source_bundle["hashes"]["requirement_clauses"],
            "extraction_manifest_sha256": source_bundle["hashes"]["extraction_manifest"],
            "requirements_input_manifest_sha256": source_bundle["hashes"]["requirements_input_manifest"],
            "semantic_review_provenance": copy.deepcopy(spec.get("semantic_review_provenance")),
        }
    payload["audit_sha256"] = sha256_json(payload)
    return payload


def _write_editable_ledger(path: Path, card: dict[str, Any], *,
                           source_sha: str, paper_sha: str, pdf_sha: str,
                           code_identity_sha: str | None) -> None:
    doc = Document()
    section = doc.sections[0]
    section.orientation = WD_ORIENT.LANDSCAPE
    section.page_width = Inches(14)
    section.page_height = Inches(8.5)
    section.top_margin = section.bottom_margin = Inches(0.42)
    section.left_margin = section.right_margin = Inches(0.38)
    doc.add_heading("BSU 独立审查台账（可编辑）", level=0)
    doc.add_paragraph(
        "本文件逐项保留当前生成稿核验与继承历史状态，不是提交许可。"
        "语义审查继承自父 run；本次没有发起模型请求或新的语义审查。"
        "具体证据和完整映射同时保存在绑定的机器可读 JSON 中。"
    )
    bindings = [
        ("父语义 run", card.get("binding", {}).get("run_id")),
        ("输入来源 DOCX SHA-256", source_sha),
        ("当前论文 DOCX SHA-256", paper_sha),
        ("当前 PDF SHA-256", pdf_sha),
        ("代码身份 SHA-256", code_identity_sha or "未提供"),
        ("submission_ready", "false"),
    ]
    meta = doc.add_table(rows=0, cols=2)
    meta.style = "Table Grid"
    for label, value in bindings:
        cells = meta.add_row().cells
        _set_cell(cells[0], label, bold=True, size=8)
        _set_cell(cells[1], value, size=8)
    doc.add_paragraph()
    doc.add_heading("状态摘要", level=1)
    summary = doc.add_table(rows=1, cols=2)
    summary.style = "Table Grid"
    _set_cell(summary.rows[0].cells[0], "状态", bold=True, size=8)
    _set_cell(summary.rows[0].cells[1], "条目数", bold=True, size=8)
    _add_table_header_repeat(summary.rows[0])
    for status in ("failed", "unverified", "pending", "verified"):
        cells = summary.add_row().cells
        _set_cell(cells[0], status, size=8)
        _set_cell(cells[1], card["status_counts"][status], size=8)
    doc.add_paragraph()
    doc.add_heading("完整条目（保持 scorecard 原顺序）", level=1)
    table = doc.add_table(rows=1, cols=6)
    table.style = "Table Grid"
    table.autofit = False
    widths = [Inches(0.85), Inches(0.75), Inches(1.2), Inches(1.1), Inches(2.15), Inches(7.0)]
    headers = ["条目 ID", "类型", "当前状态", "历史状态", "来源/追溯映射", "完整明细（JSON 投影）"]
    for cell, header, width in zip(table.rows[0].cells, headers, widths):
        cell.width = width
        _set_cell(cell, header, bold=True, size=7.5, color="FFFFFF")
        shading = OxmlElement("w:shd")
        shading.set(qn("w:fill"), "1F4E78")
        cell._tc.get_or_add_tcPr().append(shading)
    _add_table_header_repeat(table.rows[0])
    for entry in card["entries"]:
        row = table.add_row()
        values = [
            entry.get("item_id", ""), entry.get("kind", ""), entry.get("status", ""),
            entry.get("historical_status", entry.get("historical_detail_status", "—")),
            json.dumps(_source_refs(entry), ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            _detail_text(entry),
        ]
        for cell, value, width in zip(row.cells, values, widths):
            cell.width = width
            _set_cell(cell, value, size=6.5)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    saved = Document(path)
    rows = list(saved.tables[-1].rows)
    ids = [row.cells[0].text for row in rows[1:]]
    expected = [entry["item_id"] for entry in card["entries"]]
    if ids != expected or len(ids) != len(set(ids)):
        raise ValueError("editable ledger DOCX item rows do not match the machine scorecard")


def _human_role(detail: dict[str, Any]) -> str:
    role = detail.get("role")
    expected = detail.get("expected")
    if not role and isinstance(expected, dict):
        role = expected.get("role")
    if not role:
        for producer in detail.get("producer_records", []):
            record = producer.get("record", {}) if isinstance(producer, dict) else {}
            for evidence in record.get("evidence", []):
                if isinstance(evidence, dict) and evidence.get("kind") == "role":
                    role = evidence.get("value")
                    break
            if role:
                break
    return _role_label(role) or "格式区域"


def _plain_failure(entry: dict[str, Any]) -> str:
    detail = entry.get("detail") or {}
    instance = detail.get("current_output_instance") or {}
    finding = instance.get("finding_code") or detail.get("code")
    role = _human_role(detail)
    if finding == "rendered_pdf_font_mismatch":
        expected = detail.get("expected")
        if not isinstance(expected, dict):
            expected = {"name": detail.get("expected"), "script": "cjk"}
        script = "中文" if expected.get("script") == "cjk" else "英文" if expected.get("script") == "latin" else "文字"
        font = expected.get("name") or detail.get("expected") or "模板指定字体"
        font = {"SimSun": "宋体", "FangSong": "仿宋", "KaiTi": "楷体"}.get(font, font)
        if entry.get("kind") == "property":
            actual = detail.get("actual")
            actual_text = f" DOCX 保存的样式值为 {actual}。" if actual is not None else ""
            return (
                f"PDF 字体自动审计将{role}的{script}字体记录为失败（预期 {font}）。{actual_text}"
                "现有审计没有把 PDF 实测字体关联到该区域的文字、段落或页码；需先定位并记录实测字体，才能判断该处是否确有错误。"
            )
        return (
            f"PDF 字体自动审计将{role}的{script}字体记录为失败（预期 {font}）。"
            "该记录没有对应的文字、段落、页码或区域实测字体；目前不能据此确认该处存在字体错误。"
        )
    if entry.get("kind") == "property":
        prop = str(detail.get("property_path") or "")
        if prop.startswith("font."):
            script = "中文" if prop.endswith(".cjk") else "英文" if prop.endswith(".latin") else "文字"
            return f"{role}的{script}字体属性未通过当前 DOCX 核验。"
        return f"{role}的格式属性未通过当前 DOCX 核验。"
    return "当前生成稿有一项经核验不符合记录的要求；具体文字和位置见技术附件。"


def _clean_user_text(value: Any) -> str:
    text = " ".join(str(value or "").split())
    text = re.sub(r"C\d{5}\s*\[E\d{5}\]\s*:\s*", "", text)
    text = re.sub(r"\b(?:C|E|Q|R)\d{5}\b", "", text)
    text = text.replace("thesis_profile.cover_metadata.unit_code", "封面培养单位代码")
    text = text.replace("unit_code", "培养单位代码")
    text = text.replace("rule_spec", "格式契约")
    text = text.replace("abstract_zh.quality_guidance", "中文摘要质量要求")
    text = text.replace("abstract_zh.required_sections", "中文摘要内容要求")
    text = text.replace("metadata", "用户确认资料")
    text = text.replace("当前会话 Agent判定 uncertain", "当前审查记录无法确定")
    return " ".join(text.split())


def _issue_summary(card: dict[str, Any], reconciliation: dict[str, Any],
                   package: dict[str, Any], ledger_json_path: Path,
                   ledger_docx_path: Path, *,
                   source_bundle: dict[str, Any] | None = None,
                   source_crosswalk: list[dict[str, Any]] | None = None,
                   technical_json_path: Path | None = None) -> str:
    counts = card["status_counts"]
    output = reconciliation["current_output"]
    lines = [
        "# BSU 审查草稿：人工复核指引",
        "",
        "**本稿不可提交。** 它沿用既有语义审查结果；本次没有发起模型请求，也没有重新审查论文内容。",
        "",
        f"- 论文草稿：[{Path(package['output_docx']['path']).name}]({Path(package['output_docx']['path']).name})",
        f"- 可编辑逐项台账：[{ledger_docx_path.name}]({ledger_docx_path.name})；完整机器记录：[{ledger_json_path.name}]({ledger_json_path.name})",
        (f"- 来源绑定与逐条编号：[{technical_json_path.name}]({technical_json_path.name})"
         if technical_json_path else "- 来源绑定和机器编号保存在完整机器记录中。"),
        "",
        f"## 当前 {len(card['entries']):,} 项核验结果",
        "",
        f"- 已验证：{counts['verified']}；失败：{counts['failed']}；未核验：{counts['unverified']}；待人工：{counts['pending']}。",
        "- “未核验”和“待人工”都不表示通过。人工完成核验前，保持草稿状态。",
        "",
        "## 自动检查标记的失败项（待定位）",
        "",
        "这里的“失败”是自动审计记录的状态；缺少具体文字和页码定位时，不能据此认定论文中相应内容已经出错。",
        "",
    ]
    failed = [entry for entry in card["entries"] if entry.get("status") == "failed"]
    if failed:
        failure_groups = collections.Counter(_plain_failure(entry) for entry in failed)
        for reason, count in failure_groups.items():
            lines.append(f"- {reason}" if count == 1 else f"- {count} 项：{reason}")
        lines.append("")
    draw_rows = [entry for entry in card["entries"] if entry.get("kind") == "rendered_output_instance"
                 and (entry.get("detail", {}).get("code") or "").startswith("drawing_")]
    if draw_rows:
        no_candidates = all((entry.get("detail") or {}).get("candidate_count") == 0
                            for entry in draw_rows)
        no_page_locator = all(
            (((entry.get("detail") or {}).get("current_output_instance") or {})
             .get("object_identity") or {}).get("page") is None
            for entry in draw_rows
        )
        if no_candidates and no_page_locator:
            drawing_text = (
                f"PDF 自动比对未匹配到 {len(draw_rows)} 个源文档图形，每项候选数均为 0，且没有输出页码定位。"
                "这不能证明图形缺失；请对照源文档逐一查找并记录实际页码和匹配结果。"
            )
        else:
            drawing_text = (
                f"PDF 自动比对未能确认 {len(draw_rows)} 个源文档图形的输出位置。"
                "这不能证明图形缺失；请对照源文档逐一查找并记录实际页码和匹配结果。"
            )
        lines.extend([
            f"- **图片与图形：** {drawing_text}",
            "",
        ])
    font_rows = [
        entry for entry in card["entries"]
        if ("font" in str(entry.get("label", "")).lower()
            or "font" in str((entry.get("detail") or {}).get("property_path", "")).lower()
            or "font" in str((entry.get("detail") or {}).get("failure_type", "")).lower()
            or "font" in str((entry.get("detail") or {}).get("code", "")).lower())
    ]
    if font_rows:
        font_counts = {status: sum(entry.get("status") == status for entry in font_rows)
                       for status in STATUSES}
        lines.extend([
            "### 字体相关项仍未全部核验",
            "",
            (f"- {len(font_rows)} 项：失败 {font_counts['failed']}、未核验 {font_counts['unverified']}、"
             f"待人工 {font_counts['pending']}、已验证 {font_counts['verified']}；未核验/待人工项不计为通过。"),
        ])
        rendered_report = (card.get("rendered_output_audit") or {}).get("report") or {}
        resources = rendered_report.get("actual_pdf_font_resources") or []
        family_names = {
            str(row.get("base_font") or "") for row in resources if isinstance(row, dict)
        }
        family_labels = []
        for family in sorted(family_names):
            if "NotoSerifCJKsc" in family:
                family_labels.append("Noto Serif CJK SC")
            elif "NotoSansCJKsc" in family:
                family_labels.append("Noto Sans CJK SC")
            elif "NotoSansCJKhk" in family:
                family_labels.append("Noto Sans CJK HK")
        family_labels = list(dict.fromkeys(family_labels))
        font_note = (
            "PDF 字体清单只列出整份文档的字体资源"
            + (f"（例如{'、'.join(family_labels)}）" if family_labels else "")
            + "，没有把这些字体映射到具体目标文字、段落或页码，因此这些区域的实际 PDF 字体和位置仍未知。"
        )
        docx_matches = [
            entry for entry in font_rows
            if entry.get("kind") == "property"
            and entry.get("status") == "failed"
            and (((entry.get("detail") or {}).get("current_output_instance") or {})
                 .get("finding_code") == "rendered_pdf_font_mismatch")
            and (entry.get("detail") or {}).get("expected") == (entry.get("detail") or {}).get("actual")
        ]
        if docx_matches:
            font_note += (
                f"其中 {len(docx_matches)} 项的 DOCX 序列化字体值与记录的预期一致；"
                "关联的 PDF 自动审计仍为失败，须按位置检查后再判断。"
            )
        lines.extend([f"- {font_note}", ""])
    lines.extend(_render_human_review_section(
        card, source_bundle, source_crosswalk or [], technical_json_path,
    ))
    lines.extend([
        "完整当前与历史状态、条款编号及逐条证据请查看技术附件。目录缓存来自来源锚定标题及 PDF 实际书签页码的迭代物化，不代表 Word/LibreOffice 更新了动态字段。",
        "本指引与 PDF、DOCX 页眉/目录和章节定位复核均不构成整份格式通过或提交就绪。",
        "",
    ])
    return "\n".join(lines)


def export_review_package(source_docx: Path, paper_docx: Path,
                          scorecard_path: Path, reconciliation_path: Path,
                          ledger_docx_path: Path, ledger_json_path: Path,
                          summary_path: Path, *, code_identity_path: Path | None = None,
                          split_report_path: Path | None = None,
                          source_requirements_dir: Path | None = None,
                          requirements_docx_path: Path | None = None,
                          technical_json_path: Path | None = None) -> dict[str, Any]:
    outputs = [ledger_docx_path.resolve(), ledger_json_path.resolve(), summary_path.resolve()]
    inputs = [source_docx.resolve(), paper_docx.resolve(), scorecard_path.resolve(),
              reconciliation_path.resolve()]
    if code_identity_path:
        inputs.append(code_identity_path.resolve())
    if split_report_path:
        inputs.append(split_report_path.resolve())
    if bool(source_requirements_dir) != bool(requirements_docx_path):
        raise ValueError("source requirement directory and DOCX must be supplied together")
    if source_requirements_dir and not technical_json_path:
        raise ValueError("a separate technical appendix is required with source requirements")
    if source_requirements_dir:
        inputs.extend((source_requirements_dir / filename).resolve() for filename in (
            "format-spec.json", "requirement-clauses.json", "extraction-manifest.json",
            "requirements-input-manifest.json",
        ))
        inputs.append(requirements_docx_path.resolve())
    if technical_json_path:
        outputs.append(technical_json_path.resolve())
    if len(set(inputs + outputs)) != len(inputs + outputs):
        raise ValueError("review package inputs and outputs must be distinct files")
    card = strict_json_read(scorecard_path)
    reconciliation = strict_json_read(reconciliation_path)
    if not isinstance(card, dict) or not _scorecard_semantics_valid(card):
        raise ValueError("external scorecard is invalid")
    if not isinstance(reconciliation, dict) or reconciliation.get("status") != "complete":
        raise ValueError("current-output reconciliation is not complete")
    if (reconciliation.get("submission_ready") is not False
            or reconciliation.get("model_request_made") is not False
            or reconciliation.get("semantic_review_is_new") is not False):
        raise ValueError("package lineage must remain inherited, non-model, and non-submission")
    source_sha = digest(source_docx)
    paper_sha = digest(paper_docx)
    binding = card.get("binding", {})
    if binding.get("input_source_sha256") != source_sha:
        raise ValueError("scorecard source binding does not match supplied source DOCX")
    current = reconciliation.get("current_output", {})
    if current.get("docx_sha256") != paper_sha:
        raise ValueError("reconciliation is not bound to the supplied final paper DOCX")
    assessment = card.get("current_output_assessment", {})
    if (assessment.get("docx_sha256") != paper_sha
            or assessment.get("pdf_sha256") != current.get("pdf_sha256")):
        raise ValueError("scorecard current-output assessment is not bound to paper DOCX/PDF")
    current_report = card.get("rendered_output_audit", {}).get("report")
    if not isinstance(current_report, dict):
        raise ValueError("current rendered report is missing from scorecard audit attachment")
    external_audit = audit_external_scorecard(card, paper_docx, current_report)
    if not external_audit["valid"]:
        raise ValueError("scorecard external render-binding audit failed")
    actual_sc, actual_mr = _ids_in_docx(paper_docx)
    if actual_sc:
        raise ValueError("paper DOCX still contains verbose SC scorecard rows")
    if set(actual_mr) != set(_human_marker_ids(card)) or len(actual_mr) != len(set(actual_mr)):
        raise ValueError("paper DOCX manual-review markers do not match the external scorecard")
    projections = _manual_marker_projections(card)
    paper_marker_texts = {
        match.group(1): paragraph.text
        for paragraph in all_body_paragraphs(Document(paper_docx))
        if (match := MR_START.match(paragraph.text))
    }
    if any(paper_marker_texts.get(marker_id) != marker["paper_text"]
           for marker_id, marker in projections.items()):
        raise ValueError("paper DOCX manual-review marker is not the exact compact ledger projection")

    source_bundle = (_load_source_review_bundle(source_requirements_dir, requirements_docx_path)
                     if source_requirements_dir and requirements_docx_path else None)
    source_crosswalk = _build_source_crosswalk(card, source_bundle)

    code_identity_sha = digest(code_identity_path) if code_identity_path else None
    _write_editable_ledger(ledger_docx_path, card, source_sha=source_sha, paper_sha=paper_sha,
                           pdf_sha=current.get("pdf_sha256"), code_identity_sha=code_identity_sha)
    wrapper: dict[str, Any] = {
        "schema_version": "1.0", "protocol": "source_bound_external_review_ledger_v1",
        "status": "complete", "submission_ready": False,
        "model_request_made": False, "semantic_review_is_new": False,
        "lineage": {
            "parent_run_id": binding.get("run_id"), "case_id": binding.get("case_id"),
            "source_docx_sha256": source_sha,
            "semantic_response_copied_or_rebound": False,
            "semantic_review_reperformed": False,
            "scorecard_input_path": str(scorecard_path.resolve()),
            "scorecard_input_sha256": digest(scorecard_path),
            "reconciliation_path": str(reconciliation_path.resolve()),
            "reconciliation_sha256": digest(reconciliation_path),
            "code_identity_sha256": code_identity_sha,
        },
        "paper_docx": {"path": str(paper_docx.resolve()), "bytes": paper_docx.stat().st_size,
                       "sha256": paper_sha},
        "current_pdf": {"path": current_report.get("pdf", {}).get("path"),
                        "bytes": current_report.get("pdf", {}).get("bytes"),
                        "sha256": current.get("pdf_sha256"),
                        "page_count": current_report.get("pdf", {}).get("page_count")},
        "status_counts": copy.deepcopy(card["status_counts"]),
        "item_count": len(card["entries"]),
        "split_report": (strict_json_read(split_report_path)
                         if split_report_path else None),
        "scorecard": copy.deepcopy(card),
        "current_output_reconciliation": copy.deepcopy(reconciliation),
        "editable_ledger_docx": {"path": str(ledger_docx_path.resolve()),
                                  "bytes": ledger_docx_path.stat().st_size,
                                  "sha256": digest(ledger_docx_path)},
        "scope": f"full {len(card['entries'])}-entry scorecard and current/historical mappings; inherited semantic review",
    }
    wrapper["audit_sha256"] = sha256_json(wrapper)
    _write_json(ledger_json_path, wrapper)
    summary_text = _issue_summary(card, reconciliation,
                                  {"output_docx": {"path": str(paper_docx.resolve()),
                                                   "sha256": paper_sha},
                                   "source_docx": {"sha256": source_sha}},
                                  ledger_json_path, ledger_docx_path,
                                  source_bundle=source_bundle,
                                  source_crosswalk=source_crosswalk,
                                  technical_json_path=technical_json_path)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(summary_text, encoding="utf-8")
    technical_appendix_sha = None
    if technical_json_path:
        technical_path = technical_json_path.resolve()
        technical_path.parent.mkdir(parents=True, exist_ok=True)
        appendix = _technical_appendix_payload(
            card, source_docx, paper_docx, scorecard_path, reconciliation_path,
            summary_path, source_bundle, source_crosswalk, code_identity_path,
        )
        _write_json(technical_path, appendix)
        technical_appendix_sha = digest(technical_path)
    return {
        "protocol": "source_bound_external_review_ledger_export_v1",
        "submission_ready": False, "model_request_made": False,
        "paper_docx_sha256": paper_sha, "source_docx_sha256": source_sha,
        "item_count": len(card["entries"]), "status_counts": card["status_counts"],
        "editable_ledger_docx_sha256": digest(ledger_docx_path),
        "machine_ledger_json_sha256": digest(ledger_json_path),
        "summary_markdown_sha256": digest(summary_path),
        "source_crosswalk_count": len(source_crosswalk),
        "technical_appendix_sha256": technical_appendix_sha,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    strip = sub.add_parser("strip", help="remove exact scorecard block from thesis DOCX")
    strip.add_argument("source_docx", type=Path)
    strip.add_argument("baseline_docx", type=Path)
    strip.add_argument("scorecard", type=Path)
    strip.add_argument("output_docx", type=Path)
    strip.add_argument("--report", type=Path, required=True)
    export = sub.add_parser("export", help="write editable and machine-readable ledgers")
    export.add_argument("source_docx", type=Path)
    export.add_argument("paper_docx", type=Path)
    export.add_argument("scorecard", type=Path)
    export.add_argument("reconciliation", type=Path)
    export.add_argument("ledger_docx", type=Path)
    export.add_argument("ledger_json", type=Path)
    export.add_argument("summary_md", type=Path)
    export.add_argument("--code-identity", type=Path)
    export.add_argument("--split-report", type=Path)
    export.add_argument("--source-requirements-dir", type=Path,
                        help="directory containing the bound source requirement artifacts")
    export.add_argument("--requirements-docx", type=Path,
                        help="exact DOCX bound by the source requirement artifacts")
    export.add_argument("--technical-json", type=Path,
                        help="write a separate machine appendix for human-facing source mappings")
    args = parser.parse_args(argv)
    try:
        if args.command == "strip":
            result = strip_scorecard_display(args.source_docx, args.baseline_docx,
                                             args.scorecard, args.output_docx, args.report)
        else:
            result = export_review_package(
                args.source_docx, args.paper_docx, args.scorecard, args.reconciliation,
                args.ledger_docx, args.ledger_json, args.summary_md,
                code_identity_path=args.code_identity, split_report_path=args.split_report,
                source_requirements_dir=args.source_requirements_dir,
                requirements_docx_path=args.requirements_docx,
                technical_json_path=args.technical_json,
            )
    except Exception as exc:
        print(f"review package export failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
