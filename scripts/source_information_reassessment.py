"""Bounded primary proposals for contradictory printed-field classifications.

A validator contradiction is a proposal opportunity, never a semantic verdict.
No source duty, field, link or value is deleted. Fresh independent source review
must approve the proposed meaning after ordinary candidate validation.
"""
from __future__ import annotations

import copy

from host_review_contract import analyze_requirement_relations, contract_error_records
from source_literal_binding import compose_source_fragments, normalize_clause_literal
from source_atom_metadata import bind_atom_quote
from semantic_contract import sha256_json

CODE = "primary_printed_field_information_reassessment_required"
RULE_ID = "v3_source_bound_printed_field_information_reassessment"


def dispatch_record(records):
    """Recognize a complete proposal envelope, not permission to accept it."""
    if not isinstance(records, list) or any(not isinstance(r, dict) for r in records):
        return None
    proposals = [r for r in records if r.get("code") == CODE]
    ordinary = [r for r in records if r.get("code") != CODE]
    if (len(proposals) != 1 or not ordinary or not proposals[0].get("targets")
            or proposals[0].get("error_bundle_sha256") != sha256_json(ordinary)
            or any(r.get("response_sha256") != proposals[0].get("response_sha256") for r in ordinary)):
        return None
    return proposals[0]


def feedback(candidate, chunk, records, *, validate, source_projection_validation_sha256):
    """Name only empty informational reviews contradicting a printed field.

    Other non-execution categories, preexisting duties, unrelated errors and
    unsupported field ownership are deliberately not generic retry authority.
    """
    if (not isinstance(candidate, dict) or not isinstance(chunk, dict)
            or candidate.get("contract_version") != "3.0"
            or source_projection_validation_sha256 != sha256_json(chunk)
            or candidate.get("reported_conflicts") or candidate.get("conflicts")):
        return None
    try:
        actual = contract_error_records(validate(candidate, chunk), response=candidate, chunk=chunk)
        if (not actual or sorted(map(sha256_json, actual)) != sorted(map(sha256_json, records))
                or any(r.get("code") not in {"mixed_execution_classification_relation",
                                             "missing_derived_requirement", "cover_binding_violation"} for r in actual)):
            return None
        for r in actual:
            if r.get("code") == "missing_derived_requirement" and not r.get("blocked_by_parent_relation"):
                return None
            if r.get("code") == "cover_binding_violation" and not str(r.get("raw_error", "")).endswith(
                    "must be declared under cover.non_public_administration, not ordinary cover.fields"):
                return None
        clauses = {c["id"]: c for c in chunk["clauses"]}
        reviews = {r["clause_id"]: (i, r) for i, r in enumerate(candidate["clause_reviews"])}
        if len(clauses) != len(chunk["clauses"]) or len(reviews) != len(candidate["clause_reviews"]) or set(clauses) != set(reviews):
            return None
        targets = {}
        for fact in analyze_requirement_relations(candidate, chunk["clauses"]):
            if fact["category"] != "mixed_execution_classification":
                continue
            req = candidate["requirements"][fact["requirement_index"]]
            if req.get("role") != "cover" or req.get("existing_requirement_id") or req.get("field_key"):
                return None
            for cid, categories in fact["clause_classifications"].items():
                if categories[0] in {"covered", "executable", "verify_existing", "executable_with_external_check"}:
                    continue
                index, review = reviews[cid]
                if categories != ["informational"] or review.get("obligations", []) != []:
                    return None
                binding = compose_source_fragments([cid], clauses, chunk["evidence_context"],
                    requirement_clause_ids=req["clause_ids"], requirement_evidence_ids=req["evidence_ids"],
                    literal_role="cover_field_label")
                fields = req["properties"].get("fields", [])
                matches = [f for f in fields if isinstance(f, dict) and isinstance(f.get("label"), str)
                    and normalize_clause_literal(f["label"]) == normalize_clause_literal(binding["text"])]
                if (len(matches) != 1 or matches[0].get("value_from") !=
                        "thesis_profile.cover_metadata." + str(matches[0].get("id"))):
                    return None
                targets[cid] = {"clause_id": cid, "review_index": index,
                    "source_binding": binding, "printed_field": copy.deepcopy(matches[0]),
                    "old_review_sha256": sha256_json(review)}
        if not targets:
            return None
        return {"code": CODE, "json_pointer": "$.clause_reviews", "response_sha256": sha256_json(candidate),
            "source_chunk_sha256": sha256_json(chunk), "error_bundle_sha256": sha256_json(actual),
            "targets": list(targets.values()), "semantic_review_required": True,
            "independent_review_required": True, "mechanical_equivalence_claimed": False,
            "submission_ready": False}
    except (ValueError, KeyError, TypeError, IndexError, AttributeError):
        return None


def reassessment(previous, current, records, paths, chunk, *, prepare, validate):
    """Accept only the bounded proposal shape, never its semantic correctness."""
    envelope = dispatch_record(records)
    if envelope is None or not paths or not isinstance(chunk, dict):
        return None
    ordinary = [r for r in records if r.get("code") != CODE]
    try:
        if any(isinstance(value, dict) and value.get("provenance") is not None
               and value["provenance"] != chunk.get("provenance") for value in (previous, current)):
            return None
        # Feedback may bind to the immutable raw or its reproduced repair base.
        try:
            parent = prepare(copy.deepcopy(previous), chunk,
                source_projection_validation_sha256=sha256_json(chunk))[0]
        except ValueError as exc:
            parent = getattr(exc, "repair_base_candidate", None)
        if not isinstance(parent, dict):
            return None
        rebuilt = feedback(parent, chunk, ordinary, validate=validate,
                           source_projection_validation_sha256=sha256_json(chunk))
        if rebuilt != envelope:
            return None
        if [r["clause_id"] for r in current["clause_reviews"]] != [r["clause_id"] for r in parent["clause_reviews"]]:
            return None
        proposal = copy.deepcopy(parent)
        for target in rebuilt["targets"]:
            i, cid = target["review_index"], target["clause_id"]
            old, new = parent["clause_reviews"][i], current["clause_reviews"][i]
            allowed = {"classification", "normative_basis", "obligations", "reason"}
            if ({k: v for k, v in old.items() if k not in allowed} != {k: v for k, v in new.items() if k not in allowed}
                    or new.get("classification") != "executable"
                    or new.get("normative_basis") != "template_structure"
                    or not isinstance(new.get("obligations"), list) or not new["obligations"]):
                return None
            for atom in new["obligations"]:
                if atom.get("status") != "covered" or atom.get("route") != "automatic":
                    return None
                bind_atom_quote(atom["source_quote"], cid, {c["id"]: c for c in chunk["clauses"]}, chunk["evidence_context"])
            proposal["clause_reviews"][i] = copy.deepcopy(new)
        expected = prepare(proposal, chunk, source_projection_validation_sha256=sha256_json(chunk))[0]
        actual = prepare(copy.deepcopy(current), chunk, source_projection_validation_sha256=sha256_json(chunk))[0]
        for value in (expected, actual):
            value.pop("provenance", None)
        if expected != actual or validate(actual, chunk):
            return None
        return [{"rule_id": RULE_ID, "json_pointer": path, "targets": rebuilt["targets"],
                 "feedback_sha256": sha256_json(envelope), "source_chunk_sha256": sha256_json(chunk),
                 "semantic_review_required": True, "independent_review_required": True,
                 "mechanical_equivalence_claimed": False, "submission_ready": False} for path in paths]
    except (ValueError, KeyError, TypeError, IndexError, AttributeError):
        return None
