#!/usr/bin/env python3
"""Verify a current-run offline packet merge, without selecting a model.

This is a byte/contract receipt for a non-release review draft. It cannot
attest that an independent agent reviewed the packet or prove provider/model
identity. The same verifier is used by the pipeline and this portable CLI.
"""
from __future__ import annotations

import argparse
import tempfile
from pathlib import Path
from typing import Any

from host_review_contract import SUPPORTED_HOST_REVIEW_CONTRACTS
from semantic_contract import (
    request_body_sha256, request_envelope_sha256,
    sha256_file, sha256_json, strict_json_dumps, strict_json_read,
)


def _path_under(path: Path, root: Path, *, label: str) -> Path:
    resolved = path.expanduser().resolve()
    root = root.resolve()
    if resolved == root or root not in resolved.parents:
        raise ValueError(f"{label} must remain inside the work directory: {resolved}")
    return resolved


def _record(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    return {"path": str(resolved), "bytes": resolved.stat().st_size,
            "sha256": sha256_file(resolved)}


def _object(path: Path, *, label: str) -> dict[str, Any]:
    value = strict_json_read(path)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def validate_offline_merge_receipt(
    *, response_path: Path, receipt_path: Path,
    extraction_manifest: dict[str, Any], work: Path,
) -> dict[str, Any]:
    """Bind current-run response, merge receipt, ledger and commit marker."""
    work = work.resolve()
    response_path = _path_under(response_path, work, label="offline merged response")
    receipt_path = _path_under(receipt_path, work, label="offline merge receipt")
    if not isinstance(extraction_manifest, dict):
        raise ValueError("offline extraction manifest must be a JSON object")
    response = _object(response_path, label="offline merged response")
    receipt = _object(receipt_path, label="offline merge receipt")
    expected_run_id = extraction_manifest.get("run_id")
    expected_body_sha = (extraction_manifest.get("llm_request_body_sha256")
                         or extraction_manifest.get("llm_request_sha256"))
    expected_envelope_sha = extraction_manifest.get("llm_request_envelope_sha256")
    expected_file_sha = extraction_manifest.get("llm_request_file_sha256")
    if not expected_run_id or not expected_body_sha:
        raise ValueError("offline review extraction has no fresh run identity")
    if (
        receipt.get("status") != "merged"
        or receipt.get("protocol") != "host_agent_semantic_review"
        or receipt.get("run_id") != expected_run_id
        or receipt.get("request_body_sha256") != expected_body_sha
        or (expected_envelope_sha and receipt.get("request_envelope_sha256") != expected_envelope_sha)
        or (expected_file_sha and receipt.get("request_file_sha256") != expected_file_sha)
        or receipt.get("runtime_context") != extraction_manifest.get("runtime_context")
        or receipt.get("aggregate_sha256") != sha256_json(response)
        or response.get("contract_version") not in SUPPORTED_HOST_REVIEW_CONTRACTS
    ):
        raise ValueError("offline merge receipt is not bound to the current extraction and response")
    merged_path = receipt.get("merged_response_path")
    if not isinstance(merged_path, str) or Path(merged_path).resolve() != response_path:
        raise ValueError("offline merge receipt points to a different response")
    review_dir = work / "review" / "requirements"
    if receipt_path != review_dir / "merge-receipt.json":
        raise ValueError("offline merge receipt is outside the current review directory")
    ledger_value = receipt.get("semantic_review_ledger_path")
    if not isinstance(ledger_value, str) or not ledger_value.strip():
        raise ValueError("offline semantic ledger path is missing")
    ledger_path = _path_under(Path(ledger_value), work, label="offline semantic ledger")
    if ledger_path != review_dir / "semantic-review-ledger.json":
        raise ValueError("offline semantic ledger is outside the current review directory")
    ledger = _object(ledger_path, label="offline semantic ledger")
    if sha256_json(ledger) != receipt.get("semantic_review_ledger_sha256"):
        raise ValueError("offline semantic ledger is missing or changed")
    if ledger.get("response_sha256") != receipt.get("aggregate_sha256"):
        raise ValueError("offline semantic ledger is not bound to the merged response")
    marker_value = receipt.get("merge_commit_path")
    if not isinstance(marker_value, str) or not marker_value.strip():
        raise ValueError("offline merge commit marker path is missing")
    marker_path = _path_under(Path(marker_value), work, label="offline merge commit marker")
    if marker_path != review_dir / "merge-commit.json":
        raise ValueError("offline merge commit marker is missing or misplaced")
    marker = _object(marker_path, label="offline merge commit marker")
    if (
        marker.get("status") != "committed"
        or marker.get("protocol") != "host_agent_semantic_review_merge"
        or marker.get("run_id") != expected_run_id
        or marker.get("aggregate_sha256") != receipt.get("aggregate_sha256")
    ):
        raise ValueError("offline merge commit marker is not bound to this run")
    artifacts = marker.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != 3:
        raise ValueError("offline merge commit marker must cover exactly three artifacts")
    recorded: dict[Path, str] = {}
    for item in artifacts:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ValueError("offline merge commit marker has an invalid artifact")
        path = _path_under(review_dir.parent / item["path"], work,
                           label="offline merge artifact")
        digest = item.get("sha256")
        if (path in recorded or not isinstance(digest, str) or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)):
            raise ValueError("offline merge commit marker has a duplicate or invalid digest")
        if not path.is_file() or sha256_file(path) != digest:
            raise ValueError("offline merge artifact bytes changed after commit")
        recorded[path] = digest
    if set(recorded) != {response_path, receipt_path, ledger_path}:
        raise ValueError("offline merge commit marker contains unrelated artifacts")
    return {
        "status": "offline_merged_without_independent_review",
        "submission_ready": False,
        "independent_review_verified": False,
        "provider_model_verified": False,
        "response": _record(response_path),
        "merge_receipt": _record(receipt_path),
        "semantic_review_ledger": _record(ledger_path),
        "merge_commit_marker": _record(marker_path),
        "run_id": expected_run_id,
        "request_body_sha256": expected_body_sha,
        "aggregate_sha256": receipt.get("aggregate_sha256"),
    }


