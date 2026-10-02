"""Deterministic guards for semantic format contracts.

These checks reject ambiguous bindings early.  They do not decide natural
language meaning; they only prevent a model from mapping one named field to a
different trusted field or declaring an unregistered input path.
"""
from __future__ import annotations

import re
import copy
from typing import Any


REGISTERED_PROFILE_FIELDS = frozenset({
    "degree_level", "degree_category", "writing_language", "security_level",
    "metadata_status", "has_appendices", "has_figure_list", "has_table_list",
    "has_symbol_list", "co_supervisor_count", "student_id", "completion_date",
})
REGISTERED_PROFILE_OBJECTS = frozenset({
    "cover_metadata",
})
# An exact whole-input selector, not an alias for cover_metadata and not
# permission to treat every nested field as supplied/confirmed.
REGISTERED_AGGREGATE_INPUTS = {"thesis_profile": "metadata"}
INPUT_KIND_PREFIXES = {
    "metadata": "thesis_profile.",
    "source_content": "source_inventory.",
    "template_resource": "template_profile.",
    "runtime": "runtime.",
}
INPUT_CATALOG_VERSION = "registered-input-catalog-v1"
REGISTERED_COVER_FIELDS = frozenset({
    "trust", "classification_number", "unit_code", "title_zh", "title_en",
    "subtitle_zh", "subtitle_en", "author_name", "student_id", "college_name",
    "degree_discipline", "professional_degree_type", "program_name", "field_name",
    "first_discipline", "second_discipline",
    "research_direction", "supervisor_name", "co_supervisors", "completion_date",
    "security_marking", "embargo_start", "embargo_until", "approval_number", "approval_date",
    "administrative_verification",
})
REGISTERED_SOURCE_ROOTS = frozenset({
    "inventory", "figures", "tables", "display_equations", "publications",
    "abstract", "body", "content", "document", "funding", "acknowledgments",
    "bibliography", "bibliography_entries",
    "source", "latex", "runtime", "english_text",
})
REGISTERED_TEMPLATE_ROOTS = frozenset({
    "fixed_values", "regions", "structure", "resources", "render_rules",
})
REGISTERED_RUNTIME_ROOTS = frozenset({
    "declaration_anchor", "declaration_anchor_status", "anchor_inventory",
    "source_docx", "word", "render",
})
# These are the only nested runtime paths exposed by the execution pipeline.
# Keep this allow-list explicit: runtime.* must not become an arbitrary model-
# controlled lookup namespace.
REGISTERED_RUNTIME_PATHS = frozenset({
    "anchor_inventory.selected",
})
REGISTERED_CHECKER_IDS = frozenset({
    "docx.required_roles", "docx.content_length", "docx.keyword_item_length",
    "docx.keyword_separator", "docx.cover_binding", "docx.declarations_anchor",
    "docx.word_render", "docx.pdf_render", "docx.property_receipts",
    "cover_non_public_administration", "declarations_fixed_text",
    "manual.abstract_semantics", "manual.table_semantics", "manual.formula_semantics",
    "external.approval_record",
})
# A model may use a descriptive spelling for a checker that is already
# registered under the canonical executable name.  Only these exact aliases
# are normalized; arbitrary or unknown checker IDs remain a hard error.
CHECKER_ID_ALIASES = {
    "declaration_anchor_binding": "docx.declarations_anchor",
    "fixed_declaration_text": "declarations_fixed_text",
    "declaration_fixed_text": "declarations_fixed_text",
}


def normalize_verification_checker_ids(spec: dict[str, Any]) -> list[dict[str, str]]:
    """Canonicalize the small, explicit checker-alias set in one run.

    This is a mechanical vocabulary normalization, not semantic inference:
    the aliases are exact names for existing registered declaration checks.
    Return an audit trail so the raw host response remains distinguishable
    from the canonical execution contract.
    """
    changes: list[dict[str, str]] = []
    requirements = spec.get("requirements", []) if isinstance(spec, dict) else []
    if not isinstance(requirements, list):
        return changes
    for requirement_index, requirement in enumerate(requirements):
        if not isinstance(requirement, dict):
            continue
        verification = requirement.get("verification")
        if not isinstance(verification, dict):
            continue
        checker_ids = verification.get("checker_ids")
        if not isinstance(checker_ids, list):
            continue
        for checker_index, checker_id in enumerate(checker_ids):
            canonical = CHECKER_ID_ALIASES.get(checker_id)
            if canonical is None:
                continue
            checker_ids[checker_index] = canonical
            changes.append({
                "json_pointer": (
                    f"$.requirements[{requirement_index}].verification.checker_ids"
                    f"[{checker_index}]"
                ),
                "from": checker_id,
                "to": canonical,
            })
    return changes


