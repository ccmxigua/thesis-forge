from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from lxml import etree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from compatibility_builder import (_flatten_complex_fields, _normalize_declared_fonts,
                                   _replacement_text, build_compatibility_edition)  # noqa: E402
from ooxml_compatibility import NS, audit_docx  # noqa: E402
from apply_format_spec import normalize_section_property_order  # noqa: E402
from docx import Document  # noqa: E402
from docx.oxml import OxmlElement  # noqa: E402
from docx.oxml.ns import qn  # noqa: E402

PROFILE = ROOT / "template_profiles" / "cau-graduate-thesis-2025-winter" / "profile.json"
TRIMMED = ROOT / "build" / "cau-template-engine" / "cau-official-trimmed-master.docx"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"


def minimal_docx(path: Path, *, macrobutton: bool = False, external_image: bool = False,
                 anchor: bool = False, complete_section: bool = True) -> None:
    nsmap = {"w": W, "r": R, "wp": WP}
    document = etree.Element(f"{{{W}}}document", nsmap=nsmap)
    body = etree.SubElement(document, f"{{{W}}}body")
    paragraph = etree.SubElement(body, f"{{{W}}}p")
    if macrobutton:
        for kind, instruction, text in (("begin", None, None),
                                        (None, " MACROBUTTON Title1 [type title] ", None),
                                        ("separate", None, None),
                                        (None, None, "type title"),
                                        ("end", None, None)):
            run = etree.SubElement(paragraph, f"{{{W}}}r")
            if kind:
                etree.SubElement(run, f"{{{W}}}fldChar").set(f"{{{W}}}fldCharType", kind)
            if instruction:
                etree.SubElement(run, f"{{{W}}}instrText").text = instruction
            if text:
                etree.SubElement(run, f"{{{W}}}t").text = text
    else:
        etree.SubElement(etree.SubElement(paragraph, f"{{{W}}}r"), f"{{{W}}}t").text = "body"
    if anchor:
        drawing = etree.SubElement(etree.SubElement(paragraph, f"{{{W}}}r"), f"{{{W}}}drawing")
        etree.SubElement(drawing, f"{{{WP}}}anchor")
    section = etree.SubElement(body, f"{{{W}}}sectPr")
    etree.SubElement(section, f"{{{W}}}pgSz", {f"{{{W}}}w": "11906", f"{{{W}}}h": "16838"})
    if complete_section:
        etree.SubElement(section, f"{{{W}}}pgMar", {f"{{{W}}}top": "1440", f"{{{W}}}right": "1440",
                                                         f"{{{W}}}bottom": "1440", f"{{{W}}}left": "1440"})
    styles = etree.Element(f"{{{W}}}styles", nsmap={"w": W})
    fonts = etree.Element(f"{{{W}}}fonts", nsmap={"w": W})
    etree.SubElement(fonts, f"{{{W}}}font", {f"{{{W}}}name": "Arial"})
    content_types = etree.Element("{http://schemas.openxmlformats.org/package/2006/content-types}Types")
    for part, kind in (("/word/document.xml", "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"),
                       ("/word/styles.xml", "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"),
                       ("/word/fontTable.xml", "application/vnd.openxmlformats-officedocument.wordprocessingml.fontTable+xml")):
        etree.SubElement(content_types, "{http://schemas.openxmlformats.org/package/2006/content-types}Override",
                         PartName=part, ContentType=kind)
    package_rels = etree.Element(f"{{{REL}}}Relationships")
    etree.SubElement(package_rels, f"{{{REL}}}Relationship", Id="rId1",
                     Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument",
                     Target="word/document.xml")
    document_rels = etree.Element(f"{{{REL}}}Relationships")
    if external_image:
        etree.SubElement(document_rels, f"{{{REL}}}Relationship", Id="rId9",
                         Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image",
                         Target="https://example.invalid/image.png", TargetMode="External")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, root in (("[Content_Types].xml", content_types), ("_rels/.rels", package_rels),
                           ("word/document.xml", document), ("word/styles.xml", styles),
                           ("word/fontTable.xml", fonts), ("word/_rels/document.xml.rels", document_rels)):
            archive.writestr(name, etree.tostring(root, xml_declaration=True, encoding="UTF-8"))


