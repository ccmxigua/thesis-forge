"""Local HMAC receipts for the trusted Microsoft Word export entry point."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
from pathlib import Path
from typing import Any


def default_key_path() -> Path:
    return Path.home() / ".config/thesis-latex2docx/word-export.key"


def load_key(*, create: bool = False) -> bytes | None:
    path = default_key_path()
    if path.is_file():
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise PermissionError(f"Word export attestation key must not be group/world accessible: {path}")
        key = path.read_bytes()
        if len(key) < 32:
            raise ValueError(f"Word export attestation key is too short: {path}")
        return key
    if not create:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    key = os.urandom(32)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return load_key(create=False)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(key)
    return key


def payload(report: dict[str, Any]) -> dict[str, Any]:
    rendered = report.get("rendered_pdf", {})
    return {
        "schema_version": report.get("schema_version"),
        "evidence_type": report.get("evidence_type"),
        "renderer": report.get("renderer"),
        "source_docx_sha256": report.get("source_docx", {}).get("sha256"),
        "rendered_pdf_sha256": rendered.get("sha256"),
        "rendered_pdf_text_sha256": rendered.get("text_sha256"),
        "rendered_pdf_page_count": rendered.get("page_count"),
        "word_export": report.get("word_export"),
    }


def sign(report: dict[str, Any], key: bytes) -> str:
    message = json.dumps(payload(report), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def verify(report: dict[str, Any], key: bytes) -> bool:
    attestation = report.get("attestation", {})
    if not isinstance(attestation, dict):
        return False
    if attestation.get("algorithm") != "HMAC-SHA256" or attestation.get("scope") != "local_word_export_v1":
        return False
    signature = attestation.get("signature")
    return bool(signature and hmac.compare_digest(str(signature), sign(report, key)))
