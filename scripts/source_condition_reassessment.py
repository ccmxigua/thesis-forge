"""Bounded primary scope proposals after a source-first review disagrees.

The rejected review authorizes a new proposal, not an interpretation. No
scope value is copied from the reviewer, and no old candidate is accepted.
"""
from __future__ import annotations

import copy
import re

from native_semantic_review import (
    TypedSourceAtomAlignmentError, NativeSemanticReviewError,
    build_obligation_coverage_request, validate_obligation_coverage_response,
)
from semantic_contract import sha256_json
from source_atom_metadata import bind_atom_quote
from source_literal_binding import compose_source_fragments

CODE = "primary_condition_reassessment_required"
RULE_ID = "v3_source_bound_condition_reassessment"
TARGET_CODE = "primary_target_reassessment_required"
TARGET_RULE_ID = "v3_source_bound_target_reassessment"
APPLICABILITY_CODE = "primary_applicability_reassessment_required"
APPLICABILITY_RULE_ID = "v3_source_bound_applicability_reassessment"
REASSESSMENT_CODES = frozenset({CODE, TARGET_CODE, APPLICABILITY_CODE})
RETRY_BUDGET_POLICY = "v3_regular_attempts_plus_one_verified_scope_proposal"
MAX_PRIMARY_CONDITION_PROPOSALS = 1


def condition_feedback(candidate, chunk, request, rejected_review):
    """Reconstruct the complete rejected source review; only condition differs."""
    return _scope_feedback(candidate, chunk, request, rejected_review, allow_target=False)


def source_atom_feedback(candidate, chunk, request, rejected_review):
    """Rejected scope fields authorize a proposal opportunity, never equivalence.

    Validate the complete rejected review before authorizing any named field.
    Conditions-only feedback retains its existing contract. Target feedback
    freezes every other dimension and may include separately disputed conditions.
    Applicability feedback never supplies a default or a replacement value.
    """
    return _scope_feedback(candidate, chunk, request, rejected_review, allow_target=True)


def _scope_feedback(candidate, chunk, request, rejected_review, *, allow_target):
    if not all(isinstance(v, dict) for v in (candidate, chunk, request, rejected_review)):
        return None
    provenance = chunk.get("provenance")
    if (candidate.get("contract_version") != "3.0" or not isinstance(provenance, dict)
            or not provenance.get("run_id") or candidate.get("provenance") != provenance
            or request.get("run_id") != provenance["run_id"]
            or candidate.get("reported_conflicts") or candidate.get("conflicts")):
        return None
    try:
        expected = build_obligation_coverage_request(
            candidate, chunk, run_id=request["run_id"], chunk_index=request["chunk_index"])
        for key in ("attempt", "provider_attempt", "output_policy", "retry_feedback"):
            if key in request:
                expected[key] = copy.deepcopy(request[key])
        if expected != request:
            return None
        checks = request["checks"]
        results = rejected_review["results"]
        by_result = {r["check_id"]: r for r in results}
        if (len(by_result) != len(results)
                or set(by_result) != {c["check_id"] for c in checks}):
            return None
        clauses = chunk["clauses"]
        by_clause = {c["id"]: c for c in clauses}
        if len(by_clause) != len(clauses):
            return None
        changes = []
        for check in checks:
            cid = check["check_id"]
            try:
                validate_obligation_coverage_response({"results": [by_result[cid]]}, [check])
            except TypedSourceAtomAlignmentError as error:
                for disagreement in error.disagreements:
                    fields = disagreement["fields"]
                    allowed = {"target", "condition", "applicability"} if allow_target else {"condition"}
                    if not fields or any(field not in allowed for field in fields):
                        return None
                    oid = disagreement["primary_obligation_id"]
                    primaries = check["review_context"]["primary_obligations"]
                    primary = next(p for p in primaries if p["id"] == oid)
                    binding = compose_source_fragments([cid], by_clause, chunk["evidence_context"])
                    quote = bind_atom_quote(primary["source_quote"], cid, by_clause, chunk["evidence_context"])
                    changes.append({"clause_id": cid, "obligation_id": oid, "fields": list(fields),
                        "primary_sha256": sha256_json(primary),
                        "source_binding": binding, "quote_binding": quote})
            except NativeSemanticReviewError:
                return None  # Other missing inventories/fields are not this repair.
        if not changes:
            return None
        target_dispute = any("target" in change["fields"] for change in changes)
        applicability_dispute = any("applicability" in change["fields"] for change in changes)
        if not target_dispute and not applicability_dispute:
            # Preserve the existing conditions-only record representation.
            for change in changes:
                change.pop("fields")
        atoms_key = "source_atoms" if target_dispute or applicability_dispute else "condition_atoms"
        return {"code": APPLICABILITY_CODE if applicability_dispute else TARGET_CODE if target_dispute else CODE,
            "candidate_response_sha256": sha256_json(candidate),
            "source_chunk_sha256": sha256_json(chunk), "run_id": provenance["run_id"],
            "provenance": copy.deepcopy(provenance), atoms_key: changes,
            "review_request": copy.deepcopy(request), "rejected_review": copy.deepcopy(rejected_review),
            "review_request_sha256": sha256_json(request),
            "rejected_review_sha256": sha256_json(rejected_review),
            "semantic_review_required": True, "submission_ready": False}
    except (NativeSemanticReviewError, ValueError, KeyError, TypeError, IndexError, StopIteration):
        return None


