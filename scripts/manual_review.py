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

# This is a document-facing policy, not a compliance result.  It is repeated
# in the ledger so a consumer cannot mistake a visually marked draft for a
# submission artifact or silently change the review-marker appearance.
MANUAL_REVIEW_VISUAL_POLICY = {
    "text_color": "C00000",
    "highlight": "FFF2CC",
    "bold": True,
    "placeholder_prefix": "【待人工处理：",
    "placeholder_suffix": "】",
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


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item not in (None, "")]


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
        "placeholder_text": f"【待人工处理：{clause_id or question.get('question_id', '未绑定问题')}】",
        "original_blocking": True,
    }


def build_manual_review_ledger(
    capability_report: dict[str, Any] | None,
    questions: list[Any] | None,
    *,
    binding: dict[str, Any] | None = None,
    release_gates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a stable list of draft-only review items.

    Capability findings are the primary source.  Open questions are merged by
    clause/category/message so the same problem is not shown twice while all
    question/evidence IDs remain attached to the item.
    """
    report = capability_report if isinstance(capability_report, dict) else {}
    candidates: list[dict[str, Any]] = []
    question_clause_ids = {
        str(item.get("clause_id"))
        for item in (questions or [])
        if isinstance(item, dict) and item.get("clause_id")
    }
    for finding in report.get("findings", []):
        if not isinstance(finding, dict):
            continue
        evidence = finding.get("evidence")
        category = next(iter(_values(evidence, "category")), "runtime_manual_unverifiable")
        # In supported-subset analysis, a confirmed semantic issue is
        # intentionally non-blocking in the capability report.  It still
        # needs a visible red marker so the analysis result cannot be mistaken
        # for an interpreted requirement.
        if not finding.get("blocking") and category != "confirmed_semantic_issue":
            continue
        clause_ids = _values(evidence, "clause_id") or _ids_from_text(finding.get("message"), CLAUSE_RE)
        if category == "confirmed_semantic_issue" and question_clause_ids.intersection(clause_ids):
            # The open-question item carries the same clause's user-facing
            # wording and evidence.  Keep one marker, not two visually
            # different markers for the same unresolved semantic choice.
            continue
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
            "placeholder_text": (
                f"【待人工处理：{finding.get('code') or category}】"
            ),
            "original_blocking": bool(finding.get("blocking")),
        })

    for question in questions or []:
        if isinstance(question, dict):
            candidates.append(_question_item(question))

    for gate in release_gates or []:
        if not isinstance(gate, dict):
            continue
        code = str(gate.get("source_code") or "release_gate")
        category = str(gate.get("category") or "input_prerequisite")
        candidates.append({
            "source_type": "release_gate",
            "source_code": code,
            "source_codes": [code],
            "category": category,
            "clause_ids": _string_list(gate.get("clause_ids")),
            "requirement_ids": _string_list(gate.get("requirement_ids")),
            "question_ids": _string_list(gate.get("question_ids")),
            "evidence_ids": _string_list(gate.get("evidence_ids")),
            "source_text": str(gate.get("source_text") or code),
            "reason": str(gate.get("reason") or gate.get("source_text") or code),
            "action": str(gate.get("action") or CATEGORY_ACTIONS.get(category, "请人工核对并记录结果。")),
            "placeholder_text": str(
                gate.get("placeholder_text")
                or f"【待人工处理：{code}】"
            ),
            "original_blocking": bool(gate.get("original_blocking", True)),
            "release_gate": True,
        })

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
        "visual_policy": dict(MANUAL_REVIEW_VISUAL_POLICY),
        "binding": binding or {},
        "submission_ready": False,
        "items": items,
        "summary": {
            "total": len(items),
            "by_category": categories,
            "original_blocking_count": sum(bool(item["original_blocking"]) for item in items),
        },
    }


def add_manual_review_items(
    ledger: dict[str, Any], candidates: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Add late-discovered manual gates without changing their semantics.

    Style analysis happens after the capability report, so a review draft can
    discover an additional human-only gate after the initial ledger is built.
    This helper keeps marker IDs deterministic and refuses to overwrite an
    existing item with the same source/category/text identity.
    """
    if not isinstance(ledger, dict):
        raise ValueError("manual review ledger must be an object")
    existing = [item for item in ledger.get("items", []) if isinstance(item, dict)]
    identities = {
        (
            str(item.get("category") or ""),
            str(item.get("source_code") or ""),
            str(item.get("source_text") or ""),
        )
        for item in existing
    }
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        item = dict(candidate)
        identity = (
            str(item.get("category") or ""),
            str(item.get("source_code") or ""),
            str(item.get("source_text") or ""),
        )
        if identity in identities:
            continue
        identities.add(identity)
        item.setdefault("source_type", "release_gate")
        item.setdefault("source_codes", [str(item.get("source_code") or "release_gate")])
        item.setdefault("clause_ids", [])
        item.setdefault("requirement_ids", [])
        item.setdefault("question_ids", [])
        item.setdefault("evidence_ids", [])
        item.setdefault("reason", item.get("source_text", ""))
        item.setdefault("action", "请人工核对并记录结果。")
        item.setdefault(
            "placeholder_text",
            f"【待人工处理：{item.get('source_code') or item.get('category') or '未命名项目'}】",
        )
        item.setdefault("original_blocking", True)
        existing.append(item)
    for index, item in enumerate(existing, start=1):
        item["marker_id"] = f"MR-{index:04d}"
        item["status"] = "pending_manual_review"
        item["marker_required"] = True
    categories: dict[str, int] = {}
    for item in existing:
        category = str(item.get("category") or "runtime_manual_unverifiable")
        categories[category] = categories.get(category, 0) + 1
    ledger["visual_policy"] = dict(MANUAL_REVIEW_VISUAL_POLICY)
    ledger["items"] = existing
    ledger["summary"] = {
        "total": len(existing),
        "by_category": categories,
        "original_blocking_count": sum(bool(item.get("original_blocking")) for item in existing),
    }
    ledger["submission_ready"] = False
    return ledger


def write_manual_review_ledger(path: Path, ledger: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path,
        json.dumps(ledger, ensure_ascii=False, indent=2) + "\n",
    )
