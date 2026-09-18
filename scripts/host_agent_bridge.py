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
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    summarize_contract_errors as _shared_summarize_contract_errors,
    validate_response as _shared_validate_response,
)
from host_runtime import (  # noqa: E402
    HostAdapterUnavailable,
    HostRuntimeError,
    automatic_adapter_id,
    require_host_runtime,
    require_parent_session,
)
from requirements_engine import merge_host_agent_review_packets  # noqa: E402
from semantic_contract import validate_response_provenance  # noqa: E402


def _read_json(path: Path, *, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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
    return _shared_summarize_contract_errors(errors, limit=limit)


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
        ),
        encoding="utf-8",
    )
    session_key: str | None = None
    use_isolated_exec = False
    last_message_path: Path | None = None
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
        command = codex_adapter.build_command(
            binary=codex_bin,
            prompt_path=prompt_path,
            last_message_path=last_message_path,
            cwd=ROOT,
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
    raw_response_path = response_path.with_name(
        f"{response_path.stem}.raw{response_path.suffix}"
    )
    if raw_response_path.exists():
        raise ValueError(f"refusing to overwrite existing raw Host Agent response: {raw_response_path}")
    raw_response_path.write_text(
        json.dumps(response, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if provenance_mismatch_fields:
        raise HostAgentProvenanceMismatch(
            "Host Agent response provenance mismatch: "
            + ", ".join(provenance_mismatch_fields)
        )
    if controller is not None:
        controller.check()
    response_path.write_text(
        json.dumps(response, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
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
        "codex_stream_warnings": envelope.get("stream_warnings") if adapter_id == "codex" else [],
        "status": envelope.get("status", "ok"),
        "returncode": result.returncode,
        "elapsed_s": elapsed,
        "runner": "codex-exec" if adapter_id == "codex" else (
            "agent-exec" if use_isolated_exec else (
            "gateway-agent" if runner == "gateway" else "agent"
            )
        ),
        "expected_route": model if adapter_id == "openclaw" else "unobservable",
        "actual_provider": route.get("provider"),
        "actual_model": route.get("model"),
        "actual_route": route.get("route"),
        "fallback_used": route.get("fallback_used"),
        "local_process_state": "completed",
        "remote_operation_state": "remote_operation_completed",
        "response_path": str(response_path.resolve()),
        "raw_envelope_path": str(raw_envelope_path.resolve()),
        "raw_response_path": str(raw_response_path.resolve()),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    audit["provenance_observed"] = isinstance(observed_provenance, dict)
    audit["provenance_mismatch_fields"] = provenance_mismatch_fields
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
) -> dict[str, Any]:
    host_context = require_host_runtime(host_runtime)
    adapter_id = automatic_adapter_id(host_context)
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
    if manifest.get("contract_version") != "2.1":
        raise ValueError("host-agent review manifest is not contract 2.1")
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
    response_files = manifest.get("response_files")
    if not isinstance(chunks, list) or not chunks:
        raise ValueError("host-agent request chunks are missing or empty")
    if not isinstance(response_files, list) or len(response_files) != len(chunks):
        raise ValueError("host-agent response file manifest does not match request chunks")
    if any(not isinstance(item, str) or not item.strip() for item in response_files):
        raise ValueError("host-agent response file manifest contains a non-string path")
    if len(set(response_files)) != len(response_files):
        raise ValueError("host-agent response file manifest contains duplicate paths")
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
    resolution: dict[str, Any] = {
        "model": model if adapter_id == "openclaw" else None,
        "source": (
            "explicit-model" if model else "gateway-default"
        ) if adapter_id == "openclaw" else "native-codex-default",
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
            "execution_mode": "native-adapter",
            "adapter_id": adapter_id,
            **host_context.as_audit(),
            "max_concurrency": max_concurrency,
            "max_attempts": max_attempts,
            "model": effective_model,
            **_route_audit_fields(effective_model, chunk_runs),
            "route_visibility": "provider-model" if adapter_id == "openclaw" else "unobservable",
            "auth_env_only": bool(auth_env_only),
            "runner": runner,
            "model_source": resolution.get("source"),
            "openclaw_config": str(openclaw_config) if openclaw_config else None,
            "codex_bin": codex_binary,
            "route_policy": (
                "parent-effective-route-snapshot"
                if adapter_id == "openclaw" else "native-codex-default"
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
            "remote_operation_state": (
                "remote_operation_completed"
                if any(item.get("remote_operation_state") == "remote_operation_completed"
                       for item in chunk_runs)
                else "remote_operation_state_unknown"
            ),
            "chunk_runs": chunk_runs,
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
                    codex_bin=codex_binary,
                    openclaw_config=openclaw_config,
                    prompt_path=prompt_dir / f"prompt-{index:04d}-attempt-{attempt:02d}.txt",
                    model=effective_model,
                    attempt=attempt,
                    auth_env_only=auth_env_only,
                    runner=runner,
                    controller=controller,
                    adapter_id=adapter_id,
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
            except (HostAgentRouteMismatch, HostAgentProvenanceMismatch, HostAgentCancelled):
                # A route mismatch is not a model-quality error.  Retrying
                # would spend more tokens on an unauthorized route, so abort
                # the whole run immediately and preserve fail-closed behavior.
                raise
            except (OSError, ValueError, RuntimeError) as exc:
                controller.check()
                failures.append(str(exc))
                if attempt >= max_attempts:
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
            controller.terminate_all()
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
        "execution_mode": "native-adapter",
        "adapter_id": adapter_id,
        **host_context.as_audit(),
        "max_concurrency": max_concurrency,
        "max_attempts": max_attempts,
        "model": effective_model,
        **_route_audit_fields(effective_model, chunk_audits),
        "route_visibility": "provider-model" if adapter_id == "openclaw" else "unobservable",
        "local_process_state": "completed",
        "remote_operation_state": "remote_operation_completed",
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
            if adapter_id == "openclaw" else "native-codex-default"
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
            inherit_parent_model=args.inherit_parent_model,
            parent_session_key=args.parent_session_key,
            auth_env_only=args.auth_env_only,
            runner=args.runner,
            host_runtime=args.host_runtime,
        )
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"host-agent bridge failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
