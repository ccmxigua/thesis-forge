#!/usr/bin/env python3
"""Materialize a source-bound Word TOC from LibreOffice's final PDF outline.

LibreOffice's headless PDF export in the cloud runtime does not expose a usable
UNO field-update API.  This command therefore keeps the dynamic TOC/PAGEREF
field instructions, writes their cached result from actual rendered PDF
bookmarks, and rerenders until the cache and final pagination agree.  It does
not claim that LibreOffice or Word refreshed the fields.

The source DOCX is authoritative for entry text, order, and outline depth.
Only unique source headings at the configured TOC depth may be linked.  The
renderer must emit one matching PDF outline bookmark for each heading; missing
or ambiguous matches fail closed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import posixpath
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from lxml import etree
from process_runner import run_process

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_NS = "http://www.w3.org/XML/1998/namespace"
W = f"{{{W_NS}}}"
NS = {"w": W_NS}
MANUAL_STYLE_RE = re.compile(r"(?:manual.?review|human.?review|review.?marker)", re.I)
TOC_STYLE_RE = re.compile(r"^toc\s*([1-9])$", re.I)
TOC_HEADING_STYLE_RE = re.compile(r"^toc(?:\s*heading)?(?:\s*[1-9])?$", re.I)
HEADING_STYLE_RE = re.compile(r"^(?:heading\s*|heading)([1-9])$", re.I)


@dataclass(frozen=True)
class TocHeading:
    title: str
    level: int  # zero based Word outline level
    bookmark: str
    source_paragraph_index: int
    output_paragraph_index: int
    toc_field_paragraph_index: int


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normalized_title(text: str) -> str:
    # Keep punctuation and Latin word boundaries; normalize only layout spaces.
    value = re.sub(r"\s+", " ", text.replace("\u00a0", " ")).strip()
    # The formatter may introduce a visual gap between the Chinese appendix
    # label and its single-letter identifier without changing the title.
    return re.sub(r"附\s*录\s+([A-ZＡ-Ｚ])", r"附录\1", value, flags=re.I)


def _style_maps(styles_root: etree._Element) -> tuple[dict[str, str], dict[str, int]]:
    names: dict[str, str] = {}
    levels: dict[str, int] = {}
    for style in styles_root.xpath(".//w:style", namespaces=NS):
        style_id = style.get(W + "styleId")
        if not style_id:
            continue
        name_node = style.find("w:name", namespaces=NS)
        names[style_id] = name_node.get(W + "val", "") if name_node is not None else style_id
        ppr = style.find("w:pPr", namespaces=NS)
        outline = ppr.find("w:outlineLvl", namespaces=NS) if ppr is not None else None
        if outline is not None:
            try:
                levels[style_id] = int(outline.get(W + "val"))
            except (TypeError, ValueError):
                raise ValueError(f"invalid outline level in style {style_id}")
    return names, levels


def _paragraph_style_id(paragraph: etree._Element) -> str | None:
    node = paragraph.find("w:pPr/w:pStyle", namespaces=NS)
    return node.get(W + "val") if node is not None else None


def _paragraph_text(paragraph: etree._Element) -> str:
    return "".join(paragraph.xpath(".//w:t/text()", namespaces=NS))


def _paragraph_outline_level(
    paragraph: etree._Element,
    style_names: dict[str, str],
    style_levels: dict[str, int],
) -> int | None:
    direct = paragraph.find("w:pPr/w:outlineLvl", namespaces=NS)
    if direct is not None:
        try:
            return int(direct.get(W + "val"))
        except (TypeError, ValueError):
            raise ValueError("paragraph has an invalid w:outlineLvl")
    style_id = _paragraph_style_id(paragraph)
    if style_id in style_levels:
        return style_levels[style_id]
    style_name = style_names.get(style_id or "", style_id or "")
    match = HEADING_STYLE_RE.match(style_name) or HEADING_STYLE_RE.match(style_id or "")
    return int(match.group(1)) - 1 if match else None


def _inside_table(paragraph: etree._Element) -> bool:
    parent = paragraph.getparent()
    while parent is not None:
        if parent.tag == W + "tbl":
            return True
        parent = parent.getparent()
    return False


def _is_toc_style(style_id: str, style_name: str) -> bool:
    return bool(TOC_STYLE_RE.match(style_name) or TOC_STYLE_RE.match(style_id)
                or TOC_HEADING_STYLE_RE.match(style_name) or TOC_HEADING_STYLE_RE.match(style_id))


def _manual_review_snapshot(
    document_root: etree._Element, styles_root: etree._Element,
) -> dict[str, Any]:
    style_names, _ = _style_maps(styles_root)
    records: list[str] = []
    for paragraph in document_root.xpath(".//w:body//w:p", namespaces=NS):
        style_id = _paragraph_style_id(paragraph) or ""
        style_name = style_names.get(style_id, style_id)
        if MANUAL_STYLE_RE.search(style_id) or MANUAL_STYLE_RE.search(style_name):
            records.append(etree.tostring(paragraph, method="c14n").decode("utf-8"))
    payload = "\n".join(records).encode("utf-8")
    return {"count": len(records), "canonical_xml_sha256": hashlib.sha256(payload).hexdigest()}


def toc_depth_from_instruction(instruction: str) -> int:
    match = re.search(r"\\o\s+\"1-([1-9])\"", instruction, re.I)
    if not match:
        raise ValueError("TOC field must declare an explicit \\o \"1-N\" depth")
    return int(match.group(1))


def _toc_field_paragraphs(document_root: etree._Element) -> list[tuple[etree._Element, str]]:
    found: list[tuple[etree._Element, str]] = []
    for paragraph in document_root.xpath(".//w:body//w:p", namespaces=NS):
        instruction = " ".join(paragraph.xpath(".//w:instrText/text()", namespaces=NS))
        if re.search(r"\bTOC\b", instruction, re.I):
            found.append((paragraph, instruction))
    return found


def extract_source_headings(source_docx: Path, depth: int) -> list[dict[str, Any]]:
    with ZipFile(source_docx) as archive:
        document_root = etree.fromstring(archive.read("word/document.xml"))
        styles_root = etree.fromstring(archive.read("word/styles.xml"))
    style_names, style_levels = _style_maps(styles_root)
    headings: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    paragraphs = document_root.xpath(".//w:body//w:p", namespaces=NS)
    for index, paragraph in enumerate(paragraphs):
        text = normalized_title(_paragraph_text(paragraph))
        if not text or _inside_table(paragraph):
            continue
        style_id = _paragraph_style_id(paragraph) or ""
        style_name = style_names.get(style_id, style_id)
        if MANUAL_STYLE_RE.search(style_id) or MANUAL_STYLE_RE.search(style_name):
            continue
        if _is_toc_style(style_id, style_name):
            continue
        level = _paragraph_outline_level(paragraph, style_names, style_levels)
        if level is None or level < 0 or level >= depth:
            continue
        # A title named “目录” that is not a source outline heading would not
        # arrive here; field paragraphs and the dedicated TOC heading style are
        # also excluded above.
        key = normalized_title(text).casefold()
        if key in seen:
            raise ValueError(
                f"duplicate source heading at outline depth {depth}: {text!r} "
                f"(paragraphs {seen[key]} and {index})"
            )
        seen[key] = index
        headings.append({"title": text, "level": level, "source_paragraph_index": index})
    if not headings:
        raise ValueError("source contains no eligible headings at the configured TOC depth")
    return headings


def _toc_style_ids(styles_root: etree._Element, depth: int) -> dict[int, str]:
    found: dict[int, str] = {}
    for style in styles_root.xpath(".//w:style[@w:type='paragraph']", namespaces=NS):
        style_id = style.get(W + "styleId", "")
        name = style.find("w:name", namespaces=NS)
        style_name = name.get(W + "val", "") if name is not None else style_id
        match = TOC_STYLE_RE.match(style_name) or TOC_STYLE_RE.match(style_id)
        if match:
            found[int(match.group(1)) - 1] = style_id
    missing = sorted({int_heading for int_heading in range(depth)} - set(found))
    if missing:
        raise ValueError("output DOCX lacks paragraph styles for TOC levels: " + ", ".join(str(i + 1) for i in missing))
    return found


def _toc_tab_position_twips(document_root: etree._Element, toc_paragraph: etree._Element) -> int:
    """Resolve the TOC section's printable width for a right page-number tab."""
    body = document_root.find("w:body", namespaces=NS)
    if body is None:
        raise ValueError("DOCX body is missing")
    paragraphs = body.xpath(".//w:p", namespaces=NS)
    toc_index = paragraphs.index(toc_paragraph)
    owning_section = None
    for paragraph in paragraphs[toc_index + 1:]:
        sect = paragraph.find("w:pPr/w:sectPr", namespaces=NS)
        if sect is not None:
            owning_section = sect
            break
    if owning_section is None:
        owning_section = body.find("w:sectPr", namespaces=NS)
    if owning_section is None:
        raise ValueError("cannot locate the section properties that contain the TOC")
    page_size = owning_section.find("w:pgSz", namespaces=NS)
    margins = owning_section.find("w:pgMar", namespaces=NS)
    if page_size is None or margins is None:
        raise ValueError("TOC section lacks explicit page size or margins; refusing to guess tab position")
    try:
        width = int(page_size.get(W + "w"))
        left = int(margins.get(W + "left"))
        right = int(margins.get(W + "right"))
        gutter = int(margins.get(W + "gutter", "0"))
    except (TypeError, ValueError) as exc:
        raise ValueError("TOC section has invalid page dimensions or margins") from exc
    position = width - left - right - gutter
    if position <= 0:
        raise ValueError("TOC section has no positive printable width")
    return position


