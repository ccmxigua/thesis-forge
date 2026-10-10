#!/usr/bin/env python3
"""Clause-level compliance states and strict full-compliance gating.

The requirements engine records what each source clause means.  The DOCX
application stage then promotes executable clauses from ``pending_execution``
to a verified terminal state.  Keeping this logic in one module prevents a
backend capability gap from being mislabeled as ``not_applicable`` or hidden
behind role-only coverage.
"""
from __future__ import annotations

from collections import Counter
import copy
import hashlib
import json
import re
from typing import Any, Iterable

COMPLIANCE_MODES = {"full", "supported_subset"}

# Terminal states accepted for the DOCX artifact itself.  Informational clauses
# are outside the obligation set; external_compliance is reported separately.
DOCX_PASS_STATES = {"generated_and_verified", "verified_existing", "not_applicable"}
DOCX_BLOCKING_STATES = {
    "unresolved", "missing", "requires_metadata", "requires_source_content",
    "input_provided_unverified", "unsupported_backend", "unverifiable", "failed",
}
# Semantic interpretation and backend verification prevent a formatting claim.
# Missing instance inputs are tracked separately: neutral placeholders may be
# formatted, but they still prevent the final submission claim.
FORMAT_BLOCKING_STATES = {
    "unresolved", "missing", "unsupported_backend", "unverifiable", "failed",
}
INPUT_PENDING_STATES = {
    "requires_metadata", "requires_source_content", "input_provided_unverified",
}
ANALYSIS_READY_STATES = {"pending_execution", *DOCX_PASS_STATES}
EXTERNAL_STATE = "external_compliance"
INFORMATIONAL_STATE = "informational"

LEGACY_CLASSIFICATION_MAP = {
    "covered": "pending_execution",
    "ignored": INFORMATIONAL_STATE,
    "unresolved": "unresolved",
    "unsupported": "unsupported_backend",
}
DIRECT_CLASSIFICATION_MAP = {
    "executable": "pending_execution",
    "verify_existing": "pending_execution",
    # A DOCX rule may coexist with a real-world action that the document
    # cannot prove.  Preserve the executable edge, but keep the clause blocked
    # for release until the external action has been independently verified.
    "executable_with_external_check": "unverifiable",
    "not_applicable": "not_applicable",
    "external_compliance": EXTERNAL_STATE,
    "informational": INFORMATIONAL_STATE,
    "requires_metadata": "requires_metadata",
    "requires_source_content": "requires_source_content",
    # The thesis/input exists; the remaining action is verification, not new
    # authoring. It remains blocking until the user records a review result.
    "requires_source_verification": "input_provided_unverified",
    "unsupported_backend": "unsupported_backend",
    "unverifiable": "unverifiable",
}
ALLOWED_REVIEW_CLASSIFICATIONS = set(LEGACY_CLASSIFICATION_MAP) | set(DIRECT_CLASSIFICATION_MAP)
REQUIREMENT_CLASSIFICATIONS = {
    "covered", "executable", "verify_existing", "executable_with_external_check",
}


def normalized_state(classification: str) -> str:
    return LEGACY_CLASSIFICATION_MAP.get(classification, DIRECT_CLASSIFICATION_MAP.get(classification, "unresolved"))


def classification_requires_requirement(classification: str) -> bool:
    return classification in REQUIREMENT_CLASSIFICATIONS


