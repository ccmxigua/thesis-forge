"""Separate a source-first no-duty assessment from preserved primary uncertainty.

Geometry is eligibility for review, not proof of meaning or permission to release.
No clause ID, school, role assignment or source literal selects this policy.
"""
from __future__ import annotations

import copy
import re

from semantic_contract import sha256_json
from table_source_context import build_table_structure_context, text_sha256
from source_obligation_compiler import (
    compile_known_source_obligations, compile_unresolved_manual_review_codes,
    compile_source_content_verification_codes,
)
from pending_source_work import compile_pending_source_work

POLICY = "source_bound_unresolved_form_label_assessment_v1"


def unresolved_label_assessment(check, result):
    """Return an analysis-only record, or None; never rewrite either input.

Only a whole short alphabetic label ending in a source colon, in a complete
unmerged table row followed by a blank cell, with wholly unknown primary atoms
can take this path. A fresh independent source-first consistent/empty review
must still establish that no duty is identified. Readable normative prose,
known facts, mixed/executable/pending duties and missing source proof cannot.
"""
    if not isinstance(check, dict) or not isinstance(result, dict):
        return None
    context = check.get("review_context")
    text = check.get("document_text")
    if (not isinstance(context, dict) or not isinstance(text, str)
            or context.get("classification") != "unresolved"
            or context.get("requires_requirement") is not False
            or context.get("linked_requirements") != []
            or context.get("source_clause_support", [])
            or result.get("check_id") != check.get("check_id")
            or result.get("verdict") != "consistent"
            or result.get("identified_obligations") != []
            or result.get("machine_obligation_ids") != []
            or result.get("evidence_quotes") != [text]
            or not isinstance(result.get("rationale"), str) or not result["rationale"].strip()):
        return None
    # This is a closed lexical shape, not a blacklist that blesses other prose.
    label = text.strip().removesuffix(":").removesuffix("：").strip()
    if (not 1 <= len(label) <= 60 or not all(c.isalpha() or c == " " for c in label)
            or re.search(r"必须|不得|禁止|须|应|请|批准|同意|申请|签署|签字|盖章|提交|填写|"
                         r"最多|最少|不超过|不少于|至少|至多|如果|若|当|通常|一般|推荐|承担|责任|"
                         r"\b(?:must|shall|should|required|approval|consent|apply|sign|seal|submit|fill|"
                         r"if|when|unless|not|minimum|maximum|usually|generally|recommended|responsibility)\b",
                         label, re.I)
            or re.fullmatch(
                r"[\u4e00-\u9fff ]{0,24}(?:名|号|码|级|日期|单位|院系|专业|职称|方向)|"
                r"[A-Za-z ]{0,45}\b(?:name|number|code|level|date|department|faculty|major|title|author|supervisor)",
                label, re.I) is None):
        return None
    facts = (compile_known_source_obligations(text), compile_unresolved_manual_review_codes(text),
             compile_source_content_verification_codes(text), compile_pending_source_work(text))
    if any(facts) or any(context.get(k) for k in (
            "machine_obligation_ids", "manual_review_codes", "source_content_verification_codes",
            "pending_source_work")):
        return None
    atoms = context.get("primary_obligations")
    bindings = context.get("primary_obligation_quote_bindings")
    if not isinstance(atoms, list) or not atoms or not isinstance(bindings, list) or len(bindings) != len(atoms):
        return None
    ids = [a.get("id") for a in atoms if isinstance(a, dict)]
    if len(ids) != len(atoms) or any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
        return None
    for atom, binding in zip(atoms, bindings):
        if (atom.get("status") != "unresolved" or atom.get("force") != "unknown"
                or atom.get("applicability") != "unknown" or atom.get("route") != "unknown"
                or atom.get("actor") not in {None, "unknown"}
                or atom.get("action") not in {None, "unknown"} or atom.get("condition") is not None
                or atom.get("source_quote") != text
                or not isinstance(binding, dict) or binding.get("primary_obligation_id") != atom["id"]):
            return None
    evidence = context.get("cited_evidence")
    table = context.get("table_structure_context")
    if not isinstance(evidence, dict) or not isinstance(table, dict):
        return None
    eid = table.get("target_evidence_id")
    target = evidence.get(eid)
    if not isinstance(target, dict) or not isinstance(target.get("text"), str):
        return None
    full = target["text"]
    # Allow only the segmentation fringe colon, not a prose prefix/suffix.
    if not full.strip().endswith((":", "：")) or full not in {text, text + ":", text + "："}:
        return None
    try:
        rebuilt = build_table_structure_context({"source_span": {
            "evidence_id": eid, "source_sha256": text_sha256(full)}}, evidence)
        if rebuilt != table:
            return None
        row = table["source_row"]
        cells = row["cells"]
        column = table["target_column"]
        if (type(column) is not int or column + 1 >= len(cells)
                or row["grid_before"] != 0 or row["grid_after"] != 0 or row["bidi_visual"] is not False
                or row["table_grid_columns"] != len(cells)
                or any(c["grid_span"] != 1 or c["vertical_merge"] is not None
                       or c["horizontal_merge"] is not None or c["has_nested_table"] is not False for c in cells)
                or len(cells[column]["paragraphs"]) != 1
                or not cells[column + 1]["paragraphs"]
                or any(p["text"].strip() for p in cells[column + 1]["paragraphs"])):
            return None
        for binding in bindings:
            proof = binding["source_binding"]
            fragments = proof["clause_binding"]["source_fragments"]
            if (len(fragments) != 1 or proof["context_is_not_execution_scope"] is not True
                    or proof["quote_start_offset"] != 0 or proof["quote_end_offset"] != len(text)
                    or proof["quote_sha256"] != sha256_json(text)
                    or binding.get("original_source_quote") != text
                    or binding.get("review_source_quote") != text):
                return None
            f = fragments[0]
            if (f["clause_id"] != check["check_id"] or f["evidence_id"] != eid
                    or f["source_sha256"] != text_sha256(full) or f["text"] != text
                    or f["start_offset"] != 0 or f["end_offset"] != len(text)
                    or f["source_kind"] != "table_cell" or f["location"] != target["location"]):
                return None
    except (KeyError, TypeError, ValueError, IndexError):
        return None
    return {"policy": POLICY, "check_id": check["check_id"],
            "source_text": text, "source_text_sha256": sha256_json(text),
            "source_evidence_sha256": text_sha256(full), "table_context_sha256": sha256_json(table),
            "primary_classification": "unresolved", "primary_obligations": copy.deepcopy(atoms),
            "primary_obligations_sha256": sha256_json(atoms),
            "check_sha256": sha256_json(check), "independent_result_sha256": sha256_json(result),
            "independent_rationale": result["rationale"], "identified_source_obligations": [],
            "uncertainty_resolved": False, "execution_authorized": False, "submission_ready": False}


def build_unresolved_label_assessments(checks, results):
    by_id = {c["check_id"]: c for c in checks}
    return [assessment for result in results
            if (assessment := unresolved_label_assessment(by_id.get(result.get("check_id")), result)) is not None]


def validate_unresolved_label_assessments(ledger, checks, results):
    expected = build_unresolved_label_assessments(checks, results)
    if ledger.get("unresolved_label_assessments", []) != expected:
        raise ValueError("unresolved label assessments do not match the current source and primary uncertainty")
