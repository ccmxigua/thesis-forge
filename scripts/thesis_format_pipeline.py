#!/usr/bin/env python3
"""Run the auditable LaTeX/DOCX requirement-to-format pipeline as one safe command."""
from __future__ import annotations

import argparse
import copy
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
from host_review_contract import (
    SUPPORTED_HOST_REVIEW_CONTRACTS,
    HOST_REVIEW_CONTRACT_V3,
    validate_response as validate_host_review_response,
)
from pipeline_finding import evidence, finding
from region_graph import compile_region_graph
from section_model import compile_section_plan
from semantic_contract import (
    request_body_sha256, request_envelope_sha256,
    sha256_file, sha256_json, strict_json_dumps, strict_json_loads,
    validate_response_provenance,
)
from semantic_issue_confirmation import (
    bind_confirmations,
    build_confirmation_receipt,
    confirmed_clause_ids,
)
from manual_review import (
    add_manual_review_items,
    build_manual_review_ledger,
    write_manual_review_ledger,
)
from native_semantic_review import (
    OBLIGATION_COVERAGE_SCHEMA,
    OBLIGATION_COVERAGE_PROTOCOL,
    build_obligation_coverage_request,
    validate_obligation_coverage_response,
)
from semantic_source_references import (
    REFERENCE_PROTOCOL,
    build_source_reference_packet,
    compile_source_reference_response,
)
from requirements_engine import (
    _chunk_projection,
    validate_host_review_chunk_source_projection,
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
    and compatibility runs only.  An offline response is limited to a red
    review draft and can never satisfy a release or submission-ready gate.
    """
    if args.prepare_host_review and args.llm_response:
        parser.error("choose exactly one semantic-review stage: --prepare-host-review or --llm-response")
    if bool(args.host_agent_audit) != bool(args.merge_receipt):
        parser.error("--host-agent-audit and --merge-receipt must be supplied together")
    if (args.host_agent_audit or args.merge_receipt) and not args.llm_response:
        parser.error("host-agent receipts require --llm-response")
    if args.allow_offline_review and (
        args.output_policy != "review_draft"
        or args.require_submission_ready
        or args.strict_release
    ):
        parser.error(
            "--allow-offline-review is non-release only: it requires "
            "--output-policy review_draft and cannot satisfy "
            "--require-submission-ready or --strict-release"
        )
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
        if args.output_policy != "submission":
            parser.error("--strict-release requires --output-policy submission")
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
            response = strict_json_loads(args.llm_response.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
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
    return strict_json_loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path,
        strict_json_dumps(value, ensure_ascii=False, indent=2) + "\n",
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


def _audit_artifact_path(root: Path, value: Any, *, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} path is missing")
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    return _path_under(path, root, label=label)


def _validate_independent_obligation_receipts(
    *, audit: dict[str, Any], review_root: Path, expected_run_id: str,
    expected_request_body_sha: str, expected_request_envelope_sha: str | None,
    expected_request_file_sha: str | None, output_policy: str = "submission",
) -> list[dict[str, Any]]:
    """Revalidate each source-first audit and its raw native request/response bytes."""
    chunk_runs = audit.get("chunk_runs")
    chunk_count = audit.get("chunk_count")
    if (
        isinstance(chunk_count, bool) or not isinstance(chunk_count, int) or chunk_count <= 0
        or not isinstance(chunk_runs, list) or len(chunk_runs) != chunk_count
    ):
        raise ValueError("host-agent audit must contain one independent review for every chunk")
    lifecycle = audit.get("chunk_lifecycle")
    if not isinstance(lifecycle, list) or len(lifecycle) != chunk_count:
        raise ValueError("host-agent lifecycle does not cover every independently reviewed chunk")
    lifecycle_by_index: dict[int, dict[str, Any]] = {}
    for item in lifecycle:
        if not isinstance(item, dict):
            raise ValueError("host-agent lifecycle contains an invalid chunk record")
        index = item.get("chunk_index")
        if isinstance(index, bool) or not isinstance(index, int) or index in lifecycle_by_index:
            raise ValueError("host-agent lifecycle has a missing or duplicate chunk index")
        lifecycle_by_index[index] = item
    expected_indexes = set(range(1, chunk_count + 1))
    if set(lifecycle_by_index) != expected_indexes:
        raise ValueError("host-agent lifecycle chunk indexes are incomplete")

    manifest_path = _audit_artifact_path(
        review_root, "host-agent-review-manifest.json",
        label="host-agent review manifest",
    )
    manifest = strict_json_loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("protocol") != "host_agent_semantic_review":
        raise ValueError("independent obligation reviews have no valid host-agent source manifest")
    request_path = _audit_artifact_path(
        review_root, manifest.get("request_path", "llm-request.json"),
        label="host-agent source request",
    )
    chunks_path = _audit_artifact_path(
        review_root, manifest.get("request_chunks_path", "llm-request-chunks.json"),
        label="host-agent source request chunks",
    )
    full_request = strict_json_loads(request_path.read_text(encoding="utf-8"))
    request_chunks = strict_json_loads(chunks_path.read_text(encoding="utf-8"))
    if (
        not isinstance(full_request, dict)
        or not isinstance(request_chunks, list)
        or len(request_chunks) != chunk_count
        or request_body_sha256(full_request) != expected_request_body_sha
        or manifest.get("request_body_sha256") != expected_request_body_sha
        or (expected_request_envelope_sha is not None
            and (request_envelope_sha256(full_request) != expected_request_envelope_sha
                 or manifest.get("request_envelope_sha256") != expected_request_envelope_sha))
        or (expected_request_file_sha is not None
            and sha256_file(request_path) != expected_request_file_sha)
    ):
        raise ValueError("independent obligation source request does not match the fresh extraction")
    full_provenance = full_request.get("provenance")
    if not isinstance(full_provenance, dict) or full_provenance.get("run_id") != expected_run_id:
        raise ValueError("independent obligation source request run_id does not match the fresh run")
    validate_host_review_chunk_source_projection(full_request, request_chunks, manifest)
    declared_projection_sha = manifest.get("chunk_projection_sha256")
    if (
        not isinstance(declared_projection_sha, str)
        or declared_projection_sha != sha256_json(_chunk_projection(request_chunks))
    ):
        raise ValueError("independent obligation source chunk projection does not match its manifest")
    packets_by_index: dict[int, dict[str, Any]] = {}
    for packet in request_chunks:
        batch = packet.get("batch") if isinstance(packet, dict) else None
        index = batch.get("index") if isinstance(batch, dict) else None
        if (
            isinstance(index, bool) or not isinstance(index, int)
            or index not in expected_indexes or index in packets_by_index
        ):
            raise ValueError("independent obligation source packets have missing or duplicate chunk indexes")
        packets_by_index[index] = packet
    if set(packets_by_index) != expected_indexes:
        raise ValueError("independent obligation source packets do not cover every chunk")

    seen_indexes: set[int] = set()
    validated: list[dict[str, Any]] = []
    for chunk_audit in chunk_runs:
        if not isinstance(chunk_audit, dict):
            raise ValueError("host-agent audit contains an invalid chunk audit")
        index = chunk_audit.get("chunk_index")
        if (
            isinstance(index, bool) or not isinstance(index, int)
            or index not in expected_indexes or index in seen_indexes
        ):
            raise ValueError("host-agent audit has a missing or duplicate chunk index")
        seen_indexes.add(index)
        lifecycle_item = lifecycle_by_index[index]
        if (
            lifecycle_item.get("status") != "completed"
            or lifecycle_item.get("remote_operation_state") != "completed"
        ):
            raise ValueError(f"host-agent chunk {index} lifecycle is not completed")

        chunk_response_path = _audit_artifact_path(
            review_root, chunk_audit.get("response_path"),
            label=f"accepted chunk response {index}",
        )
        chunk_response = read_json(chunk_response_path)
        candidate_sha = sha256_json(chunk_response)
        if chunk_audit.get("accepted_response_sha256") != candidate_sha:
            raise ValueError(f"accepted chunk response {index} does not match its recorded hash")
        provenance = chunk_response.get("provenance") if isinstance(chunk_response, dict) else None
        independent = chunk_audit.get("independent_obligation_review")
        if (
            not isinstance(independent, dict)
            or independent.get("status") != "completed"
            or independent.get("protocol") != OBLIGATION_COVERAGE_PROTOCOL
            or independent.get("run_id") != expected_run_id
            or independent.get("chunk_index") != index
            or independent.get("candidate_response_sha256") != candidate_sha
        ):
            raise ValueError(f"host-agent chunk {index} has no response-bound independent review")

        envelope_path = _audit_artifact_path(
            review_root, independent.get("audit_path"),
            label=f"independent obligation audit {index}",
        )
        if not envelope_path.is_file() or sha256_file(envelope_path) != independent.get("audit_sha256"):
            raise ValueError(f"independent obligation audit {index} bytes do not match its receipt")
        envelope = read_json(envelope_path)
        review_audit = envelope.get("review_audit") if isinstance(envelope, dict) else None
        if (
            not isinstance(envelope, dict)
            or envelope.get("protocol") != OBLIGATION_COVERAGE_PROTOCOL
            or envelope.get("status") != "completed"
            or envelope.get("run_id") != expected_run_id
            or envelope.get("chunk_index") != index
            or envelope.get("candidate_response_sha256") != candidate_sha
            or envelope.get("provenance") != provenance
            or independent.get("candidate_response_sha256") != candidate_sha
            or not isinstance(review_audit, dict)
            or review_audit.get("status") != "completed"
            or review_audit.get("protocol") != OBLIGATION_COVERAGE_PROTOCOL
            or review_audit.get("adapter_id") != audit.get("adapter_id")
            or review_audit.get("host_runtime") != audit.get("host_runtime")
        ):
            raise ValueError(f"independent obligation audit {index} is not bound to the current native run")

        request_path = _audit_artifact_path(
            review_root, review_audit.get("request_path"),
            label=f"independent obligation request {index}",
        )
        reviewer_response_path = _audit_artifact_path(
            review_root, review_audit.get("response_path"),
            label=f"independent obligation response {index}",
        )
        review_request = read_json(request_path)
        review_response = read_json(reviewer_response_path)
        request_sha = sha256_json(review_request)
        response_file_sha = sha256_file(reviewer_response_path)
        provider_attempt = review_request.get("provider_attempt", 1) if isinstance(review_request, dict) else None
        retry_feedback = review_request.get("retry_feedback") if isinstance(review_request, dict) else None
        allowed_retry_feedback_codes = {
            "external_compliance_unrepresented_obligation",
            "missing_executable_obligation_inventory",
            "independent_obligation_review_incomplete",
        }
        if (
            request_sha != review_audit.get("request_sha256")
            or request_sha != envelope.get("review_request_sha256")
            or request_sha != independent.get("review_request_sha256")
            or response_file_sha != review_audit.get("response_sha256")
            or response_file_sha != envelope.get("review_response_sha256")
            or response_file_sha != independent.get("review_response_sha256")
            or not isinstance(review_request, dict)
            or review_request.get("protocol") != OBLIGATION_COVERAGE_PROTOCOL
            or review_request.get("run_id") != expected_run_id
            or review_request.get("chunk_index") != index
            or review_request.get("provenance") != provenance
            or isinstance(provider_attempt, bool)
            or not isinstance(provider_attempt, int)
            or provider_attempt <= 0
            or (retry_feedback is not None and (
                not isinstance(retry_feedback, dict)
                or retry_feedback.get("code") not in allowed_retry_feedback_codes
            ))
            or envelope.get("retry_feedback") != retry_feedback
            or independent.get("retry_feedback") != retry_feedback
        ):
            raise ValueError(f"independent obligation request/response {index} failed byte or identity validation")
        checks = review_request.get("checks")
        clause_reviews = chunk_response.get("clause_reviews")
        expected_clause_ids = [
            str(item.get("clause_id")) for item in clause_reviews
            if isinstance(item, dict) and isinstance(item.get("clause_id"), str)
        ] if isinstance(clause_reviews, list) else []
        check_ids = [
            item.get("check_id") for item in checks if isinstance(item, dict)
        ] if isinstance(checks, list) else []
        if (
            not isinstance(checks, list)
            or len(checks) != len(expected_clause_ids)
            or len(check_ids) != len(checks)
            or len(set(check_ids)) != len(check_ids)
            or set(check_ids) != set(expected_clause_ids)
        ):
            raise ValueError(f"independent obligation review {index} does not cover every accepted clause")
        attempt = review_request.get("attempt")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt <= 0:
            raise ValueError(f"independent obligation review {index} has an invalid attempt number")
        expected_review_request = build_obligation_coverage_request(
            chunk_response, packets_by_index[index],
            run_id=expected_run_id, chunk_index=index,
        )
        expected_review_request["attempt"] = attempt
        expected_review_request["provider_attempt"] = provider_attempt
        if retry_feedback is not None:
            expected_review_request["retry_feedback"] = copy.deepcopy(retry_feedback)
        if sha256_json(review_request) != sha256_json(expected_review_request):
            raise ValueError(
                f"independent obligation review {index} request is not reconstructed "
                "from the canonical source packet and accepted response"
            )
        normalized_results = validate_obligation_coverage_response(review_response, checks)
        if (
            normalized_results != envelope.get("results")
            or normalized_results != review_audit.get("results")
            or any(item.get("verdict") == "incomplete" for item in normalized_results)
        ):
            raise ValueError(f"independent obligation review {index} is incomplete or inconsistent")

        ledger_pointer = envelope.get("obligation_analysis_ledger")
        if (
            not isinstance(ledger_pointer, dict)
            or ledger_pointer.get("protocol") != "obligation_analysis_ledger_v1"
            or ledger_pointer.get("status") != "analysis_only"
            or ledger_pointer.get("submission_ready") is not False
            or ledger_pointer.get("path") != independent.get("obligation_analysis_ledger_path")
            or ledger_pointer.get("sha256") != independent.get("obligation_analysis_ledger_sha256")
        ):
            raise ValueError(f"independent obligation review {index} has no bound analysis-only ledger")
        ledger_path = _audit_artifact_path(
            review_root, ledger_pointer.get("path"),
            label=f"obligation analysis ledger {index}",
        )
        if not ledger_path.is_file() or sha256_file(ledger_path) != ledger_pointer.get("sha256"):
            raise ValueError(f"obligation analysis ledger {index} bytes do not match its receipt")
        ledger = read_json(ledger_path)
        compilation_path = _audit_artifact_path(
            review_root, review_audit.get("source_reference_compilation_path"),
            label=f"source-reference compilation {index}",
        )
        if (
            not compilation_path.is_file()
            or sha256_file(compilation_path) != review_audit.get("source_reference_compilation_sha256")
        ):
            raise ValueError(f"source-reference compilation {index} bytes do not match its receipt")
        compilation = read_json(compilation_path)
        source_packet = build_source_reference_packet(review_request)
        source_packet_path = _audit_artifact_path(
            review_root, review_audit.get("source_reference_packet_path"),
            label=f"source-reference packet {index}",
        )
        raw_response_path = _audit_artifact_path(
            review_root, review_audit.get("raw_response_path"),
            label=f"raw independent response {index}",
        )
        compiled_response_path = _audit_artifact_path(
            review_root, review_audit.get("compiled_response_path"),
            label=f"compiled independent response {index}",
        )
        if (
            review_audit.get("source_reference_protocol") != REFERENCE_PROTOCOL
            or not source_packet_path.is_file()
            or sha256_file(source_packet_path) != review_audit.get("source_reference_packet_sha256")
            or not raw_response_path.is_file()
            or sha256_file(raw_response_path) != review_audit.get("raw_response_file_sha256")
            or not compiled_response_path.is_file()
            or sha256_file(compiled_response_path) != review_audit.get("compiled_response_sha256")
        ):
            raise ValueError(f"independent source-reference artifacts {index} do not match their receipts")
        persisted_source_packet = read_json(source_packet_path)
        raw_review_response = read_json(raw_response_path)
        persisted_compiled_response = read_json(compiled_response_path)
        if persisted_source_packet != source_packet or persisted_compiled_response != review_response:
            raise ValueError(f"independent source-reference artifacts {index} do not match the canonical request/response")
        reconstructed_response, reconstructed_compilation = compile_source_reference_response(
            raw_review_response, review_request, OBLIGATION_COVERAGE_SCHEMA, coverage=True,
            provider_nullable_optionals=review_audit.get("adapter_id") == "codex",
        )
        if reconstructed_response != review_response or reconstructed_compilation != compilation:
            raise ValueError(
                f"source-reference compilation {index} does not reproduce from the persisted raw response"
            )
        source_checks = {
            str(item.get("check_id")): item for item in source_packet.get("checks", [])
            if isinstance(item, dict) and isinstance(item.get("check_id"), str)
        }
        selections = {
            str(item.get("check_id")): item for item in compilation.get("selections", [])
            if isinstance(item, dict) and isinstance(item.get("check_id"), str)
        } if isinstance(compilation, dict) else {}
        ledger_items = ledger.get("obligations") if isinstance(ledger, dict) else None
        if (
            not isinstance(ledger, dict)
            or ledger.get("protocol") != "obligation_analysis_ledger_v1"
            or ledger.get("status") != "analysis_only"
            or ledger.get("run_id") != expected_run_id
            or ledger.get("case_id") != review_request.get("case_id")
            or ledger.get("chunk_index") != index
            or ledger.get("attempt") != attempt
            or ledger.get("candidate_response_sha256") != candidate_sha
            or ledger.get("review_request_sha256") != request_sha
            or ledger.get("review_response_sha256") != response_file_sha
            or ledger.get("provenance") != provenance
            or ledger.get("source_reference_protocol") != "semantic_source_references_v2"
            or ledger.get("source_reference_compilation_sha256")
            != review_audit.get("source_reference_compilation_sha256")
            or ledger.get("submission_ready") is not False
            or not isinstance(ledger_items, list)
            or ledger_pointer.get("obligation_count") != len(ledger_items)
            or not isinstance(compilation, dict)
            or compilation.get("protocol") != "semantic_source_references_v2"
            or compilation.get("run_id") != expected_run_id
            or compilation.get("request_sha256") != request_sha
            or compilation.get("packet_sha256") != sha256_json(source_packet)
            or set(selections) != set(source_checks)
        ):
            raise ValueError(f"obligation analysis ledger {index} is not bound to the current review")
        expected_ledger_items: list[dict[str, Any]] = []
        for result in normalized_results:
            check_id = str(result.get("check_id"))
            selected = selections.get(check_id)
            selected_obligations = selected.get("obligations") if isinstance(selected, dict) else None
            identified = result.get("identified_obligations")
            if not isinstance(selected_obligations, list) or not isinstance(identified, list) or (
                len(selected_obligations) != len(identified)
            ):
                raise ValueError(f"obligation analysis ledger {index} source selections are incomplete")
            span_catalog = {
                item.get("ref_id"): item
                for item in source_checks[check_id].get("source_spans", [])
                if isinstance(item, dict) and isinstance(item.get("ref_id"), str)
            }
            for obligation_index, (obligation, selection) in enumerate(zip(identified, selected_obligations)):
                span = selection.get("span") if isinstance(selection, dict) else None
                source_ref = selection.get("source_ref") if isinstance(selection, dict) else None
                if (
                    not isinstance(span, dict)
                    or selection.get("obligation_index") != obligation_index
                    or span_catalog.get(source_ref) != span
                    or obligation.get("source_quote") != span.get("text")
                ):
                    raise ValueError(f"obligation analysis ledger {index} has an invalid source-span selection")
                identity = {
                    "protocol": "obligation_analysis_ledger_v1",
                    "run_id": expected_run_id,
                    "case_id": review_request.get("case_id"),
                    "chunk_index": index,
                    "attempt": attempt,
                    "candidate_response_sha256": candidate_sha,
                    "review_request_sha256": request_sha,
                    "review_response_sha256": response_file_sha,
                    "check_id": check_id,
                    "obligation_index": obligation_index,
                    "source_ref": source_ref,
                    "source_sha256": span.get("source_sha256"),
                    "start": span.get("start"),
                    "end": span.get("end"),
                }
                expected_ledger_items.append({
                    "analysis_obligation_id": "AO-" + sha256_json(identity)[:24],
                    "check_id": check_id,
                    "source_ref": source_ref,
                    "source_quote": span.get("text"),
                    "source_start": span.get("start"),
                    "source_end": span.get("end"),
                    "source_text_sha256": span.get("source_sha256"),
                    "obligation_summary": obligation.get("obligation_summary"),
                    "disposition": obligation.get("disposition"),
                    "scope_dependency_codes": copy.deepcopy(obligation.get("scope_dependency_codes") or []),
                    "scope_dependency_dimensions": copy.deepcopy(
                        obligation.get("scope_dependency_dimensions") or []
                    ),
                    "requirement_refs": copy.deepcopy(obligation.get("requirement_refs") or []),
                    "execution_authorized": False,
                })
        if ledger_items != expected_ledger_items:
            raise ValueError(f"obligation analysis ledger {index} does not match the reviewed source obligations")
        manual_review_clause_ids = enforce_obligation_review_output_policy(
            normalized_results, output_policy=output_policy,
        )
        checks_by_id = {
            str(item.get("check_id")): item for item in checks
            if isinstance(item, dict) and isinstance(item.get("check_id"), str)
        }
        source_content_pending_items: list[dict[str, Any]] = []
        backend_unsupported_items: list[dict[str, Any]] = []
        for result in normalized_results:
            if result.get("verdict") == "backend_unsupported":
                backend_unsupported_items.append({
                    "clause_id": str(result.get("check_id") or ""),
                    "source_quotes": [
                        str(item.get("source_quote"))
                        for item in result.get("identified_obligations", [])
                        if isinstance(item, dict)
                        and item.get("disposition") == "backend_unsupported"
                        and isinstance(item.get("source_quote"), str)
                    ],
                    "reason": str(result.get("rationale") or ""),
                })
            if result.get("verdict") != "source_content_pending":
                continue
            check_id = str(result.get("check_id") or "")
            check = checks_by_id.get(check_id)
            context = check.get("review_context") if isinstance(check, dict) else None
            cited_evidence = (
                context.get("cited_evidence")
                if isinstance(context, dict) and isinstance(context.get("cited_evidence"), dict)
                else {}
            )
            obligations = result.get("identified_obligations")
            source_quotes = [
                str(item.get("source_quote"))
                for item in obligations or []
                if isinstance(item, dict)
                and item.get("disposition") == "authoring_content_pending"
                and isinstance(item.get("source_quote"), str)
            ]
            if (
                not check_id
                or not source_quotes
                or not cited_evidence
                or any(
                    not isinstance(evidence_id, str) or not evidence_id.strip()
                    or not isinstance(evidence_item, dict)
                    for evidence_id, evidence_item in cited_evidence.items()
                )
            ):
                raise ValueError(
                    "source-content pending receipt has no exact clause, authoring quote, "
                    "or valid cited evidence"
                )
            source_content_pending_items.append({
                "clause_id": check_id,
                "source_quotes": source_quotes,
                "evidence_ids": sorted(str(key) for key in cited_evidence),
                "reason": str(result.get("rationale") or ""),
            })
        validated.append({
            "chunk_index": index,
            "candidate_response_sha256": candidate_sha,
            "audit_sha256": independent.get("audit_sha256"),
            "review_request_sha256": request_sha,
            "review_response_sha256": response_file_sha,
            "manual_review_required_clause_ids": manual_review_clause_ids,
            "submission_blocked_by_manual_review": bool(manual_review_clause_ids),
            "backend_unsupported_clause_ids": sorted({
                item["clause_id"] for item in backend_unsupported_items if item["clause_id"]
            }),
            "backend_unsupported_items": backend_unsupported_items,
            "submission_blocked_by_backend_unsupported": bool(backend_unsupported_items),
            "source_content_pending_clause_ids": sorted({
                item["clause_id"] for item in source_content_pending_items
            }),
            "source_content_pending_items": source_content_pending_items,
            "submission_blocked_by_source_content_pending": bool(source_content_pending_items),
        })
    if seen_indexes != expected_indexes:
        raise ValueError("host-agent audit omitted an independently reviewed chunk")
    return sorted(validated, key=lambda item: item["chunk_index"])


def enforce_obligation_review_output_policy(
    results: list[dict[str, Any]], *, output_policy: str,
) -> list[str]:
    """Permit irreducible ambiguity only in an explicitly non-release draft."""
    if output_policy not in {"review_draft", "submission"}:
        raise ValueError(f"unsupported output policy for independent review: {output_policy!r}")
    manual_review_clause_ids = sorted({
        str(item.get("check_id")) for item in results
        if isinstance(item, dict)
        and item.get("verdict") == "manual_review_required"
        and isinstance(item.get("check_id"), str)
    })
    source_content_pending_clause_ids = sorted({
        str(item.get("check_id")) for item in results
        if isinstance(item, dict)
        and item.get("verdict") == "source_content_pending"
        and isinstance(item.get("check_id"), str)
    })
    if source_content_pending_clause_ids and output_policy != "review_draft":
        raise ValueError(
            "independent obligation review requires genuine author content for clause(s) "
            + ", ".join(source_content_pending_clause_ids)
            + "; submission output is blocked (use an explicit review_draft for a non-release artifact)"
        )
    if manual_review_clause_ids and output_policy != "review_draft":
        raise ValueError(
            "independent obligation review requires manual review for clause(s) "
            + ", ".join(manual_review_clause_ids)
            + "; submission output is blocked (use an explicit review_draft for a non-release artifact)"
        )
    return manual_review_clause_ids


def _source_content_pending_release_gates(
    independent_reviews: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project validated author-input findings into draft-only visible gates."""
    if not isinstance(independent_reviews, list):
        raise ValueError("independent obligation reviews must be an array")
    gates: list[dict[str, Any]] = []
    for independent_review in independent_reviews:
        if not isinstance(independent_review, dict):
            raise ValueError("independent obligation review must be an object")
        pending_items = independent_review.get("source_content_pending_items", [])
        if not isinstance(pending_items, list):
            raise ValueError("independent review source-content items must be an array")
        for pending in pending_items:
            if not isinstance(pending, dict):
                raise ValueError("independent review source-content item must be an object")
            clause_id = pending.get("clause_id")
            quotes = pending.get("source_quotes")
            if (
                not isinstance(clause_id, str) or not clause_id
                or not isinstance(quotes, list) or not quotes
                or any(not isinstance(quote, str) or not quote.strip() for quote in quotes)
            ):
                raise ValueError("validated independent review has a malformed source-content pending item")
            evidence_ids = pending.get("evidence_ids", [])
            if not isinstance(evidence_ids, list) or not evidence_ids or any(
                not isinstance(value, str) or not value for value in evidence_ids
            ):
                raise ValueError("source-content pending requires non-empty evidence IDs")
            gates.append({
                "source_code": "independent_authoring_content_pending",
                "category": "input_prerequisite",
                "source_text": "\n".join(quotes),
                "reason": "原文含明确的作者内容指令；当前输入仍待作者提供真实内容。",
                "action": "请用本人真实研究内容替换示例或虚构内容；系统不会代写论文实质内容。完成后以 submission 模式重新开始一轮新运行。",
                "placeholder_text": f"【待补写真实论文内容：{clause_id}】",
                "clause_ids": [clause_id],
                "evidence_ids": sorted(set(evidence_ids)),
            })
    return gates


def validate_host_review_receipts(
    *, response_path: Path, audit_path: Path, receipt_path: Path,
    extraction_manifest: dict[str, Any], work: Path, output_policy: str = "submission",
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
    marker_path_value = receipt.get("merge_commit_path")
    if not marker_path_value:
        raise ValueError("merge receipt does not name a final merge commit marker")
    marker_path = _path_under(
        Path(str(marker_path_value)), work, label="merge commit marker",
    )
    if marker_path != (receipt_path.parent / "merge-commit.json").resolve():
        raise ValueError("merge commit marker path does not match the receipt directory")
    if not marker_path.is_file():
        raise ValueError("final merge commit marker is missing")
    marker = read_json(marker_path)
    if (
        marker.get("status") != "committed"
        or marker.get("protocol") != "host_agent_semantic_review_merge"
        or marker.get("run_id") != expected_run_id
        or marker.get("aggregate_sha256") != receipt.get("aggregate_sha256")
    ):
        raise ValueError("merge commit marker is not bound to the current merge receipt")
    marker_artifacts = marker.get("artifacts")
    if not isinstance(marker_artifacts, list):
        raise ValueError("merge commit marker artifacts must be an array")
    artifact_records: dict[Path, str] = {}
    for item in marker_artifacts:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ValueError("merge commit marker contains an invalid artifact record")
        path = _path_under(work / item["path"], work, label="merge artifact")
        digest = item.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64 or path in artifact_records:
            raise ValueError("merge commit marker contains an invalid hash or duplicate path")
        if not path.is_file() or sha256_file(path) != digest:
            raise ValueError(f"merge artifact bytes do not match the commit marker: {path}")
        artifact_records[path] = digest
    ledger_path_value = receipt.get("semantic_review_ledger_path")
    ledger_sha = receipt.get("semantic_review_ledger_sha256")
    if not ledger_path_value or not ledger_sha:
        raise ValueError("merge receipt must include the semantic review ledger")
    ledger_path = _path_under(
        Path(str(ledger_path_value)), work, label="semantic review ledger"
    )
    if set(artifact_records) != {response_path, receipt_path, ledger_path}:
        raise ValueError("merge commit marker does not cover exactly the response, receipt, and ledger")
    if artifact_records[response_path] != sha256_file(response_path):
        raise ValueError("merged response bytes changed after commit")
    if artifact_records[receipt_path] != sha256_file(receipt_path):
        raise ValueError("merge receipt bytes changed after commit")
    if artifact_records[ledger_path] != sha256_file(ledger_path):
        raise ValueError("semantic review ledger bytes changed after commit")
    ledger = read_json(ledger_path)
    if sha256_json(ledger) != ledger_sha:
        raise ValueError("semantic review ledger hash does not match the receipt")
    if ledger.get("response_sha256") != receipt.get("aggregate_sha256"):
        raise ValueError("semantic review ledger is not bound to the merged response")
    if merge.get("semantic_review_ledger_sha256") != ledger_sha:
        raise ValueError("host-agent audit ledger hash does not match the receipt")
    independent_reviews = _validate_independent_obligation_receipts(
        audit=audit, review_root=audit_path.parent.resolve(),
        expected_run_id=str(expected_run_id),
        expected_request_body_sha=str(expected_request_body_sha),
        expected_request_envelope_sha=(
            str(expected_request_envelope_sha) if expected_request_envelope_sha else None
        ),
        expected_request_file_sha=(
            str(expected_request_file_sha) if expected_request_file_sha else None
        ),
        output_policy=output_policy,
    )
    return {
        "response": file_record(response_path),
        "host_agent_audit": file_record(audit_path),
        "merge_receipt": file_record(receipt_path),
        "merge_commit_marker": file_record(marker_path),
        "aggregate_sha256": receipt.get("aggregate_sha256"),
        "request_sha256": expected_request_body_sha,
        "request_body_sha256": expected_request_body_sha,
        "request_envelope_sha256": expected_request_envelope_sha,
        "request_file_sha256": expected_request_file_sha,
        "run_id": expected_run_id,
        "runtime_context": expected_runtime_context,
        "contract_version": response_contract_version,
        "independent_obligation_reviews": independent_reviews,
        "semantic_review_ledger": (
            {"path": str(ledger_path), "sha256": ledger_sha}
            if response_contract_version == HOST_REVIEW_CONTRACT_V3 else None
        ),
    }


def validate_semantic_contract_gate(
    *, response_path: Path, request_path: Path, require_provenance: bool,
) -> dict[str, Any]:
    """Validate the response immediately before capability planning.

    The requirements stage intentionally keeps malformed host output available
    as diagnostic evidence.  That is useful for root-cause analysis, but it
    must never be enough to reach capability planning.  This gate is the
    single preflight boundary for every ``--llm-response`` entry point:

    * the exact current request owns the response schema and clause set;
    * the shared contract validator owns cross-array relation checks; and
    * provenance is mandatory for release/receipt-bound runs, while an
      explicitly offline supported-subset fixture may omit it.

    No repair, reclassification, or inferred relation is performed here.
    """
    response_path = response_path.expanduser().resolve()
    request_path = request_path.expanduser().resolve()
    response = read_json(response_path)
    request = read_json(request_path)
    if not isinstance(request, dict):
        raise ValueError("semantic contract gate request is not an object")
    if not isinstance(response, dict):
        raise ValueError("semantic contract gate response is not an object")
    contract_errors = validate_host_review_response(response, request)
    expected_provenance = request.get("provenance")
    actual_provenance = response.get("provenance")
    provenance_required = bool(require_provenance)
    provenance_errors: list[str] = []
    if provenance_required or isinstance(actual_provenance, dict):
        if not isinstance(expected_provenance, dict):
            provenance_errors = ["request_provenance_missing"]
        else:
            provenance_errors = validate_response_provenance(
                response, expected_provenance, require_fresh_origin=True,
            )
    if contract_errors or provenance_errors:
        details = [*contract_errors, *[f"provenance:{item}" for item in provenance_errors]]
        raise ValueError(
            "semantic contract/provenance gate failed before capability planning: "
            + "; ".join(details[:24])
        )
    return {
        "status": "passed",
        "response_path": str(response_path),
        "request_path": str(request_path),
        "response_sha256": sha256_json(response),
        "request_sha256": sha256_json(request),
        "contract_version": response.get("contract_version"),
        "contract_errors": [],
        "provenance_required": provenance_required,
        "provenance_validated": bool(provenance_required or isinstance(actual_provenance, dict)),
        "provenance_errors": [],
        "offline_provenance_exception": bool(
            not provenance_required and not isinstance(actual_provenance, dict)
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
    p.add_argument("--semantic-review-runtime", choices=["codex", "openclaw"],
                   help="explicit native runtime for post-format abstract semantic review")
    p.add_argument("--semantic-review-model",
                   help="explicit native model route for post-format abstract semantic review")
    p.add_argument("--thesis-profile", type=Path, help="JSON metadata for conditional requirements such as master/doctor limits")
    p.add_argument("--analysis-mode", choices=["llm_primary", "rule_only", "known_template"], default="llm_primary",
                   help="unseen templates default to full LLM semantic extraction and completeness review")
    p.add_argument("--compliance-mode", choices=["full", "supported_subset"], default="full",
                   help="full blocks every applicable unsupported/unverifiable DOCX clause; supported_subset is compatibility/testing only")
    p.add_argument("--output-policy", choices=["review_draft", "submission"], default="submission",
                   help="review_draft emits red manual-review markers; submission keeps strict release gates")
    p.add_argument("--capability-registry", type=Path,
                   help="optional backend capability registry (defaults to the bundled python-docx/OOXML registry)")
    p.add_argument("--source-inventory", type=Path,
                   help="optional JSON source inventory used by conditional backend capabilities")
    p.add_argument("--allow-unresolved", action="store_true",
                   help="unsafe expert override: continue despite unresolved style-map questions; requirement questions still block")
    p.add_argument("--allow-offline-review", action="store_true",
                   help="non-release test mode only; requires --output-policy review_draft and permits compilation without a native call receipt")
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
    if bool(args.semantic_review_runtime) != bool(args.semantic_review_model):
        p.error("--semantic-review-runtime and --semantic-review-model must be supplied together")
    if args.case_id is not None:
        args.case_id = args.case_id.strip()
        if not args.case_id or not all(
            character.isalnum() or character in ".-_" for character in args.case_id
        ):
            p.error("--case-id must contain only letters, digits, dot, underscore, or hyphen")
    validate_semantic_review_configuration(args, p)
    execution_compliance_mode = (
        "supported_subset" if args.output_policy == "review_draft"
        else args.compliance_mode
    )
    if args.host_review_chunk_size <= 0:
        p.error("--host-review-chunk-size must be a positive integer")
    if args.preview_placeholders and args.compliance_mode != "supported_subset" and args.output_policy != "review_draft":
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
    manual_review_items_path = work / "manual-review-items.json"
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
        except (OSError, ValueError) as exc:
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
        "output_policy": args.output_policy,
        "requested_compliance_mode": args.compliance_mode,
        "execution_compliance_mode": execution_compliance_mode,
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
                output_policy=args.output_policy,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            manifest.update(status="failed", reason="host-agent review receipt gate failed",
                            host_review_receipt_error=str(exc))
            write_json(manifest_path, manifest); print(json.dumps(manifest, ensure_ascii=False)); return 10
    if args.llm_response:
        # Do this before semantic issue binding, capability planning, section
        # compilation, or any document-generation stage.  The requirements
        # stage may have emitted a diagnostic ``format-spec.json`` from an
        # invalid response, but no invalid response may become a capability
        # input or a release candidate.
        try:
            manifest["semantic_contract_gate"] = validate_semantic_contract_gate(
                response_path=args.llm_response,
                request_path=requirements_dir / "llm-request.json",
                require_provenance=bool(
                    args.compliance_mode == "full"
                    or args.host_agent_audit
                    or args.merge_receipt
                    or args.strict_release
                ),
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            manifest.update(
                status="failed",
                reason="semantic contract/provenance gate failed before capability preflight",
                semantic_contract_gate={
                    "status": "failed",
                    "response_path": str(args.llm_response.resolve()),
                    "request_path": str((requirements_dir / "llm-request.json").resolve()),
                    "error": str(exc),
                },
            )
            write_json(manifest_path, manifest)
            print(json.dumps(manifest, ensure_ascii=False))
            return 10
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
    if args.preview_placeholders or args.output_policy == "review_draft":
        ensure_preview_cover_placeholders(spec, manifest)
        ensure_preview_page_number_selector(spec, manifest)
    if canonical_profile is not None:
        spec["thesis_profile"] = canonical_profile
        manifest["metadata_status"] = canonical_profile.get("metadata_status")
        manifest["metadata_pending_fields"] = canonical_profile.get("pending_fields", [])
    spec["compliance_mode"] = execution_compliance_mode
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
        "--compliance-mode", execution_compliance_mode,
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
    if args.output_policy == "review_draft":
        semantic_provenance = spec.get("semantic_review_provenance")
        if not isinstance(semantic_provenance, dict):
            semantic_provenance = {}
        clause_file_record = file_record(requirements_dir / "requirement-clauses.json")
        evidence_doc = read_json(requirements_dir / "document-evidence.json")
        evidence_context_path = requirements_dir / "evidence-context.json"
        evidence_file_record = file_record(evidence_context_path) if evidence_context_path.is_file() else {}
        manual_review_release_gates: list[dict[str, Any]] = []
        if not has_official_template:
            baseline_kind = (
                "中性参考文档"
                if args.neutral_reference_docx
                else "当前输入文档"
            )
            manual_review_release_gates.append({
                "source_code": "official_template_missing",
                "category": "input_prerequisite",
                "source_text": "官方版式模板未提供",
                "reason": (
                    f"当前仅有{baseline_kind}作为生成基线，不能证明学校官方版式要求。"
                ),
                "action": "提供并绑定本校官方 DOCX 模板后，以 submission 模式重新开始一轮新运行。",
                "placeholder_text": "【待提供：官方版式模板】",
            })
        host_review_receipts = manifest.get("host_review_receipts")
        independent_reviews = (
            host_review_receipts.get("independent_obligation_reviews", [])
            if isinstance(host_review_receipts, dict) else []
        )
        manual_review_release_gates.extend(
            _source_content_pending_release_gates(independent_reviews)
        )
        manifest["manual_review_release_gates"] = [
            gate["source_code"] for gate in manual_review_release_gates
        ]
        manual_review_ledger = build_manual_review_ledger(
            capability_report,
            questions,
            release_gates=manual_review_release_gates,
            clauses=clauses,
            evidence_doc=evidence_doc,
            binding={
                "case_id": args.case_id or "standalone",
                "run_id": spec.get("run_id"),
                "source_sha256": semantic_provenance.get(
                    "source_sha256", manifest["inputs"]["source"].get("sha256")
                ),
                "clause_sha256": semantic_provenance.get(
                    "clause_sha256", clause_file_record.get("sha256")
                ),
                "evidence_sha256": semantic_provenance.get(
                    "evidence_sha256", evidence_file_record.get("sha256")
                ),
                "request_sha256": semantic_provenance.get("request_sha256"),
                "requirements_sha256": manifest["inputs"]["requirements"].get("sha256"),
                "input_source_sha256": manifest["inputs"]["source"].get("sha256"),
                "format_spec_sha256": file_record(requirements_dir / "format-spec.json").get("sha256"),
                "official_template_sha256": (
                    file_record(official_template_evidence).get("sha256")
                    if official_template_evidence else None
                ),
                "official_template_source": manifest["inputs"].get("official_template_source"),
            },
        )
        # The ledger is a run product, but validate it against the checked-in
        # schema before exposing it to the DOCX stage.  A malformed sidecar
        # must fail here rather than silently producing an unbound red draft.
        manual_review_schema_errors = load_and_validate(
            manual_review_ledger, ROOT / "schema" / "manual-review-ledger.schema.json",
        )
        if manual_review_schema_errors:
            manifest.update(
                status="failed",
                reason="manual review ledger schema validation failed",
                manual_review_ledger_errors=manual_review_schema_errors,
            )
            write_json(manifest_path, manifest)
            print(json.dumps(manifest, ensure_ascii=False))
            return 13
        write_manual_review_ledger(manual_review_items_path, manual_review_ledger)
        manifest["manual_review_items"] = str(manual_review_items_path)
        manifest["manual_review_summary"] = manual_review_ledger.get("summary", {})
    satisfied_clause_ids = {
        str(item.get("clause_id")) for item in (capability_report or {}).get("clauses", [])
        if item.get("category") == "supported"
        and (item.get("metadata_fields") or item.get("template_fixed_fields"))
    }
    blockers = requirement_blockers(
        spec,
        questions,
        execution_compliance_mode,
        satisfied_clause_ids,
        confirmed_semantic_issue_ids,
    )
    if args.preview_placeholders or args.output_policy == "review_draft":
        preview_blockers = sorted(set(blockers) & PREVIEW_PLACEHOLDER_BLOCKERS)
        blockers = [item for item in blockers if item not in PREVIEW_PLACEHOLDER_BLOCKERS]
        manifest["preview_bypassed_requirement_blockers"] = preview_blockers
    if blockers:
        manifest.update(status="blocked", reason="requirements need clarification",
                        blocking_reasons=blockers,
                        questions_file=str(requirements_dir / "questions.json"))
        write_json(manifest_path, manifest); print(json.dumps(manifest, ensure_ascii=False)); return 3
    if capability_gate_blocked(capability_report, execution_compliance_mode):
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
    if args.output_policy == "review_draft" and style_result.get("status") == "needs_clarification":
        style_gate = {
            "source_type": "release_gate",
            "source_code": "style_mapping_needs_clarification",
            "source_codes": ["style_mapping_needs_clarification"],
            "category": "runtime_manual_unverifiable",
            "clause_ids": [],
            "requirement_ids": [],
            "question_ids": [],
            "evidence_ids": [],
            "source_text": "官方模板样式映射仍需人工确认",
            "reason": "样式分析返回 needs_clarification，自动选择会把不确定的版式关系写入草稿。",
            "action": "人工确认样式映射后，以 submission 模式重新开始一轮新运行。",
            "placeholder_text": "【待人工确认：官方模板样式映射】",
            "original_blocking": True,
            "release_gate": True,
        }
        manual_review_ledger = add_manual_review_items(manual_review_ledger, [style_gate])
        manual_review_schema_errors = load_and_validate(
            manual_review_ledger, ROOT / "schema" / "manual-review-ledger.schema.json",
        )
        if manual_review_schema_errors:
            manifest.update(
                status="failed",
                reason="manual review ledger schema validation failed after style analysis",
                manual_review_ledger_errors=manual_review_schema_errors,
            )
            write_json(manifest_path, manifest)
            print(json.dumps(manifest, ensure_ascii=False))
            return 13
        write_manual_review_ledger(manual_review_items_path, manual_review_ledger)
        manifest["manual_review_summary"] = manual_review_ledger.get("summary", {})
    if (
        style_result.get("status") == "needs_clarification"
        and not args.allow_unresolved
        and args.output_policy != "review_draft"
    ):
        manifest.update(status="blocked", reason="style mapping needs clarification", questions_file=str(style_map))
        write_json(manifest_path, manifest); print(json.dumps(manifest, ensure_ascii=False)); return 4

    apply_cmd = [sys.executable, str(ROOT / "scripts" / "apply_format_spec.py"), str(application_input),
                 str(requirements_dir / "format-spec.json"), str(args.output), "--out-dir", str(apply_dir),
                 "--style-map", str(style_map), "--compliance-mode", execution_compliance_mode,
                 "--output-policy", args.output_policy,
                 "--capability-report", str(capability_report_path)]
    if not args.neutral_reference_docx:
        apply_cmd.append("--require-coverage")
    if args.neutral_reference_docx:
        apply_cmd.append("--neutral-reference")
    if has_official_template:
        apply_cmd += ["--official-template", str(official_template)]
    if args.render_report:
        apply_cmd += ["--render-report", str(args.render_report)]
    if args.preview_placeholders or args.output_policy == "review_draft":
        apply_cmd.append("--preview-placeholders")
    if args.output_policy == "review_draft":
        apply_cmd += ["--manual-review-items", str(manual_review_items_path)]
    if args.semantic_review_runtime:
        apply_cmd += [
            "--semantic-review-runtime", args.semantic_review_runtime,
            "--semantic-review-model", args.semantic_review_model,
            "--case-id", args.case_id or "standalone",
        ]
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
    application_failed = bool(result.returncode or not report)
    if report:
        if args.output_policy == "review_draft":
            application_failed = application_failed or report.get("review_draft_ready") is not True
        else:
            application_failed = application_failed or report.get("valid") is not True
            application_failed = application_failed or (
                execution_compliance_mode == "full" and report.get("format_ready") is not True
            )
    if application_failed:
        manifest.update(status="failed", reason="application or validation failed",
                        validation_report=str(apply_dir / "validation-report.json"),
                        diagnostic_draft_generated=(
                            bool(report.get("diagnostic_draft_generated"))
                            if args.output_policy == "review_draft" and report else False
                        ),
                        review_draft_ready=False)
        write_json(manifest_path, manifest); print(json.dumps(manifest, ensure_ascii=False)); return result.returncode or 5

    comparison_path = apply_dir / "format-comparison.json"
    comparison_markdown = apply_dir / "FORMAT-COMPARISON.md"
    if args.output_policy == "review_draft":
        # A red-marked draft is deliberately not a submission artifact.  Do
        # not let its review page, placeholders, or unresolved semantic
        # content masquerade as an official-template comparison.  Emit an
        # explicit deferred receipt so downstream tooling can distinguish
        # “not run by policy” from a missing or failed comparison.
        comparison = {
            "schema_version": "1.0",
            "status": "review_draft_pending",
            "blocking": False,
            "output_policy": "review_draft",
            "reason": "strict official format comparison is deferred until manual review is complete",
            "manual_review_items": str(manual_review_items_path),
            "inputs": {
                "generated_docx": file_record(args.output),
                "format_spec": file_record(requirements_dir / "format-spec.json"),
                "official_template": file_record(official_template) if has_official_template else None,
            },
            "summary": {"manual_review_required": True},
        }
        write_json(comparison_path, comparison)
        atomic_write_text(
            comparison_markdown,
            "# FORMAT-COMPARISON\n\n"
            "Status: `review_draft_pending`\n\n"
            "Strict official-template comparison is deferred until the red manual-review "
            "markers are resolved. This file is not release evidence.\n",
        )
        steps.append({
            "name": "post_generation_format_comparison",
            "command": [],
            "returncode": 0,
            "status": "review_draft_pending",
            "blocking": False,
        })
        comparison_result = None
    else:
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
    if args.template_profile and not args.neutral_reference_docx and args.output_policy == "review_draft":
        template_audit_path = apply_dir / "template-audit.json"
        official_template_structure_valid = None
        official_template_submission_ready = False
        official_template_audit_status = "review_draft_pending"
        write_json(template_audit_path, {
            "schema_version": "1.0",
            "status": "review_draft_pending",
            "submission_ready": False,
            "template_validation": {
                "official_template_structure_valid": None,
                "deferred": True,
            },
            "reason": "official-template submission audit is deferred until manual review is complete",
            "manual_review_items": str(manual_review_items_path),
        })
        steps.append({
            "name": "official_template_audit",
            "command": [],
            "returncode": 0,
            "status": "review_draft_pending",
            "blocking": False,
        })
    elif args.template_profile and not args.neutral_reference_docx:
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
    if args.output_policy == "review_draft":
        final_submission_ready = False
    if confirmed_semantic_issue_ids:
        final_submission_ready = False
    if official_template_submission_ready is not None:
        final_submission_ready = final_submission_ready and official_template_submission_ready
    if args.output_policy == "review_draft" and manual_review_items_path.is_file():
        try:
            final_manual_ledger = read_json(manual_review_items_path)
        except (OSError, ValueError):
            final_manual_ledger = None
        if isinstance(final_manual_ledger, dict):
            manifest["manual_review_summary"] = final_manual_ledger.get("summary", {})
    final_format_ready = bool(
        report.get("valid")
        and report.get("format_ready")
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
        "draft_manual_review"
        if args.output_policy == "review_draft" else (
            "completed_with_confirmed_semantic_issues"
            if confirmed_semantic_issue_ids else "completed"
        )
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
                    output_policy=args.output_policy,
                    requested_compliance_mode=args.compliance_mode,
                    execution_compliance_mode=execution_compliance_mode,
                    manual_review_items=str(manual_review_items_path) if args.output_policy == "review_draft" else None,
                    manual_review_summary=manifest.get("manual_review_summary", {}),
                    diagnostic_draft_generated=(
                        bool(report.get("diagnostic_draft_generated"))
                        if args.output_policy == "review_draft" else False
                    ),
                    review_draft_ready=(
                        args.output_policy == "review_draft"
                        and bool(report.get("review_draft_ready"))
                    ),
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
                        "manual_review_required" if args.output_policy == "review_draft" else (
                            "confirmed_semantic_issues" if confirmed_semantic_issue_ids else (
                            report.get("submission_status") or (
                                "submission_ready" if final_submission_ready else "not_submission_ready"
                            )
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
