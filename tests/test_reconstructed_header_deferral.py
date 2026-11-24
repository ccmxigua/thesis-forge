"""Unresolved source evidence cannot authorize partial header execution."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from docx import Document
from docx.enum.section import WD_SECTION_START
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.shared import Inches

import test_reconstructed_property_authority as property_fixture
from test_reconstructed_property_authority import font, paragraph_format, snapshot


def source_document():
    doc = Document()
    doc.add_paragraph("正文 synthetic unrelated body")
    style = doc.styles.add_style("Original Header", WD_STYLE_TYPE.PARAGRAPH)
    font(style, size=18, latin="Courier New")
    paragraph_format(style)
    style.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    story = doc.sections[0].header
    paragraph = story.paragraphs[0]
    paragraph.text = "SOURCE HEADER 中文"
    paragraph.style = style
    link = OxmlElement("w:hyperlink")
    rid = story.part.relate_to("https://example.invalid/source-header", RT.HYPERLINK, is_external=True)
    link.set(qn("r:id"), rid)
    run, text = OxmlElement("w:r"), OxmlElement("w:t")
    text.text = " Source link"
    run.append(text)
    link.append(run)
    paragraph._p.append(link)
    nested = story.add_table(rows=1, cols=1, width=Inches(2)).cell(0, 0).paragraphs[0]
    nested.text = "Source table 中文"
    nested.style = style
    doc.add_section(WD_SECTION_START.NEW_PAGE)
    doc.add_paragraph("正文 synthetic second body")
    return doc


def unresolved_record(status="unsupported_backend", evidence="E-0", scope="docx"):
    return {"clause_id": "C-unresolved-header", "evidence_ids": [evidence], "scope": scope,
            "status": status, "requirement_ids": [],
            "reason": "Synthetic same-evidence header placement remains unresolved."}


def requirement(identifier, properties, evidence):
    return {"id": identifier, "role": "header", "properties": properties,
            "evidence_ids": [evidence], "resolved_by": "rule", "confidence": 1,
            "source_text": "Synthetic header rule."}


class HeaderDeferralTests(unittest.TestCase):
    def execute(self, root, doc, roles, overrides):
        mapping = {"header": "Header"}
        if "body_text" in roles:
            mapping["body_text"] = "Normal"
        return property_fixture.PropertyAuthorityTests(methodName="runTest").execute(
            root, doc, roles, mapping, spec_overrides=overrides)

    def test_bundle_contains_exact_header_deferral_regression(self):
        from skill_bundle import build_bundle, verify_bundle
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            archive = directory / "synthetic-runtime.zip"
            target = directory / "relocated"
            build_bundle(property_fixture.ROOT, archive)
            member = "tests/test_reconstructed_header_deferral.py"
            with zipfile.ZipFile(archive) as bundle:
                self.assertIn(member, bundle.namelist())
                self.assertEqual(bundle.read(member), Path(__file__).read_bytes())
                bundle.extractall(target)
            self.assertEqual(verify_bundle(target)["status"], "pass")

    def test_same_evidence_defer_entire_header_rule_and_preserve_source(self):
        for status in ("unsupported_backend", "unresolved"):
            for content in ({"text": "REPLACED HEADER"}, {"header_content": {"left_text": "REPLACED HEADER"}}):
                with self.subTest(status=status, content=content), tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    doc = source_document()
                    original_header = doc.sections[0].header._element.xml
                    original_style = doc.styles["Original Header"].element.xml
                    target_style = doc.styles["Header"].element.xml
                    roles = {"header": {**content, "font": {"size_pt": 12}, "paragraph": {"alignment": "center"}},
                             "body_text": {"font": {"size_pt": 13}}}
                    out, report, audits = self.execute(root, doc, roles,
                        {"clause_compliance": [unresolved_record(status)]})
                    self.assertEqual(out.sections[0].header._element.xml, original_header)
                    self.assertEqual(out.styles["Original Header"].element.xml, original_style)
                    self.assertEqual(out.styles["Header"].element.xml, target_style)
                    self.assertTrue(out.sections[1].header.is_linked_to_previous)
                    for p in out.paragraphs:
                        if p.text.startswith("正文 synthetic"):
                            self.assertEqual(snapshot(out, p)["font"]["size_pt"], 13)
                    self.assertTrue(any(item.get("code") == "header_content_source_unresolved_preserved"
                                        for item in report["scope_execution_deferrals"]))
                    receipts = json.loads((audits / "property-receipts.json").read_text())["receipts"]
                    deferred = [item for item in receipts if item["requirement_id"] == "R-0"]
                    self.assertTrue(deferred, receipts)
                    self.assertTrue(all(item["status"] == "unverified" for item in deferred), deferred)
                    entries = report["draft_scorecard"]["entries"]
                    self.assertFalse(any(item["status"] == "verified" and item.get("detail", {}).get("requirement_id") == "R-0"
                                         for item in entries))
                    records = json.loads((audits / "clause-compliance-report.json").read_text())["records"]
                    self.assertEqual(records[0]["status"], status)
                    self.assertEqual(records[0]["requirement_ids"], [])

    def test_independent_header_property_executes_without_partial_atomic_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            doc = source_document()
            original_style = doc.styles["Original Header"].element.xml
            original_text = doc.sections[0].header._element.xpath(".//w:t/text()")
            blocked = {"text": "REPLACED HEADER", "font": {"size_pt": 12}}
            independent = {"font": {"cjk": "黑体"}}
            roles = {"header": {"text": "REPLACED HEADER", "font": {"size_pt": 12, "cjk": "黑体"}}}
            overrides = {"requirements": [requirement("R-blocked", blocked, "E-0"),
                                            requirement("R-independent", independent, "E-independent")],
                         "clause_compliance": [unresolved_record()]}
            out, report, audits = self.execute(root, doc, roles, overrides)
            self.assertEqual(out.sections[0].header._element.xpath(".//w:t/text()"), original_text)
            self.assertEqual(out.styles["Original Header"].element.xml, original_style)
            self.assertTrue(out.sections[1].header.is_linked_to_previous)
            p = out.sections[0].header.paragraphs[0]
            self.assertEqual(snapshot(out, p)["font"]["size_pt"], 18)
            self.assertEqual(snapshot(out, p)["font"]["cjk"], "黑体")
            receipts = json.loads((audits / "property-receipts.json").read_text())["receipts"]
            self.assertTrue(all(item["status"] == "unverified" for item in receipts if item["requirement_id"] == "R-blocked"))
            independent_receipts = [item for item in receipts if item["requirement_id"] == "R-independent"]
            self.assertTrue(independent_receipts)
            self.assertTrue(all(item["status"] == "verified" for item in independent_receipts), independent_receipts)

    def test_unrelated_evidence_or_external_duty_does_not_block_explicit_header(self):
        for record in (unresolved_record(evidence="E-other"), unresolved_record(scope="external_submission")):
            with self.subTest(record=record), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                out, report, audits = self.execute(root, source_document(),
                    {"header": {"text": "REPLACED HEADER"}}, {"clause_compliance": [record]})
                self.assertEqual(out.sections[0].header.paragraphs[0].text, "REPLACED HEADER")
                self.assertFalse(any(item.get("code") == "header_content_source_unresolved_preserved"
                                     for item in report["scope_execution_deferrals"]))

    def test_separate_requirements_sharing_unresolved_source_are_not_partial_authority(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            doc = source_document()
            source_header = doc.sections[0].header._element.xml
            roles = {"header": {"text": "REPLACED HEADER", "font": {"size_pt": 12}}}
            overrides = {"requirements": [requirement("R-text", {"text": "REPLACED HEADER"}, "E-0"),
                                            requirement("R-font", {"font": {"size_pt": 12}}, "E-0")],
                         "clause_compliance": [unresolved_record()]}
            out, report, audits = self.execute(root, doc, roles, overrides)
            self.assertEqual(out.sections[0].header._element.xml, source_header)
            receipts = json.loads((audits / "property-receipts.json").read_text())["receipts"]
            self.assertEqual({item["requirement_id"] for item in receipts}, {"R-text", "R-font"})
            self.assertTrue(all(item["status"] == "unverified" for item in receipts), receipts)

    def test_equal_independent_font_does_not_certify_withheld_atomic_requirement(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            doc = source_document()
            blocked = {"text": "REPLACED HEADER", "font": {"size_pt": 12}}
            overrides = {"requirements": [requirement("R-blocked", blocked, "E-0"),
                                            requirement("R-independent", {"font": {"size_pt": 12}}, "E-independent")],
                         "clause_compliance": [unresolved_record()]}
            out, report, audits = self.execute(root, doc, {"header": blocked}, overrides)
            self.assertTrue(out.sections[0].header.paragraphs[0].text.startswith("SOURCE HEADER"))
            self.assertEqual(snapshot(out, out.sections[0].header.paragraphs[0])["font"]["size_pt"], 12)
            receipts = json.loads((audits / "property-receipts.json").read_text())["receipts"]
            self.assertTrue(all(item["status"] == "unverified" for item in receipts if item["requirement_id"] == "R-blocked"))
            independent = [item for item in receipts if item["requirement_id"] == "R-independent"]
            self.assertTrue(independent)
            self.assertTrue(all(item["status"] == "verified" for item in independent), independent)

    def test_deferred_scoped_header_does_not_create_source_boundaries(self):
        from header_scope import compile_header_scope_rules
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            doc = Document()
            doc.add_paragraph("Synthetic cover")
            doc.add_paragraph("目录", style="Heading 1")
            doc.add_paragraph("Synthetic contents")
            doc.add_paragraph("第一章 绪论", style="Heading 1")
            doc.add_paragraph("Synthetic body")
            doc.sections[0].header.paragraphs[0].text = "SOURCE HEADER"
            source_header = doc.sections[0].header._element.xml
            properties = {"header_content": {"left_text": "REPLACED HEADER"}}
            scoped = requirement("R-scoped", properties, "E-0")
            scoped.update(clause_ids=["C-header"], applicability={"status": "conditional", "conditions": [
                {"fact": "source_inventory.table_of_contents", "operator": "present", "value": None}]})
            overrides = {"requirements": [scoped], "clause_compliance": [unresolved_record(),
                {"clause_id": "C-header", "evidence_ids": ["E-0"], "scope": "docx",
                 "status": "pending_execution", "requirement_ids": ["R-scoped"],
                 "reason": "Synthetic accepted fragment shares unresolved source evidence."}]}
            overrides["header_scope_rules"], errors = compile_header_scope_rules(overrides)
            self.assertEqual(errors, [])
            out, report, audits = self.execute(root, doc, {"header": properties}, overrides)
            self.assertEqual(len(out.sections), 1)
            self.assertEqual(out.sections[0].header._element.xml, source_header)
            plan = json.loads((audits / "header-scope-plan.json").read_text())
            self.assertFalse(plan["scoped"])
            self.assertEqual(plan["scope_transforms"], [])


if __name__ == "__main__":
    unittest.main()
