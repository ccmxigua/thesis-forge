"""Keep a narrow primary/independent classification conflict for human review.

This record preserves competing source interpretations without treating an
ambiguous independent observation as an obligation or as complete coverage.
It is available only to the explicit non-release review-draft path.
"""
from __future__ import annotations

import copy
import hashlib

from semantic_contract import sha256_json
from source_obligation_compiler import (
    compile_known_source_obligation_ids,
    compile_source_content_verification_codes,
    compile_unresolved_manual_review_codes,
    has_mixed_external_document_action_signal,
)
from pending_source_work import compile_pending_source_work

POLICY = "source_bound_informational_uncertainty_dispute_v1"
TEMPLATE_EXAMPLE_ALIGNMENT_POLICY = "source_bound_template_example_alignment_dispute_v1"
TYPED_ALIGNMENT_RETRY_CODE = "typed_source_atom_alignment_disagreement"


def informational_uncertainty_dispute(check, result):
    """Return a source-bound dispute for one tightly constrained conflict.

The primary must explicitly classify a source-only unit as informational with
no obligations. The independent result must retain only fully ambiguous,
unlinked observations with unknown force and applicability. Code-owned facts,
pending work, source-verification rules, and mixed duties disqualify the case.
"""
    if not isinstance(check, dict) or not isinstance(result, dict):
        return None
    context, text = check.get("review_context"), check.get("document_text")
    if (
        not isinstance(context, dict)
        or not isinstance(text, str) or not text.strip()
        or context.get("classification") != "informational"
        or context.get("primary_normative_basis") != "source_content"
        or context.get("requires_requirement") is not False
        or context.get("primary_obligations") != []
        or context.get("primary_obligation_quote_bindings") != []
        or context.get("linked_requirements") != []
        or context.get("source_clause_support") != []
        or context.get("semantic_clause_text") != text
        or result.get("check_id") != check.get("check_id")
        or result.get("verdict") != "uncertain"
        or not isinstance(result.get("rationale"), str)
        or not result["rationale"].strip()
        or result.get("machine_obligation_ids") != []
    ):
        return None

    quotes = result.get("evidence_quotes")
    obligations = result.get("identified_obligations")
    if (
        not isinstance(quotes, list) or not quotes
        or any(not isinstance(q, str) or not q.strip() or q not in text for q in quotes)
        or not isinstance(obligations, list) or not obligations
    ):
        return None
    for atom in obligations:
        if (
            not isinstance(atom, dict)
            or atom.get("disposition") != "ambiguous"
            or atom.get("force") != "unknown"
            or atom.get("applicability") != "unknown"
            or atom.get("requirement_refs") != []
            or atom.get("pending_work_code") is not None
            or atom.get("scope_dependency_codes")
            or atom.get("scope_dependency_dimensions")
            or not isinstance(atom.get("source_quote"), str)
            or not atom["source_quote"].strip()
            or atom["source_quote"] not in text
            or (atom.get("target") is not None and (
                not isinstance(atom.get("target"), str)
                or not atom["target"].strip()
                or atom["target"] not in text
            ))
        ):
            return None

    machine_ids = compile_known_source_obligation_ids(text)
    pending_work = compile_pending_source_work(text)
    manual_codes = compile_unresolved_manual_review_codes(text)
    verification_codes = compile_source_content_verification_codes(text)
    if (
        machine_ids or pending_work or manual_codes or verification_codes
        or has_mixed_external_document_action_signal(text)
        or context.get("machine_obligation_ids") != machine_ids
        or context.get("pending_source_work") != pending_work
        or context.get("manual_review_codes") != manual_codes
        or context.get("source_content_verification_codes") != verification_codes
    ):
        return None

    cited_evidence = context.get("cited_evidence")
    if not isinstance(cited_evidence, dict) or not cited_evidence:
        return None
    for evidence in cited_evidence.values():
        if not isinstance(evidence, dict) or not isinstance(evidence.get("text"), str):
            return None

    return {
        "policy": POLICY,
        "check_id": check["check_id"],
        "source_text": text,
        "source_text_sha256": sha256_json(text),
        "check_sha256": sha256_json(check),
        "primary_review_context": copy.deepcopy(context),
        "primary_review_context_sha256": sha256_json(context),
        "independent_result": copy.deepcopy(result),
        "independent_result_sha256": sha256_json(result),
        "status": "human_review_pending",
        "coverage_complete": False,
        "execution_authorized": False,
        "submission_ready": False,
    }


