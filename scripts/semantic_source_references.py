"""Run-bound source selections for native semantic reviews.

The model selects a code-owned span; it never retypes the trusted quotation or
the machine-fact inventory. The captured native invocation remains the proof
of freshness. Reference IDs alone are not a receipt or a semantic verdict.
"""
from __future__ import annotations

import copy
import re
from typing import Any

from format_spec_validation import validate_instance
from host_review_schema import normalize_native_response
from semantic_contract import sha256_json
from pending_source_work import compile_pending_source_work
from source_obligation_compiler import (
    compile_source_content_verification_codes,
    typed_source_verification_inventory_is_bound,
)


REFERENCE_PROTOCOL = "semantic_source_references_v2"


class SourceReferenceResponseError(ValueError):
    """Known current checks failed wire validation; never a repaired response."""

    def __init__(self, issues: list[dict[str, Any]], schema: dict[str, Any]):
        self.issues = copy.deepcopy(issues)
        self.schema_sha256 = sha256_json(schema)
        super().__init__("source reference response rejected: " + "; ".join(
            f"{item['check_id']}: {'; '.join(item['errors'])}" for item in issues))


def source_reference_result_issues(response: Any, schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Locate schema failures only in a complete uniquely mapped current check set."""
    branches = schema["properties"]["results"]["items"]["anyOf"]
    by_id = {b["properties"]["check_id"]["enum"][0]: b for b in branches}
    if (not isinstance(response, dict) or set(response) != {"results"}
            or not isinstance(response["results"], list)
            or any(not isinstance(r, dict) or not isinstance(r.get("check_id"), str)
                   for r in response["results"])):
        return []
    ids = [r["check_id"] for r in response["results"]]
    if len(set(ids)) != len(ids) or set(ids) != set(by_id):
        return []
    issues = []
    for index, result in enumerate(response["results"]):
        branch = by_id[result["check_id"]]
        errors = validate_instance(result, branch)
        if errors:
            details = []
            atom_schema = branch["properties"].get("identified_obligations", {}).get("items", {})
            atoms = result.get("identified_obligations")
            atoms = atoms if isinstance(atoms, list) else []
            for atom_index, atom in enumerate(atoms):
                if validate_instance(atom, atom_schema):
                    alternatives = atom_schema.get("anyOf", [atom_schema])
                    details.append({"obligation_index": atom_index, "alternatives": [
                        validate_instance(atom, option) for option in alternatives]})
            issues.append({"check_id": result["check_id"], "result_index": index,
                           "rejected_result": copy.deepcopy(result), "errors": errors,
                           "obligation_errors": details})
    return issues


_ENGLISH_ABBREVIATIONS = {
    "e.g.", "i.e.", "etc.", "vs.", "dr.", "mr.", "mrs.", "ms.",
    "prof.", "fig.", "no.", "approx.", "al.", "ph.",
}
_MULTI_PART_ABBREVIATION = re.compile(r"(?:[A-Za-z]{1,3}\.){2,}$")


def _source_ranges(text: str) -> list[tuple[int, int]]:
    """Return conservative exact sentence spans without splitting decimals/abbreviations."""
    ranges: list[tuple[int, int]] = [(0, len(text))]
    start = 0
    for index, char in enumerate(text):
        boundary = char in "。！？；!?;\n"
        if char == ".":
            previous = text[index - 1] if index else ""
            following = text[index + 1] if index + 1 < len(text) else ""
            token_match = re.search(
                r"[A-Za-z]+(?:\.[A-Za-z]+)*\.$",
                text[max(start, index - 24):index + 1],
            )
            token = token_match.group().lower() if token_match else ""
            initial = len(token) == 2 and token[0].isalpha()
            decimal = previous.isdigit() and following.isdigit()
            multipart_abbreviation = bool(_MULTI_PART_ABBREVIATION.fullmatch(token))
            boundary = (
                not decimal and not initial
                and token not in _ENGLISH_ABBREVIATIONS
                and not multipart_abbreviation
            )
        if boundary:
            end = index + 1
            if text[start:end].strip():
                ranges.append((start, end))
            start = end
    if start < len(text) and text[start:].strip():
        ranges.append((start, len(text)))
    # Keep offsets exact while preventing blank-only and duplicate spans.
    return list(dict.fromkeys((begin, end) for begin, end in ranges if text[begin:end].strip()))


def build_source_reference_packet(request: dict[str, Any]) -> dict[str, Any]:
    packet = copy.deepcopy(request)
    source_checks = packet.get("checks")
    if not isinstance(source_checks, list) or not source_checks:
        raise ValueError("source reference packet requires a non-empty checks array")
    binding = sha256_json(request)
    seen: set[str] = set()
    for check in source_checks:
        if not isinstance(check, dict):
            raise ValueError("source reference packet check must be an object")
        check_id, text = check.get("check_id"), check.get("document_text")
        if not isinstance(check_id, str) or not check_id or check_id in seen:
            raise ValueError("source reference packet requires unique non-empty check IDs")
        seen.add(check_id)
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"source reference packet has no source text for {check_id}")
        # Keep all whitespace and punctuation. The whole passage remains a
        # fallback span; conservative sentence spans improve English sources
        # without splitting decimal numbers or common abbreviations.
        ranges = _source_ranges(text)
        context = check.get("review_context") or {}
        # Typed primary atoms may cite a proper subspan rather than a whole
        # sentence. Make that exact quotation selectable; never ask a model
        # to retype it or invent a range to satisfy a later equality check.
        for primary in context.get("primary_obligations") or []:
            quote = primary.get("source_quote") if isinstance(primary, dict) else None
            if isinstance(quote, str) and quote.strip() and text.count(quote) == 1:
                offset = text.index(quote)
                ranges.append((offset, offset + len(quote)))
        spans = []
        for start, end in dict.fromkeys(ranges):
            identity = {"request_sha256": binding, "check_id": check_id,
                        "start": start, "end": end, "text": text[start:end]}
            spans.append({
                "ref_id": "Q" + sha256_json(identity)[:16],
                "source_field": "document_text", "start": start, "end": end,
                "text": text[start:end], "source_sha256": sha256_json(text),
            })
        if len({span["ref_id"] for span in spans}) != len(spans):
            raise ValueError(f"source reference ID collision for {check_id}")
        check["source_spans"] = spans
    packet["source_reference_protocol"] = REFERENCE_PROTOCOL
    packet["source_reference_request_sha256"] = binding
    return packet


def _pending_verdict_generation_branches(
    branch: dict[str, Any], atoms: list[dict[str, Any]], context: dict[str, Any],
    *, pending_verdict: str, disposition_statuses: dict[str, str],
    excluded_verdicts: set[str],
) -> list[dict[str, Any]]:
    """Couple pending verdicts to their atom states without hiding omissions.

    Concrete object alternatives survive native projection (unlike if/allOf).
    This is generation-only: parsing preserves rejected provider output for
    the existing bounded correction route and canonical semantic validator.
    """
    pending_atoms = []
    primary = context.get("primary_obligations") or []
    for atom in atoms:
        for disposition, status in disposition_statuses.items():
            if disposition not in atom["properties"]["disposition"].get("enum", []):
                continue
            candidate = copy.deepcopy(atom)
            candidate["properties"]["disposition"] = {"enum": [disposition]}
            if primary:
                ids = [item["id"] for item in primary if item.get("status") == status]
                if not ids or "primary_obligation_id" not in candidate["required"]:
                    continue
                candidate["properties"]["primary_obligation_id"] = {"enum": ids}
            if disposition != "represented":
                candidate["properties"]["requirement_refs"]["maxItems"] = 0
            pending_atoms.append(candidate)
    diagnostic = copy.deepcopy(branch)
    diagnostic["properties"]["verdict"]["enum"] = [
        value for value in diagnostic["properties"]["verdict"]["enum"]
        if value not in excluded_verdicts
    ]
    alternatives = [diagnostic]
    if pending_atoms:
        pending = copy.deepcopy(branch)
        pending["properties"]["verdict"] = {"enum": [pending_verdict]}
        pending["properties"]["identified_obligations"].update({
            "minItems": max(1, len(primary)),
            "items": {"anyOf": pending_atoms},
        })
        alternatives.append(pending)
    return alternatives


def source_reference_schema(
    canonical_schema: dict[str, Any], packet: dict[str, Any], *, coverage: bool,
    constrain_requirement_links: bool = False,
) -> dict[str, Any]:
    """Compile per-check alternatives so references cannot cross clauses."""
    schema = copy.deepcopy(canonical_schema)
    template = schema["properties"]["results"]["items"]
    branches = []
    for check in packet["checks"]:
        branch = copy.deepcopy(template)
        props = branch["properties"]
        props["check_id"] = {"enum": [check["check_id"]]}
        refs = {"type": "string", "enum": [span["ref_id"] for span in check["source_spans"]]}
        props.pop("evidence_quotes")
        props["evidence_refs"] = {"type": "array", "items": refs, "minItems": 1, "uniqueItems": True}
        branch["required"] = ["evidence_refs" if key == "evidence_quotes" else key
                              for key in branch["required"]]
        if coverage:
            props.pop("machine_obligation_ids")
            branch["required"].remove("machine_obligation_ids")
            obligation_schema = props["identified_obligations"]["items"]
            obligation_branches = obligation_schema.get("anyOf")
            if not isinstance(obligation_branches, list) or not obligation_branches:
                obligation_branches = [obligation_schema]
            review_context = check.get("review_context")
            review_context = review_context if isinstance(review_context, dict) else {}
            primary_obligations = review_context.get("primary_obligations")
            primary_ids: list[str] = []
            external_mapping = review_context.get("classification") in {
                "external_compliance", "executable_with_external_check",
            }
            typed_mapping = any(
                isinstance(item, dict) and item.get("force", "unknown") != "unknown"
                and all(item.get(k) not in {None, "", "unknown"}
                        for k in ("actor", "action", "target", "source_quote"))
                for item in (primary_obligations or [])
            ) if isinstance(primary_obligations, list) else False
            if (external_mapping or typed_mapping) and primary_obligations:
                if not isinstance(primary_obligations, list):
                    raise ValueError("external primary obligation inventory is malformed")
                primary_ids = [
                    item.get("id") if isinstance(item, dict) else None
                    for item in primary_obligations
                ]
                if (
                    any(not isinstance(value, str) or not value for value in primary_ids)
                    or len(set(primary_ids)) != len(primary_ids)
                ):
                    raise ValueError("external primary obligation ids are not unique current ids")
            manual_codes = review_context.get("manual_review_codes")
            manual_codes = {
                code for code in manual_codes
                if isinstance(code, str) and code
            } if isinstance(manual_codes, list) else set()
            scope_authorized = (
                review_context.get("classification") == "unresolved"
                and not review_context.get("linked_requirements")
                and bool(manual_codes)
            )
            compiled_obligation_branches = []
            for obligation in obligation_branches:
                obligation_props = obligation.get("properties")
                if not isinstance(obligation_props, dict):
                    raise ValueError("coverage obligation schema branch has no properties")
                disposition_schema = obligation_props.get("disposition")
                is_scope_branch = (
                    isinstance(disposition_schema, dict)
                    and disposition_schema.get("enum") == ["scope_unresolved"]
                )
                if is_scope_branch:
                    if not scope_authorized:
                        continue
                    code_schema = obligation_props.get("scope_dependency_codes", {}).get("items", {})
                    registered_codes = code_schema.get("enum", [])
                    permitted_codes = sorted(manual_codes & set(registered_codes))
                    if not permitted_codes:
                        continue
                    code_schema["enum"] = permitted_codes
                if primary_ids:
                    obligation_props["primary_obligation_id"] = {"enum": primary_ids}
                    if external_mapping and not is_scope_branch:
                        obligation["required"].append("primary_obligation_id")
                    # Ordinary typed checks may discover an additional source
                    # duty with no primary atom. Keep its mapping optional;
                    # the canonical validator separately requires one exact
                    # mapping for every already typed primary atom.
                else:
                    obligation_props.pop("primary_obligation_id", None)
                obligation_props.pop("source_quote")
                obligation_props["source_ref"] = copy.deepcopy(refs)
                obligation["required"] = [
                    "source_ref" if key == "source_quote" else key
                    for key in obligation["required"]
                ]
                compiled_obligation_branches.append(obligation)
                if external_mapping and primary_ids and not is_scope_branch:
                    # A source-first reviewer must be able to report a duty
                    # the primary inventory missed. It cannot claim this new
                    # duty is covered or map it to an unrelated primary ID.
                    unmatched = copy.deepcopy(obligation)
                    unmatched["properties"].pop("primary_obligation_id", None)
                    unmatched["required"].remove("primary_obligation_id")
                    unmatched["properties"]["disposition"] = {"enum": ["unrepresented"]}
                    unmatched["properties"]["requirement_refs"]["maxItems"] = 0
                    compiled_obligation_branches.append(unmatched)
            # A registered pending-work code is a source-grammar selector,
            # not a generic human-review category. Keep generic verification
            # open without a code; do not expose unrelated codes on this check.
            # Recompute from exact source, never from model-authored inventory.
            source_pending_facts = compile_pending_source_work(check["document_text"])
            source_pending_codes = {fact["code"] for fact in source_pending_facts}
            pending_scoped = []
            for obligation in compiled_obligation_branches:
                code_schema = obligation["properties"].get("pending_work_code")
                if not isinstance(code_schema, dict):
                    pending_scoped.append(obligation)
                    continue
                generic = copy.deepcopy(obligation)
                generic["properties"]["pending_work_code"] = {
                    "type": "null",
                    "description": "No registered source-grammar code applies to this generic obligation. Omit (native: null); retain the human duty and exact source reference.",
                }
                pending_scoped.append(generic)
                permitted = sorted(source_pending_codes & set(code_schema.get("enum", [])))
                # External/mixed primary mappings accept only represented
                # DOCX atoms and actual external actions, not authoring or
                # content-verification codes. A discovered mismatched duty
                # stays reportable as unrepresented without a borrowed ID.
                if (permitted and not external_mapping and "source_content_verification_pending"
                        in obligation["properties"]["disposition"].get("enum", [])):
                    for code in permitted:
                        bound_refs = [span["ref_id"] for span in check["source_spans"]
                            if any(fact["code"] == code and span["start"] <= fact["start"]
                                   and span["end"] >= fact["end"] for fact in source_pending_facts)]
                        if not bound_refs:
                            continue
                        registered = copy.deepcopy(obligation)
                        registered["properties"]["pending_work_code"] = {"enum": [code]}
                        registered["properties"]["source_ref"] = {"type": "string", "enum": bound_refs}
                        registered["properties"]["disposition"] = {
                            "enum": ["source_content_verification_pending"]
                        }
                        if "pending_work_code" not in registered["required"]:
                            registered["required"].append("pending_work_code")
                        registered["properties"]["requirement_refs"]["maxItems"] = 0
                        pending_scoped.append(registered)
            compiled_obligation_branches = pending_scoped
            if constrain_requirement_links:
                # Generation-time constraint mirrors the canonical coverage
                # validator. Parsing still preserves invalid raw output for a
                # typed, bounded corrective review, never silently repairs it.
                live_refs = sorted({
                    item["requirement_ref"]
                    for item in review_context.get("linked_requirements", [])
                    if isinstance(item, dict) and isinstance(item.get("requirement_ref"), str)
                    and item["requirement_ref"]
                })
                constrained = []
                for obligation in compiled_obligation_branches:
                    dispositions = obligation["properties"]["disposition"].get("enum", [])
                    if "represented" not in dispositions:
                        constrained.append(obligation)
                        continue
                    other = copy.deepcopy(obligation)
                    other["properties"]["disposition"]["enum"] = [
                        value for value in dispositions if value != "represented"
                    ]
                    if other["properties"]["disposition"]["enum"]:
                        constrained.append(other)
                    if live_refs:
                        represented = copy.deepcopy(obligation)
                        represented["properties"]["disposition"]["enum"] = ["represented"]
                        represented["properties"]["requirement_refs"].update({
                            "minItems": 1, "items": {"type": "string", "enum": live_refs},
                        })
                        constrained.append(represented)
                compiled_obligation_branches = constrained
            if not compiled_obligation_branches:
                raise ValueError(
                    f"coverage check {check.get('check_id')!r} has no permitted obligation schema branch"
                )
            props["identified_obligations"]["items"] = (
                compiled_obligation_branches[0]
                if len(compiled_obligation_branches) == 1
                else {"anyOf": compiled_obligation_branches}
            )
            if constrain_requirement_links and external_mapping:
                # Keep the outer per-check properties stable for consumers;
                # the additional union constrains the whole result, not just
                # individual atoms. Diagnostic verdicts retain unmatched
                # source duties with no invented primary mapping.
                mixed = review_context.get("classification") == "executable_with_external_check"
                branch["anyOf"] = _pending_verdict_generation_branches(
                    branch, compiled_obligation_branches, review_context,
                    pending_verdict="mixed_execution_external_pending" if mixed else "external_compliance_pending",
                    disposition_statuses=({"represented": "covered", "external_action_pending": "unverifiable"}
                        if mixed else {"external_action_pending": "unverifiable"}),
                    excluded_verdicts={"external_compliance_pending", "mixed_execution_external_pending"},
                )
            elif (constrain_requirement_links
                    and review_context.get("classification") == "requires_source_verification"
                    and review_context.get("requires_requirement") is False
                    and not review_context.get("linked_requirements")
                    and review_context.get("source_content_verification_codes")
                        == compile_source_content_verification_codes(check["document_text"])
                    and typed_source_verification_inventory_is_bound(
                        check["document_text"], primary_obligations)):
                verification_atoms = []
                for atom in compiled_obligation_branches:
                    atom = copy.deepcopy(atom)
                    atom["properties"]["disposition"]["enum"] = [
                        value for value in atom["properties"]["disposition"]["enum"]
                        if value != "authoring_content_pending"
                    ]
                    if atom["properties"]["disposition"]["enum"]:
                        atom["required"] = list(dict.fromkeys(atom["required"] + ["primary_obligation_id"]))
                        verification_atoms.append(atom)
                # Source-bound pure provenance cannot authorize missing prose.
                # Keep diagnostics (including unmatched additional duties) open
                # without erasing the existing typed primary inventory.
                diagnostic_atoms = copy.deepcopy(compiled_obligation_branches)
                for atom in diagnostic_atoms:
                    atom["properties"]["disposition"]["enum"] = [
                        value for value in atom["properties"]["disposition"]["enum"]
                        if value != "authoring_content_pending"
                    ]
                diagnostic_atoms = [atom for atom in diagnostic_atoms if atom["properties"]["disposition"]["enum"]]
                props["identified_obligations"]["items"] = {"anyOf": diagnostic_atoms}
                branch["anyOf"] = _pending_verdict_generation_branches(
                    branch, verification_atoms, review_context,
                    pending_verdict="source_content_verification_pending",
                    disposition_statuses={"source_content_verification_pending": "unresolved"},
                    excluded_verdicts={"source_content_pending", "source_content_verification_pending"},
                )
            if constrain_requirement_links:
                # A diagnostic verdict must identify something to diagnose.
                # Keep typed mismatches, author work and mixed omissions
                # reportable: the canonical semantic validator, not this
                # generation shape, decides whether the inventory justifies
                # incomplete. Never project an empty result to consistent.
                alternatives = branch.get("anyOf") or [copy.deepcopy(branch)]
                nonempty_diagnostics = []
                for alternative in alternatives:
                    verdicts = alternative["properties"]["verdict"].get("enum", [])
                    if "incomplete" not in verdicts:
                        continue
                    diagnostic = copy.deepcopy(alternative)
                    diagnostic["properties"]["verdict"] = {"enum": ["incomplete"]}
                    diagnostic["properties"]["identified_obligations"].update({
                        "minItems": 1,
                        "description": (
                            "Incomplete must enumerate at least one exact-source diagnostic obligation. "
                            "No source duty: consistent with an empty inventory, not incomplete. "
                            "A known typed mismatch or authorized pending classification correction "
                            "still needs its explicit source atom; never invent one to satisfy this shape."
                        ),
                    })
                    alternative["properties"]["verdict"] = {
                        "enum": [value for value in verdicts if value != "incomplete"]
                    }
                    nonempty_diagnostics.append(diagnostic)
                branch["anyOf"] = [
                    alternative for alternative in alternatives
                    if alternative["properties"]["verdict"].get("enum")
                ] + nonempty_diagnostics
        branches.append(branch)
    schema["properties"]["results"]["items"] = {"anyOf": branches}
    return schema


def compile_source_reference_response(
    response: Any, request: dict[str, Any], canonical_schema: dict[str, Any], *, coverage: bool,
    provider_nullable_optionals: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve only selections from this immutable request; never repair prose.

    Codex strict structured output represents local optional properties as
    required nullable fields. When ``provider_nullable_optionals`` is true,
    normalize only those schema-optional nulls to omission before canonical
    validation. Keep the parsed provider response hash in the receipt so this
    deterministic projection cannot obscure the original model output.
    """
    packet = build_source_reference_packet(request)
    validation_schema = source_reference_schema(canonical_schema, packet, coverage=coverage)
    provider_response_sha256 = sha256_json(response)
    canonical_input = (
        normalize_native_response(response, validation_schema)
        if provider_nullable_optionals else copy.deepcopy(response)
    )
    errors = validate_instance(canonical_input, validation_schema)
    if errors:
        issues = source_reference_result_issues(canonical_input, validation_schema) if coverage else []
        if issues:
            raise SourceReferenceResponseError(issues, validation_schema)
        raise ValueError("source reference response rejected: " + "; ".join(errors[:8]))
    checks = {check["check_id"]: check for check in packet["checks"]}
    seen: set[str] = set()
    compiled = copy.deepcopy(canonical_input)
    selections = []
    for result in compiled["results"]:
        check_id = result["check_id"]
        if check_id in seen:
            raise ValueError(f"duplicate source reference check: {check_id}")
        seen.add(check_id)
        check = checks[check_id]
        spans = {span["ref_id"]: span for span in check["source_spans"]}
        refs = result.pop("evidence_refs")
        result["evidence_quotes"] = [spans[ref]["text"] for ref in refs]
        selected = list(refs)
        if coverage:
            result["machine_obligation_ids"] = copy.deepcopy(
                check.get("review_context", {}).get("machine_obligation_ids", [])
            )
            obligation_selections = []
            for obligation_index, obligation in enumerate(result["identified_obligations"]):
                ref = obligation.pop("source_ref")
                obligation["source_quote"] = spans[ref]["text"]
                selected.append(ref)
                obligation_selections.append({
                    "obligation_index": obligation_index,
                    "source_ref": ref,
                    "span": copy.deepcopy(spans[ref]),
                })
        else:
            obligation_selections = []
        selections.append({"check_id": check_id, "spans": [
            copy.deepcopy(spans[ref]) for ref in dict.fromkeys(selected)
        ], "obligations": obligation_selections})
    if seen != set(checks):
        raise ValueError("source reference response omitted checks: " + ", ".join(sorted(set(checks) - seen)))
    compilation = {
        "protocol": REFERENCE_PROTOCOL, "run_id": request.get("run_id"),
        "request_sha256": sha256_json(request), "packet_sha256": sha256_json(packet),
        "raw_response_sha256": provider_response_sha256,
        "compiled_response_sha256": sha256_json(compiled), "selections": selections,
        "semantic_verdicts_unchanged": True,
    }
    if provider_nullable_optionals:
        compilation["provider_nullable_normalization"] = {
            "policy": "strict_native_optional_nulls_to_omitted_v1",
            "provider_response_sha256": provider_response_sha256,
            "canonical_input_response_sha256": sha256_json(canonical_input),
        }
    return compiled, compilation


def bind_validated_source_reference_selections(
    compilation: dict[str, Any],
    compiled_response: dict[str, Any],
    validated_response: dict[str, Any],
    request: dict[str, Any],
) -> dict[str, Any]:
    """Bind post-validator obligations without overwriting provider selections.

    ``selections`` remains the exact source-reference compiler output for the
    compiled response. Validators may apply an explicitly registered,
    deterministic projection (for example, collapsing a non-explicit English
    correction claim to one human-review item). ``canonical_selections`` is a
    second, source-bound view for the final response. A changed quote can only
    reuse a span that the original response selected for that same check; if
    the selection cannot be resolved uniquely, this fails closed.
    """
    if not isinstance(compilation, dict) or not isinstance(request, dict):
        raise ValueError("source-reference canonical binding requires object inputs")
    packet = build_source_reference_packet(request)
    if (
        compilation.get("protocol") != REFERENCE_PROTOCOL
        or compilation.get("run_id") != request.get("run_id")
        or compilation.get("request_sha256") != sha256_json(request)
        or compilation.get("packet_sha256") != sha256_json(packet)
        or compilation.get("compiled_response_sha256") != sha256_json(compiled_response)
    ):
        raise ValueError("source-reference canonical binding does not match the compiled request")

    def index_results(response: dict[str, Any], label: str) -> dict[str, dict[str, Any]]:
        items = response.get("results")
        if not isinstance(items, list):
            raise ValueError(f"{label} source-reference response has no results")
        indexed: dict[str, dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("check_id"), str):
                raise ValueError(f"{label} source-reference response has a malformed result")
            check_id = item["check_id"]
            if check_id in indexed:
                raise ValueError(f"{label} source-reference response duplicates {check_id}")
            indexed[check_id] = item
        return indexed

    source_checks = {
        str(item["check_id"]): item
        for item in packet.get("checks", [])
        if isinstance(item, dict) and isinstance(item.get("check_id"), str)
    }
    before = index_results(compiled_response, "compiled")
    after = index_results(validated_response, "validated")
    raw_selections = compilation.get("selections")
    selections = {
        str(item["check_id"]): item
        for item in raw_selections
        if isinstance(item, dict) and isinstance(item.get("check_id"), str)
    } if isinstance(raw_selections, list) else {}
    if set(before) != set(source_checks) or set(after) != set(source_checks) or set(selections) != set(source_checks):
        raise ValueError("source-reference canonical binding does not cover the current checks")
    if len(selections) != len(raw_selections or []):
        raise ValueError("source-reference canonical binding has duplicate selections")

    canonical_selections: list[dict[str, Any]] = []
    changed_check_ids: list[str] = []
    before_counts: dict[str, int] = {}
    after_counts: dict[str, int] = {}
    for check_id in [str(item["check_id"]) for item in packet["checks"]]:
        source_check = source_checks[check_id]
        source_text = source_check.get("document_text")
        span_catalog = {
            span.get("ref_id"): span
            for span in source_check.get("source_spans", [])
            if isinstance(span, dict) and isinstance(span.get("ref_id"), str)
        }
        before_result = before[check_id]
        after_result = after[check_id]
        selection = selections[check_id]
        before_obligations = before_result.get("identified_obligations")
        after_obligations = after_result.get("identified_obligations")
        selected_obligations = selection.get("obligations")
        selected_spans = selection.get("spans")
        if (
            not isinstance(source_text, str)
            or not isinstance(before_obligations, list)
            or not isinstance(after_obligations, list)
            or not isinstance(selected_obligations, list)
            or not isinstance(selected_spans, list)
            or len(before_obligations) != len(selected_obligations)
        ):
            raise ValueError(f"source-reference canonical binding is incomplete for {check_id}")
        for obligation_index, (obligation, selected) in enumerate(zip(before_obligations, selected_obligations)):
            source_ref = selected.get("source_ref") if isinstance(selected, dict) else None
            span = selected.get("span") if isinstance(selected, dict) else None
            if (
                not isinstance(obligation, dict)
                or not isinstance(span, dict)
                or selected.get("obligation_index") != obligation_index
                or span_catalog.get(source_ref) != span
                or obligation.get("source_quote") != span.get("text")
            ):
                raise ValueError(f"pre-validation source selection is invalid for {check_id}")
        for span in selected_spans:
            if not isinstance(span, dict) or span_catalog.get(span.get("ref_id")) != span:
                raise ValueError(f"pre-validation source span is invalid for {check_id}")

        before_counts[check_id] = len(before_obligations)
        after_counts[check_id] = len(after_obligations)
        if before_result != after_result:
            changed_check_ids.append(check_id)

        obligations_unchanged = before_obligations == after_obligations
        canonical_obligation_selections: list[dict[str, Any]] = []
        for obligation_index, obligation in enumerate(after_obligations):
            quote = obligation.get("source_quote") if isinstance(obligation, dict) else None
            if not isinstance(quote, str) or not quote or quote not in source_text:
                raise ValueError(f"validated obligation is not an exact source quote for {check_id}")
            if obligations_unchanged:
                source_ref = selected_obligations[obligation_index]["source_ref"]
            else:
                matching_obligation_refs = {
                    selected.get("source_ref")
                    for selected in selected_obligations
                    if isinstance(selected, dict)
                    and isinstance(selected.get("span"), dict)
                    and selected["span"].get("text") == quote
                }
                if len(matching_obligation_refs) != 1:
                    matching_span_refs = {
                        span.get("ref_id") for span in selected_spans
                        if isinstance(span, dict) and span.get("text") == quote
                    }
                    matching_obligation_refs = matching_span_refs
                if len(matching_obligation_refs) != 1:
                    raise ValueError(
                        f"validated obligation source selection is not uniquely bound for {check_id}"
                    )
                source_ref = next(iter(matching_obligation_refs))
            span = span_catalog.get(source_ref)
            if not isinstance(span, dict) or span.get("text") != quote:
                raise ValueError(f"validated obligation source span is stale for {check_id}")
            canonical_obligation_selections.append({
                "obligation_index": obligation_index,
                "source_ref": source_ref,
                "span": copy.deepcopy(span),
            })
        canonical_selections.append({
            "check_id": check_id,
            "spans": copy.deepcopy(selected_spans),
            "obligations": canonical_obligation_selections,
        })

    canonical = copy.deepcopy(compilation)
    canonical["canonicalization_protocol"] = "validated_source_reference_projection_v1"
    canonical["canonical_response_sha256"] = sha256_json(validated_response)
    canonical["canonical_selections"] = canonical_selections
    canonical["validation_projection"] = {
        "protocol": "validated_source_reference_projection_v1",
        "pre_validation_response_sha256": sha256_json(compiled_response),
        "canonical_response_sha256": sha256_json(validated_response),
        "changed_check_ids": sorted(changed_check_ids),
        "pre_validation_obligation_counts": before_counts,
        "canonical_obligation_counts": after_counts,
    }
    return canonical
