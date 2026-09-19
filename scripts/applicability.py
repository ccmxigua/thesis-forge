"""Deterministic evaluation of the contract's explicit applicability facts."""
from __future__ import annotations

from typing import Any

from input_resolver import value_present


def _resolve(container: Any, dotted: str) -> tuple[bool, Any]:
    current = container
    for part in dotted.split(".") if dotted else []:
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def evaluate_applicability(
    declaration: Any,
    *,
    thesis_profile: dict[str, Any] | None = None,
    source_inventory: dict[str, Any] | None = None,
    template_profile: dict[str, Any] | None = None,
    runtime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return ``true``, ``false`` or ``unknown`` without semantic guessing.

    A conditional declaration is executable only when every declared fact is
    present and its operator can be evaluated.  Missing facts never become
    false by default; they remain an explicit unknown for the capability gate.
    """
    if not isinstance(declaration, dict):
        return {"status": "always", "result": "true", "evaluated": []}
    status = declaration.get("status", "always")
    if status == "excluded":
        return {"status": "excluded", "result": "false", "evaluated": []}
    if status == "always":
        return {"status": "always", "result": "true", "evaluated": []}
    if status != "conditional":
        return {"status": status, "result": "unknown", "evaluated": [],
                "reason": "unknown_applicability_status"}
    conditions = declaration.get("conditions")
    if not isinstance(conditions, list) or not conditions:
        return {"status": "conditional", "result": "unknown", "evaluated": [],
                "reason": "conditional_without_conditions"}
    roots = {
        "thesis_profile": thesis_profile,
        "source_inventory": source_inventory,
        "template_profile": template_profile,
        "runtime": runtime,
    }
    evaluated: list[dict[str, Any]] = []
    for index, condition in enumerate(conditions):
        if not isinstance(condition, dict):
            return {"status": "conditional", "result": "unknown", "evaluated": evaluated,
                    "reason": f"condition_{index}_not_object"}
        fact = condition.get("fact")
        operator = condition.get("operator")
        if not isinstance(fact, str) or "." not in fact or fact.split(".", 1)[0] not in roots:
            return {"status": "conditional", "result": "unknown", "evaluated": evaluated,
                    "reason": f"condition_{index}_fact_unavailable"}
        root_name, dotted = fact.split(".", 1)
        exists, actual = _resolve(roots[root_name], dotted)
        if operator in {"present", "absent"}:
            if not exists:
                return {"status": "conditional", "result": "unknown", "evaluated": evaluated,
                        "reason": f"condition_{index}_fact_missing"}
            # Presence is about an explicitly supplied typed value, not
            # Python truthiness: False and 0 are valid observed values. An
            # empty string/container remains absent by the shared input
            # contract.
            result = value_present(actual)
            if operator == "absent":
                result = not result
        elif not exists:
            return {"status": "conditional", "result": "unknown", "evaluated": evaluated,
                    "reason": f"condition_{index}_fact_missing"}
        elif operator == "equals":
            result = actual == condition.get("value")
        elif operator == "not_equals":
            result = actual != condition.get("value")
        elif operator == "in":
            values = condition.get("value")
            result = isinstance(values, list) and actual in values
        else:
            return {"status": "conditional", "result": "unknown", "evaluated": evaluated,
                    "reason": f"condition_{index}_operator_unknown"}
        evaluated.append({"fact": fact, "operator": operator, "actual": actual, "result": bool(result)})
        if not result:
            return {"status": "conditional", "result": "false", "evaluated": evaluated}
    return {"status": "conditional", "result": "true", "evaluated": evaluated}
