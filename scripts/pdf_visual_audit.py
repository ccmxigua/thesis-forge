#!/usr/bin/env python3
"""Rasterize a rendered PDF and perform conservative visual sanity checks.

This is not a substitute for human visual review.  It is an independent
post-render gate that catches a missing/blank page and binds the raster check
to the exact PDF hash.  It intentionally does not guess margins or rewrite a
document when a page looks unusual.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from artifact_io import atomic_write_text
from process_runner import run_process

ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _page_count(path: Path) -> int:
    try:
        from pypdf import PdfReader
    except ImportError:
        from PyPDF2 import PdfReader
    return len(PdfReader(str(path)).pages)


def audit_pdf(pdf: Path, *, output: Path) -> dict[str, Any]:
    pdf = pdf.resolve()
    digest = sha256(pdf)
    blockers: list[str] = []
    page_count = 0
    raster_pages: list[dict[str, Any]] = []
    pdftoppm = shutil.which("pdftoppm")
    if not pdftoppm:
        blockers.append("pdftoppm_unavailable")
    else:
        try:
            page_count = _page_count(pdf)
        except Exception as exc:
            blockers.append(f"pdf_page_count_failed:{type(exc).__name__}")
        if page_count <= 0:
            blockers.append("pdf_has_no_pages")
        if not blockers:
            try:
                from PIL import Image, ImageStat
                with tempfile.TemporaryDirectory(prefix="pdf-visual-") as td:
                    prefix = str(Path(td) / "page")
                    result = run_process(
                        [pdftoppm, "-png", "-r", "72", "-f", "1", "-l", str(page_count),
                         str(pdf), prefix],
                        cwd=ROOT, timeout=180,
                    )
                    if result.returncode != 0:
                        blockers.append("pdf_rasterization_failed")
                    else:
                        for index in range(1, page_count + 1):
                            page_path = Path(f"{prefix}-{index:02d}.png")
                            if not page_path.is_file():
                                # pdftoppm uses a non-padded suffix for small
                                # page counts on some versions.
                                page_path = Path(f"{prefix}-{index}.png")
                            if not page_path.is_file():
                                blockers.append(f"pdf_raster_page_missing:{index}")
                                continue
                            image = Image.open(page_path).convert("L")
                            stat = ImageStat.Stat(image)
                            mean = float(stat.mean[0])
                            extrema = image.getextrema()
                            # A completely white/near-white page is never a
                            # valid thesis page.  Do not reject a sparse page
                            # merely because its mean ink ratio is low.
                            nonwhite = sum(1 for value in image.getdata() if value < 250)
                            pixels = image.width * image.height
                            nonwhite_ratio = nonwhite / pixels if pixels else 0.0
                            item = {
                                "page": index,
                                "width": image.width,
                                "height": image.height,
                                "mean_gray": mean,
                                "min_gray": extrema[0],
                                "nonwhite_ratio": round(nonwhite_ratio, 8),
                            }
                            raster_pages.append(item)
                            if nonwhite_ratio <= 0.00001:
                                blockers.append(f"pdf_page_blank:{index}")
            except ImportError:
                blockers.append("pillow_unavailable")
            except OSError as exc:
                blockers.append(f"pdf_visual_probe_failed:{type(exc).__name__}")
    payload = {
        "schema_version": "1.0",
        "evidence_type": "pdf_raster_visual_sanity",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "pdf": {"path": str(pdf), "sha256": digest, "page_count": page_count},
        "pages": raster_pages,
        "checks": {
            "pdf_exists": pdf.is_file(),
            "rasterized_all_pages": len(raster_pages) == page_count and page_count > 0,
            "no_blank_pages": not any(item.startswith("pdf_page_blank:") for item in blockers),
        },
        "status": "passed" if not blockers else "blocked",
        "blockers": sorted(set(blockers)),
        "policy": {
            "independent_from_word_text_validator": True,
            "no_automatic_layout_repair": True,
            "hash_bound": True,
        },
    }
    atomic_write_text(output.resolve(), json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    pdf = args.pdf.expanduser().resolve()
    output = args.out.expanduser().resolve()
    if not pdf.is_file():
        parser.error(f"PDF does not exist: {pdf}")
    if output == pdf:
        parser.error("visual audit output must differ from the PDF")
    payload = audit_pdf(pdf, output=output)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