def build_clause_records(
    clauses: Iterable[dict[str, Any]],
    review_map: dict[str, dict[str, Any]],
    requirement_ids_by_index: dict[int, list[str]],
    accepted_requirement_indexes: set[int] | None = None,
) -> list[dict[str, Any]]:
    """Create one auditable compliance record for every extracted clause.

    ``requirement_indexes`` refer to the host response, not to the accepted
    requirements that survive semantic validation.  A rejected response item
    must therefore never become ``pending_execution`` with an empty ID list.
    When the accepted index set is supplied, such a mapping is downgraded to
    an explicit unresolved blocker while preserving the review reason.
    """
    records: list[dict[str, Any]] = []
    for clause in clauses:
        cid = clause["id"]
        review = review_map.get(cid)
        if review is None:
            records.append({
                "clause_id": cid, "evidence_ids": list(clause.get("evidence_ids", [])),
                "scope": "docx", "status": "missing", "requirement_ids": [],
                "reason": "No semantic review was supplied for this clause.",
            })
            continue
        classification = review["classification"]
        state = normalized_state(classification)
        indexes = review.get("requirement_indexes") or []
        # Contract 3.0 derives this reverse relation from every authored
        # requirement edge.  A response may contain a duplicate candidate
        # whose merge is rejected while another candidate still authoritatively
        # covers the same clause.  Keep only accepted indexes for the output
        # relation; one rejected duplicate must not invalidate an otherwise
        # accepted requirement.
        effective_indexes = (
            [index for index in indexes if index in accepted_requirement_indexes]
            if accepted_requirement_indexes is not None else list(indexes)
        )
        requirement_ids = sorted({
            rid for i in effective_indexes
            for rid in requirement_ids_by_index.get(i, [])
        })
        mapping_blocked = False
        if classification_requires_requirement(classification):
            if not requirement_ids:
                mapping_blocked = True
        if mapping_blocked:
            state = "unresolved"
            requirement_ids = []
        if state == EXTERNAL_STATE:
            scope = "external_submission"
        elif state == INFORMATIONAL_STATE:
            scope = "informational"
        else:
            scope = "docx"
        record = {
            "clause_id": cid,
            "evidence_ids": list(clause.get("evidence_ids", [])),
            "scope": scope,
            "status": state,
            "requirement_ids": requirement_ids,
            "reason": (
                review["reason"].strip()
                + (
                    " The review references a requirement that was rejected or is not "
                    "backed by an accepted executable requirement."
                    if mapping_blocked else ""
                )
            ).strip(),
        }
        obligations = review.get("obligations")
        if isinstance(obligations, list):
            # The host-review ledger carries the full actor/action/route/source
            # binding. The compiled format spec intentionally stores the
            # compact projection defined by format-spec.schema.json; retain
            # the semantic detail in the separate review ledger.
            record["obligations"] = [
                {
                    "id": item.get("id"),
                    "status": item.get("status"),
                    "reason": item.get("reason"),
                }
                for item in obligations
                if isinstance(item, dict)
            ]
        if mapping_blocked:
            # No executable requirement ID is retained for an unresolved
            # mapping; the schema and downstream gates must see a blocker.
            pass
        elif classification == "verify_existing":
            record["enforcement"] = "verify_existing"
        elif classification_requires_requirement(classification):
            record["enforcement"] = "generate_or_repair"
        records.append(record)
    return records


