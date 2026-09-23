#!/usr/bin/env python3
"""Constrained LLM-assisted repair loop for template Profile selectors.

The LLM may only select a role/candidate already enumerated by template_audit
and choose which declared candidate fields to retain.  A proposed Profile is
accepted only when every repaired role resolves uniquely in every supplied
DOCX (normally official, generated, and Word-resaved artifacts).
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from template_audit import audit_template
from template_profile import load_profile
from semantic_contract import strict_json_dumps, strict_json_read

ALLOWED_FIELDS = {"kind", "text", "match", "style_id", "section_index", "body_child_index"}
DEFAULT_FIELDS = ["kind", "text", "match", "section_index"]


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(strict_json_dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def serializable_profile(profile: dict[str, Any], *, absolute_resources: bool = False) -> dict[str, Any]:
    """Remove loader-only fields; optionally make resources temp-file safe."""
    clean = copy.deepcopy(profile)
    for item in clean.get("resources", []):
        resolved = item.pop("resolved_path", None)
        if absolute_resources and resolved:
            item["path"] = resolved
    return clean


def repair_findings(audit: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result = {}
    for finding in audit.get("failures", []):
        if finding.get("code") != "template_required_role_selector_not_unique":
            continue
        diagnostics = finding.get("selector_diagnostics")
        if isinstance(diagnostics, dict) and diagnostics.get("candidates"):
            result[finding["role"]] = finding
    return result


def build_request(profile: dict[str, Any], audit: dict[str, Any]) -> dict[str, Any]:
    findings = repair_findings(audit)
    tasks = []
    for role, finding in findings.items():
        diagnostics = finding["selector_diagnostics"]
        tasks.append({
            "role": role,
            "failure": {"actual": finding.get("actual"), "selector": finding.get("selector")},
            "top_score_margin": diagnostics.get("top_score_margin"),
            "candidates": diagnostics["candidates"],
            "allowed_keep_fields": sorted(ALLOWED_FIELDS),
            "default_keep_fields": DEFAULT_FIELDS,
        })
    return {
        "contract_version": "1.0",
        "task": "repair_template_profile_selectors",
        "profile_id": profile["profile_id"],
        "instructions": [
            "Choose only a listed role and zero-based candidate_index.",
            "Do not invent text, styles, section indexes, XML paths, or executable code.",
            "Prefer semantic and contextual constraints over absolute body_child_index.",
            "Retain body_child_index only when there is explicit evidence it is stable across all artifacts.",
            "Use unresolved=true when candidates are semantically ambiguous.",
            "The program will reject every repair that is not unique in all supplied DOCX artifacts.",
        ],
        "tasks": tasks,
        "response_schema": {
            "type": "object",
            "required": ["contract_version", "repairs"],
            "properties": {
                "contract_version": {"const": "1.0"},
                "repairs": {"type": "array", "items": {
                    "type": "object",
                    "required": ["role", "unresolved", "reason"],
                    "properties": {
                        "role": {"type": "string"},
                        "unresolved": {"type": "boolean"},
                        "candidate_index": {"type": "integer", "minimum": 0},
                        "keep_fields": {"type": "array", "items": {"enum": sorted(ALLOWED_FIELDS)},
                                        "uniqueItems": True},
                        "reason": {"type": "string", "minLength": 1},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                    "additionalProperties": False,
                }},
            },
            "additionalProperties": False,
        },
    }


def _role_rule(profile: dict[str, Any], role: str) -> dict[str, Any] | None:
    return next((item for item in profile["structure"]["ordered_roles"] if item["role"] == role), None)


def apply_response(profile: dict[str, Any], audit: dict[str, Any], response: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    findings = repair_findings(audit)
    staged = copy.deepcopy(profile)
    decisions: list[dict[str, Any]] = []
    seen: set[str] = set()
    if response.get("contract_version") != "1.0" or not isinstance(response.get("repairs"), list):
        return staged, [{"accepted": False, "reason": "invalid_contract"}]
    for item in response["repairs"]:
        role = item.get("role") if isinstance(item, dict) else None
        reasons = []
        if role not in findings or role in seen:
            reasons.append("unknown_or_duplicate_role")
        seen.add(role)
        if item.get("unresolved"):
            decisions.append({"role": role, "accepted": False, "reason": "llm_unresolved"})
            continue
        candidates = findings.get(role, {}).get("selector_diagnostics", {}).get("candidates", [])
        candidate_index = item.get("candidate_index")
        if isinstance(candidate_index, bool) or not isinstance(candidate_index, int) or not 0 <= candidate_index < len(candidates):
            reasons.append("invalid_candidate_index")
        keep_fields = item.get("keep_fields", DEFAULT_FIELDS)
        if not isinstance(keep_fields, list) or not keep_fields or not set(keep_fields) <= ALLOWED_FIELDS:
            reasons.append("invalid_keep_fields")
        if "text" not in keep_fields:
            reasons.append("text_constraint_required")
        if reasons:
            decisions.append({"role": role, "accepted": False, "reason": reasons})
            continue
        candidate = candidates[candidate_index]["candidate_selector"]
        selector = {key: candidate.get(key) for key in keep_fields if key in candidate}
        if selector.get("style_id") is None:
            selector.pop("style_id", None)
        rule = _role_rule(staged, role)
        if rule is None:
            decisions.append({"role": role, "accepted": False, "reason": "role_not_in_profile"})
            continue
        original_selector = rule.get("selector", {})
        if "text" in selector and original_selector.get("accepted_texts"):
            selector["accepted_texts"] = original_selector["accepted_texts"]
        if "style_id" in selector and original_selector.get("accepted_style_ids"):
            selector["accepted_style_ids"] = original_selector["accepted_style_ids"]
        rule["selector"] = selector
        rule.pop("selector_variants", None)
        decisions.append({"role": role, "accepted": True, "candidate_index": candidate_index,
                          "selector": selector, "reason": item.get("reason"),
                          "confidence": item.get("confidence")})
    return staged, decisions


def validate_repaired_roles(profile: dict[str, Any], decisions: list[dict[str, Any]], documents: list[Path]) -> dict[str, Any]:
    roles = {item["role"] for item in decisions if item.get("accepted")}
    results = []
    with tempfile.TemporaryDirectory() as td:
        profile_path = Path(td) / "profile.json"
        _write(profile_path, serializable_profile(profile, absolute_resources=True))
        for document in documents:
            audit = audit_template(document, profile_path)
            role_hits = audit.get("evidence", {}).get("role_hits", {})
            per_role = {role: len(role_hits.get(role, [])) for role in roles}
            results.append({"document": str(document.resolve()), "role_hit_counts": per_role,
                            "valid": all(count == 1 for count in per_role.values())})
    return {"documents": results, "valid": bool(results) and all(item["valid"] for item in results)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path)
    parser.add_argument("audit", type=Path)
    parser.add_argument("--document", type=Path, action="append", default=[])
    parser.add_argument("--request-out", type=Path, required=True)
    parser.add_argument("--response", type=Path)
    parser.add_argument("--profile-out", type=Path)
    parser.add_argument("--audit-out", type=Path)
    args = parser.parse_args(argv)
    profile = load_profile(args.profile)
    audit = strict_json_read(args.audit)
    request = build_request(profile, audit)
    _write(args.request_out, request)
    if not args.response:
        print(json.dumps({
            "status": "request_created",
            "request": str(args.request_out),
            "next_step": "ask the current host Agent to create the contract response, then rerun with --response",
        }, ensure_ascii=False))
        return 0
    if args.response:
        response = strict_json_read(args.response)
    staged, decisions = apply_response(profile, audit, response)
    validation = validate_repaired_roles(staged, decisions, args.document) if args.document else {"valid": False, "documents": []}
    result = {"schema_version": "1.0", "profile_id": profile["profile_id"], "decisions": decisions,
              "validation": validation, "accepted": validation["valid"] and any(d.get("accepted") for d in decisions)}
    if args.audit_out:
        _write(args.audit_out, result)
    if result["accepted"] and args.profile_out:
        _write(args.profile_out, serializable_profile(staged))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["accepted"] else 3


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
