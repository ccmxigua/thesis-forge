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


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def resolve_bound_requirement_docx(
    extraction_manifest_path: Path,
    clauses_path: Path,
    format_spec_path: Path,
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

    source_value = manifest.get("normalized_source_document") or manifest.get("source_document")
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
    try:
        spec = strict_json_read(format_spec_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return None, {"status": "blocked", "findings": [{
            "code": "format_spec_unreadable",
            "reason": f"{type(exc).__name__}: {exc}",
        }]}
    provenance = spec.get("semantic_review_provenance") if isinstance(spec, dict) else None
    spec_source = spec.get("source_document") if isinstance(spec, dict) else None
    if (not isinstance(spec, dict)
            or spec.get("run_id") != manifest.get("run_id")
            or not isinstance(provenance, dict)
            or provenance.get("run_id") != manifest.get("run_id")
            or provenance.get("source_sha256") != actuals["source_sha256"]
            or not isinstance(spec_source, str)
            or Path(spec_source).resolve() != source_docx.resolve()):
        return None, {"status": "blocked", "findings": [{
            "code": "format_spec_source_lineage_mismatch",
            "reason": "the derived format spec must retain the extraction run, exact requirement source path, and source digest",
        }]}
    return source_docx, {
        "status": "verified",
        "extraction_manifest_sha256": sha256_file(extraction_manifest_path),
        "source_docx_sha256": actuals["source_sha256"],
        "requirement_clauses_sha256": actuals["requirement_clauses_sha256"],
        "format_spec_sha256": sha256_file(format_spec_path),
        "format_spec_source_lineage": "verified_run_source_path_and_sha256",
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