def summarize(records: list[dict[str, Any]], mode: str = "full", phase: str = "analysis") -> dict[str, Any]:
    if mode not in COMPLIANCE_MODES:
        raise ValueError(f"unknown compliance mode: {mode}")
    docx = [r for r in records if r.get("scope") == "docx"]
    external = [r for r in records if r.get("scope") == "external_submission"]
    informational = [r for r in records if r.get("scope") == "informational"]
    counts = Counter(r.get("status", "missing") for r in docx)
    blockers = [r for r in docx if r.get("status") in DOCX_BLOCKING_STATES]
    format_blockers = [r for r in docx if r.get("status") in FORMAT_BLOCKING_STATES]
    input_pending = [r for r in docx if r.get("status") in INPUT_PENDING_STATES]
    pending = [r for r in docx if r.get("status") == "pending_execution"]

    # supported_subset remains an explicit compatibility/testing mode.  It is
    # never allowed to call the artifact fully compliant.
    execution_blockers = format_blockers if mode == "full" else [
        r for r in blockers if r.get("status") in {"unresolved", "missing", "failed"}
    ]
    execution_ready = not execution_blockers
    format_ready = not format_blockers and not pending and all(
        r.get("status") in DOCX_PASS_STATES | INPUT_PENDING_STATES for r in docx
    )
    fully_compliant = format_ready and not input_pending
    if phase == "analysis":
        status = "ready_for_execution" if execution_ready else "failed"
    elif fully_compliant:
        status = "passed"
    elif format_ready and input_pending:
        status = "input_pending"
    elif mode == "supported_subset" and execution_ready and not pending:
        status = "supported_subset_passed"
    else:
        status = "failed"
    return {
        "schema_version": "1.0",
        "mode": mode,
        "phase": phase,
        "overall_status": status,
        "execution_ready": execution_ready,
        "format_ready": format_ready,
        "docx_fully_compliant": fully_compliant,
        "docx_compliance": {
            "status": "passed" if fully_compliant else ("pending" if pending and not blockers else "failed"),
            "applicable_requirements": len(docx),
            "counts": dict(sorted(counts.items())),
            "pending_clause_ids": [r["clause_id"] for r in pending],
            "blocking_clause_ids": [r["clause_id"] for r in blockers],
            "format_blocking_clause_ids": [r["clause_id"] for r in format_blockers],
            "input_pending_clause_ids": [r["clause_id"] for r in input_pending],
            "execution_blocking_clause_ids": [r["clause_id"] for r in execution_blockers],
        },
        "external_submission": {
            "status": "pending" if external else "not_applicable",
            "count": len(external),
            "clause_ids": [r["clause_id"] for r in external],
        },
        "informational_clauses": len(informational),
    }


