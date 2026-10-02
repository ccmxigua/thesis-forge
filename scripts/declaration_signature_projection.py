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


def _heading_owner(req, index, requirements, targets, groups, evidence, by_review):
    """An existing print edge for a title may join its sole complete entity.

    No new clause classification or edge is invented: union the two supplied
    print relations only after exact current heading/body ownership is proved.
    """
    if (req.get("role") != "declarations" or req.get("existing_requirement_id") is not None
            or req.get("field_key") is not None or req.get("applicability") is not None
            or req.get("source_fragment_clause_ids") not in (None, req.get("clause_ids"))
            or req.get("input_prerequisites") not in (None, [])
            or (req.get("verification") or {}).get("mode") != "static_docx"
            or (req.get("verification") or {}).get("checker_ids") not in (None, [])):
        return None
    props = req.get("properties") or {}
    items = props.get("items")
    if set(props) != {"items", "before_role"} or not isinstance(items, list) or len(items) != 1:
        return None
    item = items[0]
    if (not isinstance(item, dict) or item.get("body") is not None or item.get("body_parts") is not None
            or item.get("signature_placeholders") != [] or item.get("source_signature_lines")
            or any(item.get(k) is not None for k in ("resource_id", "version", "sha256"))):
        return None
    matches = []
    for group in groups:
        cid = group.get("heading_clause_id")
        eids = group.get("heading_evidence_ids")
        if (req.get("clause_ids") != [cid] or req.get("evidence_ids") != eids
                or item.get("source_evidence_ids") != eids or len(eids) != 1):
            continue
        heading = evidence.get(eids[0], {}).get("text")
        if not isinstance(heading, str) or not heading.strip() or item.get("heading") not in (None, heading):
            continue
        review = by_review.get(cid, {})
        atoms = review.get("obligations")
        if (review.get("classification") not in {"covered", "executable", "verify_existing"}
                or not isinstance(atoms, list) or not atoms
                or any(not isinstance(a, dict) or a.get("status") != "covered"
                       or a.get("route") != "automatic" for a in atoms)):
            continue
        expected_body = [evidence.get(eid, {}).get("text") for eid in group["body_evidence_ids"]]
        for owner_index, owner in enumerate(requirements):
            if owner_index in targets or owner_index == index or owner.get("role") != "declarations":
                continue
            if (owner.get("existing_requirement_id") is not None or owner.get("field_key") is not None
                    or owner.get("applicability") is not None
                    or owner.get("source_fragment_clause_ids") is not None
                    or owner.get("input_prerequisites") not in (None, [])):
                continue
            owner_props = owner.get("properties") or {}
            owner_items = owner_props.get("items")
            if not isinstance(owner_items, list) or len(owner_items) != 1:
                continue
            kept = owner_items[0]
            if (owner_props.get("before_role") == props["before_role"]
                    and kept.get("heading") == heading and kept.get("body") is None
                    and kept.get("body_parts") == expected_body
                    and matches_declaration_render_selection(group, owner.get("clause_ids"), kept.get("source_evidence_ids"))):
                matches.append((owner_index, group))
    return matches[0] if len(matches) == 1 else None


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
    allowed = re.compile(r"\$\.requirements\[(\d+)\](?::non_requirement_classification_relation:clause_ids=.+|\.properties\.items: generic author/date/signature lines .+|\.properties\.items\[\d+\]: declaration_source_text_not_materialized)$")
    targets = set()
    for error in errors:
        match = allowed.fullmatch(error)
        if match is None:
            return None, []
        targets.add(int(match[1]))
    groups = derive_fixed_declaration_candidates(clauses, evidence, anchor=chunk.get("declaration_anchor_preference"))
    proofs, heading_proofs = [], []
    for index in sorted(targets):
        if index >= len(requirements):
            return None, []
        req = requirements[index]
        heading_owner = _heading_owner(req, index, requirements, targets, groups, evidence, by_review)
        if heading_owner is not None:
            owner_index, group = heading_owner
            try:
                for cid in group["clause_ids"]:
                    _exact_clause_source_text(by_id[cid], evidence)
                    span = by_id[cid]["source_span"]
                    if span.get("location") != evidence[span["evidence_id"]].get("location"):
                        return None, []
                for atom in by_review[group["heading_clause_id"]]["obligations"]:
                    bind_atom_quote(atom.get("source_quote"), group["heading_clause_id"], by_id, evidence)
            except (NativeSemanticReviewError, ValueError, KeyError, TypeError):
                return None, []
            heading_proofs.append({"removed_index": index, "retained_index": owner_index,
                "removed_proposal": copy.deepcopy(req), "source_group": copy.deepcopy(group),
                "retained_proposal": copy.deepcopy(requirements[owner_index]),
                "effect_disposition": "same_current_source_heading_already_printed_by_complete_entity"})
            continue
        cids, eids = req.get("clause_ids"), req.get("evidence_ids")
        verification = req.get("verification") or {}
        if (req.get("role") != "declarations" or req.get("existing_requirement_id") is not None
                or req.get("field_key") is not None or req.get("applicability") is not None
                or req.get("input_prerequisites") not in (None, [])
                or verification.get("mode") not in {"external", "static_docx"}
                or verification.get("checker_ids") not in (None, [])
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
            # Native proposals can express the same blank printed structure
            # using placeholders alone, rather than copying it into body_parts.
            # Neither form is authority to remove a line: the unique retained
            # declaration must already print all exact current source lines.
            body_parts = item.get("body_parts")
            if (not lines or body_parts not in (None, [line["text"] for line in lines])
                    or item.get("source_evidence_ids") != eids):
                continue
            text = re.sub(r"\s+", "", "".join(line["text"] for line in lines))
            placeholders = item.get("signature_placeholders")
            if (not isinstance(placeholders, list) or (body_parts is None and not placeholders)
                    or any(not isinstance(p, dict)
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
            "removed_print_representation": ("source_body_parts" if item.get("body_parts") is not None
                                            else "source_bound_placeholders"),
            "verification_mode": verification.get("mode"),
            "all_removed_print_operations_already_retained": True,
            "pending_reviews": {cid: copy.deepcopy(by_review[cid]) for cid in cids}})
    candidate = copy.deepcopy(response)
    for proof in heading_proofs:
        owner = candidate["requirements"][proof["retained_index"]]
        old_ids = owner["clause_ids"]
        supplied_ids = set(old_ids) | set(requirements[proof["removed_index"]]["clause_ids"])
        owner["clause_ids"] = [c["id"] for c in clauses if c["id"] in supplied_ids]
        owner["evidence_ids"] = list(dict.fromkeys(eid for cid in owner["clause_ids"] for eid in by_id[cid]["evidence_ids"]))
        proof["clause_ids_before"] = copy.deepcopy(old_ids)
        proof["clause_ids_after"] = copy.deepcopy(owner["clause_ids"])
        proof["all_edges_previously_supplied"] = True
    candidate["requirements"] = [req for i, req in enumerate(candidate["requirements"]) if i not in targets]
    if validate(candidate, chunk):
        return None, []
    audits = []
    for rule, rule_proofs in (("source_bound_signature_block_projection_v2", proofs),
                             ("source_bound_declaration_heading_coalescence_v1", heading_proofs)):
        if not rule_proofs:
            continue
        audits.append({"rule_id": rule, "proofs": rule_proofs,
        "source_response_sha256": sha256_json(response), "repaired_response_sha256": sha256_json(candidate),
        "source_chunk_sha256": sha256_json(chunk), "provenance": copy.deepcopy(chunk.get("provenance")),
        "clause_reviews_unchanged": True, "external_actions_remain_pending": True,
        "independent_review_required": True, "submission_ready": False})
    return candidate, audits
