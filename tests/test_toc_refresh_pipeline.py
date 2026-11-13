"""Synthetic public-CLI checks; flags are requests, never page-number proof."""
import json
from pathlib import Path
import tempfile
import unittest
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from tests import toc_pipeline_fixture as fixture


def field(p, instruction, cached, locked=False):
    begin=OxmlElement('w:fldChar');begin.set(qn('w:fldCharType'),'begin')
    if locked:begin.set(qn('w:fldLock'),'true')
    p.add_run()._r.append(begin)
    node=OxmlElement('w:instrText');node.text=instruction;p.add_run()._r.append(node)
    sep=OxmlElement('w:fldChar');sep.set(qn('w:fldCharType'),'separate');p.add_run()._r.append(sep)
    p.add_run(cached)
    end=OxmlElement('w:fldChar');end.set(qn('w:fldCharType'),'end');p.add_run()._r.append(end)


class TocRefreshPipelineTests(unittest.TestCase):
    def execute(self, temp, locked=False, opcode='TOC'):
        doc=Document();doc.add_paragraph('第一章 合成测试',style='Heading 1')
        doc.add_paragraph('正文 synthetic body text')
        p=doc.add_paragraph();field(p, ' '+opcode+' \\o "1-3" ', '合成标题\t17',locked)
        roles={'body_text':{'font':{'size_pt':12}}}
        return fixture.PropertyAuthorityTests().execute(Path(temp),doc,roles,{'body_text':{'style_name':'Normal'}})

    def test_formatting_requests_refresh_without_changing_cached_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc,report,audits=self.execute(tmp)
            begins=doc._element.body.xpath('.//w:fldChar[@w:fldCharType="begin"]')
            self.assertEqual([n.get(qn('w:dirty')) for n in begins],['true'])
            self.assertIn('合成标题\t17','\n'.join(p.text for p in doc.paragraphs))
            self.assertIsNone(doc.settings.element.find(qn('w:updateFields')))
            receipt=json.loads((audits/'toc-refresh-request.json').read_text())
            self.assertEqual(receipt['status'],'native_update_required')
            self.assertTrue(receipt['refresh_flags_written'])
            self.assertFalse(receipt['field_results_recomputed'])
            self.assertFalse(receipt['native_page_numbers_verified'])
            self.assertFalse(receipt['cached_pages_trusted'])

    def test_locked_toc_remains_untouched_and_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc,report,audits=self.execute(tmp,locked=True)
            begins=doc._element.body.xpath('.//w:fldChar[@w:fldCharType="begin"]')
            self.assertIsNone(begins[0].get(qn('w:dirty')))
            self.assertIsNone(doc.settings.element.find(qn('w:updateFields')))
            receipt=json.loads((audits/'toc-refresh-request.json').read_text())
            self.assertEqual(receipt['status'],'unhandled_preserved')
            self.assertFalse(receipt['refresh_flags_written'])

    def test_non_toc_field_does_not_request_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc,report,audits=self.execute(tmp,opcode='REF')
            self.assertIsNone(doc.settings.element.find(qn('w:updateFields')))
            receipt=json.loads((audits/'toc-refresh-request.json').read_text())
            self.assertEqual(receipt['status'],'not_applicable')
            self.assertFalse(receipt['refresh_flags_written'])

    def test_refresh_does_not_enable_document_wide_field_evaluation(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc=Document();doc.add_paragraph('正文 synthetic text')
            field(doc.add_paragraph(), ' TOC \\o "1-3" ', '标题 17')
            field(doc.add_paragraph(), ' INCLUDETEXT "https://example.invalid/private" ', 'untouched')
            out,report,audits=fixture.PropertyAuthorityTests().execute(Path(tmp),doc,
                {'body_text':{'font':{'size_pt':12}}},{'body_text':{'style_name':'Normal'}})
            begins=out._element.body.xpath('.//w:fldChar[@w:fldCharType="begin"]')
            self.assertEqual([n.get(qn('w:dirty')) for n in begins],['true',None])
            self.assertIsNone(out.settings.element.find(qn('w:updateFields')))
            self.assertIn('untouched','\n'.join(p.text for p in out.paragraphs))

    def test_existing_settings_and_other_stories_preserved(self):
        for setting_value in (None, 'false', 'true'):
            with self.subTest(setting=setting_value), tempfile.TemporaryDirectory() as tmp:
                doc=Document();doc.add_paragraph('正文 synthetic text')
                field(doc.add_paragraph(), ' TOC \\o "1-3" ', '标题 17')
                field(doc.add_paragraph(), ' INCLUDETEXT "https://example.invalid/locked" ', 'locked cache',True)
                simple=OxmlElement('w:fldSimple');simple.set(qn('w:instr'),' INCLUDETEXT "https://example.invalid/simple" ')
                doc.add_paragraph()._p.append(simple)
                field(doc.sections[0].header.paragraphs[0], ' INCLUDETEXT "https://example.invalid/header" ', 'header cache')
                field(doc.sections[0].footer.paragraphs[0], ' INCLUDETEXT "https://example.invalid/footer" ', 'footer cache')
                if setting_value is not None:
                    setting=OxmlElement('w:updateFields');setting.set(qn('w:val'),setting_value);doc.settings.element.append(setting)
                originals=[doc.sections[0].header.part.blob,doc.sections[0].footer.part.blob]
                out,report,audits=fixture.PropertyAuthorityTests().execute(Path(tmp),doc,
                    {'body_text':{'font':{'size_pt':12}}},{'body_text':{'style_name':'Normal'}})
                setting=out.settings.element.find(qn('w:updateFields'))
                self.assertEqual(None if setting is None else setting.get(qn('w:val')),setting_value)
                begins=out._element.body.xpath('.//w:fldChar[@w:fldCharType="begin"]')
                self.assertEqual([n.get(qn('w:dirty')) for n in begins],['true',None])
                self.assertEqual(begins[1].get(qn('w:fldLock')),'true')
                self.assertEqual(out._element.body.xpath('.//w:fldSimple')[0].get(qn('w:instr')),simple.get(qn('w:instr')))
                self.assertEqual([out.sections[0].header.part.blob,out.sections[0].footer.part.blob],originals)