def finalize_records(
    records: list[dict[str, Any]],
    requirements: list[dict[str, Any]],
    role_results: dict[str, bool],
    finding_roles: set[str],
    *,
    current_docx_sha256: str | None = None,
    property_receipt_audit: dict[str, Any] | None = None,
    role_mappings: dict[str, dict[str, Any]] | None = None,
    promote_pending: bool = True,
) -> list[dict[str, Any]]:
    """Promote pending clauses after serialized-DOCX validation.

    A clause passes only when every normalized requirement produced from it has
    a backend result and no validation finding. Unknown/non-executable roles
    therefore fail instead of being reported as not applicable. Callers that
    audit a new DOCX instance against an earlier semantic report set
    ``promote_pending=False`` so historical pending states are not rewritten
    by an absent backend snapshot.
    """
    by_id = {r.get("id"): r for r in requirements}
    receipt_rows = (property_receipt_audit.get("receipts", [])
                    if isinstance(property_receipt_audit, dict) else [])
    expected_receipt_ids = (property_receipt_audit.get("expected_receipt_ids")
                            if isinstance(property_receipt_audit, dict) else None)
    role_mappings = role_mappings or {}
    if not isinstance(receipt_rows, list):
        receipt_rows = []
    receipt_rows_by_id: dict[str, list[dict[str, Any]]] = {}
    for item in receipt_rows:
        if isinstance(item, dict) and isinstance(item.get("receipt_id"), str):
            receipt_rows_by_id.setdefault(item["receipt_id"], []).append(item)

    def same_value(left: Any, right: Any) -> bool:
        if (isinstance(left, (int, float)) and not isinstance(left, bool)
                and isinstance(right, (int, float)) and not isinstance(right, bool)):
            return abs(float(left) - float(right)) <= .05
        return left == right

    def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
        result: dict[str, Any] = {}
        if isinstance(value, dict):
            for key, child in value.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                result.update(flatten(child, path))
        else:
            result[prefix] = value
        return result

    def current_instance(record: dict[str, Any], reqs: list[dict[str, Any]]) -> dict[str, Any]:
        evidence: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        unknown: list[dict[str, Any]] = []
        hash_valid = (isinstance(current_docx_sha256, str)
                      and re.fullmatch(r"[0-9a-f]{64}", current_docx_sha256) is not None)
        expected_ids_valid = (isinstance(expected_receipt_ids, list)
                              and all(isinstance(value, str) for value in expected_receipt_ids)
                              and len(set(expected_receipt_ids)) == len(expected_receipt_ids))
        expected_set = set(expected_receipt_ids) if expected_ids_valid else set()
        if not hash_valid or not expected_ids_valid:
            return {
                "protocol": "current_docx_instance_validation_v1",
                "docx_sha256": current_docx_sha256 if hash_valid else None,
                "status": "unverified",
                "reason": "current DOCX hash or complete expected receipt inventory is missing",
                "requirement_results": [],
            }
        if not reqs:
            return {
                "protocol": "current_docx_instance_validation_v1",
                "docx_sha256": current_docx_sha256,
                "status": "unverified",
                "reason": "the historical record has no currently accepted requirement binding",
                "requirement_results": [],
            }
        for requirement in reqs:
            rid = requirement.get("id")
            role = requirement.get("role")
            props = requirement.get("properties")
            if not isinstance(rid, str) or not isinstance(role, str) or not isinstance(props, dict):
                unknown.append({"requirement_id": rid, "reason": "requirement identity or properties are incomplete"})
                continue
            paths = flatten(props)
            ids = sorted(x for x in expected_set if x.startswith(f"PR-{rid}-"))
            if not ids:
                unknown.append({"requirement_id": rid, "reason": "no expected property receipts bind this requirement"})
                continue
            requirement_evidence: list[dict[str, Any]] = []
            for receipt_id in ids:
                candidates = receipt_rows_by_id.get(receipt_id, [])
                if len(candidates) != 1:
                    unknown.append({"requirement_id": rid, "receipt_id": receipt_id,
                                    "reason": "current receipt is missing or duplicated"})
                    continue
                receipt = candidates[0]
                path = receipt.get("property_path")
                identity_matches = (
                    receipt.get("requirement_id") == rid
                    and receipt.get("role") == role
                    and isinstance(path, str)
                    and path in paths
                    and same_value(receipt.get("expected"), paths.get(path))
                    and isinstance(receipt.get("target_locator"), str)
                    and bool(receipt.get("target_locator"))
                )
                mapping = role_mappings.get(role) if isinstance(role, str) else None
                mapped_target = (f"style:{mapping.get('style_name')}"
                                 if isinstance(mapping, dict) and mapping.get("style_name")
                                 else f"role:{role}")
                identity_matches = identity_matches and receipt.get("target_locator") == mapped_target
                if not identity_matches:
                    unknown.append({"requirement_id": rid, "receipt_id": receipt_id,
                                    "reason": "receipt requirement, role, property, expected value, or target locator does not match"})
                    continue
                if receipt.get("serialized_docx_sha256") != current_docx_sha256:
                    unknown.append({"requirement_id": rid, "receipt_id": receipt_id,
                                    "reason": "receipt is bound to different DOCX bytes"})
                    continue
                result = {"requirement_id": rid, "receipt_id": receipt_id,
                          "role": role, "property_path": path,
                          "target_locator": receipt["target_locator"],
                          "docx_sha256": current_docx_sha256,
                          "receipt_status": receipt.get("status")}
                requirement_evidence.append(result)
                evidence.append(result)
                if receipt.get("status") == "failed":
                    failed.append(result)
                elif receipt.get("status") != "verified":
                    unknown.append({**result, "reason": "receipt is not verified"})
            if len(requirement_evidence) != len(ids):
                unknown.append({"requirement_id": rid,
                                "reason": "not every expected property has a unique current DOCX-bound receipt"})
        status = "failed" if failed else "unverified" if unknown else "verified"
        return {
            "protocol": "current_docx_instance_validation_v1",
            "docx_sha256": current_docx_sha256,
            "status": status,
            "requirement_results": evidence,
            "findings": failed,
            "unverified_reasons": unknown,
        }

    output: list[dict[str, Any]] = []
    for original in records:
        record = dict(original)
        initial_status = record.get("status")
        if initial_status == "pending_execution" and promote_pending:
            reqs = [by_id.get(rid) for rid in record.get("requirement_ids", [])]
            reqs = [r for r in reqs if r]
            roles = sorted({r.get("role") for r in reqs if r.get("role")})
            missing_roles = [role for role in roles if not role_results.get(role, False)]
            failed_roles = [role for role in roles if role in finding_roles]
            if not reqs:
                record["status"] = "failed"
                record["verification_reason"] = "No accepted executable requirement backs this clause."
            elif missing_roles:
                record["status"] = "unsupported_backend"
                record["verification_reason"] = f"No executable/verified backend result for roles: {', '.join(missing_roles)}"
            elif failed_roles:
                record["status"] = "failed"
                record["verification_reason"] = f"Validation findings remain for roles: {', '.join(failed_roles)}"
            else:
                record["status"] = "verified_existing" if record.get("enforcement") == "verify_existing" else "generated_and_verified"
                record["verification_reason"] = "All normalized requirements were applied or confirmed and passed serialized-DOCX validation."
        if (current_docx_sha256 is not None
                and record.get("status") in {"generated_and_verified", "verified_existing"}):
            record.setdefault("historical_status", initial_status)
            reqs = [by_id.get(rid) for rid in record.get("requirement_ids", [])]
            reqs = [r for r in reqs if isinstance(r, dict)]
            assessment = current_instance(record, reqs)
            record["current_output_instance"] = assessment
            if assessment["status"] == "failed":
                record["status"] = "failed"
                record["verification_reason"] = "A current DOCX-bound property receipt failed for this exact source obligation."
            elif assessment["status"] == "unverified":
                record["status"] = "unverifiable"
                record["verification_reason"] = "The prior status is historical; current DOCX-bound property evidence is missing or unverified."
            else:
                record["verification_reason"] = "Current DOCX-bound property receipts for every linked requirement passed."
        output.append(record)
    return output


