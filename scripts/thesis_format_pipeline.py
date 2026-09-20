#!/usr/bin/env python3
"""Run the auditable LaTeX/DOCX requirement-to-format pipeline as one safe command."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from compliance import report as compliance_report
from docx import Document
from format_spec_validation import load_and_validate
from host_review_contract import SUPPORTED_HOST_REVIEW_CONTRACTS, HOST_REVIEW_CONTRACT_V3
from pipeline_finding import evidence, finding
from region_graph import compile_region_graph
from section_model import compile_section_plan
from semantic_contract import sha256_json
from semantic_issue_confirmation import (
    bind_confirmations,
    build_confirmation_receipt,
    confirmed_clause_ids,
)
from artifact_io import atomic_write_text, paths_alias
from process_runner import run_process

ROOT = Path(__file__).resolve().parents[1]


def _runtime_code_files() -> list[Path]:
    """Return the tracked/runtime inputs whose drift invalidates a stage.

    Generated ``build`` artifacts and tests are deliberately excluded.  The
    fingerprint covers the executable pipeline, schemas, capability registry,
    skill contract, conversion wrapper, and CI/dependency declarations that
    define the meaning of an existing run.
    """
    candidates: list[Path] = []
    for relative in ("SKILL.md", "Makefile", "pytest.ini", "convert.sh"):
        path = ROOT / relative
        if path.is_file():
            candidates.append(path)
    for relative in ("requirements.txt", "requirements-test.txt", "pyproject.toml"):
        path = ROOT / relative
        if path.is_file():
            candidates.append(path)
    for directory in ("scripts", "schema", "resources", ".github/workflows"):
        root = ROOT / directory
        if not root.is_dir():
            continue
        candidates.extend(
            path for path in root.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix not in {".pyc", ".tmp"}
        )
    return sorted(set(candidates), key=lambda path: path.relative_to(ROOT).as_posix())


def runtime_code_fingerprint() -> dict[str, Any]:
    """Compute a deterministic code/runtime fingerprint for stage binding."""
    records: list[dict[str, Any]] = []
    digest = hashlib.sha256()
    for path in _runtime_code_files():
        relative = path.relative_to(ROOT).as_posix()
        record = file_record(path)
        records.append({"path": relative, "bytes": record["bytes"], "sha256": record["sha256"]})
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(record["sha256"].encode("ascii"))
        digest.update(b"\n")
    return {
        "algorithm": "sha256(path\\0file_sha256\\n)",
        "sha256": digest.hexdigest(),
        "files": records,
    }


def validate_semantic_review_configuration(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Fail before conversion when the requested compliance contract is impossible.

    Full compliance is a claim about every freshly extracted clause.  It must
    therefore never silently fall back to the deterministic baseline.  The
    baseline modes remain available for explicit supported-subset development
    and compatibility runs only.
    """
    if args.prepare_host_review and args.llm_response:
        parser.error("choose exactly one semantic-review stage: --prepare-host-review or --llm-response")
    if bool(args.host_agent_audit) != bool(args.merge_receipt):
        parser.error("--host-agent-audit and --merge-receipt must be supplied together")
    if (args.host_agent_audit or args.merge_receipt) and not args.llm_response:
        parser.error("host-agent receipts require --llm-response")
    if (
        args.compliance_mode == "full"
        and args.llm_response
        and not (args.host_agent_audit and args.merge_receipt)
        and not args.allow_offline_review
    ):
        parser.error(
            "full compliance requires a host-agent audit and merge receipt bound to the "
            "current response; use --allow-offline-review only for an explicit non-release test"
        )
    if args.compliance_mode == "full" and args.analysis_mode != "llm_primary":
        parser.error(
            "--compliance-mode full requires --analysis-mode llm_primary; "
            "rule_only/known_template are supported-subset development modes"
        )
    if args.compliance_mode == "full" and not (args.prepare_host_review or args.llm_response):
        parser.error(
            "full compliance requires a fresh complete host-Agent clause review: "
            "first run --prepare-host-review, or supply --llm-response"
        )
    if args.prepare_host_review and args.analysis_mode != "llm_primary":
        parser.error("--prepare-host-review requires --analysis-mode llm_primary")
    if args.strict_release:
        if args.compliance_mode != "full":
            parser.error("--strict-release requires --compliance-mode full")
        if not args.template_profile:
            parser.error("--strict-release requires --template-profile")
        if not args.render_report:
            parser.error("--strict-release requires accepted --render-report evidence")
        if not args.require_submission_ready:
            parser.error("--strict-release requires --require-submission-ready")
        if args.allow_unresolved or args.preview_placeholders:
            parser.error("--strict-release cannot use unresolved or preview bypasses")
        if args.allow_offline_review:
            parser.error("--strict-release cannot use --allow-offline-review")
        if not args.prepare_host_review and not (args.host_agent_audit and args.merge_receipt):
            parser.error(
                "--strict-release requires host-agent-run.json and merge-receipt.json "
                "bound to the supplied response"
            )
    if args.llm_response:
        if not args.llm_response.is_file():
            parser.error(f"LLM response does not exist: {args.llm_response}")
        try:
            response = json.loads(args.llm_response.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            parser.error(f"cannot read LLM response JSON: {exc}")
        complete = (
            isinstance(response, dict)
            and response.get("contract_version") in SUPPORTED_HOST_REVIEW_CONTRACTS
            and isinstance(response.get("requirements"), list)
            and isinstance(response.get("clause_reviews"), list)
        )
        if args.compliance_mode == "full" and not complete:
            parser.error(
                "full compliance requires a complete host review contract "
                f"({', '.join(sorted(SUPPORTED_HOST_REVIEW_CONTRACTS))})"
            )


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
    )


def file_record(path: Path) -> dict[str, Any]:
    """Return an auditable identity record for an input or generated artifact."""
    resolved = path.resolve()
    digest = hashlib.sha256()
    with resolved.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"path": str(resolved), "bytes": resolved.stat().st_size, "sha256": digest.hexdigest()}


def validate_explicit_thesis_profile(
    profile: Any, *, source_record: dict[str, Any], profile_path: Path,
) -> list[str]:
    """Validate a user-supplied profile without inferring missing semantics.

    A profile may resolve fields that the source extractor cannot determine,
    but it must still be a complete schema object, explicitly confirmed, and
    bound to the exact current source bytes.  This keeps a profile an
    auditable input rather than an untracked override or a model guess.
    """
    errors = load_and_validate(profile, ROOT / "schema" / "thesis-profile.schema.json")
    if errors:
        return errors
    if not isinstance(profile, dict):
        return ["explicit thesis profile must be a JSON object"]
    provenance = profile.get("provenance")
    if not isinstance(provenance, dict):
        return ["explicit thesis profile requires provenance bound to the current source"]
    if provenance.get("source_sha256") != source_record.get("sha256"):
        return [
            "explicit thesis profile provenance.source_sha256 does not match "
            f"the current source ({profile_path.resolve()})"
        ]
    trust = provenance.get("trust")
    if not isinstance(trust, dict) or trust.get("confirmed") is not True:
        return ["explicit thesis profile provenance.trust.confirmed must be true"]
    if trust.get("source") != "user_confirmed":
        return [
            "explicit thesis profile provenance.trust.source must be user_confirmed; "
            "source-derived metadata cannot silently become a user override"
        ]
    return []


def _path_under(path: Path, root: Path, *, label: str) -> Path:
    resolved = path.expanduser().resolve()
    root = root.resolve()
    if resolved == root or root not in resolved.parents:
        raise ValueError(f"{label} must remain inside the work directory: {resolved}")
    return resolved


