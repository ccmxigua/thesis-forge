"""Source-review regressions on synthetic DOCX inputs through the public CLI."""
from __future__ import annotations

import json
import hashlib
import copy
from pathlib import Path
import tempfile
import unittest

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.shared import Pt

import test_reconstructed_property_authority as fixture


def spacing(target, **attributes):
    node = target._element.get_or_add_pPr().get_or_add_spacing()
    for key, value in attributes.items():
        node.set(qn('w:' + key), str(value))


def effective_attribute(paragraph, child, attribute):
    elements = [paragraph._p]
    current, seen = paragraph.style, set()
    while current is not None and current.style_id not in seen:
        elements.append(current.element)
        seen.add(current.style_id)
        current = current.base_style
    for element in elements:
        values = element.xpath('./w:pPr/w:' + child + '/@w:' + attribute)
        if values:
            return values[0]
    return None


def all_runs(paragraph):
    from docx.text.run import Run
    return [Run(node, paragraph) for node in paragraph._p.iter(qn('w:r'))]


def add_hyperlink(paragraph, text):
    link = OxmlElement('w:hyperlink')
    link.set(qn('r:id'), paragraph.part.relate_to(
        'https://example.invalid/synthetic', RT.HYPERLINK, is_external=True))
    run, value = OxmlElement('w:r'), OxmlElement('w:t')
    value.text = text
    run.append(value)
    link.append(run)
    paragraph._p.append(link)