def validate_offline_parent_merge_receipt(
    *, parent_receipt_path: Path,
    child_receipt_path: Path | None = None,
    child_response_path: Path | None = None,
    child_work: Path | None = None,
    current_code_fingerprint_sha256: str | None = None,
) -> dict[str, Any]:
    """Verify an append-only declaration projection and return its request identity.

    The returned code fingerprint is inherited only to reconstruct the exact
    request already reviewed by the parent run. The caller must still execute
    a fresh extraction under current code and compare its complete request
    hashes before it may consume the child response.
    """
    parent_path = parent_receipt_path.expanduser().resolve()
    if parent_path.name != "merge-receipt.json":
        raise ValueError("parent merge receipt must be the canonical merge-receipt.json")
    parent_dir = parent_path.parent
    parent_work = parent_dir.parent.parent
    if parent_dir != parent_work / "review" / "requirements":
        raise ValueError("parent merge receipt is outside its canonical review directory")
    parent_request_path = parent_dir / "llm-request.json"
    parent_extraction_path = parent_dir / "extraction-manifest.json"
    parent = _object(parent_path, label="parent merge receipt")
    request = _object(parent_request_path, label="parent semantic request")
    extraction = _object(parent_extraction_path, label="parent extraction manifest")
    if not isinstance(request.get("runtime_context"), dict):
        raise ValueError("parent semantic request has no runtime context")
    provenance = request.get("provenance")
    if not isinstance(provenance, dict):
        raise ValueError("parent semantic request has no provenance")
    request_sha = request_body_sha256(request)
    envelope_sha = request_envelope_sha256(request)
    request_file_sha = sha256_file(parent_request_path)
    expected_run_id = parent.get("run_id")
    expected_code_fingerprint = request["runtime_context"].get("code_fingerprint_sha256")
    if (
        parent.get("status") != "merged"
        or parent.get("protocol") != "host_agent_semantic_review"
        or not isinstance(expected_run_id, str) or not expected_run_id
        or provenance.get("run_id") != expected_run_id
        or provenance.get("request_sha256") != request_sha
        or parent.get("request_sha256") != request_sha
        or parent.get("request_body_sha256") != request_sha
        or parent.get("request_envelope_sha256") != envelope_sha
        or parent.get("request_file_sha256") != request_file_sha
        or parent.get("runtime_context") != request.get("runtime_context")
        or not isinstance(expected_code_fingerprint, str)
        or len(expected_code_fingerprint) != 64
        or any(char not in "0123456789abcdef" for char in expected_code_fingerprint)
        or extraction.get("run_id") != expected_run_id
        or extraction.get("llm_request_body_sha256", extraction.get("llm_request_sha256")) != request_sha
        or extraction.get("llm_request_envelope_sha256") != envelope_sha
        or extraction.get("llm_request_file_sha256") != request_file_sha
        or extraction.get("runtime_context") != request.get("runtime_context")
    ):
        raise ValueError("parent receipt, request, and extraction are not bound to one run")

    parent_response_path = Path(str(parent.get("merged_response_path") or "")).expanduser().resolve()
    parent_result = validate_offline_merge_receipt(
        response_path=parent_response_path,
        receipt_path=parent_path,
        extraction_manifest=extraction,
        work=parent_work,
    )
    parent_manifest = _object(
        parent_dir / "host-agent-review-manifest.json",
        label="parent host-agent review manifest",
    )
    parent_response_names = parent_manifest.get("response_files")
    parent_chunk_count = parent_manifest.get("chunk_count")
    if (
        not isinstance(parent_response_names, list)
        or not parent_response_names
        or any(not isinstance(name, str) or not name for name in parent_response_names)
        or len(parent_response_names) != len(set(parent_response_names))
        or isinstance(parent_chunk_count, bool)
        or not isinstance(parent_chunk_count, int)
        or parent_chunk_count != len(parent_response_names)
        or parent.get("chunk_count") != parent_chunk_count
    ):
        raise ValueError("parent response packet manifest is incomplete")
    raw_responses: list[dict[str, Any]] = []
    for index, name in enumerate(parent_response_names, start=1):
        path = (parent_dir / name).resolve()
        if parent_dir not in path.parents or not path.is_file():
            raise ValueError("parent raw response packet is missing or escapes its review directory")
        raw_responses.append({
            "chunk_index": index,
            "path": str(path),
            "sha256": sha256_file(path),
        })

    child_result: dict[str, Any] | None = None
    if (child_receipt_path is None) != (child_response_path is None):
        raise ValueError("child response and child merge receipt must be supplied together")
    if child_receipt_path is not None and child_response_path is not None:
        if child_work is None:
            raise ValueError("child work directory is required to verify a derived merge")
        if (
            not isinstance(current_code_fingerprint_sha256, str)
            or len(current_code_fingerprint_sha256) != 64
            or any(char not in "0123456789abcdef" for char in current_code_fingerprint_sha256)
        ):
            raise ValueError("current fixed projection code fingerprint is required for child replay")
        child_path = _path_under(child_receipt_path, child_work, label="child merge receipt")
        child_response = _path_under(child_response_path, child_work, label="child merged response")
        child = _object(child_path, label="child merge receipt")
        child_value = _object(child_response, label="child merged response")
        derivation = child.get("derivation")
        if not isinstance(derivation, dict):
            raise ValueError("child merge receipt has no append-only parent derivation")
        declared_parent_path = Path(str(derivation.get("parent_merge_receipt_path") or "")).expanduser().resolve()
        declared_parent_digest = derivation.get("parent_merge_receipt_sha256")
        if (
            derivation.get("kind") != "append_only_deterministic_reprojection"
            or derivation.get("model_request_made") is not False
            or derivation.get("run_id_preserved") is not True
            or derivation.get("raw_chunk_responses_preserved") is not True
            or declared_parent_path != parent_path
            or declared_parent_digest != sha256_file(parent_path)
            or derivation.get("parent_aggregate_sha256") != parent.get("aggregate_sha256")
            or derivation.get("parent_run_id") != expected_run_id
            or derivation.get("parent_request_code_fingerprint_sha256") != expected_code_fingerprint
            or derivation.get("derivation_code_fingerprint_sha256") != current_code_fingerprint_sha256
            or child.get("run_id") != expected_run_id
            or child.get("request_body_sha256") != request_sha
            or child.get("request_envelope_sha256") != envelope_sha
            or child.get("request_file_sha256") != request_file_sha
            or child.get("runtime_context") != request.get("runtime_context")
            or child.get("aggregate_sha256") != sha256_json(child_value)
            or child.get("merged_response_path") != str(child_response)
            or child_value.get("provenance") != provenance
            or child_value.get("contract_version") != request.get("contract_version")
        ):
            raise ValueError("child merge receipt is not bound to the verified parent run")
        if child.get("response_projection_policy") != "explicit_deterministic_projector_v1":
            raise ValueError("child merge receipt does not name the deterministic projection policy")
        recorded_inputs = child.get("input_response_files")
        if not isinstance(recorded_inputs, list) or len(recorded_inputs) != len(raw_responses):
            raise ValueError("child merge receipt does not preserve every parent response packet")
        inputs_by_index: dict[int, dict[str, Any]] = {}
        for item in recorded_inputs:
            if not isinstance(item, dict) or isinstance(item.get("chunk_index"), bool):
                raise ValueError("child response packet lineage is malformed")
            index = item.get("chunk_index")
            if not isinstance(index, int) or index in inputs_by_index:
                raise ValueError("child response packet lineage has duplicate or invalid indexes")
            inputs_by_index[index] = item
        for raw in raw_responses:
            item = inputs_by_index.get(raw["chunk_index"])
            if (
                not isinstance(item, dict)
                or Path(str(item.get("path") or "")).expanduser().resolve() != Path(raw["path"])
                or item.get("sha256") != raw["sha256"]
            ):
                raise ValueError("child merge receipt does not preserve the original raw packet bytes")
        projections = child.get("declaration_source_text_projection")
        if not isinstance(projections, list) or not projections:
            raise ValueError("child merge has no fixed declaration projection audit")
        for item in projections:
            if not isinstance(item, dict):
                raise ValueError("child declaration projection audit is malformed")
            index = item.get("chunk_index")
            raw = inputs_by_index.get(index) if isinstance(index, int) else None
            if (
                item.get("rule_id") != "fixed_declaration_source_text_materialization_v1"
                or item.get("rule_version") != 2
                or not isinstance(raw, dict)
                or item.get("raw_response_file_sha256") != raw.get("sha256")
                or not isinstance(item.get("projected_response_sha256"), str)
                or len(item["projected_response_sha256"]) != 64
                or item.get("projected_response_serialization") != "canonical_json_utf8_v1"
                or item.get("projected_response_bytes_sha256") != item.get("projected_response_sha256")
            ):
                raise ValueError("child projection audit is not an exact fixed declaration projection")

        # A child hash field is only a claim. Re-run the one supported
        # projector over the immutable parent packet bytes using the fixed
        # current code, then compare both its per-packet serialized bytes and
        # the complete merged response file byte for byte.
        from host_agent_bridge import _materialize_fixed_declaration_source_text
        from requirements_engine import merge_host_agent_review_packets

        with tempfile.TemporaryDirectory(prefix="thesis-forge-projection-replay-") as temp_root:
            replay_artifacts = Path(temp_root) / "review" / "requirements"
            replay_response = replay_artifacts / "host-agent-response.json"
            _replayed, replay_metadata = merge_host_agent_review_packets(
                parent_dir,
                response_out=replay_response,
                response_projector=_materialize_fixed_declaration_source_text,
                artifact_dir=replay_artifacts,
                parent_receipt_path=parent_path,
                derivation_code_fingerprint_sha256=current_code_fingerprint_sha256,
            )
            replay_projections = replay_metadata.get("declaration_source_text_projection")
            if not isinstance(replay_projections, list) or not replay_projections:
                raise ValueError("deterministic parent replay produced no fixed declaration projection")
            if replay_projections != projections:
                raise ValueError("child projection audit differs from deterministic parent replay")
            if sha256_file(replay_response) != sha256_file(child_response):
                raise ValueError("child merged response bytes differ from deterministic parent replay")
        child_result = {
            "receipt": _record(child_path),
            "response": _record(child_response),
            "projection_count": len(projections),
            "deterministic_replay_response_sha256": sha256_file(child_response),
            "deterministic_projection_replay_verified": True,
            "derivation_code_fingerprint_sha256": current_code_fingerprint_sha256,
        }

    return {
        "parent_merge_receipt": _record(parent_path),
        "parent_merged_response": _record(parent_response_path),
        "parent_request": _record(parent_request_path),
        "parent_request_sha256": request_sha,
        "parent_request_envelope_sha256": envelope_sha,
        "parent_request_file_sha256": request_file_sha,
        "parent_run_id": expected_run_id,
        "inherited_request_code_fingerprint_sha256": expected_code_fingerprint,
        "runtime_context": request.get("runtime_context"),
        "parent_verification": parent_result,
        "child_verification": child_result,
        "model_request_made": False,
        "submission_ready": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--extraction-manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        work = args.work_dir.resolve()
        extraction_path = _path_under(args.extraction_manifest, work,
                                      label="offline extraction manifest")
        if extraction_path != work / "review" / "requirements" / "extraction-manifest.json":
            raise ValueError("offline extraction manifest is outside the current review directory")
        result = validate_offline_merge_receipt(
            response_path=args.response, receipt_path=args.receipt,
            extraction_manifest=_object(extraction_path, label="offline extraction manifest"),
            work=work,
        )
    except (OSError, ValueError, TypeError) as exc:
        parser.error(str(exc))
    print(strict_json_dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
