"""Load a human-reviewed source-scope inventory with immutable run binding."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from format_spec_validation import load_and_validate
from semantic_contract import strict_json_loads


BINDING_FIELDS = ("run_id", "case_id", "source_sha256", "format_spec_sha256")


def load_bound_scope_inventory(
    path: Path,
    *,
    expected_binding: dict[str, Any],
    schema_path: Path,
    expected_file_record: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate schema, exact source/run identity, and an optional parent hash record."""
    resolved = path.resolve()
    try:
        raw = resolved.read_bytes()
        inventory = strict_json_loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"cannot read obligation scope inventory: {exc}") from exc
    if not isinstance(inventory, dict):
        raise ValueError("obligation scope inventory must be a JSON object")
    errors = load_and_validate(inventory, schema_path)
    if errors:
        raise ValueError("invalid obligation scope inventory schema: " + "; ".join(errors))
    declared = inventory.get("binding")
    if not isinstance(declared, dict):
        raise ValueError("obligation scope inventory binding is missing")
    for key in BINDING_FIELDS:
        expected = expected_binding.get(key)
        if not isinstance(expected, str) or not expected:
            raise ValueError("current scorecard binding is incomplete: " + key)
        if declared.get(key) != expected:
            raise ValueError("obligation scope inventory binding mismatch: " + key)
    record = {
        "path": str(resolved),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    if expected_file_record is not None:
        for key in ("path", "bytes", "sha256"):
            if expected_file_record.get(key) != record[key]:
                raise ValueError("obligation scope inventory file record mismatch: " + key)
    return inventory, record
