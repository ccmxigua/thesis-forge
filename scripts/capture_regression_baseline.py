#!/usr/bin/env python3
"""Capture a stable, compact baseline from per-case pipeline manifests."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def artifact_record(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {key: value[key] for key in ("path", "bytes", "sha256") if key in value}


def collect_codes(value: Any) -> list[str]:
    codes: set[str] = set()
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            code = node.get("code")
            if isinstance(code, str) and code:
                codes.add(code)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return sorted(codes)


def normalize(case_id: str, manifest_path: Path, source_root: Path) -> dict[str, Any]:
    manifest = read_json(manifest_path)
    counts: Counter[str] = Counter()
    for item in manifest.get("unsupported_items", []) if isinstance(manifest.get("unsupported_items"), list) else []:
        if isinstance(item, dict):
            counts[str(item.get("classification") or item.get("status") or "unknown")] += 1
        else:
            counts["unsupported_items"] += 1
    render = manifest.get("render_validation")
    if isinstance(render, dict):
        counts[f"render_{render.get('status', 'unknown')}"] += 1
    try:
        relative = manifest_path.resolve().relative_to(source_root.resolve())
    except ValueError:
        relative = manifest_path.resolve()
    steps = [
        {"name": str(step.get("name", "unknown")), "returncode": int(step.get("returncode", -1))}
        for step in manifest.get("steps", []) if isinstance(step, dict)
    ]
    return {
        "case_id": case_id,
        "manifest_path": str(relative),
        "manifest_sha256": sha256(manifest_path),
        "status": manifest.get("status", "unknown"),
        "reason": manifest.get("reason"),
        "pipeline_level": manifest.get("pipeline_level"),
        "compliance_mode": manifest.get("compliance_mode"),
        "overall_status": manifest.get("overall_status"),
        "pipeline_valid": manifest.get("pipeline_valid"),
        "supported_subset_valid": manifest.get("supported_subset_valid"),
        "serialized_docx_valid": manifest.get("serialized_docx_valid"),
        "submission_ready": manifest.get("submission_ready"),
        "docx_fully_compliant": manifest.get("docx_fully_compliant"),
        "output_artifact": artifact_record(manifest.get("output_artifact")),
        "steps": steps,
        "blocking_reasons": sorted(str(x) for x in manifest.get("blocking_reasons", [])),
        "finding_codes": collect_codes(manifest),
        "counts": dict(sorted(counts.items())),
    }


def discover(root: Path) -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    for case_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        candidates = [case_dir / "work" / "pipeline-manifest.json", case_dir / "pipeline-manifest.json"]
        manifest = next((path for path in candidates if path.exists()), None)
        if manifest is not None:
            found.append((case_dir.name, manifest))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    source_root = args.source_root.resolve()
    result = {
        "schema_version": "1.0",
        "source_root": str(source_root),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cases": [normalize(case_id, manifest, source_root) for case_id, manifest in discover(source_root)],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"cases": len(result["cases"]), "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
