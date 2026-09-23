"""Canonical question/evidence identity at question-pipeline boundaries."""
from __future__ import annotations

import copy
from typing import Any


def normalize_question_record(question: Any) -> dict[str, Any]:
    """Normalize legacy ``id``/``evidence_id`` aliases once, failing on conflict."""
    if not isinstance(question, dict):
        raise ValueError("question record must be an object")
    canonical_id = question.get("question_id")
    legacy_id = question.get("id")
    if canonical_id not in (None, "") and legacy_id not in (None, "") and canonical_id != legacy_id:
        raise ValueError("question_id conflicts with legacy id")
    question_id = canonical_id if canonical_id not in (None, "") else legacy_id
    if not isinstance(question_id, str) or not question_id.strip():
        raise ValueError("question id must be a non-empty string")
    if question_id != question_id.strip():
        raise ValueError("question id must not contain surrounding whitespace")

    evidence_ids = question.get("evidence_ids", [])
    legacy_evidence_id = question.get("evidence_id")
    if evidence_ids is None:
        evidence_ids = []
    if not isinstance(evidence_ids, list) or any(
        not isinstance(value, str) or not value.strip() or value != value.strip()
        for value in evidence_ids
    ):
        raise ValueError("evidence_ids must be an array of non-empty strings")
    if len(evidence_ids) != len(set(evidence_ids)):
        raise ValueError("evidence_ids contains duplicates")
    if legacy_evidence_id not in (None, ""):
        if (
            not isinstance(legacy_evidence_id, str)
            or not legacy_evidence_id.strip()
            or legacy_evidence_id != legacy_evidence_id.strip()
        ):
            raise ValueError("legacy evidence_id must be a string")
        if evidence_ids and legacy_evidence_id not in evidence_ids:
            raise ValueError("evidence_id conflicts with evidence_ids")
        if not evidence_ids:
            evidence_ids = [legacy_evidence_id]

    result = copy.deepcopy(question)
    result.pop("id", None)
    result.pop("evidence_id", None)
    if question_id is not None:
        result["question_id"] = question_id
    result["evidence_ids"] = list(evidence_ids)
    return result


def normalize_question_records(questions: Any) -> list[dict[str, Any]]:
    if not isinstance(questions, list):
        raise ValueError("questions must be an array")
    result = [normalize_question_record(item) for item in questions]
    ids = [item.get("question_id") for item in result if item.get("question_id")]
    if len(ids) != len(set(ids)):
        raise ValueError("questions contain duplicate question_id values")
    return result


def bind_question_records(
    questions: Any,
    clauses: Any,
    evidence_doc: Any = None,
) -> list[dict[str, Any]]:
    """Bind every source question to a current clause and its cited evidence.

    A global question is accepted only with an explicit ``scope=global`` and
    no evidence IDs. This is reserved for run-level prompts, not a way to
    bypass source binding for clause questions.
    """
    normalized = normalize_question_records(questions)
    if not isinstance(clauses, list):
        raise ValueError("question source clauses must be an array")
    clause_map: dict[str, dict[str, Any]] = {}
    for clause in clauses:
        if not isinstance(clause, dict):
            raise ValueError("question source clause must be an object")
        clause_id = clause.get("id")
        if not isinstance(clause_id, str) or not clause_id.strip():
            raise ValueError("question source clause id must be a non-empty string")
        if clause_id in clause_map:
            raise ValueError(f"duplicate question source clause id: {clause_id}")
        clause_map[clause_id] = clause

    evidence_map: dict[str, dict[str, Any]] | None = None
    if evidence_doc is not None:
        if not isinstance(evidence_doc, dict) or not isinstance(evidence_doc.get("evidence"), list):
            raise ValueError("question evidence document must contain an evidence array")
        evidence_map = {}
        for evidence in evidence_doc["evidence"]:
            if not isinstance(evidence, dict):
                raise ValueError("question evidence record must be an object")
            evidence_id = evidence.get("id")
            if not isinstance(evidence_id, str) or not evidence_id.strip():
                raise ValueError("question evidence id must be a non-empty string")
            if evidence_id in evidence_map:
                raise ValueError(f"duplicate question evidence id: {evidence_id}")
            evidence_map[evidence_id] = evidence

    for question in normalized:
        clause_id = question.get("clause_id")
        evidence_ids = question.get("evidence_ids", [])
        if clause_id in (None, ""):
            if question.get("scope") == "global" and not evidence_ids:
                continue
            raise ValueError(
                f"question {question['question_id']} is not bound to a source clause"
            )
        if not isinstance(clause_id, str) or clause_id not in clause_map:
            raise ValueError(
                f"question {question['question_id']} references an unknown source clause"
            )
        clause = clause_map[clause_id]
        clause_evidence = clause.get("evidence_ids")
        if (
            not isinstance(clause_evidence, list)
            or not clause_evidence
            or any(
                not isinstance(value, str) or not value.strip() or value != value.strip()
                for value in clause_evidence
            )
            or len(clause_evidence) != len(set(clause_evidence))
        ):
            raise ValueError(f"source clause {clause_id} has no bound evidence IDs")
        if not evidence_ids:
            raise ValueError(f"question {question['question_id']} has no cited evidence IDs")
        if not set(evidence_ids) <= set(clause_evidence):
            raise ValueError(
                f"question {question['question_id']} cites evidence not bound to {clause_id}"
            )
        if evidence_map is not None and not set(evidence_ids) <= set(evidence_map):
            raise ValueError(
                f"question {question['question_id']} cites evidence absent from the current evidence map"
            )
        source_text = question.get("source_text")
        clause_text = clause.get("text")
        if not isinstance(clause_text, str) or not clause_text.strip():
            raise ValueError(f"source clause {clause_id} has no source text")
        if source_text not in (None, "") and source_text != clause_text:
            raise ValueError(
                f"question {question['question_id']} source_text differs from its bound clause"
            )
        question["source_text"] = clause_text
    return normalized
