"""One lossless declaration body representation shared by consumers."""
from __future__ import annotations

from typing import Any


def declaration_body_parts(item: dict[str, Any]) -> list[str]:
    """Preserve source bytes and reject competing nonempty body forms.

    Source-bound alias removal belongs to the host projection, not here:
    consumers have no authority to guess whether one field is redundant.
    Legacy scalar-only and paragraph-array-only inputs remain supported.
    """
    body = item.get("body")
    raw_parts = item.get("body_parts")
    parts = [part for part in raw_parts if isinstance(part, str) and part.strip()] if isinstance(raw_parts, list) else []
    scalar = isinstance(body, str) and bool(body.strip())
    if scalar and parts:
        raise ValueError("ambiguous declaration body: use body or body_parts, not both")
    return parts if parts else [body] if scalar else []
