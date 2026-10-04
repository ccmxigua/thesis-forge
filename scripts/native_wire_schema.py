"""Versioned, lossless sharing of repeated provider-schema subtrees."""
from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from typing import Any

from host_review_schema import native_output_schema

POLICY = "exact_subtree_refs_v1"
POLICY_KEY = "native_wire_schema_policy"


def validate_policy(policy: str | None) -> None:
    if policy not in (None, POLICY):
        raise ValueError("unknown native wire schema policy")


def _encoded(node: Any) -> str:
    return json.dumps(node, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _children(node: dict[str, Any]):
    # Visit schema positions only, never JSON data in enum/const/annotations.
    for key in ("properties", "$defs", "definitions", "patternProperties", "dependentSchemas", "dependencies"):
        if isinstance(node.get(key), dict):
            yield from (value for value in node[key].values() if isinstance(value, dict))
    for key in ("items", "additionalItems", "additionalProperties", "not", "if", "then", "else",
                "contains", "propertyNames", "unevaluatedItems", "unevaluatedProperties", "contentSchema"):
        if isinstance(node.get(key), dict):
            yield node[key]
    for key in ("anyOf", "allOf", "oneOf", "prefixItems", "items"):
        if isinstance(node.get(key), list):
            yield from (value for value in node[key] if isinstance(value, dict))


def _nodes(node: dict[str, Any]):
    yield node
    for child in _children(node):
        yield from _nodes(child)


def expand_shared_schema(schema: dict[str, Any], added_names: set[str]) -> dict[str, Any]:
    """Restore only definitions introduced by this projection, not old refs."""
    result = copy.deepcopy(schema)
    definitions = result.get("$defs", {})

    def expand(node: dict[str, Any], chain: frozenset[str] = frozenset()) -> None:
        ref = node.get("$ref")
        name = ref.removeprefix("#/$defs/") if isinstance(ref, str) else None
        if isinstance(ref, str) and ref.startswith("#/$defs/") and name in added_names:
            if set(node) != {"$ref"} or name in chain or name not in definitions:
                raise ValueError("native shared schema is not reversibly expandable")
            replacement = copy.deepcopy(definitions[name])
            expand(replacement, chain | {name})
            node.clear()
            node.update(replacement)
            return
        for child in _children(node):
            expand(child, chain)

    for name in added_names:
        if name not in definitions:
            raise ValueError("native shared schema definition is missing")
    # Keep the saved definitions available to the expander, without walking
    # the introduced definition roots as if they were original schema nodes.
    result["$defs"] = {name: value for name, value in definitions.items() if name not in added_names}
    expand(result)
    return result


def compact_native_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Factor identical complete schemas, including descriptions and enums.

    Greedy steps reduce serialized size; every inserted reference is absolute
    within this same root. A reverse expansion must recover the exact input.
    Neither constraints nor provider guidance may be discarded or normalized.
    """
    result = copy.deepcopy(schema)
    original_definitions = set(schema.get("$defs", {}))
    for node in _nodes(result):
        if any(key in node for key in ("$id", "$anchor", "$dynamicAnchor", "$dynamicRef")):
            raise ValueError("native schema sharing does not support scoped identifiers")
        ref = node.get("$ref")
        if ref is not None and (not isinstance(ref, str) or not ref.startswith("#/$defs/")
                                or ref[len("#/$defs/"):] not in original_definitions):
            raise ValueError("native schema sharing requires bound root definition references")
        if node is not result and "$defs" in node:
            raise ValueError("native schema sharing does not relocate nested definitions")
    added: set[str] = set()
    while True:
        nodes = list(_nodes(result))[1:]  # Native root stays an object.
        encoded = [_encoded(node) for node in nodes]
        counts = Counter(encoded)
        choices = []
        for value, count in counts.items():
            size = len(value.encode("utf-8"))
            if count < 2 or size < 64:
                continue
            name = "shared_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
            while name in result.get("$defs", {}):
                name += "_"
            ref = {"$ref": "#/$defs/" + name}
            saving = (count - 1) * size - count * len(_encoded(ref).encode("utf-8")) - len(name) - 8
            if saving > 0:
                choices.append((saving, value, name, ref))
        if not choices:
            break
        _, value, name, ref = max(choices, key=lambda item: (item[0], item[1]))
        body = copy.deepcopy(nodes[encoded.index(value)])

        def replace(node: dict[str, Any]) -> None:
            if node is not result and _encoded(node) == value:
                node.clear()
                node.update(ref)
                return
            for child in list(_children(node)):
                replace(child)

        replace(result)
        result.setdefault("$defs", {})[name] = body
        added.add(name)
    restored = expand_shared_schema(result, added)
    if "$defs" not in schema:
        restored.pop("$defs", None)
    if restored != schema:
        raise ValueError("native schema sharing changed the provider schema")
    # The root $defs wrapper itself can exceed the saving on a tiny schema.
    return result if len(_encoded(result)) < len(_encoded(schema)) else copy.deepcopy(schema)


def review_wire_schema(schema: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    policy = request.get(POLICY_KEY)
    validate_policy(policy)
    projected = native_output_schema(schema)
    return compact_native_schema(projected) if policy == POLICY else projected
