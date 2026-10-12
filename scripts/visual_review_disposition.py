#!/usr/bin/env python3
"""Create a new report with source-backed human dispositions; keep parents immutable."""
from __future__ import annotations

import argparse
import copy
import json
import uuid
from pathlib import Path
from typing import Any

import visual_page_review as visual
from semantic_contract import strict_json_read
from visual_page_review_human import verify_human_receipt
from visual_review_protocol import (
    renderer_status_projection, review_status, validate_closed_finding_source,
    validate_issue_ledger,
)


def apply_dispositions(report_path: Path, dispositions_path: Path) -> Path:
    report_path = report_path.expanduser().resolve()
    dispositions_path = dispositions_path.expanduser().resolve()
    report = visual._read_object(report_path, label="human-reviewed visual report")
    if report.get("protocol") != visual.PROTOCOL or report.get("review_method") != "human_browser_session":
        raise ValueError("source-backed dispositions require a completed human browser session report")
    if report.get("code_fingerprint") != visual.code_fingerprint():
        raise ValueError("review code changed after the human session; prepare and review a fresh run")
    if report.get("submission_ready") is not False or report.get("model_request_attempted") is not False:
        raise ValueError("disposition workflow cannot claim submission readiness or reuse a model attempt")
    receipt_record = report.get("human_review_receipt")
    if not isinstance(receipt_record, dict):
        raise ValueError("human browser receipt is missing")
    receipt_path = Path(receipt_record["path"]).expanduser().resolve()
    if visual.file_record(receipt_path) != receipt_record:
        raise ValueError("human browser receipt changed after review")
    human_check = verify_human_receipt(receipt_path, report_path=report_path, manifest=report)
    if human_check.get("valid") is not True:
        raise ValueError("human browser receipt failed independent verification: " + "; ".join(human_check["blockers"]))

    pages = report.get("pages", [])
    review_evidence_complete = (
        report.get("coverage_complete") is True
        and all(page.get("status") == "completed"
                and page.get("page_map", {}).get("source_mapping_status") == "resolved"
                and page.get("review_method") == "human_browser_session"
                and page.get("request_image_receipt", {}).get("status") == "human_session_confirmed_images"
                for page in pages)
    )
    if not review_evidence_complete:
        raise ValueError("source report lacks complete human-reviewed, source-mapped page coverage")

    from format_spec_validation import load_and_validate
    dispositions = visual._read_object(dispositions_path, label="issue dispositions")
    errors = load_and_validate(dispositions, visual.DISPOSITIONS_SCHEMA)
    if errors:
        raise ValueError("issue dispositions failed schema validation: " + "; ".join(errors[:12]))
    if dispositions.get("run_id") != report.get("run_id") or dispositions.get("pdf_sha256") != report.get("pdf", {}).get("sha256"):
        raise ValueError("issue dispositions refer to another run or PDF")
    ledger_record = report.get("issue_ledger")
    if not isinstance(ledger_record, dict):
        raise ValueError("human report has no issue ledger")
    parent_ledger_path = Path(ledger_record["path"]).expanduser().resolve()
    if visual.file_record(parent_ledger_path) != ledger_record:
        raise ValueError("parent issue ledger hash mismatch")
    ledger = strict_json_read(parent_ledger_path)
    finding_map = {row["finding_id"]: row for row in ledger.get("findings", [])}
    source_clauses = strict_json_read(Path(report["source_clauses"]["path"]))
    allowed_clause_ids = {row["id"] for row in visual._clause_rows(source_clauses)}
    seen: set[str] = set()
    for item in dispositions["dispositions"]:
        finding_id = item["finding_id"]
        if finding_id in seen:
            raise ValueError(f"duplicate finding disposition: {finding_id}")
        seen.add(finding_id)
        if item["status"] == "intentional_with_source_basis":
            if finding_id not in finding_map:
                raise ValueError(f"unknown current finding disposition: {finding_id}")
            finding = finding_map[finding_id]
            if finding.get("severity") not in {"medium", "low"}:
                raise ValueError(f"{finding_id} is {finding.get('severity')} and cannot be cleared as intentional")
            if any(clause_id not in allowed_clause_ids for clause_id in item["source_clause_ids"]):
                raise ValueError(f"{finding_id} cites a source clause outside this run")
            finding.update(
                status="intentional_with_source_basis",
                reviewer_claim=item["reviewer_claim"],
                disposition_reason=item["reason"],
                source_clause_ids=item["source_clause_ids"],
            )
        elif item["status"] == "closed_after_rerender":
            if finding_id in finding_map:
                raise ValueError(f"{finding_id} is still present in the current rerender; it cannot be closed")
            source_record = item["source_report"]
            source_path = Path(source_record["path"]).expanduser().resolve()
            if not source_path.is_file() or visual.file_record(source_path) != source_record:
                raise ValueError(f"{finding_id} source report hash mismatch")
            source = visual._read_object(source_path, label="prior finding report")
            if source.get("review_method") != "human_browser_session":
                raise ValueError(f"{finding_id} source report needs a verified human browser review")
            source_receipt_record = source.get("human_review_receipt")
            if not isinstance(source_receipt_record, dict):
                raise ValueError(f"{finding_id} source report has no human review receipt")
            source_receipt_path = Path(source_receipt_record["path"]).expanduser().resolve()
            if visual.file_record(source_receipt_path) != source_receipt_record:
                raise ValueError(f"{finding_id} source human receipt hash mismatch")
            source_human_check = verify_human_receipt(source_receipt_path, report_path=source_path, manifest=source)
            if source_human_check.get("valid") is not True:
                raise ValueError(f"{finding_id} source human receipt is invalid")
            source_matches = []
            for source_page in source.get("pages", []):
                for source_issue in source_page.get("issues", []):
                    if source_issue.get("finding_id") == finding_id:
                        source_matches.append((source_page, source_issue))
            if len(source_matches) != 1:
                raise ValueError(f"{finding_id} is not uniquely present in its source report")
            source_page, source_issue = source_matches[0]
            if source_issue.get("status") not in {"open", "needs_human_review", "fixed_pending_rerender"}:
                raise ValueError(f"{finding_id} source report does not contain an open finding")
            prior_sha = (source.get("pdf") or {}).get("sha256")
            source_projection = {
                "finding_id": finding_id,
                "physical_pdf_page": source_page["page_number"],
                "evidence_image_ids": list(source_issue["evidence_image_ids"]),
                "finding": source_issue["finding"],
                "severity": source_issue["severity"],
                "source_clause_ids": list(source_issue["source_clause_ids"]),
                "closure": {"prior_pdf_sha256": prior_sha},
            }
            validate_closed_finding_source(
                source_projection, source, current_run_id=report["run_id"],
                current_code_fingerprint=report["code_fingerprint"],
                current_code_fingerprint_sha256=report["code_fingerprint_sha256"],
                current_source_clauses_sha256=report["source_clauses"]["sha256"],
            )
            ledger["findings"].append({
                "finding_id": finding_id,
                "physical_pdf_page": source_page["page_number"],
                "evidence_image_ids": list(source_issue["evidence_image_ids"]),
                "status": "closed_after_rerender",
                "finding": source_issue["finding"],
                "severity": source_issue["severity"],
                "reviewer_claim": item["reviewer_claim"],
                "disposition_reason": item["reason"],
                "source_clause_ids": list(source_issue["source_clause_ids"]),
                "closure": {
                    "prior_pdf_sha256": prior_sha,
                    "rerender_pdf_sha256": report["pdf"]["sha256"],
                    "new_review_run_id": report["run_id"],
                    "new_full_page_coverage": review_evidence_complete,
                    "source_finding_id": finding_id,
                    "source_report": source_record,
                },
            })

    all_image_ids = {page["page_number"]: {row["image_id"] for row in page.get("image_map", [])} for page in pages}
    validate_issue_ledger(
        ledger, run_id=report["run_id"], current_pdf_sha256=report["pdf"]["sha256"],
        page_count=report["page_count"], source_clause_ids=allowed_clause_ids,
        current_run_full_coverage=review_evidence_complete,
        allowed_image_ids_by_page=all_image_ids,
    )
    all_current_ids = {issue["finding_id"] for page in pages for issue in page.get("issues", [])}
    ledger_by_id = {row["finding_id"]: row for row in ledger["findings"]}
    if not all_current_ids.issubset(ledger_by_id):
        raise ValueError("issue ledger must contain every current visual finding")
    if set(ledger_by_id) - all_current_ids != {
        row["finding_id"] for row in ledger["findings"] if row["status"] == "closed_after_rerender"
    }:
        raise ValueError("only source-bound closed_after_rerender findings may carry forward")
    resolved = {row["finding_id"] for row in ledger_by_id.values()
                if row.get("status") == "intentional_with_source_basis"}
    output = copy.deepcopy(report)
    run_tag = uuid.uuid4().hex
    ledger_path = report_path.parent / f"visual-review-issue-ledger-{run_tag}.json"
    visual._write_json(ledger_path, ledger)
    output["issue_ledger"] = visual.file_record(ledger_path)
    output["disposition_parent_report"] = visual.file_record(report_path)
    output["disposition_input"] = visual.file_record(dispositions_path)
    status, gate, blockers = review_status(
        page_count=output["page_count"], pages=output["pages"],
        scope=output["renderer_scope"], resolved_finding_ids=resolved,
    )
    output["visual_review_status"] = status
    output["review_gate_status"] = gate
    output["renderer_scoped_status"] = renderer_status_projection(output["renderer_scope"], status)
    output["visual_review_blockers"] = blockers
    output["status"] = "passed" if status == "passed" else status
    output["submission_ready"] = False
    from format_spec_validation import load_and_validate
    schema_errors = load_and_validate(output, visual.RECEIPT_SCHEMA)
    if schema_errors:
        raise ValueError("disposition report failed visual receipt schema: " + "; ".join(schema_errors[:12]))
    output_path = report_path.parent / f"visual-review-disposition-{run_tag}.json"
    visual._write_json(output_path, output)
    return output_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--dispositions", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        output = apply_dispositions(args.report, args.dispositions)
        print(json.dumps({"status": "created", "report": str(output), "submission_ready": False}, ensure_ascii=False))
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        print(json.dumps({"status": "blocked", "error": f"{type(exc).__name__}: {exc}", "submission_ready": False}, ensure_ascii=False))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
