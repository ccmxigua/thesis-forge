"""Source-bound primary correction of an action invented from a policy condition.

Only the existing complete two-effect publication grammar qualifies. Unknown,
mixed, quoted or extended prose keeps the ordinary semantic review path. Code
rejects the invalid inventory; it never removes atoms or chooses a new response.
"""
from __future__ import annotations

import copy

from semantic_contract import sha256_json
from source_literal_binding import compose_source_fragments
from source_atom_metadata import bind_atom_quote
from source_obligation_compiler import (
    compile_known_source_obligations, PUBLICATION_DEFAULT_OBLIGATION_ID,
    PUBLIC_ADMIN_BLANK_OBLIGATION_ID,
)

CODE = "publication_policy_external_inventory"
RULE_ID = "v3_source_bound_publication_inventory_reassessment"
FACT_IDS = {PUBLICATION_DEFAULT_OBLIGATION_ID, PUBLIC_ADMIN_BLANK_OBLIGATION_ID}


def _bound_entries(response, chunk):
    if (not isinstance(response, dict) or response.get("contract_version") != "3.0"
            or not isinstance(chunk, dict)):
        return []
    try:
        clauses = chunk["clauses"]
        by_id = {c["id"]: c for c in clauses}
        reviews = response["clause_reviews"]
        if (len(by_id) != len(clauses)
                or len({r["clause_id"] for r in reviews}) != len(reviews)):
            return []
        entries = []
        for i, review in enumerate(reviews):
            cid = review["clause_id"]
            binding = compose_source_fragments([cid], by_id, chunk["evidence_context"])
            facts = compile_known_source_obligations(binding["text"])
            if len(facts) != 2 or {f["id"] for f in facts} != FACT_IDS:
                continue
            atoms = review.get("obligations", [])
            rejected = [a for a in atoms if a.get("status") == "unverifiable"]
            if rejected:
                entries.append({"review_index": i, "clause_id": cid,
                    "source_binding": binding, "facts": facts,
                    "rejected_atoms": copy.deepcopy(rejected)})
        return entries
    except (ValueError, KeyError, TypeError, IndexError, AttributeError):
        return []


def policy_inventory_errors(response, chunk):
    return [f"$.clause_reviews[{e['review_index']}]: {CODE}"
            for e in _bound_entries(response, chunk)]


def policy_inventory_retry_ledger(previous, current, records, paths, chunk,
                                  *, validate, make_records):
    """Authenticate a primary proposal, never a deletion or a semantic pass."""
    if (not paths or not isinstance(records, list) or not records
            or any(not isinstance(r, dict) or r.get("code") != CODE for r in records)
            or not isinstance(current, dict) or not isinstance(previous, dict)):
        return None
    try:
        errors = validate(previous, chunk)
        if records != make_records(errors, response=previous, chunk=chunk):
            return None  # Complete feedback and the exact stage/run must match.
        entries = _bound_entries(previous, chunk)
        if len(entries) != len(records) or not entries:
            return None
        expected = copy.deepcopy(previous)
        by_prefix = {}
        for entry in entries:
            i, cid = entry["review_index"], entry["clause_id"]
            old = previous["clause_reviews"][i]
            new = current["clause_reviews"][i]
            atoms = old["obligations"]
            ids = [a["id"] for a in atoms]
            if (old.get("classification") != "executable_with_external_check"
                    or len(set(ids)) != len(ids) or any(not x for x in ids)
                    or new.get("classification") not in {"covered", "executable", "verify_existing"}
                    or not isinstance(new.get("reason"), str) or not new["reason"].strip()):
                return None
            retained = [a for a in atoms if a.get("status") != "unverifiable"]
            if (len(retained) != 2 or any(a.get("status") != "covered" for a in retained)
                    or any(a.get("route") != "human" for a in entry["rejected_atoms"])):
                return None
            clauses = {c["id"]: c for c in chunk["clauses"]}
            for atom in atoms:
                bind_atom_quote(atom["source_quote"], cid, clauses, chunk["evidence_context"])
            assignments = [[j for j, a in enumerate(retained)
                            if f["evidence_text"] in a["source_quote"]]
                           for f in entry["facts"]]
            if any(len(a) != 1 for a in assignments) or len({a[0] for a in assignments}) != 2:
                return None  # Preserve both distinct document-policy effects.
            prefix = f"$.clause_reviews[{i}]"
            expected["clause_reviews"][i]["obligations"] = copy.deepcopy(retained)
            expected["clause_reviews"][i]["classification"] = new["classification"]
            expected["clause_reviews"][i]["reason"] = new["reason"]
            by_prefix[prefix] = entry
        semantic = lambda value: {k: v for k, v in value.items() if k != "provenance"}
        if semantic(expected) != semantic(current) or validate(current, chunk):
            return None
        proofs = []
        for path in paths:
            matching = [e for p, e in by_prefix.items() if path == p or path.startswith(p + ".")]
            if len(matching) != 1:
                return None
            e = matching[0]
            proofs.append({"json_pointer": path, "rule_id": RULE_ID,
                "source_binding_complete": True, "source_binding": e["source_binding"],
                "clause_id": e["clause_id"], "source_chunk_sha256": sha256_json(chunk),
                "baseline_response_sha256": sha256_json(previous),
                "candidate_response_sha256": sha256_json(current),
                "error_bundle_sha256": sha256_json(records),
                "rejected_model_atoms": e["rejected_atoms"],
                "retained_source_facts": e["facts"],
                "semantic_review_required": True, "independent_review_required": True,
                "mechanical_equivalence_claimed": False, "submission_ready": False})
        return proofs
    except (ValueError, KeyError, TypeError, IndexError, AttributeError):
        return None