def _semantic(value):
    value = copy.deepcopy(value)
    value.pop("provenance", None)
    return value


def condition_proposal_budget_receipt(candidate, chunk, records, normalized_raw_parent):
    """Authenticate a separate one-shot proposal opportunity, never a pass.

    A locally valid candidate may first reach independent review after ordinary
    repair attempts are exhausted. Rebuild the complete source-bound feedback
    before reserving this opportunity; an error-code string alone is not enough.
    Actual proposed edits still need condition_reassessment and a fresh review.
    """
    if (not isinstance(records, list) or len(records) != 1
            or not isinstance(records[0], dict) or records[0].get("code") not in REASSESSMENT_CODES
            or not isinstance(normalized_raw_parent, dict)):
        return None
    record = records[0]
    if record.get("response_sha256") != sha256_json(normalized_raw_parent):
        return None
    rebuilt = source_atom_feedback(candidate, chunk, record.get("review_request"),
                                 record.get("rejected_review"))
    if rebuilt is None or any(record.get(key) != value for key, value in rebuilt.items()):
        return None
    atoms_key = "condition_atoms" if rebuilt["code"] == CODE else "source_atoms"
    return {"policy_version": RETRY_BUDGET_POLICY,
            "reassessment_code": rebuilt["code"],
            "proposal_limit": MAX_PRIMARY_CONDITION_PROPOSALS,
            "feedback_sha256": sha256_json(record),
            "normalized_raw_parent_sha256": sha256_json(normalized_raw_parent),
            "candidate_response_sha256": rebuilt["candidate_response_sha256"],
            "source_chunk_sha256": rebuilt["source_chunk_sha256"],
            "run_id": rebuilt["run_id"],
            atoms_key: [{"clause_id": item["clause_id"],
                         "obligation_id": item["obligation_id"],
                         "fields": item.get("fields", ["condition"])}
                        for item in rebuilt[atoms_key]],
            "semantic_review_required": True, "submission_ready": False}


