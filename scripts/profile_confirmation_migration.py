"""Validate a user-confirmed profile overlay without changing a parent review.

This migration is deliberately narrower than a new semantic review. The parent
request remains byte-for-byte authoritative for semantic review. A separately
user-confirmed profile may be used for document generation only when all of its
non-provenance values match the parent profile and it is bound to the exact
fixed DOCX that supplied the parent's structure evidence.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from semantic_contract import strict_json_read


POLICY = "user_confirmed_profile_provenance_projection_v1"
CONFIRMATION_SCOPE = "profile_values_apply_to_exact_fixed_source_docx"
ALLOWED_PROFILE_PATHS = [
    "provenance.source_document",
    "provenance.source_sha256",
    "provenance.trust.source",
    "provenance.trust.note",
    "cover_metadata.trust.source",
    "cover_metadata.trust.note",
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_record(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    return {
        "path": str(resolved),
        "bytes": resolved.stat().st_size,
        "sha256": sha256_file(resolved),
    }


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def profile_semantic_projection(profile: dict[str, Any]) -> dict[str, Any]:
    """Remove only the six provenance fields explicitly authorized to differ."""
    projected = copy.deepcopy(profile)
    provenance = projected.get("provenance")
    if isinstance(provenance, dict):
        provenance.pop("source_document", None)
        provenance.pop("source_sha256", None)
        trust = provenance.get("trust")
        if isinstance(trust, dict):
            trust.pop("source", None)
            trust.pop("note", None)
    cover = projected.get("cover_metadata")
    if isinstance(cover, dict):
        trust = cover.get("trust")
        if isinstance(trust, dict):
            trust.pop("source", None)
            trust.pop("note", None)
    return projected


def _record_matches(value: Any, path: Path, *, label: str) -> None:
    expected = file_record(path)
    if not isinstance(value, dict) or any(value.get(k) != expected[k] for k in expected):
        raise ValueError(f"profile confirmation migration {label} record mismatch")


def _source_record_matches(value: Any, path: Path, *, label: str) -> None:
    expected = file_record(path)
    if (
        not isinstance(value, dict)
        or value.get("path") != expected["path"]
        or value.get("bytes") != expected["bytes"]
        or value.get("sha256") != expected["sha256"]
    ):
        raise ValueError(f"profile confirmation migration {label} binding mismatch")


def validate_profile_confirmation_migration(
    *,
    parent_receipt_path: Path,
    current_source_path: Path,
    requirements_path: Path,
    confirmed_profile_path: Path,
    confirmation_record_path: Path,
    expected_run_id: str,
) -> dict[str, Any]:
    """Prove that a confirmed profile changes only approved provenance fields.

    The original profile is taken from the canonical parent run directory and
    must match the complete profile embedded in the immutable parent request.
    The user-confirmed profile is validated as an explicit profile by the main
    pipeline before this function is called. This function additionally binds
    it to the exact current DOCX, verifies the user's confirmation record, and
    verifies that all profile fields outside the six allow-listed metadata
    paths are identical.
    """
    parent_receipt = parent_receipt_path.expanduser().resolve()
    if parent_receipt.name != "merge-receipt.json":
        raise ValueError("profile confirmation migration requires canonical parent merge-receipt.json")
    parent_review_dir = parent_receipt.parent
    parent_work = parent_review_dir.parent.parent
    if parent_review_dir != parent_work / "review" / "requirements":
        raise ValueError("profile confirmation migration parent receipt is outside its canonical run")

    source_path = current_source_path.expanduser().resolve()
    requirements_path = requirements_path.expanduser().resolve()
    confirmed_path = confirmed_profile_path.expanduser().resolve()
    confirmation_path = confirmation_record_path.expanduser().resolve()
    if source_path.suffix.lower() != ".docx":
        raise ValueError("profile confirmation migration requires the fixed DOCX as current source")

    parent_profile_path = parent_work / "thesis-profile.json"
    parent_pipeline_path = parent_work / "pipeline-manifest.json"
    parent_extraction_path = parent_review_dir / "extraction-manifest.json"
    parent_request_path = parent_review_dir / "llm-request.json"
    required = (parent_profile_path, parent_pipeline_path, parent_extraction_path,
                parent_request_path, confirmed_path, confirmation_path,
                source_path, requirements_path)
    if any(not path.is_file() for path in required):
        missing = [str(path) for path in required if not path.is_file()]
        raise ValueError("profile confirmation migration evidence is missing: " + ", ".join(missing))

    parent_profile = strict_json_read(parent_profile_path)
    confirmed_profile = strict_json_read(confirmed_path)
    confirmation = strict_json_read(confirmation_path)
    parent_request = strict_json_read(parent_request_path)
    parent_pipeline = strict_json_read(parent_pipeline_path)
    parent_extraction = strict_json_read(parent_extraction_path)

    parent_runtime = parent_request.get("runtime_context")
    if not isinstance(parent_runtime, dict):
        raise ValueError("profile confirmation migration parent request has no runtime context")
    if parent_request.get("provenance", {}).get("run_id") != expected_run_id:
        raise ValueError("profile confirmation migration parent request run ID mismatch")
    if parent_pipeline.get("run_id") != expected_run_id:
        raise ValueError("profile confirmation migration parent pipeline run ID mismatch")
    if parent_runtime.get("confirmed_thesis_profile") != parent_profile:
        raise ValueError("parent profile differs from the profile embedded in its original request")
    parent_profile_hash = sha256_file(parent_profile_path)
    if parent_runtime.get("thesis_profile_sha256") != parent_profile_hash:
        raise ValueError("parent profile hash differs from the original request binding")
    _record_matches(
        (parent_pipeline.get("inputs") or {}).get("thesis_profile"),
        parent_profile_path, label="parent profile",
    )

    parent_source = (parent_pipeline.get("inputs") or {}).get("source")
    parent_profile_provenance = parent_profile.get("provenance")
    if (
        not isinstance(parent_source, dict)
        or not isinstance(parent_profile_provenance, dict)
        or parent_profile_provenance.get("source_document") != parent_source.get("path")
        or parent_profile_provenance.get("source_sha256") != parent_source.get("sha256")
    ):
        raise ValueError("parent profile is not bound to the source recorded by its pipeline")

    source_tex_path = Path(str(parent_source.get("path") or "")).expanduser().resolve()
    if (source_tex_path.suffix.lower() != ".tex"
            or parent_pipeline.get("pipeline_level") != "latex_end_to_end"):
        raise ValueError("parent pipeline does not record a LaTeX source-to-DOCX run")
    _source_record_matches(parent_source, source_tex_path, label="parent source TEX")
    conversion_steps = [
        step for step in parent_pipeline.get("steps", [])
        if isinstance(step, dict)
        and step.get("name") in {"latex_to_docx", "latex_to_docx_reuse"}
    ] if isinstance(parent_pipeline.get("steps"), list) else []
    if len(conversion_steps) != 1:
        raise ValueError("parent pipeline must record exactly one source TEX-to-DOCX step")
    conversion_step = conversion_steps[0]
    if (conversion_step.get("returncode") != 0
            or (conversion_step.get("name") == "latex_to_docx_reuse"
                and conversion_step.get("reused") is not True)):
        raise ValueError("parent TEX-to-DOCX step is not a successful recorded conversion/reuse")

    current_source = file_record(source_path)
    current_requirements = file_record(requirements_path)
    _record_matches(parent_pipeline.get("intermediate_docx"), source_path,
                    label="parent intermediate DOCX")
    _source_record_matches(conversion_step.get("artifact"), source_path,
                           label="parent conversion-step DOCX")
    parent_sources = parent_extraction.get("sources")
    if not isinstance(parent_sources, dict):
        raise ValueError("parent extraction manifest has no source inventory")
    _source_record_matches(parent_sources.get("structure_source"), source_path,
                           label="parent structure DOCX")
    _source_record_matches(parent_sources.get("requirements_source"), requirements_path,
                           label="parent requirements DOCX")
    parent_requirements = (parent_pipeline.get("inputs") or {}).get("requirements")
    _record_matches(parent_requirements, requirements_path, label="requirements DOCX")

    runtime_inventory = parent_runtime.get("runtime_inventory")
    anchor_inventory = runtime_inventory.get("anchor_inventory") if isinstance(runtime_inventory, dict) else None
    if not isinstance(anchor_inventory, dict):
        raise ValueError("parent request has no source-bound anchor inventory")
    _source_record_matches(anchor_inventory.get("source"), source_path,
                           label="parent request source structure")

    if not isinstance(confirmed_profile, dict):
        raise ValueError("confirmed profile is not a JSON object")
    confirmed_provenance = confirmed_profile.get("provenance")
    confirmed_trust = confirmed_provenance.get("trust") if isinstance(confirmed_provenance, dict) else None
    cover_trust = (confirmed_profile.get("cover_metadata") or {}).get("trust")
    if (
        not isinstance(confirmed_provenance, dict)
        or confirmed_provenance.get("source_document") != str(source_path)
        or confirmed_provenance.get("source_sha256") != current_source["sha256"]
        or not isinstance(confirmed_trust, dict)
        or confirmed_trust.get("source") != "user_confirmed"
        or confirmed_trust.get("confirmed") is not True
        or not isinstance(cover_trust, dict)
        or cover_trust.get("source") != "user_confirmed"
        or cover_trust.get("confirmed") is not True
    ):
        raise ValueError("confirmed profile is not user-confirmed and bound to the exact current DOCX")

    parent_semantic = profile_semantic_projection(parent_profile)
    confirmed_semantic = profile_semantic_projection(confirmed_profile)
    semantic_projection_sha256 = _canonical_sha256(parent_semantic)
    if parent_semantic != confirmed_semantic:
        raise ValueError("profile confirmation migration changes semantic profile fields")

    if not isinstance(confirmation, dict):
        raise ValueError("profile confirmation record is not a JSON object")
    if (
        confirmation.get("policy") != POLICY
        or confirmation.get("confirmation_scope") != CONFIRMATION_SCOPE
        or not isinstance(confirmation.get("approval_reference"), dict)
        or not confirmation["approval_reference"].get("statement")
    ):
        raise ValueError("profile confirmation record does not state the approved migration scope")
    _record_matches(confirmation.get("parent_profile"), parent_profile_path,
                    label="confirmation parent profile")
    _record_matches(confirmation.get("parent_pipeline_manifest"), parent_pipeline_path,
                    label="confirmation parent pipeline manifest")
    _record_matches(confirmation.get("source_tex"), source_tex_path,
                    label="confirmation sample TEX")
    _record_matches(confirmation.get("confirmed_profile"), confirmed_path,
                    label="confirmation user-confirmed profile")
    _record_matches(confirmation.get("source_docx"), source_path,
                    label="confirmation fixed source DOCX")
    _record_matches(confirmation.get("requirements_docx"), requirements_path,
                    label="confirmation requirements DOCX")
    if confirmation.get("semantic_values_sha256") != semantic_projection_sha256:
        raise ValueError("profile confirmation semantic-values digest mismatch")

    return {
        "policy": POLICY,
        "confirmation_scope": CONFIRMATION_SCOPE,
        "parent_run_id": expected_run_id,
        "parent_profile": file_record(parent_profile_path),
        "confirmed_profile": file_record(confirmed_path),
        "confirmation_record": file_record(confirmation_path),
        "parent_pipeline_manifest": file_record(parent_pipeline_path),
        "sample_tex": file_record(source_tex_path),
        "fixed_source_docx": current_source,
        "requirements_docx": current_requirements,
        "semantic_values_sha256": semantic_projection_sha256,
        "allowed_profile_changes": list(ALLOWED_PROFILE_PATHS),
        "semantic_values_identical": True,
        "review_request_profile": "verified_parent_profile_unchanged",
        "generation_profile": "user_confirmed_profile",
        "model_request_made": False,
        "submission_ready": False,
    }