def _effective_page_numbering(
    document_root: etree._Element, package_members: dict[str, bytes] | None = None,
) -> dict[str, Any]:
    """Determine whether PDF physical pages equal default Word PAGEREF values.

    A section-specific restart or non-decimal display format needs a rendered
    page-label map.  This cloud renderer emits no PDF page labels, so these
    cases are rejected rather than silently presenting physical pages as
    section-relative values.
    """
    restarts: list[dict[str, Any]] = []
    for index, section in enumerate(document_root.xpath(".//w:sectPr", namespaces=NS), 1):
        node = section.find("w:pgNumType", namespaces=NS)
        if node is None:
            continue
        start, fmt = node.get(W + "start"), node.get(W + "fmt")
        if start is not None or (fmt is not None and fmt != "decimal"):
            restarts.append({"section": index, "start": start, "format": fmt})
    if restarts:
        raise ValueError(
            "cannot map physical PDF pages to displayed PAGEREF page numbers "
            "for section restarts/non-decimal formats without rendered page labels: "
            + json.dumps(restarts, ensure_ascii=False)
        )
    visible_page_fields: int | None = None
    if package_members is not None:
        visible_page_fields = 0
        try:
            rels = etree.fromstring(package_members["word/_rels/document.xml.rels"])
        except (KeyError, etree.XMLSyntaxError) as exc:
            raise ValueError("DOCX relationships are missing or invalid; cannot bind page fields") from exc
        rel_by_id = {
            item.get("Id"): item.get("Target")
            for item in rels
            if item.get("Id") and item.get("Target")
        }
        active_footer_parts: set[str] = set()
        for reference in document_root.xpath(".//w:sectPr/w:footerReference", namespaces=NS):
            rid = reference.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
            target = rel_by_id.get(rid)
            if not target:
                continue
            part = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("word", target))
            active_footer_parts.add(part)
        for part in active_footer_parts:
            payload = package_members.get(part)
            if payload is None:
                raise ValueError(f"active footer relationship target is missing: {part}")
            footer = etree.fromstring(payload)
            instruction_text = " ".join(footer.xpath(".//w:instrText/text()", namespaces=NS))
            simple_fields = footer.xpath(".//w:fldSimple/@w:instr", namespaces=NS)
            if re.search(r"\bPAGE\b", instruction_text, re.I) or any(
                re.match(r"\s*PAGE(?:\s|$)", value, re.I) for value in simple_fields
            ):
                visible_page_fields += 1
    return {"mode": "continuous_default_decimal", "physical_equals_display": True,
            "explicit_section_restarts": [],
            "visible_footer_PAGE_field_count": visible_page_fields,
            "note": "PAGEREF cache values use rendered physical page indices; these equal default decimal document page values because no restart/alternate format is declared."}


