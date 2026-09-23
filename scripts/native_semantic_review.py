"""Run evidence-bound, read-only semantic checks through the declared host agent."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any

from format_spec_validation import validate_instance
from host_adapters import codex as codex_adapter
from host_adapters import openclaw as openclaw_adapter
from host_runtime import automatic_adapter_id, require_host_runtime
from semantic_contract import sha256_json


class NativeSemanticReviewError(RuntimeError):
    """A native semantic review could not be proven valid for this run."""


RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["results"],
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["check_id", "verdict", "rationale", "evidence_quotes"],
                "properties": {
                    "check_id": {"type": "string", "minLength": 1},
                    "verdict": {"enum": ["satisfied", "noncompliant", "uncertain"]},
                    "rationale": {"type": "string", "minLength": 1},
                    "evidence_quotes": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "minItems": 1,
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_response(
    response: Any, checks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Validate exact coverage and source-grounded evidence for each check."""
    schema_errors = validate_instance(response, RESPONSE_SCHEMA)
    if schema_errors:
        raise NativeSemanticReviewError(
            "native semantic response violates its JSON schema: "
            + "; ".join(schema_errors[:8])
        )
    if not isinstance(response, dict) or not isinstance(response.get("results"), list):
        raise NativeSemanticReviewError("native semantic response must contain a results array")
    expected = {str(item.get("check_id")): item for item in checks}
    results: dict[str, dict[str, Any]] = {}
    for item in response["results"]:
        if not isinstance(item, dict):
            raise NativeSemanticReviewError("semantic review result is not an object")
        check_id = item.get("check_id")
        if not isinstance(check_id, str) or check_id not in expected:
            raise NativeSemanticReviewError(f"semantic review returned unknown check_id: {check_id!r}")
        if check_id in results:
            raise NativeSemanticReviewError(f"semantic review returned duplicate check_id: {check_id}")
        if item.get("verdict") not in {"satisfied", "noncompliant", "uncertain"}:
            raise NativeSemanticReviewError(f"semantic review has invalid verdict for {check_id}")
        rationale = item.get("rationale")
        quotes = item.get("evidence_quotes")
        if not isinstance(rationale, str) or not rationale.strip():
            raise NativeSemanticReviewError(f"semantic review has no rationale for {check_id}")
        if not isinstance(quotes, list) or not quotes or any(not isinstance(q, str) or not q for q in quotes):
            raise NativeSemanticReviewError(f"semantic review has no evidence quotes for {check_id}")
        source_text = str(expected[check_id].get("document_text") or "")
        if any(quote not in source_text for quote in quotes):
            raise NativeSemanticReviewError(
                f"semantic review evidence quote is not an exact substring of current text for {check_id}"
            )
        results[check_id] = {
            "check_id": check_id,
            "verdict": item["verdict"],
            "rationale": rationale.strip(),
            "evidence_quotes": list(quotes),
        }
    missing = sorted(set(expected) - set(results))
    if missing:
        raise NativeSemanticReviewError("native semantic response omitted checks: " + ", ".join(missing))
    return [results[key] for key in sorted(results)]


def _prompt(request: dict[str, Any]) -> str:
    return (
        "You are the native agent of the currently declared host runtime. "
        "Perform a read-only semantic compliance review of the supplied thesis passages. "
        "Do not edit, rewrite, normalize, or add thesis content. Do not infer missing facts. "
        "For every check_id, return exactly one verdict: satisfied, noncompliant, or uncertain. "
        "Use uncertain whenever the source rule or passage does not support a reliable judgment. "
        "Each evidence_quotes value must be copied exactly from that check's document_text. "
        "A satisfied verdict requires concrete textual evidence; a noncompliant verdict must "
        "identify the specific unmet condition; do not mark a check satisfied merely because "
        "the text is fluent. Return only the JSON object required by the output schema.\n\n"
        "Current run-bound request:\n"
        + json.dumps(request, ensure_ascii=False, sort_keys=True, indent=2)
    )


