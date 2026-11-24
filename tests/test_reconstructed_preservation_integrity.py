"""A preserve-existing audit must prove identities, not just equal field counts."""
from __future__ import annotations

import copy
import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from lxml import etree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(Path(__file__).parent))
from test_reconstructed_section_safety import add_logo_link
from page_field_ops import inspect_docx
from section_executor import add_page_field, execute_section_plan, unlink_story_preserving_content
from section_model import audit_plan_against_docx, compile_section_plan


class PreservationIntegrityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='synthetic-preservation-integrity-')
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.source, self.output = root / 'source.docx', root / 'changed.docx'
        doc = Document()
        doc.add_paragraph('Synthetic body')
        footer = doc.sections[0].footer
        add_page_field(footer.paragraphs[0], 'ROMAN')
        field = OxmlElement('w:fldSimple')
        field.set(qn('w:instr'), 'DATE \\@ yyyy')
        footer.add_paragraph()._p.append(field)
        add_logo_link(footer)
        doc.save(self.source)
        self.plan = compile_section_plan({}, self.source)

    def rewrite(self, mutate, extras=None):
        with zipfile.ZipFile(self.source) as zin, zipfile.ZipFile(self.output, 'w') as zout:
            for info in zin.infolist():
                zout.writestr(info, mutate(info.filename, zin.read(info.filename)))
            for name, data in (extras or {}).items():
                zout.writestr(name, data)

    def findings(self):
        actual = inspect_docx(self.output)
        self.assertEqual(len(actual['sections'][0]['stories']['footer']['default']['page_fields']), 1)
        return {x['code'] for x in audit_plan_against_docx(self.plan, self.output)}

    def test_simple_page_instruction_change_with_same_count_is_rejected(self):
        def mutate(name, data):
            if name == 'word/footer1.xml':
                root = etree.fromstring(data)
                root.find('.//' + qn('w:fldSimple')).set(qn('w:instr'), 'PAGE \\* ARABIC')
                return etree.tostring(root)
            return data
        self.rewrite(mutate)
        self.assertIn('preserved_footer_field_markup_mismatch', self.findings())

    def test_complex_page_instruction_change_with_same_count_is_rejected(self):
        doc = Document(self.source)
        paragraph = doc.sections[0].footer.paragraphs[0]
        paragraph._p.remove(paragraph._p.find(qn('w:fldSimple')))
        for kind, text in (('begin', None), ('instrText', 'PAGE \\* ROMAN'),
                           ('separate', None), ('t', '1'), ('end', None)):
            run = OxmlElement('w:r')
            if kind in ('begin', 'separate', 'end'):
                child = OxmlElement('w:fldChar')
                child.set(qn('w:fldCharType'), kind)
            else:
                child = OxmlElement('w:' + kind)
                child.text = text
            run.append(child)
            paragraph._p.append(run)
        doc.save(self.source)
        self.plan = compile_section_plan({}, self.source)
        self.rewrite(lambda name, data: data.replace(b'ROMAN', b'ARABIC') if name == 'word/footer1.xml' else data)
        self.assertIn('preserved_footer_field_markup_mismatch', self.findings())

    def test_nonpage_instruction_change_with_same_page_count_is_rejected(self):
        def mutate(name, data):
            if name == 'word/footer1.xml':
                return data.replace(b'DATE', b'TIME')
            return data
        self.rewrite(mutate)
        self.assertIn('preserved_footer_field_markup_mismatch', self.findings())

    def test_field_relocation_with_same_instructions_is_rejected(self):
        def mutate(name, data):
            if name == 'word/footer1.xml':
                root = etree.fromstring(data)
                paragraphs = root.findall(qn('w:p'))
                field = paragraphs[0].find(qn('w:fldSimple'))
                paragraphs[1].append(field)
                return etree.tostring(root)
            return data
        self.rewrite(mutate)
        self.assertIn('preserved_footer_field_markup_mismatch', self.findings())

    def test_valid_external_hyperlink_retarget_is_rejected(self):
        def mutate(name, data):
            if name == 'word/_rels/footer1.xml.rels':
                root = etree.fromstring(data)
                next(x for x in root if x.get('Type') == RT.HYPERLINK).set('Target', 'https://example.invalid/another-valid-target')
                return etree.tostring(root)
            return data
        self.rewrite(mutate)
        self.assertEqual(inspect_docx(self.output)['findings'], [])
        self.assertIn('preserved_footer_relationship_targets_mismatch', self.findings())

    def test_existing_internal_target_with_same_bytes_is_not_same_identity(self):
        with zipfile.ZipFile(self.source) as archive:
            image = archive.read('word/media/image1.png')
        def mutate(name, data):
            if name == 'word/_rels/footer1.xml.rels':
                root = etree.fromstring(data)
                next(x for x in root if x.get('Type') == RT.IMAGE).set('Target', 'media/another.png')
                return etree.tostring(root)
            return data
        self.rewrite(mutate, {'word/media/another.png': image})
        self.assertEqual(inspect_docx(self.output)['findings'], [])
        self.assertIn('preserved_footer_relationship_targets_mismatch', self.findings())

    def test_internal_target_bytes_change_at_same_path_is_rejected(self):
        # A valid PNG text chunk changes asset bytes without changing its path.
        import struct, zlib
        payload = b'Comment\x00changed synthetic image metadata'
        chunk = struct.pack('>I', len(payload)) + b'tEXt' + payload + struct.pack('>I', zlib.crc32(b'tEXt' + payload))
        self.rewrite(lambda name, data: data[:-12] + chunk + data[-12:] if name == 'word/media/image1.png' else data)
        self.assertEqual(inspect_docx(self.output)['findings'], [])
        self.assertIn('preserved_footer_relationship_targets_mismatch', self.findings())

    def test_missing_preservation_evidence_cannot_pass_by_count(self):
        plan = copy.deepcopy(self.plan)
        for section in plan['sections']:
            for variant in section['stories']['footer'].values():
                variant.pop('source_footer_integrity', None)
        self.assertIn('preserved_footer_integrity_unverified', {x['code'] for x in audit_plan_against_docx(plan, self.source)})

    def test_missing_current_identity_evidence_is_unverified(self):
        actual = inspect_docx(self.source)
        actual['sections'][0]['stories']['footer']['default'].pop('integrity', None)
        self.assertIn('preserved_footer_integrity_unverified',
                      {x['code'] for x in audit_plan_against_docx(self.plan, actual)})

    def test_explicit_page_font_changes_remain_allowed(self):
        doc = Document(self.source)
        execute_section_plan(doc, self.plan, footer_font={'size_pt': 18, 'latin': 'Arial'})
        doc.save(self.output)
        self.assertEqual(audit_plan_against_docx(self.plan, self.output), [])

    def test_header_content_is_not_frozen_by_pagination_preservation(self):
        doc = Document(self.source)
        doc.sections[0].header.paragraphs[0].text = 'Independently requested header'
        doc.save(self.output)
        self.assertEqual(audit_plan_against_docx(self.plan, self.output), [])

    def test_cloned_relationship_ids_keep_same_target_identity(self):
        doc = Document(self.source)
        doc.add_section(WD_SECTION.NEW_PAGE)
        doc.add_paragraph('Second section')
        doc.save(self.source)
        plan = compile_section_plan({}, self.source)
        unlink_story_preserving_content(doc.sections[1].footer)
        doc.save(self.output)
        self.assertEqual(audit_plan_against_docx(plan, self.output), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
