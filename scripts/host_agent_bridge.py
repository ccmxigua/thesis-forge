#!/usr/bin/env python3
"""Run the current OpenClaw Host Agent over a fresh semantic-review packet.

This is the optional host-runtime adapter for the otherwise provider-neutral
pipeline.  It deliberately invokes the local OpenClaw CLI instead of owning
an API client or an API key.  Every chunk receives a fresh isolated turn, the
run snapshots the invoking parent session's effective provider/model, and the
response is captured without delivery to Telegram.  The existing offline
merger remains the only authority that can accept the response.
"""
from __future__ import annotations

import argparse
import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import signal
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
# An omitted model is intentional: the bridge must inherit the parent
# session's route when requested, rather than silently binding a provider.
DEFAULT_HOST_AGENT_MODEL: str | None = None
PARENT_SESSION_ENV_NAMES = (
    "OPENCLAW_PARENT_SESSION_KEY",
    "OPENCLAW_SESSION_KEY",
)
AUTO_PARENT_SESSION_MAX_AGE_MINUTES = 10


class HostAgentRouteMismatch(RuntimeError):
    """Raised when a child did not execute on the run's route snapshot."""


def _run_command(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    """Run a child in its own process group and reap descendants on timeout.

    ``openclaw agent`` can spawn an ``openclaw-agent`` child that inherits the
    captured stdout/stderr pipes.  Killing only the CLI parent leaves those
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
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(
            command, timeout, output=stdout, stderr=stderr,
        ) from exc
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from compliance import classification_requires_requirement  # noqa: E402
from evidence_context_guards import sample_content_guard  # noqa: E402
from format_spec_validation import validate_instance  # noqa: E402
from requirements_engine import merge_host_agent_review_packets  # noqa: E402
from semantic_contract import validate_response_provenance  # noqa: E402


def _read_json(path: Path, *, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {label}: {path}: {exc}") from exc


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _strip_json_wrapper(text: str) -> str:
    """Accept raw JSON or one Markdown JSON fence, and nothing else."""
    value = text.strip()
    if value.startswith("```") and value.endswith("```"):
        lines = value.splitlines()
        if len(lines) < 3:
            raise ValueError("Host Agent returned an empty JSON fence")
        value = "\n".join(lines[1:-1]).strip()
        if not value:
            raise ValueError("Host Agent returned an empty JSON fence")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        # Models occasionally add one short sentence around an otherwise
        # valid object.  Recover only a single complete object; the local
        # contract/provenance merger still decides whether it is acceptable.
        start = value.find("{")
        end = value.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("Host Agent did not return a JSON object") from None
        candidate = value[start:end + 1]
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Host Agent returned invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Host Agent response must be one JSON object")
    return parsed


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
        "provenance": chunk.get("provenance"),
        "batch": chunk.get("batch"),
        "clauses": chunk.get("clauses", []),
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
                "contract_version", "provenance", "requirements", "clause_reviews",
                "unsupported_items", "reported_conflicts",
            ],
            "allowed_classifications": [
                "covered", "executable", "external_compliance", "ignored", "informational",
                "not_applicable", "requires_metadata", "requires_source_content", "unresolved",
                "unsupported", "unsupported_backend", "unverifiable", "verify_existing",
            ],
        },
    }


def _summarize_contract_errors(errors: list[str], *, limit: int = 12) -> str:
    """Make retry feedback bounded and deterministic without exposing payloads."""
    unique: list[str] = []
    for error in errors:
        if error not in unique:
            unique.append(error)
    suffix = f"; ... ({len(unique) - limit} more)" if len(unique) > limit else ""
    return "; ".join(unique[:limit]) + suffix


def validate_host_agent_response(response: Any, chunk: dict[str, Any]) -> list[str]:
    """Validate one chunk before it can be merged into the full response.

    This is deliberately a provider-neutral preflight.  It mirrors the
    response-side parts of the final semantic contract while retaining the
    final full-run merge as the authoritative gate.  Keeping the preflight
    here gives a retry a precise, local reason instead of waiting until the
    DOCX stage to discover schema drift.
    """
    if not isinstance(response, dict):
        return ["response_must_be_object"]

    errors: list[str] = []
    response_schema = chunk.get("response_schema")
    if not isinstance(response_schema, dict) or not response_schema:
        errors.append("response_schema_missing")
    else:
        errors.extend(validate_instance(response, response_schema, response_schema))

    contract = chunk.get("requirement_contract")
    if not isinstance(contract, dict):
        errors.append("requirement_contract_missing")
        contract = {}
    contract_root = {"$defs": contract.get("$defs", {})}
    role_schemas = contract.get("role_properties_schema", {})
    allowed_roles = set(contract.get("allowed_roles", []))

    clauses = chunk.get("clauses")
    if not isinstance(clauses, list):
        clauses = []
    clause_map = {
        str(item.get("id")): item
        for item in clauses
        if isinstance(item, dict) and item.get("id")
    }
    evidence_context = chunk.get("evidence_context")
    if not isinstance(evidence_context, dict):
        evidence_context = {}
    evidence_ids = {str(key) for key in evidence_context}

    requirements = response.get("requirements")
    if not isinstance(requirements, list):
        return errors + ["requirements_must_be_array"]

    requirement_clause_sets: list[set[str]] = []
    for index, item in enumerate(requirements):
        if not isinstance(item, dict):
            requirement_clause_sets.append(set())
            continue
        role = item.get("role")
        if role not in allowed_roles:
            errors.append(f"$.requirements[{index}].role: unknown_or_disallowed_role")
        role_schema = role_schemas.get(role) if isinstance(role_schemas, dict) else None
        if isinstance(role_schema, dict):
            try:
                errors.extend(validate_instance(
                    item.get("properties"), role_schema, contract_root,
                    f"$.requirements[{index}].properties",
                ))
            except (KeyError, ValueError) as exc:
                errors.append(
                    f"$.requirements[{index}].properties: role_schema_resolution_failed:{exc}"
                )

        clause_ids = item.get("clause_ids")
        clause_set = {str(value) for value in clause_ids} if isinstance(clause_ids, list) else set()
        requirement_clause_sets.append(clause_set)
        if not clause_set:
            errors.append(f"$.requirements[{index}].clause_ids: must be non-empty")
        unknown_clauses = sorted(clause_set - set(clause_map))
        if unknown_clauses:
            errors.append(
                f"$.requirements[{index}].clause_ids: unknown:{','.join(unknown_clauses)}"
            )

        cited_evidence = {
            str(value) for value in item.get("evidence_ids", [])
        } if isinstance(item.get("evidence_ids"), list) else set()
        allowed_evidence = {
            str(evidence_id)
            for clause_id in clause_set
            for evidence_id in (clause_map.get(clause_id, {}).get("evidence_ids", []) or [])
        }
        if not cited_evidence:
            errors.append(f"$.requirements[{index}].evidence_ids: must be non-empty")
        unknown_evidence = sorted(cited_evidence - evidence_ids)
        if unknown_evidence:
            errors.append(
                f"$.requirements[{index}].evidence_ids: not_in_chunk:{','.join(unknown_evidence)}"
            )
        unrelated_evidence = sorted(cited_evidence - allowed_evidence)
        if unrelated_evidence:
            errors.append(
                f"$.requirements[{index}].evidence_ids: not_backed_by_clause:{','.join(unrelated_evidence)}"
            )

    expected_clause_ids = [str(item.get("id")) for item in clauses if isinstance(item, dict)]
    reviews = response.get("clause_reviews")
    if not isinstance(reviews, list):
        return errors + ["clause_reviews_must_be_array"]
    actual_clause_ids = [
        str(item.get("clause_id")) for item in reviews if isinstance(item, dict)
    ]
    if (
        len(actual_clause_ids) != len(expected_clause_ids)
        or len(set(actual_clause_ids)) != len(actual_clause_ids)
        or set(actual_clause_ids) != set(expected_clause_ids)
    ):
        errors.append("clause_reviews_must_cover_each_chunk_clause_exactly_once")

    referenced_indexes: set[int] = set()
    for review_index, review in enumerate(reviews):
        if not isinstance(review, dict):
            continue
        clause_id = str(review.get("clause_id"))
        classification = review.get("classification")
        indexes = review.get("requirement_indexes")
        clause = clause_map.get(clause_id)
        if (
            isinstance(classification, str)
            and classification_requires_requirement(classification)
            and isinstance(clause, dict)
        ):
            guard = sample_content_guard(clause, clauses)
            if guard:
                errors.append(
                    f"$.clause_reviews[{review_index}]:"
                    f"sample_content_cannot_be_executable:{guard['kind']}"
                )
        if not isinstance(indexes, list):
            continue
        valid_indexes: list[int] = []
        for requirement_index in indexes:
            if (
                isinstance(requirement_index, bool)
                or not isinstance(requirement_index, int)
                or requirement_index < 0
                or requirement_index >= len(requirements)
            ):
                errors.append(
                    f"$.clause_reviews[{review_index}].requirement_indexes: out_of_range"
                )
                continue
            valid_indexes.append(requirement_index)
            referenced_indexes.add(requirement_index)
            if (
                isinstance(classification, str)
                and classification_requires_requirement(classification)
                and clause_id not in requirement_clause_sets[requirement_index]
            ):
                errors.append(
                    f"$.clause_reviews[{review_index}]: requirement_index_not_backed_by_clause"
                )
        if isinstance(classification, str):
            if classification_requires_requirement(classification) and not valid_indexes:
                errors.append(
                    f"$.clause_reviews[{review_index}]: executable_review_requires_requirement_index"
                )
            elif not classification_requires_requirement(classification) and valid_indexes:
                errors.append(
                    f"$.clause_reviews[{review_index}]: nonexecutable_review_must_not_reference_requirement"
                )

    unused_indexes = sorted(set(range(len(requirements))) - referenced_indexes)
    if unused_indexes:
        errors.append(
            "requirements_not_referenced_by_clause_review:" + ",".join(map(str, unused_indexes))
        )
    return errors


def parse_openclaw_result(stdout: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Extract the model payload from an OpenClaw JSON result envelope."""
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"openclaw agent did not return JSON: {exc}") from exc
    if not isinstance(envelope, dict):
        raise ValueError("openclaw agent JSON envelope must be an object")
    if envelope.get("status") not in {None, "ok"}:
        summary = envelope.get("summary") or envelope.get("status")
        raise ValueError(f"openclaw agent failed: {summary}")
    result = envelope.get("result")
    payloads = result.get("payloads") if isinstance(result, dict) else None
    if not isinstance(payloads, list):
        # ``openclaw agent exec --json`` projects payloads at the envelope
        # root, while ``openclaw agent --json`` nests them under result.
        payloads = envelope.get("payloads")
    if not isinstance(payloads, list):
        raise ValueError("OpenClaw response has no payloads array")
    texts = [item.get("text", "") for item in payloads
             if isinstance(item, dict) and isinstance(item.get("text"), str) and item.get("text")]
    if not texts:
        raise ValueError("openclaw agent returned no text payload")
    return _strip_json_wrapper("\n".join(texts)), envelope


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


