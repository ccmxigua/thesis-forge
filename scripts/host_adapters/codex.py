"""Codex-specific command and JSONL result adapter.

    The adapter invokes the native ``codex exec`` CLI only after the launcher has
    declared ``THESIS_FORGE_HOST_RUNTIME=codex``.  It deliberately does not infer
or manufacture a provider/model route: the Codex CLI's installed binary is a
host identity, not evidence of the model or provider used for a turn.
"""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
from datetime import datetime, timezone
from typing import Any

from semantic_contract import strict_json_loads

# ``None`` means use the model selected by the current native Codex account.
# A reproducible route must be supplied explicitly by the caller.
DEFAULT_MODEL: str | None = None


def resolve_binary(binary: str | None) -> str:
    """Resolve an explicitly supplied or installed native Codex executable."""
    candidate = binary or shutil.which("codex")
    if not candidate:
        raise RuntimeError("codex executable was not found")
    path = Path(candidate).expanduser()
    if not path.is_file():
        raise ValueError(f"codex executable does not exist: {path}")
    if not os.access(path, os.X_OK):
        raise ValueError(f"codex executable is not executable: {path}")
    return str(path.resolve())


def build_command(
    *,
    binary: str,
    prompt_path: Path,
    last_message_path: Path,
    cwd: Path,
    model: str | None = None,
    output_schema_path: Path | None = None,
) -> list[str]:
    """Build an isolated, read-only native Codex invocation.

    ``--ignore-user-config`` keeps unrelated desktop MCP/plugin servers out of
    this ephemeral subprocess.  An explicit model, when supplied, and normal
    Codex authentication remain in force, while the subprocess can terminate after
    its semantic response instead of hanging during an unrelated MCP shutdown.
    ``codex exec --json`` emits the auditable JSONL event stream while
    ``--output-last-message`` gives the bridge the exact semantic response
    text without attempting to scrape human-facing logs.
    """
    prompt = prompt_path.read_text(encoding="utf-8")
    if not prompt.strip():
        raise ValueError(f"Codex prompt is empty: {prompt_path}")
    command = [
        binary,
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--sandbox", "read-only",
        "--json",
        "--color", "never",
        "--output-last-message", str(last_message_path),
        "-C", str(cwd),
        prompt,
    ]
    if output_schema_path is not None:
        schema_path = output_schema_path.expanduser().resolve()
        if not schema_path.is_file():
            raise ValueError(f"Codex output schema does not exist: {schema_path}")
        if not os.access(schema_path, os.R_OK):
            raise ValueError(f"Codex output schema is not readable: {schema_path}")
        command[command.index("-C"):command.index("-C")] = [
            "--output-schema", str(schema_path),
        ]
    if model is not None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("Codex model must be a non-empty string when supplied")
        command[4:4] = ["--model", model.strip()]
    return command


def probe_capabilities(binary: str, *, timeout: float = 10.0) -> dict[str, Any]:
    """Probe the native CLI before a run and record the exact structured mode.

    A prompt that merely asks for JSON is not equivalent to native structured
    output.  The bridge therefore has to observe the installed CLI's help
    surface and fail closed when ``--output-schema`` is unavailable.
    """
    resolved = resolve_binary(binary)
    version_result = subprocess.run(
        [resolved, "--version"], capture_output=True, text=True,
        timeout=timeout, check=False,
    )
    help_result = subprocess.run(
        [resolved, "exec", "--help"], capture_output=True, text=True,
        timeout=timeout, check=False,
    )
    version_text = (version_result.stdout or version_result.stderr or "").strip()
    help_text = (help_result.stdout or "") + ("\n" + help_result.stderr if help_result.stderr else "")
    schema_supported = "--output-schema" in help_text
    return {
        "binary": resolved,
        "version": version_text,
        "version_returncode": version_result.returncode,
        "exec_help_returncode": help_result.returncode,
        "exec_help_sha256": hashlib.sha256(help_text.encode("utf-8")).hexdigest(),
        "output_schema_supported": schema_supported,
        "structured_output_mode": "native_schema" if schema_supported else "prompt_only",
        "probed_at": datetime.now(timezone.utc).isoformat(),
    }


