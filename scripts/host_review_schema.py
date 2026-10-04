"""Shared schema fragments and native structured-output checks.

The host-review response is consumed by more than one layer: the model-facing
request, the local contract validator, and native structured-output adapters.
Keeping the provider-sensitive fragments here prevents a permissive JSON Schema
placeholder from silently reaching a strict provider.
"""
from __future__ import annotations

import copy
import json
from typing import Any

from compliance import classification_requires_requirement
from format_contract_guards import registered_input_catalog, input_prerequisite_generation_schema
from responsibility_ledger import route_for_obligation


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


_COMPOSITION_KEYS = {"$ref", "const", "enum", "anyOf"}
PRIMARY_CLAUSE_REVIEW_WIRE_FORMAT = "host_review_v3_clause_reviews_by_id_v1"
_PRIMARY_CLAUSE_REVIEW_WIRE_FIELD = "response_wire_format"
_PRIMARY_CLAUSE_REVIEW_DEF = "primaryClauseReviewByIdV1"

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
    "allOf", "oneOf", "not", "if", "then", "else",
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
    # The provider cannot enforce these keywords, but it can see their
    # meaning. This annotation never replaces the unchanged local validator.
    local_constraints = {
        key: node[key] for key in sorted(_NATIVE_UNSUPPORTED_KEYWORDS) if key in node
    }
    if local_constraints:
        guidance = (
            "Local validator also requires: "
            + json.dumps(local_constraints, ensure_ascii=False, sort_keys=True)
            + ". These constraints remain mandatory after native decoding."
        )
        description = projected.get("description")
        projected["description"] = (
            f"{description}\n{guidance}" if isinstance(description, str) and description
            else guidance
        )
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


def primary_generation_schema(response_schema: dict[str, Any]) -> dict[str, Any]:
    """Require coherent explicit dimensions in NEW v3 primary proposals.

    This is not the canonical historical-response schema, not an interpretation,
    and not the independent-review schema. Retries keep their parent contract;
    an omitted field may only change through authenticated named reassessment.
    No value is inserted into a response. Unknown/conflicted values remain
    explicit semantic judgments, never a compliance pass.
    """
    schema = copy.deepcopy(response_schema)
    properties = schema.get("properties", {})
    if properties.get("contract_version", {}).get("const") != "3.0":
        return schema
    items = properties.get("clause_reviews", {}).get("items", {})
    generated_branches = []
    for branch in items.get("anyOf", [items]):
        atom = branch.get("properties", {}).get("obligations", {}).get("items", {})
        fields = atom.get("properties", {})
        if not all(name in fields for name in ("force", "applicability")):
            generated_branches.append(branch)
            continue
        required = atom.setdefault("required", [])
        for name in ("force", "applicability"):
            if name not in required:
                required.append(name)
        _couple_covered_scope_for_generation(atom)
        classifications = branch.get("properties", {}).get("classification", {}).get("enum", [])
        statuses = fields.get("status", {}).get("enum", [])
        if not classifications or not statuses or "route" not in fields:
            generated_branches.append(branch)
            continue
        # Equal routing vectors share a review branch. The source classification
        # and status remain model choices; only their existing derived route is
        # constrained. Explicit routes are required by declaration projection.
        groups: dict[tuple[str, ...], list[str]] = {}
        for classification in classifications:
            routes = tuple(route_for_obligation(classification, status) for status in statuses)
            groups.setdefault(routes, []).append(classification)
        for names in groups.values():
            generated = copy.deepcopy(branch)
            generated["properties"]["classification"]["enum"] = names
            _couple_routes_for_generation(
                generated["properties"]["obligations"]["items"], names[0],
            )
            generated_branches.append(generated)
    if "anyOf" in items:
        items["anyOf"] = generated_branches
    elif generated_branches:
        properties["clause_reviews"]["items"] = {"anyOf": generated_branches}
    # This value is a deterministic projection of a narrowly recognized exact
    # source policy. Let the bridge materialize it only after an exact source
    # binding; a model-authored value can otherwise attach it to a label-only
    # clause and fail the source binding contract.
    administration = schema.get("$defs", {}).get("nonPublicAdministrationSpec")
    if isinstance(administration, dict):
        admin_properties = administration.get("properties")
        if isinstance(admin_properties, dict):
            admin_properties.pop("publication_default_policy", None)
    return schema


