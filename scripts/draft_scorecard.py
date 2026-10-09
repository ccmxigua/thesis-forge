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
from pipeline_finding import integrity_findings
from responsibility_ledger import WEIGHTS, FORCES, APPLICABILITIES, ROUTES
from source_obligation_assessment import assessment_projection_valid, build_obligation_assessment

POLICY = "source_obligation_draft_scorecard_v3"
PREFIX = "【SC-"
STATUSES = ("failed", "unverified", "pending", "verified")


def _entry_score(status: str) -> int | None:
    """Only observed outcomes receive a numeric diagnostic score."""
    if status == "verified":
        return 100
    if status == "failed":
        return 0
    if status in {"unverified", "pending"}:
        return None
    raise ValueError("unknown scorecard status")


def _scorecard_semantics_valid(card: dict[str, Any]) -> bool:
    entries = card.get("entries")
    if not isinstance(entries, list) or any(not isinstance(item, dict) for item in entries):
        return False
    expected_counts = {status: 0 for status in STATUSES}
    for item in entries:
        status = item.get("status")
        if (not isinstance(status, str) or status not in expected_counts
                or item.get("score") != _entry_score(status)):
            return False
        expected_counts[status] += 1
    if card.get("status_counts") != expected_counts:
        return False
    if (card.get("item_count") != len(entries)
            or card.get("diagnostic_item_count") != len(entries)
            or card.get("verified_count") != expected_counts["verified"]
            or card.get("failed_count") != expected_counts["failed"]
            or card.get("unverified_count") != expected_counts["unverified"]
            or card.get("pending_count") != expected_counts["pending"]):
        return False
    return (card.get("submission_ready") is False
            and assessment_projection_valid(card.get("obligation_assessment")))


