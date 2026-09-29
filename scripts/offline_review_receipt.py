#!/usr/bin/env python3
"""Verify a current-run offline packet merge, without selecting a model.

This is a byte/contract receipt for a non-release review draft. It cannot
attest that an independent agent reviewed the packet or prove provider/model
identity. The same verifier is used by the pipeline and this portable CLI.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from host_review_contract import SUPPORTED_HOST_REVIEW_CONTRACTS
from semantic_contract import sha256_file, sha256_json, strict_json_dumps, strict_json_read


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
