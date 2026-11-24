#!/usr/bin/env python3
"""Independently audit rendered PDF fonts and inline drawing visibility.

This report is observational. It never claims that Word/LibreOffice refreshed
fields or accepts a review draft for submission.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import posixpath
import re
import sys
from pathlib import Path
from typing import Any
from zipfile import ZipFile

from lxml import etree
from PIL import Image
from PIL import ImageOps

from semantic_contract import sha256_json, strict_json_read
from pdf_glyph_integrity import audit_pdf_glyph_integrity
from drawing_line_box import effective_line_spacing, inline_drawing_geometry, line_box_assessment

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PR = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"w": W, "wp": WP, "a": A, "r": R, "pr": PR,
      "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture"}
EMU_PER_PT = 12700.0


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _resolve_target(source_part: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(source_part), target))


def _styles(zf: ZipFile) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    root = etree.fromstring(zf.read("word/styles.xml"))
    styles: dict[str, dict[str, Any]] = {}
    names: dict[str, str] = {}
    for node in root.xpath(".//w:style", namespaces=NS):
        sid = node.get(f"{{{W}}}styleId")
        if not sid:
            continue
        name = node.find("w:name", namespaces=NS)
        names[sid] = name.get(f"{{{W}}}val", sid) if name is not None else sid
        parent = node.find("w:basedOn", namespaces=NS)
        spacing = node.find("w:pPr/w:spacing", namespaces=NS)
        styles[sid] = {
            "based_on": parent.get(f"{{{W}}}val") if parent is not None else None,
            "line": spacing.get(f"{{{W}}}line") if spacing is not None else None,
            "line_rule": spacing.get(f"{{{W}}}lineRule") if spacing is not None else None,
        }
    return styles, names


def _drawing_records(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    with ZipFile(path) as zf:
        document = etree.fromstring(zf.read("word/document.xml"))
        rels = etree.fromstring(zf.read("word/_rels/document.xml.rels"))
        _style_map, names = _styles(zf)
        styles_root = etree.fromstring(zf.read("word/styles.xml"))
        relmap = {
            relation.get("Id"): _resolve_target("word/document.xml", relation.get("Target", ""))
            for relation in rels if relation.get("Id") and relation.get("Target")
        }
        records = []
        line_boxes = []
        paragraphs = document.xpath(".//w:body//w:p", namespaces=NS)
        for p_index, paragraph in enumerate(paragraphs, 1):
            # Only a plain embedded-picture container is raster-only. Keep a
            # separate aggregate unverified record for a mixed/group container:
            # one embedded image never proves visibility of its other content.
            blips = paragraph.xpath(
                ".//a:blip[@r:embed] | .//w:drawing["
                "not(.//a:blip[@r:embed]) or .//a:blip[@r:link] or "
                ".//a:graphicData/*[not(self::pic:pic)]]",
                namespaces=NS,
            )
            if not paragraph.xpath(".//w:drawing", namespaces=NS):
                continue
            pstyle = paragraph.find("w:pPr/w:pStyle", namespaces=NS)
            sid = pstyle.get(f"{{{W}}}val") if pstyle is not None else "Normal"
            spacing = effective_line_spacing(paragraph, styles_root)
            geometry = inline_drawing_geometry(paragraph)
            heights = []
            for blip_index, blip in enumerate(blips, 1):
                if blip.tag == f"{{{W}}}drawing":
                    records.append({
                        "paragraph_index": p_index, "drawing_index_in_paragraph": blip_index,
                        "representation": "unsupported_or_mixed_drawing_container",
                        "record_scope": "aggregate_container_not_individual_object",
                        "embedded_raster_count": len(blip.xpath(".//a:blip[@r:embed]", namespaces=NS)),
                        "linked_image_count": len(blip.xpath(".//a:blip[@r:link]", namespaces=NS)),
                        "graphic_content_tags": [node.tag for node in blip.xpath(".//a:graphicData/*", namespaces=NS)],
                        "visibility_binding_supported": False,
                        "source_crop": [], "extent_pt": {},
                        "media_sha256": None, "pixel_sha256": None, "pixel_size": None,
                    })
                    continue
                rid = blip.get(f"{{{R}}}embed")
                target = relmap.get(rid)
                extent_node = blip.getparent()
                while extent_node is not None and extent_node.tag not in {f"{{{WP}}}inline", f"{{{WP}}}anchor"}:
                    extent_node = extent_node.getparent()
                extent = extent_node.find("wp:extent", namespaces=NS) if extent_node is not None else None
                source_crop = [dict(node.attrib) for node in blip.getparent().findall("a:srcRect", namespaces=NS)]
                try:
                    height_pt = round(int(extent.get("cy")) / EMU_PER_PT, 3) if extent is not None else None
                    width_pt = round(int(extent.get("cx")) / EMU_PER_PT, 3) if extent is not None else None
                    if height_pt is not None and (height_pt <= 0 or width_pt <= 0):
                        height_pt = width_pt = None
                except (TypeError, ValueError):
                    height_pt = width_pt = None
                if height_pt is not None:
                    heights.append(height_pt)
                member = target if target in zf.namelist() else None
                media_sha = hashlib.sha256(zf.read(member)).hexdigest() if member else None
                pixel_size = None
                pixel_sha = None
                if member:
                    try:
                        with Image.open(io.BytesIO(zf.read(member))) as image:
                            normalized = ImageOps.exif_transpose(image).convert("RGBA")
                            pixel_size = [int(normalized.width), int(normalized.height)]
                            pixel_sha = hashlib.sha256(normalized.tobytes()).hexdigest()
                    except Exception:
                        pass
                records.append({
                    "paragraph_index": p_index, "drawing_index_in_paragraph": blip_index,
                    "relationship_id": rid, "media_part": member, "media_sha256": media_sha,
                    "pixel_size": pixel_size, "pixel_sha256": pixel_sha,
                    "extent_pt": {"width": width_pt, "height": height_pt},
                    "source_crop": source_crop,
                })
            line_boxes.append({
                "paragraph_index": p_index, "style_id": sid, "style_name": names.get(sid, sid),
                "drawing_count": len(paragraph.xpath(".//w:drawing", namespaces=NS)),
                "observation_record_count": len(blips), "max_drawing_extent_pt": max(heights) if heights else None,
                "effective_line_spacing": spacing, "geometry": geometry,
                "assessment": line_box_assessment(geometry, spacing), "rendered_verified": False,
            })
        return records, line_boxes


def _font_key(value: str) -> str:
    from font_declarations import font_comparison_key
    value = re.sub(r"^[A-Z]{6}\+", "", value)
    return font_comparison_key(value)


def _style_names_used(path: Path) -> set[str]:
    with ZipFile(path) as zf:
        names: dict[str, str] = {}
        styles = etree.fromstring(zf.read("word/styles.xml"))
        for node in styles.xpath(".//w:style", namespaces=NS):
            sid = node.get(f"{{{W}}}styleId")
            if not sid:
                continue
            name = node.find("w:name", namespaces=NS)
            names[sid] = name.get(f"{{{W}}}val", sid) if name is not None else sid
        used: set[str] = set()
        for name in zf.namelist():
            if not re.fullmatch(r"word/(?:document|header\d*|footer\d*|footnotes|endnotes)\.xml", name):
                continue
            root = etree.fromstring(zf.read(name))
            for paragraph in root.xpath(".//w:p", namespaces=NS):
                pstyle = paragraph.find("w:pPr/w:pStyle", namespaces=NS)
                sid = pstyle.get(f"{{{W}}}val") if pstyle is not None else "Normal"
                used.add(names.get(sid, sid))
        return used


def _font_expectations(spec: dict[str, Any], style_map_path: Path | None,
                       final_docx: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    roles = spec.get("roles") or {}
    if not isinstance(roles, dict):
        return [], [{"code": "rendered_font_roles_invalid"}], {"status": "blocked"}
    if style_map_path is None or not style_map_path.is_file():
        return [], [{"code": "rendered_font_style_map_missing",
                     "reason": "the rendered font check requires the exact application style map"}], {
                         "status": "blocked", "style_map": None, "used_styles": sorted(_style_names_used(final_docx)),
                     }
    try:
        data = strict_json_read(style_map_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [], [{"code": "rendered_font_style_map_unreadable",
                     "reason": f"{type(exc).__name__}: {exc}"}], {"status": "blocked"}
    mappings = data.get("mappings", data) if isinstance(data, dict) else None
    if not isinstance(mappings, dict):
        return [], [{"code": "rendered_font_style_map_invalid"}], {"status": "blocked"}
    used_styles = _style_names_used(final_docx)
    expectations: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    unresolved_used_roles = []
    for role, role_spec in roles.items():
        if not isinstance(role_spec, dict):
            continue
        mapped = mappings.get(role)
        style_name = mapped.get("style_name") if isinstance(mapped, dict) else None
        # Only styles that occur in the final document contribute a PDF font
        # expectation. This avoids requiring absent conditional roles.
        if not style_name or style_name not in used_styles:
            continue
        font = role_spec.get("font")
        if not isinstance(font, dict):
            continue
        for script in ("latin", "cjk"):
            name = font.get(script)
            if isinstance(name, str) and name.strip():
                requirement_ids = sorted({
                    requirement.get("id")
                    for requirement in spec.get("requirements", [])
                    if isinstance(requirement, dict)
                    and requirement.get("role") == role
                    and isinstance(requirement.get("id"), str)
                    and isinstance(requirement.get("properties"), dict)
                    and isinstance(requirement["properties"].get("font"), dict)
                    and requirement["properties"]["font"].get(script) == name
                })
                expectations.append({"role": role, "style_name": style_name,
                                     "script": script, "name": name,
                                     "requirement_ids": requirement_ids})
    # A missing mapping for a used spec role is only detectable if the style
    # map explicitly declares it. Do not infer a role from a similar style name.
    return expectations, findings, {
        "status": "verified", "style_map": str(style_map_path.resolve()),
        "style_map_sha256": digest(style_map_path), "used_styles": sorted(used_styles),
        "expectation_count": len(expectations), "unresolved_used_roles": unresolved_used_roles,
    }


def _pdf_image_pixel_sha(document: Any, xref: Any) -> str | None:
    if not isinstance(xref, int) or xref <= 0:
        return None
    try:
        payload = document.extract_image(xref)
        with Image.open(io.BytesIO(payload["image"])) as image:
            normalized = ImageOps.exif_transpose(image).convert("RGBA")
            return hashlib.sha256(normalized.tobytes()).hexdigest()
    except Exception:
        return None


def audit(source_docx: Path, final_docx: Path, pdf_path: Path,
          format_spec_path: Path, renderer_report_path: Path | None = None,
          style_map_path: Path | None = None) -> dict[str, Any]:
    import fitz

    source_drawings, source_line_boxes = _drawing_records(source_docx)
    output_drawings, output_line_boxes = _drawing_records(final_docx)
    findings: list[dict[str, Any]] = []
    if len(output_drawings) != len(source_drawings):
        findings.append({"code": "drawing_count_changed", "source_count": len(source_drawings),
                         "output_count": len(output_drawings)})
    for index, (before, after) in enumerate(zip(source_drawings, output_drawings), 1):
        if before.get("media_sha256") != after.get("media_sha256") or before.get("pixel_size") != after.get("pixel_size"):
            findings.append({"code": "drawing_media_changed", "drawing_index": index,
                             "source": before, "output": after})
        if before.get("source_crop") != after.get("source_crop"):
            findings.append({"code": "drawing_crop_changed", "drawing_index": index, "status": "failed",
                             "source_crop": before.get("source_crop"), "output_crop": after.get("source_crop"),
                             "reason": "matching media bytes do not establish preservation of image cropping"})
    for row in output_line_boxes:
        if row["assessment"] == "restrictive_exact":
            findings.append({"code": "drawing_line_box_not_object_safe", "paragraph_index": row["paragraph_index"],
                             "line_box": row["effective_line_spacing"], "geometry": row["geometry"],
                             "reason": "observed inline bounds exceed a fixed exact line height", "rendered_verified": False})
        elif row["assessment"] == "unverified":
            findings.append({"code": "drawing_line_box_geometry_unverified", "paragraph_index": row["paragraph_index"],
                             "status": "unverified", "line_box": row["effective_line_spacing"],
                             "geometry": row["geometry"], "rendered_verified": False})

    spec = strict_json_read(format_spec_path)
    expected_fonts, font_scope_findings, font_scope = _font_expectations(
        spec, style_map_path, final_docx,
    )
    findings.extend(font_scope_findings)

    pdf_fonts: dict[str, dict[str, Any]] = {}
    image_instances: list[dict[str, Any]] = []
    with fitz.open(pdf_path) as document:
        page_count = len(document)
        glyph_integrity = audit_pdf_glyph_integrity(document)
        findings.extend(glyph_integrity["findings"])
        for page_number, page in enumerate(document, 1):
            for font in page.get_fonts(full=True):
                base = str(font[3]) if len(font) > 3 else str(font[0])
                entry = pdf_fonts.setdefault(base, {"base_font": base, "pages": [], "xrefs": []})
                if page_number not in entry["pages"]:
                    entry["pages"].append(page_number)
                xref = int(font[0]) if font and isinstance(font[0], int) else None
                if xref is not None and xref not in entry["xrefs"]:
                    entry["xrefs"].append(xref)
            for info in page.get_image_info(xrefs=True):
                bbox = fitz.Rect(info["bbox"])
                clip = bbox & page.rect
                visibility = clip.get_area() / bbox.get_area() if bbox.get_area() > 0 else 0.0
                image_instances.append({
                    "page": page_number, "xref": info.get("xref"),
                    "pixel_size": [int(info.get("width", 0)), int(info.get("height", 0))],
                    "pixel_sha256": _pdf_image_pixel_sha(document, info.get("xref")),
                    "bbox_pt": [round(value, 2) for value in info["bbox"]],
                    "raster_bbox_area_inside_page_ratio": round(visibility, 6),
                    "visible_area_ratio": None, "visible_clip_bound": False,
                    "fully_inside_page": (bbox.x0 >= -.25 and bbox.y0 >= -.25
                                           and bbox.x1 <= page.rect.width + .25
                                           and bbox.y1 <= page.rect.height + .25),
                })

    font_names = sorted(pdf_fonts)
    font_keys = {key for name in font_names if (key := _font_key(name))}
    font_findings = []
    for expected in expected_fonts:
        if not _font_key(expected["name"]) or _font_key(expected["name"]) not in font_keys:
            item = {"code": "rendered_pdf_font_mismatch", "expected": expected,
                    "actual_pdf_fonts": font_names}
            font_findings.append(item)
            findings.append(item)

    remaining = list(image_instances)
    drawing_visibility = []
    for index, drawing in enumerate(output_drawings, 1):
        if drawing.get("visibility_binding_supported") is False:
            drawing_visibility.append({**drawing, "status": "unverified"})
            findings.append({"code": "rendered_drawing_visibility_unverified", "drawing_index": index,
                             "reason": "non-embedded raster or unsupported drawing lacks a reliable PDF visibility binding"})
            continue
        size = drawing.get("pixel_size")
        pixel_sha = drawing.get("pixel_sha256")
        exact_candidates = [i for i, image in enumerate(remaining)
                            if pixel_sha and image.get("pixel_sha256") == pixel_sha
                            and image.get("pixel_size") == size]
        if exact_candidates:
            match_index = exact_candidates[0]
        else:
            size_candidates = [i for i, image in enumerate(remaining)
                               if image.get("pixel_size") == size]
            match_index = size_candidates[0] if len(size_candidates) == 1 and not pixel_sha else None
        if match_index is None:
            item = {"code": "drawing_not_found_in_rendered_pdf", "drawing_index": index,
                    "pixel_size": size, "pixel_sha256": pixel_sha,
                    "media_sha256": drawing.get("media_sha256"),
                    "candidate_count": sum(1 for image in remaining if image.get("pixel_size") == size)}
            drawing_visibility.append({**drawing, "status": "unmatched_or_ambiguous"})
            findings.append(item)
            continue
        image = remaining.pop(match_index)
        expected_height = (drawing.get("extent_pt") or {}).get("height")
        actual_height = round(image["bbox_pt"][3] - image["bbox_pt"][1], 2)
        row = {**drawing, "rendered": image, "expected_extent_height_pt": expected_height,
               "rendered_height_pt": actual_height}
        # A full raster transform does not bind the effective PDF clipping
        # path. An on-page bbox can still hide half the image behind a clip.
        row["status"] = "unverified"
        findings.append({"code": "rendered_drawing_visibility_unverified", "drawing_index": index,
                         "page": image["page"], "source_crop": drawing["source_crop"],
                         "reason": "PDF raster bounds are observed but the effective visible clipping path is unbound"})
        drawing_visibility.append(row)

    renderer = None
    if renderer_report_path and renderer_report_path.is_file():
        renderer = strict_json_read(renderer_report_path)
        expected_pdf_hash = renderer.get("final_pdf", {}).get("pdf_sha256") if isinstance(renderer.get("final_pdf"), dict) else None
        if expected_pdf_hash and expected_pdf_hash != digest(pdf_path):
            findings.append({"code": "renderer_report_pdf_hash_mismatch",
                             "expected": expected_pdf_hash, "actual": digest(pdf_path)})
    report: dict[str, Any] = {
        "schema_version": "1.0", "protocol": "rendered_format_audit_v1",
        "source_docx_sha256": digest(source_docx),
        "docx_sha256": digest(final_docx),
        "pdf_sha256": digest(pdf_path),
        "source_docx": {"path": str(source_docx.resolve()), "bytes": source_docx.stat().st_size,
                        "sha256": digest(source_docx)},
        "final_docx": {"path": str(final_docx.resolve()), "bytes": final_docx.stat().st_size,
                       "sha256": digest(final_docx)},
        "pdf": {"path": str(pdf_path.resolve()), "bytes": pdf_path.stat().st_size,
                "sha256": digest(pdf_path), "page_count": page_count},
        "renderer": renderer,
        "drawing_line_boxes": {"source": source_line_boxes, "output": output_line_boxes},
        "drawing_visibility": drawing_visibility,
        "expected_pdf_fonts": expected_fonts,
        "font_scope": font_scope,
        "actual_pdf_font_resources": [pdf_fonts[name] for name in font_names],
        "font_findings": font_findings,
        "glyph_integrity": glyph_integrity,
        "findings": findings,
        "status": "passed" if not findings else "issues_found",
        "submission_ready": False,
        "field_refresh_claimed": False,
    }
    report["audit_sha256"] = sha256_json(report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_docx", type=Path)
    parser.add_argument("final_docx", type=Path)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("format_spec", type=Path)
    parser.add_argument("--style-map", type=Path, required=True,
                        help="application role-to-style map used to scope font expectations")
    parser.add_argument("--renderer-report", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    inputs = [args.source_docx, args.final_docx, args.pdf, args.format_spec]
    inputs.append(args.style_map)
    if args.renderer_report:
        inputs.append(args.renderer_report)
    resolved = [path.resolve() for path in inputs]
    output = args.out.resolve()
    if output in resolved:
        parser.error("audit output must differ from every audit input")
    try:
        report = audit(args.source_docx, args.final_docx, args.pdf, args.format_spec,
                       args.renderer_report, args.style_map)
    except Exception as exc:
        print(f"rendered format audit failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
