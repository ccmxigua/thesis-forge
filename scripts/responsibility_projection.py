"""Prove redundant render proposals without deleting source obligations.

This does not reclassify, guess a target, widen a source edge, or manufacture
approval. The complete caller validator and independent review remain required.
"""
from __future__ import annotations

import copy
from typing import Any, Callable

from administrative_relation_projection import _contained
from fixed_declaration_source import derive_fixed_declaration_candidates
from native_semantic_review import NativeSemanticReviewError, _exact_clause_source_text
from semantic_contract import sha256_json

POLICY = "source_entity_responsibility_projection_v1"


def project_redundant_render_entities(response: dict, chunk: dict, *,
        validate: Callable[[dict, dict], list[str]]) -> tuple[dict | None, list[dict]]:
    if response.get("contract_version") != "3.0" or response.get("reported_conflicts") or response.get("conflicts"):
        return None, []
    requirements, reviews, clauses = (response.get("requirements"), response.get("clause_reviews"), chunk.get("clauses"))
    if not all(isinstance(x, list) for x in (requirements, reviews, clauses)):
        return None, []
    by_id = {c.get("id"): c for c in clauses if isinstance(c, dict)}
    review_map = {r.get("clause_id"): r for r in reviews if isinstance(r, dict)}
    if len(by_id) != len(clauses) or len(review_map) != len(reviews) or set(by_id) != set(review_map):
        return None, []
    evidence = chunk.get("evidence_context", {})
    try:
        texts = {cid: _exact_clause_source_text(c, evidence) for cid, c in by_id.items()}
    except (NativeSemanticReviewError, ValueError, TypeError, KeyError):
        return None, []
    for clause in clauses:
        span = clause["source_span"]
        record = evidence.get(span["evidence_id"], {})
        if (span.get("location") != record.get("location")
                or clause.get("location", span.get("location")) != span.get("location")):
            return None, []

    def ids(req: dict) -> set | None:
        cids, eids = req.get("clause_ids"), req.get("evidence_ids")
        if (not isinstance(cids, list) or not cids or len(set(cids)) != len(cids)
                or not set(cids) <= set(by_id) or not isinstance(eids, list)
                or len(set(eids)) != len(eids)
                or set(eids) != {eid for cid in cids for eid in by_id[cid].get("evidence_ids", [])}):
            return None
        return set(cids)

    def safe_envelope(req: dict) -> bool:
        return (isinstance(req, dict) and req.get("existing_requirement_id") is None
                and req.get("input_prerequisites") in (None, [])
                and set(req) <= {"role", "properties", "clause_ids", "evidence_ids", "reason", "confidence",
                    "field_key", "verification", "existing_requirement_id", "input_prerequisites",
                    "applicability", "source_fragment_clause_ids"})

    def complete_review(cid: str, external: bool = False) -> bool:
        r = review_map[cid]
        obs = r.get("obligations")
        return (isinstance(obs, list) and bool(obs) and all(isinstance(o, dict) for o in obs)
                and ((r.get("classification") == "external_compliance"
                      and all(o.get("status") == "unverifiable" for o in obs)) if external else
                     (r.get("classification") in {"covered", "executable", "verify_existing", "executable_with_external_check"}
                      and any(o.get("status") == "covered" for o in obs))))

    removed: dict[int, int] = {}
    proofs = []
    for index, req in enumerate(requirements):
        if not safe_envelope(req):
            continue
        cids = ids(req)
        if not cids:
            continue
        matches = []
        for other_index, retained in enumerate(requirements):
            if index == other_index or not safe_envelope(retained) or retained.get("role") != req.get("role"):
                continue
            other_ids = ids(retained)
            if not other_ids or req.get("applicability") != retained.get("applicability"):
                continue
            if not _contained(req.get("properties"), retained.get("properties")):
                continue
            checks = (req.get("verification") or {}).get("checker_ids") or []
            kept_checks = (retained.get("verification") or {}).get("checker_ids") or []
            if not set(checks) <= set(kept_checks):
                continue
            if req.get("role") == "declarations":
                if not cids < other_ids or not all(complete_review(cid) for cid in other_ids):
                    continue
                anchor = retained.get("properties", {}).get("before_role")
                candidates = derive_fixed_declaration_candidates(clauses, evidence, anchor=anchor)
                items = retained.get("properties", {}).get("items", [])
                if len(items) != 1 or req.get("properties") != retained.get("properties"):
                    continue
                groups = [g for g in candidates if other_ids <= set(g["clause_ids"])
                          and set(items[0].get("source_evidence_ids") or []) == set(g["evidence_ids"])]
                if len(groups) != 1 or req.get("field_key") != retained.get("field_key"):
                    continue
                entity = {"kind": "fixed_declaration", "source_group": groups[0]}
            elif req.get("role") == "cover":
                if req.get("source_fragment_clause_ids") is not None or not all(complete_review(cid, True) for cid in cids):
                    continue
                if not all(complete_review(cid) for cid in other_ids):
                    continue
                admin = retained.get("properties", {}).get("non_public_administration")
                if not isinstance(admin, dict):
                    continue
                heading_ids = [cid for cid in by_id if texts[cid] == admin.get("source_region")]
                tables = {by_id[cid].get("source_span", {}).get("location", {}).get("table_child_index")
                          for cid in other_ids if "table_child_index" in by_id[cid].get("source_span", {}).get("location", {})}
                if len(heading_ids) != 1 or len(tables) != 1:
                    continue
                heading = by_id[heading_ids[0]]["source_span"].get("location", {})
                table = next(iter(tables))
                if type(table) is not int or type(heading.get("child_index")) is not int:
                    continue
                # A pure approval can refer to this table only through the same
                # operative source paragraph immediately before that table.
                if table != heading["child_index"] + 2 or any(
                    by_id[cid]["source_span"].get("location", {}).get("part") != heading.get("part")
                    or by_id[cid]["source_span"].get("location", {}).get("child_index") != table - 1
                    or not any(by_id[cid]["source_span"].get("evidence_id") == by_id[k]["source_span"].get("evidence_id")
                               for k in other_ids) for cid in cids):
                    continue
                entity = {"kind": "administrative_table", "heading_span": by_id[heading_ids[0]]["source_span"],
                          "table_child_index": table}
            else:
                continue
            matches.append((other_index, entity))
        if len(matches) == 1:
            retained_index, entity = matches[0]
            removed[index] = retained_index
            proofs.append({"removed_index": index, "retained_index": retained_index, "entity": entity,
                           "removed_proposal": copy.deepcopy(req), "pending_obligations": {
                               cid: copy.deepcopy(review_map[cid].get("obligations")) for cid in sorted(cids)},
                           "effect_disposition": "exact_payload_contained_in_unique_source_entity",
                           "source_edges_changed": False})
    if not removed or any(target in removed for target in removed.values()):
        return None, []
    candidate = copy.deepcopy(response)
    candidate["requirements"] = [r for i, r in enumerate(candidate["requirements"]) if i not in removed]
    if validate(candidate, chunk):
        return None, []
    return candidate, [{"rule_id": POLICY, "proofs": proofs, "removed_indexes": sorted(removed),
                        "source_chunk_sha256": sha256_json(chunk), "provenance": copy.deepcopy(chunk.get("provenance")),
                        "source_response_sha256": sha256_json(response), "repaired_response_sha256": sha256_json(candidate),
                        "clause_reviews_unchanged": candidate["clause_reviews"] == response["clause_reviews"],
                        "external_actions_remain_pending": True, "independent_review_required": True}]
