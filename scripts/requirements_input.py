#!/usr/bin/env python3
"""Normalize written-requirements inputs without interpreting their meaning.

Legacy binary Word ``.doc`` files are converted into a fresh, isolated DOCX
artifact for each invocation.  This module deliberately performs no semantic
parsing: it records byte identities and converter provenance, then validates
only that the result is a readable WordprocessingML package.
"""
from __future__ import annotations

import hashlib
import json
import platform
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
import xml.etree.ElementTree as ET


WORDPROCESSINGML_MAIN = (
    "application/vnd.openxmlformats-officedocument."
    "wordprocessingml.document.main+xml"
)
CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


class RequirementsInputError(ValueError):
    """Raised when a requirements input cannot be normalized safely."""


@dataclass(frozen=True)
class Converter:
    tool: str
    kind: str
    path: Path
    version: str


@dataclass(frozen=True)
class NormalizedRequirements:
    original_path: Path
    normalized_path: Path
    manifest_path: Path
    manifest: dict[str, Any]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_record(path: Path, *, kind: str | None = None) -> dict[str, Any]:
    resolved = path.resolve()
    suffix = resolved.suffix.lower()
    return {
        "path": str(resolved),
        "suffix": suffix,
        "kind": kind or suffix.lstrip("."),
        "bytes": resolved.stat().st_size,
        "sha256": _sha256(resolved),
    }


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _soffice_version(path: Path) -> str:
    try:
        result = subprocess.run(
            [str(path), "--version"], text=True, capture_output=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return (result.stdout.strip() or result.stderr.strip() or "unknown").splitlines()[0]


def _discover_converters() -> list[Converter]:
    """Return deterministic local converters in preferred order."""
    candidates: list[tuple[str, str, str | None]] = [
        ("libreoffice", "soffice", shutil.which("soffice")),
        ("libreoffice", "soffice", shutil.which("libreoffice")),
        ("libreoffice", "soffice", "/opt/homebrew/bin/soffice"),
        ("libreoffice", "soffice", "/usr/local/bin/soffice"),
        (
            "libreoffice", "soffice",
            "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        ),
        ("apple_textutil", "textutil", "/usr/bin/textutil"),
    ]
    found: list[Converter] = []
    seen: set[Path] = set()
    for tool, kind, raw_path in candidates:
        if not raw_path:
            continue
        path = Path(raw_path).resolve()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        version = (
            _soffice_version(path)
            if kind == "soffice"
            else f"macOS {platform.mac_ver()[0] or platform.release()} system textutil"
        )
        found.append(Converter(tool=tool, kind=kind, path=path, version=version))
    return found


def _validate_docx(path: Path) -> dict[str, Any]:
    """Validate the minimum package and XML invariants required downstream."""
    if not path.is_file():
        raise RequirementsInputError(f"normalized DOCX was not created: {path}")
    if not zipfile.is_zipfile(path):
        raise RequirementsInputError(f"normalized file is not a ZIP-based DOCX: {path}")
    try:
        with zipfile.ZipFile(path) as package:
            corrupt_member = package.testzip()
            if corrupt_member:
                raise RequirementsInputError(
                    f"normalized DOCX contains a corrupt ZIP member: {corrupt_member}"
                )
            names = set(package.namelist())
            required = {"[Content_Types].xml", "_rels/.rels", "word/document.xml"}
            missing = sorted(required - names)
            if missing:
                raise RequirementsInputError(
                    "normalized DOCX is missing required OOXML members: "
                    + ", ".join(missing)
                )
            parsed_xml: dict[str, ET.Element] = {}
            for member in sorted(
                name for name in names if name.endswith(".xml") or name.endswith(".rels")
            ):
                try:
                    parsed_xml[member] = ET.fromstring(package.read(member))
                except ET.ParseError as exc:
                    raise RequirementsInputError(
                        f"normalized DOCX contains unreadable XML in {member}: {exc}"
                    ) from exc
            content_types = parsed_xml["[Content_Types].xml"]
            document = parsed_xml["word/document.xml"]
    except RequirementsInputError:
        raise
    except (OSError, zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise RequirementsInputError(f"normalized DOCX is not readable: {exc}") from exc

    overrides = {
        (item.get("PartName"), item.get("ContentType"))
        for item in content_types.findall(f"{{{CONTENT_TYPES_NS}}}Override")
    }
    if ("/word/document.xml", WORDPROCESSINGML_MAIN) not in overrides:
        raise RequirementsInputError(
            "normalized package does not declare a standard DOCX main document"
        )
    if document.tag != f"{{{WORD_NS}}}document":
        raise RequirementsInputError(
            "normalized word/document.xml is not a WordprocessingML document"
        )
    return {
        "status": "valid",
        "checks": [
            "zip_integrity",
            "required_ooxml_members",
            "all_ooxml_xml_readable",
            "docx_main_content_type",
            "wordprocessingml_document_root",
        ],
        "xml_parts_parsed": len(parsed_xml),
    }


def _converter_command(
    converter: Converter, source: Path, run_dir: Path, normalized_path: Path,
) -> tuple[list[str], Path]:
    if converter.kind == "soffice":
        profile_dir = run_dir / "libreoffice-profile"
        command = [
            str(converter.path),
            "--headless",
            f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
            "--convert-to", "docx",
            "--outdir", str(run_dir),
            str(source),
        ]
        return command, run_dir / f"{source.stem}.docx"
    if converter.kind == "textutil":
        command = [
            str(converter.path), "-convert", "docx",
            "-output", str(normalized_path), str(source),
        ]
        return command, normalized_path
    raise RequirementsInputError(f"unsupported converter kind: {converter.kind}")


def normalize_requirements_input(
    source: Path,
    work_root: Path,
    manifest_path: Path,
) -> NormalizedRequirements:
    """Return a validated DOCX plus a complete, persistent provenance manifest."""
    original = source.expanduser().resolve()
    normalization_id = str(uuid4())
    run_dir = work_root.resolve() / normalization_id
    manifest: dict[str, Any] = {
        "schema_version": "1.0",
        "normalization_id": normalization_id,
        "started_at": _now(),
        "status": "running",
        "conversion_status": "not_started",
        "artifact_reused": False,
        "working_directory": str(run_dir),
        "original": {
            "path": str(original),
            "suffix": original.suffix.lower(),
            "kind": original.suffix.lower().lstrip("."),
        },
        "converter": None,
        "conversion_attempts": [],
        "normalized": None,
        "validation": {"status": "not_run"},
    }

    def fail(message: str) -> None:
        manifest.update(
            status="failed",
            conversion_status="failed",
            completed_at=_now(),
            error=message,
        )
        _write_manifest(manifest_path, manifest)
        raise RequirementsInputError(message)

    if not original.is_file():
        fail(f"requirements input does not exist or is not a file: {original}")
    suffix = original.suffix.lower()
    if suffix not in {".doc", ".docx"}:
        fail(
            "requirements input must be a .doc or .docx Word file; "
            f"received {suffix or '<no suffix>'}: {original}"
        )
    manifest["original"] = _file_record(original, kind=suffix.lstrip("."))

    if suffix == ".docx":
        try:
            validation = _validate_docx(original)
        except RequirementsInputError as exc:
            manifest["validation"] = {"status": "invalid", "error": str(exc)}
            fail(
                f"requirements DOCX is not a valid readable OOXML document: {exc}. "
                "Replace it with an uncorrupted .doc/.docx file and retry; the original was not modified."
            )
        current_original = _file_record(original, kind="docx")
        if current_original != manifest["original"]:
            fail(
                "requirements input changed while it was being validated; retry with a stable file. "
                "The original was not modified by this tool."
            )
        manifest.update(
            status="completed",
            conversion_status="not_required",
            completed_at=_now(),
            normalized=current_original,
            validation=validation,
        )
        _write_manifest(manifest_path, manifest)
        return NormalizedRequirements(original, original, manifest_path.resolve(), manifest)

    converters = _discover_converters()
    if not converters:
        fail(
            "cannot normalize legacy .doc requirements because no supported local converter "
            "was found. Install LibreOffice (soffice), or use macOS /usr/bin/textutil, then retry; "
            "the original was not modified."
        )

    run_dir.mkdir(parents=True, exist_ok=False)
    normalized_path = run_dir / "requirements.normalized.docx"
    final_error = "conversion did not produce a readable DOCX"
    for converter in converters:
        command, produced_path = _converter_command(
            converter, original, run_dir, normalized_path,
        )
        attempt: dict[str, Any] = {
            "tool": converter.tool,
            "path": str(converter.path),
            "version": converter.version,
            "command": command,
        }
        try:
            result = subprocess.run(
                command, cwd=run_dir, text=True, capture_output=True, timeout=180,
            )
            attempt.update(
                returncode=result.returncode,
                stdout=result.stdout.strip(),
                stderr=result.stderr.strip(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            attempt.update(returncode=None, error=str(exc))
            manifest["conversion_attempts"].append(attempt)
            final_error = f"{converter.tool} could not be executed: {exc}"
            continue
        manifest["conversion_attempts"].append(attempt)
        if result.returncode != 0:
            final_error = (
                f"{converter.tool} exited with status {result.returncode}: "
                f"{result.stderr.strip() or result.stdout.strip() or 'no diagnostic output'}"
            )
            continue
        if produced_path != normalized_path:
            if not produced_path.is_file():
                final_error = f"{converter.tool} reported success but created no DOCX output"
                continue
            produced_path.replace(normalized_path)
        try:
            validation = _validate_docx(normalized_path)
        except RequirementsInputError as exc:
            attempt["validation_error"] = str(exc)
            final_error = f"{converter.tool} produced an invalid DOCX: {exc}"
            if normalized_path.exists():
                normalized_path.unlink()
            continue
        current_original = _file_record(original, kind="doc")
        if current_original != manifest["original"]:
            if normalized_path.exists():
                normalized_path.unlink()
            fail(
                "requirements input changed during legacy .doc conversion; retry with a stable file. "
                "The original was not modified by this tool."
            )
        manifest.update(
            status="completed",
            conversion_status="converted",
            completed_at=_now(),
            converter={
                "tool": converter.tool,
                "path": str(converter.path),
                "version": converter.version,
                "command": command,
                "returncode": result.returncode,
            },
            normalized=_file_record(normalized_path, kind="docx"),
            validation=validation,
        )
        _write_manifest(manifest_path, manifest)
        return NormalizedRequirements(
            original, normalized_path.resolve(), manifest_path.resolve(), manifest,
        )

    fail(
        "legacy .doc requirements conversion failed closed: "
        f"{final_error}. Check that the file is readable and not password-protected, "
        "then retry with a supported local converter; the original was not modified."
    )
