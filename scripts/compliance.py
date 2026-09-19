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
    "not_applicable": "not_applicable",
    "external_compliance": EXTERNAL_STATE,
    "informational": INFORMATIONAL_STATE,
    "requires_metadata": "requires_metadata",
    "requires_source_content": "requires_source_content",
    "unsupported_backend": "unsupported_backend",
    "unverifiable": "unverifiable",
}
ALLOWED_REVIEW_CLASSIFICATIONS = set(LEGACY_CLASSIFICATION_MAP) | set(DIRECT_CLASSIFICATION_MAP)
REQUIREMENT_CLASSIFICATIONS = {"covered", "executable", "verify_existing"}


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
        requirement_ids = sorted({rid for i in indexes for rid in requirement_ids_by_index.get(i, [])})
        mapping_blocked = False
        if classification_requires_requirement(classification):
            if (accepted_requirement_indexes is not None
                    and not set(indexes) <= accepted_requirement_indexes):
                mapping_blocked = True
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
) -> list[dict[str, Any]]:
    """Promote pending clauses after serialized-DOCX validation.

    A clause passes only when every normalized requirement produced from it has
    a backend result and no validation finding.  Unknown/non-executable roles
    therefore fail instead of being reported as not applicable.
    """
    by_id = {r.get("id"): r for r in requirements}
    output: list[dict[str, Any]] = []
    for original in records:
        record = dict(original)
        if record.get("status") != "pending_execution":
            output.append(record)
            continue
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
        output.append(record)
    return output


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
