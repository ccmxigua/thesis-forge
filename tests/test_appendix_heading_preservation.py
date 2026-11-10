"""Appendix renumbering must retain native navigation and source formatting."""
from pathlib import Path
import sys
import unittest

from docx import Document
from lxml import etree
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import apply_format_spec as afs

RULE = {'label_style': 'alpha_upper'}


class AppendixHeadingPreservationTests(unittest.TestCase):
    def test_normalization_preserves_bookmark_and_title_run(self):
        doc = Document()
        heading = doc.add_paragraph()
        start = OxmlElement('w:bookmarkStart')
        start.set(qn('w:id'), '27')
        start.set(qn('w:name'), '_TocAppendix')
        heading._p.append(start)
        heading.add_run('附录B ')
        title = heading.add_run('模拟变量字典')
        title.bold = True
        end = OxmlElement('w:bookmarkEnd')
        end.set(qn('w:id'), '27')
        heading._p.append(end)
        title_xml, start_xml, end_xml = title._r.xml, etree.tostring(start), etree.tostring(end)

        result = afs.apply_appendix_rules(doc, RULE)

        self.assertEqual(heading.text, '附录 A  模拟变量字典')
        self.assertEqual(result['labels_normalized'], 1)
        self.assertEqual(etree.tostring(heading._p.xpath('./w:bookmarkStart')[0]), start_xml)
        self.assertEqual(etree.tostring(heading._p.xpath('./w:bookmarkEnd')[0]), end_xml)
        self.assertEqual(title._r.getparent(), heading._p)
        self.assertEqual(title._r.xml, title_xml)

    def test_normalization_preserves_split_label_runs_and_comments(self):
        doc = Document()
        heading = doc.add_paragraph()
        runs = [heading.add_run(s) for s in ('附', '录', 'Ｂ', '  ', '数据与参数')]
        runs[0].italic = True
        marker = OxmlElement('w:commentRangeStart')
        marker.set(qn('w:id'), '8')
        heading._p.insert(0, marker)
        end = OxmlElement('w:commentRangeEnd')
        end.set(qn('w:id'), '8')
        heading._p.append(end)
        title_xml = runs[-1]._r.xml

        afs.apply_appendix_rules(doc, RULE)

        self.assertEqual(heading.text, '附录 A  数据与参数')
        self.assertTrue(runs[0].italic)
        self.assertEqual(runs[-1]._r.xml, title_xml)
        self.assertEqual(runs[-1]._r.getparent(), heading._p)
        self.assertEqual(len(heading._p.xpath('./w:commentRangeStart')), 1)
        self.assertEqual(len(heading._p.xpath('./w:commentRangeEnd')), 1)

    def test_non_text_heading_is_preserved_and_audited_not_flattened(self):
        doc = Document()
        heading = doc.add_paragraph('附录A  数据')
        drawing = OxmlElement('w:drawing')
        heading.add_run()._r.append(drawing)
        before = heading._p.xml

        result = afs.apply_appendix_rules(doc, RULE)

        self.assertEqual(heading._p.xml, before)
        self.assertEqual(result['labels_normalized'], 0)
        self.assertEqual(result['labels_unresolved'], 1)
        findings = afs.audit_appendix_rules(doc, RULE, {})
        self.assertTrue(any(f.get('failure_type') == 'appendix_label_normalization_unresolved'
                            for f in findings))

    def test_repeat_normalization_is_idempotent(self):
        doc = Document()
        heading = doc.add_paragraph('附录B  数据')
        afs.apply_appendix_rules(doc, RULE)
        before = heading._p.xml
        self.assertEqual(afs.apply_appendix_rules(doc, RULE)['labels_normalized'], 0)
        self.assertEqual(heading._p.xml, before)

    def test_whitespace_and_titleless_labels_normalize_without_losing_runs(self):
        for text, expected in [('  附录Ｂ  数据  ', '附录 A  数据'),
                               (' 附录Ｂ ', '附录 A')]:
            with self.subTest(text=text):
                doc = Document()
                heading = doc.add_paragraph()
                runs = [heading.add_run(char) for char in text]
                afs.apply_appendix_rules(doc, RULE)
                self.assertEqual(heading.text, expected)
                self.assertTrue(all(run._r.getparent() is heading._p for run in runs))

    def test_fields_and_hyperlinks_are_not_flattened(self):
        for kind in ('field', 'hyperlink'):
            with self.subTest(kind=kind):
                doc = Document()
                heading = doc.add_paragraph('附录B  数据')
                if kind == 'field':
                    field = OxmlElement('w:fldSimple')
                    field.set(qn('w:instr'), 'REF appendix_source')
                    heading._p.append(field)
                else:
                    link = OxmlElement('w:hyperlink')
                    link.set(qn('w:anchor'), 'appendix_source')
                    heading._p.append(link)
                before = heading._p.xml
                result = afs.apply_appendix_rules(doc, RULE)
                self.assertEqual(heading._p.xml, before)
                self.assertEqual(result['labels_unresolved'], 1)
                self.assertTrue(afs.audit_appendix_rules(doc, RULE, {}))