def _make_bookmark(title: str, level: int, ordinal: int) -> str:
    digest = hashlib.sha256(f"{level}:{ordinal}:{title}".encode("utf-8")).hexdigest()[:14]
    return f"_TfToc_{ordinal:03d}_{digest}"


def _paragraph_has_direct_heading_style(paragraph: etree._Element, style_names: dict[str, str]) -> bool:
    style_id = _paragraph_style_id(paragraph) or ""
    style_name = style_names.get(style_id, style_id)
    # Compiled custom semantic styles commonly include “Heading” plus a role
    # (for example ThesisHeadingAcknowledgments) and do not inherit the built-in
    # Heading 1 style.  The source-bound exact title provides the role identity;
    # this check only rejects ordinary prose and marker styles.
    return bool(HEADING_STYLE_RE.match(style_name) or HEADING_STYLE_RE.match(style_id)
                or re.search(r"heading|appendix|title", style_name, re.I)
                or re.search(r"heading|appendix|title", style_id, re.I))


def _prepare_output_headings(
    document_root: etree._Element,
    styles_root: etree._Element,
    headings: list[dict[str, Any]],
    bookmarks: set[str],
) -> list[TocHeading]:
    style_names, _ = _style_maps(styles_root)
    toc_fields = _toc_field_paragraphs(document_root)
    if len(toc_fields) != 1:
        raise ValueError(f"expected one dynamic TOC field in output; found {len(toc_fields)}")
    toc_p, instruction = toc_fields[0]
    depth = toc_depth_from_instruction(instruction)
    body_paragraphs = document_root.xpath(".//w:body//w:p", namespaces=NS)
    toc_index = body_paragraphs.index(toc_p)
    matched: list[TocHeading] = []
    for ordinal, item in enumerate(headings, 1):
        title = item["title"]
        expected = normalized_title(title).casefold()
        candidates: list[tuple[int, etree._Element]] = []
        for index, paragraph in enumerate(body_paragraphs):
            if _inside_table(paragraph):
                continue
            style_id = _paragraph_style_id(paragraph) or ""
            style_name = style_names.get(style_id, style_id)
            if MANUAL_STYLE_RE.search(style_id) or MANUAL_STYLE_RE.search(style_name):
                continue
            if TOC_STYLE_RE.match(style_name) or TOC_STYLE_RE.match(style_id):
                continue
            if normalized_title(_paragraph_text(paragraph)).casefold() != expected:
                continue
            # A title candidate must already be semantic heading-shaped, or
            # have an explicit outline mapping.  Exact text alone cannot turn
            # arbitrary body text or a marker into a TOC destination.
            if not (_paragraph_has_direct_heading_style(paragraph, style_names)
                    or paragraph.find("w:pPr/w:outlineLvl", namespaces=NS) is not None):
                continue
            candidates.append((index, paragraph))
        if len(candidates) != 1:
            raise ValueError(
                f"source heading must bind to exactly one output body heading: {title!r}; "
                f"found {len(candidates)}"
            )
        output_index, paragraph = candidates[0]
        ppr = paragraph.find("w:pPr", namespaces=NS)
        if ppr is None:
            ppr = etree.Element(W + "pPr")
            paragraph.insert(0, ppr)
        outline = ppr.find("w:outlineLvl", namespaces=NS)
        if outline is None:
            outline = etree.Element(W + "outlineLvl")
            insertion = len(ppr)
            for child_index, child in enumerate(ppr):
                if child.tag in {W + "divId", W + "rPr", W + "sectPr"}:
                    insertion = child_index
                    break
            ppr.insert(insertion, outline)
        outline.set(W + "val", str(item["level"]))

        bookmark = _make_bookmark(title, item["level"], ordinal)
        if bookmark in bookmarks:
            raise ValueError(f"output already contains reserved TOC bookmark {bookmark}")
        bookmarks.add(bookmark)
        bookmark_id = str(max([int(x) for x in document_root.xpath(
            ".//w:bookmarkStart/@w:id", namespaces=NS
        ) if str(x).isdigit()] or [0]) + 1)
        start = etree.Element(W + "bookmarkStart", {W + "id": bookmark_id, W + "name": bookmark})
        end = etree.Element(W + "bookmarkEnd", {W + "id": bookmark_id})
        # Keep pPr first, then place the start before the paragraph's content.
        paragraph.insert(1 if len(paragraph) and paragraph[0].tag == W + "pPr" else 0, start)
        paragraph.append(end)
        matched.append(TocHeading(
            title=title, level=item["level"], bookmark=bookmark,
            source_paragraph_index=item["source_paragraph_index"],
            output_paragraph_index=output_index, toc_field_paragraph_index=toc_index,
        ))
    if any(item.level >= depth for item in matched):
        raise ValueError("source heading depth exceeds the dynamic TOC field depth")
    return matched


