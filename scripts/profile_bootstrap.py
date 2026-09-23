#!/usr/bin/env python3
"""Build an auditable official-template profile from bounded DOCX candidates.

The bootstrap is deliberately mechanical.  Semantic role choices must arrive as
candidate indexes from a separately recorded adjudication.  The script rejects
invented, duplicate, out-of-order, or non-unique candidates before emitting a
profile and immediately compiles the result against the official DOCX.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from format_spec_validation import load_and_validate
from template_profile import compile_profile
from semantic_contract import strict_json_read

ROOT = Path(__file__).resolve().parents[1]
DYNAMIC_ROLES = ("body", "acknowledgments", "references", "appendices", "academic_outputs")


def _read(path: Path) -> Any:
    return strict_json_read(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _selector(candidate: dict[str, Any], *, starts_with: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "kind": "paragraph",
        "text": candidate["text"],
        "match": "starts_with" if starts_with else "exact",
        "body_child_index": candidate["body_child_index"],
        "section_index": candidate["section_index"],
    }
    if candidate.get("style_id") is not None:
        result["style_id"] = candidate["style_id"]
    return result


def _audit_selector(candidate: dict[str, Any], *, starts_with: bool = False) -> dict[str, Any]:
    """Return only constraints stable after range replacement and Word re-save."""
    result: dict[str, Any] = {
        "kind": "paragraph",
        "text": candidate["text"],
        "match": "starts_with" if starts_with else "exact",
    }
    if candidate.get("style_id") is not None:
        result["style_id"] = candidate["style_id"]
    return result


def build_profile(*, official_docx: Path, candidates_path: Path, role_selection_path: Path,
                  required_selection_path: Path, output_dir: Path, profile_id: str,
                  title: str, organization: str, source_url: str,
                  effective_version: str, published_date: str | None = None) -> dict[str, Any]:
    packet = _read(candidates_path)
    selection = _read(role_selection_path)
    required_selection = _read(required_selection_path)
    candidates = {int(item["candidate_index"]): item for item in packet["candidates"]}
    role_order = selection["role_order"]
    indexes = selection["candidate_indexes"]
    excluded_output_roles = set(selection.get("excluded_output_roles", []))
    if len(role_order) != len(indexes) or len(set(role_order)) != len(role_order):
        raise ValueError("role selection must contain one candidate for every unique role")
    unknown_excluded = sorted(excluded_output_roles - set(role_order))
    if unknown_excluded:
        raise ValueError(f"excluded output roles are not selected roles: {unknown_excluded}")
    dynamic_excluded = sorted(excluded_output_roles & set(DYNAMIC_ROLES))
    if dynamic_excluded:
        raise ValueError(f"dynamic assembly roles cannot be excluded from output: {dynamic_excluded}")
    if len(set(indexes)) != len(indexes):
        raise ValueError("role selection reuses a candidate index")
    unknown = sorted(set(indexes) - set(candidates))
    if unknown:
        raise ValueError(f"role selection invented candidate indexes: {unknown}")
    selected = {role: candidates[index] for role, index in zip(role_order, indexes)}
    positions = [selected[role]["body_child_index"] for role in role_order]
    if positions != sorted(positions):
        raise ValueError("selected structural roles are not in official-template order")
    if tuple(role for role in role_order if role in DYNAMIC_ROLES) != DYNAMIC_ROLES:
        raise ValueError("dynamic roles are missing or not in assembly order")

    source_required = {item["role"]: bool(item["source_required"])
                       for item in required_selection["regions"]}
    if set(source_required) != set(DYNAMIC_ROLES):
        raise ValueError("source-required decision must cover exactly the dynamic roles")

    output_dir.mkdir(parents=True, exist_ok=True)
    resource_dir = output_dir / "resources"
    resource_dir.mkdir(exist_ok=True)
    copied_template = resource_dir / "official-template.docx"
    shutil.copyfile(official_docx, copied_template)

    ordered_roles = []
    for role in role_order:
        if role in excluded_output_roles:
            continue
        candidate = selected[role]
        optional = role in DYNAMIC_ROLES and not source_required[role]
        ordered_role = {
            "role": role,
            "required": not optional,
            "must_start_new_page": True,
            "min_occurrences": 0 if optional else 1,
            "max_occurrences": 1,
            "selector": _audit_selector(candidate, starts_with=(role == "appendices")),
        }
        if optional:
            ordered_role["conditional"] = f"source_role.{role}.present"
        ordered_roles.append(ordered_role)

    nodes = []
    for index, role in enumerate(DYNAMIC_ROLES):
        start = _selector(selected[role], starts_with=(role == "appendices"))
        node: dict[str, Any] = {
            "id": role,
            "kind": "dynamic_content" if source_required[role] else "optional",
            "content_role": role,
            "start_selector": start,
            "range_policy": "replace_between",
        }
        if index + 1 < len(DYNAMIC_ROLES):
            next_role = DYNAMIC_ROLES[index + 1]
            node["end_selector"] = _selector(
                selected[next_role], starts_with=(next_role == "appendices")
            )
        else:
            node["end_boundary"] = "body_end"
        if not source_required[role]:
            node["condition"] = f"source_role.{role}.present"
        nodes.append(node)

    profile: dict[str, Any] = {
        "schema_version": "1.0",
        "profile_id": profile_id,
        "title": title,
        "authority": {
            "organization": organization,
            "source_url": source_url,
            "effective_version": effective_version,
            **({"published_date": published_date} if published_date else {}),
        },
        "resources": [{
            "id": "official_word_template",
            "path": "resources/official-template.docx",
            "sha256": _sha256(copied_template),
            "kind": "official_docx",
        }],
        "structure": {
            "ordered_roles": ordered_roles,
            "forbidden_numbered_titles": [
                selected["acknowledgments"]["text"],
                selected["references"]["text"],
                selected["academic_outputs"]["text"],
            ],
        },
        "render_rules": {
            "body_start_anchors": [selected["body"]["text"]],
            "no_header_before_body": True,
            # These are unnumbered chapter *headings*, not physical pages that
            # suppress the PAGE field.  render_rules.unnumbered_roles is
            # reserved for cover/declaration pages with no visible page number.
            "unnumbered_roles": [],
        },
        "regions": {
            "graph_id": f"{profile_id}-assembly",
            "source_section_policy": "discard",
            "nodes": nodes,
            "edges": [
                {"kind": "order", "from": left, "to": right}
                for left, right in zip(DYNAMIC_ROLES, DYNAMIC_ROLES[1:])
            ],
        },
    }
    errors = load_and_validate(profile, ROOT / "schema" / "template-profile.schema.json")
    if errors:
        raise ValueError("generated profile is schema-invalid:\n" + "\n".join(errors))
    profile_path = output_dir / "profile.json"
    profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    compiled = compile_profile(profile_path)
    (output_dir / "compiled-profile.json").write_text(
        json.dumps(compiled, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    audit = {
        "schema_version": "1.0",
        "status": "generated",
        "inputs": {
            "official_docx": str(official_docx.resolve()),
            "official_docx_sha256": _sha256(official_docx),
            "candidates": str(candidates_path.resolve()),
            "role_selection": str(role_selection_path.resolve()),
            "required_selection": str(required_selection_path.resolve()),
        },
        "selected_roles": {role: {"candidate_index": indexes[position], **selected[role]}
                           for position, role in enumerate(role_order)},
        "excluded_output_roles": sorted(excluded_output_roles),
        "profile": str(profile_path.resolve()),
        "compiled_profile": str((output_dir / "compiled-profile.json").resolve()),
    }
    (output_dir / "bootstrap-audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return audit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--official-docx", type=Path, required=True)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--role-selection", type=Path, required=True)
    parser.add_argument("--required-selection", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--organization", required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--effective-version", required=True)
    parser.add_argument("--published-date")
    args = parser.parse_args(argv)
    try:
        audit = build_profile(
            official_docx=args.official_docx, candidates_path=args.candidates,
            role_selection_path=args.role_selection, required_selection_path=args.required_selection,
            output_dir=args.output_dir, profile_id=args.profile_id, title=args.title,
            organization=args.organization, source_url=args.source_url,
            effective_version=args.effective_version, published_date=args.published_date,
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps({"status": audit["status"], "profile": audit["profile"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