def build_scorecard(binding: dict[str, Any], receipt_audit: dict[str, Any],
                    manual_items: list[dict[str, Any]], findings: list[dict[str, Any]],
                    capability_findings: list[dict[str, Any]] | None = None,
                    *,
                    evaluation_units_by_requirement: dict[str, list[dict[str, Any]]] | None = None,
                    obligation_scope_inventory: dict[str, Any] | None = None,
                    expected_docx_sha256: str | None = None,
                    ) -> dict[str, Any]:
    """Known automatic source units count once; unknown semantics stay unweighted.

    Legacy check percent remains a separately named diagnostic, never silently
    upgraded to a normative score. A shared source unit requires explicit,
    code-generated identity; wording, role, confidence and array position do
    not establish equivalence. Every raw check remains independently visible.
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
    if integrity_findings([*findings, *(capability_findings or [])]):
        raise ValueError("scorecard cannot bypass an integrity gate")
    if evaluation_units_by_requirement is not None and (
        not isinstance(evaluation_units_by_requirement, dict)
        or any(not isinstance(key, str) or not isinstance(value, list)
               for key, value in evaluation_units_by_requirement.items())
    ):
        raise ValueError("authoritative evaluation-unit inventory is invalid")
    ids = [item.get("receipt_id") for item in receipts if isinstance(item, dict)]
    if (len(ids) != len(receipts) or any(not isinstance(x, str) or not x for x in ids)
            or len(set(ids)) != len(ids) or len(set(expected)) != len(expected) or set(ids) != set(expected)):
        raise ValueError("scorecard receipt identities do not match")
    entries: list[dict[str, Any]] = []
    units: dict[str, dict[str, Any]] = {}
    diagnostic_keys: set[str] = set()

    def attach_unit(entry: dict, unit: dict) -> None:
        uid = unit["evaluation_unit_id"]
        if uid in units and units[uid]["identity"] != unit:
            raise ValueError("conflicting score unit metadata")
        if uid not in units:
            units[uid] = {"evaluation_unit_id": uid, "identity": copy.deepcopy(unit), "item_ids": [], "statuses": []}
        units[uid]["item_ids"].append(entry["item_id"])
        units[uid]["statuses"].append(entry["status"])
        entry.setdefault("evaluation_unit_ids", []).append(uid)

    def add(kind: str, identity: Any, label: str, status: str, detail: Any) -> dict | None:
        key = sha256_json({"kind": kind, "identity": identity})
        if key in diagnostic_keys:
            return None  # Exact duplicate diagnostics do not inflate any denominator.
        diagnostic_keys.add(key)
        entry = {
            "item_id": "SC-" + sha256_json({"kind": kind, "identity": identity, "binding": binding})[:24],
            "kind": kind, "label": label, "status": status,
            "score": _entry_score(status), "max_score": 100,
            "human_check_required": status != "verified", "detail": copy.deepcopy(detail),
        }
        entries.append(entry)
        return entry

    if evaluation_units_by_requirement is not None:
        for requirement_id, expected_units in evaluation_units_by_requirement.items():
            for unit in expected_units:
                if not isinstance(unit, dict) or not isinstance(unit.get("evaluation_unit_id"), str):
                    raise ValueError("authoritative evaluation unit is invalid")
                uid = unit["evaluation_unit_id"]
                if uid in units and units[uid]["identity"] != unit:
                    raise ValueError("conflicting authoritative evaluation unit metadata")
                units.setdefault(uid, {
                    "evaluation_unit_id": uid, "identity": copy.deepcopy(unit),
                    "item_ids": [], "statuses": [],
                })

    for item in receipts:
        if item.get("status") not in {"verified", "failed", "unverified"}:
            raise ValueError("unknown property receipt status")
        record = {key: copy.deepcopy(value) for key, value in item.items() if key != "serialized_docx_sha256"}
        entry = add("property", item["receipt_id"], str(item.get("role", "")) + "." + str(item.get("property_path", "")), item["status"], record)
        declared = item.get("evaluation_units")
        if declared is not None and (not isinstance(declared, list) or not declared):
            raise ValueError("invalid evaluation unit inventory")
        requirement_id = item.get("requirement_id")
        if declared is not None:
            if evaluation_units_by_requirement is None:
                raise ValueError("typed score units require a verified current-run source ledger")
            expected_units = evaluation_units_by_requirement.get(requirement_id)
            if not expected_units or declared != expected_units:
                raise ValueError("property receipt units do not match the verified source ledger")
        elif (evaluation_units_by_requirement is not None
              and requirement_id in evaluation_units_by_requirement):
            raise ValueError("property receipt omitted verified source units")
        for unit in declared or []:
            if (not isinstance(unit, dict) or unit.get("force") not in FORCES
                    or unit.get("route") not in ROUTES or unit.get("applicability") not in APPLICABILITIES
                    or unit.get("source_sha256") != binding["source_sha256"]
                    or not isinstance(unit.get("canonical_obligation_key"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", unit["canonical_obligation_key"])
                    or unit.get("evaluation_unit_id") != "EU-" + unit["canonical_obligation_key"][:32]
                    or not re.fullmatch(r"[0-9a-f]{64}", str(unit.get("source_span_sha256", "")))
                    or unit.get("semantic_basis") not in {"typed_source_atom", "legacy_unknown_dimensions"}
                    or (unit["semantic_basis"] != "typed_source_atom" and unit["force"] != "unknown")):
                raise ValueError("score unit is not bound to the current source atom")
            attach_unit(entry, unit)
        if not declared:
            # Requirement IDs are only legacy grouping, never semantic identity.
            # Keep them unweighted; splitting properties cannot create points.
            key = sha256_json({"legacy_requirement": item.get("requirement_id", item["receipt_id"]),
                               "source_sha256": binding["source_sha256"]})
            attach_unit(entry, {"evaluation_unit_id": "EU-" + key[:32], "canonical_obligation_key": key,
                               "force": "unknown", "applicability": "unknown", "route": "unknown",
                               "semantic_basis": "legacy_unknown_dimensions"})
    if evaluation_units_by_requirement is not None:
        for uid, row in units.items():
            if row["identity"].get("route") == "human":
                # A property receipt can verify a document property, but it
                # cannot complete a source/provenance decision. Keep this unit
                # pending and visible even if an associated property receipt
                # says "verified"; only a separate human-verification receipt
                # may eventually resolve that obligation.
                diagnostic = add(
                    "human_review", uid,
                    "来源或适用性需要人工核验", "pending",
                    {"evaluation_unit_id": uid, **copy.deepcopy(row["identity"])},
                )
                if diagnostic is not None:
                    row["item_ids"].append(diagnostic["item_id"])
                row["statuses"].append("pending")
                continue
            if row["statuses"]:
                continue
            diagnostic = add(
                "source_obligation", uid,
                "来源义务缺少可核验的文档属性回执", "unverified",
                {"evaluation_unit_id": uid, **copy.deepcopy(row["identity"])},
            )
            if diagnostic is not None:
                row["item_ids"].append(diagnostic["item_id"])
                row["statuses"].append("unverified")
        authoritative_ids = {
            unit.get("evaluation_unit_id")
            for rows in evaluation_units_by_requirement.values()
            for unit in rows if isinstance(unit, dict)
        }
        if any(uid not in authoritative_ids for uid, row in units.items()
               if row["identity"].get("semantic_basis") == "typed_source_atom"):
            raise ValueError("scorecard contains a typed unit outside the verified source inventory")
    for item in manual_items:
        add("human_review", item, str(item.get("source_code") or item.get("source_text") or "人工核验"), "pending", item)
    for item in findings:
        # This is a known observability gap, not evidence that the document's
        # drawing/math text uses the wrong font. Keep it visible and blocking
        # in the underlying audit, while withholding a compliance score.
        if item.get("failure_type") == "document_font_not_observable":
            add("capability", item,
                str(item.get("role", "")) + "." + str(item.get("property") or item.get("failure_type")),
                "unverified", item)
        else:
            add("finding", item,
                str(item.get("role", "")) + "." + str(item.get("property") or item.get("failure_type") or "未满足检查"),
                "failed", item)
    for item in capability_findings or []:
        add("capability", item, str(item.get("code") or "能力预检待处理"), "unverified", item)
    if len({item["item_id"] for item in entries}) != len(entries):
        raise ValueError("duplicate scorecard item")
    # Proven mismatches precede unknown and human work. Stable order preserves
    # source/receipt order within each state; unknown work never sorts by a
    # fabricated zero.
    status_order = {status: index for index, status in enumerate(STATUSES)}
    entries.sort(key=lambda item: status_order[item["status"]])
    status_counts = {status: sum(item["status"] == status for item in entries)
                     for status in STATUSES}
    earned = status_counts["verified"]
    possible = 100 * len(entries)
    source_units = []
    for uid in sorted(units):
        row = units[uid]
        identity, statuses = row["identity"], row.pop("statuses")
        status = ("pending" if identity["route"] == "human" else
                  "failed" if "failed" in statuses else "unverified" if "unverified" in statuses else "verified")
        bucket = ("H" if identity["route"] == "human" else "I" if identity["route"] == "input" else
                  "F" if identity["route"] in {"automatic", "check_only"} and identity["force"] != "unknown"
                  and identity["applicability"] == "applicable" else "U")
        weight = WEIGHTS[identity["force"]] if bucket == "F" else None
        source_units.append({**row, "ledger": bucket, "status": status,
                             "weight": weight, "earned_weight": weight if weight and status == "verified" else 0})
    total_weight = sum(u["weight"] or 0 for u in source_units)
    earned_weight = sum(u["earned_weight"] for u in source_units)
    ledgers = {letter: [u["evaluation_unit_id"] for u in source_units if u["ledger"] == letter] for letter in "FHIU"}
    ledgers["H"].extend(e["item_id"] for e in entries if e["kind"] == "human_review")
    # Findings without a proved unit reference are diagnostics, not new duties.
    ledgers["U"].extend(e["item_id"] for e in entries if e["kind"] in {"finding", "capability"})
    obligation_assessment = build_obligation_assessment(
        binding, evaluation_units_by_requirement, receipt_audit, obligation_scope_inventory,
        expected_docx_sha256,
    )
    return {
        "schema_version": "3.0", "policy": POLICY, "output_policy": "review_draft",
        "binding": copy.deepcopy(binding), "submission_ready": False,
        "human_check_required": True, "coverage_complete": False,
        "score_meaning": "verified_weight_of_known_applicable_automatic_source_units_not_release",
        "weighting": "product_policy_required_prohibited_5_recommended_2_optional_1_unknown_unweighted",
        "weight_policy": copy.deepcopy(WEIGHTS), "weight_policy_is_school_rule": False,
        "score": round(earned_weight * 100 / total_weight, 2) if total_weight else None,
        "earned_weight": earned_weight, "total_weight": total_weight,
        "observed_checks_verified_percent": round(earned * 100 / len(entries), 2) if entries else None,
        "evaluation_units": source_units, "ledgers": ledgers,
        "minimum_draft_score": None,
        "status_counts": status_counts,
        "verified_count": status_counts["verified"],
        "failed_count": status_counts["failed"],
        "unverified_count": status_counts["unverified"],
        "pending_count": status_counts["pending"],
        "item_count": len(entries), "diagnostic_item_count": len(entries),
        "evaluation_unit_count": len(source_units),
        "typed_source_unit_count": sum(
            unit["identity"].get("semantic_basis") == "typed_source_atom" for unit in source_units
        ),
        "legacy_unknown_unit_count": sum(
            unit["identity"].get("semantic_basis") == "legacy_unknown_dimensions" for unit in source_units
        ),
        "unique_source_obligation_count": (
            sum(unit["identity"].get("semantic_basis") == "typed_source_atom" for unit in source_units)
            if any(unit["identity"].get("semantic_basis") == "typed_source_atom" for unit in source_units)
            else None
        ),
        "diagnostic_count_meaning": (
            "Deduplicated diagnostic entries, not a count of independent source obligations. "
            "Legacy unknown-dimension evaluation units do not establish unique obligation identity."
        ),
        "obligation_assessment": obligation_assessment,
        "entries": entries,
    }


def scorecard_lines(card: dict[str, Any]) -> list[str]:
    head = ("自动核验评分（审查草稿，不可提交）\n"
            f"得分：{card['score'] if card['score'] is not None else '未评分'}/100；"
            f"诊断条目{card['diagnostic_item_count']}项（不是独立义务数）；"
            f"已验证{card['verified_count']}项，已证实未满足{card['failed_count']}项，"
            f"未核验{card['unverified_count']}项，待人工核验{card['pending_count']}项。\n"
            f"来源评价单元{card['evaluation_unit_count']}个，其中可确认独立来源义务"
            f"{card['unique_source_obligation_count'] if card['unique_source_obligation_count'] is not None else '未知'}个。\n"
            + _obligation_assessment_line(card.get("obligation_assessment")) + "\n"
            + "按已知适用的自动义务单元计分：强制/禁止5、建议2、已选择可选项1；这是产品策略，不是学校权重。"
            + "未知强制性/适用性不赋权，人工、待输入、未知问题单列；重复检查不重复加权。"
            + "所有未满足/待核验项须人工检查；高分不能抵消安全错误或授权提交。")
    statuses = {"verified": "已验证满足", "failed": "未满足", "unverified": "未核验", "pending": "待人工核验"}
    def details(item: dict[str, Any]) -> str:
        record = item["detail"]
        # Full producer records stay in JSON, not an unbounded paragraph in
        # the document. Render the actionable evidence, never guessed roles.
        fields = {key: record[key] for key in (
            "requirement_id", "clause_ids", "evidence_ids", "source_text", "source_code",
            "property_path", "property", "expected", "actual", "required_value",
            "evaluation_unit_id", "canonical_obligation_key",
            "template_value", "reason", "status_reason", "action", "message", "status",
        ) if key in record}
        return json.dumps(fields, ensure_ascii=False, sort_keys=True)
    def score_text(item: dict[str, Any]) -> str:
        if item["status"] == "verified":
            return "100/100 已验证满足"
        if item["status"] == "failed":
            return "0/100 已证实未满足"
        if item["status"] == "unverified":
            return "未评分（未核验）"
        return "未评分（待人工核验）"
    return [head] + [
        f"【{item['item_id']}｜自动评分】{score_text(item)}：{item['label']}\n"
        + "具体检查/要求/实际值及原因：" + details(item)
        + ("\n处理：请对照原文和文档人工核验、修正后重新检查。" if item["human_check_required"] else "")
        for item in card["entries"]
    ]


def _obligation_assessment_line(assessment: dict[str, Any] | None) -> str:
    if not isinstance(assessment, dict):
        return "对象级来源义务：未评估。"
    if assessment.get("status") == "scope_inventory_missing":
        return (
            "对象级来源义务：范围清单缺失，不计算覆盖率或满足率；"
            f"旧式未知来源单元{assessment.get('legacy_unknown_unit_count', 0)}个，"
            f"结构化来源单元{assessment.get('typed_source_unit_count', 0)}个。"
        )
    if assessment.get("assessment_coverage") is None:
        return (
            "对象级来源义务：范围或适用性未完整确认，不计算覆盖率或满足率；"
            f"清单完整={assessment.get('inventory_complete') is True}，"
            f"旧式未知来源单元{assessment.get('legacy_unknown_unit_count', 0)}个。"
        )
    coverage = f"{assessment['assessment_coverage'] * 100:.1f}%"
    satisfaction = assessment.get("satisfaction_ratio")
    if satisfaction is None:
        return f"对象级核验覆盖率{coverage}；仍有未核验/待人工/未知项，不宣称部分满足或完整满足。"
    return (
        f"对象级核验覆盖率{coverage}；完整核验范围内满足率"
        f"{satisfaction * 100:.1f}%（不代表学校评分或发布授权）。"
    )


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
    semantic_valid = _scorecard_semantics_valid(card)
    lines = scorecard_lines(card) if semantic_valid else []
    texts = [p.text for p in paragraphs]
    expected_ids = {item["item_id"] for item in card["entries"]}
    actual_ids = [p.text.split("｜", 1)[0][1:] for p in paragraphs if p.text.startswith(PREFIX)]
    valid = (semantic_valid and all(texts.count(line) == 1 for line in lines)
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
                             capability_findings: list[dict[str, Any]] | None = None,
                             evaluation_units_by_requirement: dict[str, list[dict[str, Any]]] | None = None,
                             obligation_scope_inventory: dict[str, Any] | None = None,
                             expected_docx_sha256: str | None = None) -> bool:
    try:
        expected = build_scorecard(
            binding, receipt_audit, manual_items, findings, capability_findings,
            evaluation_units_by_requirement=evaluation_units_by_requirement,
            obligation_scope_inventory=obligation_scope_inventory,
            expected_docx_sha256=expected_docx_sha256,
        )
    except (ValueError, TypeError):
        return False
    return sha256_json(card) == sha256_json(expected) and audit_scorecard(output, card)["valid"]
