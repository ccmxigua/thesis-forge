#!/usr/bin/env python3
"""Independent audit of source-bound TOC rows against a rendered PDF.

This verifier deliberately re-parses the source DOCX, final DOCX, and PDF
without importing the materializer's implementation.  It checks semantic
heading order/depth, dynamic field instructions, cached page values, PDF
outline destinations, and visible title/page rows.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZipFile

from lxml import etree

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = f"{{{W_NS}}}"
NS = {"w": W_NS}
REL_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
MANUAL = re.compile(r"(?:manual.?review|human.?review|review.?marker)", re.I)
TOC_STYLE = re.compile(r"^toc(?:\s*heading)?(?:\s*[1-9])?$", re.I)
TOC_LEVEL = re.compile(r"^toc\s*([1-9])$", re.I)
HEADING = re.compile(r"^(?:heading\s*|heading)([1-9])$", re.I)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def title_key(value: str) -> str:
    value = re.sub(r"\s+", " ", value.replace("\u00a0", " ")).strip()
    value = re.sub(r"附\s*录\s+([A-ZＡ-Ｚ])", r"附录\1", value, flags=re.I)
    return value.casefold()


def _package(path: Path) -> tuple[etree._Element, etree._Element]:
    with ZipFile(path) as archive:
        return (etree.fromstring(archive.read("word/document.xml")),
                etree.fromstring(archive.read("word/styles.xml")))


def _styles(root: etree._Element) -> tuple[dict[str, str], dict[str, int]]:
    names: dict[str, str] = {}
    outlines: dict[str, int] = {}
    for style in root.xpath(".//w:style", namespaces=NS):
        sid = style.get(W + "styleId")
        if not sid:
            continue
        name = style.find("w:name", namespaces=NS)
        names[sid] = name.get(W + "val", sid) if name is not None else sid
        node = style.find("w:pPr/w:outlineLvl", namespaces=NS)
        if node is not None:
            outlines[sid] = int(node.get(W + "val"))
    return names, outlines


def _pstyle(paragraph: etree._Element) -> str:
    node = paragraph.find("w:pPr/w:pStyle", namespaces=NS)
    return node.get(W + "val", "") if node is not None else ""


def _level(paragraph: etree._Element, names: dict[str, str], style_levels: dict[str, int]) -> int | None:
    node = paragraph.find("w:pPr/w:outlineLvl", namespaces=NS)
    if node is not None:
        return int(node.get(W + "val"))
    sid = _pstyle(paragraph)
    if sid in style_levels:
        return style_levels[sid]
    match = HEADING.match(names.get(sid, sid)) or HEADING.match(sid)
    return int(match.group(1)) - 1 if match else None


def source_entries(source: Path, depth: int) -> list[dict[str, object]]:
    document, styles = _package(source)
    names, style_levels = _styles(styles)
    entries: list[dict[str, object]] = []
    seen: set[str] = set()
    for index, paragraph in enumerate(document.xpath(".//w:body//w:p", namespaces=NS)):
        parent = paragraph.getparent()
        if any(node.tag == W + "tbl" for node in [parent, *list(parent.iterancestors())] if node is not None):
            continue
        sid = _pstyle(paragraph)
        name = names.get(sid, sid)
        if MANUAL.search(sid) or MANUAL.search(name) or TOC_STYLE.match(sid) or TOC_STYLE.match(name):
            continue
        text = "".join(paragraph.xpath(".//w:t/text()", namespaces=NS)).strip()
        level = _level(paragraph, names, style_levels)
        if not text or level is None or level < 0 or level >= depth:
            continue
        key = title_key(text)
        if key in seen:
            raise ValueError(f"duplicate source heading: {text!r}")
        seen.add(key)
        entries.append({"title": re.sub(r"\s+", " ", text), "level": level, "source_order": index})
    if not entries:
        raise ValueError("no source headings match the dynamic TOC depth")
    return entries


def _toc_instruction(document: etree._Element) -> tuple[etree._Element, str, int]:
    matches = []
    paragraphs = document.xpath(".//w:body//w:p", namespaces=NS)
    for index, paragraph in enumerate(paragraphs):
        text = " ".join(paragraph.xpath(".//w:instrText/text()", namespaces=NS))
        if re.search(r"\bTOC\b", text, re.I):
            matches.append((paragraph, text, index))
    if len(matches) != 1:
        raise ValueError(f"final DOCX must have one dynamic TOC field; found {len(matches)}")
    match = re.search(r"\\o\s+\"1-([1-9])\"", matches[0][1], re.I)
    if not match:
        raise ValueError("final TOC field has no explicit depth")
    return matches[0][0], matches[0][1], int(match.group(1))


def _read_toc_rows(document: etree._Element, styles: etree._Element,
                   field_p: etree._Element) -> list[dict[str, object]]:
    names, _ = _styles(styles)
    body = document.find("w:body", namespaces=NS)
    paragraphs = body.xpath(".//w:p", namespaces=NS)
    start = paragraphs.index(field_p)
    rows: list[dict[str, object]] = []
    for paragraph in paragraphs[start + 1:]:
        sid = _pstyle(paragraph)
        name = names.get(sid, sid)
        match = TOC_LEVEL.match(name) or TOC_LEVEL.match(sid)
        if not match:
            if rows:
                break
            continue
        title = "".join(paragraph.xpath(".//w:hyperlink//w:t/text()", namespaces=NS))
        anchors = paragraph.xpath(".//w:hyperlink/@w:anchor", namespaces=NS)
        instructions = paragraph.xpath(".//w:instrText/text()", namespaces=NS)
        refs = [re.search(r"PAGEREF\s+([^\s\\]+)", instr, re.I) for instr in instructions]
        refs = [item for item in refs if item]
        if len(anchors) != 1 or len(refs) != 1 or anchors[0] != refs[0].group(1):
            raise ValueError(f"TOC row has missing/ambiguous bookmark or PAGEREF: {title!r}")
        instr = next(node for node in paragraph.xpath(".//w:instrText", namespaces=NS)
                     if re.search(r"\bPAGEREF\b", node.text or "", re.I))
        run = instr
        while run is not None and run.tag != W + "r":
            run = run.getparent()
        direct_runs = paragraph.xpath(".//w:r", namespaces=NS)
        if run not in direct_runs:
            raise ValueError(f"PAGEREF result run is missing: {title!r}")
        cached = ""
        for next_run in direct_runs[direct_runs.index(run) + 1:]:
            if next_run.xpath(".//w:fldChar[@w:fldCharType='end']", namespaces=NS):
                break
            cached += "".join(next_run.xpath(".//w:t/text()", namespaces=NS))
        if not cached.strip().isdigit():
            raise ValueError(f"PAGEREF cached result is not a decimal page number: {title!r}")
        rows.append({"title": re.sub(r"\s+", " ", title).strip(),
                     "level": int(match.group(1)) - 1, "bookmark": anchors[0],
                     "page": int(cached.strip())})
    return rows


def _verify_output_bindings(document: etree._Element, styles: etree._Element,
                            entries: list[dict[str, object]],
                            field_p: etree._Element) -> None:
    names, style_levels = _styles(styles)
    paragraphs = document.xpath(".//w:body//w:p", namespaces=NS)
    bookmarks = set(document.xpath(".//w:bookmarkStart/@w:name", namespaces=NS))
    for entry in entries:
        expected = title_key(str(entry["title"]))
        candidates = []
        for paragraph in paragraphs:
            sid = _pstyle(paragraph)
            name = names.get(sid, sid)
            if MANUAL.search(sid) or MANUAL.search(name) or TOC_STYLE.match(sid) or TOC_STYLE.match(name):
                continue
            text = "".join(paragraph.xpath(".//w:t/text()", namespaces=NS)).strip()
            if title_key(text) == expected:
                candidates.append(paragraph)
        if len(candidates) != 1:
            raise ValueError(f"source heading output binding is not unique: {entry['title']!r}")
        actual_level = _level(candidates[0], names, style_levels)
        if actual_level != int(entry["level"]):
            raise ValueError(f"source outline level was not preserved for {entry['title']!r}")
    for row in _read_toc_rows(document, styles, field_p):
        if row["bookmark"] not in bookmarks:
            raise ValueError(f"TOC row points to missing bookmark: {row['title']!r}")


def _pdf_outline(pdf_path: Path) -> list[tuple[int, str, int]]:
    import fitz
    doc = fitz.open(pdf_path)
    try:
        return [(int(level), str(title), int(page)) for level, title, page in doc.get_toc(simple=True)]
    finally:
        doc.close()


def _visible_rows(pdf_path: Path, rows: list[dict[str, object]]) -> list[dict[str, object]]:
    import fitz
    doc = fitz.open(pdf_path)
    found_rows: list[dict[str, object]] = []
    try:
        rendered_lines: list[dict[str, object]] = []
        for page_no, page in enumerate(doc, 1):
            words = sorted(page.get_text("words"), key=lambda item: (float(item[1]), float(item[0])))
            groups: list[list[tuple[float, float, str]]] = []
            for word in words:
                token = (float(word[0]), float(word[1]), str(word[4]))
                if groups and abs(token[1] - sum(item[1] for item in groups[-1]) / len(groups[-1])) < 4.0:
                    groups[-1].append(token)
                else:
                    groups.append([token])
            for group in groups:
                line = " ".join(item[2] for item in sorted(group))
                rendered_lines.append({
                    "page": page_no, "y": min(item[1] for item in group),
                    "line": line.strip(), "compact": re.sub(r"\s+", "", line).casefold(),
                })
        for row in rows:
            title_compact = re.sub(r"\s+", "", str(row["title"])).casefold()
            matches = []
            for rendered in rendered_lines:
                number = re.search(r"(\d+)\s*$", str(rendered["line"]))
                if (title_compact in rendered["compact"] and number
                        and int(number.group(1)) == int(row["page"])):
                    matches.append(rendered)
            if len(matches) != 1:
                raise ValueError(f"rendered PDF visible TOC row mismatch for {row['title']!r}: {len(matches)}")
            found_rows.append({**row, "toc_page": matches[0]["page"],
                               "toc_y": matches[0]["y"], "visible_line": matches[0]["line"]})
    finally:
        doc.close()
    if sorted(found_rows, key=lambda item: (int(item["toc_page"]), float(item["toc_y"]))) != found_rows:
        raise ValueError("visible PDF TOC rows are not in source order")
    return found_rows


def audit(source: Path, final_docx: Path, pdf: Path) -> dict[str, object]:
    source_document, _source_styles = _package(source)
    final_document, final_styles = _package(final_docx)
    _source_field_p, source_instruction, source_depth = _toc_instruction(source_document)
    field_p, instruction, depth = _toc_instruction(final_document)
    if source_depth != depth:
        raise ValueError(f"output TOC depth {depth} differs from source depth {source_depth}")
    entries = source_entries(source, depth)
    rows = _read_toc_rows(final_document, final_styles, field_p)
    if len(rows) != len(entries):
        raise ValueError(f"final DOCX TOC row count {len(rows)} != source heading count {len(entries)}")
    expected = [(str(item["title"]), int(item["level"])) for item in entries]
    actual = [(str(item["title"]), int(item["level"])) for item in rows]
    if [(title_key(title), level) for title, level in actual] != [
        (title_key(title), level) for title, level in expected
    ]:
        raise ValueError("final DOCX TOC title order or outline depth differs from the source")
    _verify_output_bindings(final_document, final_styles, entries, field_p)
    outline = _pdf_outline(pdf)
    by_title: dict[str, list[tuple[int, int]]] = {}
    for level, title, page in outline:
        by_title.setdefault(title_key(title), []).append((level, page))
    for row in rows:
        matches = by_title.get(title_key(str(row["title"])), [])
        if len(matches) != 1:
            raise ValueError(f"PDF outline destination must be unique for {row['title']!r}: {len(matches)}")
        level, page = matches[0]
        if level != int(row["level"]) + 1 or page != int(row["page"]):
            raise ValueError(f"cached page/depth differs from final PDF outline for {row['title']!r}")
    visible = _visible_rows(pdf, rows)
    with ZipFile(final_docx) as archive:
        document_xml = etree.fromstring(archive.read("word/document.xml"))
    manual_count = 0
    names, _ = _styles(final_styles)
    for paragraph in document_xml.xpath(".//w:body//w:p", namespaces=NS):
        sid = _pstyle(paragraph)
        if MANUAL.search(sid) or MANUAL.search(names.get(sid, sid)):
            manual_count += 1
    return {
        "schema_version": "1.0", "protocol": "independent_toc_render_audit_v1",
        "created_at": datetime.now(timezone.utc).isoformat(), "status": "passed",
        "submission_ready": False,
        "source": {"path": str(source.resolve()), "sha256": digest(source)},
        "final_docx": {"path": str(final_docx.resolve()), "sha256": digest(final_docx),
                       "bytes": final_docx.stat().st_size},
        "pdf": {"path": str(pdf.resolve()), "sha256": digest(pdf)},
        "source_toc_instruction": source_instruction,
        "toc_instruction": instruction, "field_refresh_claimed": False,
        "source_heading_count": len(entries), "rows": visible,
        "manual_review_marker_paragraph_count": manual_count,
        "checks": {"source_title_order_depth": True, "output_source_bindings": True,
                   "dynamic_fields_and_cached_pages": True, "pdf_bookmarks_match_cache": True,
                   "visible_pdf_title_page_rows": True, "submission_ready": False},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_docx", type=Path)
    parser.add_argument("final_docx", type=Path)
    parser.add_argument("rendered_pdf", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = audit(args.source_docx.resolve(), args.final_docx.resolve(), args.rendered_pdf.resolve())
    except Exception as exc:
        print(f"independent TOC audit failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