def _template_example_alignment_retry_is_bound(check, request):
    """Require the exact final, source-bound typed-alignment retry for this check."""
    if not isinstance(check, dict) or not isinstance(request, dict):
        return None
    checks = request.get("checks")
    feedback = request.get("retry_feedback")
    check_id = check.get("check_id")
    request_check = next((item for item in checks or []
                          if isinstance(item, dict) and item.get("check_id") == check_id), None)
    check_projection = {key: value for key, value in check.items() if key != "source_spans"}
    request_check_projection = (
        {key: value for key, value in request_check.items() if key != "source_spans"}
        if isinstance(request_check, dict) else None
    )
    if (
        request.get("output_policy") != "review_draft"
        or request.get("provider_attempt") != 2
        or not isinstance(checks, list)
        or request_check_projection != check_projection
        or not isinstance(feedback, dict)
        or feedback.get("code") != TYPED_ALIGNMENT_RETRY_CODE
        or feedback.get("checks_sha256") != sha256_json(checks)
        or feedback.get("run_id") != request.get("run_id")
        or feedback.get("provenance") != request.get("provenance")
        or not isinstance(check_id, str)
        or not isinstance(feedback.get("clause_ids"), list)
        or not isinstance(feedback.get("disagreements"), list)
        or check_id not in (feedback.get("clause_ids") or [])
    ):
        return None
    context = check.get("review_context") if isinstance(check.get("review_context"), dict) else {}
    primary = context.get("primary_obligations")
    if not isinstance(primary, list) or len(primary) != 1:
        return None
    atom = primary[0]
    if not isinstance(atom, dict):
        return None
    matching = [item for item in feedback.get("disagreements", [])
                if isinstance(item, dict) and item.get("check_id") == check_id]
    if (
        len(matching) != 1
        or matching[0].get("primary_obligation_id") != atom.get("id")
        or matching[0].get("primary_sha256") != sha256_json(atom)
        or matching[0].get("fields") != ["primary_obligation_id"]
        or not isinstance(feedback.get("candidate_response_sha256"), str)
        or len(feedback["candidate_response_sha256"]) != 64
        or any(char not in "0123456789abcdef" for char in feedback["candidate_response_sha256"])
    ):
        return None
    return copy.deepcopy(matching[0]), sha256_json(feedback)


