#!/usr/bin/env python3
"""Create hash-bound evidence for a PDF exported by Microsoft Word.

This command does not accept a human-authored ``passed`` flag.  It re-opens the
PDF, extracts every page's text, rejects known Word field errors, and binds the
result to exact DOCX/PDF SHA-256 digests.  ``submission_audit.py`` repeats these
checks instead of trusting this report.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from submission_audit import PDF_RENDER_ERROR_PATTERNS, _pdf_layout_evidence
from artifact_io import atomic_write_text, paths_alias
from process_runner import run_process

ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def word_version() -> str | None:
    if platform.system() != "Darwin":
        return None
    try:
        result = run_process(
            ["osascript", "-e", 'tell application "Microsoft Word" to get version'],
            cwd=ROOT, timeout=15,
        )
    except OSError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def inspect_pdf(path: Path) -> dict[str, Any]:
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
    return {
        "path": str(path.resolve()),
        "sha256": sha256(path),
        "page_count": len(pages),
        "text_length": len(text),
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "error_hits": hits[:100],
        "layout": _pdf_layout_evidence(path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("docx", type=Path, help="the exact DOCX opened/rendered in Word")
    parser.add_argument("pdf", type=Path, help="the PDF exported by Microsoft Word")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--word-version", help="override Word version when AppleScript discovery is unavailable")
    args = parser.parse_args()

    if not args.docx.is_file():
        parser.error(f"DOCX does not exist: {args.docx}")
    if not args.pdf.is_file():
        parser.error(f"PDF does not exist: {args.pdf}")
    docx = args.docx.expanduser().resolve()
    pdf_path = args.pdf.expanduser().resolve()
    output = args.out.expanduser().resolve()
    if paths_alias((docx, pdf_path, output)):
        parser.error("DOCX, PDF, and report output must be three distinct paths")

    pdf = inspect_pdf(pdf_path)
    checks = {
        "pdf_readable": pdf["page_count"] > 0,
        "pdf_has_extractable_text": pdf["text_length"] > 0,
        "no_known_word_field_errors": not pdf["error_hits"],
    }
    report = {
        "schema_version": "1.0",
        "evidence_type": "microsoft_word_pdf_render",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "renderer": {"name": "Microsoft Word", "version": args.word_version or word_version()},
        "source_docx": {"path": str(docx), "sha256": sha256(docx)},
        "rendered_pdf": pdf,
        "checks": checks,
        "rendered_verified": all(checks.values()),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(output, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["rendered_verified"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
