"""Property-level execution receipts for serialized DOCX verification."""
from __future__ import annotations

from typing import Any, Callable


def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            result.update(flatten(child, path))
    else:
        result[prefix] = value
    return result


def equal(left: Any, right: Any) -> bool:
    if isinstance(left, (int, float)) and not isinstance(left, bool) and \
            isinstance(right, (int, float)) and not isinstance(right, bool):
        return abs(float(left) - float(right)) <= 0.05
    return left == right


def satisfies(property_path: str, actual: Any, expected: Any) -> bool:
    """Evaluate exact properties and monotone constraint properties."""
    leaf = property_path.rsplit(".", 1)[-1]
    if leaf == "min_count" and isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return actual >= expected
    if leaf == "max_count" and isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return actual <= expected
    return equal(actual, expected)


def build_property_receipts(
    requirements: list[dict[str, Any]],
    mappings: dict[str, dict[str, Any]],
    actual_by_role: dict[str, dict[str, Any]],
    *,
    serialized_docx_sha256: str,
    role_results: dict[str, bool] | None = None,
    applicable_roles: set[str] | None = None,
    verification_methods: dict[str, str] | None = None,
    verification_method: str = "serialized_docx_style",
) -> list[dict[str, Any]]:
    """Create one receipt per declared requirement property.

    Style-backed roles receive concrete serialized values.  Structural roles
    without a style mapping remain explicitly ``unverified``; they cannot be
    mistaken for role-level coverage in a formal full run.
    """
    receipts: list[dict[str, Any]] = []
    role_results = role_results or {}
    verification_methods = verification_methods or {}
    for requirement in requirements:
        requirement_id = str(requirement.get("id") or "")
        role = str(requirement.get("role") or "")
        properties = requirement.get("properties")
        if not requirement_id or not role or not isinstance(properties, dict):
            continue
        expected = flatten(properties)
        # Literal content is verified through content-instance records, not as
        # a singleton role property.  An empty style property object therefore
        # must not become a synthetic role-level receipt that can never be
        # verified against a shared style.
        if not expected and requirement.get("field_instance_ids"):
            continue
        # A conditional role with no matching content is not an execution
        # failure. Do not manufacture a role-level receipt for an absent
        # instance, such as a four-level heading rule in a thesis with no
        # four-level headings. Direct unit-test callers keep the historical
        # behavior when applicability is omitted.
        if applicable_roles is not None and role not in applicable_roles:
            continue
        actual = flatten(actual_by_role.get(role, {}))
        mapping = mappings.get(role) or {}
        target_locator = (
            f"style:{mapping.get('style_name')}"
            if mapping.get("style_name") else f"role:{role}"
        )
        if not expected:
            expected = {"__requirement__": True}
        for index, (property_path, expected_value) in enumerate(sorted(expected.items())):
            actual_value = actual.get(property_path)
            if property_path == "__requirement__":
                actual_value = bool(role_results.get(role))
            if (role not in actual_by_role
                    or (property_path != "__requirement__"
                        and property_path not in actual)):
                status = "unverified"
            elif satisfies(property_path, actual_value, expected_value):
                status = "verified"
            else:
                status = "failed"
            receipts.append({
                "receipt_id": f"PR-{requirement_id}-{index + 1:04d}",
                "requirement_id": requirement_id,
                "clause_ids": list(requirement.get("clause_ids") or []),
                "role": role,
                "property_path": property_path,
                "target_locator": target_locator,
                "expected": expected_value,
                "actual": actual_value,
                "status": status,
                "verification_method": verification_methods.get(role, verification_method),
                "serialized_docx_sha256": serialized_docx_sha256,
            })
    return receipts


def audit_property_receipts(receipts: list[dict[str, Any]]) -> dict[str, Any]:
    failures = [item for item in receipts if item.get("status") != "verified"]
    return {
        "schema_version": "1.0",
        "valid": not failures,
        "receipt_count": len(receipts),
        "verified_count": sum(item.get("status") == "verified" for item in receipts),
        "failed_count": sum(item.get("status") == "failed" for item in receipts),
        "unverified_count": sum(item.get("status") == "unverified" for item in receipts),
        "failures": failures,
        "receipts": receipts,
    }