def reconcile_rendered_output_records(
    records: list[dict[str, Any]], requirements: list[dict[str, Any]],
    role_mappings: dict[str, dict[str, Any]], report: dict[str, Any],
    current_docx_sha256: str,
) -> dict[str, Any]:
    """Apply exact rendered findings without rewriting source-review history.

    A style/font finding can affect a clause only when its requirement ID,
    role, source property, mapped style, and expected font all agree. Findings
    without that unique semantic edge remain current-output diagnostics and do
    not get attributed to unrelated clauses.
    """
    report_payload = {key: value for key, value in report.items() if key != "audit_sha256"}
    digest = report.get("audit_sha256")
    final_docx = report.get("final_docx")
    if (report.get("protocol") != "rendered_format_audit_v1"
            or report.get("docx_sha256") != current_docx_sha256
            or not isinstance(final_docx, dict)
            or final_docx.get("sha256") != current_docx_sha256
            or not isinstance(report.get("pdf_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", report.get("pdf_sha256", "")) is None
            or report.get("submission_ready") is not False
            or report.get("field_refresh_claimed") is not False
            or not isinstance(report.get("findings"), list)
            or not isinstance(digest, str)):
        raise ValueError("rendered output findings are not bound to the current DOCX")
    try:
        from semantic_contract import sha256_json
    except ImportError:
        from .semantic_contract import sha256_json
    if sha256_json(report_payload) != digest:
        raise ValueError("rendered output report integrity hash is invalid")

    by_id = {item.get("id"): item for item in requirements if isinstance(item, dict)}
    requirement_ids_by_font: dict[tuple[str, str, str, str], set[str]] = {}
    for requirement in requirements:
        if not isinstance(requirement, dict):
            continue
        rid, role = requirement.get("id"), requirement.get("role")
        props = requirement.get("properties")
        mapping = role_mappings.get(role) if isinstance(role, str) else None
        style = mapping.get("style_name") if isinstance(mapping, dict) else None
        if not isinstance(rid, str) or not isinstance(role, str) or not isinstance(style, str):
            continue
        flattened: dict[str, Any] = {}

        def visit(value: Any, prefix: str = "") -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    visit(child, f"{prefix}.{key}" if prefix else str(key))
            else:
                flattened[prefix] = value

        visit(props)
        for path, value in flattened.items():
            if path in {"font.cjk", "font.latin"} and isinstance(value, str):
                script = "cjk" if path.endswith(".cjk") else "latin"
                requirement_ids_by_font.setdefault((role, style, script, value), set()).add(rid)

    output = copy.deepcopy(records)
    clause_by_requirement: dict[str, list[dict[str, Any]]] = {}
    for record in output:
        for rid in record.get("requirement_ids", []):
            if isinstance(rid, str):
                clause_by_requirement.setdefault(rid, []).append(record)
        if record.get("status") in {"generated_and_verified", "verified_existing"}:
            record.setdefault("historical_status", record.get("status"))
            instance = record.get("current_output_instance")
            if (not isinstance(instance, dict)
                    or instance.get("docx_sha256") != current_docx_sha256):
                record["status"] = "unverifiable"
                record["verification_reason"] = "Current DOCX instance lacks a matching property receipt."
                record["current_output_instance"] = {
                    "protocol": "current_docx_instance_validation_v1",
                    "docx_sha256": current_docx_sha256,
                    "status": "unverified",
                    "reason": "no current-hash serialized property evidence was supplied",
                }

    unbound_findings: list[dict[str, Any]] = []
    bound_findings: list[dict[str, Any]] = []
    for finding in report.get("findings", []):
        if not isinstance(finding, dict):
            continue
        code = finding.get("code")
        targets: set[str] = set()
        severity = "unverified"
        expected = finding.get("expected") if isinstance(finding.get("expected"), dict) else {}
        if code == "rendered_pdf_font_mismatch":
            role, style, script = expected.get("role"), expected.get("style_name"), expected.get("script")
            font = expected.get("name")
            code_owned_ids = requirement_ids_by_font.get((role, style, script, font), set())
            reported_ids = expected.get("requirement_ids")
            if (isinstance(reported_ids, list)
                    and all(isinstance(item, str) for item in reported_ids)
                    and sorted(set(reported_ids)) == sorted(code_owned_ids)):
                targets = code_owned_ids
            severity = "failed"
        elif code in {"rendered_drawing_clipped_by_page", "rendered_drawing_extent_mismatch"}:
            severity = "failed"
        elif code in {"drawing_not_found_in_rendered_pdf", "drawing_match_ambiguous"}:
            severity = "unverified"
        matching_records = {
            id(record): record for rid in targets for record in clause_by_requirement.get(rid, [])
        }
        if matching_records:
            for record in matching_records.values():
                record["status"] = "failed" if severity == "failed" else "unverifiable"
                record["verification_reason"] = (
                    "A current, source-bound rendered output finding applies to this requirement."
                    if severity == "failed" else
                    "Rendered output could not uniquely verify this bound requirement."
                )
                record["current_output_instance"] = {
                    "protocol": "current_docx_instance_validation_v1",
                    "docx_sha256": current_docx_sha256,
                    "pdf_sha256": report.get("pdf_sha256"),
                    "status": "failed" if severity == "failed" else "unverified",
                    "finding": copy.deepcopy(finding),
                }
            bound_findings.append({"finding": copy.deepcopy(finding),
                                   "requirement_ids": sorted(targets),
                                   "status": severity})
        else:
            unbound_findings.append({"finding": copy.deepcopy(finding),
                                     "status": severity,
                                     "reason": "no exact requirement/role/property/style binding was established"})
    return {
        "schema_version": "1.0", "protocol": "rendered_output_compliance_reconciliation_v1",
        "docx_sha256": current_docx_sha256, "pdf_sha256": report.get("pdf_sha256"),
        "records": output, "bound_rendered_findings": bound_findings,
        "unbound_rendered_findings": unbound_findings,
        "submission_ready": False,
    }


def annotate_satisfied_inputs(
    records: list[dict[str, Any]],
    capability_clauses: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Carry capability-preflight input evidence into the final report.

    Metadata/source values being present is not by itself proof that every
    required output location is correct.  Preserve that distinction instead
    of misleadingly reporting the value as still missing.
    """
    by_id = {
        str(item.get("clause_id")): item
        for item in capability_clauses
        if isinstance(item, dict) and item.get("clause_id")
    }
    output: list[dict[str, Any]] = []
    for original in records:
        record = dict(original)
        capability = by_id.get(str(record.get("clause_id")))
        # Applicability is a deterministic capability decision.  Carry the
        # explicit external/not-applicable state into the final compliance
        # report instead of allowing finalize_records to treat a conditional
        # requirement as an executable DOCX obligation.
        if (capability and capability.get("category") == "external_not_applicable"
                and capability.get("disposition") == "not_applicable"):
            record["status"] = "not_applicable"
            record["scope"] = capability.get("scope") or record.get("scope")
            record["applicability_status"] = "false"
            record["applicability_reason"] = capability.get("reason") or (
                "The declared applicability facts evaluated to false."
            )
            output.append(record)
            continue
        if record.get("status") in INPUT_PENDING_STATES:
            # Keep the legacy clause state for downstream compatibility, but
            # expose the two release dimensions explicitly: the DOCX can be
            # format-ready while the final submission claim remains pending.
            record["format_status"] = "input_pending"
            record["submission_status"] = "input_pending"
        evidence = capability if (
            capability
            and capability.get("category") in {"supported", "input_prerequisite_satisfied"}
            and (capability.get("metadata_fields")
                 or capability.get("template_fixed_fields")
                 or capability.get("input_evidence"))
        ) else None
        if evidence and record.get("status") in {"requires_metadata", "requires_source_content"}:
            record["source_prerequisite_status"] = record["status"]
            record["status"] = "input_provided_unverified"
            if evidence.get("metadata_fields"):
                record["metadata_fields"] = list(evidence["metadata_fields"])
            if evidence.get("template_fixed_fields"):
                record["template_fixed_fields"] = list(evidence["template_fixed_fields"])
            if evidence.get("input_evidence"):
                record["input_evidence"] = list(evidence["input_evidence"])
            record["input_status"] = evidence.get("input_status", "provided")
            record["binding_status"] = evidence.get("binding_status", "bound")
            record["output_status"] = evidence.get("output_status", "unverified")
            record["reason"] = evidence.get("reason") or (
                "Required input is present, but its rendered output location has not yet "
                "been verified clause-by-clause."
            )
        output.append(record)
    return output


def _external_kind(reason: str) -> str:
    if any(token in reason for token in ("签字", "签署", "signature")): return "handwritten_signature"
    if any(token in reason for token in ("封皮", "封面颜色", "书脊", "装订", "打印")): return "physical_artifact"
    if any(token in reason for token in ("导师", "备案", "批准", "分委员会", "密级", "资格")): return "administrative_eligibility"
    if any(token in reason for token in ("目录", "培养方案", "分类号")): return "authoritative_catalog"
    return "external_submission"


def report(records: list[dict[str, Any]], mode: str, phase: str) -> dict[str, Any]:
    result = summarize(records, mode, phase)
    result["records"] = records
    result["external_checklist"] = [
        {"clause_id": r["clause_id"], "reason": r.get("reason", ""),
         "status": "requires_external_verification", "verification_kind": _external_kind(r.get("reason", "")),
         "responsible_party": "university_or_submitter"}
        for r in records if r.get("scope") == "external_submission"
    ]
    return result
