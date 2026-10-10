#!/usr/bin/env python3
"""Create a new deterministic format-repair child of an existing review draft.

This route makes no model request and never edits its source or parent inputs.
It is for post-review deterministic formatting repairs only; lineage explicitly
retains the parent semantic-review identity and the result remains a review draft.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from docx import Document

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from apply_format_spec import (  # noqa: E402
    apply_drawing_line_box_safety,
    apply_table_rules,
    audit_caption_separators,
    audit_drawing_line_boxes,
    audit_table_rules,
    caption_separator_observations,
    normalize_caption_separators,
    table_border_observations,
)
from draft_scorecard import audit_scorecard  # noqa: E402
from format_spec_validation import load_and_validate  # noqa: E402
from manual_review_display import audit_manual_review_markers  # noqa: E402
from semantic_contract import sha256_json, strict_json_read  # noqa: E402
from source_table_contract import (  # noqa: E402
    compile_source_table_contract,
    resolve_bound_requirement_docx,
    sha256_file,
)


CODE_FILES = (
    "schema/format-spec.schema.json",
    "scripts/apply_format_spec.py",
    "scripts/source_table_contract.py",
    "scripts/derive_review_draft_repairs.py",
    "scripts/rendered_format_audit.py",
    "scripts/toc_materializer.py",
    "scripts/audit_toc_materialization.py",
    "scripts/draft_scorecard.py",
    "scripts/submission_audit.py",
)


def _json(path: Path) -> Any:
    return strict_json_read(path)


def _code_identity() -> dict[str, Any]:
    files = {name: sha256_file(ROOT / name) for name in CODE_FILES}
    return {"files": files, "sha256": sha256_json(files)}


def _same_file(path: Path, other: Path) -> bool:
    if path.resolve() == other.resolve():
        return True
    try:
        return path.exists() and other.exists() and os.path.samefile(path, other)
    except OSError:
        return False


def _require_inputs(paths: list[Path], output: Path) -> None:
    for path in paths:
        if not path.is_file():
            raise ValueError(f"required input is not a file: {path}")
        if _same_file(path, output):
            raise ValueError(f"output aliases immutable input: {path}")
    if output.exists():
        raise ValueError(f"refusing to overwrite existing output: {output}")


def _verify_parent_lineage(source: Path, spec_path: Path,
                           extraction_manifest: dict[str, Any], card: dict[str, Any],
                           parent_docx: Path) -> list[str]:
    errors: list[str] = []
    binding = card.get("binding") if isinstance(card, dict) else None
    if not isinstance(binding, dict):
        return ["parent_scorecard_binding_missing"]
    if binding.get("run_id") != extraction_manifest.get("run_id"):
        errors.append("parent_scorecard_run_id_mismatch")
    source_sha = sha256_file(source)
    if binding.get("input_source_sha256") != source_sha:
        errors.append("parent_scorecard_thesis_source_hash_mismatch")
    requirements_source_sha = (extraction_manifest.get("normalized_source_sha256")
                               or extraction_manifest.get("source_sha256"))
    if binding.get("source_sha256") != requirements_source_sha:
        errors.append("parent_scorecard_requirements_source_hash_mismatch")
    if binding.get("requirements_sha256") != requirements_source_sha:
        errors.append("parent_scorecard_requirements_hash_mismatch")
    if binding.get("format_spec_sha256") != sha256_file(spec_path):
        errors.append("parent_scorecard_format_spec_hash_mismatch")
    scorecard_audit = audit_scorecard(parent_docx, card)
    if scorecard_audit.get("valid") is not True:
        errors.append("parent_scorecard_document_display_audit_failed")
    return errors


def derive(source_docx: Path, parent_docx: Path, format_spec_path: Path,
           clauses_path: Path, extraction_manifest_path: Path,
           style_map_path: Path, scorecard_path: Path,
           manual_review_ledger_path: Path, output_path: Path,
           report_path: Path) -> dict[str, Any]:
    inputs = [source_docx, parent_docx, format_spec_path, clauses_path,
              extraction_manifest_path, style_map_path, scorecard_path,
              manual_review_ledger_path]
    _require_inputs(inputs, output_path)
    if report_path.resolve() == output_path.resolve() or report_path.exists():
        raise ValueError("repair report must use a fresh path distinct from DOCX output")
    if any(_same_file(report_path, path) for path in inputs):
        raise ValueError("repair report aliases immutable input")
    scorecard_copy_path = report_path.parent / "inherited-draft-scorecard.json"
    if scorecard_copy_path.exists() or any(_same_file(scorecard_copy_path, path) for path in inputs):
        raise ValueError("inherited scorecard copy path is not fresh")
    original_hashes = {str(path.resolve()): sha256_file(path) for path in inputs}

    spec = _json(format_spec_path)
    schema_errors = load_and_validate(
        spec, ROOT / "schema" / "format-spec.schema.json",
        allow_missing_required_metadata=True,
    )
    if schema_errors:
        raise ValueError("format spec schema validation failed: " + "; ".join(schema_errors[:10]))
    extraction = _json(extraction_manifest_path)
    card = _json(scorecard_path)
    ledger = _json(manual_review_ledger_path)
    card_binding = card.get("binding") if isinstance(card, dict) else None
    ledger_binding = ledger.get("binding") if isinstance(ledger, dict) else None
    if not isinstance(card_binding, dict) or not isinstance(ledger_binding, dict):
        raise ValueError("parent scorecard/manual-review ledger binding is missing")
    for key in ("run_id", "case_id", "source_sha256", "format_spec_sha256"):
        if card_binding.get(key) != ledger_binding.get(key):
            raise ValueError(f"parent scorecard/manual-review ledger {key} mismatch")
    lineage_errors = _verify_parent_lineage(
        source_docx, format_spec_path, extraction, card, parent_docx,
    )
    if lineage_errors:
        raise ValueError("parent review lineage is not bound: " + "; ".join(lineage_errors))
    source_requirements_docx, source_binding = resolve_bound_requirement_docx(
        extraction_manifest_path, clauses_path, format_spec_path,
    )
    if source_requirements_docx is None:
        raise ValueError("requirement source binding failed: " + json.dumps(source_binding, ensure_ascii=False))
    clauses_data = _json(clauses_path)
    clauses = clauses_data if isinstance(clauses_data, list) else clauses_data.get("clauses", [])
    source_table_contract = compile_source_table_contract(
        source_requirements_docx, clauses, spec, source_binding=source_binding,
    )
    if source_table_contract.get("status") == "blocked":
        raise ValueError("source table contract failed closed: "
                         + json.dumps(source_table_contract.get("findings", []), ensure_ascii=False))
    schema_errors = load_and_validate(
        spec, ROOT / "schema" / "format-spec.schema.json",
        allow_missing_required_metadata=True,
    )
    if schema_errors:
        raise ValueError("compiled format spec schema validation failed: " + "; ".join(schema_errors[:10]))

    manual_before = audit_manual_review_markers(parent_docx, ledger)
    if manual_before.get("valid") is not True:
        raise ValueError("parent manual-review marker audit failed")

    doc = Document(parent_docx)
    mappings_data = _json(style_map_path)
    mappings = mappings_data.get("mappings", mappings_data)
    caption_repairs = normalize_caption_separators(doc, spec.get("roles", {}), mappings)
    table_changes = apply_table_rules(doc, spec.get("tables", {}), mappings)
    drawing_changes = apply_drawing_line_box_safety(doc)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(output_path)
    final_doc = Document(output_path)
    caption_findings = audit_caption_separators(final_doc, spec.get("roles", {}), mappings)
    table_findings = audit_table_rules(final_doc, spec.get("tables", {}), mappings=mappings)
    drawing_findings = audit_drawing_line_boxes(final_doc)
    table_observations = table_border_observations(final_doc, spec.get("tables", {}), mappings)
    manual_after = audit_manual_review_markers(output_path, ledger)
    scorecard_after = audit_scorecard(output_path, card)
    findings = [*caption_findings, *table_findings, *drawing_findings]
    if manual_after.get("valid") is not True:
        findings.append({"code": "manual_review_marker_audit_failed", "audit": manual_after})
    if scorecard_after.get("valid") is not True:
        findings.append({"code": "scorecard_display_audit_failed", "audit": scorecard_after})
    final_hashes = {str(path.resolve()): sha256_file(path) for path in inputs}
    if final_hashes != original_hashes:
        findings.append({"code": "immutable_input_hash_changed",
                         "before": original_hashes, "after": final_hashes})

    report: dict[str, Any] = {
        "schema_version": "1.0",
        "protocol": "deterministic_review_draft_format_repair_v1",
        "status": "passed" if not findings else "issues_found",
        "submission_ready": False,
        "model_request_made": False,
        "semantic_review_is_new": False,
        "semantic_review_lineage": {
            "status": "inherited_from_parent_review_draft",
            "run_id": card["binding"]["run_id"],
            "case_id": card["binding"]["case_id"],
            "response_copied_or_rebound": False,
            "parent_scorecard_sha256": sha256_file(scorecard_path),
            "parent_docx_sha256": sha256_file(parent_docx),
        },
        "source_thesis": {"path": str(source_docx.resolve()), "bytes": source_docx.stat().st_size,
                           "sha256": sha256_file(source_docx)},
        "source_requirements": {"path": str(source_requirements_docx.resolve()),
                                 "bytes": source_requirements_docx.stat().st_size,
                                 "sha256": sha256_file(source_requirements_docx),
                                 "extraction_manifest_sha256": sha256_file(extraction_manifest_path),
                                 "clauses_sha256": sha256_file(clauses_path),
                                 "format_spec_sha256": sha256_file(format_spec_path),
                                 "source_binding": source_binding},
        "style_map_sha256": sha256_file(style_map_path),
        "manual_review_ledger_sha256": sha256_file(manual_review_ledger_path),
        "parent_docx_sha256": sha256_file(parent_docx),
        "output_docx": {"path": str(output_path.resolve()), "bytes": output_path.stat().st_size,
                        "sha256": sha256_file(output_path)},
        "compiled_source_table_contract": source_table_contract,
        "table_repairs": table_changes,
        "table_border_observations": table_observations,
        "caption_separator_repairs": caption_repairs,
        "caption_separator_observations": caption_separator_observations(
            final_doc, spec.get("roles", {}), mappings,
        ),
        "drawing_line_box_repairs": drawing_changes,
        "audits": {
            "caption_separators": {"valid": not caption_findings, "findings": caption_findings},
            "table_rules": {"valid": not table_findings, "findings": table_findings},
            "drawing_line_boxes": {"valid": not drawing_findings, "findings": drawing_findings},
            "manual_review_markers": manual_after,
            "parent_scorecard_display": scorecard_after,
        },
        "toc_page_cache_status": "not_refreshed_here; run the iterative TOC materializer after repairs",
        "code_identity": _code_identity(),
        "input_hashes_unchanged": original_hashes == final_hashes,
        "findings": findings,
    }
    report["audit_sha256"] = sha256_json(report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    shutil.copyfile(scorecard_path, scorecard_copy_path)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_thesis_docx", type=Path)
    parser.add_argument("parent_review_docx", type=Path)
    parser.add_argument("format_spec", type=Path)
    parser.add_argument("requirement_clauses", type=Path)
    parser.add_argument("extraction_manifest", type=Path)
    parser.add_argument("style_map", type=Path)
    parser.add_argument("parent_scorecard", type=Path)
    parser.add_argument("manual_review_ledger", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = derive(
            args.source_thesis_docx, args.parent_review_docx, args.format_spec,
            args.requirement_clauses, args.extraction_manifest, args.style_map,
            args.parent_scorecard, args.manual_review_ledger, args.out, args.report,
        )
    except Exception as exc:
        print(f"review draft repair blocked: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
