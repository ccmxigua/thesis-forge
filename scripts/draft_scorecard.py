"""Evidence-based draft grading; never a compliance or release certificate."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from docx import Document
from docx.oxml import OxmlElement
from docx.text.paragraph import Paragraph
from manual_review_display import (ensure_manual_review_styles, mark_manual_review_paragraph,
                                   MANUAL_REVIEW_STYLE, _effective_run_attribute)
from docx_semantics import all_body_paragraphs
from semantic_contract import sha256_json

POLICY = "evidence_based_draft_scorecard_v1"
PREFIX = "【SC-"


def build_scorecard(binding: dict[str, Any], receipt_audit: dict[str, Any],
                    manual_items: list[dict[str, Any]], findings: list[dict[str, Any]],
                    capability_findings: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Equal weight per observed property/check, verified=100, otherwise=0.

    Missing source obligations cannot be inferred from a high average. Unknown
    and pending are explicitly zero *verified points*, not claims of violation.
    """
    if not isinstance(binding, dict) or not binding.get("run_id") or not binding.get("case_id"):
        raise ValueError("scorecard requires current run/case binding")
    for key in ("source_sha256", "format_spec_sha256"):
        if not isinstance(binding.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", binding[key]):
            raise ValueError("scorecard input hash missing: " + key)
    if any(receipt_audit.get(key) != 0 for key in ("missing_count", "unexpected_count", "duplicate_count")):
        raise ValueError("scorecard cannot bypass receipt inventory integrity")
    receipts = receipt_audit.get("receipts")
    expected = receipt_audit.get("expected_receipt_ids")
    if not isinstance(receipts, list) or not isinstance(expected, list):
        raise ValueError("scorecard requires complete expected receipt inventory")
    if any(not isinstance(value, str) or not value for value in expected):
        raise ValueError("scorecard expected receipt IDs are invalid")
    for rows in (manual_items, findings, capability_findings or []):
        if not isinstance(rows, list) or any(not isinstance(item, dict) for item in rows):
            raise ValueError("scorecard inventory is not a record list")
    ids = [item.get("receipt_id") for item in receipts if isinstance(item, dict)]
    if (len(ids) != len(receipts) or any(not isinstance(x, str) or not x for x in ids)
            or len(set(ids)) != len(ids) or len(set(expected)) != len(expected) or set(ids) != set(expected)):
        raise ValueError("scorecard receipt identities do not match")
    entries: list[dict[str, Any]] = []

    def add(kind: str, identity: Any, label: str, status: str, detail: Any) -> None:
        entries.append({
            "item_id": "SC-" + sha256_json({"kind": kind, "identity": identity, "binding": binding})[:24],
            "kind": kind, "label": label, "status": status,
            "score": 100 if status == "verified" else 0, "max_score": 100,
            "human_check_required": status != "verified", "detail": copy.deepcopy(detail),
        })

    for item in receipts:
        if item.get("status") not in {"verified", "failed", "unverified"}:
            raise ValueError("unknown property receipt status")
        record = {key: copy.deepcopy(value) for key, value in item.items() if key != "serialized_docx_sha256"}
        add("property", item["receipt_id"], str(item.get("role", "")) + "." + str(item.get("property_path", "")), item["status"], record)
    for item in manual_items:
        add("human_review", item, str(item.get("source_code") or item.get("source_text") or "人工核验"), "pending", item)
    for item in findings:
        add("finding", item, str(item.get("role", "")) + "." + str(item.get("property") or item.get("failure_type") or "未满足检查"), "failed", item)
    for item in capability_findings or []:
        add("capability", item, str(item.get("code") or "能力预检待处理"), "unverified", item)
    if len({item["item_id"] for item in entries}) != len(entries):
        raise ValueError("duplicate scorecard item")
    # Unmet/pending checks are the first human work queue; stable order keeps
    # source/receipt order within each score group, without guessing importance.
    entries.sort(key=lambda item: item["score"])
    earned = sum(item["score"] for item in entries)
    possible = 100 * len(entries)
    return {
        "schema_version": "1.0", "policy": POLICY, "output_policy": "review_draft",
        "binding": copy.deepcopy(binding), "submission_ready": False,
        "human_check_required": True, "coverage_complete": False,
        "score_meaning": "observed_checks_verified_percent_not_normative_compliance",
        "weighting": "equal_per_observed_check_no_inferred_school_importance",
        "score": round(earned * 100 / possible, 2) if possible else None,
        "verified_count": sum(item["status"] == "verified" for item in entries),
        "unmet_or_pending_count": sum(item["status"] != "verified" for item in entries),
        "item_count": len(entries), "entries": entries,
    }


def scorecard_lines(card: dict[str, Any]) -> list[str]:
    head = ("自动核验评分（审查草稿，不可提交）\n"
            f"得分：{card['score'] if card['score'] is not None else '未评分'}/100；"
            f"已验证{card['verified_count']}项，未满足或待核验{card['unmet_or_pending_count']}项。\n"
            "按当前已记录检查等权计分，未核验不计已验证分；不是论文质量、学校重要性或完整合规分。"
            "所有未满足/待核验项须人工检查；高分不能抵消安全错误或授权提交。")
    statuses = {"verified": "已验证满足", "failed": "未满足", "unverified": "未核验", "pending": "待人工核验"}
    def details(item: dict[str, Any]) -> str:
        record = item["detail"]
        # Full producer records stay in JSON, not an unbounded paragraph in
        # the document. Render the actionable evidence, never guessed roles.
        fields = {key: record[key] for key in (
            "requirement_id", "clause_ids", "evidence_ids", "source_text", "source_code",
            "property_path", "property", "expected", "actual", "required_value",
            "template_value", "reason", "action", "message", "status",
        ) if key in record}
        return json.dumps(fields, ensure_ascii=False, sort_keys=True)
    return [head] + [
        f"【{item['item_id']}｜自动评分】{item['score']}/100 {statuses[item['status']]}：{item['label']}\n"
        + "具体检查/要求/实际值及原因：" + details(item)
        + ("\n处理：请对照原文和文档人工核验、修正后重新检查。" if item["human_check_required"] else "")
        for item in card["entries"]
    ]


def append_scorecard(doc: Any, card: dict[str, Any]) -> None:
    ensure_manual_review_styles(doc)
    # A separate document-front section; never a guessed thesis role/location.
    anchor = doc.paragraphs[0] if doc.paragraphs else doc.add_paragraph()
    for text in scorecard_lines(card):
        element = OxmlElement("w:p")
        anchor._p.addprevious(element)
        paragraph = Paragraph(element, anchor._parent)
        paragraph.style = doc.styles[MANUAL_REVIEW_STYLE]
        paragraph.add_run(text)
        mark_manual_review_paragraph(paragraph)


def audit_scorecard(path: Path, card: dict[str, Any]) -> dict[str, Any]:
    doc = Document(path)
    paragraphs = list(all_body_paragraphs(doc))
    lines = scorecard_lines(card)
    texts = [p.text for p in paragraphs]
    expected_ids = {item["item_id"] for item in card["entries"]}
    actual_ids = [p.text.split("｜", 1)[0][1:] for p in paragraphs if p.text.startswith(PREFIX)]
    valid = (all(texts.count(line) == 1 for line in lines)
             and len(actual_ids) == len(expected_ids) and set(actual_ids) == expected_ids
             and all(p.runs and all(
                 _effective_run_attribute(run, p, doc, "vanish") in (None, "0", "false", "off")
                 and _effective_run_attribute(run, p, doc, "color") == "C00000"
                 and bool(_effective_run_attribute(run, p, doc, "rFonts", "eastAsia"))
                 for run in p.runs if run.text)
                     for p in paragraphs if p.text in lines))
    return {"valid": valid, "policy": POLICY, "submission_ready": False,
            "scorecard_sha256": sha256_json(card),
            "docx_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "item_count": len(actual_ids), "visual_verification": "required"}


def validate_bound_scorecard(card: dict[str, Any], *, binding: dict[str, Any],
                             receipt_audit: dict[str, Any], manual_items: list[dict[str, Any]],
                             findings: list[dict[str, Any]], output: Path,
                             capability_findings: list[dict[str, Any]] | None = None) -> bool:
    expected = build_scorecard(binding, receipt_audit, manual_items, findings, capability_findings)
    return sha256_json(card) == sha256_json(expected) and audit_scorecard(output, card)["valid"]
