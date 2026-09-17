#!/usr/bin/env python3
"""Validate and deeply merge the V2 YAML defaults with a school overlay."""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

import yaml


def deep_merge(base: Any, overlay: Any) -> Any:
    # YAML null is an explicit "use the default" value.  Treating it as a
    # replacement would erase nested defaults and make a small overlay
    # surprisingly incomplete for downstream consumers.
    if overlay is None:
        return copy.deepcopy(base)
    if isinstance(base, dict) and isinstance(overlay, dict):
        result = copy.deepcopy(base)
        for key, value in overlay.items():
            result[key] = deep_merge(result[key], value) if key in result else copy.deepcopy(value)
        return result
    # Lists are atomic configuration values: an overlay replaces the list.
    return copy.deepcopy(overlay)


def unknown_paths(base: Any, overlay: Any, prefix: str = "") -> list[str]:
    errors = []
    if not isinstance(overlay, dict):
        return errors
    if prefix.rstrip('.') == 'page.size' and isinstance(base, str):
        return [f'page.size.{key}' for key in sorted(set(overlay) - {'width', 'height'})]
    if not isinstance(base, dict):
        return [prefix.rstrip(".")]
    for key, value in overlay.items():
        path = f"{prefix}{key}"
        if key not in base:
            errors.append(path)
        elif isinstance(value, dict):
            errors.extend(unknown_paths(base[key], value, path + "."))
    return errors


def type_errors(base: Any, overlay: Any, prefix: str = "") -> list[str]:
    errors = []
    if overlay is None:
        return errors
    path = prefix.rstrip('.')
    # ``page.size`` is the one schema scalar that deliberately also accepts a
    # custom dimension mapping. Keep this exception explicit instead of
    # weakening type checking for every scalar in the configuration.
    if path == 'page.size' and isinstance(base, str) and isinstance(overlay, dict):
        unknown = sorted(set(overlay) - {'width', 'height'})
        errors.extend(f'page.size.{key}: unknown field' for key in unknown)
        for key in ('width', 'height'):
            if key in overlay and (isinstance(overlay[key], bool) or
                                   not isinstance(overlay[key], int)):
                errors.append(
                    f'page.size.{key}: expected integer twips, got {type(overlay[key]).__name__}'
                )
        return errors
    if isinstance(base, dict):
        if not isinstance(overlay, dict): return [f"{path}: expected mapping, got {type(overlay).__name__}"]
        for key, value in overlay.items():
            if key in base: errors.extend(type_errors(base[key], value, f"{prefix}{key}."))
    elif isinstance(base, list):
        if not isinstance(overlay, list): errors.append(f"{path}: expected list, got {type(overlay).__name__}")
    elif base is not None and not isinstance(overlay, type(base)):
        numeric_pair = isinstance(base, (int, float)) and not isinstance(base, bool) and \
            isinstance(overlay, (int, float)) and not isinstance(overlay, bool)
        if not numeric_pair:
            errors.append(f"{path}: expected {type(base).__name__}, got {type(overlay).__name__}")
    return errors


