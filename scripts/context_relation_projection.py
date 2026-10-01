"""Separate zero-duty context from execution edges without dropping source.

This candidate-only repair never decides a clause's meaning. The primary
review is preserved and fresh source-first review must still confirm it before
acceptance. Detached context stays in the full source and repair receipt.
"""
from __future__ import annotations

import copy
import re
from typing import Any, Callable

from host_review_contract import analyze_requirement_relations, contract_error_records
from native_semantic_review import NativeSemanticReviewError, _exact_clause_source_text
from semantic_contract import sha256_json
from source_obligation_compiler import (
    compile_known_source_obligations, compile_keyword_source_constraints,
    compile_abstract_source_constraints,
)

POLICY = "zero_duty_context_edge_projection_v1"


def _context_heading(clause: dict, evidence: dict) -> bool:
    """Closed nominal-heading grammar, not proof that arbitrary prose is empty.

    Unknown wording stays on the ordinary semantic path. In particular, an
    empty model inventory cannot authorize detaching an approval instruction.
    """
    span = clause["source_span"]
    source = evidence[span["evidence_id"]]
    text = span["text"].strip()
    if (source.get("kind") != "paragraph"
            or span["start_offset"] != 0 or span["end_offset"] != len(source["text"])
            or not text or len(text) > 80):
        return False
    if re.search(
        r"[。；;!?！？\n]|必须|不得|禁止|须|应|请|批准|同意|申请|签署|签字|盖章|提交|"
        r"\b(?:must|shall|should|approval|consent|apply|sign|seal|submit)\b|"
        r"\d\s*(?:字|个|组|行|页|年|cm|mm|pt|磅|words?\b)", text, re.I,
    ):
        return False
    return re.fullmatch(
        r"[^。；;!?！？\n]{0,70}(?:说明|须知|要求|规范|规则)|"
        r"(?:[\w -]{0,60} )?(?:notes|instructions|requirements|guidelines)", text, re.I,
    ) is not None


def _references(value: Any, ids: set[str]) -> bool:
    """Don't detach source selected by a payload, selector or condition."""
    if isinstance(value, str):
        return any(source_id in value for source_id in ids)
    if isinstance(value, list):
        return any(_references(item, ids) for item in value)
    if isinstance(value, dict):
        return any(key in ids or _references(item, ids) for key, item in value.items())
    return False


