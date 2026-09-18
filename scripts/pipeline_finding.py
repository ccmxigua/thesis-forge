#!/usr/bin/env python3
"""Small constructors for stable, machine-readable pipeline findings."""
from __future__ import annotations

from typing import Any, Iterable

SEVERITIES = {"info", "warning", "error"}


def evidence(kind: str, value: Any, source: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"kind": kind, "value": value}
    if source:
        item["source"] = source
    return item


def finding(
    code: str,
    stage: str,
    severity: str,
    blocking: bool,
    message: str,
    evidence_items: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    if severity not in SEVERITIES:
        raise ValueError(f"invalid finding severity: {severity}")
    return {
        "code": code,
        "stage": stage,
        "severity": severity,
        "blocking": bool(blocking),
        "evidence": list(evidence_items),
        "message": message,
    }
