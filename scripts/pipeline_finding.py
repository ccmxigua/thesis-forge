#!/usr/bin/env python3
"""Small constructors for stable, machine-readable pipeline findings."""
from __future__ import annotations

from typing import Any, Iterable

SEVERITIES = {"info", "warning", "error"}
GATE_CLASSES = {"integrity", "quality", "input", "semantic"}
# This registry is code-owned. A producer cannot downgrade these failures by
# setting blocking=false or by advertising a different gate class.
INTEGRITY_CODES = frozenset({
    "capability.contract_binding_error", "assembly.contract_missing",
    "source.binding_invalid", "schema.contract_invalid", "invocation.identity_invalid",
    "artifact.integrity_invalid", "receipt.inventory_invalid", "repair.authorization_invalid",
})


def integrity_findings(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise ValueError("findings must be a record list")
    for item in items:
        if "gate_class" in item and item["gate_class"] not in GATE_CLASSES:
            raise ValueError("unknown finding gate_class")
    return [item for item in items if item.get("code") in INTEGRITY_CODES
            or item.get("gate_class") == "integrity"]


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
    result = {
        "code": code,
        "stage": stage,
        "severity": severity,
        "blocking": bool(blocking),
        "evidence": list(evidence_items),
        "message": message,
    }
    if code in INTEGRITY_CODES:
        result.update(gate_class="integrity", blocking=True)
    return result