def registered_input_key(key: Any) -> bool:
    """Return whether an input prerequisite key belongs to a known namespace."""
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", key):
        return False
    if key in REGISTERED_AGGREGATE_INPUTS:
        return True
    prefix, _, path = key.partition(".")
    if not path:
        return False
    parts = path.split(".")
    if prefix == "thesis_profile":
        if len(parts) == 1 and parts[0] in REGISTERED_PROFILE_OBJECTS:
            return True
        if parts[0] == "cover_metadata":
            return len(parts) == 2 and parts[1] in REGISTERED_COVER_FIELDS
        return len(parts) == 1 and parts[0] in REGISTERED_PROFILE_FIELDS
    if prefix == "source_inventory":
        return parts[0] in REGISTERED_SOURCE_ROOTS
    if prefix == "template_profile":
        return parts[0] in REGISTERED_TEMPLATE_ROOTS
    if prefix == "runtime":
        return (
            (len(parts) == 1 and parts[0] in REGISTERED_RUNTIME_ROOTS)
            or ".".join(parts) in REGISTERED_RUNTIME_PATHS
        )
    return False


def registered_input_catalog(
    runtime_context: dict[str, Any] | None = None,
    requirements: list[dict[str, Any]] | None = None,
) -> dict[str, list[str]]:
    """Generate selectable scopes, never proof of presence or equivalence.

    Registered roots remain selectable even when absent. Nested source and
    template selectors come from current input dictionaries or existing
    prerequisite declarations, not guessed aliases. Offline contracts retain
    their registered nested-path support; generation only lists current paths.
    """
    keys = set(REGISTERED_AGGREGATE_INPUTS)
    keys.update(f"thesis_profile.{name}" for name in REGISTERED_PROFILE_FIELDS | REGISTERED_PROFILE_OBJECTS)
    keys.update(f"thesis_profile.cover_metadata.{name}" for name in REGISTERED_COVER_FIELDS)
    for prefix, roots in (("source_inventory", REGISTERED_SOURCE_ROOTS),
                          ("template_profile", REGISTERED_TEMPLATE_ROOTS),
                          ("runtime", REGISTERED_RUNTIME_ROOTS | REGISTERED_RUNTIME_PATHS)):
        keys.update(f"{prefix}.{name}" for name in roots)

    def visit(value: Any, prefix: str) -> None:
        if not isinstance(value, dict):
            return  # Array positions are not stable dotted dictionary paths.
        for name, child in value.items():
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                continue
            path = f"{prefix}.{name}"
            if registered_input_key(path):
                keys.add(path)
                visit(child, path)

    context = runtime_context if isinstance(runtime_context, dict) else {}
    for field, prefix in (("source_inventory", "source_inventory"),
                          ("template_profile", "template_profile"),
                          ("runtime_inventory", "runtime"),
                          ("confirmed_thesis_profile", "thesis_profile")):
        visit(context.get(field), prefix)
    for requirement in requirements or []:
        if not isinstance(requirement, dict):
            continue
        prerequisites = requirement.get("input_prerequisites")
        for prerequisite in prerequisites if isinstance(prerequisites, list) else []:
            key = prerequisite.get("key") if isinstance(prerequisite, dict) else None
            if registered_input_key(key):
                keys.add(key)
    return {kind: sorted(key for key in keys if key.startswith(prefix)
                        or REGISTERED_AGGREGATE_INPUTS.get(key) == kind)
            for kind, prefix in INPUT_KIND_PREFIXES.items()}