def condition_reassessment(previous, current, records, paths, chunk, *, prepare, validate):
    """Authorize only named scope fields; label display is condition-only.

Raw and projected candidates must each satisfy the same narrow transition.
The code never chooses a replacement target, condition, applicability or metadata value.
"""
    if (not isinstance(previous, dict) or not isinstance(current, dict)
            or not isinstance(chunk, dict) or not isinstance(records, list)
            or len(records) != 1 or not isinstance(records[0], dict)
            or records[0].get("code") not in REASSESSMENT_CODES or not paths
            or len(paths) != len(set(paths))):
        return None
    record = records[0]
    try:
        previous_sha = sha256_json(previous)
        if previous_sha == record.get("candidate_response_sha256"):
            candidate = copy.deepcopy(previous)
        elif previous_sha == record.get("response_sha256"):
            candidate = prepare(copy.deepcopy(previous), chunk)[0]
            candidate["provenance"] = copy.deepcopy(chunk["provenance"])
        else:
            return None
        rebuilt = source_atom_feedback(candidate, chunk, record["review_request"], record["rejected_review"])
        if rebuilt is None or any(record.get(k) != v for k, v in rebuilt.items()):
            return None
        expected = copy.deepcopy(previous)
        permitted = {}
        atoms_key = "condition_atoms" if rebuilt["code"] == CODE else "source_atoms"
        for entry in rebuilt[atoms_key]:
            cid, oid = entry["clause_id"], entry["obligation_id"]
            matching = [(i, j) for i, review in enumerate(previous["clause_reviews"])
                        if review["clause_id"] == cid
                        for j, atom in enumerate(review["obligations"]) if atom["id"] == oid]
            if len(matching) != 1:
                return None
            i, j = matching[0]
            after = current["clause_reviews"][i]["obligations"][j]
            fields = entry.get("fields", ["condition"])
            for field in fields:
                value = after.get(field)
                if (field == "target" and (not isinstance(value, str) or not value.strip()
                        or value.strip().lower() == "unknown")):
                    return None
                if field == "condition" and value is not None and not isinstance(value, str):
                    return None
                if field == "applicability" and (not isinstance(value, str) or value not in {
                        "applicable", "not_applicable", "unknown", "conflicted"}):
                    return None
                if (field == "applicability" and value in {"unknown", "conflicted"}
                        and after.get("status") == "covered"):
                    return None  # An undecided scope cannot establish coverage.
                if field in after:
                    expected["clause_reviews"][i]["obligations"][j][field] = value
                else:
                    expected["clause_reviews"][i]["obligations"][j].pop(field, None)
                permitted[f"$.clause_reviews[{i}].obligations[{j}].{field}"] = entry
            # A label/value separation is possible only for the exact field
            # already linked to this same current source label. Values, value
            # policy, identities, applicability and all other payload stay.
            if "condition" not in fields:
                continue  # A target disagreement never authorizes cover edits.
            source = re.sub(r"\s+|[：:]", "", entry["source_binding"]["text"])
            for ri, requirement in enumerate(previous["requirements"]):
                if (requirement.get("role") not in {"cover", "cover_outer", "cover_inner"}
                        or cid not in requirement.get("clause_ids", [])):
                    continue
                properties = requirement.get("properties", {})
                containers = [("fields", properties, current["requirements"][ri]["properties"],
                               expected["requirements"][ri]["properties"])]
                if isinstance(properties.get("non_public_administration"), dict):
                    containers.append(("non_public_administration.fields", properties["non_public_administration"],
                        current["requirements"][ri]["properties"]["non_public_administration"],
                        expected["requirements"][ri]["properties"]["non_public_administration"]))
                for prefix, before_container, after_container, expected_container in containers:
                    for fi, field in enumerate(before_container.get("fields", [])):
                        if (field.get("id") in {"title_zh", "title_en"}
                                or re.sub(r"\s+|[：:]", "", str(field.get("label", ""))) != source
                                or field.get("label_display_policy") not in {None, "with_value"}):
                            continue
                        label_path = f"$.requirements[{ri}].properties.{prefix}[{fi}].label_display_policy"
                        new_field = after_container["fields"][fi]
                        if new_field.get("label_display_policy") == "always":
                            expected_container["fields"][fi]["label_display_policy"] = "always"
                            permitted[label_path] = entry
        if (any(path not in permitted for path in paths)
                or _semantic(expected) != _semantic(current)):
            return None
        proposed = prepare(copy.deepcopy(current), chunk)[0]
        proposed["provenance"] = copy.deepcopy(chunk["provenance"])
        if validate(proposed, chunk):
            return None
        rule_id = {CODE: RULE_ID, TARGET_CODE: TARGET_RULE_ID,
                   APPLICABILITY_CODE: APPLICABILITY_RULE_ID}[rebuilt["code"]]
        return [{"rule_id": rule_id,
            "json_pointer": path,
            "source_binding": permitted[path]["source_binding"],
            "clause_id": permitted[path]["clause_id"],
            "obligation_id": permitted[path]["obligation_id"],
            "authorized_atom_fields": permitted[path].get("fields", ["condition"]),
            "feedback_sha256": sha256_json(record),
            "source_chunk_sha256": sha256_json(chunk),
            "baseline_response_sha256": previous_sha,
            "candidate_response_sha256": sha256_json(current),
            "semantic_review_required": True, "independent_review_required": True,
            "mechanical_equivalence_claimed": False, "submission_ready": False}
            for path in paths]
    except (ValueError, KeyError, TypeError, IndexError, AttributeError):
        return None


