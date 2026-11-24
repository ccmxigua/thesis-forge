"""Synthetic, dependency-light regressions for the reconstructed section code."""
from __future__ import annotations

import base64
import copy
import io
import inspect
import json
import posixpath
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
from page_field_ops import inspect_docx
from section_executor import add_page_field, execute_section_plan, unlink_story_preserving_content
from section_model import audit_plan_against_docx, compile_section_plan

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jBv8AAAAASUVORK5CYII=')
R_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
URL = 'https://example.invalid/synthetic-university'


def rule(*, explicit_front=False):
    page = {'front_matter_format': 'roman_upper', 'front_matter_start': 1,
            'body_format': 'decimal', 'body_start': 1,
            'body_start_selector': {'strategy': 'section_index', 'section_index': 6}}
    if explicit_front:
        page['front_matter_start_selector'] = {'strategy': 'section_index', 'section_index': 3}
    return {'page': {'page_number': page}}


def review_plan(spec, source):
    # Exercise the old implementation's behavior as well as the new explicit
    # review API, so the pre-fix log shows the actual wrong restart/link scope.
    options = ({'preserve_unresolved_front': True}
               if 'preserve_unresolved_front' in inspect.signature(compile_section_plan).parameters else {})
    return compile_section_plan(spec, source, **options)


def source_document(path, *, front_start=1):
    doc = Document()
    for index, title in enumerate(('Cover', 'Declaration', 'Abstract ZH', 'Abstract EN', 'Contents', 'Body', 'Chapter 2')):
        if index:
            doc.add_section(WD_SECTION.NEW_PAGE)
        doc.add_paragraph(title)
    doc.sections[0].header.paragraphs[0].text = ''
    for index, section in enumerate(doc.sections):
        if index >= 2:
            pg = OxmlElement('w:pgNumType')
            pg.set(qn('w:fmt'), 'upperRoman' if index < 5 else 'decimal')
            if index in (2, 5):
                pg.set(qn('w:start'), str(front_start if index == 2 else 1))
            section._sectPr.append(pg)
        if index in (2, 5):
            section.footer.is_linked_to_previous = False
            add_page_field(section.footer.paragraphs[0], 'ROMAN' if index == 2 else 'ARABIC')
        if index == 5:
            section.header.is_linked_to_previous = False
            section.header.paragraphs[0].text = 'Original chapter header'
    doc.save(path)
    return Document(path)


def add_logo_link(story):
    paragraph = story.add_paragraph()
    paragraph.add_run().add_picture(io.BytesIO(PNG))
    rid = story.part.relate_to(URL, RT.HYPERLINK, is_external=True)
    link = OxmlElement('w:hyperlink')
    link.set(qn('r:id'), rid)
    run, text = OxmlElement('w:r'), OxmlElement('w:t')
    text.text = 'Synthetic university link'
    run.append(text)
    link.append(run)
    paragraph._p.append(link)


def story_source(path, *, kind, explicit):
    doc = Document()
    doc.add_paragraph('Section A')
    add_logo_link(getattr(doc.sections[0], kind))
    doc.add_section(WD_SECTION.NEW_PAGE)
    doc.add_paragraph('Section B')
    if explicit:
        ref = doc.sections[0]._sectPr.find(qn('w:' + kind + 'Reference'))
        doc.sections[1]._sectPr.insert(0, copy.deepcopy(ref))
    doc.save(path)
    return Document(path)


def linked_targets(path, kind):
    result = []
    with zipfile.ZipFile(path) as archive:
        for section in inspect_docx(path)['sections']:
            part = section['stories'][kind]['default']['part']
            if not part:
                result.append([])
                continue
            root = etree.fromstring(archive.read(part))
            rels_path = str(Path(part).parent / '_rels' / (Path(part).name + '.rels'))
            rels = ({node.get('Id'): node for node in etree.fromstring(archive.read(rels_path))}
                    if rels_path in archive.namelist() else {})
            targets = []
            for node in root.iter():
                for attribute, rid in node.attrib.items():
                    if not attribute.startswith('{' + R_NS + '}'):
                        continue
                    if rid not in rels:
                        raise AssertionError(f'{part} has dangling {attribute}={rid}')
                    rel = rels[rid]
                    if rel.get('TargetMode') == 'External':
                        targets.append((rel.get('Type'), rel.get('Target')))
                    else:
                        target = posixpath.normpath(str(Path(part).parent / rel.get('Target')))
                        targets.append((rel.get('Type'), archive.read(target)))
            result.append(targets)
    return result


class SectionSafetyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='synthetic-section-safety-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source, self.output = self.root / 'source.docx', self.root / 'output.docx'

    def test_review_preservation_is_an_explicit_compiler_option(self):
        self.assertIn('preserve_unresolved_front', inspect.signature(compile_section_plan).parameters)

    def test_review_unbound_front_retains_original_starts_fields_and_links(self):
        for front_start in (1, 9):
            with self.subTest(front_start=front_start):
                doc = source_document(self.source, front_start=front_start)
                before = inspect_docx(self.source)
                plan = review_plan(rule(), self.source)
                self.assertTrue(plan['valid'], plan['findings'])
                self.assertIsNone(plan['front_matter_start_section'])
                self.assertIn('front_selector_unresolved_preserved', {x['code'] for x in plan['findings']})
                self.assertTrue(all(x['page_properties_policy'] == 'preserve_existing' for x in plan['sections'][:5]))
                execute_section_plan(doc, plan, alignment='center')
                # Review UI does not establish or add a source section boundary.
                doc.paragraphs[0].insert_paragraph_before('Synthetic review UI')
                doc.save(self.output)
                after = inspect_docx(self.output)
                self.assertEqual(len(after['sections']), 7)
                for source, actual in zip(before['sections'][:5], after['sections'][:5]):
                    self.assertEqual(actual['page_number'], source['page_number'])
                    self.assertEqual(actual['stories'], source['stories'])
                self.assertEqual(after['sections'][5]['page_number']['start'], 1)
                self.assertEqual(audit_plan_against_docx(plan, self.output), [])

    def test_explicit_front_and_body_selectors_still_execute(self):
        doc = source_document(self.source, front_start=9)
        plan = review_plan(rule(explicit_front=True), self.source)
        self.assertEqual(plan['front_matter_start_section'], 3)
        execute_section_plan(doc, plan)
        doc.save(self.output)
        after = inspect_docx(self.output)
        self.assertIsNone(after['sections'][0]['page_number']['start'])
        self.assertEqual(after['sections'][2]['page_number']['start'], 1)
        self.assertEqual(after['sections'][5]['page_number']['start'], 1)
        self.assertEqual(audit_plan_against_docx(plan, self.output), [])

    def test_explicit_none_and_formatless_preservation_keep_their_contracts(self):
        for spec in ({}, {'page': {'page_number': {'body_format': 'none'}}}):
            with self.subTest(spec=spec):
                doc = source_document(self.source)
                plan = review_plan(spec, self.source)
                execute_section_plan(doc, plan)
                doc.save(self.output)
                self.assertEqual(audit_plan_against_docx(plan, self.output), [])
                if spec:
                    self.assertTrue(all(s['page_number']['start'] is None for s in inspect_docx(self.output)['sections']))

    def test_invalid_selector_remains_blocked(self):
        source_document(self.source)
        spec = rule()
        spec['page']['page_number']['front_matter_start_selector'] = {'strategy': 'section_index', 'section_index': 99}
        plan = review_plan(spec, self.source)
        self.assertFalse(plan['valid'])
        self.assertNotIn('front_selector_unresolved_preserved', {x['code'] for x in plan['findings']})

    def test_preserved_restart_tampering_is_detected(self):
        doc = source_document(self.source)
        plan = review_plan(rule(), self.source)
        execute_section_plan(doc, plan)
        doc.sections[2]._sectPr.find(qn('w:pgNumType')).set(qn('w:start'), '77')
        doc.save(self.output)
        self.assertIn('preserved_page_contract_mismatch', {x['code'] for x in audit_plan_against_docx(plan, self.output)})

    def test_absent_inherited_story_is_created_only_on_target_section(self):
        for kind in ('header', 'footer'):
            with self.subTest(kind=kind):
                doc = Document()
                doc.add_paragraph('A')
                doc.add_section(WD_SECTION.NEW_PAGE)
                doc.add_paragraph('B')
                unlink_story_preserving_content(getattr(doc.sections[1], kind))
                self.assertEqual([len(s._sectPr.findall(qn('w:' + kind + 'Reference'))) for s in doc.sections], [0, 1])

    def test_isolation_preserves_logo_and_link_relationships(self):
        for kind in ('header', 'footer'):
            for explicit in (False, True):
                with self.subTest(kind=kind, explicit=explicit):
                    doc = story_source(self.source, kind=kind, explicit=explicit)
                    before = linked_targets(self.source, kind)
                    unlink_story_preserving_content(getattr(doc.sections[1], kind), force_clone=explicit)
                    doc.save(self.output)
                    self.assertIsNot(getattr(doc.sections[0], kind).part, getattr(doc.sections[1], kind).part)
                    self.assertEqual(linked_targets(self.output, kind), before)
                    self.assertEqual(inspect_docx(self.output)['findings'], [])

    def test_body_shared_logo_footer_does_not_rewrite_preserved_front(self):
        doc = source_document(self.source)
        add_logo_link(doc.sections[2].footer)
        old = doc.sections[2]._sectPr.find(qn('w:footerReference'))
        body = doc.sections[5]._sectPr.find(qn('w:footerReference'))
        body.set(qn('r:id'), old.get(qn('r:id')))
        doc.save(self.source)
        before = inspect_docx(self.source)
        targets = linked_targets(self.source, 'footer')
        plan = review_plan(rule(), self.source)
        execute_section_plan(doc, plan)
        doc.save(self.output)
        self.assertEqual(linked_targets(self.output, 'footer'), targets)
        self.assertEqual(inspect_docx(self.output)['sections'][2]['stories']['footer'], before['sections'][2]['stories']['footer'])
        self.assertEqual(audit_plan_against_docx(plan, self.output), [])

    def test_serialized_dangling_references_are_rejected(self):
        for kind in ('header', 'footer'):
            for corruption in ('missing_relationship', 'missing_target', 'duplicate_relationship'):
                with self.subTest(kind=kind, corruption=corruption):
                    story_source(self.source, kind=kind, explicit=False)
                    plan = compile_section_plan({}, self.source)
                    rels_name = f'word/_rels/{kind}1.xml.rels'
                    with zipfile.ZipFile(self.source) as zin, zipfile.ZipFile(self.output, 'w') as zout:
                        for info in zin.infolist():
                            data = zin.read(info.filename)
                            if info.filename == rels_name:
                                root = etree.fromstring(data)
                                image = next(node for node in root if node.get('Type') == RT.IMAGE)
                                if corruption == 'missing_relationship':
                                    root.remove(image)
                                elif corruption == 'missing_target':
                                    image.set('Target', 'media/missing.png')
                                else:
                                    root.append(copy.deepcopy(image))
                                data = etree.tostring(root)
                            zout.writestr(info, data)
                    self.assertIn('broken_story_content_relationship', {x['code'] for x in audit_plan_against_docx(plan, self.output)})

    def test_invalid_relationship_blocks_before_mutating_section_links(self):
        for defect in ('missing', 'unsupported'):
            with self.subTest(defect=defect):
                doc = story_source(self.source, kind='footer', explicit=False)
                story = doc.sections[0].footer
                rid = 'rIdMissing' if defect == 'missing' else story.part.relate_to('https://example.invalid/other', RT.OLE_OBJECT, is_external=True)
                story._element.xpath('.//a:blip')[0].set(qn('r:embed'), rid)
                before = doc.element.xml, story._element.xml, set(doc.part.rels)
                with self.assertRaisesRegex(ValueError, 'relationship'):
                    unlink_story_preserving_content(doc.sections[1].footer)
                self.assertEqual((doc.element.xml, story._element.xml, set(doc.part.rels)), before)

    def test_sparse_relationship_ids_are_remapped_without_source_changes(self):
        doc = story_source(self.source, kind='footer', explicit=False)
        part = doc.sections[0].footer.part
        for node in part.element.iter():
            for attribute, rid in list(node.attrib.items()):
                if not attribute.startswith('{' + R_NS + '}'):
                    continue
                rel = part.rels[rid]
                new = 'rId9' if rel.reltype == RT.IMAGE else 'rId42'
                part.rels.add_relationship(rel.reltype, rel.target_ref if rel.is_external else rel.target_part, new, rel.is_external)
                del part.rels[rid]
                node.set(attribute, new)
        doc.save(self.source)
        before = linked_targets(self.source, 'footer')
        unlink_story_preserving_content(doc.sections[1].footer)
        doc.save(self.output)
        self.assertEqual(linked_targets(self.output, 'footer'), before)
        self.assertEqual(set(part.rels), {'rId9', 'rId42'})
        self.assertEqual(set(doc.sections[1].footer.part.rels), {'rId1', 'rId2'})

    def test_second_execution_keeps_story_parts_and_relationships_stable(self):
        doc = source_document(self.source)
        add_logo_link(doc.sections[2].footer)
        doc.sections[5]._sectPr.find(qn('w:footerReference')).set(
            qn('r:id'), doc.sections[2]._sectPr.find(qn('w:footerReference')).get(qn('r:id')))
        doc.save(self.source)
        execute_section_plan(doc, review_plan(rule(), self.source))
        doc.save(self.output)
        before = inspect_docx(self.output)
        targets = linked_targets(self.output, 'footer')
        doc = Document(self.output)
        execute_section_plan(doc, review_plan(rule(), self.output))
        repeated = self.root / 'repeated.docx'
        doc.save(repeated)
        after = inspect_docx(repeated)
        self.assertEqual(set(after['stories']), set(before['stories']))
        self.assertEqual(after['sections'], before['sections'])
        self.assertEqual(linked_targets(repeated, 'footer'), targets)

    def test_partial_policy_plan_matches_schema(self):
        import jsonschema
        source_document(self.source)
        plan = review_plan(rule(), self.source)
        jsonschema.validate(plan, json.loads((ROOT / 'schema/section-plan.schema.json').read_text()))


if __name__ == '__main__':
    unittest.main(verbosity=2)