def template_example_alignment_dispute(check, result, *, request=None):
    """Preserve one exact final-retry conflict about an optional cover example.

    The primary's single typed atom must be an optional template display
    example, bound to the current source occurrence. The independent reviewer
    must explicitly return a consistent empty inventory. This never changes
    either review or certifies coverage.
    """
    if not isinstance(check, dict) or not isinstance(result, dict):
        return None
    text = check.get("document_text")
    context = check.get("review_context") if isinstance(check.get("review_context"), dict) else {}
    check_id = check.get("check_id")
    retry = _template_example_alignment_retry_is_bound(check, request)
    if (
        retry is None
        or not isinstance(text, str) or not text.strip()
        or context.get("classification") != "informational"
        or context.get("primary_normative_basis") != "template_structure"
        or context.get("requires_requirement") is not False
        or context.get("semantic_clause_text") != text
        or context.get("linked_requirements") != []
        or context.get("source_clause_support") != []
        or result.get("check_id") != check_id
        or result.get("verdict") != "consistent"
        or result.get("identified_obligations") != []
        or result.get("machine_obligation_ids") != []
        or not isinstance(result.get("rationale"), str)
        or not result["rationale"].strip()
    ):
        return None

    quotes = result.get("evidence_quotes")
    if not isinstance(quotes, list) or not quotes or any(
        not isinstance(quote, str) or not quote or quote not in text for quote in quotes
    ):
        return None

    atom = context["primary_obligations"][0]
    quote = atom.get("source_quote")
    if (
        not isinstance(atom.get("id"), str) or not atom["id"]
        or atom.get("status") != "covered"
        or atom.get("actor") != "template"
        or atom.get("action") != "display"
        or atom.get("force") != "optional"
        or atom.get("applicability") != "applicable"
        or atom.get("route") != "example"
        or atom.get("condition") not in (None, "")
        or not isinstance(quote, str) or not quote or quote not in text
        or atom.get("target") not in {
            f"cover field label {quote}", f"cover date marker {quote}",
        }
    ):
        return None

    bindings = context.get("primary_obligation_quote_bindings")
    if not isinstance(bindings, list) or len(bindings) != 1:
        return None
    binding = bindings[0]
    source_binding = binding.get("source_binding") if isinstance(binding, dict) else None
    clause_binding = source_binding.get("clause_binding") if isinstance(source_binding, dict) else None
    fragments = clause_binding.get("source_fragments") if isinstance(clause_binding, dict) else None
    evidence = context.get("cited_evidence")
    if (
        not isinstance(evidence, dict)
        or not isinstance(binding, dict)
        or not isinstance(source_binding, dict)
        or binding.get("primary_obligation_id") != atom["id"]
        or binding.get("review_source_quote") != quote
        or binding.get("review_quote_sha256") != sha256_json(quote)
        or binding.get("semantic_dimensions_unchanged") is not True
        or not isinstance(binding.get("original_source_quote"), str)
        or binding.get("original_quote_sha256") != sha256_json(binding["original_source_quote"])
        or source_binding.get("policy") != "current_source_occurrence_quote_v1"
        or not isinstance(clause_binding, dict)
        or clause_binding.get("text") != text
        or not isinstance(fragments, list) or not fragments
    ):
        return None
    for fragment in fragments:
        if not isinstance(fragment, dict):
            return None
        evidence_item = evidence.get(fragment.get("evidence_id"))
        if (
            fragment.get("clause_id") != check_id
            or fragment.get("text") != text
            or not isinstance(fragment.get("source_sha256"), str)
            or not isinstance(evidence_item, dict)
            or not isinstance(evidence_item.get("text"), str)
            or isinstance(fragment.get("start_offset"), bool)
            or not isinstance(fragment.get("start_offset"), int)
            or isinstance(fragment.get("end_offset"), bool)
            or not isinstance(fragment.get("end_offset"), int)
            or fragment["end_offset"] - fragment["start_offset"] != len(fragment["text"])
            or fragment["start_offset"] < 0
            or fragment["end_offset"] > len(evidence_item["text"])
            or evidence_item["text"][fragment["start_offset"]:fragment["end_offset"]] != fragment["text"]
            or fragment["source_sha256"] != hashlib.sha256(evidence_item["text"].encode("utf-8")).hexdigest()
        ):
            return None
    original_quote = binding["original_source_quote"]
    start = source_binding.get("quote_start_offset")
    end = source_binding.get("quote_end_offset")
    if (
        source_binding.get("quote_sha256") != sha256_json(original_quote)
        or isinstance(start, bool) or not isinstance(start, int)
        or isinstance(end, bool) or not isinstance(end, int)
        or start < 0
        or end - start != len(original_quote)
        or not any(
            item.get("text", "")[start:end] == original_quote
            for item in evidence.values()
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    ):
        return None

    machine_ids = compile_known_source_obligation_ids(text)
    pending_work = compile_pending_source_work(text)
    manual_codes = compile_unresolved_manual_review_codes(text)
    verification_codes = compile_source_content_verification_codes(text)
    if (
        machine_ids or pending_work or manual_codes or verification_codes
        or has_mixed_external_document_action_signal(text)
        or context.get("machine_obligation_ids") != machine_ids
        or context.get("pending_source_work") != pending_work
        or context.get("manual_review_codes") != manual_codes
        or context.get("source_content_verification_codes") != verification_codes
    ):
        return None

    disagreement, feedback_sha = retry
    request = request or {}
    return {
        "policy": TEMPLATE_EXAMPLE_ALIGNMENT_POLICY,
        "dispute_type": "optional_template_example_vs_empty_independent_inventory",
        "check_id": check_id,
        "source_text": text,
        "source_text_sha256": sha256_json(text),
        "check_sha256": sha256_json(check),
        "provider_attempt": request.get("provider_attempt"),
        "retry_feedback_sha256": feedback_sha,
        "typed_alignment_disagreement": disagreement,
        "primary_review_context": copy.deepcopy(context),
        "primary_review_context_sha256": sha256_json(context),
        "independent_result": copy.deepcopy(result),
        "independent_result_sha256": sha256_json(result),
        "status": "human_review_pending",
        "coverage_complete": False,
        "execution_authorized": False,
        "submission_ready": False,
    }


def build_informational_uncertainty_disputes(checks, results, *, request=None):
    by_id = {item["check_id"]: item for item in checks}
    assessments = []
    for result in results:
        if not isinstance(result, dict):
            continue
        check = by_id.get(result.get("check_id"))
        assessment = informational_uncertainty_dispute(check, result)
        if assessment is None:
            assessment = template_example_alignment_dispute(check, result, request=request)
        if assessment is not None:
            assessments.append(assessment)
    return assessments


def validate_informational_uncertainty_disputes(record, checks, results, *, request=None):
    expected = build_informational_uncertainty_disputes(checks, results, request=request)
    if record.get("source_classification_disputes", []) != expected:
        raise ValueError("source classification disputes do not match the current source and both reviews")
    return expected
