#!/usr/bin/env python3
"""Perform read-only checks for a thesis-forge run.

This command deliberately does not create a run directory, normalize inputs,
call a host agent, invoke Pandoc, or start the ten-school batch driver.  It is
safe to use before deciding whether a fresh run can be authorized.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from batch_rerun_ten_schools import load_manifest  # noqa: E402
from host_runtime import HostRuntimeError, inspect_host_runtime  # noqa: E402


REQUIRED_PATHS = (
    "SKILL.md",
    "scripts/thesis_format.py",
    "scripts/thesis_format_pipeline.py",
    "scripts/requirements_engine.py",
    "schema/format-spec.schema.json",
    "tests/sample-thesis.tex",
)


def _tool_record(name: str, *, required: bool) -> dict[str, Any]:
    path = shutil.which(name)
    record: dict[str, Any] = {"required": required, "path": path}
    if path is None:
        record.update({"available": False, "version": None, "error": "not found"})
        return record
    try:
        result = subprocess.run(
            [path, "-v" if name == "pdftoppm" else "--version"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        record.update({"available": True, "version": None, "probe_error": str(exc)})
        return record
    output = (result.stdout or result.stderr or "").strip()
    record.update({
        # Some required conversion tools (notably pdftoppm) use a non-zero
        # status for their help/version switch.  Presence is the availability
        # gate; retain the probe result separately instead of misclassifying a
        # runnable executable as missing.
        "available": True,
        "version": output.splitlines()[0] if output else None,
        "probe_returncode": result.returncode,
    })
    if result.returncode != 0:
        record["probe_error"] = output[-500:] or "version command failed"
    return record


def _path_records() -> tuple[dict[str, Any], list[str]]:
    records: dict[str, Any] = {}
    missing: list[str] = []
    for relative in REQUIRED_PATHS:
        path = (ROOT / relative).resolve()
        exists = path.is_file()
        records[relative] = {"path": str(path), "is_file": exists}
        if not exists:
            missing.append(relative)
    return records, missing


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    paths, missing_paths = _path_records()
    required_tools = {"pandoc", *(args.require_tool or [])}
    tools = {
        name: _tool_record(name, required=name in required_tools)
        for name in sorted({"pandoc", "soffice", "pdftoppm", *required_tools})
    }
    missing_tools = sorted(
        name for name, record in tools.items()
        if record["required"] and not record["available"]
    )

    host_error: str | None = None
    try:
        host = inspect_host_runtime(
            expected=args.host_runtime,
            require=args.require_host_runtime,
        )
        host_report: dict[str, Any] = host.as_audit()
    except HostRuntimeError as exc:
        host_report = {"verification_status": "invalid"}
        host_error = str(exc)

    manifest_report: dict[str, Any] = {"requested": bool(args.template_manifest)}
    manifest_error: str | None = None
    if args.template_manifest:
        try:
            source, cases, manifest_path = load_manifest(args.template_manifest)
            manifest_report.update({
                "path": str(manifest_path.resolve()),
                "source_tex": str(source.resolve()),
                "case_count": len(cases),
                "case_ids": [str(case["id"]) for case in cases],
                "valid": True,
            })
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            manifest_report.update({"valid": False})
            manifest_error = str(exc)

    errors = [
        *(f"missing_required_path:{item}" for item in missing_paths),
        *(f"missing_required_tool:{item}" for item in missing_tools),
    ]
    if host_error:
        errors.append(f"host_runtime:{host_error}")
    if manifest_error:
        errors.append(f"template_manifest:{manifest_error}")
    return {
        "schema_version": "1.0",
        "status": "passed" if not errors else "blocked",
        "read_only": True,
        "repo_root": str(ROOT),
        "paths": paths,
        "tools": tools,
        "host_runtime": host_report,
        "template_manifest": manifest_report,
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--template-manifest",
        type=Path,
        help="optional manifest to validate without creating a batch run",
    )
    parser.add_argument(
        "--host-runtime",
        help="expected host runtime; only checked against explicit host context",
    )
    parser.add_argument(
        "--require-host-runtime",
        action="store_true",
        help="fail unless a host runtime is explicitly declared",
    )
    parser.add_argument(
        "--require-tool",
        action="append",
        metavar="NAME",
        help="mark an additional executable as required; repeatable",
    )
    parser.add_argument("--json", action="store_true", help="emit one JSON report")
    args = parser.parse_args(argv)
    report = build_report(args)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"preflight: {report['status']} (read_only={report['read_only']})")
        for name, record in report["tools"].items():
            state = "available" if record["available"] else "missing"
            print(f"  tool {name}: {state} {record.get('version') or ''}".rstrip())
        if report["template_manifest"].get("requested"):
            print(f"  template_manifest: {report['template_manifest'].get('valid', False)}")
        if report["errors"]:
            for error in report["errors"]:
                print(f"  error: {error}", file=sys.stderr)
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
