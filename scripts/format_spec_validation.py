#!/usr/bin/env python3
"""Small dependency-free JSON Schema validator for the format-spec contract.

It implements exactly the draft-2020-12 keywords used by
schema/format-spec.schema.json, so validation remains available in the base
runtime without silently degrading when the optional jsonschema package is not
installed.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

try:
    from .resource_registry import fixed_text_sha256
    from .format_contract_guards import (
        cover_binding_errors, input_prerequisite_errors, verification_checker_errors,
    )
except ImportError:  # direct script execution
    from resource_registry import fixed_text_sha256
    from format_contract_guards import (
        cover_binding_errors, input_prerequisite_errors, verification_checker_errors,
    )


def _resolve(root: dict[str, Any], ref: str) -> dict[str, Any]:
    if not ref.startswith("#/"):
        raise ValueError(f"unsupported external $ref: {ref}")
    node: Any = root
    for part in ref[2:].split("/"):
        node = node[part.replace("~1", "/").replace("~0", "~")]
    return node


def _typename(value: Any) -> str:
    if value is None: return "null"
    if isinstance(value, bool): return "boolean"
    if isinstance(value, dict): return "object"
    if isinstance(value, list): return "array"
    if isinstance(value, str): return "string"
    if isinstance(value, int): return "integer"
    if isinstance(value, float): return "number"
    return type(value).__name__


# The bundled validator intentionally implements a small, explicit subset of
# JSON Schema.  Silently ignoring a newer keyword is unsafe: a schema can then
# appear to pass while the executor never enforced its constraint.
_SUPPORTED_SCHEMA_KEYWORDS = {
    "$ref", "$defs", "$schema", "$id", "const", "enum", "type", "minProperties", "required",
    "properties", "additionalProperties", "minItems", "uniqueItems", "items",
    "maxItems", "minimum", "maximum", "exclusiveMinimum", "minLength", "pattern",
    "description", "title", "default", "anyOf", "allOf", "oneOf", "not", "if", "then", "else",
}
_SCHEMA_ANNOTATION_KEYWORDS = {"$comment", "examples", "deprecated", "readOnly", "writeOnly"}


def schema_support_errors(schema: Any, path: str = "$") -> list[str]:
    """Return unsupported JSON-Schema keywords instead of silently ignoring them."""
    errors: list[str] = []
    if not isinstance(schema, dict):
        return [f"{path}: schema must be an object"]
    for key, value in schema.items():
        if key not in _SUPPORTED_SCHEMA_KEYWORDS and key not in _SCHEMA_ANNOTATION_KEYWORDS:
            errors.append(f"{path}: unsupported_schema_keyword:{key}")
        if key == "properties" and isinstance(value, dict):
            for name, child in value.items():
                errors.extend(schema_support_errors(child, f"{path}.properties.{name}"))
        elif key == "$defs" and isinstance(value, dict):
            for name, child in value.items():
                errors.extend(schema_support_errors(child, f"{path}.$defs.{name}"))
        elif key == "items":
            errors.extend(schema_support_errors(value, f"{path}.items"))
        elif key == "additionalProperties" and isinstance(value, dict):
            errors.extend(schema_support_errors(value, f"{path}.additionalProperties"))
        elif key in {"anyOf", "allOf", "oneOf"} and isinstance(value, list):
            for index, child in enumerate(value):
                errors.extend(schema_support_errors(child, f"{path}.{key}[{index}]"))
        elif key in {"not", "if", "then", "else"} and isinstance(value, dict):
            errors.extend(schema_support_errors(value, f"{path}.{key}"))
    return errors


def validate_instance(instance: Any, schema: dict[str, Any], root: dict[str, Any] | None = None,
                      path: str = "$") -> list[str]:
    root_was_none = root is None
    root = schema if root is None else root
    support_errors = schema_support_errors(schema) if root_was_none else []
    if "$ref" in schema:
        return support_errors + validate_instance(instance, _resolve(root, schema["$ref"]), root, path)
    errors: list[str] = list(support_errors)
    if "allOf" in schema:
        for child in schema["allOf"]:
            errors.extend(validate_instance(instance, child, root, path))
    if "anyOf" in schema:
        if not any(not validate_instance(instance, child, root, path) for child in schema["anyOf"]):
            errors.append(f"{path}: must match at least one schema in anyOf")
    if "oneOf" in schema:
        matches = sum(not validate_instance(instance, child, root, path) for child in schema["oneOf"])
        if matches != 1:
            errors.append(f"{path}: must match exactly one schema in oneOf (matched {matches})")
    if "not" in schema and not validate_instance(instance, schema["not"], root, path):
        errors.append(f"{path}: must not match schema in not")
    if "if" in schema:
        condition_matches = not validate_instance(instance, schema["if"], root, path)
        branch = schema.get("then") if condition_matches else schema.get("else")
        if isinstance(branch, dict):
            errors.extend(validate_instance(instance, branch, root, path))
    if "const" in schema and instance != schema["const"]: errors.append(f"{path}: must equal {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]: errors.append(f"{path}: {instance!r} is not in {schema['enum']!r}")
    typ = schema.get("type")
    ok = True
    if typ == "object": ok = isinstance(instance, dict)
    elif typ == "array": ok = isinstance(instance, list)
    elif typ == "string": ok = isinstance(instance, str)
    elif typ == "boolean": ok = isinstance(instance, bool)
    elif typ == "integer": ok = isinstance(instance, int) and not isinstance(instance, bool)
    elif typ == "number": ok = isinstance(instance, (int, float)) and not isinstance(instance, bool)
    if typ and not ok:
        return [f"{path}: expected {typ}, got {_typename(instance)}"]
    if isinstance(instance, dict):
        if len(instance) < schema.get("minProperties", 0):
            errors.append(f"{path}: requires at least {schema['minProperties']} properties")
        for key in schema.get("required", []):
            if key not in instance: errors.append(f"{path}: missing required property {key!r}")
        properties = schema.get("properties", {})
        for key, value in instance.items():
            if key in properties: errors.extend(validate_instance(value, properties[key], root, f"{path}.{key}"))
            elif schema.get("additionalProperties") is False: errors.append(f"{path}: unknown property {key!r}")
            elif isinstance(schema.get("additionalProperties"), dict):
                errors.extend(validate_instance(value, schema["additionalProperties"], root, f"{path}.{key}"))
    if isinstance(instance, list):
        if len(instance) < schema.get("minItems", 0): errors.append(f"{path}: requires at least {schema['minItems']} items")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{path}: requires at most {schema['maxItems']} items")
        if schema.get("uniqueItems"):
            seen = set()
            for value in instance:
                marker = json.dumps(value, sort_keys=True, ensure_ascii=False)
                if marker in seen: errors.append(f"{path}: items must be unique"); break
                seen.add(marker)
        if "items" in schema:
            for i, value in enumerate(instance): errors.extend(validate_instance(value, schema["items"], root, f"{path}[{i}]"))
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]: errors.append(f"{path}: must be >= {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]: errors.append(f"{path}: must be <= {schema['maximum']}")
        if "exclusiveMinimum" in schema and instance <= schema["exclusiveMinimum"]: errors.append(f"{path}: must be > {schema['exclusiveMinimum']}")
    if isinstance(instance, str):
        if len(instance) < schema.get("minLength", 0): errors.append(f"{path}: is shorter than {schema['minLength']} characters")
        if "pattern" in schema and not re.search(schema["pattern"], instance): errors.append(f"{path}: does not match {schema['pattern']!r}")
    return errors


def load_and_validate(instance: Any, schema_path: Path) -> list[str]:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    errors = validate_instance(instance, schema)
    roles = instance.get("roles", {}) if isinstance(instance, dict) else {}
    for role, spec in roles.items() if isinstance(roles, dict) else []:
        ls = spec.get("paragraph", {}).get("line_spacing") if isinstance(spec, dict) else None
        if isinstance(ls, dict):
            typ, unit = ls.get("type"), ls.get("unit")
            if typ in {"exact", "at_least"} and unit != "pt": errors.append(f"$.roles.{role}.paragraph.line_spacing.unit: {typ} requires pt")
            if typ in {"multiple", "single", "one_point_five", "double"} and unit != "multiple": errors.append(f"$.roles.{role}.paragraph.line_spacing.unit: {typ} requires multiple")
        paragraph = spec.get("paragraph", {}) if isinstance(spec, dict) else {}
        if isinstance(paragraph, dict) and ({"space_before_lines", "space_after_lines"} & set(paragraph)):
            # A line-height is needed only to convert a non-zero line count to
            # points.  Zero-line spacing is already an exact, unit-independent
            # value and must remain valid even when the role has no font size.
            line_values = [paragraph.get(key) for key in ("space_before_lines", "space_after_lines") if key in paragraph]
            has_nonzero_lines = any(isinstance(value, (int, float)) and not isinstance(value, bool) and value != 0
                                    for value in line_values)
            if has_nonzero_lines and "spacing_line_height_pt" not in paragraph:
                errors.append(f"$.roles.{role}.paragraph.spacing_line_height_pt: required when spacing is expressed in lines")
        # Inline is a valid semantic position for ordinary text/citation
        # content.  The current executor only lacks inline placement for
        # figure/table captions, so keep the failure closed at that boundary
        # instead of rejecting body_text and other text roles globally.
        if isinstance(spec, dict) and spec.get("position") == "inline" and role in {"figure_caption", "table_caption"}:
            errors.append(f"$.roles.{role}.position: inline placement is not executable by the current backend")
        numbering = spec.get("numbering") if isinstance(spec, dict) else None
        if isinstance(numbering, dict) and "depth" in numbering:
            errors.append(f"$.roles.{role}.numbering.depth: declarative depth is not executable; use explicit per-level formats")
    requirements = instance.get("requirements", []) if isinstance(instance, dict) else []
    seen = set()
    for i, req in enumerate(requirements if isinstance(requirements, list) else []):
        if not isinstance(req, dict): continue
        rid = req.get("id")
        if rid in seen: errors.append(f"$.requirements[{i}].id: duplicate {rid!r}")
        seen.add(rid)
        schema_name = {
            "page": "pageSpec",
            "table": "tableSpec",
            "objects": "objectPaginationSpec",
            "content_constraints": "contentConstraintSpec",
            "conditional_constraints": "conditionalConstraintSpec",
            "document_structure": "documentStructureSpec",
            "appendices": "appendixSpec",
            "equations": "equationLayoutSpec",
            "cover": "coverSpec",
            "declarations": "declarationsSpec",
        }.get(req.get("role"), "roleSpec")
        target = schema["$defs"][schema_name]
        errors.extend(validate_instance(req.get("properties"), target, schema, f"$.requirements[{i}].properties"))
    field_instances = []
    field_instances_key = "content_instances"
    if isinstance(instance, dict):
        if "content_instances" in instance:
            field_instances = instance.get("content_instances", [])
        else:
            field_instances_key = "cover_field_instances"
            field_instances = instance.get("cover_field_instances", [])
    if isinstance(field_instances, list):
        instance_ids: set[str] = set()
        instance_keys: set[tuple[str, str]] = set()
        instance_roles: dict[str, str] = {}
        for i, field_instance in enumerate(field_instances):
            if not isinstance(field_instance, dict):
                continue
            field_id = field_instance.get("id")
            field_key = field_instance.get("field_key")
            role = field_instance.get("role")
            if field_id in instance_ids:
                errors.append(f"$.{field_instances_key}[{i}].id: duplicate {field_id!r}")
            if isinstance(field_id, str):
                instance_ids.add(field_id)
                instance_roles[field_id] = role
            identity = (role, field_key)
            if identity in instance_keys:
                errors.append(f"$.{field_instances_key}[{i}]: duplicate role/field_key {identity!r}")
            instance_keys.add(identity)
        referenced_ids: set[str] = set()
        for i, req in enumerate(requirements if isinstance(requirements, list) else []):
            if not isinstance(req, dict):
                continue
            refs = req.get("field_instance_ids", [])
            if not isinstance(refs, list):
                continue
            for field_id in refs:
                referenced_ids.add(field_id)
                if field_id not in instance_ids:
                    errors.append(f"$.requirements[{i}].field_instance_ids: unknown instance {field_id!r}")
                elif req.get("role") != instance_roles.get(field_id):
                    errors.append(f"$.requirements[{i}].field_instance_ids: role does not match instance {field_id!r}")
        for i, field_instance in enumerate(field_instances):
            if isinstance(field_instance, dict) and field_instance.get("id") not in referenced_ids:
                errors.append(f"$.{field_instances_key}[{i}].id: instance is not referenced by a requirement")
    profile = instance.get("thesis_profile", {}) if isinstance(instance, dict) else {}
    cover = instance.get("cover") if isinstance(instance, dict) else None
    metadata = profile.get("cover_metadata") if isinstance(profile, dict) else None
    # A declared cover is structural.  It may be generated with neutral
    # placeholders when no instance metadata has been supplied.  Once a
    # cover_metadata object is supplied, however, it remains an all-or-nothing
    # trusted record: partial or blank trusted records are rejected below.
    if isinstance(cover, dict):
        metadata_values = metadata if isinstance(metadata, dict) else {}
        field_ids: list[str] = []
        field_orders: list[int] = []
        for i, field in enumerate(cover.get("fields", [])):
            if not isinstance(field, dict):
                continue
            field_id = field.get("id")
            field_ids.append(field_id)
            field_orders.append(field.get("order"))
            if field.get("value_from") != f"thesis_profile.cover_metadata.{field_id}":
                errors.append(f"$.cover.fields[{i}].value_from: must bind to its own field id {field_id!r}")
            value = metadata_values.get(field_id)
            if isinstance(metadata, dict) and field.get("display_policy") == "required" and (value in (None, "", []) or
                    isinstance(value, str) and not value.strip()):
                errors.append(f"$.cover.fields[{i}]: required metadata {field_id!r} is missing")
        if len(field_ids) != len(set(field_ids)):
            errors.append("$.cover.fields: field ids must be unique")
        if len(field_orders) != len(set(field_orders)):
            errors.append("$.cover.fields: field order values must be unique")
    if isinstance(metadata, dict):
        for key, value in metadata.items():
            if key != "trust" and isinstance(value, str) and not value.strip():
                errors.append(f"$.thesis_profile.cover_metadata.{key}: must not be blank")
        if profile.get("student_id") and profile.get("student_id") != metadata.get("student_id"):
            errors.append("$.thesis_profile.student_id: must equal cover_metadata.student_id")
        if profile.get("completion_date") and profile.get("completion_date") != metadata.get("completion_date"):
            errors.append("$.thesis_profile.completion_date: must equal cover_metadata.completion_date")
        count = profile.get("co_supervisor_count")
        if count is not None and count != len(metadata.get("co_supervisors", [])):
            errors.append("$.thesis_profile.co_supervisor_count: must equal cover_metadata.co_supervisors length")
        if profile.get("security_level") == "public" and (
                metadata.get("security_marking") or metadata.get("embargo_start")
                or metadata.get("embargo_until")):
            errors.append("$.thesis_profile.cover_metadata: public theses must not request a security/embargo marking")
        if profile.get("security_level") in {"restricted", "classified"} and not metadata.get("administrative_verification"):
            errors.append("$.thesis_profile.cover_metadata.administrative_verification: required for restricted/classified metadata")
        if (metadata.get("embargo_start") and metadata.get("embargo_until")
                and str(metadata["embargo_start"]) > str(metadata["embargo_until"])):
            errors.append("$.thesis_profile.cover_metadata: embargo_start must not be after embargo_until")
    declarations = instance.get("declarations") if isinstance(instance, dict) else None
    if isinstance(declarations, dict):
        items = [item for item in declarations.get("items", []) if isinstance(item, dict)]
        ids = [item.get("id") for item in items]
        if not items:
            errors.append("$.declarations.items: must contain at least one declaration resource")
        if any(not isinstance(item_id, str) or not item_id.strip() for item_id in ids):
            errors.append("$.declarations.items.id: must be non-empty semantic identifiers")
        if len(ids) != len(set(ids)):
            errors.append("$.declarations.items.id: identifiers must be unique")

        registry = instance.get("resource_registry")
        if not isinstance(registry, dict):
            errors.append("$.resource_registry: required when declarations are present")
            registry_items: dict[str, Any] = {}
        else:
            registry_items = registry.get("items", {}) if isinstance(registry.get("items"), dict) else {}
            if isinstance(instance.get("run_id"), str) and registry.get("run_id") != instance.get("run_id"):
                errors.append("$.resource_registry.run_id: must equal $.run_id")
            for resource_id, resource in registry_items.items():
                if not isinstance(resource, dict):
                    continue
                if resource.get("id") != resource_id:
                    errors.append(f"$.resource_registry.items.{resource_id}.id: must equal its registry key")
                heading = resource.get("heading")
                body_parts = resource.get("body_parts")
                if isinstance(heading, str) and isinstance(body_parts, list) and all(isinstance(part, str) for part in body_parts):
                    expected_hash = fixed_text_sha256("\n".join([heading, *body_parts]))
                    if resource.get("sha256") != expected_hash:
                        errors.append(f"$.resource_registry.items.{resource_id}.sha256: does not match fixed resource text")
        for i, item in enumerate(items):
            item_id = item.get("id")
            resource_id = item.get("resource_id")
            resource = registry_items.get(resource_id) if isinstance(resource_id, str) else None
            if not isinstance(resource, dict):
                errors.append(f"$.declarations.items[{i}].resource_id: not found in current run resource registry")
                continue
            for field in ("version", "sha256"):
                if item.get(field) != resource.get(field):
                    errors.append(f"$.declarations.items[{i}].{field}: does not match bound resource")
            if resource.get("kind") != "fixed_text":
                errors.append(f"$.declarations.items[{i}].resource_id: resource kind must be fixed_text")
            placeholders = item.get("signature_placeholders", [])
            if isinstance(placeholders, list):
                for placeholder_index, placeholder in enumerate(placeholders):
                    if not isinstance(placeholder, dict):
                        continue
                    label = str(placeholder.get("label", ""))
                    # The label is input-derived, but it must remain a neutral
                    # blank field.  Actual signing is an external/manual fact;
                    # no school-specific label whitelist belongs here.
                    if re.search(r"已签名|已签署|实际签名|亲笔签名|签字确认", label):
                        errors.append(
                            f"$.declarations.items[{i}].signature_placeholders[{placeholder_index}].label: "
                            "must use the canonical blank placeholder set and must not claim an actual signature"
                        )
    errors.extend(validate_clause_contract(instance))
    # JSON Schema can prove that a field id and value_from are well-typed,
    # but it cannot prove that a model mapped an administrative label to the
    # semantically correct trusted field.  Keep these guards in the canonical
    # loader so CLI and library callers share the same fail-closed boundary.
    errors.extend(cover_binding_errors(instance if isinstance(instance, dict) else {}))
    errors.extend(input_prerequisite_errors(instance if isinstance(instance, dict) else {}))
    errors.extend(verification_checker_errors(instance if isinstance(instance, dict) else {}))
    return errors


def validate_clause_contract(instance: Any) -> list[str]:
    """Validate cross-object traceability that JSON Schema cannot express."""
    if not isinstance(instance, dict):
        return []
    records = instance.get("clause_compliance")
    # Legacy/rule-only specs remain readable, but full mode is deliberately
    # strict and cannot be hand-authored with an empty compliance partition.
    if records is None:
        return ["$.clause_compliance: required when compliance_mode is full"] if instance.get("compliance_mode") == "full" else []
    if not isinstance(records, list):
        return []
    errors: list[str] = []
    reqs = instance.get("requirements", []) if isinstance(instance.get("requirements"), list) else []
    req_map = {r.get("id"): r for r in reqs if isinstance(r, dict) and r.get("id")}
    record_map: dict[str, dict[str, Any]] = {}
    for i, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        cid = record.get("clause_id")
        if cid in record_map:
            errors.append(f"$.clause_compliance[{i}].clause_id: duplicate {cid!r}")
        if cid:
            record_map[cid] = record
        state = record.get("status")
        rids = record.get("requirement_ids") or []
        if state == "pending_execution" and not rids:
            errors.append(f"$.clause_compliance[{i}].requirement_ids: pending_execution requires at least one requirement")
        if state != "pending_execution" and state not in {"generated_and_verified", "verified_existing"} and rids:
            errors.append(f"$.clause_compliance[{i}].requirement_ids: state {state!r} must not reference executable requirements")
        for rid in rids:
            req = req_map.get(rid)
            if req is None:
                errors.append(f"$.clause_compliance[{i}].requirement_ids: unknown requirement {rid!r}")
            elif cid not in (req.get("clause_ids") or []):
                errors.append(f"$.clause_compliance[{i}]: requirement {rid!r} does not cite clause {cid!r}")
    for i, req in enumerate(reqs):
        if not isinstance(req, dict):
            continue
        rid = req.get("id")
        clause_ids = req.get("clause_ids") or []
        if instance.get("analysis_mode") == "llm_primary" and not clause_ids:
            errors.append(f"$.requirements[{i}].clause_ids: required for llm_primary traceability")
        for cid in clause_ids:
            record = record_map.get(cid)
            if record is None:
                errors.append(f"$.requirements[{i}].clause_ids: no compliance record for {cid!r}")
            elif record.get("status") == "external_compliance":
                # A fixed-text requirement may cite the companion signature,
                # date, or other real-world duty as evidence for a DOCX
                # placeholder.  The external record intentionally has no
                # executable requirement_id; the actual duty must remain
                # outside the artifact's execution scope.
                continue
            elif rid not in (record.get("requirement_ids") or []):
                errors.append(f"$.requirements[{i}]: clause {cid!r} does not reference requirement {rid!r}")
        role = req.get("role")
        top_level = {"table": "tables", "objects": "objects", "content_constraints": "content_constraints",
                     "conditional_constraints": "conditional_constraints", "document_structure": "document_structure",
                     "appendices": "appendices", "equations": "equations", "cover": "cover",
                     "declarations": "declarations"}.get(role)
        if top_level and top_level not in instance:
            errors.append(f"$.requirements[{i}]: role {role!r} requires top-level {top_level!r} properties")
    return errors