def validate_host_review_receipts(
    *, response_path: Path, audit_path: Path, receipt_path: Path,
    extraction_manifest: dict[str, Any], work: Path,
) -> dict[str, Any]:
    """Bind the final deterministic stage to the immutable host review."""
    response_path = _path_under(response_path, work, label="host response")
    audit_path = _path_under(audit_path, work, label="host audit")
    receipt_path = _path_under(receipt_path, work, label="merge receipt")
    response = read_json(response_path)
    audit = read_json(audit_path)
    receipt = read_json(receipt_path)
    response_contract_version = response.get("contract_version") if isinstance(response, dict) else None
    if response_contract_version not in SUPPORTED_HOST_REVIEW_CONTRACTS:
        raise ValueError(
            "host response has unsupported contract_version: "
            f"{response_contract_version!r}"
        )
    expected_run_id = extraction_manifest.get("run_id")
    # ``llm_request_sha256`` is the legacy name for the semantic request-body
    # hash. Prefer the explicit field, but retain a read-only compatibility
    # fallback for manifests produced before the hash domains were separated.
    expected_request_body_sha = (
        extraction_manifest.get("llm_request_body_sha256")
        or extraction_manifest.get("llm_request_sha256")
    )
    expected_request_envelope_sha = extraction_manifest.get(
        "llm_request_envelope_sha256"
    )
    expected_request_file_sha = extraction_manifest.get("llm_request_file_sha256")
    expected_runtime_context = extraction_manifest.get("runtime_context")
    if not expected_run_id or not expected_request_body_sha:
        raise ValueError("fresh extraction manifest is missing run_id or request body hash")
    if receipt.get("status") != "merged" or receipt.get("protocol") != "host_agent_semantic_review":
        raise ValueError("merge receipt is not a successful host-agent semantic-review receipt")
    if receipt.get("run_id") != expected_run_id:
        raise ValueError("merge receipt run_id does not match the fresh extraction")
    if receipt.get("request_body_sha256", receipt.get("request_sha256")) != expected_request_body_sha:
        raise ValueError("merge receipt request body hash does not match the fresh request")
    if expected_request_envelope_sha and receipt.get("request_envelope_sha256") != expected_request_envelope_sha:
        raise ValueError("merge receipt request envelope hash does not match the fresh request")
    if expected_request_file_sha and receipt.get("request_file_sha256") != expected_request_file_sha:
        raise ValueError("merge receipt request file hash does not match the fresh request")
    if receipt.get("aggregate_sha256") != sha256_json(response):
        raise ValueError("merge receipt aggregate_sha256 does not match the response")
    if expected_runtime_context is not None and receipt.get("runtime_context") != expected_runtime_context:
        raise ValueError("merge receipt runtime context does not match the fresh request")
    merged_response_path = receipt.get("merged_response_path")
    if merged_response_path and Path(str(merged_response_path)).resolve() != response_path:
        raise ValueError("merge receipt response path does not match --llm-response")
    if audit.get("status") != "merged" or audit.get("run_id") != expected_run_id:
        raise ValueError("host-agent audit is not a successful record for the fresh run")
    if audit.get("response_contract_version") != response_contract_version:
        raise ValueError("host-agent audit response contract does not match the response")
    if audit.get("adapter_id") == "codex" and audit.get("structured_output_mode") != "native_schema":
        raise ValueError(
            "release host-agent audit must prove native Codex structured output"
        )
    if expected_runtime_context is not None and audit.get("runtime_context") != expected_runtime_context:
        raise ValueError("host-agent audit runtime context does not match the fresh request")
    if audit.get("response_path") and Path(str(audit["response_path"])).resolve() != response_path:
        raise ValueError("host-agent audit response path does not match --llm-response")
    merge = audit.get("merge") if isinstance(audit.get("merge"), dict) else {}
    if merge.get("aggregate_sha256") != receipt.get("aggregate_sha256"):
        raise ValueError("host-agent audit merge hash does not match the merge receipt")
    if merge.get("request_body_sha256", merge.get("request_sha256")) != expected_request_body_sha:
        raise ValueError("host-agent audit request body hash does not match the fresh request")
    if expected_request_envelope_sha and merge.get("request_envelope_sha256") != expected_request_envelope_sha:
        raise ValueError("host-agent audit request envelope hash does not match the fresh request")
    if expected_request_file_sha and merge.get("request_file_sha256") != expected_request_file_sha:
        raise ValueError("host-agent audit request file hash does not match the fresh request")
    if expected_runtime_context is not None and merge.get("runtime_context") != expected_runtime_context:
        raise ValueError("host-agent merge runtime context does not match the fresh request")
    if merge.get("merge_receipt_path") and Path(str(merge["merge_receipt_path"])).resolve() != receipt_path:
        raise ValueError("host-agent audit receipt path does not match the supplied receipt")
    ledger_path_value = receipt.get("semantic_review_ledger_path")
    ledger_sha = receipt.get("semantic_review_ledger_sha256")
    if response_contract_version == HOST_REVIEW_CONTRACT_V3:
        if not ledger_path_value or not ledger_sha:
            raise ValueError("contract 3.0 receipt must include the semantic review ledger")
        ledger_path = _path_under(
            Path(str(ledger_path_value)), work, label="semantic review ledger"
        )
        if not ledger_path.is_file():
            raise ValueError("semantic review ledger does not exist")
        ledger = read_json(ledger_path)
        if sha256_json(ledger) != ledger_sha:
            raise ValueError("semantic review ledger hash does not match the receipt")
        if ledger.get("response_sha256") != receipt.get("aggregate_sha256"):
            raise ValueError("semantic review ledger is not bound to the merged response")
        if merge.get("semantic_review_ledger_sha256") != ledger_sha:
            raise ValueError("host-agent audit ledger hash does not match the receipt")
    return {
        "response": file_record(response_path),
        "host_agent_audit": file_record(audit_path),
        "merge_receipt": file_record(receipt_path),
        "aggregate_sha256": receipt.get("aggregate_sha256"),
        "request_sha256": expected_request_body_sha,
        "request_body_sha256": expected_request_body_sha,
        "request_envelope_sha256": expected_request_envelope_sha,
        "request_file_sha256": expected_request_file_sha,
        "run_id": expected_run_id,
        "runtime_context": expected_runtime_context,
        "contract_version": response_contract_version,
        "semantic_review_ledger": (
            {"path": str(ledger_path), "sha256": ledger_sha}
            if response_contract_version == HOST_REVIEW_CONTRACT_V3 else None
        ),
    }


def requirements_normalization_errors(
    normalization: dict[str, Any], original_record: dict[str, Any], original_suffix: str,
) -> list[str]:
    """Verify the deterministic adapter record before trusting extraction output."""
    errors: list[str] = []
    original = normalization.get("original") if isinstance(normalization.get("original"), dict) else {}
    normalized = normalization.get("normalized") if isinstance(normalization.get("normalized"), dict) else {}
    if normalization.get("status") != "completed":
        errors.append("normalization_not_completed")
    if normalization.get("artifact_reused") is not False:
        errors.append("normalized_artifact_reuse_not_disproven")
    if original.get("path") != original_record.get("path"):
        errors.append("normalization_original_path_mismatch")
    if original.get("sha256") != original_record.get("sha256"):
        errors.append("normalization_original_sha256_mismatch")
    if original.get("bytes") != original_record.get("bytes"):
        errors.append("normalization_original_size_mismatch")
    expected_status = "converted" if original_suffix == ".doc" else "not_required"
    if normalization.get("conversion_status") != expected_status:
        errors.append("normalization_conversion_status_mismatch")
    if normalized.get("suffix") != ".docx" or normalized.get("kind") != "docx":
        errors.append("normalized_artifact_is_not_docx")
    normalized_path = Path(str(normalized.get("path") or ""))
    if not normalized_path.is_file():
        errors.append("normalized_artifact_missing")
    else:
        current = file_record(normalized_path)
        if current.get("sha256") != normalized.get("sha256"):
            errors.append("normalized_artifact_sha256_mismatch")
        if current.get("bytes") != normalized.get("bytes"):
            errors.append("normalized_artifact_size_mismatch")
    if original_suffix == ".doc" and normalized_path.resolve() == Path(original_record["path"]).resolve():
        errors.append("legacy_source_was_not_isolated")
    validation = normalization.get("validation")
    if not isinstance(validation, dict) or validation.get("status") != "valid":
        errors.append("normalized_docx_validation_missing")
    converter = normalization.get("converter")
    if original_suffix == ".doc" and (
        not isinstance(converter, dict)
        or not converter.get("tool")
        or not converter.get("version")
        or not isinstance(converter.get("command"), list)
    ):
        errors.append("converter_provenance_incomplete")
    return errors


def run_step(
    name: str,
    command: list[str],
    steps: list[dict[str, Any]],
    *,
    timeout: int = 1800,
) -> subprocess.CompletedProcess[str]:
    result = run_process(command, cwd=ROOT, timeout=timeout)
    steps.append({"name": name, "command": command, "returncode": result.returncode,
                  "stdout": result.stdout.strip(), "stderr": result.stderr.strip()})
    return result


def requirement_blockers(
    spec: dict[str, Any], questions: list[Any], compliance_mode: str = "full",
    satisfied_clause_ids: set[str] | None = None,
    confirmed_semantic_issue_ids: set[str] | None = None,
) -> list[str]:
    """Return conditions that make the selected compliance mode unsafe."""
    satisfied_clause_ids = satisfied_clause_ids or set()
    confirmed_semantic_issue_ids = confirmed_semantic_issue_ids or set()
    analysis_relief_ids = (
        confirmed_semantic_issue_ids if compliance_mode == "supported_subset" else set()
    )
    excluded_ids = satisfied_clause_ids | analysis_relief_ids
    completeness = spec.get("completeness") if isinstance(spec.get("completeness"), dict) else {}
    blockers = []
    unresolved_ids = set(completeness.get("unresolved_clause_ids") or []) - excluded_ids
    if spec.get("status") == "needs_clarification" and (
        compliance_mode == "full" or unresolved_ids
    ):
        blockers.append("status_needs_clarification")
    if unresolved_ids: blockers.append("unresolved_clauses")
    if completeness.get("missing_clause_ids"): blockers.append("missing_clauses")
    if spec.get("blocking_errors"): blockers.append("blocking_errors")
    if questions:
        remaining_questions = [item for item in questions
                               if isinstance(item, dict)
                               and str(item.get("clause_id")) not in excluded_ids]
        if remaining_questions or not all(isinstance(item, dict) for item in questions):
            blockers.append("open_questions")
    if compliance_mode == "full" and confirmed_semantic_issue_ids:
        blockers.append("confirmed_semantic_issues")
    records = spec.get("clause_compliance") if isinstance(spec.get("clause_compliance"), list) else []
    records = [item for item in records
               if str(item.get("clause_id")) not in excluded_ids]
    if compliance_mode == "full":
        if not records:
            blockers.append("missing_clause_compliance")
        elif not compliance_report(records, "full", "analysis")["execution_ready"]:
            blockers.append("full_compliance_analysis_failed")
        if spec.get("semantic_review_provenance_valid") is not True:
            blockers.append("semantic_review_provenance_invalid")
    return blockers