def _empty_field_result(toc_p: etree._Element) -> tuple[etree._Element, etree._Element]:
    chars = toc_p.xpath(".//w:fldChar", namespaces=NS)
    types = [node.get(W + "fldCharType") for node in chars]
    if types != ["begin", "separate", "end"]:
        raise ValueError("only the supported empty single-paragraph TOC field is accepted")
    separate, end = chars[1], chars[2]
    children = list(toc_p.iter())
    start_i, end_i = children.index(separate), children.index(end)
    result = children[start_i + 1:end_i]
    visible = "".join(node.text or "" for node in result if node.tag == W + "t")
    if visible.strip():
        raise ValueError("output TOC field already has a non-empty result; refusing to overwrite it")
    end_run = end.getparent()
    if end_run is not None and end_run.tag == W + "r":
        toc_p.remove(end_run)
    for node in list(toc_p):
        if node.tag == W + "r" and node is not separate.getparent():
            instr = "".join(node.xpath(".//w:instrText/text()", namespaces=NS))
            fld_types = node.xpath(".//w:fldChar/@w:fldCharType", namespaces=NS)
            if not instr and not fld_types and not "".join(node.xpath(".//w:t/text()", namespaces=NS)).strip():
                toc_p.remove(node)
    return toc_p, separate


def _field_run(instruction: str, cached: str, *, outer_end: bool = False) -> etree._Element:
    run = etree.Element(W + "r")
    if outer_end:
        end = etree.SubElement(run, W + "fldChar")
        end.set(W + "fldCharType", "end")
        return run
    begin = etree.SubElement(run, W + "fldChar")
    begin.set(W + "fldCharType", "begin")
    # Keep each complex-field control in its own run for Word/LibreOffice.
    parent = etree.Element(W + "field-placeholder")
    parent.append(run)
    instr = etree.SubElement(parent, W + "r")
    instr_node = etree.SubElement(instr, W + "instrText")
    instr_node.set(f"{{{XML_NS}}}space", "preserve")
    instr_node.text = f" {instruction} "
    separate_run = etree.SubElement(parent, W + "r")
    separate = etree.SubElement(separate_run, W + "fldChar")
    separate.set(W + "fldCharType", "separate")
    result_run = etree.SubElement(parent, W + "r")
    text = etree.SubElement(result_run, W + "t")
    text.text = cached
    end_run = etree.SubElement(parent, W + "r")
    end = etree.SubElement(end_run, W + "fldChar")
    end.set(W + "fldCharType", "end")
    # Caller unwraps the temporary container.
    return parent


def _pageref_field_elements(instruction: str, cached: str) -> list[etree._Element]:
    container = _field_run(instruction, cached)
    return list(container)


