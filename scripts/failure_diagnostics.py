"""Exception locations for failed runs, without frame locals or source text."""
from __future__ import annotations

from typing import Any


def exception_diagnostics(error: BaseException) -> dict[str, Any]:
    """Capture both chains, including a cleanup error's suppressed context.

    This is diagnostic evidence only. It neither classifies a permission as
    allowed nor changes exception propagation, retry or acceptance policy.
    Do not capture locals, command arguments, environment, source lines or
    exception messages (some exception formatters embed argv in the message).
    """
    pending = [error]
    indexes = {id(error): 0}
    records: list[dict[str, Any]] = []
    truncated = False
    for current in pending:
        frames = []
        cursor = current.__traceback__
        while cursor is not None and len(frames) < 64:
            frames.append({
                "file": cursor.tb_frame.f_code.co_filename,
                "line": cursor.tb_lineno,
                "function": cursor.tb_frame.f_code.co_name,
            })
            cursor = cursor.tb_next
        record: dict[str, Any] = {
            "type": type(current).__name__,
            "frames": frames,
            "frames_truncated": cursor is not None,
            "context_suppressed": current.__suppress_context__,
        }
        if isinstance(current, OSError):
            record["os_error"] = {
                "errno": current.errno,
                "filename": (current.filename.decode("utf-8", errors="replace")
                             if isinstance(current.filename, bytes) else current.filename),
                "filename2": (current.filename2.decode("utf-8", errors="replace")
                              if isinstance(current.filename2, bytes) else current.filename2),
            }
        for key, linked in (("cause", current.__cause__), ("context", current.__context__)):
            record[key] = None
            if linked is None:
                continue
            if id(linked) not in indexes:
                if len(pending) >= 64:
                    truncated = True
                    continue
                indexes[id(linked)] = len(pending)
                pending.append(linked)
            record[key] = indexes[id(linked)]
        records.append(record)
    return {
        "protocol": "exception_locations_v1",
        "captures_locals": False,
        "chain_truncated": truncated,
        "exceptions": records,
    }
