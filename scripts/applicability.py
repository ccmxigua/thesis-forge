"""Deterministic evaluation of the contract's explicit applicability facts."""
from __future__ import annotations

from typing import Any

from input_resolver import value_present


_OPERATORS = {"present", "absent", "equals", "not_equals", "in"}


def _typed_equal(left: Any, right: Any) -> bool:
    """Compare JSON values without Python's bool-as-int equivalence."""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _typed_equal(a, b) for a, b in zip(left, right)
        )
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _typed_equal(left[key], right[key]) for key in left
        )
    return left == right


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
    # Validate the whole declaration before evaluating any facts.  A malformed
    # later condition must not be hidden by an earlier false condition.
    prepared: list[tuple[int, dict[str, Any], str, str, str]] = []
    for index, condition in enumerate(conditions):
        if not isinstance(condition, dict):
            return {"status": "conditional", "result": "unknown", "evaluated": [],
                    "reason": f"condition_{index}_not_object"}
        fact = condition.get("fact")
        operator = condition.get("operator")
        if not isinstance(fact, str) or "." not in fact or fact.split(".", 1)[0] not in roots:
            return {"status": "conditional", "result": "unknown", "evaluated": [],
                    "reason": f"condition_{index}_fact_unavailable"}
        if operator not in _OPERATORS:
            return {"status": "conditional", "result": "unknown", "evaluated": [],
                    "reason": f"condition_{index}_operator_unknown"}
        if operator in {"equals", "not_equals", "in"} and "value" not in condition:
            return {"status": "conditional", "result": "unknown", "evaluated": [],
                    "reason": f"condition_{index}_value_missing"}
        if set(condition) - {"fact", "operator", "value"}:
            return {"status": "conditional", "result": "unknown", "evaluated": [],
                    "reason": f"condition_{index}_has_unknown_fields"}
        root_name, dotted = fact.split(".", 1)
        prepared.append((index, condition, root_name, dotted, operator))

    evaluated: list[dict[str, Any]] = []
    unknown_reasons: list[str] = []
    has_false = False
    for index, condition, root_name, dotted, operator in prepared:
        exists, actual = _resolve(roots[root_name], dotted)
        if operator in {"present", "absent"}:
            if not exists:
                evaluated.append({"fact": condition["fact"], "operator": operator,
                                  "actual": None, "result": "unknown"})
                unknown_reasons.append(f"condition_{index}_fact_missing")
                continue
            # Presence is about an explicitly supplied typed value, not
            # Python truthiness: False and 0 are valid observed values. An
            # empty string/container remains absent by the shared input
            # contract.
            result = value_present(actual)
            if operator == "absent":
                result = not result
        elif not exists:
            evaluated.append({"fact": condition["fact"], "operator": operator,
                              "actual": None, "result": "unknown"})
            unknown_reasons.append(f"condition_{index}_fact_missing")
            continue
        elif operator == "equals":
            result = _typed_equal(actual, condition["value"])
        elif operator == "not_equals":
            result = not _typed_equal(actual, condition["value"])
        elif operator == "in":
            values = condition["value"]
            if not isinstance(values, list):
                return {"status": "conditional", "result": "unknown", "evaluated": evaluated,
                        "reason": f"condition_{index}_in_value_not_array"}
            result = any(_typed_equal(actual, value) for value in values)
        evaluated.append({"fact": condition["fact"], "operator": operator,
                          "actual": actual, "result": bool(result)})
        has_false = has_false or not result
    if has_false:
        return {"status": "conditional", "result": "false", "evaluated": evaluated}
    if unknown_reasons:
        return {"status": "conditional", "result": "unknown", "evaluated": evaluated,
                "reason": unknown_reasons[0]}
    return {"status": "conditional", "result": "true", "evaluated": evaluated}
