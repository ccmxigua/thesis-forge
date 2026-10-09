"""Source-bound, unweighted obligation assessment for review drafts.

Diagnostic receipts are not obligations. This module computes scoped coverage
only when an explicit, complete object inventory binds each source atom to its
current source occurrence and exact serialized-property receipts. Legacy or
incomplete inventories remain unscored.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any

from semantic_contract import sha256_json
from responsibility_ledger import FORCES, ROUTES, APPLICABILITIES

POLICY = "source_bound_object_assessment_v1"
OBSERVED_RECEIPT_STATES = {"verified", "failed", "unverified"}
TERMINAL_ATOM_STATES = {"satisfied", "failed"}


def expected_scope_id(source_binding: dict[str, Any], object_ref: str,
                      condition: str) -> str:
    return "OS-" + sha256_json({
        "policy": "source_object_condition_scope_v1",
        "source_binding": source_binding,
        "object_ref": object_ref,
        "condition": condition,
    })[:32]


def _authoritative_units(units_by_requirement: dict[str, list[dict[str, Any]]] | None
                         ) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for rows in (units_by_requirement or {}).values():
        for unit in rows:
            if not isinstance(unit, dict):
                raise ValueError("obligation assessment unit is not an object")
            uid = unit.get("evaluation_unit_id")
            if not isinstance(uid, str) or not uid:
                raise ValueError("obligation assessment unit identity missing")
            if uid in result and result[uid] != unit:
                raise ValueError("conflicting authoritative obligation unit")
            result[uid] = copy.deepcopy(unit)
    return result


def _receipt_index(receipt_audit: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = receipt_audit.get("receipts")
    if not isinstance(rows, list):
        raise ValueError("obligation assessment needs serialized property receipts")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("receipt_id"), str):
            raise ValueError("obligation assessment receipt identity invalid")
        if row["receipt_id"] in result:
            raise ValueError("duplicate property receipt cannot be assessed")
        if row.get("status") not in OBSERVED_RECEIPT_STATES:
            raise ValueError("unknown property receipt status in obligation assessment")
        result[row["receipt_id"]] = row
    return result


def _binding_errors(binding: dict[str, Any], inventory: dict[str, Any]) -> list[str]:
    declared = inventory.get("binding")
    if not isinstance(declared, dict):
        return ["scope_inventory_binding_missing"]
    errors = []
    for key in ("run_id", "case_id", "source_sha256", "format_spec_sha256"):
        expected = binding.get(key)
        if expected is not None and declared.get(key) != expected:
            errors.append("scope_inventory_binding_mismatch:" + key)
    return errors


def _unit_source_matches(unit: dict[str, Any], scope: dict[str, Any]) -> bool:
    context = unit.get("source_context")
    assertion = unit.get("semantic_assertion")
    source = scope.get("source_binding")
    if not isinstance(context, dict) or not isinstance(assertion, dict) or not isinstance(source, dict):
        return False
    span = context.get("span")
    evidence_ids = context.get("evidence_ids")
    return (
        context.get("source_sha256") == source.get("source_sha256")
        and context.get("clause_id") == source.get("clause_id")
        and isinstance(span, dict)
        and sha256_json(span) == source.get("source_span_sha256")
        and sorted(evidence_ids or []) == sorted(source.get("evidence_ids") or [])
        and assertion.get("target") == scope.get("object_ref")
        and assertion.get("condition") == scope.get("condition")
    )


def _atom_observations(atom: dict[str, Any], receipts: dict[str, dict[str, Any]],
                       unit: dict[str, Any],
                       expected_docx_sha256: str | None) -> tuple[str, dict[str, int], list[str], list[str]]:
    receipt_ids = atom.get("evidence_receipt_ids")
    if not isinstance(receipt_ids, list) or any(not isinstance(x, str) or not x for x in receipt_ids):
        raise ValueError("scope atom receipt references invalid")
    if len(receipt_ids) != len(set(receipt_ids)):
        raise ValueError("duplicate receipt reference cannot increase atom coverage")
    if unit.get("route") in {"human", "input"}:
        if receipt_ids:
            raise ValueError("property receipts cannot resolve a human-owned or input-owned obligation")
        counts = {"verified": 0, "failed": 0, "unverified": 0, "pending": 1, "unknown": 0}
        return "pending", counts, [], []
    rows = []
    stale_ids = []
    for receipt_id in receipt_ids:
        receipt = receipts.get(receipt_id)
        if receipt is None:
            raise ValueError("scope references a missing property receipt: " + receipt_id)
        linked_ids = {
            item.get("evaluation_unit_id") for item in receipt.get("evaluation_units", [])
            if isinstance(item, dict)
        }
        if unit.get("evaluation_unit_id") not in linked_ids:
            raise ValueError("scope receipt is not linked to the current source unit: " + receipt_id)
        if (expected_docx_sha256
                and receipt.get("serialized_docx_sha256") != expected_docx_sha256):
            stale_ids.append(receipt_id)
        rows.append(receipt)
    counts = {"verified": 0, "failed": 0, "unverified": 0, "pending": 0, "unknown": 0}
    for row in rows:
        counts[row["status"]] += 1
    if stale_ids:
        state = "unverified"
        counts = {"verified": 0, "failed": 0, "unverified": 1, "pending": 0, "unknown": 0}
    elif rows:
        if counts["failed"] and (counts["unverified"] or counts["pending"] or counts["unknown"]):
            state = "failed_with_unresolved"
        elif counts["failed"]:
            state = "failed"
        elif counts["unverified"] or counts["pending"]:
            state = "unverified"
        elif counts["verified"] == len(rows):
            state = "satisfied"
        else:
            state = "unknown"
    elif unit.get("route") in {"unknown", "example"}:
        state = "unknown"
        counts["unknown"] = 1
    else:
        state = "unverified"
        counts["unverified"] = 1
    if unit.get("route") in {"unknown", "example"}:
        # Observed properties do not establish that an obligation has a
        # supported owner/execution route. Preserve the observation ids, but
        # keep the atom unresolved.
        state = "unknown"
        counts["unknown"] = max(1, counts["unknown"])
    return state, counts, receipt_ids, stale_ids


def _scope_result(scope: dict[str, Any], units: dict[str, dict[str, Any]],
                  receipts: dict[str, dict[str, Any]],
                  expected_docx_sha256: str | None) -> dict[str, Any]:
    source_binding = scope.get("source_binding")
    object_ref, condition = scope.get("object_ref"), scope.get("condition")
    if (not isinstance(source_binding, dict) or not isinstance(object_ref, str) or not object_ref
            or not isinstance(condition, str) or not condition):
        raise ValueError("scope object and condition binding are required")
    complete = scope.get("inventory_complete") is True
    scope_context_resolved = (
        object_ref.strip().casefold() not in {"unknown", "unspecified"}
        and condition.strip().casefold() not in {"unknown", "unspecified"}
    )
    if complete and not scope_context_resolved:
        raise ValueError("complete scope cannot use an unknown object or condition")
    expected_id = expected_scope_id(source_binding, object_ref, condition)
    if scope.get("scope_id") != expected_id:
        raise ValueError("scope id does not match source, object and condition")
    expected_atom_ids, atoms = scope.get("expected_atom_ids"), scope.get("atoms")
    if (not isinstance(expected_atom_ids, list) or not expected_atom_ids
            or any(not isinstance(x, str) or not x for x in expected_atom_ids)
            or len(expected_atom_ids) != len(set(expected_atom_ids))
            or not isinstance(atoms, list) or any(not isinstance(a, dict) for a in atoms)):
        raise ValueError("scope atom inventory is invalid")
    atom_ids = [a.get("evaluation_unit_id") for a in atoms]
    if len(atom_ids) != len(set(atom_ids)) or set(atom_ids) != set(expected_atom_ids):
        raise ValueError("scope atom list differs from its declared inventory")
    if scope.get("importance") != "not_assessed":
        raise ValueError("importance is not an approved score input")
    priority = scope.get("review_priority")
    if not isinstance(priority, dict) or priority.get("effect") != "sort_only_no_compliance_or_release_effect":
        raise ValueError("review priority must remain sort-only")
    atom_rows = []
    total_counts = {k: 0 for k in ("satisfied", "failed", "unverified", "pending", "unknown", "not_applicable")}
    for atom in atoms:
        uid = atom["evaluation_unit_id"]
        unit = units.get(uid)
        if unit is None or unit.get("semantic_basis") != "typed_source_atom":
            raise ValueError("scope atom is not a current typed source unit")
        if not _unit_source_matches(unit, scope):
            raise ValueError("scope source/object/condition differs from current source atom")
        if (unit.get("force") not in FORCES or unit.get("applicability") not in APPLICABILITIES
                or unit.get("route") not in ROUTES):
            raise ValueError("scope atom has invalid orthogonal dimensions")
        if unit.get("applicability") == "not_applicable":
            basis = scope.get("applicability_evidence")
            if (not isinstance(basis, dict)
                    or basis.get("source_sha256") != source_binding.get("source_sha256")
                    or basis.get("clause_id") != source_binding.get("clause_id")
                    or sorted(basis.get("evidence_ids") or []) != sorted(source_binding.get("evidence_ids") or [])
                    or not basis.get("quote")):
                raise ValueError("not_applicable requires source-bound applicability evidence")
            state, counts, receipt_ids, stale_ids = "not_applicable", {
                k: 0 for k in ("verified", "failed", "unverified", "pending", "unknown")
            }, []
        else:
            state, counts, receipt_ids, stale_ids = _atom_observations(
                atom, receipts, unit, expected_docx_sha256,
            )
            if (unit.get("force") == "unknown"
                    or unit.get("applicability") in {"unknown", "conflicted"}):
                # A property may be observed, but unresolved normative
                # dimensions cannot turn that observation into a pass.
                state = "unknown"
                counts["unknown"] = max(1, counts.get("unknown", 0))
        row = {"evaluation_unit_id": uid, "force": unit["force"],
               "applicability": unit["applicability"], "route": unit["route"],
               "status": state, "receipt_status_counts": counts,
               "evidence_receipt_ids": list(receipt_ids),
               "stale_evidence_receipt_ids": list(stale_ids)}
        atom_rows.append(row)
        if state == "not_applicable":
            total_counts["not_applicable"] += 1
        elif state == "failed_with_unresolved":
            total_counts["failed"] += int(counts["failed"] > 0)
            total_counts["unverified"] += int(counts["unverified"] > 0)
            total_counts["pending"] += int(counts["pending"] > 0)
            total_counts["unknown"] += int(counts["unknown"] > 0)
        else:
            total_counts[state] += 1

    applicable_atoms = [a for a in atom_rows if a["applicability"] == "applicable"]
    applicability_resolved = all(a["applicability"] in {"applicable", "not_applicable"} for a in atom_rows)
    coverage = satisfaction = None
    if complete and scope_context_resolved and applicability_resolved and applicable_atoms:
        coverage = round(sum(a["status"] in TERMINAL_ATOM_STATES for a in applicable_atoms)
                         / len(applicable_atoms), 4)
        dimensions_resolved = all(a["force"] != "unknown" for a in applicable_atoms)
        if dimensions_resolved and all(a["status"] in TERMINAL_ATOM_STATES for a in applicable_atoms):
            satisfaction = round(sum(a["status"] == "satisfied" for a in applicable_atoms)
                                 / len(applicable_atoms), 4)
    states = [a["status"] for a in atom_rows if a["applicability"] != "not_applicable"]
    if not complete:
        outcome = "scope_incomplete"
    elif (not scope_context_resolved or not applicability_resolved
          or any(a["force"] == "unknown" for a in applicable_atoms)):
        outcome = "normative_dimensions_unresolved"
    elif any(s == "failed_with_unresolved" for s in states):
        outcome = "partially_assessed_with_failures"
    elif "failed" in states and "satisfied" in states:
        outcome = "partially_satisfied"
    elif "failed" in states:
        outcome = "failed"
    elif any(s in {"unverified", "pending", "unknown"} for s in states):
        outcome = "partially_verified" if "satisfied" in states else "unresolved"
    elif states and all(s == "satisfied" for s in states):
        outcome = "satisfied"
    else:
        outcome = "not_assessable"
    return {
        "scope_id": expected_id, "source_binding": copy.deepcopy(source_binding),
        "object_ref": object_ref, "condition": condition,
        "inventory_complete": complete, "importance": "not_assessed",
        "review_priority_effect": "sort_only_no_compliance_or_release_effect",
        "atom_count": len(atom_rows), "counts": total_counts,
        "assessment_coverage": coverage, "satisfaction_ratio": satisfaction,
        "outcome": outcome, "atoms": atom_rows,
    }


def build_obligation_assessment(binding: dict[str, Any],
                                units_by_requirement: dict[str, list[dict[str, Any]]] | None,
                                receipt_audit: dict[str, Any],
                                inventory: dict[str, Any] | None,
                                expected_docx_sha256: str | None = None) -> dict[str, Any]:
    units = _authoritative_units(units_by_requirement)
    receipts = _receipt_index(receipt_audit)
    typed = {uid: row for uid, row in units.items() if row.get("semantic_basis") == "typed_source_atom"}
    legacy = sum(row.get("semantic_basis") == "legacy_unknown_dimensions" for row in units.values())
    base_binding = {k: binding.get(k) for k in ("run_id", "case_id", "source_sha256", "format_spec_sha256")}
    if inventory is None:
        return {
            "schema_version": "1.0", "policy": POLICY, "status": "scope_inventory_missing",
            "binding": base_binding, "scope_inventory_sha256": None, "inventory_complete": False,
            "scope_count": 0, "typed_source_unit_count": len(typed),
            "scoped_source_unit_count": 0, "unscoped_typed_source_unit_count": len(typed),
            "legacy_unknown_unit_count": legacy, "assessment_coverage": None,
            "satisfaction_ratio": None, "importance_assessment_complete": False,
            "review_priority_effect": "sort_only_no_compliance_or_release_effect",
            "scopes": [], "blockers": ["source_obligation_scope_inventory_missing"],
            "submission_ready": False,
        }
    if inventory.get("schema_version") != "1.0" or inventory.get("policy") != "explicit_source_scope_inventory_v1":
        raise ValueError("unsupported source obligation scope inventory")
    errors = _binding_errors(binding, inventory)
    if errors:
        raise ValueError(";".join(errors))
    attestation = inventory.get("review_attestation")
    if inventory.get("inventory_complete") is True:
        if (not isinstance(attestation, dict)
                or attestation.get("method") != "human_source_first_scope_review"
                or not isinstance(attestation.get("reviewer_id"), str)
                or not attestation.get("reviewer_id")
                or attestation.get("reviewed_source_sha256") != binding.get("source_sha256")):
            raise ValueError("complete scope inventory lacks a current source-first human attestation")
    scopes = inventory.get("scopes")
    if not isinstance(scopes, list) or any(not isinstance(s, dict) for s in scopes):
        raise ValueError("scope inventory must contain object records")
    attestation = inventory.get("review_attestation")
    if any(scope.get("inventory_complete") is True for scope in scopes):
        if (not isinstance(attestation, dict)
                or attestation.get("method") != "human_source_first_scope_review"
                or not isinstance(attestation.get("reviewer_id"), str)
                or not attestation.get("reviewer_id")
                or attestation.get("reviewed_source_sha256") != binding.get("source_sha256")):
            raise ValueError("complete object scope lacks a current source-first human attestation")
    results = [
        _scope_result(scope, units, receipts, expected_docx_sha256)
        for scope in scopes
    ]
    scope_ids = [row["scope_id"] for row in results]
    if len(scope_ids) != len(set(scope_ids)):
        raise ValueError("duplicate source object scope")
    scoped_ids = [atom["evaluation_unit_id"] for row in results for atom in row["atoms"]]
    if len(scoped_ids) != len(set(scoped_ids)):
        raise ValueError("a source atom cannot be counted in multiple object scopes")
    if any(uid not in typed for uid in scoped_ids):
        raise ValueError("scope inventory contains a foreign source atom")
    complete = (inventory.get("inventory_complete") is True
                and all(row["inventory_complete"] for row in results)
                and set(scoped_ids) == set(typed) and legacy == 0)
    applicable = [atom for row in results for atom in row["atoms"]
                  if atom["applicability"] == "applicable"]
    all_applicability_resolved = all(
        atom["applicability"] in {"applicable", "not_applicable"}
        for row in results for atom in row["atoms"]
    )
    globally_assessable = (
        complete and all_applicability_resolved
        and all(a["force"] != "unknown" for a in applicable)
    )
    coverage = satisfaction = None
    applicable_scopes = [row for row in results if any(a["applicability"] == "applicable" for a in row["atoms"])]
    if complete and applicable and all(row["assessment_coverage"] is not None for row in applicable_scopes):
        coverage = round(sum(a["status"] in TERMINAL_ATOM_STATES for a in applicable) / len(applicable), 4)
    if (globally_assessable and applicable
            and all(a["status"] in TERMINAL_ATOM_STATES for a in applicable)):
        satisfaction = round(sum(a["status"] == "satisfied" for a in applicable) / len(applicable), 4)
    blockers = []
    if not complete:
        blockers.append("source_obligation_scope_inventory_incomplete")
    for row in results:
        for atom in row["atoms"]:
            if atom["applicability"] == "not_applicable":
                continue
            if atom["force"] in {"required", "prohibited"}:
                if atom["status"] in {"failed", "failed_with_unresolved"}:
                    blockers.append("hard_obligation_failed:" + atom["evaluation_unit_id"])
                elif atom["status"] not in TERMINAL_ATOM_STATES:
                    blockers.append("hard_obligation_unresolved:" + atom["evaluation_unit_id"])
            elif atom["force"] == "unknown" or atom["applicability"] in {"unknown", "conflicted"}:
                blockers.append("normative_dimensions_unresolved:" + atom["evaluation_unit_id"])
    inventory_bytes = json.dumps(inventory, sort_keys=True, ensure_ascii=False,
                                 separators=(",", ":")).encode("utf-8")
    return {
        "schema_version": "1.0", "policy": POLICY,
        "status": "complete" if complete else "scope_inventory_incomplete",
        "binding": base_binding, "scope_inventory_sha256": hashlib.sha256(inventory_bytes).hexdigest(),
        "inventory_complete": complete, "scope_count": len(results),
        "typed_source_unit_count": len(typed), "scoped_source_unit_count": len(set(scoped_ids)),
        "unscoped_typed_source_unit_count": len(set(typed) - set(scoped_ids)),
        "legacy_unknown_unit_count": legacy, "assessment_coverage": coverage,
        "satisfaction_ratio": satisfaction, "importance_assessment_complete": False,
        "review_priority_effect": "sort_only_no_compliance_or_release_effect",
        "scopes": results, "blockers": sorted(set(blockers)), "submission_ready": False,
    }


def assessment_projection_valid(assessment: Any) -> bool:
    """Recompute all visible counts and ratios from the atom projection."""
    if not isinstance(assessment, dict) or assessment.get("policy") != POLICY:
        return False
    scopes = assessment.get("scopes")
    if (not isinstance(scopes, list) or assessment.get("schema_version") != "1.0"
            or assessment.get("submission_ready") is not False
            or assessment.get("importance_assessment_complete") is not False
            or assessment.get("review_priority_effect") != "sort_only_no_compliance_or_release_effect"):
        return False
    count_fields = ("scope_count", "typed_source_unit_count", "scoped_source_unit_count",
                    "unscoped_typed_source_unit_count", "legacy_unknown_unit_count")
    if any(not isinstance(assessment.get(key), int) or isinstance(assessment.get(key), bool)
           or assessment[key] < 0 for key in count_fields):
        return False
    if assessment.get("scope_count") != len(scopes):
        return False
    binding = assessment.get("binding")
    if (not isinstance(binding, dict)
            or any(not isinstance(binding.get(key), str) or not binding.get(key)
                   for key in ("run_id", "case_id"))
            or any(not isinstance(binding.get(key), str)
                   or not re.fullmatch(r"[0-9a-f]{64}", binding[key])
                   for key in ("source_sha256", "format_spec_sha256"))):
        return False
    if (assessment.get("scoped_source_unit_count")
            + assessment.get("unscoped_typed_source_unit_count")
            != assessment.get("typed_source_unit_count")):
        return False
    blockers = assessment.get("blockers")
    if (not isinstance(blockers, list) or any(not isinstance(x, str) or not x for x in blockers)
            or blockers != sorted(set(blockers))):
        return False
    if assessment.get("status") == "scope_inventory_missing":
        return (not scopes and assessment.get("inventory_complete") is False
                and assessment.get("scope_inventory_sha256") is None
                and assessment.get("scoped_source_unit_count") == 0
                and assessment.get("unscoped_typed_source_unit_count")
                    == assessment.get("typed_source_unit_count")
                and assessment.get("assessment_coverage") is None
                and assessment.get("satisfaction_ratio") is None
                and blockers == ["source_obligation_scope_inventory_missing"])
    if assessment.get("scope_inventory_sha256") is not None and not re.fullmatch(
            r"[0-9a-f]{64}", str(assessment.get("scope_inventory_sha256"))):
        return False

    all_atom_ids: list[str] = []
    all_applicable_atoms: list[dict[str, Any]] = []
    applicable_scopes = []
    all_applicability_resolved = True
    for scope in scopes:
        if not isinstance(scope, dict) or not isinstance(scope.get("atoms"), list):
            return False
        if (scope.get("importance") != "not_assessed"
                or scope.get("review_priority_effect") != "sort_only_no_compliance_or_release_effect"
                or scope.get("atom_count") != len(scope["atoms"])):
            return False
        source_binding = scope.get("source_binding")
        object_ref, condition = scope.get("object_ref"), scope.get("condition")
        if (not isinstance(source_binding, dict) or not isinstance(object_ref, str)
                or not isinstance(condition, str)
                or source_binding.get("source_sha256") != binding.get("source_sha256")
                or scope.get("scope_id") != expected_scope_id(source_binding, object_ref, condition)):
            return False
        atom_ids = [a.get("evaluation_unit_id") for a in scope["atoms"] if isinstance(a, dict)]
        if (len(atom_ids) != len(scope["atoms"]) or len(atom_ids) != len(set(atom_ids))
                or any(not isinstance(uid, str) or not uid for uid in atom_ids)):
            return False
        all_atom_ids.extend(atom_ids)
        expected = {k: 0 for k in ("satisfied", "failed", "unverified", "pending", "unknown", "not_applicable")}
        applicable_atoms = []
        for atom in scope["atoms"]:
            status, force = atom.get("status"), atom.get("force")
            applicability, route = atom.get("applicability"), atom.get("route")
            if (force not in FORCES or applicability not in APPLICABILITIES or route not in ROUTES):
                return False
            receipt_counts = atom.get("receipt_status_counts")
            receipt_keys = {"verified", "failed", "unverified", "pending", "unknown"}
            if (not isinstance(receipt_counts, dict) or set(receipt_counts) != receipt_keys
                    or any(not isinstance(v, int) or isinstance(v, bool) or v < 0
                           for v in receipt_counts.values())):
                return False
            receipt_ids = atom.get("evidence_receipt_ids")
            stale_receipts = atom.get("stale_evidence_receipt_ids")
            if (not isinstance(receipt_ids, list) or any(not isinstance(x, str) for x in receipt_ids)
                    or len(receipt_ids) != len(set(receipt_ids))
                    or not isinstance(stale_receipts, list)
                    or any(x not in receipt_ids for x in stale_receipts)):
                return False
            if stale_receipts and status in TERMINAL_ATOM_STATES:
                return False
            if ((force == "unknown" or applicability in {"unknown", "conflicted"}
                 or route in {"human", "input", "unknown", "example"})
                    and status in TERMINAL_ATOM_STATES):
                return False
            if status == "failed_with_unresolved":
                if (receipt_counts["failed"] < 1
                        or not any(receipt_counts[key] for key in ("unverified", "pending", "unknown"))):
                    return False
                for key in ("failed", "unverified", "pending", "unknown"):
                    expected[key] += int(receipt_counts[key] > 0)
            elif status in expected:
                expected[status] += 1
            else:
                return False
            if status == "satisfied" and (receipt_counts["verified"] < 1
                    or any(receipt_counts[k] for k in ("failed", "unverified", "pending", "unknown"))):
                return False
            if status == "failed" and receipt_counts["failed"] < 1:
                return False
            if status == "pending" and route not in {"human", "input"}:
                return False
            if status == "not_applicable" and (applicability != "not_applicable"
                    or receipt_ids or any(receipt_counts.values())):
                return False
            if applicability == "applicable":
                applicable_atoms.append(atom)
            elif applicability not in {"not_applicable", "unknown", "conflicted"}:
                return False
            if applicability not in {"applicable", "not_applicable"}:
                all_applicability_resolved = False
        if scope.get("counts") != expected:
            return False
        scope_complete = scope.get("inventory_complete") is True
        app_resolved = all(a.get("applicability") in {"applicable", "not_applicable"}
                           for a in scope["atoms"])
        expected_coverage = expected_satisfaction = None
        if scope_complete and app_resolved and applicable_atoms:
            expected_coverage = round(
                sum(a.get("status") in TERMINAL_ATOM_STATES for a in applicable_atoms)
                / len(applicable_atoms), 4,
            )
            if (all(a.get("force") != "unknown" for a in applicable_atoms)
                    and all(a.get("status") in TERMINAL_ATOM_STATES for a in applicable_atoms)):
                expected_satisfaction = round(
                    sum(a.get("status") == "satisfied" for a in applicable_atoms)
                    / len(applicable_atoms), 4,
                )
        if (scope.get("assessment_coverage") != expected_coverage
                or scope.get("satisfaction_ratio") != expected_satisfaction):
            return False
        states = [a.get("status") for a in scope["atoms"]
                  if a.get("applicability") != "not_applicable"]
        if not scope_complete:
            expected_outcome = "scope_incomplete"
        elif not app_resolved or any(a.get("force") == "unknown" for a in applicable_atoms):
            expected_outcome = "normative_dimensions_unresolved"
        elif any(state == "failed_with_unresolved" for state in states):
            expected_outcome = "partially_assessed_with_failures"
        elif "failed" in states and "satisfied" in states:
            expected_outcome = "partially_satisfied"
        elif "failed" in states:
            expected_outcome = "failed"
        elif any(state in {"unverified", "pending", "unknown"} for state in states):
            expected_outcome = "partially_verified" if "satisfied" in states else "unresolved"
        elif states and all(state == "satisfied" for state in states):
            expected_outcome = "satisfied"
        else:
            expected_outcome = "not_assessable"
        if scope.get("outcome") != expected_outcome:
            return False
        all_applicable_atoms.extend(applicable_atoms)
        if applicable_atoms:
            applicable_scopes.append(scope)
    if len(all_atom_ids) != len(set(all_atom_ids)):
        return False
    calculated_complete = (
        assessment.get("status") == "complete"
        and all(scope.get("inventory_complete") is True for scope in scopes)
        and assessment.get("scoped_source_unit_count") == assessment.get("typed_source_unit_count")
        and assessment.get("unscoped_typed_source_unit_count") == 0
        and assessment.get("legacy_unknown_unit_count") == 0
    )
    if assessment.get("inventory_complete") is not calculated_complete:
        return False
    if not calculated_complete and assessment.get("status") != "scope_inventory_incomplete":
        return False
    expected_global_coverage = expected_global_satisfaction = None
    if (calculated_complete and all_applicable_atoms
            and all(scope.get("assessment_coverage") is not None for scope in applicable_scopes)):
        expected_global_coverage = round(
            sum(atom.get("status") in TERMINAL_ATOM_STATES for atom in all_applicable_atoms)
            / len(all_applicable_atoms), 4,
        )
    if (calculated_complete and all_applicable_atoms and all_applicability_resolved
            and all(atom.get("force") != "unknown" for atom in all_applicable_atoms)
            and all(atom.get("status") in TERMINAL_ATOM_STATES for atom in all_applicable_atoms)):
        expected_global_satisfaction = round(
            sum(atom.get("status") == "satisfied" for atom in all_applicable_atoms)
            / len(all_applicable_atoms), 4,
        )
    if (assessment.get("assessment_coverage") != expected_global_coverage
            or assessment.get("satisfaction_ratio") != expected_global_satisfaction
            or assessment.get("scoped_source_unit_count") != len(set(all_atom_ids))):
        return False
    return True
