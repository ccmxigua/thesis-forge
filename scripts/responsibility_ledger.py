"""Code-owned identities and responsibility routes, not execution receipts.

Unknown semantic dimensions stay unknown. Legacy records are not silently
upgraded and event AO/MO/MR IDs remain separate from canonical atom identities.
"""
from __future__ import annotations

import copy
import hashlib
import re
from typing import Any

from semantic_contract import sha256_json

FORCES = {"required", "prohibited", "recommended", "optional", "unknown"}
ROUTES = {"automatic", "check_only", "human", "input", "unknown", "example"}
APPLICABILITIES = {"applicable", "not_applicable", "unknown", "conflicted"}
WEIGHTS = {"required": 5, "prohibited": 5, "recommended": 2, "optional": 1, "unknown": None}
ATOM_FIELDS = {"actor", "action", "target", "condition", "source_quote", "force", "applicability", "route"}


def canonical_atom_identity(source: dict[str, Any], atom: dict[str, Any]) -> dict[str, str]:
    # All non-event semantic payload participates. Only receipts' representation
    # fields are excluded. Similar wording or a shared evidence is never merged.
    explicit = all(isinstance(atom.get(key), str) and atom[key] not in {"", "unknown"}
                   for key in ("actor", "action", "target", "source_quote"))
    semantic = {key: copy.deepcopy(value) for key, value in atom.items()
                if key not in {"status", "requirement_refs", "analysis_obligation_id", "reason"}
                and (key != "id" or not explicit)}
    if not explicit:
        semantic["legacy_reason"] = atom.get("reason")
    key = sha256_json({"protocol": "canonical_source_atom_v1", "source": source, "semantic": semantic})
    return {"canonical_obligation_key": key, "evaluation_unit_id": "EU-" + key[:32]}


def route_for_obligation(classification: str, status: str) -> str:
    if classification == "requires_source_verification":
        # The independent reviewer has identified a source/provenance question,
        # not a machine-checkable document property. Keep it in the human queue
        # until a separately bound human verification receipt exists.
        return "human"
    if status in {"requires_metadata", "requires_source_content"}:
        return "input"
    if classification == "external_compliance" or status == "unverifiable":
        return "human"
    if classification in {"informational", "ignored", "not_applicable"}:
        return "example"
    if status == "covered" and classification in {"covered", "executable", "executable_with_external_check"}:
        return "automatic"
    if status == "covered" and classification == "verify_existing":
        return "check_only"
    return "unknown"


def canonical_review_atom(source_sha256: Any, check_id: str, span: dict, atom: dict) -> dict:
    """Stable review unit; does not authorize reuse of any AO/run receipt."""
    source = {"source_sha256": source_sha256, "check_id": check_id,
              "text_sha256": span.get("source_sha256"), "start": span.get("start"), "end": span.get("end")}
    semantic = {key: copy.deepcopy(value) for key, value in atom.items()
                if key not in {"requirement_refs", "primary_obligation_id", "disposition"}}
    return canonical_atom_identity(source, semantic)


