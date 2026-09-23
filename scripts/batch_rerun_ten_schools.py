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
from artifact_io import atomic_write_text  # noqa: E402
from host_adapters import codex as codex_adapter  # noqa: E402
from host_runtime import (  # noqa: E402
    HostRuntimeError,
    automatic_adapter_id,
    require_host_runtime,
)
from manual_review import HUMAN_MARKER_CATEGORIES  # noqa: E402
from pdf_visual_audit import audit_pdf  # noqa: E402
from manual_review_display import audit_manual_review_markers  # noqa: E402
from native_semantic_review import NativeSemanticReviewError, validate_response  # noqa: E402
from process_runner import run_process  # noqa: E402
from semantic_contract import sha256_json, strict_json_dumps, strict_json_loads  # noqa: E402
from thesis_format_pipeline import runtime_code_fingerprint  # noqa: E402


def resolve_project_path(value: str | Path, *, label: str) -> Path:
    path = Path(value)
    resolved = path.resolve() if path.is_absolute() else (ROOT / path).resolve()
    if not resolved.is_file():
        raise ValueError(f"{label} does not exist: {resolved}")
    return resolved


def native_semantic_review_integrity(
    review: Any, *, case_id: Any, run_id: Any, case_root: Path,
) -> bool:
    """Require semantic-review evidence to bind to this case and cover all checks."""
    if not isinstance(review, dict):
        return False
    if (
        review.get("schema_version") != "1.0"
        or review.get("protocol") != "native_semantic_content_review_v1"
        or review.get("case_id") != case_id
        or review.get("run_id") != run_id
    ):
        return False
    checks = review.get("checks")
    results = review.get("results")
    if not isinstance(checks, list) or not isinstance(results, list):
        return False
    if not checks:
        return review.get("status") == "not_required" and not results
    if review.get("status") != "completed":
        return False
    try:
        request_path = _artifact_path(review.get("request_path"), root=case_root)
        response_path = _artifact_path(review.get("response_path"), root=case_root)
        if not _path_within(request_path, case_root) or not _path_within(response_path, case_root):
            return False
        if request_path is None or response_path is None:
            return False
        request = strict_json_loads(request_path.read_text(encoding="utf-8"))
        response = strict_json_loads(response_path.read_text(encoding="utf-8"))
        if not isinstance(request, dict) or not isinstance(response, dict):
            return False
        request_sha256 = sha256_json(request)
        response_sha256 = hashlib.sha256(response_path.read_bytes()).hexdigest()
        if (
            request_sha256 != review.get("request_sha256")
            or response_sha256 != review.get("response_sha256")
            or request.get("case_id") != case_id
            or request.get("run_id") != run_id
            or request.get("checks") != checks
            or request.get("source_sha256") != review.get("source_sha256")
            or request.get("format_spec_sha256") != review.get("format_spec_sha256")
            or request.get("document_text_sha256") != review.get("document_text_sha256")
        ):
            return False
        document_text_sha256 = hashlib.sha256(strict_json_dumps(
            [(item["check_id"], item["document_text"]) for item in checks],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        if request.get("document_text_sha256") != document_text_sha256:
            return False
        validated_results = validate_response(response, checks)
        if validated_results != results:
            return False
    except (OSError, KeyError, TypeError, ValueError, NativeSemanticReviewError):
        return False
    return True


def load_manifest(path: Path) -> tuple[Path, list[dict[str, Any]], Path]:
    manifest_path = path.resolve() if path.is_absolute() else (ROOT / path).resolve()
    try:
        payload = strict_json_loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
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
        thesis_profile_value = raw.get("thesis_profile")
        thesis_profile = (
            resolve_project_path(thesis_profile_value, label=f"{case_id}.thesis_profile")
            if thesis_profile_value else None
        )
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
            "thesis_profile": thesis_profile,
            "template_boundary": template_boundary,
            "analysis_mode": mode,
        })
        seen.add(case_id)
    return source, cases, manifest_path


def run_command(cmd: list[str], *, label: str, timeout: int = 1800) -> dict[str, Any]:
    print(f"\n{'=' * 70}\n{label}\n{'=' * 70}", flush=True)
    started = time.time()
    result = run_process(cmd, cwd=ROOT, timeout=timeout)
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
        "timeout_s": timeout,
        "timed_out": result.returncode == 124 and "[process-timeout]" in (result.stderr or ""),
        "stdout_tail": result.stdout[-3000:],
        "stderr_tail": result.stderr[-3000:],
    }


def interrupted_case_result(case: dict[str, Any]) -> dict[str, Any]:
    """Create a truthful terminal record when Ctrl-C interrupts one case."""
    return {
        "returncode": 130,
        "case_id": case.get("id"),
        "analysis_mode": case.get("analysis_mode"),
        "status": "interrupted",
        "error": "KeyboardInterrupt: operator interrupted the batch",
        "current_stage_state": "unknown",
        "fresh_run": {},
        "stages": {},
        "acceptance": {
            "accepted": False,
            "status": "interrupted",
            "blockers": ["operator_interrupted; current stage outcome is unknown"],
        },
    }


def failed_case_result(case: dict[str, Any], error: BaseException) -> dict[str, Any]:
    """Record an exception without implying which stage completed."""
    return {
        "returncode": 2,
        "case_id": case.get("id"),
        "analysis_mode": case.get("analysis_mode"),
        "status": "failed",
        "terminal_status": "failed",
        "current_stage_state": "unknown",
        "error": f"{type(error).__name__}: {error}",
        "fresh_run": {},
        "stages": {},
    }


