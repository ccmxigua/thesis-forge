"""Shared schema fragments and native structured-output checks.

The host-review response is consumed by more than one layer: the model-facing
request, the local contract validator, and native structured-output adapters.
Keeping the provider-sensitive fragments here prevents a permissive JSON Schema
placeholder from silently reaching a strict provider.
"""
from __future__ import annotations

import copy
from typing import Any


def applicability_value_schema() -> dict[str, Any]:
    """Return the closed value domain used by applicability conditions.

    Conditions are evaluated against scalar profile/inventory facts or a list
    of scalar candidates (the ``in`` operator).  An unconstrained ``{}`` is
    valid JSON Schema, but it is not a valid native structured-output schema
    and would make the provider reject the entire request.
    """
    scalar = [
        {"type": "string"},
        {"type": "number"},
        {"type": "boolean"},
        {"type": "null"},
    ]
    return {
        "anyOf": [
            *scalar,
            {"type": "array", "items": {"anyOf": scalar}},
        ]
    }


_COMPOSITION_KEYS = {"$ref", "const", "enum", "anyOf", "allOf", "oneOf", "not", "if"}

# OpenAI-compatible strict structured outputs accept the shape of JSON data,
# but not every JSON-Schema validation keyword.  These constraints remain in
# the local contract schema and are checked after the provider returns; they
# are omitted only from the provider-facing projection.
_NATIVE_UNSUPPORTED_KEYWORDS = frozenset({
    "minLength", "maxLength", "pattern", "format",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minItems", "maxItems", "uniqueItems", "contains",
    "minProperties", "maxProperties", "propertyNames", "patternProperties",
    "dependencies", "dependentRequired", "dependentSchemas",
    "unevaluatedProperties", "unevaluatedItems",
})


