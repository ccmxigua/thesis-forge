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


REFERENCE_PROTOCOL = "semantic_source_references_v1"


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
        # Keep all whitespace and punctuation. These are exact ranges, not
        # normalized matches against some other clause or evidence paragraph.
        ranges = [(0, len(text))]
        ranges.extend(
            (match.start(), match.end())
            for match in re.finditer(r"[^。！？；\n]+[。！？；]?", text)
            if match.group().strip()
        )
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
            for obligation in result["identified_obligations"]:
                ref = obligation.pop("source_ref")
                obligation["source_quote"] = spans[ref]["text"]
                selected.append(ref)
        selections.append({"check_id": check_id, "spans": [
            copy.deepcopy(spans[ref]) for ref in dict.fromkeys(selected)
        ]})
    if seen != set(checks):
        raise ValueError("source reference response omitted checks: " + ", ".join(sorted(set(checks) - seen)))
    return compiled, {
        "protocol": REFERENCE_PROTOCOL, "run_id": request.get("run_id"),
        "request_sha256": sha256_json(request), "packet_sha256": sha256_json(packet),
        "raw_response_sha256": sha256_json(response),
        "compiled_response_sha256": sha256_json(compiled), "selections": selections,
        "semantic_verdicts_unchanged": True,
    }
