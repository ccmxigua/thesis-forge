#!/usr/bin/env python3
"""Run the reusable LaTeX sample through conversion and optional school formatting."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .extract_semantic_metadata import extract
    from .inspect_docx_semantics import inspect
    from .semantic_contract import strict_json_dumps, strict_json_read
except ImportError:  # direct script execution
    from extract_semantic_metadata import extract
    from inspect_docx_semantics import inspect
    from semantic_contract import strict_json_dumps, strict_json_read

ROOT = Path(__file__).resolve().parents[1]


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(strict_json_dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(name: str, command: list[str], steps: list[dict[str, Any]]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    steps.append({"name": name, "command": command, "returncode": result.returncode,
                  "stdout": result.stdout.strip(), "stderr": result.stderr.strip()})
    return result


def expected_failures(inventory: dict[str, Any], expected: dict[str, Any], stage: str) -> list[dict[str, Any]]:
    failures = []
    roles = inventory.get("roles", {})
    for role, contract in expected.get("roles", {}).items():
        count = roles.get(role, {}).get("count", 0)
        if "exact" in contract and count != contract["exact"]:
            failures.append({"stage": stage, "role": role, "failure_type": "format_mismatch",
                             "actual": count, "expected": {"exact": contract["exact"]}})
        if "minimum" in contract and count < contract["minimum"]:
            failures.append({"stage": stage, "role": role, "failure_type": "content_missing",
                             "actual": count, "expected": {"minimum": contract["minimum"]}})
    return failures


def source_to_docx_losses(source: dict[str, Any], docx: dict[str, Any], stage: str) -> list[dict[str, Any]]:
    source_counts = {
        "thesis_title_zh": int(bool(source.get("title_zh"))),
        "thesis_title_en": int(bool(source.get("title_en"))),
        "figure": source.get("inventory", {}).get("figures", 0),
        "table": source.get("inventory", {}).get("tables", 0),
        "equation": source.get("inventory", {}).get("display_equations", 0),
    }
    failures = []
    for role, source_count in source_counts.items():
        output_count = docx.get("roles", {}).get(role, {}).get("count", 0)
        if source_count > 0 and output_count == 0:
            failures.append({"stage": stage, "role": role, "failure_type": "conversion_loss",
                             "source_count": source_count, "output_count": output_count})
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--expected", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--requirements", type=Path)
    parser.add_argument("--style-template", type=Path)
    parser.add_argument("--llm-response", type=Path)
    parser.add_argument("--prepare-host-review", action="store_true")
    parser.add_argument("--analysis-mode", choices=["llm_primary", "rule_only", "known_template"], default="llm_primary")
    parser.add_argument("--compliance-mode", choices=["full", "supported_subset"], default="full")
    parser.add_argument("--thesis-profile", type=Path)
    args = parser.parse_args(argv)
    if args.prepare_host_review and args.llm_response:
        parser.error("choose either --prepare-host-review or --llm-response")

    out = args.out_dir.resolve(); out.mkdir(parents=True, exist_ok=True)
    generic = out / "generic.docx"; final = out / "final.docx"
    manifest_path = out / "golden-e2e-manifest.json"
    steps: list[dict[str, Any]] = []
    manifest: dict[str, Any] = {"schema_version": "1.0", "started_at": datetime.now(timezone.utc).isoformat(),
                                "status": "running", "source": str(args.source.resolve()), "steps": steps}
    source_semantics = extract(args.source)
    write_json(out / "semantic-metadata.json", source_semantics)
    expected = strict_json_read(args.expected)

    final_semantics = None
    if args.requirements:
        pipeline = out / "pipeline"
        command = [sys.executable, str(ROOT / "scripts" / "thesis_format_pipeline.py"),
                   str(args.requirements), str(args.source), str(final), "--work-dir", str(pipeline),
                   "--analysis-mode", args.analysis_mode,
                   "--compliance-mode", args.compliance_mode]
        if args.style_template:
            command += ["--style-template", str(args.style_template)]
        if args.llm_response:
            command += ["--llm-response", str(args.llm_response)]
        elif args.prepare_host_review:
            command.append("--prepare-host-review")
        if args.thesis_profile:
            command += ["--thesis-profile", str(args.thesis_profile)]
        result = run("school_pipeline", command, steps)
        if result.returncode:
            manifest.update(status="failed", failed_stage="school_pipeline", failures=[])
            write_json(manifest_path, manifest); return result.returncode
        if args.prepare_host_review:
            manifest.update(status="host_review_required", finished_at=datetime.now(timezone.utc).isoformat(),
                            host_agent_review_manifest=str((pipeline / "requirements" / "host-agent-review-manifest.json").resolve()))
            write_json(manifest_path, manifest)
            print(json.dumps({"status": manifest["status"], "manifest": str(manifest_path)}, ensure_ascii=False))
            return 0
        pipeline_manifest = strict_json_read(pipeline / "pipeline-manifest.json")
        if pipeline_manifest.get("pipeline_level") != "latex_end_to_end" or "intermediate_docx" not in pipeline_manifest:
            manifest.update(status="failed", failed_stage="school_pipeline_provenance",
                            failures=[{"failure_type": "missing_latex_end_to_end_provenance"}])
            write_json(manifest_path, manifest); return 2
        generic = Path(pipeline_manifest["intermediate_docx"]["path"])
        generic_semantics = inspect(generic); write_json(out / "generic-semantics.json", generic_semantics)
        failures = source_to_docx_losses(source_semantics, generic_semantics, "generic_docx")
        failures.extend(expected_failures(generic_semantics, expected, "generic_docx"))
        final_semantics = inspect(final); write_json(out / "final-semantics.json", final_semantics)
        failures.extend(source_to_docx_losses(source_semantics, final_semantics, "final_docx"))
        failures.extend(expected_failures(final_semantics, expected, "final_docx"))
    else:
        result = run("convert", [str(ROOT / "convert.sh"), str(args.source), str(generic)], steps)
        if result.returncode:
            manifest.update(status="failed", failed_stage="convert")
            write_json(manifest_path, manifest); return result.returncode
        generic_semantics = inspect(generic); write_json(out / "generic-semantics.json", generic_semantics)
        failures = source_to_docx_losses(source_semantics, generic_semantics, "generic_docx")
        failures.extend(expected_failures(generic_semantics, expected, "generic_docx"))

    manifest.update(status="failed" if failures else "completed", finished_at=datetime.now(timezone.utc).isoformat(),
                    generic_docx=str(generic), final_docx=str(final) if final_semantics else None,
                    failures=failures, expected=str(args.expected.resolve()))
    write_json(manifest_path, manifest)
    print(json.dumps({"status": manifest["status"], "manifest": str(manifest_path), "failures": failures}, ensure_ascii=False))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
