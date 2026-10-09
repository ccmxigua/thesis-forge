"""Property-level execution receipts for serialized DOCX verification."""
from __future__ import annotations

from functools import lru_cache
import copy
import json
from pathlib import Path
import re
from typing import Any, Callable

try:
    from .format_spec_validation import validate_instance
    from .semantic_contract import strict_json_read
except ImportError:
    from format_spec_validation import validate_instance
    from semantic_contract import strict_json_read


@lru_cache(maxsize=1)
def _format_schema() -> dict[str, Any]:
    return strict_json_read(Path(__file__).resolve().parents[1]
                            / "schema" / "format-spec.schema.json")


def _advisory_prefixes(role: str, properties: dict[str, Any]) -> set[str]:
    """Exclude only schema-valid, explicitly nonbinding advice from execution.

    Advice stays in the requirement and measured guidance-advisories report;
    it must not receive a fabricated successful DOCX execution receipt.
    Unknown paths, malformed guidance and all hard properties stay fail-closed.
    """
    if role != "content_constraints":
        return set()
    definitions = _format_schema()["$defs"]
    declared = {
        ("keywords_zh", "count_guidance"): definitions["keywordConstraint"]["properties"]["count_guidance"],
        ("keywords_en", "count_guidance"): definitions["keywordConstraint"]["properties"]["count_guidance"],
        ("abstract_zh", "length_guidance"): definitions["contentConstraintSpec"]["properties"]["abstract_zh"]["properties"]["length_guidance"],
        ("abstract_zh", "third_person_guidance"): definitions["contentConstraintSpec"]["properties"]["abstract_zh"]["properties"]["third_person_guidance"],
    }
    prefixes: set[str] = set()
    for (key, name), schema in declared.items():
        parent = properties.get(key)
        if not isinstance(parent, dict) or name not in parent:
            continue
        guidance = parent[name]
        if validate_instance(guidance, schema):
            continue
        if isinstance(guidance, dict):
            lower, upper = ("min_count", "max_count") if name == "count_guidance" else ("min_chars", "max_chars")
            if guidance[lower] > guidance[upper]:
                continue
        prefixes.add(f"{key}.{name}")
    return prefixes


def _execution_property_items(role: str, properties: dict[str, Any]) -> list[tuple[int, str, Any]]:
    expected = flatten(properties)
    if not expected:
        expected = {"__requirement__": True}
    advisory = _advisory_prefixes(role, properties)
    # Keep original indices so adding this policy does not renumber hard
    # property identities relative to the complete, sorted requirement.
    return [
        (index, path, value)
        for index, (path, value) in enumerate(sorted(expected.items()), start=1)
        if not any(path == prefix or path.startswith(prefix + ".") for prefix in advisory)
    ]


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
    if property_path == "fields":
        if not isinstance(actual, list) or not isinstance(expected, list):
            return False

        def label_key(value: Any) -> str | None:
            if not isinstance(value, str):
                return None
            return re.sub(r"[\s:：]+$", "", value)

        # A source requirement may define one field subset of a larger cover
        # contract. Match by stable field identity, source binding and label;
        # preserve source order and never accept a field by label alone.
        cursor = 0
        for required in expected:
            if not isinstance(required, dict) or not isinstance(required.get("id"), str):
                return False
            match_index = None
            for index in range(cursor, len(actual)):
                observed = actual[index]
                if not isinstance(observed, dict):
                    continue
                if (observed.get("id") != required.get("id")
                        or observed.get("value_from") != required.get("value_from")
                        or label_key(observed.get("label")) != label_key(required.get("label"))):
                    continue
                if (required.get("display_policy") is not None
                        and observed.get("display_policy") != required.get("display_policy")):
                    continue
                if (required.get("label_display_policy") is not None
                        and observed.get("label_display_policy") != required.get("label_display_policy")):
                    continue
                match_index = index
                break
            if match_index is None:
                return False
            cursor = match_index + 1
        return True
    leaf = property_path.rsplit(".", 1)[-1]
    lower_bounds = {"min_count", "min_chars", "min_words"}
    upper_bounds = {"max_count", "max_chars", "max_words", "max_item_chars"}
    if leaf in lower_bounds | upper_bounds:
        if (isinstance(actual, bool) or isinstance(expected, bool)
                or not isinstance(actual, (int, float))
                or not isinstance(expected, (int, float))):
            return False
        return actual >= expected if leaf in lower_bounds else actual <= expected
    return equal(actual, expected)


