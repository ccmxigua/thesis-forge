#!/usr/bin/env python3
"""Build deterministic, run-bound manual-review items for draft documents.

The draft policy is deliberately additive: it records the original finding and
its evidence, but never changes a semantic review classification or invents a
formatting requirement.  A DOCX marker is only a visible reminder; this ledger
is the authoritative machine-readable record.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from artifact_io import atomic_write_text

CLAUSE_RE = re.compile(r"\bC\d{5}\b")
REQUIREMENT_RE = re.compile(r"\bR\d{5}\b")

CATEGORY_ACTIONS = {
    "backend_capability_gap": "补充后端 capability、schema 或 deterministic checker；确认后再提交。",
    "input_prerequisite": "补充论文、模板或用户确认的真实输入；不要用示例内容替代。",
    "runtime_manual_unverifiable": "人工核对生成文档和原始条款，并在清单中记录结果。",
    "confirmed_semantic_issue": "提供权威解释后重新绑定条款；当前标注只表示问题已确认存在。",
}


def _values(evidence: Any, kind: str) -> list[str]:
    if not isinstance(evidence, list):
        return []
    return sorted({
        str(item.get("value"))
        for item in evidence
        if isinstance(item, dict) and item.get("kind") == kind and item.get("value")
    })


def _ids_from_text(text: Any, pattern: re.Pattern[str]) -> list[str]:
    return sorted(set(pattern.findall(str(text or ""))))


def _question_item(question: dict[str, Any]) -> dict[str, Any]:
    clause_id = str(question.get("clause_id") or "")
    return {
        "source_type": "open_question",
        "source_code": "open_question",
        "source_codes": ["open_question"],
        "category": "runtime_manual_unverifiable",
        "clause_ids": [clause_id] if clause_id else [],
        "requirement_ids": [],
        "question_ids": [str(question.get("question_id"))]
        if question.get("question_id") else [],
        "evidence_ids": [str(question.get("evidence_id"))]
        if question.get("evidence_id") else [],
        "source_text": str(question.get("question") or question.get("text") or ""),
        "reason": str(question.get("reason") or question.get("question") or ""),
        "action": "请人工确认该条款的适用对象、数值单位或权威解释。",
        "original_blocking": True,
    }


def build_manual_review_ledger(
    capability_report: dict[str, Any] | None,
    questions: list[Any] | None,
    *,
    binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a stable list of draft-only review items.

    Capability findings are the primary source.  Open questions are merged by
    clause/category/message so the same problem is not shown twice while all
    question/evidence IDs remain attached to the item.
    """
    report = capability_report if isinstance(capability_report, dict) else {}
    candidates: list[dict[str, Any]] = []
    for finding in report.get("findings", []):
        if not isinstance(finding, dict) or not finding.get("blocking"):
            continue
        evidence = finding.get("evidence")
        category = next(iter(_values(evidence, "category")), "runtime_manual_unverifiable")
        clause_ids = _values(evidence, "clause_id") or _ids_from_text(finding.get("message"), CLAUSE_RE)
        requirement_ids = _values(evidence, "requirement_id") or _ids_from_text(
            finding.get("message"), REQUIREMENT_RE
        )
        candidates.append({
            "source_type": "capability_finding",
            "source_code": str(finding.get("code") or "capability_finding"),
            "source_codes": [str(finding.get("code") or "capability_finding")],
            "category": category,
            "clause_ids": clause_ids,
            "requirement_ids": requirement_ids,
            "question_ids": [],
            "evidence_ids": _values(evidence, "evidence_id"),
            "source_text": str(finding.get("message") or ""),
            "reason": str(finding.get("message") or ""),
            "action": CATEGORY_ACTIONS.get(category, "请人工核对该项并记录处理结果。"),
            "original_blocking": True,
        })

    for question in questions or []:
        if isinstance(question, dict):
            candidates.append(_question_item(question))

    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    for candidate in candidates:
        key = (
            candidate["category"],
            tuple(candidate["clause_ids"]),
            tuple(candidate["requirement_ids"]),
            candidate["source_text"],
        )
        current = merged.setdefault(key, dict(candidate))
        for field in ("question_ids", "evidence_ids", "clause_ids", "requirement_ids", "source_codes"):
            current[field] = sorted(set(current.get(field, [])) | set(candidate.get(field, [])))
        if current.get("source_type") != candidate.get("source_type"):
            current["source_type"] = "capability_finding_and_open_question"

    items: list[dict[str, Any]] = []
    for index, item in enumerate(sorted(
        merged.values(),
        key=lambda value: (
            tuple(value.get("clause_ids", [])),
            tuple(value.get("requirement_ids", [])),
            value.get("source_code", ""),
            value.get("source_text", ""),
        ),
    ), start=1):
        items.append({
            "marker_id": f"MR-{index:04d}",
            "status": "pending_manual_review",
            "marker_required": True,
            **item,
        })

    categories: dict[str, int] = {}
    for item in items:
        category = str(item["category"])
        categories[category] = categories.get(category, 0) + 1
    return {
        "schema_version": "1.0",
        "policy": "review_draft_only",
        "binding": binding or {},
        "submission_ready": False,
        "items": items,
        "summary": {
            "total": len(items),
            "by_category": categories,
            "original_blocking_count": sum(bool(item["original_blocking"]) for item in items),
        },
    }


def write_manual_review_ledger(path: Path, ledger: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path,
        json.dumps(ledger, ensure_ascii=False, indent=2) + "\n",
    )