def pipeline_command(case: dict[str, Any], source: Path, work_dir: Path, output_docx: Path,
                     *, compliance_mode: str, prepare_host_review: bool,
                     output_policy: str = "review_draft",
                     neutral_reference_docx: Path | None = None,
                     llm_response: Path | None = None,
                     run_id: str | None = None,
                     requirements_dir: Path | None = None,
                     host_agent_audit: Path | None = None,
                     merge_receipt: Path | None = None,
                     host_review_chunk_size: int = 20,
                     semantic_review_runtime: str | None = None,
                     semantic_review_model: str | None = None) -> list[str]:
    command = [
        sys.executable, "scripts/thesis_format_pipeline.py",
        str(case["requirements"]), str(source), str(output_docx),
        "--work-dir", str(work_dir),
        "--analysis-mode", str(case["analysis_mode"]),
        "--compliance-mode", compliance_mode,
        "--output-policy", output_policy,
        "--host-review-chunk-size", str(host_review_chunk_size),
        "--case-id", str(case["id"]),
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
        # The full stage is the second half of one fresh run: preparation has
        # already created source-conversion/review artifacts in this work
        # directory.  The pipeline still performs its own run-id, request,
        # receipt, and input-hash checks; this flag only authorizes that
        # explicitly bound stage transition and must not be used for a
        # standalone compatibility run.
        command.append("--allow-existing-work")
        command.extend(["--llm-response", str(llm_response)])
    if run_id:
        command.extend(["--run-id", str(run_id)])
    if host_agent_audit:
        command.extend(["--host-agent-audit", str(host_agent_audit)])
    if merge_receipt:
        command.extend(["--merge-receipt", str(merge_receipt)])
    if case.get("template_profile") and not neutral_reference_docx:
        command.extend(["--template-profile", str(case["template_profile"])])
    if case.get("thesis_profile"):
        command.extend(["--thesis-profile", str(case["thesis_profile"])])
    if semantic_review_runtime and semantic_review_model:
        command.extend([
            "--semantic-review-runtime", semantic_review_runtime,
            "--semantic-review-model", semantic_review_model,
        ])
    return command


def post_render_command(
    case: dict[str, Any], *, source_docx: Path, final_docx: Path, pdf: Path,
    render_report: Path, visual_audit: Path, pre_validation: Path, format_spec: Path,
    official_template: Path, official_style_map: Path, generated_style_map: Path,
    submission_audit: Path, format_comparison: Path,
    format_comparison_markdown: Path, acceptance_out: Path,
    thesis_profile: Path | None = None,
    case_id: str | None = None,
    run_id: str | None = None,
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
        "--visual-audit", str(visual_audit),
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
    if case_id:
        command.extend(["--case-id", str(case_id)])
    if run_id:
        command.extend(["--run-id", str(run_id)])
    return command


def attach_post_render_manifest(
    manifest_path: Path, *, pre_render_docx: Path, final_docx: Path, pdf: Path,
    render_report: Path, visual_audit: Path, submission_audit: Path, format_comparison: Path,
    format_comparison_markdown: Path, acceptance_out: Path,
    accepted: bool, case_id: str | None = None, run_id: str | None = None,
) -> dict[str, Any]:
    """Record the two-stage artifact chain without rewriting pre-render receipts."""
    payload = strict_json_loads(manifest_path.read_text(encoding="utf-8"))
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
        "visual_audit": str(visual_audit.resolve()),
        "submission_audit": str(submission_audit.resolve()),
        "format_comparison": str(format_comparison.resolve()),
        "format_comparison_markdown": str(format_comparison_markdown.resolve()),
        "pre_render_receipts_remain_bound_to_pre_render_docx": True,
        "case_id": case_id,
        "run_id": run_id,
    }
    payload["post_render_identity"] = {"case_id": case_id, "run_id": run_id}
    if accepted:
        payload["output"] = str(final_docx.resolve())
        payload["format_comparison"] = str(format_comparison.resolve())
        payload["format_comparison_markdown"] = str(format_comparison_markdown.resolve())
        payload["submission_audit"] = str(submission_audit.resolve())
        payload["render_report"] = str(render_report.resolve())
        payload["rendered_pdf"] = str(pdf.resolve())
        payload["submission_ready"] = True
    atomic_write_text(
        manifest_path,
        strict_json_dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )
    return payload


def canonical_profile_report(work: Path) -> dict[str, Any] | None:
    path = work / "thesis-profile.json"
    if not path.exists():
        return None
    profile = strict_json_loads(path.read_text(encoding="utf-8"))
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
        data = strict_json_loads(extraction_manifest.read_text(encoding="utf-8"))
        report.update({
            "run_id": data.get("run_id"),
            "cache_reused": data.get("cache_reused"),
            "source_sha256": data.get("source_sha256"),
            "semantic_review_provenance": data.get("semantic_review_provenance"),
        })
    if format_spec.exists():
        spec = strict_json_loads(format_spec.read_text(encoding="utf-8"))
        registry = spec.get("resource_registry") if isinstance(spec, dict) else None
        report.update({
            "declarations_present": isinstance(spec, dict) and spec.get("declarations") is not None,
            "resource_registry_present": isinstance(registry, dict),
        })
    if pipeline_manifest.exists():
        data = strict_json_loads(pipeline_manifest.read_text(encoding="utf-8"))
        report["pipeline_status"] = data.get("status")
        report["host_agent_review_manifest"] = data.get("host_agent_review_manifest")
    return report


def _artifact_path(value: Any, *, root: Path) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _path_within(path: Path | None, root: Path) -> bool:
    """Return whether an artifact is physically inside the current case."""
    if path is None:
        return False
    resolved_root = root.resolve()
    resolved = path.resolve()
    return resolved != resolved_root and resolved_root in resolved.parents


def _read_artifact_json(value: Any, *, root: Path) -> tuple[Path | None, dict[str, Any] | None]:
    path = _artifact_path(value, root=root)
    if path is None or not path.is_file():
        return path, None
    try:
        payload = strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
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

    # Batch outputs have a canonical case boundary.  A manifest may contain
    # absolute paths, but acceptance must not allow it to point at an older
    # build, a shared worktree, or an arbitrary external file.
    case_root = manifest_path.parent.parent if manifest_path is not None else None
    if case_root is None:
        blockers.append("case_root_missing")
        return {"status": "blocked", "accepted": False, "blockers": sorted(set(blockers)), "checks": checks}
    checks["case_root"] = str(case_root.resolve())
    if not _path_within(case_root, root):
        blockers.append("case_root_outside_run_root")
    if manifest_path != (case_root / "work" / "pipeline-manifest.json").resolve():
        blockers.append("pipeline_manifest_path_not_canonical")

    def case_artifact(label: str, value: Any) -> Path | None:
        path = _artifact_path(value, root=root)
        if path is not None and not _path_within(path, case_root):
            blockers.append(f"{label}_outside_case")
            return None
        return path

    # Review drafts are an explicit, auditable intermediate product.  They may
    # contain red placeholders for facts or semantic choices that a person
    # must resolve, but they must still be bound to the fresh run and pass the
    # mechanical DOCX/package checks. Human-only open inputs may remain as
    # red markers, but any deterministic/capability failure blocks acceptance
    # and therefore stops a fail-fast school run.
    if manifest.get("output_policy") == "review_draft":
        checks["output_policy"] = "review_draft"
        if manifest.get("status") != "draft_manual_review":
            blockers.append(f"review_draft_status:{manifest.get('status')}")
        if manifest.get("execution_compliance_mode") != "supported_subset":
            blockers.append("review_draft_not_supported_subset_execution")
        if manifest.get("submission_ready") is True:
            blockers.append("review_draft_claims_submission_ready")
        if manifest.get("blocking_reasons"):
            blockers.append("review_draft_pipeline_blocking_reasons")

        expected_case_id = manifest.get("case_id")
        extraction = manifest.get("requirements_extraction")
        expected_run_id = extraction.get("run_id") if isinstance(extraction, dict) else None
        if not isinstance(expected_case_id, str) or not expected_case_id:
            blockers.append("case_id_missing")
        if not isinstance(expected_run_id, str) or not expected_run_id:
            blockers.append("pipeline_run_id_missing")

        manual_path = case_artifact("manual_review_items", manifest.get("manual_review_items"))
        manual_ledger = None
        if manual_path is None or not manual_path.is_file():
            blockers.append("manual_review_ledger_missing")
        else:
            try:
                payload = strict_json_loads(manual_path.read_text(encoding="utf-8"))
                manual_ledger = payload if isinstance(payload, dict) else None
            except (OSError, ValueError):
                manual_ledger = None
            if not isinstance(manual_ledger, dict):
                blockers.append("manual_review_ledger_invalid")
            else:
                if manual_ledger.get("schema_version") != "1.0":
                    blockers.append("manual_review_ledger_schema_mismatch")
                if manual_ledger.get("policy") != "review_draft_only":
                    blockers.append("manual_review_ledger_policy_mismatch")
                if manual_ledger.get("submission_ready") is not False:
                    blockers.append("manual_review_ledger_submission_flag_invalid")
                binding = manual_ledger.get("binding")
                if isinstance(binding, dict) and expected_run_id and binding.get("run_id") != expected_run_id:
                    blockers.append("manual_review_ledger_run_mismatch")
                items = manual_ledger.get("items")
                if not isinstance(items, list):
                    blockers.append("manual_review_ledger_items_invalid")
                else:
                    marker_ids = [
                        str(item.get("marker_id")) for item in items
                        if isinstance(item, dict) and item.get("marker_id")
                    ]
                    invalid_categories = sorted({
                        str(item.get("category") or "") for item in items
                        if not isinstance(item, dict)
                        or str(item.get("category") or "") not in HUMAN_MARKER_CATEGORIES
                    })
                    if len(marker_ids) != len(items) or len(set(marker_ids)) != len(marker_ids):
                        blockers.append("manual_review_ledger_marker_ids_invalid")
                    if invalid_categories:
                        blockers.append("manual_review_ledger_contains_technical_diagnostics")
                checks["manual_review"] = {
                    "path": str(manual_path),
                    "summary": manual_ledger.get("summary"),
                    "item_count": len(items) if isinstance(items, list) else 0,
                }

        markers_path = case_artifact(
            "manual_review_markers",
            str(case_root / "work" / "application" / "manual-review-markers.json"),
        )
        markers = None
        if markers_path is None or not markers_path.is_file():
            blockers.append("manual_review_markers_missing")
        else:
            try:
                payload = strict_json_loads(markers_path.read_text(encoding="utf-8"))
                markers = payload if isinstance(payload, dict) else None
            except (OSError, ValueError):
                markers = None
            if not isinstance(markers, dict) or markers.get("policy") != "review_draft":
                blockers.append("manual_review_markers_invalid")
            elif not isinstance(markers.get("markers"), list):
                blockers.append("manual_review_markers_invalid")
            checks["manual_review_markers"] = str(markers_path)

        capability_path = case_artifact("capability_preflight", manifest.get("capability_preflight"))
        capability = None
        if capability_path is None or not capability_path.is_file():
            blockers.append("capability_artifact_missing")
        else:
            try:
                payload = strict_json_loads(capability_path.read_text(encoding="utf-8"))
                capability = payload if isinstance(payload, dict) else None
            except (OSError, ValueError):
                capability = None
            if capability is None or capability.get("status") == "failed":
                blockers.append("capability_artifact_invalid")
            else:
                raw_findings = capability.get("findings", [])
                blocking_count = sum(
                    1 for item in raw_findings
                    if isinstance(item, dict) and item.get("blocking")
                ) if isinstance(raw_findings, list) else 0
                human_input_count = 0
                technical_blocking_count = 0
                if isinstance(raw_findings, list):
                    for item in raw_findings:
                        if not isinstance(item, dict) or not item.get("blocking"):
                            continue
                        categories = {
                            evidence.get("value")
                            for evidence in item.get("evidence", [])
                            if isinstance(evidence, dict)
                            and evidence.get("kind") == "category"
                        }
                        if categories and categories.issubset(HUMAN_MARKER_CATEGORIES):
                            human_input_count += 1
                        else:
                            technical_blocking_count += 1
                if technical_blocking_count:
                    blockers.append("capability_technical_blocking_findings")
                if (capability.get("status") == "blocked"
                        and (not human_input_count or blocking_count != human_input_count)):
                    blockers.append("capability_blocked_without_human_only_basis")
                marker_count = (
                    len(markers.get("markers", []))
                    if isinstance(markers, dict) and isinstance(markers.get("markers"), list)
                    else 0
                )
                ledger_count = (
                    len(manual_ledger.get("items", []))
                    if isinstance(manual_ledger, dict) and isinstance(manual_ledger.get("items"), list)
                    else 0
                )
                if marker_count != ledger_count:
                    blockers.append("manual_review_markers_do_not_cover_ledger")
                checks["capability_preflight"] = {
                    "path": str(capability_path),
                    "status": capability.get("status"),
                    "blocking_findings": blocking_count,
                    "human_marker_eligible_findings": human_input_count,
                    "technical_blocking_findings": technical_blocking_count,
                }

        output_path = case_artifact("output", manifest.get("output"))
        checks["output"] = str(output_path) if output_path else None
        if output_path is None or not output_path.is_file() or output_path.stat().st_size <= 0:
            blockers.append("generated_docx_missing")
        else:
            output_artifact = _inspect_docx_artifact(output_path)
            checks["output_artifact"] = output_artifact
            if not output_artifact.get("opc_package_valid"):
                blockers.append("generated_docx_not_valid_opc")
            if output_path != (case_root / "generated.docx").resolve():
                blockers.append("generated_output_path_not_canonical")
            if output_artifact.get("opc_package_valid") and isinstance(manual_ledger, dict):
                try:
                    marker_audit = audit_manual_review_markers(output_path, manual_ledger)
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    marker_audit = {"valid": False, "error": str(exc)}
                checks["serialized_manual_review_markers"] = marker_audit
                if marker_audit.get("valid") is not True:
                    blockers.append("manual_review_serialized_markers_invalid")

        validation_path = case_artifact("validation_report", manifest.get("validation_report"))
        validation = None
        if validation_path is None or not validation_path.is_file():
            blockers.append("validation_report_missing_or_invalid")
        else:
            try:
                payload = strict_json_loads(validation_path.read_text(encoding="utf-8"))
                validation = payload if isinstance(payload, dict) else None
            except (OSError, ValueError):
                validation = None
            if validation is None:
                blockers.append("validation_report_missing_or_invalid")
            else:
                if validation.get("review_draft_ready") is not True:
                    blockers.append("review_draft_not_ready_in_validation")
                if validation.get("diagnostic_draft_generated") is not True:
                    blockers.append("review_draft_not_generated_in_validation")
                validation_findings = validation.get("findings")
                technical_validation_ok = (
                    validation.get("valid") is True
                    and validation.get("format_ready") is True
                    and validation.get("diagnostic_draft_generated") is True
                    and validation.get("review_draft_package_valid") is True
                    and isinstance(validation_findings, list)
                    and not validation_findings
                )
                checks["technical_validation"] = technical_validation_ok
                if not technical_validation_ok:
                    blockers.append("review_draft_technical_validation_not_passed")
                semantic_review = validation.get("native_semantic_content_review")
                semantic_review_ok = native_semantic_review_integrity(
                    semantic_review,
                    case_id=manifest.get("case_id"),
                    run_id=fresh.get("run_id"),
                    case_root=case_root,
                )
                checks["native_semantic_content_review"] = {
                    "valid": semantic_review_ok,
                    "status": semantic_review.get("status")
                    if isinstance(semantic_review, dict) else None,
                    "check_count": len(semantic_review.get("checks", []))
                    if isinstance(semantic_review, dict)
                    and isinstance(semantic_review.get("checks"), list) else None,
                    "result_count": len(semantic_review.get("results", []))
                    if isinstance(semantic_review, dict)
                    and isinstance(semantic_review.get("results"), list) else None,
                }
                if not semantic_review_ok:
                    blockers.append("review_draft_semantic_review_invalid_or_unbound")
                review_evidence = (
                    validation.get("submission_audit", {}).get("evidence", {})
                    if isinstance(validation.get("submission_audit"), dict) else {}
                )
                if validation.get("review_draft_package_valid") is not True and review_evidence.get("opc_package_valid") is not True:
                    blockers.append("review_draft_docx_package_not_verified")
                receipt_audit = validation.get("property_receipt_audit")
                receipt_integrity_ok = False
                if isinstance(receipt_audit, dict):
                    receipts = receipt_audit.get("receipts")
                    expected_ids = receipt_audit.get("expected_receipt_ids")
                    receipt_ids = [
                        str(item.get("receipt_id")) for item in receipts
                        if isinstance(item, dict) and item.get("receipt_id")
                    ] if isinstance(receipts, list) else []
                    output_sha = hashlib.sha256(output_path.read_bytes()).hexdigest() if output_path else None
                    expected_ids_valid = (
                        isinstance(expected_ids, list)
                        and all(isinstance(value, str) and value for value in expected_ids)
                        and len(expected_ids) == len(set(expected_ids))
                    )
                    receipt_statuses = [
                        item.get("status") for item in receipts
                        if isinstance(item, dict)
                    ] if isinstance(receipts, list) else []
                    status_counts = {
                        "verified_count": receipt_statuses.count("verified"),
                        "failed_count": receipt_statuses.count("failed"),
                        "unverified_count": receipt_statuses.count("unverified"),
                    }
                    receipt_integrity_ok = (
                        isinstance(receipts, list)
                        and len(receipt_ids) == len(receipts)
                        and len(receipt_ids) == len(set(receipt_ids))
                        and expected_ids_valid
                        and set(receipt_ids) == set(expected_ids)
                        and len(receipt_ids) == len(expected_ids)
                        and receipt_audit.get("receipt_count") == len(receipts)
                        and all(item.get("status") == "verified" for item in receipts)
                        and all(item.get("serialized_docx_sha256") == output_sha for item in receipts)
                        and all(receipt_audit.get(key) == value for key, value in status_counts.items())
                        and receipt_audit.get("missing_count") == 0
                        and receipt_audit.get("unexpected_count") == 0
                        and receipt_audit.get("duplicate_count") == 0
                        and receipt_audit.get("valid") is True
                    )
                if not receipt_integrity_ok:
                    blockers.append("review_draft_property_receipts_not_verified")
                checks["property_receipts"] = {
                    "integrity_bound_to_output": receipt_integrity_ok,
                    "all_expected_receipts_verified": receipt_integrity_ok,
                    "verified": receipt_audit.get("verified_count") if isinstance(receipt_audit, dict) else None,
                    "failed": receipt_audit.get("failed_count") if isinstance(receipt_audit, dict) else None,
                    "unverified": receipt_audit.get("unverified_count") if isinstance(receipt_audit, dict) else None,
                }
                if validation.get("submission_ready") is True:
                    blockers.append("review_draft_validation_claims_submission_ready")
            checks["validation_report"] = str(validation_path)

        comparison_path = case_artifact(
            "format_comparison", manifest.get("format_comparison"),
        )
        comparison = None
        if comparison_path is None or not comparison_path.is_file():
            blockers.append("review_draft_comparison_missing")
        else:
            try:
                payload = strict_json_loads(comparison_path.read_text(encoding="utf-8"))
                comparison = payload if isinstance(payload, dict) else None
            except (OSError, ValueError):
                comparison = None
            if comparison is None or comparison.get("status") != "review_draft_pending":
                blockers.append("review_draft_comparison_status_invalid")

        if manifest.get("code_fingerprint") != runtime_code_fingerprint():
            blockers.append("code_runtime_fingerprint_mismatch")
        checks["diagnostic_draft_generated"] = manifest.get("diagnostic_draft_generated") is True
        if manifest.get("diagnostic_draft_generated") is not True:
            blockers.append("review_draft_not_generated_in_manifest")
        checks["review_draft_ready"] = manifest.get("review_draft_ready") is True
        if manifest.get("review_draft_ready") is not True:
            blockers.append("review_draft_not_ready")
        unique_blockers = sorted(set(blockers))
        return {
            "status": "accepted_review_draft" if not unique_blockers else "blocked",
            "accepted": not unique_blockers,
            "review_draft": True,
            "blockers": unique_blockers,
            "checks": checks,
        }

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

    post_acceptance_path = _artifact_path(manifest.get("post_render_acceptance"), root=root)
    post_acceptance = None
    if post_acceptance_path is not None and not _path_within(post_acceptance_path, case_root):
        blockers.append("post_render_acceptance_outside_case")
        post_acceptance_path = None
    elif post_acceptance_path is not None and post_acceptance_path.is_file():
        try:
            payload = strict_json_loads(post_acceptance_path.read_text(encoding="utf-8"))
            post_acceptance = payload if isinstance(payload, dict) else None
        except (OSError, ValueError):
            post_acceptance = None
    # Full compliance is never accepted on the basis of the pre-render DOCX.
    # The post-Word chain is a mandatory release gate, even when its manifest
    # is missing or malformed; otherwise a self-authored pipeline manifest can
    # silently downgrade a release to the weaker pre-render checks.
    requires_post_render = manifest.get("compliance_mode") == "full"
    post_render_active = requires_post_render or (
        post_acceptance is not None or manifest.get("post_render_status") is not None
    )
    checks["post_render_required"] = requires_post_render
    checks["post_render_acceptance"] = str(post_acceptance_path) if post_acceptance_path else None
    if requires_post_render and post_acceptance is None:
        blockers.append("post_render_acceptance_required_missing")
    if post_render_active and (post_acceptance is None or post_acceptance.get("status") != "accepted"):
        blockers.append("post_render_acceptance_not_passed")

    if post_render_active and isinstance(post_acceptance, dict):
        if post_acceptance.get("blockers"):
            blockers.append("post_render_acceptance_contains_blockers")
        for label, value in (
            ("post_render_acceptance", post_acceptance_path),
            ("post_render_docx", _artifact_path(post_acceptance.get("post_render_docx"), root=root)),
            ("rendered_pdf", _artifact_path(post_acceptance.get("rendered_pdf"), root=root)),
            ("render_report", _artifact_path(post_acceptance.get("render_report"), root=root)),
            ("submission_audit", _artifact_path(post_acceptance.get("submission_audit"), root=root)),
            ("format_comparison", _artifact_path(post_acceptance.get("format_comparison"), root=root)),
        ):
            if value is not None and not _path_within(value, case_root):
                blockers.append(f"{label}_outside_case")
        if not post_acceptance.get("pre_render_docx_sha256"):
            blockers.append("post_render_pre_render_hash_missing")
        if not post_acceptance.get("post_render_docx_sha256"):
            blockers.append("post_render_final_hash_missing")

    output_path = case_artifact("output", manifest.get("output"))
    checks["output"] = str(output_path) if output_path else None
    if output_path is None or not output_path.is_file() or output_path.stat().st_size <= 0:
        blockers.append("generated_docx_missing")
        output_artifact = None
    else:
        output_artifact = _inspect_docx_artifact(output_path)
        checks["output_artifact"] = output_artifact
        if not output_artifact.get("opc_package_valid"):
            blockers.append("generated_docx_not_valid_opc")
    expected_output = case_root / ("final-word.docx" if post_render_active else "generated.docx")
    if output_path is not None and output_path != expected_output.resolve():
        blockers.append("generated_output_path_not_canonical")

    pre_render_output_path = case_artifact("pre_render_output", manifest.get("pre_render_output"))
    if post_render_active:
        if pre_render_output_path is None or not pre_render_output_path.is_file():
            blockers.append("pre_render_docx_missing")
        else:
            pre_artifact = _inspect_docx_artifact(pre_render_output_path)
            checks["pre_render_output_artifact"] = pre_artifact
            if not pre_artifact.get("opc_package_valid"):
                blockers.append("pre_render_docx_not_valid_opc")
        if pre_render_output_path is not None and pre_render_output_path != (case_root / "generated.docx").resolve():
            blockers.append("pre_render_output_path_not_canonical")
        if post_acceptance is not None:
            if _artifact_path(post_acceptance.get("post_render_docx"), root=root) != output_path:
                blockers.append("post_render_acceptance_output_path_mismatch")
            if (output_artifact is not None
                    and post_acceptance.get("post_render_docx_sha256") != output_artifact.get("sha256")):
                blockers.append("post_render_acceptance_artifact_hash_mismatch")

    if post_render_active:
        post_manifest = manifest.get("post_word_render")
        if not isinstance(post_manifest, dict):
            blockers.append("post_word_render_manifest_missing")
        else:
            required_post_keys = (
                "pre_render_docx", "final_docx", "pdf", "render_report",
                "visual_audit", "submission_audit", "format_comparison",
                "format_comparison_markdown",
            )
            for key in required_post_keys:
                path = _artifact_path(post_manifest.get(key), root=root)
                if path is None or not path.is_file():
                    blockers.append(f"post_word_render_{key}_missing")
                elif not _path_within(path, case_root):
                    blockers.append(f"post_word_render_{key}_outside_case")
            if _artifact_path(post_manifest.get("final_docx"), root=root) != output_path:
                blockers.append("post_word_render_final_path_mismatch")
            if _artifact_path(post_manifest.get("pre_render_docx"), root=root) != pre_render_output_path:
                blockers.append("post_word_render_pre_render_path_mismatch")

    format_spec_path = case_artifact("format_spec", manifest.get("format_spec"))
    schema_path = format_spec_path.parent / "schema-validation.json" if format_spec_path else None
    schema = None
    if schema_path and schema_path.is_file():
        try:
            schema = strict_json_loads(schema_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            schema = None
    checks["schema_validation"] = str(schema_path) if schema_path else None
    if not isinstance(schema, dict) or schema.get("valid") is not True:
        blockers.append("schema_validation_not_passed")

    capability_path = case_artifact("capability_preflight", manifest.get("capability_preflight"))
    capability = None
    if capability_path is not None and capability_path.is_file():
        try:
            payload = strict_json_loads(capability_path.read_text(encoding="utf-8"))
            capability = payload if isinstance(payload, dict) else None
        except (OSError, ValueError):
            capability = None
    checks["capability_preflight"] = str(capability_path) if capability_path else None
    if capability is None or capability.get("status") in {"blocked", "failed"}:
        blockers.append("capability_artifact_not_passed")
    elif any(item.get("blocking") for item in capability.get("findings", []) if isinstance(item, dict)):
        blockers.append("capability_blocking_findings")

    # A report from another checkout or case must not be promoted merely
    # because its individual hashes look plausible.  Bind acceptance to the
    # exact runtime code inventory and the fresh case/run identity.
    if manifest.get("code_fingerprint") != runtime_code_fingerprint():
        blockers.append("code_runtime_fingerprint_mismatch")
    expected_case_id = manifest.get("case_id")
    extraction = manifest.get("requirements_extraction")
    expected_run_id = extraction.get("run_id") if isinstance(extraction, dict) else None
    if not isinstance(expected_case_id, str) or not expected_case_id:
        blockers.append("case_id_missing")
    if not isinstance(expected_run_id, str) or not expected_run_id:
        blockers.append("pipeline_run_id_missing")
    if post_acceptance is not None:
        if post_acceptance.get("case_id") != expected_case_id:
            blockers.append("post_render_case_id_mismatch")
        if post_acceptance.get("run_id") != expected_run_id:
            blockers.append("post_render_run_id_mismatch")

    validation_path = case_artifact("validation_report", manifest.get("validation_report"))
    validation = None
    if validation_path is not None and validation_path.is_file():
        try:
            payload = strict_json_loads(validation_path.read_text(encoding="utf-8"))
            validation = payload if isinstance(payload, dict) else None
        except (OSError, ValueError):
            validation = None
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

    comparison_path = case_artifact("format_comparison", manifest.get("format_comparison"))
    comparison = None
    if comparison_path is not None and comparison_path.is_file():
        try:
            payload = strict_json_loads(comparison_path.read_text(encoding="utf-8"))
            comparison = payload if isinstance(payload, dict) else None
        except (OSError, ValueError):
            comparison = None
    checks["format_comparison"] = str(comparison_path) if comparison_path else None
    if comparison is None or comparison.get("status") != "passed":
        blockers.append("post_generation_comparison_not_passed")

    if post_render_active:
        post_pdf = case_artifact("post_render_pdf", (manifest.get("post_word_render") or {}).get("pdf"))
        if post_pdf is None or not post_pdf.is_file() or post_pdf.stat().st_size <= 0:
            blockers.append("post_render_pdf_missing")
        else:
            checks["post_render_pdf_sha256"] = hashlib.sha256(post_pdf.read_bytes()).hexdigest()
        post_manifest = manifest.get("post_word_render") if isinstance(manifest.get("post_word_render"), dict) else {}
        render_path = case_artifact("post_render_report", post_manifest.get("render_report"))
        render = None
        if render_path and render_path.is_file():
            try:
                render = strict_json_loads(render_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                render = None
        if not isinstance(render, dict):
            blockers.append("post_render_report_missing_or_invalid")
        else:
            final_hash = output_artifact.get("sha256") if output_artifact else None
            pdf_hash = checks.get("post_render_pdf_sha256")
            source_record = render.get("source_docx") if isinstance(render.get("source_docx"), dict) else {}
            pdf_record = render.get("rendered_pdf") if isinstance(render.get("rendered_pdf"), dict) else {}
            if _artifact_path(source_record.get("path"), root=root) != output_path:
                blockers.append("post_render_report_final_docx_path_mismatch")
            if _artifact_path(pdf_record.get("path"), root=root) != post_pdf:
                blockers.append("post_render_report_pdf_path_mismatch")
            if source_record.get("sha256") != final_hash:
                blockers.append("post_render_report_final_docx_hash_mismatch")
            if pdf_record.get("sha256") != pdf_hash:
                blockers.append("post_render_report_pdf_hash_mismatch")
            if expected_case_id := manifest.get("case_id"):
                if render.get("case_id") != expected_case_id:
                    blockers.append("post_render_report_case_id_mismatch")
            if expected_run_id := ((manifest.get("requirements_extraction") or {}).get("run_id")
                                   if isinstance(manifest.get("requirements_extraction"), dict) else None):
                if render.get("run_id") != expected_run_id:
                    blockers.append("post_render_report_run_id_mismatch")
        visual_path = case_artifact("post_render_visual_audit", post_manifest.get("visual_audit"))
        visual = None
        if visual_path and visual_path.is_file():
            try:
                visual = strict_json_loads(visual_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                visual = None
        if not isinstance(visual, dict) or visual.get("status") != "passed":
            blockers.append("post_render_pdf_visual_audit_not_passed")
        elif ((visual.get("pdf") or {}).get("sha256")
              != checks.get("post_render_pdf_sha256")):
            blockers.append("post_render_pdf_visual_audit_hash_mismatch")
        # Do not trust a report that was produced by the same post-render
        # command merely because its status and hash look correct.  Re-run the
        # PDF parser/raster sanity gate from this independent acceptance
        # process and bind the result to the exact bytes observed above.
        independent_visual_path = case_root / "post-render-independent-pdf-visual-audit.json"
        if post_pdf is None or not post_pdf.is_file() or post_pdf.stat().st_size <= 0:
            independent_visual = {
                "status": "blocked",
                "blockers": ["independent_pdf_visual_audit_input_missing"],
            }
        else:
            try:
                independent_visual = audit_pdf(post_pdf, output=independent_visual_path)
            except (OSError, ValueError, RuntimeError, TypeError, AttributeError) as exc:
                independent_visual = {
                    "status": "blocked",
                    "blockers": [f"independent_pdf_visual_audit_failed:{type(exc).__name__}"],
                }
        checks["independent_pdf_visual_audit"] = independent_visual
        if not isinstance(independent_visual, dict) or independent_visual.get("status") != "passed":
            blockers.append("independent_pdf_visual_audit_not_passed")
        elif ((independent_visual.get("pdf") or {}).get("sha256")
              != checks.get("post_render_pdf_sha256")):
            blockers.append("independent_pdf_visual_audit_hash_mismatch")
        audit_path = case_artifact("post_render_submission_audit", post_manifest.get("submission_audit"))
        audit = None
        if audit_path and audit_path.is_file():
            try:
                audit = strict_json_loads(audit_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                audit = None
        if not isinstance(audit, dict) or audit.get("submission_ready") is not True:
            blockers.append("post_render_submission_not_ready")
        else:
            if _artifact_path(audit.get("artifact"), root=root) != output_path:
                blockers.append("post_render_submission_artifact_path_mismatch")
            render_validation = audit.get("render_validation")
            if not isinstance(render_validation, dict) or render_validation.get("rendered_verified") is not True:
                blockers.append("post_render_trusted_render_not_verified")
            else:
                render_evidence = render_validation.get("evidence") if isinstance(render_validation.get("evidence"), dict) else {}
                if render_evidence.get("source_docx_sha256") != (output_artifact or {}).get("sha256"):
                    blockers.append("post_render_submission_docx_hash_mismatch")
                rendered_pdf = render_evidence.get("rendered_pdf") if isinstance(render_evidence.get("rendered_pdf"), dict) else {}
                if rendered_pdf.get("sha256") != checks.get("post_render_pdf_sha256"):
                    blockers.append("post_render_submission_pdf_hash_mismatch")
        if isinstance(comparison, dict):
            comparison_inputs = comparison.get("inputs") if isinstance(comparison.get("inputs"), dict) else {}
            if comparison_inputs.get("generated_docx_sha256") != (output_artifact or {}).get("sha256"):
                blockers.append("post_render_comparison_artifact_hash_mismatch")
            if _artifact_path(comparison_inputs.get("generated_docx"), root=root) != output_path:
                blockers.append("post_render_comparison_docx_path_mismatch")
            if _artifact_path(comparison_inputs.get("format_spec"), root=root) != format_spec_path:
                blockers.append("post_render_comparison_format_spec_path_mismatch")
            if post_manifest.get("requirements_only") or manifest.get("inputs", {}).get("baseline_authority") == "fallback_input_not_official":
                if comparison_inputs.get("official_template") not in (None, ""):
                    blockers.append("requirements_only_official_template_evidence_present")
                if comparison_inputs.get("official_template_sha256") not in (None, ""):
                    blockers.append("requirements_only_official_template_hash_present")
            else:
                style_record = (manifest.get("inputs") or {}).get("style_template")
                expected_style = _artifact_path(style_record.get("path"), root=root) if isinstance(style_record, dict) else None
                if _artifact_path(comparison_inputs.get("official_template"), root=root) != expected_style:
                    blockers.append("post_render_comparison_official_template_path_mismatch")
                if isinstance(style_record, dict) and comparison_inputs.get("official_template_sha256") != style_record.get("sha256"):
                    blockers.append("post_render_comparison_official_template_hash_mismatch")

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
             output_policy: str = "review_draft",
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
             codex_model: str | None = None,
             semantic_review_model: str | None = None,
             allow_prompt_only: bool = False,
             neutral_reference_docx: Path | None = None,
             word_open_timeout: int = 45,
             word_timeout: int = 180,
             stage_timeout: int = 1800) -> dict[str, Any]:
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
                         output_policy=output_policy,
                         prepare_host_review=host_review,
                         requirements_dir=(review_requirements if host_review else execution_requirements),
                         host_review_chunk_size=host_review_chunk_size,
                         neutral_reference_docx=neutral_reference_docx),
        label=(f"[{case['id']}] fresh extraction + host-agent packet"
               if host_review else
               f"[{case['id']}] fresh deterministic extraction ({case['analysis_mode']})"),
        timeout=stage_timeout,
    )
    # Keep the final case summary separate from each stage record.  Otherwise
    # attaching ``stages`` below makes the active stage dict contain itself and
    # ``run-results.json`` cannot be serialized.
    result = dict(prepare_result)
    stages: dict[str, Any] = {"prepare": prepare_result}
    if llm_case and auto_host_agent and prepare_result["returncode"] == 0:
        extraction_manifest = review_requirements / "extraction-manifest.json"
        try:
            extraction = strict_json_loads(extraction_manifest.read_text(encoding="utf-8"))
            run_id = extraction["run_id"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
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
                if codex_model:
                    bridge_command.extend(["--codex-model", codex_model])
                if allow_prompt_only:
                    bridge_command.append("--allow-prompt-only")
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
                timeout=stage_timeout,
            )
        stages["host_agent"] = host_result
        result = dict(host_result)
        if host_result["returncode"] == 0:
            full_result = run_command(
                pipeline_command(
                    case, source, work, output,
                    compliance_mode=compliance_mode,
                    output_policy=output_policy,
                    prepare_host_review=False,
                    requirements_dir=execution_requirements,
                    neutral_reference_docx=neutral_reference_docx,
                    llm_response=response_out,
                    run_id=str(run_id),
                    host_agent_audit=review_requirements / "host-agent-run.json",
                    merge_receipt=review_requirements / "merge-receipt.json",
                    host_review_chunk_size=host_review_chunk_size,
                    semantic_review_runtime=host_runtime,
                    semantic_review_model=(semantic_review_model or (
                        codex_model if host_adapter_id == "codex" else host_agent_model
                    )),
                ),
                label=f"[{case['id']}] full deterministic DOCX + declaration resources",
                timeout=stage_timeout,
            )
            stages["full"] = full_result
            result = dict(full_result)

    # A generated DOCX is not a release artifact until Microsoft Word has
    # produced a separate final DOCX/PDF pair and both final outputs have been
    # audited.  Preparation-only runs have no output and therefore stop before
    # this stage.  The post-render helper keeps pre-render receipts immutable.
    if result.get("returncode") == 0 and output.is_file() and output_policy == "submission":
        post_dir = work / "application"
        final_docx = case_dir / "final-word.docx"
        pdf = case_dir / "final.pdf"
        render_report = post_dir / "word-render-report.json"
        visual_audit = post_dir / "pdf-visual-audit.json"
        submission_audit = post_dir / "final-submission-audit.json"
        format_comparison = post_dir / "final-format-comparison.json"
        format_comparison_markdown = post_dir / "FINAL-FORMAT-COMPARISON.md"
        acceptance_out = post_dir / "post-render-acceptance.json"
        manifest_path = work / "pipeline-manifest.json"
        try:
            pipeline_manifest = strict_json_loads(manifest_path.read_text(encoding="utf-8"))
            extraction = pipeline_manifest.get("requirements_extraction")
            case_run_id = extraction.get("run_id") if isinstance(extraction, dict) else None
            if not isinstance(case_run_id, str) or not case_run_id:
                raise ValueError("pipeline manifest does not contain a fresh requirements run_id")
            pipeline_manifest["case_id"] = case["id"]
            pipeline_manifest["case_run_id"] = case_run_id
            atomic_write_text(
                manifest_path,
                strict_json_dumps(pipeline_manifest, ensure_ascii=False, indent=2) + "\n",
            )
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
                visual_audit=visual_audit,
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
                case_id=case["id"],
                run_id=case_run_id,
                word_open_timeout=word_open_timeout,
                word_timeout=word_timeout,
            )
            post_result = run_command(
                post_cmd,
                label=f"[{case['id']}] Word render + final artifact acceptance",
                timeout=stage_timeout,
            )
        except (OSError, TypeError, ValueError) as exc:
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
            acceptance_payload = strict_json_loads(acceptance_out.read_text(encoding="utf-8")) if acceptance_out.is_file() else None
        except (OSError, ValueError):
            acceptance_payload = None
        attach_post_render_manifest(
            manifest_path,
            pre_render_docx=output,
            final_docx=final_docx,
            pdf=pdf,
            render_report=render_report,
            visual_audit=visual_audit,
            submission_audit=submission_audit,
            format_comparison=format_comparison,
            format_comparison_markdown=format_comparison_markdown,
            acceptance_out=acceptance_out,
            accepted=bool(acceptance_payload and acceptance_payload.get("status") == "accepted"),
            case_id=case["id"],
            run_id=(
                acceptance_payload.get("run_id")
                if isinstance(acceptance_payload, dict)
                else None
            ),
        )
    elif result.get("returncode") == 0 and output.is_file() and output_policy == "review_draft":
        # Review drafts intentionally stop before Word/PDF release evidence.
        # Record the policy decision in the same run manifest so acceptance
        # cannot confuse “not run by draft policy” with an interrupted render.
        manifest_path = work / "pipeline-manifest.json"
        try:
            pipeline_manifest = strict_json_loads(manifest_path.read_text(encoding="utf-8"))
            pipeline_manifest["post_render_status"] = "not_run_review_draft"
            pipeline_manifest["post_render_policy"] = (
                "Word/PDF release rendering requires output-policy=submission "
                "after manual review markers are resolved"
            )
            atomic_write_text(
                manifest_path,
                strict_json_dumps(pipeline_manifest, ensure_ascii=False, indent=2) + "\n",
            )
            stages["post_render"] = {
                "returncode": 0,
                "status": "not_run_review_draft",
                "command": [],
                "reason": pipeline_manifest["post_render_policy"],
            }
        except (OSError, ValueError, TypeError):
            stages["post_render"] = {
                "returncode": 2,
                "status": "manifest_update_failed",
                "command": [],
            }
            result = {"returncode": 2, "stderr_tail": "cannot record review-draft post-render policy"}
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
    parser.add_argument(
        "--output-policy", choices=("review_draft", "submission"), default="review_draft",
        help=(
            "review_draft emits accepted red-marked DOCX drafts and continues past manual "
            "review items; submission runs the strict Word/PDF release gate"
        ),
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
    parser.add_argument("--codex-model",
                        help="optional explicit native Codex model; omitted means the current Codex CLI configuration")
    parser.add_argument("--semantic-review-model",
                        help="explicit model route for the post-format semantic review; defaults to the selected host model")
    parser.add_argument(
        "--allow-prompt-only", action="store_true",
        help="explicit non-release override when native Codex lacks --output-schema",
    )
    parser.add_argument("--word-open-timeout", type=int, default=45,
                        help="seconds to wait for Microsoft Word to activate the staged DOCX")
    parser.add_argument("--word-timeout", type=int, default=180,
                        help="maximum seconds for one Microsoft Word export")
    parser.add_argument("--stage-timeout", type=int, default=1800,
                        help="hard timeout for each pipeline/bridge/render subprocess")
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
    if args.stage_timeout <= 0:
        parser.error("--stage-timeout must be a positive integer")
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
            "output_policy": args.output_policy,
            "canonical_case_order": [str(case["id"]) for case in selected],
            "unstarted_case_ids": [
                str(case["id"]) for case in selected if str(case["id"]) not in results
            ],
            "terminal_status": terminal_status,
            "global_stop_reason": global_stop_reason,
            "cases": results,
        }
        atomic_write_text(
            base / "run-results.json",
            strict_json_dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )

    for case in selected:
        try:
            result = run_case(
                base, source, case, prepare_host_review=args.prepare_host_review,
                output_policy=args.output_policy,
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
                semantic_review_model=args.semantic_review_model,
                allow_prompt_only=args.allow_prompt_only,
                neutral_reference_docx=neutral_reference,
                word_open_timeout=args.word_open_timeout,
                word_timeout=args.word_timeout,
                stage_timeout=args.stage_timeout,
            )
        except KeyboardInterrupt:
            result = interrupted_case_result(case)
            results[str(case["id"])] = result
            terminal_status = "interrupted"
            global_stop_reason = (
                f"{case['id']}: operator interruption; current stage outcome is unknown"
            )
            write_batch_results()
            break
        except Exception as exc:  # keep each school independently auditable
            result = failed_case_result(case, exc)
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
