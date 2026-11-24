"""Synthetic public-CLI regressions; never use a user's thesis or a renderer."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from docx import Document
from docx.enum.section import WD_SECTION_START
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.shared import Inches, Pt
from docx.text.run import Run
from lxml import etree

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = Path(os.environ.get("THESIS_TEST_SCRIPT_ROOT", ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from manual_review import build_manual_review_ledger
from visible_instance_format import collect_visible_instance_format


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    return path


def record(path):
    return {"path": str(path), "sha256": digest(path), "bytes": path.stat().st_size}


def font(target, size=12, cjk="楷体", latin="Times New Roman", italic=True):
    target.font.name = latin
    target.font.size = Pt(size)
    target.font.italic = italic
    target.font.bold = False
    target._element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), cjk)


def paragraph_format(target):
    p = target.paragraph_format
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.space_before = Pt(7)
    p.space_after = Pt(9)
    p.line_spacing = Pt(22)
    p.line_spacing_rule = WD_LINE_SPACING.EXACTLY


def snapshot(doc, paragraph):
    return collect_visible_instance_format(doc, {"target": [paragraph]})["actual_by_role"]["target"]


def review_args(root, doc, roles, mappings, template=None, unsupported=False, spec_overrides=None):
    """Build actual, hash-bound inputs, not mocks of the validation boundary."""
    source = root / "synthetic-source.docx"
    doc.save(source)
    req = root / "synthetic-requirements.docx"
    requirements_doc = Document()
    requirements_doc.add_paragraph("Synthetic regression requirement fixture only.")
    requirements_doc.save(req)
    spec = {"schema_version": "1.0", "source_document": str(req), "run_id": "synthetic-properties",
            "status": "rule_resolved", "compliance_mode": "supported_subset", "roles": roles,
            "requirements": [
                {"id": "R-%d" % i, "role": role, "properties": properties,
                 "evidence_ids": ["E-%d" % i], "resolved_by": "rule", "confidence": 1,
                 "source_text": "Synthetic explicitly requested properties."}
                for i, (role, properties) in enumerate(roles.items()) if properties
            ]}
    spec.update(spec_overrides or {})
    if unsupported:
        spec["clause_compliance"] = [
            {"clause_id": "C00159", "evidence_ids": ["E-atomic"], "scope": "docx",
             "status": "unsupported_backend", "requirement_ids": [],
             "reason": "Synthetic indivisible 12pt font and outline obligation is unsupported."}]
    spec_path = write_json(root / "format-spec.json", spec)
    mapping_path = write_json(root / "mappings.json", mappings)
    clauses = write_json(root / "clauses.json", [])
    evidence = write_json(root / "evidence.json", {})
    context = write_json(root / "context.json", {})
    extraction = write_json(root / "extraction.json", {
        "status": "completed", "cache_reused": False, "run_id": spec["run_id"],
        "source_sha256": digest(req), "requirement_clauses_sha256": digest(clauses),
        "document_evidence_sha256": digest(evidence), "evidence_sha256": digest(context)})
    binding = {"case_id": "standalone", "run_id": spec["run_id"], "source_sha256": digest(source),
               "clause_sha256": digest(clauses), "evidence_sha256": digest(context),
               "request_sha256": None, "requirements_sha256": digest(req),
               "input_source_sha256": digest(source), "format_spec_sha256": digest(spec_path),
               "official_template_sha256": digest(template) if template else None,
               "official_template_source": "style_template" if template else "not_supplied"}
    ledger = write_json(root / "manual.json", build_manual_review_ledger(None, [], binding=binding))
    inputs = {"requirements": record(req), "source": record(source),
              "official_template_source": binding["official_template_source"]}
    if template:
        inputs["official_template_evidence"] = record(template)
    pipeline = write_json(root / "pipeline.json", {
        "case_id": "standalone", "inputs": inputs, "manual_review_ledger_input_sha256": digest(ledger)})
    output = root / "synthetic-output.docx"
    audits = root / "audits"
    args = [sys.executable, "-B", str(SCRIPT_ROOT / "scripts/apply_format_spec.py"), str(source),
            str(spec_path), str(output), "--out-dir", str(audits), "--style-map", str(mapping_path),
            "--output-policy", "review_draft", "--require-coverage", "--manual-review-items", str(ledger),
            "--pipeline-manifest", str(pipeline), "--source-clauses", str(clauses),
            "--source-evidence", str(evidence), "--source-evidence-context", str(context),
            "--source-extraction-manifest", str(extraction)]
    if template:
        args += ["--official-template", str(template)]
    return args, source, spec_path, output, audits


class PropertyAuthorityTests(unittest.TestCase):
    def execute(self, root, doc, roles, mappings, template=None, unsupported=False, spec_overrides=None, tamper_scorecard=False):
        args, source, spec, output, audits = review_args(root, doc, roles, mappings, template, unsupported, spec_overrides)
        before = (source.read_bytes(), spec.read_bytes())
        if tamper_scorecard:
            script = "import sys;sys.path.insert(0,sys.argv.pop(1));import apply_format_spec as a;original=a.append_scorecard;\ndef tamper(doc, card):\n original(doc,card)\n p=next(p for p in doc.paragraphs if p.text=='第一章 合成')\n p.add_run('unexpected heading mutation')\na.append_scorecard=tamper\na.main(sys.argv[1:])"
            args = [sys.executable, '-B', '-c', script, str(SCRIPT_ROOT/'scripts')] + args[3:]
        result = subprocess.run(args, capture_output=True, text=True, timeout=90,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                                     "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"})
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout[-6000:])
        self.assertEqual((source.read_bytes(), spec.read_bytes()), before)
        report = json.loads((audits / "validation-report.json").read_text())
        self.assertFalse(report["submission_ready"])
        return Document(output), report, audits

    def test_only_explicit_body_bibliography_table_caption_leaves_change(self):
        for inherited in (False, True):
            for bibliography_spacing in (False, True):
                with self.subTest(inherited=inherited, bibliography_spacing=bibliography_spacing), tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    doc = Document()
                    font(doc.styles["Normal"])
                    paragraph_format(doc.styles["Normal"])
                    doc.add_paragraph("第一章 绪论", style="Heading 1")
                    body = doc.add_paragraph("正文 synthetic body text")
                    table = doc.add_table(rows=1, cols=1).cell(0, 0).paragraphs[0]
                    table.add_run("表内 synthetic cell")
                    caption_style = doc.styles.add_style("Original Caption", WD_STYLE_TYPE.PARAGRAPH)
                    font(caption_style, size=18, latin="Courier New")
                    paragraph_format(caption_style)
                    caption = doc.add_paragraph("图 1  synthetic caption", style=caption_style)
                    doc.add_paragraph("参考文献", style="Heading 1")
                    bib_style = doc.styles.add_style("参考文献正文", WD_STYLE_TYPE.PARAGRAPH)
                    font(bib_style)
                    paragraph_format(bib_style)
                    bib = doc.add_paragraph("[1] 合成 synthetic reference.", style=bib_style)
                    for p in (body, table, caption, bib):
                        if not inherited:
                            font(p.runs[0], size=18 if p is caption else 12,
                                 latin="Courier New" if p is caption else "Times New Roman")
                            paragraph_format(p)
                    template_doc = Document()
                    font(template_doc.styles["Normal"], size=10.5, cjk="宋体", latin="宋体", italic=False)
                    template_doc.styles["Normal"].paragraph_format.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
                    template_doc.styles.add_style("参考文献正文", WD_STYLE_TYPE.PARAGRAPH).base_style = template_doc.styles["Normal"]
                    font(template_doc.styles["Caption"], size=10.5, cjk="宋体", latin="宋体", italic=False)
                    template = root / "synthetic-template.docx"
                    template_doc.save(template)
                    line = {"line_spacing": {"type": "exact", "value": 20, "unit": "pt"}}
                    roles = {"body_text": {"paragraph": line},
                             "bibliography_entry": {"paragraph": line} if bibliography_spacing else {},
                             "table_text": {"font": {"size_pt": 10.5}, "paragraph": line},
                             "figure_caption": {"font": {"cjk": "黑体"}}}
                    mappings = {"body_text": "Normal", "table_text": "Normal",
                                "bibliography_entry": "参考文献正文", "figure_caption": "Caption"}
                    original = {role: snapshot(doc, p) for role, p in zip(roles, (body, bib, table, caption))}
                    bib_xml = bib._p.xml
                    out, report, audits = self.execute(root, doc, roles, mappings, template)
                    targets = {"body_text": next(p for p in out.paragraphs if p.text.startswith("正文")),
                               "bibliography_entry": next(p for p in out.paragraphs if p.text.startswith("[1]")),
                               "table_text": out.tables[0].cell(0, 0).paragraphs[0],
                               "figure_caption": next(p for p in out.paragraphs if p.text.startswith("图 1"))}
                    for role, p in targets.items():
                        with self.subTest(role=role):
                            expected = copy.deepcopy(original[role])
                            for group, properties in roles[role].items():
                                expected[group].update(properties)
                            self.assertEqual(snapshot(out, p), expected)
                            if roles[role]:
                                coverage = next(item for item in report["role_coverage"] if item["role"] == role)
                                self.assertEqual(coverage["status"], "present")
                    if not bibliography_spacing:
                        self.assertEqual(targets["bibliography_entry"]._p.xml, bib_xml)
                    self.assertEqual(targets["figure_caption"].style.name, "Original Caption")

    def test_unsupported_atomic_rule_does_not_supply_font(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            doc = Document()
            font(doc.styles["Normal"], size=13)
            p = doc.add_paragraph("正文 synthetic source remains thirteen points")
            template_doc = Document()
            font(template_doc.styles["Normal"], size=12)
            template = root / "synthetic-template.docx"
            template_doc.save(template)
            out, report, audits = self.execute(root, doc,
                {"body_text": {"paragraph": {"line_spacing": {"type": "exact", "value": 20, "unit": "pt"}}}},
                {"body_text": "Normal"}, template, unsupported=True)
            target = next(p for p in out.paragraphs if p.text.startswith("正文 synthetic"))
            self.assertEqual(snapshot(out, target)["font"]["size_pt"], 13)
            compliance = json.loads((audits / "clause-compliance-report.json").read_text())
            atomic = next(item for item in compliance["records"] if item["clause_id"] == "C00159")
            self.assertEqual(atomic["status"], "unsupported_backend")
            self.assertEqual(atomic["requirement_ids"], [])
            self.assertEqual(json.loads((root / "format-spec.json").read_text())["clause_compliance"][0]["requirement_ids"], [])

    def test_property_only_header_footer_preserve_styles_links_and_all_paragraphs(self):
        for role in ("header", "footer"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                doc = Document()
                doc.add_paragraph("synthetic ordinary source")
                style = doc.styles.add_style("Original " + role, WD_STYLE_TYPE.PARAGRAPH)
                font(style, size=18, latin="Courier New")
                paragraph_format(style)
                style.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.RIGHT
                story = getattr(doc.sections[0], role)
                p = story.paragraphs[0]
                p.text = "中文 synthetic first"
                p.style = style
                second = story.add_paragraph("中文 synthetic second", style=style)
                nested = story.add_table(rows=1, cols=1, width=Inches(2)).cell(0, 0).paragraphs[0]
                nested.text = "中文 synthetic nested"
                nested.style = style
                before = [snapshot(doc, p) for p in (p, second, nested)]
                doc.add_section(WD_SECTION_START.NEW_PAGE)
                original_parts = {str(p.partname) for p in doc.part.package.parts if str(p.partname).startswith('/word/' + role)}
                out, report, audits = self.execute(root, doc, {role: {"font": {"cjk": "黑体"}}}, {role: role.title()})
                story = getattr(out.sections[0], role)
                targets = [*story.paragraphs, story.tables[0].cell(0, 0).paragraphs[0]]
                self.assertEqual(len(targets), 3)
                for p, expected in zip(targets, before):
                    expected["font"]["cjk"] = "黑体"
                    self.assertEqual(snapshot(out, p), expected)
                    self.assertEqual(p.style.name, "Original " + role)
                self.assertTrue(getattr(out.sections[1], role).is_linked_to_previous)
                self.assertEqual({str(p.partname) for p in out.part.package.parts if str(p.partname).startswith('/word/' + role)}, original_parts)
                coverage = next(item for item in report["role_coverage"] if item["role"] == role)
                self.assertEqual(coverage["status"], "present")
                self.assertEqual(coverage["count"], 3)

    def test_explicit_header_content_still_replaces_text(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            doc = Document()
            doc.add_paragraph("synthetic ordinary source")
            doc.sections[0].header.paragraphs[0].text = "old header"
            out, report, audits = self.execute(root, doc,
                {"header": {"text": "explicit new header", "font": {"size_pt": 12}}},
                {"header": "Header"})
            self.assertEqual(out.sections[0].header.paragraphs[0].text, "explicit new header")
            self.assertEqual(out.sections[0].header.paragraphs[0].style.name, "Header")

    def test_property_only_story_preserves_inactive_variants(self):
        for role in ("header", "footer"):
            for active in (False, True):
                with self.subTest(role=role, active=active), tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    doc = Document()
                    doc.add_paragraph("synthetic ordinary source")
                    style = doc.styles.add_style("Original Story", WD_STYLE_TYPE.PARAGRAPH)
                    font(style, size=18)
                    paragraph_format(style)
                    for prefix in ("", "first_page_", "even_page_"):
                        p = getattr(doc.sections[0], prefix + role).paragraphs[0]
                        p.text = prefix + "中文 synthetic story"
                        p.style = style
                    doc.sections[0].different_first_page_header_footer = active
                    doc.settings.odd_and_even_pages_header_footer = active
                    originals = {prefix: getattr(doc.sections[0], prefix + role).paragraphs[0]._p.xml
                                 for prefix in ("first_page_", "even_page_")}
                    out, report, audits = self.execute(root, doc,
                        {role: {"font": {"cjk": "黑体"}}}, {role: role.title()})
                    for prefix in ("", "first_page_", "even_page_"):
                        p = getattr(out.sections[0], prefix + role).paragraphs[0]
                        self.assertEqual(snapshot(out, p)["font"]["cjk"], "黑体" if not prefix or active else "楷体")
                        if prefix and not active:
                            self.assertEqual(p._p.xml, originals[prefix])
                    coverage = next(item for item in report["role_coverage"] if item["role"] == role)
                    self.assertEqual(coverage["count"], 3 if active else 1)

    def test_unbound_front_is_preserved_and_reported_unverified_by_public_cli(self):
        from test_reconstructed_section_safety import source_document, rule
        from page_field_ops import inspect_docx
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            doc = source_document(root / "fixture.docx", front_start=9)
            before = inspect_docx(root / "fixture.docx")
            overrides = rule()
            overrides["requirements"] = [{
                "id": "R-page", "role": "page", "properties": overrides["page"],
                "evidence_ids": ["E-page"], "resolved_by": "rule", "confidence": 1,
                "source_text": "Synthetic page numbering with only a body selector."}]
            out, report, audits = self.execute(root, doc, {}, {}, spec_overrides=overrides)
            after = inspect_docx(root / "synthetic-output.docx")
            for source, actual in zip(before["sections"][:5], after["sections"][:5]):
                self.assertEqual(actual["page_number"], source["page_number"])
                self.assertEqual(actual["stories"], source["stories"])
            self.assertEqual(after["sections"][5]["page_number"]["start"], 1)
            plan = json.loads((audits / "section-plan-applied.json").read_text())
            self.assertIsNone(plan["front_matter_start_section"])
            items = report["draft_scorecard"]["entries"]
            deferrals = [item for item in items if item.get("detail", {}).get("code") == "front_selector_unresolved_preserved"]
            self.assertEqual([item["status"] for item in deferrals], ["unverified"])
            receipts = json.loads((audits / "property-receipts.json").read_text())["receipts"]
            front = [item for item in receipts if item.get("property_path") in {"page_number.front_matter_format", "page_number.front_matter_start"}]
            self.assertEqual(len(front), 2)
            self.assertTrue(all(item["status"] == "unverified" for item in front), front)
            body_start = next(item for item in receipts if item.get("property_path") == "page_number.body_start")
            self.assertEqual(body_start["status"], "verified", body_start)

    def test_pagination_does_not_drop_non_page_footer_properties(self):
        from section_executor import add_page_field
        for mixed_page_paragraph in (False, True):
            with self.subTest(mixed_page_paragraph=mixed_page_paragraph), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                doc = Document()
                doc.add_paragraph("synthetic ordinary source")
                style = doc.styles.add_style("Original Footer", WD_STYLE_TYPE.PARAGRAPH)
                font(style, size=18, latin="Courier New")
                paragraph_format(style)
                story = doc.sections[0].footer
                page = story.paragraphs[0]
                page.style = style
                page.alignment = WD_ALIGN_PARAGRAPH.CENTER
                if mixed_page_paragraph:
                    font(page.add_run("中文 prefix "), size=18, latin="Courier New")
                add_page_field(page, "ARABIC")
                paragraph = story.add_paragraph("中文 footer prose", style=style)
                font(paragraph.runs[0], size=18, latin="Courier New")
                link = OxmlElement("w:hyperlink")
                rid = story.part.relate_to("https://example.invalid/synthetic-footer", RT.HYPERLINK, is_external=True)
                link.set(qn("r:id"), rid)
                run = OxmlElement("w:r")
                text = OxmlElement("w:t")
                text.text = " 中文 linked text"
                run.append(text)
                link.append(run)
                paragraph._p.append(link)
                font(Run(run, paragraph), size=18, latin="Courier New")
                table = story.add_table(rows=1, cols=1, width=Inches(2))
                cell = table.cell(0, 0).paragraphs[0]
                cell.style = style
                font(cell.add_run("中文 footer table"), size=18, latin="Courier New")
                source_order = [child.tag for child in story._element]
                source_text = story._element.xpath(".//w:t/text()")
                page_properties = etree.tostring(page._p.pPr, method="c14n", exclusive=True)
                footer_rule = {"font": {"size_pt": 12, "latin": "Times New Roman", "cjk": "黑体"},
                               "paragraph": {"alignment": "justify", "space_before_pt": 3,
                                             "space_after_pt": 4}}
                page_rule = {"body_format": "decimal", "alignment": "center"}
                if mixed_page_paragraph:
                    # Preserve an unresolved front section containing PAGE
                    # and text in one paragraph. A later body section has an
                    # explicit numbering boundary and an independent PAGE.
                    doc.add_section(WD_SECTION_START.NEW_PAGE)
                    doc.add_paragraph("synthetic body section")
                    doc.sections[1].footer.is_linked_to_previous = False
                    body_page = doc.sections[1].footer.paragraphs[0]
                    body_page.style = style
                    add_page_field(body_page, "ARABIC")
                    page_rule.update(front_matter_format="roman_upper",
                                     body_start_selector={"strategy": "section_index", "section_index": 2})
                out, report, audits = self.execute(root, doc, {"footer": footer_rule}, {"footer": "Footer"},
                    spec_overrides={"page": {"page_number": page_rule}})
                story = out.sections[0].footer
                targets = [story.paragraphs[1], story.tables[0].cell(0, 0).paragraphs[0]]
                for p in targets:
                    with self.subTest(target=p.text):
                        actual = snapshot(out, p)
                        for key, value in footer_rule["font"].items():
                            self.assertEqual(actual["font"][key], value)
                        for key, value in footer_rule["paragraph"].items():
                            self.assertEqual(actual["paragraph"][key], value)
                        self.assertEqual(p.style.name, "Original Footer")
                # PAGE layout and all story content remain owned by their
                # original structure/SectionPlan, not a generic footer rule.
                page = story.paragraphs[0]
                self.assertEqual(etree.tostring(page._p.pPr, method="c14n", exclusive=True), page_properties)
                self.assertEqual(snapshot(out, page)["font"]["size_pt"], 12)
                self.assertEqual(story._element.xpath(".//w:t/text()"), source_text)
                self.assertEqual([child.tag for child in story._element], source_order)
                self.assertEqual(page._p.xpath("./w:fldSimple/@w:instr"), ["PAGE \\* ARABIC"])
                self.assertEqual(story.part.rels[rid].target_ref, "https://example.invalid/synthetic-footer")
                receipts = json.loads((audits / "property-receipts.json").read_text())["receipts"]
                alignment = next(item for item in receipts if item["property_path"] == "paragraph.alignment")
                self.assertNotEqual(alignment["status"], "verified", alignment)
                self.assertEqual(set(alignment["actual"]), {"center", "justify"})
                font_size = next(item for item in receipts if item["property_path"] == "font.size_pt")
                self.assertEqual(font_size["status"], "verified", font_size)
                self.assertTrue(any(item.get("code") == "footer_page_paragraph_properties_preserved"
                                    for item in report["scope_execution_deferrals"]))
                coverage = next(item for item in report["role_coverage"] if item["role"] == "footer")
                self.assertEqual(coverage["count"], 4 if mixed_page_paragraph else 3)

    def test_numbered_mixed_page_paragraph_still_fails_closed_without_relayout(self):
        from section_executor import add_page_field
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            doc = Document()
            doc.add_paragraph("synthetic ordinary source")
            paragraph = doc.sections[0].footer.paragraphs[0]
            paragraph.text = "Do not remove this existing prefix: "
            add_page_field(paragraph, "ARABIC")
            args, source, spec, output, audits = review_args(root, doc,
                {"footer": {"font": {"size_pt": 12}}}, {"footer": "Footer"},
                spec_overrides={"page": {"page_number": {"body_format": "decimal"}}})
            before = source.read_bytes()
            result = subprocess.run(args, capture_output=True, text=True, timeout=90,
                                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("automatic placement would be ambiguous", result.stderr)
            self.assertEqual(source.read_bytes(), before)
            self.assertFalse(output.exists())

    def test_property_only_story_shared_style_does_not_change_inactive_variants(self):
        for role in ("header", "footer"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                doc = Document()
                doc.add_paragraph("synthetic ordinary source")
                style = doc.styles[role.title()]
                font(style, size=18, latin="Courier New")
                paragraph_format(style)
                for prefix in ("", "first_page_", "even_page_"):
                    p = getattr(doc.sections[0], prefix + role).paragraphs[0]
                    p.text = prefix + "中文 synthetic shared style"
                    p.style = style
                source_style_xml = style.element.xml
                before = {prefix: snapshot(doc, getattr(doc.sections[0], prefix + role).paragraphs[0])
                          for prefix in ("", "first_page_", "even_page_")}
                requested = {"font": {"size_pt": 12, "cjk": "黑体"},
                             "paragraph": {"alignment": "right", "space_before_pt": 2}}
                out, report, audits = self.execute(root, doc, {role: requested}, {role: role.title()})
                for prefix in ("", "first_page_", "even_page_"):
                    expected = before[prefix]
                    if not prefix:
                        for group, values in requested.items():
                            expected[group].update(values)
                    self.assertEqual(snapshot(out, getattr(out.sections[0], prefix + role).paragraphs[0]), expected)
                self.assertEqual(out.styles[role.title()].element.xml, source_style_xml)


if __name__ == "__main__":
    unittest.main()
