from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

from docx import Document

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from source_table_contract import (  # noqa: E402
    compile_source_table_contract,
    resolve_bound_requirement_docx,
    sha256_file,
)


THREE_LINE = "表的编排须采用国际通用的三线表。"
BORDER_WIDTHS = "说明4：表格的外边框线粗1.5磅，内边框线粗0.5磅。"


def clause(clause_id: str, text: str, evidence_id: str, child_index: int,
           span_text: str) -> dict:
    start = text.index(span_text)
    return {
        "id": clause_id,
        "text": text.rstrip("。"),
        "source_text_full": text,
        "evidence_ids": [evidence_id],
        "source_span": {
            "evidence_id": evidence_id,
            "start_offset": start,
            "end_offset": start + len(span_text),
            "text": span_text,
            "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "location": {"part": "document", "child_index": child_index},
        },
    }


def make_bundle(root: Path) -> tuple[Path, Path, Path, Path, list[dict]]:
    source = root / "requirements.docx"
    doc = Document()
    doc.add_paragraph(THREE_LINE)
    doc.add_paragraph(BORDER_WIDTHS)
    doc.save(source)
    clauses = [
        clause("C-three", THREE_LINE, "E-three", 0, THREE_LINE[:-1]),
        clause("C-widths", BORDER_WIDTHS, "E-widths", 1, BORDER_WIDTHS[:-1]),
    ]
    clauses_path = root / "requirement-clauses.json"
    clauses_path.write_text(json.dumps(clauses, ensure_ascii=False), encoding="utf-8")
    spec_path = root / "format-spec.json"
    spec = {
        "source_document": str(source.resolve()),
        "run_id": "test-run",
        "roles": {"table_caption": {"position": "above"}},
        "semantic_review_provenance": {
            "run_id": "test-run", "source_sha256": sha256_file(source),
        },
        "clause_compliance": [
            {"clause_id": item["id"], "status": "unsupported_backend", "requirement_ids": []}
            for item in clauses
        ],
        "requirements": [],
    }
    spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    manifest_path = root / "extraction-manifest.json"
    manifest = {
        "status": "completed", "run_id": "test-run",
        "normalized_source_document": str(source.resolve()),
        "normalized_source_sha256": sha256_file(source),
        "requirement_clauses_sha256": sha256_file(clauses_path),
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return source, clauses_path, spec_path, manifest_path, clauses


class SourceTableContractTests(unittest.TestCase):
    def test_compiles_exact_bound_three_line_and_border_width_clauses(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source, clauses_path, spec_path, manifest_path, clauses = make_bundle(root)
            bound_source, binding = resolve_bound_requirement_docx(manifest_path, clauses_path, spec_path)
            self.assertEqual(bound_source, source)
            self.assertEqual(binding["status"], "verified")
            spec = json.loads(spec_path.read_text())
            result = compile_source_table_contract(source, clauses, spec, source_binding=binding)
            self.assertEqual(result["status"], "compiled", result["findings"])
            self.assertEqual(result["contract"]["scope"], "captioned_tables")
            self.assertEqual(result["contract"]["border_widths_pt"], {
                "top": 1.5, "header": 0.5, "bottom": 1.5,
                "left": 0, "right": 0, "inside_h": 0, "inside_v": 0,
            })
            self.assertEqual([item["status"] for item in spec["clause_compliance"]],
                             ["pending_execution", "pending_execution"])

    def test_manifest_source_or_clause_digest_substitution_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source, clauses_path, spec_path, manifest_path, _ = make_bundle(root)
            manifest = json.loads(manifest_path.read_text())
            manifest["normalized_source_sha256"] = "0" * 64
            manifest_path.write_text(json.dumps(manifest))
            result, binding = resolve_bound_requirement_docx(manifest_path, clauses_path, spec_path)
            self.assertIsNone(result)
            self.assertIn("requirements_extraction_binding_mismatch",
                          {item["code"] for item in binding["findings"]})

    def test_clause_offset_or_source_location_tamper_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source, _, _, _, clauses = make_bundle(root)
            bad_offset = copy.deepcopy(clauses)
            bad_offset[0]["source_span"]["start_offset"] = 1
            spec = {"requirements": [], "clause_compliance": []}
            spec["roles"] = {"table_caption": {"position": "above"}}
            result = compile_source_table_contract(source, bad_offset, spec)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["findings"][0]["reason"],
                             "source_span_offsets_do_not_match_source_paragraph")

            bad_location = copy.deepcopy(clauses)
            bad_location[1]["source_span"]["location"]["child_index"] = 9
            result = compile_source_table_contract(source, bad_location, spec)
            self.assertEqual(result["status"], "blocked")
            self.assertIn("source_document_locator_not_a_paragraph",
                          {item["reason"] for item in result["findings"]})

    def test_duplicate_topology_or_conflicting_spec_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source, _, _, _, clauses = make_bundle(root)
            duplicate = copy.deepcopy(clauses)
            duplicate.append(copy.deepcopy(clauses[0]) | {"id": "C-three-duplicate"})
            result = compile_source_table_contract(source, duplicate,
                                                   {"requirements": [], "clause_compliance": []})
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["findings"][0]["code"], "source_table_topology_ambiguous")

            conflict_spec = {"tables": {"style": "other"}, "requirements": [],
                             "clause_compliance": [],
                             "roles": {"table_caption": {"position": "above"}}}
            result = compile_source_table_contract(source, clauses, conflict_spec)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["findings"][0]["code"],
                             "source_table_contract_conflicts_with_format_spec")


if __name__ == "__main__":
    unittest.main()
