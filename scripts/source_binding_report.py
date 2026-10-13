"""Run-bound, explicit outcomes for source-scope and target-instance checks."""
from __future__ import annotations

import copy
import re
from collections import Counter
from typing import Any


_SCOPE_CLASSES = {
    "semantic_ambiguity",
    "human_confirmation_required",
    "parser_capability_gap",
}
_RECEIPT_STATUS_CLASSES = {
    "verified": ("passed", "property_passed"),
    "failed": ("failed", "property_failed"),
    "unverified": ("unverified", "property_unverified"),
}


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def build_source_binding_report(
    binding: dict[str, Any], *,
    source_docx_sha256: str,
    output_docx_sha256: str,
    questions: list[dict[str, Any]],
    content_instance_audit: list[dict[str, Any]],
    property_receipts: list[dict[str, Any]],
    manual_items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Classify current-run observations without converting pending work to pass.

    The report is diagnostic.  It preserves source questions, target literal
    observations, serialized property receipts, and explicit human approval
    gates as distinct outcome families.
    """
    if not isinstance(binding, dict) or not all(
        isinstance(binding.get(key), str) and binding[key]
        for key in ("run_id", "case_id")
    ):
        raise ValueError("source-binding report requires current run/case binding")
    if not _is_sha256(source_docx_sha256) or not _is_sha256(output_docx_sha256):
        raise ValueError("source-binding report requires source and output DOCX SHA-256 values")
    for key in ("source_sha256", "format_spec_sha256"):
        if not _is_sha256(binding.get(key)):
            raise ValueError("source-binding report binding hash is invalid: " + key)
    for name, rows in (
        ("questions", questions), ("content instance audit", content_instance_audit),
        ("property receipts", property_receipts), ("manual items", manual_items),
    ):
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("source-binding report " + name + " must be a record list")
    if any(
        receipt.get("serialized_docx_sha256") != output_docx_sha256
        for receipt in property_receipts
    ):
        raise ValueError("source-binding report property receipt DOCX hash mismatch")

    outcomes: list[dict[str, Any]] = []
    for question in questions:
        diagnosis = question.get("source_scope_diagnosis")
        if not isinstance(diagnosis, dict):
            continue
        classification = diagnosis.get("classification")
        if classification not in _SCOPE_CLASSES:
            raise ValueError("source-binding question has an unknown scope classification")
        outcomes.append({
            "classification": classification,
            "status": "pending_input",
            "source_clause_id": diagnosis.get("source_clause_id") or question.get("clause_id"),
            "question_id": question.get("question_id"),
            "candidate_clause_ids": copy.deepcopy(diagnosis.get("candidate_clause_ids", [])),
            "reason": diagnosis.get("reason"),
            "resolution_policy": diagnosis.get("resolution_policy"),
        })

    for instance in content_instance_audit:
        instance_id = instance.get("instance_id")
        if not isinstance(instance_id, str) or not instance_id:
            continue
        state = instance.get("serialized_target_status")
        if instance.get("status") == "not_present":
            role = instance.get("role")
            outcomes.append({
                "classification": "missing_target_instance",
                "status": "failed",
                "instance_id": instance_id,
                "role": role,
                "text": instance.get("text"),
                "reason": (
                    "规范对象已解析、缺实际封面实例"
                    if isinstance(role, str) and role.startswith("cover")
                    else "已绑定的来源字面实例在目标 DOCX 中不存在。"
                ),
                "evidence": {
                    "match_count": instance.get("match_count"),
                    "source_bindings": copy.deepcopy(instance.get("source_bindings", [])),
                },
            })
        elif state == "verified":
            outcomes.append({
                "classification": "bound_instance_observed",
                "status": "verified",
                "instance_id": instance_id,
                "role": instance.get("role"),
                "text": instance.get("text"),
                "verification_scope": "serialized target paragraph and exact literal binding; not a visual or full-format pass",
            })
        elif state == "unverified":
            outcomes.append({
                "classification": "bound_but_unverified" if instance.get("source_bindings") else "unverified_instance",
                "status": "unverified",
                "instance_id": instance_id,
                "role": instance.get("role"),
                "errors": copy.deepcopy(instance.get("serialized_target_errors", [])),
            })

    for receipt in property_receipts:
        status = receipt.get("status")
        if status not in _RECEIPT_STATUS_CLASSES:
            raise ValueError("source-binding report has an unknown property receipt status")
        mapped_status, mapped_class = _RECEIPT_STATUS_CLASSES[status]
        classification = receipt.get("diagnostic_classification") or mapped_class
        if status == "unverified" and str(receipt.get("target_locator", "")).startswith("content_instances:"):
            classification = "bound_but_unverified"
        if classification == "missing_target_instance":
            mapped_status = "failed"
        outcomes.append({
            "classification": classification,
            "status": mapped_status,
            "receipt_id": receipt.get("receipt_id"),
            "requirement_id": receipt.get("requirement_id"),
            "clause_ids": copy.deepcopy(receipt.get("clause_ids", [])),
            "role": receipt.get("role"),
            "property_path": receipt.get("property_path"),
            "diagnostic_summary": receipt.get("diagnostic_summary"),
            "expected": copy.deepcopy(receipt.get("expected")),
            "actual": copy.deepcopy(receipt.get("actual")),
            "reason": receipt.get("status_reason"),
        })

    for item in manual_items:
        if item.get("source_binding_classification") != "human_approval_required":
            continue
        outcomes.append({
            "classification": "human_approval_required",
            "status": "pending_human_approval",
            "manual_obligation_id": item.get("manual_obligation_id"),
            "clause_ids": copy.deepcopy(item.get("clause_ids", [])),
            "source_text": item.get("source_text"),
            "reason": item.get("reason"),
            "action": item.get("action"),
            "execution_authorized": False,
        })

    return {
        "schema_version": "1.0",
        "protocol": "source_binding_outcomes_v1",
        "policy": "diagnostic_only_no_automatic_human_resolution",
        "binding": copy.deepcopy(binding),
        "source_docx_sha256": source_docx_sha256,
        "output_docx_sha256": output_docx_sha256,
        "submission_ready": False,
        "outcomes": outcomes,
        "summary": {
            "by_classification": dict(sorted(Counter(row["classification"] for row in outcomes).items())),
            "by_status": dict(sorted(Counter(row["status"] for row in outcomes).items())),
            "human_approval_auto_satisfied": False,
        },
    }
