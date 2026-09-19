"""OpenClaw-specific command and response-envelope adapter.

This module is intentionally not a generic host dispatcher.  It is selected
only after :mod:`host_runtime` has verified that the invoking runtime is
OpenClaw.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
from typing import Any

from semantic_contract import strict_json_loads


def resolve_binary(binary: str | None) -> str:
    return binary or shutil.which("openclaw") or "openclaw"


def session_key(*, agent_id: str, run_id: str, chunk_index: int, attempt: int) -> str:
    return (
        f"agent:{agent_id}:thesis-host-agent:{run_id}:chunk-{chunk_index:04d}"
        f":attempt-{attempt:02d}"
    )


def build_command(
    *,
    binary: str,
    agent_id: str,
    session_key_value: str,
    prompt_path: Path,
    model: str | None,
    runner: str,
    timeout: int,
    cwd: Path,
    config: Path | None = None,
    auth_env_only: bool = False,
) -> tuple[list[str], bool]:
    if runner not in {"exec", "gateway"}:
        raise ValueError(f"unsupported OpenClaw runner: {runner}")
    use_isolated_exec = bool(model) and agent_id == "main" and runner == "exec"
    if use_isolated_exec:
        command = [binary, "agent", "exec"]
        if config is not None:
            command.extend(["--config", str(config)])
        command.extend([
            "--cwd", str(cwd),
            "--model", model,
            "--fallback", model,
            "--message-file", str(prompt_path),
            "--json", "--timeout", str(timeout),
        ])
        if auth_env_only:
            command.append("--auth-env-only")
        return command, True
    if auth_env_only and runner == "exec":
        raise ValueError(
            "--auth-env-only requires an explicit model route with agent_id=main")
    command = [
        binary, "agent", "--agent", agent_id,
        "--session-key", session_key_value,
        "--message-file", str(prompt_path),
    ]
    if model:
        command += ["--model", model]
    command += ["--json", "--timeout", str(timeout)]
    return command, False


def _strip_json_wrapper(text: str) -> dict[str, Any]:
    value = text.strip()
    if value.startswith("```") and value.endswith("```"):
        lines = value.splitlines()
        if len(lines) < 3:
            raise ValueError("OpenClaw returned an empty JSON fence")
        value = "\n".join(lines[1:-1]).strip()
    try:
        parsed = strict_json_loads(value)
    except (ValueError, json.JSONDecodeError):
        start = value.find("{")
        end = value.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("OpenClaw did not return a JSON object") from None
        try:
            parsed = strict_json_loads(value[start:end + 1])
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"OpenClaw returned invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("OpenClaw response must be one JSON object")
    return parsed


def parse_result(stdout: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Extract the semantic payload from either OpenClaw JSON envelope shape."""
    try:
        envelope = strict_json_loads(stdout)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"OpenClaw agent did not return JSON: {exc}") from exc
    if not isinstance(envelope, dict):
        raise ValueError("OpenClaw agent JSON envelope must be an object")
    if envelope.get("status") not in {None, "ok"}:
        summary = envelope.get("summary") or envelope.get("status")
        raise ValueError(f"OpenClaw agent failed: {summary}")
    result = envelope.get("result")
    payloads = result.get("payloads") if isinstance(result, dict) else None
    if not isinstance(payloads, list):
        payloads = envelope.get("payloads")
    if not isinstance(payloads, list):
        raise ValueError("OpenClaw response has no payloads array")
    texts = [
        item.get("text", "") for item in payloads
        if isinstance(item, dict)
        and isinstance(item.get("text"), str)
        and item.get("text")
    ]
    if not texts:
        raise ValueError("OpenClaw agent returned no text payload")
    return _strip_json_wrapper("\n".join(texts)), envelope
