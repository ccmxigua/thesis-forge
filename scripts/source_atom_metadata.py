"""Bounded representation repairs, not semantic obligation corrections."""
from __future__ import annotations

import copy
import re

from responsibility_ledger import ROUTES, route_for_obligation
from semantic_contract import sha256_json
from source_literal_binding import compose_source_fragments, normalize_clause_literal, evidence_map_for_clauses


def bind_atom_quote(quote, clause_id, clause_map, evidence_context):
    """Bind an exact quotation to its current occurrence, including context.

    A quotation may contain the selected span plus source context (e.g. the
    sentence's original punctuation). That context does not expand the atom's
    execution scope: its clause_id/span and independent review remain fixed.
    Quotes from another occurrence or another evidence are never accepted.
    """
    if not isinstance(quote, str) or not quote.strip():
        raise ValueError("empty source quote")
    binding = compose_source_fragments([clause_id], clause_map, evidence_context)
    fragment = binding["source_fragments"][0]
    source = evidence_map_for_clauses(list(clause_map.values()), evidence_context)[fragment["evidence_id"]]["text"]
    start, end = fragment["start_offset"], fragment["end_offset"]
    matches, cursor = [], 0
    while (offset := source.find(quote, cursor)) >= 0:
        stop = offset + len(quote)
        if (start <= offset < stop <= end) or (offset <= start < end <= stop):
            matches.append((offset, stop))
        cursor = offset + 1
    if len(matches) != 1:
        raise ValueError("quote not uniquely bound to current source occurrence")
    return {"policy": "current_source_occurrence_quote_v1", "clause_binding": binding,
            "quote_start_offset": matches[0][0], "quote_end_offset": matches[0][1],
            "quote_sha256": sha256_json(quote), "context_is_not_execution_scope": True}


def project_atom_metadata(response, records, chunk):
    """Repair only validator-named fields of an exact current source atom.

    Quote whitespace/edge punctuation can differ because clause segmentation
    produces a semantic fragment. Canonicalize to its *original* exact span,
    never to normalized prose. The route is derived from the unchanged review
    classification/status, not inferred from an actor or an action string.
    All semantic fields, source identities and array order are preserved.
    """
    if not isinstance(response, dict) or not isinstance(chunk, dict):
        return None, []
    clauses = chunk.get("clauses")
    reviews = response.get("clause_reviews")
    if not isinstance(clauses, list) or not isinstance(reviews, list):
        return None, []
    clause_map = {c.get("id"): c for c in clauses if isinstance(c, dict)}
    if len(clause_map) != len(clauses):
        return None, []
    candidate, audit = copy.deepcopy(response), []
    seen = set()
    for record in records:
        if not isinstance(record, dict) or record.get("response_sha256") != sha256_json(response):
            return None, []
        path = str(record.get("json_pointer") or "")
        match = re.fullmatch(r"\$\.clause_reviews\[(\d+)\]\.obligations\[(\d+)\]\.(source_quote|route)", path)
        if not match or record.get("code") != "contract_validation_error":
            continue
        i, j, field = int(match[1]), int(match[2]), match[3]
        suffix = "must_equal_current_source_subspan" if field == "source_quote" else "responsibility_route_conflict"
        if record.get("raw_error") != f"{path}: {suffix}" or path in seen:
            return None, []
        seen.add(path)
        if i >= len(reviews) or not isinstance(reviews[i], dict):
            return None, []
        review = reviews[i]
        cid = review.get("clause_id")
        atoms = review.get("obligations")
        if (record.get("clause_id") != cid or not isinstance(atoms, list)
                or j >= len(atoms) or not isinstance(atoms[j], dict)
                or sum(isinstance(r, dict) and r.get("clause_id") == cid for r in reviews) != 1):
            return None, []
        atom = atoms[j]
        try:
            binding = compose_source_fragments([cid], clause_map, chunk.get("evidence_context"))
        except (ValueError, KeyError, TypeError):
            continue
        original = atom.get(field)
        if field == "source_quote":
            exact = binding["text"]
            try:
                bind_atom_quote(original, cid, clause_map, chunk.get("evidence_context"))
                continue  # Correct original evidence must not be rewritten.
            except (ValueError, KeyError, TypeError):
                pass
            if (not isinstance(original, str) or not normalize_clause_literal(exact)
                    or normalize_clause_literal(original) != normalize_clause_literal(exact)):
                continue
            replacement = exact
        else:
            replacement = route_for_obligation(review.get("classification"), atom.get("status"))
            if original not in ROUTES or replacement == "unknown":
                continue
        if original == replacement:
            continue
        candidate["clause_reviews"][i]["obligations"][j][field] = replacement
        audit.append({
            "rule_id": "current_source_atom_metadata_v1", "json_pointer": path,
            "field": field, "clause_id": cid, "old_value": original, "new_value": replacement,
            "source_binding": binding, "source_chunk_sha256": sha256_json(chunk),
            "validator_record_sha256": sha256_json(record),
            "baseline_response_sha256": sha256_json(response),
            "semantic_obligation_fields_unchanged": True,
            "independent_review_required": True, "submission_ready": False,
        })
    if not audit:
        return None, []
    for proof in audit:
        proof["result_response_sha256"] = sha256_json(candidate)
    return candidate, audit
