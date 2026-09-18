#!/usr/bin/env python3
"""Apply narrow, verified OMML layout fixes to a Word-rendered baseline.

The source conversion is still used to preserve the mathematical tokens, but
the final layout operation is performed on the serialized OMML.  This is
intentional: Pandoc represents the nested ``aligned`` used for (3.125) as a
one-row ``m:m`` matrix, so a source-only check cannot prove that Word will
actually receive continuation rows.  The patch below converts that scaffold
to ``m:eqArr`` and carries the already verified four-row OMML for (3.127).

Only the unique ``m:oMath`` in each explicitly labelled paragraph is replaced.
No font size, paragraph style, or document-wide setting is changed.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from lxml import etree


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
NS = {"w": W_NS, "m": M_NS}
M = f"{{{M_NS}}}"


def find_tagged_paragraph(root: etree._Element, label: str) -> etree._Element:
    paragraphs = root.xpath(".//w:body/w:p", namespaces=NS)
    hits = []
    for paragraph in paragraphs:
        text = "".join(paragraph.xpath(".//w:t/text()", namespaces=NS))
        if label in text:
            hits.append(paragraph)
    if len(hits) != 1:
        raise RuntimeError(f"expected exactly one paragraph containing {label!r}, found {len(hits)}")
    return hits[0]


def get_single_math(paragraph: etree._Element, label: str) -> etree._Element:
    math = paragraph.xpath(".//m:oMath", namespaces=NS)
    if len(math) != 1:
        raise RuntimeError(f"expected one m:oMath for {label}; found {len(math)}")
    return math[0]


def validate_four_row_math(math: etree._Element, label: str) -> dict[str, int]:
    rows = math.xpath(".//m:eqArr/m:e", namespaces=NS)
    breaks = math.xpath(".//m:brk", namespaces=NS)
    if len(rows) != 4 or breaks:
        raise RuntimeError(
            f"{label} is not the verified four-row layout: rows={len(rows)}, breaks={len(breaks)}"
        )
    return {"eqarr_row_count": len(rows), "break_count": len(breaks)}


def convert_3125_radical_scaffold(source_math: etree._Element) -> tuple[etree._Element, dict[str, int]]:
    """Convert the source nested-aligned matrix scaffold to an OMML array."""
    math = copy.deepcopy(source_math)
    candidates = math.xpath(".//m:rad/m:e/m:m", namespaces=NS)
    candidates = [
        matrix
        for matrix in candidates
        if len(matrix.xpath("./m:mr", namespaces=NS)) == 1
        and len(matrix.xpath("./m:mr/m:e", namespaces=NS)) == 5
    ]
    if len(candidates) != 1:
        raise RuntimeError(f"3.125 source must contain one five-cell radical scaffold; found {len(candidates)}")

    matrix = candidates[0]
    cells = matrix.xpath("./m:mr/m:e", namespaces=NS)
    if "".join(cells[0].xpath(".//m:t/text()", namespaces=NS)).strip():
        raise RuntimeError("3.125 scaffold first cell is not the expected empty alignment cell")

    eqarr = etree.Element(f"{M}eqArr")
    for cell in cells[1:]:
        row = etree.SubElement(eqarr, f"{M}e")
        for child in cell:
            row.append(copy.deepcopy(child))
    matrix.getparent().replace(matrix, eqarr)

    rows = math.xpath(".//m:eqArr/m:e", namespaces=NS)
    breaks = math.xpath(".//m:brk", namespaces=NS)
    if len(rows) != 4 or breaks:
        raise RuntimeError(f"3.125 OMML conversion failed: rows={len(rows)}, breaks={len(breaks)}")
    return math, {
        "source_matrix_cell_count": len(cells),
        "eqarr_row_count": len(rows),
        "break_count": len(breaks),
    }


def apply_patches(
    base: Path,
    equation_source: Path,
    output: Path,
    report: Path,
    labels: tuple[str, ...],
) -> None:
    with ZipFile(base) as archive, ZipFile(equation_source) as source_archive:
        base_document = etree.fromstring(archive.read("word/document.xml"))
        source_document = etree.fromstring(source_archive.read("word/document.xml"))
        records: dict[str, dict[str, object]] = {}

        for label in labels:
            base_paragraph = find_tagged_paragraph(base_document, label)
            source_paragraph = find_tagged_paragraph(source_document, label)
            base_math = get_single_math(base_paragraph, label)
            source_math = get_single_math(source_paragraph, label)

            if label == "3.125":
                replacement, record = convert_3125_radical_scaffold(source_math)
                record["source_layout"] = "nested aligned serialized as five-cell m:m scaffold"
            else:
                replacement = copy.deepcopy(source_math)
                record = {
                    "source_layout": "source aligned serialized as four-row m:eqArr",
                    **validate_four_row_math(replacement, label),
                }

            parent = base_math.getparent()
            parent.replace(base_math, replacement)
            records[label] = {
                "base_paragraph_math_count": 1,
                "source_paragraph_math_count": 1,
                "replacement_scope": "single m:oMath in the uniquely tagged paragraph",
                **record,
            }

        output.parent.mkdir(parents=True, exist_ok=True)
        with ZipFile(output, "w", compression=ZIP_DEFLATED) as out_archive:
            for info in archive.infolist():
                data = (
                    etree.tostring(base_document, xml_declaration=True, encoding="UTF-8", standalone="yes")
                    if info.filename == "word/document.xml"
                    else archive.read(info.filename)
                )
                out_archive.writestr(info, data)

    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps(
            {
                "schema_version": "2.0",
                "base_docx": str(base),
                "equation_source_docx": str(equation_source),
                "output_docx": str(output),
                "labels": list(labels),
                "records": records,
                "replacement_scope": "two explicitly labelled m:oMath objects; no font or paragraph-style changes",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("base_docx", type=Path)
    parser.add_argument("equation_source_docx", type=Path)
    parser.add_argument("output_docx", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--labels", nargs="+", default=["3.125", "3.127"])
    args = parser.parse_args()
    apply_patches(args.base_docx, args.equation_source_docx, args.output_docx, args.report, tuple(args.labels))


if __name__ == "__main__":
    main()
