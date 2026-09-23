#!/usr/bin/env python3
"""Shared semantic-role and Word-style registry.

All Python stages use this module so canonical names, aliases, and analyzer
patterns cannot silently drift apart.  The JSON file is the auditable source of
truth; this module only supplies normalized lookup helpers.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    from .semantic_contract import strict_json_read
except ImportError:  # direct script/module execution
    from semantic_contract import strict_json_read

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "schema" / "role-registry.json"


@lru_cache(maxsize=None)
def load_registry(path: str | Path = DEFAULT_REGISTRY) -> dict[str, Any]:
    registry_path = Path(path)
    data = strict_json_read(registry_path)
    if data.get("schema_version") != "1.0" or not isinstance(data.get("roles"), dict):
        raise ValueError(f"invalid role registry: {registry_path}")
    return data


def roles() -> dict[str, dict[str, Any]]:
    return load_registry()["roles"]


def role_names() -> list[str]:
    return list(roles())


def role_config(role: str) -> dict[str, Any]:
    return roles().get(role, {})


def canonical_style(role: str) -> str | None:
    value = role_config(role).get("canonical_style")
    return value if isinstance(value, str) and value else None


def generated_style(role: str) -> str | None:
    """Return the school-neutral style used for newly generated content."""
    value = role_config(role).get("generated_style")
    return value if isinstance(value, str) and value else canonical_style(role)


def style_aliases(role: str, *, include_canonical: bool = True) -> list[str]:
    config = role_config(role)
    values: list[str] = []
    if include_canonical and canonical_style(role):
        values.append(canonical_style(role) or "")
    aliases = config.get("style_aliases", [])
    if isinstance(aliases, list):
        values.extend(value for value in aliases if isinstance(value, str) and value)
    return list(dict.fromkeys(values))


def style_patterns(role: str) -> list[tuple[str, int]]:
    result: list[tuple[str, int]] = []
    for item in role_config(role).get("style_patterns", []):
        if isinstance(item, list) and len(item) == 2 and isinstance(item[0], str):
            result.append((item[0], int(item[1])))
    return result


def structural_detector(role: str) -> str | None:
    value = role_config(role).get("structural_detector")
    return value if isinstance(value, str) and value else None


def normalize_style_name(name: str | None) -> str:
    """Normalize display names and style IDs for alias comparison."""
    return re.sub(r"[\s_\-]+", "", name or "").casefold()


def style_matches(role: str, style_id: str | None = None, style_name: str | None = None) -> bool:
    candidates = {normalize_style_name(style_id), normalize_style_name(style_name)} - {""}
    aliases = {normalize_style_name(value) for value in style_aliases(role)}
    return bool(candidates & aliases)


def find_existing_style(role: str, available: list[str] | set[str]) -> str | None:
    by_normalized = {normalize_style_name(name): name for name in available}
    for alias in style_aliases(role):
        existing = by_normalized.get(normalize_style_name(alias))
        if existing:
            return existing
    return None
