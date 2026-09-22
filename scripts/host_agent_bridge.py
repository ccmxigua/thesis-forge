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
# Versioned neutral default used when a schema-required cover institution is
# absent in an administrative-only chunk. It is a placeholder, not an
# institution identity and never comes from the model.
NEUTRAL_COVER_PLACEHOLDER = "——"
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
from compliance import classification_requires_requirement  # noqa: E402
from host_review_contract import (  # noqa: E402
    HOST_REVIEW_CONTRACT_V2,
    HOST_REVIEW_CONTRACT_V3,
    SUPPORTED_HOST_REVIEW_CONTRACTS,
    contract_error_records,
    provenance_error_records,
    analyze_requirement_relations,
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
        "fixed_declaration_candidates": _fixed_declaration_candidates(
            chunk.get("clauses"), compact_evidence,
            anchor=chunk.get("declaration_anchor_preference"),
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


def _fixed_declaration_candidates(
    clauses: Any, evidence_context: dict[str, Any], *, anchor: Any,
) -> list[dict[str, Any]]:
    """Derive exact fixed-declaration groups from the current evidence.

    This is only an evidence grouping hint for the host model.  It never
    invents declaration text, assigns a semantic role to arbitrary prose, or
    changes a clause classification.  The final response validator still
    requires the emitted declaration text to equal the cited evidence.
    """
    if not isinstance(clauses, list) or not isinstance(evidence_context, dict):
        return []

    def normalized(value: Any) -> str:
        return re.sub(r"\s+", "", str(value or ""))

    heading_pattern = re.compile(
        r"(?:原创性|独创性|诚信|使用授权|版权授权|公开授权).{0,12}(?:声明|说明|书)$"
        r"|^(?:非公开|不公开)学位论文标注说明$"
        r"|^(?:声明|授权书)$"
    )
    boundary_pattern = re.compile(
        r"^(?:摘\s*要|ABSTRACT|目\s*录|参考文献|第[一二三四五六七八九十\d]+章)$",
        re.IGNORECASE,
    )
    signature_pattern = re.compile(
        r"(?:签名|签字)|日期.{0,12}年?.{0,6}月?.{0,6}日?"
    )

    candidates: list[dict[str, Any]] = []
    for start, clause in enumerate(clauses):
        if not isinstance(clause, dict):
            continue
        heading_text = str(clause.get("text") or "")
        if len(normalized(heading_text)) > 50 or not heading_pattern.search(normalized(heading_text)):
            continue
        body_clauses: list[dict[str, Any]] = []
        for following in clauses[start + 1:]:
            if not isinstance(following, dict):
                continue
            text = str(following.get("text") or "")
            compact = normalized(text)
            if boundary_pattern.match(compact):
                break
            evidence_ids = [str(value) for value in following.get("evidence_ids", [])]
            evidence_texts = [
                str(evidence_context[evidence_id].get("text") or "")
                for evidence_id in evidence_ids
                if isinstance(evidence_context.get(evidence_id), dict)
            ]
            source_text = evidence_texts[0] if evidence_texts else text
            if compact and not signature_pattern.search(source_text):
                body_clauses.append(following)
        if not body_clauses:
            continue
        clauses_in_group = [clause, *body_clauses]
        evidence_ids = [
            str(value)
            for item in clauses_in_group
            for value in item.get("evidence_ids", [])
        ]
        candidates.append({
            "kind": "exact_fixed_declaration",
            "heading_clause_id": clause.get("id"),
            "body_clause_ids": [item.get("id") for item in body_clauses],
            "clause_ids": [item.get("id") for item in clauses_in_group],
            "heading_evidence_ids": [str(value) for value in clause.get("evidence_ids", [])],
            "body_evidence_ids": [
                str(value)
                for item in body_clauses
                for value in item.get("evidence_ids", [])
            ],
            "evidence_ids": list(dict.fromkeys(evidence_ids)),
            "before_role": anchor,
            "policy": "copy exact cited heading/body text; do not paraphrase",
        })
    return candidates


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
    "For an explicit acknowledgments length clause such as '字数一般不超过500字', use the content_constraints role with the nested payload properties.acknowledgments.max_chars. Do not emit generic null-valued placeholder fields or put the limit at the role root; the bridge may compile this exact evidence-backed form mechanically.",
    "For an explicit appendix placement clause such as '附录放在正文之后另起页', use the appendices role with properties.page_break_each: true. Do not emit generic null-valued appendix fields or infer labels/titles/order from this clause; the bridge may compile only this exact evidence-backed page-break form mechanically.",
    "For a cover requirement with an empty required institution string and the declared neutral placeholder policy, preserve the cover structure and use '——'; never copy a school name or infer an institution identity from nearby evidence.",
    "Do not fabricate evidence or guess a semantic classification. Make only the mechanical schema corrections required by the supplied error, then regenerate the complete response from the current chunk.",
    "Administrative approval/marking tables belong under cover.non_public_administration, must be conditional on thesis_profile.security_level with an equals or in condition selecting restricted/classified theses, and must be blank for public theses. If the current chunk contains only this administrative region, cover.fields may be an empty array; never duplicate administrative fields into ordinary cover.fields. Do not use not_equals as the executable binding. Bind approval-number and approval-date labels to approval_number and approval_date; never substitute classification_number or completion_date. When one visible 保密期限 label describes an explicit two-ended date range, preserve two distinct fields in source order: first embargo_start, then embargo_until; do not collapse both endpoints into embargo_until.",
    "When the packet contains fixed_declaration_candidates, treat each candidate as an exact evidence grouping. If any candidate clause is classified executable/covered/verify_existing, emit one declarations requirement covering the candidate clause_ids, copy the cited heading/body text exactly, and preserve the supplied declaration anchor; never leave an executable declaration clause without a derived declarations requirement.",
    "Input prerequisite keys are namespace-bound by kind: metadata uses thesis_profile., source_content uses source_inventory., template_resource uses template_profile., and runtime uses runtime.; never emit runtime_context.* or invent an unregistered path.",
    "A clause can be executable only when every independently verifiable obligation is represented. Preserve language targets, units, limits, exceptions, and prohibited-content requirements; a partial requirement must be classified non-executable with requirement_indexes: [] rather than promoted to full coverage.",
    "A single clause may support multiple requirements when it contains obligations for different roles. Repeat the exact clause_id and cited evidence_ids in each semantically matching requirement; a continuation-table clause may therefore bind both table continuation and table_caption position/alignment. Do not hide one role's obligation inside another role or change classification merely because one role is incomplete.",
    "Role boundary for equations: use the top-level equations role for layout properties declared by equationLayoutSpec (same_line, no_lines, alignment, number_alignment, number_parentheses, center_tab_twips, or right_tab_twips). Use equation only for an exact equation/content occurrence. Never put style or an invented layout key in equation; if no declared equations property represents the rule, keep the clause non-executable rather than guessing.",
    "A partial_clause_coverage error never authorizes changing classification, obligations, clause_ids, or requirement count on retry. Preserve the baseline and complete a missing role-specific requirement only when current evidence and the declared schema support it; otherwise return the baseline unchanged and let the bridge fail closed.",
    "When executable_review_requires_all_obligations_covered is reported, never change a non-covered obligation to covered merely to satisfy the validator. Preserve each obligation id and status; if the current evidence/backend cannot cover one obligation, reclassify only that clause to the most accurate non-executable classification and remove its clause edge from requirements. Do not change any other review, requirement payload, evidence, or obligation.",
    "When a contract-3.0 retry reports executable_review_requires_derived_requirement, preserve every non-placeholder requirement from the rejected response and add one evidence-backed requirement for each distinct named executable clause that lacks an authoritative edge; never drop verify_existing requirements, collapse several missing clauses into an unbound placeholder, or replace the baseline requirement set.",
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
    if "applicability" in text and "does not match" in text and ".fact" in text:
        targeted.append(
            "For an explicit condition such as '论文中出现英文时需要使用Times New Roman字体', use the registered fact source_inventory.english_text with operator present and value null. Change only the invalid fact namespace; preserve status, operator, value, exceptions, requirement identity, and all semantic fields. Do not invent a different source key or turn a missing fact into false."
        )
    if "non_public_administration" in text:
        targeted.append(
            "Keep the administrative region under cover.non_public_administration and bind applicability to thesis_profile.security_level with operator equals or in selecting restricted/classified. Do not use not_equals, move the fields to ordinary cover.fields, or guess approval values."
        )
    if "embargo" in text or "保密" in raw_text:
        targeted.append(
            "For an explicit two-ended confidentiality date range with one visible 保密期限 label, keep two fields in source order: the first binds embargo_start and the second binds embargo_until. Do not replace the first endpoint with a duplicate embargo_until."
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
            "Preserve the baseline classification, obligations, clause_ids, and requirement count. "
            "When the current evidence and declared schema support a missing role-specific requirement, "
            "complete that requirement and repeat the exact clause_id/evidence_ids for the additional role; "
            "otherwise return the baseline unchanged. Do not add a guessed min/max/semantic property, "
            "change Chinese abstract to English abstract, or reclassify merely to escape the coverage error."
        )
    if "executable_review_requires_all_obligations_covered" in text:
        targeted.append(
            "The executable review has at least one obligation that is not fully represented. Never change that obligation status to covered merely to satisfy the gate. Preserve its id and status; reclassify only the affected clause as the most accurate non-executable status, use requirement_indexes: [] for contract 2.1 or omit the reverse index for contract 3.0, and remove that clause from every requirement edge. Keep every other review, requirement, evidence ID, property, and obligation unchanged."
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
        if code.startswith("existing_requirement_") or code in {
            "unknown_existing_requirement_id", "invalid_existing_requirement_id",
        }:
            rule = (
                f"At {pointer}, the existing requirement reference is not bound to this exact current-input occurrence. "
                "Do not guess a replacement ID, remove the reference to disguise a mismatch as a new requirement, "
                "or change role, clause_ids, evidence_ids or classification. Preserve the parent and fail closed "
                "if the identity is wrong. New requirements use an omitted/null existing_requirement_id on the initial "
                "response, never a self-allocated or incremented ID."
            )
        elif code == "normative_basis_invalid":
            rule = (
                f"At {pointer}, omit the invalid normative_basis field rather than replacing it with a guessed value; "
                "keep classification and cited evidence unchanged unless the current evidence independently requires a semantic re-review."
            )
        elif code == "requirement_relation_mismatch":
            if contract_version == HOST_REVIEW_CONTRACT_V3:
                rule = (
                    f"At {pointer}, regenerate the authoritative requirements[].clause_ids relation from the current chunk. "
                    "Do not emit or maintain a reverse index in clause_reviews. If an executable clause truly has no "
                    "evidence-backed requirement, add only that requirement and preserve every existing review and requirement."
                )
            else:
                rule = (
                    f"At {pointer}, do not copy or repair a neighboring relation. The deterministic matching requirement indexes are "
                    f"{matching if isinstance(matching, list) else 'unknown'}; regenerate the complete relation from the current chunk."
                )
        elif code == "informational_requirement_forbidden":
            rule = (
                f"At {pointer}, this is a code-owned projection: the bridge will remove the requirement object only after "
                "recomputing that every linked clause review is informational. Keep each clause_review classification, reason, "
                "evidence, and source wording unchanged. Do not reclassify the clause, invent a requirement, or edit a "
                "clause-to-requirement reverse index; if no other repair is required, return the parent object unchanged."
            )
        elif code == "mixed_execution_classification_relation":
            rule = (
                f"At {pointer}, do not remove or broaden this requirement: its linked clauses mix executable and non-executable classifications. "
                "Preserve the clauses and classifications and return the response unchanged unless the current evidence supports a genuine semantic re-review."
            )
        elif code == "missing_clause_review":
            rule = (
                f"At {pointer}, do not infer a missing or duplicate clause review. Preserve the authoritative clause set and fail closed until every linked clause has exactly one current review."
            )
        elif code == "unknown_clause_relation":
            rule = (
                f"At {pointer}, do not repair an unknown clause_id by guessing a neighboring clause. Use only the exact clause IDs in the current chunk and fail closed otherwise."
            )
        elif code == "non_requirement_classification_relation":
            rule = (
                f"At {pointer}, remove only this requirement object if every clause_id it contains is classified as a non-requirement state such as unresolved, requires_source_content, unsupported, external_compliance, or informational. Preserve every clause_review classification, obligation, reason, evidence ID, and every other requirement exactly; do not reclassify a clause, split or merge requirements, or invent a replacement. If the linked classifications are mixed or removal would change any other field, return the parent unchanged and fail closed."
            )
        elif code == "unused_executable_requirement":
            rule = (
                f"At {pointer}, preserve this executable requirement and re-check its exact evidence-backed clause binding. Do not delete an executable requirement as a mechanical cleanup."
            )
        elif code == "missing_derived_requirement":
            clause_label = (
                f" for clause_id={record.get('clause_id')}"
                if record.get("clause_id") else ""
            )
            rule = (
                f"At {pointer}{clause_label}, add exactly one evidence-backed requirement for this distinct executable clause if it has no authoritative requirement edge; preserve every existing non-placeholder requirement and review. If several such records are present, satisfy each distinct clause_id separately rather than adding one empty or generic placeholder."
            )
        elif code == "duplicate_evidence_ids":
            rule = (
                f"At {pointer}, remove only repeated evidence IDs while preserving the first occurrence and its order. "
                "Keep the requirement role, properties, clause_ids, all other evidence IDs, classifications, and every other response field unchanged."
            )
        elif code == "partial_clause_coverage":
            rule = (
                f"At {pointer}, preserve every obligation and do not promote partial coverage. "
                "Use a non-executable classification when the supplied evidence does not resolve all obligations."
            )
        elif code == "executable_review_obligations_uncovered":
            rule = (
                f"At {pointer}, do not change any non-covered obligation status to covered merely to satisfy the gate. "
                "Preserve every obligation id and status; if the evidence/backend cannot cover one obligation, "
                "reclassify only that clause to the most accurate non-executable classification and emit no requirement "
                "edge for it. Keep all other reviews, requirements, evidence IDs, properties, and obligations unchanged."
            )
        elif code == "unknown_property":
            rule = (
                f"At {pointer}, remove only the unsupported property named by the schema error; "
                "do not move it, rename it, or invent a replacement."
            )
        elif code == "empty_requirement_properties":
            rule = (
                f"At {pointer}, emit a non-empty role-specific properties object. "
                "field_key alone is not an executable payload; copy exact evidence-backed text into properties.text or emit the declared style/layout property. "
                "If this object has no clause_ids, no evidence_ids, no semantic properties, and an empty reason, it is an unbound provider placeholder: remove only that placeholder rather than filling it or assigning a guessed clause. "
                "For the exact appendix placement wording '附录放在正文之后另起页', use only appendices.page_break_each: true; do not guess other appendix properties."
            )
        elif code == "cover_institution_placeholder":
            rule = (
                f"At {pointer}, this cover chunk has no trusted institution value. "
                f"Replace only the empty cover institution with the neutral placeholder {NEUTRAL_COVER_PLACEHOLDER!r}; "
                "do not copy a school name or change fields, administrative bindings, classifications, or unsupported_items."
            )
        elif code == "non_public_administration_fields_missing":
            rule = (
                f"At {pointer}, the source does not provide a non-empty administrative field list. "
                "Do not invent approval, date, security-marking, embargo, or other fields and do not use an empty fields array as an executable requirement. "
                "Preserve any exact fixed declaration heading/body that the evidence supports. "
                "If the cited source has no explicit field labels, keep only the independently supported fixed declaration requirements, remove the incomplete administrative requirement edge, and classify only the affected administrative obligation as requires_source_content with no requirement relation so it remains a visible manual-review item. "
                "If the source does name exact fields, emit only those exact fields with their source-backed labels and bindings; otherwise fail closed."
            )
        elif code == "contract_validation_error" and "items must be unique" in str(record.get("raw_error") or ""):
            rule = (
                f"At {pointer}, remove only repeated values while preserving the first occurrence and order. "
                "Do not change the cited text, requirement identity, role, clause_ids, or any other semantic field."
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
        elif code == "applicability_fact_namespace":
            rule = (
                f"At {pointer}, replace only an invalid human-language fact with its registered "
                "namespaced source fact. For the explicit English-text presence rule, use "
                "source_inventory.english_text with operator present and value null; preserve "
                "the condition status, exceptions, requirement identity, and all other fields. "
                "Do not invent a key or reinterpret a missing fact as false."
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
        if rule not in seen:
            seen.add(rule)
            lines.append(f"- {rule}")
    return "\n".join(lines) or "- Re-read the current chunk contract and regenerate the complete JSON object."


def _strip_v2_relation_guidance(text: str) -> str:
    """Remove legacy reverse-index prose from a contract-3.0 prompt."""
    kept: list[str] = []
    for line in text.splitlines():
        lowered = line.lower()
        if (
            "requirement_indexes" in lowered
            or "zero-based index" in lowered
            or "review/index pair" in lowered
            or re.search(r"\brequirement indexes?\b", lowered)
        ):
            continue
        kept.append(line)
    return "\n".join(kept)


def _semantic_retry_view(response: Any) -> dict[str, Any] | None:
    """Project the fields whose change is semantic rather than diagnostic."""
    if not isinstance(response, dict):
        return None
    requirements: list[dict[str, Any]] = []
    for item in response.get("requirements", []) if isinstance(response.get("requirements"), list) else []:
        if not isinstance(item, dict):
            requirements.append({"invalid": item})
            continue
        # Keep every requirement field in the semantic projection.  The
        # identity matcher still keys by role/clause/evidence identity, but
        # retry authorization must also see field_key, confidence, reason,
        # custom payload fields, and any future contract additions.
        requirements.append(copy.deepcopy(item))
    reviews: list[dict[str, Any]] = []
    for item in response.get("clause_reviews", []) if isinstance(response.get("clause_reviews"), list) else []:
        if not isinstance(item, dict):
            reviews.append({"invalid": item})
            continue
        reviews.append(copy.deepcopy(item))
    top_level = {
        key: copy.deepcopy(value)
        for key, value in response.items()
        if key not in {
            "requirements", "clause_reviews", "provenance", "contract_version", "unsupported_items",
        }
    }
    return {
        "contract_version": response.get("contract_version"),
        "requirements": requirements,
        "clause_reviews": reviews,
        "unsupported_items": sorted(response.get("unsupported_items") or [])
        if isinstance(response.get("unsupported_items"), list) else response.get("unsupported_items"),
        "top_level": top_level,
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

    # Requirements and clause reviews are semantically keyed collections, not
    # positional lists.  Match them by their authoritative identity before
    # diffing the payload.  This keeps a legal property-only repair tied to the
    # response's real JSON pointer, so validator records such as
    # ``requirements[2]`` cannot be confused with a sorted comparison index.
    def requirement_identity(item: Any) -> str | None:
        if not isinstance(item, dict):
            return None
        identity = {
            key: item.get(key)
            for key in (
                "role", "clause_ids", "evidence_ids", "existing_requirement_id",
            )
            if key in item
        }
        if not identity:
            return None
        return json.dumps(identity, ensure_ascii=False, sort_keys=True)

    def review_identity(item: Any) -> str | None:
        if not isinstance(item, dict) or not item.get("clause_id"):
            return None
        return str(item["clause_id"])

    def keyed_changes(
        left: list[Any], right: list[Any], path: str, identity_fn: Any,
    ) -> None:
        left_map: dict[str, list[Any]] = {}
        right_map: dict[str, list[tuple[int, Any]]] = {}
        for item in left:
            identity = identity_fn(item)
            if identity is None:
                changed.append(path)
                return
            left_map.setdefault(identity, []).append(item)
        for index, item in enumerate(right):
            identity = identity_fn(item)
            if identity is None:
                changed.append(path)
                return
            right_map.setdefault(identity, []).append((index, item))
        if set(left_map) != set(right_map) or any(
            len(left_map[key]) != len(right_map[key]) for key in left_map
        ):
            changed.append(path)
            return
        for identity, right_items in right_map.items():
            for current_index, right_item in right_items:
                left_item = left_map[identity].pop(0)
                visit(left_item, right_item, f"{path}[{current_index}]")

    # Keep the same root ordering used by the old projection for stable error
    # messages: reviews, requirements, then the remaining top-level fields.
    keyed_changes(
        before.get("clause_reviews", []), after.get("clause_reviews", []),
        "$.clause_reviews", review_identity,
    )
    keyed_changes(
        before.get("requirements", []), after.get("requirements", []),
        "$.requirements", requirement_identity,
    )
    visit(before.get("contract_version"), after.get("contract_version"), "$.contract_version")
    visit(before.get("unsupported_items"), after.get("unsupported_items"), "$.unsupported_items")
    visit(before.get("top_level"), after.get("top_level"), "$.top_level")
    return changed


def _v3_duplicate_evidence_ids_allowed(
    previous_response: Any,
    current_response: Any,
    records: list[dict[str, Any]],
    changed_paths: list[str],
) -> bool:
    """Allow only first-occurrence deduplication of requirement evidence IDs.

    Evidence references are a set-valued relation at validation time, but the
    response schema carries them as an ordered list. A retry may remove a
    repeated ID after the validator reports it; it may not reorder evidence,
    alter its set, or change any neighboring semantic field.
    """
    if not isinstance(previous_response, dict) or not isinstance(current_response, dict):
        return False
    codes = {str(item.get("code")) for item in records if isinstance(item, dict)}
    if codes != {"duplicate_evidence_ids"} or changed_paths != ["$.requirements"]:
        return False
    previous_view = _semantic_retry_view(previous_response)
    current_view = _semantic_retry_view(current_response)
    if previous_view is None or current_view is None:
        return False
    for key in ("contract_version", "clause_reviews", "unsupported_items", "top_level"):
        if previous_view.get(key) != current_view.get(key):
            return False
    previous_requirements = previous_view.get("requirements")
    current_requirements = current_view.get("requirements")
    if not isinstance(previous_requirements, list) or not isinstance(current_requirements, list):
        return False
    if len(previous_requirements) != len(current_requirements):
        return False
    changed = False
    for previous, current in zip(previous_requirements, current_requirements):
        if not isinstance(previous, dict) or not isinstance(current, dict):
            return False
        previous_without_evidence = copy.deepcopy(previous)
        current_without_evidence = copy.deepcopy(current)
        previous_ids = previous_without_evidence.pop("evidence_ids", None)
        current_ids = current_without_evidence.pop("evidence_ids", None)
        if previous_without_evidence != current_without_evidence:
            return False
        if not isinstance(previous_ids, list) or not isinstance(current_ids, list):
            return False
        deduplicated: list[Any] = []
        for evidence_id in previous_ids:
            if evidence_id not in deduplicated:
                deduplicated.append(evidence_id)
        if len(deduplicated) != len(previous_ids):
            changed = True
        if current_ids != deduplicated:
            return False
    return changed


def _retry_changes_allowed(
    records: list[dict[str, Any]], changed_paths: list[str], *, contract_version: str,
    previous_response: Any = None, current_response: Any = None,
    chunk: dict[str, Any] | None = None,
) -> bool:
    """Allow only explicitly mechanical contract corrections on a retry."""
    if not changed_paths:
        return True
    codes = {str(item.get("code")) for item in records if isinstance(item, dict)}
    empty_payload_repair = "empty_requirement_properties" in codes
    unknown_payload_repair = "unknown_property" in codes
    cover_placeholder_repair = "cover_institution_placeholder" in codes
    cover_binding_repair = "cover_binding_violation" in codes
    source_evidence_dedup_repair = any(
        isinstance(item, dict)
        and item.get("code") == "contract_validation_error"
        and "items must be unique" in str(item.get("raw_error") or "")
        and re.fullmatch(
            r"\$\.requirements\[\d+\]\.properties\.items\[\d+\]\.source_evidence_ids",
            str(item.get("json_pointer") or ""),
        )
        for item in records
    )
    payload_repair = (
        empty_payload_repair
        or unknown_payload_repair
        or cover_placeholder_repair
        or cover_binding_repair
        or source_evidence_dedup_repair
    )
    previous_view = _semantic_retry_view(previous_response) if payload_repair else None
    current_view = _semantic_retry_view(current_response) if payload_repair else None
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
    unknown_property_paths: set[str] = set()
    empty_payload_indexes: set[int] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        code = str(record.get("code") or "")
        pointer = str(record.get("json_pointer") or "")
        if code == "empty_requirement_properties":
            match = re.fullmatch(r"\$\.requirements\[(\d+)\]\.properties", pointer)
            if match:
                empty_payload_indexes.add(int(match.group(1)))
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
        elif code == "unknown_property":
            property_match = re.search(
                r"unknown property ['\"]([^'\"]+)['\"]",
                str(record.get("raw_error") or ""),
            )
            if property_match and pointer:
                unknown_property_paths.add(f"{pointer}.{property_match.group(1)}")

    def under(prefix: str, path: str) -> bool:
        return path == prefix or path.startswith(prefix + ".") or path.startswith(prefix + "[")

    if (
        contract_version == HOST_REVIEW_CONTRACT_V3
        and _v3_cover_contract_completion_allowed(
            previous_response, current_response, records, changed_paths, chunk=chunk,
        )
    ):
        return True

    if (
        contract_version == HOST_REVIEW_CONTRACT_V3
        and _v3_incomplete_completion_allowed(
            previous_response, current_response, records, changed_paths, chunk=chunk,
        )
    ):
        return True

    if (
        contract_version == HOST_REVIEW_CONTRACT_V3
        and _v3_clause_review_completion_allowed(
            previous_response, current_response, records, changed_paths, chunk=chunk,
        )
    ):
        return True

    if (
        contract_version == HOST_REVIEW_CONTRACT_V3
        and _v3_duplicate_evidence_ids_allowed(
            previous_response, current_response, records, changed_paths,
        )
    ):
        return True

    if (
        contract_version == HOST_REVIEW_CONTRACT_V3
        and _v3_informational_projection_allowed(
            previous_response, current_response, records, changed_paths, chunk=chunk,
        )
    ):
        return True

    if (
        contract_version == HOST_REVIEW_CONTRACT_V3
        and _v3_non_requirement_projection_allowed(
            previous_response, current_response, records, changed_paths, chunk=chunk,
        )
    ):
        return True

    if (
        contract_version == HOST_REVIEW_CONTRACT_V3
        and _v3_non_requirement_projection_with_mechanical_repairs_allowed(
            previous_response, current_response, records, changed_paths, chunk=chunk,
        )
    ):
        return True

    if (
        contract_version == HOST_REVIEW_CONTRACT_V3
        and _v3_fixed_declaration_completion_allowed(
            previous_response, current_response, records, changed_paths, chunk=chunk,
        )
    ):
        return True

    if (
        contract_version == HOST_REVIEW_CONTRACT_V3
        and _v3_uncovered_obligation_reclassification_allowed(
            previous_response, current_response, records, chunk=chunk,
        )
    ):
        return True

    if (
        contract_version == HOST_REVIEW_CONTRACT_V3
        and bool(codes & {"requirement_relation_mismatch", "missing_derived_requirement"})
        and _v3_relation_completion_response(
            previous_response, current_response, records, chunk=chunk,
        )[0] is not None
    ):
        return True

    if (
        contract_version == HOST_REVIEW_CONTRACT_V3
        and bool(codes & {"requirement_relation_mismatch", "missing_derived_requirement"})
        and _v3_relation_addition_allowed(
            previous_response, current_response, records, changed_paths,
        )
    ):
        return True

    for path in changed_paths:
        if cover_placeholder_repair:
            match = re.fullmatch(
                r"\$\.requirements\[(\d+)\]\.properties\.institution", path,
            )
            if match:
                index = int(match.group(1))
                before = (
                    previous_requirements[index]
                    if index < len(previous_requirements)
                    else None
                )
                after = (
                    current_requirements[index]
                    if index < len(current_requirements)
                    else None
                )
                if isinstance(before, dict) and isinstance(after, dict):
                    before_properties = before.get("properties")
                    after_properties = after.get("properties")
                    before_rest = copy.deepcopy(before)
                    after_rest = copy.deepcopy(after)
                    before_rest.pop("properties", None)
                    after_rest.pop("properties", None)
                    if isinstance(before_properties, dict) and isinstance(after_properties, dict):
                        before_institution = before_properties.get("institution")
                        after_institution = after_properties.get("institution")
                        before_properties = copy.deepcopy(before_properties)
                        after_properties = copy.deepcopy(after_properties)
                        before_properties.pop("institution", None)
                        after_properties.pop("institution", None)
                        if (
                            before_rest == after_rest
                            and before_properties == after_properties
                            and before_institution in ("", None)
                            and after_institution == NEUTRAL_COVER_PLACEHOLDER
                        ):
                            continue
        if source_evidence_dedup_repair:
            match = re.fullmatch(
                r"\$\.requirements\[(\d+)\]\.properties\.items\[(\d+)\]\.source_evidence_ids",
                path,
            )
            if match:
                requirement_index = int(match.group(1))
                item_index = int(match.group(2))
                before_requirement = (
                    previous_requirements[requirement_index]
                    if requirement_index < len(previous_requirements)
                    else None
                )
                after_requirement = (
                    current_requirements[requirement_index]
                    if requirement_index < len(current_requirements)
                    else None
                )
                if isinstance(before_requirement, dict) and isinstance(after_requirement, dict):
                    before_properties = before_requirement.get("properties")
                    after_properties = after_requirement.get("properties")
                    before_items = (
                        before_properties.get("items")
                        if isinstance(before_properties, dict)
                        else None
                    )
                    after_items = (
                        after_properties.get("items")
                        if isinstance(after_properties, dict)
                        else None
                    )
                    if (
                        isinstance(before_items, list)
                        and isinstance(after_items, list)
                        and item_index < len(before_items)
                        and item_index < len(after_items)
                    ):
                        before_item = before_items[item_index]
                        after_item = after_items[item_index]
                        if isinstance(before_item, dict) and isinstance(after_item, dict):
                            before_ids = before_item.get("source_evidence_ids")
                            after_ids = after_item.get("source_evidence_ids")
                            before_item_rest = copy.deepcopy(before_item)
                            after_item_rest = copy.deepcopy(after_item)
                            before_item_rest.pop("source_evidence_ids", None)
                            after_item_rest.pop("source_evidence_ids", None)
                            deduplicated: list[Any] = []
                            if isinstance(before_ids, list):
                                for evidence_id in before_ids:
                                    if evidence_id not in deduplicated:
                                        deduplicated.append(evidence_id)
                            if (
                                before_item_rest == after_item_rest
                                and isinstance(before_ids, list)
                                and isinstance(after_ids, list)
                                and after_ids == deduplicated
                                and len(after_ids) < len(before_ids)
                            ):
                                continue
        if path.endswith(".reason") and empty_payload_repair:
            match = re.fullmatch(r"\$\.requirements\[(\d+)\]\.reason", path)
            if match and int(match.group(1)) in empty_payload_indexes:
                index = int(match.group(1))
                before = (
                    previous_requirements[index]
                    if index < len(previous_requirements)
                    else None
                )
                after = (
                    current_requirements[index]
                    if index < len(current_requirements)
                    else None
                )
                if isinstance(before, dict) and isinstance(after, dict):
                    before_properties = before.get("properties")
                    after_properties = after.get("properties")
                    before_rest = copy.deepcopy(before)
                    after_rest = copy.deepcopy(after)
                    before_rest.pop("properties", None)
                    after_rest.pop("properties", None)
                    before_rest.pop("reason", None)
                    after_rest.pop("reason", None)
                    if (
                        before_properties == {}
                        and isinstance(after_properties, dict)
                        and after_properties
                        and before_rest == after_rest
                    ):
                        # ``reason`` is diagnostic prose, not an execution
                        # binding.  It may be regenerated together with a
                        # bounded empty-payload fill; roles, relations,
                        # evidence, and properties remain checked below.
                        continue
        if path.endswith(".reason") and "cover_binding_violation" in codes:
            match = re.fullmatch(r"\$\.clause_reviews\[(\d+)\]\.reason", path)
            if match:
                review_index = int(match.group(1))
                previous_reviews = previous_response.get("clause_reviews")
                current_reviews = current_response.get("clause_reviews")
                cover_indexes = {
                    int(index_match.group(1))
                    for prefix in cover_property_prefixes
                    if (index_match := re.match(
                        r"^\$\.requirements\[(\d+)\]\.properties", prefix,
                    )) is not None
                }
                if (
                    isinstance(previous_reviews, list)
                    and isinstance(current_reviews, list)
                    and review_index < len(previous_reviews)
                    and review_index < len(current_reviews)
                    and isinstance(previous_reviews[review_index], dict)
                    and isinstance(current_reviews[review_index], dict)
                ):
                    previous_review = copy.deepcopy(previous_reviews[review_index])
                    current_review = copy.deepcopy(current_reviews[review_index])
                    previous_reason = previous_review.pop("reason", None)
                    current_reason = current_review.pop("reason", None)
                    clause_id = str(previous_review.get("clause_id") or "")
                    linked_to_cover = any(
                        0 <= index < len(previous_requirements)
                        and isinstance(previous_requirements[index], dict)
                        and clause_id in {
                            str(value)
                            for value in (previous_requirements[index].get("clause_ids") or [])
                        }
                        for index in cover_indexes
                    )
                    if (
                        previous_reason != current_reason
                        and current_review == previous_review
                        and str(current_reason or "").strip()
                        and linked_to_cover
                    ):
                        continue
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
        if unknown_payload_repair and chunk is not None and path.endswith(
            ".properties.text"
        ):
            # ``run_host_agent_chunk`` may first delete one or more
            # validator-named unknown properties and then fill the now-empty
            # text-capable role with the one exact cited evidence string.
            # The retry audit compares the raw parent with the accepted,
            # mechanically repaired child, so this paired text addition must
            # be admitted only when the complete before/after payload proves
            # that no semantic value was invented.
            match = re.fullmatch(
                r"\$\.requirements\[(\d+)\]\.properties\.text", path,
            )
            if match:
                index = int(match.group(1))
                root = f"$.requirements[{index}].properties"
                removed_names = {
                    unknown_path.removeprefix(root + ".")
                    for unknown_path in unknown_property_paths
                    if unknown_path.startswith(root + ".")
                }
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
                current_requirement = (
                    current_requirements[index]
                    if index < len(current_requirements)
                    else None
                )
                exact_text = _exact_cited_text(current_requirement, chunk)
                if (
                    removed_names
                    and isinstance(before, dict)
                    and set(before) == removed_names
                    and isinstance(after, dict)
                    and exact_text is not None
                    and after == {"text": exact_text}
                ):
                    continue
        if "fixed_text_evidence_mismatch" in codes and (
            ".body_parts" in path or ".heading" in path
        ):
            continue
        if path in unknown_property_paths:
            match = re.fullmatch(
                r"\$\.requirements\[(\d+)\]\.properties\.([^\.\[]+)", path,
            )
            if match:
                index = int(match.group(1))
                property_name = match.group(2)
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
                if (
                    isinstance(before, dict)
                    and property_name in before
                    and isinstance(after, dict)
                    and property_name not in after
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


def _role_supports_exact_text(requirement: Any, chunk: dict[str, Any]) -> bool:
    if not isinstance(requirement, dict):
        return False
    role = requirement.get("role")
    contract = chunk.get("requirement_contract")
    if not isinstance(role, str) or not isinstance(contract, dict):
        return False
    role_schemas = contract.get("role_properties_schema")
    if not isinstance(role_schemas, dict):
        return False
    role_schema = role_schemas.get(role)
    if not isinstance(role_schema, dict):
        return False
    if role_schema.get("$ref") == "#/$defs/roleSpec":
        return True
    properties = role_schema.get("properties")
    return isinstance(properties, dict) and "text" in properties


def _exact_cited_text(requirement: Any, chunk: dict[str, Any]) -> str | None:
    if not isinstance(requirement, dict) or not _role_supports_exact_text(requirement, chunk):
        return None
    evidence_ids = requirement.get("evidence_ids")
    evidence_context = chunk.get("evidence_context")
    if not isinstance(evidence_ids, list) or not isinstance(evidence_context, dict):
        return None
    values = sorted({
        str(evidence_context.get(str(evidence_id), {}).get("text"))
        for evidence_id in evidence_ids
        if isinstance(evidence_context.get(str(evidence_id)), dict)
        and str(evidence_context[str(evidence_id)].get("text") or "").strip()
    })
    return values[0] if len(values) == 1 else None


def _v3_incomplete_completion_allowed(
    previous_response: Any,
    current_response: Any,
    records: list[dict[str, Any]],
    changed_paths: list[str],
    *,
    chunk: dict[str, Any] | None = None,
) -> bool:
    """Allow a retry to complete an otherwise truncated v3 response.

    This is narrower than a general semantic retry: the first response must
    contain no clause reviews, the second response must pass the complete
    current-chunk validator, every first-attempt requirement must survive with
    only an exact evidence-derived text fill allowed, and the remaining
    requirements/reviews must be the missing completion.  Classification,
    obligations, and existing non-empty properties are never rewritten here.
    """
    if chunk is None:
        return False
    if not isinstance(previous_response, dict) or not isinstance(current_response, dict):
        return False
    previous_requirements = previous_response.get("requirements")
    current_requirements = current_response.get("requirements")
    previous_reviews = previous_response.get("clause_reviews")
    current_reviews = current_response.get("clause_reviews")
    if not all(isinstance(value, list) for value in (
        previous_requirements, current_requirements, previous_reviews, current_reviews,
    )):
        return False
    if previous_reviews or not current_reviews or len(current_requirements) < len(previous_requirements):
        return False
    codes = {str(item.get("code")) for item in records if isinstance(item, dict)}
    if not codes <= {
        "empty_requirement_properties",
        "contract_validation_error",
        "fixed_text_evidence_mismatch",
        "missing_derived_requirement",
        "missing_clause_review",
        "requirement_relation_mismatch",
        "schema_contract_violation",
        "unknown_property",
    }:
        return False
    if not any(
        "clause_reviews_must_cover_each_chunk_clause_exactly_once" in str(item.get("raw_error") or "")
        for item in records if isinstance(item, dict)
    ):
        return False
    if validate_host_agent_response(current_response, chunk):
        return False

    def unsupported_items_match_reviews(response: dict[str, Any]) -> bool:
        """Accept only diagnostic unsupported items tied to current reviews."""
        items = response.get("unsupported_items")
        reviews = response.get("clause_reviews")
        if not isinstance(items, list) or not isinstance(reviews, list):
            return False
        item_clause_ids: list[str] = []
        for item in items:
            if not isinstance(item, str):
                return False
            match = re.match(r"^\s*([A-Za-z]+\d+)\s*[:：]", item)
            if match is None:
                return False
            item_clause_ids.append(match.group(1))
        if len(item_clause_ids) != len(set(item_clause_ids)):
            return False
        expected_clause_ids = [
            str(review.get("clause_id"))
            for review in reviews
            if isinstance(review, dict)
            and review.get("classification") in {"unsupported", "unsupported_backend"}
            and review.get("clause_id")
        ]
        return sorted(item_clause_ids) == sorted(expected_clause_ids)

    unsupported_items_changed = "$.unsupported_items" in changed_paths
    if unsupported_items_changed:
        if previous_response.get("unsupported_items") not in (None, []):
            return False
        if not unsupported_items_match_reviews(current_response):
            return False
    elif previous_response.get("unsupported_items") != current_response.get("unsupported_items"):
        return False

    def has_meaningful_value(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, list):
            return any(has_meaningful_value(item) for item in value)
        if isinstance(value, dict):
            return any(has_meaningful_value(item) for item in value.values())
        return True

    unknown_placeholder_properties: dict[int, set[str]] = {}
    for record in records:
        if not isinstance(record, dict) or record.get("code") != "unknown_property":
            continue
        pointer = str(record.get("json_pointer") or "")
        if not pointer:
            pointer_match = re.search(
                r"(\$\.requirements\[\d+\]\.properties)",
                str(record.get("raw_error") or ""),
            )
            pointer = pointer_match.group(1) if pointer_match else ""
        match = re.fullmatch(r"\$\.requirements\[(\d+)\]\.properties", pointer)
        property_match = re.search(
            r"unknown property ['\"]([^'\"]+)['\"]",
            str(record.get("raw_error") or ""),
        )
        if match is not None and property_match is not None:
            unknown_placeholder_properties.setdefault(int(match.group(1)), set()).add(
                property_match.group(1)
            )

    def is_unbound_placeholder(requirement: Any, index: int) -> bool:
        if not isinstance(requirement, dict):
            return False
        properties = copy.deepcopy(requirement.get("properties"))
        if isinstance(properties, dict):
            for property_name in unknown_placeholder_properties.get(index, set()):
                properties.pop(property_name, None)
        return (
            isinstance(properties, dict)
            and not has_meaningful_value(properties)
            and not has_meaningful_value(requirement.get("clause_ids"))
            and not has_meaningful_value(requirement.get("evidence_ids"))
            and not has_meaningful_value(requirement.get("existing_requirement_id"))
            and not has_meaningful_value(requirement.get("field_key"))
            and not has_meaningful_value(requirement.get("reason"))
            and requirement.get("confidence") in (None, 0, 0.0)
            and not has_meaningful_value(requirement.get("applicability"))
            and not has_meaningful_value(requirement.get("input_prerequisites"))
            and not has_meaningful_value(requirement.get("verification"))
        )

    placeholder_indexes = {
        index for index, requirement in enumerate(previous_requirements)
        if is_unbound_placeholder(requirement, index)
    }
    fixed_text_indexes = {
        int(match.group(1))
        for record in records
        if isinstance(record, dict)
        and record.get("code") == "fixed_text_evidence_mismatch"
        for match in [re.match(
            r"^\$\.requirements\[(\d+)\]\.properties(?:\.|$)",
            str(record.get("json_pointer") or ""),
        )]
        if match is not None
    }
    placeholder_related_codes = {
        "contract_validation_error", "empty_requirement_properties",
        "requirement_relation_mismatch", "schema_contract_violation",
        "unknown_property",
    }
    for record in records:
        if not isinstance(record, dict):
            return False
        pointer = str(record.get("json_pointer") or "")
        match = re.match(r"^\$\.requirements\[(\d+)\]", pointer)
        if match is None:
            continue
        index = int(match.group(1))
        if index in placeholder_indexes and record.get("code") not in placeholder_related_codes:
            return False
    unbound_placeholder = bool(placeholder_indexes)
    root_completion = (
        (
            set(changed_paths) == {"$.clause_reviews", "$.requirements"}
            or set(changed_paths) == {
                "$.clause_reviews", "$.requirements", "$.unsupported_items",
            }
        )
        and len(changed_paths) == len(set(changed_paths))
    )
    placeholder_replacement = (
        unbound_placeholder
        and "$.clause_reviews" in changed_paths
        and all(
            path.startswith("$.requirements[0].")
            for path in changed_paths
            if path != "$.clause_reviews"
        )
    )
    if not root_completion and not placeholder_replacement:
        return False
    if len(current_requirements) == len(previous_requirements) and not unbound_placeholder:
        return False

    # Match every initial requirement by its authoritative identity.  The
    # only tolerated difference is filling an entirely empty role payload
    # with the one exact cited evidence text.
    remaining = [copy.deepcopy(item) for item in current_requirements]
    identity_keys = ("role", "clause_ids", "evidence_ids", "existing_requirement_id")
    for previous_index, previous in enumerate(previous_requirements):
        if not isinstance(previous, dict):
            return False
        if previous_index in placeholder_indexes:
            # An entirely unbound provider placeholder is not a semantic
            # requirement.  The completion retry may remove it, but only
            # when every validator record pointing at it is one of the
            # explicitly mechanical placeholder errors checked above.
            continue
        match_index = next(
            (
                index for index, candidate in enumerate(remaining)
                if isinstance(candidate, dict)
                and all(candidate.get(key) == previous.get(key) for key in identity_keys)
            ),
            None,
        )
        if match_index is None:
            if (
                unbound_placeholder
                and previous.get("clause_ids") == []
                and previous.get("evidence_ids") == []
                and not str(previous.get("reason") or "").strip()
                and not previous.get("existing_requirement_id")
            ):
                continue
            return False
        current = remaining.pop(match_index)
        previous_properties = previous.get("properties")
        current_properties = current.get("properties")
        if previous_properties == current_properties:
            continue
        fixed_text_repair = _v3_fixed_text_requirement_change_allowed(
            previous, current, records, chunk=chunk,
        ) if previous_index in fixed_text_indexes else False
        exact_text_repair = (
            previous_properties == {}
            and current_properties == {"text": _exact_cited_text(current, chunk)}
        )
        if not fixed_text_repair and not exact_text_repair:
            return False
    return bool(remaining)


def _v3_clause_review_completion_allowed(
    previous_response: Any,
    current_response: Any,
    records: list[dict[str, Any]],
    changed_paths: list[str],
    *,
    chunk: dict[str, Any] | None = None,
) -> bool:
    """Allow only validator-directed addition of missing clause reviews.

    A v3 response may already contain all requirements but omit one clause
    review. The retry may add exactly the clause IDs named by the validator,
    while preserving every existing review and requirement byte-for-byte. This
    does not permit reclassification, reordering, or changing an existing
    review; the current response must pass the complete local validator first.
    """
    if (
        chunk is None
        or not isinstance(previous_response, dict)
        or not isinstance(current_response, dict)
        or previous_response.get("contract_version") != HOST_REVIEW_CONTRACT_V3
        or changed_paths != ["$.clause_reviews"]
    ):
        return False
    if validate_host_agent_response(current_response, chunk):
        return False
    if not records or any(
        not isinstance(record, dict)
        or record.get("code") not in {"missing_clause_review", "contract_validation_error"}
        for record in records
    ):
        return False
    missing_records = [
        record for record in records
        if isinstance(record, dict) and record.get("code") == "missing_clause_review"
    ]
    if not missing_records or not any(
        isinstance(record, dict)
        and record.get("code") == "contract_validation_error"
        and record.get("raw_error") == "clause_reviews_must_cover_each_chunk_clause_exactly_once"
        for record in records
    ):
        return False

    previous_reviews = previous_response.get("clause_reviews")
    current_reviews = current_response.get("clause_reviews")
    previous_requirements = previous_response.get("requirements")
    current_requirements = current_response.get("requirements")
    if not all(isinstance(value, list) for value in (
        previous_reviews, current_reviews, previous_requirements, current_requirements,
    )):
        return False
    if previous_requirements != current_requirements:
        return False
    for key in set(previous_response) | set(current_response):
        if key in {"provenance", "clause_reviews"}:
            continue
        if previous_response.get(key) != current_response.get(key):
            return False

    clause_ids = [
        str(clause.get("id"))
        for clause in chunk.get("clauses", [])
        if isinstance(clause, dict) and clause.get("id")
    ]
    if not clause_ids or len(clause_ids) != len(set(clause_ids)):
        return False
    missing_clause_ids: set[str] = set()
    for record in missing_records:
        pointer = str(record.get("json_pointer") or "")
        if re.fullmatch(r"\$\.requirements\[\d+\]", pointer) is None:
            return False
        raw_error = str(record.get("raw_error") or "")
        match = re.fullmatch(
            r"\$\.requirements\[\d+\]:missing_clause_review:clause_ids=([^:]+)",
            raw_error,
        )
        if match is None:
            return False
        values = {value for value in match.group(1).split(",") if value}
        if not values or not values <= set(clause_ids):
            return False
        missing_clause_ids.update(values)
    if not missing_clause_ids:
        return False

    def review_id(review: Any) -> str | None:
        return (
            str(review.get("clause_id"))
            if isinstance(review, dict) and review.get("clause_id")
            else None
        )

    previous_ids = [review_id(review) for review in previous_reviews]
    current_ids = [review_id(review) for review in current_reviews]
    previous_id_set = {value for value in previous_ids if value is not None}
    if (
        any(value is None for value in previous_ids)
        or any(value is None for value in current_ids)
        or len(set(previous_ids)) != len(previous_ids)
        or len(set(current_ids)) != len(current_ids)
        or current_ids != clause_ids
        or len(current_reviews) != len(previous_reviews) + len(missing_clause_ids)
        or set(current_ids) - previous_id_set != missing_clause_ids
    ):
        return False
    if [
        review for review in current_reviews
        if review_id(review) in previous_id_set
    ] != previous_reviews:
        return False

    # Adding a review is safe here only when the corresponding requirement
    # already exists. Adding a requirement is a separate semantic repair.
    requirement_clause_ids = {
        str(clause_id)
        for requirement in previous_requirements
        if isinstance(requirement, dict)
        for clause_id in (requirement.get("clause_ids") or [])
    }
    return missing_clause_ids <= requirement_clause_ids


def _v3_fixed_declaration_completion_allowed(
    previous_response: Any,
    current_response: Any,
    records: list[dict[str, Any]],
    changed_paths: list[str],
    *,
    chunk: dict[str, Any] | None = None,
) -> bool:
    """Allow only evidence-candidate completion of a missing declaration.

    A native response can contain an unbound role-schema placeholder while
    omitting a declaration requirement whose exact heading/body grouping was
    already derived from the current source packet.  The retry is safe only
    when it removes that placeholder, adds exactly the deterministic fixed
    declaration candidate, preserves every other requirement, and changes at
    most diagnostic ``reason`` text for the candidate's reviews.  This is not
    a general semantic retry permission.
    """
    if (
        chunk is None
        or not isinstance(previous_response, dict)
        or not isinstance(current_response, dict)
        or changed_paths.count("$.requirements") != 1
        or not isinstance(chunk.get("clauses"), list)
        or not isinstance(chunk.get("evidence_context"), dict)
    ):
        return False
    codes = {str(item.get("code")) for item in records if isinstance(item, dict)}
    if "missing_derived_requirement" not in codes:
        return False
    if not codes <= {
        "contract_validation_error", "schema_contract_violation",
        "cover_binding_violation", "empty_requirement_properties",
        "missing_derived_requirement", "requirement_relation_mismatch",
    }:
        return False
    if validate_host_agent_response(current_response, chunk):
        return False

    candidates = _fixed_declaration_candidates(
        chunk.get("clauses"), chunk.get("evidence_context", {}),
        anchor=chunk.get("declaration_anchor_preference"),
    )
    if not candidates:
        return False
    previous_requirements = previous_response.get("requirements")
    current_requirements = current_response.get("requirements")
    previous_reviews = previous_response.get("clause_reviews")
    current_reviews = current_response.get("clause_reviews")
    if not all(isinstance(value, list) for value in (
        previous_requirements, current_requirements, previous_reviews, current_reviews,
    )):
        return False

    def is_empty_value(value: Any) -> bool:
        return value is None or value == "" or value == [] or value == {}

    def is_unbound_placeholder(requirement: Any) -> bool:
        if not isinstance(requirement, dict):
            return False
        properties = requirement.get("properties")
        if not isinstance(properties, dict) or not all(
            is_empty_value(value) for value in properties.values()
        ):
            return False
        return (
            is_empty_value(requirement.get("clause_ids"))
            and is_empty_value(requirement.get("evidence_ids"))
            and is_empty_value(requirement.get("existing_requirement_id"))
            and is_empty_value(requirement.get("field_key"))
            and is_empty_value(requirement.get("reason"))
            and requirement.get("confidence") in (None, 0, 0.0)
            and is_empty_value(requirement.get("applicability"))
            and is_empty_value(requirement.get("input_prerequisites"))
            and is_empty_value(requirement.get("verification"))
        )

    placeholder_indexes = {
        index for index, requirement in enumerate(previous_requirements)
        if is_unbound_placeholder(requirement)
    }
    if not placeholder_indexes:
        return False

    candidate_by_clause_set = {
        frozenset(str(value) for value in candidate.get("clause_ids", [])): candidate
        for candidate in candidates
    }
    declaration_indexes = [
        index for index, requirement in enumerate(current_requirements)
        if isinstance(requirement, dict)
        and requirement.get("role") == "declarations"
        and frozenset(str(value) for value in (requirement.get("clause_ids") or []))
        in candidate_by_clause_set
    ]
    if len(declaration_indexes) != 1:
        return False
    declaration_index = declaration_indexes[0]
    declaration = current_requirements[declaration_index]
    candidate = candidate_by_clause_set[frozenset(
        str(value) for value in declaration.get("clause_ids", [])
    )]
    preserved_requirements = [
        copy.deepcopy(requirement)
        for index, requirement in enumerate(previous_requirements)
        if index not in placeholder_indexes
    ]
    current_without_declaration = [
        copy.deepcopy(requirement)
        for index, requirement in enumerate(current_requirements)
        if index != declaration_index
    ]
    if current_without_declaration != preserved_requirements:
        return False
    if declaration.get("existing_requirement_id"):
        return False
    if declaration.get("clause_ids") != list(candidate.get("clause_ids", [])):
        return False
    if declaration.get("evidence_ids") != list(candidate.get("evidence_ids", [])):
        return False
    properties = declaration.get("properties")
    if not isinstance(properties, dict) or properties.get("before_role") != candidate.get("before_role"):
        return False
    items = properties.get("items")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        return False
    item = items[0]
    evidence_context = chunk.get("evidence_context", {})
    heading_ids = [str(value) for value in candidate.get("heading_evidence_ids", [])]
    body_ids = [str(value) for value in candidate.get("body_evidence_ids", [])]
    heading_texts = [
        str(evidence_context[evidence_id].get("text") or "")
        for evidence_id in heading_ids
        if isinstance(evidence_context.get(evidence_id), dict)
    ]
    expected_body_parts: list[str] = []
    seen_body_ids: set[str] = set()
    for evidence_id in body_ids:
        if evidence_id in seen_body_ids:
            continue
        seen_body_ids.add(evidence_id)
        evidence = evidence_context.get(evidence_id)
        if not isinstance(evidence, dict):
            return False
        expected_body_parts.append(str(evidence.get("text") or ""))
    if (
        len(heading_texts) != 1
        or item.get("heading") != heading_texts[0]
        or item.get("body_parts") != expected_body_parts
        or item.get("source_evidence_ids") != list(candidate.get("evidence_ids", []))
    ):
        return False

    candidate_clause_ids = set(map(str, candidate.get("clause_ids", [])))
    if len(previous_reviews) != len(current_reviews):
        return False
    previous_review_ids = [
        str(review.get("clause_id")) for review in previous_reviews
        if isinstance(review, dict) and review.get("clause_id")
    ]
    current_review_ids = [
        str(review.get("clause_id")) for review in current_reviews
        if isinstance(review, dict) and review.get("clause_id")
    ]
    if previous_review_ids != current_review_ids:
        return False
    for previous_review, current_review in zip(previous_reviews, current_reviews):
        if not isinstance(previous_review, dict) or not isinstance(current_review, dict):
            return False
        clause_id = str(previous_review.get("clause_id") or "")
        previous_without_reason = copy.deepcopy(previous_review)
        current_without_reason = copy.deepcopy(current_review)
        previous_reason = previous_without_reason.pop("reason", None)
        current_reason = current_without_reason.pop("reason", None)
        if previous_without_reason != current_without_reason:
            return False
        if previous_reason != current_reason and clause_id not in candidate_clause_ids:
            return False

    for path in changed_paths:
        if path == "$.requirements":
            continue
        match = re.fullmatch(r"\$\.clause_reviews\[(\d+)\]\.reason", path)
        if match is None:
            return False
        index = int(match.group(1))
        if index >= len(current_reviews):
            return False
        if str(current_reviews[index].get("clause_id")) not in candidate_clause_ids:
            return False

    missing_candidate_review = False
    for record in records:
        if not isinstance(record, dict) or record.get("code") != "missing_derived_requirement":
            continue
        match = re.fullmatch(r"\$\.clause_reviews\[(\d+)\]", str(record.get("json_pointer") or ""))
        if match is None:
            return False
        index = int(match.group(1))
        if index >= len(previous_reviews):
            return False
        review = previous_reviews[index]
        if not isinstance(review, dict) or str(review.get("clause_id")) not in candidate_clause_ids:
            return False
        if review.get("classification") not in {"covered", "executable", "verify_existing"}:
            return False
        missing_candidate_review = True
    return missing_candidate_review


def _v3_cover_contract_completion_allowed(
    previous_response: Any,
    current_response: Any,
    records: list[dict[str, Any]],
    changed_paths: list[str],
    *,
    chunk: dict[str, Any] | None = None,
) -> bool:
    """Allow schema-directed cover completion after a role-branch mismatch.

    Native unions can make a ``cover`` requirement resemble a different
    object branch.  A retry may then replace the invalid cover payload with a
    schema-valid one.  This is accepted only when the local validator reports
    a cover/schema error, all requirement identities and clause-review
    semantics stay unchanged, and every non-cover change is the exact
    evidence-text fill handled by the mechanical payload rule. A reason-only
    update on a clause tied to the repaired cover is diagnostic metadata and
    is allowed; classifications, obligations, evidence, and relations remain
    byte-for-byte protected.
    """
    if chunk is None or not isinstance(previous_response, dict) or not isinstance(current_response, dict):
        return False
    if not changed_paths:
        return False
    codes = {str(item.get("code")) for item in records if isinstance(item, dict)}
    if "cover_binding_violation" not in codes:
        return False
    if not codes <= {
        "contract_validation_error",
        "schema_contract_violation",
        "unknown_property",
        "cover_binding_violation",
        "empty_requirement_properties",
    }:
        return False
    if validate_host_agent_response(current_response, chunk):
        return False
    previous_view = _semantic_retry_view(previous_response)
    current_view = _semantic_retry_view(current_response)
    if previous_view is None or current_view is None:
        return False
    previous_requirements = previous_view.get("requirements", [])
    current_requirements = current_view.get("requirements", [])
    if len(previous_requirements) != len(current_requirements):
        return False

    changed_cover_indexes = {
        int(match.group(1))
        for path in changed_paths
        if (match := re.fullmatch(
            r"\$\.requirements\[(\d+)\]\.properties(?:\..+)?", path,
        )) is not None
    }
    previous_reviews = previous_response.get("clause_reviews")
    current_reviews = current_response.get("clause_reviews")
    if not isinstance(previous_reviews, list) or not isinstance(current_reviews, list):
        return False
    if len(previous_reviews) != len(current_reviews):
        return False
    for review_index, (previous_review, current_review) in enumerate(
        zip(previous_reviews, current_reviews)
    ):
        if not isinstance(previous_review, dict) or not isinstance(current_review, dict):
            return False
        previous_without_reason = copy.deepcopy(previous_review)
        current_without_reason = copy.deepcopy(current_review)
        previous_reason = previous_without_reason.pop("reason", None)
        current_reason = current_without_reason.pop("reason", None)
        if previous_without_reason != current_without_reason:
            return False
        if previous_reason == current_reason:
            continue
        reason_path = f"$.clause_reviews[{review_index}].reason"
        if reason_path not in changed_paths or not str(current_reason or "").strip():
            return False
        clause_id = str(previous_review.get("clause_id") or "")
        if not any(
            index in changed_cover_indexes
            and isinstance(previous_requirements[index], dict)
            and clause_id in {
                str(value) for value in (previous_requirements[index].get("clause_ids") or [])
            }
            for index in changed_cover_indexes
            if 0 <= index < len(previous_requirements)
        ):
            return False

    def empty_verification_check_removed(path: str) -> bool:
        match = re.fullmatch(
            r"\$\.requirements\[(\d+)\]\.verification\.checks", path,
        )
        if match is None:
            return False
        index = int(match.group(1))
        if index >= len(previous_requirements) or index >= len(current_requirements):
            return False
        previous_verification = previous_requirements[index].get("verification")
        current_verification = current_requirements[index].get("verification")
        if not isinstance(previous_verification, dict) or not isinstance(current_verification, dict):
            return False
        previous_checks = previous_verification.get("checks")
        current_checks = current_verification.get("checks")
        if not isinstance(previous_checks, list) or not isinstance(current_checks, list):
            return False
        check_record = next(
            (
                record for record in records
                if isinstance(record, dict)
                and record.get("code") == "contract_validation_error"
                and (
                    str(record.get("json_pointer") or "") == path
                    or str(record.get("json_pointer") or "").startswith(path + "[")
                )
                and "is shorter than 1 characters" in str(record.get("raw_error") or "")
            ),
            None,
        )
        if check_record is None or len(previous_checks) != len(current_checks) + 1:
            return False
        empty_indexes = [check_index for check_index, value in enumerate(previous_checks) if value == ""]
        if len(empty_indexes) != 1:
            return False
        empty_index = empty_indexes[0]
        return current_checks == previous_checks[:empty_index] + previous_checks[empty_index + 1:]

    cover_changed = False
    for path in changed_paths:
        match = re.fullmatch(r"\$\.requirements\[(\d+)\]\.properties(?:\.(.+))?", path)
        if match is None:
            if empty_verification_check_removed(path):
                continue
            if re.fullmatch(r"\$\.clause_reviews\[\d+\]\.reason", path):
                continue
            return False
        index = int(match.group(1))
        if index >= len(previous_requirements) or index >= len(current_requirements):
            return False
        previous = previous_requirements[index]
        current = current_requirements[index]
        for key in ("role", "clause_ids", "evidence_ids", "existing_requirement_id"):
            if previous.get(key) != current.get(key):
                return False
        if current.get("role") == "cover":
            cover_changed = True
            continue
        if path != f"$.requirements[{index}].properties.text":
            return False
        if previous.get("properties") != {}:
            return False
        if current.get("properties") != {
            "text": _exact_cited_text(current, chunk),
        }:
            return False
    return cover_changed


def _v3_relation_addition_allowed(
    previous_response: Any,
    current_response: Any,
    records: list[dict[str, Any]],
    changed_paths: list[str],
) -> bool:
    """Allow a retry to add only requirements missing for executable reviews.

    Contract 3.0 makes ``requirements[].clause_ids`` authoritative.  A
    missing relation therefore cannot be repaired by editing a reverse index;
    the model must add the missing, evidence-backed requirement.  This guard
    permits that narrow addition while requiring every prior requirement and
    review to remain byte-for-byte semantically unchanged.
    """
    if not isinstance(previous_response, dict) or not isinstance(current_response, dict):
        return False
    if changed_paths != ["$.requirements"]:
        return False
    previous_requirements = previous_response.get("requirements")
    current_requirements = current_response.get("requirements")
    previous_reviews = previous_response.get("clause_reviews")
    current_reviews = current_response.get("clause_reviews")
    if not all(isinstance(value, list) for value in (
        previous_requirements, current_requirements, previous_reviews, current_reviews,
    )):
        return False
    if len(current_requirements) <= len(previous_requirements) or previous_reviews != current_reviews:
        return False

    remaining = [copy.deepcopy(item) for item in current_requirements]
    for previous in previous_requirements:
        match = next((index for index, candidate in enumerate(remaining) if candidate == previous), None)
        if match is None:
            return False
        remaining.pop(match)
    if not remaining:
        return False

    missing_clause_ids: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or record.get("code") not in {
            "requirement_relation_mismatch", "missing_derived_requirement",
        }:
            continue
        match = re.search(r"\$\.clause_reviews\[(\d+)\]", str(record.get("json_pointer") or ""))
        raw_error = str(record.get("raw_error") or "")
        if match is None:
            legacy_match = re.fullmatch(
                r"requirements_not_referenced_by_clause_review:(\d+)", raw_error,
            )
            if legacy_match is None:
                return False
            requirement_index = int(legacy_match.group(1))
            if requirement_index >= len(previous_requirements):
                return False
            clause_ids = previous_requirements[requirement_index].get("clause_ids")
            if not isinstance(clause_ids, list) or not clause_ids:
                return False
            missing_clause_ids.update(str(value) for value in clause_ids)
            continue
        index = int(match.group(1))
        if index >= len(previous_reviews) or not isinstance(previous_reviews[index], dict):
            return False
        clause_id = previous_reviews[index].get("clause_id")
        if not isinstance(clause_id, str) or not clause_id:
            return False
        if previous_reviews[index].get("classification") not in {
            "covered", "executable", "verify_existing",
        }:
            return False
        if raw_error and "executable_review_requires_derived_requirement" not in raw_error and not re.fullmatch(
            r"requirements_not_referenced_by_clause_review:\d+", raw_error,
        ):
            return False
        missing_clause_ids.add(clause_id)
    if not missing_clause_ids:
        return False
    for item in remaining:
        if not isinstance(item, dict):
            return False
        clause_ids = item.get("clause_ids")
        if not isinstance(clause_ids, list) or not clause_ids:
            return False
        if not set(map(str, clause_ids)) <= missing_clause_ids:
            return False
    return True


def _retry_requirement_semantic_payload(requirement: Any) -> Any:
    """Return the execution payload used to compare a preserved requirement.

    ``reason`` explains why the model selected a role/property; it is not an
    execution binding.  A retry that only rephrases that explanation must not
    turn an otherwise safe relation completion into semantic drift.  Every
    executable field remains in this projection, so role, clause/evidence
    identity, properties, applicability, prerequisites, verification,
    confidence, and future contract fields still have to remain unchanged.
    """
    if not isinstance(requirement, dict):
        return requirement
    payload = copy.deepcopy(requirement)
    payload.pop("reason", None)
    return payload


def _v3_informational_projection_allowed(
    previous_response: Any,
    current_response: Any,
    records: list[dict[str, Any]],
    changed_paths: list[str],
    *,
    chunk: dict[str, Any] | None = None,
) -> bool:
    """Allow only the separately audited informational-only requirement projection.

    This is deliberately not part of the generic retry semantic-change
    whitelist.  The previous response must prove the exact informational-only
    set, and the current response must equal that response with only that set
    removed by an order-preserving mask.  Provenance is excluded from the
    comparison because the bridge binds it after local contract validation.
    """
    if changed_paths != ["$.requirements"]:
        return False
    if not isinstance(previous_response, dict) or not isinstance(current_response, dict):
        return False
    if not records or any(
        not isinstance(record, dict)
        or record.get("code") != "informational_requirement_forbidden"
        for record in records
    ):
        return False
    previous_requirements = previous_response.get("requirements")
    current_requirements = current_response.get("requirements")
    if not isinstance(previous_requirements, list) or not isinstance(current_requirements, list):
        return False
    analysis = analyze_requirement_relations(
        previous_response,
        chunk.get("clauses") if isinstance(chunk, dict) else None,
    )
    info_indexes = {
        int(item["requirement_index"])
        for item in analysis
        if isinstance(item, dict) and item.get("category") == "informational_only"
    }
    record_indexes = {
        int(record["requirement_index"])
        for record in records
        if isinstance(record.get("requirement_index"), int)
    }
    if not info_indexes or info_indexes != record_indexes:
        return False
    expected_requirements = [
        item for index, item in enumerate(previous_requirements)
        if index not in info_indexes
    ]
    if current_requirements != expected_requirements:
        return False
    previous_without_requirements = copy.deepcopy(previous_response)
    current_without_requirements = copy.deepcopy(current_response)
    previous_without_requirements.pop("requirements", None)
    current_without_requirements.pop("requirements", None)
    previous_without_requirements.pop("provenance", None)
    current_without_requirements.pop("provenance", None)
    return previous_without_requirements == current_without_requirements


def _v3_non_requirement_projection_allowed(
    previous_response: Any,
    current_response: Any,
    records: list[dict[str, Any]],
    changed_paths: list[str],
    *,
    chunk: dict[str, Any] | None = None,
) -> bool:
    """Allow removal of requirements bound only to non-requirement reviews.

    A v3 requirement cannot remain attached to a clause classified as
    unresolved, requires_source_content, external_compliance, or another
    non-executable state.  The model may therefore remove exactly the
    validator-identified requirement objects while preserving every review and
    every other requirement.  This is a relation projection, not a semantic
    reclassification: the unresolved review remains unresolved and the
    resulting full run remains fail-closed.
    """
    if (
        changed_paths != ["$.requirements"]
        or chunk is None
        or not isinstance(previous_response, dict)
        or not isinstance(current_response, dict)
    ):
        return False
    records = [record for record in records if isinstance(record, dict)]
    target_records = [
        record for record in records
        if record.get("code") == "non_requirement_classification_relation"
    ]
    if not target_records:
        return False
    previous_requirements = previous_response.get("requirements")
    current_requirements = current_response.get("requirements")
    previous_reviews = previous_response.get("clause_reviews")
    current_reviews = current_response.get("clause_reviews")
    if not all(isinstance(value, list) for value in (
        previous_requirements, current_requirements, previous_reviews, current_reviews,
    )):
        return False
    if previous_reviews != current_reviews:
        return False

    analysis = analyze_requirement_relations(previous_response, chunk.get("clauses", []))
    target_indexes: set[int] = set()
    for record in target_records:
        index_value = record.get("requirement_index")
        if not isinstance(index_value, int):
            match = re.fullmatch(
                r"\$\.requirements\[(\d+)\]", str(record.get("json_pointer") or ""),
            )
            if match is None:
                return False
            index_value = int(match.group(1))
        target_indexes.add(index_value)
    actual_indexes = {
        int(item["requirement_index"])
        for item in analysis
        if isinstance(item, dict)
        and item.get("category") == "non_requirement_classification"
        and isinstance(item.get("requirement_index"), int)
    }
    if not target_indexes or target_indexes != actual_indexes:
        return False
    expected_requirements = [
        item for index, item in enumerate(previous_requirements)
        if index not in target_indexes
    ]
    if current_requirements != expected_requirements:
        return False
    previous_without_requirements = copy.deepcopy(previous_response)
    current_without_requirements = copy.deepcopy(current_response)
    previous_without_requirements.pop("requirements", None)
    current_without_requirements.pop("requirements", None)
    previous_without_requirements.pop("provenance", None)
    current_without_requirements.pop("provenance", None)
    return previous_without_requirements == current_without_requirements


def _v3_non_requirement_projection_with_mechanical_repairs_allowed(
    previous_response: Any,
    current_response: Any,
    records: list[dict[str, Any]],
    changed_paths: list[str],
    *,
    chunk: dict[str, Any] | None = None,
) -> bool:
    """Allow a non-requirement projection plus named payload cleanup.

    Native retries sometimes remove a requirement that is linked only to an
    unresolved/non-executable clause while also dropping a validator-named
    unknown property from a preserved requirement.  The retry is safe only if
    the preserved requirements remain identical after those exact removals;
    an empty surviving payload must still be fillable from one exact cited
    evidence text.  No classification, relation, or other property change is
    admitted here.
    """
    if (
        changed_paths != ["$.requirements"]
        or chunk is None
        or not isinstance(previous_response, dict)
        or not isinstance(current_response, dict)
    ):
        return False
    records = [record for record in records if isinstance(record, dict)]
    target_records = [
        record for record in records
        if record.get("code") == "non_requirement_classification_relation"
    ]
    if not target_records:
        return False
    if any(record.get("code") not in {
        "non_requirement_classification_relation",
        "unknown_property",
        "empty_requirement_properties",
        "contract_validation_error",
        "schema_contract_violation",
    } for record in records):
        return False
    previous_reviews = previous_response.get("clause_reviews")
    current_reviews = current_response.get("clause_reviews")
    if previous_reviews != current_reviews:
        return False
    previous_requirements = previous_response.get("requirements")
    current_requirements = current_response.get("requirements")
    if not isinstance(previous_requirements, list) or not isinstance(current_requirements, list):
        return False

    clauses = chunk.get("clauses")
    if not isinstance(clauses, list):
        return False
    analysis = analyze_requirement_relations(previous_response, clauses)
    target_indexes: set[int] = set()
    for record in target_records:
        index_value = record.get("requirement_index")
        if not isinstance(index_value, int):
            match = re.fullmatch(
                r"\$\.requirements\[(\d+)\]", str(record.get("json_pointer") or ""),
            )
            if match is None:
                return False
            index_value = int(match.group(1))
        target_indexes.add(index_value)
    actual_indexes = {
        int(item["requirement_index"])
        for item in analysis
        if isinstance(item, dict)
        and item.get("category") == "non_requirement_classification"
        and isinstance(item.get("requirement_index"), int)
    }
    if not target_indexes or target_indexes != actual_indexes:
        return False

    # Unknown-property records are addressed against the first response's
    # indexes.  The retry may remove only those exact names and only from a
    # requirement that survives the relation projection.
    removed_properties: dict[int, set[str]] = {}
    for record in records:
        if record.get("code") != "unknown_property":
            continue
        pointer_match = re.fullmatch(
            r"\$\.requirements\[(\d+)\]\.properties", str(record.get("json_pointer") or ""),
        )
        property_match = re.search(
            r"unknown property ['\"]([^'\"]+)['\"]",
            str(record.get("raw_error") or ""),
        )
        if pointer_match is None or property_match is None:
            return False
        index = int(pointer_match.group(1))
        property_name = property_match.group(1)
        if index in target_indexes or index >= len(previous_requirements):
            return False
        previous_properties = previous_requirements[index].get("properties")
        if not isinstance(previous_properties, dict) or property_name not in previous_properties:
            return False
        removed_properties.setdefault(index, set()).add(property_name)

    expected_requirements: list[Any] = []
    for index, previous in enumerate(previous_requirements):
        if index in target_indexes:
            continue
        expected = copy.deepcopy(previous)
        if removed_properties.get(index):
            properties = expected.get("properties")
            if not isinstance(properties, dict):
                return False
            for property_name in removed_properties[index]:
                properties.pop(property_name, None)
        expected_requirements.append(expected)
    if len(current_requirements) != len(expected_requirements):
        return False
    for expected, current in zip(expected_requirements, current_requirements):
        if expected != current:
            return False
        if isinstance(current, dict) and current.get("properties") == {}:
            # The subsequent mechanical phase must be able to fill the
            # surviving empty payload from one exact cited evidence text.
            if _exact_cited_text(current, chunk) is None:
                return False

    previous_without_requirements = copy.deepcopy(previous_response)
    current_without_requirements = copy.deepcopy(current_response)
    previous_without_requirements.pop("requirements", None)
    current_without_requirements.pop("requirements", None)
    previous_without_requirements.pop("provenance", None)
    current_without_requirements.pop("provenance", None)
    return previous_without_requirements == current_without_requirements


_NON_REQUIREMENT_RECLASSIFICATIONS = frozenset({
    "external_compliance",
    "informational",
    "not_applicable",
    "requires_metadata",
    "requires_source_content",
    "unresolved",
    "unsupported",
    "unsupported_backend",
    "unverifiable",
})


def _v3_uncovered_obligation_reclassification_response(
    previous_response: Any,
    current_response: Any,
    records: list[dict[str, Any]],
    *,
    chunk: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Preserve an uncovered obligation while projecting its relation away.

    The model is allowed to make the semantic decision that a clause is not
    executable when the validator has proved that one of its obligations is
    not covered.  It is not allowed to satisfy the retry mechanically by
    changing that obligation to ``covered``.  This helper accepts only a
    complete, locally valid retry that preserves the affected obligation
    ``id``/``status`` pair and changes only the affected review's
    classification.  The accepted response is rebuilt from the previous
    response, and requirement edges for the affected clause are removed by
    deterministic projection rather than trusted from model bookkeeping.  A
    provider may leave behind the exact old requirement object with only
    ``clause_ids: []`` after removing its last affected edge; that invalid
    empty shell is accepted only as an intermediate form and is dropped by
    the same deterministic projection.  No other invalid or changed
    requirement payload is admitted.
    """
    if (
        chunk is None
        or not isinstance(previous_response, dict)
        or not isinstance(current_response, dict)
        or previous_response.get("contract_version") != HOST_REVIEW_CONTRACT_V3
    ):
        return None, None
    if not records or any(
        not isinstance(record, dict)
        or record.get("code") != "executable_review_obligations_uncovered"
        for record in records
    ):
        return None, None
    previous_reviews = previous_response.get("clause_reviews")
    current_reviews = current_response.get("clause_reviews")
    previous_requirements = previous_response.get("requirements")
    current_requirements = current_response.get("requirements")
    if not all(isinstance(value, list) for value in (
        previous_reviews, current_reviews, previous_requirements, current_requirements,
    )):
        return None, None
    if len(previous_reviews) != len(current_reviews):
        return None, None

    affected_indexes: list[int] = []
    for record in records:
        pointer = str(record.get("json_pointer") or "")
        match = re.fullmatch(r"\$\.clause_reviews\[(\d+)\]\.obligations", pointer)
        if match is None:
            return None, None
        review_index = int(match.group(1))
        if review_index >= len(previous_reviews) or review_index in affected_indexes:
            return None, None
        affected_indexes.append(review_index)
    if not affected_indexes:
        return None, None

    affected_clause_ids: set[str] = set()
    repaired_reviews = copy.deepcopy(previous_reviews)
    for review_index in affected_indexes:
        previous_review = previous_reviews[review_index]
        current_review = current_reviews[review_index]
        if not isinstance(previous_review, dict) or not isinstance(current_review, dict):
            return None, None
        clause_id = previous_review.get("clause_id")
        if not isinstance(clause_id, str) or not clause_id:
            return None, None
        if current_review.get("clause_id") != clause_id:
            return None, None
        if not classification_requires_requirement(str(previous_review.get("classification"))):
            return None, None
        current_classification = current_review.get("classification")
        if (
            current_classification not in _NON_REQUIREMENT_RECLASSIFICATIONS
            or classification_requires_requirement(str(current_classification))
        ):
            return None, None
        previous_obligations = previous_review.get("obligations")
        current_obligations = current_review.get("obligations")
        if not isinstance(previous_obligations, list) or not isinstance(current_obligations, list):
            return None, None
        if not any(
            isinstance(item, dict) and item.get("status") != "covered"
            for item in previous_obligations
        ):
            return None, None
        previous_signatures = [
            (item.get("id"), item.get("status"))
            for item in previous_obligations
            if isinstance(item, dict)
        ]
        current_signatures = [
            (item.get("id"), item.get("status"))
            for item in current_obligations
            if isinstance(item, dict)
        ]
        if (
            len(previous_signatures) != len(previous_obligations)
            or len(current_signatures) != len(current_obligations)
            or previous_signatures != current_signatures
        ):
            return None, None
        expected_review = copy.deepcopy(previous_review)
        expected_review["classification"] = current_classification
        if current_review != expected_review:
            return None, None
        repaired_reviews[review_index] = expected_review
        affected_clause_ids.add(clause_id)

    # No unrelated semantic response field may change during this bounded
    # reclassification.  Provenance is owned by the bridge and is excluded.
    for key in set(previous_response) | set(current_response):
        if key in {"provenance", "requirements", "clause_reviews"}:
            continue
        if previous_response.get(key) != current_response.get(key):
            return None, None

    projected_requirements: list[dict[str, Any]] = []
    intermediate_requirements: list[dict[str, Any]] = []
    clauses = chunk.get("clauses")
    if not isinstance(clauses, list):
        return None, None
    clause_map = {
        str(item.get("id")): item
        for item in clauses
        if isinstance(item, dict) and item.get("id")
    }
    for requirement in previous_requirements:
        if not isinstance(requirement, dict):
            return None, None
        raw_clause_ids = requirement.get("clause_ids")
        if not isinstance(raw_clause_ids, list) or not raw_clause_ids:
            return None, None
        clause_ids = [str(value) for value in raw_clause_ids]
        if len(clause_ids) != len(set(clause_ids)) or any(
            clause_id not in clause_map for clause_id in clause_ids
        ):
            return None, None
        remaining_clause_ids = [
            clause_id for clause_id in clause_ids if clause_id not in affected_clause_ids
        ]
        if not remaining_clause_ids:
            stale_empty = copy.deepcopy(requirement)
            stale_empty["clause_ids"] = []
            intermediate_requirements.append(stale_empty)
            continue
        projected = copy.deepcopy(requirement)
        if remaining_clause_ids != clause_ids:
            evidence_ids = requirement.get("evidence_ids")
            if not isinstance(evidence_ids, list):
                return None, None
            backed_evidence_ids = {
                str(evidence_id)
                for clause_id in remaining_clause_ids
                for evidence_id in (clause_map[clause_id].get("evidence_ids") or [])
            }
            if not set(map(str, evidence_ids)) <= backed_evidence_ids:
                return None, None
            projected["clause_ids"] = remaining_clause_ids
        projected_requirements.append(projected)
        intermediate_requirements.append(copy.deepcopy(projected))

    if current_requirements not in (projected_requirements, intermediate_requirements):
        return None, None
    repaired = copy.deepcopy(previous_response)
    repaired["clause_reviews"] = repaired_reviews
    repaired["requirements"] = projected_requirements
    if validate_host_agent_response(repaired, chunk):
        return None, None
    return repaired, {
        "rule_id": "preserve_uncovered_obligation_and_project_relation_v1",
        "affected_clause_ids": sorted(affected_clause_ids),
        "affected_review_indexes": sorted(affected_indexes),
        "preserved_obligation_statuses": True,
        "removed_clause_edges": sorted(affected_clause_ids),
        "preserved_requirement_count": len(previous_requirements),
        "projected_requirement_count": len(projected_requirements),
        "dropped_stale_empty_requirement_count": (
            len(intermediate_requirements) - len(projected_requirements)
        ),
    }


def _bind_current_invocation_provenance(
    response: Any,
    provenance: dict[str, Any],
) -> dict[str, Any]:
    """Re-bind bridge-owned provenance after a bounded semantic projection.

    The v3 retry projections intentionally rebuild their semantic payload from
    raw provider responses, which do not contain trusted identity fields. A
    projection must therefore receive the current invocation provenance again
    before it is persisted or validated. This function never preserves a
    provider-supplied provenance object.
    """
    if not isinstance(response, dict):
        raise ValueError("semantic retry projection did not produce an object")
    bound = copy.deepcopy(response)
    bound["provenance"] = copy.deepcopy(provenance)
    return bound


def _v3_uncovered_obligation_reclassification_allowed(
    previous_response: Any,
    current_response: Any,
    records: list[dict[str, Any]],
    *,
    chunk: dict[str, Any] | None = None,
) -> bool:
    repaired, _audit = _v3_uncovered_obligation_reclassification_response(
        previous_response, current_response, records, chunk=chunk,
    )
    return repaired is not None


def _v3_relation_completion_response(
    previous_response: Any,
    current_response: Any,
    records: list[dict[str, Any]],
    *,
    chunk: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Accept a bounded v3 relation-completion retry.

    The provider owns the semantic construction of a missing requirement.  The
    bridge only accepts the retry when it can prove that every non-placeholder
    baseline requirement and every review survived, that each added
    requirement is evidence-backed for a validator-named executable clause,
    and that any removed item was an unbound empty provider placeholder.  A
    fixed declaration text correction is admitted only when it is the exact
    cited evidence text.  No requirement or semantic payload is synthesized
    here.
    """
    if chunk is None or not isinstance(previous_response, dict) or not isinstance(current_response, dict):
        return None, None
    relation_codes = {"requirement_relation_mismatch", "missing_derived_requirement"}
    mechanical_codes = {
        "contract_validation_error", "schema_contract_violation", "unknown_property",
        "empty_requirement_properties", "fixed_text_evidence_mismatch",
    }
    codes = {
        str(record.get("code"))
        for record in records
        if isinstance(record, dict)
    }
    if (
        not records
        or not codes & relation_codes
        or not codes <= relation_codes | mechanical_codes
    ):
        return None, None
    if previous_response.get("contract_version") != HOST_REVIEW_CONTRACT_V3:
        return None, None
    if previous_response.get("clause_reviews") != current_response.get("clause_reviews"):
        return None, None
    if previous_response.get("unsupported_items") != current_response.get("unsupported_items"):
        return None, None
    if previous_response.get("reported_conflicts") != current_response.get("reported_conflicts"):
        return None, None
    current_requirements = current_response.get("requirements")
    previous_requirements = previous_response.get("requirements")
    if not isinstance(previous_requirements, list) or not isinstance(current_requirements, list):
        return None, None
    missing_clause_ids: set[str] = set()
    previous_reviews = previous_response.get("clause_reviews")
    if not isinstance(previous_reviews, list):
        return None, None
    placeholder_indexes: set[int] = set()

    def has_meaningful_value(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, list):
            return any(has_meaningful_value(item) for item in value)
        if isinstance(value, dict):
            return any(has_meaningful_value(item) for item in value.values())
        return True

    def is_unbound_placeholder(requirement: Any) -> bool:
        if not isinstance(requirement, dict):
            return False
        properties = requirement.get("properties")
        return (
            isinstance(properties, dict)
            and not has_meaningful_value(properties)
            and not has_meaningful_value(requirement.get("clause_ids"))
            and not has_meaningful_value(requirement.get("evidence_ids"))
            and not has_meaningful_value(requirement.get("existing_requirement_id"))
            and not has_meaningful_value(requirement.get("field_key"))
            and not has_meaningful_value(requirement.get("reason"))
            and requirement.get("confidence") in (None, 0, 0.0)
            and not has_meaningful_value(requirement.get("applicability"))
            and not has_meaningful_value(requirement.get("input_prerequisites"))
            and not has_meaningful_value(requirement.get("verification"))
        )

    for index, requirement in enumerate(previous_requirements):
        if is_unbound_placeholder(requirement):
            placeholder_indexes.add(index)

    for record in records:
        if not isinstance(record, dict):
            return None, None
        code = str(record.get("code") or "")
        pointer = str(record.get("json_pointer") or "")
        raw_error = str(record.get("raw_error") or "")
        if code == "empty_requirement_properties":
            match = re.fullmatch(r"\$\.requirements\[(\d+)\]\.properties", pointer)
            if match is None or int(match.group(1)) not in placeholder_indexes:
                return None, None
            continue
        if code in {"contract_validation_error", "schema_contract_violation", "unknown_property", "fixed_text_evidence_mismatch"}:
            continue
        if code not in relation_codes:
            return None, None
        review_match = re.search(r"\$\.clause_reviews\[(\d+)\]", pointer)
        if review_match is not None:
            review_index = int(review_match.group(1))
            if review_index >= len(previous_reviews):
                return None, None
            review = previous_reviews[review_index]
            if not isinstance(review, dict) or review.get("classification") not in {
                "covered", "executable", "verify_existing",
            }:
                return None, None
            clause_id = review.get("clause_id")
            if not isinstance(clause_id, str) or not clause_id:
                return None, None
            if (
                code not in {"missing_derived_requirement", "requirement_relation_mismatch"}
                or "executable_review_requires_derived_requirement" not in raw_error
            ):
                return None, None
            missing_clause_ids.add(clause_id)
            continue
        legacy_match = re.fullmatch(
            r"requirements_not_referenced_by_clause_review:(\d+)", raw_error,
        )
        if legacy_match is None:
            return None, None
        requirement_index = int(legacy_match.group(1))
        if requirement_index >= len(previous_requirements):
            return None, None
        clause_ids = previous_requirements[requirement_index].get("clause_ids")
        if requirement_index in placeholder_indexes:
            continue
        if not isinstance(clause_ids, list) or not clause_ids:
            return None, None
        missing_clause_ids.update(str(value) for value in clause_ids)
    if not missing_clause_ids:
        return None, None

    def identity(item: Any) -> str | None:
        if not isinstance(item, dict):
            return None
        return json.dumps({
            key: item.get(key)
            for key in ("role", "clause_ids", "evidence_ids", "existing_requirement_id")
            if key in item
        }, ensure_ascii=False, sort_keys=True)

    previous_by_identity: dict[str, list[dict[str, Any]]] = {}
    previous_non_placeholders = [
        item for index, item in enumerate(previous_requirements)
        if index not in placeholder_indexes
    ]
    for item in previous_non_placeholders:
        key = identity(item)
        if key is None:
            return None, None
        previous_by_identity.setdefault(key, []).append(item)
    additions: list[dict[str, Any]] = []
    preserved_by_identity: dict[str, list[dict[str, Any]]] = {}
    consumed_previous: dict[str, int] = {}
    current_placeholder_indexes: set[int] = set()
    for index, item in enumerate(current_requirements):
        if is_unbound_placeholder(item):
            current_placeholder_indexes.add(index)
            if index not in placeholder_indexes:
                return None, None
            continue
        key = identity(item)
        if key is None:
            return None, None
        candidates = previous_by_identity.get(key, [])
        consumed = consumed_previous.get(key, 0)
        if consumed < len(candidates):
            previous_item = candidates[consumed]
            if _retry_requirement_semantic_payload(item) != _retry_requirement_semantic_payload(previous_item):
                previous_item_index = next(
                    (
                        index
                        for index, candidate in enumerate(previous_requirements)
                        if candidate is previous_item
                    ),
                    None,
                )
                fixed_text_allowed = _v3_fixed_text_requirement_change_allowed(
                    previous_item, item, records, chunk=chunk,
                )
                unknown_property_text_allowed = (
                    previous_item_index is not None
                    and _v3_unknown_property_text_requirement_change_allowed(
                        previous_item,
                        item,
                        records,
                        chunk=chunk,
                        requirement_index=previous_item_index,
                    )
                )
                if not fixed_text_allowed and not unknown_property_text_allowed:
                    return None, None
                preserved_item = copy.deepcopy(previous_item)
                preserved_item["properties"] = copy.deepcopy(item.get("properties"))
            else:
                # Keep the baseline object (including its diagnostic reason)
                # rather than allowing an otherwise-unnecessary retry rewrite.
                preserved_item = copy.deepcopy(previous_item)
            preserved_by_identity.setdefault(key, []).append(preserved_item)
            consumed_previous[key] = consumed + 1
            continue
        clause_ids = item.get("clause_ids") if isinstance(item, dict) else None
        if (
            not isinstance(item, dict)
            or item.get("existing_requirement_id")
            or not isinstance(clause_ids, list)
            or not clause_ids
            or not set(map(str, clause_ids)) <= missing_clause_ids
        ):
            return None, None
        additions.append(copy.deepcopy(item))
    preserved_items: list[dict[str, Any]] = []
    restored_omitted_count = 0
    for previous_item in previous_non_placeholders:
        key = identity(previous_item)
        if key is None:
            return None, None
        candidates = preserved_by_identity.get(key, [])
        if candidates:
            preserved_items.append(candidates.pop(0))
        else:
            # A retry is allowed to omit a baseline requirement while adding
            # the missing relation. Restore that exact baseline object; the
            # bridge never accepts the omission as a semantic deletion.
            preserved_items.append(copy.deepcopy(previous_item))
            restored_omitted_count += 1
    added_clause_ids = {
        str(clause_id)
        for item in additions
        for clause_id in (item.get("clause_ids") or [])
    }
    if not additions or not missing_clause_ids <= added_clause_ids:
        return None, None

    repaired = copy.deepcopy(current_response)
    repaired["requirements"] = preserved_items + additions
    if validate_host_agent_response(repaired, chunk):
        return None, None
    return repaired, {
        "rule_id": "preserve_baseline_add_missing_v3_requirements_and_remove_unbound_placeholder",
        "missing_clause_ids": sorted(missing_clause_ids),
        "removed_unbound_placeholder_count": len(current_placeholder_indexes),
        "preserved_requirement_count": len(previous_non_placeholders),
        "restored_omitted_baseline_count": restored_omitted_count,
        "added_requirement_count": len(additions),
    }


def _v3_fixed_text_requirement_change_allowed(
    previous_requirement: Any,
    current_requirement: Any,
    records: list[dict[str, Any]],
    *,
    chunk: dict[str, Any],
) -> bool:
    """Allow only exact cited-text edits inside a fixed declaration payload."""
    if not any(
        isinstance(record, dict) and record.get("code") == "fixed_text_evidence_mismatch"
        for record in records
    ):
        return False
    changed_paths = _retry_change_paths(
        {"requirements": [previous_requirement]},
        {"requirements": [current_requirement]},
    )
    if not changed_paths:
        return False
    evidence_context = chunk.get("evidence_context")
    evidence_ids = current_requirement.get("evidence_ids") if isinstance(current_requirement, dict) else None
    if not isinstance(evidence_context, dict) or not isinstance(evidence_ids, list):
        return False
    evidence_texts = {
        str(evidence_context[str(evidence_id)].get("text") or "")
        for evidence_id in evidence_ids
        if str(evidence_id) in evidence_context
        and isinstance(evidence_context[str(evidence_id)], dict)
        and str(evidence_context[str(evidence_id)].get("text") or "").strip()
    }
    properties = current_requirement.get("properties") if isinstance(current_requirement, dict) else None
    items = properties.get("items") if isinstance(properties, dict) else None
    if not isinstance(items, list):
        return False
    for path in changed_paths:
        if path.endswith(".heading"):
            match = re.fullmatch(r"\$\.requirements\[0\]\.properties\.items\[(\d+)\]\.heading", path)
            if match is None or int(match.group(1)) >= len(items):
                return False
            value = items[int(match.group(1))].get("heading") if isinstance(items[int(match.group(1))], dict) else None
            if value not in evidence_texts:
                return False
            continue
        match = re.fullmatch(
            r"\$\.requirements\[0\]\.properties\.items\[(\d+)\]\.body_parts\[(\d+)\]",
            path,
        )
        if match is None:
            return False
        item_index, part_index = int(match.group(1)), int(match.group(2))
        if item_index >= len(items) or not isinstance(items[item_index], dict):
            return False
        body_parts = items[item_index].get("body_parts")
        if not isinstance(body_parts, list) or part_index >= len(body_parts):
            return False
        if body_parts[part_index] not in evidence_texts:
            return False
    return True


def _v3_unknown_property_text_requirement_change_allowed(
    previous_requirement: Any,
    current_requirement: Any,
    records: list[dict[str, Any]],
    *,
    chunk: dict[str, Any],
    requirement_index: int,
) -> bool:
    """Allow an unknown-property removal followed by exact cited text fill.

    ``run_host_agent_chunk`` can deterministically remove a validator-named
    layout property and then fill a text-capable role with the one exact
    cited evidence string.  Relation completion must recognize that bound
    mechanical projection when it also accepts a newly derived requirement;
    it must not treat the projection as a provider-authored semantic rewrite.
    """
    if not isinstance(previous_requirement, dict) or not isinstance(current_requirement, dict):
        return False
    unknown_names: set[str] = set()
    pointer = f"$.requirements[{requirement_index}].properties"
    for record in records:
        if not isinstance(record, dict) or record.get("code") != "unknown_property":
            continue
        if str(record.get("json_pointer") or "") != pointer:
            continue
        match = re.search(
            r"unknown property ['\"]([^'\"]+)['\"]",
            str(record.get("raw_error") or ""),
        )
        if match is None:
            return False
        unknown_names.add(match.group(1))
    if not unknown_names:
        return False
    previous_properties = previous_requirement.get("properties")
    current_properties = current_requirement.get("properties")
    exact_text = _exact_cited_text(current_requirement, chunk)
    if (
        not isinstance(previous_properties, dict)
        or not isinstance(current_properties, dict)
        or set(previous_properties) != unknown_names
        or current_properties != {"text": exact_text}
        or exact_text is None
    ):
        return False
    previous_rest = copy.deepcopy(previous_requirement)
    current_rest = copy.deepcopy(current_requirement)
    previous_rest.pop("properties", None)
    current_rest.pop("properties", None)
    return previous_rest == current_rest


def _retry_semantic_change_error(
    previous_response: Any,
    current_response: Any,
    records: list[dict[str, Any]],
    *,
    contract_version: str,
    chunk: dict[str, Any] | None = None,
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
        chunk=chunk,
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


def _apply_safe_mechanical_repairs(
    response: Any, error_records: list[dict[str, Any]],
    *, chunk: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Apply only validator-directed, bounded JSON repairs.

    Unknown properties, evidence IDs that are explicitly reported as not
    backed by the authoritative clause relation, empty role payloads whose
    exact text is present in cited evidence, and an explicit optional-caption
    contradiction are mechanical boundary errors. One additional narrow
    projection converts an incomplete administrative cover branch into an
    explicit manual-review state when the source has no field labels. It is
    deliberately not a semantic guess: fixed declaration text is preserved,
    the incomplete requirement edge is removed, and release gates remain
    fail-closed. The helper never invents an administrative field or value.
    """
    if not isinstance(response, dict) or not error_records:
        return None, []
    allowed_codes = {
        "unknown_property", "evidence_relation_mismatch",
        "empty_requirement_properties", "informational_requirement_forbidden",
        "applicability_fact_namespace", "partial_clause_coverage",
        "cover_institution_placeholder", "contract_validation_error",
        "schema_contract_violation", "requirement_relation_mismatch",
        "non_public_administration_fields_missing",
    }
    if any(
        not isinstance(record, dict)
        or record.get("code") not in allowed_codes
        for record in error_records
    ):
        return None, []
    repaired = copy.deepcopy(response)
    repairs: list[dict[str, Any]] = []

    # A source can require a non-public thesis approval/marking statement
    # without specifying the actual administrative form fields. The schema
    # correctly rejects ``fields: []``; do not satisfy that schema by
    # inventing approval_number/date/security fields. Instead, only when the
    # source has no explicit field labels and the response has an independent
    # administrative obligation review, remove the incomplete cover branch,
    # retain exact fixed declaration requirements, and downgrade that narrow
    # administrative obligation to a visible source-content/manual-review
    # state. This projection is bound to the current chunk and response; it
    # is not a global rule for any particular clause ID or school.
    missing_admin_records = [
        record for record in error_records
        if isinstance(record, dict)
        and record.get("code") == "non_public_administration_fields_missing"
    ]
    if missing_admin_records:
        requirements = repaired.get("requirements")
        reviews = repaired.get("clause_reviews")
        if not isinstance(requirements, list) or not isinstance(reviews, list):
            return None, []

        def record_requirement_index(record: dict[str, Any]) -> int | None:
            pointer = str(record.get("json_pointer") or "")
            match = re.match(r"^\$\.requirements\[(\d+)\]", pointer)
            return int(match.group(1)) if match else None

        admin_indexes = {
            index for record in missing_admin_records
            if (index := record_requirement_index(record)) is not None
        }
        if not admin_indexes:
            return None, []

        clauses_by_id = {
            str(clause.get("id")): clause
            for clause in (chunk.get("clauses", []) if isinstance(chunk, dict) else [])
            if isinstance(clause, dict) and clause.get("id")
        }
        evidence_context = (
            chunk.get("evidence_context", {})
            if isinstance(chunk, dict) else {}
        )
        if not isinstance(evidence_context, dict):
            evidence_context = {}

        def source_texts(requirement: dict[str, Any]) -> list[str]:
            evidence_ids: list[str] = []
            for value in requirement.get("evidence_ids", []):
                if str(value) not in evidence_ids:
                    evidence_ids.append(str(value))
            for clause_id in requirement.get("clause_ids", []):
                clause = clauses_by_id.get(str(clause_id))
                if not isinstance(clause, dict):
                    continue
                for value in clause.get("evidence_ids", []):
                    if str(value) not in evidence_ids:
                        evidence_ids.append(str(value))
            texts: list[str] = []
            for evidence_id in evidence_ids:
                evidence = evidence_context.get(evidence_id)
                if isinstance(evidence, dict):
                    for key in ("text", "source_text_full", "body"):
                        value = evidence.get(key)
                        if isinstance(value, str) and value.strip():
                            texts.append(value)
                            break
            for clause_id in requirement.get("clause_ids", []):
                clause = clauses_by_id.get(str(clause_id))
                if isinstance(clause, dict):
                    for key in ("text", "source_text_full"):
                        value = clause.get(key)
                        if isinstance(value, str) and value.strip():
                            texts.append(value)
                            break
            return texts

        # These are field labels, not general approval language. A sentence
        # saying that approval is required is insufficient evidence for a
        # particular field. If any exact label is present, leave the response
        # fail-closed so the model or a later authoritative source can bind it.
        explicit_field_patterns = (
            r"审批表编号", r"批准日期", r"保密期限", r"保密级别", r"密级",
            r"approval[_ ]number", r"approval[_ ]date", r"security[_ ]marking",
            r"embargo[_ ](?:start|until)",
        )

        reviews_by_clause = {
            str(review.get("clause_id")): review
            for review in reviews
            if isinstance(review, dict) and review.get("clause_id")
        }
        administrative_obligation_tokens = (
            "admin", "approval", "embargo", "security", "public_blank",
        )
        target_clause_ids: set[str] = set()
        removed_requirement_fingerprints: list[str] = []
        source_hashes: dict[int, str] = {}
        for index in sorted(admin_indexes):
            if index < 0 or index >= len(requirements):
                return None, []
            requirement = requirements[index]
            if not isinstance(requirement, dict) or requirement.get("role") != "cover":
                return None, []
            if requirement.get("existing_requirement_id") not in (None, "", []):
                return None, []
            properties = requirement.get("properties")
            if not isinstance(properties, dict):
                return None, []
            administration = properties.get("non_public_administration")
            if (
                not isinstance(administration, dict)
                or administration.get("fields") != []
            ):
                return None, []
            texts = source_texts(requirement)
            source_hashes[index] = _response_sha256(texts)
            if any(
                re.search(pattern, " ".join(texts), re.IGNORECASE)
                for pattern in explicit_field_patterns
            ):
                return None, []
            clause_ids = requirement.get("clause_ids")
            if not isinstance(clause_ids, list) or not clause_ids:
                return None, []
            for clause_id_value in clause_ids:
                clause_id = str(clause_id_value)
                review = reviews_by_clause.get(clause_id)
                if not isinstance(review, dict):
                    return None, []
                obligations = review.get("obligations")
                if not isinstance(obligations, list):
                    return None, []
                admin_obligation = any(
                    any(
                        token in str(obligation.get("id") or "").lower()
                        for token in administrative_obligation_tokens
                    )
                    for obligation in obligations
                    if isinstance(obligation, dict)
                )
                if admin_obligation:
                    if review.get("classification") not in {
                        "covered", "executable", "verify_existing",
                    }:
                        return None, []
                    target_clause_ids.add(clause_id)
        if not target_clause_ids:
            return None, []

        # Remove the invalid cover branch first, retaining the old-index map
        # for legacy contract 2.1 reverse indexes. The current contract is
        # v3, where requirements[].clause_ids is the sole relation authority.
        original_requirements = list(requirements)
        old_to_new: dict[int, int | None] = {}
        kept_requirements: list[dict[str, Any]] = []
        for old_index, requirement in enumerate(original_requirements):
            if old_index in admin_indexes:
                old_to_new[old_index] = None
                removed_requirement_fingerprints.append(_response_sha256(requirement))
                continue
            old_to_new[old_index] = len(kept_requirements)
            kept_requirements.append(requirement)

        # Remove only the unresolved administrative clause edges from all
        # surviving requirements. If that would leave a bound requirement
        # empty, refuse the projection instead of guessing whether it should
        # be deleted or reclassified.
        for requirement in kept_requirements:
            clause_ids = requirement.get("clause_ids")
            if not isinstance(clause_ids, list):
                return None, []
            retained_clause_ids = [
                value for value in clause_ids if str(value) not in target_clause_ids
            ]
            if clause_ids and not retained_clause_ids:
                return None, []
            requirement["clause_ids"] = retained_clause_ids
        repaired["requirements"] = kept_requirements

        for review in reviews:
            if not isinstance(review, dict) or not review.get("clause_id"):
                continue
            clause_id = str(review["clause_id"])
            indexes = review.get("requirement_indexes")
            if clause_id in target_clause_ids:
                review["classification"] = "requires_source_content"
                review["reason"] = (
                    str(review.get("reason") or "").rstrip()
                    + " 原文未提供可执行的行政字段清单，已保留原文并转为人工审查占位；"
                    "补充权威字段后才能生成行政表格。"
                )
                if isinstance(indexes, list):
                    review["requirement_indexes"] = []
            elif isinstance(indexes, list):
                remapped = [
                    old_to_new[index]
                    for index in indexes
                    if isinstance(index, int)
                    and index in old_to_new
                    and old_to_new[index] is not None
                ]
                review["requirement_indexes"] = remapped

        repairs.append({
            "code": "non_public_administration_fields_missing",
            "rule_id": "downgrade_unlabeled_admin_region_to_manual_review_v1",
            "removed_requirement_indexes": sorted(admin_indexes),
            "removed_requirement_fingerprints": removed_requirement_fingerprints,
            "target_clause_ids": sorted(target_clause_ids),
            "source_clause_ids": sorted({
                str(value)
                for index in admin_indexes
                for value in original_requirements[index].get("clause_ids", [])
            }),
            "source_text_hashes": source_hashes,
            "disposition": "manual_review_draft_only",
            "reason": "source has an approval/marking obligation but no explicit administrative field labels",
            "source_response_sha256": _response_sha256(response),
            "repaired_response_sha256": _response_sha256(repaired),
        })

        remaining_records: list[dict[str, Any]] = []
        for record in error_records:
            index = record_requirement_index(record) if isinstance(record, dict) else None
            code = str(record.get("code") or "") if isinstance(record, dict) else ""
            pointer = str(record.get("json_pointer") or "") if isinstance(record, dict) else ""
            raw_error = str(record.get("raw_error") or "") if isinstance(record, dict) else ""
            projection_error = (
                code == "non_public_administration_fields_missing"
                or (
                    code == "cover_institution_placeholder"
                    and pointer in {
                        f"$.requirements[{index}].properties",
                        f"$.requirements[{index}].properties.institution",
                    }
                )
                or (
                    code == "contract_validation_error"
                    and pointer == f"$.requirements[{index}].properties"
                    and "must match at least one schema in anyof" in raw_error.lower()
                )
                or (
                    code in {"cover_binding_violation", "schema_contract_violation"}
                    and ".non_public_administration" in pointer
                )
            )
            if index not in admin_indexes or not projection_error:
                remaining_records.append(record)
        error_records = remaining_records
        if not error_records:
            return repaired, repairs

    informational_records = [
        record for record in error_records
        if isinstance(record, dict)
        and record.get("code") == "informational_requirement_forbidden"
    ]
    if informational_records:
        # This is the only relation projection that is currently safe to do
        # mechanically.  Recompute the complete relation from the current
        # response and chunk; never trust an index embedded in an error string.
        analysis_clauses = (
            chunk.get("clauses") if isinstance(chunk, dict) else None
        )
        relation_analysis = analyze_requirement_relations(repaired, analysis_clauses)
        relation_by_index = {
            int(item["requirement_index"]): item
            for item in relation_analysis
            if isinstance(item, dict) and isinstance(item.get("requirement_index"), int)
        }
        candidate_indexes: set[int] = set()
        for record in informational_records:
            requirement_index = record.get("requirement_index")
            if not isinstance(requirement_index, int):
                match = re.fullmatch(
                    r"\$\.requirements\[(\d+)\]", str(record.get("json_pointer") or "")
                )
                if match is None:
                    return None, []
                requirement_index = int(match.group(1))
            candidate_indexes.add(requirement_index)
        actual_informational_indexes = {
            index for index, fact in relation_by_index.items()
            if fact.get("category") == "informational_only"
        }
        if not candidate_indexes or candidate_indexes != actual_informational_indexes:
            return None, []
        requirements = repaired.get("requirements")
        if not isinstance(requirements, list):
            return None, []
        authoritative_clauses = (
            analysis_clauses if isinstance(analysis_clauses, list) else []
        )
        clause_counts: dict[str, int] = {}
        for clause in authoritative_clauses:
            if isinstance(clause, dict) and clause.get("id"):
                clause_id = str(clause["id"])
                clause_counts[clause_id] = clause_counts.get(clause_id, 0) + 1
        reviews = repaired.get("clause_reviews")
        if not isinstance(reviews, list):
            return None, []
        review_counts: dict[str, int] = {}
        review_classes: dict[str, list[str]] = {}
        for review in reviews:
            if not isinstance(review, dict) or not review.get("clause_id"):
                continue
            clause_id = str(review["clause_id"])
            review_counts[clause_id] = review_counts.get(clause_id, 0) + 1
            review_classes.setdefault(clause_id, []).append(
                str(review.get("classification") or "")
            )
        removed_requirement_fingerprints: list[str] = []
        for index in sorted(candidate_indexes):
            if index < 0 or index >= len(requirements) or not isinstance(requirements[index], dict):
                return None, []
            requirement = requirements[index]
            raw_clause_ids = requirement.get("clause_ids")
            if not isinstance(raw_clause_ids, list) or not raw_clause_ids:
                return None, []
            clause_ids = [str(value) for value in raw_clause_ids]
            if len(clause_ids) != len(set(clause_ids)):
                return None, []
            for clause_id in clause_ids:
                if authoritative_clauses and clause_counts.get(clause_id) != 1:
                    return None, []
                if review_counts.get(clause_id) != 1 or review_classes.get(clause_id) != ["informational"]:
                    return None, []
            removed_requirement_fingerprints.append(_response_sha256(requirement))
            repairs.append({
                "code": "informational_requirement_forbidden",
                "rule_id": "remove_informational_only_requirement_v1",
                "removed_requirement_index": index,
                "removed_clause_ids": clause_ids,
                "removed_requirement": copy.deepcopy(requirement),
                "removed_requirement_sha256": removed_requirement_fingerprints[-1],
                "linked_classifications": {
                    clause_id: list(review_classes[clause_id]) for clause_id in clause_ids
                },
                "reason": "every linked clause review is exactly informational",
            })
        original_count = len(requirements)
        retained_index_map: dict[str, int | None] = {}
        repaired_requirements: list[dict[str, Any]] = []
        for index, requirement in enumerate(requirements):
            if index in candidate_indexes:
                retained_index_map[str(index)] = None
                continue
            retained_index_map[str(index)] = len(repaired_requirements)
            repaired_requirements.append(requirement)
        repaired["requirements"] = repaired_requirements
        repairs.append({
            "code": "informational_requirement_forbidden",
            "rule_id": "remove_informational_only_requirement_v1",
            "removed_requirement_indexes": sorted(candidate_indexes),
            "removed_requirement_count": len(candidate_indexes),
            "before_requirement_count": original_count,
            "after_requirement_count": len(repaired["requirements"]),
            "projection": "ordered_mask",
            "original_index_to_repaired_index": retained_index_map,
            "removed_requirement_fingerprints": removed_requirement_fingerprints,
            "source_response_sha256": _response_sha256(response),
            "repaired_response_sha256": _response_sha256(repaired),
        })
        return repaired, repairs

    # A native model sometimes emits one role-schema default as a trailing
    # ``requirement`` even though it has no clause/evidence relation and no
    # semantic content at all.  This is not a requirement that can be
    # compiled: it is an unbound placeholder produced by the response shape.
    # Remove it only under the complete predicate below.  In particular, a
    # requirement with any clause, evidence, reason, confidence, applicability,
    # prerequisite, verification, field key, existing id, or non-empty payload
    # remains fail-closed and cannot be guessed away.
    empty_placeholder_records = [
        record for record in error_records
        if isinstance(record, dict)
        and record.get("code") == "empty_requirement_properties"
    ]
    if empty_placeholder_records:
        requirements = repaired.get("requirements")
        if not isinstance(requirements, list):
            return None, []

        def is_empty_value(value: Any) -> bool:
            return value is None or value == "" or value == [] or value == {}

        def is_unbound_placeholder(requirement: Any) -> bool:
            if not isinstance(requirement, dict):
                return False
            properties = requirement.get("properties")
            if not isinstance(properties, dict) or not all(
                is_empty_value(value) for value in properties.values()
            ):
                return False
            return (
                is_empty_value(requirement.get("clause_ids"))
                and is_empty_value(requirement.get("evidence_ids"))
                and is_empty_value(requirement.get("existing_requirement_id"))
                and is_empty_value(requirement.get("field_key"))
                and is_empty_value(requirement.get("reason"))
                and requirement.get("confidence") in (None, 0, 0.0)
                and is_empty_value(requirement.get("applicability"))
                and is_empty_value(requirement.get("input_prerequisites"))
                and is_empty_value(requirement.get("verification"))
            )

        placeholder_related_codes = {
            "contract_validation_error", "empty_requirement_properties",
            "schema_contract_violation", "requirement_relation_mismatch",
        }

        def related_placeholder_index(record: dict[str, Any]) -> int | None:
            code = str(record.get("code") or "")
            pointer = str(record.get("json_pointer") or "")
            raw_error = str(record.get("raw_error") or "")
            pointer_match = re.match(r"^\$\.requirements\[(\d+)\]", pointer)
            if pointer_match is None:
                return None
            index = int(pointer_match.group(1))
            if code == "empty_requirement_properties":
                if pointer != f"$.requirements[{index}].properties" or "must_include_semantic_payload" not in raw_error:
                    return None
                return index
            if code == "contract_validation_error":
                if pointer != f"$.requirements[{index}].reason" or "is shorter than 1 characters" not in raw_error:
                    return None
                return index
            if code == "schema_contract_violation":
                if pointer not in {
                    f"$.requirements[{index}].clause_ids",
                    f"$.requirements[{index}].evidence_ids",
                } or "must_be_non_empty" not in raw_error:
                    return None
                return index
            if code == "requirement_relation_mismatch":
                if pointer != f"$.requirements[{index}]":
                    return None
                if raw_error != f"requirements_not_referenced_by_clause_review:{index}":
                    return None
                if record.get("relation_category") != "missing_clause_relation":
                    return None
                return index
            return None

        indexes: set[int] = set()
        can_remove_placeholders = all(
            isinstance(record, dict)
            and str(record.get("code") or "") in placeholder_related_codes
            for record in error_records
        )
        if can_remove_placeholders:
            for record in error_records:
                index = related_placeholder_index(record)
                if index is None:
                    can_remove_placeholders = False
                    break
                if index >= len(requirements) or not is_unbound_placeholder(requirements[index]):
                    can_remove_placeholders = False
                    break
                indexes.add(index)
        if not can_remove_placeholders or not indexes:
            # This is an ordinary empty-payload error with a bound evidence
            # relation; let the exact-text/registered-payload compilers below
            # handle it instead of treating it as a placeholder.
            indexes.clear()
        else:
            before_count = len(requirements)
            removed_fingerprints: list[str] = []
            for index in sorted(indexes, reverse=True):
                removed = requirements.pop(index)
                removed_fingerprints.append(_response_sha256(removed))
                repairs.append({
                    "code": "empty_requirement_properties",
                    "rule_id": "remove_unbound_empty_requirement_placeholder_v1",
                    "removed_requirement_index": index,
                    "removed_requirement": copy.deepcopy(removed),
                    "removed_requirement_sha256": removed_fingerprints[-1],
                    "reason": "requirement has no clause/evidence binding or semantic payload",
                })
            repairs.append({
                "code": "empty_requirement_properties",
                "rule_id": "remove_unbound_empty_requirement_placeholder_v1",
                "removed_requirement_indexes": sorted(indexes),
                "removed_requirement_count": len(indexes),
                "before_requirement_count": before_count,
                "after_requirement_count": len(requirements),
                "projection": "ordered_mask",
                "removed_requirement_fingerprints": list(reversed(removed_fingerprints)),
                "source_response_sha256": _response_sha256(response),
                "repaired_response_sha256": _response_sha256(repaired),
            })
            return repaired, repairs

    # The source clause explicitly says the continuation caption may be
    # omitted. If the model nevertheless emits the boolean continuation flag
    # as required, the validator reports an exact, source-backed contradiction
    # rather than a semantic ambiguity. Normalize only that registered
    # contradiction; all other partial-coverage errors remain fail-closed.
    # A table-caption requirement can be emitted with an empty normalized
    # payload when the native provider supplies nullable role fields.  The
    # validator has already named the exact missing obligations, so compile
    # only the two registered source-backed facts below.  This does not infer
    # a caption from a generic table mention: every linked source clause must
    # explicitly state the position/alignment phrase before the field is
    # materialized.
    table_caption_partial_records = [
        record for record in error_records
        if isinstance(record, dict)
        and record.get("code") == "partial_clause_coverage"
        and "table_caption." in str(record.get("raw_error") or "")
    ]
    table_caption_empty_records = [
        record for record in error_records
        if isinstance(record, dict)
        and record.get("code") == "empty_requirement_properties"
    ]
    if table_caption_partial_records and table_caption_empty_records:
        if (
            not isinstance(chunk, dict)
            or any(
                not isinstance(record, dict)
                or record.get("code") not in {
                    "empty_requirement_properties", "partial_clause_coverage",
                }
                for record in error_records
            )
        ):
            return None, []
        required_fields: set[str] = set()
        for record in table_caption_partial_records:
            raw_error = str(record.get("raw_error") or "")
            suffix = raw_error.split("partial_clause_coverage:", 1)[-1]
            for gap in suffix.split(","):
                if gap not in {
                    "table_caption.position:above",
                    "table_caption.paragraph.alignment:center",
                }:
                    return None, []
                required_fields.add(gap)
        clauses = chunk.get("clauses")
        requirements = repaired.get("requirements")
        if not isinstance(clauses, list) or not isinstance(requirements, list):
            return None, []
        clauses_by_id = {
            str(clause.get("id")): clause
            for clause in clauses
            if isinstance(clause, dict) and clause.get("id")
        }
        seen_indexes: set[int] = set()
        for record in table_caption_empty_records:
            pointer = str(record.get("json_pointer") or "")
            match = re.fullmatch(r"\$\.requirements\[(\d+)\]\.properties", pointer)
            if match is None:
                return None, []
            requirement_index = int(match.group(1))
            if requirement_index in seen_indexes or requirement_index >= len(requirements):
                return None, []
            requirement = requirements[requirement_index]
            properties = requirement.get("properties") if isinstance(requirement, dict) else None
            clause_ids = requirement.get("clause_ids") if isinstance(requirement, dict) else None
            if (
                not isinstance(requirement, dict)
                or requirement.get("role") != "table_caption"
                or properties != {}
                or not isinstance(clause_ids, list)
                or not clause_ids
            ):
                return None, []
            linked_text = " ".join(
                str(clauses_by_id[clause_id].get("text") or clauses_by_id[clause_id].get("source_text_full") or "")
                for clause_id in map(str, clause_ids)
                if clause_id in clauses_by_id
            )
            if not linked_text or not re.search(r"表", linked_text):
                return None, []
            if "table_caption.position:above" in required_fields and not re.search(
                r"表上方|置于表(?:的)?上方|表上.*居中|居中.*表上", linked_text
            ):
                return None, []
            if "table_caption.paragraph.alignment:center" in required_fields and "居中" not in linked_text:
                return None, []
            if "table_caption.position:above" in required_fields:
                properties["position"] = "above"
                repairs.append({
                    "code": "partial_clause_coverage",
                    "json_pointer": f"$.requirements[{requirement_index}].properties.position",
                    "replacement": "above",
                    "rule_id": "compile_explicit_table_caption_position_v1",
                    "source_clause_ids": [str(value) for value in clause_ids],
                })
            if "table_caption.paragraph.alignment:center" in required_fields:
                properties["paragraph"] = {"alignment": "center"}
                repairs.append({
                    "code": "partial_clause_coverage",
                    "json_pointer": f"$.requirements[{requirement_index}].properties.paragraph.alignment",
                    "replacement": "center",
                    "rule_id": "compile_explicit_table_caption_alignment_v1",
                    "source_clause_ids": [str(value) for value in clause_ids],
                })
            seen_indexes.add(requirement_index)
        return repaired, repairs

    partial_records = [
        record for record in error_records
        if isinstance(record, dict) and record.get("code") == "partial_clause_coverage"
    ]
    if partial_records:
        if len(partial_records) != len(error_records) or not isinstance(chunk, dict):
            return None, []
        clauses = chunk.get("clauses")
        reviews = repaired.get("clause_reviews")
        requirements = repaired.get("requirements")
        if not isinstance(clauses, list) or not isinstance(reviews, list) or not isinstance(requirements, list):
            return None, []
        clauses_by_id = {
            str(clause.get("id")): clause
            for clause in clauses
            if isinstance(clause, dict) and clause.get("id")
        }
        for record in partial_records:
            raw_error = str(record.get("raw_error") or "")
            if not raw_error.endswith("partial_clause_coverage:table.continuation.optional_caption_marked_required"):
                return None, []
            match = re.fullmatch(r"\$\.clause_reviews\[(\d+)\]", str(record.get("json_pointer") or ""))
            if match is None:
                return None, []
            review_index = int(match.group(1))
            if review_index >= len(reviews) or not isinstance(reviews[review_index], dict):
                return None, []
            clause_id = str(reviews[review_index].get("clause_id") or "")
            clause = clauses_by_id.get(clause_id)
            clause_text = str((clause or {}).get("text") or (clause or {}).get("source_text_full") or "")
            if not re.search(r"表", clause_text) or not re.search(r"续", clause_text):
                return None, []
            if not re.search(r"可省略|可略", clause_text):
                return None, []
            matched = 0
            for requirement_index, requirement in enumerate(requirements):
                if not isinstance(requirement, dict) or clause_id not in {
                    str(value) for value in requirement.get("clause_ids", [])
                }:
                    continue
                properties = requirement.get("properties")
                continuation = properties.get("continuation") if isinstance(properties, dict) else None
                if requirement.get("role") != "table" or not isinstance(continuation, dict):
                    continue
                if continuation.get("caption_required_on_continuation") is not True:
                    return None, []
                continuation["caption_required_on_continuation"] = False
                matched += 1
                repairs.append({
                    "code": "partial_clause_coverage",
                    "json_pointer": f"$.requirements[{requirement_index}].properties.continuation.caption_required_on_continuation",
                    "replacement": False,
                    "rule_id": "optional_continuation_caption_is_not_required",
                    "source_clause_id": clause_id,
                })
            if matched != 1:
                return None, []
        return repaired, repairs

    # A small, explicit compiler rule handles the one registered source fact
    # that can be derived without a semantic decision. The model often writes
    # the natural-language condition from the clause verbatim. When the
    # clause itself explicitly says that English text uses Times New Roman,
    # the only safe normalization is the registered presence fact; arbitrary
    # fact renames remain fail-closed.
    applicability_records = [
        record for record in error_records
        if isinstance(record, dict)
        and record.get("code") == "applicability_fact_namespace"
    ]
    if applicability_records and len(applicability_records) != len(error_records):
        return None, []
    for record in applicability_records:
        pointer = record.get("json_pointer")
        match = re.fullmatch(
            r"\$\.requirements\[(\d+)\]\.applicability\.conditions\[(\d+)\]\.fact",
            str(pointer or ""),
        )
        if match is None or not isinstance(chunk, dict):
            return None, []
        requirement_index, condition_index = (int(value) for value in match.groups())
        requirements = repaired.get("requirements")
        clauses = chunk.get("clauses")
        if (
            not isinstance(requirements, list)
            or requirement_index >= len(requirements)
            or not isinstance(requirements[requirement_index], dict)
            or not isinstance(clauses, list)
        ):
            return None, []
        requirement = requirements[requirement_index]
        applicability = requirement.get("applicability")
        conditions = applicability.get("conditions") if isinstance(applicability, dict) else None
        if (
            not isinstance(applicability, dict)
            or applicability.get("status") != "conditional"
            or not isinstance(conditions, list)
            or condition_index >= len(conditions)
            or not isinstance(conditions[condition_index], dict)
        ):
            return None, []
        condition = conditions[condition_index]
        clause_ids = {str(value) for value in requirement.get("clause_ids", [])}
        linked_text = " ".join(
            str(clause.get("text") or clause.get("source_text_full") or "")
            for clause in clauses
            if isinstance(clause, dict) and str(clause.get("id")) in clause_ids
        )
        if (
            not re.search(r"论文中出现英文", linked_text)
            or not re.search(r"Times\s+New\s+Roman", linked_text, re.I)
            or requirement.get("role") != "body_text"
            or not isinstance(requirement.get("properties"), dict)
            or not isinstance(requirement["properties"].get("font"), dict)
            or requirement["properties"]["font"].get("latin") != "Times New Roman"
            or condition.get("operator") != "present"
            or condition.get("value") is not None
        ):
            return None, []
        condition["fact"] = "source_inventory.english_text"
        repairs.append({
            "code": "applicability_fact_namespace",
            "json_pointer": str(pointer),
            "replacement": "source_inventory.english_text",
            "rule_id": "explicit_english_presence_times_new_roman",
            "source_clause_ids": sorted(clause_ids),
        })
    if applicability_records:
        return repaired, repairs

    placeholder_records = [
        record for record in error_records
        if isinstance(record, dict)
        and record.get("code") == "cover_institution_placeholder"
    ]
    if placeholder_records and len(placeholder_records) == len(error_records):
        requirements = repaired.get("requirements")
        if not isinstance(requirements, list):
            return None, []
        seen_indexes: set[int] = set()
        for record in placeholder_records:
            pointer = str(record.get("json_pointer") or "")
            match = re.fullmatch(
                r"\$\.requirements\[(\d+)\]\.properties(?:\.institution)?",
                pointer,
            )
            if match is None:
                return None, []
            requirement_index = int(match.group(1))
            if requirement_index in seen_indexes:
                continue
            if requirement_index >= len(requirements):
                return None, []
            requirement = requirements[requirement_index]
            properties = requirement.get("properties") if isinstance(requirement, dict) else None
            if (
                not isinstance(requirement, dict)
                or requirement.get("role") != "cover"
                or not isinstance(properties, dict)
                or str(properties.get("institution") or "").strip()
                or not isinstance(properties.get("fields"), list)
                or properties.get("missing_value_policy") != "placeholder"
                or properties.get("missing_value_placeholder") != NEUTRAL_COVER_PLACEHOLDER
            ):
                return None, []
            properties["institution"] = NEUTRAL_COVER_PLACEHOLDER
            seen_indexes.add(requirement_index)
            repairs.append({
                "code": "cover_institution_placeholder",
                "json_pointer": f"$.requirements[{requirement_index}].properties.institution",
                "replacement": NEUTRAL_COVER_PLACEHOLDER,
                "rule_id": "compile_neutral_cover_institution_placeholder_v1",
                "reason": "cover chunk has no trusted institution value",
            })
        return repaired, repairs

    def resolve_pointer(pointer: str) -> Any:
        if not isinstance(pointer, str) or not pointer.startswith("$"):
            return None
        tokens = re.findall(r"\.([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]", pointer[1:])
        current: Any = repaired
        for key, index in tokens:
            if key:
                if not isinstance(current, dict) or key not in current:
                    return None
                current = current[key]
            else:
                if not isinstance(current, list):
                    return None
                position = int(index)
                if position >= len(current):
                    return None
                current = current[position]
        return current

    for record in error_records:
        pointer = record.get("json_pointer")
        raw_error = str(record.get("raw_error") or "")
        unknown_match = re.search(r"unknown property ['\"]([^'\"]+)['\"]", raw_error)
        evidence_match = re.search(r"not_backed_by_clause:([^;\s]+)", raw_error)
        if not isinstance(pointer, str):
            return None, []
        target = resolve_pointer(pointer)
        if record.get("code") == "unknown_property":
            if unknown_match is None:
                return None, []
            property_name = unknown_match.group(1)
            if not isinstance(target, dict) or property_name not in target:
                return None, []
            del target[property_name]
            repairs.append({
                "code": "unknown_property",
                "json_pointer": pointer,
                "removed_property": property_name,
            })
            # A provider can put a role's layout-only fields into the
            # generic properties object.  After the validator-directed
            # removals, the only remaining safe completion is the one exact
            # text supported by the cited evidence.  Do not synthesize this
            # for a non-text role or when any other payload remains.
            requirement_match = re.fullmatch(
                r"\$\.requirements\[(\d+)\]\.properties", pointer,
            )
            requirements = repaired.get("requirements")
            requirement_index = (
                int(requirement_match.group(1))
                if requirement_match is not None else None
            )
            requirement = (
                requirements[requirement_index]
                if isinstance(requirements, list)
                and requirement_index is not None
                and requirement_index < len(requirements)
                and isinstance(requirements[requirement_index], dict)
                else None
            )
            if (
                isinstance(requirement, dict)
                and isinstance(chunk, dict)
                and isinstance(target, dict)
                and all(value is None for value in target.values())
            ):
                exact_text = _exact_cited_text(requirement, chunk)
                if exact_text is not None:
                    target["text"] = exact_text
                    repairs.append({
                        "code": "empty_requirement_properties",
                        "json_pointer": pointer,
                        "filled_property": "text",
                        "source_evidence_ids": [
                            str(evidence_id)
                            for evidence_id in requirement.get("evidence_ids", [])
                        ],
                        "value": exact_text,
                        "after_unknown_property_removal": True,
                    })
        elif record.get("code") == "cover_institution_placeholder":
            match = re.fullmatch(
                r"\$\.requirements\[(\d+)\]\.properties(?:\.institution)?",
                pointer,
            )
            requirements = repaired.get("requirements")
            if match is None or not isinstance(requirements, list):
                return None, []
            requirement_index = int(match.group(1))
            if requirement_index >= len(requirements):
                return None, []
            requirement = requirements[requirement_index]
            properties = requirement.get("properties") if isinstance(requirement, dict) else None
            if (
                not isinstance(requirement, dict)
                or requirement.get("role") != "cover"
                or not isinstance(properties, dict)
                or not isinstance(properties.get("fields"), list)
                or properties.get("missing_value_policy") != "placeholder"
                or properties.get("missing_value_placeholder") != NEUTRAL_COVER_PLACEHOLDER
            ):
                return None, []
            institution = properties.get("institution")
            if institution not in ("", None, NEUTRAL_COVER_PLACEHOLDER):
                return None, []
            if institution != NEUTRAL_COVER_PLACEHOLDER:
                properties["institution"] = NEUTRAL_COVER_PLACEHOLDER
                repairs.append({
                    "code": "cover_institution_placeholder",
                    "json_pointer": f"$.requirements[{requirement_index}].properties.institution",
                    "replacement": NEUTRAL_COVER_PLACEHOLDER,
                    "rule_id": "compile_neutral_cover_institution_placeholder_v1",
                    "reason": "cover chunk has no trusted institution value",
                })
        elif (
            record.get("code") == "contract_validation_error"
            and "items must be unique" in raw_error
        ):
            match = re.fullmatch(
                r"\$\.requirements\[(\d+)\]\.properties\.items\[(\d+)\]\.source_evidence_ids",
                pointer,
            )
            if match is None or not isinstance(target, list):
                # A parent anyOf error is only a summary when a more specific
                # child error is present; the child branch below owns repair.
                if (
                    re.fullmatch(r"\$\.requirements\[(\d+)\]\.properties", pointer)
                    and any(
                        isinstance(other, dict)
                        and str(other.get("json_pointer") or "").startswith(pointer + ".")
                        for other in error_records
                    )
                ):
                    continue
                return None, []
            deduplicated: list[Any] = []
            for evidence_id in target:
                if evidence_id not in deduplicated:
                    deduplicated.append(evidence_id)
            if deduplicated == target:
                return None, []
            removed_duplicate_count = len(target) - len(deduplicated)
            target[:] = deduplicated
            repairs.append({
                "code": "contract_validation_error",
                "json_pointer": pointer,
                "rule_id": "deduplicate_first_occurrence_source_evidence_ids_v1",
                "removed_duplicate_count": removed_duplicate_count,
            })
        elif record.get("code") == "contract_validation_error":
            # A parent anyOf/schema error is diagnostic when a more specific
            # child record in the same payload identifies the exact repair.
            if (
                re.fullmatch(r"\$\.requirements\[(\d+)\]\.properties", pointer)
                and any(
                    isinstance(other, dict)
                    and str(other.get("json_pointer") or "").startswith(pointer + ".")
                    for other in error_records
                )
            ):
                continue
            return None, []
        elif record.get("code") == "evidence_relation_mismatch":
            if evidence_match is None or not isinstance(target, list):
                return None, []
            evidence_id = evidence_match.group(1)
            if evidence_id not in target:
                return None, []
            target[:] = [item for item in target if item != evidence_id]
            repairs.append({
                "code": "evidence_relation_mismatch",
                "json_pointer": pointer,
                "removed_evidence_id": evidence_id,
            })
        elif record.get("code") == "empty_requirement_properties":
            match = re.fullmatch(r"\$\.requirements\[(\d+)\]\.properties", pointer)
            requirements = repaired.get("requirements")
            if (
                match is None
                or not isinstance(requirements, list)
                or int(match.group(1)) >= len(requirements)
                or not isinstance(requirements[int(match.group(1))], dict)
                or not isinstance(target, dict)
                or any(value is not None for value in target.values())
                or not isinstance(chunk, dict)
            ):
                return None, []
            requirement = requirements[int(match.group(1))]
            if requirement.get("role") == "appendices":
                clauses = chunk.get("clauses")
                evidence_context = chunk.get("evidence_context")
                if not isinstance(clauses, list) or not isinstance(evidence_context, dict):
                    return None, []
                linked_clause_ids = {
                    str(value) for value in requirement.get("clause_ids", [])
                }
                linked_evidence_ids = {
                    str(value) for value in requirement.get("evidence_ids", [])
                }
                source_texts = [
                    str(clause.get("text") or clause.get("source_text_full") or "")
                    for clause in clauses
                    if isinstance(clause, dict) and str(clause.get("id")) in linked_clause_ids
                ]
                source_texts.extend(
                    str(evidence_context[evidence_id].get("text") or "")
                    for evidence_id in linked_evidence_ids
                    if isinstance(evidence_context.get(evidence_id), dict)
                )
                if source_texts and all(
                    re.search(r"附录.*正文.*另起页", text, re.S)
                    for text in source_texts
                ):
                    target.clear()
                    target["page_break_each"] = True
                    repairs.append({
                        "code": "empty_requirement_properties",
                        "json_pointer": pointer,
                        "filled_property": "page_break_each",
                        "value": True,
                        "rule_id": "compile_exact_appendix_page_break_v1",
                        "source_clause_ids": sorted(linked_clause_ids),
                        "source_evidence_ids": sorted(linked_evidence_ids),
                    })
                    continue

                if (
                    source_texts
                    and all(
                        re.search(r"附录.*序号.*A.*B.*C", text, re.S)
                        and re.search(r"每个附录.*标题", text, re.S)
                        for text in source_texts
                    )
                ):
                    target.clear()
                    target.update({
                        "label_style": "alpha_upper",
                        "per_appendix_title_required": True,
                    })
                    repairs.append({
                        "code": "empty_requirement_properties",
                        "json_pointer": pointer,
                        "filled_properties": [
                            "label_style", "per_appendix_title_required",
                        ],
                        "value": {
                            "label_style": "alpha_upper",
                            "per_appendix_title_required": True,
                        },
                        "rule_id": "compile_exact_appendix_label_title_v1",
                        "source_clause_ids": sorted(linked_clause_ids),
                        "source_evidence_ids": sorted(linked_evidence_ids),
                    })
                    continue
            if requirement.get("role") == "content_constraints":
                clauses = chunk.get("clauses")
                evidence_context = chunk.get("evidence_context")
                if not isinstance(clauses, list) or not isinstance(evidence_context, dict):
                    return None, []
                linked_clause_ids = {
                    str(value) for value in requirement.get("clause_ids", [])
                }
                linked_texts = [
                    str(clause.get("text") or clause.get("source_text_full") or "")
                    for clause in clauses
                    if isinstance(clause, dict) and str(clause.get("id")) in linked_clause_ids
                ]
                evidence_texts = [
                    str(evidence_context[str(evidence_id)].get("text") or "")
                    for evidence_id in requirement.get("evidence_ids", [])
                    if str(evidence_id) in evidence_context
                    and isinstance(evidence_context[str(evidence_id)], dict)
                ]
                source_texts = [*linked_texts, *evidence_texts]
                values = {
                    int(match.group(1))
                    for text in source_texts
                    for match in re.finditer(r"字数\s*一般?不超过\s*(\d+)\s*字", text)
                }
                if len(values) == 1:
                    max_chars = next(iter(values))
                    target["acknowledgments"] = {"max_chars": max_chars}
                    repairs.append({
                        "code": "empty_requirement_properties",
                        "json_pointer": pointer,
                        "filled_property": "acknowledgments.max_chars",
                        "value": max_chars,
                        "rule_id": "compile_exact_acknowledgments_max_chars_v1",
                        "source_clause_ids": sorted(linked_clause_ids),
                        "source_evidence_ids": [
                            str(evidence_id)
                            for evidence_id in requirement.get("evidence_ids", [])
                        ],
                    })
                    continue
            requirements = repaired.get("requirements")
            requirement = (
                requirements[int(match.group(1))]
                if isinstance(requirements, list) and int(match.group(1)) < len(requirements)
                else None
            )
            if not isinstance(requirement, dict):
                return None, []
            evidence_context = chunk.get("evidence_context")
            evidence_ids = requirement.get("evidence_ids")
            if not isinstance(evidence_context, dict) or not isinstance(evidence_ids, list):
                return None, []
            exact_text = _exact_cited_text(requirement, chunk)
            if exact_text is None:
                return None, []
            target["text"] = exact_text
            repairs.append({
                "code": "empty_requirement_properties",
                "json_pointer": pointer,
                "filled_property": "text",
                "source_evidence_ids": [
                    str(evidence_id)
                    for evidence_id in evidence_ids
                    if isinstance(evidence_context.get(str(evidence_id)), dict)
                    and evidence_context[str(evidence_id)].get("text") == exact_text
                ],
                "value": exact_text,
            })
        else:
            match = re.fullmatch(r"\$\.requirements\[(\d+)\]\.properties", pointer)
            if match is None or not isinstance(target, dict):
                return None, []
            requirement_index = int(match.group(1))
            requirements = repaired.get("requirements")
            if (
                not isinstance(requirements, list)
                or requirement_index >= len(requirements)
                or not isinstance(requirements[requirement_index], dict)
                or any(value is not None for value in target.values())
                or not isinstance(chunk, dict)
            ):
                return None, []
            evidence_context = chunk.get("evidence_context")
            requirement = requirements[requirement_index]
            evidence_ids = requirement.get("evidence_ids")
            if not isinstance(evidence_context, dict) or not isinstance(evidence_ids, list):
                return None, []
            evidence_items = [
                evidence_context.get(str(evidence_id))
                for evidence_id in evidence_ids
            ]
            exact_text = _exact_cited_text(requirement, chunk)
            if exact_text is not None:
                target["text"] = exact_text
                repairs.append({
                    "code": "empty_requirement_properties",
                    "json_pointer": pointer,
                    "filled_property": "text",
                    "source_evidence_ids": [
                        str(evidence_id)
                        for evidence_id, item in zip(evidence_ids, evidence_items)
                        if isinstance(item, dict) and item.get("text") == exact_text
                    ],
                    "value": exact_text,
                })
            else:
                return None, []
    return repaired, repairs


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
        repair_guidance = _strip_v2_relation_guidance(repair_guidance)
    if retry_hint:
        retry_guidance = (
            _structured_contract_repair_guidance(
                retry_error_records, contract_version=contract_version,
            )
            if retry_error_records else _contract_repair_guidance(retry_hint, include_base=False)
        )
        if contract_version == HOST_REVIEW_CONTRACT_V3:
            retry_guidance = _strip_v2_relation_guidance(retry_guidance)
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
    retry_codes = {
        str(item.get("code")) for item in (retry_error_records or [])
        if isinstance(item, dict)
    }
    v3_relation_addition_retry = (
        contract_version == HOST_REVIEW_CONTRACT_V3
        and bool(retry_codes & {"requirement_relation_mismatch", "missing_derived_requirement"})
    )
    v3_non_requirement_projection_retry = (
        contract_version == HOST_REVIEW_CONTRACT_V3
        and "non_requirement_classification_relation" in retry_codes
    )
    if retry_parent_response_path is not None:
        retry_parent_inline = None
        try:
            parent_response = json.loads(
                retry_parent_response_path.read_text(encoding="utf-8")
            )
            if isinstance(parent_response, dict):
                # The parent is a semantic repair baseline, not an identity
                # source.  Keep the exact payload available in the prompt so
                # a native agent cannot silently regenerate the whole response
                # merely because it failed to open the sidecar file.
                parent_response = copy.deepcopy(parent_response)
                parent_response.pop("provenance", None)
                retry_parent_inline = json.dumps(
                    parent_response, ensure_ascii=False, indent=2,
                )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            # The path and hash remain in the prompt.  The bridge still fails
            # closed if the retry changes unapproved semantic fields.
            retry_parent_inline = None
        requirement_change_rule = (
            "For every distinct executable clause explicitly identified by the validator as missing an authoritative requirement edge, "
            "the retry must add one evidence-backed requirement for that clause. Preserve every existing non-placeholder "
            "requirement and review unchanged. If the parent contains a requirement with empty clause_ids, empty evidence_ids, "
            "empty properties, and an empty reason, remove only that unbound provider placeholder; never assign it a guessed clause."
            if v3_relation_addition_retry else
            "For non_requirement_classification_relation, remove only the validator-identified requirement objects whose clause_ids are all classified as non-requirement states. Preserve every clause_review and every other requirement byte-for-byte; do not reclassify, split, merge, reorder, or invent a replacement."
            if v3_non_requirement_projection_retry else
            "For a clause with executable_review_requires_all_obligations_covered, preserve every obligation id and status. "
            "If an obligation remains non-covered, reclassify only that clause to the most accurate non-executable classification "
            "and remove that clause from every requirements[].clause_ids edge; do not change any other review or requirement."
            if bool(retry_codes & {"executable_review_obligations_uncovered"}) else
            "Do not split, merge, add, drop, or reorder requirements, and do not reclassify a clause."
        )
        parent_text = f"""The rejected parent response is available only as a repair baseline at:
{retry_parent_response_path}
Read that file on this retry. The current chunk packet and cited evidence remain
the semantic authority. Preserve every non-error semantic field from the parent
response exactly: clause IDs, classifications, obligations, roles, clause_ids,
evidence_ids, applicability, prerequisites, verification, and unrelated
properties. The requirement-list membership and count may change only under
the exact requirement-list projection explicitly authorized below. Apply only
the minimum mechanical edits explicitly
identified by the structured validator records above. {requirement_change_rule} Return the full
response object, not a patch. The bridge will reject any unapproved semantic
drift. Parent response sha256: {retry_parent_response_sha256 or 'unavailable'}.
Do not regenerate the response from the chunk when the baseline below is
available. Start from this exact semantic payload, preserve its clause_reviews
and top-level diagnostic fields, and apply only the validator-authorized
minimum change. The embedded payload excludes bridge-owned provenance:
"""
        if retry_parent_inline is not None:
            parent_text += (
                "\n<immutable_parent_response_without_provenance>\n"
                + retry_parent_inline
                + "\n</immutable_parent_response_without_provenance>"
            )
        else:
            parent_text += (
                "\nThe embedded parent payload is unavailable; read the sidecar "
                "path above and preserve it exactly."
            )
    else:
        parent_text = (
            f"The rejected parent response sha256 was {retry_parent_response_sha256}; "
            "there is no prior response file to reuse."
            if retry_parent_response_sha256 else "There is no prior response to reuse."
        )
    retry_invariant = (
        (
            """\nFINAL RETRY INVARIANT: copy every clause_review classification and obligation
from the repair baseline exactly. Preserve every non-placeholder requirement
identity, clause_ids, evidence_ids, and semantic payload. Add one new,
evidence-backed requirement for EACH distinct validator-identified executable
clause that lacks an authoritative edge. If the baseline contains an unbound
empty provider placeholder, remove only that placeholder. Do not turn an
unresolved or informational review into executable, do not invent properties,
and do not reuse one requirement for an unrelated clause. If a safe local
repair is not possible without changing semantics, return the parent object
unchanged and let the bridge fail closed."""
        ) if v3_relation_addition_retry else (
            """\nFINAL RETRY INVARIANT: copy every clause_review classification, obligation, reason, and evidence ID from the repair baseline exactly. Remove only the validator-identified requirement objects whose clause_ids are all bound to non-requirement classifications; preserve every other requirement object and all requirement fields exactly. Do not reclassify a clause, invent a replacement, or change any unrelated field. If this exact projection is not possible, return the parent unchanged and let the bridge fail closed."""
            if v3_non_requirement_projection_retry else
            """\nFINAL RETRY INVARIANT: copy every clause_review classification, obligation,
requirement identity, clause_ids, evidence_ids, and requirement count from the
repair baseline exactly. The only permitted differences are the exact property
paths named by the structured validator records above. If a review is already
classified executable, repair its missing relation only; do not turn an unresolved or informational review into executable. If a safe local repair is not possible
without changing semantics, return the parent object unchanged and let the
bridge fail closed."""
        )
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
If the packet contains fixed_declaration_candidates, they are deterministic
groupings of exact cited evidence. Any candidate clause classified executable,
covered, or verify_existing must have one declarations requirement covering its
candidate clause_ids; copy its heading/body text exactly and preserve the
supplied declaration anchor.
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
existing_requirement_id is an optional selector, NOT an output ID to allocate.
Use an ID only from eligible_existing_requirements and only for the exact supplied
role, clause_ids, evidence_ids and source occurrence. For a NEW requirement,
omit this field (use null in native structured output); deterministic code assigns
its final ID. Never increment, infer or copy an ID from another chunk.
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
    mechanical_repairs: list[dict[str, Any]] = []
    mechanical_repair_revalidation: dict[str, Any] | None = None
    contract_errors = validate_host_agent_response(response, chunk)
    if contract_errors:
        error_records = contract_error_records(
            contract_errors, response=response, chunk=chunk,
        )
        repaired_response, mechanical_repairs = _apply_safe_mechanical_repairs(
            response, error_records, chunk=chunk,
        )
        if repaired_response is not None:
            remaining_errors = validate_host_agent_response(repaired_response, chunk)
            if not remaining_errors:
                response = repaired_response
                mechanical_repair_revalidation = {
                    "status": "passed",
                    "remaining_error_count": 0,
                    "remaining_error_codes": [],
                }
            else:
                mechanical_repair_revalidation = {
                    "status": "failed",
                    "remaining_error_count": len(remaining_errors),
                    "remaining_error_codes": sorted({
                        str(error.get("code") or "unknown")
                        for error in remaining_errors
                        if isinstance(error, dict)
                    }),
                }
                error = ValueError(
                    "local response contract validation failed before provenance binding: "
                    + _summarize_contract_errors(remaining_errors)
                )
                remaining_error_records = contract_error_records(
                    remaining_errors, response=response, chunk=chunk,
                )
                # Keep the validator evidence that authorized a mechanical
                # projection even when that projection did not make the
                # entire response valid.  A subsequent native retry may
                # legitimately repeat that same bounded correction (for
                # example, replacing an empty cover institution or removing
                # a duplicate evidence ID) while also repairing unrelated
                # schema fields.  Dropping the original records would make
                # the retry-drift guard mistake the authorized correction for
                # semantic rewriting.
                combined_error_records: list[dict[str, Any]] = []
                seen_error_record_keys: set[tuple[str, str, str]] = set()
                for record in [*error_records, *remaining_error_records]:
                    if not isinstance(record, dict):
                        continue
                    record_key = (
                        str(record.get("code") or ""),
                        str(record.get("json_pointer") or ""),
                        str(record.get("raw_error") or ""),
                    )
                    if record_key in seen_error_record_keys:
                        continue
                    seen_error_record_keys.add(record_key)
                    combined_error_records.append(record)
                error.error_records = combined_error_records  # type: ignore[attr-defined]
                raise error
        else:
            error = ValueError(
                "local response contract validation failed before provenance binding: "
                + _summarize_contract_errors(contract_errors)
            )
            error.error_records = error_records  # type: ignore[attr-defined]
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
        "raw_response_sha256": _response_sha256(raw_response),
        "accepted_response_sha256": _response_sha256(response),
        "mechanical_repair_policy": (
            "remove_informational_only_requirement_v1 is the only relation projection; "
            "all other relation categories fail closed"
        ),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "retry_parent_response_sha256": retry_parent_response_sha256,
    }
    if mechanical_repairs:
        audit["mechanical_repairs"] = mechanical_repairs
        audit["mechanical_repair_count"] = len(mechanical_repairs)
        audit["mechanical_repair_revalidation"] = mechanical_repair_revalidation or {
            "status": "not_run",
            "remaining_error_count": None,
            "remaining_error_codes": [],
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
                        response_schema = (
                            chunk.get("response_schema")
                            if isinstance(chunk.get("response_schema"), dict)
                            else {}
                        )
                        current_response = normalize_native_response(
                            _read_json(
                                attempt_response_path,
                                label=f"Host Agent response {index} attempt {attempt}",
                            ),
                            response_schema,
                        )
                        relation_repair, relation_repair_audit = _v3_relation_completion_response(
                            previous_response,
                            current_response,
                            retry_error_records,
                            chunk=chunk,
                        )
                        if relation_repair is not None:
                            relation_repair = _bind_current_invocation_provenance(
                                relation_repair, provenance,
                            )
                            atomic_write_text(
                                attempt_response_path,
                                json.dumps(relation_repair, ensure_ascii=False, indent=2) + "\n",
                            )
                            current_response = relation_repair
                            audit.setdefault("semantic_retry_repairs", []).append(
                                relation_repair_audit
                            )
                        obligation_repair, obligation_repair_audit = (
                            _v3_uncovered_obligation_reclassification_response(
                                previous_response,
                                current_response,
                                retry_error_records,
                                chunk=chunk,
                            )
                        )
                        if obligation_repair is not None:
                            obligation_repair = _bind_current_invocation_provenance(
                                obligation_repair, provenance,
                            )
                            atomic_write_text(
                                attempt_response_path,
                                json.dumps(obligation_repair, ensure_ascii=False, indent=2) + "\n",
                            )
                            current_response = obligation_repair
                            audit.setdefault("semantic_retry_repairs", []).append(
                                obligation_repair_audit
                            )
                        change_error, semantic_changes = _retry_semantic_change_error(
                            previous_response,
                            current_response,
                            retry_error_records,
                            contract_version=contract_version,
                            chunk=chunk,
                        )
                        if change_error is not None:
                            with lifecycle_lock:
                                chunk_lifecycle[index].setdefault("semantic_retry_changes", []).extend(
                                    semantic_changes
                                )
                            raise change_error
                        if semantic_changes:
                            audit["semantic_retry_changes"] = semantic_changes
                            if all(
                                isinstance(record, dict)
                                and record.get("code") == "informational_requirement_forbidden"
                                for record in retry_error_records
                            ):
                                audit["semantic_retry_change_policy"] = (
                                    "code_owned_informational_projection"
                                )
                            elif all(
                                isinstance(record, dict)
                                and record.get("code") in {
                                    "missing_clause_review", "contract_validation_error",
                                }
                                for record in retry_error_records
                            ) and any(
                                isinstance(record, dict)
                                and record.get("code") == "missing_clause_review"
                                for record in retry_error_records
                            ):
                                audit["semantic_retry_change_policy"] = (
                                    "code_owned_missing_clause_review_completion"
                                )
                            else:
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
                            if _response_sha256(previous_response) == _response_sha256(current_response):
                                with lifecycle_lock:
                                    chunk_lifecycle[index].setdefault(
                                        "no_progress_events", []
                                    ).append({
                                        "attempt": attempt,
                                        "response_sha256": _response_sha256(current_response),
                                        "reason": "retry_response_identical_to_parent",
                                    })
                            change_error, semantic_changes = _retry_semantic_change_error(
                                previous_response,
                                current_response,
                                previous_error_records,
                                contract_version=contract_version,
                                chunk=chunk,
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
