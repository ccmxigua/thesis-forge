"""Bounded primary selection of full source context, never lexical equivalence.

A repeated whitespace-normalized quote has no unique occurrence. Code cannot
pick one. A primary retry may instead select its clause's exact full span; that
selection still needs a fresh independent semantic review before acceptance.
"""
from __future__ import annotations

import copy
import re

from host_review_contract import contract_error_records
from semantic_contract import sha256_json
from source_atom_metadata import bind_atom_quote
from source_literal_binding import compose_source_fragments

RULE_ID = "v3_ambiguous_quote_context_reassessment"


def quote_context_reassessment(previous, current, records, paths, chunk, *, validate):
    """Return proofs only for the complete, validator-bound quote-only edit."""
    if (not isinstance(previous, dict) or not isinstance(current, dict)
            or not isinstance(chunk, dict) or previous.get("contract_version") != "3.0"
            or current.get("contract_version") != "3.0" or not paths
            or previous.get("conflicts") or previous.get("reported_conflicts")):
        return None
    clauses = chunk.get("clauses")
    if not isinstance(clauses, list) or any(not isinstance(c, dict) for c in clauses):
        return None
    by_id = {c.get("id"): c for c in clauses}
    if len(by_id) != len(clauses):
        return None
    actual = contract_error_records(validate(previous, chunk), response=previous, chunk=chunk)
    if (not actual or not isinstance(records, list)
            or any(not isinstance(r, dict) for r in records)
            or sorted(sha256_json(r) for r in actual) != sorted(sha256_json(r) for r in records)
            or len(paths) != len(set(paths)) or len(actual) != len(paths)
            or {r.get("json_pointer") for r in actual} != set(paths)):
        return None
    expected, proofs = copy.deepcopy(previous), []
    for record in actual:
        path = record.get("json_pointer", "")
        match = re.fullmatch(r"\$\.clause_reviews\[(\d+)\]\.obligations\[(\d+)\]\.source_quote", path)
        if (match is None or record.get("code") != "contract_validation_error"
                or record.get("raw_error") != f"{path}: must_equal_current_source_subspan"
                or record.get("response_sha256") != sha256_json(previous)):
            return None
        try:
            i, j = map(int, match.groups())
            old_review, new_review = previous["clause_reviews"][i], current["clause_reviews"][i]
            cid = old_review["clause_id"]
            if (cid != record.get("clause_id") or cid != new_review["clause_id"]
                    or sum(r.get("clause_id") == cid for r in previous["clause_reviews"]) != 1
                    or sum(r.get("clause_id") == cid for r in current["clause_reviews"]) != 1):
                return None
            old, new = old_review["obligations"][j]["source_quote"], new_review["obligations"][j]["source_quote"]
            binding = compose_source_fragments([cid], by_id, chunk.get("evidence_context"))
            exact = binding["text"]
            if not isinstance(old, str) or not old.strip() or new != exact or old == new:
                return None
            try:
                bind_atom_quote(old, cid, by_id, chunk.get("evidence_context"))
            except ValueError:
                pass
            else:
                return None  # An already valid quote grants no reassessment.
            pattern = "".join(r"\s+" if token.isspace() else re.escape(token)
                              for token in re.findall(r"\s+|\S+", old.strip()))
            matches = list(re.finditer(pattern, exact))
            if len(matches) < 2:
                return None  # Unique metadata recovery has its own code path.
            quote_binding = bind_atom_quote(new, cid, by_id, chunk.get("evidence_context"))
            expected["clause_reviews"][i]["obligations"][j]["source_quote"] = new
            proofs.append({"rule_id": RULE_ID, "json_pointer": path, "clause_id": cid,
                "old_quote": old, "new_quote": new, "ambiguous_match_count": len(matches),
                "source_binding": binding, "selected_quote_binding": quote_binding,
                "source_chunk_sha256": sha256_json(chunk), "validator_record_sha256": sha256_json(record),
                "baseline_response_sha256": sha256_json(previous), "candidate_response_sha256": sha256_json(current),
                "primary_semantic_reassessment": True, "mechanical_equivalence_claimed": False,
                "context_is_not_execution_scope": True, "independent_review_required": True,
                "submission_ready": False})
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            return None
    # Invocation provenance is bridge-owned and checked separately. Everything
    # else, including ordering, conditions, status, routes and relations, stays.
    expected.pop("provenance", None)
    compared = copy.deepcopy(current)
    compared.pop("provenance", None)
    if expected != compared or validate(current, chunk):
        return None
    return proofs
