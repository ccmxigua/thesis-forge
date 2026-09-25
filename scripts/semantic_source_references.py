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
from semantic_contract import sha256_json


REFERENCE_PROTOCOL = "semantic_source_references_v2"

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


def source_reference_schema(
    canonical_schema: dict[str, Any], packet: dict[str, Any], *, coverage: bool,
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
            obligation = props["identified_obligations"]["items"]
            obligation["properties"].pop("source_quote")
            obligation["properties"]["source_ref"] = copy.deepcopy(refs)
            obligation["required"] = ["source_ref" if key == "source_quote" else key
                                      for key in obligation["required"]]
        branches.append(branch)
    schema["properties"]["results"]["items"] = {"anyOf": branches}
    return schema


def compile_source_reference_response(
    response: Any, request: dict[str, Any], canonical_schema: dict[str, Any], *, coverage: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve only selections from this immutable request; never repair prose."""
    packet = build_source_reference_packet(request)
    errors = validate_instance(response, source_reference_schema(canonical_schema, packet, coverage=coverage))
    if errors:
        raise ValueError("source reference response rejected: " + "; ".join(errors[:8]))
    checks = {check["check_id"]: check for check in packet["checks"]}
    seen: set[str] = set()
    compiled = copy.deepcopy(response)
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
    return compiled, {
        "protocol": REFERENCE_PROTOCOL, "run_id": request.get("run_id"),
        "request_sha256": sha256_json(request), "packet_sha256": sha256_json(packet),
        "raw_response_sha256": sha256_json(response),
        "compiled_response_sha256": sha256_json(compiled), "selections": selections,
        "semantic_verdicts_unchanged": True,
    }