def expected_receipt_ids(
    requirements: list[dict[str, Any]],
    *,
    applicable_roles: set[str] | None = None,
) -> set[str]:
    """Derive the exact receipt key set from the executable requirement IR."""
    result: set[str] = set()
    for requirement in requirements:
        requirement_id = str(requirement.get("id") or "")
        role = str(requirement.get("role") or "")
        properties = requirement.get("properties")
        if not requirement_id or not role or not isinstance(properties, dict):
            continue
        expected = flatten(properties)
        if not expected and requirement.get("field_instance_ids"):
            continue
        if applicable_roles is not None and role not in applicable_roles:
            continue
        result.update(
            f"PR-{requirement_id}-{index:04d}"
            for index, _path, _value in _execution_property_items(role, properties)
        )
    return result


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
    actual_by_requirement: dict[str, dict[str, Any]] | None = None,
    role_observability: dict[str, dict[str, dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Create one receipt per declared requirement property.

    Style-backed roles receive concrete serialized values.  Structural roles
    without a style mapping remain explicitly ``unverified``; they cannot be
    mistaken for role-level coverage in a formal full run.
    """
    receipts: list[dict[str, Any]] = []
    role_results = role_results or {}
    verification_methods = verification_methods or {}
    actual_by_requirement = actual_by_requirement or {}
    role_observability = role_observability or {}
    cover_field_requirements = [
        item for item in requirements
        if isinstance(item, dict) and item.get("role") == "cover"
        and isinstance(item.get("properties"), dict)
        and isinstance(item["properties"].get("fields"), list)
    ]
    cover_field_signatures = {
        json.dumps(item["properties"]["fields"], ensure_ascii=False, sort_keys=True,
                   separators=(",", ":"))
        for item in cover_field_requirements
    }
    ambiguous_cover_scope = len(cover_field_signatures) > 1
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
        has_requirement_actual = requirement_id in actual_by_requirement
        requirement_actual = actual_by_requirement.get(requirement_id)
        selected_actual = (
            requirement_actual if has_requirement_actual and isinstance(requirement_actual, dict)
            else actual_by_role.get(role, {})
        )
        actual = flatten(selected_actual)
        mapping = mappings.get(role) or {}
        target_locator = (
            f"style:{mapping.get('style_name')}"
            if mapping.get("style_name") else f"role:{role}"
        )
        for index, property_path, expected_value in _execution_property_items(role, properties):
            actual_value = actual.get(property_path)
            status_reason = None
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
            # Multiple source cover field lists can describe distinct cover
            # instances. A role-wide list without an instance binding cannot
            # prove that a differing list is missing from the selected page.
            # Keep the discrepancy visible as unknown until an exact instance
            # map is supplied; a single unambiguous cover remains fail-closed.
            if (role == "cover" and property_path == "fields"
                    and ambiguous_cover_scope
                    and not requirement.get("cover_instance_id")
                    and not requirement.get("field_instance_ids")
                    and not has_requirement_actual
                    and status == "failed"):
                status = "unverified"
                status_reason = "source cover field set has no instance-specific binding"
            observation = role_observability.get(role, {}).get(property_path)
            if isinstance(observation, dict) and observation.get("observed") is False:
                status = "unverified"
                status_reason = str(observation.get("reason") or "property has no observed content")
            receipts.append({
                "receipt_id": f"PR-{requirement_id}-{index:04d}",
                "requirement_id": requirement_id,
                "clause_ids": list(requirement.get("clause_ids") or []),
                "role": role,
                "property_path": property_path,
                "target_locator": target_locator,
                "expected": expected_value,
                "actual": actual_value,
                "status": status,
                **({"status_reason": status_reason} if status_reason else {}),
                "verification_method": (
                    observation.get("verification_method")
                    if isinstance(observation, dict) and observation.get("verification_method")
                    else verification_methods.get(role, verification_method)
                ),
                "serialized_docx_sha256": serialized_docx_sha256,
                **({"evaluation_units": copy.deepcopy(requirement["evaluation_units"])}
                   if "evaluation_units" in requirement else {}),
            })
    return receipts


def evaluation_unit_receipt_errors(receipts: list[dict], requirements: list[dict]) -> list[str]:
    """Reconstruct code-owned unit metadata from the current spec, not a sidecar."""
    by_id = {r.get("id"): r for r in requirements if isinstance(r, dict)}
    errors = []
    for receipt in receipts:
        requirement = by_id.get(receipt.get("requirement_id"))
        if requirement is None or receipt.get("evaluation_units") != requirement.get("evaluation_units"):
            errors.append("evaluation_unit_receipt_mismatch:" + str(receipt.get("receipt_id")))
    return errors


def audit_property_receipts(
    receipts: list[dict[str, Any]],
    *,
    expected_receipt_ids: set[str] | None = None,
) -> dict[str, Any]:
    failures = [item for item in receipts if item.get("status") != "verified"]
    actual_ids = {
        str(item.get("receipt_id"))
        for item in receipts
        if isinstance(item, dict) and item.get("receipt_id")
    }
    actual_id_list = [
        str(item.get("receipt_id"))
        for item in receipts
        if isinstance(item, dict) and item.get("receipt_id")
    ]
    duplicate_ids = sorted({
        receipt_id for receipt_id in actual_id_list
        if actual_id_list.count(receipt_id) > 1
    })
    missing_ids = sorted(
        (expected_receipt_ids or set()) - actual_ids
    ) if expected_receipt_ids is not None else []
    unexpected_ids = sorted(
        actual_ids - expected_receipt_ids
    ) if expected_receipt_ids is not None else []
    failures.extend({
        "receipt_id": receipt_id,
        "status": "missing",
        "failure_type": "expected_receipt_missing",
    } for receipt_id in missing_ids)
    failures.extend({
        "receipt_id": receipt_id,
        "status": "unexpected",
        "failure_type": "unexpected_receipt",
    } for receipt_id in unexpected_ids)
    failures.extend({
        "receipt_id": receipt_id,
        "status": "duplicate",
        "failure_type": "duplicate_receipt",
    } for receipt_id in duplicate_ids)
    return {
        "schema_version": "1.0",
        "valid": not failures and not missing_ids,
        "receipt_count": len(receipts),
        "verified_count": sum(item.get("status") == "verified" for item in receipts),
        "failed_count": sum(item.get("status") == "failed" for item in receipts),
        "unverified_count": sum(item.get("status") == "unverified" for item in receipts),
        "missing_count": len(missing_ids),
        "unexpected_count": len(unexpected_ids),
        "duplicate_count": len(duplicate_ids),
        "expected_receipt_ids": (
            sorted(expected_receipt_ids)
            if expected_receipt_ids is not None else None
        ),
        "failures": failures,
        "receipts": receipts,
    }
