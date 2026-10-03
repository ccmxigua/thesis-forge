"""Record competing no-duty/pending-duty interpretations without choosing one.

This code-owned assessment is not a model verdict, obligation, or permission to
execute. Only consumers which reconstruct it from the current request and raw
review may carry it into an explicitly non-release review draft.
"""
from __future__ import annotations

import copy

from semantic_contract import sha256_json
from source_atom_metadata import bind_atom_quote
from source_obligation_compiler import (
    compile_known_source_obligation_ids, compile_source_content_verification_codes,
    compile_unresolved_manual_review_codes, has_mixed_external_document_action_signal,
)
from pending_source_work import compile_pending_source_work

POLICY = "source_bound_pending_inventory_existence_dispute_v1"


def inventory_existence_dispute(check, result):
    """Return a disputed assessment, never convert an empty inventory to coverage.

The primary must contain complete, exclusively pending human atoms and no
executable links. The independent reviewer must explicitly return a grounded
consistent/empty assessment. Known source facts and malformed/source-unbound
records remain errors, even for drafts. No label, clause ID or school is special.
"""
    if not isinstance(check, dict) or not isinstance(result, dict):
        return None
    context, text = check.get("review_context"), check.get("document_text")
    if (not isinstance(context, dict) or not isinstance(text, str) or not text.strip()
            or context.get("classification") != "external_compliance"
            or context.get("primary_normative_basis") != "external_duty"
            or context.get("requires_requirement") is not False
            or context.get("linked_requirements") != []
            or context.get("source_clause_support", [])
            or result.get("check_id") != check.get("check_id")
            or result.get("verdict") != "consistent"
            or result.get("identified_obligations") != []
            or result.get("machine_obligation_ids") != []
            or not isinstance(result.get("rationale"), str) or not result["rationale"].strip()):
        return None
    quotes = result.get("evidence_quotes")
    if (not isinstance(quotes, list) or not quotes
            or any(not isinstance(q, str) or not q.strip() or q not in text for q in quotes)):
        return None
    if (compile_known_source_obligation_ids(text) or compile_pending_source_work(text)
            or compile_unresolved_manual_review_codes(text)
            or compile_source_content_verification_codes(text)
            or has_mixed_external_document_action_signal(text)
            or any(context.get(k) for k in (
                "machine_obligation_ids", "manual_review_codes", "pending_source_work",
                "source_content_verification_codes"))):
        return None
    atoms, bindings = context.get("primary_obligations"), context.get("primary_obligation_quote_bindings")
    evidence = context.get("cited_evidence")
    if (not isinstance(atoms, list) or not atoms or not isinstance(bindings, list)
            or len(bindings) != len(atoms) or not isinstance(evidence, dict) or not evidence):
        return None
    ids = [a.get("id") for a in atoms if isinstance(a, dict)]
    if (len(ids) != len(atoms) or any(not isinstance(i, str) or not i for i in ids)
            or len(set(ids)) != len(ids)):
        return None
    for atom, binding in zip(atoms, bindings):
        if (atom.get("status") != "unverifiable" or atom.get("route") != "human"
                or atom.get("force") not in {"required", "recommended", "optional", "prohibited"}
                or atom.get("applicability") not in {"applicable", "conditional", "unknown"}
                or any(not isinstance(atom.get(k), str) or not atom[k].strip() or atom[k] == "unknown"
                       for k in ("actor", "action", "target", "source_quote"))
                or atom["source_quote"] not in text
                or not isinstance(binding, dict)):
            return None
        try:
            proof = binding["source_binding"]
            fragments = proof["clause_binding"]["source_fragments"]
            if len(fragments) != 1:
                return None
            fragment = fragments[0]
            cid, eid = fragment["clause_id"], fragment["evidence_id"]
            if cid != check["check_id"] or eid not in evidence or fragment["text"] != text:
                return None
            clause = {"id": cid, "text": text, "evidence_ids": [eid],
                      "source_kind": fragment["source_kind"], "location": fragment["location"],
                      "source_span": {k: fragment[k] for k in (
                          "evidence_id", "start_offset", "end_offset", "source_sha256", "text")}}
            original = binding["original_source_quote"]
            rebuilt = bind_atom_quote(original, cid, {cid: clause}, evidence)
            review_quote = original if original in text else text
            expected = {"primary_obligation_id": atom["id"], "original_source_quote": original,
                        "review_source_quote": review_quote, "source_binding": rebuilt,
                        "original_quote_sha256": sha256_json(original),
                        "review_quote_sha256": sha256_json(review_quote),
                        "semantic_dimensions_unchanged": True}
            if binding != expected or atom["source_quote"] != review_quote:
                return None
        except (KeyError, TypeError, ValueError, IndexError):
            return None
    return {
        "policy": POLICY, "check_id": check["check_id"],
        "source_text": text, "source_text_sha256": sha256_json(text),
        "check_sha256": sha256_json(check),
        "primary_review_context": copy.deepcopy(context),
        "primary_review_context_sha256": sha256_json(context),
        "independent_result": copy.deepcopy(result),
        "independent_result_sha256": sha256_json(result),
        "status": "human_review_pending", "coverage_complete": False,
        "execution_authorized": False, "submission_ready": False,
    }


def build_inventory_existence_disputes(checks, results):
    by_id = {c["check_id"]: c for c in checks}
    return [assessment for result in results
            if (assessment := inventory_existence_dispute(by_id.get(result.get("check_id")), result)) is not None]


def validate_inventory_existence_disputes(record, checks, results):
    expected = build_inventory_existence_disputes(checks, results)
    if record.get("source_inventory_disputes", []) != expected:
        raise ValueError("source inventory disputes do not match the current source and both reviews")
    return expected
