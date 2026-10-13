"""Compile a narrow three-line table contract from bound source evidence.

This compiler consumes the requirement document named by the completed
extraction manifest, never the thesis being formatted. It recognizes only the
explicit conjunction of a three-line rule and stated outside/inside widths;
unknown wording or topology fails closed.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from docx import Document
from docx.text.paragraph import Paragraph
from semantic_contract import strict_json_read


NUMBER = r"([0-9]+(?:\.[0-9]+)?)"


def deterministic_requirement_subset(
    parent_spec: dict[str, Any], selected_roles: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Replay the supported child-spec projection from a fixed parent."""
    requirements = [
        copy.deepcopy(item)
        for item in parent_spec.get("requirements", [])
        if isinstance(item, dict)
        and item.get("role") in selected_roles
        and isinstance(item.get("source_scope"), dict)
    ]
    selected_instance_ids = {
        str(value)
        for item in requirements
        for value in item.get("field_instance_ids", [])
    }
    instances = [
        copy.deepcopy(item)
        for item in parent_spec.get("content_instances", [])
        if isinstance(item, dict) and item.get("id") in selected_instance_ids
    ]
    return requirements, instances


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _pipeline_manifest_binds_current_spec(
    pipeline_manifest_path: Path | None,
    *,
    extraction_run_id: str,
    original_source_path: Path,
    original_source_sha256: str,
    clauses_path: Path,
    format_spec_path: Path,
) -> bool:
    """Accept pipeline-owned spec enrichment only with exact current records."""
    if pipeline_manifest_path is None:
        return False
    try:
        pipeline = strict_json_read(pipeline_manifest_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(pipeline, dict) or pipeline.get("run_id") != extraction_run_id:
        return False

    def exact_record(record: Any, path: Path) -> bool:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            return False
        try:
            candidate = Path(record["path"]).expanduser().resolve(strict=True)
            expected = path.expanduser().resolve(strict=True)
            return (
                candidate == expected
                and record.get("bytes") == expected.stat().st_size
                and record.get("sha256") == sha256_file(expected)
            )
        except (OSError, RuntimeError, ValueError):
            return False

    inputs = pipeline.get("inputs")
    requirements_record = inputs.get("requirements") if isinstance(inputs, dict) else None
    return (
        exact_record(pipeline.get("format_spec_record"), format_spec_path)
        and exact_record(pipeline.get("source_clause_record"), clauses_path)
        and exact_record(requirements_record, original_source_path)
        and isinstance(requirements_record, dict)
        and requirements_record.get("sha256") == original_source_sha256
    )


def resolve_bound_requirement_docx(
    extraction_manifest_path: Path,
    clauses_path: Path,
    format_spec_path: Path,
    pipeline_manifest_path: Path | None = None,
) -> tuple[Path | None, dict[str, Any]]:
    """Resolve and verify the exact extracted requirements DOCX bundle."""
    try:
        manifest = strict_json_read(extraction_manifest_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return None, {"status": "blocked", "findings": [{
            "code": "requirements_extraction_manifest_unreadable",
            "reason": f"{type(exc).__name__}: {exc}",
        }]}
    if not isinstance(manifest, dict) or manifest.get("status") != "completed":
        return None, {"status": "blocked", "findings": [{
            "code": "requirements_extraction_manifest_incomplete",
            "reason": "the requirement extraction manifest is missing or not completed",
        }]}

    original_source_value = manifest.get("source_document")
    source_value = manifest.get("normalized_source_document") or original_source_value
    expected_source_sha = (manifest.get("normalized_source_sha256")
                           or manifest.get("source_sha256"))
    if not isinstance(source_value, str) or not source_value:
        return None, {"status": "blocked", "findings": [{
            "code": "requirements_source_path_missing",
            "reason": "the extraction manifest has no normalized requirement source path",
        }]}
    source_docx = Path(source_value)
    if not source_docx.is_file():
        return None, {"status": "blocked", "findings": [{
            "code": "requirements_source_missing",
            "path": source_value,
            "reason": "the manifest-bound requirement source is not readable in this environment",
        }]}

    actuals = {
        "source_sha256": sha256_file(source_docx),
        "requirement_clauses_sha256": sha256_file(clauses_path) if clauses_path.is_file() else None,
    }
    expected = {
        "source_sha256": expected_source_sha,
        "requirement_clauses_sha256": manifest.get("requirement_clauses_sha256"),
    }
    mismatches = [{"artifact": key, "expected": expected[key], "actual": actuals[key]}
                  for key in expected if not isinstance(expected[key], str)
                  or expected[key] != actuals[key]]
    if mismatches:
        return None, {"status": "blocked", "findings": [{
            "code": "requirements_extraction_binding_mismatch",
            "mismatches": mismatches,
            "reason": "source, clauses and format spec must match the completed extraction manifest byte-for-byte",
        }]}
    # Extraction records keep the submitted input identity separately from a
    # normalized DOCX. In particular, a .doc source may be converted to a
    # temporary .docx while the format spec remains bound to the original
    # source path. Verify both records and compare the spec path to the
    # submitted input, while returning the normalized DOCX for OOXML parsing.
    lineage_source = source_docx
    actual_original_source_sha: str | None = None
    if isinstance(original_source_value, str) and original_source_value:
        original_source = Path(original_source_value)
        if not original_source.is_file():
            return None, {"status": "blocked", "findings": [{
                "code": "requirements_original_source_missing",
                "path": original_source_value,
                "reason": "the original requirement input named by the extraction manifest is unavailable",
            }]}
        lineage_source = original_source
        actual_original_source_sha = sha256_file(original_source)
        expected_original_sha = manifest.get("source_sha256")
        if not isinstance(expected_original_sha, str) or expected_original_sha != actual_original_source_sha:
            return None, {"status": "blocked", "findings": [{
                "code": "requirements_original_source_binding_mismatch",
                "expected": expected_original_sha,
                "actual": actual_original_source_sha,
                "reason": "the original requirement input path and hash must match the extraction manifest",
            }]}
    try:
        spec = strict_json_read(format_spec_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return None, {"status": "blocked", "findings": [{
            "code": "format_spec_unreadable",
            "reason": f"{type(exc).__name__}: {exc}",
        }]}
    provenance = spec.get("semantic_review_provenance") if isinstance(spec, dict) else None
    spec_source = spec.get("source_document") if isinstance(spec, dict) else None
    base_lineage_valid = (
        isinstance(spec, dict)
        and spec.get("run_id") == manifest.get("run_id")
        and isinstance(spec_source, str)
        and Path(spec_source).resolve() == lineage_source.resolve()
    )
    current_spec_sha = sha256_file(format_spec_path)
    projection = spec.get("source_projection") if isinstance(spec, dict) else None
    projection_valid = False
    projection_reason = "source_projection_missing"
    if isinstance(projection, dict):
        projection_reason = "source_projection_invalid"
        parent = projection.get("parent_format_spec")
        selected_roles = projection.get("selected_roles")
        if (
            projection.get("protocol") == "deterministic_requirement_subset_v1"
            and isinstance(parent, dict)
            and isinstance(parent.get("path"), str)
            and isinstance(parent.get("sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", parent["sha256"])
            and isinstance(selected_roles, list)
            and selected_roles
            and all(isinstance(value, str) and value for value in selected_roles)
            and len(selected_roles) == len(set(selected_roles))
        ):
            try:
                parent_path = Path(parent["path"]).expanduser().resolve(strict=True)
                expected_parent_path = (
                    extraction_manifest_path.expanduser().resolve().parent / "format-spec.json"
                ).resolve(strict=True)
                parent_bytes = parent_path.read_bytes()
                parent_spec = strict_json_read(parent_path)
            except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
                parent_bytes = None
                expected_parent_path = None
                parent_spec = None
            if parent_bytes is None:
                projection_reason = "source_projection_parent_unavailable"
            elif parent_path != expected_parent_path:
                projection_reason = "source_projection_parent_path_not_canonical"
            elif hashlib.sha256(parent_bytes).hexdigest() != parent["sha256"]:
                projection_reason = "source_projection_parent_hash_mismatch"
            elif manifest.get("format_spec_sha256") != parent["sha256"]:
                projection_reason = "source_projection_parent_not_bound_to_extraction_manifest"
            elif not isinstance(parent_spec, dict):
                projection_reason = "source_projection_parent_spec_malformed"
            elif (
                not base_lineage_valid
                or parent_spec.get("run_id") != manifest.get("run_id")
                or parent_spec.get("source_document") != spec_source
                or parent_spec.get("analysis_mode") not in {"rule_only", "known_template"}
                or spec.get("analysis_mode") != parent_spec.get("analysis_mode")
            ):
                projection_reason = "source_projection_parent_lineage_mismatch"
            elif projection.get("projection_code_sha256") != sha256_file(Path(__file__).resolve()):
                projection_reason = "source_projection_code_identity_mismatch"
            else:
                expected_requirements, expected_instances = deterministic_requirement_subset(
                    parent_spec, selected_roles,
                )
                expected_projection_sha256 = hashlib.sha256(
                    json.dumps(
                        {"requirements": expected_requirements,
                         "content_instances": expected_instances},
                        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                selected_instance_ids = {
                    str(value)
                    for item in expected_requirements
                    for value in item.get("field_instance_ids", [])
                }
                projection_valid = (
                    spec.get("requirements") == expected_requirements
                    and spec.get("content_instances") == expected_instances
                    and projection.get("projected_payload_sha256") == expected_projection_sha256
                    and spec.get("roles") == {}
                    and spec.get("status") == "rule_resolved"
                    and len(expected_requirements) > 0
                    and selected_instance_ids == {
                        str(item.get("id")) for item in expected_instances
                    }
                )
                projection_reason = (
                    "deterministic_role_subset_replayed_and_hashed_from_manifest_bound_parent"
                    if projection_valid else "source_projection_output_differs_from_parent_replay"
                )
    semantic_lineage_valid = (
        isinstance(provenance, dict)
        and provenance.get("run_id") == manifest.get("run_id")
        and provenance.get("source_sha256") == (
            actual_original_source_sha or actuals["source_sha256"]
        )
    )
    pipeline_manifest_lineage_valid = _pipeline_manifest_binds_current_spec(
        pipeline_manifest_path,
        extraction_run_id=str(manifest.get("run_id") or ""),
        original_source_path=lineage_source,
        original_source_sha256=actual_original_source_sha or actuals["source_sha256"],
        clauses_path=clauses_path,
        format_spec_path=format_spec_path,
    )
    deterministic_lineage_valid = (
        isinstance(spec, dict)
        and spec.get("analysis_mode") in {"rule_only", "known_template"}
        and (
            manifest.get("format_spec_sha256") == current_spec_sha
            or pipeline_manifest_lineage_valid
        )
    )
    if not (base_lineage_valid and (
            semantic_lineage_valid or deterministic_lineage_valid or projection_valid)):
        return None, {"status": "blocked", "findings": [{
            "code": "format_spec_source_lineage_mismatch",
            "reason": (
                "the format spec must retain the extraction run and exact requirement source path/hash; "
                "deterministic review requires the manifest-bound source spec or a replayable projection "
                f"(base_lineage={base_lineage_valid}, semantic_lineage={semantic_lineage_valid}, "
                f"manifest_spec_digest={deterministic_lineage_valid}, "
                f"pipeline_manifest={pipeline_manifest_lineage_valid}, projection={projection_reason})"
            ),
        }]}
    return source_docx, {
        "status": "verified",
        "extraction_manifest_sha256": sha256_file(extraction_manifest_path),
        "source_docx_sha256": actuals["source_sha256"],
        "requirement_clauses_sha256": actuals["requirement_clauses_sha256"],
        "format_spec_sha256": sha256_file(format_spec_path),
        "format_spec_source_lineage": (
            "verified_deterministic_subset_parent_projection"
            if projection_valid else "verified_pipeline_manifest_current_spec"
            if pipeline_manifest_lineage_valid else "verified_run_source_path_and_sha256"
        ),
    }


def _paragraphs(path: Path) -> tuple[list[str], dict[int, str]]:
    doc = Document(path)
    body_children = list(doc._body._element)
    direct: dict[int, str] = {}
    all_text: list[str] = []
    for index, child in enumerate(body_children):
        if child.tag.endswith("}p"):
            value = Paragraph(child, doc._body).text
            direct[index] = value
            all_text.append(value)
        elif child.tag.endswith("}tbl"):
            for paragraph in child.xpath(".//w:p"):
                value = Paragraph(paragraph, doc._body).text
                all_text.append(value)
    return all_text, direct


def _norm(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _bound_clause(clause: dict[str, Any], source_texts: list[str],
                  direct_paragraphs: dict[int, str]) -> tuple[bool, str]:
    full = clause.get("source_text_full")
    span = clause.get("source_span")
    if not isinstance(full, str) or not full.strip() or not isinstance(span, dict):
        return False, "source_text_or_span_missing"
    if span.get("source_sha256") != hashlib.sha256(full.encode("utf-8")).hexdigest():
        return False, "source_literal_digest_mismatch"
    span_text = span.get("text")
    if not isinstance(span_text, str) or not span_text or _norm(span_text) not in _norm(full):
        return False, "source_span_text_not_in_full_literal"
    location = span.get("location")
    if (not isinstance(location, dict) or location.get("part") != "document"
            or not isinstance(location.get("child_index"), int)):
        return False, "source_document_locator_missing"
    paragraph = direct_paragraphs.get(location["child_index"])
    if paragraph is None:
        return False, "source_document_locator_not_a_paragraph"
    start, end = span.get("start_offset"), span.get("end_offset")
    if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end < start:
        return False, "source_span_offsets_invalid"
    if paragraph[start:end] != span_text:
        return False, "source_span_offsets_do_not_match_source_paragraph"
    if _norm(full) not in _norm(paragraph):
        return False, "source_full_literal_not_in_bound_paragraph"
    hits = sum(_norm(full) in _norm(candidate) for candidate in source_texts)
    if hits != 1:
        return False, f"source_literal_occurrence_count:{hits}"
    evidence_ids = clause.get("evidence_ids")
    if (not isinstance(evidence_ids, list) or not evidence_ids
            or any(not isinstance(x, str) or not x for x in evidence_ids)):
        return False, "source_evidence_ids_missing"
    if span.get("evidence_id") not in evidence_ids:
        return False, "source_span_evidence_id_not_in_clause"
    return True, "manifest_source_locator_literal_and_digest_verified"


def compile_source_table_contract(
    source_docx: Path,
    clauses: list[dict[str, Any]],
    spec: dict[str, Any],
    *,
    source_binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a code-owned table contract or a fail-closed diagnostic."""
    relevant = [clause for clause in clauses
                if isinstance(clause, dict) and isinstance(clause.get("text"), str)
                and re.search(r"三线表|外边框线|内边框线", clause["text"])]
    if not relevant:
        return {"status": "not_applicable", "findings": [], "contract": None}
    if source_binding is not None and source_binding.get("status") != "verified":
        return {"status": "blocked", "findings": source_binding.get("findings", []),
                "contract": None}
    try:
        source_texts, direct_paragraphs = _paragraphs(source_docx)
    except Exception as exc:
        return {"status": "blocked", "findings": [{
            "code": "source_table_contract_source_unreadable",
            "reason": f"{type(exc).__name__}: {exc}",
        }], "contract": None}

    source_records: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for clause in relevant:
        valid, reason = _bound_clause(clause, source_texts, direct_paragraphs)
        source_records.append({
            "clause_id": clause.get("id"),
            "text": clause.get("source_text_full"),
            "source_sha256": (clause.get("source_span") or {}).get("source_sha256"),
            "evidence_ids": copy.deepcopy(clause.get("evidence_ids", [])),
            "binding_status": "verified" if valid else "failed",
            "binding_reason": reason,
        })
        if not valid:
            findings.append({"code": "source_table_clause_unbound",
                             "clause_id": clause.get("id"), "reason": reason})
    if findings:
        return {"status": "blocked", "findings": findings,
                "source_clauses": source_records, "contract": None}

    three_line = [c for c in relevant if re.search(r"三线表", c["text"])]
    border_clauses = [c for c in relevant if re.search(r"外边框线", c["text"])
                      and re.search(r"内边框线", c["text"])]
    if len(three_line) != 1 or len(border_clauses) != 1:
        return {"status": "blocked", "source_clauses": source_records, "contract": None,
                "findings": [{"code": "source_table_topology_ambiguous",
                              "reason": "requires exactly one source-bound three-line rule and one source-bound outer/inner width rule"}]}

    text = border_clauses[0]["text"]
    outer_match = re.search(r"外边框线\s*粗\s*" + NUMBER + r"\s*(?:磅|pt)", text, re.I)
    inner_match = re.search(r"内边框线\s*粗\s*" + NUMBER + r"\s*(?:磅|pt)", text, re.I)
    if not outer_match or not inner_match:
        return {"status": "blocked", "source_clauses": source_records, "contract": None,
                "findings": [{"code": "source_table_width_unparsed",
                              "reason": "explicit outer and inner line widths could not be parsed"}]}
    outer, inner = float(outer_match.group(1)), float(inner_match.group(1))
    if not (0 < outer <= 12 and 0 <= inner <= 12):
        return {"status": "blocked", "source_clauses": source_records, "contract": None,
                "findings": [{"code": "source_table_width_out_of_range",
                              "outer_pt": outer, "inner_pt": inner}]}

    clause_ids = [str(c["id"]) for c in (three_line[0], border_clauses[0])]
    evidence_ids = list(dict.fromkeys(
        str(eid) for c in (three_line[0], border_clauses[0]) for eid in c.get("evidence_ids", [])
    ))
    border_widths = {"top": outer, "header": inner, "bottom": outer,
                     "left": 0, "right": 0, "inside_h": 0, "inside_v": 0}
    caption_position = (spec.get("roles", {}).get("table_caption", {}).get("position")
                       if isinstance(spec.get("roles"), dict) else None)
    if caption_position not in {"above", "below"}:
        return {"status": "blocked", "source_clauses": source_records, "contract": None,
                "findings": [{"code": "source_table_caption_position_unresolved",
                              "reason": "captioned table scope requires a source-resolved table caption position"}]}
    contract = {
        "style": "three_line", "top_border_pt": outer,
        "header_border_pt": inner, "bottom_border_pt": outer,
        "remove_vertical_borders": True, "scope": "captioned_tables",
        "caption_position": caption_position,
        "border_widths_pt": border_widths,
    }
    existing = spec.get("tables")
    if existing and existing != contract:
        return {"status": "blocked", "source_clauses": source_records, "contract": None,
                "findings": [{"code": "source_table_contract_conflicts_with_format_spec",
                              "existing": copy.deepcopy(existing), "derived": contract}]}
    spec["tables"] = copy.deepcopy(contract)

    requirement_id = "RSC-" + hashlib.sha256("\0".join(clause_ids).encode("utf-8")).hexdigest()[:16]
    requirements = spec.setdefault("requirements", [])
    matching = [r for r in requirements if set(r.get("clause_ids", [])) & set(clause_ids)]
    if matching:
        if any(r.get("role") != "table"
               or r.get("properties", {}).get("border_widths_pt") != border_widths
               for r in matching):
            return {"status": "blocked", "source_clauses": source_records,
                    "contract": contract, "findings": [{
                        "code": "source_table_requirement_conflict",
                        "reason": "existing requirements for the source border clauses differ from the compiled contract",
                    }]}
        requirement_id = str(matching[0]["id"])
    else:
        requirements.append({
            "id": requirement_id, "role": "table",
            "properties": {"border_widths_pt": copy.deepcopy(border_widths)},
            "evidence_ids": evidence_ids, "clause_ids": clause_ids,
            "resolved_by": "template", "confidence": 1.0,
            "source_text": "\n".join(str(c.get("source_text_full") or c["text"])
                                      for c in (three_line[0], border_clauses[0])),
            "reason": "The exact source-bound three-line and width clauses compile to explicit table-edge properties.",
        })

    compliance = {r.get("clause_id"): r for r in spec.get("clause_compliance", [])
                  if isinstance(r, dict)}
    for cid in clause_ids:
        record = compliance.get(cid)
        if record is None:
            findings.append({"code": "source_table_clause_compliance_record_missing",
                             "clause_id": cid})
            continue
        if record.get("status") == "unsupported_backend":
            record["status"] = "pending_execution"
            record["requirement_ids"] = list(dict.fromkeys(
                [*record.get("requirement_ids", []), requirement_id]))
            record["reason"] = "A source-bound deterministic table contract is compiled; serialized border verification remains pending."
        elif record.get("status") == "pending_execution":
            record["requirement_ids"] = list(dict.fromkeys(
                [*record.get("requirement_ids", []), requirement_id]))
        elif record.get("status") in {"generated_and_verified", "verified_existing"}:
            if requirement_id not in record.get("requirement_ids", []):
                findings.append({"code": "source_table_clause_status_conflicts_with_compiler",
                                 "clause_id": cid, "status": record.get("status")})
        else:
            findings.append({"code": "source_table_clause_not_executable",
                             "clause_id": cid, "status": record.get("status")})

    return {
        "status": "blocked" if findings else "compiled",
        "source_docx_sha256": sha256_file(source_docx),
        "source_binding": copy.deepcopy(source_binding),
        "source_clauses": source_records, "contract": contract,
        "requirement_id": requirement_id, "table_scope": "captioned_tables",
        "findings": findings,
    }
