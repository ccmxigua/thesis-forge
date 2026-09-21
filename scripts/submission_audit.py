#!/usr/bin/env python3
"""Template-independent, evidence-driven submission audit for DOCX artifacts.

This module deliberately does not trust generator counters.  It re-opens the
serialized OPC package, resolves section/header/footer relationships, inspects
actual field instructions and visible story text, and emits a separate
submission-readiness state.  Rendering is a distinct proof stage: without an
accepted Word/PDF render report, ``submission_ready`` remains false.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from lxml import etree

from format_spec_validation import load_and_validate
from render_attestation import load_key, verify as verify_attestation
from artifact_io import atomic_write_text, paths_alias

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"w": W_NS, "r": R_NS, "pr": PKG_REL_NS}
W = f"{{{W_NS}}}"
R = f"{{{R_NS}}}"

SOURCE_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("latex_ref", re.compile(r"\\(?:ref|autoref|cref|Cref)\s*\{")),
    ("latex_cite", re.compile(r"\\(?:cite|citep|citet|parencite|textcite)\s*\{")),
    ("latex_label", re.compile(r"\\label\s*\{")),
    ("bracket_reference", re.compile(r"\[(?:fig|tab|table|eq|equation|sec|section|chap|chapter):[^\]\n）)]*[\]\n）)]?", re.I)),
    ("tjufe_xref_sentinel", re.compile(r"TJUFE_(?:XREF|LABEL|EQLABEL)")),
    ("unresolved_reference_fallback", re.compile(
        r"(?:Reference|Figure|Table|Equation|Section|Chapter|Theorem|Lemma|Proposition|Corollary|"
        r"Definition|Remark|图|表|式|节|章|定理|引理|命题|推论|定义|备注)\s*\[[A-Za-z][A-Za-z0-9_.:-]*\)",
        re.I,
    )),
)
PLACEHOLDER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("unconfirmed_metadata", re.compile(r"(?:待确认|待填写|待补充|TBD|TODO)", re.I)),
    # Review-draft markers are intentionally visible and must never be
    # mistaken for release evidence.  The strict submission audit treats
    # either the Chinese label or its stable marker id as a critical
    # placeholder; the draft policy can still emit the same DOCX safely.
    ("manual_review_marker", re.compile(r"(?:人工审查|人工待审|MR-\d{4})")),
    ("template_x_placeholder", re.compile(r"(?:X{2,}|Ｘ{2,})")),
    ("template_delete_note", re.compile(r"提交存档论文时请删除此备注")),
    ("incomplete_date_placeholder", re.compile(r"(?:20\s*年\s*月\s*日|二[〇○零]XX年XX月)", re.I)),
)
RENDER_ERROR_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("word_reference_error", re.compile(r"Error!\s*(?:Reference source not found|Bookmark not defined)", re.I)),
    ("word_reference_error_zh", re.compile(r"错误[！!]?(?:未找到引用源|未定义书签)")),
)
PDF_RENDER_ERROR_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    *RENDER_ERROR_PATTERNS,
    ("word_styleref_error", re.compile(
        r"(?:Error!\s*No text of specified style in document|"
        r"错误[！!]?\s*使用[“\"]?开始[”\"]?选项卡将\s*Heading\s*1\s*应用于要在此处显示的文字)",
        re.I,
    )),
    ("word_field_error", re.compile(r"(?:Error!|错误[！!])[^\n]{0,160}", re.I)),
)
DUPLICATE_EQUATION_PREFIX = re.compile(r"式[\s\u00a0]+式\s*[（(]")
# Pandoc emits unresolved citation keys such as ``[peters1994fractal]`` when
# citeproc was not enabled.  Require several hits to avoid treating an
# occasional editorial bracket (for example ``[online]``) as a bibliography
# failure.
RAW_CITATION_KEY = re.compile(r"\[([a-z][A-Za-z0-9_.:-]{4,})\]")
BIBLIOGRAPHY_HEADINGS = {"参考文献", "references", "bibliography"}
CHAPTER_HEADING = re.compile(
    r"^\s*(?:第\s*([一二三四五六七八九十百零〇两\d]+)\s*章|(\d+))(?=\s|$)"
)
CHAPTER_CLAIM = re.compile(r"(?:全文|本文)(?:共(?:分为|有|计)?|分为|包括)\s*([一二三四五六七八九十百零〇两\d]+)\s*章")


def _json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _normalize_part(source_part: str, target: str) -> str:
    """Resolve an OPC relationship target against its source part."""
    if target.startswith("/"):
        return target.lstrip("/")
    base = PurePosixPath(source_part).parent
    pieces: list[str] = []
    for piece in (base / target).parts:
        if piece in {"", "."}:
            continue
        if piece == "..":
            if pieces:
                pieces.pop()
        else:
            pieces.append(piece)
    return "/".join(pieces)


def _relationship_map(zf: zipfile.ZipFile, source_part: str) -> dict[str, str]:
    source = PurePosixPath(source_part)
    rel_part = str(source.parent / "_rels" / f"{source.name}.rels")
    if rel_part not in zf.namelist():
        return {}
    root = etree.fromstring(zf.read(rel_part))
    return {
        node.get("Id", ""): _normalize_part(source_part, node.get("Target", ""))
        for node in root
        if node.get("Id") and node.get("Target") and node.get("TargetMode") != "External"
    }


def _field_instructions(root: etree._Element) -> list[str]:
    values = [v.strip() for v in root.xpath(".//w:fldSimple/@w:instr", namespaces=NS)]
    values.extend(v.strip() for v in root.xpath(".//w:instrText/text()", namespaces=NS))
    return [v for v in values if v]


def _visible_text(root: etree._Element) -> str:
    return "".join(root.xpath(".//w:t/text()", namespaces=NS))


def _paragraph_texts(root: etree._Element) -> list[str]:
    return ["".join(p.xpath(".//w:t/text()", namespaces=NS)) for p in root.xpath(".//w:p", namespaces=NS)]


def _paragraph_main_text(paragraph: etree._Element) -> str:
    """Return a paragraph's own text without nested textbox annotations.

    Word templates sometimes anchor a VML textbox containing formatting
    instructions inside the same w:p as a real heading.  Raw descendant text
    then starts with the textbox annotation even though Word/python-docx show
    the heading itself as the paragraph text.  Structural checks must use the
    main text flow, while the nested textbox remains available to the general
    document-text scans as its own paragraph.
    """
    return "".join(paragraph.xpath(
        ".//w:t[not(ancestor::w:txbxContent)]/text()", namespaces=NS,
    ))


def _field_name(instruction: str) -> str:
    match = re.match(r"\s*([A-Za-z]+)", instruction)
    return match.group(1).upper() if match else ""


def _number(value: str) -> int | None:
    value = re.sub(r"\s+", "", value)
    if value.isdigit():
        return int(value)
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if value == "十":
        return 10
    if "十" in value:
        left, right = value.split("十", 1)
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        return tens * 10 + ones
    if len(value) == 1:
        return digits.get(value)
    return None


def _issue(code: str, severity: str, message: str, evidence: Any = None,
           stage: str = "serialized_docx") -> dict[str, Any]:
    result = {"code": code, "severity": severity, "stage": stage, "message": message}
    if evidence is not None:
        result["evidence"] = evidence
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


PAPER_SIZES_PT: dict[str, tuple[float, float]] = {
    "a4": (595.28, 841.89),
    "letter": (612.0, 792.0),
    "legal": (612.0, 1008.0),
}


def _normalized_page_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _roman_value(value: str) -> int | None:
    if not re.fullmatch(r"[ivxlcdm]+", value, re.I):
        return None
    total = 0
    previous = 0
    for character in reversed(value.upper()):
        current = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}[character]
        total += -current if current < previous else current
        previous = max(previous, current)
    return total


def _page_number_candidate(word: tuple[Any, ...], page_width: float) -> dict[str, Any] | None:
    token = str(word[4]).strip()
    if re.fullmatch(r"\d+", token):
        number_format = "decimal"
        value = int(token)
    else:
        value = _roman_value(token)
        if value is None:
            return None
        number_format = "roman_upper" if token.isupper() else "roman"
    x0, y0, x1, y1 = (float(word[index]) for index in range(4))
    center_x = (x0 + x1) / 2
    relative_x = center_x / page_width if page_width else 0.5
    alignment_region = "left" if relative_x < 0.34 else "right" if relative_x > 0.66 else "center"
    return {
        "token": token,
        "value": value,
        "format": number_format,
        "x0": round(x0, 2),
        "y0": round(y0, 2),
        "x1": round(x1, 2),
        "y1": round(y1, 2),
        "relative_center_x": round(relative_x, 4),
        "alignment_region": alignment_region,
    }


def _numbering_runs(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    for page in pages:
        candidates = page.get("footer_page_number_candidates", [])
        selected = candidates[0] if len(candidates) == 1 else None
        item = {"page": page["page"], **selected} if selected else None
        if item is None:
            if current:
                runs.append({"pages": current})
                current = []
            continue
        if current and (
            item["page"] != current[-1]["page"] + 1
            or item["format"] != current[-1]["format"]
        ):
            runs.append({"pages": current})
            current = []
        current.append(item)
    if current:
        runs.append({"pages": current})
    for run in runs:
        values = [item["value"] for item in run["pages"]]
        run["format"] = run["pages"][0]["format"]
        run["continuous"] = all(right == left + 1 for left, right in zip(values, values[1:]))
        run["start"] = values[0]
        run["end"] = values[-1]
    return runs


def _text_runs(pages: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for page in pages:
        value = str(page.get(key, "")).strip()
        if not value:
            continue
        if runs and runs[-1]["text"] == value and runs[-1]["end_page"] == page["page"] - 1:
            runs[-1]["end_page"] = page["page"]
        else:
            runs.append({"start_page": page["page"], "end_page": page["page"], "text": value})
    return runs


def _pdf_layout_evidence(path: Path) -> dict[str, Any]:
    """Extract deterministic per-page geometry/text evidence with PyMuPDF."""
    import fitz

    document = fitz.open(path)
    pages: list[dict[str, Any]] = []
    duplicate_index: dict[str, list[int]] = {}
    try:
        for page_number, page in enumerate(document, 1):
            rect = page.rect
            words = page.get_text("words", sort=True)
            page_text = page.get_text("text", sort=True)
            normalized = _normalized_page_text(page_text)
            duplicate_content = _normalized_page_text(" ".join(
                str(word[4]) for word in words if float(word[3]) < rect.height * 0.82
            ))
            body_content = _normalized_page_text(" ".join(
                str(word[4]) for word in words
                if float(word[1]) > rect.height * 0.15 and float(word[3]) < rect.height * 0.82
            ))
            compact_length = len(re.sub(r"\s+", "", page_text))
            body_compact_length = len(re.sub(r"\s+", "", body_content))
            top_words = [str(word[4]) for word in words if float(word[1]) <= rect.height * 0.15]
            bottom_words = [str(word[4]) for word in words if float(word[3]) >= rect.height * 0.82]
            footer_numbers = [
                # Word positions footer glyphs by their baseline.  Their bounding-box top can
                # sit a point or two above the final 10% of the page even though the glyph box
                # clearly extends into the footer.  Classify by the box bottom, not its top.
                candidate for word in words if float(word[3]) >= rect.height * 0.90
                for candidate in [_page_number_candidate(word, rect.width)] if candidate is not None
            ]
            image_count = len(page.get_images(full=True))
            drawing_count = len(page.get_drawings())
            normalized_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            duplicate_hash = hashlib.sha256(duplicate_content.encode("utf-8")).hexdigest()
            if len(duplicate_content) >= 40:
                duplicate_index.setdefault(duplicate_hash, []).append(page_number)
            toc_candidate = bool(re.search(r"(?:^|\s)(?:目\s*录|contents?)(?:\s|$)", normalized, re.I))
            toc_entries = []
            if toc_candidate:
                for raw_line in page_text.splitlines():
                    line = _normalized_page_text(raw_line)
                    match = re.match(r"^(.{2,}?)\s*(?:\.{2,}|…+|\s{2,})\s*(\d+|[ivxlcdm]+)\s*$", line, re.I)
                    if match:
                        token = match.group(2)
                        toc_entries.append({
                            "text": line[:500],
                            "title": match.group(1).strip()[:400],
                            "page_token": token,
                            "page_value": int(token) if token.isdigit() else _roman_value(token),
                        })
            pages.append({
                "page": page_number,
                "width_pt": round(rect.width, 2),
                "height_pt": round(rect.height, 2),
                "orientation": "landscape" if rect.width > rect.height else "portrait",
                "rotation": page.rotation,
                "text_length": len(page_text),
                "full_text": page_text[:20000],
                "compact_text_length": compact_length,
                "body_compact_text_length": body_compact_length,
                "normalized_text_sha256": normalized_hash,
                "duplicate_content_sha256": duplicate_hash,
                "top_text": _normalized_page_text(" ".join(top_words))[:500],
                "bottom_text": _normalized_page_text(" ".join(bottom_words))[:500],
                "footer_page_number_candidates": footer_numbers[:20],
                "image_count": image_count,
                "drawing_count": drawing_count,
                "near_blank_candidate": body_compact_length <= 8 and image_count == 0 and drawing_count == 0,
                "toc_candidate": toc_candidate,
                "toc_entry_candidates": toc_entries[:200],
            })
    finally:
        document.close()
    duplicate_groups = [page_numbers for page_numbers in duplicate_index.values() if len(page_numbers) > 1]
    numbering_runs = _numbering_runs(pages)
    return {
        "pages": pages,
        "page_sizes_pt": sorted({(page["width_pt"], page["height_pt"]) for page in pages}),
        "orientations": sorted({page["orientation"] for page in pages}),
        "near_blank_page_candidates": [page["page"] for page in pages if page["near_blank_candidate"]],
        "duplicate_text_page_groups": duplicate_groups,
        "toc_candidate_pages": [page["page"] for page in pages if page["toc_candidate"]],
        "toc_entry_candidates": {
            str(page["page"]): page["toc_entry_candidates"]
            for page in pages if page["toc_entry_candidates"]
        },
        "footer_page_number_candidates": {
            str(page["page"]): page["footer_page_number_candidates"]
            for page in pages if page["footer_page_number_candidates"]
        },
        "page_numbering_runs": numbering_runs,
        "top_text_runs": _text_runs(pages, "top_text"),
        "bottom_text_runs": _text_runs(pages, "bottom_text"),
    }


def _pdf_evidence(path: Path) -> dict[str, Any]:
    """Re-open a rendered PDF and derive evidence independently of its report."""
    result: dict[str, Any] = {
        "path": str(path.resolve()), "exists": path.is_file(), "readable": False,
        "page_count": 0, "text_sha256": None, "text_length": 0, "error_hits": [],
    }
    if not path.is_file():
        return result
    try:
        try:
            from pypdf import PdfReader
        except ImportError:  # compatibility for existing deployments; CI uses pypdf
            from PyPDF2 import PdfReader

        reader = PdfReader(str(path))
        pages = list(reader.pages)
        text = "\n\f\n".join((page.extract_text() or "") for page in pages)
        normalized = re.sub(r"\s+", " ", text).strip()
        hits = []
        for code, pattern in PDF_RENDER_ERROR_PATTERNS:
            for match in pattern.finditer(normalized):
                hits.append({"code": code, "text": match.group(0)[:240]})
        result.update(
            readable=True,
            page_count=len(pages),
            text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            text_length=len(text),
            error_hits=hits[:100],
        )
        result["layout"] = _pdf_layout_evidence(path)
    except Exception as exc:  # fail closed for absent/broken PDF dependencies or files
        result["error"] = str(exc)
    return result


def _render_policy_findings(pdf: dict[str, Any], spec: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    failures: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    layout = pdf.get("layout") if isinstance(pdf.get("layout"), dict) else {}
    pages = layout.get("pages") if isinstance(layout.get("pages"), list) else []
    page_rule = spec.get("page") if isinstance(spec.get("page"), dict) else {}

    expected_size = str(page_rule.get("size", "")).strip().lower().replace(" ", "")
    expected_dimensions = PAPER_SIZES_PT.get(expected_size)
    if expected_dimensions:
        tolerance_pt = 2.0
        mismatches = []
        for page in pages:
            actual = (float(page["width_pt"]), float(page["height_pt"]))
            portrait_expected = expected_dimensions
            landscape_expected = tuple(reversed(expected_dimensions))
            matches = any(
                abs(actual[0] - candidate[0]) <= tolerance_pt
                and abs(actual[1] - candidate[1]) <= tolerance_pt
                for candidate in (portrait_expected, landscape_expected)
            )
            if not matches:
                mismatches.append({"page": page["page"], "actual_pt": actual})
        if mismatches:
            failures.append({
                "code": "rendered_pdf_page_size_mismatch",
                "expected": page_rule.get("size"),
                "expected_portrait_pt": expected_dimensions,
                "evidence": mismatches,
            })

    expected_orientation = page_rule.get("orientation")
    if expected_orientation in {"portrait", "landscape"}:
        mismatches = [
            {"page": page["page"], "actual": page["orientation"]}
            for page in pages if page.get("orientation") != expected_orientation
        ]
        if mismatches:
            failures.append({
                "code": "rendered_pdf_page_orientation_mismatch",
                "expected": expected_orientation,
                "evidence": mismatches,
            })

    if layout.get("near_blank_page_candidates"):
        observations.append({
            "code": "rendered_pdf_near_blank_page_candidates",
            "pages": layout["near_blank_page_candidates"],
            "blocking": False,
        })
    if layout.get("duplicate_text_page_groups"):
        observations.append({
            "code": "rendered_pdf_duplicate_text_page_candidates",
            "page_groups": layout["duplicate_text_page_groups"],
            "blocking": False,
        })
    page_number_rule = page_rule.get("page_number") if isinstance(page_rule.get("page_number"), dict) else {}
    numbering_expected = any(
        page_number_rule.get(key) not in {None, "none"}
        for key in ("front_matter_format", "body_format")
    )
    if numbering_expected and pages and not layout.get("footer_page_number_candidates"):
        failures.append({
            "code": "rendered_pdf_no_footer_page_number_candidates",
            "message": "Page numbering is required, but no numeric/Roman footer token was extracted.",
        })
    numbering_runs = layout.get("page_numbering_runs") if isinstance(layout.get("page_numbering_runs"), list) else []
    candidate_counts = {
        page["page"]: len(page.get("footer_page_number_candidates", [])) for page in pages
    }
    numbered_pages = [page for page, count in candidate_counts.items() if count]
    front_numbered = page_number_rule.get("front_matter_format") not in {None, "none"}
    body_numbered = page_number_rule.get("body_format") not in {None, "none"}
    if numbering_expected and pages and numbered_pages:
        # Cover/declaration zones may intentionally precede the first numbered
        # front-matter page.  PDF geometry cannot map those pages back to DOCX
        # section selectors, so begin rendered coverage at the first observed
        # number rather than assuming physical page 1 is numbered.
        expected_start = min(numbered_pages)
        expected_end = len(pages) if body_numbered else max(numbered_pages)
        near_blank_pages = set(layout.get("near_blank_page_candidates") or [])
        unusable = [
            {"page": page, "candidate_count": candidate_counts.get(page, 0)}
            for page in range(expected_start, expected_end + 1)
            if candidate_counts.get(page, 0) > 1
            or (candidate_counts.get(page, 0) == 0 and page not in near_blank_pages)
        ]
        if unusable:
            failures.append({
                "code": "rendered_pdf_page_number_coverage_or_ambiguity",
                "expected_numbered_page_span": [expected_start, expected_end],
                "evidence": unusable,
            })
    discontinuous_runs = [run for run in numbering_runs if not run.get("continuous", True)]
    if numbering_expected and discontinuous_runs:
        failures.append({
            "code": "rendered_pdf_page_number_sequence_discontinuous",
            "evidence": discontinuous_runs,
        })
    expected_formats = {
        str(page_number_rule[key])
        for key in ("front_matter_format", "body_format")
        if page_number_rule.get(key) not in {None, "none"}
    }
    observed_formats = {str(run.get("format")) for run in numbering_runs if run.get("format")}
    missing_formats = sorted(expected_formats - observed_formats)
    if numbering_expected and missing_formats:
        failures.append({
            "code": "rendered_pdf_page_number_format_mismatch",
            "expected_formats": sorted(expected_formats),
            "observed_formats": sorted(observed_formats),
            "missing_formats": missing_formats,
        })
    expected_alignment = page_number_rule.get("alignment")
    if expected_alignment in {"left", "center", "right"}:
        misaligned = []
        for page in pages:
            for candidate in page.get("footer_page_number_candidates", []):
                if candidate.get("alignment_region") != expected_alignment:
                    misaligned.append({"page": page["page"], **candidate})
        if misaligned:
            failures.append({
                "code": "rendered_pdf_page_number_alignment_mismatch",
                "expected": expected_alignment,
                "evidence": misaligned,
            })

    roles = spec.get("roles") if isinstance(spec.get("roles"), dict) else {}
    header_rule = roles.get("header") if isinstance(roles.get("header"), dict) else {}
    header_content = header_rule.get("header_content") if isinstance(header_rule.get("header_content"), dict) else {}
    expected_left_header = str(header_content.get("left_text", "")).strip()
    degree_level = spec.get("thesis_profile", {}).get("degree_level")
    degree_text = {"doctor": "博士", "master": "硕士"}.get(degree_level)
    if degree_text:
        expected_left_header = re.sub(
            r"博士\s*/\s*硕士|硕士\s*/\s*博士", degree_text, expected_left_header
        )
    if expected_left_header and pages and not any(
        expected_left_header in str(page.get("top_text", "")) for page in pages
    ):
        failures.append({
            "code": "rendered_pdf_fixed_header_text_missing",
            "expected": expected_left_header,
            "evidence": layout.get("top_text_runs", []),
        })
    if header_content.get("right_field") == "styleref_heading_1":
        observations.append({
            "code": "rendered_pdf_dynamic_header_sequence",
            "top_text_runs": layout.get("top_text_runs", []),
            "blocking": False,
        })

    structure_rule = spec.get("document_structure") if isinstance(spec.get("document_structure"), dict) else {}
    toc_expected = isinstance(structure_rule.get("toc_depth"), int)
    if toc_expected and not layout.get("toc_candidate_pages"):
        failures.append({
            "code": "rendered_pdf_toc_page_missing",
            "expected_toc_depth": structure_rule.get("toc_depth"),
        })
    elif toc_expected and not layout.get("toc_entry_candidates"):
        failures.append({
            "code": "rendered_pdf_toc_entries_or_page_numbers_missing",
            "toc_candidate_pages": layout.get("toc_candidate_pages", []),
        })
    return failures, observations


def _render_summary(render_report: dict[str, Any] | None, docx_path: Path,
                    spec: dict[str, Any] | None = None) -> dict[str, Any]:
    if not render_report:
        return {
            "status": "not_run",
            "rendered_verified": False,
            "reason": "No accepted Microsoft Word/PDF render evidence was supplied.",
            "evidence": {},
        }
    failures: list[dict[str, Any]] = []
    if render_report.get("schema_version") != "1.0":
        failures.append({"code": "unsupported_render_report_schema", "actual": render_report.get("schema_version")})
    if render_report.get("evidence_type") != "microsoft_word_pdf_render":
        failures.append({"code": "invalid_render_evidence_type", "actual": render_report.get("evidence_type")})

    source = render_report.get("source_docx") if isinstance(render_report.get("source_docx"), dict) else {}
    expected_docx_hash = source.get("sha256")
    actual_docx_hash = _sha256(docx_path) if docx_path.is_file() else None
    if not expected_docx_hash or expected_docx_hash != actual_docx_hash:
        failures.append({
            "code": "source_docx_hash_mismatch",
            "expected": expected_docx_hash,
            "actual": actual_docx_hash,
        })

    rendered = render_report.get("rendered_pdf") if isinstance(render_report.get("rendered_pdf"), dict) else {}
    raw_pdf_path = rendered.get("path")
    pdf_path = Path(raw_pdf_path).expanduser() if isinstance(raw_pdf_path, str) and raw_pdf_path else None
    pdf = _pdf_evidence(pdf_path) if pdf_path else {
        "path": None, "exists": False, "readable": False, "page_count": 0,
        "text_sha256": None, "text_length": 0, "error_hits": [],
    }
    expected_pdf_hash = rendered.get("sha256")
    actual_pdf_hash = _sha256(pdf_path) if pdf_path and pdf_path.is_file() else None
    if not expected_pdf_hash or expected_pdf_hash != actual_pdf_hash:
        failures.append({
            "code": "rendered_pdf_hash_mismatch",
            "expected": expected_pdf_hash,
            "actual": actual_pdf_hash,
        })
    if not pdf.get("readable") or not pdf.get("page_count"):
        failures.append({"code": "rendered_pdf_unreadable_or_empty", "evidence": pdf})
    if pdf.get("text_length", 0) == 0:
        failures.append({"code": "rendered_pdf_has_no_extractable_text"})
    if pdf.get("error_hits"):
        failures.append({"code": "rendered_pdf_contains_word_errors", "evidence": pdf["error_hits"]})

    reported_pages = rendered.get("page_count")
    if reported_pages is not None and reported_pages != pdf.get("page_count"):
        failures.append({
            "code": "rendered_pdf_page_count_mismatch",
            "expected": reported_pages,
            "actual": pdf.get("page_count"),
        })
    reported_text_hash = rendered.get("text_sha256")
    if reported_text_hash is not None and reported_text_hash != pdf.get("text_sha256"):
        failures.append({
            "code": "rendered_pdf_text_hash_mismatch",
            "expected": reported_text_hash,
            "actual": pdf.get("text_sha256"),
        })

    renderer = render_report.get("renderer") if isinstance(render_report.get("renderer"), dict) else {}
    if renderer.get("name") != "Microsoft Word":
        failures.append({"code": "renderer_not_microsoft_word", "actual": renderer})
    word_export = render_report.get("word_export")
    required_word_summary = {"story_count", "field_count", "updated_count", "failed_count", "toc_count"}
    if (not isinstance(word_export, dict) or set(word_export) != required_word_summary
            or not all(isinstance(word_export[key], int) and word_export[key] >= 0 for key in required_word_summary)
            or word_export["failed_count"] != 0
            or word_export["updated_count"] != word_export["field_count"]):
        failures.append({"code": "word_export_summary_invalid", "actual": word_export})
    try:
        key = load_key(create=False)
    except (OSError, ValueError) as exc:
        key = None
        failures.append({"code": "word_export_attestation_key_invalid", "detail": str(exc)})
    if key is None:
        failures.append({"code": "word_export_attestation_key_unavailable"})
    elif not verify_attestation(render_report, key):
        failures.append({"code": "word_export_attestation_invalid_or_missing"})
    policy_failures, observations = _render_policy_findings(pdf, spec or {})
    failures.extend(policy_failures)
    passed = not failures
    return {
        "status": "passed" if passed else "failed",
        "rendered_verified": passed,
        "reason": ("DOCX/PDF hashes match, the local Word-export receipt is valid, and the independently parsed PDF contains no known Word field errors."
                   if passed else "Render evidence failed independent verification."),
        "evidence": {
            "source_docx_sha256": actual_docx_hash,
            "rendered_pdf": pdf,
            "renderer": renderer,
        },
        "observations": observations,
        "failures": failures,
    }


def audit_docx(docx_path: Path, spec: dict[str, Any] | None = None,
               render_report: dict[str, Any] | None = None,
               template_profile_path: Path | None = None,
               thesis_profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """Audit serialized evidence without relying on generator-side counters."""
    spec = spec or {}
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    submission_blockers: list[dict[str, Any]] = []
    names: set[str] = set()
    package_valid = False
    story_evidence: dict[str, Any] = {}
    field_counts: dict[str, int] = {}
    section_evidence: list[dict[str, Any]] = []
    all_text_parts: list[tuple[str, str]] = []
    effective_profile = thesis_profile if isinstance(thesis_profile, dict) else spec.get("thesis_profile")
    if isinstance(effective_profile, dict) and "metadata_status" in effective_profile:
        pending_fields = list(effective_profile.get("pending_fields") or [])
        if effective_profile.get("metadata_status") != "complete" or pending_fields:
            submission_blockers.append(_issue(
                "incomplete_thesis_profile", "critical",
                "Thesis metadata is pending; the artifact cannot be marked submission-ready until all profile fields are resolved.",
                {
                    "metadata_status": effective_profile.get("metadata_status"),
                    "pending_fields": pending_fields,
                },
                stage="submission_gate",
            ))

    try:
        with zipfile.ZipFile(docx_path) as zf:
            names = set(zf.namelist())
            bad_member = zf.testzip()
            package_valid = bad_member is None
            if bad_member:
                issues.append(_issue("opc_corrupt_member", "critical", "DOCX ZIP member failed CRC.", bad_member))
            required = {"[Content_Types].xml", "word/document.xml"}
            missing = sorted(required - names)
            if missing:
                issues.append(_issue("opc_missing_core_parts", "critical", "DOCX is missing core OPC parts.", missing))
                raise ValueError("missing core parts")

            document = etree.fromstring(zf.read("word/document.xml"))
            document_namespace = etree.QName(document).namespace
            if document_namespace != W_NS:
                issues.append(_issue(
                    "unsupported_wordprocessingml_namespace", "critical",
                    "Only Transitional WordprocessingML is currently audited; unsupported namespaces fail closed.",
                    {"namespace": document_namespace},
                ))
                raise ValueError(f"unsupported WordprocessingML namespace: {document_namespace}")
            document_rels = _relationship_map(zf, "word/document.xml")
            styles = etree.fromstring(zf.read("word/styles.xml")) if "word/styles.xml" in names else None
            settings = etree.fromstring(zf.read("word/settings.xml")) if "word/settings.xml" in names else None
            odd_even = bool(settings is not None and settings.xpath("./w:evenAndOddHeaders", namespaces=NS))

            referenced_parts: dict[str, set[str]] = {"header": set(), "footer": set()}
            sectprs = document.xpath("//w:sectPr", namespaces=NS)
            inherited_parts: dict[str, dict[str, str | None]] = {
                "header": {"default": None, "first": None, "even": None},
                "footer": {"default": None, "first": None, "even": None},
            }
            for index, sectpr in enumerate(sectprs, 1):
                current = {"section": index, "page_number_format": None, "page_number_start": None,
                           "headers": {}, "footers": {}}
                pg = sectpr.find(f"{W}pgNumType")
                if pg is not None:
                    current["page_number_format"] = pg.get(f"{W}fmt")
                    current["page_number_start"] = pg.get(f"{W}start")
                title_page = sectpr.find(f"{W}titlePg") is not None
                current["different_first_page"] = title_page
                current["different_odd_even"] = odd_even
                for kind in ("header", "footer"):
                    explicit_variants: set[str] = set()
                    for ref in sectpr.findall(f"{W}{kind}Reference"):
                        variant = ref.get(f"{W}type", "default")
                        explicit_variants.add(variant)
                        rid = ref.get(f"{R}id")
                        part = document_rels.get(rid or "")
                        current[f"{kind}s"][variant] = part
                        if not part or part not in names:
                            issues.append(_issue(
                                f"broken_{kind}_relationship", "critical",
                                f"Section {index} has an invalid {variant} {kind} relationship.",
                                {"relationship_id": rid, "part": part},
                            ))
                        else:
                            referenced_parts[kind].add(part)
                            inherited_parts[kind][variant] = part
                    for variant in ("default", "first", "even"):
                        if variant not in explicit_variants and inherited_parts[kind][variant]:
                            inherited = inherited_parts[kind][variant]
                            current[f"{kind}s"][variant] = inherited
                            current.setdefault(f"{kind}_inherited", {})[variant] = True
                            referenced_parts[kind].add(inherited)
                section_evidence.append(current)

            for kind, parts in referenced_parts.items():
                for part in sorted(parts):
                    root = etree.fromstring(zf.read(part))
                    instructions = _field_instructions(root)
                    fields: dict[str, list[str]] = {}
                    for instruction in instructions:
                        name = _field_name(instruction)
                        fields.setdefault(name or "UNKNOWN", []).append(instruction)
                        field_counts[name or "UNKNOWN"] = field_counts.get(name or "UNKNOWN", 0) + 1
                    paragraphs = _paragraph_texts(root)
                    story_evidence[part] = {
                        "kind": kind,
                        "visible_text": "\n".join(paragraphs),
                        "paragraph_count": len(paragraphs),
                        "field_instructions": instructions,
                        "fields": fields,
                    }
                    all_text_parts.append((part, "\n".join(paragraphs)))

            body_text = "\n".join(_paragraph_texts(document))
            all_text_parts.insert(0, ("word/document.xml", body_text))
            comments_count = 0
            comments_evidence: list[dict[str, Any]] = []
            for optional in ("word/footnotes.xml", "word/endnotes.xml", "word/comments.xml"):
                if optional in names:
                    root = etree.fromstring(zf.read(optional))
                    all_text_parts.append((optional, "\n".join(_paragraph_texts(root))))
                    if optional == "word/comments.xml":
                        comments = root.xpath("//w:comment", namespaces=NS)
                        comments_count = len(comments)
                        comments_evidence = [
                            {
                                "id": comment.get(f"{W}id"),
                                "author": comment.get(f"{W}author"),
                                "text": "".join(comment.xpath(".//w:t/text()", namespaces=NS))[:240],
                            }
                            for comment in comments[:20]
                        ]
            if comments_count:
                issues.append(_issue(
                    "unresolved_document_comments", "critical",
                    "The serialized DOCX still contains review comments; a submission artifact must remove them before final rendering.",
                    {"comment_count": comments_count, "comments": comments_evidence},
                ))

            all_visible = "\n".join(text for _, text in all_text_parts)

            body_paragraphs = _paragraph_texts(document)
            bibliography_headings = [
                text.strip() for text in body_paragraphs
                if text.strip().lower() in BIBLIOGRAPHY_HEADINGS
            ]
            raw_citation_keys = RAW_CITATION_KEY.findall(all_visible)
            if len(raw_citation_keys) >= 3:
                evidence = {
                    "hit_count": len(raw_citation_keys),
                    "distinct_count": len(set(raw_citation_keys)),
                    "sample": raw_citation_keys[:20],
                    "bibliography_headings": bibliography_headings,
                }
                issues.append(_issue(
                    "unresolved_raw_citation_keys", "critical",
                    "Serialized document contains unresolved citation keys; citeproc/bibliography processing was not completed.",
                    evidence,
                ))
                if not bibliography_headings:
                    issues.append(_issue(
                        "missing_bibliography_section", "critical",
                        "Document contains citation-key residue but no serialized bibliography heading.",
                        evidence,
                    ))

            for code, pattern in SOURCE_MARKERS:
                matches = []
                for part, text in all_text_parts:
                    for match in pattern.finditer(text):
                        matches.append({"part": part, "text": match.group(0)[:240]})
                        if len(matches) >= 20:
                            break
                    if len(matches) >= 20:
                        break
                if matches:
                    issues.append(_issue(
                        f"unresolved_source_marker_{code}", "critical",
                        "Serialized document contains unresolved source/cross-reference residue.",
                        matches,
                    ))

            for code, pattern in PLACEHOLDER_PATTERNS:
                matches = []
                for part, text in all_text_parts:
                    for match in pattern.finditer(text):
                        start = max(0, match.start() - 60)
                        end = min(len(text), match.end() + 60)
                        matches.append({"part": part, "text": text[start:end].replace("\n", " ")})
                        if len(matches) >= 20:
                            break
                    if len(matches) >= 20:
                        break
                if matches:
                    issues.append(_issue(
                        f"submission_placeholder_{code}", "critical",
                        "Submission document contains unconfirmed or template-placeholder content.",
                        matches,
                    ))
            page_rule = spec.get("page", {}).get("page_number", {}) if isinstance(spec.get("page"), dict) else {}
            numbering_expected = any(page_rule.get(key) not in {None, "none"}
                                     for key in ("front_matter_format", "body_format"))
            page_fields = field_counts.get("PAGE", 0)
            if numbering_expected and page_fields == 0:
                issues.append(_issue(
                    "missing_serialized_page_fields", "critical",
                    "Page numbering is required, but no serialized PAGE field exists in any referenced footer.",
                    {"referenced_footer_parts": sorted(referenced_parts["footer"]), "page_fields": 0},
                ))
            if numbering_expected:
                format_switch = {
                    "upperRoman": "ROMAN", "lowerRoman": "roman", "decimal": "ARABIC",
                    "roman_upper": "ROMAN", "roman": "roman",
                }
                per_section_failures = []
                configured_format = {"decimal": "decimal", "roman": "lowerRoman", "roman_upper": "upperRoman"}
                selector = page_rule.get("body_start_selector", {}) if isinstance(page_rule.get("body_start_selector"), dict) else {}
                strategy = selector.get("strategy")
                body_start = int(selector.get("section_index", 1)) if strategy == "section_index" else 1
                if strategy in {"first_heading_1", "heading_text"}:
                    current_section = 1
                    heading_pattern = selector.get("heading_text_pattern")
                    body = document.find(f"{W}body")
                    selector_matches = []
                    for child in body if body is not None else ():
                        if child.tag != f"{W}p":
                            continue
                        text = "".join(child.xpath(".//w:t/text()", namespaces=NS)).strip()
                        pstyle = child.find("./w:pPr/w:pStyle", namespaces=NS)
                        style_id = pstyle.get(f"{W}val", "") if pstyle is not None else ""
                        normalized_style_id = re.sub(r"[ _-]", "", style_id).lower()
                        # A cached table of contents can contain the same
                        # chapter text as the real heading (often including
                        # its cached page number).  It is not a body-start
                        # boundary.  Keep the text selector strict for real
                        # paragraphs while excluding the well-defined TOC
                        # paragraph styles from this structural match.
                        is_toc_style = (
                            normalized_style_id.startswith("toc")
                            or bool(re.fullmatch(r"目录\d+", normalized_style_id))
                        )
                        matched = (
                            strategy == "first_heading_1"
                            and re.sub(r"[ _-]", "", style_id).lower() in {"heading1", "标题1"}
                        ) or (
                            strategy == "heading_text"
                            and bool(heading_pattern)
                            and not is_toc_style
                            and bool(re.search(str(heading_pattern), text))
                        )
                        if matched:
                            selector_matches.append({
                                "section": current_section,
                                "text": text[:240],
                                "style_id": style_id,
                            })
                        if child.find("./w:pPr/w:sectPr", namespaces=NS) is not None:
                            current_section += 1
                    if len(selector_matches) == 1:
                        body_start = selector_matches[0]["section"]
                    elif not selector_matches:
                        issues.append(_issue(
                            "missing_body_start_selector_match", "critical",
                            "The configured body-start selector matched no paragraph in the serialized DOCX.",
                            {"selector": selector, "matches": []},
                        ))
                    else:
                        issues.append(_issue(
                            "ambiguous_body_start_selector", "critical",
                            "The configured body-start selector matched multiple paragraphs; page numbering cannot be assigned safely.",
                            {"selector": selector, "matches": selector_matches[:20]},
                        ))
                # A thesis can intentionally leave title/declaration sections
                # unnumbered and begin Roman numbering at the abstract.  The
                # compact format spec records the front numbering start but has
                # no separate front-start selector.  Treat leading sections as
                # intentionally unnumbered only when a later front section
                # provides strong serialized evidence: an explicit restart at
                # the configured start value and a PAGE field with the required
                # switch.  This does not excuse gaps after numbering begins.
                first_numbered_front_section = 1
                expected_front = configured_format.get(
                    str(page_rule.get("front_matter_format")),
                    page_rule.get("front_matter_format"),
                )
                expected_front_switch = format_switch.get(str(expected_front))
                configured_front_start = str(page_rule.get("front_matter_start", 1))
                for candidate in section_evidence:
                    if candidate["section"] >= body_start:
                        break
                    default_part = candidate.get("footers", {}).get("default")
                    instructions = story_evidence.get(default_part or "", {}).get("fields", {}).get("PAGE", [])
                    switch_ok = bool(instructions) and (
                        expected_front_switch is None
                        or any(re.search(
                            rf"\\\*\s+{re.escape(expected_front_switch)}(?:\s|$)", value,
                            0 if expected_front_switch == "roman" else re.I,
                        ) for value in instructions)
                    )
                    if candidate.get("page_number_start") == configured_front_start and switch_ok:
                        first_numbered_front_section = candidate["section"]
                        break
                for section in section_evidence:
                    configured = (page_rule.get("front_matter_format") if section["section"] < body_start
                                  else page_rule.get("body_format"))
                    expected_format = configured_format.get(str(configured), configured)
                    actual_section_format = section.get("page_number_format")
                    if expected_format in {None, "none"}:
                        continue
                    if section["section"] < first_numbered_front_section:
                        continue
                    # Word may omit w:fmt after saving when the PAGE field
                    # switch/default continuation already determines the same
                    # format.  Only an explicit contradictory value is a
                    # serialized conflict; field coverage is checked below.
                    if actual_section_format is not None and actual_section_format != expected_format:
                        per_section_failures.append({
                            "section": section["section"], "variant": "section_properties", "footer_part": None,
                            "section_page_format": actual_section_format,
                            "expected_section_page_format": expected_format,
                            "expected_field_switch": format_switch.get(str(expected_format)),
                            "actual_page_instructions": [],
                        })
                    # The default story renders on ordinary pages and must carry a
                    # PAGE field.  First/even stories are additionally required only
                    # when their section/settings variants are active.
                    required_variants = ["default"]
                    if section.get("different_first_page"):
                        required_variants.append("first")
                    if section.get("different_odd_even"):
                        required_variants.append("even")
                    for variant in required_variants:
                        part = section["footers"].get(variant)
                        evidence = story_evidence.get(part or "", {})
                        instructions = evidence.get("fields", {}).get("PAGE", [])
                        expected_switch = format_switch.get(str(expected_format))
                        format_ok = bool(instructions) and (
                            expected_switch is None
                            or any(re.search(rf"\\\*\s+{re.escape(expected_switch)}(?:\s|$)", value,
                                             0 if expected_switch == "roman" else re.I)
                                   for value in instructions)
                        )
                        if not instructions or not format_ok:
                            per_section_failures.append({
                                "section": section["section"], "variant": variant, "footer_part": part,
                                "section_page_format": actual_section_format,
                                "expected_section_page_format": expected_format,
                                "expected_field_switch": expected_switch,
                                "actual_page_instructions": instructions,
                            })
                if per_section_failures:
                    issues.append(_issue(
                        "page_field_section_coverage_or_format_mismatch", "critical",
                        "One or more numbered sections lack an active PAGE field or use a field switch inconsistent with the section page-number format.",
                        per_section_failures,
                    ))
            if page_fields:
                centered = 0
                style_alignment: dict[str, str | None] = {}
                if styles is not None:
                    style_nodes = {
                        node.get(f"{W}styleId", ""): node
                        for node in styles.xpath(".//w:style[@w:type='paragraph']", namespaces=NS)
                    }

                    def resolved_style_alignment(style_id: str, trail: set[str] | None = None) -> str | None:
                        if style_id in style_alignment:
                            return style_alignment[style_id]
                        trail = set() if trail is None else trail
                        if not style_id or style_id in trail or style_id not in style_nodes:
                            return None
                        trail.add(style_id)
                        node = style_nodes[style_id]
                        jc = node.find("./w:pPr/w:jc", namespaces=NS)
                        value = jc.get(f"{W}val") if jc is not None else None
                        if value is None:
                            based = node.find("./w:basedOn", namespaces=NS)
                            value = resolved_style_alignment(
                                based.get(f"{W}val", "") if based is not None else "", trail
                            )
                        style_alignment[style_id] = value
                        return value

                for part in referenced_parts["footer"]:
                    root = etree.fromstring(zf.read(part))
                    for paragraph in root.xpath(".//w:p[.//w:fldSimple[contains(translate(@w:instr,'page','PAGE'),'PAGE')] or .//w:instrText[contains(translate(text(),'page','PAGE'),'PAGE')]]", namespaces=NS):
                        jc = paragraph.find("./w:pPr/w:jc", namespaces=NS)
                        alignment = jc.get(f"{W}val") if jc is not None else None
                        if alignment is None:
                            pstyle = paragraph.find("./w:pPr/w:pStyle", namespaces=NS)
                            alignment = resolved_style_alignment(
                                pstyle.get(f"{W}val", "") if pstyle is not None else ""
                            )
                        if alignment == "center":
                            centered += 1
                if page_rule.get("alignment") == "center" and centered < page_fields:
                    issues.append(_issue(
                        "page_fields_not_centered", "high",
                        "Not every serialized PAGE field is in a centered footer paragraph.",
                        {"page_fields": page_fields, "centered_page_field_paragraphs": centered},
                    ))

            header_role = spec.get("roles", {}).get("header", {}) if isinstance(spec.get("roles"), dict) else {}
            content = header_role.get("header_content", {}) if isinstance(header_role, dict) else {}
            styleref_expected = content.get("right_field") == "styleref_heading_1"
            styleref_fields = field_counts.get("STYLEREF", 0)
            literal_heading_placeholder = any(
                re.search(r"(?:^|\s|论文)Heading\s*1(?:\s|$)", evidence["visible_text"], re.I)
                for evidence in story_evidence.values() if evidence["kind"] == "header"
            )
            if styleref_expected and styleref_fields == 0:
                issues.append(_issue(
                    "missing_serialized_styleref", "critical",
                    "A dynamic chapter header is required, but no serialized STYLEREF field exists.",
                    {"referenced_header_parts": sorted(referenced_parts["header"])},
                ))
            if literal_heading_placeholder and styleref_fields == 0:
                issues.append(_issue(
                    "literal_heading_style_placeholder", "critical",
                    "A header contains literal 'Heading 1' text instead of a dynamic STYLEREF field.",
                    [part for part, evidence in story_evidence.items()
                     if evidence["kind"] == "header" and "Heading 1" in evidence["visible_text"]],
                ))
            generic_degree = [part for part, evidence in story_evidence.items()
                              if evidence["kind"] == "header" and re.search(r"博士\s*/\s*硕士|硕士\s*/\s*博士", evidence["visible_text"])]
            if generic_degree:
                issues.append(_issue(
                    "unresolved_degree_header_placeholder", "high",
                    "Header still contains a generic doctoral/master degree placeholder.", generic_degree,
                ))

            for marker_name, pattern in SOURCE_MARKERS:
                hits = []
                for part, text in all_text_parts:
                    for match in pattern.finditer(text):
                        hits.append({"part": part, "text": text[max(0, match.start()-30):match.end()+70]})
                if hits:
                    issues.append(_issue(
                        f"unresolved_source_marker_{marker_name}", "critical",
                        f"Unresolved source-format marker detected: {marker_name}.", hits[:50],
                    ))
            for error_name, pattern in RENDER_ERROR_PATTERNS:
                hits = [{"part": part, "text": match.group(0)} for part, text in all_text_parts for match in pattern.finditer(text)]
                if hits:
                    issues.append(_issue(error_name, "critical", "Word reference error text is visible in the artifact.", hits[:50]))
            equation_hits = [
                {"part": part, "text": text[max(0, match.start()-20):match.end()+40]}
                for part, text in all_text_parts for match in DUPLICATE_EQUATION_PREFIX.finditer(text)
            ]
            if equation_hits:
                issues.append(_issue(
                    "duplicate_equation_reference_prefix", "high",
                    "Duplicated equation-reference prefix detected (for example, '式 式（…）').", equation_hits,
                ))

            heading_1_style_ids: set[str] = set()
            if styles is not None:
                for style in styles.xpath(".//w:style[@w:type='paragraph']", namespaces=NS):
                    style_id = style.get(f"{W}styleId", "")
                    name = style.find("./w:name", namespaces=NS)
                    display = name.get(f"{W}val", "") if name is not None else ""
                    # Word commonly localizes or renumbers built-in style IDs
                    # (for example styleId="1", display name="heading 1").
                    # Match the semantic display name as well as the ID, but do
                    # not let TOC 1/toc 1 paragraphs masquerade as chapter
                    # headings merely because their cached text starts with
                    # "第一章".
                    if (re.fullmatch(r"Heading\s*1", style_id, re.I)
                            or re.fullmatch(r"heading\s*1|标题\s*1", display, re.I)):
                        heading_1_style_ids.add(style_id)
            styled_chapters: list[tuple[int, str, int]] = []
            unstyled_plain_chapters: list[tuple[int, str, int]] = []
            body_paragraph_nodes = document.xpath(".//w:body//w:p", namespaces=NS)
            for paragraph_index, paragraph in enumerate(body_paragraph_nodes):
                pstyle = paragraph.find("./w:pPr/w:pStyle", namespaces=NS)
                style_id = pstyle.get(f"{W}val", "") if pstyle is not None else ""
                text = _paragraph_main_text(paragraph).strip()
                match = CHAPTER_HEADING.match(text)
                if not match:
                    continue
                number = _number(match.group(1) or match.group(2))
                if number is None:
                    continue
                if style_id in heading_1_style_ids:
                    styled_chapters.append((paragraph_index, text, number))
                elif not style_id and match.group(2):
                    # Some assembly paths retain a top-level chapter's visible
                    # numbering but lose only its Heading 1 style.  Recover a
                    # missing leading chapter conservatively: plain-number
                    # syntax only (so 1.1 cannot match), no paragraph style,
                    # and only when later styled chapter evidence establishes
                    # the same sequence.
                    unstyled_plain_chapters.append((paragraph_index, text, number))
            chapters = list(styled_chapters)
            if styled_chapters:
                first_styled_number = min(item[2] for item in styled_chapters)
                for candidate in unstyled_plain_chapters:
                    if candidate[2] < first_styled_number:
                        chapters.append(candidate)
            chapters.sort(key=lambda item: item[0])
            chapter_indexes = [item[0] for item in chapters]
            numeric_chapters = [item[2] for item in chapters]
            if numeric_chapters:
                expected = list(range(1, max(numeric_chapters) + 1))
                if numeric_chapters != expected:
                    issues.append(_issue(
                        "non_contiguous_chapter_numbering", "high",
                        "Chapter numbering is not contiguous from Chapter 1.",
                        {"actual": numeric_chapters, "expected": expected},
                    ))
            claims = []
            paragraphs = _paragraph_texts(document)
            for i, text in enumerate(paragraphs):
                for match in CHAPTER_CLAIM.finditer(text):
                    claims.append({"paragraph": i, "claimed": _number(match.group(1)), "text": text[:240]})
            for claim in claims:
                if claim["claimed"] is not None and claim["claimed"] != len(numeric_chapters):
                    issues.append(_issue(
                        "chapter_count_claim_mismatch", "critical",
                        "Narrative chapter count does not match actual numbered chapter headings.",
                        {"actual_chapter_count": len(numeric_chapters), **claim},
                    ))

            reference_positions = [i for i, text in enumerate(paragraphs)
                                   if re.fullmatch(r"\s*参考文献\s*", text)]
            conclusion_positions = [i for i, text in enumerate(paragraphs)
                                    if re.search(r"结论|讨论|结论与展望|总结与展望", text)
                                    and (CHAPTER_HEADING.match(text) or len(text.strip()) < 30)]
            structure_rule = spec.get("document_structure", {}) if isinstance(spec.get("document_structure"), dict) else {}
            ordered_roles = structure_rule.get("ordered_roles", [])
            explicit_reference_after_conclusion = (
                "references" in ordered_roles and any(role in ordered_roles for role in ("conclusion", "discussion"))
            )
            if explicit_reference_after_conclusion and reference_positions and conclusion_positions:
                if min(reference_positions) < max(conclusion_positions):
                    issues.append(_issue(
                        "references_before_conclusion", "critical",
                        "References appear before the conclusion/discussion required by the format specification.",
                        {"reference_positions": reference_positions, "conclusion_positions": conclusion_positions},
                    ))
            elif reference_positions and chapter_indexes:
                last_chapter = max(chapter_indexes)
                if any(position > last_chapter and position < len(paragraphs) for position in reference_positions):
                    # This is only evidence, not a universal school rule.  A school-specific
                    # ordering rule can promote it to a blocker.
                    warnings.append(_issue(
                        "references_order_requires_policy", "warning",
                        "Reference placement was detected but no normalized ordering policy proves whether it is valid.",
                        {"reference_positions": reference_positions},
                    ))

            toc_fields = field_counts.get("TOC", 0)
            if toc_fields == 0:
                # TOC usually lives in document.xml rather than a story part.
                body_instructions = _field_instructions(document)
                toc_fields = sum(1 for value in body_instructions if _field_name(value) == "TOC")
                field_counts["TOC"] = toc_fields
            toc_style_entries = sum(1 for p in document.xpath("//w:p", namespaces=NS)
                                    if any((node.get(f"{W}val") or "").upper().startswith("TOC")
                                           for node in p.xpath("./w:pPr/w:pStyle", namespaces=NS)))
            if toc_fields and toc_style_entries == 0:
                warnings.append(_issue(
                    "toc_cache_not_visible", "render_required",
                    "TOC field exists, but no cached TOC entry paragraphs are serialized; Word/PDF render verification is required.",
                    {"toc_fields": toc_fields, "cached_toc_entries": 0},
                    stage="rendered_output",
                ))

    except (zipfile.BadZipFile, etree.XMLSyntaxError, ValueError) as exc:
        issues.append(_issue("docx_parse_failed", "critical", "Serialized DOCX could not be audited.", str(exc)))

    render = _render_summary(render_report, docx_path, spec)
    serialized_verified = package_valid and not issues
    template_validation: dict[str, Any] = {
        "status": "not_requested", "official_template_structure_valid": None,
        "manual_required_fields": [], "failures": [],
    }
    if template_profile_path is not None:
        from template_audit import audit_template
        layout = render.get("evidence", {}).get("rendered_pdf", {}).get("layout")
        template_validation = audit_template(
            docx_path, template_profile_path, thesis_profile=thesis_profile, render_layout=layout
        )
        template_validation["status"] = (
            "passed" if template_validation["official_template_structure_valid"] else "failed"
        )
    template_ok = template_validation["official_template_structure_valid"] is not False
    submission_ready = (
        serialized_verified
        and render["rendered_verified"]
        and template_ok
        and not submission_blockers
    )
    return {
        "schema_version": "1.0",
        "artifact": str(docx_path.resolve()),
        "pipeline_valid": None,
        "supported_subset_valid": None,
        "serialized_docx_valid": serialized_verified,
        "render_validation": render,
        "template_validation": template_validation,
        "submission_ready": submission_ready,
        "status": "submission_ready" if submission_ready else "not_submission_ready",
        "issues": issues,
        "warnings": warnings,
        "submission_blockers": submission_blockers,
        "evidence": {
            "opc_package_valid": package_valid,
            "section_count": len(section_evidence),
            "sections": section_evidence,
            "field_counts": dict(sorted(field_counts.items())),
            "story_parts": story_evidence,
        },
        "policy": {
            "fail_closed": True,
            "render_evidence_required_for_submission_ready": True,
            "generator_counters_are_not_accepted_as_serialized_evidence": True,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("docx", type=Path)
    parser.add_argument("--format-spec", type=Path)
    parser.add_argument("--render-report", type=Path)
    parser.add_argument("--template-profile", type=Path,
                        help="official template profile; when supplied, submission readiness requires it to pass")
    parser.add_argument("--thesis-profile", type=Path,
                        help="trusted thesis metadata used by the template profile")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    spec = _json(args.format_spec)
    if args.format_spec:
        schema_path = Path(__file__).resolve().parents[1] / "schema" / "format-spec.schema.json"
        schema_errors = load_and_validate(spec, schema_path)
        if schema_errors:
            parser.error("invalid format spec:\n" + "\n".join(f"- {error}" for error in schema_errors))
    resolved = [args.docx.expanduser().resolve()]
    if args.format_spec:
        resolved.append(args.format_spec.expanduser().resolve())
    if args.render_report:
        resolved.append(args.render_report.expanduser().resolve())
    if args.template_profile:
        resolved.append(args.template_profile.expanduser().resolve())
    if args.thesis_profile:
        resolved.append(args.thesis_profile.expanduser().resolve())
    if args.out:
        output = args.out.expanduser().resolve()
        if paths_alias((*resolved, output)):
            parser.error("audit output must differ from all audit inputs")
    else:
        output = None
    result = audit_docx(
        args.docx, spec, _json(args.render_report) if args.render_report else None,
        template_profile_path=args.template_profile.resolve() if args.template_profile else None,
        thesis_profile=_json(args.thesis_profile) if args.thesis_profile else None,
    )
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(output, payload)
    print(payload, end="")
    return 0 if result["submission_ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