PREVIEW_PLACEHOLDER_BLOCKERS = {
    "status_needs_clarification",
    "unresolved_clauses",
    "open_questions",
}


def capability_gate_blocked(report: dict[str, Any], compliance_mode: str) -> bool:
    """Fail closed only in full mode; subset mode must retain explicit gaps."""
    return compliance_mode == "full" and any(
        item.get("blocking") for item in report.get("findings", []))


def record_capability_summary(manifest: dict[str, Any], summary: dict[str, Any]) -> None:
    """Expose separate issue, clause, and requirement counts.

    ``capability_input_prerequisites`` historically meant clause-level gaps,
    so retain that field and add explicit totals rather than silently changing
    its meaning in old manifests.
    """
    manifest["capability_gaps"] = summary.get("gaps", 0)
    manifest["capability_backend_gaps"] = summary.get(
        "clause_backend_capability_gaps", summary.get("backend_capability_gaps", 0))
    manifest["capability_input_prerequisites"] = summary.get(
        "clause_input_prerequisites", summary.get("input_prerequisites", 0))
    manifest["capability_runtime_manual_unverifiable"] = summary.get(
        "clause_runtime_manual_unverifiable", summary.get("runtime_manual_unverifiable", 0))
    manifest["capability_confirmed_semantic_issues"] = summary.get(
        "confirmed_semantic_issues", 0)
    manifest["capability_external_not_applicable"] = summary.get(
        "clause_external_not_applicable", summary.get("external_not_applicable", 0))
    manifest["capability_clause_gaps"] = summary.get("clause_gaps", summary.get("gaps", 0))
    manifest["capability_requirement_gaps"] = summary.get("requirement_gaps", 0)
    manifest["capability_input_prerequisites_total"] = summary.get(
        "input_prerequisites", 0)
    manifest["capability_clause_input_prerequisites"] = summary.get(
        "clause_input_prerequisites", 0)
    manifest["capability_requirement_input_prerequisites"] = summary.get(
        "requirement_input_prerequisites", 0)


def official_template_from_profile(profile_path: Path) -> Path | None:
    """Resolve the official DOCX resource declared by a template profile."""
    profile = read_json(profile_path)
    for resource in profile.get("resources", []):
        if resource.get("kind") == "official_docx" and resource.get("path"):
            return (profile_path.resolve().parent / resource["path"]).resolve()
    return None


def merge_official_page_baseline(spec: dict[str, Any], official_docx: Path) -> list[str]:
    """Fill unspecified executable page properties from the official DOCX.

    The post-generation audit has always treated the official template as the
    fallback authority when a written rule is silent.  Applying the same
    fallback before generation prevents a guaranteed compare-time failure.
    Explicit written values remain untouched.
    """
    doc = Document(official_docx)
    if not doc.sections:
        return []
    section = doc.sections[0]
    page = spec.setdefault("page", {})
    added: list[str] = []
    if "size" not in page and abs(section.page_width.mm - 210) < 1 and abs(section.page_height.mm - 297) < 1:
        page["size"] = "A4"; added.append("size")
    if "orientation" not in page:
        page["orientation"] = "landscape" if section.page_width > section.page_height else "portrait"
        added.append("orientation")
    margins = page.setdefault("margins_pt", {})
    for key, value in {
        "top": section.top_margin, "bottom": section.bottom_margin,
        "left": section.left_margin, "right": section.right_margin,
        "header": section.header_distance, "footer": section.footer_distance,
        "gutter": section.gutter,
    }.items():
        if key not in margins and value is not None:
            margins[key] = round(value.pt, 3); added.append(f"margins_pt.{key}")
    page_number = page.get("page_number")
    if page_number is None:
        # Silence in the extracted requirements is not evidence that the
        # official template has no pagination.  Preserve its serialized
        # pgNumType/footer PAGE contract unless a written page-number rule is
        # explicitly supplied.
        page["page_number"] = {"preserve_existing_locations": True}
        added.append("page_number.preserve_existing_locations")
    elif isinstance(page_number, dict):
        explicit_formats = {page_number.get("front_matter_format"), page_number.get("body_format")} - {None}
        if not explicit_formats and not page_number.get("preserve_existing_locations"):
            page_number["preserve_existing_locations"] = True
            added.append("page_number.preserve_existing_locations")
    return added


def ensure_declared_cover(spec: dict[str, Any], clauses: list[dict[str, Any]]) -> bool:
    """Normalize an already explicit cover contract without inferring one.

    Natural-language keywords cannot establish that a cover exists, which
    fields it contains, or where those fields belong.  Those decisions belong
    to the host semantic review and its evidence-bound response.  This helper
    therefore only normalizes the placement of a cover object that the
    accepted contract already contains; an omitted cover remains omitted and
    is handled by the normal completeness/capability gates.
    """
    existing = spec.get("cover")
    if not isinstance(existing, dict):
        return False
    if existing.get("before_role") == "document_start":
        return False
    existing["before_role"] = "document_start"
    return True


def ensure_preview_cover_placeholders(spec: dict[str, Any], manifest: dict[str, Any]) -> list[str]:
    """Add explicitly declared cover roles that need a preview placeholder."""
    roles = spec.get("roles") if isinstance(spec.get("roles"), dict) else {}
    cover = spec.get("cover") if isinstance(spec.get("cover"), dict) else None
    if cover is None or "thesis_title_en" not in roles:
        return []
    fields = cover.get("fields") if isinstance(cover.get("fields"), list) else []
    existing = {str(item.get("id")) for item in fields if isinstance(item, dict)}
    if "title_en" in existing:
        return []
    next_order = max((int(item.get("order", 0)) for item in fields
                      if isinstance(item, dict) and isinstance(item.get("order"), int)), default=0) + 1
    fields.append({
        "id": "title_en",
        "label": "英文题目",
        "value_from": "thesis_profile.cover_metadata.title_en",
        "display_policy": "required",
        "order": next_order,
    })
    cover["fields"] = fields
    manifest.setdefault("preview_added_cover_fields", []).append("title_en")
    return ["title_en"]


def ensure_preview_page_number_selector(spec: dict[str, Any], manifest: dict[str, Any]) -> bool:
    """Use the synthetic thesis's explicit chapter-one boundary in preview mode."""
    page = spec.get("page") if isinstance(spec.get("page"), dict) else None
    rule = page.get("page_number") if page and isinstance(page.get("page_number"), dict) else None
    if rule is None:
        return False
    front = rule.get("front_matter_format")
    body = rule.get("body_format")
    if front is None or body is None or front == body or rule.get("body_start_selector"):
        return False
    rule["body_start_selector"] = {
        "strategy": "heading_text",
        "heading_text_pattern": r"^第\s*1\s*章",
        "evidence_ids": [],
    }
    manifest["preview_inferred_page_number_selector"] = {
        "strategy": "heading_text",
        "heading_text_pattern": r"^第\s*1\s*章",
        "reason": "synthetic thesis has an explicit chapter-one body boundary",
    }
    manifest.setdefault("preview_bypassed_section_plan_findings", []).append("body_selector_required")
    return True


