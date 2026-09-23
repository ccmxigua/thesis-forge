#!/usr/bin/env python3
"""Create and merge a constrained LLM review for unresolved clause reviews.

This is deliberately a *classification* pass, not a DOCX execution pass.  The
model may only choose a semantic disposition for an already extracted clause;
it may not invent requirement properties or claim backend execution.  Every
decision must cite evidence belonging to the clause.  Unknown, incomplete, or
low-confidence decisions remain ``unresolved``.

The script supports an offline response file so a request can be audited and
replayed without calling a model.  It is intentionally independent from the
complete contract-2.1 merger: the existing full response is preserved and
only unresolved reviews are eligible for a monotonic transition.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

from semantic_contract import strict_json_dumps, strict_json_read

ALLOWED = {
    "informational", "requires_metadata", "requires_source_content",
    "external_compliance", "unverifiable", "unresolved",
}
UPGRADES = ALLOWED - {"unresolved"}


def load(path: Path) -> Any:
    return strict_json_read(path)


def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(strict_json_dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_packet(response: dict[str, Any], clauses: list[dict[str, Any]], school: str) -> dict[str, Any]:
    reviews = {r.get("clause_id"): r for r in response.get("clause_reviews", [])}
    unresolved = []
    for clause in clauses:
        review = reviews.get(clause.get("id"))
        if not review or review.get("classification") != "unresolved":
            continue
        unresolved.append({
            "clause_id": clause["id"],
            "text": clause.get("text", ""),
            "source_text_full": clause.get("source_text_full", clause.get("text", "")),
            "evidence_ids": list(clause.get("evidence_ids", [])),
            "context_before": clause.get("context_before", []),
            "context_after": clause.get("context_after", []),
            "source_kind": clause.get("source_kind"),
            "part_index": clause.get("part_index", 0),
        })
    return {
        "contract_version": "unresolved-review-1",
        "task": "classify_only_unresolved_thesis_clauses",
        "school": school,
        "instructions": [
            "Review only the supplied unresolved clauses.",
            "Use the clause text and its supplied context; do not infer missing text.",
            "Choose only informational, requires_metadata, requires_source_content, external_compliance, unverifiable, or unresolved.",
            "Never choose executable, verify_existing, or unsupported_backend in this pass.",
            "Use informational only for examples, labels, template/UI instructions, or explanatory prose that creates no independent obligation.",
            "Use requires_source_content only when the clause requires the author's thesis content or an actual scholarly decision.",
            "Use requires_metadata only for a missing identity/date/degree/school field.",
            "Use external_compliance only for physical production, signatures, binding, legal or administrative action outside the DOCX artifact.",
            "Use unverifiable for subjective criteria or conditions that need human judgment.",
            "Use unresolved whenever evidence is incomplete, the fragment is ambiguous, or a requirement mapping is needed.",
            "Every decision must cite one or more evidence_ids from that same clause.",
            "Confidence must be between 0 and 1. Do not upgrade below 0.90.",
            "Return JSON only.",
        ],
        "response_schema": {
            "type": "object",
            "required": ["contract_version", "reviews"],
            "properties": {
                "contract_version": {"const": "unresolved-review-1"},
                "reviews": {"type": "array", "items": {
                    "type": "object",
                    "required": ["clause_id", "classification", "evidence_ids", "confidence", "reason"],
                    "properties": {
                        "clause_id": {"type": "string"},
                        "classification": {"enum": sorted(ALLOWED)},
                        "evidence_ids": {"type": "array", "items": {"type": "string"}},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "reason": {"type": "string", "minLength": 1},
                    },
                    "additionalProperties": False,
                }},
            },
            "additionalProperties": False,
        },
        "clauses": unresolved,
    }


def merge(response: dict[str, Any], packet: dict[str, Any], llm: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    result = copy.deepcopy(response)
    if isinstance(result.get("provenance"), dict):
        result["provenance"] = {**result["provenance"], "origin": "migration"}
    by_id = {c["clause_id"]: c for c in packet.get("clauses", [])}
    seen: set[str] = set()
    changes: list[dict[str, Any]] = []
    rejects: list[dict[str, Any]] = []
    if llm.get("contract_version") != "unresolved-review-1" or not isinstance(llm.get("reviews"), list):
        raise ValueError("invalid unresolved-review-1 response")
    review_map = {r.get("clause_id"): r for r in result.get("clause_reviews", [])}
    for item in llm["reviews"]:
        cid = item.get("clause_id")
        reason = item.get("reason")
        evidence = item.get("evidence_ids")
        confidence = item.get("confidence")
        classification = item.get("classification")
        errors = []
        if cid not in by_id: errors.append("unknown_or_not_unresolved_clause")
        if cid in seen: errors.append("duplicate_review")
        seen.add(cid)
        if classification not in ALLOWED: errors.append("invalid_classification")
        if not isinstance(reason, str) or not reason.strip(): errors.append("missing_reason")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            errors.append("invalid_confidence")
        elif classification != "unresolved" and confidence < 0.90:
            errors.append("upgrade_requires_confidence_at_least_0.90")
        allowed_evidence = set(by_id.get(cid, {}).get("evidence_ids", []))
        if not isinstance(evidence, list) or not evidence or not set(evidence) <= allowed_evidence:
            errors.append("evidence_ids_not_subset_of_clause_evidence")
        original = review_map.get(cid)
        if not original or original.get("classification") != "unresolved":
            errors.append("original_review_not_unresolved")
        if errors:
            rejects.append({"accepted": False, "clause_id": cid, "errors": errors, "response": item})
            continue
        if classification == "unresolved":
            original["reason"] = reason.strip()
            continue
        old = original.get("classification")
        original["classification"] = classification
        original["requirement_indexes"] = []
        original["reason"] = reason.strip()
        changes.append({"clause_id": cid, "before": old, "after": classification,
                        "confidence": confidence, "evidence_ids": sorted(evidence),
                        "reason": reason.strip()})
    audit = {
        "algorithm": "constrained_llm_unresolved_review_v1",
        "contract_version": "unresolved-review-1",
        "input_count": len(by_id),
        "response_count": len(llm["reviews"]),
        "accepted_change_count": len(changes),
        "changes": changes,
        "rejects": rejects,
    }
    return result, audit


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--response", type=Path, required=True)
    ap.add_argument("--clauses", type=Path, required=True)
    ap.add_argument("--school", required=True)
    ap.add_argument("--packet-out", type=Path, required=True)
    ap.add_argument("--llm-response", type=Path)
    ap.add_argument("--response-out", type=Path)
    ap.add_argument("--audit-out", type=Path)
    args = ap.parse_args()
    response = load(args.response)
    clauses = load(args.clauses)
    packet = build_packet(response, clauses, args.school)
    dump(args.packet_out, packet)
    if args.llm_response:
        merged, audit = merge(response, packet, load(args.llm_response))
        if not args.response_out or not args.audit_out:
            raise SystemExit("--response-out and --audit-out are required with --llm-response")
        dump(args.response_out, merged)
        dump(args.audit_out, audit)
        print(json.dumps({"input": len(packet["clauses"]), "changes": len(audit["changes"]), "rejects": len(audit["rejects"])}, ensure_ascii=False))
    else:
        print(json.dumps({"school": args.school, "unresolved": len(packet["clauses"]), "packet": str(args.packet_out)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