def _write_fresh(path: Path, text: str) -> None:
    if path.exists():
        raise NativeSemanticReviewError(f"refusing to reuse native semantic review artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def run_native_semantic_review(
    request: dict[str, Any],
    *,
    output_dir: Path,
    host_runtime: str,
    model: str,
    timeout: int = 900,
    agent_id: str = "main",
    runner: str = "exec",
    binary: str | None = None,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Run one fresh native-host review; never fall back to another host.

    The current source identity and document hash are generated by code and are
    stored in the resulting audit. The model only judges semantic checks and
    cannot provide or replace those trusted bindings.
    """
    if not isinstance(model, str) or not model.strip():
        raise NativeSemanticReviewError("native semantic review requires an explicit model route")
    if isinstance(timeout, bool) or timeout <= 0:
        raise NativeSemanticReviewError("native semantic review timeout must be positive")
    context = require_host_runtime(host_runtime)
    adapter_id = automatic_adapter_id(context)
    checks = request.get("checks")
    if not isinstance(checks, list) or not checks:
        return {
            "schema_version": "1.0", "protocol": "native_semantic_content_review_v1",
            "status": "not_required", "adapter_id": adapter_id,
            "host_runtime": context.runtime, "model": model,
            "case_id": request.get("case_id"), "run_id": request.get("run_id"),
            "request_sha256": sha256_json(request), "checks": [],
            "results": [], "summary": {"satisfied": 0, "noncompliant": 0, "uncertain": 0},
        }
    output_dir.mkdir(parents=True, exist_ok=True)
    request_path = output_dir / "request.json"
    prompt_path = output_dir / "prompt.txt"
    response_path = output_dir / "response.json"
    stdout_path = output_dir / "stdout.jsonl"
    stderr_path = output_dir / "stderr.txt"
    schema_path = output_dir / "response-schema.json"
    reserved_paths = [
        request_path, prompt_path, response_path, stdout_path, stderr_path,
        schema_path, output_dir / "last-message.txt",
    ]
    existing_paths = [str(path) for path in reserved_paths if path.exists()]
    if existing_paths:
        raise NativeSemanticReviewError(
            "refusing to reuse semantic review artifacts: " + ", ".join(existing_paths)
        )
    _write_fresh(request_path, json.dumps(request, ensure_ascii=False, indent=2) + "\n")
    _write_fresh(prompt_path, _prompt(request))
    _write_fresh(schema_path, json.dumps(RESPONSE_SCHEMA, ensure_ascii=False, indent=2) + "\n")

    if adapter_id == "codex":
        codex_binary = codex_adapter.resolve_binary(binary)
        capabilities = codex_adapter.probe_capabilities(codex_binary)
        if not capabilities.get("output_schema_supported"):
            raise NativeSemanticReviewError(
                "native Codex CLI does not support --output-schema; refusing unstructured semantic review"
            )
        command = codex_adapter.build_command(
            binary=codex_binary, prompt_path=prompt_path,
            last_message_path=output_dir / "last-message.txt",
            cwd=Path(__file__).resolve().parents[1], model=model,
            output_schema_path=schema_path,
        )
        route_audit: dict[str, Any] = {
            "binary": codex_binary,
            "capabilities": capabilities,
            "route_visibility": "native_codex_model_unobservable",
        }
    elif adapter_id == "openclaw":
        if "/" not in model.strip():
            raise NativeSemanticReviewError(
                "OpenClaw semantic review requires an explicit provider/model route"
            )
        openclaw_binary = openclaw_adapter.resolve_binary(binary)
        session_key = openclaw_adapter.session_key(
            agent_id=agent_id, run_id=str(request.get("run_id") or "semantic-review"),
            chunk_index=1, attempt=1,
        )
        command, isolated = openclaw_adapter.build_command(
            binary=openclaw_binary, agent_id=agent_id,
            session_key_value=session_key, prompt_path=prompt_path,
            model=model, runner=runner, timeout=timeout,
            cwd=Path(__file__).resolve().parents[1], config=config_path,
        )
        route_audit = {
            "binary": openclaw_binary,
            "session_key": session_key,
            "isolated_exec": isolated,
            "route_visibility": "provider_model_from_native_envelope",
        }
    else:  # pragma: no cover - automatic_adapter_id already fails closed
        raise NativeSemanticReviewError(f"unsupported host adapter: {adapter_id}")

    env = os.environ.copy()
    env["THESIS_FORGE_HOST_RUNTIME"] = str(context.runtime)
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        completed = subprocess.run(
            command, cwd=Path(__file__).resolve().parents[1], env=env,
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        _write_fresh(stdout_path, str(exc.stdout or ""))
        _write_fresh(stderr_path, str(exc.stderr or ""))
        raise NativeSemanticReviewError(
            f"native semantic review exceeded {timeout} seconds"
        ) from exc
    _write_fresh(stdout_path, completed.stdout or "")
    _write_fresh(stderr_path, completed.stderr or "")
    if completed.returncode != 0:
        raise NativeSemanticReviewError(
            f"native semantic review process failed ({completed.returncode}); see {stderr_path}"
        )
    try:
        if adapter_id == "codex":
            last_message = (output_dir / "last-message.txt").read_text(encoding="utf-8")
            response, envelope = codex_adapter.parse_result(
                completed.stdout or "", last_message=last_message,
            )
        else:
            response, envelope = openclaw_adapter.parse_result(completed.stdout or "")
            from host_agent_bridge import verify_host_agent_route
            route_audit["verified_route"] = verify_host_agent_route(envelope, model)
        results = validate_response(response, checks)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise NativeSemanticReviewError(f"native semantic response rejected: {exc}") from exc
    _write_fresh(response_path, json.dumps(response, ensure_ascii=False, indent=2) + "\n")
    finished_at = datetime.now(timezone.utc).isoformat()
    counts = {key: sum(item["verdict"] == key for item in results)
              for key in ("satisfied", "noncompliant", "uncertain")}
    return {
        "schema_version": "1.0",
        "status": "completed",
        "protocol": "native_semantic_content_review_v1",
        "adapter_id": adapter_id,
        **context.as_audit(),
        "model_requested": model,
        **route_audit,
        "case_id": request.get("case_id"),
        "run_id": request.get("run_id"),
        "source_sha256": request.get("source_sha256"),
        "format_spec_sha256": request.get("format_spec_sha256"),
        "document_text_sha256": request.get("document_text_sha256"),
        "request_sha256": sha256_json(request),
        "request_path": str(request_path.resolve()),
        "prompt_path": str(prompt_path.resolve()),
        "prompt_sha256": sha256_file(prompt_path),
        "response_path": str(response_path.resolve()),
        "response_sha256": sha256_file(response_path),
        "stdout_path": str(stdout_path.resolve()),
        "stderr_path": str(stderr_path.resolve()),
        "started_at": started_at,
        "finished_at": finished_at,
        "result_event_types": envelope.get("event_types") if adapter_id == "codex" else None,
        "checks": checks,
        "results": results,
        "summary": counts,
    }
