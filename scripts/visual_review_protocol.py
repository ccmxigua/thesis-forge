"""Shared primitives for hash-bound, renderer-scoped visual review v2.

This module deliberately separates rasterization receipts from review claims.
It never infers that a provider received or understood an image from a local
CLI argument, exit code, or textual response.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from PIL import Image

PROTOCOL = "native_visual_page_review_v2"
CHECKLIST_ITEMS = (
    "headers_footers_and_parity",
    "visible_folio_and_page_mapping",
    "body_readability_and_glyphs",
    "whitespace_and_page_breaks",
    "figures_and_captions",
    "tables_and_splits",
    "equations_and_symbols",
    "cross_page_continuity",
    "source_rule_and_section_mapping",
    "contents_entries_and_targets",
)
CHECKLIST_STATES = {"pass", "issue", "not_applicable", "unable_to_assess"}
REVIEW_STATES = {"observations_recorded", "needs_human_review", "unable_to_assess"}
MAX_REGION_CROPS_PER_PAGE = 32
SMALL_TEXT_CANDIDATE_PT = 10.0


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _clamp_rect(rect: tuple[int, int, int, int], width: int, height: int) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = rect
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(width, x1), min(height, y1)
    if x0 >= x1 or y0 >= y1:
        raise ValueError("region crop has zero or out-of-page area")
    return x0, y0, x1, y1


def _region_candidates(pdf_page: Any) -> list[dict[str, Any]]:
    """Return conservative candidate regions from extractable PDF structure.

    These are navigation aids, not a complete semantic detector. The receipt
    preserves detector identity so the checklist can mark any missing scope
    unknown instead of implying that all figures/tables/formulas were found.
    """
    import fitz

    page_rect = pdf_page.rect
    width, height = float(page_rect.width), float(page_rect.height)
    candidates: list[dict[str, Any]] = [
        {"kind": "header_band", "bbox": (0.0, 0.0, width, height * 0.17), "detector": "fixed_page_band_v1"},
        {"kind": "footer_band", "bbox": (0.0, height * 0.83, width, height), "detector": "fixed_page_band_v1"},
    ]
    text = pdf_page.get_text("dict")
    image_index = 0
    small_index = 0
    caption_index = 0
    equation_index = 0
    caption_re = re.compile(r"(?i)(?:^|\s)(?:figure|fig\.?|table|tab\.?|图|表)\s*[-.\d一二三四五六七八九十]*")
    equation_re = re.compile(r"(?:[=≈≠≤≥∑∫√±×÷]|\b(?:sin|cos|log|exp)\b|\([0-9]+[-.]\d+\))")
    for block in text.get("blocks", []):
        bbox = tuple(float(v) for v in block.get("bbox", (0, 0, 0, 0)))
        if len(bbox) != 4 or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
            continue
        block_type = block.get("type")
        if block_type == 1:
            candidates.append({"kind": "pdf_image_block", "bbox": bbox,
                               "detector": "pymupdf_image_block_v1", "region_index": image_index})
            image_index += 1
            continue
        if block_type != 0:
            continue
        lines = block.get("lines", [])
        text_value = " ".join(
            span.get("text", "") for line in lines for span in line.get("spans", [])
        ).strip()
        if not text_value:
            continue
        # The full-page raster already carries every body-text block. Cropping
        # each ordinary paragraph adds dozens of redundant images on text-heavy
        # pages and can exceed the per-page evidence budget. Keep focused crops
        # for small text, captions, equations, images, drawings, and the page
        # bands; the exact full-page image remains mandatory and is never
        # replaced by these navigation aids.
        if any(float(span.get("size", 99)) <= SMALL_TEXT_CANDIDATE_PT
               for line in lines for span in line.get("spans", [])):
            candidates.append({"kind": "small_text_candidate", "bbox": bbox,
                               "detector": "pymupdf_span_size_threshold_v1",
                               "threshold_pt": SMALL_TEXT_CANDIDATE_PT, "region_index": small_index})
            small_index += 1
        if caption_re.search(text_value):
            candidates.append({"kind": "caption_candidate", "bbox": bbox,
                               "detector": "pymupdf_caption_text_pattern_v1", "region_index": caption_index})
            caption_index += 1
        if equation_re.search(text_value):
            candidates.append({"kind": "equation_candidate", "bbox": bbox,
                               "detector": "pymupdf_equation_text_pattern_v1", "region_index": equation_index})
            equation_index += 1

    # Vector drawings can carry tables or figures without raster image blocks.
    # Keep individual drawing bounds as candidates rather than claiming semantic
    # table/figure detection. Many tiny line segments are merged by overlap.
    drawings = []
    for drawing in pdf_page.get_drawings():
        rect = drawing.get("rect")
        if rect is None or rect.is_empty or rect.is_infinite:
            continue
        if rect.width >= 12 and rect.height >= 8:
            drawings.append(fitz.Rect(rect))
    groups: list[fitz.Rect] = []
    for rect in drawings:
        matched = None
        for group in groups:
            padded = fitz.Rect(group.x0 - 6, group.y0 - 6, group.x1 + 6, group.y1 + 6)
            if padded.intersects(rect):
                matched = group
                break
        if matched is None:
            groups.append(fitz.Rect(rect))
        else:
            matched.include_rect(rect)
    for index, rect in enumerate(groups):
        if rect.width * rect.height >= 320:
            candidates.append({"kind": "pdf_vector_drawing_candidate",
                               "bbox": (rect.x0, rect.y0, rect.x1, rect.y1),
                               "detector": "pymupdf_vector_drawing_group_v1", "region_index": index})
    return candidates


def create_region_crops(pdf_path: Path, page_number: int, full_page_path: Path,
                        crop_dir: Path, *, max_crops: int = MAX_REGION_CROPS_PER_PAGE) -> list[dict[str, Any]]:
    """Crop only from the 300-DPI (or greater) exact-page raster, never upscale."""
    import fitz

    with fitz.open(pdf_path) as document:
        if page_number < 1 or page_number > len(document):
            raise ValueError("crop page number is outside the bound PDF")
        page = document[page_number - 1]
        page_width_pt, page_height_pt = float(page.rect.width), float(page.rect.height)
        candidates = _region_candidates(page)
    with Image.open(full_page_path) as parent:
        if parent.format != "PNG":
            raise ValueError("full-page evidence must be PNG")
        image = parent.convert("RGB")
        width, height = image.size
        parent_digest = hashlib.sha256(full_page_path.read_bytes()).hexdigest()
        sx, sy = width / page_width_pt, height / page_height_pt
        dedupe: set[tuple[int, int, int, int, str]] = set()
        crops: list[dict[str, Any]] = []
        for index, candidate in enumerate(candidates):
            x0, y0, x1, y1 = candidate["bbox"]
            # A small contextual margin aids visual interpretation. It is
            # clamped to this page and remains part of the recorded source box.
            margin_x = min(8.0, max(2.0, (x1 - x0) * 0.05))
            margin_y = min(8.0, max(2.0, (y1 - y0) * 0.08))
            x0, y0 = max(0.0, x0 - margin_x), max(0.0, y0 - margin_y)
            x1, y1 = min(page_width_pt, x1 + margin_x), min(page_height_pt, y1 + margin_y)
            pixel_box = _clamp_rect((int(x0 * sx), int(y0 * sy), int(x1 * sx + 0.999), int(y1 * sy + 0.999)), width, height)
            key = (*pixel_box, candidate["kind"])
            if key in dedupe:
                continue
            dedupe.add(key)
            if len(crops) >= max_crops:
                raise ValueError(f"page {page_number} has more than {max_crops} evidence crops; review scope is incomplete")
            crop_id = f"p{page_number:04d}-crop{len(crops) + 1:03d}"
            path = crop_dir / f"{crop_id}.png"
            crop = image.crop(pixel_box)
            crop.save(path, format="PNG", optimize=False)
            raw = path.read_bytes()
            crops.append({
                "image_id": crop_id,
                "kind": candidate["kind"],
                "detector": candidate["detector"],
                "detector_region_index": candidate.get("region_index"),
                "bbox_pdf_points": [round(float(v), 4) for v in (x0, y0, x1, y1)],
                "bbox_norm": [round(x0 / page_width_pt, 8), round(y0 / page_height_pt, 8),
                              round(x1 / page_width_pt, 8), round(y1 / page_height_pt, 8)],
                "bbox_pixels_in_parent": list(pixel_box),
                "parent_page_image_sha256": parent_digest,
                "image": {"path": str(path.resolve()), "bytes": len(raw), "sha256": sha256_bytes(raw)},
                "width_px": crop.width,
                "height_px": crop.height,
            })
    return crops


def build_page_map(pdf_path: Path) -> list[dict[str, Any]]:
    """Record measurable page facts while leaving unanchored semantics unknown."""
    import fitz

    rows: list[dict[str, Any]] = []
    with fitz.open(pdf_path) as document:
        for index, page in enumerate(document, start=1):
            height = float(page.rect.height)
            words = page.get_text("words")
            folio_candidates = sorted({
                str(word[4]).strip() for word in words
                if float(word[1]) >= height * 0.78 and str(word[4]).strip()
            })
            rows.append({
                "physical_pdf_page": index,
                "pdf_page_label": page.get_label() or None,
                "visible_folio_text_candidates": folio_candidates,
                "visible_folio_status": "candidate_text_only_unconfirmed",
                "section_id": None,
                "chapter_or_appendix_anchor": None,
                "page_type": "unknown",
                "source_mapping_status": "unknown",
                "applicability": {"contents_entries_and_targets": None},
                "mapping_note": "No source-bound page/section map was supplied to this visual-review run.",
            })
    return rows


def renderer_scope(render_report: dict[str, Any], pdf_sha256: str) -> dict[str, Any]:
    renderer = render_report.get("renderer") if isinstance(render_report.get("renderer"), dict) else {}
    name = renderer.get("name") or renderer.get("renderer")
    version = renderer.get("version")
    if name == "Microsoft Word" and isinstance(version, str) and version.strip():
        scope = "word_delivery"
    elif isinstance(name, str) and name.lower().startswith("libreoffice") and isinstance(version, str) and version.strip():
        scope = "lo_preview"
    else:
        scope = "unknown"
    return {
        "scope": scope,
        "renderer_name": name if isinstance(name, str) else "unknown",
        "renderer_version": version if isinstance(version, str) and version.strip() else "unknown",
        "operating_system": render_report.get("operating_system", "unknown"),
        "font_environment": render_report.get("font_environment", "unknown"),
        "pdf_sha256": pdf_sha256,
        "word_attestation_present": bool(
            isinstance(render_report.get("attestation"), dict)
            and render_report["attestation"].get("algorithm") == "HMAC-SHA256"
            and isinstance(render_report["attestation"].get("signature"), str)
        ),
    }


def validate_page_map(value: Any, *, docx_sha256: str, pdf_sha256: str,
                      page_count: int, allowed_clause_ids: set[str]) -> list[dict[str, Any]]:
    """Validate one explicit source-bound section/page map; never infer anchors."""
    if not isinstance(value, dict) or set(value) != {
        "schema_version", "source_docx_sha256", "pdf_sha256", "pages"
    }:
        raise ValueError("page map must follow visual-page-map.schema.json")
    if value.get("schema_version") != "1.0":
        raise ValueError("page map schema version is unsupported")
    if value.get("source_docx_sha256") != docx_sha256 or value.get("pdf_sha256") != pdf_sha256:
        raise ValueError("page map source DOCX/PDF hash binding does not match current artifacts")
    pages = value.get("pages")
    if not isinstance(pages, list) or len(pages) != page_count:
        raise ValueError("page map must contain one record for every physical PDF page")
    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(pages, 1):
        if not isinstance(row, dict) or set(row) != {
            "physical_pdf_page", "section_id", "chapter_or_appendix_anchor",
            "page_type", "source_clause_ids", "applicability",
        }:
            raise ValueError(f"page map row {index} is malformed")
        if row.get("physical_pdf_page") != index:
            raise ValueError("page map physical pages must be exactly 1..N in order")
        if not isinstance(row.get("section_id"), str) or not row["section_id"].strip():
            raise ValueError(f"page map row {index} has no section_id")
        if row.get("page_type") not in {
            "cover", "front_matter", "contents", "body", "chapter_start", "appendix",
            "afterword", "separator", "blank_verso", "ledger", "other",
        }:
            raise ValueError(f"page map row {index} has an unsupported page_type")
        clause_ids = row.get("source_clause_ids")
        if (not isinstance(clause_ids, list) or len(clause_ids) != len(set(clause_ids))
                or any(not isinstance(cid, str) or cid not in allowed_clause_ids for cid in clause_ids)):
            raise ValueError(f"page map row {index} cites missing or unknown source clauses")
        applicability = row.get("applicability")
        if (not isinstance(applicability, dict)
                or set(applicability) != {"contents_entries_and_targets"}
                or not isinstance(applicability.get("contents_entries_and_targets"), bool)):
            raise ValueError(f"page map row {index} applicability is unresolved")
        normalized.append({
            **row,
            "source_mapping_status": "resolved",
            "mapping_note": "Explicit page map bound to exact DOCX, PDF, and source clause IDs.",
        })
    return normalized


def validate_issue_ledger(ledger: Any, *, run_id: str, current_pdf_sha256: str,
                          page_count: int, source_clause_ids: set[str],
                          current_run_full_coverage: bool,
                          allowed_image_ids_by_page: dict[int, set[str]] | None = None) -> None:
    if not isinstance(ledger, dict) or set(ledger) != {
        "schema_version", "run_id", "reviewed_pdf_sha256", "findings"
    }:
        raise ValueError("issue ledger does not match the strict protocol")
    if ledger.get("schema_version") != "1.0" or ledger.get("run_id") != run_id:
        raise ValueError("issue ledger run identity is stale")
    if ledger.get("reviewed_pdf_sha256") != current_pdf_sha256:
        raise ValueError("issue ledger is bound to a different PDF")
    findings = ledger.get("findings")
    if not isinstance(findings, list):
        raise ValueError("issue ledger findings must be an array")
    seen: set[str] = set()
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            raise ValueError(f"finding {index} must be an object")
        finding_id = finding.get("finding_id")
        if not isinstance(finding_id, str) or not finding_id.strip() or finding_id in seen:
            raise ValueError(f"finding {index} has a missing or duplicate id")
        seen.add(finding_id)
        status = finding.get("status")
        if status not in {
            "open", "needs_human_review", "intentional_with_source_basis",
            "fixed_pending_rerender", "closed_after_rerender", "unknown",
        }:
            raise ValueError(f"finding {finding_id} has an invalid lifecycle state")
        page = finding.get("physical_pdf_page")
        if isinstance(page, bool) or not isinstance(page, int) or page < 1 or (status != "closed_after_rerender" and page > page_count):
            raise ValueError(f"finding {finding_id} refers to an invalid physical page")
        image_ids = finding.get("evidence_image_ids")
        if not isinstance(image_ids, list) or not image_ids or len(image_ids) != len(set(image_ids)):
            raise ValueError(f"finding {finding_id} has no unique image evidence references")
        if (allowed_image_ids_by_page is not None and status != "closed_after_rerender"
                and any(image_id not in allowed_image_ids_by_page.get(page, set()) for image_id in image_ids)):
            raise ValueError(f"finding {finding_id} cites an image outside its physical page")
        clause_ids = finding.get("source_clause_ids", [])
        if not isinstance(clause_ids, list) or any(cid not in source_clause_ids for cid in clause_ids):
            raise ValueError(f"finding {finding_id} cites an unknown source clause")
        reason = finding.get("disposition_reason")
        reviewer = finding.get("reviewer_claim")
        closure = finding.get("closure")
        if status == "intentional_with_source_basis":
            if not clause_ids or not isinstance(reason, str) or not reason.strip() or not isinstance(reviewer, str) or not reviewer.strip():
                raise ValueError(f"finding {finding_id} intentional disposition needs source basis and reviewer claim")
            if finding.get("severity") in {"critical", "high"}:
                raise ValueError(f"finding {finding_id} critical/high severity cannot be disposed as intentional")
            if closure is not None:
                raise ValueError(f"finding {finding_id} has a rerender closure with an intentional disposition")
        elif status == "closed_after_rerender":
            required = {"prior_pdf_sha256", "rerender_pdf_sha256", "new_review_run_id",
                        "new_full_page_coverage", "source_finding_id", "source_report"}
            if not isinstance(closure, dict) or set(closure) != required:
                raise ValueError(f"finding {finding_id} closure receipt is incomplete")
            if (closure.get("rerender_pdf_sha256") != current_pdf_sha256
                    or closure.get("prior_pdf_sha256") == current_pdf_sha256
                    or closure.get("new_review_run_id") != run_id
                    or closure.get("new_full_page_coverage") is not True
                    or not current_run_full_coverage):
                raise ValueError(f"finding {finding_id} is not backed by a new fully reviewed rerender")
            if closure.get("source_finding_id") != finding_id:
                raise ValueError(f"finding {finding_id} closure points to a different source finding")
            source_report = closure.get("source_report")
            if (not isinstance(source_report, dict)
                    or set(source_report) != {"path", "bytes", "sha256"}
                    or not isinstance(source_report.get("path"), str)
                    or isinstance(source_report.get("bytes"), bool)
                    or not isinstance(source_report.get("bytes"), int)
                    or not isinstance(source_report.get("sha256"), str)):
                raise ValueError(f"finding {finding_id} source report binding is malformed")
            if not isinstance(reviewer, str) or not reviewer.strip() or not isinstance(reason, str) or not reason.strip():
                raise ValueError(f"finding {finding_id} closure needs reviewer claim and disposition reason")
        elif closure is not None:
            raise ValueError(f"finding {finding_id} has a closure receipt without a closed_after_rerender state")


def validate_closed_finding_source(finding: dict[str, Any], source_report: dict[str, Any], *,
                                   current_run_id: str, current_code_fingerprint: dict[str, Any],
                                   current_code_fingerprint_sha256: str,
                                   current_source_clauses_sha256: str) -> None:
    """Prove a carried closure row comes from one reviewed prior report.

    File-byte and human-receipt verification are performed by the caller. This
    function checks the semantic parent relation and rejects stale-code or
    stale-run evidence before the closure can be treated as carried history.
    """
    closure = finding.get("closure")
    if not isinstance(closure, dict):
        raise ValueError("closed finding has no closure object")
    if (source_report.get("protocol") != PROTOCOL
            or source_report.get("run_id") == current_run_id
            or source_report.get("code_fingerprint") != current_code_fingerprint
            or source_report.get("code_fingerprint_sha256") != current_code_fingerprint_sha256):
        raise ValueError("closed finding source report protocol, run, or code identity mismatch")
    source_pdf_sha256 = (source_report.get("pdf") or {}).get("sha256")
    if source_pdf_sha256 != closure.get("prior_pdf_sha256"):
        raise ValueError("closed finding prior PDF does not match its source report")
    if (source_report.get("source_clauses") or {}).get("sha256") != current_source_clauses_sha256:
        raise ValueError("closed finding source clauses differ from the current rerender")
    finding_id = finding.get("finding_id")
    matches = [
        (page, issue)
        for page in source_report.get("pages", []) if isinstance(page, dict)
        for issue in page.get("issues", []) if isinstance(issue, dict)
        if issue.get("finding_id") == finding_id
    ]
    if len(matches) != 1:
        raise ValueError("closed finding must occur exactly once in its source report")
    page, issue = matches[0]
    if issue.get("status") not in {None, "open", "needs_human_review", "fixed_pending_rerender"}:
        raise ValueError("closed finding source report does not contain an open finding")
    if (page.get("page_number") != finding.get("physical_pdf_page")
            or issue.get("evidence_image_ids") != finding.get("evidence_image_ids")
            or issue.get("finding") != finding.get("finding")
            or issue.get("severity") != finding.get("severity")
            or issue.get("source_clause_ids", []) != finding.get("source_clause_ids", [])):
        raise ValueError("closed finding projection differs from its source report")


def validate_checklist(checklist: Any, *, allowed_image_ids: set[str]) -> None:
    if not isinstance(checklist, list) or len(checklist) != len(CHECKLIST_ITEMS):
        raise ValueError("checklist must contain every required visual item exactly once")
    seen: set[str] = set()
    for index, item in enumerate(checklist):
        if not isinstance(item, dict) or set(item) != {
            "item_id", "status", "observation", "reason", "evidence_image_ids"
        }:
            raise ValueError(f"checklist item {index} does not match the strict contract")
        item_id = item.get("item_id")
        if item_id not in CHECKLIST_ITEMS or item_id in seen:
            raise ValueError(f"checklist item {index} has an unknown or duplicate item_id")
        seen.add(item_id)
        if item.get("status") not in CHECKLIST_STATES:
            raise ValueError(f"checklist item {item_id} has an invalid state")
        observation = item.get("observation")
        reason = item.get("reason")
        if not isinstance(observation, str) or not observation.strip() or len(observation) > 500:
            raise ValueError(f"checklist item {item_id} requires a concise observation")
        if item["status"] == "not_applicable" and (not isinstance(reason, str) or not reason.strip()):
            raise ValueError(f"not_applicable checklist item {item_id} requires a reason")
        if item["status"] == "unable_to_assess" and (not isinstance(reason, str) or not reason.strip()):
            raise ValueError(f"unable_to_assess checklist item {item_id} requires a reason")
        image_ids = item.get("evidence_image_ids")
        if (not isinstance(image_ids, list) or not image_ids
                or any(not isinstance(image_id, str) or image_id not in allowed_image_ids for image_id in image_ids)
                or len(image_ids) != len(set(image_ids))):
            raise ValueError(f"checklist item {item_id} must cite an attached full-page or crop image")
    if seen != set(CHECKLIST_ITEMS):
        raise ValueError("checklist has missing required visual items")


def build_issue_ledger(run_id: str, pdf_sha256: str, pages: list[dict[str, Any]]) -> dict[str, Any]:
    findings = []
    for page in pages:
        for issue in page.get("issues", []):
            findings.append({
                "finding_id": issue["finding_id"],
                "physical_pdf_page": page["page_number"],
                "evidence_image_ids": list(issue["evidence_image_ids"]),
                "status": "open", "finding": issue["finding"],
                "severity": issue["severity"], "reviewer_claim": None,
                "disposition_reason": None,
                "source_clause_ids": list(issue["source_clause_ids"]),
                "closure": None,
            })
    return {"schema_version": "1.0", "run_id": run_id,
            "reviewed_pdf_sha256": pdf_sha256, "findings": findings}


def review_status(*, page_count: int, pages: list[dict[str, Any]], scope: dict[str, Any],
                  resolved_finding_ids: set[str] | None = None) -> tuple[str, str, list[str]]:
    """Return renderer-specific status; provider claims never substitute for receipt."""
    blockers: list[str] = []
    resolved_finding_ids = resolved_finding_ids or set()
    expected = list(range(1, page_count + 1))
    actual = [page.get("page_number") for page in pages]
    if actual != expected:
        blockers.append("physical_page_coverage_incomplete")
    for page in pages:
        number = page.get("page_number")
        if page.get("request_image_receipt", {}).get("status") != "provider_confirmed_exact_images":
            if page.get("review_method") != "human_browser_session":
                blockers.append(f"request_image_delivery_unobservable:{number}")
        if page.get("page_map", {}).get("source_mapping_status") != "resolved":
            blockers.append(f"page_source_mapping_unknown:{number}")
        if page.get("page_status") != "observations_recorded":
            blockers.append(f"page_observations_incomplete:{number}")
        issues = page.get("issues", [])
        open_issues = [issue for issue in issues if issue.get("finding_id") not in resolved_finding_ids]
        checklist = page.get("checklist", [])
        has_unable = any(item.get("status") == "unable_to_assess" for item in checklist)
        has_issue = any(item.get("status") == "issue" for item in checklist)
        has_unresolved_issue = has_issue and (not issues or bool(open_issues))
        if (open_issues or has_unable or has_unresolved_issue
                or (page.get("unresolved_checklist_items") and not checklist)):
            blockers.append(f"page_has_findings_or_unknowns:{number}")
    if scope.get("scope") == "word_delivery" and not scope.get("word_attestation_present"):
        blockers.append("word_delivery_render_attestation_missing")
    if scope.get("scope") == "unknown":
        blockers.append("renderer_scope_unknown")
    if blockers:
        return "unknown", "blocked", sorted(set(blockers))
    return "passed", "passed", []


def renderer_status_projection(scope: dict[str, Any], status: str) -> dict[str, str]:
    values = {"lo_preview_visual_status": "not_run", "word_delivery_visual_status": "not_run"}
    target = {
        "lo_preview": "lo_preview_visual_status",
        "word_delivery": "word_delivery_visual_status",
    }.get(scope.get("scope"))
    if target:
        values[target] = status
    return values
