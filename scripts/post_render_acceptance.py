#!/usr/bin/env python3
"""Run and independently accept the post-Word release stage.

The deterministic pipeline produces a pre-render DOCX.  Microsoft Word may
rewrite that package while updating fields and exporting PDF, so the final
artifact needs a separate evidence chain.  This command keeps the two stages
explicit:

    pre-render DOCX -> trusted Word export -> final DOCX/PDF/report
                    -> final submission audit -> final format comparison

It never rewrites a pre-render receipt to the post-Word hash.  The resulting
JSON records both hashes and fails closed when any evidence is missing or
bound to the wrong artifact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from artifact_io import atomic_write_text, paths_alias
from process_runner import run_process

ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_record(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    return {"path": str(resolved), "bytes": resolved.stat().st_size, "sha256": sha256(resolved)}


def inspect_docx(path: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            bad_member = archive.testzip()
        missing = sorted({"[Content_Types].xml", "word/document.xml"} - names)
        return {
            **file_record(path),
            "opc_package_valid": not missing and bad_member is None,
            "missing_members": missing,
            "corrupt_member": bad_member,
        }
    except (OSError, zipfile.BadZipFile, ValueError) as exc:
        return {
            "path": str(path.resolve()),
            "bytes": path.stat().st_size if path.exists() else 0,
            "sha256": sha256(path) if path.is_file() else None,
            "opc_package_valid": False,
            "missing_members": [],
            "corrupt_member": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


def read_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def run_step(command: list[str], *, timeout: int = 900) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    try:
        result = run_process(command, cwd=ROOT, timeout=timeout)
        return {
            "returncode": result.returncode,
            "started_at": started.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "command": command,
            "stdout_tail": result.stdout[-3000:],
            "stderr_tail": result.stderr[-3000:],
        }
    except OSError as exc:
        return {
            "returncode": 127,
            "started_at": started.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "command": command,
            "stdout_tail": "",
            "stderr_tail": f"{type(exc).__name__}: {exc}",
        }


def _path(value: Path) -> Path:
    return value.expanduser().resolve()


def _write(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _pre_render_checks(source: Path, validation_path: Path) -> tuple[dict[str, Any], list[str]]:
    checks: dict[str, Any] = {}
    blockers: list[str] = []
    source = _path(source)
    validation_path = _path(validation_path)
    source_artifact = inspect_docx(source) if source.is_file() else {
        "path": str(source), "opc_package_valid": False,
    }
    checks["pre_render_artifact"] = source_artifact
    if not source_artifact.get("opc_package_valid"):
        blockers.append("pre_render_docx_not_valid_opc")
    validation = read_object(validation_path)
    checks["pre_render_validation_report"] = str(validation_path)
    if validation is None:
        blockers.append("pre_render_validation_missing_or_invalid")
        return checks, blockers
    checks["pre_render_validation"] = {
        "valid": validation.get("valid"),
        "format_ready": validation.get("format_ready"),
        "serialized_docx_valid": validation.get("serialized_docx_valid"),
    }
    reported_output = validation.get("output_docx")
    if isinstance(reported_output, str) and _path(Path(reported_output)) != source:
        blockers.append("pre_render_validation_output_path_mismatch")
    if validation.get("valid") is not True or validation.get("format_ready") is not True:
        blockers.append("pre_render_validation_not_passed")
    if validation.get("serialized_docx_valid") is not True:
        blockers.append("pre_render_serialized_docx_not_verified")
    receipt_audit = validation.get("property_receipt_audit")
    if not isinstance(receipt_audit, dict) or receipt_audit.get("valid") is not True:
        blockers.append("pre_render_property_receipts_not_verified")
    else:
        receipts = receipt_audit.get("receipts")
        if not isinstance(receipts, list):
            blockers.append("pre_render_property_receipts_malformed")
        else:
            receipt_hashes = {
                str(item.get("serialized_docx_sha256"))
                for item in receipts if isinstance(item, dict)
            }
            if receipt_hashes and receipt_hashes != {source_artifact.get("sha256")}:
                blockers.append("pre_render_property_receipt_artifact_hash_mismatch")
            checks["pre_render_property_receipt_hashes"] = sorted(receipt_hashes)
    return checks, blockers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_docx", type=Path, help="pre-Word DOCX produced by the deterministic pipeline")
    parser.add_argument("final_docx", type=Path, help="post-Word final DOCX; must differ from source_docx")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--render-report", type=Path, required=True)
    parser.add_argument("--visual-audit", type=Path, required=True,
                        help="independent raster visual-sanity report for the final PDF")
    parser.add_argument("--format-spec", type=Path, required=True)
    parser.add_argument("--pre-validation", type=Path, required=True)
    parser.add_argument("--submission-audit", type=Path, required=True)
    parser.add_argument("--format-comparison", type=Path, required=True)
    parser.add_argument("--format-comparison-markdown", type=Path)
    parser.add_argument("--acceptance-out", type=Path, required=True)
    parser.add_argument("--official-template", type=Path, required=True)
    parser.add_argument("--official-style-map", type=Path, required=True)
    parser.add_argument("--generated-style-map", type=Path, required=True)
    parser.add_argument("--template-profile", type=Path)
    parser.add_argument("--thesis-profile", type=Path)
    parser.add_argument("--requirements-only", action="store_true")
    parser.add_argument("--word-open-timeout", type=int, default=45)
    parser.add_argument("--word-timeout", type=int, default=180)
    args = parser.parse_args(argv)

    source = _path(args.source_docx)
    final = _path(args.final_docx)
    pdf = _path(args.pdf)
    render_report = _path(args.render_report)
    visual_audit = _path(args.visual_audit)
    pre_validation = _path(args.pre_validation)
    submission_audit = _path(args.submission_audit)
    comparison = _path(args.format_comparison)
    comparison_markdown = _path(args.format_comparison_markdown) if args.format_comparison_markdown else None
    acceptance_out = _path(args.acceptance_out)
    format_spec = _path(args.format_spec)
    official_template = _path(args.official_template)
    official_style_map = _path(args.official_style_map)
    generated_style_map = _path(args.generated_style_map)

    case_root = source.parent.resolve()
    output_scope = {
        "final_docx": final,
        "pdf": pdf,
        "render_report": render_report,
        "visual_audit": visual_audit,
        "submission_audit": submission_audit,
        "format_comparison": comparison,
        "acceptance_out": acceptance_out,
    }
    for label, path in output_scope.items():
        if path == case_root or case_root not in path.parents:
            parser.error(f"{label} must remain inside the case output directory: {path}")

    if not source.is_file():
        parser.error(f"pre-Word DOCX does not exist: {source}")
    for label, path in (
        ("format spec", format_spec), ("pre-render validation", pre_validation),
        ("official template", official_template), ("official style map", official_style_map),
        ("generated style map", generated_style_map),
    ):
        if not path.is_file():
            parser.error(f"{label} does not exist: {path}")
    output_paths = [source, final, pdf, render_report, submission_audit, comparison, acceptance_out]
    if comparison_markdown:
        output_paths.append(comparison_markdown)
    input_paths = [pre_validation, format_spec, official_template, official_style_map, generated_style_map]
    if args.template_profile:
        input_paths.append(_path(args.template_profile))
    if args.thesis_profile:
        input_paths.append(_path(args.thesis_profile))
    if paths_alias(output_paths + input_paths):
        parser.error("source DOCX, final DOCX, PDF, reports, and acceptance output must be distinct")
    if source == final:
        parser.error("final DOCX must differ from source DOCX")

    checks, blockers = _pre_render_checks(source, pre_validation)
    if blockers:
        payload = {
            "schema_version": "1.0",
            "status": "blocked",
            "artifact_stage": "post_word_render",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "pre_render_docx": str(source),
            "pre_render_docx_sha256": checks.get("pre_render_artifact", {}).get("sha256"),
            "blockers": sorted(set(blockers)),
            "checks": checks,
        }
        _write(acceptance_out, payload)
        print(json.dumps(payload, ensure_ascii=False))
        return 2

    render_cmd = [
        sys.executable, str(ROOT / "scripts" / "word_render_export.py"),
        str(source), str(final), str(pdf), "--report", str(render_report),
        "--open-timeout", str(args.word_open_timeout), "--word-timeout", str(args.word_timeout),
    ]
    render_step = run_step(render_cmd)
    checks["word_render"] = render_step
    if render_step["returncode"] != 0:
        blockers.append("word_render_failed")

    if render_step["returncode"] == 0:
        visual_cmd = [
            sys.executable, str(ROOT / "scripts" / "pdf_visual_audit.py"),
            str(pdf), "--out", str(visual_audit),
        ]
        visual_step = run_step(visual_cmd)
        checks["pdf_visual_audit_step"] = visual_step
        if visual_step["returncode"] != 0:
            blockers.append("pdf_visual_audit_failed")

    if render_step["returncode"] == 0:
        audit_cmd = [
            sys.executable, str(ROOT / "scripts" / "submission_audit.py"), str(final),
            "--format-spec", str(format_spec), "--render-report", str(render_report),
            "--out", str(submission_audit),
        ]
        if args.template_profile:
            audit_cmd += ["--template-profile", str(_path(args.template_profile))]
        if args.thesis_profile:
            audit_cmd += ["--thesis-profile", str(_path(args.thesis_profile))]
        audit_step = run_step(audit_cmd)
        checks["submission_audit_step"] = audit_step
        if audit_step["returncode"] not in {0, 2}:
            blockers.append("post_render_submission_audit_execution_failed")

        comparison_cmd = [
            sys.executable, str(ROOT / "scripts" / "post_generation_format_audit.py"), str(final),
            "--official-template", str(official_template), "--format-spec", str(format_spec),
            "--official-style-map", str(official_style_map),
            "--generated-style-map", str(generated_style_map),
            "--out", str(comparison), "--strict",
        ]
        if comparison_markdown:
            comparison_cmd += ["--markdown", str(comparison_markdown)]
        if args.requirements_only:
            comparison_cmd.append("--requirements-only")
        comparison_step = run_step(comparison_cmd)
        checks["post_render_format_comparison_step"] = comparison_step
        if comparison_step["returncode"] != 0:
            blockers.append("post_render_format_comparison_failed")

    final_artifact = inspect_docx(final) if final.is_file() else {
        "path": str(final), "opc_package_valid": False, "sha256": None,
    }
    checks["post_render_artifact"] = final_artifact
    if not final_artifact.get("opc_package_valid"):
        blockers.append("post_render_docx_not_valid_opc")
    pdf_artifact = file_record(pdf) if pdf.is_file() else {
        "path": str(pdf), "bytes": 0, "sha256": None,
    }
    checks["post_render_pdf"] = pdf_artifact
    if not pdf.is_file() or pdf.stat().st_size <= 0:
        blockers.append("post_render_pdf_missing")

    render = read_object(render_report)
    checks["render_report"] = str(render_report)
    if not isinstance(render, dict):
        blockers.append("render_report_missing_or_invalid")
    else:
        source_record = render.get("source_docx") if isinstance(render.get("source_docx"), dict) else {}
        rendered_record = render.get("rendered_pdf") if isinstance(render.get("rendered_pdf"), dict) else {}
        if source_record.get("sha256") != final_artifact.get("sha256"):
            blockers.append("render_report_final_docx_hash_mismatch")
        if rendered_record.get("sha256") != (sha256(pdf) if pdf.is_file() else None):
            blockers.append("render_report_pdf_hash_mismatch")

    visual = read_object(visual_audit)
    checks["pdf_visual_audit"] = str(visual_audit)
    if not isinstance(visual, dict):
        blockers.append("pdf_visual_audit_missing_or_invalid")
    else:
        if visual.get("status") != "passed":
            blockers.append("pdf_visual_audit_not_passed")
        visual_pdf = visual.get("pdf") if isinstance(visual.get("pdf"), dict) else {}
        if visual_pdf.get("sha256") != (sha256(pdf) if pdf.is_file() else None):
            blockers.append("pdf_visual_audit_hash_mismatch")

    audit = read_object(submission_audit)
    checks["post_render_submission_audit"] = str(submission_audit)
    if not isinstance(audit, dict):
        blockers.append("post_render_submission_audit_missing_or_invalid")
    else:
        if audit.get("submission_ready") is not True:
            blockers.append("post_render_submission_not_ready")
        render_validation = audit.get("render_validation")
        if not isinstance(render_validation, dict) or render_validation.get("rendered_verified") is not True:
            blockers.append("post_render_trusted_render_not_verified")

    final_comparison = read_object(comparison)
    checks["post_render_format_comparison"] = str(comparison)
    if not isinstance(final_comparison, dict):
        blockers.append("post_render_format_comparison_missing_or_invalid")
    else:
        if final_comparison.get("status") != "passed":
            blockers.append("post_render_format_comparison_not_passed")
        generated_inputs = final_comparison.get("inputs") if isinstance(final_comparison.get("inputs"), dict) else {}
        if generated_inputs.get("generated_docx_sha256") != final_artifact.get("sha256"):
            blockers.append("post_render_comparison_artifact_hash_mismatch")

    payload = {
        "schema_version": "1.0",
        "status": "accepted" if not blockers else "blocked",
        "artifact_stage": "post_word_render",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "pre_render_docx": str(source),
        "pre_render_docx_sha256": checks.get("pre_render_artifact", {}).get("sha256"),
        "post_render_docx": str(final),
        "post_render_docx_sha256": final_artifact.get("sha256"),
        "rendered_pdf": str(pdf),
        "rendered_pdf_sha256": pdf_artifact.get("sha256"),
        "render_report": str(render_report),
        "visual_audit": str(visual_audit),
        "submission_audit": str(submission_audit),
        "format_comparison": str(comparison),
        "blockers": sorted(set(blockers)),
        "checks": checks,
        "policy": {
            "pre_render_receipts_are_not_final_receipts": True,
            "final_docx_requires_independent_word_render_and_post_audit": True,
            "fail_closed": True,
        },
    }
    _write(acceptance_out, payload)
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload["status"] == "accepted" else 2


if __name__ == "__main__":
    raise SystemExit(main())
