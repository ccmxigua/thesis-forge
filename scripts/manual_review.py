#!/usr/bin/env python3
"""Build deterministic, run-bound manual-review items for draft documents.

The draft policy is deliberately additive: it records the original finding and
its evidence, but never changes a semantic review classification or invents a
formatting requirement.  A DOCX marker is only a visible reminder; this ledger
is the authoritative machine-readable record.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from question_contract import bind_question_records, normalize_question_records
from semantic_contract import strict_json_dumps, strict_json_loads
from format_spec_validation import validate_instance
from obligation_workflow import OBLIGATION_ANALYSIS_LEDGER_PROTOCOL
from responsibility_ledger import canonical_review_atom

from artifact_io import atomic_write_text

CLAUSE_RE = re.compile(r"\bC\d{5}\b")
REQUIREMENT_RE = re.compile(r"\bR\d{5}\b")

CATEGORY_ACTIONS = {
    "input_prerequisite": "补充论文、模板或用户确认的真实输入；不要用示例内容替代。",
    "runtime_manual_unverifiable": "人工核对生成文档和原始条款，并在清单中记录结果。",
    "confirmed_semantic_issue": "提供权威解释后重新绑定条款；当前标注只表示问题已确认存在。",
    "semantic_content_review": "对照权威条款人工核实；系统不会改写论文正文。",
}

MANUAL_REVIEW_LEDGER_SCHEMA_VERSION = "1.3"
MANUAL_REVIEW_OBLIGATION_ID_PROTOCOL = "manual_review_obligation_v1"
MANUAL_REVIEW_CROSSWALK_PROTOCOL = "manual_review_obligation_crosswalk_v1"
MANUAL_OBLIGATION_ID_RE = re.compile(r"^MO-[0-9a-f]{64}$")
_GENERATED_ITEM_FIELDS = frozenset({
    "marker_id", "manual_obligation_id", "status", "marker_required",
})
_SET_LIKE_ITEM_FIELDS = (
    "clause_ids", "requirement_ids", "question_ids", "evidence_ids",
    "source_codes", "scope_dependency_codes", "scope_dependency_dimensions",
)

# Red markers are reserved for an actual human decision/input. Deterministic
# format, schema, property-receipt, capability, and render failures belong in
# their technical reports and keep release gates closed; they are not TODOs
# that should be pushed onto the author as unexplained red text.
HUMAN_MARKER_CATEGORIES = frozenset({
    "input_prerequisite",
    "runtime_manual_unverifiable",
    "confirmed_semantic_issue",
    "semantic_content_review",
})
KNOWN_TECHNICAL_CATEGORIES = frozenset({
    "backend_capability_gap", "input_prerequisite_satisfied", "supported",
    "external_not_applicable", "render_validation", "schema_validation",
    "property_validation", "semantic_validation", "execution_failure",
})

# This is a document-facing policy, not a compliance result.  It is repeated
# in the ledger so a consumer cannot mistake a visually marked draft for a
# submission artifact or silently change the review-marker appearance.
MANUAL_REVIEW_VISUAL_POLICY = {
    "text_color": "C00000",
    "highlight": "FFF2CC",
    "bold": True,
    "cjk_font": "Noto Sans SC",
    "unlocated_region": "document_front_unlocated",
    "placement_basis": "code_owned_role_or_property_path_only",
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


def _source_location_identity(item: dict[str, Any]) -> str:
    locations: list[Any] = []
    singular = item.get("source_location")
    if isinstance(singular, dict):
        locations.append(singular)
    plural = item.get("source_locations")
    if isinstance(plural, list):
        locations.extend(value for value in plural if isinstance(value, dict))
    unique = {
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for value in locations
    }
    return json.dumps(sorted(unique), ensure_ascii=False, separators=(",", ":"))


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )


def _manual_item_identity(item: dict[str, Any]) -> str:
    """Identify the complete normalized obligation, not a lossy display key.

    The producer records are excluded only from semantic deduplication: two
    producers may report the same exact obligation, in which case both raw
    records are retained by ``_merge_manual_item``. Every field that can
    change the meaning, source, disposition, or requested action participates.
    """
    payload = {
        key: copy.deepcopy(value)
        for key, value in item.items()
        if key not in _GENERATED_ITEM_FIELDS and key != "producer_records"
    }
    for field in _SET_LIKE_ITEM_FIELDS:
        if isinstance(payload.get(field), list):
            payload[field] = sorted(set(_string_list(payload[field])))
    if isinstance(payload.get("source_locations"), list):
        payload["source_locations"] = sorted(
            (copy.deepcopy(value) for value in payload["source_locations"]
             if isinstance(value, dict)),
            key=_canonical_json,
        )
    return _canonical_json(payload)


def _manual_obligation_payload(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(value)
        for key, value in item.items()
        # Producer records are an append-only provenance collection.  They are
        # deliberately excluded from identity because adding a second producer
        # must not invalidate an already serialized MR/MO reference.
        if key not in _GENERATED_ITEM_FIELDS and key != "producer_records"
    }


def _manual_review_binding_errors(binding: Any) -> list[str]:
    if not isinstance(binding, dict):
        return ["manual_review_ledger_binding_must_be_object"]
    errors: list[str] = []
    for field in ("case_id", "run_id"):
        value = binding.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"manual_review_ledger_binding_{field}_missing")
    for field in (
        "source_sha256", "clause_sha256", "evidence_sha256",
        "requirements_sha256", "input_source_sha256", "format_spec_sha256",
    ):
        value = binding.get(field)
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            errors.append(f"manual_review_ledger_binding_{field}_invalid")
    for field in ("request_sha256", "official_template_sha256"):
        value = binding.get(field)
        if value is not None and (
            not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
        ):
            errors.append(f"manual_review_ledger_binding_{field}_invalid")
    if binding.get("official_template_source") not in {
        "not_supplied", "style_template", "template_profile",
    }:
        errors.append("manual_review_ledger_binding_official_template_source_invalid")
    return errors


def manual_obligation_id_for_item(binding: Any, item: dict[str, Any]) -> str:
    """Compute a full-length current-ledger-bound atomic obligation ID."""
    current_binding = binding if isinstance(binding, dict) else {}
    binding_errors = _manual_review_binding_errors(current_binding)
    if binding_errors:
        raise ValueError(
            "manual obligation identity requires a complete current-run binding: "
            + ", ".join(binding_errors)
        )
    identity = {
        "protocol": MANUAL_REVIEW_OBLIGATION_ID_PROTOCOL,
        "binding": current_binding,
        "obligation": _manual_obligation_payload(item),
    }
    return "MO-" + hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()


def _crosswalk_refs(item: dict[str, Any], field: str) -> list[str]:
    return sorted(set(_string_list(item.get(field))))


def _validate_analysis_obligation_identity(
    item: dict[str, Any], binding: dict[str, Any],
) -> dict[str, Any] | None:
    """Require every AO link to be fully identified and bound to this run."""
    analysis_id = item.get("analysis_obligation_id")
    identity = item.get("analysis_obligation_identity")
    if analysis_id is None and identity is None:
        return None
    if (
        not isinstance(analysis_id, str)
        or re.fullmatch(r"AO-[0-9a-f]{24}", analysis_id) is None
        or not isinstance(identity, dict)
    ):
        raise ValueError("analysis obligation link requires both ID and identity")

    expected_id = "AO-" + hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()[:24]
    if analysis_id != expected_id:
        raise ValueError("analysis obligation ID does not match its identity")

    required_identity_fields = {
        "protocol", "run_id", "case_id", "chunk_index", "attempt",
        "candidate_response_sha256", "review_request_sha256", "review_response_sha256",
        "check_id", "obligation_index", "source_ref", "source_sha256", "start", "end",
    }
    if set(identity) != required_identity_fields:
        raise ValueError("analysis obligation identity fields are incomplete or unknown")
    if identity.get("protocol") != OBLIGATION_ANALYSIS_LEDGER_PROTOCOL:
        raise ValueError("analysis obligation identity protocol is not recognized")
    for field in ("run_id", "case_id"):
        if (
            not isinstance(identity.get(field), str)
            or identity[field] != binding.get(field)
        ):
            raise ValueError(f"analysis obligation identity {field} is not bound to this ledger run")
    for field in ("candidate_response_sha256", "review_request_sha256", "review_response_sha256", "source_sha256"):
        if (
            not isinstance(identity.get(field), str)
            or re.fullmatch(r"[0-9a-f]{64}", identity[field]) is None
        ):
            raise ValueError(f"analysis obligation identity {field} is invalid")
    for field, minimum in (("chunk_index", 1), ("attempt", 1), ("obligation_index", 0), ("start", 0)):
        value = identity.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"analysis obligation identity {field} is invalid")
    end = identity.get("end")
    if isinstance(end, bool) or not isinstance(end, int) or end <= identity["start"]:
        raise ValueError("analysis obligation identity end is invalid")
    for field in ("check_id", "source_ref"):
        if not isinstance(identity.get(field), str) or not identity[field]:
            raise ValueError(f"analysis obligation identity {field} is missing")

    if identity.get("source_ref") != item.get("source_ref"):
        raise ValueError("analysis obligation source reference does not match the manual item")
    if identity.get("check_id") not in _string_list(item.get("clause_ids")):
        raise ValueError("analysis obligation clause does not match the manual item")
    item_source_sha256 = item.get("source_text_sha256")
    if (
        not isinstance(item_source_sha256, str)
        or item_source_sha256 != identity.get("source_sha256")
    ):
        raise ValueError("analysis obligation source hash does not match the manual item")
    for item_field, identity_field in (("source_start", "start"), ("source_end", "end")):
        value = item.get(item_field)
        if (
            isinstance(value, bool) or not isinstance(value, int)
            or value != identity.get(identity_field)
        ):
            raise ValueError(f"analysis obligation {item_field} does not match the manual item")

    producer_context = item.get("producer_context")
    if producer_context is not None:
        if not isinstance(producer_context, dict):
            raise ValueError("analysis obligation producer context must be an object")
        for field in (
            "run_id", "case_id", "chunk_index", "attempt", "candidate_response_sha256",
            "review_request_sha256", "review_response_sha256",
        ):
            if producer_context.get(field) != identity.get(field):
                raise ValueError(f"analysis obligation producer context {field} mismatch")
    if "canonical_obligation_key" in item or "evaluation_unit_id" in item:
        producers = [p.get("record", {}).get("obligation") for p in item.get("producer_records", [])
                     if isinstance(p, dict) and p.get("producer") == "independent_obligation_review"
                     and isinstance(p.get("record"), dict)]
        matching = [p for p in producers if isinstance(p, dict)
                    and p.get("analysis_obligation_id") == analysis_id and isinstance(p.get("source_atom"), dict)]
        if len(matching) != 1:
            raise ValueError("canonical unit requires the original bound AO source atom")
        expected = canonical_review_atom(binding["source_sha256"], identity["check_id"],
            {"source_sha256": identity["source_sha256"], "start": identity["start"], "end": identity["end"]},
            matching[0]["source_atom"])
        if any(item.get(k) != expected[k] or matching[0].get(k) != expected[k] for k in expected):
            raise ValueError("canonical unit differs from current AO source atom")
    return identity


def build_manual_review_crosswalk(
    items: Any, binding: Any, *, semantic_review_ledger_sha256: Any = None,
) -> dict[str, Any]:
    """Create an exact identifier crosswalk without inferring semantic links."""
    if not isinstance(items, list) or not isinstance(binding, dict):
        raise ValueError("manual-review crosswalk requires item and binding objects")
    binding_errors = _manual_review_binding_errors(binding)
    if binding_errors:
        raise ValueError(
            "manual-review crosswalk requires a complete current-run binding: "
            + ", ".join(binding_errors)
        )
    if semantic_review_ledger_sha256 is not None and (
        not isinstance(semantic_review_ledger_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", semantic_review_ledger_sha256)
    ):
        raise ValueError("semantic review ledger hash for crosswalk is invalid")
    entries: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("manual-review crosswalk contains a non-object item")
        analysis_identity = _validate_analysis_obligation_identity(item, binding)
        analysis_id = item.get("analysis_obligation_id")
        entries.append({
            **{k: item[k] for k in ("canonical_obligation_key", "evaluation_unit_id") if k in item},
            "marker_id": item.get("marker_id"),
            "manual_obligation_id": item.get("manual_obligation_id"),
            "analysis_obligation_id": analysis_id,
            "analysis_obligation_identity_sha256": (
                hashlib.sha256(_canonical_json(analysis_identity).encode("utf-8")).hexdigest()
                if isinstance(analysis_identity, dict) else None
            ),
            "clause_ids": _crosswalk_refs(item, "clause_ids"),
            "requirement_ids": _crosswalk_refs(item, "requirement_ids"),
            "question_ids": _crosswalk_refs(item, "question_ids"),
            "evidence_ids": _crosswalk_refs(item, "evidence_ids"),
            "item_sha256": hashlib.sha256(_canonical_json(item).encode("utf-8")).hexdigest(),
            "link_basis": "explicit_identifiers_from_same_ledger_item",
            "status": "pending_manual_review",
        })
    return {
        "protocol": MANUAL_REVIEW_CROSSWALK_PROTOCOL,
        "schema_version": "1.0",
        "status": "analysis_only",
        "submission_ready": False,
        "binding_sha256": hashlib.sha256(_canonical_json(binding).encode("utf-8")).hexdigest(),
        "semantic_review_ledger_sha256": semantic_review_ledger_sha256,
        "entries": entries,
    }


def refresh_manual_review_crosswalk(ledger: dict[str, Any]) -> None:
    semantic_hash = None
    existing = ledger.get("obligation_crosswalk")
    if isinstance(existing, dict):
        semantic_hash = existing.get("semantic_review_ledger_sha256")
    ledger["obligation_crosswalk"] = build_manual_review_crosswalk(
        ledger.get("items"), ledger.get("binding"),
        semantic_review_ledger_sha256=semantic_hash,
    )


def validate_manual_obligation_ids(ledger: Any) -> list[str]:
    """Verify presence, uniqueness, and recomputation of every MO identity."""
    if not isinstance(ledger, dict):
        return ["manual_review_ledger_must_be_object"]
    if ledger.get("schema_version") != MANUAL_REVIEW_LEDGER_SCHEMA_VERSION:
        return ["manual_review_ledger_schema_version_mismatch"]
    if ledger.get("obligation_identity_protocol") != MANUAL_REVIEW_OBLIGATION_ID_PROTOCOL:
        return ["manual_review_obligation_identity_protocol_mismatch"]
    items = ledger.get("items")
    if not isinstance(items, list):
        return ["manual_review_ledger_items_must_be_array"]
    binding = ledger.get("binding")
    if not isinstance(binding, dict):
        return ["manual_review_ledger_binding_must_be_object"]
    binding_errors = _manual_review_binding_errors(binding)
    if binding_errors:
        return binding_errors
    ids = [
        item.get("manual_obligation_id")
        for item in items if isinstance(item, dict)
    ]
    errors: list[str] = []
    if len(ids) != len(items) or any(
        not isinstance(value, str) or not MANUAL_OBLIGATION_ID_RE.fullmatch(value)
        for value in ids
    ):
        errors.append("manual_review_obligation_ids_missing_or_invalid")
    elif len(ids) != len(set(ids)):
        errors.append("manual_review_obligation_ids_not_unique")
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        expected = manual_obligation_id_for_item(binding, item)
        if item.get("manual_obligation_id") != expected:
            errors.append(f"manual_review_obligation_id_mismatch:{index}")
    try:
        expected_crosswalk = build_manual_review_crosswalk(
            items, binding,
            semantic_review_ledger_sha256=(
                ledger.get("obligation_crosswalk", {}).get("semantic_review_ledger_sha256")
                if isinstance(ledger.get("obligation_crosswalk"), dict) else None
            ),
        )
        if ledger.get("obligation_crosswalk") != expected_crosswalk:
            errors.append("manual_review_obligation_crosswalk_mismatch")
    except (TypeError, ValueError) as exc:
        errors.append(
            f"manual_review_obligation_crosswalk_invalid:{type(exc).__name__}:{exc}"
        )
    return errors


def _producer_record(producer: str, record: Any) -> dict[str, Any]:
    payload = copy.deepcopy(record)
    if isinstance(payload, dict):
        for field in _GENERATED_ITEM_FIELDS:
            payload.pop(field, None)
    if not isinstance(payload, dict):
        payload = {"value": payload}
    return {"producer": producer, "record": payload}


def _ensure_producer_records(
    item: dict[str, Any], *, producer: str = "manual_review_item", record: Any = None,
) -> None:
    records = item.get("producer_records")
    if not isinstance(records, list) or not records:
        item["producer_records"] = [_producer_record(
            producer, record if record is not None else item,
        )]
        return
    unique: dict[str, dict[str, Any]] = {}
    for value in records:
        if not isinstance(value, dict) or not isinstance(value.get("producer"), str) or not isinstance(
            value.get("record"), dict
        ):
            raise ValueError("manual-review producer record must preserve producer and object record")
        unique[_canonical_json(value)] = copy.deepcopy(value)
    item["producer_records"] = [unique[key] for key in sorted(unique)]


def _require_semantic_marker_source(item: dict[str, Any]) -> None:
    if str(item.get("category") or "") != "semantic_content_review":
        return
    if not _string_list(item.get("clause_ids")):
        raise ValueError("semantic-content review marker requires a bound clause_id")
    if not _string_list(item.get("evidence_ids")):
        raise ValueError("semantic-content review marker requires a bound evidence_id")
    if not isinstance(item.get("source_text"), str) or not item["source_text"].strip():
        raise ValueError("semantic-content review marker requires source text")


def _normalize_manual_marker_text(item: dict[str, Any]) -> None:
    source_text = item.get("source_text")
    if not isinstance(source_text, str) or not source_text.strip():
        raise ValueError("manual-review marker requires non-empty source text")
    if not isinstance(item.get("reason"), str) or not item["reason"].strip():
        item["reason"] = source_text
    category = str(item.get("category") or "")
    if not isinstance(item.get("action"), str) or not item["action"].strip():
        item["action"] = CATEGORY_ACTIONS.get(category, "请人工核对该项并记录结果。")
    if not isinstance(item.get("placeholder_text"), str) or not item["placeholder_text"].strip():
        item["placeholder_text"] = f"【待人工处理：{item.get('source_code') or category or '未命名项目'}】"


def _merge_manual_item(target: dict[str, Any], candidate: dict[str, Any]) -> None:
    # Callers may merge only byte-for-byte-equivalent normalized semantics.
    # Keep all producer evidence without selecting one producer as authoritative.
    left = target.get("producer_records", [])
    right = candidate.get("producer_records", [])
    unique = {_canonical_json(value): copy.deepcopy(value) for value in [*left, *right]}
    target["producer_records"] = [unique[key] for key in sorted(unique)]


def _deduplicate_manual_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for original in items:
        if not isinstance(original, dict):
            raise ValueError("manual-review ledger contains a non-object item")
        item = copy.deepcopy(original)
        _ensure_producer_records(item, record=original)
        _normalize_manual_marker_text(item)
        _require_semantic_marker_source(item)
        key = _manual_item_identity(item)
        if key in merged:
            _merge_manual_item(merged[key], item)
        else:
            merged[key] = dict(item)
    return list(merged.values())


def _question_item(
    question: dict[str, Any], *, producer_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    normalized = normalize_question_records([question])
    question = normalized[0] if normalized else {}
    question_id = question.get("question_id")
    evidence_ids = _string_list(question.get("evidence_ids"))
    clause_id = str(question.get("clause_id") or "")
    source_text = str(
        question.get("source_text")
        or question.get("question")
        or question.get("text")
        or ""
    )
    return {
        "source_type": "open_question",
        "source_code": "open_question",
        "source_codes": ["open_question"],
        "category": "runtime_manual_unverifiable",
        "clause_ids": [clause_id] if clause_id else [],
        "requirement_ids": [],
        "question_ids": [str(question_id)] if question_id else [],
        "evidence_ids": evidence_ids,
        # Keep the extracted source clause when available.  The short human
        # question is useful as the reason, but cannot locate an inline marker
        # in the generated document or explain what the reviewer must decide.
        "source_text": source_text,
        "reason": str(question.get("reason") or question.get("question") or ""),
        "action": "请人工确认该条款的适用对象、数值单位或权威解释。",
        "placeholder_text": f"【待人工处理：{clause_id or question_id or '未绑定问题'}】",
        "original_blocking": True,
        "producer_records": copy.deepcopy(producer_records or [
            _producer_record("open_question_normalized", question),
        ]),
    }


def build_manual_review_ledger(
    capability_report: dict[str, Any] | None,
    questions: list[Any] | None,
    *,
    binding: dict[str, Any] | None = None,
    release_gates: list[dict[str, Any]] | None = None,
    clauses: list[dict[str, Any]] | None = None,
    evidence_doc: dict[str, Any] | None = None,
    semantic_review_ledger_sha256: str | None = None,
) -> dict[str, Any]:
    """Return a stable list of draft-only review items.

    Capability findings are the primary source.  Open questions are merged by
    clause/category/message so the same problem is not shown twice while all
    question/evidence IDs remain attached to the item.
    """
    binding_errors = _manual_review_binding_errors(binding)
    if binding_errors:
        raise ValueError(
            "manual review ledger requires a complete current-run binding: "
            + ", ".join(binding_errors)
        )
    report = capability_report if isinstance(capability_report, dict) else {}
    raw_questions = copy.deepcopy(questions or [])
    if clauses is not None:
        questions = bind_question_records(questions or [], clauses, evidence_doc)
    else:
        questions = normalize_question_records(questions or [])
    candidates: list[dict[str, Any]] = []
    raw_questions_by_id: dict[str, dict[str, Any]] = {}
    for raw_question in raw_questions:
        if not isinstance(raw_question, dict):
            continue
        raw_id = raw_question.get("question_id") or raw_question.get("id")
        if isinstance(raw_id, str) and raw_id:
            raw_questions_by_id[raw_id] = raw_question
    for finding in report.get("findings", []):
        if not isinstance(finding, dict):
            continue
        evidence = finding.get("evidence")
        category = next(iter(_values(evidence, "category")), "")
        # In supported-subset analysis, a confirmed semantic issue is
        # intentionally non-blocking in the capability report.  It still
        # needs a visible red marker so the analysis result cannot be mistaken
        # for an interpreted requirement.
        if not finding.get("blocking") and category != "confirmed_semantic_issue":
            continue
        if category not in HUMAN_MARKER_CATEGORIES | KNOWN_TECHNICAL_CATEGORIES:
            raise ValueError(
                f"unknown capability finding category cannot be silently discarded: {category!r}"
            )
        if category not in HUMAN_MARKER_CATEGORIES:
            continue
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
            "placeholder_text": (
                f"【待人工处理：{finding.get('code') or category}】"
            ),
            "original_blocking": bool(finding.get("blocking")),
            "producer_records": [_producer_record("capability_finding", finding)],
        })

    for question in questions or []:
        # Run-level workflow failures are technical release gates, not author
        # decisions. Keep them in the run manifest rather than emitting a red
        # source marker with no clause/evidence anchor.
        if isinstance(question, dict) and question.get("scope") != "global":
            question_id = question.get("question_id") or question.get("id")
            raw_question = raw_questions_by_id.get(str(question_id), question)
            candidates.append(_question_item(
                question,
                producer_records=[
                    _producer_record("open_question_input", raw_question),
                    _producer_record("open_question_normalized", question),
                ],
            ))

    for gate in release_gates or []:
        if not isinstance(gate, dict):
            continue
        code = str(gate.get("source_code") or "release_gate")
        category = str(gate.get("category") or "")
        if category not in HUMAN_MARKER_CATEGORIES | KNOWN_TECHNICAL_CATEGORIES:
            raise ValueError(
                f"unknown release-gate category cannot be silently discarded: {category!r}"
            )
        if category not in HUMAN_MARKER_CATEGORIES:
            continue
        candidate = copy.deepcopy(gate)
        candidate.update({
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
        for generated_field in _GENERATED_ITEM_FIELDS:
            candidate.pop(generated_field, None)
        _ensure_producer_records(
            candidate, producer="release_gate", record=gate,
        )
        candidates.append(candidate)

    merged: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        _ensure_producer_records(candidate, record=candidate)
        _normalize_manual_marker_text(candidate)
        _require_semantic_marker_source(candidate)
        key = _manual_item_identity(candidate)
        if key not in merged:
            merged[key] = dict(candidate)
        else:
            _merge_manual_item(merged[key], candidate)

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
        item = {
            "marker_id": f"MR-{index:04d}",
            "status": "pending_manual_review",
            "marker_required": True,
            **item,
        }
        item["manual_obligation_id"] = manual_obligation_id_for_item(binding or {}, item)
        items.append(item)

    categories: dict[str, int] = {}
    for item in items:
        category = str(item["category"])
        categories[category] = categories.get(category, 0) + 1
    ledger = {
        "schema_version": MANUAL_REVIEW_LEDGER_SCHEMA_VERSION,
        "obligation_identity_protocol": MANUAL_REVIEW_OBLIGATION_ID_PROTOCOL,
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
    ledger["obligation_crosswalk"] = build_manual_review_crosswalk(
        items, binding or {},
        semantic_review_ledger_sha256=semantic_review_ledger_sha256,
    )
    return ledger


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
    version = ledger.get("schema_version")
    if version != MANUAL_REVIEW_LEDGER_SCHEMA_VERSION:
        raise ValueError(f"unsupported manual-review ledger schema version: {version!r}")
    if ledger.get("obligation_identity_protocol") != MANUAL_REVIEW_OBLIGATION_ID_PROTOCOL:
        raise ValueError("manual-review obligation identity protocol mismatch")
    identity_errors = validate_manual_obligation_ids(ledger)
    if identity_errors:
        raise ValueError(
            "current manual-review ledger obligation identity is invalid: "
            + ", ".join(identity_errors[:8])
        )
    existing = _deduplicate_manual_items([
        item for item in ledger.get("items", []) if isinstance(item, dict)
    ])
    items_by_identity = {_manual_item_identity(item): item for item in existing}
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        item = copy.deepcopy(candidate)
        _ensure_producer_records(item, record=candidate)
        category = str(item.get("category") or "")
        if category not in HUMAN_MARKER_CATEGORIES | KNOWN_TECHNICAL_CATEGORIES:
            raise ValueError(f"unknown manual-review category cannot be silently discarded: {category!r}")
        if category not in HUMAN_MARKER_CATEGORIES:
            continue
        item.setdefault("source_type", "release_gate")
        item.setdefault("source_codes", [str(item.get("source_code") or "release_gate")])
        item.setdefault("clause_ids", [])
        item.setdefault("requirement_ids", [])
        item.setdefault("question_ids", [])
        item.setdefault("evidence_ids", [])
        item.setdefault("reason", item.get("source_text", ""))
        item.setdefault(
            "action",
            CATEGORY_ACTIONS.get(category, "请人工核对并记录结果。"),
        )
        item.setdefault(
            "placeholder_text",
            f"【待人工处理：{item.get('source_code') or item.get('category') or '未命名项目'}】",
        )
        item.setdefault("original_blocking", True)
        _normalize_manual_marker_text(item)
        _require_semantic_marker_source(item)
        # Dedupe only after applying the same defaults and normalizers as the
        # canonical ledger builder.  Otherwise an omitted default field can
        # make an exact duplicate look distinct during identity comparison.
        identity = _manual_item_identity(item)
        if identity in items_by_identity:
            _merge_manual_item(items_by_identity[identity], item)
            continue
        existing.append(item)
        items_by_identity[identity] = item
    current_binding = ledger.get("binding") if isinstance(ledger.get("binding"), dict) else {}
    for index, item in enumerate(existing, start=1):
        item["marker_id"] = f"MR-{index:04d}"
        item["status"] = "pending_manual_review"
        item["marker_required"] = True
        item["manual_obligation_id"] = manual_obligation_id_for_item(current_binding, item)
    categories: dict[str, int] = {}
    for item in existing:
        category = str(item.get("category") or "runtime_manual_unverifiable")
        categories[category] = categories.get(category, 0) + 1
    ledger["visual_policy"] = dict(MANUAL_REVIEW_VISUAL_POLICY)
    ledger["schema_version"] = MANUAL_REVIEW_LEDGER_SCHEMA_VERSION
    ledger["obligation_identity_protocol"] = MANUAL_REVIEW_OBLIGATION_ID_PROTOCOL
    ledger["items"] = existing
    ledger["summary"] = {
        "total": len(existing),
        "by_category": categories,
        "original_blocking_count": sum(bool(item.get("original_blocking")) for item in existing),
    }
    ledger["submission_ready"] = False
    refresh_manual_review_crosswalk(ledger)
    return ledger


def filter_manual_marker_ledger(ledger: dict[str, Any]) -> dict[str, Any]:
    """Remove legacy technical diagnostics from a red-marker ledger.

    The original diagnostics remain in validation/capability/receipt reports.
    This boundary also protects apply-time consumers from old ledgers created
    before technical findings were separated from human decisions.
    """
    if not isinstance(ledger, dict):
        raise ValueError("manual review ledger must be an object")
    version = ledger.get("schema_version")
    if version != MANUAL_REVIEW_LEDGER_SCHEMA_VERSION:
        raise ValueError(f"unsupported manual-review ledger schema version: {version!r}")
    if ledger.get("obligation_identity_protocol") != MANUAL_REVIEW_OBLIGATION_ID_PROTOCOL:
        raise ValueError("manual-review obligation identity protocol mismatch")
    identity_errors = validate_manual_obligation_ids(ledger)
    if identity_errors:
        raise ValueError(
            "current manual-review ledger obligation identity is invalid: "
            + ", ".join(identity_errors[:8])
        )
    raw_items = ledger.get("items")
    if not isinstance(raw_items, list):
        raise ValueError("manual-review ledger items must be an array")
    retained: list[dict[str, Any]] = []
    for original in raw_items:
        if not isinstance(original, dict):
            raise ValueError("manual-review ledger contains a non-object item")
        category = str(original.get("category") or "")
        if category in HUMAN_MARKER_CATEGORIES:
            retained.append(dict(original))
        elif category in KNOWN_TECHNICAL_CATEGORIES:
            continue
        elif original.get("marker_required") is True or original.get("status") == "pending_manual_review":
            raise ValueError(f"unknown required manual-review category cannot be discarded: {category!r}")
    items = _deduplicate_manual_items(retained)
    items.sort(key=lambda item: (
        tuple(item.get("clause_ids") or []),
        tuple(item.get("requirement_ids") or []),
        str(item.get("source_code") or ""),
        str(item.get("source_text") or ""),
    ))
    categories: dict[str, int] = {}
    for index, item in enumerate(items, start=1):
        item["marker_id"] = f"MR-{index:04d}"
        item["status"] = "pending_manual_review"
        item["marker_required"] = True
        item["manual_obligation_id"] = manual_obligation_id_for_item(ledger["binding"], item)
        category = str(item.get("category") or "")
        categories[category] = categories.get(category, 0) + 1
    ledger["visual_policy"] = dict(MANUAL_REVIEW_VISUAL_POLICY)
    ledger["schema_version"] = MANUAL_REVIEW_LEDGER_SCHEMA_VERSION
    ledger["obligation_identity_protocol"] = MANUAL_REVIEW_OBLIGATION_ID_PROTOCOL
    ledger["items"] = items
    ledger["summary"] = {
        "total": len(items),
        "by_category": categories,
        "original_blocking_count": sum(bool(item.get("original_blocking")) for item in items),
    }
    ledger["submission_ready"] = False
    refresh_manual_review_crosswalk(ledger)
    return ledger


def validate_manual_review_ledger_ingress(
    ledger: Any, *, expected_binding: Any, expected_ledger_sha256: Any,
    actual_ledger_sha256: str,
    expected_semantic_review_ledger_sha256: str | None = None,
) -> list[str]:
    """Reject stale, changed, or ambiguously identified review sidecars.

    The expected binding and input digest must come from the current pipeline
    invocation's manifest; this check runs before any filtering or marker-ID
    normalization can hide a malformed/stale input ledger.
    """
    errors: list[str] = []
    if not isinstance(ledger, dict):
        return ["manual_review_ledger_must_be_object"]
    schema_path = Path(__file__).resolve().parents[1] / "schema" / "manual-review-ledger.schema.json"
    try:
        schema = strict_json_loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"manual_review_ledger_schema_unavailable:{type(exc).__name__}"]
    schema_errors = validate_instance(ledger, schema)
    if schema_errors:
        errors.append("manual_review_ledger_schema_invalid:" + ";".join(schema_errors[:8]))
    if not isinstance(expected_binding, dict) or not expected_binding:
        errors.append("current_pipeline_manual_review_binding_missing")
    elif ledger.get("binding") != expected_binding:
        errors.append("manual_review_ledger_current_run_binding_mismatch")
    if (
        not isinstance(expected_ledger_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_ledger_sha256)
        or actual_ledger_sha256 != expected_ledger_sha256
    ):
        errors.append("manual_review_ledger_input_sha256_mismatch")
    crosswalk = ledger.get("obligation_crosswalk")
    if (
        not isinstance(crosswalk, dict)
        or crosswalk.get("semantic_review_ledger_sha256")
        != expected_semantic_review_ledger_sha256
    ):
        errors.append("manual_review_crosswalk_semantic_ledger_hash_mismatch")
    items = ledger.get("items")
    if not isinstance(items, list):
        errors.append("manual_review_ledger_items_must_be_array")
    else:
        marker_ids = [
            item.get("marker_id") for item in items if isinstance(item, dict)
        ]
        if len(marker_ids) != len(items) or any(
            not isinstance(marker_id, str) or not marker_id for marker_id in marker_ids
        ):
            errors.append("manual_review_ledger_marker_ids_missing")
        elif len(set(marker_ids)) != len(marker_ids):
            errors.append("manual_review_ledger_marker_ids_not_unique")
        errors.extend(validate_manual_obligation_ids(ledger))
    return errors


def write_manual_review_ledger(path: Path, ledger: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path,
        strict_json_dumps(ledger, ensure_ascii=False, indent=2) + "\n",
    )
