#!/usr/bin/env python3
"""Validate and bind user acknowledgements for unresolved semantic clauses.

This is deliberately separate from the Host Agent response contract.  A
confirmation acknowledges that a semantic issue is real and should remain
visible; it never resolves the clause, creates a requirement, or supplies an
executable formatting property.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from format_spec_validation import validate_instance
from semantic_contract import sha256_json, strict_json_loads


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schema" / "semantic-issue-confirmation.schema.json"
RECEIPT_SCHEMA_PATH = ROOT / "schema" / "semantic-issue-confirmation-receipt.schema.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def load_json(path: Path) -> Any:
    return strict_json_loads(path.read_text(encoding="utf-8"))


def _schema_errors(value: Any) -> list[str]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return validate_instance(value, schema)


def _receipt_schema_errors(value: Any) -> list[str]:
    schema = json.loads(RECEIPT_SCHEMA_PATH.read_text(encoding="utf-8"))
    return validate_instance(value, schema)


def _clause_binding(clause: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": clause.get("id"),
        "text": clause.get("text"),
        "source_text_full": clause.get("source_text_full"),
        "source_kind": clause.get("source_kind"),
        "evidence_ids": sorted(str(item) for item in (clause.get("evidence_ids") or [])),
    }


def _question_binding(question: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(question)


def _provenance(spec: dict[str, Any]) -> dict[str, Any]:
    value = spec.get("semantic_review_provenance")
    if not isinstance(value, dict):
        raise ValueError("format spec has no semantic_review_provenance")
    if spec.get("semantic_review_provenance_valid") is not True:
        raise ValueError("format spec semantic_review_provenance_valid is not true")
    required = (
        "run_id", "source_sha256", "evidence_sha256", "clause_sha256", "request_sha256",
    )
    missing = [key for key in required if not isinstance(value.get(key), str)]
    invalid = [key for key in required[1:] if not SHA256_RE.fullmatch(str(value.get(key) or ""))]
    if missing or invalid:
        raise ValueError(
            "semantic review provenance is incomplete: "
            + ", ".join(sorted(set(missing + invalid)))
        )
    return value


def bind_confirmations(
    raw: dict[str, Any],
    spec: dict[str, Any],
    questions: list[Any],
    clauses: list[Any],
    *,
    expected_case_id: str | None = None,
    expected_run_id: str | None = None,
) -> dict[str, Any]:
    """Validate a confirmation request against the current extracted run.

    The returned object is a bound ledger.  Its binding is generated from the
    current spec/questions/clauses and cannot be supplied by a model or copied
    from a previous run.
    """
    errors = _schema_errors(raw)
    if errors:
        raise ValueError("invalid semantic issue confirmation: " + "; ".join(errors))
    if raw.get("binding") is not None:
        raise ValueError("confirmation input must be unbound; pass a bound ledger only to downstream stages")
    provenance = _provenance(spec)
    case_id = str(raw.get("case_id") or "")
    actual_case_id = str(expected_case_id or "")
    if not actual_case_id:
        raise ValueError("current case_id is required for semantic issue confirmation binding")
    if case_id != actual_case_id:
        raise ValueError(
            f"semantic issue case_id mismatch: confirmation={case_id!r}, current={actual_case_id!r}"
        )
    source_binding = raw["source_binding"]
    expected_source_binding = {
        "run_id": str(provenance["run_id"]),
        "case_id": actual_case_id,
        "source_sha256": provenance["source_sha256"],
        "evidence_sha256": provenance["evidence_sha256"],
        "clause_sha256": provenance["clause_sha256"],
        "request_sha256": provenance["request_sha256"],
    }
    if source_binding != expected_source_binding:
        raise ValueError("semantic issue confirmation source binding does not match the current run")
    run_id = str(expected_run_id or spec.get("run_id") or "")
    if not run_id:
        raise ValueError("current run_id is required for semantic issue confirmation binding")
    if run_id != str(provenance["run_id"]) or str(spec.get("run_id") or "") != run_id:
        raise ValueError("semantic issue confirmation run_id does not match the current format spec")

    question_by_id = {
        str(item.get("id")): item
        for item in questions
        if isinstance(item, dict) and item.get("id")
    }
    clause_by_id = {
        str(item.get("id")): item
        for item in clauses
        if isinstance(item, dict) and item.get("id")
    }
    compliance_by_id = {
        str(item.get("clause_id")): item
        for item in (spec.get("clause_compliance") or [])
        if isinstance(item, dict) and item.get("clause_id")
    }
    seen: set[str] = set()
    bound_items: list[dict[str, Any]] = []
    for index, item in enumerate(raw["confirmations"]):
        clause_id = str(item.get("clause_id") or "")
        question_id = str(item.get("question_id") or "")
        if clause_id in seen:
            raise ValueError(f"duplicate semantic issue confirmation for {clause_id}")
        seen.add(clause_id)
        question = question_by_id.get(question_id)
        clause = clause_by_id.get(clause_id)
        compliance = compliance_by_id.get(clause_id)
        if question is None:
            raise ValueError(f"confirmation[{index}] references unknown question_id {question_id}")
        if clause is None:
            raise ValueError(f"confirmation[{index}] references unknown clause_id {clause_id}")
        if str(question.get("clause_id")) != clause_id:
            raise ValueError(f"confirmation[{index}] question/clause mismatch for {clause_id}")
        if not isinstance(compliance, dict) or compliance.get("status") != "unresolved":
            raise ValueError(f"confirmation[{index}] clause {clause_id} is not currently unresolved")
        expected_evidence = sorted(str(value) for value in (question.get("evidence_ids") or []))
        clause_evidence = sorted(str(value) for value in (clause.get("evidence_ids") or []))
        supplied_evidence = sorted(str(value) for value in (item.get("evidence_ids") or []))
        if supplied_evidence != expected_evidence or supplied_evidence != clause_evidence:
            raise ValueError(f"confirmation[{index}] evidence_ids do not exactly match current evidence for {clause_id}")
        if not str(item.get("reason") or "").strip():
            raise ValueError(f"confirmation[{index}] has an empty reason")
        bound_items.append({
            "clause_id": clause_id,
            "question_id": question_id,
            "evidence_ids": supplied_evidence,
            "reason": str(item["reason"]).strip(),
            "clause_sha256": sha256_json(_clause_binding(clause)),
            "question_sha256": sha256_json(_question_binding(question)),
        })

    binding = {
        "run_id": run_id,
        "case_id": actual_case_id,
        "source_sha256": provenance["source_sha256"],
        "evidence_sha256": provenance["evidence_sha256"],
        "clause_sha256": provenance["clause_sha256"],
        "request_sha256": provenance["request_sha256"],
        "questions_sha256": sha256_json(questions),
        "clauses_sha256": sha256_json(clauses),
        "input_sha256": sha256_json(raw),
    }
    return {
        "schema_version": "1.0",
        "case_id": actual_case_id,
        "scope": "analysis_only",
        "disposition": "confirmed_semantic_issue",
        "source_binding": copy.deepcopy(source_binding),
        "confirmations": bound_items,
        "binding": binding,
    }


def validate_bound_ledger_for_spec(
    ledger: dict[str, Any], spec: dict[str, Any], *, expected_case_id: str | None = None,
    clauses: list[Any] | None = None, questions: list[Any] | None = None,
) -> set[str]:
    """Validate a bound ledger at the formatting boundary and return clause IDs."""
    errors = _schema_errors(ledger)
    if errors:
        raise ValueError("invalid bound semantic issue ledger: " + "; ".join(errors))
    provenance = _provenance(spec)
    if str(spec.get("run_id") or "") != str(provenance["run_id"]):
        raise ValueError("format spec run_id does not match semantic review provenance")
    binding = ledger["binding"]
    source_binding = ledger["source_binding"]
    if binding["case_id"] != ledger["case_id"]:
        raise ValueError("semantic issue ledger binding case_id does not match its top-level case_id")
    if source_binding != {
        "run_id": str(provenance["run_id"]),
        "case_id": ledger["case_id"],
        "source_sha256": provenance["source_sha256"],
        "evidence_sha256": provenance["evidence_sha256"],
        "clause_sha256": provenance["clause_sha256"],
        "request_sha256": provenance["request_sha256"],
    }:
        raise ValueError("semantic issue ledger source binding does not match the current run")
    if expected_case_id is not None and binding["case_id"] != expected_case_id:
        raise ValueError("semantic issue ledger case_id does not match the current case")
    for key in ("run_id", "source_sha256", "evidence_sha256", "clause_sha256", "request_sha256"):
        expected = str(provenance.get(key) or "")
        if binding.get(key) != expected:
            raise ValueError(f"semantic issue ledger binding mismatch for {key}")
    ids = {str(item["clause_id"]) for item in ledger["confirmations"]}
    unresolved = {
        str(item.get("clause_id"))
        for item in (spec.get("clause_compliance") or [])
        if isinstance(item, dict) and item.get("status") == "unresolved"
    }
    if not ids <= unresolved:
        raise ValueError("semantic issue ledger contains a clause that is not unresolved in the current spec")
    if clauses is not None:
        if binding["clauses_sha256"] != sha256_json(clauses):
            raise ValueError("semantic issue ledger clauses hash does not match the current clause set")
        clause_by_id = {
            str(item.get("id")): item for item in clauses
            if isinstance(item, dict) and item.get("id")
        }
        for item in ledger["confirmations"]:
            clause = clause_by_id.get(str(item["clause_id"]))
            if clause is None or item["clause_sha256"] != sha256_json(_clause_binding(clause)):
                raise ValueError(f"semantic issue ledger clause binding is stale for {item['clause_id']}")
            if sorted(str(value) for value in item["evidence_ids"]) != sorted(
                str(value) for value in (clause.get("evidence_ids") or [])
            ):
                raise ValueError(f"semantic issue ledger evidence binding is stale for {item['clause_id']}")
    if questions is not None:
        if binding["questions_sha256"] != sha256_json(questions):
            raise ValueError("semantic issue ledger questions hash does not match the current question set")
        question_by_id = {
            str(item.get("id")): item for item in questions
            if isinstance(item, dict) and item.get("id")
        }
        for item in ledger["confirmations"]:
            question = question_by_id.get(str(item["question_id"]))
            if question is None or item["question_sha256"] != sha256_json(_question_binding(question)):
                raise ValueError(f"semantic issue ledger question binding is stale for {item['question_id']}")
    return ids


def confirmed_clause_ids(ledger: dict[str, Any] | None) -> set[str]:
    if not isinstance(ledger, dict):
        return set()
    return {
        str(item.get("clause_id"))
        for item in ledger.get("confirmations", [])
        if isinstance(item, dict) and item.get("clause_id")
    }


def build_confirmation_receipt(ledger: dict[str, Any]) -> dict[str, Any]:
    """Create a deterministic receipt for the bound acknowledgement ledger.

    This receipt is separate from the immutable Host Agent merge receipt.  It
    records the post-review user acknowledgement stage without mutating or
    re-signing the model response.
    """
    receipt = {
        "schema_version": "1.0",
        "receipt_type": "semantic_issue_confirmation",
        "status": "bound",
        "case_id": ledger["case_id"],
        "scope": ledger["scope"],
        "disposition": ledger["disposition"],
        "source_binding": copy.deepcopy(ledger["source_binding"]),
        "binding": copy.deepcopy(ledger["binding"]),
        "confirmed_clause_ids": sorted(confirmed_clause_ids(ledger)),
        "confirmations": copy.deepcopy(ledger["confirmations"]),
        "ledger_sha256": sha256_json(ledger),
    }
    errors = _receipt_schema_errors(receipt)
    if errors:
        raise ValueError("generated semantic issue confirmation receipt is invalid: " + "; ".join(errors))
    return receipt


def validate_confirmation_receipt(
    receipt: dict[str, Any], ledger: dict[str, Any]
) -> None:
    """Verify that a receipt is exactly for the supplied bound ledger."""
    errors = _receipt_schema_errors(receipt)
    if errors:
        raise ValueError("invalid semantic issue confirmation receipt: " + "; ".join(errors))
    if receipt["ledger_sha256"] != sha256_json(ledger):
        raise ValueError("semantic issue confirmation receipt ledger hash does not match the ledger")
    for key in (
        "case_id", "scope", "disposition", "source_binding", "binding",
        "confirmed_clause_ids", "confirmations",
    ):
        expected = (
            sorted(confirmed_clause_ids(ledger))
            if key == "confirmed_clause_ids"
            else ledger[key]
        )
        if receipt[key] != expected:
            raise ValueError(f"semantic issue confirmation receipt mismatch for {key}")


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirmations", type=Path, required=True)
    parser.add_argument("--format-spec", type=Path, required=True)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--clauses", type=Path, required=True)
    parser.add_argument("--case-id")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--receipt-out", type=Path)
    args = parser.parse_args(argv)
    raw = load_json(args.confirmations)
    spec = load_json(args.format_spec)
    questions = load_json(args.questions)
    clause_document = load_json(args.clauses)
    clauses = clause_document if isinstance(clause_document, list) else clause_document.get("clauses", [])
    ledger = bind_confirmations(
        raw, spec, questions, clauses,
        expected_case_id=args.case_id,
        expected_run_id=str(spec.get("run_id") or "") or None,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.receipt_out:
        args.receipt_out.parent.mkdir(parents=True, exist_ok=True)
        receipt = build_confirmation_receipt(ledger)
        args.receipt_out.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps({"status": "bound", "out": str(args.out),
                      "receipt": str(args.receipt_out) if args.receipt_out else None,
                      "confirmed_clause_ids": sorted(confirmed_clause_ids(ledger))},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
