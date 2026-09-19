"""Deterministic resolution of run inputs used by capability preflight.

The semantic model may declare which input it needs, but it must not decide
whether a value is present by truthiness or by looking at an unrelated input
namespace.  This module is intentionally small and provider-independent so
the same rules can be used by the planner and its tests.
"""
from __future__ import annotations

from typing import Any


EXPECTED_SCALAR_METADATA_KEYS = frozenset({
    "degree_level", "degree_category", "writing_language", "security_level",
    "metadata_status", "student_id", "completion_date", "title_zh", "title_en",
    "author_name", "supervisor_name", "college_name", "degree_discipline",
    "professional_degree_type", "program_name", "field_name", "research_direction",
    "classification_number", "unit_code", "security_marking", "embargo_until",
    "approval_number", "approval_date", "embargo_start", "author", "author_name", "advisor",
    "school", "college", "major", "discipline", "degree_type",
    "degree_display", "confidentiality_level", "confidentiality",
})
EXPECTED_BOOLEAN_METADATA_KEYS = frozenset({
    "has_appendices", "has_figure_list", "has_table_list", "has_symbol_list",
})
EXPECTED_INTEGER_METADATA_KEYS = frozenset({"co_supervisor_count"})


def value_present(value: Any) -> bool:
    """Return whether a typed input value was supplied.

    ``False`` and numeric zero are valid supplied values.  Empty strings and
    empty containers are absent because they cannot bind a document field.
    """
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def input_value_type_valid(key: str, value: Any) -> bool:
    """Check only types with a stable canonical contract.

    Unknown source-inventory shapes remain provider-specific and are not
    guessed here.  Canonical thesis-profile fields, however, have a schema
    and must not be satisfied by a list/dict accidentally serialized under a
    scalar field name.
    """
    if not isinstance(key, str) or not key.startswith("thesis_profile."):
        return True
    path = key.removeprefix("thesis_profile.")
    leaf = path.rsplit(".", 1)[-1]
    if leaf in EXPECTED_BOOLEAN_METADATA_KEYS:
        return isinstance(value, bool)
    if leaf in EXPECTED_INTEGER_METADATA_KEYS:
        return isinstance(value, int) and not isinstance(value, bool)
    if leaf in EXPECTED_SCALAR_METADATA_KEYS:
        return isinstance(value, str)
    if path in {"cover_metadata.co_supervisors", "co_supervisors"}:
        return isinstance(value, list)
    return True


def resolve_dotted(container: dict[str, Any] | None, dotted: str) -> Any:
    """Resolve a dotted path without coercing or guessing its value."""
    if not isinstance(container, dict) or not isinstance(dotted, str) or not dotted:
        return None
    current: Any = container
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def resolve_metadata(metadata: dict[str, Any] | None, dotted: str) -> Any:
    """Resolve canonical thesis-profile paths and legacy nested metadata.

    The canonical profile stores cover values below ``cover_metadata`` while
    older callers sometimes pass a flat metadata object.  Both are accepted,
    but no value is inferred from a sibling field.
    """
    if not isinstance(metadata, dict):
        return None
    candidates = (metadata, metadata.get("cover_metadata"), metadata.get("metadata"))
    for container in candidates:
        value = resolve_dotted(container if isinstance(container, dict) else None, dotted)
        if value_present(value):
            return value
    # This is a registered semantic binding, not a fuzzy lookup.
    if dotted == "co_supervisors.enterprise":
        values = metadata.get("co_supervisors")
        if isinstance(values, list):
            matches = [
                item for item in values
                if isinstance(item, dict)
                and item.get("kind") == "enterprise"
                and value_present(item.get("name"))
            ]
            return matches or None
    return None


def resolve_input(
    key: str,
    *,
    source_inventory: dict[str, Any] | None = None,
    template_profile: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    runtime_inventory: dict[str, Any] | None = None,
) -> tuple[bool, str | None, Any]:
    """Resolve a registered prerequisite key.

    Returns ``(present, namespace, value)``.  Unknown namespaces are absent;
    callers must fail closed rather than searching all input objects.
    """
    if not isinstance(key, str) or not key.strip():
        return False, None, None
    key = key.strip()
    if key.startswith("source_inventory."):
        value = resolve_dotted(source_inventory, key.removeprefix("source_inventory."))
        return value_present(value), "source_inventory", value
    if key.startswith("template_profile."):
        value = resolve_dotted(template_profile, key.removeprefix("template_profile."))
        return value_present(value), "template_profile", value
    if key.startswith("thesis_profile."):
        value = resolve_metadata(metadata, key.removeprefix("thesis_profile."))
        if not value_present(value) and isinstance(source_inventory, dict):
            # Preserve the historical planner input shape while keeping the
            # namespace explicit.  This is a compatibility fallback, not a
            # fuzzy search across arbitrary source fields.
            legacy_profile = source_inventory.get("thesis_profile")
            value = resolve_dotted(
                legacy_profile if isinstance(legacy_profile, dict) else None,
                key.removeprefix("thesis_profile."),
            )
        return value_present(value), "thesis_profile", value
    if key.startswith("runtime."):
        value = resolve_dotted(runtime_inventory, key.removeprefix("runtime."))
        if not value_present(value) and isinstance(source_inventory, dict):
            legacy_runtime = source_inventory.get("runtime")
            value = resolve_dotted(
                legacy_runtime if isinstance(legacy_runtime, dict) else None,
                key.removeprefix("runtime."),
            )
        return value_present(value), "runtime", value
    return False, None, None


def input_conflicts(
    key: str,
    *,
    source_inventory: dict[str, Any] | None = None,
    template_profile: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    runtime_inventory: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Detect contradictory values across explicitly supplied namespaces."""
    if not isinstance(key, str) or not key.startswith("thesis_profile."):
        return []
    path = key.removeprefix("thesis_profile.")
    sources: list[tuple[str, Any]] = []
    metadata_value = resolve_metadata(metadata, path)
    inventory_value = resolve_dotted(source_inventory, key)
    legacy_inventory_value = resolve_dotted(source_inventory, path)
    if value_present(metadata_value):
        sources.append(("metadata", metadata_value))
    if value_present(inventory_value):
        sources.append(("source_inventory.thesis_profile", inventory_value))
    elif value_present(legacy_inventory_value):
        sources.append(("source_inventory", legacy_inventory_value))
    if len(sources) < 2:
        return []
    first_name, first_value = sources[0]
    conflicts = []
    for name, value in sources[1:]:
        if value != first_value:
            conflicts.append({
                "key": key,
                "left": {"source": first_name, "value": first_value},
                "right": {"source": name, "value": value},
            })
    return conflicts
