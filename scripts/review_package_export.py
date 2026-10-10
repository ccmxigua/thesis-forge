#!/usr/bin/env python3
"""Export a concise review draft plus a separately editable full audit ledger.

The split removes only the scorecard display block whose complete text is
bound to the supplied scorecard JSON. Manual-review markers remain in the
thesis. Full current/historical entries are retained in an editable DOCX and a
machine-readable JSON ledger. Nothing in this module promotes readiness.
"""
from __future__ import annotations

import argparse
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


def _issue_summary(card: dict[str, Any], reconciliation: dict[str, Any],
                   package: dict[str, Any], ledger_json_path: Path,
                   ledger_docx_path: Path) -> str:
    counts = card["status_counts"]
    output = reconciliation["current_output"]
    lines = [
        "# BSU 审查草稿问题摘要",
        "",
        "**状态：不可提交；`submission_ready=false`。** 该稿继承现有语义审查 run；本次未发起模型请求、未新做语义审查。",
        "",
        f"- 论文 DOCX：`{package['output_docx']['path']}`",
        f"- 论文 DOCX SHA-256：`{package['output_docx']['sha256']}`",
        f"- 当前渲染 PDF SHA-256：`{output.get('pdf_sha256')}`",
        f"- 来源 DOCX SHA-256：`{package['source_docx']['sha256']}`",
        f"- 父语义 run：`{card['binding']['run_id']}`（沿用，不代表新调用）",
        f"- 当前审查台账：[{ledger_docx_path.name}]({ledger_docx_path.name})；机器报告：[{ledger_json_path.name}]({ledger_json_path.name})",
        "",
        f"## 当前 {len(card['entries']):,} 项核验状态",
        "",
        f"- 已验证：{counts['verified']}；失败：{counts['failed']}；未核验：{counts['unverified']}；待人工：{counts['pending']}。",
        "- 当前状态是新稿精确 DOCX/PDF 字节的渲染审计与继承条目的重算结果；历史状态逐项留在台账中。",
        "",
        "## 尚未解决的问题",
        "",
    ]
    failed = [entry for entry in card["entries"] if entry.get("status") == "failed"]
    if failed:
        lines.append("### 已证实失败")
        lines.append("")
        for entry in failed:
            detail = entry.get("detail", {})
            instance = detail.get("current_output_instance", {})
            reason = detail.get("reason") or instance.get("reason") or detail.get("code") or "需见完整条目明细"
            lines.append(f"- `{entry['item_id']}` — {entry.get('label', '')}；{reason}")
        lines.append("")
    draw_rows = [entry for entry in card["entries"] if entry.get("kind") == "rendered_output_instance"
                 and (entry.get("detail", {}).get("code") or "").startswith("drawing_")]
    if draw_rows:
        lines.append("### 图片/图形待核")
        lines.append("")
        for entry in draw_rows:
            detail = entry.get("detail", {})
            lines.append(f"- `{entry['item_id']}` — {detail.get('code')}，图形序号 {detail.get('drawing_index')}，状态 {entry.get('status')}。")
        lines.append("")
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
        pdf_font_rows = [entry for entry in font_rows
                         if "rendered_pdf_font" in str((entry.get("detail") or {}).get("code", ""))]
        if pdf_font_rows:
            lines.append("- PDF 字体渲染检查仍有问题：" + ", ".join(
                f"`{entry['item_id']}` ({entry.get('status')})" for entry in pdf_font_rows
            ) + "。")
        lines.append("")
    unit_code = next((entry for entry in card["entries"]
                      if entry.get("kind") == "human_review"
                      and "unit_code" in str(entry.get("label", ""))), None)
    if unit_code:
        lines.append("### 输入仍待确认")
        lines.append("")
        lines.append(f"- `{unit_code['item_id']}` / `{unit_code.get('detail', {}).get('marker_id')}` — `unit_code` 未由用户确认，仍为 {unit_code['status']}。")
        lines.append("")
    lines.extend([
        "其余未核验和人工事项均保留在完整台账中；文档未把这些条目转写为通过。",
        "目录缓存来自来源锚定标题及 PDF 实际书签页码的迭代物化，不代表 Word/LibreOffice 更新了动态字段。",
        "PDF、DOCX 页眉/目录和章节定位复核不构成整份格式通过或提交就绪。",
        "",
    ])
    return "\n".join(lines)


def export_review_package(source_docx: Path, paper_docx: Path,
                          scorecard_path: Path, reconciliation_path: Path,
                          ledger_docx_path: Path, ledger_json_path: Path,
                          summary_path: Path, *, code_identity_path: Path | None = None,
                          split_report_path: Path | None = None) -> dict[str, Any]:
    outputs = [ledger_docx_path.resolve(), ledger_json_path.resolve(), summary_path.resolve()]
    inputs = [source_docx.resolve(), paper_docx.resolve(), scorecard_path.resolve(),
              reconciliation_path.resolve()]
    if code_identity_path:
        inputs.append(code_identity_path.resolve())
    if split_report_path:
        inputs.append(split_report_path.resolve())
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
                                  ledger_json_path, ledger_docx_path)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(summary_text, encoding="utf-8")
    return {
        "protocol": "source_bound_external_review_ledger_export_v1",
        "submission_ready": False, "model_request_made": False,
        "paper_docx_sha256": paper_sha, "source_docx_sha256": source_sha,
        "item_count": len(card["entries"]), "status_counts": card["status_counts"],
        "editable_ledger_docx_sha256": digest(ledger_docx_path),
        "machine_ledger_json_sha256": digest(ledger_json_path),
        "summary_markdown_sha256": digest(summary_path),
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
            )
    except Exception as exc:
        print(f"review package export failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