def _make_entry_paragraph(style_id: str, heading: TocHeading, page: int,
                          tab_position_twips: int) -> etree._Element:
    paragraph = etree.Element(W + "p")
    ppr = etree.SubElement(paragraph, W + "pPr")
    pstyle = etree.SubElement(ppr, W + "pStyle")
    pstyle.set(W + "val", style_id)
    tabs = etree.SubElement(ppr, W + "tabs")
    tab = etree.SubElement(tabs, W + "tab")
    tab.set(W + "val", "right")
    tab.set(W + "leader", "dot")
    tab.set(W + "pos", str(tab_position_twips))
    hyperlink = etree.SubElement(paragraph, W + "hyperlink")
    hyperlink.set(W + "anchor", heading.bookmark)
    hyperlink.set(W + "history", "1")
    run = etree.SubElement(hyperlink, W + "r")
    text = etree.SubElement(run, W + "t")
    text.set(f"{{{XML_NS}}}space", "preserve")
    text.text = heading.title
    tab_run = etree.SubElement(paragraph, W + "r")
    etree.SubElement(tab_run, W + "tab")
    field_nodes = _pageref_field_elements(f"PAGEREF {heading.bookmark} \\h", str(page))
    for node in field_nodes:
        paragraph.append(node)
    return paragraph


def materialize_docx(
    baseline_docx: Path,
    output_docx: Path,
    headings: list[dict[str, Any]],
    page_cache: dict[str, int],
) -> list[TocHeading]:
    with ZipFile(baseline_docx) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    document_root = etree.fromstring(members["word/document.xml"])
    styles_root = etree.fromstring(members["word/styles.xml"])
    toc_fields = _toc_field_paragraphs(document_root)
    if len(toc_fields) != 1:
        raise ValueError(f"expected exactly one dynamic TOC field; found {len(toc_fields)}")
    toc_p, instruction = toc_fields[0]
    depth = toc_depth_from_instruction(instruction)
    style_ids = _toc_style_ids(styles_root, depth)
    tab_position = _toc_tab_position_twips(document_root, toc_p)
    existing_names = set(document_root.xpath(".//w:bookmarkStart/@w:name", namespaces=NS))
    toc_headings = _prepare_output_headings(document_root, styles_root, headings, existing_names)
    if page_cache and set(page_cache) != {item.bookmark for item in toc_headings}:
        raise ValueError("page cache keys do not match the source-bound heading set")
    _, _ = _empty_field_result(toc_p)
    body = document_root.find("w:body", namespaces=NS)
    if body is None:
        raise ValueError("DOCX body is missing")
    insert_at = list(body).index(toc_p) + 1
    for item in toc_headings:
        page = page_cache.get(item.bookmark)
        if page is None:
            # Initial renderer pass: retain an empty TOC field so the renderer
            # can paginate without fabricated page values.
            continue
        if not isinstance(page, int) or page < 1:
            raise ValueError(f"invalid rendered page value for {item.title!r}: {page!r}")
        entry = _make_entry_paragraph(style_ids[item.level], item, page, tab_position)
        body.insert(insert_at, entry)
        insert_at += 1
    if page_cache:
        # The master TOC field spans the generated cached result paragraphs.
        last_entry = body[insert_at - 1]
        last_entry.append(_field_run("", "", outer_end=True))
    else:
        toc_p.append(_field_run("", "", outer_end=True))
    members["word/document.xml"] = etree.tostring(
        document_root, xml_declaration=True, encoding="UTF-8", standalone=True
    )
    output_docx.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output_docx, "w", ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return toc_headings


