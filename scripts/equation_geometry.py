"""Conservative, rendered OMML layout checks. Never edit the caller's math.

Measurements are made on private DOCX copies with the source styles/settings:
one wide math page, an optional wide label page, and the proposed paragraph in
its actual text measure. The last page verifies automatic line wrapping as
well as the placement predicted from the independent object measurements.
Unavailable renderers, incomplete glyph extraction, or ambiguous ink fail
closed. These observations are LibreOffice evidence, never Word attestation.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import shutil
import tempfile
import unicodedata
from collections import Counter, OrderedDict
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from lxml import etree

from process_runner import run_process

_CACHE: OrderedDict[str, tuple[dict, bytes, bytes | None]] = OrderedDict()
_PROBE_PAGE = 31680  # A bounded probe; touching its limits is never accepted.
_MARGIN = 720
_TOLERANCE = 2.0  # points: glyph side bearings / bracket ink vs layout box


def _el(tag, **attrs):
    node = OxmlElement(tag)
    for name, value in attrs.items():
        node.set(qn("w:" + name), str(value))
    return node


def _normal(text):
    return "".join(c for c in unicodedata.normalize("NFKC", text).replace("−", "-")
                   if not c.isspace())


def _complete(expected, actual):
    return not (Counter(_normal(expected)) - Counter(_normal(actual)))


def has_math_ink(paragraph):
    return bool(any(t.strip() for t in paragraph._p.xpath(".//m:t/text()")) or
                paragraph._p.xpath(".//m:f|.//m:nary|.//m:d|.//m:rad|.//m:m|.//m:eqArr"))


def _section(width, height):
    section = _el("w:sectPr")
    section.append(_el("w:type", val="nextPage"))
    section.append(_el("w:pgSz", w=width, h=height))
    section.append(_el("w:pgMar", top=_MARGIN, right=_MARGIN, bottom=_MARGIN,
                       left=_MARGIN, header=0, footer=0, gutter=0))
    section.append(_el("w:cols", num=1, space=0))
    return section


def _probe_paragraph(source, *, wide=False):
    node = copy.deepcopy(source)
    ppr = node.find(qn("w:pPr"))
    if ppr is None:
        ppr = _el("w:pPr"); node.insert(0, ppr)
    for tag in ("sectPr", "pageBreakBefore", "pBdr", "shd"):
        for child in ppr.findall(qn("w:" + tag)):
            ppr.remove(child)
    if wide:
        for tag in ("tabs", "ind", "jc", "spacing"):
            for child in ppr.findall(qn("w:" + tag)):
                ppr.remove(child)
        ppr.extend([_el("w:tabs"), _el("w:ind", left=0, right=0, firstLine=0),
                    _el("w:jc", val="left"), _el("w:spacing", before=0, after=0, line=240, lineRule="auto")])
    return node


def _probe_bytes(doc, paragraph, width, height, label):
    stream = io.BytesIO(); doc.save(stream)
    with ZipFile(stream) as package:
        parts = {name: package.read(name) for name in package.namelist()}
    root = etree.fromstring(parts["word/document.xml"])
    body = root.find(qn("w:body")); body.clear()
    source = paragraph._p
    maths = source.xpath("./m:oMathPara|./m:oMath")
    if len(maths) != 1:
        raise ValueError("math object is not uniquely addressable")
    math_page = _probe_paragraph(source, wide=True)
    for child in list(math_page):
        if child.tag != qn("w:pPr"):
            math_page.remove(child)
    math_page.append(copy.deepcopy(maths[0]))
    body.append(math_page)
    last_wide = math_page
    used_colors = set(source.xpath(".//w:color/@w:val"))
    sentinel = next(value for value in ("13A5C7", "A517C3", "37B51D") if value not in used_colors)
    if label:
        label_page = _probe_paragraph(source, wide=True)
        for child in list(label_page):
            if child.tag != qn("w:pPr"):
                label_page.remove(child)
        seen_math = False; prefix = True
        for child in source:
            if child is maths[0]:
                seen_math = True; continue
            if not seen_math or child.tag != qn("w:r"):
                continue
            run = copy.deepcopy(child)
            for item in list(run):
                if item.tag == qn("w:tab"):
                    run.remove(item)
                elif item.tag == qn("w:t") and prefix:
                    item.text = (item.text or "").lstrip()
                    if item.text: prefix = False
            label_page.append(run)
        label_page.find(qn("w:pPr")).append(_el("w:pageBreakBefore"))
        body.append(label_page); last_wide = label_page
    last_wide.find(qn("w:pPr")).append(_section(_PROBE_PAGE, _PROBE_PAGE))
    context = _probe_paragraph(source)
    for run in context.findall(qn("w:r")):
        rpr = run.find(qn("w:rPr"))
        if rpr is None: rpr = _el("w:rPr"); run.insert(0, rpr)
        color = rpr.find(qn("w:color"))
        if color is None: color = _el("w:color"); rpr.append(color)
        color.attrib.clear(); color.set(qn("w:val"), sentinel)
    body.append(context)
    body.append(_section(width + 2 * _MARGIN, height + 2 * _MARGIN))
    parts["word/document.xml"] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    result = io.BytesIO()
    with ZipFile(result, "w", compression=ZIP_DEFLATED) as package:
        # Stable member timestamps make the in-process cache content-based.
        from zipfile import ZipInfo
        for name, data in sorted(parts.items()):
            info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0)); info.compress_type = ZIP_DEFLATED
            package.writestr(info, data)
    return result.getvalue(), int(sentinel, 16)


def _spans(page):
    return [s for block in page.get_text("dict")["blocks"]
            for line in block.get("lines", []) for s in line.get("spans", [])]


def _box(rects):
    import fitz
    if not rects: return None
    box = fitz.Rect(rects[0])
    for rect in rects[1:]: box |= fitz.Rect(rect)
    return box


def _render(probe):
    import fitz
    soffice = shutil.which("soffice")
    if not soffice:
        raise RuntimeError("LibreOffice is unavailable; equation geometry requires manual review")
    with tempfile.TemporaryDirectory(prefix="thesis-equation-probe-") as directory:
        root = Path(directory); path = root / "probe.docx"; path.write_bytes(probe)
        result = run_process([soffice, "--headless", "--norestore", "--nodefault",
                              "-env:UserInstallation=" + (root / "profile").as_uri(),
                              "--convert-to", "pdf", "--outdir", str(root), str(path)],
                             cwd=root, timeout=60)
        pdf = root / "probe.pdf"
        if result.returncode or not pdf.is_file():
            raise RuntimeError(f"equation probe render failed (exit {result.returncode})")
        data = pdf.read_bytes()
        # Opening here also rejects malformed/empty renderer output.
        with fitz.open(stream=data, filetype="pdf") as rendered:
            if not len(rendered): raise RuntimeError("empty equation probe PDF")
        return data


def _renderer_identity():
    path = shutil.which("soffice")
    if not path:
        raise RuntimeError("LibreOffice is unavailable; equation geometry requires manual review")
    stat = Path(path).stat()
    return {"path": str(Path(path).resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def verify_equation(doc, paragraph, *, width, height, left, right, center,
                    label, alignment="center", evidence_dir=None):
    """Return a source-bound rendered safety observation; unknown is blocking."""
    if not has_math_ink(paragraph):
        return {"status": "empty_placeholder", "findings": [], "probe_geometry_verified": False}
    failure = lambda code, **facts: {"code": code, "requires_manual_review": True, **facts}
    invalid = []
    if width <= 0 or height <= 0 or left < 0 or right <= left or right > width:
        invalid.append(failure("equation_text_measure_unresolved"))
    if width + 2 * _MARGIN > _PROBE_PAGE or height + 2 * _MARGIN > _PROBE_PAGE:
        invalid.append(failure("equation_probe_measure_out_of_range"))
    if invalid: return {"status": "manual_review_required", "findings": invalid, "probe_geometry_verified": False}
    probe = pdf = None
    try:
        import fitz
        renderer = _renderer_identity()
        probe, sentinel = _probe_bytes(doc, paragraph, width, height, label)
        parameters = json.dumps([width, height, left, right, center, label, alignment, renderer]).encode()
        key = hashlib.sha256(probe + parameters).hexdigest()
        if key in _CACHE:
            result, _, pdf = _CACHE[key]; _CACHE.move_to_end(key)
        else:
            pdf = _render(probe)
            rendered = fitz.open(stream=pdf, filetype="pdf")
            expected_pages = 3 if label else 2
            findings = []
            if len(rendered) != expected_pages:
                findings.append(failure("equation_probe_pagination_unresolved", actual_pages=len(rendered)))
            else:
                expected_text = "".join(paragraph._p.xpath(".//m:t/text()"))
                wide = rendered[0]
                math_box = _box([bbox for kind, bbox in wide.get_bboxlog() if kind != "ignore-text"])
                label_box = _box([bbox for kind, bbox in rendered[1].get_bboxlog() if kind != "ignore-text"]) if label else None
                if math_box is None or not _complete(expected_text, wide.get_text()):
                    findings.append(failure("equation_probe_glyphs_incomplete"))
                elif math_box.x0 < 2 or math_box.y0 < 2 or math_box.x1 > wide.rect.width - 2 or math_box.y1 > wide.rect.height - 2:
                    findings.append(failure("equation_probe_clipped"))
                if label and (label_box is None or not _complete(label, rendered[1].get_text())):
                    findings.append(failure("equation_label_probe_incomplete"))
                if math_box is not None:
                    if math_box.width > (right - left) / 20 + _TOLERANCE:
                        findings.append(failure("equation_too_wide", math_width_pt=math_box.width, available_pt=(right-left)/20))
                    if math_box.height > height / 20 + _TOLERANCE:
                        findings.append(failure("equation_too_tall", math_height_pt=math_box.height, available_pt=height/20))
                    if label_box is not None and alignment == "center":
                        gap = max(1.0, label_box.height / 2)
                        if math_box.width / 2 + label_box.width + gap > (right - center) / 20:
                            findings.append(failure("equation_number_has_no_clearance", math_width_pt=math_box.width,
                                                    label_width_pt=label_box.width, minimum_gap_pt=gap))
                context = rendered[-1]; spans = _spans(context)
                labels = [s for s in spans if s["color"] == sentinel and s["text"].strip()]
                math_spans = [s for s in spans if s["color"] != sentinel]
                labels_box = _box([s["bbox"] for s in labels])
                paths = [d["rect"] for d in context.get_drawings()
                         if labels_box is None or not d["rect"].intersects(labels_box)]
                actual_math = _box([s["bbox"] for s in math_spans] + paths)
                if actual_math is None or not _complete(expected_text, "".join(s["text"] for s in math_spans)):
                    findings.append(failure("equation_rendered_glyphs_incomplete"))
                else:
                    origin = _MARGIN / 20
                    if actual_math.x0 < origin + left/20 - _TOLERANCE or actual_math.x1 > origin + right/20 + _TOLERANCE:
                        findings.append(failure("equation_rendered_outside_text_area", bbox=list(actual_math)))
                    if actual_math.y0 < origin - _TOLERANCE or actual_math.y1 > origin + height/20 + _TOLERANCE:
                        findings.append(failure("equation_rendered_outside_page_height", bbox=list(actual_math)))
                    if alignment == "center" and abs((actual_math.x0+actual_math.x1)/2 - origin - center/20) > _TOLERANCE:
                        findings.append(failure("equation_rendered_not_centered", bbox=list(actual_math), required_center_pt=origin+center/20))
                    if alignment == "left" and abs(actual_math.x0-origin-left/20) > _TOLERANCE:
                        findings.append(failure("equation_rendered_not_left_aligned", bbox=list(actual_math)))
                    if alignment == "right" and abs(actual_math.x1-origin-right/20) > _TOLERANCE:
                        findings.append(failure("equation_rendered_not_right_aligned", bbox=list(actual_math)))
                if label:
                    if labels_box is None or not _complete(label, "".join(s["text"] for s in labels)):
                        findings.append(failure("equation_rendered_number_missing"))
                    elif actual_math is not None:
                        if abs(labels_box.x1 - (_MARGIN + right)/20) > _TOLERANCE:
                            findings.append(failure("equation_rendered_number_not_right_aligned", bbox=list(labels_box)))
                        if (labels_box.y0+labels_box.y1)/2 < actual_math.y0-_TOLERANCE or (labels_box.y0+labels_box.y1)/2 > actual_math.y1+_TOLERANCE:
                            findings.append(failure("equation_rendered_number_not_same_line", math_bbox=list(actual_math), label_bbox=list(labels_box)))
                        if actual_math.intersects(labels_box):
                            findings.append(failure("equation_rendered_number_collision"))
                result = {"status": "passed" if not findings else "manual_review_required", "findings": findings,
                          "probe_geometry_verified": not findings, "math_width_pt": math_box.width if math_box else None,
                          "math_height_pt": math_box.height if math_box else None,
                          "label_width_pt": label_box.width if label_box else None,
                          "label_height_pt": label_box.height if label_box else None,
                          "context_math_bbox": list(actual_math) if actual_math else None,
                          "context_label_bbox": list(labels_box) if labels_box else None}
            if len(rendered) != expected_pages:
                result = {"status": "manual_review_required", "findings": findings, "probe_geometry_verified": False}
            result.update(probe_docx_sha256=hashlib.sha256(probe).hexdigest(), validation_key=key,
                          probe_pdf_sha256=hashlib.sha256(pdf).hexdigest(),
                          renderer="LibreOffice", renderer_identity=renderer,
                          verification_scope="isolated_formula_in_same_text_measure",
                          whole_document_visual_review=False, word_wps_verified=False)
            _CACHE[key] = (result, probe, pdf)
            if len(_CACHE) > 128: _CACHE.popitem(last=False)
        if evidence_dir is not None:
            directory = Path(evidence_dir) / key; directory.mkdir(parents=True, exist_ok=True)
            (directory / "probe.docx").write_bytes(probe)
            (directory / "probe.pdf").write_bytes(pdf)
            (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        return copy.deepcopy(result)
    except (ImportError, RuntimeError, ValueError, OSError, StopIteration) as exc:
        return {"status": "manual_review_required", "probe_geometry_verified": False,
                "findings": [failure("equation_geometry_unavailable", reason=str(exc))]}
