"""Audit an actual PDF export against native OMML, without repairing the PDF.

PDF ToUnicode failures and short delimiters are invisible to DOCX-only checks.
An optional source/PDF-hash-bound regions JSON identifies each tall delimiter's
operand rectangle in PDF points, by 0-based math/delimiter index and 1-based
page. A reviewer must measure these regions on the actual export; an unmapped
object is blocking, never inferred from a matching token bag. This check does
not attest to the editor UI, pagination equality, or a complete visual review.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import unicodedata
from zipfile import ZipFile

from docx.oxml.ns import qn
from lxml import etree
import fitz

from equation_compat import tall_delimiters
from semantic_contract import strict_json_read

_DELIMITER_INK = set("[]{}()|⌈⌉⌊⌋⟨⟩⎡⎢⎣⎤⎥⎦⎧⎨⎩⎫⎬⎭⎛⎜⎝⎞⎟⎠")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _normal(text):
    return "".join(c for c in unicodedata.normalize("NFKC", text)
                   if not c.isspace()).replace("−", "-")


def source_math(path):
    with ZipFile(path) as package:
        root = etree.fromstring(package.read("word/document.xml"),
                                etree.XMLParser(resolve_entities=False, no_network=True))
        settings = etree.fromstring(package.read("word/settings.xml"))
    math = root.xpath(".//m:oMathPara|.//m:oMath[not(ancestor::m:oMathPara)]",
                      namespaces={"m": "http://schemas.openxmlformats.org/officeDocument/2006/math"})
    font = settings.find(".//"+qn("m:mathFont"))
    return math, font.get(qn("m:val")) if font is not None else None


def _box(rects):
    if not rects:
        return None
    result = fitz.Rect(rects[0])
    for rect in rects[1:]:
        result |= fitz.Rect(rect)
    return result


def _finding(code, **facts):
    return {"code": code, "requires_manual_review": True, **facts}


def audit_delimiter(page, delimiter, bbox):
    """Measure text or vector delimiters; never treat absent ink as success."""
    region = fitz.Rect(bbox)
    findings = []
    if region.is_empty or not page.rect.contains(region):
        return {"findings": [_finding("delimiter_region_invalid")]}
    chars = []
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                for char in span["chars"]:
                    rect = fitz.Rect(char["bbox"])
                    if region.contains(rect.tl+(rect.br-rect.tl)/2) and char["c"].strip():
                        chars.append({**char, "size": span["size"], "font": span["font"]})
    content = [c for c in chars if c["c"] not in _DELIMITER_INK]
    font_metric_box = _box([c["bbox"] for c in content])
    # Stretched font glyphs may have a small logical text box and a tall ink
    # outline. seqno binds text traces to the actual PDF drawing bbox; using
    # the logical box here would falsely reject a correctly stretched glyph.
    ink_log = page.get_bboxlog(); operand_ink = []; delimiter_ink = []
    for span in page.get_texttrace():
        value = "".join(chr(c[0]) for c in span["chars"] if c[0] >= 0).strip()
        kind, bounds = ink_log[span["seqno"]]
        rect = fitz.Rect(bounds)
        if not value or kind not in {"fill-text", "stroke-text"} or not region.contains(rect):
            continue
        visible = set(value)-set(" ")
        if visible <= _DELIMITER_INK:
            delimiter_ink.append(rect)
        elif not visible.intersection(_DELIMITER_INK):
            operand_ink.append(rect)
    content_box = _box(operand_ink)
    if content_box is None:
        return {"findings": [_finding("delimiter_operand_ink_unresolved")]}
    # Keep this a strict ordered check of the supported flat row/cell grammar.
    # Structured/nested operands need a reviewer; they are not flattened to a
    # multiset that could hide cell swaps or altered piecewise correspondence.
    operands = delimiter.findall(qn("m:e"))
    supported = len(operands) == 1 and len(tall_delimiters(delimiter)) == 1
    containers = [n for n in operands[0] if n.tag in {qn("m:m"), qn("m:eqArr")}] if operands else []
    supported = supported and len(containers) == 1 and len(operands[0]) == 1
    if supported:
        container = containers[0]
        rows = container.findall(qn("m:mr")) if container.tag == qn("m:m") else container.findall(qn("m:e"))
        cells = [e for row in rows for e in row.findall(qn("m:e"))] if container.tag == qn("m:m") else rows
        supported = all(all(n.tag in {qn("m:r")} for n in cell) for cell in cells)
    if not supported:
        findings.append(_finding("delimiter_operand_structure_requires_review"))
    else:
        bands = []
        for c in sorted(content, key=lambda c: (fitz.Rect(c["bbox"]).y0+fitz.Rect(c["bbox"]).y1)/2):
            cy = (c["bbox"][1]+c["bbox"][3])/2
            if not bands or abs(cy-bands[-1][0]) > c["size"]*.35:
                bands.append((cy, [c]))
            else:
                bands[-1][1].append(c)
        actual_rows = ["".join(c["c"] for c in sorted(chars, key=lambda c: c["bbox"][0])) for _, chars in bands]
        expected_rows = ["".join(t.text or "" for t in row.iter(qn("m:t"))) for row in rows]
        if [_normal(t) for t in actual_rows] != [_normal(t) for t in expected_rows]:
            findings.append(_finding("delimiter_operand_text_unverified", expected_rows=expected_rows,
                                     extracted_rows=actual_rows))
    pr = delimiter.find(qn("m:dPr"))
    sides = []
    for side, tag, default in (("left", "begChr", "("), ("right", "endChr", ")")):
        char = pr.find(qn("m:"+tag)) if pr is not None else None
        value = char.get(qn("m:val"), default) if char is not None else default
        if value:
            sides.append((side, value))
    candidates = delimiter_ink
    candidates += [d["rect"] for d in page.get_drawings()
                   if region.contains(d["rect"]) and d["rect"].height > 1]
    measured = []
    max_width = max(c["size"] for c in content)*1.5
    for side, char in sides:
        matches = [rect for rect in candidates if rect.width <= max_width and
                   ((rect.x0+rect.x1)/2 <= content_box.x0+1 if side == "left"
                    else (rect.x0+rect.x1)/2 >= content_box.x1-1)]
        # Separate side-by-side objects are ambiguous even in a measured ROI.
        if matches and max(r.x1 for r in matches)-min(r.x0 for r in matches) > max_width:
            matches = []
        ink = _box(matches)
        facts = {"side": side, "character": char, "ink_bbox": list(ink) if ink else None}
        if ink is None:
            findings.append(_finding("delimiter_ink_unresolved", **facts))
        else:
            facts["height_coverage"] = ink.height/content_box.height
            if ink.y0 > content_box.y0+2 or ink.y1 < content_box.y1-2:
                findings.append(_finding("delimiter_does_not_cover_operand", **facts,
                                         operand_bbox=list(content_box)))
        measured.append(facts)
    return {"findings": findings, "region_bbox": list(region),
            "operand_bbox": list(content_box), "font_metric_operand_bbox": list(font_metric_box) if font_metric_box else None,
            "delimiters": measured,
            "observed_fonts": sorted({c["font"] for c in chars})}


def audit(source, pdf_path, regions=None):
    math, font = source_math(source)
    source_sha = sha(source); pdf_sha = sha(pdf_path)
    findings = []; observations = []; needed = []
    for index, node in enumerate(math):
        for di, delimiter in enumerate(tall_delimiters(node)):
            pr = delimiter.find(qn("m:dPr")); grow = pr.find(qn("m:grow")) if pr is not None else None
            if grow is None or grow.get(qn("m:val"), "1") not in {"0", "false", "off"}:
                needed.append((index, di, delimiter))
    mapping = {}
    if regions is not None:
        if regions.get("source_docx_sha256") != source_sha or regions.get("pdf_sha256") != pdf_sha:
            findings.append(_finding("export_region_source_hash_mismatch"))
        else:
            for item in regions.get("regions", []):
                key = (item.get("math_index"), item.get("delimiter_index"))
                if key in mapping:
                    findings.append(_finding("export_region_duplicate", math_index=key[0], delimiter_index=key[1]))
                mapping[key] = item
    with fitz.open(pdf_path) as pdf:
        fonts = sorted({font[3] for page in pdf for font in page.get_fonts()})
        for number, page in enumerate(pdf, 1):
            broken = {}
            for span in page.get_texttrace():
                bad = [c for c in span["chars"] if c[0] == 0xfffd]
                if bad:
                    entry = broken.setdefault(span["font"], {"count": 0, "glyph_ids": set()})
                    entry["count"] += len(bad); entry["glyph_ids"].update(c[1] for c in bad)
            for name, facts in broken.items():
                findings.append(_finding("pdf_unicode_mapping_unresolved", page=number, font=name,
                                         count=facts["count"], glyph_ids=sorted(facts["glyph_ids"])))
        for index, di, delimiter in needed:
            item = mapping.get((index, di))
            if item is None:
                findings.append(_finding("tall_delimiter_region_unmapped", math_index=index, delimiter_index=di))
                continue
            try:
                page_number = item["page"]
                if type(page_number) is not int or not 1 <= page_number <= len(pdf):
                    raise ValueError("page out of range")
                result = audit_delimiter(pdf[page_number-1], delimiter, item["bbox"])
            except (KeyError, TypeError, ValueError) as exc:
                result = {"findings": [_finding("delimiter_region_invalid", reason=str(exc))]}
            identity = {"math_index": index, "delimiter_index": di, "page": item.get("page")}
            observations.append({**identity, **result})
            findings.extend({**identity, **finding} for finding in result["findings"])
        return {"status": "manual_review_required" if findings else "checks_passed",
                "export_checks_passed": not findings, "source_docx_sha256": source_sha,
                "pdf_sha256": pdf_sha, "pdf_metadata": pdf.metadata, "pdf_pages": len(pdf),
                "native_math_objects": len(math), "declared_math_font": font,
                "observed_pdf_fonts": fonts, "tall_delimiters_requiring_coverage": len(needed),
                "delimiter_observations": observations, "findings": findings,
                "verification_scope": "PDF Unicode mapping and measured tall delimiter coverage; flat row text order",
                "whole_document_visual_review": False, "editor_ui_verified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path); parser.add_argument("pdf", type=Path)
    parser.add_argument("--regions", type=Path); parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = audit(args.source, args.pdf, strict_json_read(args.regions) if args.regions else None)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n")
    return 0 if report["export_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