def compile_preflight_plans(spec: dict[str, Any], effective_input: Path,
                            template_profile_path: Path | None,
                            section_plan_path: Path,
                            assembly_plan_path: Path) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Compile read-only structural contracts before any style or DOCX mutation."""
    section_plan = compile_section_plan(spec, effective_input)
    write_json(section_plan_path, section_plan)
    section_errors = load_and_validate(section_plan, ROOT / "schema" / "section-plan.schema.json")
    if section_errors:
        raise ValueError("invalid compiled SectionPlan:\n" + "\n".join(section_errors))

    assembly_result = None
    if template_profile_path:
        profile = read_json(template_profile_path)
        if "regions" in profile:
            assembly_result = compile_region_graph(
                profile, template_path=official_template_from_profile(template_profile_path))
            write_json(assembly_plan_path, assembly_result)
        else:
            ordered_roles = profile.get("structure", {}).get("ordered_roles", [])
            required_roles = [item.get("role") for item in ordered_roles
                              if isinstance(item, dict) and item.get("required")]
            official_template = official_template_from_profile(template_profile_path)
            input_is_official = bool(
                official_template and effective_input.resolve() == official_template.resolve()
            )
            if required_roles and not input_is_official:
                issue = finding(
                    "assembly.contract_missing", "structural_preflight", "error", True,
                    "The template profile requires ordered structural roles, but declares no executable region graph for assembling a non-template source.",
                    [evidence("profile_id", profile.get("profile_id")),
                     evidence("required_roles", required_roles),
                     evidence("effective_input", str(effective_input.resolve())),
                     evidence("official_template", str(official_template) if official_template else None)],
                )
                assembly_result = {
                    "schema_version": "1.0",
                    "profile_id": profile.get("profile_id"),
                    "graph_id": None,
                    "status": "invalid",
                    "template_path": str(official_template) if official_template else None,
                    "assembly_plan": None,
                    "findings": [issue],
                }
                write_json(assembly_plan_path, assembly_result)
    return section_plan, assembly_result


def _main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "requirements", type=Path,
        help="school requirements Word file (.doc is normalized automatically; .docx passes through)",
    )
    p.add_argument("input", type=Path, help="target thesis source (.tex for end-to-end conversion, or .docx for DOCX-stage use)")
    p.add_argument("output", type=Path, help="formatted output DOCX")
    p.add_argument("--work-dir", type=Path, required=True, help="directory for all auditable JSON products")
    p.add_argument("--allow-existing-work", action="store_true",
                   help="explicit compatibility override; otherwise a non-empty work directory is rejected")
    p.add_argument("--requirements-dir", type=Path,
                   help="stage-specific requirements artifact directory below --work-dir")
    p.add_argument("--style-template", type=Path,
                   help="official template DOCX used for pre-generation evidence/reconciliation and style-role analysis")
    p.add_argument("--neutral-reference-docx", type=Path,
                   help="school-neutral DOCX baseline for synthetic conversion/render validation; disables official template/profile checks")
    p.add_argument("--prepare-host-review", action="store_true",
                   help="prepare evidence-bound packets for the current host Agent; never calls a provider")
    p.add_argument("--host-review-chunk-size", type=int, default=20,
                   help="number of clauses per host-Agent packet (default: 20)")
    p.add_argument(
        "--llm-response", type=Path,
        help="offline complete host-review response produced by the native Host Agent",
    )
    p.add_argument("--host-agent-audit", type=Path,
                   help="immutable host-agent-run.json bound to --llm-response")
    p.add_argument("--merge-receipt", type=Path,
                   help="immutable merge-receipt.json bound to --llm-response")
    p.add_argument("--run-id", help="explicit current semantic-review run id when resuming a fresh host response")
    p.add_argument("--case-id", help="stable batch case identity bound into the host-review request")
    p.add_argument("--thesis-profile", type=Path, help="JSON metadata for conditional requirements such as master/doctor limits")
    p.add_argument("--analysis-mode", choices=["llm_primary", "rule_only", "known_template"], default="llm_primary",
                   help="unseen templates default to full LLM semantic extraction and completeness review")
    p.add_argument("--compliance-mode", choices=["full", "supported_subset"], default="full",
                   help="full blocks every applicable unsupported/unverifiable DOCX clause; supported_subset is compatibility/testing only")
    p.add_argument("--capability-registry", type=Path,
                   help="optional backend capability registry (defaults to the bundled python-docx/OOXML registry)")
    p.add_argument("--source-inventory", type=Path,
                   help="optional JSON source inventory used by conditional backend capabilities")
    p.add_argument("--allow-unresolved", action="store_true",
                   help="unsafe expert override: continue despite unresolved style-map questions; requirement questions still block")
    p.add_argument("--allow-offline-review", action="store_true",
                   help="explicit non-release test mode; permits full semantic compilation without a native call receipt")
    p.add_argument("--preview-placeholders", action="store_true",
                   help="opt-in supported-subset preview: continue past unresolved requirement semantics while keeping the artifact non-submission-ready")
    p.add_argument(
        "--semantic-issue-confirmations", type=Path,
        help=(
            "run-bound user acknowledgements for unresolved semantic clauses; "
            "supported_subset may continue, full compliance remains blocked"
        ),
    )
    p.add_argument("--render-report", type=Path,
                   help="optional accepted Microsoft Word/PDF render evidence JSON")
    p.add_argument("--template-profile", type=Path,
                   help="official template profile whose structural audit must pass")
    p.add_argument("--template-fixed-values", type=Path,
                   help="validated school/template fixed-value contract")
    p.add_argument("--template-school",
                   help="exact school key selecting one fixed-value contract entry")
    p.add_argument("--require-submission-ready", action="store_true",
                   help="fail closed unless serialized DOCX and render evidence both prove submission readiness")
    p.add_argument("--strict-release", action="store_true",
                   help="formal release gate: fresh bound review, official template profile, render evidence, and submission readiness")
    p.add_argument("--tex-overlay", type=Path,
                   help="optional convert.sh YAML overlay, valid only when input is .tex")
    p.add_argument("--pandoc-arg", action="append", default=[],
                   help="extra argument forwarded to convert.sh/Pandoc for .tex input; repeat as needed")
    args = p.parse_args(argv)
    if args.case_id is not None:
        args.case_id = args.case_id.strip()
        if not args.case_id or not all(
            character.isalnum() or character in ".-_" for character in args.case_id
        ):
            p.error("--case-id must contain only letters, digits, dot, underscore, or hyphen")
    validate_semantic_review_configuration(args, p)
    if args.host_review_chunk_size <= 0:
        p.error("--host-review-chunk-size must be a positive integer")
    if args.preview_placeholders and args.compliance_mode != "supported_subset":
        p.error("--preview-placeholders requires --compliance-mode supported_subset")
    if args.semantic_issue_confirmations and not args.semantic_issue_confirmations.is_file():
        p.error(f"semantic issue confirmation file does not exist: {args.semantic_issue_confirmations}")
    if bool(args.template_fixed_values) != bool(args.template_school):
        p.error("--template-fixed-values and --template-school must be supplied together")
    if args.neutral_reference_docx and (args.style_template or args.template_profile):
        p.error("--neutral-reference-docx cannot be combined with --style-template or --template-profile")
    if args.neutral_reference_docx and not args.neutral_reference_docx.is_file():
        p.error(f"neutral reference DOCX does not exist: {args.neutral_reference_docx}")
    if args.style_template and not args.style_template.is_file():
        p.error(f"style/official template DOCX does not exist: {args.style_template}")

    work = args.work_dir.resolve()
    current_code_fingerprint = runtime_code_fingerprint()
    requirements_dir = (args.requirements_dir.resolve() if args.requirements_dir
                        else work / "requirements")
    if requirements_dir == work or work not in requirements_dir.parents:
        p.error("--requirements-dir must be a child directory of --work-dir")
    style_map = work / "style-map.json"
    apply_dir = work / "application"; manifest_path = work / "pipeline-manifest.json"
    capability_report_path = work / "capability-preflight.json"
    metadata_path = work / "semantic-metadata.json"
    canonical_profile_path = work / "thesis-profile.json"
    section_plan_path = work / "section-plan.json"
    assembly_plan_path = work / "assembly-plan.json"
    assembly_report_path = work / "assembly-execution.json"
    source_role_map_path = work / "source-role-map.json"
    semantic_issue_ledger_path = work / "semantic-issue-ledger.json"
    semantic_issue_receipt_path = work / "semantic-issue-confirmation-receipt.json"
    requirements_source = args.requirements.resolve()
    requirements_suffix = requirements_source.suffix.lower()
    if not requirements_source.is_file():
        p.error(f"requirements input does not exist or is not a file: {requirements_source}")
    if requirements_suffix not in {".doc", ".docx"}:
        p.error("requirements input must be a .doc or .docx Word file")
    if requirements_source.is_relative_to(requirements_dir):
        p.error(
            "requirements input cannot be inside the stage requirements directory because that "
            "directory is rebuilt for every fresh extraction"
        )
    if work.exists():
        try:
            existing_entries = sorted(item.name for item in work.iterdir())
        except OSError as exc:
            p.error(f"cannot inspect work directory: {work}: {exc}")
        if existing_entries and not args.allow_existing_work:
            p.error(
                "refusing to reuse a non-empty work directory; start a fresh run directory "
                "or pass --allow-existing-work for an explicit compatibility run: "
                + ", ".join(existing_entries[:8])
            )
    work.mkdir(parents=True, exist_ok=True)
    source_input = args.input.resolve()
    source_suffix = source_input.suffix.lower()
    if source_suffix not in {".tex", ".docx"}:
        p.error("input must be a .tex or .docx file")
    if args.tex_overlay and source_suffix != ".tex":
        p.error("--tex-overlay is valid only with a .tex input")
    output_path = args.output.resolve()
    if paths_alias((requirements_source, source_input, output_path)):
        p.error("requirements, source input, and output DOCX must be filesystem-distinct paths")
    if output_path.exists():
        p.error(f"refusing to overwrite an existing output DOCX: {output_path}")

    prior_manifest: dict[str, Any] | None = None
    if args.allow_existing_work and manifest_path.is_file():
        try:
            candidate = read_json(manifest_path)
        except (OSError, json.JSONDecodeError) as exc:
            p.error(f"cannot read existing pipeline manifest for a compatibility stage: {exc}")
        if not isinstance(candidate, dict):
            p.error("existing pipeline manifest is not a JSON object; start a fresh run directory")
        prior_manifest = candidate
        prior_status = candidate.get("status")
        allowed_reuse_statuses = {"completed"} if not args.llm_response else {"host_review_required"}
        if prior_status not in allowed_reuse_statuses:
            p.error(
                "--allow-existing-work is limited to a completed supported-subset rebuild "
                "or the explicit host-review-to-execution transition; "
                f"existing manifest status is {prior_status!r}"
            )
        if args.compliance_mode == "full" and not args.llm_response:
            p.error(
                "--allow-existing-work cannot reuse a full-compliance work directory "
                "without a fresh bound --llm-response and host receipts"
            )
        if args.llm_response and args.analysis_mode != "llm_primary":
            p.error("a bound --llm-response continuation requires --analysis-mode llm_primary")
        prior_source = (candidate.get("inputs") or {}).get("source")
        current_source = file_record(source_input)
        if (
            not isinstance(prior_source, dict)
            or prior_source.get("path") != current_source.get("path")
            or prior_source.get("sha256") != current_source.get("sha256")
        ):
            p.error(
                "existing work directory is bound to a different source input; "
                "start a fresh run directory instead of mixing stages"
            )
        prior_code_fingerprint = candidate.get("code_fingerprint")
        if prior_code_fingerprint != current_code_fingerprint:
            p.error(
                "existing work directory was created by a different pipeline code/runtime "
                "fingerprint; start a fresh run directory instead of mixing stages"
            )
    elif args.allow_existing_work:
        p.error(
            "--allow-existing-work requires an existing pipeline-manifest.json; "
            "start a fresh work directory instead"
        )

    steps: list[dict[str, Any]] = []
    manifest: dict[str, Any] = {
        "schema_version": "1.1", "started_at": datetime.now(timezone.utc).isoformat(), "status": "running",
        "pipeline_level": "latex_end_to_end" if source_suffix == ".tex" else "docx_formatting_stage",
        "inputs": {
            "requirements": file_record(requirements_source),
            "requirements_kind": requirements_suffix.lstrip("."),
            "source": file_record(source_input),
            "source_kind": "latex" if source_suffix == ".tex" else "docx",
        },
        "output": str(args.output.resolve()), "work_dir": str(work), "steps": steps,
        "code_fingerprint": current_code_fingerprint,
        "case_id": args.case_id,
        "work_reuse_policy": (
            "explicit_allow_existing_work" if args.allow_existing_work
            else "new_directory_required"
        ),
        "preview_placeholders": bool(args.preview_placeholders),
        "preview_bypassed_requirement_blockers": [],
        "preview_bypassed_section_plan_findings": [],
        "preview_added_cover_fields": [],
    }
    # Persist a parseable running marker before any external converter or
    # metadata extractor starts.  The outer wrapper can then close an
    # unexpected exception or KeyboardInterrupt as a truthful terminal state
    # instead of leaving an untracked invocation.
    write_json(manifest_path, manifest)

    effective_input = source_input
    if source_suffix == ".tex":
        converted_dir = work / "source-conversion"
        converted_dir.mkdir(parents=True, exist_ok=True)
        effective_input = converted_dir / "source.docx"
        prior_intermediate = prior_manifest.get("intermediate_docx") if prior_manifest else None
        reusable_intermediate = (
            isinstance(prior_intermediate, dict)
            and Path(str(prior_intermediate.get("path") or "")).resolve() == effective_input.resolve()
            and effective_input.is_file()
        )
        if reusable_intermediate:
            current_intermediate = file_record(effective_input)
            reusable_intermediate = all(
                current_intermediate.get(key) == prior_intermediate.get(key)
                for key in ("path", "bytes", "sha256")
            )
        if args.allow_existing_work and effective_input.exists() and not reusable_intermediate:
            p.error(
                "existing intermediate DOCX is not provably bound to the current source; "
                "start a fresh run directory instead of overwriting it"
            )
        if reusable_intermediate:
            steps.append({
                "name": "latex_to_docx_reuse",
                "command": [],
                "returncode": 0,
                "reused": True,
                "artifact": file_record(effective_input),
            })
        else:
            convert_cmd = [str(ROOT / "convert.sh"), str(source_input), str(effective_input)]
            if args.tex_overlay:
                convert_cmd.append(str(args.tex_overlay.resolve()))
                manifest["inputs"]["tex_overlay"] = file_record(args.tex_overlay)
            convert_cmd.extend(args.pandoc_arg)
            result = run_step("latex_to_docx", convert_cmd, steps)
            if result.returncode or not effective_input.exists():
                manifest.update(status="failed", reason="LaTeX to DOCX conversion failed")
                write_json(manifest_path, manifest); print(json.dumps(manifest, ensure_ascii=False)); return result.returncode or 2
        manifest["intermediate_docx"] = file_record(effective_input)

    # Resolve thesis metadata before requirements extraction so the exact
    # confirmed profile is visible in the host-review request.  For TeX, the
    # extractor still runs to produce independent source evidence; an explicit
    # profile then replaces only the canonical profile artifact after its
    # schema, trust, and source-byte binding pass.
    canonical_profile: dict[str, Any] | None = None
    if source_suffix == ".tex":
        metadata_cmd = [
            sys.executable, str(ROOT / "scripts" / "extract_semantic_metadata.py"),
            str(source_input), str(metadata_path),
            "--thesis-profile-out", str(canonical_profile_path),
        ]
        metadata_result = run_step("semantic_metadata", metadata_cmd, steps)
        if metadata_result.returncode or not metadata_path.exists() or not canonical_profile_path.exists():
            manifest.update(status="failed", reason="semantic metadata extraction failed")
            write_json(manifest_path, manifest)
            print(json.dumps(manifest, ensure_ascii=False))
            return metadata_result.returncode or 2
        semantic_metadata = read_json(metadata_path)
        extracted_profile = read_json(canonical_profile_path)
        extracted_errors = load_and_validate(extracted_profile, ROOT / "schema" / "thesis-profile.schema.json")
        extracted_source_sha = (
            extracted_profile.get("provenance", {}).get("source_sha256")
            if isinstance(extracted_profile, dict) else None
        )
        if extracted_errors or extracted_source_sha != semantic_metadata.get("source_sha256"):
            manifest.update(
                status="failed",
                reason="canonical thesis profile validation failed",
                thesis_profile_errors=extracted_errors or ["canonical profile/source provenance mismatch"],
            )
            write_json(manifest_path, manifest)
            print(json.dumps(manifest, ensure_ascii=False))
            return 2
        canonical_profile = extracted_profile
        manifest["inputs"]["semantic_metadata"] = file_record(metadata_path)
        if args.thesis_profile:
            supplied_profile = read_json(args.thesis_profile)
            profile_errors = validate_explicit_thesis_profile(
                supplied_profile,
                source_record=manifest["inputs"]["source"],
                profile_path=args.thesis_profile,
            )
            if profile_errors:
                manifest.update(status="failed", reason="explicit thesis profile validation failed",
                                thesis_profile_errors=profile_errors)
                write_json(manifest_path, manifest)
                print(json.dumps(manifest, ensure_ascii=False))
                return 2
            canonical_profile = supplied_profile
            write_json(canonical_profile_path, canonical_profile)
            manifest["inputs"]["thesis_profile_source"] = file_record(args.thesis_profile)
        manifest["inputs"]["thesis_profile"] = file_record(canonical_profile_path)
    elif args.thesis_profile:
        supplied_profile = read_json(args.thesis_profile)
        profile_errors = validate_explicit_thesis_profile(
            supplied_profile,
            source_record=manifest["inputs"]["source"],
            profile_path=args.thesis_profile,
        )
        if profile_errors:
            manifest.update(status="failed", reason="explicit thesis profile validation failed",
                            thesis_profile_errors=profile_errors)
            write_json(manifest_path, manifest)
            print(json.dumps(manifest, ensure_ascii=False))
            return 2
        canonical_profile = supplied_profile
        write_json(canonical_profile_path, canonical_profile)
        manifest["inputs"]["semantic_metadata"] = file_record(args.thesis_profile)
        manifest["inputs"]["thesis_profile_source"] = file_record(args.thesis_profile)
        manifest["inputs"]["thesis_profile"] = file_record(canonical_profile_path)

    profile_official_template = official_template_from_profile(args.template_profile) if args.template_profile else None
    official_template_evidence = (
        args.style_template.resolve() if args.style_template else profile_official_template
    )
    # A neutral reference remains a generation/style-analysis baseline only;
    # it is never promoted to official-template evidence or authority.
    has_official_template = bool(official_template_evidence)
    official_template = (args.neutral_reference_docx.resolve() if args.neutral_reference_docx else
                         official_template_evidence or effective_input)
    manifest["inputs"].update({
        # Keep `input` for manifest consumers written against schema 1.0.
        "input": str(effective_input),
        "effective_docx": file_record(effective_input),
        "structure_source": file_record(effective_input),
        "requirements_source": file_record(requirements_source),
        "official_template_evidence": (
            file_record(official_template_evidence) if official_template_evidence else None
        ),
        "style_template": file_record(official_template),
        "official_template_source": ("style_template" if args.style_template else
                                     "template_profile" if profile_official_template else "not_supplied"),
        "formatting_baseline_source": ("neutral_reference_docx" if args.neutral_reference_docx else
                                       "official_template_evidence" if official_template_evidence else
                                       "target_input_fallback"),
        "baseline_is_official": bool(official_template_evidence),
        "baseline_authority": (
            "official_template_evidence" if official_template_evidence
            else "fallback_input_not_official"
        ),
    })
    if args.neutral_reference_docx:
        manifest["validation_mode"] = "neutral_reference"

    # A supplied school-requirements DOCX always starts a new extraction run.
    # The stage directory is an audit destination, never a format-spec cache.
    # requirements_engine removes only deterministic products and refuses to
    # rebuild over immutable Host Agent review artifacts.
    requirements_dir.mkdir(parents=True, exist_ok=True)
    manifest["requirements_extraction"] = {
        "policy": "fresh_required_docx_extraction",
        "input_normalization_policy": "fresh_per_invocation_no_cache",
        "cache_reused": False,
        "source_sha256": manifest["inputs"]["requirements"]["sha256"],
        "stage_directory": str(requirements_dir),
    }
    req_cmd = [sys.executable, str(ROOT / "scripts" / "requirements_engine.py"), str(requirements_source), "--out", str(requirements_dir),
               "--analysis-mode", args.analysis_mode, "--structure-docx", str(effective_input),
               "--host-review-chunk-size", str(args.host_review_chunk_size),
               "--code-fingerprint", current_code_fingerprint["sha256"]]
    if args.case_id:
        req_cmd += ["--case-id", args.case_id]
    if canonical_profile is not None:
        req_cmd += ["--thesis-profile", str(canonical_profile_path)]
    if official_template_evidence:
        req_cmd += ["--official-template-evidence-docx", str(official_template_evidence)]
    if args.compliance_mode == "full" or args.strict_release:
        req_cmd.append("--strict-provenance")
    if args.llm_response:
        req_cmd += ["--llm-response", str(args.llm_response)]
    if args.run_id:
        req_cmd += ["--run-id", args.run_id]
    if args.prepare_host_review:
        req_cmd.append("--prepare-host-review")
    result = run_step("requirements", req_cmd, steps)
    normalization_manifest_path = requirements_dir / "requirements-input-manifest.json"
    if normalization_manifest_path.is_file():
        manifest["requirements_input_normalization"] = read_json(normalization_manifest_path)
    if result.returncode:
        manifest.update(status="failed", reason="requirements analysis failed")
        write_json(manifest_path, manifest); print(json.dumps(manifest, ensure_ascii=False)); return result.returncode

    extraction_manifest = read_json(requirements_dir / "extraction-manifest.json")
    normalization = extraction_manifest.get("requirements_input_normalization")
    normalization_errors = requirements_normalization_errors(
        normalization if isinstance(normalization, dict) else {},
        manifest["inputs"]["requirements"],
        requirements_suffix,
    )
    if (extraction_manifest.get("cache_reused") is not False or
            extraction_manifest.get("source_sha256") != manifest["inputs"]["requirements"]["sha256"] or
            normalization_errors):
        manifest.update(status="failed", reason="fresh requirements extraction provenance mismatch")
        manifest["requirements_normalization_errors"] = normalization_errors
        write_json(manifest_path, manifest); print(json.dumps(manifest, ensure_ascii=False)); return 10
    if args.llm_response and args.host_agent_audit and args.merge_receipt:
        try:
            manifest["host_review_receipts"] = validate_host_review_receipts(
                response_path=args.llm_response,
                audit_path=args.host_agent_audit,
                receipt_path=args.merge_receipt,
                extraction_manifest=extraction_manifest,
                work=work,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            manifest.update(status="failed", reason="host-agent review receipt gate failed",
                            host_review_receipt_error=str(exc))
            write_json(manifest_path, manifest); print(json.dumps(manifest, ensure_ascii=False)); return 10
    manifest["requirements_input_normalization"] = normalization
    manifest["inputs"]["requirements_normalized"] = normalization["normalized"]
    manifest["requirements_extraction"].update({
        "run_id": extraction_manifest.get("run_id"),
        "completed_at": extraction_manifest.get("completed_at"),
        "clause_count": extraction_manifest.get("clause_count"),
        "manifest": str((requirements_dir / "extraction-manifest.json").resolve()),
        "evidence_context": str((requirements_dir / "evidence-context.json").resolve()),
        "semantic_review_provenance": extraction_manifest.get("semantic_review_provenance"),
        "normalized_source_sha256": extraction_manifest.get("normalized_source_sha256"),
        "input_normalization_manifest": extraction_manifest.get("requirements_input_manifest"),
        "sources": extraction_manifest.get("sources"),
        "template_evidence": extraction_manifest.get("template_evidence"),
        "template_reconciliation": extraction_manifest.get("template_reconciliation"),
        "runtime_context": extraction_manifest.get("runtime_context"),
    })
    manifest["template_evidence"] = extraction_manifest.get("template_evidence")
    manifest["template_reconciliation"] = extraction_manifest.get("template_reconciliation")

    if args.prepare_host_review:
        review_manifest_path = requirements_dir / "host-agent-review-manifest.json"
        manifest.update(
            status="host_review_required",
            reason=(
                "host Agent must generate the contract-3.0 response with its current runtime model"
            ),
            host_agent_review_manifest=str(review_manifest_path),
            host_agent_review_request=str(requirements_dir / "llm-request.json"),
        )
        write_json(manifest_path, manifest)
        print(json.dumps({"status": manifest["status"], "manifest": str(manifest_path),
                          "host_agent_review_manifest": str(review_manifest_path)}, ensure_ascii=False))
        return 0

    spec = read_json(requirements_dir / "format-spec.json")
    clause_document = read_json(requirements_dir / "requirement-clauses.json")
    clauses = clause_document if isinstance(clause_document, list) else clause_document.get("clauses", [])
    if ensure_declared_cover(spec, clauses):
        manifest["cover_contract_fallback"] = {
            "reason": "explicit cover clauses existed but semantic contract omitted top-level cover",
            "placeholder": spec["cover"]["missing_value_placeholder"],
            "field_count": len(spec["cover"]["fields"]),
        }
    if args.preview_placeholders:
        ensure_preview_cover_placeholders(spec, manifest)
        ensure_preview_page_number_selector(spec, manifest)
    if canonical_profile is not None:
        spec["thesis_profile"] = canonical_profile
        manifest["metadata_status"] = canonical_profile.get("metadata_status")
        manifest["metadata_pending_fields"] = canonical_profile.get("pending_fields", [])
    spec["compliance_mode"] = args.compliance_mode
    write_json(requirements_dir / "format-spec.json", spec)
    questions = read_json(requirements_dir / "questions.json")
    semantic_issue_ledger: dict[str, Any] | None = None
    confirmed_semantic_issue_ids: set[str] = set()
    if args.semantic_issue_confirmations:
        try:
            confirmation_input = read_json(args.semantic_issue_confirmations)
            expected_case_id = args.case_id or extraction_manifest.get("case_id")
            semantic_issue_ledger = bind_confirmations(
                confirmation_input,
                spec,
                questions,
                clauses,
                expected_case_id=str(expected_case_id) if expected_case_id else None,
                expected_run_id=str(spec.get("run_id") or "") or None,
            )
            write_json(semantic_issue_ledger_path, semantic_issue_ledger)
            semantic_issue_receipt = build_confirmation_receipt(semantic_issue_ledger)
            write_json(semantic_issue_receipt_path, semantic_issue_receipt)
            confirmed_semantic_issue_ids = confirmed_clause_ids(semantic_issue_ledger)
            manifest["inputs"]["semantic_issue_confirmations"] = file_record(
                args.semantic_issue_confirmations.resolve()
            )
            manifest["semantic_issue_ledger"] = file_record(semantic_issue_ledger_path)
            manifest["semantic_issue_receipt"] = file_record(semantic_issue_receipt_path)
            manifest["semantic_issue_binding"] = semantic_issue_ledger["binding"]
            manifest["semantic_issue_confirmations"] = semantic_issue_ledger["confirmations"]
            manifest["confirmed_semantic_issue_ids"] = sorted(confirmed_semantic_issue_ids)
            manifest["semantic_issue_scope"] = semantic_issue_ledger["scope"]
            manifest["semantic_issue_disposition"] = semantic_issue_ledger["disposition"]
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            manifest.update(
                status="failed",
                reason="semantic issue confirmation binding failed",
                semantic_issue_confirmation_error=str(exc),
            )
            write_json(manifest_path, manifest)
            print(json.dumps(manifest, ensure_ascii=False))
            return 3

    # Capability preflight, assembly metadata input, and official-template
    # audit all consume this exact validated work artifact.  Raw semantic JSON
    # remains available solely as provenance for .tex extraction.
    runtime_inventory_path = work / "runtime-input-inventory.json"
    runtime_context = extraction_manifest.get("runtime_context")
    runtime_inventory = (
        runtime_context.get("runtime_inventory")
        if isinstance(runtime_context, dict)
        else None
    )
    if isinstance(runtime_inventory, dict):
        write_json(runtime_inventory_path, runtime_inventory)
        manifest["inputs"]["runtime_inventory"] = file_record(runtime_inventory_path)
    capability_cmd = [
        sys.executable, str(ROOT / "scripts" / "capability_planner.py"),
        str(requirements_dir / "format-spec.json"), "--out", str(capability_report_path),
        "--compliance-mode", args.compliance_mode,
        "--clauses", str(requirements_dir / "requirement-clauses.json"),
    ]
    if args.capability_registry:
        capability_cmd += ["--registry", str(args.capability_registry.resolve())]
        manifest["inputs"]["capability_registry"] = file_record(args.capability_registry)
    source_inventory_path = args.source_inventory
    # A TeX source already produced a source-bound semantic metadata artifact
    # in this fresh run.  Use it as the default source inventory so registered
    # applicability facts (for example source_inventory.english_text) are
    # evaluated against the current source instead of becoming unknown merely
    # because the caller omitted a redundant CLI flag.  Explicit inventory
    # input remains authoritative when supplied.
    if source_inventory_path is None and metadata_path.is_file():
        source_inventory_path = metadata_path
    if source_inventory_path:
        capability_cmd += ["--source-inventory", str(source_inventory_path.resolve())]
        manifest["inputs"]["source_inventory"] = file_record(source_inventory_path)
    if args.template_profile and not args.neutral_reference_docx:
        capability_cmd += ["--template-profile", str(args.template_profile.resolve())]
    if args.template_fixed_values:
        capability_cmd += [
            "--template-fixed-values", str(args.template_fixed_values.resolve()),
            "--template-school", args.template_school,
        ]
        manifest["inputs"]["template_fixed_values"] = file_record(args.template_fixed_values.resolve())
        manifest["inputs"]["template_school"] = args.template_school
    if canonical_profile is not None:
        capability_cmd += ["--metadata", str(canonical_profile_path)]
    if isinstance(runtime_inventory, dict):
        capability_cmd += ["--runtime-inventory", str(runtime_inventory_path)]
    if semantic_issue_ledger is not None:
        capability_cmd += ["--semantic-issue-ledger", str(semantic_issue_ledger_path)]
    capability_result = run_step("capability_preflight", capability_cmd, steps)
    capability_report = read_json(capability_report_path) if capability_report_path.exists() else None
    manifest["capability_preflight"] = str(capability_report_path)
    if capability_report:
        manifest["capability_preflight_status"] = capability_report.get("status")
        manifest["capability_preflight_provenance"] = capability_report.get("provenance")
        capability_summary = capability_report.get("summary", {})
        record_capability_summary(manifest, capability_summary)
    if not capability_report or capability_result.returncode not in {0, 3}:
        manifest.update(status="failed", reason="capability preflight failed")
        write_json(manifest_path, manifest); print(json.dumps(manifest, ensure_ascii=False)); return capability_result.returncode or 2
    satisfied_clause_ids = {
        str(item.get("clause_id")) for item in (capability_report or {}).get("clauses", [])
        if item.get("category") == "supported"
        and (item.get("metadata_fields") or item.get("template_fixed_fields"))
    }
    blockers = requirement_blockers(
        spec,
        questions,
        args.compliance_mode,
        satisfied_clause_ids,
        confirmed_semantic_issue_ids,
    )
    if args.preview_placeholders:
        preview_blockers = sorted(set(blockers) & PREVIEW_PLACEHOLDER_BLOCKERS)
        blockers = [item for item in blockers if item not in PREVIEW_PLACEHOLDER_BLOCKERS]
        manifest["preview_bypassed_requirement_blockers"] = preview_blockers
    if blockers:
        manifest.update(status="blocked", reason="requirements need clarification",
                        blocking_reasons=blockers,
                        questions_file=str(requirements_dir / "questions.json"))
        write_json(manifest_path, manifest); print(json.dumps(manifest, ensure_ascii=False)); return 3
    if capability_gate_blocked(capability_report, args.compliance_mode):
        manifest.update(status="blocked", reason="backend capability preflight blocked execution",
                        capability_findings=capability_report.get("findings", []))
        write_json(manifest_path, manifest); print(json.dumps(manifest, ensure_ascii=False)); return 8

    try:
        section_plan, assembly_result = compile_preflight_plans(
            spec, effective_input, None if args.neutral_reference_docx else args.template_profile,
            section_plan_path, assembly_plan_path)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        manifest.update(status="failed", reason="structural preflight compilation failed",
                        structural_preflight_error=str(exc))
        write_json(manifest_path, manifest); print(json.dumps(manifest, ensure_ascii=False)); return 9
    manifest["section_plan"] = str(section_plan_path)
    manifest["section_plan_valid"] = section_plan.get("valid")
    if not section_plan.get("valid"):
        manifest.update(status="blocked", reason="section plan preflight blocked execution",
                        section_plan_findings=section_plan.get("findings", []))
        write_json(manifest_path, manifest); print(json.dumps(manifest, ensure_ascii=False)); return 9
    if assembly_result is not None:
        manifest["assembly_plan"] = str(assembly_plan_path)
        manifest["assembly_plan_status"] = assembly_result.get("status")
        if assembly_result.get("status") != "compiled":
            manifest.update(status="blocked", reason="region graph preflight blocked execution",
                            assembly_plan_findings=assembly_result.get("findings", []))
            write_json(manifest_path, manifest); print(json.dumps(manifest, ensure_ascii=False)); return 10

    application_input = effective_input
    if assembly_result is not None:
        assembly_plan = assembly_result["assembly_plan"]
        required_replace_roles = sorted({
            str(operation["content_role"])
            for operation in assembly_plan.get("operations", [])
            if operation.get("action") == "replace_content" and operation.get("content_role")
        })
        optional_source_roles = sorted({
            str(operation["content_role"])
            for operation in assembly_plan.get("operations", [])
            if operation.get("action") == "conditionally_assemble"
            and str(operation.get("condition", "")).startswith("source_role.")
            and operation.get("content_role")
        })
        # Even a single dynamic role needs a source boundary when the official
        # template replaces a whole body region.  Without the map, the
        # executor imports the entire converted source DOCX, including its
        # source cover/abstract/TOC, into the official template.  Multiple
        # roles and conditional roles already required this path; a single
        # required role must use it as well.
        requires_source_role_map = bool(required_replace_roles) or bool(optional_source_roles)
        if requires_source_role_map:
            source_role_cmd = [
                sys.executable, str(ROOT / "scripts" / "source_role_extractor.py"),
                str(effective_input), "--out", str(source_role_map_path),
                "--template-profile", str(args.template_profile.resolve()),
            ]
            for role in required_replace_roles:
                source_role_cmd += ["--required-role", role]
            if len(required_replace_roles) == 1 and not optional_source_roles:
                source_role_cmd += ["--whole-document-role", required_replace_roles[0]]
            source_role_result = run_step("source_role_extraction", source_role_cmd, steps)
            source_role_map = read_json(source_role_map_path) if source_role_map_path.exists() else None
            manifest["source_role_map"] = str(source_role_map_path)
            manifest["source_role_map_status"] = source_role_map.get("status") if source_role_map else None
            if (source_role_result.returncode or not source_role_map
                    or source_role_map.get("status") != "extracted"):
                manifest.update(status="blocked", reason="source role extraction blocked assembly",
                                source_role_findings=(source_role_map or {}).get("findings", []))
                write_json(manifest_path, manifest); print(json.dumps(manifest, ensure_ascii=False)); return 11
        application_input = work / "assembled-source.docx"
        assembly_cmd = [
            sys.executable, str(ROOT / "scripts" / "assembly_executor.py"),
            str(assembly_plan_path), str(official_template), str(effective_input), str(application_input),
            "--report", str(assembly_report_path),
        ]
        if requires_source_role_map:
            assembly_cmd += ["--source-role-map", str(source_role_map_path)]
        assembly_cmd += ["--source-section-policy", assembly_plan.get("source_section_policy", "reject")]
        if canonical_profile is not None:
            assembly_cmd += ["--metadata", str(canonical_profile_path)]
        assembly_execution = run_step("assembly_execution", assembly_cmd, steps)
        assembly_report = read_json(assembly_report_path) if assembly_report_path.exists() else None
        manifest["assembly_execution"] = str(assembly_report_path)
        manifest["assembly_execution_status"] = assembly_report.get("status") if assembly_report else None
        if assembly_execution.returncode or not assembly_report or assembly_report.get("status") != "assembled":
            manifest.update(status="blocked", reason="assembly plan execution blocked",
                            assembly_execution_findings=(assembly_report or {}).get("findings", []))
            write_json(manifest_path, manifest); print(json.dumps(manifest, ensure_ascii=False)); return 11

    style_cmd = [sys.executable, str(ROOT / "scripts" / "analyze_template_styles.py"),
                 str(official_template), "--out", str(style_map)]
    result = run_step("style_analysis", style_cmd, steps)
    if result.returncode:
        manifest.update(status="failed", reason="style analysis failed")
        write_json(manifest_path, manifest); print(json.dumps(manifest, ensure_ascii=False)); return result.returncode

    style_result = read_json(style_map)
    if style_result.get("status") == "needs_clarification" and not args.allow_unresolved:
        manifest.update(status="blocked", reason="style mapping needs clarification", questions_file=str(style_map))
        write_json(manifest_path, manifest); print(json.dumps(manifest, ensure_ascii=False)); return 4

    apply_cmd = [sys.executable, str(ROOT / "scripts" / "apply_format_spec.py"), str(application_input),
                 str(requirements_dir / "format-spec.json"), str(args.output), "--out-dir", str(apply_dir),
                 "--style-map", str(style_map), "--compliance-mode", args.compliance_mode,
                 "--capability-report", str(capability_report_path)]
    if not args.neutral_reference_docx:
        apply_cmd.append("--require-coverage")
    if args.neutral_reference_docx:
        apply_cmd.append("--neutral-reference")
    if has_official_template:
        apply_cmd += ["--official-template", str(official_template)]
    if args.render_report:
        apply_cmd += ["--render-report", str(args.render_report)]
    if args.preview_placeholders:
        apply_cmd.append("--preview-placeholders")
    if args.template_profile and not args.neutral_reference_docx:
        # apply_format_spec retains its legacy interface; the profile gate is
        # run independently below so generation cannot self-certify it.
        manifest["inputs"]["template_profile"] = str(args.template_profile.resolve())
    if semantic_issue_ledger is not None:
        apply_cmd += ["--semantic-issue-ledger", str(semantic_issue_ledger_path)]
    if args.require_submission_ready:
        apply_cmd.append("--require-submission-ready")
    result = run_step("apply_and_validate", apply_cmd, steps)
    report = read_json(apply_dir / "validation-report.json") if (apply_dir / "validation-report.json").exists() else None
    applied_section_plan_path = apply_dir / "section-plan-applied.json"
    section_execution_path = apply_dir / "section-execution.json"
    section_plan_audit_path = apply_dir / "section-plan-audit.json"
    manifest["section_execution"] = {
        "plan": str(applied_section_plan_path),
        "execution": str(section_execution_path),
        "audit": str(section_plan_audit_path),
    }
    if steps and steps[-1].get("name") == "apply_and_validate":
        steps[-1]["artifacts"] = dict(manifest["section_execution"])
    if result.returncode or not report or not report.get("valid") or (args.compliance_mode == "full" and not report.get("format_ready")):
        manifest.update(status="failed", reason="application or validation failed",
                        validation_report=str(apply_dir / "validation-report.json"))
        write_json(manifest_path, manifest); print(json.dumps(manifest, ensure_ascii=False)); return result.returncode or 5

    comparison_path = apply_dir / "format-comparison.json"
    comparison_markdown = apply_dir / "FORMAT-COMPARISON.md"
    comparison_cmd = [
        sys.executable, str(ROOT / "scripts" / "post_generation_format_audit.py"), str(args.output),
        "--official-template", str(official_template),
        "--format-spec", str(requirements_dir / "format-spec.json"),
        "--official-style-map", str(style_map),
        "--generated-style-map", str(apply_dir / "style-map.json"),
        "--out", str(comparison_path), "--markdown", str(comparison_markdown), "--strict",
    ]
    if not has_official_template:
        comparison_cmd.append("--requirements-only")
    comparison_result = run_step("post_generation_format_comparison", comparison_cmd, steps)
    comparison = read_json(comparison_path) if comparison_path.exists() else None
    if comparison_result.returncode or not comparison or comparison.get("status") != "passed":
        manifest.update(status="failed", reason="post-generation official format comparison failed",
                        format_comparison=str(comparison_path),
                        format_comparison_markdown=str(comparison_markdown))
        write_json(manifest_path, manifest); print(json.dumps(manifest, ensure_ascii=False)); return 7

    official_template_structure_valid: bool | None = None
    official_template_submission_ready: bool | None = None
    official_template_audit_status: str | None = None
    if args.template_profile and not args.neutral_reference_docx:
        template_audit_path = apply_dir / "template-audit.json"
        audit_cmd = [sys.executable, str(ROOT / "scripts" / "submission_audit.py"), str(args.output),
                     "--format-spec", str(requirements_dir / "format-spec.json"),
                     "--template-profile", str(args.template_profile), "--out", str(template_audit_path)]
        if args.render_report:
            audit_cmd += ["--render-report", str(args.render_report)]
        if canonical_profile is not None:
            audit_cmd += ["--thesis-profile", str(canonical_profile_path)]
        template_result = run_step("official_template_audit", audit_cmd, steps)
        official_audit = read_json(template_audit_path) if template_audit_path.exists() else None
        template_validation = (official_audit or {}).get("template_validation")
        if not isinstance(template_validation, dict):
            template_validation = {}
        official_template_structure_valid = template_validation.get("official_template_structure_valid")
        official_template_submission_ready = (
            bool(official_audit.get("submission_ready")) if official_audit else None
        )
        official_template_audit_status = official_audit.get("status") if official_audit else None
        if steps and steps[-1].get("name") == "official_template_audit":
            steps[-1]["official_template_structure_valid"] = official_template_structure_valid
            steps[-1]["submission_ready"] = official_template_submission_ready
            steps[-1]["outcome"] = (
                "submission_ready"
                if official_template_submission_ready
                else "submission_gate_pending"
            )
        if official_audit is None or official_template_structure_valid is not True:
            manifest.update(status="failed", reason="official template structure audit failed",
                            template_audit=str(template_audit_path),
                            official_template_structure_valid=official_template_structure_valid,
                            official_template_submission_ready=False,
                            submission_ready=False)
            write_json(manifest_path, manifest); print(json.dumps(manifest, ensure_ascii=False)); return 6
        # submission_audit.py intentionally exits 2 when the serialized artifact
        # is structurally valid but lacks a complete thesis profile or trusted
        # Word/PDF render evidence.  That is a submission gate, not an official
        # template-structure failure.  Only unexpected audit exit codes are
        # treated as execution failures here; --require-submission-ready is
        # enforced earlier by apply_and_validate.
        if template_result.returncode not in {0, 2}:
            manifest.update(status="failed", reason="official template audit execution failed",
                            template_audit=str(template_audit_path),
                            official_template_structure_valid=True,
                            official_template_submission_ready=official_template_submission_ready,
                            submission_ready=False)
            write_json(manifest_path, manifest); print(json.dumps(manifest, ensure_ascii=False)); return 6

    final_submission_ready = bool(report.get("submission_ready"))
    if confirmed_semantic_issue_ids:
        final_submission_ready = False
    if official_template_submission_ready is not None:
        final_submission_ready = final_submission_ready and official_template_submission_ready
    final_format_ready = bool(
        report.get("valid") and report.get("format_ready")
        and comparison.get("status") == "passed"
        and (official_template_structure_valid is not False)
    )
    if args.strict_release and not final_submission_ready:
        manifest.update(status="failed", reason="strict release submission gate failed",
                        format_ready=final_format_ready,
                        submission_ready=False,
                        submission_status=report.get("submission_status") or "not_submission_ready",
                        official_template_structure_valid=official_template_structure_valid,
                        official_template_audit_status=official_template_audit_status)
        write_json(manifest_path, manifest); print(json.dumps(manifest, ensure_ascii=False)); return 12
    final_status = (
        "completed_with_confirmed_semantic_issues"
        if confirmed_semantic_issue_ids else "completed"
    )
    manifest.update(status=final_status, finished_at=datetime.now(timezone.utc).isoformat(),
                    output_artifact=file_record(args.output),
                    format_spec=str(requirements_dir / "format-spec.json"), style_map=str(style_map),
                    capability_preflight=str(capability_report_path),
                    capability_preflight_status=capability_report.get("status"),
                    section_plan=str(section_plan_path),
                    section_plan_valid=section_plan.get("valid"),
                    section_execution={
                        "plan": str(applied_section_plan_path),
                        "execution": str(section_execution_path),
                        "audit": str(section_plan_audit_path),
                    },
                    assembly_plan=str(assembly_plan_path) if assembly_result is not None else None,
                    assembly_plan_status=assembly_result.get("status") if assembly_result is not None else None,
                    validation_report=str(apply_dir / "validation-report.json"),
                    format_comparison=str(comparison_path),
                    format_comparison_markdown=str(comparison_markdown),
                    format_comparison_summary=comparison.get("summary"),
                    unsupported_items=spec.get("completeness", {}).get("unsupported_items", []),
                    compliance_mode=args.compliance_mode,
                    overall_status=report.get("overall_status"),
                    pipeline_valid=report.get("pipeline_valid"),
                    supported_subset_valid=report.get("supported_subset_valid"),
                    serialized_docx_valid=report.get("serialized_docx_valid"),
                    render_validation=report.get("render_validation"),
                    official_template_structure_valid=official_template_structure_valid,
                    official_template_submission_ready=official_template_submission_ready,
                    official_template_audit_status=official_template_audit_status,
                    format_ready=final_format_ready,
                    submission_ready=final_submission_ready,
                    submission_status=(
                        "confirmed_semantic_issues" if confirmed_semantic_issue_ids else (
                            report.get("submission_status") or (
                                "submission_ready" if final_submission_ready else "not_submission_ready"
                            )
                        )
                    ),
                    official_template_audit=(
                        str((apply_dir / "template-audit.json").resolve())
                        if official_template_structure_valid is not None else None
                    ),
                    submission_audit=str(apply_dir / "submission-audit.json"),
                    docx_fully_compliant=report.get("docx_fully_compliant"))
    manifest["preview_placeholders"] = bool(args.preview_placeholders)
    manifest["preview_bypassed_requirement_blockers"] = report.get(
        "preview_bypassed_blockers",
        manifest.get("preview_bypassed_requirement_blockers", []),
    )
    record_capability_summary(manifest, capability_report.get("summary", {}))
    write_json(manifest_path, manifest)
    print(json.dumps({"status": manifest["status"], "output": str(args.output), "work_dir": str(work),
                      "manifest": str(manifest_path)}, ensure_ascii=False))
    return 0


def _work_dir_from_argv(argv: list[str]) -> Path | None:
    for index, value in enumerate(argv):
        if value == "--work-dir" and index + 1 < len(argv):
            return Path(argv[index + 1]).expanduser().resolve()
        if value.startswith("--work-dir="):
            return Path(value.split("=", 1)[1]).expanduser().resolve()
    return None


def main(argv: list[str]) -> int:
    """Run the pipeline and close an already-created manifest on interruption."""
    try:
        return _main(argv)
    except KeyboardInterrupt as exc:
        work = _work_dir_from_argv(argv)
        if work is not None:
            manifest_path = work / "pipeline-manifest.json"
            try:
                current = read_json(manifest_path) if manifest_path.is_file() else {}
                if isinstance(current, dict) and current.get("status") == "running":
                    current.update(
                        status="interrupted",
                        finished_at=datetime.now(timezone.utc).isoformat(),
                        failure_stage=(current.get("steps") or [{}])[-1].get("name"),
                        error_type=type(exc).__name__,
                        error="pipeline interrupted",
                    )
                    write_json(manifest_path, current)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
        raise
    except Exception as exc:
        work = _work_dir_from_argv(argv)
        if work is not None:
            manifest_path = work / "pipeline-manifest.json"
            try:
                current = read_json(manifest_path) if manifest_path.is_file() else {}
                if isinstance(current, dict) and current.get("status") == "running":
                    current.update(
                        status="failed",
                        finished_at=datetime.now(timezone.utc).isoformat(),
                        failure_stage=(current.get("steps") or [{}])[-1].get("name"),
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
                    write_json(manifest_path, current)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
        raise


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
