#!/usr/bin/env python3
"""Reconcile a derived review draft against its exact current DOCX/PDF bytes.

This entry point consumes already-created artifacts. It does not run models,
source extraction, formatting, or document conversion. If the scorecard display
changes the DOCX, it exits 2 and requires a fresh render and audit before the
final current-output report is accepted.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from compliance import finalize_records, reconcile_rendered_output_records, summarize
from draft_scorecard import attach_rendered_output_audit
from semantic_contract import sha256_json, strict_json_read


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    temp_path = Path(temporary)
    try:
        temp_path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def reconcile(
    historical_clause_report_path: Path,
    format_spec_path: Path,
    style_map_path: Path,
    property_receipt_audit_path: Path,
    rendered_report_path: Path,
    final_docx_path: Path,
    scorecard_path: Path,
    output_path: Path,
    *,
    historical_docx_sha256: str | None = None,
) -> tuple[dict[str, Any], int]:
    inputs = [historical_clause_report_path, format_spec_path, style_map_path,
              property_receipt_audit_path, rendered_report_path, final_docx_path,
              scorecard_path]
    resolved = [path.resolve() for path in inputs]
    if len(set(resolved)) != len(resolved) or output_path.resolve() in set(resolved):
        raise ValueError("current-output inputs and report output must be distinct files")
    for path in inputs:
        if not path.is_file():
            raise FileNotFoundError(path)

    historical = strict_json_read(historical_clause_report_path)
    format_spec = strict_json_read(format_spec_path)
    style_map = strict_json_read(style_map_path)
    receipt_audit = strict_json_read(property_receipt_audit_path)
    rendered_report = strict_json_read(rendered_report_path)
    if not isinstance(historical, dict) or not isinstance(historical.get("records"), list):
        raise ValueError("historical clause report must contain its original records list")
    if not isinstance(format_spec, dict) or not isinstance(format_spec.get("requirements"), list):
        raise ValueError("format spec must contain the accepted normalized requirements")
    if not isinstance(receipt_audit, dict) or not isinstance(receipt_audit.get("receipts"), list):
        raise ValueError("property receipt audit must contain its original receipt list")
    if not isinstance(style_map, dict):
        raise ValueError("style map is not an object")
    mappings = style_map.get("mappings", style_map)
    if not isinstance(mappings, dict):
        raise ValueError("style map mappings are invalid")

    current_docx_sha = sha256_file(final_docx_path)
    source_records = copy.deepcopy(historical["records"])
    for record in source_records:
        if not isinstance(record, dict) or not isinstance(record.get("status"), str):
            raise ValueError("historical clause report contains an invalid record")
        record.setdefault("historical_status", record["status"])
        if "current_output_instance" in record:
            raise ValueError("input records are already reconciled; use the immutable historical report")
    current_records = finalize_records(
        source_records, format_spec["requirements"], {}, set(),
        current_docx_sha256=current_docx_sha,
        property_receipt_audit=receipt_audit,
        role_mappings=mappings,
        promote_pending=False,
    )
    rendered = reconcile_rendered_output_records(
        current_records, format_spec["requirements"], mappings,
        rendered_report, current_docx_sha,
    )
    mode = historical.get("mode", "full")
    summary = summarize(rendered["records"], mode=mode, phase="formatting")
    summary["docx_fully_compliant"] = False
    summary["submission_ready"] = False
    summary["overall_status"] = "review_draft_pending"
    counts = summary["docx_compliance"]["counts"]
    current_instance_records = [item for item in rendered["records"]
                                if isinstance(item.get("current_output_instance"), dict)]
    instance_counts = {
        state: sum(item["current_output_instance"].get("status") == state
                   for item in current_instance_records)
        for state in ("verified", "failed", "unverified")
    }
    rendered_failed = any(item.get("status") == "failed"
                          for item in (*rendered["bound_rendered_findings"],
                                       *rendered["unbound_rendered_findings"]))
    rendered_unknown = any(item.get("status") == "unverified"
                           for item in (*rendered["bound_rendered_findings"],
                                        *rendered["unbound_rendered_findings"]))
    if instance_counts["failed"] or rendered_failed:
        current_status = "issues_found"
    elif instance_counts["unverified"] or rendered_unknown:
        current_status = "unverified"
    else:
        current_status = "passed"

    scorecard_binding = strict_json_read(scorecard_path).get("binding", {})
    lineage = historical.get("semantic_review_lineage")
    if not isinstance(lineage, dict):
        lineage = {
            "status": "inherited_semantic_review",
            "run_id": scorecard_binding.get("run_id") if isinstance(scorecard_binding, dict) else None,
            "case_id": scorecard_binding.get("case_id") if isinstance(scorecard_binding, dict) else None,
            "response_copied_or_rebound": False,
        }
    current_receipts = [item for item in receipt_audit["receipts"]
                        if isinstance(item, dict)
                        and item.get("serialized_docx_sha256") == current_docx_sha]
    receipt_counts = {
        state: sum(item.get("status") == state for item in current_receipts)
        for state in ("verified", "failed", "unverified")
    }

    report: dict[str, Any] = {
        "schema_version": "1.0",
        "protocol": "current_output_compliance_reconciliation_v1",
        "status": "in_progress",
        "current_output_status": current_status,
        "submission_ready": False,
        "model_request_made": False,
        "semantic_review_is_new": False,
        "semantic_review_lineage": copy.deepcopy(lineage),
        "historical_semantic_report": {
            "path": str(historical_clause_report_path.resolve()),
            "sha256": sha256_file(historical_clause_report_path),
            "record_count": len(historical["records"]),
            "original_summary": {key: copy.deepcopy(historical.get(key)) for key in (
                "overall_status", "docx_fully_compliant", "docx_compliance")},
        },
        "current_output": {
            "docx_path": str(final_docx_path.resolve()),
            "docx_sha256": current_docx_sha,
            "pdf_sha256": rendered_report.get("pdf_sha256"),
            "rendered_report_sha256": sha256_json(rendered_report),
            "property_receipt_audit_sha256": sha256_file(property_receipt_audit_path),
            "current_docx_bound_receipt_counts": receipt_counts,
            "scorecard_path": str(scorecard_path.resolve()),
            "historical_docx_sha256": historical_docx_sha256,
            "clause_record_status_counts": counts,
            "current_instance_record_status_counts": instance_counts,
            "historical_clause_record_status_counts": historical.get("docx_compliance", {}).get("counts"),
            "submission_ready": False,
            "field_refresh_claimed": False,
        },
        "historical_records": historical["records"],
        "records": rendered["records"],
        "bound_rendered_findings": rendered["bound_rendered_findings"],
        "unbound_rendered_findings": rendered["unbound_rendered_findings"],
        "summary": summary,
    }

    result_code = 0
    try:
        validated_finding_bindings = {
            sha256_json(item["finding"]): list(item["requirement_ids"])
            for item in rendered["bound_rendered_findings"]
            if isinstance(item.get("finding"), dict)
            and isinstance(item.get("requirement_ids"), list)
        }
        card = attach_rendered_output_audit(
            scorecard_path, rendered_report, final_docx_path, receipt_audit,
            historical_docx_sha256=historical_docx_sha256,
            rendered_requirement_bindings=validated_finding_bindings,
        )
        report["scorecard_reconciliation"] = {
            "status": "attached_and_visible",
            "scorecard_sha256": sha256_json(card),
            "status_counts": card.get("status_counts"),
            "docx_sha256_after": sha256_file(final_docx_path),
        }
        if report["scorecard_reconciliation"]["docx_sha256_after"] != current_docx_sha:
            raise RuntimeError("scorecard changed the DOCX after final audit attachment")
        report["status"] = "complete"
    except ValueError as exc:
        message = str(exc)
        after_sha = sha256_file(final_docx_path)
        if "rerender and re-audit" in message or after_sha != current_docx_sha:
            report["status"] = "rerender_required"
            report["scorecard_reconciliation"] = {
                "status": "visible_scorecard_changed_docx",
                "message": message,
                "docx_sha256_before": current_docx_sha,
                "docx_sha256_after": after_sha,
                "rendered_report_is_stale": True,
                "next_step": "refresh the TOC if needed, render this exact DOCX to PDF, rerun rendered-format audit, then rerun this reconciliation",
            }
            result_code = 2
        else:
            report["status"] = "blocked"
            report["scorecard_reconciliation"] = {
                "status": "blocked", "message": message,
                "docx_sha256_after": after_sha,
            }
            result_code = 3
    report["audit_sha256"] = sha256_json(report)
    _write_json(output_path, report)
    return report, result_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("historical_clause_report", type=Path)
    parser.add_argument("format_spec", type=Path)
    parser.add_argument("style_map", type=Path)
    parser.add_argument("property_receipt_audit", type=Path)
    parser.add_argument("rendered_report", type=Path)
    parser.add_argument("final_docx", type=Path)
    parser.add_argument("scorecard", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--historical-docx-sha256")
    args = parser.parse_args(argv)
    report, code = reconcile(
        args.historical_clause_report, args.format_spec, args.style_map,
        args.property_receipt_audit, args.rendered_report, args.final_docx,
        args.scorecard, args.out,
        historical_docx_sha256=args.historical_docx_sha256,
    )
    print(json.dumps({"status": report["status"], "current_output_status": report["current_output_status"],
                      "docx_sha256": report["current_output"]["docx_sha256"],
                      "scorecard_reconciliation": report["scorecard_reconciliation"]},
                     ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
