#!/usr/bin/env python3
"""Run a manifest-defined batch with a fresh evidence run for every case.

This driver deliberately has no migration mode.  It never reads a previous
build, old clause corpus, or old semantic response.  Each invocation receives
its template inputs from a manifest and creates a new output directory.  The
default fresh batch requires every case to use ``llm_primary`` and, with
``--auto-host-agent``, continues automatically from each fresh packet through
the explicitly declared native host adapter, offline provenance merge, and full
DOCX/declaration generation.  ``rule_only`` and ``known_template`` remain
available only through the explicit ``--allow-supported-subset`` opt-in for
development and compatibility regression runs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = Path("inputs/ten-school-template-manifest.json")
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
from host_adapters import codex as codex_adapter  # noqa: E402
from host_runtime import (  # noqa: E402
    HostRuntimeError,
    automatic_adapter_id,
    require_host_runtime,
)


def resolve_project_path(value: str | Path, *, label: str) -> Path:
    path = Path(value)
    resolved = path.resolve() if path.is_absolute() else (ROOT / path).resolve()
    if not resolved.is_file():
        raise ValueError(f"{label} does not exist: {resolved}")
    return resolved


def load_manifest(path: Path) -> tuple[Path, list[dict[str, Any]], Path]:
    manifest_path = path.resolve() if path.is_absolute() else (ROOT / path).resolve()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read template manifest: {manifest_path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != "1.0":
        raise ValueError("template manifest must be an object with schema_version 1.0")
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("template manifest cases must be a non-empty array")
    source_value = payload.get("source_tex", "tests/sample-thesis.tex")
    source = resolve_project_path(source_value, label="source_tex")
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_cases, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"template manifest case {index} must be an object")
        case_id = raw.get("id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"template manifest case {index} needs a non-empty id")
        case_id = case_id.strip()
        if case_id in seen:
            raise ValueError(f"duplicate template manifest case id: {case_id}")
        mode = raw.get("analysis_mode", "llm_primary")
        if mode not in {"llm_primary", "rule_only", "known_template"}:
            raise ValueError(f"unsupported analysis_mode for {case_id}: {mode}")
        requirements = resolve_project_path(raw.get("requirements", ""), label=f"{case_id}.requirements")
        style_value = raw.get("style_template")
        style = resolve_project_path(style_value, label=f"{case_id}.style_template") if style_value else None
        profile_value = raw.get("template_profile")
        profile = resolve_project_path(profile_value, label=f"{case_id}.template_profile") if profile_value else None
        template_boundary = raw.get("template_boundary")
        if template_boundary is None:
            template_boundary = "official_template" if (style or profile) else "requirements_only"
        if template_boundary not in {"requirements_only", "official_template"}:
            raise ValueError(
                f"unsupported template_boundary for {case_id}: {template_boundary}"
            )
        if template_boundary == "requirements_only" and (style or profile):
            raise ValueError(
                f"{case_id} declares requirements_only but also supplies a template resource"
            )
        if template_boundary == "official_template" and not (style or profile):
            raise ValueError(
                f"{case_id} declares official_template without style_template or template_profile"
            )
        cases.append({
            "id": case_id,
            "requirements": requirements,
            "style_template": style,
            "template_profile": profile,
            "template_boundary": template_boundary,
            "analysis_mode": mode,
        })
        seen.add(case_id)
    return source, cases, manifest_path


def run_command(cmd: list[str], *, label: str) -> dict[str, Any]:
    print(f"\n{'=' * 70}\n{label}\n{'=' * 70}", flush=True)
    started = time.time()
    result = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)
    elapsed = round(time.time() - started, 1)
    print(f"returncode={result.returncode} ({elapsed:.1f}s)")
    if result.stdout:
        print(result.stdout[-1200:])
    if result.stderr:
        print("STDERR:", result.stderr[-1200:])
    return {
        "returncode": result.returncode,
        "elapsed_s": elapsed,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "command": cmd,
        "stdout_tail": result.stdout[-3000:],
        "stderr_tail": result.stderr[-3000:],
    }


def pipeline_command(case: dict[str, Any], source: Path, work_dir: Path, output_docx: Path,
                     *, compliance_mode: str, prepare_host_review: bool,
                     neutral_reference_docx: Path | None = None,
                     llm_response: Path | None = None,
                     run_id: str | None = None,
                     requirements_dir: Path | None = None,
                     host_agent_audit: Path | None = None,
                     merge_receipt: Path | None = None,
                     host_review_chunk_size: int = 20) -> list[str]:
    command = [
        sys.executable, "scripts/thesis_format_pipeline.py",
        str(case["requirements"]), str(source), str(output_docx),
        "--work-dir", str(work_dir),
        "--analysis-mode", str(case["analysis_mode"]),
        "--compliance-mode", compliance_mode,
        "--host-review-chunk-size", str(host_review_chunk_size),
    ]
    if requirements_dir:
        command.extend(["--requirements-dir", str(requirements_dir)])
    if neutral_reference_docx:
        command.extend(["--neutral-reference-docx", str(neutral_reference_docx)])
    elif case.get("style_template"):
        command.extend(["--style-template", str(case["style_template"])])
    if prepare_host_review:
        command.append("--prepare-host-review")
    if llm_response:
        command.extend(["--llm-response", str(llm_response)])
    if run_id:
        command.extend(["--run-id", str(run_id)])
    if host_agent_audit:
        command.extend(["--host-agent-audit", str(host_agent_audit)])
    if merge_receipt:
        command.extend(["--merge-receipt", str(merge_receipt)])
    if case.get("template_profile") and not neutral_reference_docx:
        command.extend(["--template-profile", str(case["template_profile"])])
    return command


def post_render_command(
    case: dict[str, Any], *, source_docx: Path, final_docx: Path, pdf: Path,
    render_report: Path, pre_validation: Path, format_spec: Path,
    official_template: Path, official_style_map: Path, generated_style_map: Path,
    submission_audit: Path, format_comparison: Path,
    format_comparison_markdown: Path, acceptance_out: Path,
    thesis_profile: Path | None = None,
    word_open_timeout: int = 45, word_timeout: int = 180,
) -> list[str]:
    """Build the explicit post-Word release command for one case.

    The final DOCX is deliberately a different path from the deterministic
    pre-render DOCX.  ``post_render_acceptance.py`` then binds Word's report,
    the final submission audit, and the final format comparison to that final
    path.
    """
    command = [
        sys.executable, "scripts/post_render_acceptance.py",
        str(source_docx), str(final_docx), str(pdf),
        "--render-report", str(render_report),
        "--format-spec", str(format_spec),
        "--pre-validation", str(pre_validation),
        "--submission-audit", str(submission_audit),
        "--format-comparison", str(format_comparison),
        "--format-comparison-markdown", str(format_comparison_markdown),
        "--acceptance-out", str(acceptance_out),
        "--official-template", str(official_template),
        "--official-style-map", str(official_style_map),
        "--generated-style-map", str(generated_style_map),
        "--word-open-timeout", str(word_open_timeout),
        "--word-timeout", str(word_timeout),
    ]
    if case.get("template_boundary") == "requirements_only":
        command.append("--requirements-only")
    if case.get("template_profile"):
        command.extend(["--template-profile", str(case["template_profile"])])
    if thesis_profile:
        command.extend(["--thesis-profile", str(thesis_profile)])
    return command


def attach_post_render_manifest(
    manifest_path: Path, *, pre_render_docx: Path, final_docx: Path, pdf: Path,
    render_report: Path, submission_audit: Path, format_comparison: Path,
    format_comparison_markdown: Path, acceptance_out: Path,
    accepted: bool,
) -> dict[str, Any]:
    """Record the two-stage artifact chain without rewriting pre-render receipts."""
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    old_comparison = payload.get("format_comparison")
    payload["pre_render_output"] = str(pre_render_docx.resolve())
    payload["pre_render_format_comparison"] = old_comparison
    payload["post_render_status"] = "accepted" if accepted else "blocked"
    payload["post_render_acceptance"] = str(acceptance_out.resolve())
    payload["post_word_render"] = {
        "pre_render_docx": str(pre_render_docx.resolve()),
        "final_docx": str(final_docx.resolve()),
        "pdf": str(pdf.resolve()),
        "render_report": str(render_report.resolve()),
        "submission_audit": str(submission_audit.resolve()),
        "format_comparison": str(format_comparison.resolve()),
        "format_comparison_markdown": str(format_comparison_markdown.resolve()),
        "pre_render_receipts_remain_bound_to_pre_render_docx": True,
    }
    if accepted:
        payload["output"] = str(final_docx.resolve())
        payload["format_comparison"] = str(format_comparison.resolve())
        payload["format_comparison_markdown"] = str(format_comparison_markdown.resolve())
        payload["submission_audit"] = str(submission_audit.resolve())
        payload["render_report"] = str(render_report.resolve())
        payload["rendered_pdf"] = str(pdf.resolve())
        payload["submission_ready"] = True
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def canonical_profile_report(work: Path) -> dict[str, Any] | None:
    path = work / "thesis-profile.json"
    if not path.exists():
        return None
    profile = json.loads(path.read_text(encoding="utf-8"))
    return {
        "path": str(path.resolve()),
        "metadata_status": profile.get("metadata_status"),
        "pending_fields": profile.get("pending_fields", []),
    }


def extraction_report(work: Path, *, requirements_dir: Path | None = None,
                      host_review_dir: Path | None = None) -> dict[str, Any]:
    requirements = requirements_dir or (work / "requirements")
    extraction_manifest = requirements / "extraction-manifest.json"
    format_spec = requirements / "format-spec.json"
    pipeline_manifest = work / "pipeline-manifest.json"
    report: dict[str, Any] = {
        "extraction_manifest": str(extraction_manifest.resolve()) if extraction_manifest.exists() else None,
        "format_spec": str(format_spec.resolve()) if format_spec.exists() else None,
        "pipeline_manifest": str(pipeline_manifest.resolve()) if pipeline_manifest.exists() else None,
    }
    if host_review_dir is not None:
        audit = host_review_dir / "host-agent-run.json"
        receipt = host_review_dir / "merge-receipt.json"
        report.update({
            "host_review_dir": str(host_review_dir.resolve()),
            "host_agent_audit": str(audit.resolve()) if audit.exists() else None,
            "merge_receipt": str(receipt.resolve()) if receipt.exists() else None,
        })
    if extraction_manifest.exists():
        data = json.loads(extraction_manifest.read_text(encoding="utf-8"))
        report.update({
            "run_id": data.get("run_id"),
            "cache_reused": data.get("cache_reused"),
            "source_sha256": data.get("source_sha256"),
            "semantic_review_provenance": data.get("semantic_review_provenance"),
        })
    if format_spec.exists():
        spec = json.loads(format_spec.read_text(encoding="utf-8"))
        registry = spec.get("resource_registry") if isinstance(spec, dict) else None
        report.update({
            "declarations_present": isinstance(spec, dict) and spec.get("declarations") is not None,
            "resource_registry_present": isinstance(registry, dict),
        })
    if pipeline_manifest.exists():
        data = json.loads(pipeline_manifest.read_text(encoding="utf-8"))
        report["pipeline_status"] = data.get("status")
        report["host_agent_review_manifest"] = data.get("host_agent_review_manifest")
    return report


def _artifact_path(value: Any, *, root: Path) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _read_artifact_json(value: Any, *, root: Path) -> tuple[Path | None, dict[str, Any] | None]:
    path = _artifact_path(value, root=root)
    if path is None or not path.is_file():
        return path, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return path, None
    return path, payload if isinstance(payload, dict) else None


def _inspect_docx_artifact(path: Path) -> dict[str, Any]:
    """Independently verify the current output is a readable OPC DOCX package."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        with ZipFile(path) as archive:
            names = set(archive.namelist())
            required = {"[Content_Types].xml", "word/document.xml"}
            bad_member = archive.testzip()
        missing = sorted(required - names)
        return {
            "path": str(path.resolve()),
            "bytes": path.stat().st_size,
            "sha256": digest.hexdigest(),
            "opc_package_valid": not missing and bad_member is None,
            "missing_members": missing,
            "corrupt_member": bad_member,
        }
    except (OSError, BadZipFile, ValueError) as exc:
        return {
            "path": str(path.resolve()),
            "bytes": path.stat().st_size if path.exists() else 0,
            "sha256": digest.hexdigest() if path.exists() else None,
            "opc_package_valid": False,
            "missing_members": [],
            "corrupt_member": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


def case_acceptance(result: dict[str, Any], *, root: Path = ROOT) -> dict[str, Any]:
    """Apply one strict, artifact-bound acceptance gate to a batch case.

    A subprocess return code is only a stage signal.  The case is accepted only
    when the current run, current manifest, generated DOCX, schema validation,
    capability gate, serialized-DOCX audit, and final comparison all agree on
    the same artifact.  When the post-Word stage is present, pre-render
    receipts are checked against the pre-render DOCX and final submission
    evidence is checked against the separate post-Word DOCX.
    """
    blockers: list[str] = []
    checks: dict[str, Any] = {}

    if result.get("returncode") != 0:
        blockers.append(f"pipeline_returncode:{result.get('returncode')}")
    fresh = result.get("fresh_run") if isinstance(result.get("fresh_run"), dict) else {}
    checks["fresh_run"] = fresh
    if fresh.get("cache_reused") is not False:
        blockers.append("fresh_run_not_proven")
    if not fresh.get("run_id"):
        blockers.append("fresh_run_id_missing")
    if result.get("analysis_mode") == "llm_primary":
        audit_path, audit = _read_artifact_json(fresh.get("host_agent_audit"), root=root)
        receipt_path, receipt = _read_artifact_json(fresh.get("merge_receipt"), root=root)
        checks["host_agent_audit"] = str(audit_path) if audit_path else None
        checks["merge_receipt"] = str(receipt_path) if receipt_path else None
        if audit is None or audit.get("status") != "merged":
            blockers.append("host_agent_audit_not_merged")
        elif audit.get("run_id") != fresh.get("run_id"):
            blockers.append("host_agent_audit_run_mismatch")
        if receipt is None or receipt.get("status") != "merged":
            blockers.append("merge_receipt_not_merged")
        elif receipt.get("run_id") != fresh.get("run_id"):
            blockers.append("merge_receipt_run_mismatch")

    manifest_path, manifest = _read_artifact_json(fresh.get("pipeline_manifest"), root=root)
    checks["pipeline_manifest"] = str(manifest_path) if manifest_path else None
    if manifest is None:
        blockers.append("pipeline_manifest_missing_or_invalid")
        return {"status": "blocked", "accepted": False, "blockers": sorted(set(blockers)), "checks": checks}

    checks["pipeline_status"] = manifest.get("status")
    if manifest.get("status") != "completed":
        blockers.append(f"pipeline_status:{manifest.get('status')}")
    if manifest.get("compliance_mode") != "full":
        blockers.append("not_full_compliance")
    if manifest.get("blocking_reasons"):
        blockers.append("blocking_reasons_present")
    if manifest.get("metadata_status") == "pending" or manifest.get("metadata_pending_fields"):
        blockers.append("metadata_pending")
    if manifest.get("capability_preflight_status") in {None, "blocked", "failed"}:
        blockers.append("capability_preflight_not_passed")

    post_acceptance_path, post_acceptance = _read_artifact_json(
        manifest.get("post_render_acceptance"), root=root
    )
    post_render_active = post_acceptance is not None or manifest.get("post_render_status") is not None
    checks["post_render_acceptance"] = str(post_acceptance_path) if post_acceptance_path else None
    if post_render_active and (post_acceptance is None or post_acceptance.get("status") != "accepted"):
        blockers.append("post_render_acceptance_not_passed")

    output_path = _artifact_path(manifest.get("output"), root=root)
    checks["output"] = str(output_path) if output_path else None
    if output_path is None or not output_path.is_file() or output_path.stat().st_size <= 0:
        blockers.append("generated_docx_missing")
        output_artifact = None
    else:
        output_artifact = _inspect_docx_artifact(output_path)
        checks["output_artifact"] = output_artifact
        if not output_artifact.get("opc_package_valid"):
            blockers.append("generated_docx_not_valid_opc")
    if output_path is not None and manifest.get("output"):
        manifest_output = _artifact_path(manifest.get("output"), root=root)
        if manifest_output != output_path:
            blockers.append("manifest_output_path_mismatch")

    pre_render_output_path = _artifact_path(manifest.get("pre_render_output"), root=root)
    if post_render_active:
        if pre_render_output_path is None or not pre_render_output_path.is_file():
            blockers.append("pre_render_docx_missing")
        else:
            pre_artifact = _inspect_docx_artifact(pre_render_output_path)
            checks["pre_render_output_artifact"] = pre_artifact
            if not pre_artifact.get("opc_package_valid"):
                blockers.append("pre_render_docx_not_valid_opc")
        if post_acceptance is not None:
            if _artifact_path(post_acceptance.get("post_render_docx"), root=root) != output_path:
                blockers.append("post_render_acceptance_output_path_mismatch")
            if (output_artifact is not None
                    and post_acceptance.get("post_render_docx_sha256") != output_artifact.get("sha256")):
                blockers.append("post_render_acceptance_artifact_hash_mismatch")

    format_spec_path = _artifact_path(manifest.get("format_spec"), root=root)
    schema_path = format_spec_path.parent / "schema-validation.json" if format_spec_path else None
    schema = None
    if schema_path and schema_path.is_file():
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            schema = None
    checks["schema_validation"] = str(schema_path) if schema_path else None
    if not isinstance(schema, dict) or schema.get("valid") is not True:
        blockers.append("schema_validation_not_passed")

    capability_path, capability = _read_artifact_json(manifest.get("capability_preflight"), root=root)
    checks["capability_preflight"] = str(capability_path) if capability_path else None
    if capability is None or capability.get("status") in {"blocked", "failed"}:
        blockers.append("capability_artifact_not_passed")
    elif any(item.get("blocking") for item in capability.get("findings", []) if isinstance(item, dict)):
        blockers.append("capability_blocking_findings")

    validation_path, validation = _read_artifact_json(manifest.get("validation_report"), root=root)
    checks["validation_report"] = str(validation_path) if validation_path else None
    if validation is None:
        blockers.append("validation_report_missing_or_invalid")
    else:
        reported_output = _artifact_path(validation.get("output_docx"), root=root)
        expected_validation_output = pre_render_output_path if post_render_active else output_path
        if reported_output is not None and reported_output != expected_validation_output:
            blockers.append("validation_output_path_mismatch")
        if validation.get("valid") is not True or validation.get("format_ready") is not True:
            blockers.append("format_validation_not_passed")
        if validation.get("serialized_docx_valid") is not True:
            blockers.append("serialized_docx_not_verified")
        receipt_audit = validation.get("property_receipt_audit")
        if not isinstance(receipt_audit, dict) or receipt_audit.get("valid") is not True:
            blockers.append("property_receipts_not_verified")
        elif (output_artifact is not None or post_render_active):
            receipts = receipt_audit.get("receipts", [])
            if not isinstance(receipts, list):
                blockers.append("property_receipts_malformed")
            else:
                receipt_hashes = {
                    str(item.get("serialized_docx_sha256"))
                    for item in receipts if isinstance(item, dict)
                }
                receipt_target = (
                    checks.get("pre_render_output_artifact", {}).get("sha256")
                    if post_render_active else output_artifact["sha256"]
                )
                if receipt_hashes and receipt_hashes != {receipt_target}:
                    blockers.append("property_receipt_artifact_hash_mismatch")
        render = validation.get("render_validation")
        if not post_render_active:
            if not isinstance(render, dict) or render.get("rendered_verified") is not True:
                blockers.append("trusted_render_not_verified")
            elif output_artifact is not None:
                render_source = ((render.get("evidence") or {}).get("source_docx")
                                 if isinstance(render.get("evidence"), dict) else None)
                if (not isinstance(render_source, dict)
                        or render_source.get("sha256") != output_artifact["sha256"]):
                    blockers.append("render_artifact_hash_mismatch")
            if validation.get("submission_ready") is not True:
                blockers.append("submission_gate_not_ready")

    comparison_path, comparison = _read_artifact_json(manifest.get("format_comparison"), root=root)
    checks["format_comparison"] = str(comparison_path) if comparison_path else None
    if comparison is None or comparison.get("status") != "passed":
        blockers.append("post_generation_comparison_not_passed")

    unique_blockers = sorted(set(blockers))
    return {
        "status": "accepted" if not unique_blockers else "blocked",
        "accepted": not unique_blockers,
        "blockers": unique_blockers,
        "checks": checks,
    }


def select_cases(cases: list[dict[str, Any]], requested: list[str] | None = None) -> list[dict[str, Any]]:
    """Select cases in manifest order, which is the canonical batch order."""
    requested_ids = {str(item) for item in (requested or [case["id"] for case in cases])}
    return [case for case in cases if str(case["id"]) in requested_ids]


GLOBAL_FATAL_MARKERS = (
    "host runtime mismatch",
    "thesis_forge_host_runtime",
    "no automatic adapter",
    "parent session binding is missing",
    "parent session key was not found",
    "cannot inspect openclaw parent sessions",
    "openclaw executable was not found",
    "codex executable was not found",
    "codex returned invalid jsonl",
    "codex turn did not complete",
)


def global_fatal_reason(result: dict[str, Any]) -> str | None:
    """Return a reason that must stop the whole batch, not just one case."""
    text_parts: list[str] = []
    if isinstance(result.get("error"), str):
        text_parts.append(result["error"])
    stages = result.get("stages")
    if isinstance(stages, dict):
        for stage in stages.values():
            if not isinstance(stage, dict):
                continue
            for key in ("stderr_tail", "stdout_tail", "error"):
                if isinstance(stage.get(key), str):
                    text_parts.append(stage[key])
    combined = "\n".join(text_parts)
    lowered = combined.lower()
    for marker in GLOBAL_FATAL_MARKERS:
        if marker in lowered:
            return marker
    return None


def run_case(base: Path, source: Path, case: dict[str, Any], *, prepare_host_review: bool,
             auto_host_agent: bool = False, host_agent_timeout: int = 900,
             host_agent_max_concurrency: int = 4,
             host_agent_max_attempts: int = 2,
             host_agent_model: str | None = None,
             host_agent_parent_session_key: str | None = None,
             host_agent_auth_env_only: bool = False,
             host_agent_runner: str = "exec",
             host_runtime: str | None = None,
             host_adapter_id: str | None = None,
             inherit_parent_model: bool = True,
             host_review_chunk_size: int = 20,
             host_agent_id: str = "main", openclaw_bin: str | None = None,
             openclaw_config: Path | None = None,
             codex_bin: str | None = None,
             codex_model: str = codex_adapter.DEFAULT_MODEL,
             neutral_reference_docx: Path | None = None,
             word_open_timeout: int = 45,
             word_timeout: int = 180) -> dict[str, Any]:
    case_dir = base / str(case["id"])
    work = case_dir / "work"
    review_requirements = work / "review" / "requirements"
    execution_requirements = work / "execution" / "requirements"
    output = case_dir / "generated.docx"
    llm_case = case["analysis_mode"] == "llm_primary"
    if llm_case and not (prepare_host_review or auto_host_agent):
        raise ValueError(
            f"case {case['id']} is llm_primary; fresh-only batch requires "
            "--prepare-host-review or --auto-host-agent and never accepts an implicit response"
        )
    if llm_case:
        compliance_mode = "full"
        host_review = True
    else:
        compliance_mode = "supported_subset"
        host_review = False
    prepare_result = run_command(
        pipeline_command(case, source, work, output,
                         compliance_mode=compliance_mode,
                         prepare_host_review=host_review,
                         requirements_dir=(review_requirements if host_review else execution_requirements),
                         host_review_chunk_size=host_review_chunk_size,
                         neutral_reference_docx=neutral_reference_docx),
        label=(f"[{case['id']}] fresh extraction + host-agent packet"
               if host_review else
               f"[{case['id']}] fresh deterministic extraction ({case['analysis_mode']})"),
    )
    # Keep the final case summary separate from each stage record.  Otherwise
    # attaching ``stages`` below makes the active stage dict contain itself and
    # ``run-results.json`` cannot be serialized.
    result = dict(prepare_result)
    stages: dict[str, Any] = {"prepare": prepare_result}
    if llm_case and auto_host_agent and prepare_result["returncode"] == 0:
        extraction_manifest = review_requirements / "extraction-manifest.json"
        try:
            extraction = json.loads(extraction_manifest.read_text(encoding="utf-8"))
            run_id = extraction["run_id"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            host_result = {
                "returncode": 2,
                "elapsed_s": 0.0,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "command": [],
                "stdout_tail": "",
                "stderr_tail": f"cannot read fresh run_id: {exc}",
            }
        else:
            response_out = work / "review" / "host-agent-response.json"
            bridge_command = [
                sys.executable, "scripts/host_agent_bridge.py",
                str(review_requirements),
                "--response-out", str(response_out),
                "--run-id", str(run_id),
                "--timeout", str(host_agent_timeout),
                "--max-concurrency", str(host_agent_max_concurrency),
                "--max-attempts", str(host_agent_max_attempts),
            ]
            if host_runtime:
                bridge_command.extend(["--host-runtime", host_runtime])
            if host_adapter_id == "codex":
                if codex_bin:
                    bridge_command.extend(["--codex-bin", codex_bin])
                bridge_command.extend(["--codex-model", codex_model])
            else:
                bridge_command.extend(["--agent-id", host_agent_id])
                if host_agent_auth_env_only:
                    bridge_command.append("--auth-env-only")
                bridge_command.extend(["--runner", host_agent_runner])
                if host_agent_model:
                    bridge_command.extend(["--model", host_agent_model])
                elif inherit_parent_model:
                    bridge_command.append("--inherit-parent-model")
                else:
                    bridge_command.append("--no-inherit-parent-model")
                if host_agent_parent_session_key:
                    bridge_command.extend(["--parent-session-key", host_agent_parent_session_key])
                if openclaw_bin:
                    bridge_command.extend(["--openclaw-bin", openclaw_bin])
                if openclaw_config:
                    bridge_command.extend(["--openclaw-config", str(openclaw_config)])
            host_result = run_command(
                bridge_command,
                label=f"[{case['id']}] current Host Agent review + provenance merge",
            )
        stages["host_agent"] = host_result
        result = dict(host_result)
        if host_result["returncode"] == 0:
            full_result = run_command(
                pipeline_command(
                    case, source, work, output,
                    compliance_mode=compliance_mode,
                    prepare_host_review=False,
                    requirements_dir=execution_requirements,
                    neutral_reference_docx=neutral_reference_docx,
                    llm_response=response_out,
                    run_id=str(run_id),
                    host_agent_audit=review_requirements / "host-agent-run.json",
                    merge_receipt=review_requirements / "merge-receipt.json",
                    host_review_chunk_size=host_review_chunk_size,
                ),
                label=f"[{case['id']}] full deterministic DOCX + declaration resources",
            )
            stages["full"] = full_result
            result = dict(full_result)

    # A generated DOCX is not a release artifact until Microsoft Word has
    # produced a separate final DOCX/PDF pair and both final outputs have been
    # audited.  Preparation-only runs have no output and therefore stop before
    # this stage.  The post-render helper keeps pre-render receipts immutable.
    if result.get("returncode") == 0 and output.is_file():
        post_dir = work / "application"
        final_docx = case_dir / "final-word.docx"
        pdf = case_dir / "final.pdf"
        render_report = post_dir / "word-render-report.json"
        submission_audit = post_dir / "final-submission-audit.json"
        format_comparison = post_dir / "final-format-comparison.json"
        format_comparison_markdown = post_dir / "FINAL-FORMAT-COMPARISON.md"
        acceptance_out = post_dir / "post-render-acceptance.json"
        manifest_path = work / "pipeline-manifest.json"
        try:
            pipeline_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            style_record = ((pipeline_manifest.get("inputs") or {}).get("style_template")
                            if isinstance(pipeline_manifest, dict) else None)
            official_template = Path(str(style_record.get("path"))) if isinstance(style_record, dict) else None
            if official_template is None or not official_template.is_file():
                raise ValueError("pipeline manifest does not contain a usable style-template path")
            post_cmd = post_render_command(
                case,
                source_docx=output,
                final_docx=final_docx,
                pdf=pdf,
                render_report=render_report,
                pre_validation=work / "application" / "validation-report.json",
                format_spec=execution_requirements / "format-spec.json",
                official_template=official_template,
                official_style_map=work / "style-map.json",
                generated_style_map=work / "application" / "style-map.json",
                submission_audit=submission_audit,
                format_comparison=format_comparison,
                format_comparison_markdown=format_comparison_markdown,
                acceptance_out=acceptance_out,
                thesis_profile=(work / "thesis-profile.json") if (work / "thesis-profile.json").is_file() else None,
                word_open_timeout=word_open_timeout,
                word_timeout=word_timeout,
            )
            post_result = run_command(post_cmd, label=f"[{case['id']}] Word render + final artifact acceptance")
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            post_result = {
                "returncode": 2,
                "elapsed_s": 0.0,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "command": [],
                "stdout_tail": "",
                "stderr_tail": f"{type(exc).__name__}: {exc}",
            }
        stages["post_render"] = post_result
        result = dict(post_result)
        try:
            acceptance_payload = json.loads(acceptance_out.read_text(encoding="utf-8")) if acceptance_out.is_file() else None
        except (OSError, json.JSONDecodeError):
            acceptance_payload = None
        attach_post_render_manifest(
            manifest_path,
            pre_render_docx=output,
            final_docx=final_docx,
            pdf=pdf,
            render_report=render_report,
            submission_audit=submission_audit,
            format_comparison=format_comparison,
            format_comparison_markdown=format_comparison_markdown,
            acceptance_out=acceptance_out,
            accepted=bool(acceptance_payload and acceptance_payload.get("status") == "accepted"),
        )
    result["stages"] = stages
    result["case_id"] = case["id"]
    result["analysis_mode"] = case["analysis_mode"]
    result["stage_paths"] = {
        "review_requirements": str(review_requirements.resolve()) if host_review else None,
        "execution_requirements": str(execution_requirements.resolve()),
    }
    result["fresh_run"] = extraction_report(
        work,
        requirements_dir=(execution_requirements if llm_case and auto_host_agent and
                           stages.get("host_agent", {}).get("returncode") == 0
                           else review_requirements if host_review else execution_requirements),
        host_review_dir=review_requirements if host_review else None,
    )
    result["extraction_thesis_profile"] = canonical_profile_report(work)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template-manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--build-dir", type=Path, help="new output directory; it must not already exist")
    parser.add_argument("--schools", nargs="*", help="optional case ids from the manifest")
    parser.add_argument(
        "--neutral-reference-docx", type=Path,
        help="optional neutral DOCX used for deterministic backend checks; it is still read fresh",
    )
    parser.add_argument(
        "--prepare-host-review", action="store_true",
        help="required for llm_primary cases; creates current evidence-bound packets only",
    )
    parser.add_argument(
        "--auto-host-agent", action="store_true",
        help="one command through the explicitly declared native host adapter",
    )
    parser.add_argument(
        "--host-runtime",
        help="expected native host runtime; must match THESIS_FORGE_HOST_RUNTIME",
    )
    parser.add_argument(
        "--allow-supported-subset", action="store_true",
        help="explicitly allow rule_only/known_template compatibility cases; default batches require llm_primary",
    )
    parser.add_argument("--host-agent-timeout", type=int, default=900,
                        help="per-chunk native Host Agent timeout in seconds (default: 900)")
    parser.add_argument("--host-agent-max-concurrency", type=int, default=4,
                        help="maximum number of independent Host Agent chunks in flight (default: 4)")
    parser.add_argument("--host-agent-max-attempts", type=int, default=2,
                        help="maximum attempts per Host Agent chunk before failing closed (default: 2)")
    parser.add_argument("--host-agent-model",
                        help="explicit OpenClaw provider/model route; rejected by the Codex adapter")
    parser.add_argument("--host-agent-parent-session-key",
                        help="exact parent session key to use for route inheritance; never auto-discovered")
    parser.add_argument("--host-agent-auth-env-only", action="store_true",
                        help="pass --auth-env-only to OpenClaw and ignore stored provider credentials")
    parser.add_argument("--host-agent-runner", choices=("exec", "gateway"), default="exec",
                        help="OpenClaw invocation path used by --auto-host-agent (default: exec)")
    parser.add_argument("--no-host-agent-model-inheritance", action="store_true",
                        help="disable parent inheritance only with an explicit --host-agent-model route")
    parser.add_argument("--host-review-chunk-size", type=int, default=20,
                        help="clauses per fresh Host Agent packet (default: 20)")
    parser.add_argument("--host-agent-id", default="main",
                        help="OpenClaw agent id used by --auto-host-agent (default: main)")
    parser.add_argument("--openclaw-bin",
                        help="optional openclaw executable used by --auto-host-agent")
    parser.add_argument("--openclaw-config", type=Path,
                        help="optional config file passed explicitly to `openclaw agent exec`")
    parser.add_argument("--codex-bin",
                        help="optional native codex executable used by --auto-host-agent")
    parser.add_argument("--codex-model", default=codex_adapter.DEFAULT_MODEL,
                        help=f"explicit native Codex model (default: {codex_adapter.DEFAULT_MODEL})")
    parser.add_argument("--word-open-timeout", type=int, default=45,
                        help="seconds to wait for Microsoft Word to activate the staged DOCX")
    parser.add_argument("--word-timeout", type=int, default=180,
                        help="maximum seconds for one Microsoft Word export")
    args = parser.parse_args(argv)
    try:
        source, cases, manifest_path = load_manifest(args.template_manifest)
    except ValueError as exc:
        parser.error(str(exc))
    requested = set(args.schools or [str(case["id"]) for case in cases])
    known = {str(case["id"]) for case in cases}
    unknown = sorted(requested - known)
    if unknown:
        parser.error("unknown manifest case ids: " + ", ".join(unknown))
    selected = select_cases(cases, list(requested))
    non_llm_cases = sorted(
        str(case["id"]) for case in selected if case["analysis_mode"] != "llm_primary"
    )
    if non_llm_cases and not args.allow_supported_subset:
        parser.error(
            "default fresh batches require analysis_mode=llm_primary for every case; "
            "use --allow-supported-subset only for explicit compatibility runs: "
            + ", ".join(non_llm_cases)
        )
    if args.prepare_host_review and args.auto_host_agent:
        parser.error("choose exactly one of --prepare-host-review or --auto-host-agent")
    if args.host_agent_timeout <= 0:
        parser.error("--host-agent-timeout must be a positive integer")
    if args.host_agent_max_concurrency <= 0:
        parser.error("--host-agent-max-concurrency must be a positive integer")
    if args.host_agent_max_attempts <= 0:
        parser.error("--host-agent-max-attempts must be a positive integer")
    if args.host_review_chunk_size <= 0:
        parser.error("--host-review-chunk-size must be a positive integer")
    if args.word_open_timeout <= 0:
        parser.error("--word-open-timeout must be a positive integer")
    if args.word_timeout <= 0:
        parser.error("--word-timeout must be a positive integer")
    if any(case["analysis_mode"] == "llm_primary" for case in selected) and not (
        args.prepare_host_review or args.auto_host_agent
    ):
        parser.error("fresh-only batch requires --prepare-host-review or --auto-host-agent for llm_primary cases")
    adapter_id: str | None = None
    if args.auto_host_agent:
        if args.no_host_agent_model_inheritance and not args.host_agent_model:
            parser.error("--no-host-agent-model-inheritance requires --host-agent-model")
        try:
            runtime = require_host_runtime(args.host_runtime)
            adapter_id = automatic_adapter_id(runtime)
        except HostRuntimeError as exc:
            parser.error(str(exc))
        if adapter_id == "codex":
            forbidden = []
            if args.host_agent_model:
                forbidden.append("--host-agent-model")
            if args.host_agent_parent_session_key:
                forbidden.append("--host-agent-parent-session-key")
            if args.host_agent_auth_env_only:
                forbidden.append("--host-agent-auth-env-only")
            if args.host_agent_runner != "exec":
                forbidden.append("--host-agent-runner")
            if args.no_host_agent_model_inheritance:
                forbidden.append("--no-host-agent-model-inheritance")
            if args.host_agent_id != "main":
                forbidden.append("--host-agent-id")
            if args.openclaw_bin:
                forbidden.append("--openclaw-bin")
            if args.openclaw_config:
                forbidden.append("--openclaw-config")
            if forbidden:
                parser.error(
                    "Codex native adapter does not accept OpenClaw-only options: "
                    + ", ".join(forbidden)
                )

    neutral_reference = None
    if args.neutral_reference_docx:
        neutral_reference = resolve_project_path(
            args.neutral_reference_docx, label="neutral_reference_docx"
        )

    now = datetime.now(timezone.utc)
    base = args.build_dir or Path("build") / f"fresh-batch-{now.strftime('%Y%m%d-%H%M%S') }"
    base = base if base.is_absolute() else ROOT / base
    if base.exists():
        parser.error(f"output directory already exists; choose a new run directory: {base}")
    base.mkdir(parents=True, exist_ok=False)

    results: dict[str, Any] = {}
    terminal_status = "completed"
    global_stop_reason: str | None = None

    def write_batch_results() -> None:
        payload = {
            "schema_version": "1.1",
            "build_dir": str(base.resolve()),
            "template_manifest": str(manifest_path.resolve()),
            "source_tex": str(source.resolve()),
            "started_at": now.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "fresh_run_policy": "current_input_only_new_run_no_reuse",
            "semantic_review_policy": (
                "full_llm_primary_default"
                if not args.allow_supported_subset
                else "explicit_supported_subset_opt_in"
            ),
            "allow_supported_subset": bool(args.allow_supported_subset),
            "canonical_case_order": [str(case["id"]) for case in selected],
            "terminal_status": terminal_status,
            "global_stop_reason": global_stop_reason,
            "cases": results,
        }
        (base / "run-results.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    for case in selected:
        try:
            result = run_case(
                base, source, case, prepare_host_review=args.prepare_host_review,
                auto_host_agent=args.auto_host_agent,
                host_agent_timeout=args.host_agent_timeout,
                host_agent_max_concurrency=args.host_agent_max_concurrency,
                host_agent_max_attempts=args.host_agent_max_attempts,
                host_agent_model=args.host_agent_model,
                host_agent_parent_session_key=args.host_agent_parent_session_key,
                host_agent_auth_env_only=args.host_agent_auth_env_only,
                host_agent_runner=args.host_agent_runner,
                host_runtime=args.host_runtime,
                host_adapter_id=adapter_id,
                inherit_parent_model=not args.no_host_agent_model_inheritance,
                host_review_chunk_size=args.host_review_chunk_size,
                host_agent_id=args.host_agent_id,
                openclaw_bin=args.openclaw_bin,
                openclaw_config=args.openclaw_config,
                codex_bin=args.codex_bin,
                codex_model=args.codex_model,
                neutral_reference_docx=neutral_reference,
                word_open_timeout=args.word_open_timeout,
                word_timeout=args.word_timeout,
            )
        except Exception as exc:  # keep each school independently auditable
            result = {
                "returncode": 2,
                "case_id": case["id"],
                "analysis_mode": case["analysis_mode"],
                "error": f"{type(exc).__name__}: {exc}",
                "fresh_run": {},
                "stages": {},
            }
        result["acceptance"] = case_acceptance(result)
        results[str(case["id"])] = result
        write_batch_results()
        fatal_reason = global_fatal_reason(result)
        if fatal_reason:
            terminal_status = "stopped_global_fatal"
            global_stop_reason = fatal_reason
            write_batch_results()
            break
        if result["acceptance"].get("accepted") is not True:
            terminal_status = "stopped_case_failure"
            blockers = result["acceptance"].get("blockers") or ["acceptance_failed"]
            global_stop_reason = f"{case['id']}: {', '.join(str(item) for item in blockers[:8])}"
            write_batch_results()
            break

    if terminal_status != "completed":
        print(f"\nBATCH STOPPED ({global_stop_reason}) -> {base}")
    else:
        print(f"\nALL DONE -> {base}")
    failed = False
    for case_id, result in results.items():
        code = result.get("returncode")
        fresh = result.get("fresh_run", {})
        acceptance = result.get("acceptance", {})
        print(f"  {case_id:8s} returncode={code} run_id={fresh.get('run_id')} "
              f"cache_reused={fresh.get('cache_reused')} "
              f"acceptance={acceptance.get('status')}")
        if acceptance.get("accepted") is not True:
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
