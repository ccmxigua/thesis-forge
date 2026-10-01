"""Move no duties: remove only a redundant, mis-typed blank signature block.

The complete declaration already prints the exact adjacent source line through
its bound resource. Original external reviews, quotes and pending atoms remain.
"""
from __future__ import annotations

import copy
import re
from fixed_declaration_source import derive_fixed_declaration_candidates, bound_signature_lines, matches_declaration_render_selection
from native_semantic_review import _exact_clause_source_text, NativeSemanticReviewError
from source_atom_metadata import bind_atom_quote
from semantic_contract import sha256_json


def project_signature_only_declarations(response, chunk, *, validate):
    if (response.get("contract_version") != "3.0" or response.get("conflicts")
            or response.get("reported_conflicts")):
        return None, []
    requirements, clauses, reviews = (response.get("requirements"), chunk.get("clauses"), response.get("clause_reviews"))
    if not all(isinstance(x, list) for x in (requirements, clauses, reviews)):
        return None, []
    by_id = {c.get("id"): c for c in clauses if isinstance(c, dict)}
    by_review = {r.get("clause_id"): r for r in reviews if isinstance(r, dict)}
    if len(by_id) != len(clauses) or len(by_review) != len(reviews) or set(by_id) != set(by_review):
        return None, []
    evidence = chunk.get("evidence_context", {})
    errors = validate(response, chunk)
    if not errors:
        return None, []
    allowed = re.compile(r"\$\.requirements\[(\d+)\](?::non_requirement_classification_relation:clause_ids=.+|\.properties\.items: generic author/date/signature lines .+)$")
    targets = set()
    for error in errors:
        match = allowed.fullmatch(error)
        if match is None:
            return None, []
        targets.add(int(match[1]))
    groups = derive_fixed_declaration_candidates(clauses, evidence, anchor=chunk.get("declaration_anchor_preference"))
    proofs = []
    for index in sorted(targets):
        if index >= len(requirements):
            return None, []
        req = requirements[index]
        cids, eids = req.get("clause_ids"), req.get("evidence_ids")
        if (req.get("role") != "declarations" or req.get("existing_requirement_id") is not None
                or req.get("field_key") is not None or req.get("applicability") is not None
                or req.get("input_prerequisites") not in (None, [])
                or (req.get("verification") or {}).get("mode") != "external"
                or not isinstance(cids, list) or not cids or len(set(cids)) != len(cids)
                or any(cid not in by_id for cid in cids)
                or req.get("source_fragment_clause_ids") not in (None, cids)):
            return None, []
        props = req.get("properties", {})
        items = props.get("items")
        if set(props) != {"items", "before_role"} or not isinstance(items, list) or len(items) != 1:
            return None, []
        item = items[0]
        if (not isinstance(item, dict) or item.get("heading") is not None or item.get("body") is not None
                or any(item.get(k) is not None for k in ("resource_id", "version", "sha256"))
                or item.get("source_signature_lines")):
            return None, []
        matches = []
        for group in groups:
            if group.get("signature_clause_ids") != cids or group.get("signature_evidence_ids") != eids:
                continue
            lines = bound_signature_lines(group, evidence)
            if (not lines or item.get("body_parts") != [line["text"] for line in lines]
                    or item.get("source_evidence_ids") != eids):
                continue
            text = re.sub(r"\s+", "", "".join(line["text"] for line in lines))
            placeholders = item.get("signature_placeholders")
            if (not isinstance(placeholders, list) or any(not isinstance(p, dict)
                    or p.get("attestation_scope") != "placeholder_presence_only"
                    or p.get("role") not in {"author", "supervisor", "date"}
                    or not isinstance(p.get("label"), str) or not p["label"].strip()
                    or re.sub(r"\s+", "", p["label"]) not in text for p in placeholders)):
                continue
            for other_index, kept in enumerate(requirements):
                if other_index in targets or kept.get("role") != "declarations":
                    continue
                kept_props = kept.get("properties", {})
                kept_items = kept_props.get("items", [])
                if (kept_props.get("before_role") == props["before_role"] and len(kept_items) == 1
                        and kept_items[0].get("source_evidence_ids") == group["evidence_ids"]
                        and kept_items[0].get("source_signature_lines") == lines
                        and matches_declaration_render_selection(group, kept.get("clause_ids"),
                            kept_items[0].get("source_evidence_ids"))):
                    matches.append((other_index, group, lines))
        if len(matches) != 1:
            return None, []
        retained_index, group, lines = matches[0]
        try:
            for cid in [*group["clause_ids"], *cids]:
                _exact_clause_source_text(by_id[cid], evidence)
                span = by_id[cid]["source_span"]
                if span.get("location") != evidence[span["evidence_id"]].get("location"):
                    return None, []
            for cid in cids:
                review = by_review[cid]
                atoms = review.get("obligations")
                if (review.get("classification") != "external_compliance" or not isinstance(atoms, list) or not atoms
                        or any(not isinstance(a, dict) or a.get("status") != "unverifiable"
                               or a.get("route") != "human" for a in atoms)):
                    return None, []
                for atom in atoms:
                    bind_atom_quote(atom.get("source_quote"), cid, by_id, evidence)
        except (NativeSemanticReviewError, ValueError, KeyError, TypeError):
            return None, []
        proofs.append({"removed_index": index, "retained_index": retained_index,
            "removed_proposal": copy.deepcopy(req), "source_group": copy.deepcopy(group),
            "source_signature_lines": lines,
            "pending_reviews": {cid: copy.deepcopy(by_review[cid]) for cid in cids}})
    candidate = copy.deepcopy(response)
    candidate["requirements"] = [req for i, req in enumerate(candidate["requirements"]) if i not in targets]
    if validate(candidate, chunk):
        return None, []
    return candidate, [{"rule_id": "source_bound_signature_block_projection_v1", "proofs": proofs,
        "source_response_sha256": sha256_json(response), "repaired_response_sha256": sha256_json(candidate),
        "source_chunk_sha256": sha256_json(chunk), "provenance": copy.deepcopy(chunk.get("provenance")),
        "clause_reviews_unchanged": True, "external_actions_remain_pending": True,
        "independent_review_required": True, "submission_ready": False}]
