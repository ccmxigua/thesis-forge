"""Classification-only primary proposals; no code-authored semantic verdict.

The hard mixed-execution contract is unchanged. A nonempty all-covered
inventory permits a bounded proposal, not acceptance. Reproduce the rejected
candidate and feedback at the same stage; freeze its duties and requirement
graph, then require ordinary validation and fresh independent source review.
"""
from __future__ import annotations

import copy

from host_review_contract import contract_error_records, MIXED_INVENTORY_CODE
from semantic_contract import sha256_json
from source_atom_metadata import bind_atom_quote
from source_literal_binding import compose_source_fragments

ERROR_CODE = MIXED_INVENTORY_CODE
CODE = "primary_all_covered_classification_reassessment_required"
RULE_ID = "v3_source_bound_all_covered_classification_reassessment"
CLASSES = {"covered", "executable"}


def dispatch_record(records):
    if not isinstance(records, list) or any(not isinstance(r, dict) for r in records):
        return None
    proposals = [r for r in records if r.get("code") == CODE]
    ordinary = [r for r in records if r.get("code") != CODE]
    if (len(proposals) != 1 or not ordinary or not proposals[0].get("targets")
            or any(r.get("code") != ERROR_CODE for r in ordinary)
            or proposals[0].get("error_bundle_sha256") != sha256_json(ordinary)
            or any(r.get("response_sha256") != proposals[0].get("response_sha256") for r in ordinary)):
        return None
    return proposals[0]


def feedback(candidate, chunk, records, *, validate, source_projection_validation_sha256):
    if (not isinstance(candidate, dict) or candidate.get("contract_version") != "3.0"
            or not isinstance(chunk, dict) or source_projection_validation_sha256 != sha256_json(chunk)
            or candidate.get("reported_conflicts") or candidate.get("conflicts")):
        return None
    try:
        actual = contract_error_records(validate(candidate, chunk), response=candidate, chunk=chunk)
        if not actual or actual != records or any(r.get("code") != ERROR_CODE for r in actual):
            return None  # No partial error bundle or unrelated repair authority.
        clauses = {c["id"]: c for c in chunk["clauses"]}
        reviews = candidate["clause_reviews"]
        if (len(clauses) != len(chunk["clauses"])
                or len({r["clause_id"] for r in reviews}) != len(reviews)
                or set(clauses) != {r["clause_id"] for r in reviews}):
            return None
        targets = []
        for record in actual:
            cid = record["clause_id"]
            index = next(i for i, r in enumerate(reviews) if r["clause_id"] == cid)
            review = reviews[index]
            atoms = review.get("obligations")
            if (review.get("classification") != "executable_with_external_check"
                    or not isinstance(atoms, list) or not atoms
                    or len({a["id"] for a in atoms}) != len(atoms)
                    or any(a.get("status") != "covered" or a.get("route") != "automatic"
                           or a.get("applicability") not in {"applicable", "conditional"} for a in atoms)
                    or not record.get("matching_requirement_indexes")):
                return None
            binding = compose_source_fragments([cid], clauses, chunk["evidence_context"])
            fragment = binding["source_fragments"][0]
            quotes = [bind_atom_quote(a["source_quote"], cid, clauses, chunk["evidence_context"]) for a in atoms]
            if any(q["quote_start_offset"] < fragment["start_offset"]
                   or q["quote_end_offset"] > fragment["end_offset"] for q in quotes):
                return None  # Shared evidence/context never grants a sibling's duty.
            targets.append({"clause_id": cid, "review_index": index, "source_binding": binding,
                "quote_bindings": quotes, "review_sha256": sha256_json(review),
                "inventory_sha256": sha256_json(atoms),
                "matching_requirement_indexes": record["matching_requirement_indexes"]})
        if len({t["clause_id"] for t in targets}) != len(targets):
            return None
        return {"code": CODE, "json_pointer": "$.clause_reviews", "targets": targets,
            "response_sha256": sha256_json(candidate), "source_chunk_sha256": sha256_json(chunk),
            "error_bundle_sha256": sha256_json(actual), "allowed_classifications": sorted(CLASSES),
            "semantic_review_required": True, "independent_review_required": True,
            "mechanical_equivalence_claimed": False, "submission_ready": False}
    except (ValueError, KeyError, TypeError, IndexError, AttributeError, StopIteration):
        return None


def reassessment(previous, current, records, paths, chunk, *, prepare, validate, changed_paths):
    envelope = dispatch_record(records)
    if envelope is None or not paths or not isinstance(chunk, dict):
        return None
    try:
        for value in (previous, current):
            if (not isinstance(value, dict) or value.get("contract_version") != "3.0"
                    or (value.get("provenance") is not None and value["provenance"] != chunk.get("provenance"))):
                return None
        try:
            parent = prepare(copy.deepcopy(previous), chunk,
                source_projection_validation_sha256=sha256_json(chunk))[0]
        except ValueError as exc:
            parent = getattr(exc, "repair_base_candidate", None)
        if not isinstance(parent, dict):
            return None
        ordinary = [r for r in records if r.get("code") != CODE]
        rebuilt = feedback(parent, chunk, ordinary, validate=validate,
                           source_projection_validation_sha256=sha256_json(chunk))
        if rebuilt != envelope:
            return None  # Do not re-seal old stage/run/source feedback.
        proposal, raw_proposal = copy.deepcopy(parent), copy.deepcopy(previous)
        permitted = []
        for target in rebuilt["targets"]:
            i, cid = target["review_index"], target["clause_id"]
            if (current["clause_reviews"][i]["clause_id"] != cid
                    or previous["clause_reviews"][i]["clause_id"] != cid
                    or current["clause_reviews"][i].get("classification") not in CLASSES):
                return None
            classification = current["clause_reviews"][i]["classification"]
            proposal["clause_reviews"][i]["classification"] = classification
            raw_proposal["clause_reviews"][i]["classification"] = classification
            permitted.append(f"$.clause_reviews[{i}].classification")
        semantic = lambda value: {k: v for k, v in value.items() if k != "provenance"}
        if semantic(current) not in (semantic(proposal), semantic(raw_proposal)):
            return None  # Freeze EVERY field, including reasons, metadata and order.
        expected = prepare(proposal, chunk, source_projection_validation_sha256=sha256_json(chunk))[0]
        actual = prepare(copy.deepcopy(current), chunk, source_projection_validation_sha256=sha256_json(chunk))[0]
        if semantic(expected) != semantic(actual) or validate(actual, chunk):
            return None
        if any(p not in permitted for p in changed_paths(parent, expected)):
            return None  # Classification-sensitive projections cannot smuggle edits.
        return [{"rule_id": RULE_ID, "json_pointer": path,
            "source_binding_complete": True, "targets": rebuilt["targets"],
            "source_chunk_sha256": sha256_json(chunk), "feedback_sha256": sha256_json(envelope),
            "baseline_response_sha256": sha256_json(previous),
            "replayed_parent_candidate_sha256": sha256_json(parent),
            "candidate_response_sha256": sha256_json(current),
            "replayed_candidate_sha256": sha256_json(actual),
            "proposal_paths": permitted, "code_projection_paths": changed_paths(previous, parent),
            "semantic_review_required": True, "independent_review_required": True,
            "mechanical_equivalence_claimed": False, "submission_ready": False} for path in paths]
    except (ValueError, KeyError, TypeError, IndexError, AttributeError):
        return None