def _clause_review_ids(response_schema: dict[str, Any]) -> set[str]:
    """Collect the clause IDs permitted by a canonical v3 review item schema."""
    items = (response_schema.get("properties", {}).get("clause_reviews", {})
             if isinstance(response_schema.get("properties"), dict) else {})
    item_schema = items.get("items") if isinstance(items, dict) else None
    found: set[str] = set()

    def visit(node: Any) -> None:
        if not isinstance(node, dict):
            return
        properties = node.get("properties")
        clause_id = properties.get("clause_id") if isinstance(properties, dict) else None
        enum = clause_id.get("enum") if isinstance(clause_id, dict) else None
        if isinstance(enum, list):
            found.update(value for value in enum if isinstance(value, str) and value)
        for key in ("anyOf", "allOf", "oneOf"):
            children = node.get(key)
            if isinstance(children, list):
                for child in children:
                    visit(child)

    visit(item_schema)
    return found


def primary_clause_review_wire_schema(
    response_schema: dict[str, Any], clause_ids: list[str],
) -> dict[str, Any]:
    """Require one keyed review slot for every exact current v3 clause ID.

    The canonical local response remains an array. This versioned native
    transport object makes missing/extra clause slots structurally impossible
    under strict output decoding while allowing existing array receipts to be
    replayed unchanged.
    """
    schema = copy.deepcopy(response_schema)
    properties = schema.get("properties")
    if (not isinstance(properties, dict)
            or properties.get("contract_version", {}).get("const") != "3.0"):
        return schema
    existing_format = properties.get(_PRIMARY_CLAUSE_REVIEW_WIRE_FIELD)
    review_container = properties.get("clause_reviews")
    if (isinstance(existing_format, dict)
            and existing_format.get("enum") == [PRIMARY_CLAUSE_REVIEW_WIRE_FORMAT]
            and isinstance(review_container, dict)
            and review_container.get("type") == "object"):
        return schema

    ids = list(clause_ids)
    if (not ids or any(not isinstance(value, str) or not value for value in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError("fresh v3 primary response requires unique current clause IDs")
    if set(ids) != _clause_review_ids(schema):
        raise ValueError("fresh v3 primary clause IDs do not match the canonical response schema")
    if not isinstance(review_container, dict) or review_container.get("type") != "array":
        raise ValueError("fresh v3 primary response has no canonical clause-review array")
    item_schema = review_container.get("items")
    if not isinstance(item_schema, dict):
        raise ValueError("fresh v3 primary response has no clause-review item schema")
    definitions = schema.setdefault("$defs", {})
    if not isinstance(definitions, dict) or _PRIMARY_CLAUSE_REVIEW_DEF in definitions:
        raise ValueError("fresh v3 primary clause-review wire definition collides with an existing definition")
    definitions[_PRIMARY_CLAUSE_REVIEW_DEF] = copy.deepcopy(item_schema)
    properties["clause_reviews"] = {
        "type": "object",
        "properties": {
            clause_id: {"$ref": f"#/$defs/{_PRIMARY_CLAUSE_REVIEW_DEF}"}
            for clause_id in ids
        },
        "required": ids,
        "additionalProperties": False,
        "description": (
            "Versioned native transport map: include every required current clause ID "
            "exactly once as a key. The value's clause_id must equal that key."
        ),
    }
    properties[_PRIMARY_CLAUSE_REVIEW_WIRE_FIELD] = {
        "type": "string",
        "enum": [PRIMARY_CLAUSE_REVIEW_WIRE_FORMAT],
        "description": "Transport format marker; the local bridge removes it after exact canonicalization.",
    }
    required = schema.setdefault("required", [])
    if _PRIMARY_CLAUSE_REVIEW_WIRE_FIELD not in required:
        required.append(_PRIMARY_CLAUSE_REVIEW_WIRE_FIELD)
    return schema


def _couple_routes_for_generation(atom: dict[str, Any], classification: str) -> None:
    """Mirror the responsibility ledger without rewriting a producer answer."""
    required = atom.setdefault("required", [])
    if "route" not in required:
        required.append("route")
    if isinstance(atom.get("anyOf"), list):
        for alternative in atom["anyOf"]:
            _couple_routes_for_generation(alternative, classification)
        return
    groups: dict[str, list[str]] = {}
    for status in atom["properties"]["status"]["enum"]:
        groups.setdefault(route_for_obligation(classification, status), []).append(status)
    if len(groups) == 1:
        atom["properties"]["route"]["enum"] = list(groups)
        return
    alternatives = []
    for route, statuses in groups.items():
        alternative = copy.deepcopy(atom)
        alternative["properties"]["status"]["enum"] = statuses
        alternative["properties"]["route"]["enum"] = [route]
        alternatives.append(alternative)
    atom["anyOf"] = alternatives


def _couple_covered_scope_for_generation(atom: dict[str, Any]) -> None:
    """Express the existing covered/scope invariant with portable anyOf.

    Operates only on a generation-schema copy. Complete object alternatives
    survive native projection, unlike conditional assertions. All semantic
    fields remain model-owned; this supplies neither a duty nor its scope.
    """
    if isinstance(atom.get("anyOf"), list):
        for alternative in atom["anyOf"]:
            _couple_covered_scope_for_generation(alternative)
        return
    fields = atom.get("properties", {})
    statuses = fields.get("status", {}).get("enum", [])
    scopes = fields.get("applicability", {}).get("enum", [])
    if "covered" not in statuses or not set(scopes).intersection({"unknown", "conflicted"}):
        return
    covered = copy.deepcopy(atom)
    covered["properties"]["status"]["enum"] = ["covered"]
    covered["properties"]["applicability"]["enum"] = [
        value for value in scopes if value not in {"unknown", "conflicted"}
    ]
    pending = copy.deepcopy(atom)
    pending["properties"]["status"]["enum"] = [value for value in statuses if value != "covered"]
    atom["anyOf"] = [covered] if not pending["properties"]["status"]["enum"] else [covered, pending]


def native_output_schema(response_schema: dict[str, Any]) -> dict[str, Any]:
    """Return the strict schema sent to a native structured-output provider.

    ``provenance`` is deliberately absent: it is trusted invocation metadata
    bound by the bridge after the provider returns, never authored by the
    model. Declaration signature-line text, references and byte hashes are
    likewise code-owned: the provider emits only null (the omission sentinel),
    and the current-source materializer fills the locally validated payload.
    Other optional fields are required nullable fields in this projection.
    """
    local_schema = copy.deepcopy(response_schema)
    properties = local_schema.get("properties")
    if isinstance(properties, dict) and "provenance" in properties:
        properties.pop("provenance")
        local_schema["required"] = [
            name for name in local_schema.get("required", [])
            if name != "provenance"
        ]
    declaration_item = local_schema.get("$defs", {}).get("declarationItem")
    if isinstance(declaration_item, dict):
        item_properties = declaration_item.get("properties")
        if isinstance(item_properties, dict) and "source_signature_lines" in item_properties:
            # Restrict only this registered declaration field, not arbitrary
            # same-named fields or the authoritative format/resource schemas.
            # Never repair a model-provided hash by stamping current identity.
            item_properties["source_signature_lines"] = {
                "type": "null",
                "description": (
                    "Code-owned source projection. Emit null; never copy signature "
                    "text, evidence IDs or hashes here. Code preserves uniquely "
                    "adjacent blank current-source lines after local validation; "
                    "actual signing/dating remains pending human verification."
                ),
            }
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
    if "enum" in schema and value not in schema["enum"]:
        return False
    if "const" in schema and value != schema["const"]:
        return False
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
            if any(key not in declared for key in value):
                return False
            # The provider-facing requirement union is discriminated by the
            # sibling ``role`` field.  Looking only at key names would select
            # the first object-shaped branch and could normalize a table
            # payload under a different role.  Recurse through actual values
            # so enums and role-specific property schemas participate.
            required = set(schema.get("required", []) or [])
            return all(
                (child is None and key not in required)
                or _schema_shape_matches(declared[key], child, root)
                for key, child in value.items()
            )
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


def _decode_primary_clause_review_wire(
    response: Any, response_schema: dict[str, Any],
) -> Any:
    """Decode only the explicitly marked, exact-key v3 transport shape."""
    if (not isinstance(response, dict)
            or response.get("contract_version") != "3.0"
            or response.get(_PRIMARY_CLAUSE_REVIEW_WIRE_FIELD)
            != PRIMARY_CLAUSE_REVIEW_WIRE_FORMAT):
        return response
    keyed = response.get("clause_reviews")
    expected_ids = sorted(_clause_review_ids(response_schema))
    if (not isinstance(keyed, dict) or not expected_ids
            or set(keyed) != set(expected_ids)):
        return response
    reviews: list[dict[str, Any]] = []
    for clause_id in expected_ids:
        review = keyed.get(clause_id)
        if not isinstance(review, dict) or review.get("clause_id") != clause_id:
            return response
        reviews.append(copy.deepcopy(review))
    decoded = copy.deepcopy(response)
    decoded.pop(_PRIMARY_CLAUSE_REVIEW_WIRE_FIELD, None)
    decoded["clause_reviews"] = reviews
    return decoded


def normalize_native_response(
    response: Any, response_schema: dict[str, Any],
) -> Any:
    """Canonicalize native wire encodings and provider-required null optionals.

    Native strict schemas encode local optional properties as required nullable
    properties.  A ``null`` at a property that is optional in the local
    contract means exactly "omitted"; removing it restores the local contract
    without changing a semantic value. For v3 informational/not-applicable
    reviews not referenced by any requirement or diagnostic, omitted/null/empty
    optional inventories share the explicit empty-array normal form. Linked
    reviews retain their inventory-presence signal for context-edge repair.
    Nonempty inventories and required arrays are never erased. Required nulls and values
    in unconstrained branches are preserved for the normal fail-closed
    validator. Fresh v3 primary calls may also use the explicitly marked keyed
    clause map; it is decoded only when every current key is present and each
    row's ``clause_id`` matches the key. Historical canonical arrays pass
    through unchanged. Requirement ``properties`` is represented by the union of
    the registered role schemas, so nullable provider fields can be normalized
    without turning an arbitrary object into an accepted semantic payload.
    """
    response = _decode_primary_clause_review_wire(response, response_schema)
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
                # Only a declared optional property may use null as the
                # provider's omission sentinel. Preserve unknown keys (even
                # null-valued ones) so the local additionalProperties check
                # rejects them instead of normalization erasing the violation.
                if child is None and key in properties and key not in required:
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

    normalized = normalize(response, response_schema)
    if (isinstance(normalized, dict)
            and normalized.get("contract_version") == "3.0"
            and root.get("properties", {}).get("contract_version", {}).get("const") == "3.0"):
        review_schema = root.get("properties", {}).get("clause_reviews", {}).get("items", {})
        reviews = normalized.get("clause_reviews")

        def references(value: Any, clause_id: str) -> bool:
            if isinstance(value, str):
                return clause_id in value
            if isinstance(value, list):
                return any(references(item, clause_id) for item in value)
            if isinstance(value, dict):
                return any(references(key, clause_id) or references(child, clause_id)
                           for key, child in value.items())
            return False

        consumers = {key: normalized.get(key) for key in
                     ("requirements", "reported_conflicts", "unsupported_items")}
        if isinstance(reviews, list):
            for review in reviews:
                if (not isinstance(review, dict)
                        or not isinstance(review.get("classification"), str)
                        or review.get("classification") not in {"informational", "not_applicable"}
                        or ("obligations" in review and review["obligations"] != [])
                        or not isinstance(review.get("clause_id"), str)
                        or not review["clause_id"]
                        or references(consumers, review["clause_id"])):
                    continue
                branch = _schema_for_value(review_schema, review, root)
                inventory = branch.get("properties", {}).get("obligations", {})
                if (inventory.get("type") == "array"
                        and inventory.get("minItems", 0) == 0
                        and "obligations" not in branch.get("required", [])):
                    # Never invent atoms. Leave linked reviews untouched:
                    # context-edge projection requires explicit empty input.
                    # Raw output remains unchanged in its decoded artifact.
                    review["obligations"] = []
    return normalized


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
    for keyword in sorted(set(schema) & _NATIVE_UNSUPPORTED_KEYWORDS):
        errors.append(f"{path}: native_schema_unsupported_keyword:{keyword}")
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
    eligible_existing_ids: list[str] | None = None,
    eligible_clause_ids: list[str] | None = None,
    input_catalog: dict[str, list[str]] | None = None,
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
    request_defs["inputPrerequisiteSpec"] = input_prerequisite_generation_schema(
        request_defs["inputPrerequisiteSpec"], input_catalog or registered_input_catalog(),
    )
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
    reported_conflict_definition = request_defs.get("reportedConflict")
    if isinstance(reported_conflict_definition, dict):
        target_role = (
            reported_conflict_definition.get("properties", {})
            .get("target", {}).get("properties", {}).get("role")
        )
        if isinstance(target_role, dict):
            target_role["enum"] = sorted(allowed_requirement_roles)
    # Use the exact current packet IDs in the local contract and in its native
    # projection. Never rely on the model to preserve zero padding or infer an
    # ID from a nearby clause. The post-response relation validator remains
    # authoritative for which of these IDs may be cited together.
    clause_ids = sorted(set(eligible_clause_ids or []))
    clause_id_schema = (
        {"type": "string", "enum": clause_ids}
        if clause_ids else {"type": "string"}
    )
    if isinstance(reported_conflict_definition, dict) and clause_ids:
        reported_conflict_definition["properties"]["clause_ids"]["items"] = copy.deepcopy(
            clause_id_schema
        )
    requirement_common_properties = {
        "existing_requirement_id": {"type": "string", "minLength": 1},
        "field_key": {"type": "string", "minLength": 1},
        "clause_ids": {
            "type": "array", "items": copy.deepcopy(clause_id_schema),
            "description": "Must contain at least one current source clause ID; the local relation validator rejects an empty array.",
        },
        "source_fragment_clause_ids": {
            "type": "array", "items": {**copy.deepcopy(clause_id_schema), "minLength": 1},
            "minItems": 1, "uniqueItems": True,
            "description": "Ordered, structurally adjacent references for one literal occurrence. Code materializes exact text; repeated text at distinct locations requires separate requirements, not concatenation.",
        },
        "evidence_ids": {
            "type": "array", "items": {"type": "string"},
            "description": "Must contain at least one evidence ID backed by the cited current clauses; the local relation validator rejects an empty array.",
        },
        "confidence": {"type": "number"},
        "reason": {"type": "string", "minLength": 1},
        "applicability": {"$ref": "#/$defs/applicabilitySpec"},
        "input_prerequisites": {"type": "array", "items": {"$ref": "#/$defs/inputPrerequisiteSpec"}},
        "verification": {"$ref": "#/$defs/verificationSpec"},
    }
    requirement_required = [
        "role", "properties", "clause_ids", "evidence_ids", "confidence", "reason",
    ]
    requirement_role_branches = []
    for role in sorted(role_schema_names):
        branch_properties = copy.deepcopy(requirement_common_properties)
        branch_properties["role"] = {"enum": [role]}
        branch_properties["properties"] = {"$ref": f"#/$defs/{role_schema_names[role]}"}
        requirement_role_branches.append({
            "type": "object",
            "required": requirement_required,
            "properties": branch_properties,
            "additionalProperties": False,
        })

    response_schema: dict[str, Any] = {
        "type": "object",
        "required": ["contract_version", "requirements", "clause_reviews", "unsupported_items", "reported_conflicts"],
        "properties": {
            "contract_version": {"const": contract_version},
            "provenance": {"type": "object", "required": [
                "version", "origin", "source_sha256", "evidence_sha256",
                "clause_sha256", "request_sha256",
            ]},
            "requirements": {"type": "array", "items": {"anyOf": requirement_role_branches}},
            "clause_reviews": {"type": "array", "items": {
                "type": "object",
                "required": (
                    ["clause_id", "classification", "reason"]
                    if contract_version == "3.0"
                    else ["clause_id", "classification", "requirement_indexes", "reason"]
                ),
                "properties": {
                    "clause_id": copy.deepcopy(clause_id_schema),
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
                            "actor": {"type": "string", "minLength": 1},
                            "action": {"type": "string", "minLength": 1},
                            "target": {"type": "string", "minLength": 1},
                            "condition": {"type": "string", "minLength": 1},
                            "source_quote": {"type": "string", "minLength": 1},
                            "force": {"enum": ["required", "prohibited", "recommended", "optional", "unknown"]},
                            "applicability": {"enum": ["applicable", "not_applicable", "unknown", "conflicted"]},
                            "route": {"enum": ["automatic", "check_only", "human", "input", "unknown", "example"]},
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
            "reported_conflicts": {
                "type": "array", "items": {"$ref": "#/$defs/reportedConflict"},
            },
        },
        "$defs": request_defs,
        "additionalProperties": False,
    }
    if contract_version == "2.1":
        response_schema["properties"]["clause_reviews"]["items"]["properties"]["requirement_indexes"] = {
            "type": "array", "items": {"type": "integer", "minimum": 0}, "uniqueItems": True,
        }
    elif contract_version == "3.0":
        review_template = response_schema["properties"]["clause_reviews"]["items"]
        review_branches = []
        for executable in (True, False):
            classifications = sorted(
                name for name in allowed_review_classifications
                if classification_requires_requirement(name) == executable
            )
            if not classifications:
                continue
            branch = copy.deepcopy(review_template)
            branch["properties"]["classification"] = {"enum": classifications}
            if executable:
                # Optional native fields become nullable. This field must not:
                # the model, not a mechanical filler, supplies semantic duties.
                branch["required"].append("obligations")
                branch["properties"]["obligations"]["minItems"] = 1
            review_branches.append(branch)
        response_schema["properties"]["clause_reviews"]["items"] = {"anyOf": review_branches}
    if eligible_existing_ids is not None:
        # Scope before hashing the request, not later inside a host adapter.
        # Native optional fields become nullable; null means a NEW requirement.
        ids = sorted(set(eligible_existing_ids))
        existing_id_schema = {"type": "string", "enum": ids} if ids else {"type": "null"}
        for branch in response_schema["properties"]["requirements"]["items"]["anyOf"]:
            branch["properties"]["existing_requirement_id"] = copy.deepcopy(existing_id_schema)
    return response_schema
