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
from table_source_context import build_table_structure_context
from source_obligation_compiler import (
    compile_known_source_obligations, compile_keyword_source_constraints,
    compile_abstract_source_constraints,
)

POLICY = "zero_duty_context_edge_projection_v1"
TABLE_POLICY = "source_bound_blank_date_context_edge_v1"
# Versioned metadata types from coverMetadata, not source/school identifiers.
# Other field types must remain on the semantic path even beside a date blank.
DATE_METADATA_FIELDS = frozenset({"completion_date", "approval_date", "embargo_start", "embargo_until"})


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


def _blank_date_field_context(clause: dict, evidence: dict, req: dict,
                             retained: list[str], by_id: dict) -> dict | None:
    """Propose context separation, not a date-format or approval verdict.

    A current, complete, unmerged table row must prove a single adjacent
    label occurrence already selected by a retained field. Neither normalized
    clause text nor a model's context_before can establish ownership. Source
    formatting remains available to the mandatory independent review.
    """
    span = clause["source_span"]
    source = evidence[span["evidence_id"]]
    if (req.get("role") != "cover" or source.get("kind") != "table_cell"
            or re.fullmatch(r"\s*年\s*月\s*日\s*", span["text"]) is None
            or span["start_offset"] != 0 or span["end_offset"] != len(source["text"])):
        return None
    try:
        context = build_table_structure_context(clause, evidence)
    except (ValueError, KeyError, TypeError):
        return None
    if not context or context["relationship"] != "same_row_immediate_left_unmerged":
        return None
    row = context["source_row"]
    left = row["cells"][context["immediate_left_column"]]
    label_parts = [p for p in left["paragraphs"] if p["text"].strip()]
    if len(label_parts) != 1:
        return None
    label = label_parts[0]
    eid, text = label["evidence_id"], label["text"]
    if not text.strip() or re.fullmatch(r"\s*年\s*月\s*日\s*", text):
        return None
    # Duplicate labels/clauses cannot select a field owner by first-match.
    if sum(p["text"] == text for cell in row["cells"] for p in cell["paragraphs"]) != 1:
        return None
    owners = [cid for cid, c in by_id.items()
              if c["source_span"]["evidence_id"] == eid]
    if len(owners) != 1 or owners[0] not in retained or eid not in evidence:
        return None
    owner = by_id[owners[0]]["source_span"]
    if (owner["start_offset"] != 0 or owner["end_offset"] != len(text)
            or owner["text"] != text):
        return None
    props = req.get("properties")
    if not isinstance(props, dict):
        return None
    fields = props.get("fields", [])
    admin = props.get("non_public_administration", {})
    if not isinstance(fields, list) or not isinstance(admin, dict):
        return None
    admin_fields = admin.get("fields", [])
    if not isinstance(admin_fields, list):
        return None
    fields = fields + admin_fields
    if any(not isinstance(f, dict) for f in fields):
        return None
    selected = [f for f in fields if f.get("label") == text]
    if (len(selected) != 1 or selected[0].get("id") not in DATE_METADATA_FIELDS
            or selected[0].get("value_from") != f"thesis_profile.cover_metadata.{selected[0]['id']}"
            or sum(f.get("id") == selected[0]["id"] for f in fields) != 1):
        return None
    return {"policy": TABLE_POLICY, "owner_clause_id": owners[0],
            "retained_field": copy.deepcopy(selected[0]),
            "table_structure_context": context,
            "date_format_confirmed": False, "external_action_confirmed": False}


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
        context_basis = {}
        for cid in contexts:
            review = review_map[cid]
            table_basis = _blank_date_field_context(by_id[cid], evidence, req, retained, by_id)
            # A primary informational label cannot erase compiled source
            # facts, an unknown inventory or an explicitly normative duty.
            if (review.get("obligations") != []
                    or review.get("normative_basis") != "insufficient"
                    or not (_context_heading(by_id[cid], evidence) or table_basis is not None)
                    or by_id[cid].get("deterministic_obligation_keys")
                    or compile_known_source_obligations(by_id[cid]["source_span"]["text"])
                    or compile_keyword_source_constraints(by_id[cid])
                    or compile_abstract_source_constraints(by_id[cid]["source_span"]["text"])):
                return None, []
            context_basis[cid] = table_basis or {"policy": POLICY, "kind": "nominal_heading"}
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
                "context_basis": context_basis[cid],
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
