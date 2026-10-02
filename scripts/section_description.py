"""Source-bound section descriptions are not missing-manuscript instructions.

This closed grammar recognizes complete nominative paragraphs only. It cannot
establish whether a thesis section exists, authorize writing, or satisfy a rule.
Unrecognized text stays with the semantic reviewer and the existing gates.
"""
from __future__ import annotations

import copy
import re
from typing import Any

from format_spec_validation import validate_instance
from semantic_contract import sha256_json
from source_literal_binding import compose_source_fragments

POLICY = "standalone-section-description/v1"
_DESCRIPTION = re.compile(
    r"\s*(?:这|本)(?:一)?(?:部分|节|章)是(?:本)?论文的"
    r"(?P<section>中文摘要|英文摘要|摘要|绪论|引言|结论|参考文献|致谢)[。.]?\s*"
)


def compile_section_description(source: Any) -> dict[str, Any] | None:
    match = _DESCRIPTION.fullmatch(source) if isinstance(source, str) else None
    if match is None:
        return None
    return {"policy": POLICY, "section": match["section"], "source_quote": source,
            "source_quote_sha256": sha256_json(source),
            "authoring_instruction": False, "manuscript_presence": "not_assessed",
            "execution_authorized": False, "submission_ready": False}


def project_section_description_claims(response: Any, chunk: dict) -> tuple[Any, list[dict]]:
    """Retract only source-disproved input claims; retain their original audit.

    No requirement/edge is removed. An existing catalog rule, partial paragraph,
    stale span, malformed review, mixed duty, condition, or duplicate identity
    prevents projection. Independent review must still read the new candidate.
    """
    candidate = copy.deepcopy(response)
    if not isinstance(response, dict) or response.get("contract_version") != "3.0":
        return candidate, []
    reviews, requirements = response.get("clause_reviews"), response.get("requirements")
    clauses = chunk.get("clauses")
    evidence = chunk.get("evidence_context")
    schema = chunk.get("response_schema", {}).get("properties", {}).get("clause_reviews", {}).get("items")
    if not (isinstance(reviews, list) and isinstance(requirements, list)
            and isinstance(clauses, list) and isinstance(evidence, dict) and isinstance(schema, dict)):
        return candidate, []
    clause_map = {c.get("id"): c for c in clauses if isinstance(c, dict)}
    if len(clause_map) != len(clauses):
        return candidate, []
    counts = [r.get("clause_id") for r in reviews if isinstance(r, dict)]
    catalog = chunk.get("rule_spec", {}).get("requirements", [])
    conflicts = response.get("reported_conflicts", [])
    if not isinstance(catalog, list) or not isinstance(conflicts, list):
        return candidate, []
    audits = []
    for index, review in enumerate(reviews):
        if not isinstance(review, dict) or validate_instance(review, schema):
            continue
        cid = review.get("clause_id")
        if counts.count(cid) != 1 or review.get("classification") != "requires_source_content":
            continue
        if any(cid in (r.get("clause_ids") or []) for r in [*requirements, *catalog] if isinstance(r, dict)):
            continue
        if any(cid in (conflict.get("clause_ids") or []) for conflict in conflicts if isinstance(conflict, dict)):
            continue
        try:
            binding = compose_source_fragments([cid], clause_map, evidence)
        except (ValueError, KeyError, TypeError):
            continue
        fragment = binding["source_fragments"][0]
        source = evidence[fragment["evidence_id"]].get("text")
        fact = compile_section_description(source)
        if (fact is None or fragment["source_kind"] != "paragraph"
                or fragment["text"].strip().rstrip("。.") != source.strip().rstrip("。.")):
            continue
        atoms = review.get("obligations", [])
        if not isinstance(atoms, list):
            continue
        ids = [a.get("id") for a in atoms if isinstance(a, dict)]
        if len(ids) != len(atoms) or len(set(ids)) != len(ids):
            continue
        if any(a.get("status") != "requires_source_content"
               or a.get("source_quote") not in (source, fragment["text"])
               or a.get("condition") or a.get("route") not in (None, "input")
               or a.get("force") not in (None, "required")
               or a.get("applicability") not in (None, "applicable") for a in atoms):
            continue
        after = candidate["clause_reviews"][index]
        after.update(classification="informational", obligations=[],
                     reason="Current standalone source describes a section; it does not instruct the author to supply content. Manuscript presence is not assessed.")
        audits.append({"policy": POLICY, "clause_id": cid, "source_fact": fact,
                       "source_binding": binding, "provenance": copy.deepcopy(chunk.get("provenance")),
                       "before_review": copy.deepcopy(review), "after_review": copy.deepcopy(after),
                       "before_review_sha256": sha256_json(review), "after_review_sha256": sha256_json(after),
                       "requirements_unchanged": True, "requires_independent_review": True,
                       "submission_ready": False})
    return candidate, audits
