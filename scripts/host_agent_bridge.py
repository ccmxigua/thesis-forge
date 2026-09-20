#!/usr/bin/env python3
"""Run the declared native Host Agent over a fresh semantic-review packet.

This is the optional host-runtime adapter for the otherwise provider-neutral
pipeline.  It invokes only the explicitly declared native host CLI (currently
OpenClaw or Codex) instead of owning an API client or an API key.  Every chunk
receives a fresh isolated turn, and the response is captured without delivery
to an external channel.  The existing offline merger remains the only
authority that can accept the response.
"""
from __future__ import annotations

import argparse
import copy
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from artifact_io import atomic_write_text

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
PARENT_SESSION_ENV_NAMES = (
    "OPENCLAW_PARENT_SESSION_KEY",
    "OPENCLAW_SESSION_KEY",
)


class HostAgentRouteMismatch(RuntimeError):
    """Raised when a child did not execute on the run's route snapshot."""


class HostAgentProvenanceMismatch(RuntimeError):
    """Raised when the raw response does not echo the exact chunk identity."""


class HostAgentCancelled(RuntimeError):
    """Raised when a bridge run is stopped before a response is accepted."""


class RunController:
    """Coordinate bounded cancellation without touching unrelated processes."""

    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        self._processes: dict[int, subprocess.Popen[str]] = {}
        self.reason: str | None = None

    def request_stop(self, reason: str) -> None:
        self.reason = reason
        self.stop_event.set()

    def check(self) -> None:
        if self.stop_event.is_set():
            raise HostAgentCancelled(
                self.reason or "Host Agent run was cancelled before acceptance")

    def register(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            self._processes[process.pid] = process
        if self.stop_event.is_set():
            self._terminate(process)

    def unregister(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            self._processes.pop(process.pid, None)

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        else:
            process.terminate()

    def terminate_all(self) -> None:
        with self._lock:
            processes = list(self._processes.values())
        for process in processes:
            self._terminate(process)


def _terminate_and_reap(process: subprocess.Popen[str]) -> tuple[str, str]:
    """Stop one owned process group and close its pipes before returning."""
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    else:
        process.terminate()
    try:
        return process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        return process.communicate()


def _run_command(
    command: list[str],
    *,
    timeout: int,
    controller: RunController | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a child in its own process group and reap descendants on timeout.

    A host CLI can spawn a child that inherits the captured stdout/stderr
    pipes.  Killing only the CLI parent leaves those
    pipes open and can make ``subprocess.run`` hang forever after its timeout.
    Keeping the process group explicit lets the bridge fail closed and return
    a deterministic timeout to the retry loop.
    """
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=(os.name == "posix"),
    )
    if controller is not None:
        controller.register(process)
    try:
        if controller is None:
            stdout, stderr = process.communicate(timeout=timeout)
        else:
            deadline = time.monotonic() + timeout
            while True:
                controller.check()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    stdout, stderr = process.communicate(
                        timeout=min(0.5, remaining)
                    )
                    break
                except subprocess.TimeoutExpired:
                    continue
    except HostAgentCancelled:
        _terminate_and_reap(process)
        raise
    except subprocess.TimeoutExpired as exc:
        stdout, stderr = _terminate_and_reap(process)
        raise subprocess.TimeoutExpired(
            command, timeout, output=stdout, stderr=stderr,
        ) from exc
    finally:
        if controller is not None:
            controller.unregister(process)
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from host_adapters import codex as codex_adapter  # noqa: E402
from host_adapters import openclaw as openclaw_adapter  # noqa: E402
from host_review_contract import (  # noqa: E402
    HOST_REVIEW_CONTRACT_V2,
    HOST_REVIEW_CONTRACT_V3,
    SUPPORTED_HOST_REVIEW_CONTRACTS,
    contract_error_records,
    provenance_error_records,
    _response_sha256,
    summarize_contract_errors as _shared_summarize_contract_errors,
    validate_response as _shared_validate_response,
)
from host_review_schema import (  # noqa: E402
    native_output_schema,
    normalize_native_response,
    require_native_schema,
)
from host_runtime import (  # noqa: E402
    HostAdapterUnavailable,
    HostRuntimeError,
    automatic_adapter_id,
    require_host_runtime,
    require_parent_session,
)
from requirements_engine import (  # noqa: E402
    merge_host_agent_review_packets,
    validate_host_review_chunk_source_projection,
)
from semantic_contract import (  # noqa: E402
    sha256_file,
    strict_json_loads,
    validate_response_provenance,
)


def _read_json(path: Path, *, label: str) -> Any:
    try:
        return strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {label}: {path}: {exc}") from exc


def _bound_path(root: Path, value: str | Path, *, label: str) -> Path:
    """Resolve a manifest/output path while forbidding directory escape."""
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ValueError(f"{label} must be a non-empty path")
    root = root.resolve()
    candidate = Path(value)
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if resolved == root or root not in resolved.parents:
        raise ValueError(f"{label} escapes its approved run directory: {value!r}")
    return resolved


def _write_json(path: Path, value: Any) -> None:
    atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
    )


def compact_model_packet(chunk: dict[str, Any]) -> dict[str, Any]:
    """Keep the current chunk's semantic data while dropping redundant evidence.

    The on-disk chunk remains the exact provenance source.  The model-facing
    view keeps every clause and cited evidence occurrence, but omits repeated
    rich DOCX run payloads.  The machine-readable contract is intentionally
    preserved: without the role schemas and response sub-schemas the model is
    forced to guess field names and the final merger receives a response that
    is structurally plausible but not executable.
    """
    evidence_context = chunk.get("evidence_context")
    compact_evidence: dict[str, Any] = {}
    if isinstance(evidence_context, dict):
        for evidence_id, item in evidence_context.items():
            if not isinstance(item, dict):
                continue
            compact_evidence[str(evidence_id)] = {
                key: item[key]
                for key in (
                    "id", "kind", "text", "style_id", "style_name", "location",
                )
                if key in item
            }
    contract = chunk.get("requirement_contract")
    role_properties = contract.get("role_properties_schema", {}) if isinstance(contract, dict) else {}
    compact_contract = {}
    if isinstance(contract, dict):
        compact_contract = {
            "allowed_roles": copy.deepcopy(contract.get("allowed_roles", [])),
            "role_properties_schema": copy.deepcopy(
                contract.get("role_properties_schema", {})
            ),
            "$defs": copy.deepcopy(contract.get("$defs", {})),
        }
    response_schema = chunk.get("response_schema")
    rule_spec = chunk.get("rule_spec")
    existing_requirements = (
        rule_spec.get("requirements", [])
        if isinstance(rule_spec, dict) and isinstance(rule_spec.get("requirements"), list)
        else []
    )
    structure = chunk.get("document_structure")
    compact_structure: dict[str, Any] = {}
    if isinstance(structure, dict):
        compact_structure["section_count"] = structure.get("section_count")
        compact_structure["page_fields"] = structure.get("page_fields", [])
        compact_structure["style_names"] = structure.get("style_names", {})
        sections = structure.get("sections")
        if isinstance(sections, list):
            compact_structure["sections"] = []
            for section in sections:
                if not isinstance(section, dict):
                    continue
                first = section.get("first_paragraphs", [])
                last = section.get("last_paragraphs", [])
                compact_structure["sections"].append({
                    "section_index": section.get("section_index"),
                    "paragraph_count": section.get("paragraph_count"),
                    "first_texts": [item.get("text", "") for item in first[:3]
                                    if isinstance(item, dict)],
                    "last_texts": [item.get("text", "") for item in last[-2:]
                                   if isinstance(item, dict)],
                    "page_number_format": section.get("page_number_format"),
                    "page_number_start": section.get("page_number_start"),
                    "footer_references": section.get("footer_references", []),
                })
    return {
        "contract_version": chunk.get("contract_version"),
        "task": chunk.get("task"),
        "instructions": chunk.get("instructions", []),
        "batch": chunk.get("batch"),
        "clauses": chunk.get("clauses", []),
        "source_continuity_context": copy.deepcopy(
            chunk.get("source_continuity_context", {})
        ),
        "runtime_context": _compact_runtime_context(chunk.get("runtime_context")),
        "declaration_anchor_candidates": copy.deepcopy(
            chunk.get("declaration_anchor_candidates", [])
        ),
        "declaration_anchor_preference": chunk.get(
            "declaration_anchor_preference"
        ),
        "evidence_context": compact_evidence,
        "document_structure": compact_structure,
        "page_evidence": chunk.get("page_evidence", {}),
        "allowed_roles": contract.get("allowed_roles", []) if isinstance(contract, dict) else [],
        "requirement_contract": compact_contract,
        "response_schema": copy.deepcopy(response_schema) if isinstance(response_schema, dict) else {},
        "declarations_schema": role_properties.get("declarations") if isinstance(role_properties, dict) else None,
        "rule_spec_advisory": {
            key: rule_spec[key]
            for key in ("roles", "page", "requirements")
            if isinstance(rule_spec, dict) and key in rule_spec
        },
        "eligible_existing_requirements": existing_requirements,
        "response_contract": {
            "required": [
                "contract_version", "requirements", "clause_reviews",
                "unsupported_items", "reported_conflicts",
            ],
            "allowed_classifications": [
                "covered", "executable", "external_compliance", "ignored", "informational",
                "not_applicable", "requires_metadata", "requires_source_content", "unresolved",
                "unsupported", "unsupported_backend", "unverifiable", "verify_existing",
            ],
        },
    }


def _compact_runtime_context(value: Any) -> dict[str, Any] | None:
    """Expose bounded semantic inputs without giving the model identity fields."""
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    profile = value.get("confirmed_thesis_profile")
    if isinstance(profile, dict):
        result["confirmed_thesis_profile"] = {
            key: copy.deepcopy(profile[key])
            for key in ("schema_version", "profile_id", "degree_category", "security_level", "cover_metadata")
            if key in profile
        }
        result["confirmed_thesis_profile_status"] = "confirmed_source_bound"
    inventory = value.get("runtime_inventory")
    if isinstance(inventory, dict):
        result["runtime_inventory"] = {
            key: copy.deepcopy(inventory[key])
            for key in ("status", "declaration_anchor_status", "anchor_inventory")
            if key in inventory
        }
    if isinstance(value.get("case_id"), str):
        result["case_id"] = value["case_id"]
    result["policy"] = "read_only_semantic_inputs; trusted hashes and provenance omitted"
    return result


def _summarize_contract_errors(errors: list[str], *, limit: int = 12) -> str:
    return _shared_summarize_contract_errors(errors, limit=limit)


_BASE_CONTRACT_REPAIR_RULES = (
    "Never add unknown properties or invent role names; emit only fields and nested properties present in response_schema and requirement_contract.",
    "classification and normative_basis are different fields: classification is a review status; normative_basis must be one of the declared enum values and must never be the word informational.",
    "If classification is informational, use requirement_indexes: [] and omit normative_basis unless a declared normative basis is explicitly supported by the cited evidence; never copy classification into normative_basis.",
    "Only covered, executable, and verify_existing clause reviews may contain requirement_indexes; every other classification must use an empty array.",
    "Every executable/covered/verify_existing requirement reference must be a zero-based index of a semantically matching emitted requirement whose clause_ids contains that exact review clause_id; check every review/index pair independently. Never carry an adjacent clause's index, change clause_ids to make validation pass, or emit an unused requirement.",
    "Do not move nested properties to a top-level requirement role: a nested key such as require_after_role is legal only where the supplied role schema places it.",
    "Every emitted requirement must contain at least one non-null property in its role-specific properties object. A field_key identifies a content instance but is not an executable payload; do not emit properties: {} or use field_key alone. For text and cover-field roles, copy the exact evidence-backed text into properties.text; for style/layout roles, emit the declared nested style or layout property.",
    "Do not fabricate evidence or guess a semantic classification. Make only the mechanical schema corrections required by the supplied error, then regenerate the complete response from the current chunk.",
    "Administrative approval/marking tables belong under cover.non_public_administration, must be conditional on thesis_profile.security_level with an equals or in condition selecting restricted/classified theses, and must be blank for public theses. Do not use not_equals as the executable binding. Bind approval-number and approval-date labels to approval_number and approval_date; never substitute classification_number or completion_date.",
    "Input prerequisite keys are namespace-bound by kind: metadata uses thesis_profile., source_content uses source_inventory., template_resource uses template_profile., and runtime uses runtime.; never emit runtime_context.* or invent an unregistered path.",
    "A clause can be executable only when every independently verifiable obligation is represented. Preserve language targets, units, limits, exceptions, and prohibited-content requirements; a partial requirement must be classified non-executable with requirement_indexes: [] rather than promoted to full coverage.",
    "Use only a verified runtime_context.runtime_inventory anchor. A zero-match, multi-match, or blocked anchor is not executable; never infer a nearby heading or use the declarations role as an insertion anchor.",
)


def _contract_repair_guidance(
    error_text: str,
    *,
    include_base: bool = True,
) -> str:
    """Return deterministic, narrow repair instructions for a rejected reply.

    A raw validator error is useful to a human but is too easy for a model to
    misread as permission to coerce the payload.  These rules describe the
    contract boundary without suggesting a semantic value or silently
    changing the rejected response.
    """
    text = str(error_text or "").lower()
    raw_text = str(error_text or "")
    rules = list(_BASE_CONTRACT_REPAIR_RULES) if include_base else []
    targeted: list[str] = []
    clause_match = re.search(r"\$\.clause_reviews\[(\d+)\]\.normative_basis", text)
    if clause_match:
        clause_index = clause_match.group(1)
        targeted.append(
            f"At clause_reviews[{clause_index}], remove the entire normative_basis property if the value is informational or otherwise not supported by the cited evidence; never replace it with another guessed value. Keep that review's classification unchanged unless the current chunk evidence independently requires a different semantic classification."
        )
    elif "normative_basis" in text or "informational" in text:
        targeted.append(
            "For every clause_review, scan normative_basis separately from classification. Remove normative_basis when no declared evidence basis is supported; never replace it with another guessed value, and never use informational or any classification string as its value."
        )
    if "requirement_index" in text or "nonexecutable" in text:
        targeted.append(
            "Re-check every clause_review classification against its requirement_indexes before returning; non-executable reviews must have [] even when the rejected response had an index."
        )
    backed_matches = list(re.finditer(
        r"\$\.clause_reviews\[(?P<review>\d+)\]: "
        r"requirement_index_not_backed_by_clause:"
        r"clause_id=(?P<clause>[^:;]+):"
        r"requirement_index=(?P<bad>\d+):"
        r"matching_indexes=\[(?P<matching>[^\]]*)\]",
        raw_text,
    ))
    for match in backed_matches[:4]:
        matching = match.group("matching").strip() or "none"
        targeted.append(
            f"At clause_reviews[{match.group('review')}] for clause_id "
            f"{match.group('clause')!r}, remove requirement index "
            f"{match.group('bad')} because requirements[{match.group('bad')}].clause_ids "
            f"does not contain that exact clause. The matching requirement indexes "
            f"are [{matching}]; keep only those indexes "
            "that semantically support this review. Do not copy a neighboring "
            "clause's index or edit clause_ids just to satisfy the validator."
        )
    if "requirement_index_not_backed_by_clause" in text and not backed_matches:
        targeted.append(
            "For each covered/executable/verify_existing review, check every referenced "
            "index against requirements[index].clause_ids and keep an index only when "
            "that exact review clause_id is present. Do not copy an adjacent clause's "
            "index or change clause_ids; if no matching requirement exists, regenerate "
            "the complete requirement mapping from the current chunk."
        )
    if "require_after_role" in text or "unknown_or_disallowed_role" in text or "additionalproperties" in text:
        targeted.append(
            "Re-read the exact role_properties_schema for each requirement and place each property only at its declared nesting level; do not create a keywords_zh/keywords_en role when the schema expects content_constraints.properties.keywords_zh/keywords_en."
        )
    if "input_prerequisites" in text or "runtime_context" in text:
        targeted.append(
            "For each input_prerequisite, use the namespace required by its kind: thesis_profile. for metadata, source_inventory. for source content, template_profile. for template resources, and runtime. for runtime services. Replace runtime_context.* with the registered runtime.* key; do not invent or remap a missing input."
        )
    if "non_public_administration" in text:
        targeted.append(
            "Keep the administrative region under cover.non_public_administration and bind applicability to thesis_profile.security_level with operator equals or in selecting restricted/classified. Do not use not_equals, move the fields to ordinary cover.fields, or guess approval values."
        )
    if "must_include_semantic_payload" in text or "empty_requirement_properties" in text:
        targeted.append(
            "Every requirement must have a non-empty role-specific properties object. A field_key alone is only an identity and cannot replace the payload. For a text or cover-field requirement, copy the exact evidence-backed text into properties.text; for a style/layout requirement, include the declared nested property. Do not invent a value or silently drop the requirement."
        )
    if "before_role" in text or "declarations" in text and "enum" in text:
        targeted.append(
            "For a declarations requirement, before_role must equal the supplied declaration_anchor_preference and must be one of the supplied declaration_anchor_candidates. The word declarations is a requirement role, not an insertion anchor; do not substitute another role or invent an anchor."
        )
    if "signature" in text or "author-name" in text or "author name" in text or "date" in text:
        targeted.append(
            "Do not emit a declarations requirement consisting only of generic author/name/date/signature/location lines. Reclassify those clauses as requires_metadata, external_compliance, or unresolved with requirement_indexes: [] unless the current evidence explicitly proves they are fixed declaration text completing an identified declaration."
        )
    if "complete cited source" in text or "shorten a source paragraph" in text or "paraphrase" in text:
        targeted.append(
            "Every declaration heading and body_parts entry must equal a complete cited source-evidence text. Do not paraphrase or shorten fixed prose; omit heading for a continuation and preserve the complete source paragraph in body_parts."
        )
    unknown_property = re.search(r"unknown property ['\"]([^'\"]+)['\"]", text)
    if unknown_property:
        targeted.append(
            f"Delete only the unknown property {unknown_property.group(1)!r} from the indicated object; do not rename it, move it to another object, or invent a replacement field."
        )
    if "not valid json" in text or "jsondecodeerror" in text:
        targeted.append(
            "Return raw JSON only: no Markdown fences, comments, trailing commas, duplicate keys, or explanatory text; parse the complete object before sending it."
        )
    if "partial_clause_coverage" in text or "abstract_target_or_translation_ambiguous" in text:
        targeted.append(
            "The rejected clause contains residual abstract obligations or an ambiguous language target. "
            "Do not add a guessed min/max/semantic property or change Chinese abstract to English abstract; "
            "return unresolved or another non-executable classification with requirement_indexes: [] until "
            "the cited evidence resolves the target, unit, exception, and all independent obligations."
        )
    if "item_length_metric:cjk_characters" in text:
        targeted.append(
            "Preserve the source wording 'Chinese characters' as the explicit cjk_characters metric. "
            "Do not convert it into an English-word or English-letter limit, and do not truncate keywords."
        )
    if targeted:
        rules.extend(targeted)
    return "\n".join(f"- {rule}" for rule in rules) or "- Re-read the current chunk contract and regenerate the complete JSON object."


def _structured_contract_repair_guidance(
    records: list[dict[str, Any]], *, contract_version: str,
) -> str:
    """Build retry guidance from structured validator facts, not error parsing."""
    lines: list[str] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        code = str(record.get("code") or "contract_validation_error")
        pointer = str(record.get("json_pointer") or "the indicated field")
        matching = record.get("matching_requirement_indexes")
        if code == "normative_basis_invalid":
            rule = (
                f"At {pointer}, omit the invalid normative_basis field rather than replacing it with a guessed value; "
                "keep classification and cited evidence unchanged unless the current evidence independently requires a semantic re-review."
            )
        elif code == "requirement_relation_mismatch":
            rule = (
                f"At {pointer}, do not copy or repair a neighboring relation. The deterministic matching requirement indexes are "
                f"{matching if isinstance(matching, list) else 'unknown'}; regenerate the complete relation from the current chunk."
            )
        elif code == "partial_clause_coverage":
            rule = (
                f"At {pointer}, preserve every obligation and do not promote partial coverage. "
                "Use a non-executable classification when the supplied evidence does not resolve all obligations."
            )
        elif code == "unknown_property":
            rule = (
                f"At {pointer}, remove only the unsupported property named by the schema error; "
                "do not move it, rename it, or invent a replacement."
            )
        elif code == "empty_requirement_properties":
            rule = (
                f"At {pointer}, emit a non-empty role-specific properties object. "
                "field_key alone is not an executable payload; copy exact evidence-backed text into properties.text or emit the declared style/layout property, without guessing."
            )
        elif code == "fixed_text_evidence_mismatch":
            rule = (
                f"At {pointer}, replace only the fixed declaration text with the complete, exact cited evidence paragraph. "
                "Do not split, paraphrase, shorten, or alter classifications, relations, anchors, or unrelated requirements."
            )
        elif code == "input_prerequisite_namespace":
            rule = (
                f"At {pointer}, use only the registered namespace for the prerequisite kind: "
                "thesis_profile. for metadata, source_inventory. for source content, "
                "template_profile. for template resources, and runtime. for runtime services. "
                "Do not emit runtime_context.* or invent a replacement path."
            )
        elif code == "cover_binding_violation":
            rule = (
                f"At {pointer}, keep non-public administration under cover.non_public_administration "
                "and bind it to thesis_profile.security_level with equals or in selecting restricted/classified; "
                "do not use not_equals or move the fields to ordinary cover.fields."
            )
        elif code == "schema_contract_violation":
            rule = (
                f"At {pointer}, conform to the supplied response_schema and regenerate the complete object; "
                "do not change unrelated semantic fields."
            )
        else:
            rule = (
                f"At {pointer}, resolve validator code {code} using only the supplied schema and evidence; "
                "do not guess or reuse a prior response."
            )
        if contract_version == HOST_REVIEW_CONTRACT_V3 and "requirement indexes" in rule:
            rule = rule.replace("requirement indexes", "code-derived reverse relation")
        if rule not in seen:
            seen.add(rule)
            lines.append(f"- {rule}")
    return "\n".join(lines) or "- Re-read the current chunk contract and regenerate the complete JSON object."


def _semantic_retry_view(response: Any) -> dict[str, Any] | None:
    """Project the fields whose change is semantic rather than diagnostic."""
    if not isinstance(response, dict):
        return None
    requirements: list[dict[str, Any]] = []
    for item in response.get("requirements", []) if isinstance(response.get("requirements"), list) else []:
        if not isinstance(item, dict):
            requirements.append({"invalid": item})
            continue
        requirements.append({
            key: copy.deepcopy(item.get(key))
            for key in (
                "role", "properties", "clause_ids", "evidence_ids", "existing_requirement_id",
                "applicability", "input_prerequisites", "verification",
            )
            if key in item
        })
    # Sort by identity fields that must remain stable across a mechanical
    # retry.  Sorting by the complete requirement made a legal property-only
    # repair look like a list-wide semantic rewrite when the repaired
    # properties changed the lexical order.
    requirements.sort(key=lambda item: json.dumps({
        key: item.get(key)
        for key in (
            "role", "clause_ids", "evidence_ids", "existing_requirement_id",
        )
        if key in item
    }, ensure_ascii=False, sort_keys=True))
    reviews: list[dict[str, Any]] = []
    for item in response.get("clause_reviews", []) if isinstance(response.get("clause_reviews"), list) else []:
        if not isinstance(item, dict):
            reviews.append({"invalid": item})
            continue
        reviews.append({
            key: copy.deepcopy(item.get(key))
            for key in (
                "clause_id", "classification", "normative_basis", "obligations",
                "requirement_indexes",
            )
            if key in item
        })
    reviews.sort(key=lambda item: str(item.get("clause_id") or ""))
    return {
        "contract_version": response.get("contract_version"),
        "requirements": requirements,
        "clause_reviews": reviews,
        "unsupported_items": sorted(response.get("unsupported_items") or [])
        if isinstance(response.get("unsupported_items"), list) else response.get("unsupported_items"),
    }


def _retry_change_paths(previous: Any, current: Any) -> list[str]:
    before = _semantic_retry_view(previous)
    after = _semantic_retry_view(current)
    if before is None or after is None:
        return ["$"]
    if before == after:
        return []
    changed: list[str] = []

    def visit(left: Any, right: Any, path: str) -> None:
        if type(left) is not type(right):
            changed.append(path)
            return
        if isinstance(left, dict):
            for key in sorted(set(left) | set(right)):
                if key not in left or key not in right:
                    changed.append(f"{path}.{key}")
                else:
                    visit(left[key], right[key], f"{path}.{key}")
            return
        if isinstance(left, list):
            if len(left) != len(right):
                changed.append(path)
                return
            for index, (item_left, item_right) in enumerate(zip(left, right)):
                visit(item_left, item_right, f"{path}[{index}]")
            return
        if left != right:
            changed.append(path)

    visit(before, after, "$")
    return changed


def _retry_changes_allowed(
    records: list[dict[str, Any]], changed_paths: list[str], *, contract_version: str,
    previous_response: Any = None, current_response: Any = None,
) -> bool:
    """Allow only explicitly mechanical contract corrections on a retry."""
    if not changed_paths:
        return True
    codes = {str(item.get("code")) for item in records if isinstance(item, dict)}
    empty_payload_repair = "empty_requirement_properties" in codes
    previous_view = _semantic_retry_view(previous_response) if empty_payload_repair else None
    current_view = _semantic_retry_view(current_response) if empty_payload_repair else None
    previous_requirements = (
        previous_view.get("requirements", [])
        if previous_view is not None
        else []
    )
    current_requirements = (
        current_view.get("requirements", [])
        if current_view is not None
        else []
    )
    cover_property_prefixes: set[str] = set()
    exact_property_prefixes: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        code = str(record.get("code") or "")
        pointer = str(record.get("json_pointer") or "")
        if code == "cover_binding_violation":
            match = re.match(r"^(\$\.requirements\[\d+\]\.properties)\.([^.\[]+)", pointer)
            if match:
                root = match.group(1)
                field = match.group(2)
                cover_property_prefixes.add(f"{root}.{field}")
                # Administrative bindings may require moving a field between
                # the ordinary fields array and the dedicated conditional
                # region.  Permit only those two declared property roots.
                if field == "fields":
                    cover_property_prefixes.add(f"{root}.non_public_administration")
        elif code == "contract_validation_error":
            match = re.match(r"^(\$\.requirements\[\d+\]\.properties)\.([^.\[]+)", pointer)
            if match:
                exact_property_prefixes.add(f"{match.group(1)}.{match.group(2)}")

    def under(prefix: str, path: str) -> bool:
        return path == prefix or path.startswith(prefix + ".") or path.startswith(prefix + "[")

    for path in changed_paths:
        if path.endswith(".normative_basis") and "normative_basis_invalid" in codes:
            continue
        if path.endswith(".verification") and "schema_contract_violation" in codes:
            if any("verification" in str(item.get("json_pointer") or "") for item in records):
                continue
        if path.endswith(".requirement_indexes") and contract_version == HOST_REVIEW_CONTRACT_V2:
            if "requirement_relation_mismatch" in codes:
                continue
        if empty_payload_repair and ".properties" in path:
            match = re.match(r"\$\.requirements\[(\d+)\]\.properties(?:\.|$)", path)
            if match:
                index = int(match.group(1))
                before = (
                    previous_requirements[index].get("properties")
                    if index < len(previous_requirements)
                    else None
                )
                after = (
                    current_requirements[index].get("properties")
                    if index < len(current_requirements)
                    else None
                )
                if before == {} and isinstance(after, dict) and after:
                    continue
        if "fixed_text_evidence_mismatch" in codes and (
            ".body_parts" in path or ".heading" in path
        ):
            continue
        if "cover_binding_violation" in codes and any(
            under(prefix, path) for prefix in cover_property_prefixes
        ):
            continue
        if "contract_validation_error" in codes and any(
            under(prefix, path) for prefix in exact_property_prefixes
        ):
            continue
        # Semantic fields, requirement properties, obligations, and
        # classifications are never silently changed by a mechanical retry.
        return False
    return True


def _retry_semantic_change_error(
    previous_response: Any,
    current_response: Any,
    records: list[dict[str, Any]],
    *,
    contract_version: str,
) -> tuple[ValueError | None, list[str]]:
    """Reject semantic drift even when the retry response is still invalid.

    The first implementation compared attempts only after the retry had
    passed local validation.  That left an invalid second response free to
    change classifications, bindings, or cover conditions before its final
    contract error was recorded.  Raw-attempt comparison must happen before
    another retry decision, while still allowing the narrow empty-payload
    completion rule handled by ``_retry_changes_allowed``.
    """
    changed_paths = _retry_change_paths(previous_response, current_response)
    if not changed_paths or _retry_changes_allowed(
        records,
        changed_paths,
        contract_version=contract_version,
        previous_response=previous_response,
        current_response=current_response,
    ):
        return None, changed_paths
    error = ValueError(
        "retry changed semantic fields and requires explicit semantic re-review: "
        + ", ".join(changed_paths[:12])
    )
    error.error_records = [{  # type: ignore[attr-defined]
        "code": "semantic_retry_change",
        "json_pointer": path,
        "schema_pointer": path,
        "clause_id": None,
        "raw_error": str(error),
        "response_sha256": _response_sha256(current_response),
        "allowed_values": None,
        "matching_requirement_indexes": None,
        "requirement_count": (
            len(current_response.get("requirements", []))
            if isinstance(current_response, dict)
            and isinstance(current_response.get("requirements"), list)
            else None
        ),
        "semantic_review_required": True,
    } for path in changed_paths]
    return error, changed_paths


# Compatibility name for callers that imported the bridge directly.  The
# shared host-independent implementation is now authoritative.
validate_host_agent_response = _shared_validate_response

# Compatibility name; the OpenClaw envelope implementation lives in its
# explicit adapter module rather than in the generic bridge.
parse_openclaw_result = openclaw_adapter.parse_result


def _split_model_route(model_ref: str) -> tuple[str, str]:
    """Require the explicit provider/model form used for route pinning."""
    value = str(model_ref or "").strip()
    if "/" not in value:
        raise ValueError(
            "Host Agent route must be an explicit provider/model pair; "
            f"got {value!r}"
        )
    provider, model = value.split("/", 1)
    provider = provider.strip()
    model = model.strip()
    if not provider or not model:
        raise ValueError(f"Host Agent route is incomplete: {value!r}")
    return provider, model


def _first_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _actual_route_from_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    """Read the winner route from either ``agent`` or ``agent exec`` output."""
    result = envelope.get("result") if isinstance(envelope.get("result"), dict) else {}
    meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    agent_meta = meta.get("agentMeta") if isinstance(meta.get("agentMeta"), dict) else {}
    trace = result.get("executionTrace") if isinstance(result.get("executionTrace"), dict) else {}

    provider = _first_string(
        envelope.get("provider"),
        agent_meta.get("provider"),
        trace.get("winnerProvider"),
        trace.get("provider"),
    )
    model = _first_string(
        envelope.get("model"),
        agent_meta.get("model"),
        trace.get("winnerModel"),
        trace.get("model"),
    )
    fallback_used = any(
        value is True
        for value in (
            envelope.get("fallbackUsed"),
            result.get("fallbackUsed"),
            trace.get("fallbackUsed"),
            trace.get("fallbackOccurred"),
        )
    )
    return {
        "provider": provider,
        "model": model,
        "route": f"{provider}/{model}" if provider and model else None,
        "fallback_used": fallback_used,
    }


def verify_host_agent_route(
    envelope: dict[str, Any], expected_model: str | None,
) -> dict[str, Any]:
    """Fail closed unless the child winner matches the run route snapshot."""
    actual = _actual_route_from_envelope(envelope)
    if not expected_model:
        return actual
    expected_provider, expected_model_id = _split_model_route(expected_model)
    expected_route = f"{expected_provider}/{expected_model_id}"
    if actual["route"] != expected_route:
        raise HostAgentRouteMismatch(
            "Host Agent child route mismatch: "
            f"expected {expected_route}, got {actual['route'] or 'unknown'}"
        )
    if actual["fallback_used"]:
        raise HostAgentRouteMismatch(
            f"Host Agent child used a fallback while pinned to {expected_route}"
        )
    return actual


def _route_audit_fields(
    expected_route: str | None,
    chunk_audits: list[dict[str, Any]],
) -> dict[str, Any]:
    """Separate expected route fields from observed child route evidence."""
    expected_provider = expected_model = None
    if expected_route:
        expected_provider, expected_model = _split_model_route(expected_route)
    observed_routes = sorted({
        str(item.get("actual_route"))
        for item in chunk_audits
        if isinstance(item.get("actual_route"), str) and item.get("actual_route")
    })
    observed_route = observed_routes[0] if len(observed_routes) == 1 else None
    observed_provider = observed_model = None
    if observed_route and observed_route != "unobservable":
        observed_provider, observed_model = _split_model_route(observed_route)
    return {
        "expected_provider": expected_provider,
        "expected_model": expected_model,
        "observed_provider": observed_provider,
        "observed_model": observed_model,
        "observed_routes": observed_routes,
        "observed_route_consistent": len(observed_routes) <= 1,
    }


def _host_prompt(*, request_path: Path, chunk_path: Path,
                 response_path: Path, run_id: str, chunk_index: int,
                 chunk_count: int, attempt: int = 1,
                 retry_hint: str | None = None,
                 retry_parent_response_sha256: str | None = None,
                 retry_parent_response_path: Path | None = None,
                 retry_error_records: list[dict[str, Any]] | None = None,
                 provenance: dict[str, Any] | None = None) -> str:
    contract_version = HOST_REVIEW_CONTRACT_V2
    try:
        packet = json.loads(chunk_path.read_text(encoding="utf-8"))
        if isinstance(packet, dict) and packet.get("contract_version") in SUPPORTED_HOST_REVIEW_CONTRACTS:
            contract_version = str(packet["contract_version"])
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    repair_guidance = _contract_repair_guidance("")
    retry_text = ""
    if contract_version == HOST_REVIEW_CONTRACT_V3:
        # The v3 packet removes the reverse relation from the model-facing
        # schema.  Do not leave v2 repair prose in the prompt, because a retry
        # must not reintroduce the duplicate model-maintained index.
        repair_guidance = "\n".join(
            line for line in repair_guidance.splitlines()
            if "requirement_indexes" not in line
        )
    if retry_hint:
        retry_guidance = (
            _structured_contract_repair_guidance(
                retry_error_records, contract_version=contract_version,
            )
            if retry_error_records else _contract_repair_guidance(retry_hint, include_base=False)
        )
        if contract_version == HOST_REVIEW_CONTRACT_V3:
            retry_guidance = "\n".join(
                line for line in retry_guidance.splitlines()
                if "requirement_indexes" not in line
            )
        retry_text = (
            "\nThis is a retry after the previous attempt was rejected locally. "
            "Do not discuss the failure; return a newly generated valid JSON object. "
            f"Reason category: {retry_hint}.\n"
            "Apply the following targeted contract repair rules:\n"
            f"{retry_guidance}\n"
        )
    relation_text = (
        "Do not emit clause_reviews.requirement_indexes; requirements[].clause_ids is the sole authoritative relation and the bridge derives the reverse view."
        if contract_version == HOST_REVIEW_CONTRACT_V3 else
        "Maintain requirement_indexes exactly as required by the contract and verify every index against requirements[index].clause_ids."
    )
    if retry_parent_response_path is not None:
        parent_text = f"""The rejected parent response is available only as a repair baseline at:
{retry_parent_response_path}
Read that file on this retry. The current chunk packet and cited evidence remain
the semantic authority. Preserve every non-error semantic field from the parent
response exactly: clause IDs, classifications, obligations, requirement count,
roles, clause_ids, evidence_ids, applicability, prerequisites, verification,
and unrelated properties. Apply only the minimum mechanical edits explicitly
identified by the structured validator records above. Do not split, merge, add,
drop, or reorder requirements, and do not reclassify a clause. Return the full
response object, not a patch. The bridge will reject any unapproved semantic
drift. Parent response sha256: {retry_parent_response_sha256 or 'unavailable'}."""
    else:
        parent_text = (
            f"The rejected parent response sha256 was {retry_parent_response_sha256}; "
            "there is no prior response file to reuse."
            if retry_parent_response_sha256 else "There is no prior response to reuse."
        )
    retry_invariant = (
        """\nFINAL RETRY INVARIANT: copy every clause_review classification, obligation,
requirement identity, clause_ids, evidence_ids, and requirement count from the
repair baseline exactly. The only permitted differences are the exact property
paths named by the structured validator records above. If a review is already
classified executable, repair its missing relation only; do not turn an unresolved or informational review into executable. If a safe local repair is
not possible without changing semantics, return the parent object unchanged
and let the bridge fail closed."""
        if retry_parent_response_path is not None else ""
    )
    return f"""You are the current Host Agent for one fresh thesis-format semantic-review run.

Return exactly ONE JSON object and nothing else. Do not use Markdown fences,
explanations, shell commands, OOXML, or code outside that JSON object.

Full request audit pointer (do not open the full file for this subtask):
{request_path}

Read exactly this compact current-chunk packet with your local file tool:
{chunk_path}

It contains every clause and cited evidence item for this subtask, plus the
allowed roles, the complete machine-readable requirement_contract and
response_schema, declaration instructions, and structure summary. The trusted
provenance remains in the bridge-owned request packet and is not a model input;
the bridge will bind it only after the semantic contract passes.
The requirement_contract and response_schema are authoritative. Follow their
role-specific properties and nested schemas exactly; do not invent aliases or
free-form replacements for fields such as applicability, input_prerequisites,
verification, or confidence.
Do not read the full llm-request-chunks.json file, because it contains other
chunks that are outside this subtask.

This is chunk {chunk_index} of {chunk_count}, attempt {attempt}, run_id {run_id}. Read the chunk
JSON and follow its contract_version {contract_version} instructions literally. The local
bridge will bind the response to this current invocation and request; do not
write, copy, abbreviate, or recompute a provenance/hash object in the response.
The raw response is retained before binding for audit. Review every and only the
supplied clause IDs exactly once.
Cite only evidence and clause IDs present in this chunk.
For declaration clauses, copy fixed headings/body paragraphs exactly from the
cited evidence, use a run-local semantic item id, and use blank signature
placeholders only; never invent resource_id, version, or sha256.

Do not consult, copy, or repair any previous response, build directory,
school-specific resource, or conversation memory, except for the explicit
immutable repair baseline named above when this is a retry. The current chunk
JSON is the only semantic source. Do not modify project files. The runner will save
your JSON as:
{response_path}

{relation_text}
{parent_text}
Before answering, verify that the result is a complete contract-{contract_version} object
with requirements, clause_reviews, unsupported_items, and reported_conflicts.
Mechanical contract checklist (apply before returning JSON):
{repair_guidance}
{retry_text}
{retry_invariant}
"""


def _resolve_openclaw(binary: str | None) -> str:
    return openclaw_adapter.resolve_binary(binary)


def _resolve_codex(binary: str | None) -> str:
    return codex_adapter.resolve_binary(binary)


def _load_session_records(
    openclaw_bin: str,
    *,
    agent_id: str | None = None,
    active_minutes: int | None = None,
) -> list[dict[str, Any]]:
    """Read stored OpenClaw session metadata without starting an agent turn."""
    command = [openclaw_bin, "sessions", "--json", "--limit", "all"]
    # OpenClaw refuses an unscoped session-store read when more than one
    # agent is configured.  Keep discovery scoped to the Host Agent's
    # configured owner, just as the subsequent turn is.
    if agent_id:
        command += ["--agent", agent_id]
    if active_minutes is not None:
        command += ["--active", str(active_minutes)]
    # Session discovery is part of the host invocation boundary.  Keep it in
    # the same owned process group as agent calls so a timed-out gateway
    # query cannot leave a descendant holding captured pipes open.
    result = _run_command(command, timeout=30)
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()[-1200:]
        raise RuntimeError(
            f"cannot inspect OpenClaw parent sessions (returncode {result.returncode}): {detail}"
        )
    try:
        payload = strict_json_loads(result.stdout)
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"OpenClaw sessions returned invalid JSON: {exc}") from exc
    records = payload.get("sessions") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise RuntimeError("OpenClaw sessions JSON has no sessions array")
    return [record for record in records if isinstance(record, dict)]


def _parent_session_from_environment() -> str | None:
    for name in PARENT_SESSION_ENV_NAMES:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def _route_from_parent_record(record: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Build the parent's effective provider/model route.

    A session override wins because it is the route explicitly selected for
    that session.  Otherwise the stored effective ``modelProvider`` + ``model``
    pair is used.  Returning ``None`` here would silently delegate to the
    gateway default, which is precisely the cross-user route leak this bridge
    must prevent.
    """
    model_override = str(record.get("modelOverride") or "").strip()
    provider_override = str(record.get("providerOverride") or "").strip()
    effective_provider = str(record.get("modelProvider") or "").strip()
    effective_model = str(record.get("model") or "").strip()
    metadata = {
        "parent_model_override": model_override or None,
        "parent_provider_override": provider_override or None,
        "parent_effective_provider": effective_provider or None,
        "parent_effective_model": effective_model or None,
    }
    if "/" in model_override:
        return model_override, metadata
    if model_override:
        if not provider_override:
            raise ValueError(
                "parent session has modelOverride but no providerOverride"
            )
        return f"{provider_override}/{model_override}", metadata
    if not effective_provider or not effective_model:
        raise ValueError(
            "parent session has no resolvable effective provider/model; "
            "refusing to use the gateway default"
        )
    return f"{effective_provider}/{effective_model}", metadata


def resolve_parent_model(
    openclaw_bin: str,
    *,
    agent_id: str = "main",
    parent_session_key: str | None = None,
) -> dict[str, Any]:
    """Resolve the parent session route for a Host-Agent run.

    An explicit key wins, followed by the two supported environment names.
    If neither is available, the call fails closed; a recent interactive
    session is never selected.  A parent without a session override uses its stored effective
    ``modelProvider`` + ``model`` route.  If that route cannot be resolved,
    this function fails closed instead of using the gateway default.
    """
    requested_key = (parent_session_key or _parent_session_from_environment() or "").strip()
    if not requested_key:
        raise ValueError(
            "parent session binding is missing; refusing to select a recent or global session"
        )
    records = _load_session_records(openclaw_bin, agent_id=agent_id)
    record = next((item for item in records if item.get("key") == requested_key), None)
    if record is None:
        raise ValueError(f"parent session key was not found: {requested_key}")
    selected_key = requested_key
    source = "explicit-parent-session"
    model, metadata = _route_from_parent_record(record)
    return {
        "model": model,
        "source": (
            "parent-session-override"
            if metadata.get("parent_model_override")
            else "parent-session-effective"
        ),
        "parent_session_key": selected_key,
        **metadata,
        "resolution_mode": source,
    }


def run_host_agent_chunk(
    *,
    request_path: Path,
    chunk_path: Path,
    chunk: dict[str, Any],
    response_path: Path,
    run_id: str,
    chunk_index: int,
    chunk_count: int,
    agent_id: str,
    timeout: int,
    openclaw_bin: str | None,
    prompt_path: Path,
    model: str | None = None,
    openclaw_config: Path | None = None,
    attempt: int = 1,
    retry_hint: str | None = None,
    auth_env_only: bool = False,
    runner: str = "exec",
    controller: RunController | None = None,
    adapter_id: str = "openclaw",
    codex_bin: str | None = None,
    codex_model: str | None = None,
    structured_output_mode: str = "prompt_only",
    retry_parent_response_sha256: str | None = None,
    retry_parent_response_path: Path | None = None,
    retry_error_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if controller is not None:
        controller.check()
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    model_packet_path = prompt_path.with_name(prompt_path.stem.replace("prompt", "input") + ".json")
    _write_json(model_packet_path, compact_model_packet(chunk))
    prompt_path.write_text(
        _host_prompt(
            request_path=request_path, chunk_path=model_packet_path,
            response_path=response_path,
            run_id=run_id,
            chunk_index=chunk_index,
            chunk_count=chunk_count,
            attempt=attempt,
            retry_hint=retry_hint,
            retry_parent_response_sha256=retry_parent_response_sha256,
            retry_parent_response_path=retry_parent_response_path,
            retry_error_records=retry_error_records,
            provenance=chunk.get("provenance") if isinstance(chunk.get("provenance"), dict) else None,
        ),
        encoding="utf-8",
    )
    session_key: str | None = None
    use_isolated_exec = False
    last_message_path: Path | None = None
    output_schema_path: Path | None = None
    if adapter_id == "openclaw":
        if not openclaw_bin:
            raise ValueError("OpenClaw executable is missing")
        session_key = openclaw_adapter.session_key(
            agent_id=agent_id, run_id=run_id,
            chunk_index=chunk_index, attempt=attempt,
        )
        # ``openclaw agent`` accepts --model but still consults the global fallback
        # chain for a new child session.  ``agent exec`` lets this invocation pass
        # a route-local fallback list; repeating the snapshot route is deliberate:
        # a provider outage may retry on the same route, but may never jump to a
        # different user's/global provider.  The normal command remains available
        # for the explicit no-model compatibility mode and non-main agents.
        command, use_isolated_exec = openclaw_adapter.build_command(
            binary=openclaw_bin,
            agent_id=agent_id,
            session_key_value=session_key,
            prompt_path=prompt_path,
            model=model,
            runner=runner,
            timeout=timeout,
            cwd=ROOT,
            config=openclaw_config,
            auth_env_only=auth_env_only,
        )
    elif adapter_id == "codex":
        if not codex_bin:
            raise ValueError("Codex executable is missing")
        last_message_path = response_path.with_name(
            f"{response_path.stem}.last-message.txt"
        )
        if last_message_path.exists():
            raise ValueError(
                f"refusing to overwrite existing Codex final message: {last_message_path}"
            )
        output_schema_path = prompt_path.with_name(
            f"{prompt_path.stem}.response-schema.json"
        )
        if output_schema_path.exists():
            raise ValueError(
                f"refusing to overwrite existing Codex output schema: {output_schema_path}"
            )
        response_schema = chunk.get("response_schema")
        if not isinstance(response_schema, dict) or not response_schema:
            raise ValueError("current Host Agent chunk has no response schema")
        provider_schema = native_output_schema(response_schema)
        require_native_schema(provider_schema)
        _write_json(output_schema_path, provider_schema)
        command = codex_adapter.build_command(
            binary=codex_bin,
            prompt_path=prompt_path,
            last_message_path=last_message_path,
            cwd=ROOT,
            model=codex_model,
            output_schema_path=output_schema_path if structured_output_mode == "native_schema" else None,
        )
    else:
        raise ValueError(f"unsupported Host Agent adapter: {adapter_id}")
    started = time.time()
    try:
        result = _run_command(
            command, timeout=max(timeout + 30, timeout), controller=controller,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Host Agent chunk {chunk_index}/{chunk_count} exceeded {timeout}s"
        ) from exc
    elapsed = round(time.time() - started, 1)
    raw_envelope_path = response_path.with_name(
        f"{response_path.stem}.raw-envelope.txt"
    )
    if raw_envelope_path.exists():
        raise ValueError(f"refusing to overwrite existing raw Host Agent output: {raw_envelope_path}")
    raw_envelope_path.write_text(result.stdout or "", encoding="utf-8")
    raw_stderr_path: Path | None = None
    if adapter_id == "codex" and result.stderr:
        raw_stderr_path = response_path.with_name(
            f"{response_path.stem}.raw-stderr.txt"
        )
        if raw_stderr_path.exists():
            raise ValueError(f"refusing to overwrite existing Codex stderr: {raw_stderr_path}")
        raw_stderr_path.write_text(result.stderr, encoding="utf-8")
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()[-1600:]
        raise RuntimeError(
            f"Host Agent chunk {chunk_index}/{chunk_count} failed with returncode "
            f"{result.returncode}: {detail}"
        )
    if controller is not None:
        controller.check()
    if adapter_id == "openclaw":
        response, envelope = openclaw_adapter.parse_result(result.stdout)
        route = verify_host_agent_route(envelope, model)
    else:
        final_message = None
        if last_message_path is not None and last_message_path.exists():
            final_message = last_message_path.read_text(encoding="utf-8")
        response, envelope = codex_adapter.parse_result(
            result.stdout, last_message=final_message,
        )
        route = {
            "provider": None,
            "model": None,
            "route": "unobservable",
            "fallback_used": None,
        }
    raw_response = copy.deepcopy(response)
    expected_provenance = chunk.get("provenance")
    observed_provenance = raw_response.get("provenance") if isinstance(raw_response, dict) else None
    provenance_mismatch_fields: list[str] = []
    if isinstance(expected_provenance, dict):
        if isinstance(observed_provenance, dict):
            provenance_mismatch_fields = sorted({
                str(key) for key in set(expected_provenance) | set(observed_provenance)
                if observed_provenance.get(key) != expected_provenance.get(key)
            })
        elif observed_provenance is not None:
            provenance_mismatch_fields = sorted(str(key) for key in expected_provenance)
    raw_response_path = response_path.with_name(
        f"{response_path.stem}.raw{response_path.suffix}"
    )
    if raw_response_path.exists():
        raise ValueError(f"refusing to overwrite existing raw Host Agent response: {raw_response_path}")
    atomic_write_text(
        raw_response_path,
        json.dumps(raw_response, ensure_ascii=False, indent=2) + "\n",
    )
    # The model-facing packet deliberately omits trusted identity fields.  A
    # native bridge may therefore bind a response that omits ``provenance``
    # after semantic validation.  It must never, however, overwrite a
    # provenance object that the model did emit: an old or foreign envelope
    # with a plausible payload is an invocation-integrity failure, not a value
    # to repair with the current hash.
    if provenance_mismatch_fields:
        raise HostAgentProvenanceMismatch(
            "Host Agent response provenance conflict: "
            + ", ".join(provenance_mismatch_fields)
        )
    response_schema = chunk.get("response_schema")
    if not isinstance(response_schema, dict):
        raise ValueError("current Host Agent chunk has no local response schema")
    response = normalize_native_response(raw_response, response_schema)
    contract_errors = validate_host_agent_response(response, chunk)
    if contract_errors:
        error = ValueError(
            "local response contract validation failed before provenance binding: "
            + _summarize_contract_errors(contract_errors)
        )
        error.error_records = contract_error_records(  # type: ignore[attr-defined]
            contract_errors, response=response, chunk=chunk,
        )
        raise error
    if not isinstance(expected_provenance, dict):
        raise ValueError("current Host Agent chunk has no bindable provenance")
    response["provenance"] = copy.deepcopy(expected_provenance)
    provenance_binding = (
        "model_echo_verified" if isinstance(observed_provenance, dict)
        else "bridge_generated"
    )
    if controller is not None:
        controller.check()
    atomic_write_text(
        response_path,
        json.dumps(response, ensure_ascii=False, indent=2) + "\n",
    )
    audit = {
        "adapter_id": adapter_id,
        "chunk_index": chunk_index,
        "chunk_count": chunk_count,
        "attempt": attempt,
        "session_key": session_key,
        "openclaw_run_id": envelope.get("runId") if adapter_id == "openclaw" else None,
        "codex_event_count": envelope.get("event_count") if adapter_id == "codex" else None,
        "codex_event_types": envelope.get("event_types") if adapter_id == "codex" else None,
        "codex_terminal_event_index": envelope.get("terminal_event_index") if adapter_id == "codex" else None,
        "codex_thread_id": envelope.get("thread_id") if adapter_id == "codex" else None,
        "codex_final_message_sha256": envelope.get("final_message_sha256") if adapter_id == "codex" else None,
        "codex_stream_warnings": envelope.get("stream_warnings") if adapter_id == "codex" else [],
        "structured_output_mode": structured_output_mode if adapter_id == "codex" else None,
        "codex_output_schema_path": str(output_schema_path.resolve()) if output_schema_path else None,
        "codex_output_schema_sha256": sha256_file(output_schema_path) if output_schema_path else None,
        "status": envelope.get("status", "ok"),
        "returncode": result.returncode,
        "elapsed_s": elapsed,
        "runner": "codex-exec" if adapter_id == "codex" else (
            "agent-exec" if use_isolated_exec else (
            "gateway-agent" if runner == "gateway" else "agent"
            )
        ),
        "expected_route": model if adapter_id == "openclaw" else "unobservable",
        "requested_model": codex_model if adapter_id == "codex" else model,
        "actual_provider": route.get("provider"),
        "actual_model": route.get("model"),
        "actual_route": route.get("route"),
        "fallback_used": route.get("fallback_used"),
        "local_process_state": "completed",
        "remote_operation_state": "remote_operation_completed",
        "response_path": str(response_path.resolve()),
        "prompt_sha256": sha256_file(prompt_path),
        "model_packet_sha256": sha256_file(model_packet_path),
        "raw_envelope_path": str(raw_envelope_path.resolve()),
        "raw_response_path": str(raw_response_path.resolve()),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "retry_parent_response_sha256": retry_parent_response_sha256,
    }
    audit["provenance_observed"] = isinstance(observed_provenance, dict)
    audit["provenance_mismatch_fields"] = provenance_mismatch_fields
    audit["provenance_binding"] = provenance_binding
    if raw_stderr_path is not None:
        audit["raw_stderr_path"] = str(raw_stderr_path.resolve())
    return audit


def run_bridge(
    review_dir: Path,
    *,
    response_out: Path | None = None,
    run_id: str | None = None,
    agent_id: str = "main",
    timeout: int = 900,
    max_concurrency: int = 4,
    max_attempts: int = 2,
    model: str | None = None,
    openclaw_bin: str | None = None,
    openclaw_config: Path | None = None,
    codex_bin: str | None = None,
    inherit_parent_model: bool = False,
    parent_session_key: str | None = None,
    auth_env_only: bool = False,
    runner: str = "exec",
    host_runtime: str | None = None,
    codex_model: str | None = None,
    allow_prompt_only: bool = False,
) -> dict[str, Any]:
    host_context = require_host_runtime(host_runtime)
    adapter_id = automatic_adapter_id(host_context)
    if adapter_id == "codex":
        if codex_model is not None:
            codex_model = codex_model.strip()
            if not codex_model:
                raise HostRuntimeError("Codex model must be non-empty when explicitly supplied")
    if adapter_id == "openclaw" and model is None and not inherit_parent_model and not host_context.parent_session_id:
        raise HostRuntimeError(
            "automatic OpenClaw execution requires an explicit model route or a bound parent session; refusing the gateway default"
        )
    if adapter_id == "codex":
        forbidden_options = []
        if model:
            forbidden_options.append("--model")
        if openclaw_bin:
            forbidden_options.append("--openclaw-bin")
        if openclaw_config:
            forbidden_options.append("--openclaw-config")
        if parent_session_key:
            forbidden_options.append("--parent-session-key")
        if inherit_parent_model:
            forbidden_options.append("--inherit-parent-model")
        if auth_env_only:
            forbidden_options.append("--auth-env-only")
        if runner != "exec":
            forbidden_options.append("--runner")
        if forbidden_options:
            raise HostRuntimeError(
                "Codex native adapter does not accept OpenClaw-only options: "
                + ", ".join(forbidden_options)
            )
    elif adapter_id != "openclaw":
        raise HostAdapterUnavailable(
            f"automatic adapter {adapter_id!r} is not implemented by this bridge"
        )
    review_dir = review_dir.resolve()
    manifest_path = review_dir / "host-agent-review-manifest.json"
    manifest = _read_json(manifest_path, label="host-agent review manifest")
    if manifest.get("protocol") != "host_agent_semantic_review":
        raise ValueError("review manifest is not a host-agent semantic-review manifest")
    contract_version = manifest.get("contract_version")
    if contract_version not in SUPPORTED_HOST_REVIEW_CONTRACTS:
        raise ValueError(
            "host-agent review manifest has unsupported contract version: "
            f"{contract_version!r}"
        )
    request_path = _bound_path(
        review_dir, str(manifest.get("request_path", "llm-request.json")),
        label="host-agent request path",
    )
    chunks_path = _bound_path(
        review_dir, str(manifest.get("request_chunks_path", "llm-request-chunks.json")),
        label="host-agent request chunks path",
    )
    full_request = _read_json(request_path, label="host-agent request")
    chunks = _read_json(chunks_path, label="host-agent request chunks")
    if not isinstance(full_request, dict) or not isinstance(full_request.get("provenance"), dict):
        raise ValueError("host-agent full request is missing an object provenance")
    if full_request.get("contract_version") != contract_version:
        raise ValueError("host-agent manifest contract version does not match full request")
    response_files = manifest.get("response_files")
    if not isinstance(chunks, list) or not chunks:
        raise ValueError("host-agent request chunks are missing or empty")
    if not isinstance(response_files, list) or len(response_files) != len(chunks):
        raise ValueError("host-agent response file manifest does not match request chunks")
    if any(not isinstance(item, str) or not item.strip() for item in response_files):
        raise ValueError("host-agent response file manifest contains a non-string path")
    if len(set(response_files)) != len(response_files):
        raise ValueError("host-agent response file manifest contains duplicate paths")
    # Do this before starting any native host process.  A packet that is
    # internally self-consistent is still unsafe if its clause/evidence text
    # no longer matches the frozen full request.
    validate_host_review_chunk_source_projection(
        full_request, chunks, manifest,
    )
    expected_run_id = ((full_request.get("provenance") or {}).get("run_id")
                       if isinstance(full_request, dict) else None)
    runtime_context = copy.deepcopy(full_request.get("runtime_context"))
    effective_run_id = run_id or expected_run_id
    if not isinstance(effective_run_id, str) or not effective_run_id:
        raise ValueError("fresh host-agent request is missing provenance.run_id")
    if expected_run_id and effective_run_id != expected_run_id:
        raise ValueError("supplied run_id does not match the fresh request provenance")
    if isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("Host Agent timeout must be a positive integer")
    if isinstance(max_concurrency, bool) or max_concurrency <= 0:
        raise ValueError("Host Agent max concurrency must be a positive integer")
    if isinstance(max_attempts, bool) or max_attempts <= 0:
        raise ValueError("Host Agent max attempts must be a positive integer")

    response_out = _bound_path(
        review_dir.parent,
        response_out or (review_dir.parent / "host-agent-response.json"),
        label="Host Agent merged response path",
    )
    audit_path = review_dir / "host-agent-run.json"
    if audit_path.exists():
        raise ValueError(f"refusing to reuse an existing Host Agent audit: {audit_path}")
    if response_out.exists():
        raise ValueError(f"refusing to reuse an existing Host Agent response: {response_out}")

    started_at = datetime.now(timezone.utc).isoformat()
    prompt_dir = review_dir / "host-agent-prompts"
    controller = RunController()
    binary: str | None = None
    codex_binary: str | None = None
    if adapter_id == "openclaw":
        binary = _resolve_openclaw(openclaw_bin)
        if openclaw_config is not None:
            openclaw_config = openclaw_config.expanduser().resolve()
            if not openclaw_config.is_file():
                raise ValueError(f"OpenClaw config does not exist: {openclaw_config}")
    else:
        codex_binary = _resolve_codex(codex_bin)
        openclaw_config = None
    codex_capabilities: dict[str, Any] | None = None
    structured_output_mode = "prompt_only"
    if adapter_id == "codex":
        codex_capabilities = codex_adapter.probe_capabilities(codex_binary)
        if not codex_capabilities.get("output_schema_supported") and not allow_prompt_only:
            raise HostRuntimeError(
                "native Codex CLI does not advertise --output-schema; refusing prompt-only "
                "semantic review (use --allow-prompt-only only for an explicit non-release run)"
            )
        structured_output_mode = str(codex_capabilities.get("structured_output_mode") or "prompt_only")
    resolution: dict[str, Any] = {
        "model": model if adapter_id == "openclaw" else codex_model,
        "source": (
            "explicit-model" if model else "gateway-default"
        ) if adapter_id == "openclaw" else (
            "explicit-native-codex-model" if codex_model
            else "native-codex-cli-current-config"
        ),
        "parent_session_key": None,
        "parent_model_override": None,
        "parent_provider_override": None,
        "parent_effective_provider": None,
        "parent_effective_model": None,
    }
    bound_parent_session = None
    if adapter_id == "openclaw":
        bound_parent_session = require_parent_session(
            host_context, parent_session_key,
        ) if (inherit_parent_model or parent_session_key or host_context.parent_session_id) else None
        if model is None and (inherit_parent_model or bound_parent_session):
            resolution = resolve_parent_model(
                binary,
                agent_id=agent_id,
                parent_session_key=bound_parent_session,
            )
    effective_model = resolution.get("model")
    if effective_model and adapter_id == "openclaw":
        _split_model_route(str(effective_model))

    lifecycle_lock = threading.Lock()
    chunk_lifecycle: dict[int, dict[str, Any]] = {
        index: {
            "chunk_index": index,
            "status": "not_started",
            "started_at": None,
            "finished_at": None,
            "attempts": [],
            "remote_operation_state": "unknown",
        }
        for index in range(1, len(chunks) + 1)
    }

    def update_chunk_lifecycle(index: int, **updates: Any) -> None:
        with lifecycle_lock:
            record = chunk_lifecycle.setdefault(index, {"chunk_index": index})
            record.update(updates)

    def persist_failure_audit(
        error: BaseException,
        chunk_runs: list[dict[str, Any]],
        *,
        in_flight_chunk_indexes: list[int] | None = None,
        chunk_lifecycle: dict[int, dict[str, Any]] | None = None,
    ) -> None:
        """Persist a terminal failure without manufacturing a merged response."""
        _write_json(audit_path, {
            "schema_version": "1.0",
            "status": "failed",
            "protocol": "host_agent_semantic_review",
            "run_id": effective_run_id,
            "agent_id": agent_id,
            "execution_mode": "native-adapter",
            "adapter_id": adapter_id,
            **host_context.as_audit(),
            "max_concurrency": max_concurrency,
            "max_attempts": max_attempts,
            "model": effective_model,
            "codex_capabilities": copy.deepcopy(codex_capabilities),
            "structured_output_mode": structured_output_mode,
            "runtime_context": copy.deepcopy(runtime_context),
            **_route_audit_fields(
                effective_model if adapter_id == "openclaw" else None,
                chunk_runs,
            ),
            "route_visibility": "provider-model" if adapter_id == "openclaw" else "unobservable",
            "auth_env_only": bool(auth_env_only),
            "runner": runner,
            "model_source": resolution.get("source"),
            "openclaw_config": str(openclaw_config) if openclaw_config else None,
            "codex_bin": codex_binary,
            "route_policy": (
                "parent-effective-route-snapshot"
                if adapter_id == "openclaw" else (
                    "explicit-native-codex-model" if codex_model
                    else "native-codex-cli-current-config"
                )
            ),
            "route_verification": (
                "child-winner-must-match; fallback-must-be-false"
                if adapter_id == "openclaw" else "provider-model-unobservable"
            ),
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "error_type": type(error).__name__,
            "error": str(error),
            "terminal_status": "stopped" if isinstance(error, HostAgentCancelled) else "failed",
            "local_process_state": (
                "stopped" if isinstance(error, HostAgentCancelled)
                else "failed_or_not_started"
            ),
            # A completed local child is not proof that the whole remote
            # operation completed.  In particular, a sibling failure can
            # terminate the local CLI while a gateway-side request remains
            # unobservable.  Never report overall completion from a partial
            # chunk list.
            "remote_operation_state": "remote_operation_state_unknown",
            "remote_operation_observation": (
                "local_processes_terminated_or_failed; remote_completion_not_proven"
            ),
            "completed_chunk_indexes": sorted({
                int(item.get("chunk_index")) for item in chunk_runs
                if isinstance(item, dict) and isinstance(item.get("chunk_index"), int)
            }),
            "in_flight_chunk_indexes": sorted({
                int(item) for item in (in_flight_chunk_indexes or [])
                if isinstance(item, int)
            }),
            "chunk_runs": chunk_runs,
            "structured_error_records": [
                record
                for item in (chunk_lifecycle or {}).values()
                for record in (item.get("structured_error_records") or [])
                if isinstance(record, dict)
            ],
            "failure_chain": [
                {
                    "chunk_index": item.get("chunk_index"),
                    "attempts": copy.deepcopy(item.get("attempts", [])),
                    "terminal_error": item.get("error"),
                }
                for item in (chunk_lifecycle or {}).values()
            ],
            "chunk_lifecycle": [
                (chunk_lifecycle or {})[index]
                for index in sorted(chunk_lifecycle or {})
            ],
            "merged_response_written": False,
        })

    def run_and_validate(index: int, chunk: dict[str, Any], response_name: str) -> dict[str, Any]:
        controller.check()
        if not isinstance(chunk, dict):
            raise ValueError(f"host-agent chunk {index} is not an object")
        if not isinstance(response_name, str) or not response_name:
            raise ValueError(f"host-agent response filename {index} is invalid")
        response_path = _bound_path(
            review_dir, response_name, label=f"Host Agent response path {index}",
        )
        if response_path.exists():
            raise ValueError(f"refusing to reuse an existing chunk response: {response_path}")
        provenance = chunk.get("provenance")
        if not isinstance(provenance, dict):
            raise ValueError(f"host-agent chunk {index} has no provenance")
        batch = chunk.get("batch") if isinstance(chunk.get("batch"), dict) else {}
        chunk_index = int(batch.get("index", index))
        chunk_count = int(batch.get("count", len(chunks)))
        update_chunk_lifecycle(
            index,
            status="running",
            started_at=datetime.now(timezone.utc).isoformat(),
            chunk_count=chunk_count,
        )
        failures: list[str] = []
        retry_error_records: list[dict[str, Any]] = []
        for attempt in range(1, max_attempts + 1):
            attempt_response_path = response_path.with_name(
                f"{response_path.stem}.attempt-{attempt:02d}{response_path.suffix}"
            )
            if attempt_response_path.exists():
                raise ValueError(
                    "refusing to reuse an existing Host Agent attempt response: "
                    f"{attempt_response_path}"
                )
            try:
                attempt_started = datetime.now(timezone.utc).isoformat()
                with lifecycle_lock:
                    chunk_lifecycle[index].update(
                        current_attempt=attempt,
                        attempts=[
                            *chunk_lifecycle[index].get("attempts", []),
                            {"attempt": attempt, "status": "running", "started_at": attempt_started},
                        ],
                    )
                retry_parent_response_path = None
                if attempt > 1:
                    candidate_parent_path = response_path.with_name(
                        f"{response_path.stem}.attempt-{attempt - 1:02d}.raw{response_path.suffix}"
                    )
                    if candidate_parent_path.is_file():
                        retry_parent_response_path = candidate_parent_path
                audit = run_host_agent_chunk(
                    request_path=request_path,
                    chunk_path=chunks_path,
                    chunk=chunk,
                    response_path=attempt_response_path,
                    run_id=effective_run_id,
                    chunk_index=chunk_index,
                    chunk_count=chunk_count,
                    agent_id=agent_id,
                    timeout=timeout,
                    openclaw_bin=binary,
                    codex_bin=codex_binary,
                    openclaw_config=openclaw_config,
                    prompt_path=prompt_dir / f"prompt-{index:04d}-attempt-{attempt:02d}.txt",
                    model=effective_model,
                    attempt=attempt,
                    auth_env_only=auth_env_only,
                    runner=runner,
                    controller=controller,
                    adapter_id=adapter_id,
                    codex_model=codex_model,
                    structured_output_mode=structured_output_mode,
                    retry_parent_response_sha256=(
                        chunk_lifecycle[index].get("retry_parent_response_sha256")
                    ),
                    retry_parent_response_path=retry_parent_response_path,
                    retry_error_records=copy.deepcopy(retry_error_records),
                    retry_hint=(
                        "local contract validation failed; repair the response: "
                        + failures[-1]
                        if failures else None
                    ),
                )
                if attempt > 1:
                    previous_raw_path = response_path.with_name(
                        f"{response_path.stem}.attempt-{attempt - 1:02d}.raw{response_path.suffix}"
                    )
                    if previous_raw_path.is_file():
                        previous_response = normalize_native_response(
                            _read_json(
                            previous_raw_path,
                            label=f"Host Agent previous raw response {index} attempt {attempt - 1}",
                            ),
                            chunk.get("response_schema") if isinstance(chunk.get("response_schema"), dict) else {},
                        )
                        current_response = _read_json(
                            attempt_response_path,
                            label=f"Host Agent response {index} attempt {attempt}",
                        )
                        change_error, semantic_changes = _retry_semantic_change_error(
                            previous_response,
                            current_response,
                            retry_error_records,
                            contract_version=contract_version,
                        )
                        if change_error is not None:
                            with lifecycle_lock:
                                chunk_lifecycle[index].setdefault("semantic_retry_changes", []).extend(
                                    semantic_changes
                                )
                            raise change_error
                        if semantic_changes:
                            audit["semantic_retry_changes"] = semantic_changes
                            audit["semantic_retry_change_policy"] = "mechanical_only"
                response = _read_json(
                    attempt_response_path,
                    label=f"Host Agent response {index} attempt {attempt}",
                )
                if response.get("contract_version") != contract_version:
                    raise ValueError(f"response is not contract {contract_version}")
                provenance_errors = validate_response_provenance(
                    response, provenance, require_fresh_origin=True,
                )
                if provenance_errors:
                    error = ValueError(
                        "provenance failed: " + ", ".join(provenance_errors)
                    )
                    error.error_records = provenance_error_records(  # type: ignore[attr-defined]
                        provenance_errors, response=response,
                    )
                    raise error
                contract_errors = validate_host_agent_response(response, chunk)
                if contract_errors:
                    error = ValueError(
                        "local response contract validation failed: "
                        + _summarize_contract_errors(contract_errors)
                    )
                    error.error_records = contract_error_records(  # type: ignore[attr-defined]
                        contract_errors, response=response, chunk=chunk,
                    )
                    raise error
                attempt_response_path.replace(response_path)
                audit["attempt_failures"] = failures
                update_chunk_lifecycle(
                    index,
                    status="completed",
                    finished_at=datetime.now(timezone.utc).isoformat(),
                    remote_operation_state="completed",
                )
                with lifecycle_lock:
                    if chunk_lifecycle[index].get("attempts"):
                        chunk_lifecycle[index]["attempts"][-1].update(
                            status="completed", finished_at=datetime.now(timezone.utc).isoformat()
                        )
                return audit
            except (HostAgentRouteMismatch, HostAgentProvenanceMismatch, HostAgentCancelled) as exc:
                # A route mismatch is not a model-quality error.  Retrying
                # would spend more tokens on an unauthorized route, so abort
                # the whole run immediately and preserve fail-closed behavior.
                with lifecycle_lock:
                    if chunk_lifecycle[index].get("attempts"):
                        chunk_lifecycle[index]["attempts"][-1].update(
                            status="terminated",
                            finished_at=datetime.now(timezone.utc).isoformat(),
                            error=str(exc),
                        )
                raise
            except (OSError, ValueError, RuntimeError) as exc:
                controller.check()
                previous_error_records = copy.deepcopy(retry_error_records)
                if attempt > 1 and previous_error_records:
                    previous_raw_path = response_path.with_name(
                        f"{response_path.stem}.attempt-{attempt - 1:02d}.raw{response_path.suffix}"
                    )
                    current_raw_path = attempt_response_path.with_name(
                        f"{attempt_response_path.stem}.raw{attempt_response_path.suffix}"
                    )
                    response_schema = chunk.get("response_schema")
                    if (
                        previous_raw_path.is_file()
                        and current_raw_path.is_file()
                        and isinstance(response_schema, dict)
                    ):
                        try:
                            previous_response = normalize_native_response(
                                _read_json(
                                    previous_raw_path,
                                    label=f"Host Agent previous raw response {index} attempt {attempt - 1}",
                                ),
                                response_schema,
                            )
                            current_response = normalize_native_response(
                                _read_json(
                                    current_raw_path,
                                    label=f"Host Agent raw response {index} attempt {attempt}",
                                ),
                                response_schema,
                            )
                            change_error, semantic_changes = _retry_semantic_change_error(
                                previous_response,
                                current_response,
                                previous_error_records,
                                contract_version=contract_version,
                            )
                            if semantic_changes:
                                with lifecycle_lock:
                                    chunk_lifecycle[index].setdefault(
                                        "semantic_retry_changes", []
                                    ).extend(semantic_changes)
                            if change_error is not None:
                                exc = change_error
                        except (OSError, ValueError, TypeError, json.JSONDecodeError):
                            # The primary contract failure remains authoritative
                            # when an attempt cannot be decoded for the
                            # secondary retry-drift audit.
                            pass
                failures.append(str(exc))
                error_records = getattr(exc, "error_records", None)
                if isinstance(error_records, list):
                    with lifecycle_lock:
                        chunk_lifecycle[index].setdefault("structured_error_records", []).extend(
                            copy.deepcopy(error_records)
                        )
                    retry_error_records = copy.deepcopy(error_records)
                parent_response_path = attempt_response_path
                if not parent_response_path.exists():
                    raw_candidate = attempt_response_path.with_name(
                        f"{attempt_response_path.stem}.raw{attempt_response_path.suffix}"
                    )
                    if raw_candidate.exists():
                        parent_response_path = raw_candidate
                if parent_response_path.exists():
                    try:
                        parent_sha = sha256_file(parent_response_path)
                        with lifecycle_lock:
                            chunk_lifecycle[index]["retry_parent_response_sha256"] = parent_sha
                    except OSError:
                        pass
                with lifecycle_lock:
                    if chunk_lifecycle[index].get("attempts"):
                        chunk_lifecycle[index]["attempts"][-1].update(
                            status="failed" if attempt >= max_attempts else "retrying",
                            finished_at=datetime.now(timezone.utc).isoformat(),
                            error=str(exc),
                            error_records=copy.deepcopy(error_records) if isinstance(error_records, list) else [],
                        )
                if attempt >= max_attempts:
                    update_chunk_lifecycle(
                        index,
                        status="failed",
                        finished_at=datetime.now(timezone.utc).isoformat(),
                        remote_operation_state="unknown",
                        error=str(exc),
                    )
                    raise ValueError(
                        f"Host Agent response {index}/{len(chunks)} failed after "
                        f"{max_attempts} attempts: {failures[-1]}"
                    ) from exc
        raise AssertionError("unreachable Host Agent retry state")

    chunk_audits_by_index: dict[int, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(max_concurrency, len(chunks))) as executor:
        pending = iter(enumerate(zip(chunks, response_files), start=1))
        futures: dict[Any, int] = {}

        def fill_slots() -> None:
            while len(futures) < max_concurrency:
                controller.check()
                try:
                    index, (chunk, response_name) = next(pending)
                except StopIteration:
                    return
                futures[executor.submit(
                    run_and_validate, index, chunk, response_name,
                )] = index

        fill_slots()
        try:
            while futures:
                done, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in done:
                    index = futures.pop(future)
                    chunk_audits_by_index[index] = future.result()
                fill_slots()
        except Exception as exc:
            controller.request_stop(str(exc))
            in_flight_chunk_indexes = sorted({
                int(index) for index in futures.values()
                if isinstance(index, int)
            })
            controller.terminate_all()
            with lifecycle_lock:
                for index, record in chunk_lifecycle.items():
                    if record.get("status") in {"not_started", "running", "retrying"}:
                        record.update(
                            status="terminated",
                            finished_at=datetime.now(timezone.utc).isoformat(),
                            remote_operation_state="unknown",
                            termination_reason=str(exc),
                        )
            for future in futures:
                update_chunk_lifecycle(
                    futures[future],
                    status="terminated",
                    finished_at=datetime.now(timezone.utc).isoformat(),
                    remote_operation_state="unknown",
                    termination_reason=str(exc),
                )
                future.cancel()
            persist_failure_audit(
                exc,
                [chunk_audits_by_index[index]
                 for index in sorted(chunk_audits_by_index)],
                in_flight_chunk_indexes=in_flight_chunk_indexes,
                chunk_lifecycle=chunk_lifecycle,
            )
            raise

    chunk_audits = [chunk_audits_by_index[index]
                    for index in sorted(chunk_audits_by_index)]

    try:
        merged, merge_metadata = merge_host_agent_review_packets(
            review_dir, response_out=response_out,
        )
    except Exception as exc:
        persist_failure_audit(exc, chunk_audits, chunk_lifecycle=chunk_lifecycle)
        raise
    payload = {
        "schema_version": "1.0",
        "status": "merged",
        "protocol": "host_agent_semantic_review",
        "run_id": effective_run_id,
        "agent_id": agent_id,
        "execution_mode": "native-adapter",
        "adapter_id": adapter_id,
        **host_context.as_audit(),
        "max_concurrency": max_concurrency,
        "max_attempts": max_attempts,
        "model": effective_model,
        "codex_capabilities": copy.deepcopy(codex_capabilities),
        "structured_output_mode": structured_output_mode,
        "runtime_context": copy.deepcopy(runtime_context),
        **_route_audit_fields(
            effective_model if adapter_id == "openclaw" else None,
            chunk_audits,
        ),
        "route_visibility": "provider-model" if adapter_id == "openclaw" else "unobservable",
        "local_process_state": "completed",
        "remote_operation_state": "remote_operation_completed",
        "chunk_lifecycle": [chunk_lifecycle[index] for index in sorted(chunk_lifecycle)],
        "auth_env_only": bool(auth_env_only),
        "runner": runner,
        "model_source": resolution.get("source"),
        "codex_bin": codex_binary,
        "parent_session_key": resolution.get("parent_session_key"),
        "parent_model_override": resolution.get("parent_model_override"),
        "parent_provider_override": resolution.get("parent_provider_override"),
        "parent_effective_provider": resolution.get("parent_effective_provider"),
        "parent_effective_model": resolution.get("parent_effective_model"),
        "route_policy": (
            "parent-effective-route-snapshot"
            if adapter_id == "openclaw" else (
                "explicit-native-codex-model" if codex_model
                else "native-codex-cli-current-config"
            )
        ),
        "route_verification": (
            "child-winner-must-match; fallback-must-be-false"
            if adapter_id == "openclaw" else "provider-model-unobservable"
        ),
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "response_path": str(response_out),
        "chunk_count": len(chunk_audits),
        "chunk_runs": chunk_audits,
        "merge": merge_metadata,
        "response_contract_version": merged.get("contract_version"),
        "response_clause_count": len(merged.get("clause_reviews", [])),
        "response_requirement_count": len(merged.get("requirements", [])),
    }
    _write_json(audit_path, payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("review_dir", type=Path,
                        help="fresh directory containing host-agent-review-manifest.json")
    parser.add_argument("--response-out", type=Path,
                        help="merged response path; defaults outside review_dir")
    parser.add_argument("--run-id", help="optional run id; must match request provenance")
    parser.add_argument(
        "--host-runtime",
        help="expected native host runtime; it must match "
             "THESIS_FORGE_HOST_RUNTIME and is never a cross-host fallback",
    )
    parser.add_argument("--agent-id", default="main",
                        help="OpenClaw agent id used by the OpenClaw adapter")
    parser.add_argument("--timeout", type=int, default=900,
                        help="per-chunk native Host Agent timeout in seconds (default: 900)")
    parser.add_argument("--max-concurrency", type=int, default=4,
                        help="maximum number of independent Host Agent chunks in flight (default: 4)")
    parser.add_argument("--max-attempts", type=int, default=2,
                        help="maximum attempts per chunk before failing closed (default: 2)")
    parser.add_argument("--model",
                        help="explicit OpenClaw provider/model route; rejected by the Codex adapter")
    parser.add_argument("--parent-session-key",
                        help="exact parent session key whose effective provider/model route should be copied")
    parser.add_argument("--auth-env-only", action="store_true",
                        help="use provider credentials from environment variables only")
    parser.add_argument("--runner", choices=("exec", "gateway"), default="exec",
                        help="OpenClaw invocation path (default: exec)")
    parser.add_argument("--inherit-parent-model", dest="inherit_parent_model",
                        action="store_true", default=None,
                        help="copy the parent session model override when --model is omitted")
    parser.add_argument("--no-inherit-parent-model", dest="inherit_parent_model",
                        action="store_false",
                        help="disable parent inheritance only with an explicit --model route")
    parser.add_argument("--openclaw-bin",
                        help="optional path to the openclaw executable")
    parser.add_argument("--openclaw-config", type=Path,
                        help="optional config file passed explicitly to `openclaw agent exec`")
    parser.add_argument("--codex-bin",
                        help="optional path to the native codex executable")
    parser.add_argument("--codex-model",
                        help="optional explicit native Codex model; omitted means the current Codex CLI configuration")
    parser.add_argument(
        "--allow-prompt-only", action="store_true",
        help="explicit non-release override when native Codex lacks --output-schema",
    )
    args = parser.parse_args(argv)
    if args.inherit_parent_model is False and not args.model:
        parser.error("--no-inherit-parent-model requires an explicit --model route")
    if args.inherit_parent_model is None:
        # The native Codex adapter must never receive the OpenClaw-only parent
        # route option.  Preserve the historical OpenClaw CLI default while
        # making the unset state adapter-aware.
        declared_runtime = (args.host_runtime or
                             os.environ.get("THESIS_FORGE_HOST_RUNTIME", "openclaw"))
        args.inherit_parent_model = declared_runtime.strip().lower() != "codex"
    try:
        payload = run_bridge(
            args.review_dir,
            response_out=args.response_out,
            run_id=args.run_id,
            agent_id=args.agent_id,
            timeout=args.timeout,
            max_concurrency=args.max_concurrency,
            max_attempts=args.max_attempts,
            model=args.model,
            openclaw_bin=args.openclaw_bin,
            openclaw_config=args.openclaw_config,
            codex_bin=args.codex_bin,
            codex_model=args.codex_model,
            inherit_parent_model=args.inherit_parent_model,
            parent_session_key=args.parent_session_key,
            auth_env_only=args.auth_env_only,
            runner=args.runner,
            host_runtime=args.host_runtime,
            allow_prompt_only=args.allow_prompt_only,
        )
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"host-agent bridge failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
