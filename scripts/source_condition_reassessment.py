"""Bounded primary condition proposals after a source-first review disagrees.

The rejected review authorizes a new proposal, not an interpretation. No
condition is copied from the reviewer, and no old candidate is accepted.
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


def condition_feedback(candidate, chunk, request, rejected_review):
    """Reconstruct the complete rejected source review; only condition differs."""
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
                    if disagreement["fields"] != ["condition"]:
                        return None
                    oid = disagreement["primary_obligation_id"]
                    primaries = check["review_context"]["primary_obligations"]
                    primary = next(p for p in primaries if p["id"] == oid)
                    binding = compose_source_fragments([cid], by_clause, chunk["evidence_context"])
                    quote = bind_atom_quote(primary["source_quote"], cid, by_clause, chunk["evidence_context"])
                    changes.append({"clause_id": cid, "obligation_id": oid,
                        "primary_sha256": sha256_json(primary),
                        "source_binding": binding, "quote_binding": quote})
            except NativeSemanticReviewError:
                return None  # Other missing inventories/fields are not this repair.
        if not changes:
            return None
        return {"code": CODE, "candidate_response_sha256": sha256_json(candidate),
            "source_chunk_sha256": sha256_json(chunk), "run_id": provenance["run_id"],
            "provenance": copy.deepcopy(provenance), "condition_atoms": changes,
            "review_request": copy.deepcopy(request), "rejected_review": copy.deepcopy(rejected_review),
            "review_request_sha256": sha256_json(request),
            "rejected_review_sha256": sha256_json(rejected_review),
            "semantic_review_required": True, "submission_ready": False}
    except (ValueError, KeyError, TypeError, IndexError, StopIteration):
        return None


def _semantic(value):
    value = copy.deepcopy(value)
    value.pop("provenance", None)
    return value


def condition_reassessment(previous, current, records, paths, chunk, *, prepare, validate):
    """Authorize exact condition fields and separately bound label-only display.

Raw and projected candidates must each satisfy the same narrow transition.
The code never chooses a replacement condition or a metadata value.
"""
    if (not isinstance(previous, dict) or not isinstance(current, dict)
            or not isinstance(chunk, dict) or not isinstance(records, list)
            or len(records) != 1 or not isinstance(records[0], dict)
            or records[0].get("code") != CODE or not paths
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
        rebuilt = condition_feedback(candidate, chunk, record["review_request"], record["rejected_review"])
        if rebuilt is None or any(record.get(k) != v for k, v in rebuilt.items()):
            return None
        expected = copy.deepcopy(previous)
        permitted = {}
        for entry in rebuilt["condition_atoms"]:
            cid, oid = entry["clause_id"], entry["obligation_id"]
            matching = [(i, j) for i, review in enumerate(previous["clause_reviews"])
                        if review["clause_id"] == cid
                        for j, atom in enumerate(review["obligations"]) if atom["id"] == oid]
            if len(matching) != 1:
                return None
            i, j = matching[0]
            path = f"$.clause_reviews[{i}].obligations[{j}].condition"
            after = current["clause_reviews"][i]["obligations"][j]
            if after.get("condition") is not None and not isinstance(after["condition"], str):
                return None
            if "condition" in after:
                expected["clause_reviews"][i]["obligations"][j]["condition"] = after["condition"]
            else:
                expected["clause_reviews"][i]["obligations"][j].pop("condition", None)
            permitted[path] = entry
            # A label/value separation is possible only for the exact field
            # already linked to this same current source label. Values, value
            # policy, identities, applicability and all other payload stay.
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
        return [{"rule_id": RULE_ID, "json_pointer": path,
            "source_binding": permitted[path]["source_binding"],
            "clause_id": permitted[path]["clause_id"],
            "obligation_id": permitted[path]["obligation_id"],
            "feedback_sha256": sha256_json(record),
            "source_chunk_sha256": sha256_json(chunk),
            "baseline_response_sha256": previous_sha,
            "candidate_response_sha256": sha256_json(current),
            "semantic_review_required": True, "independent_review_required": True,
            "mechanical_equivalence_claimed": False, "submission_ready": False}
            for path in paths]
    except (ValueError, KeyError, TypeError, IndexError, AttributeError):
        return None