def input_prerequisite_generation_schema(
    base: dict[str, Any], catalog: dict[str, list[str]],
) -> dict[str, Any]:
    """Couple each prerequisite kind to its exact registered key domain.

    Enum and anyOf survive strict native projection; a namespace regex does
    not. Never normalize an invalid provider key into a different input scope.
    """
    schema = copy.deepcopy(base)
    schema["properties"]["key"]["enum"] = sorted({key for keys in catalog.values() for key in keys})
    branches = []
    for kind, keys in catalog.items():
        branch = copy.deepcopy(schema)
        branch["properties"]["kind"] = {"enum": [kind]}
        branch["properties"]["key"]["enum"] = list(keys)
        branches.append(branch)
    schema["anyOf"] = branches
    return schema


def normalize_label(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip("：:;；,，。")


def cover_binding_errors(spec: dict[str, Any]) -> list[str]:
    """Reject semantically impossible cover field mappings.

    A model can choose the wrong canonical field while still satisfying the
    JSON schema.  Labels are not enough to populate a different trusted field;
    these exact bindings keep public/non-public administrative data separate.
    """
    cover = spec.get("cover") if isinstance(spec, dict) else None
    if not isinstance(cover, dict):
        return []
    errors: list[str] = []
    expected_by_label = {
        "论文题目": "title_zh",
        "中文论文题目": "title_zh",
        "英文题目": "title_en",
        "一级学科": "first_discipline",
        "二级学科": "second_discipline",
        "申请密级": "security_marking",
        "密级": "security_marking",
        "保密期限": "embargo_until",
        "保密起始日期": "embargo_start",
        "保密开始日期": "embargo_start",
        "审批表编号": "approval_number",
        "论文审批表编号": "approval_number",
        "审批表号": "approval_number",
        "批准日期": "approval_date",
        "批准时间": "approval_date",
    }
    administrative_labels = {
        "申请密级", "密级", "保密期限", "保密起始日期", "保密开始日期",
        "审批表编号", "论文审批表编号", "审批表号", "批准日期", "批准时间",
    }
    ordinary_fields = cover.get("fields", [])
    if not isinstance(ordinary_fields, list):
        ordinary_fields = []
    for index, field in enumerate(ordinary_fields):
        if not isinstance(field, dict):
            continue
        field_id = field.get("id")
        label = normalize_label(field.get("label"))
        expected = expected_by_label.get(label)
        if label in administrative_labels:
            errors.append(
                f"$.cover.fields[{index}]: administrative label {label!r} must be "
                "declared under cover.non_public_administration, not ordinary cover.fields"
            )
        if expected and field_id != expected:
            errors.append(
                f"$.cover.fields[{index}]: label {label!r} must bind to {expected!r}, "
                f"not {field_id!r}"
            )
        value_from = field.get("value_from")
        if isinstance(value_from, str) and field_id and value_from != f"thesis_profile.cover_metadata.{field_id}":
            errors.append(
                f"$.cover.fields[{index}].value_from: must bind to its own field id {field_id!r}"
            )
    admin = cover.get("non_public_administration")
    if not ordinary_fields and not isinstance(admin, dict):
        errors.append(
            "$.cover.fields: must contain an ordinary cover field unless "
            "cover.non_public_administration is present"
        )
    if admin is not None and not isinstance(admin, dict):
        errors.append("$.cover.non_public_administration: must be an object")
    if isinstance(admin, dict):
        applicability = admin.get("applicability")
        conditions = applicability.get("conditions") if isinstance(applicability, dict) else None
        security_conditions = [
            item for item in conditions if isinstance(item, dict)
            and item.get("fact") == "thesis_profile.security_level"
            and item.get("operator") in {"equals", "in"}
        ] if isinstance(conditions, list) else []
        if not security_conditions:
            errors.append(
                "$.cover.non_public_administration.applicability: must be conditional "
                "on thesis_profile.security_level"
            )
        else:
            allowed = set()
            for condition in security_conditions:
                value = condition.get("value")
                if condition.get("operator") == "equals":
                    allowed.add(value)
                elif isinstance(value, list):
                    allowed.update(value)
            if not allowed & {"restricted", "classified"}:
                errors.append(
                    "$.cover.non_public_administration.applicability: must select "
                    "restricted or classified theses"
                )
        fields = admin.get("fields", [])
        field_ids = {
            item.get("id") for item in fields
            if isinstance(item, dict)
        }
        if "embargo_until" in field_ids and "embargo_start" not in field_ids:
            errors.append(
                "$.cover.non_public_administration.fields: an embargo range must bind "
                "both embargo_start and embargo_until"
            )
        admin_fields = fields if isinstance(fields, list) else []
        embargo_until_indexes = [
            index for index, item in enumerate(admin_fields)
            if isinstance(item, dict) and item.get("id") == "embargo_until"
        ]
        for index, field in enumerate(admin_fields):
            if not isinstance(field, dict):
                continue
            field_id = field.get("id")
            label = normalize_label(field.get("label"))
            expected = expected_by_label.get(label)
            # Some official tables print one visible "保密期限" label over
            # a two-ended date range.  Preserve that source label while
            # binding the first endpoint to embargo_start and the later one
            # to embargo_until.  The pair and its order are deterministic;
            # this does not authorize a free-form semantic relabeling.
            paired_range_start = (
                label == "保密期限"
                and field_id == "embargo_start"
                and bool(embargo_until_indexes)
                and index < min(embargo_until_indexes)
            )
            if expected and field_id != expected and not paired_range_start:
                errors.append(
                    f"$.cover.non_public_administration.fields[{index}]: label {label!r} "
                    f"must bind to {expected!r}, not {field_id!r}"
                )
            value_from = field.get("value_from")
            if field_id and value_from != f"thesis_profile.cover_metadata.{field_id}":
                errors.append(
                    f"$.cover.non_public_administration.fields[{index}].value_from: "
                    f"must bind to {field_id!r}"
                )
        if admin.get("public_policy") != "blank":
            errors.append("$.cover.non_public_administration.public_policy: must be 'blank'")
        if ("publication_default_policy" in admin
                and admin["publication_default_policy"] != "unapproved_is_public"):
            errors.append(
                "$.cover.non_public_administration.publication_default_policy: "
                "must be 'unapproved_is_public'"
            )
    return errors


def input_prerequisite_errors(spec: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for index, requirement in enumerate(spec.get("requirements", []) if isinstance(spec, dict) else []):
        if not isinstance(requirement, dict):
            continue
        prerequisites = requirement.get("input_prerequisites", [])
        if not isinstance(prerequisites, list):
            continue  # The schema reports malformed containers separately.
        for pindex, prerequisite in enumerate(prerequisites):
            if not isinstance(prerequisite, dict):
                continue
            key = prerequisite.get("key")
            if not registered_input_key(key):
                errors.append(
                    f"$.requirements[{index}].input_prerequisites[{pindex}].key: "
                    f"unregistered input path {key!r}"
                )
            expected_prefix = INPUT_KIND_PREFIXES.get(prerequisite.get("kind"))
            aggregate_kind_matches = (
                isinstance(key, str)
                and key in REGISTERED_AGGREGATE_INPUTS
                and REGISTERED_AGGREGATE_INPUTS.get(key) == prerequisite.get("kind")
            )
            if expected_prefix and not aggregate_kind_matches and (
                    not isinstance(key, str) or not key.startswith(expected_prefix)):
                errors.append(
                    f"$.requirements[{index}].input_prerequisites[{pindex}].key: "
                    f"kind {prerequisite.get('kind')!r} must use namespace {expected_prefix!r}"
                )
    return errors


def verification_checker_errors(spec: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for index, requirement in enumerate(spec.get("requirements", []) if isinstance(spec, dict) else []):
        if not isinstance(requirement, dict):
            continue
        verification = requirement.get("verification")
        if not isinstance(verification, dict):
            continue
        checker_ids = verification.get("checker_ids", [])
        if not isinstance(checker_ids, list):
            errors.append(f"$.requirements[{index}].verification.checker_ids: must be an array")
            continue
        for checker_index, checker_id in enumerate(checker_ids):
            if checker_id not in REGISTERED_CHECKER_IDS:
                errors.append(
                    f"$.requirements[{index}].verification.checker_ids[{checker_index}]: "
                    f"unregistered checker id {checker_id!r}"
                )
    return errors