def _nullable_native_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Make a formerly optional property legal in strict native output.

    OpenAI-compatible structured outputs require every object property to be
    listed in ``required``.  Optionality is therefore represented by a
    nullable value instead of by omitting the property.  This transformation
    is only for the provider-facing projection; the local contract schema
    keeps the original optional-property semantics.
    """
    projected = copy.deepcopy(schema)
    variants = projected.get("anyOf")
    if isinstance(variants, list):
        if not any(isinstance(item, dict) and item.get("type") == "null" for item in variants):
            variants.append({"type": "null"})
        return projected
    if projected.get("type") == "null":
        return projected
    return {"anyOf": [projected, {"type": "null"}]}


def _project_native_schema(node: Any) -> Any:
    """Compile a local JSON Schema node to strict native-output form."""
    if isinstance(node, list):
        return [_project_native_schema(item) for item in node]
    if not isinstance(node, dict):
        return copy.deepcopy(node)

    projected = {
        key: copy.deepcopy(value)
        for key, value in node.items()
        if key not in _NATIVE_UNSUPPORTED_KEYWORDS
    }
    # ``enum`` is supported by the native subset and is a portable equivalent
    # for a single-value ``const`` assertion.
    if "const" in projected and "enum" not in projected:
        projected["enum"] = [projected.pop("const")]
    properties = projected.get("properties")
    if isinstance(properties, dict):
        original_required = set(projected.get("required", []) or [])
        native_properties: dict[str, Any] = {}
        for name, child in properties.items():
            child_projection = _project_native_schema(child)
            if name not in original_required:
                child_projection = _nullable_native_schema(child_projection)
            native_properties[name] = child_projection
        projected["properties"] = native_properties
        projected["required"] = list(native_properties)
        projected["additionalProperties"] = False
    elif projected.get("type") == "object":
        # An object with no declared fields is still made explicit so the
        # provider never sees an underspecified object placeholder.
        projected.setdefault("properties", {})
        projected["required"] = list(projected["properties"])
        projected["additionalProperties"] = False

    if isinstance(projected.get("$defs"), dict):
        projected["$defs"] = {
            name: _project_native_schema(child)
            for name, child in projected["$defs"].items()
        }
    if isinstance(projected.get("items"), dict):
        projected["items"] = _project_native_schema(projected["items"])
    if isinstance(projected.get("additionalProperties"), dict):
        projected["additionalProperties"] = _project_native_schema(
            projected["additionalProperties"]
        )
    for key in ("anyOf", "allOf", "oneOf"):
        if isinstance(projected.get(key), list):
            projected[key] = [_project_native_schema(child) for child in projected[key]]
    for key in ("not", "if", "then", "else"):
        if isinstance(projected.get(key), dict):
            projected[key] = _project_native_schema(projected[key])
    return projected


def native_output_schema(response_schema: dict[str, Any]) -> dict[str, Any]:
    """Return the strict schema sent to a native structured-output provider.

    ``provenance`` is deliberately absent: it is trusted invocation metadata
    bound by the bridge after the provider returns, never authored by the
    model.  All other optional fields are represented as required nullable
    fields in the provider projection.
    """
    local_schema = copy.deepcopy(response_schema)
    properties = local_schema.get("properties")
    if isinstance(properties, dict) and "provenance" in properties:
        properties.pop("provenance")
        local_schema["required"] = [
            name for name in local_schema.get("required", [])
            if name != "provenance"
        ]
    return _project_native_schema(local_schema)


def _resolve_schema_ref(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    current = schema
    seen: set[str] = set()
    while isinstance(current, dict) and isinstance(current.get("$ref"), str):
        reference = current["$ref"]
        if reference in seen or not reference.startswith("#/$defs/"):
            break
        seen.add(reference)
        name = reference.removeprefix("#/$defs/")
        target = root.get("$defs", {}).get(name)
        if not isinstance(target, dict):
            break
        current = target
    return current


def _schema_shape_matches(schema: dict[str, Any], value: Any, root: dict[str, Any]) -> bool:
    schema = _resolve_schema_ref(schema, root)
    if isinstance(schema.get("anyOf"), list):
        return any(
            isinstance(item, dict) and _schema_shape_matches(item, value, root)
            for item in schema["anyOf"]
        )
    schema_type = schema.get("type")
    if schema_type == "object":
        if not isinstance(value, dict):
            return False
        # Several role schemas are object-shaped.  Use their closed property
        # names to select the right local branch when normalizing the native
        # provider's required-nullable projection.  Without this check the
        # first object branch (usually roleSpec) wins for every role, leaving
        # nulls from page/cover/declaration-specific fields in the response.
        declared = schema.get("properties")
        if isinstance(declared, dict) and schema.get("additionalProperties") is False:
            return all(key in declared for key in value)
        return True
    if schema_type == "array":
        return isinstance(value, list)
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "boolean":
        return isinstance(value, bool)
    if schema_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "null":
        return value is None
    return True


def _schema_for_value(schema: dict[str, Any], value: Any, root: dict[str, Any]) -> dict[str, Any]:
    resolved = _resolve_schema_ref(schema, root)
    variants = resolved.get("anyOf")
    if isinstance(variants, list):
        for variant in variants:
            if isinstance(variant, dict) and _schema_shape_matches(variant, value, root):
                return _resolve_schema_ref(variant, root)
    return resolved


def normalize_native_response(
    response: Any, response_schema: dict[str, Any],
) -> Any:
    """Canonicalize provider-required nullable optionals before local checks.

    Native strict schemas encode local optional properties as required nullable
    properties.  A ``null`` at a property that is optional in the local
    contract means exactly "omitted"; removing it restores the local contract
    without changing any non-null semantic value.  Required nulls and values
    in unconstrained branches are preserved for the normal fail-closed
    validator.  Requirement ``properties`` is represented by the union of the
    registered role schemas, so nullable provider fields can be normalized
    without turning an arbitrary object into an accepted semantic payload.
    """
    root = response_schema

    def normalize(value: Any, schema: Any) -> Any:
        if not isinstance(schema, dict):
            return copy.deepcopy(value)
        if value is None:
            return None
        resolved = _schema_for_value(schema, value, root)
        if isinstance(value, dict):
            properties = resolved.get("properties")
            if not isinstance(properties, dict):
                return copy.deepcopy(value)
            required = set(resolved.get("required", []) or [])
            normalized: dict[str, Any] = {}
            for key, child in value.items():
                child_schema = properties.get(key)
                if child is None and key not in required:
                    continue
                if isinstance(child_schema, dict):
                    normalized[key] = normalize(child, child_schema)
                else:
                    normalized[key] = copy.deepcopy(child)
            return normalized
        if isinstance(value, list):
            item_schema = resolved.get("items")
            if isinstance(item_schema, dict):
                return [normalize(item, item_schema) for item in value]
        return copy.deepcopy(value)

    return normalize(response, response_schema)


def native_schema_support_errors(schema: Any, path: str = "$") -> list[str]:
    """Reject schema constructs that native structured output cannot accept.

    This is deliberately a small provider-boundary check, not a replacement
    for the project JSON Schema validator.  The most important invariant is
    that no empty schema reaches a native adapter: a permissive local validator
    may accept it, while the provider requires a concrete ``type`` at that
    location.
    """
    if not isinstance(schema, dict):
        return [f"{path}: native schema must be an object"]
    errors: list[str] = []
    if not schema:
        errors.append(f"{path}: native_schema_empty_schema")
    if "type" not in schema and not (set(schema) & _COMPOSITION_KEYS):
        # ``description``/``title`` alone are not a value schema.  ``enum``
        # and composition forms are intentionally exempt because they carry
        # an explicit assertion without a separate type keyword.
        errors.append(f"{path}: native_schema_missing_type")
    properties = schema.get("properties")
    if isinstance(properties, dict):
        required = schema.get("required")
        if not isinstance(required, list) or set(required) != set(properties):
            errors.append(f"{path}: native_schema_required_must_cover_properties")
        if schema.get("additionalProperties") is not False:
            errors.append(f"{path}: native_schema_additional_properties_must_be_false")
    for name, child in (properties or {}).items():
        errors.extend(native_schema_support_errors(child, f"{path}.properties.{name}"))
    for name, child in (schema.get("$defs") or {}).items():
        errors.extend(native_schema_support_errors(child, f"{path}.$defs.{name}"))
    if isinstance(schema.get("items"), dict):
        errors.extend(native_schema_support_errors(schema["items"], f"{path}.items"))
    if isinstance(schema.get("additionalProperties"), dict):
        errors.extend(native_schema_support_errors(schema["additionalProperties"], f"{path}.additionalProperties"))
    for key in ("anyOf", "allOf", "oneOf"):
        for index, child in enumerate(schema.get(key, []) or []):
            errors.extend(native_schema_support_errors(child, f"{path}.{key}[{index}]"))
    for key in ("not", "if", "then", "else"):
        if isinstance(schema.get(key), dict):
            errors.extend(native_schema_support_errors(schema[key], f"{path}.{key}"))
    return errors


def require_native_schema(schema: dict[str, Any]) -> None:
    """Raise before a provider call when the native schema is malformed."""
    errors = native_schema_support_errors(schema)
    if errors:
        raise ValueError("native Host Review response schema is not provider-compatible: " + "; ".join(errors[:12]))


def build_host_review_response_schema(
    format_schema: dict[str, Any],
    *,
    allowed_requirement_roles: set[str],
    top_level_requirement_roles: set[str],
    allowed_review_classifications: set[str],
    contract_version: str,
) -> dict[str, Any]:
    """Compile the one Host Review response schema used by all adapters.

    The format-spec schema remains the source of role property definitions;
    this function owns the response envelope and its contract-version-specific
    relation rule.  Contract 3.0 intentionally has no model-maintained reverse
    integer index.
    """
    role_schema_by_name = {
        "page": "pageSpec", "table": "tableSpec", "objects": "objectPaginationSpec",
        "content_constraints": "contentConstraintSpec",
        "conditional_constraints": "conditionalConstraintSpec",
        "document_structure": "documentStructureSpec", "appendices": "appendixSpec",
        "equations": "equationLayoutSpec", "cover": "coverSpec",
        "declarations": "declarationsSpec",
    }
    role_schema_names = {
        role: ("roleSpec" if role not in top_level_requirement_roles else role_schema_by_name[role])
        for role in sorted(allowed_requirement_roles)
    }
    request_defs = {
        key: copy.deepcopy(value)
        for key, value in format_schema.get("$defs", {}).items()
        if key not in {"contentInstance", "coverFieldInstance"}
    }
    if isinstance(request_defs.get("requirement"), dict):
        request_defs["requirement"].get("properties", {}).pop("field_instance_ids", None)
    applicability = request_defs.get("applicabilitySpec")
    if isinstance(applicability, dict):
        condition_items = applicability.get("properties", {}).get("conditions", {}).get("items", {})
        if isinstance(condition_items, dict):
            condition_properties = condition_items.setdefault("properties", {})
            condition_properties["value"] = applicability_value_schema()
            fact_schema = condition_properties.get("fact")
            if isinstance(fact_schema, dict):
                fact_schema["pattern"] = r"^(thesis_profile|source_inventory|template_profile|runtime)\."
    response_schema: dict[str, Any] = {
        "type": "object",
        "required": ["contract_version", "requirements", "clause_reviews", "unsupported_items", "reported_conflicts"],
        "properties": {
            "contract_version": {"const": contract_version},
            "provenance": {"type": "object", "required": [
                "version", "origin", "source_sha256", "evidence_sha256",
                "clause_sha256", "request_sha256",
            ]},
            "requirements": {"type": "array", "items": {
                "type": "object", "required": [
                    "role", "properties", "clause_ids", "evidence_ids", "confidence", "reason",
                ],
                "properties": {
                    "existing_requirement_id": {"type": "string", "minLength": 1},
                    "role": {"enum": sorted(allowed_requirement_roles)},
                    "field_key": {"type": "string", "minLength": 1},
                    # The role is selected by the sibling ``role`` field and
                    # is checked again by the host-independent validator.
                    # A bare object schema projects to
                    # ``additionalProperties: false`` with no fields, which
                    # only permits ``{}`` and makes real style/text
                    # properties impossible to emit.
                    "properties": {
                        "anyOf": [
                            {"$ref": f"#/$defs/{role_schema_names[role]}"}
                            for role in sorted(role_schema_names)
                        ]
                    },
                    "clause_ids": {"type": "array", "items": {"type": "string"}},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string", "minLength": 1},
                    "applicability": {"$ref": "#/$defs/applicabilitySpec"},
                    "input_prerequisites": {"type": "array", "items": {"$ref": "#/$defs/inputPrerequisiteSpec"}},
                    "verification": {"$ref": "#/$defs/verificationSpec"},
                },
                "additionalProperties": False,
            }},
            "clause_reviews": {"type": "array", "items": {
                "type": "object",
                "required": (
                    ["clause_id", "classification", "reason"]
                    if contract_version == "3.0"
                    else ["clause_id", "classification", "requirement_indexes", "reason"]
                ),
                "properties": {
                    "clause_id": {"type": "string"},
                    "classification": {"enum": sorted(allowed_review_classifications)},
                    "reason": {"type": "string", "minLength": 1},
                    "obligations": {"type": "array", "items": {
                        "type": "object", "required": ["id", "status", "reason"],
                        "properties": {
                            "id": {"type": "string", "minLength": 1},
                            "status": {"enum": [
                                "covered", "requires_metadata", "requires_source_content",
                                "unsupported_backend", "unverifiable", "unresolved",
                            ]},
                            "reason": {"type": "string", "minLength": 1},
                        },
                        "additionalProperties": False,
                    }, "uniqueItems": True},
                    "normative_basis": {"enum": [
                        "explicit_normative_text", "template_structure", "fixed_statement",
                        "sample_content", "source_content", "external_duty", "insufficient",
                    ]},
                },
                "additionalProperties": False,
            }},
            "unsupported_items": {"type": "array", "items": {"type": "string"}},
            "reported_conflicts": {"type": "array", "items": {"type": "object"}},
        },
        "$defs": request_defs,
        "additionalProperties": False,
    }
    if contract_version == "2.1":
        response_schema["properties"]["clause_reviews"]["items"]["properties"]["requirement_indexes"] = {
            "type": "array", "items": {"type": "integer", "minimum": 0}, "uniqueItems": True,
        }
    return response_schema