def _host_prompt(*, request_path: Path, chunk_path: Path,
                 response_path: Path, run_id: str, chunk_index: int,
                 chunk_count: int, attempt: int = 1,
                 retry_hint: str | None = None) -> str:
    retry_text = ""
    if retry_hint:
        retry_text = (
            "\nThis is a retry after the previous attempt was rejected locally. "
            "Do not discuss the failure; return a newly generated valid JSON object. "
            f"Reason category: {retry_hint}.\n"
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
response_schema, declaration instructions, structure summary, and provenance.
The requirement_contract and response_schema are authoritative. Follow their
role-specific properties and nested schemas exactly; do not invent aliases or
free-form replacements for fields such as applicability, input_prerequisites,
verification, or confidence.
Do not read the full llm-request-chunks.json file, because it contains other
chunks that are outside this subtask.

This is chunk {chunk_index} of {chunk_count}, attempt {attempt}, run_id {run_id}. Read the chunk
JSON and follow its contract_version 2.1 instructions literally. The local
merger will validate the complete on-disk schema after the response. Review every
and only the supplied clause IDs exactly once. Copy the chunk's provenance
object unchanged. Cite only evidence and clause IDs present in this chunk.
For declaration clauses, copy fixed headings/body paragraphs exactly from the
cited evidence, use a run-local semantic item id, and use blank signature
placeholders only; never invent resource_id, version, or sha256.

Do not consult, copy, or repair any previous response, build directory,
school-specific resource, or conversation memory. The current chunk JSON is
the only semantic source. Do not modify project files. The runner will save
your JSON as:
{response_path}

Before answering, verify that the result is a complete contract-2.1 object
with requirements, clause_reviews, unsupported_items, reported_conflicts, and
the unchanged provenance object requested by the chunk.
{retry_text}
"""


def _resolve_openclaw(binary: str | None) -> str:
    if binary:
        return binary
    return shutil.which("openclaw") or "openclaw"


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
    result = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()[-1200:]
        raise RuntimeError(
            f"cannot inspect OpenClaw parent sessions (returncode {result.returncode}): {detail}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
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


def _is_host_agent_session(key: str) -> bool:
    return ":thesis-host-agent:" in key or ":host-model-probe" in key


def _select_auto_parent_session(
    records: list[dict[str, Any]],
    *,
    agent_id: str,
) -> dict[str, Any] | None:
    """Select the freshest likely interactive parent when no key was supplied.

    OpenClaw does not currently export the invoking session key to ordinary
    subprocesses.  Prefer a recent Telegram direct session (the normal skill
    entry point), then any other recent direct session.  Host-Agent probe and
    bridge sessions are excluded so a retry cannot inherit from itself.
    """
    candidates: list[dict[str, Any]] = []
    for record in records:
        key = record.get("key")
        if not isinstance(key, str) or not key.strip():
            continue
        if record.get("agentId") not in {None, agent_id}:
            continue
        if record.get("kind") not in {None, "direct"}:
            continue
        if _is_host_agent_session(key):
            continue
        if record.get("abortedLastRun") is True:
            continue
        candidates.append(record)
    if not candidates:
        return None
    telegram = [record for record in candidates if ":telegram:" in str(record.get("key"))]
    pool = telegram or candidates
    return max(pool, key=lambda record: int(record.get("updatedAt") or 0))


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
    max_age_minutes: int = AUTO_PARENT_SESSION_MAX_AGE_MINUTES,
) -> dict[str, Any]:
    """Resolve the parent session route for a Host-Agent run.

    An explicit key wins, followed by the two supported environment names.
    If neither is available, a recent interactive session is selected.  A
    parent without a session override uses its stored effective
    ``modelProvider`` + ``model`` route.  If that route cannot be resolved,
    this function fails closed instead of using the gateway default.
    """
    requested_key = (parent_session_key or _parent_session_from_environment() or "").strip()
    if requested_key:
        records = _load_session_records(openclaw_bin, agent_id=agent_id)
        record = next((item for item in records if item.get("key") == requested_key), None)
        if record is None:
            raise ValueError(f"parent session key was not found: {requested_key}")
        selected_key = requested_key
        source = "explicit-parent-session"
    else:
        if isinstance(max_age_minutes, bool) or max_age_minutes <= 0:
            raise ValueError("parent session max age must be a positive integer")
        records = _load_session_records(
            openclaw_bin, agent_id=agent_id, active_minutes=max_age_minutes,
        )
        record = _select_auto_parent_session(records, agent_id=agent_id)
        if record is None:
            raise ValueError("no recent interactive parent session with a resolvable route")
        selected_key = str(record["key"])
        source = "auto-recent-parent-session"
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
    openclaw_bin: str,
    prompt_path: Path,
    model: str | None = None,
    openclaw_config: Path | None = None,
    attempt: int = 1,
    retry_hint: str | None = None,
    auth_env_only: bool = False,
    runner: str = "exec",
) -> dict[str, Any]:
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
        ),
        encoding="utf-8",
    )
    session_key = (
        f"agent:{agent_id}:thesis-host-agent:{run_id}:chunk-{chunk_index:04d}"
        f":attempt-{attempt:02d}"
    )
    # ``openclaw agent`` accepts --model but still consults the global fallback
    # chain for a new child session.  ``agent exec`` lets this invocation pass
    # a route-local fallback list; repeating the snapshot route is deliberate:
    # a provider outage may retry on the same route, but may never jump to a
    # different user's/global provider.  The normal command remains available
    # for the explicit no-model compatibility mode and non-main agents.
    if runner not in {"exec", "gateway"}:
        raise ValueError(f"unsupported Host Agent runner: {runner}")
    use_isolated_exec = bool(model) and agent_id == "main" and runner == "exec"
    if use_isolated_exec:
        command = [
            openclaw_bin, "agent", "exec",
        ]
        if openclaw_config is not None:
            command.extend(["--config", str(openclaw_config)])
        command.extend([
            "--cwd", str(ROOT),
            "--model", model,
            "--fallback", model,
            "--message-file", str(prompt_path),
            "--json", "--timeout", str(timeout),
        ])
        if auth_env_only:
            command.append("--auth-env-only")
    elif auth_env_only and runner == "exec":
        raise ValueError(
            "--auth-env-only requires an explicit model route with agent_id=main"
        )
    else:
        command = [
            openclaw_bin, "agent", "--agent", agent_id,
            "--session-key", session_key,
            "--message-file", str(prompt_path),
        ]
        if model:
            command += ["--model", model]
        command += ["--json", "--timeout", str(timeout)]
    started = time.time()
    try:
        result = _run_command(command, timeout=max(timeout + 30, timeout))
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Host Agent chunk {chunk_index}/{chunk_count} exceeded {timeout}s"
        ) from exc
    elapsed = round(time.time() - started, 1)
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()[-1600:]
        raise RuntimeError(
            f"Host Agent chunk {chunk_index}/{chunk_count} failed with returncode "
            f"{result.returncode}: {detail}"
        )
    response, envelope = parse_openclaw_result(result.stdout)
    route = verify_host_agent_route(envelope, model)
    expected_provenance = chunk.get("provenance")
    observed_provenance = response.get("provenance") if isinstance(response, dict) else None
    provenance_mismatch_fields: list[str] = []
    if isinstance(expected_provenance, dict):
        if isinstance(observed_provenance, dict):
            provenance_mismatch_fields = sorted({
                str(key) for key in set(expected_provenance) | set(observed_provenance)
                if observed_provenance.get(key) != expected_provenance.get(key)
            })
        else:
            provenance_mismatch_fields = sorted(str(key) for key in expected_provenance)
        # Provenance is transport metadata owned by this bridge.  The model's
        # copy is retained only as an audit signal; a truncated or otherwise
        # damaged model echo must not make a valid immutable chunk unusable.
        response["provenance"] = copy.deepcopy(expected_provenance)
    response_path.write_text(
        json.dumps(response, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    audit = {
        "chunk_index": chunk_index,
        "chunk_count": chunk_count,
        "attempt": attempt,
        "session_key": session_key,
        "openclaw_run_id": envelope.get("runId"),
        "status": envelope.get("status", "ok"),
        "returncode": result.returncode,
        "elapsed_s": elapsed,
        "runner": "agent-exec" if use_isolated_exec else (
            "gateway-agent" if runner == "gateway" else "agent"
        ),
        "expected_route": model,
        "actual_provider": route.get("provider"),
        "actual_model": route.get("model"),
        "actual_route": route.get("route"),
        "fallback_used": route.get("fallback_used"),
        "response_path": str(response_path.resolve()),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    audit["provenance_copy_warning"] = (
        {
            "action": "bridge_injected_expected_chunk_provenance",
            "observed_present": isinstance(observed_provenance, dict),
            "mismatch_fields": provenance_mismatch_fields,
        }
        if provenance_mismatch_fields else None
    )
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
    inherit_parent_model: bool = False,
    parent_session_key: str | None = None,
    parent_session_max_age_minutes: int = AUTO_PARENT_SESSION_MAX_AGE_MINUTES,
    auth_env_only: bool = False,
    runner: str = "exec",
) -> dict[str, Any]:
    review_dir = review_dir.resolve()
    manifest_path = review_dir / "host-agent-review-manifest.json"
    manifest = _read_json(manifest_path, label="host-agent review manifest")
    if manifest.get("protocol") != "host_agent_semantic_review":
        raise ValueError("review manifest is not a host-agent semantic-review manifest")
    if manifest.get("contract_version") != "2.1":
        raise ValueError("host-agent review manifest is not contract 2.1")
    request_path = review_dir / str(manifest.get("request_path", "llm-request.json"))
    chunks_path = review_dir / str(manifest.get("request_chunks_path", "llm-request-chunks.json"))
    full_request = _read_json(request_path, label="host-agent request")
    chunks = _read_json(chunks_path, label="host-agent request chunks")
    response_files = manifest.get("response_files")
    if not isinstance(chunks, list) or not chunks:
        raise ValueError("host-agent request chunks are missing or empty")
    if not isinstance(response_files, list) or len(response_files) != len(chunks):
        raise ValueError("host-agent response file manifest does not match request chunks")
    expected_run_id = ((full_request.get("provenance") or {}).get("run_id")
                       if isinstance(full_request, dict) else None)
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

    response_out = (response_out or (review_dir.parent / "host-agent-response.json")).resolve()
    audit_path = review_dir / "host-agent-run.json"
    if audit_path.exists():
        raise ValueError(f"refusing to reuse an existing Host Agent audit: {audit_path}")
    if response_out.exists():
        raise ValueError(f"refusing to reuse an existing Host Agent response: {response_out}")

    started_at = datetime.now(timezone.utc).isoformat()
    prompt_dir = review_dir / "host-agent-prompts"
    binary = _resolve_openclaw(openclaw_bin)
    if openclaw_config is not None:
        openclaw_config = openclaw_config.expanduser().resolve()
        if not openclaw_config.is_file():
            raise ValueError(f"OpenClaw config does not exist: {openclaw_config}")
    resolution: dict[str, Any] = {
        "model": model,
        "source": "explicit-model" if model else "gateway-default",
        "parent_session_key": None,
        "parent_model_override": None,
        "parent_provider_override": None,
        "parent_effective_provider": None,
        "parent_effective_model": None,
    }
    if model is None and (inherit_parent_model or parent_session_key):
        resolution = resolve_parent_model(
            binary,
            agent_id=agent_id,
            parent_session_key=parent_session_key,
            max_age_minutes=parent_session_max_age_minutes,
        )
    effective_model = resolution.get("model")
    if effective_model:
        _split_model_route(str(effective_model))

    def persist_failure_audit(error: BaseException, chunk_runs: list[dict[str, Any]]) -> None:
        """Persist a terminal failure without manufacturing a merged response."""
        _write_json(audit_path, {
            "schema_version": "1.0",
            "status": "failed",
            "protocol": "host_agent_semantic_review",
            "run_id": effective_run_id,
            "agent_id": agent_id,
            "max_concurrency": max_concurrency,
            "max_attempts": max_attempts,
            "model": effective_model,
            "auth_env_only": bool(auth_env_only),
            "runner": runner,
            "model_source": resolution.get("source"),
            "openclaw_config": str(openclaw_config) if openclaw_config else None,
            "route_policy": "parent-effective-route-snapshot",
            "route_verification": "child-winner-must-match; fallback-must-be-false",
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "error_type": type(error).__name__,
            "error": str(error),
            "chunk_runs": chunk_runs,
            "merged_response_written": False,
        })

    def run_and_validate(index: int, chunk: dict[str, Any], response_name: str) -> dict[str, Any]:
        if not isinstance(chunk, dict):
            raise ValueError(f"host-agent chunk {index} is not an object")
        if not isinstance(response_name, str) or not response_name:
            raise ValueError(f"host-agent response filename {index} is invalid")
        response_path = (review_dir / response_name).resolve()
        if response_path.exists():
            raise ValueError(f"refusing to reuse an existing chunk response: {response_path}")
        provenance = chunk.get("provenance")
        if not isinstance(provenance, dict):
            raise ValueError(f"host-agent chunk {index} has no provenance")
        batch = chunk.get("batch") if isinstance(chunk.get("batch"), dict) else {}
        chunk_index = int(batch.get("index", index))
        chunk_count = int(batch.get("count", len(chunks)))
        failures: list[str] = []
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
                    openclaw_config=openclaw_config,
                    prompt_path=prompt_dir / f"prompt-{index:04d}-attempt-{attempt:02d}.txt",
                    model=effective_model,
                    attempt=attempt,
                    auth_env_only=auth_env_only,
                    runner=runner,
                    retry_hint=(
                        "local contract validation failed; repair the response: "
                        + failures[-1]
                        if failures else None
                    ),
                )
                response = _read_json(
                    attempt_response_path,
                    label=f"Host Agent response {index} attempt {attempt}",
                )
                if response.get("contract_version") != "2.1":
                    raise ValueError("response is not contract 2.1")
                provenance_errors = validate_response_provenance(
                    response, provenance, require_fresh_origin=True,
                )
                if provenance_errors:
                    raise ValueError(
                        "provenance failed: " + ", ".join(provenance_errors)
                    )
                contract_errors = validate_host_agent_response(response, chunk)
                if contract_errors:
                    raise ValueError(
                        "local response contract validation failed: "
                        + _summarize_contract_errors(contract_errors)
                    )
                attempt_response_path.replace(response_path)
                audit["attempt_failures"] = failures
                return audit
            except HostAgentRouteMismatch:
                # A route mismatch is not a model-quality error.  Retrying
                # would spend more tokens on an unauthorized route, so abort
                # the whole run immediately and preserve fail-closed behavior.
                raise
            except (OSError, ValueError, RuntimeError) as exc:
                failures.append(str(exc))
                if attempt >= max_attempts:
                    raise ValueError(
                        f"Host Agent response {index}/{len(chunks)} failed after "
                        f"{max_attempts} attempts: {failures[-1]}"
                    ) from exc
        raise AssertionError("unreachable Host Agent retry state")

    chunk_audits_by_index: dict[int, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(max_concurrency, len(chunks))) as executor:
        futures = {
            executor.submit(run_and_validate, index, chunk, response_name): index
            for index, (chunk, response_name) in enumerate(
                zip(chunks, response_files), start=1
            )
        }
        try:
            for future in as_completed(futures):
                index = futures[future]
                chunk_audits_by_index[index] = future.result()
        except Exception as exc:
            for future in futures:
                future.cancel()
            persist_failure_audit(
                exc,
                [chunk_audits_by_index[index]
                 for index in sorted(chunk_audits_by_index)],
            )
            raise

    chunk_audits = [chunk_audits_by_index[index]
                    for index in sorted(chunk_audits_by_index)]

    try:
        merged, merge_metadata = merge_host_agent_review_packets(
            review_dir, response_out=response_out,
        )
    except Exception as exc:
        persist_failure_audit(exc, chunk_audits)
        raise
    payload = {
        "schema_version": "1.0",
        "status": "merged",
        "protocol": "host_agent_semantic_review",
        "run_id": effective_run_id,
        "agent_id": agent_id,
        "max_concurrency": max_concurrency,
        "max_attempts": max_attempts,
        "model": effective_model,
        "auth_env_only": bool(auth_env_only),
        "runner": runner,
        "model_source": resolution.get("source"),
        "parent_session_key": resolution.get("parent_session_key"),
        "parent_model_override": resolution.get("parent_model_override"),
        "parent_provider_override": resolution.get("parent_provider_override"),
        "parent_effective_provider": resolution.get("parent_effective_provider"),
        "parent_effective_model": resolution.get("parent_effective_model"),
        "route_policy": "parent-effective-route-snapshot",
        "route_verification": "child-winner-must-match; fallback-must-be-false",
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
    parser.add_argument("--agent-id", default="main",
                        help="OpenClaw agent id used for the current Host Agent")
    parser.add_argument("--timeout", type=int, default=900,
                        help="per-chunk OpenClaw timeout in seconds (default: 900)")
    parser.add_argument("--max-concurrency", type=int, default=4,
                        help="maximum number of independent Host Agent chunks in flight (default: 4)")
    parser.add_argument("--max-attempts", type=int, default=2,
                        help="maximum attempts per chunk before failing closed (default: 2)")
    parser.add_argument("--model",
                        help="explicit OpenClaw provider/model route; omitted means inherit the parent session")
    parser.add_argument("--parent-session-key",
                        help="exact parent session key whose effective provider/model route should be copied")
    parser.add_argument("--parent-session-max-age-minutes", type=int,
                        default=AUTO_PARENT_SESSION_MAX_AGE_MINUTES,
                        help="max age for automatic parent-session discovery (default: 10 minutes)")
    parser.add_argument("--auth-env-only", action="store_true",
                        help="use provider credentials from environment variables only")
    parser.add_argument("--runner", choices=("exec", "gateway"), default="exec",
                        help="OpenClaw invocation path (default: exec)")
    parser.add_argument("--inherit-parent-model", dest="inherit_parent_model",
                        action="store_true", default=True,
                        help="copy the parent session model override when --model is omitted (default)")
    parser.add_argument("--no-inherit-parent-model", dest="inherit_parent_model",
                        action="store_false",
                        help="use OpenClaw's ordinary default route when --model is omitted")
    parser.add_argument("--openclaw-bin",
                        help="optional path to the openclaw executable")
    parser.add_argument("--openclaw-config", type=Path,
                        help="optional config file passed explicitly to `openclaw agent exec`")
    args = parser.parse_args(argv)
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
            inherit_parent_model=args.inherit_parent_model,
            parent_session_key=args.parent_session_key,
            parent_session_max_age_minutes=args.parent_session_max_age_minutes,
            auth_env_only=args.auth_env_only,
            runner=args.runner,
        )
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"host-agent bridge failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