def render_pdf(docx_path: Path, output_pdf: Path, log_path: Path, profile_dir: Path,
               *, timeout: int = 900) -> dict[str, Any]:
    soffice = shutil.which("soffice")
    if not soffice:
        raise RuntimeError("LibreOffice soffice executable is unavailable")
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    profile_dir.mkdir(parents=True, exist_ok=True)
    command = [soffice, "--headless", "--norestore", "--nodefault",
               f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
               "--convert-to", "pdf", "--outdir", str(output_pdf.parent), str(docx_path)]
    started = time.monotonic()
    result = run_process(command, cwd=output_pdf.parent, timeout=timeout)
    elapsed = round(time.monotonic() - started, 3)
    log_path.write_text(
        json.dumps({"command": command, "exit_code": result.returncode,
                    "stdout": result.stdout, "stderr": result.stderr,
                    "elapsed_seconds": elapsed, "timed_out": result.returncode == 124},
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    actual_pdf = output_pdf.parent / f"{docx_path.stem}.pdf"
    if result.returncode != 0 or not actual_pdf.is_file() or actual_pdf.stat().st_size == 0:
        raise RuntimeError(
            f"LibreOffice PDF render failed (exit={result.returncode}, expected={actual_pdf}); "
            f"see {log_path}"
        )
    try:
        import fitz
        document = fitz.open(actual_pdf)
        page_count = len(document)
        document.close()
    except Exception as exc:
        raise RuntimeError(f"rendered PDF is unreadable: {actual_pdf}: {exc}") from exc
    return {"command": command, "exit_code": result.returncode, "log": str(log_path),
            "elapsed_seconds": elapsed, "pdf": str(actual_pdf),
            "pdf_sha256": sha256(actual_pdf), "pdf_bytes": actual_pdf.stat().st_size,
            "page_count": page_count, "renderer_version": _soffice_version(soffice)}


def _soffice_version(soffice: str) -> str:
    result = subprocess.run([soffice, "--version"], capture_output=True, text=True, timeout=30, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"cannot identify LibreOffice version (exit={result.returncode})")
    return result.stdout.strip()


def rendered_page_map(pdf_path: Path, headings: list[TocHeading]) -> dict[str, int]:
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PyMuPDF is required to read rendered PDF outlines") from exc
    document = fitz.open(pdf_path)
    try:
        raw_outline = document.get_toc(simple=True)
    finally:
        document.close()
    indexed: dict[str, list[tuple[int, int, str]]] = {}
    for level, title, page in raw_outline:
        normalized = normalized_title(str(title)).casefold()
        indexed.setdefault(normalized, []).append((int(level), int(page), str(title)))
    page_cache: dict[str, int] = {}
    for heading in headings:
        matches = indexed.get(normalized_title(heading.title).casefold(), [])
        if len(matches) != 1:
            raise ValueError(
                f"rendered PDF must contain exactly one outline target for source heading "
                f"{heading.title!r}; found {len(matches)}"
            )
        actual_level, page, rendered_title = matches[0]
        if actual_level != heading.level + 1:
            raise ValueError(
                f"PDF outline depth mismatch for {heading.title!r}: "
                f"source={heading.level + 1}, rendered={actual_level}"
            )
        if page < 1:
            raise ValueError(f"PDF outline has invalid page for {heading.title!r}: {page}")
        page_cache[heading.bookmark] = page
    return page_cache


def audit_visible_toc_rows(
    pdf_path: Path, headings: list[TocHeading], expected_pages: dict[str, int],
) -> list[dict[str, Any]]:
    """Verify each cached title+page is visibly rendered as one TOC row.

    This rejects an XML-only cache that LibreOffice did not actually paint or
    a layout that wraps the page number away from its source title.
    """
    import fitz

    document = fitz.open(pdf_path)
    try:
        visible_rows: list[dict[str, Any]] = []
        for heading in headings:
            expected_page = expected_pages[heading.bookmark]
            target = re.sub(r"\s+", "", normalized_title(heading.title)).casefold()
            matches: list[dict[str, Any]] = []
            for page_number, page in enumerate(document, 1):
                grouped: list[list[tuple[float, float, str]]] = []
                page_words = sorted(page.get_text("words"), key=lambda word: (float(word[1]), float(word[0])))
                for word in page_words:
                    x0, y0, _x1, _y1, value, _block_no, _line_no, _word_no = word
                    # PDF text extractors may split a single visual row into
                    # several logical lines at hyperlink/run boundaries.  Use
                    # rendered baselines, not those inferred block IDs.
                    item = (float(x0), float(y0), str(value))
                    if grouped and abs(item[1] - sum(token[1] for token in grouped[-1]) / len(grouped[-1])) <= 4.0:
                        grouped[-1].append(item)
                    else:
                        grouped.append([item])
                for words in grouped:
                    line = " ".join(value for _, _, value in sorted(words, key=lambda item: item[0]))
                    compact = re.sub(r"\s+", "", line).casefold()
                    final_number = re.search(r"(\d+)\s*$", line)
                    if target in compact and final_number and int(final_number.group(1)) == expected_page:
                        matches.append({"page": page_number, "y": min(y0 for _, y0, _ in words),
                                       "line": line.strip()[:500]})
            if len(matches) != 1:
                raise ValueError(
                    f"rendered PDF must show exactly one single-line TOC row for "
                    f"{heading.title!r} -> {expected_page}; found {len(matches)}"
                )
            visible_rows.append({"title": heading.title, "page_value": expected_page, **matches[0]})
        ordered = sorted(visible_rows, key=lambda row: (row["page"], row["y"]))
        if [row["title"] for row in ordered] != [item.title for item in headings]:
            raise ValueError("visible PDF TOC entry order differs from the source heading order")
        return visible_rows
    finally:
        document.close()


def audit_materialized_toc(
    docx_path: Path, pdf_path: Path, headings: list[TocHeading],
    expected_pages: dict[str, int],
) -> dict[str, Any]:
    with ZipFile(docx_path) as archive:
        document_root = etree.fromstring(archive.read("word/document.xml"))
        styles_root = etree.fromstring(archive.read("word/styles.xml"))
    toc_fields = _toc_field_paragraphs(document_root)
    if len(toc_fields) != 1:
        raise ValueError(f"final audit expected one dynamic TOC field; found {len(toc_fields)}")
    toc_p, instruction = toc_fields[0]
    depth = toc_depth_from_instruction(instruction)
    if depth < max(item.level for item in headings) + 1:
        raise ValueError("final field depth excludes a source-bound heading")
    body = document_root.find("w:body", namespaces=NS)
    body_paragraphs = body.xpath(".//w:p", namespaces=NS) if body is not None else []
    toc_index = body_paragraphs.index(toc_p)
    entries: list[dict[str, Any]] = []
    for paragraph in body_paragraphs[toc_index + 1:]:
        style_id = _paragraph_style_id(paragraph) or ""
        style_node = styles_root.xpath(f".//w:style[@w:styleId='{style_id}']/w:name/@w:val", namespaces=NS)
        style_name = style_node[0] if style_node else style_id
        match = TOC_STYLE_RE.match(style_name) or TOC_STYLE_RE.match(style_id)
        if not match:
            if entries:
                break
            continue
        title = normalized_title("".join(paragraph.xpath(".//w:hyperlink//w:t/text()", namespaces=NS)))
        anchors = paragraph.xpath(".//w:hyperlink/@w:anchor", namespaces=NS)
        instructions = paragraph.xpath(".//w:instrText/text()", namespaces=NS)
        values = paragraph.xpath(".//w:instrText[contains(translate(., 'pageref', 'PAGEREF'), 'PAGEREF')]/../following-sibling::w:r/w:t/text()", namespaces=NS)
        # The field uses adjacent runs; parse by its exact instruction, then
        # read text from its cached result range in the containing paragraph.
        instruction_text = " ".join(instructions)
        ref = re.search(r"PAGEREF\s+([^\s\\]+)", instruction_text, re.I)
        if not ref or len(anchors) != 1:
            raise ValueError(f"TOC row lacks a unique hyperlink/PAGEREF target: {title!r}")
        bookmark = ref.group(1)
        if anchors[0] != bookmark:
            raise ValueError(f"TOC hyperlink and PAGEREF targets differ for {title!r}")
        # Cached result is the text node in the field run after the separator.
        field_instruction = paragraph.xpath(
            ".//w:instrText[contains(translate(., 'pageref', 'PAGEREF'), 'PAGEREF')]",
            namespaces=NS,
        )
        if len(field_instruction) != 1:
            raise ValueError(f"TOC row must have one PAGEREF field: {title!r}")
        runs = list(paragraph.xpath(".//w:r", namespaces=NS))
        instr_run = field_instruction[0].getparent()
        while instr_run is not None and instr_run.tag != W + "r":
            instr_run = instr_run.getparent()
        if instr_run not in runs:
            raise ValueError(f"PAGEREF instruction run missing for {title!r}")
        run_index = runs.index(instr_run)
        cached_text = ""
        for run in runs[run_index + 1:]:
            if run.xpath(".//w:fldChar[@w:fldCharType='end']", namespaces=NS):
                break
            cached_text += "".join(run.xpath(".//w:t/text()", namespaces=NS))
        try:
            cached_page = int(cached_text.strip())
        except ValueError as exc:
            raise ValueError(f"TOC row has an invalid cached page value: {title!r}: {cached_text!r}") from exc
        entries.append({"title": title, "level": int(match.group(1)) - 1,
                        "bookmark": bookmark, "cached_page": cached_page})
    expected = [{"title": item.title, "level": item.level, "bookmark": item.bookmark,
                 "cached_page": expected_pages[item.bookmark]} for item in headings]
    if entries != expected:
        raise ValueError("final TOC rows/order/depth/cached page values differ from source and rendered PDF")
    actual_pages = rendered_page_map(pdf_path, headings)
    if actual_pages != expected_pages:
        raise ValueError("final PDF outline pagination differs from the page map used for the TOC cache")
    visible_rows = audit_visible_toc_rows(pdf_path, headings, expected_pages)
    return {"status": "passed", "entry_count": len(entries), "entries": entries,
            "dynamic_toc_instruction": instruction, "rendered_pdf_outline_matches": True,
            "visible_pdf_rows": visible_rows, "field_refresh_claimed": False}


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def materialize(
    source_docx: Path, baseline_docx: Path, output_docx: Path, work_dir: Path,
    *, max_iterations: int = 8, timeout: int = 900,
) -> dict[str, Any]:
    if max_iterations < 2:
        raise ValueError("at least two render iterations are required to verify cache convergence")
    for path, label in ((source_docx, "source DOCX"), (baseline_docx, "baseline review draft")):
        if not path.is_file():
            raise ValueError(f"{label} is missing: {path}")
    if source_docx.resolve() == output_docx.resolve() or baseline_docx.resolve() == output_docx.resolve():
        raise ValueError("output DOCX must be a new file distinct from both source inputs")
    work_dir.mkdir(parents=True, exist_ok=False)
    source_hash, baseline_hash = sha256(source_docx), sha256(baseline_docx)
    with ZipFile(baseline_docx) as archive:
        baseline_root = etree.fromstring(archive.read("word/document.xml"))
        baseline_styles = etree.fromstring(archive.read("word/styles.xml"))
    toc_fields = _toc_field_paragraphs(baseline_root)
    if len(toc_fields) != 1:
        raise ValueError(f"expected exactly one baseline dynamic TOC field; found {len(toc_fields)}")
    toc_depth = toc_depth_from_instruction(toc_fields[0][1])
    with ZipFile(baseline_docx) as archive:
        baseline_members = {name: archive.read(name) for name in archive.namelist()}
    page_numbering = _effective_page_numbering(baseline_root, baseline_members)
    manual_before = _manual_review_snapshot(baseline_root, baseline_styles)
    headings = extract_source_headings(source_docx, toc_depth)
    # Validate that output styles can represent every selected source level.
    _toc_style_ids(baseline_styles, toc_depth)
    cache: dict[str, int] = {}
    iteration_reports: list[dict[str, Any]] = []
    converged = False
    final_pdf: Path | None = None
    final_items: list[TocHeading] = []
    for iteration in range(1, max_iterations + 1):
        iteration_dir = work_dir / f"iteration-{iteration:02d}"
        iteration_dir.mkdir()
        candidate = iteration_dir / "candidate.docx"
        final_items = materialize_docx(baseline_docx, candidate, headings, cache)
        log = iteration_dir / "libreoffice-render.json"
        profile = iteration_dir / "lo-user-profile"
        render = render_pdf(candidate, iteration_dir / "candidate.pdf", log, profile, timeout=timeout)
        pdf = Path(render["pdf"])
        actual = rendered_page_map(pdf, final_items)
        iteration_reports.append({
            "iteration": iteration, "input_cached_pages": dict(cache),
            "rendered_pages": dict(actual), "render": render,
            "cache_matches_render": bool(cache) and cache == actual,
            "candidate_docx_sha256": sha256(candidate),
        })
        if cache and cache == actual:
            converged = True
            final_pdf = pdf
            output_docx.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(candidate, output_docx)
            break
        cache = actual
    if not converged or final_pdf is None:
        raise RuntimeError(
            f"TOC page cache did not converge in {max_iterations} actual PDF renders; "
            "no final artifact was accepted"
        )
    if sha256(source_docx) != source_hash or sha256(baseline_docx) != baseline_hash:
        raise RuntimeError("immutable source or baseline review-draft bytes changed during materialization")
    with ZipFile(output_docx) as archive:
        final_root = etree.fromstring(archive.read("word/document.xml"))
        final_styles = etree.fromstring(archive.read("word/styles.xml"))
    manual_after = _manual_review_snapshot(final_root, final_styles)
    if manual_before != manual_after:
        raise RuntimeError("materialization changed manual-review marker paragraphs")
    final_pages = rendered_page_map(final_pdf, final_items)
    independent_audit = audit_materialized_toc(output_docx, final_pdf, final_items, final_pages)
    report = {
        "schema_version": "1.0", "protocol": "source_bound_pdf_outline_toc_cache_v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "passed", "submission_ready": False,
        "source": {"path": str(source_docx.resolve()), "sha256": source_hash,
                   "bytes": source_docx.stat().st_size},
        "baseline_review_draft": {"path": str(baseline_docx.resolve()),
                                  "sha256": baseline_hash, "bytes": baseline_docx.stat().st_size},
        "output_docx": {"path": str(output_docx.resolve()), "sha256": sha256(output_docx),
                        "bytes": output_docx.stat().st_size},
        "renderer": {"name": "LibreOffice headless PDF export",
                     "version": iteration_reports[-1]["render"]["renderer_version"]},
        "pagination_semantics": page_numbering,
        "final_heading_page_map": [
            {"title": item.title, "outline_level": item.level + 1,
             "physical_pdf_page": final_pages[item.bookmark],
             "display_page": final_pages[item.bookmark],
             "page_number_basis": "continuous_default_decimal"}
            for item in final_items
        ],
        "field_update": {"actual_word_or_libreoffice_field_refresh": False,
                         "method": "source-bound headings + PDF outline page map + cached PAGEREF fields",
                         "dynamic_toc_and_pageref_instructions_preserved": True},
        "source_headings": [asdict(item) for item in final_items],
        "iterations": iteration_reports,
        "final_pdf": {"path": str(final_pdf.resolve()), "sha256": sha256(final_pdf),
                      "bytes": final_pdf.stat().st_size,
                      "page_count": iteration_reports[-1]["render"]["page_count"]},
        "independent_toc_audit": independent_audit,
        "manual_review_items_retained": {"passed": True, "before": manual_before, "after": manual_after},
    }
    _write_json(work_dir / "toc-materialization-report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_docx", type=Path, help="immutable source DOCX defining TOC titles and levels")
    parser.add_argument("baseline_review_draft", type=Path, help="newly derived review-draft baseline with an empty TOC field")
    parser.add_argument("output_docx", type=Path, help="new final review-draft DOCX path")
    parser.add_argument("--work-dir", type=Path, required=True,
                        help="new directory for all PDF renders, LO profiles, logs, and report")
    parser.add_argument("--max-iterations", type=int, default=8)
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()
    try:
        report = materialize(args.source_docx.resolve(), args.baseline_review_draft.resolve(),
                             args.output_docx.resolve(), args.work_dir.resolve(),
                             max_iterations=args.max_iterations, timeout=args.timeout)
    except Exception as exc:
        print(f"TOC materialization blocked: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
