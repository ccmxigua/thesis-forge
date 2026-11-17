"""Explicit enabled pagination contracts must override a direct false flag."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import unittest
from docx import Document
from docx.oxml import OxmlElement
from apply_format_spec import apply_object_pagination

class ObjectPaginationTests(unittest.TestCase):
    def figure(self, enabled=True, matching=True):
        doc = Document()
        picture = doc.add_paragraph()
        picture.add_run()._r.append(OxmlElement('w:drawing'))
        picture.paragraph_format.keep_with_next = False
        doc.add_paragraph('Figure 1', 'Caption' if matching else 'Normal')
        result = apply_object_pagination(doc, {'figure_caption': {'style_name': 'Caption'}}, {'keep_figure_with_caption': enabled}, {})
        return picture, result

    def test_explicit_false_figure_is_enabled(self):
        paragraph, result = self.figure()
        self.assertIs(paragraph.paragraph_format.keep_with_next, True)
        self.assertEqual(result['figures_kept_with_captions'], 1)

    def test_explicit_false_table_caption_is_enabled(self):
        doc = Document()
        caption = doc.add_paragraph('Table 1', 'Caption')
        caption.paragraph_format.keep_with_next = False
        doc.add_table(rows=1, cols=1)
        result = apply_object_pagination(doc, {'table_caption': {'style_name': 'Caption'}}, {'keep_table_with_caption': True}, {})
        self.assertIs(caption.paragraph_format.keep_with_next, True)
        self.assertEqual(result['table_captions_kept_with_tables'], 1)

    def test_no_contract_preserves_document_xml(self):
        doc = Document()
        picture = doc.add_paragraph()
        picture.add_run()._r.append(OxmlElement('w:drawing'))
        picture.paragraph_format.keep_with_next = False
        doc.add_paragraph('Figure 1', 'Caption')
        caption = doc.add_paragraph('Table 1', 'Caption')
        caption.paragraph_format.keep_with_next = False
        doc.add_table(rows=1, cols=1)
        before = doc.element.xml
        result = apply_object_pagination(doc, {'figure_caption': {'style_name': 'Caption'}, 'table_caption': {'style_name': 'Caption'}}, {}, {})
        self.assertEqual(doc.element.xml, before)
        self.assertEqual(sum(result.values()), 0)

    def test_disabled_or_unmatched_table_preserves_document_xml(self):
        for enabled, matched in [(False, True), (True, False)]:
            with self.subTest(enabled=enabled, matched=matched):
                doc = Document()
                caption = doc.add_paragraph('Table 1', 'Caption' if matched else 'Normal')
                caption.paragraph_format.keep_with_next = False
                doc.add_table(rows=1, cols=1)
                before = doc.element.xml
                result = apply_object_pagination(doc, {'table_caption': {'style_name': 'Caption'}}, {'keep_table_with_caption': enabled}, {})
                self.assertEqual(doc.element.xml, before)
                self.assertEqual(result['table_captions_kept_with_tables'], 0)

    def test_disabled_contract_preserves_direct_false(self):
        paragraph, result = self.figure(enabled=False)
        self.assertIs(paragraph.paragraph_format.keep_with_next, False)
        self.assertEqual(result['figures_kept_with_captions'], 0)

    def test_unmatched_caption_preserves_direct_false(self):
        paragraph, result = self.figure(matching=False)
        self.assertIs(paragraph.paragraph_format.keep_with_next, False)
        self.assertEqual(result['figures_kept_with_captions'], 0)

if __name__ == '__main__':
    unittest.main()