def project_scope_proposal(parent, proposal, records, chunk, *, prepare, validate, changed_paths):
    """Extract named scope fields without widening the one-shot authorization.

    Only surplus target/condition/applicability edits can be discarded here.
    Changes to quotations, identities, requirements, routes or any other parent
    field remain failures. Authentication and semantic authorization are still
    performed by the complete existing source-first feedback verifier.
    """
    audit = {"policy": "source_bound_scope_field_patch_v1", "status": "not_applicable",
             "parent_response_sha256": sha256_json(parent),
             "model_retry_response_sha256": sha256_json(proposal),
             "applied_paths": [], "discarded_unrequested_paths": [],
             "independent_review_required": True, "submission_ready": False}
    if (not all(isinstance(v, dict) for v in (parent, proposal, chunk))
            or not isinstance(records, list) or len(records) != 1
            or not isinstance(records[0], dict) or records[0].get("code") not in REASSESSMENT_CODES):
        return None, audit
    try:
        record = records[0]
        digest = sha256_json(parent)
        if digest == record.get("candidate_response_sha256"):
            candidate = copy.deepcopy(parent)
        elif digest == record.get("response_sha256"):
            candidate = prepare(copy.deepcopy(parent), chunk)[0]
            candidate["provenance"] = copy.deepcopy(chunk["provenance"])
        else:
            return None, audit
        rebuilt = source_atom_feedback(candidate, chunk, record["review_request"], record["rejected_review"])
        if rebuilt is None or any(record.get(k) != v for k, v in rebuilt.items()):
            return None, audit
        if "provenance" in proposal and proposal["provenance"] != chunk["provenance"]:
            return None, audit
        atoms_key = "condition_atoms" if rebuilt["code"] == CODE else "source_atoms"
        authority = {(e["clause_id"], e["obligation_id"]): e.get("fields", ["condition"])
                     for e in rebuilt[atoms_key]}
        if len(authority) != len(rebuilt[atoms_key]):
            return None, audit
        projected, non_scope = copy.deepcopy(parent), copy.deepcopy(proposal)
        seen = set()
        if len(parent["clause_reviews"]) != len(proposal["clause_reviews"]):
            return None, audit
        for i, before_review in enumerate(parent["clause_reviews"]):
            after_review = proposal["clause_reviews"][i]
            if (before_review["clause_id"] != after_review["clause_id"]
                    or len(before_review.get("obligations", [])) != len(after_review.get("obligations", []))):
                return None, audit
            for j, before in enumerate(before_review.get("obligations", [])):
                after = after_review["obligations"][j]
                key = (before_review["clause_id"], before["id"])
                if key in seen or before["id"] != after["id"]:
                    return None, audit
                seen.add(key)
                for field in ("target", "condition", "applicability"):
                    path = f"$.clause_reviews[{i}].obligations[{j}].{field}"
                    changed = (field in before) != (field in after) or before.get(field) != after.get(field)
                    if changed:
                        if field in authority.get(key, []):
                            dest = projected["clause_reviews"][i]["obligations"][j]
                            if field in after:
                                dest[field] = copy.deepcopy(after[field])
                            else:
                                dest.pop(field, None)
                            audit["applied_paths"].append(path)
                        else:
                            audit["discarded_unrequested_paths"].append(path)
                    dest = non_scope["clause_reviews"][i]["obligations"][j]
                    if field in before:
                        dest[field] = copy.deepcopy(before[field])
                    else:
                        dest.pop(field, None)
        if (not set(authority).issubset(seen) or _semantic(non_scope) != _semantic(parent)
                or not audit["applied_paths"] or not audit["discarded_unrequested_paths"]):
            return None, audit
        paths = changed_paths(parent, projected)
        proofs = condition_reassessment(parent, projected, records, paths, chunk,
                                       prepare=prepare, validate=validate)
        if proofs is None:
            return None, audit
        audit.update(status="projected", projected_response_sha256=sha256_json(projected),
                     feedback_sha256=sha256_json(record), source_chunk_sha256=sha256_json(chunk),
                     authorized_scope_proposals=proofs, mechanical_equivalence_claimed=False)
        return projected, audit
    except (NativeSemanticReviewError, ValueError, KeyError, TypeError, IndexError, AttributeError):
        return None, audit