def requirement_evaluation_units(review_map: dict, clauses: dict, clause_ids: list, source_sha256: str) -> list[dict]:
    """Code-owned units, never a model-supplied score or requirement index.

    A typed source atom can be assessed by the independent review protocol.
    Legacy prose-only atoms keep unknown force/applicability and no weight.
    """
    if not isinstance(source_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        return []  # Legacy identity is unknown, not newly source-bound.
    units = {}
    for cid in clause_ids:
        clause, review = clauses[cid], review_map[cid]
        for atom in review.get("obligations") or []:
            if atom.get("status") != "covered":
                continue
            span = clause.get("source_span") or {}
            source = {"source_sha256": source_sha256, "span": span,
                      "evidence_ids": sorted(clause.get("evidence_ids") or [])}
            identity = canonical_atom_identity(source, atom)
            explicit = all(isinstance(atom.get(k), str) and atom[k] not in {"", "unknown"}
                           for k in ("actor", "action", "target", "source_quote"))
            unit = {**identity, "force": atom.get("force", "unknown") if explicit else "unknown",
                    "applicability": atom.get("applicability", "unknown") if explicit else "unknown",
                    "route": route_for_obligation(review.get("classification"), atom.get("status")),
                    "source_sha256": source_sha256, "source_span_sha256": sha256_json(span),
                    "semantic_basis": "typed_source_atom" if explicit else "legacy_unknown_dimensions"}
            key = unit["evaluation_unit_id"]
            if key in units and units[key] != unit:
                raise ValueError("conflicting evaluation unit identity")
            units[key] = unit
    return [units[key] for key in sorted(units)]


def validate_requirement_evaluation_units(
    spec: dict[str, Any],
    semantic_ledger: Any,
    source_clauses: list[dict[str, Any]],
    *,
    expected_run_id: str | None,
    expected_response_sha256: str | None,
) -> tuple[list[str], dict[str, list[dict[str, Any]]]]:
    """Recompute score units from the current accepted review ledger and clauses.

    Format-spec fields and property receipts are projections, not authorities
    for semantic force/applicability. This validator reconstructs the expected
    units from the run-bound semantic ledger and exact extracted clause set.
    """
    requirements = spec.get("requirements")
    if not isinstance(requirements, list) or any(not isinstance(item, dict) for item in requirements):
        return ["evaluation_units_requirements_invalid"], {}
    has_units = any("evaluation_units" in item for item in requirements)
    if not isinstance(semantic_ledger, dict):
        return (["evaluation_units_semantic_ledger_missing"] if has_units else []), {}
    errors: list[str] = []
    provenance = semantic_ledger.get("provenance")
    spec_provenance = spec.get("semantic_review_provenance")
    if not isinstance(provenance, dict) or provenance != spec_provenance:
        errors.append("evaluation_units_semantic_provenance_mismatch")
        provenance = {}
    source_sha256 = provenance.get("source_sha256")
    if (not isinstance(source_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", source_sha256)):
        errors.append("evaluation_units_source_identity_missing")
    if provenance.get("run_id") != expected_run_id or spec.get("run_id") != expected_run_id:
        errors.append("evaluation_units_run_id_mismatch")
    if semantic_ledger.get("response_sha256") != expected_response_sha256:
        errors.append("evaluation_units_response_receipt_mismatch")

    if not isinstance(source_clauses, list) or any(not isinstance(item, dict) for item in source_clauses):
        return errors + ["evaluation_units_source_clauses_invalid"], {}
    current_by_id: dict[str, dict[str, Any]] = {}
    for clause in source_clauses:
        clause_id = clause.get("id")
        if not isinstance(clause_id, str) or not clause_id or clause_id in current_by_id:
            errors.append("evaluation_units_source_clause_identity_invalid")
            continue
        current_by_id[clause_id] = clause
    if sha256_json(source_clauses) != provenance.get("clause_sha256"):
        errors.append("evaluation_units_current_clause_hash_mismatch")

    ledger_clauses = semantic_ledger.get("clauses")
    if not isinstance(ledger_clauses, list) or any(not isinstance(item, dict) for item in ledger_clauses):
        return errors + ["evaluation_units_ledger_clauses_invalid"], {}
    ledger_by_id: dict[str, dict[str, Any]] = {}
    for row in ledger_clauses:
        clause_id = row.get("clause_id")
        if not isinstance(clause_id, str) or not clause_id or clause_id in ledger_by_id:
            errors.append("evaluation_units_ledger_clause_identity_invalid")
            continue
        ledger_by_id[clause_id] = row
    if set(ledger_by_id) != set(current_by_id):
        errors.append("evaluation_units_ledger_clause_coverage_mismatch")

    clause_map: dict[str, dict[str, Any]] = {}
    review_map: dict[str, dict[str, Any]] = {}
    for clause_id, clause in current_by_id.items():
        row = ledger_by_id.get(clause_id)
        if row is None:
            continue
        source_text = str(clause.get("text") or clause.get("source_text_full") or "")
        if row.get("source_text_sha256") != hashlib.sha256(source_text.encode("utf-8")).hexdigest():
            errors.append("evaluation_units_clause_text_hash_mismatch:" + clause_id)
        obligations = row.get("obligations")
        if not isinstance(obligations, list) or any(not isinstance(atom, dict) for atom in obligations):
            errors.append("evaluation_units_obligation_records_invalid:" + clause_id)
            continue
        clause_map[clause_id] = clause
        review_map[clause_id] = {
            "classification": row.get("classification") or "unresolved",
            "obligations": obligations,
        }

    expected_by_requirement: dict[str, list[dict[str, Any]]] = {}
    for index, requirement in enumerate(requirements):
        requirement_id = requirement.get("id")
        if not isinstance(requirement_id, str) or not requirement_id:
            errors.append(f"evaluation_units_requirement_id_missing:{index}")
            continue
        resolved_by = requirement.get("resolved_by")
        if resolved_by in {"rule", "template", "user"}:
            if "evaluation_units" in requirement:
                errors.append("evaluation_units_non_semantic_requirement:" + requirement_id)
            # Evaluation units are a projection of the accepted semantic
            # review requirements. Deterministic/template/user requirements
            # can cite the same source clauses, but they do not inherit the
            # reviewer's obligation inventory merely through that citation.
            continue
        clause_ids = requirement.get("clause_ids") or []
        if not isinstance(clause_ids, list) or any(not isinstance(cid, str) for cid in clause_ids):
            errors.append("evaluation_units_requirement_clause_ids_invalid:" + requirement_id)
            continue
        if any(cid not in clause_map for cid in clause_ids):
            errors.append("evaluation_units_requirement_has_unbound_clause:" + requirement_id)
            continue
        try:
            expected = requirement_evaluation_units(
                review_map, clause_map, clause_ids, source_sha256,
            ) if isinstance(source_sha256, str) and re.fullmatch(r"[0-9a-f]{64}", source_sha256) else []
        except (KeyError, TypeError, ValueError):
            errors.append("evaluation_units_cannot_be_recomputed:" + requirement_id)
            continue
        declared = requirement.get("evaluation_units")
        if declared != expected and (declared is not None or expected):
            errors.append("evaluation_units_projection_mismatch:" + requirement_id)
        if expected:
            expected_by_requirement[requirement_id] = expected
    return errors, expected_by_requirement


def build_responsibility_ledger(response: dict, clauses: list[dict]) -> dict:
    if not isinstance(response, dict) or not isinstance(clauses, list):
        raise ValueError("responsibility ledger requires a response object and clause array")
    if any(not isinstance(clause, dict) or not isinstance(clause.get("id"), str)
           or not clause.get("id") for clause in clauses):
        raise ValueError("invalid responsibility source clause")
    reviews = response.get("clause_reviews", [])
    requirements = response.get("requirements", [])
    if not isinstance(reviews, list) or not isinstance(requirements, list):
        raise ValueError("responsibility source inventories must be arrays")
    by_id = {c["id"]: c for c in clauses}
    if len(by_id) != len(clauses):
        raise ValueError("duplicate source clause")
    atoms, edges, errors = [], [], []
    review_counts: dict[str, int] = {}
    for review in reviews:
        if not isinstance(review, dict) or not isinstance(review.get("clause_id"), str):
            errors.append({"code": "invalid_clause_review_identity"})
            continue
        cid = review["clause_id"]
        review_counts[cid] = review_counts.get(cid, 0) + 1
        if cid not in by_id:
            errors.append({"clause_id": cid, "code": "unknown_responsibility_source"})
            continue
        if review_counts[cid] > 1:
            errors.append({"clause_id": cid, "code": "duplicate_clause_review"})
            continue
        clause = by_id[cid]
        source = {"source_sha256": (response.get("provenance") or {}).get("source_sha256"),
                  "span": copy.deepcopy(clause.get("source_span")), "clause_id": cid,
                  "evidence_ids": copy.deepcopy(clause.get("evidence_ids", []))}
        linked = []
        for i, req in enumerate(requirements):
            if not isinstance(req, dict):
                errors.append({"requirement_index": i, "code": "invalid_requirement_record"})
                continue
            requirement_clause_ids = req.get("clause_ids") or []
            if not isinstance(requirement_clause_ids, list):
                errors.append({"requirement_index": i, "code": "invalid_requirement_clause_ids"})
                continue
            if any(source_id not in by_id for source_id in requirement_clause_ids):
                errors.append({"requirement_index": i, "code": "unknown_requirement_source"})
            if cid in requirement_clause_ids:
                linked.append(i)
        classification = review.get("classification", "unresolved")
        if classification in {"external_compliance", "informational", "requires_metadata", "requires_source_content",
                              "requires_source_verification", "unresolved", "unsupported_backend", "unverifiable"} and linked:
            errors.append({"clause_id": cid, "code": "unauthorized_document_proposal", "requirement_indexes": linked})
        obligations = review.get("obligations") or []
        if not isinstance(obligations, list):
            errors.append({"clause_id": cid, "code": "invalid_obligation_inventory"})
            continue
        for obligation in obligations:
            if not isinstance(obligation, dict):
                errors.append({"clause_id": cid, "code": "invalid_obligation_record"})
                continue
            force = obligation.get("force") or "unknown"
            applicability = obligation.get("applicability") or "unknown"
            route = route_for_obligation(classification, obligation.get("status"))
            if force not in FORCES or applicability not in APPLICABILITIES:
                raise ValueError("unknown responsibility dimension")
            proposed_route = obligation.get("route")
            if proposed_route is not None and proposed_route != route:
                errors.append({"clause_id": cid, "code": "responsibility_route_conflict", "obligation_id": obligation.get("id")})
            identity = canonical_atom_identity(source, obligation)
            atoms.append({**identity, "source": source, "producer_record": copy.deepcopy(obligation),
                          "classification": classification, "route": route,
                          "force": force, "applicability": applicability,
                          "actor": obligation.get("actor") or "unknown", "action": obligation.get("action") or "unknown",
                          "target": obligation.get("target") or "unknown", "condition": obligation.get("condition") or "unknown",
                          "modification_authority": "check_only" if route == "check_only" else "not_requested",
                          "execution_status": "not_executed", "human_status": "pending" if route == "human" else "not_applicable",
                          "checkability": "human" if route == "human" or classification == "requires_source_verification" else "unproven",
                          "mutability": "not_authorized" if route in {"check_only", "human", "input", "unknown", "example"} else "requires_execution_permission",
                          "integrity": "unknown",
                          "source_span_present": isinstance(clause.get("source_span"), dict),
                          "identity_quality": "explicit_semantic_atom" if all(obligation.get(k) for k in
                              ("actor", "action", "target", "source_quote")) else "legacy_unknown_dimensions",
                          "candidate_requirement_indexes": linked if route in {"automatic", "check_only"} else [],
                          "candidate_link_scope": "clause_level_only_not_property_or_execution_proof"})
    for cid in sorted(set(by_id) - set(review_counts)):
        errors.append({"clause_id": cid, "code": "missing_clause_review"})
    for index, req in enumerate(requirements):
        if not isinstance(req, dict):
            continue
        for cid in req.get("clause_ids", []):
            edges.append({"requirement_index": index, "clause_id": cid, "type": "property_basis",
                          "authority": "validated_requirements.clause_ids"})
        if req.get("role") == "declarations":
            for item in req.get("properties", {}).get("items", []):
                for eid in item.get("source_evidence_ids") or []:
                    edges.append({"requirement_index": index, "evidence_id": eid, "type": "render_entity",
                                  "authority": "validated_fixed_declaration_source", "property_authority": False})
    return {"schema_version": "1.0", "protocol": "responsibility_route_ledger_v1",
            "binding": copy.deepcopy(response.get("provenance", {})), "submission_ready": False,
            "coverage_complete": False,
            "review_coverage_complete": (
                set(review_counts) == set(by_id) and all(count == 1 for count in review_counts.values())
            ),
            "authorization_status": "not_an_authorization",
            "status": "invalid" if errors else "candidate_routes_valid",
            "atoms": atoms, "source_edges": edges, "errors": errors,
            "weight_policy": copy.deepcopy(WEIGHTS), "execution_proof": False}