class OoxmlCompatibilityTests(unittest.TestCase):
    def test_section_header_footer_references_are_moved_before_page_setup(self):
        document = Document()
        sectpr = document.sections[0]._sectPr
        footer = OxmlElement("w:footerReference")
        footer.set(qn("w:type"), "default")
        footer.set(qn("r:id"), "rId99")
        sectpr.append(footer)
        self.assertNotEqual(list(sectpr)[0].tag, qn("w:footerReference"))

        self.assertEqual(normalize_section_property_order(document), 1)
        self.assertEqual(list(sectpr)[0].tag, qn("w:footerReference"))
        self.assertEqual(list(sectpr)[0].get(qn("r:id")), "rId99")
        self.assertEqual(normalize_section_property_order(document), 0)

    def test_clean_minimal_docx_passes(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "clean.docx"
            minimal_docx(path)
            report = audit_docx(path)
            self.assertTrue(report["compatible"], report["findings"])
            self.assertEqual(report["failure_mode"], "closed")

    def test_risky_features_fail_closed_with_structured_findings(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "risky.docx"
            minimal_docx(path, macrobutton=True, external_image=True, anchor=True, complete_section=False)
            report = audit_docx(path)
            self.assertFalse(report["compatible"])
            codes = {item["code"] for item in report["findings"]}
            self.assertIn("macrobutton_field", codes)
            self.assertIn("external_relationship", codes)
            self.assertIn("floating_drawing", codes)
            self.assertIn("section_page_setup_incomplete", codes)
            self.assertTrue(all(item["severity"] == "error" for item in report["findings"]))

    def test_bare_macrobutton_without_cached_result_flattens_to_empty_static_text(self):
        self.assertEqual(_replacement_text(" MACROBUTTON AcceptAllChangesShown ", "", {}), "")

    def test_header_macrobutton_is_flattened_by_story_transform(self):
        header = etree.Element(f"{{{W}}}hdr", nsmap={"w": W})
        paragraph = etree.SubElement(header, f"{{{W}}}p")
        for kind, instruction, text in (("begin", None, None),
                                        (None, " MACROBUTTON Title1 [type title] ", None),
                                        ("separate", None, None),
                                        (None, None, "type title"),
                                        ("end", None, None)):
            run = etree.SubElement(paragraph, f"{{{W}}}r")
            if kind:
                etree.SubElement(run, f"{{{W}}}fldChar").set(f"{{{W}}}fldCharType", kind)
            if instruction:
                etree.SubElement(run, f"{{{W}}}instrText").text = instruction
            if text:
                etree.SubElement(run, f"{{{W}}}t").text = text
        changes = _flatten_complex_fields(header, {})
        self.assertEqual(len(changes), 1)
        self.assertEqual(header.xpath("string(.//w:t)", namespaces=NS), "type title")
        self.assertFalse(header.xpath(".//w:instrText", namespaces=NS))

    def test_missing_referenced_fonts_are_added_to_font_table(self):
        document = etree.Element(f"{{{W}}}document", nsmap={"w": W})
        rpr = etree.SubElement(etree.SubElement(document, f"{{{W}}}r"), f"{{{W}}}rPr")
        etree.SubElement(rpr, f"{{{W}}}rFonts").set(f"{{{W}}}ascii", "Consolas")
        fonts = etree.Element(f"{{{W}}}fonts", nsmap={"w": W})
        etree.SubElement(fonts, f"{{{W}}}font", {f"{{{W}}}name": "Arial"})
        members = {
            "word/document.xml": etree.tostring(document),
            "word/fontTable.xml": etree.tostring(fonts),
        }
        self.assertEqual(_normalize_declared_fonts(members), ["Consolas"])
        normalized = etree.fromstring(members["word/fontTable.xml"])
        self.assertIn("Consolas", normalized.xpath("//w:font/@w:name", namespaces=NS))

    @unittest.skipUnless(TRIMMED.is_file(), "profile-built CAU trimmed master is not present")
    def test_real_compatibility_build_flattens_fields_and_emits_pending_manifest(self):
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            output = temp / "compatible.docx"
            manifest = temp / "word-finalization.json"
            result = build_compatibility_edition(PROFILE, output, manifest, source=TRIMMED)
            self.assertTrue(output.is_file())
            self.assertTrue(result["compatibility_editing_edition"]["static_compatibility_passed"])
            self.assertFalse(result["word_finalization"]["submission_ready"])
            self.assertEqual(result["word_finalization"]["status"], "pending_trusted_word_render")
            self.assertIn("originality_declaration_2025",
                          result["transformations"]["fixed_declaration_ooxml_preserved"])
            with zipfile.ZipFile(output) as archive:
                root = etree.fromstring(archive.read("word/document.xml"))
                instructions = " ".join(root.xpath("//w:instrText/text()", namespaces=NS))
                self.assertNotIn("MACROBUTTON", instructions.upper())
                self.assertEqual(len(root.xpath("//wp:anchor", namespaces=NS)), 0)
                self.assertFalse(any(b"www.wps.cn" in archive.read(name).lower()
                                     for name in archive.namelist()
                                     if name.startswith("customXml/") and name.endswith(".xml")))
            persisted = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertFalse(persisted["word_finalization"]["submission_ready"])


if __name__ == "__main__":
    unittest.main()