class SourceReviewPropertyTests(unittest.TestCase):
    def execute(self, root, doc, roles, mappings, **kwargs):
        return fixture.PropertyAuthorityTests(methodName='runTest').execute(
            root, doc, roles, mappings, **kwargs)

    def test_keyword_content_font_preserves_label_and_links(self):
        for kind in ('split', 'single', 'hyperlink', 'inherited_label'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                doc = Document()
                style = doc.styles.add_style('Source Keywords', WD_STYLE_TYPE.PARAGRAPH)
                fixture.font(style, size=16, cjk='黑体', latin='Courier New')
                paragraph = doc.add_paragraph(style=style)
                if kind == 'single':
                    fixture.font(paragraph.add_run('关键词：原词；第二词'), size=16, cjk='黑体')
                else:
                    for text in ('关', '键词', '：'):
                        run = paragraph.add_run(text)
                        if kind != 'inherited_label':
                            fixture.font(run, size=16, cjk='黑体')
                    if kind == 'hyperlink':
                        add_hyperlink(paragraph, '原词；第二词')
                    else:
                        fixture.font(paragraph.add_run('原词；第二词'), size=15, cjk='楷体')
                source_text = ''.join(run.text for run in all_runs(paragraph))
                source_style = style.element.xml
                out, report, audits = self.execute(Path(temp), doc,
                    {'keywords_zh': {'font': {'cjk': '宋体', 'size_pt': 12},
                                     'paragraph': {'alignment': 'justify'}}},
                    {'keywords_zh': 'Source Keywords'})
                target = next(p for p in out.paragraphs if p.text.startswith('关键词'))
                runs = all_runs(target)
                self.assertEqual(''.join(run.text for run in runs), source_text)
                label, offset = [], 0
                for run in runs:
                    if offset < 4:
                        label.append(run)
                        self.assertLessEqual(offset + len(run.text), 4)
                        cjk = run._r.xpath('./w:rPr/w:rFonts/@w:eastAsia')
                        self.assertEqual(cjk[0] if cjk else target.style.element.rPr.rFonts.get(qn('w:eastAsia')), '黑体')
                        self.assertEqual(run.font.size.pt if run.font.size else target.style.font.size.pt, 16)
                    elif run.text:
                        self.assertEqual(run._r.rPr.rFonts.get(qn('w:eastAsia')), '宋体')
                        self.assertEqual(run.font.size.pt, 12)
                    offset += len(run.text)
                self.assertEqual(out.styles['Source Keywords'].element.xml, source_style)
                self.assertEqual(len(target._p.xpath('./w:hyperlink')), int(kind == 'hyperlink'))
                receipts = json.loads((audits / 'property-receipts.json').read_text())['receipts']
                font_receipts = [r for r in receipts if r['role'] == 'keywords_zh' and r['property_path'].startswith('font.')]
                self.assertTrue(font_receipts)
                self.assertTrue(all(r['status'] == 'verified' for r in font_receipts), font_receipts)
                observed = json.loads((audits / 'visible-instance-format.json').read_text())
                instance = observed['instance_evidence']['keywords_zh'][0]
                self.assertEqual(instance['font_text_span'], [4, len(source_text)])
                self.assertEqual(instance['text'], source_text)

    def test_unbound_keyword_label_does_not_authorize_font(self):
        for text in ('关键词 原词；第二词', '术语：原词；第二词'):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as temp:
                doc = Document()
                style = doc.styles.add_style('KeywordsLineCN', WD_STYLE_TYPE.PARAGRAPH)
                fixture.font(style, size=16, cjk='黑体')
                p = doc.add_paragraph(text, style=style)
                source = p._p.xml
                out, _, audits = self.execute(Path(temp), doc,
                    {'keywords_zh': {'font': {'cjk': '宋体', 'size_pt': 12}}},
                    {'keywords_zh': 'KeywordsLineCN'})
                target = next(p for p in out.paragraphs if p.text == text)
                self.assertEqual(target._p.xml, source)
                self.assertEqual(out.styles['KeywordsLineCN'].element.xml, style.element.xml)
                receipts = json.loads((audits / 'property-receipts.json').read_text())['receipts']
                self.assertFalse(any(r['status'] == 'verified' for r in receipts if r['role'] == 'keywords_zh'))

    def test_body_outline_ten_serializes_nine_and_receipts_match(self):
        for role, text, style in (('body_text', '正文 synthetic paragraph', 'Normal'),
                                  ('toc_title', '目录', 'TOC Heading'),
                                  ('keywords_zh', '关键词：合成', 'Normal')):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as temp:
                doc = Document()
                doc.add_paragraph(text, style=style)
                out, _, audits = self.execute(Path(temp), doc,
                    {role: {'paragraph': {'outline_level': 10}}}, {role: style})
                target = next(p for p in out.paragraphs if p.text == text)
                self.assertEqual(effective_attribute(target, 'outlineLvl', 'val'), '9')
                receipts = json.loads((audits / 'property-receipts.json').read_text())['receipts']
                outline = [r for r in receipts if r['property_path'] == 'paragraph.outline_level']
                self.assertTrue(outline)
                self.assertTrue(all(r['status'] == 'verified' and r['actual'] == 10 for r in outline), outline)

    def test_ninth_heading_is_not_reinterpreted_as_body(self):
        with tempfile.TemporaryDirectory() as temp:
            doc = Document()
            doc.add_paragraph('正文 synthetic explicitly level nine')
            out, _, _ = self.execute(Path(temp), doc,
                {'body_text': {'paragraph': {'outline_level': 9}}}, {'body_text': 'Normal'})
            target = next(p for p in out.paragraphs if p.text.startswith('正文'))
            self.assertEqual(effective_attribute(target, 'outlineLvl', 'val'), '8')

    def test_heading_style_is_based_on_its_real_source_not_misbound_map(self):
        for mapped in ('Header', 'Unrelated Template Banner'):
            with self.subTest(mapped=mapped), tempfile.TemporaryDirectory() as temp:
                doc = Document()
                source = doc.styles.add_style('Original Chapter', WD_STYLE_TYPE.PARAGRAPH)
                source.base_style = doc.styles['Heading 1']
                fixture.font(source, size=17, cjk='楷体')
                source.paragraph_format.space_after = Pt(7)
                paragraph = doc.add_paragraph('第1章 合成章', style=source)
                wrong = doc.styles[mapped] if mapped in doc.styles else doc.styles.add_style(mapped, WD_STYLE_TYPE.PARAGRAPH)
                border, bottom = OxmlElement('w:pBdr'), OxmlElement('w:bottom')
                bottom.set(qn('w:val'), 'single'); bottom.set(qn('w:sz'), '6')
                border.append(bottom); wrong.element.get_or_add_pPr().append(border)
                doc.sections[0].header.paragraphs[0].style = wrong
                doc.sections[0].header.paragraphs[0].text = '第1章 unrelated header'
                header_xml = doc.sections[0].header._element.xml
                out, _, audits = self.execute(Path(temp), doc,
                    {'heading_1': {'font': {'size_pt': 15},
                                   'paragraph': {'outline_level': 1},
                                   'numbering': {'format': '第{chapter}章', 'style': 'decimal'}}},
                    {'heading_1': mapped})
                target = next(p for p in out.paragraphs if p.text == paragraph.text)
                chain, current = [], target.style
                while current is not None and current.style_id not in chain:
                    chain.append(current.style_id)
                    self.assertFalse(current.element.xpath('./w:pPr/w:pBdr'), current.element.xml)
                    current = current.base_style
                self.assertIn(source.style_id, chain)
                self.assertEqual(target.runs[0].font.size.pt, 15)
                self.assertEqual(effective_attribute(target, 'spacing', 'after'), '140')
                self.assertEqual(out.sections[0].header._element.xml, header_xml)

    def test_explicit_line_spacing_replaces_inherited_line_and_auto_spacing(self):
        for value in (0, 0.75):
            with self.subTest(lines=value), tempfile.TemporaryDirectory() as temp:
                doc = Document()
                source = doc.styles.add_style('Original Subheading', WD_STYLE_TYPE.PARAGRAPH)
                source.base_style = doc.styles['Heading 2']
                spacing(source, beforeLines=50, beforeAutospacing=1, afterLines=125, afterAutospacing=1)
                paragraph = doc.add_paragraph('1.1 合成标题', style=source)
                out, _, audits = self.execute(Path(temp), doc,
                    {'heading_2': {'paragraph': {'space_before_lines': value, 'spacing_line_height_pt': 14}}},
                    {'heading_2': 'Heading 2'})
                target = next(p for p in out.paragraphs if p.text == paragraph.text)
                self.assertEqual(effective_attribute(target, 'spacing', 'beforeLines'), str(round(value * 100)))
                self.assertEqual(effective_attribute(target, 'spacing', 'beforeAutospacing'), '0')
                self.assertEqual(effective_attribute(target, 'spacing', 'afterLines'), '125')
                self.assertEqual(effective_attribute(target, 'spacing', 'afterAutospacing'), '1')
                receipts = json.loads((audits / 'property-receipts.json').read_text())['receipts']
                before = [r for r in receipts if r['property_path'] == 'paragraph.space_before_lines']
                self.assertTrue(before, receipts)
                self.assertTrue(all(r['status'] == 'verified' and r['actual'] == value for r in before), before)

    def test_numbered_headings_with_different_source_styles_keep_their_inheritance(self):
        with tempfile.TemporaryDirectory() as temp:
            doc = Document()
            styles = []
            for index in (1, 2):
                style = doc.styles.add_style('Source Chapter ' + str(index), WD_STYLE_TYPE.PARAGRAPH)
                style.base_style = doc.styles['Heading 1']
                fixture.font(style, size=17, cjk='楷体')
                style.paragraph_format.space_after = Pt(index + 6)
                doc.add_paragraph('合成章' + str(index), style=style)
                styles.append(style)
            out, _, audits = self.execute(Path(temp), doc,
                {'heading_1': {'font': {'size_pt': 15},
                               'numbering': {'format': '第{chapter}章', 'style': 'decimal'}}},
                {'heading_1': 'Header'})
            for index in (1, 2):
                paragraph = next(p for p in out.paragraphs if p.text.endswith('合成章' + str(index)))
                self.assertTrue(paragraph.text.startswith('第' + str(index) + '章'))
                self.assertEqual(effective_attribute(paragraph, 'spacing', 'after'), str((index + 6) * 20))
                actual = fixture.snapshot(out, paragraph)
                self.assertEqual(actual['font']['size_pt'], 15)
            receipts = json.loads((audits / 'property-receipts.json').read_text())['receipts']
            self.assertTrue(all(r['status'] == 'verified' for r in receipts
                               if r['property_path'] == 'font.size_pt'), receipts)

    def test_automatic_spacing_cannot_certify_requested_line_units(self):
        from visible_instance_format import collect_visible_instance_format
        doc = Document()
        p = doc.add_paragraph('Synthetic automatic spacing')
        spacing(p, beforeLines=0, beforeAutospacing=1)
        evidence = collect_visible_instance_format(doc, {'target': [p]},
            property_paths_by_role={'target': ['paragraph.space_before_lines']})
        self.assertFalse(evidence['role_observability']['target']['paragraph.space_before_lines']['observed'])

    def test_english_keyword_label_stays_distinct_and_complex_text_is_deferred(self):
        for complex_text in (False, True):
            with self.subTest(complex_text=complex_text), tempfile.TemporaryDirectory() as temp:
                doc = Document()
                p = doc.add_paragraph()
                fixture.font(p.add_run('Key words: alpha; beta'), size=16, latin='Courier New')
                if complex_text:
                    p.runs[0].add_break()
                before = p._p.xml
                out, _, audits = self.execute(Path(temp), doc,
                    {'keywords_en': {'font': {'latin': 'Times New Roman', 'size_pt': 12}}},
                    {'keywords_en': 'Normal'})
                target = next(p for p in out.paragraphs if p.text.startswith('Key words:'))
                if complex_text:
                    self.assertEqual(target._p.xml, before)
                else:
                    self.assertEqual(target.runs[0].text, 'Key words: ')
                    self.assertEqual(target.runs[0].font.name, 'Courier New')
                    self.assertEqual(target.runs[1].font.name, 'Times New Roman')
                receipts = json.loads((audits / 'property-receipts.json').read_text())['receipts']
                self.assertTrue(all((r['status'] == 'unverified' if complex_text else r['status'] == 'verified')
                                    for r in receipts if r['role'] == 'keywords_en'), receipts)

    def test_fresh_host_packet_allows_body_ten_and_preserves_ninth_heading(self):
        from requirements_engine import build_llm_request, validate_host_review_response
        for level, text in ((10, '正文大纲级别为正文文本'), (9, '正文大纲级别为第9级')):
            with self.subTest(level=level):
                clause = {'id': 'C00001', 'text': text, 'evidence_ids': ['E00001'],
                          'source_span': {'evidence_id': 'E00001', 'start_offset': 0,
                              'end_offset': len(text), 'text': text,
                              'source_sha256': hashlib.sha256(text.encode()).hexdigest()}}
                request = build_llm_request([], [clause],
                    {'evidence': [{'id': 'E00001', 'text': text}]},
                    {'roles': {}, 'requirements': []}, mode='full', contract_version='3.0')
                for root in (request['response_schema'], request['requirement_contract']):
                    leaf = root['$defs']['paragraphSpec']['properties']['outline_level']
                    self.assertEqual(leaf['maximum'], 10)
                    self.assertIn('正文文本', leaf['description'])
                response = {'contract_version': '3.0', 'requirements': [{
                    'role': 'body_text', 'properties': {'paragraph': {'outline_level': level}},
                    'clause_ids': ['C00001'], 'evidence_ids': ['E00001'], 'confidence': 1.0,
                    'reason': 'Synthetic explicit outline requirement.'}],
                    'clause_reviews': [{'clause_id': 'C00001', 'classification': 'executable',
                        'reason': 'Synthetic explicit outline requirement.',
                        'normative_basis': 'explicit_normative_text', 'obligations': [{
                            'id': 'O1', 'status': 'covered', 'reason': 'Synthetic exact outline duty.',
                            'actor': 'author', 'action': 'set outline level', 'target': 'body text',
                            'condition': 'unconditional', 'source_quote': text,
                            'force': 'required', 'applicability': 'applicable', 'route': 'automatic'}]}],
                    'unsupported_items': [], 'reported_conflicts': []}
                before = copy.deepcopy(response)
                self.assertEqual(validate_host_review_response(response, request), [])
                self.assertEqual(response, before)

    def test_structure_receipts_use_local_source_witnesses_not_last_projection(self):
        with tempfile.TemporaryDirectory() as temp:
            doc = Document()
            doc.add_paragraph('关键词：合成')
            doc.add_paragraph('第一章 合成', style='Heading 1')
            doc.add_paragraph('正文 synthetic source witness')
            doc.add_paragraph('参考文献', style='Heading 1')
            requirements = []
            for i, role in enumerate(('cover', 'keywords_zh', 'body_text', 'bibliography_heading')):
                requirements.append({'id': 'R-' + role, 'role': 'document_structure',
                    'properties': {'required_roles': [role]}, 'evidence_ids': ['E-' + str(i)],
                    'resolved_by': 'rule', 'confidence': 1,
                    'source_text': 'Synthetic explicit required section.'})
            out, report, audits = self.execute(Path(temp), doc, {}, {}, spec_overrides={
                'requirements': requirements,
                'document_structure': {'required_roles': ['bibliography_heading']}})
            receipts = json.loads((audits / 'property-receipts.json').read_text())['receipts']
            for role in ('keywords_zh', 'body_text', 'bibliography_heading'):
                item = next(r for r in receipts if r['requirement_id'] == 'R-' + role)
                self.assertEqual(item['status'], 'verified', item)
                self.assertEqual(item['actual'], [role])
            cover = next(r for r in receipts if r['requirement_id'] == 'R-cover')
            self.assertEqual(cover['status'], 'unverified', cover)
            self.assertFalse(any(item.get('property', '').startswith('required_roles.')
                and item.get('template_value') is False for item in report['findings']))

    def test_unrequested_line_spacing_remains_byte_identical(self):
        with tempfile.TemporaryDirectory() as temp:
            doc = Document()
            p = doc.add_paragraph('正文 synthetic unrequested spacing')
            spacing(p, beforeLines=50, beforeAutospacing=1, afterLines=125)
            before = p._p.pPr.spacing.xml
            out, _, _ = self.execute(Path(temp), doc,
                {'body_text': {'font': {'size_pt': 12}}}, {'body_text': 'Normal'})
            target = next(item for item in out.paragraphs if item.text == p.text)
            self.assertEqual(target._p.pPr.spacing.xml, before)


if __name__ == '__main__':
    unittest.main()
