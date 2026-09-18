#!/usr/bin/env python3
"""Run-scoped, content-addressed resources for executable fixed text.

The formatting engine must never decide which institution a declaration belongs
to.  A semantic review supplies the exact source-derived text for each item;
this module gives that text a fresh run binding and records the bytes that the
DOCX executor is allowed to write.
"""
from __future__ import annotations

import copy
import hashlib
import re
import unicodedata
from typing import Any


SCHEMA_VERSION = "1.0"


def _normalized(text: str) -> str:
    """Normalize presentation-only whitespace without erasing structure.

    A fixed-text digest must distinguish paragraph boundaries and meaningful
    word spacing.  The previous implementation removed every whitespace
    character, making ``"研究 成果"`` collide with ``"研究成果"`` and
    collapsing two source paragraphs into one digest.
    """
    value = unicodedata.normalize("NFKC", str(text))
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[^\S\r\n]+", " ", value)
    value = re.sub(r" *\n *", "\n", value)
    return value.strip()


def fixed_text_sha256(text: str) -> str:
    return hashlib.sha256(_normalized(text).encode("utf-8")).hexdigest()


def _body_parts(item: dict[str, Any]) -> list[str]:
    parts = item.get("body_parts")
    if isinstance(parts, list):
        result = [part.strip() for part in parts if isinstance(part, str) and part.strip()]
        if result:
            return result
    body = item.get("body")
    if isinstance(body, str) and body.strip():
        return [body.strip()]
    return []


def _resource_digest(run_id: str, item_id: str, heading: str, body_parts: list[str]) -> str:
    material = "\0".join([run_id, item_id, heading, *body_parts])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _resource_sha256(heading: str, body_parts: list[str]) -> str:
    # The digest covers the complete fixed resource, while the executor audits
    # each body paragraph separately after serialization.
    return fixed_text_sha256("\n".join([heading, *body_parts]))


def _validate_existing_registry(spec: dict[str, Any], run_id: str) -> None:
    """Re-validate an already materialized registry before reusing it.

    Same-run idempotency is useful, but returning an unchecked registry would
    let a partially written or hand-edited artifact pass through the boundary.
    This check deliberately validates only local binding integrity; source
    evidence membership is checked by the format-spec validator.
    """
    registry = spec.get("resource_registry")
    if not isinstance(registry, dict):
        raise ValueError("existing resource registry must be an object")
    if registry.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("existing resource registry has an unsupported schema_version")
    if registry.get("run_id") != run_id:
        raise ValueError("resource registry run_id does not match the current extraction run")
    registry_items = registry.get("items")
    if not isinstance(registry_items, dict) or not registry_items:
        raise ValueError("existing resource registry must contain resource items")

    declarations = spec.get("declarations")
    items = declarations.get("items") if isinstance(declarations, dict) else None
    if not isinstance(items, list) or not items:
        raise ValueError("materialized declarations must contain declaration items")
    seen_ids: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("each materialized declaration item must be an object")
        item_id = item.get("id")
        resource_id = item.get("resource_id")
        if not isinstance(item_id, str) or not item_id.strip():
            raise ValueError("materialized declaration item needs a non-empty semantic id")
        if item_id in seen_ids:
            raise ValueError(f"duplicate materialized declaration id: {item_id}")
        if not isinstance(resource_id, str) or not resource_id.strip():
            raise ValueError(f"declaration {item_id!r} has no bound resource_id")
        resource = registry_items.get(resource_id)
        if not isinstance(resource, dict):
            raise ValueError(f"declaration {item_id!r} points to a missing resource")
        if resource.get("id") != resource_id or resource.get("kind") != "fixed_text":
            raise ValueError(f"declaration {item_id!r} points to an invalid fixed-text resource")
        heading = resource.get("heading")
        body_parts = resource.get("body_parts")
        if not isinstance(heading, str) or not heading.strip():
            raise ValueError(f"resource {resource_id!r} has no heading")
        if (not isinstance(body_parts, list)
                or not body_parts
                or any(not isinstance(part, str) or not part.strip() for part in body_parts)):
            raise ValueError(f"resource {resource_id!r} has invalid body_parts")
        if resource.get("sha256") != _resource_sha256(heading.strip(), [part.strip() for part in body_parts]):
            raise ValueError(f"resource {resource_id!r} sha256 does not match its fixed text")
        if item.get("version") != resource.get("version") or item.get("sha256") != resource.get("sha256"):
            raise ValueError(f"declaration {item_id!r} binding does not match its resource")
        seen_ids.add(item_id)


def materialize_declaration_resources(spec: dict[str, Any], run_id: str) -> dict[str, Any]:
    """Bind source-derived declaration text to a new execution run.

    LLM output may contain ``heading`` and ``body_parts`` but must not invent a
    persistent resource identifier or version.  Those fields are generated
    here from the current run id and exact text.  A previously materialized
    spec is accepted only when its registry belongs to this same run.
    """
    declarations = spec.get("declarations")
    if not isinstance(declarations, dict):
        return spec

    existing_registry = spec.get("resource_registry")
    if isinstance(existing_registry, dict):
        _validate_existing_registry(spec, run_id)
        return spec

    raw_items = declarations.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("declarations.items must contain source-derived resources")

    registry: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "items": {},
    }
    materialized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise ValueError("each declaration item must be an object")
        item_id = raw.get("id")
        heading = raw.get("heading")
        body_parts = _body_parts(raw)
        if not isinstance(item_id, str) or not item_id.strip():
            raise ValueError("each declaration item needs a non-empty semantic id")
        if item_id in seen_ids:
            raise ValueError(f"duplicate declaration id: {item_id}")
        if not isinstance(heading, str) or not heading.strip():
            raise ValueError(f"declaration {item_id!r} needs exact source-derived heading text")
        if not body_parts:
            raise ValueError(f"declaration {item_id!r} needs exact source-derived body text")
        source_evidence_ids = sorted({
            str(value) for value in (raw.get("source_evidence_ids") or [])
            if isinstance(value, str) and value.strip()
        })
        if not source_evidence_ids:
            raise ValueError(f"declaration {item_id!r} needs source_evidence_ids")
        seen_ids.add(item_id)

        digest = _resource_digest(run_id, item_id, heading.strip(), body_parts)
        resource_id = f"runtime_{digest[:24]}"
        version = f"run-{run_id}"
        resource = {
            "id": resource_id,
            "kind": "fixed_text",
            "version": version,
            "heading": heading.strip(),
            "body_parts": body_parts,
            "sha256": _resource_sha256(heading.strip(), body_parts),
            "source_evidence_ids": source_evidence_ids,
        }
        registry["items"][resource_id] = resource
        materialized.append({
            "id": item_id,
            "resource_id": resource_id,
            "version": version,
            "sha256": resource["sha256"],
            "signature_placeholders": copy.deepcopy(raw.get("signature_placeholders") or []),
        })

    spec["run_id"] = run_id
    spec["resource_registry"] = registry
    spec["declarations"] = {
        "before_role": declarations.get("before_role", "abstract_title_zh"),
        "items": materialized,
    }
    return spec


def resource_items(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    registry = spec.get("resource_registry") if isinstance(spec, dict) else None
    items = registry.get("items") if isinstance(registry, dict) else None
    return items if isinstance(items, dict) else {}
