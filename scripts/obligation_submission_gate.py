"""Independent fail-closed source-obligation gate for submission auditing.

This module reads the source scope inventory and serialized receipts directly.
It never trusts scorecard percentages or aggregate labels.
"""
from __future__ import annotations

from typing import Any

from responsibility_ledger import APPLICABILITIES, FORCES, ROUTES
from semantic_contract import sha256_json
from source_obligation_assessment import expected_scope_id


def _units_by_id(units_by_requirement: dict[str, list[dict[str, Any]]] | None
                 ) -> tuple[dict[str, dict[str, Any]], list[str]]:
    units: dict[str, dict[str, Any]] = {}
    errors = []
    for rows in (units_by_requirement or {}).values():
        for unit in rows:
            if not isinstance(unit, dict) or not isinstance(unit.get("evaluation_unit_id"), str):
                errors.append("authoritative_source_unit_invalid")
                continue
            uid = unit["evaluation_unit_id"]
            if uid in units and units[uid] != unit:
                errors.append("duplicate_source_unit_conflict:" + uid)
            units[uid] = unit
    return units, errors


def audit_obligation_submission_gate(
    *,
    required: bool,
    inventory: dict[str, Any] | None,
    expected_binding: dict[str, Any],
    units_by_requirement: dict[str, list[dict[str, Any]]] | None,
    receipt_audit: dict[str, Any],
    serialized_docx_sha256: str,
) -> dict[str, Any]:
    """Recompute mandatory and unresolved blockers from primary inputs."""
    units, errors = _units_by_id(units_by_requirement)
    if not required:
        return {
            "valid": True, "required": False, "hard_gate_passed": True,
            "submission_ready": False, "blockers": [], "errors": [],
            "basis": "no source-semantic obligation inventory in this pipeline",
        }
    blockers: list[str] = []
    if inventory is None:
        blockers.append("source_obligation_scope_inventory_missing")
        return {
            "valid": False, "required": True, "hard_gate_passed": False,
            "submission_ready": False, "scope_inventory_complete": False,
            "typed_source_unit_count": sum(
                unit.get("semantic_basis") == "typed_source_atom" for unit in units.values()
            ),
            "legacy_unknown_unit_count": sum(
                unit.get("semantic_basis") == "legacy_unknown_dimensions" for unit in units.values()
            ),
            "blockers": blockers, "errors": errors,
        }
    if inventory.get("schema_version") != "1.0" or inventory.get("policy") != "explicit_source_scope_inventory_v1":
        errors.append("scope_inventory_protocol_invalid")
    declared_binding = inventory.get("binding")
    if not isinstance(declared_binding, dict):
        errors.append("scope_inventory_binding_missing")
        declared_binding = {}
    for key in ("run_id", "case_id", "source_sha256", "format_spec_sha256"):
        if declared_binding.get(key) != expected_binding.get(key):
            errors.append("scope_inventory_binding_mismatch:" + key)
    scopes_for_attestation = inventory.get("scopes")
    scope_claims_complete = (
        isinstance(scopes_for_attestation, list)
        and any(isinstance(scope, dict) and scope.get("inventory_complete") is True
                for scope in scopes_for_attestation)
    )
    if inventory.get("inventory_complete") is True or scope_claims_complete:
        attestation = inventory.get("review_attestation")
        if (not isinstance(attestation, dict)
                or attestation.get("method") != "human_source_first_scope_review"
                or not attestation.get("reviewer_id")
                or attestation.get("reviewed_source_sha256") != expected_binding.get("source_sha256")):
            errors.append("source_first_scope_attestation_missing_or_stale")
    receipt_rows = receipt_audit.get("receipts")
    receipt_map: dict[str, dict[str, Any]] = {}
    if not isinstance(receipt_rows, list):
        errors.append("serialized_property_receipts_missing")
    else:
        for receipt in receipt_rows:
            if not isinstance(receipt, dict) or not isinstance(receipt.get("receipt_id"), str):
                errors.append("serialized_property_receipt_invalid")
                continue
            if receipt["receipt_id"] in receipt_map:
                errors.append("duplicate_serialized_property_receipt:" + receipt["receipt_id"])
            receipt_map[receipt["receipt_id"]] = receipt
    scopes = inventory.get("scopes")
    if not isinstance(scopes, list):
        errors.append("scope_inventory_scopes_invalid")
        scopes = []
    covered_ids: list[str] = []
    if inventory.get("inventory_complete") is not True:
        blockers.append("source_obligation_scope_inventory_incomplete")
    for scope in scopes:
        if not isinstance(scope, dict):
            errors.append("scope_record_invalid")
            continue
        source_binding = scope.get("source_binding")
        object_ref, condition = scope.get("object_ref"), scope.get("condition")
        if (not isinstance(source_binding, dict) or not isinstance(object_ref, str)
                or not isinstance(condition, str)
                or scope.get("scope_id") != expected_scope_id(source_binding, object_ref, condition)):
            errors.append("scope_identity_invalid")
        if (scope.get("inventory_complete") is True
                and (not isinstance(object_ref, str) or object_ref.strip().casefold() in {"unknown", "unspecified"}
                     or not isinstance(condition, str) or condition.strip().casefold() in {"unknown", "unspecified"})):
            errors.append("complete_scope_context_unresolved")
        if scope.get("inventory_complete") is not True:
            blockers.append("source_object_scope_incomplete:" + str(scope.get("scope_id")))
        if scope.get("importance") != "not_assessed":
            errors.append("importance_dimension_used_as_gate")
        priority = scope.get("review_priority")
        if (not isinstance(priority, dict)
                or priority.get("effect") != "sort_only_no_compliance_or_release_effect"):
            errors.append("review_priority_dimension_used_as_gate")
        expected_ids, atoms = scope.get("expected_atom_ids"), scope.get("atoms")
        if (not isinstance(expected_ids, list) or not isinstance(atoms, list)
                or any(not isinstance(atom, dict) for atom in atoms)):
            errors.append("scope_atom_inventory_invalid")
            continue
        atom_ids = [atom.get("evaluation_unit_id") for atom in atoms]
        if (len(expected_ids) != len(set(expected_ids))
                or len(atom_ids) != len(set(atom_ids)) or set(expected_ids) != set(atom_ids)):
            errors.append("scope_atom_inventory_mismatch")
            continue
        for atom in atoms:
            uid = atom.get("evaluation_unit_id")
            covered_ids.append(uid)
            unit = units.get(uid)
            if unit is None or unit.get("semantic_basis") != "typed_source_atom":
                errors.append("scope_atom_not_current_typed_unit:" + str(uid))
                continue
            context, assertion = unit.get("source_context"), unit.get("semantic_assertion")
            if (not isinstance(context, dict) or not isinstance(assertion, dict)
                    or context.get("source_sha256") != source_binding.get("source_sha256")
                    or context.get("clause_id") != source_binding.get("clause_id")
                    or not isinstance(context.get("span"), dict)
                    or sha256_json(context["span"]) != source_binding.get("source_span_sha256")
                    or sorted(context.get("evidence_ids") or []) != sorted(source_binding.get("evidence_ids") or [])
                    or assertion.get("target") != object_ref
                    or assertion.get("condition") != condition):
                errors.append("scope_atom_source_binding_mismatch:" + str(uid))
                continue
            force, applicability, route = unit.get("force"), unit.get("applicability"), unit.get("route")
            if force not in FORCES or applicability not in APPLICABILITIES or route not in ROUTES:
                errors.append("scope_atom_dimension_invalid:" + str(uid))
                continue
            if applicability == "not_applicable":
                basis = scope.get("applicability_evidence")
                if (not isinstance(basis, dict)
                        or basis.get("source_sha256") != source_binding.get("source_sha256")
                        or basis.get("clause_id") != source_binding.get("clause_id")
                        or sorted(basis.get("evidence_ids") or []) != sorted(source_binding.get("evidence_ids") or [])
                        or not isinstance(basis.get("quote"), str) or not basis["quote"]):
                    errors.append("not_applicable_without_source_evidence:" + str(uid))
                continue
            if force in {"required", "prohibited"} and applicability in {"unknown", "conflicted"}:
                blockers.append("hard_obligation_applicability_unresolved:" + str(uid))
            if force == "unknown":
                blockers.append("hard_obligation_force_unresolved:" + str(uid))
            receipt_ids = atom.get("evidence_receipt_ids")
            if not isinstance(receipt_ids, list) or len(receipt_ids) != len(set(receipt_ids)):
                errors.append("scope_atom_receipt_inventory_invalid:" + str(uid))
                continue
            if route in {"human", "input"}:
                blockers.append("human_owned_obligation_pending:" + str(uid))
                continue
            if route in {"unknown", "example"}:
                blockers.append("obligation_route_unresolved:" + str(uid))
            statuses = []
            for receipt_id in receipt_ids:
                receipt = receipt_map.get(receipt_id)
                if receipt is None:
                    errors.append("scope_atom_receipt_missing:" + str(receipt_id))
                    continue
                linked = {
                    item.get("evaluation_unit_id") for item in receipt.get("evaluation_units", [])
                    if isinstance(item, dict)
                }
                if uid not in linked:
                    errors.append("scope_atom_receipt_unit_mismatch:" + str(receipt_id))
                    continue
                if receipt.get("serialized_docx_sha256") != serialized_docx_sha256:
                    errors.append("scope_atom_receipt_stale_docx:" + str(receipt_id))
                    continue
                status = receipt.get("status")
                if status not in {"verified", "failed", "unverified"}:
                    errors.append("scope_atom_receipt_status_invalid:" + str(receipt_id))
                    continue
                statuses.append(status)
            if force in {"required", "prohibited"}:
                if "failed" in statuses:
                    blockers.append("hard_obligation_failed:" + str(uid))
                elif not statuses or any(status != "verified" for status in statuses):
                    blockers.append("hard_obligation_unresolved:" + str(uid))
            elif applicability in {"unknown", "conflicted"} or not statuses:
                blockers.append("obligation_unresolved:" + str(uid))
    typed_ids = {
        uid for uid, unit in units.items()
        if unit.get("semantic_basis") == "typed_source_atom"
    }
    legacy_count = sum(unit.get("semantic_basis") == "legacy_unknown_dimensions" for unit in units.values())
    if len(covered_ids) != len(set(covered_ids)):
        errors.append("source_atom_counted_in_multiple_scopes")
    if inventory.get("inventory_complete") is True and (
            set(covered_ids) != typed_ids or legacy_count > 0):
        blockers.append("scope_inventory_does_not_cover_all_source_units")
    errors = sorted(set(errors))
    blockers = sorted(set(blockers))
    return {
        "valid": not errors and not blockers, "required": True,
        "hard_gate_passed": not errors and not blockers,
        "submission_ready": False, "scope_inventory_complete": (
            inventory.get("inventory_complete") is True and not legacy_count
            and set(covered_ids) == typed_ids and all(
                isinstance(scope, dict) and scope.get("inventory_complete") is True for scope in scopes
            )
        ),
        "typed_source_unit_count": len(typed_ids), "scoped_source_unit_count": len(set(covered_ids)),
        "legacy_unknown_unit_count": legacy_count, "blockers": blockers, "errors": errors,
        "basis": "independent recomputation from source scope inventory and serialized receipts",
    }