def load_resolved_config(schema_path: Path, overlay_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load, validate, and resolve one configuration for every pipeline stage."""
    base = load_yaml(schema_path)
    overlay_raw = load_yaml(overlay_path)
    overlay, migration_warnings = migrate_overlay(overlay_raw)
    unknown = unknown_paths(base, overlay)
    shape_errors = type_errors(base, overlay)
    effective = deep_merge(base, overlay)
    errors, warnings = semantic_validate(effective) if not shape_errors else (shape_errors, [])
    warnings = migration_warnings + warnings
    return effective, {
        "errors": errors,
        "warnings": warnings,
        "unknown_fields": unknown,
    }


def migrate_overlay(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    out = copy.deepcopy(raw); warnings = []
    toc = out.get("toc")
    if isinstance(toc, dict):
        for key in ("list_of_figures", "list_of_tables"):
            if key in toc and key not in out:
                out[key] = toc.pop(key); warnings.append(f"migrated toc.{key} to root {key}")
    for kind in ("figure", "table"):
        node = out.get("figures_tables", {}).get(kind, {}) if isinstance(out.get("figures_tables"), dict) else {}
        if node.get("numbering") == "chapter_seq":
            node["numbering"] = "chapter-seq"; warnings.append(f"normalized figures_tables.{kind}.numbering")
    return out, warnings


def semantic_validate(cfg: dict[str, Any]) -> tuple[list[str], list[str]]:
    errors: list[str] = []; warnings: list[str] = []
    metadata = cfg.get('metadata') or {}
    if metadata.get('degree_level', '') not in {'', 'doctor', 'master', 'professional_master'}:
        errors.append('metadata.degree_level must be doctor, master, professional_master, or empty')

    page = cfg.get('page') or {}
    size = page.get('size')
    if isinstance(size, dict):
        for key in ('width', 'height'):
            value = size.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                errors.append(f'page.size.{key} must be a positive integer twip value')
    margins = page.get('margins') or {}
    for key in ('top', 'bottom', 'left', 'right', 'header_distance', 'footer_distance', 'gutter'):
        value = margins.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            errors.append(f'page.margins.{key} must be a non-negative number')

    for key in ("cn_keywords", "en_keywords"):
        node = cfg.get(key, {})
        minimum, maximum = node.get("min_count", 0), node.get("max_count", 10**9)
        if isinstance(minimum, (int, float)) and minimum < 0:
            errors.append(f"{key}.min_count must be non-negative")
        if isinstance(maximum, (int, float)) and maximum < 0:
            errors.append(f"{key}.max_count must be non-negative")
        if minimum > maximum: errors.append(f"{key}.min_count exceeds max_count")
        if not isinstance(node.get('label'), str) or not isinstance(node.get('delimiter'), str):
            errors.append(f"{key}.label and delimiter must be strings")

    toc = cfg.get("toc", {}) or {}
    sections = cfg.get("sections", {}) or {}
    depth = toc.get("depth", 0)
    numbering_depth = sections.get("numbering_depth", 9)
    if not isinstance(depth, int) or isinstance(depth, bool) or not 1 <= depth <= 3:
        errors.append('toc.depth must be an integer from 1 to 3')
    if not isinstance(numbering_depth, int) or isinstance(numbering_depth, bool) or numbering_depth < 1:
        errors.append('sections.numbering_depth must be a positive integer')
    if isinstance(depth, int) and isinstance(numbering_depth, int) and depth > numbering_depth:
        errors.append("toc.depth exceeds sections.numbering_depth")
    if size not in {"A4", "16K", "16k", "B5"} and not isinstance(size, dict):
        warnings.append("page.size is custom; ensure the backend supports the declared dimensions")

    equations = cfg.get('equations') or {}
    for key in ('spacing_before', 'spacing_after'):
        value = equations.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            errors.append(f'equations.{key} must be a non-negative number')
    label_format = equations.get('label_format')
    if not isinstance(label_format, str) or '{chapter}' not in label_format or '{seq}' not in label_format:
        errors.append('equations.label_format must contain {chapter} and {seq}')

    typography = cfg.get('typography') or {}
    for field in ('body_line_spacing', 'heading_line_spacing', 'footnote_line_spacing'):
        spacing = typography.get(field) or {}
        value = spacing.get('value')
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            errors.append(f'typography.{field}.value must be positive')
        if spacing.get('unit') not in {'pt'}:
            errors.append(f'typography.{field}.unit must be pt')
    for field, value in (cfg.get('font_sizes') or {}).items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            errors.append(f'font_sizes.{field} must be positive')

    for section_name in ('cover', 'title_page'):
        node = cfg.get(section_name) or {}
        rows = node.get('info_fields')
        if rows is not None:
            if not isinstance(rows, list):
                errors.append(f'{section_name}.info_fields must be a list')
            else:
                for index, row in enumerate(rows):
                    if (not isinstance(row, dict) or
                            not isinstance(row.get('label'), str) or
                            not isinstance(row.get('value'), str)):
                        errors.append(f'{section_name}.info_fields[{index}] requires string label and value')
        if section_name == 'cover' and not isinstance(node.get('top_lines'), list):
            errors.append('cover.top_lines must be a list')

    valid_line_rules = {"exact", "atLeast", "auto"}
    for key in ("body_line_rule", "heading_line_rule", "footnote_line_rule"):
        val = cfg.get("typography", {}).get(key)
        if val not in valid_line_rules: errors.append(f"typography.{key} has unsupported value {val!r}")
    return errors, warnings


def load_yaml(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict): raise ValueError(f"{path} must contain a YAML mapping")
    return raw


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("schema", type=Path); p.add_argument("overlay", type=Path); p.add_argument("output", type=Path)
    p.add_argument("--report", type=Path); p.add_argument("--strict", action="store_true")
    args = p.parse_args(argv)
    try:
        effective, diagnostics = load_resolved_config(args.schema, args.overlay)
    except (OSError, UnicodeDecodeError, yaml.YAMLError, ValueError) as exc:
        report = {
            "valid": False, "errors": [str(exc)], "warnings": [],
            "unknown_fields": [], "schema": str(args.schema),
            "overlay": str(args.overlay), "output": str(args.output),
        }
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))
        return 1
    unknown = diagnostics["unknown_fields"]
    errors = diagnostics["errors"]
    warnings = diagnostics["warnings"]
    if unknown:
        (errors if args.strict else warnings).extend(f"unknown field: {x}" for x in unknown)
    report = {"valid": not errors, "errors": errors, "warnings": warnings, "unknown_fields": unknown,
              "schema": str(args.schema), "overlay": str(args.overlay), "output": str(args.output)}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not errors:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(yaml.safe_dump(effective, allow_unicode=True, sort_keys=False, width=120), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