def _strip_json_wrapper(text: str) -> dict[str, Any]:
    value = text.strip()
    if value.startswith("```") and value.endswith("```"):
        lines = value.splitlines()
        if len(lines) < 3:
            raise ValueError("Codex returned an empty JSON fence")
        value = "\n".join(lines[1:-1]).strip()
    try:
        parsed = strict_json_loads(value)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"Codex final message is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Codex final message must be one JSON object")
    return parsed


def retryable_failure_code(stdout: str) -> str | None:
    """Classify only explicit Codex terminal capacity failures as retryable.

    Human-readable stderr and ordinary assistant text are deliberately ignored;
    an exact capacity marker must appear in a structured ``turn.failed`` event.
    """
    capacity_markers = (
        "selected model is at capacity",
        "model is currently at capacity",
        "model is at capacity",
        "model at capacity",
    )
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = strict_json_loads(line)
        except (ValueError, json.JSONDecodeError):
            continue
        if not isinstance(event, dict) or event.get("type") != "turn.failed":
            continue
        error = event.get("error")
        detail = event.get("message")
        if isinstance(error, dict):
            detail = error.get("message") or error.get("code") or detail
        if not isinstance(detail, str):
            continue
        normalized = " ".join(detail.casefold().split())
        if any(marker in normalized for marker in capacity_markers):
            return "model_capacity"
    return None


def parse_result(
    stdout: str,
    *,
    last_message: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate a Codex JSONL run and parse its final semantic response.

    A successful process exit alone is insufficient.  The stream must contain
    a terminal ``turn.completed`` event and a final assistant message.  Nested
    error items are retained as warnings when the turn still completed; this
    accommodates CLI configuration warnings that do not prevent a valid turn.
    """
    events: list[dict[str, Any]] = []
    event_types: list[str] = []
    stream_errors: list[str] = []
    agent_messages: list[str] = []
    thread_id: str | None = None
    for line_number, line in enumerate(stdout.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = strict_json_loads(line)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"Codex returned invalid JSONL at line {line_number}: {exc}"
            ) from exc
        if not isinstance(event, dict):
            raise ValueError(f"Codex JSONL event at line {line_number} is not an object")
        events.append(event)
        event_type = event.get("type")
        if isinstance(event_type, str):
            event_types.append(event_type)
        if event_type == "thread.started" and isinstance(event.get("thread_id"), str):
            thread_id = event["thread_id"]
        if event_type in {"error", "turn.failed"}:
            detail = event.get("message") or event.get("error") or event_type
            stream_errors.append(str(detail))
        item = event.get("item")
        if isinstance(item, dict):
            item_type = item.get("type")
            if item_type == "error":
                detail = item.get("message") or item.get("error") or "item error"
                stream_errors.append(str(detail))
            if item_type == "agent_message" and isinstance(item.get("text"), str):
                if item["text"].strip():
                    agent_messages.append(item["text"])

    if not events:
        raise ValueError("Codex returned no JSONL events")
    turn_completed = "turn.completed" in event_types
    if "turn.failed" in event_types:
        detail = "; ".join(stream_errors[-3:]) or "turn.failed"
        raise ValueError(f"Codex turn failed: {detail}")
    if not turn_completed:
        detail = "; ".join(stream_errors[-3:]) or "missing turn.completed"
        raise ValueError(f"Codex turn did not complete: {detail}")

    source = "output-last-message"
    final_text = (last_message or "").strip()
    if not final_text and agent_messages:
        source = "agent-message-event"
        final_text = agent_messages[-1].strip()
    if not final_text:
        raise ValueError("Codex turn completed without a final assistant message")

    response = _strip_json_wrapper(final_text)
    if last_message and agent_messages:
        event_response = _strip_json_wrapper(agent_messages[-1].strip())
        if event_response != response:
            raise ValueError(
                "Codex final message does not match the terminal agent-message event"
            )
    terminal_event_index = max(
        index for index, event in enumerate(events)
        if event.get("type") in {"turn.completed", "turn.failed"}
    )
    return response, {
        "event_count": len(events),
        "event_types": event_types,
        "turn_completed": True,
        "terminal_event_index": terminal_event_index,
        "thread_id": thread_id,
        "stream_warnings": stream_errors,
        "final_message_source": source,
        "final_message_sha256": hashlib.sha256(
            final_text.encode("utf-8")
        ).hexdigest(),
    }
