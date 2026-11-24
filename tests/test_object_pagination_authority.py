"""Synthetic pagination authority tests; no renderer or school inputs.

The existing explicit keep flag authorizes the already-supported adjacent
image/caption relation.  It does not authorize inferring subfigure groups
from label text.  Counts describe relations actually kept, not attempts.
"""
from __future__ import annotations

from copy import deepcopy
import io
from pathlib import Path
import sys
import unittest
from zipfile import ZipFile

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt
from lxml import etree
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from apply_format_spec import apply_object_pagination


MAPPINGS = {"figure_caption": {"style_name": "FigureCaption"}}
ENABLED = {"keep_figure_with_caption": True}
ZERO_COUNTS = {
    "figures_kept_with_captions": 0,
    "table_captions_kept_with_tables": 0,
}


def add_picture(paragraph):
    stream = io.BytesIO()
    Image.new("RGB", (20, 20), "blue").save(stream, format="PNG")
    paragraph.add_run().add_picture(io.BytesIO(stream.getvalue()), width=Inches(1))


def set_keep_next(paragraph, value):
    if value is not None:
        node = OxmlElement("w:keepNext")
        node.set(qn("w:val"), value)
        paragraph._p.get_or_add_pPr().append(node)


def fixture(keep_next="0"):
    doc = Document()
    doc.styles.add_style("FigureCaption", WD_STYLE_TYPE.PARAGRAPH)
    picture = doc.add_paragraph()
    add_picture(picture)
    picture.paragraph_format.space_before = Pt(9)
    picture.paragraph_format.space_after = Pt(7)
    picture.paragraph_format.keep_together = False
    set_keep_next(picture, keep_next)
    caption = doc.add_paragraph("图1-1    图题", style="FigureCaption")
    caption.paragraph_format.keep_with_next = False
    prose = doc.add_paragraph("Unrelated following prose.")
    prose.paragraph_format.keep_with_next = False
    return doc


def package_parts(doc):
    """Compare member payloads, avoiding irrelevant ZIP timestamps."""
    stream = io.BytesIO()
    doc.save(stream)
    with ZipFile(io.BytesIO(stream.getvalue())) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def reopen(doc):
    stream = io.BytesIO()
    doc.save(stream)
    return Document(io.BytesIO(stream.getvalue()))


def document_without_first_keep_next(doc):
    root = deepcopy(doc._element)
    for node in root.xpath("./w:body/w:p[1]/w:pPr/w:keepNext"):
        node.getparent().remove(node)
    return etree.tostring(root, method="c14n")


class ObjectPaginationAuthorityTests(unittest.TestCase):
    def test_explicit_true_overrides_each_false_keep_next_after_serialization(self):
        # Catches the append-only implementation that leaves explicit false in place.
        for value in ("0", "false", "off"):
            with self.subTest(keep_next=value):
                doc = fixture(value)
                counts = apply_object_pagination(doc, MAPPINGS, ENABLED, {})
                restored = reopen(doc)
                self.assertIs(restored.paragraphs[0].paragraph_format.keep_with_next, True)
                self.assertEqual(counts["figures_kept_with_captions"], 1)
                self.assertIs(restored.paragraphs[1].paragraph_format.keep_with_next, False)
                self.assertIs(restored.paragraphs[2].paragraph_format.keep_with_next, False)

    def test_success_count_matches_relations_really_kept_in_saved_document(self):
        # One initially false and one unset relation exposes attempted-vs-real counts.
        doc = fixture("0")
        second_picture = doc.add_paragraph()
        add_picture(second_picture)
        doc.add_paragraph("图1-2    图题", style="FigureCaption")
        counts = apply_object_pagination(doc, MAPPINGS, ENABLED, {})
        restored = reopen(doc)
        actual_kept = sum(
            restored.paragraphs[index].paragraph_format.keep_with_next is True
            for index in (0, 3)
        )
        self.assertEqual(counts["figures_kept_with_captions"], actual_kept)
        self.assertEqual(actual_kept, 2)
        self.assertEqual(counts["table_captions_kept_with_tables"], 0)

    def test_missing_keep_next_is_set_without_other_package_changes_and_is_idempotent(self):
        doc = fixture(None)
        before_document = document_without_first_keep_next(doc)
        before_parts = package_parts(doc)
        counts = apply_object_pagination(doc, MAPPINGS, ENABLED, {})
        restored = reopen(doc)
        self.assertIs(restored.paragraphs[0].paragraph_format.keep_with_next, True)
        self.assertEqual(counts["figures_kept_with_captions"], 1)
        self.assertEqual(document_without_first_keep_next(restored), before_document)
        after_parts = package_parts(restored)
        del before_parts["word/document.xml"]
        del after_parts["word/document.xml"]
        self.assertEqual(after_parts, before_parts)
        once = package_parts(restored)
        again = apply_object_pagination(restored, MAPPINGS, ENABLED, {})
        self.assertEqual(again["figures_kept_with_captions"], 1)
        self.assertEqual(package_parts(restored), once)

    def test_absent_or_false_flag_preserves_all_package_payloads(self):
        for object_spec in ({}, {"keep_figure_with_caption": False}):
            with self.subTest(object_spec=object_spec):
                doc = fixture("0")
                before = package_parts(doc)
                self.assertEqual(apply_object_pagination(doc, MAPPINGS, object_spec, {}), ZERO_COUNTS)
                self.assertEqual(package_parts(doc), before)

    def test_missing_or_wrong_caption_mapping_preserves_all_package_payloads(self):
        for mappings in ({}, {"figure_caption": {"style_name": "OtherCaption"}}):
            with self.subTest(mappings=mappings):
                doc = fixture("0")
                before = package_parts(doc)
                self.assertEqual(apply_object_pagination(doc, mappings, ENABLED, {}), ZERO_COUNTS)
                self.assertEqual(package_parts(doc), before)

    def test_nonadjacent_caption_is_not_bound_through_label_prose_or_blank(self):
        for intervening_text in ("(a)分图1", "Unrelated prose.", ""):
            with self.subTest(intervening_text=intervening_text):
                doc = fixture("0")
                intervening = doc.paragraphs[1].insert_paragraph_before(intervening_text)
                intervening.paragraph_format.keep_with_next = False
                before = package_parts(doc)
                self.assertEqual(apply_object_pagination(doc, MAPPINGS, ENABLED, {}), ZERO_COUNTS)
                self.assertEqual(package_parts(doc), before)

    def test_five_paragraph_unbound_subfigure_group_is_unchanged_not_counted(self):
        # Scope guard, not a claim that the real multi-panel layout is repaired.
        doc = Document()
        doc.styles.add_style("FigureCaption", WD_STYLE_TYPE.PARAGRAPH)
        for label in ("(a)分图1", "(b)分图2"):
            picture = doc.add_paragraph()
            add_picture(picture)
            picture.paragraph_format.keep_with_next = False
            doc.add_paragraph(label).paragraph_format.keep_with_next = False
        doc.add_paragraph("图1-2    图题", style="FigureCaption").paragraph_format.keep_with_next = False
        before = package_parts(doc)
        self.assertEqual(apply_object_pagination(doc, MAPPINGS, ENABLED, {}), ZERO_COUNTS)
        self.assertEqual(package_parts(doc), before)


if __name__ == "__main__":
    unittest.main()
