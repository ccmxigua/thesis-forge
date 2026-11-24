"""Synthetic public review_draft table-scope regressions; no school artifacts."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from lxml import etree

from test_reconstructed_property_authority import digest, record, review_args, write_json
from manual_review import add_manual_review_items, build_manual_review_ledger


TABLE_RULE = {"style": "three_line", "top_border_pt": .75,
              "header_border_pt": .75, "bottom_border_pt": .75,
              "remove_vertical_borders": True}


def table_fixture():
    doc = Document()
    doc.add_paragraph("Synthetic cover information")
    cover = doc.add_table(rows=2, cols=2)
    cover.cell(0, 0).text = "Name"
    cover.cell(0, 1).text = "Synthetic author"
    borders = OxmlElement("w:tblBorders")
    for side in ("top", "bottom", "left", "right", "insideH", "insideV"):
        edge = OxmlElement("w:" + side)
        edge.set(qn("w:val"), "none")
        borders.append(edge)
    cover._tbl.tblPr.append(borders)
    for row in cover.rows:
        cell_borders = OxmlElement("w:tcBorders")
        edge = OxmlElement("w:bottom")
        edge.set(qn("w:val"), "single")
        edge.set(qn("w:sz"), "8")
        cell_borders.append(edge)
        row.cells[1]._tc.get_or_add_tcPr().append(cell_borders)
    doc.add_paragraph("第一章 正文", style="Heading 1")
    doc.styles.add_style("Synthetic Caption", WD_STYLE_TYPE.PARAGRAPH)
    doc.add_paragraph("表1-1  合成数据", style="Synthetic Caption")
    doc.add_table(rows=2, cols=2).cell(0, 0).text = "Bound data"
    doc.add_paragraph("表1-2  合成数据", style="Synthetic Caption")
    doc.add_paragraph("单位：mm")
    doc.add_table(rows=2, cols=2).cell(0, 0).text = "Annotation intervenes"
    doc.add_paragraph("续表1-2  合成数据")
    doc.add_table(rows=2, cols=2).cell(0, 0).text = "Continuation unresolved"
    return doc


def border_state(table):
    nodes = table._tbl.xpath("./w:tblPr/w:tblBorders|.//w:tcPr/w:tcBorders")
    # Expanded-name topology avoids irrelevant namespace-prefix/format changes.
    return [(etree.QName(node).localname,
             [(etree.QName(edge).localname, tuple(sorted(edge.attrib.items()))) for edge in node])
            for node in nodes]


def add_bound_unresolved_table_clause(root, spec_path):
    """Bind a real synthetic source clause whose caption position is unknown."""
    literal = "表格用三线表，黑色单线，线粗3/4磅。"
    req = root / "synthetic-requirements.docx"
    source = root / "synthetic-source.docx"
    doc = Document()
    doc.add_paragraph(literal)
    doc.save(req)
    clauses_path = root / "clauses.json"
    write_json(clauses_path, [{
        "id": "C00001", "text": literal, "source_text_full": literal,
        "evidence_ids": ["E00001"],
        "source_span": {"source_sha256": hashlib.sha256(literal.encode()).hexdigest(),
                        "text": literal, "evidence_id": "E00001", "start_offset": 0,
                        "end_offset": len(literal), "location": {"part": "document", "child_index": 0}},
    }])
    spec = json.loads(spec_path.read_text())
    spec["analysis_mode"] = "rule_only"
    write_json(spec_path, spec)
    extraction_path = root / "extraction.json"
    extraction = json.loads(extraction_path.read_text())
    extraction.update(source_document=str(req), source_sha256=digest(req),
                      requirement_clauses_sha256=digest(clauses_path), format_spec_sha256=digest(spec_path))
    write_json(extraction_path, extraction)
    binding = {"case_id": "standalone", "run_id": spec["run_id"], "source_sha256": digest(source),
               "clause_sha256": digest(clauses_path), "evidence_sha256": digest(root / "context.json"),
               "request_sha256": None, "requirements_sha256": digest(req),
               "input_source_sha256": digest(source), "format_spec_sha256": digest(spec_path),
               "official_template_sha256": None, "official_template_source": "not_supplied"}
    manual = write_json(root / "manual.json", build_manual_review_ledger(None, [], binding=binding))
    write_json(root / "pipeline.json", {"case_id": "standalone", "inputs": {
        "requirements": record(req), "source": record(source), "official_template_source": "not_supplied"},
        "manual_review_ledger_input_sha256": digest(manual)})


class TableScopeTests(unittest.TestCase):
    def execute(self, root, table_rule, *, blocked_source=False, caption_marker=False):
        doc = table_fixture()
        if caption_marker:
            # Use one genuinely adjacent caption/table pair. The marker helper
            # anchors the last mapped caption, so remove the unrelated cases.
            children = list(doc._body._element)
            target = doc.tables[1]._tbl
            for node in children[children.index(target) + 1:]:
                if node.tag != qn("w:sectPr"):
                    node.getparent().remove(node)
        before = [border_state(table) for table in doc.tables]
        roles = {"table_caption": {"paragraph": {"alignment": "center"}}}
        requirements = [{"id": "R-TABLE", "role": "table", "properties": copy.deepcopy(table_rule),
                         "evidence_ids": ["E00001"], "resolved_by": "rule", "confidence": 1,
                         "source_text": "Synthetic table formatting requirement."}]
        args, source, spec, output, audits = review_args(
            root, doc, roles, {"table_caption": "Synthetic Caption"},
            spec_overrides={"tables": table_rule, "requirements": requirements})
        if blocked_source:
            add_bound_unresolved_table_clause(root, spec)
        if caption_marker:
            manual_path = root / "manual.json"
            ledger = add_manual_review_items(json.loads(manual_path.read_text()), [{
                "category": "runtime_manual_unverifiable", "source_type": "format_constraint",
                "source_text": "table_caption.title", "source_code": "caption-title-review",
            }])
            write_json(manual_path, ledger)
            pipeline_path = root / "pipeline.json"
            pipeline = json.loads(pipeline_path.read_text())
            pipeline["manual_review_ledger_input_sha256"] = digest(manual_path)
            write_json(pipeline_path, pipeline)
        original = source.read_bytes()
        run = subprocess.run(args, capture_output=True, text=True, timeout=90,
                             env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                                  "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"})
        self.assertEqual(run.returncode, 0, run.stderr + run.stdout[-6000:])
        self.assertEqual(source.read_bytes(), original)
        result = Document(output)
        report = json.loads((audits / "validation-report.json").read_text())
        receipt_data = json.loads((audits / "property-receipts.json").read_text())
        receipts = receipt_data if isinstance(receipt_data, list) else receipt_data["receipts"]
        return before, result, report, [r for r in receipts if r["requirement_id"] == "R-TABLE"], audits

    def test_missing_scope_preserves_cover_and_does_not_guess_remaining_tables(self):
        with tempfile.TemporaryDirectory() as temp:
            before, doc, report, receipts, _ = self.execute(Path(temp), dict(TABLE_RULE))
            self.assertEqual([border_state(t) for t in doc.tables], before)
            self.assertEqual(report["table_changes"]["selected_table_count"], 0)
            self.assertTrue(any(f.get("code") == "table_scope_unresolved"
                                for f in report["scope_execution_deferrals"]))
            self.assertTrue(receipts)
            self.assertEqual({r["status"] for r in receipts}, {"unverified"})
            self.assertFalse([f for f in report["findings"]
                              if f.get("role") == "table" and str(f.get("property", "")).startswith("tables[")])

    def test_blocked_source_caption_scope_cannot_fall_back_to_all_tables(self):
        with tempfile.TemporaryDirectory() as temp:
            before, doc, report, receipts, audits = self.execute(Path(temp), dict(TABLE_RULE), blocked_source=True)
            contract = json.loads((audits / "source-table-contract.json").read_text())
            self.assertEqual(contract["status"], "blocked")
            self.assertEqual(contract["findings"][0]["code"], "source_table_caption_position_unresolved")
            self.assertEqual(contract["source_clauses"][0]["binding_status"], "verified")
            self.assertEqual([border_state(t) for t in doc.tables], before)
            self.assertEqual(report["table_changes"]["selected_table_count"], 0)
            self.assertEqual({r["status"] for r in receipts}, {"unverified"})

    def test_captioned_scope_execution_audit_and_receipts_share_one_population(self):
        with tempfile.TemporaryDirectory() as temp:
            rule = {**TABLE_RULE, "scope": "captioned_tables", "caption_position": "above"}
            before, doc, report, receipts, audits = self.execute(Path(temp), rule)
            after = [border_state(t) for t in doc.tables]
            self.assertEqual(after[0], before[0])
            self.assertNotEqual(after[1], before[1])
            self.assertEqual(after[2:], before[2:])
            self.assertEqual(report["table_changes"]["selected_table_count"], 1)
            observations = json.loads((audits / "draft-scorecard.json").read_text())["serialized_format_audit"]["table_border_observations"]
            self.assertEqual(observations["table_count"], 1)
            style = next(r for r in receipts if r["property_path"] == "style")
            self.assertEqual((style["status"], style["actual"]), ("verified", "three_line"))
            self.assertFalse([f for f in report["findings"]
                              if f.get("role") == "table" and str(f.get("property", "")).startswith("tables[")])

    def test_explicit_all_tables_scope_remains_executable(self):
        with tempfile.TemporaryDirectory() as temp:
            before, doc, report, receipts, _ = self.execute(Path(temp), {**TABLE_RULE, "scope": "all_tables"})
            self.assertTrue(all(border_state(t) != prior for t, prior in zip(doc.tables, before)))
            self.assertEqual(report["table_changes"]["selected_table_count"], 4)
            style = next(r for r in receipts if r["property_path"] == "style")
            self.assertEqual(style["status"], "verified")

    def test_captioned_scope_without_position_remains_unverified(self):
        with tempfile.TemporaryDirectory() as temp:
            before, doc, report, receipts, _ = self.execute(Path(temp), {**TABLE_RULE, "scope": "captioned_tables"})
            self.assertEqual([border_state(t) for t in doc.tables], before)
            self.assertEqual(report["table_changes"]["selected_table_count"], 0)
            self.assertEqual({r["status"] for r in receipts}, {"unverified"})

    def test_final_marker_scope_is_reobserved_without_reusing_pre_marker_actuals(self):
        with tempfile.TemporaryDirectory() as temp:
            rule = {**TABLE_RULE, "scope": "captioned_tables", "caption_position": "above"}
            before, doc, report, receipts, audits = self.execute(Path(temp), rule, caption_marker=True)
            self.assertEqual(border_state(doc.tables[0]), before[0])
            self.assertEqual(report["table_changes"]["selected_table_count"], 1)
            marker_audit = json.loads((audits / "manual-review-marker-audit.json").read_text())
            self.assertTrue(marker_audit["valid"])
            self.assertIn("待人工", "".join(doc.tables[1]._tbl.getprevious().itertext()))
            card = json.loads((audits / "draft-scorecard.json").read_text())
            observations = card["serialized_format_audit"]["table_border_observations"]
            self.assertEqual((observations["status"], observations["table_count"]), ("blocked", 0))
            for path in ("style", "remove_vertical_borders"):
                receipt = next(r for r in receipts if r["property_path"] == path)
                self.assertEqual(receipt["status"], "unverified", path)
                self.assertIsNone(receipt["actual"], path)
            self.assertEqual({r["status"] for r in receipts}, {"unverified"})

    def test_final_marker_cannot_certify_only_surviving_captioned_tables(self):
        def two_captioned_tables():
            doc = Document()
            doc.add_paragraph("第一章 正文", style="Heading 1")
            doc.styles.add_style("Synthetic Caption", WD_STYLE_TYPE.PARAGRAPH)
            for number in (1, 2):
                doc.add_paragraph(f"表1-{number}  合成数据", style="Synthetic Caption")
                doc.add_table(rows=2, cols=2).cell(0, 0).text = f"Synthetic table {number}"
            return doc

        with tempfile.TemporaryDirectory() as temp, patch(__name__ + ".table_fixture", two_captioned_tables):
            rule = {**TABLE_RULE, "scope": "captioned_tables", "caption_position": "above"}
            _, doc, report, receipts, audits = self.execute(Path(temp), rule, caption_marker=True)
            self.assertEqual(report["table_changes"]["selected_table_count"], 2)
            self.assertIn("待人工", "".join(doc.tables[1]._tbl.getprevious().itertext()))
            self.assertNotIn("待人工", "".join(doc.tables[0]._tbl.getprevious().itertext()))
            card = json.loads((audits / "draft-scorecard.json").read_text())
            observations = card["serialized_format_audit"]["table_border_observations"]
            self.assertEqual(observations["status"], "blocked")
            self.assertEqual({r["status"] for r in receipts}, {"unverified"})


if __name__ == "__main__":
    unittest.main()