def project_context_edges(
    response: dict, chunk: dict, records: list[dict], *,
    validate: Callable[[dict, dict], list[str]],
    source_projection_validation_sha256: str | None,
    invocation_fingerprints: dict,
) -> tuple[dict | None, list[dict]]:
    """Detach zero-duty informational edges from a full validator bundle.

    The caller must authenticate the chunk against the full current source.
    Self-consistent spans alone are not authority. External/unresolved edges,
    missing inventories, selectors, conflicts and existing payloads are not
    eligible. This is not a semantic reclassification or publication gate.
    """
    if (source_projection_validation_sha256 != sha256_json(chunk)
            or response.get("contract_version") != "3.0"
            or response.get("reported_conflicts") or response.get("conflicts")
            or invocation_fingerprints.get("chunk_sha256") != sha256_json(chunk)):
        return None, []
    actual = contract_error_records(validate(response, chunk), response=response, chunk=chunk)
    if (not actual or not records
            or sorted(sha256_json(r) for r in actual) != sorted(sha256_json(r) for r in records)
            or any(r.get("code") not in {
                "mixed_execution_classification_relation", "missing_derived_requirement",
            } for r in actual)):
        return None, []
    requirements, reviews, clauses = response.get("requirements"), response.get("clause_reviews"), chunk.get("clauses")
    if not all(isinstance(items, list) for items in (requirements, reviews, clauses)):
        return None, []
    if any(not isinstance(c, dict) or not isinstance(c.get("id"), str) for c in clauses):
        return None, []
    if any(not isinstance(r, dict) or not isinstance(r.get("clause_id"), str) for r in reviews):
        return None, []
    by_id = {c["id"]: c for c in clauses}
    review_map = {r["clause_id"]: r for r in reviews}
    if len(by_id) != len(clauses) or len(review_map) != len(reviews) or set(by_id) != set(review_map):
        return None, []
    evidence = chunk.get("evidence_context")
    if not isinstance(evidence, dict):
        return None, []
    try:
        for clause in clauses:
            _exact_clause_source_text(clause, evidence)
            span = clause["source_span"]
            record = evidence[span["evidence_id"]]
            if (record.get("id") != span["evidence_id"]
                    or span.get("location") != record.get("location")
                    or clause.get("location", span.get("location")) != span.get("location")):
                return None, []
    except (NativeSemanticReviewError, ValueError, KeyError, TypeError):
        return None, []

    candidate = copy.deepcopy(response)
    proofs = []
    for fact in analyze_requirement_relations(response, clauses):
        if fact["category"] != "mixed_execution_classification":
            continue
        index = fact["requirement_index"]
        req = requirements[index]
        cids, eids = req.get("clause_ids"), req.get("evidence_ids")
        if (not isinstance(cids, list) or any(not isinstance(cid, str) for cid in cids)
                or len(set(cids)) != len(cids) or not set(cids) <= set(by_id)
                or not isinstance(eids, list) or any(not isinstance(eid, str) for eid in eids)
                or len(set(eids)) != len(eids)
                or set(eids) != {eid for cid in cids for eid in by_id[cid].get("evidence_ids", [])}
                or req.get("existing_requirement_id") or req.get("field_key")
                or req.get("source_fragment_clause_ids") is not None
                or req.get("role") == "declarations"
                or (isinstance(req.get("properties"), dict)
                    and req["properties"].get("text") is not None)):
            return None, []
        contexts = [cid for cid in cids if review_map[cid].get("classification") == "informational"]
        retained = [cid for cid in cids if cid not in contexts]
        if not contexts or not retained:
            return None, []
        for cid in contexts:
            review = review_map[cid]
            # A primary informational label cannot erase compiled source
            # facts, an unknown inventory or an explicitly normative duty.
            if (review.get("obligations") != []
                    or review.get("normative_basis") != "insufficient"
                    or not _context_heading(by_id[cid], evidence)
                    or by_id[cid].get("deterministic_obligation_keys")
                    or compile_known_source_obligations(by_id[cid]["source_span"]["text"])
                    or compile_keyword_source_constraints(by_id[cid])
                    or compile_abstract_source_constraints(by_id[cid]["source_span"]["text"])):
                return None, []
        for cid in retained:
            review = review_map[cid]
            obs = review.get("obligations")
            if (review.get("classification") not in {"covered", "executable", "verify_existing"}
                    or not isinstance(obs, list) or not obs
                    or any(not isinstance(o, dict) or o.get("status") != "covered" for o in obs)):
                return None, []
        allowed_evidence = {eid for cid in retained for eid in by_id[cid].get("evidence_ids", [])}
        detached_evidence = set(eids) - allowed_evidence
        payload = {key: value for key, value in req.items() if key not in {"clause_ids", "evidence_ids"}}
        if _references(payload, set(contexts) | detached_evidence):
            return None, []
        candidate["requirements"][index]["clause_ids"] = retained
        candidate["requirements"][index]["evidence_ids"] = [eid for eid in eids if eid in allowed_evidence]
        proofs.append({
            "requirement_index": index,
            "json_pointer": f"$.requirements[{index}].clause_ids",
            "original_requirement": copy.deepcopy(req),
            "retained_clause_ids": retained,
            "context_edges": [{
                "clause_id": cid, "clause": copy.deepcopy(by_id[cid]),
                "review": copy.deepcopy(review_map[cid]),
                "evidence": [copy.deepcopy(evidence[eid]) for eid in by_id[cid]["evidence_ids"]],
            } for cid in contexts],
            "detached_requirement_evidence_ids": [eid for eid in eids if eid in detached_evidence],
        })
    if not proofs or validate(candidate, chunk):
        return None, []
    return candidate, [{
        "rule_id": POLICY, "proofs": proofs,
        "input_fingerprints": copy.deepcopy(invocation_fingerprints),
        "source_projection_validation_sha256": source_projection_validation_sha256,
        "source_chunk_sha256": sha256_json(chunk),
        "source_response_sha256": sha256_json(response),
        "repaired_response_sha256": sha256_json(candidate),
        "error_bundle_sha256": sha256_json(records),
        "clause_reviews_unchanged": True, "requirement_payloads_unchanged": True,
        "context_preserved_in_source_and_receipt": True,
        "independent_review_required": True, "submission_ready": False,
    }]
