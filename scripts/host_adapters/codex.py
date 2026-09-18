"""Codex-specific command and JSONL result adapter.

The adapter invokes the native ``codex exec`` CLI only after the launcher has
declared ``THESIS_FORGE_HOST_RUNTIME=codex``.  It deliberately does not infer
or manufacture a provider/model route: the Codex CLI's installed binary is a
host identity, not evidence of the model or provider used for a turn.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
from typing import Any


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
) -> list[str]:
    """Build an isolated, read-only native Codex invocation.

    ``codex exec --json`` emits the auditable JSONL event stream while
    ``--output-last-message`` gives the bridge the exact semantic response
    text without attempting to scrape human-facing logs.
    """
    prompt = prompt_path.read_text(encoding="utf-8")
    if not prompt.strip():
        raise ValueError(f"Codex prompt is empty: {prompt_path}")
    return [
        binary,
        "exec",
        "--ephemeral",
        "--sandbox", "read-only",
        "--json",
        "--color", "never",
        "--output-last-message", str(last_message_path),
        "-C", str(cwd),
        prompt,
    ]


def _strip_json_wrapper(text: str) -> dict[str, Any]:
    value = text.strip()
    if value.startswith("```") and value.endswith("```"):
        lines = value.splitlines()
        if len(lines) < 3:
            raise ValueError("Codex returned an empty JSON fence")
        value = "\n".join(lines[1:-1]).strip()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Codex final message is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Codex final message must be one JSON object")
    return parsed


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
    for line_number, line in enumerate(stdout.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Codex returned invalid JSONL at line {line_number}: {exc}"
            ) from exc
        if not isinstance(event, dict):
            raise ValueError(f"Codex JSONL event at line {line_number} is not an object")
        events.append(event)
        event_type = event.get("type")
        if isinstance(event_type, str):
            event_types.append(event_type)
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
    return response, {
        "event_count": len(events),
        "event_types": event_types,
        "turn_completed": True,
        "stream_warnings": stream_errors,
        "final_message_source": source,
    }
