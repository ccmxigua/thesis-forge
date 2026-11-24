"""Synthetic-only line-box, source-crop, PDF-observation and bundle regressions."""
from __future__ import annotations

import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from zipfile import ZipFile

import fitz
from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from apply_format_spec import apply_drawing_line_box_safety, audit_drawing_line_boxes
from rendered_format_audit import audit
from skill_bundle import build_bundle, verify_bundle


def png():
    stream = io.BytesIO()
    Image.new('RGB', (20, 20), 'blue').save(stream, format='PNG')
    return stream.getvalue()


def picture(p):
    p.add_run().add_picture(io.BytesIO(png()), width=Inches(1))
    return p._p.xpath('.//wp:inline')[-1]


def fixture():
    d = Document()
    p = d.add_paragraph()
    p.paragraph_format.line_spacing = Pt(12)
    p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY
    return d, p, picture(p)


class DrawingSafetyTests(unittest.TestCase):
    def test_dynamic_and_adequate_spacing_is_byte_preserved(self):
        for rule, value in [(None, None), (WD_LINE_SPACING.SINGLE, 1),
                            (WD_LINE_SPACING.MULTIPLE, 1.3),
                            (WD_LINE_SPACING.AT_LEAST, Pt(12)),
                            (WD_LINE_SPACING.AT_LEAST, Pt(100)),
                            (WD_LINE_SPACING.EXACTLY, Pt(100))]:
            with self.subTest(rule=rule, value=value):
                d = Document(); p = d.add_paragraph(); picture(p)
                if value is not None:
                    p.paragraph_format.line_spacing = value
                    p.paragraph_format.line_spacing_rule = rule
                before = p._p.xml
                self.assertEqual(audit_drawing_line_boxes(d), [])
                row = apply_drawing_line_box_safety(d)[0]
                self.assertFalse(row['changed'])
                self.assertEqual(p._p.xml, before)
                self.assertFalse(row['rendered_verified'])

    def test_relative_multiple_is_not_reported_in_points(self):
        d,p,_ = fixture(); p.paragraph_format.line_spacing = 1.3
        row = apply_drawing_line_box_safety(d)[0]
        self.assertIsNone(row['effective_line_spacing_before']['value_pt'])
        self.assertEqual(row['effective_line_spacing_before']['multiple'], 1.3)

    def test_anchor_does_not_authorize_or_inflate_inline_repair(self):
        d,p,node = fixture(); node.tag = qn('wp:anchor'); before = p._p.xml
        self.assertEqual(audit_drawing_line_boxes(d), [])
        self.assertFalse(apply_drawing_line_box_safety(d)[0]['changed'])
        self.assertEqual(p._p.xml, before)
        node.find(qn('wp:extent')).set('cy', str(720 * 12700))
        picture(p)
        self.assertTrue(apply_drawing_line_box_safety(d)[0]['changed'])
        self.assertEqual(p.paragraph_format.line_spacing.pt, 72)

    def test_unknown_or_partial_geometry_is_immutable_and_unverified(self):
        for kind in ('missing', 'invalid', 'zero', 'negative', 'effect', 'partial'):
            with self.subTest(kind=kind):
                d,p,node = fixture(); extent = node.find(qn('wp:extent'))
                if kind in ('missing', 'partial'):
                    node.remove(extent)
                    if kind == 'partial': picture(p)
                elif kind == 'effect':
                    effect = OxmlElement('wp:effectExtent'); effect.set('t', 'bad'); effect.set('b', '0'); node.insert(1, effect)
                else: extent.set('cy', {'invalid':'bad','zero':'0','negative':'-1'}[kind])
                before = p._p.xml
                self.assertFalse(apply_drawing_line_box_safety(d)[0]['changed'])
                self.assertEqual(p._p.xml, before)
                findings = audit_drawing_line_boxes(d)
                self.assertTrue(findings)
                self.assertTrue(all(f.get('status') == 'unverified' for f in findings))

    def test_effect_bounds_round_up_and_only_line_attributes_change(self):
        d,p,node = fixture()
        p.paragraph_format.space_before = Pt(9); p.paragraph_format.space_after = Pt(7)
        effect = OxmlElement('wp:effectExtent')
        for key,val in {'l':0,'t':12700,'r':0,'b':6351}.items(): effect.set(key,str(val))
        node.insert(1,effect)
        drawing_before, styles_before = node.xml, d.styles._element.xml
        self.assertTrue(apply_drawing_line_box_safety(d)[0]['changed'])
        self.assertEqual(p.paragraph_format.line_spacing.pt, 73.55)
        self.assertEqual(p.paragraph_format.space_before.pt,9)
        self.assertEqual(p.paragraph_format.space_after.pt,7)
        self.assertEqual(node.xml,drawing_before); self.assertEqual(d.styles._element.xml, styles_before)
        stream=io.BytesIO(); d.save(stream); reopened=Document(io.BytesIO(stream.getvalue()))
        before=reopened._element.xml
        self.assertFalse(apply_drawing_line_box_safety(reopened)[0]['changed'])
        self.assertEqual(reopened._element.xml,before)

    def test_inherited_exact_is_repaired_without_changing_style(self):
        d = Document(); p = d.add_paragraph(); picture(p)
        normal = d.styles['Normal']
        normal.paragraph_format.line_spacing = Pt(12)
        normal.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY
        before=normal._element.xml
        self.assertTrue(apply_drawing_line_box_safety(d)[0]['changed'])
        self.assertEqual(p.paragraph_format.line_spacing.pt,72)
        self.assertEqual(normal._element.xml,before)

    def test_malformed_inheritance_or_spacing_never_authorizes_mutation(self):
        cases=('empty_pstyle','empty_basedon','wrong_type','duplicate_spacing','duplicate_pstyle',
               'duplicate_basedon','missing_style','missing_default_id','bad_rule','bad_line','cyclic_style',
               'missing_style_direct','cyclic_style_direct')
        for case in cases:
            with self.subTest(case=case):
                d = Document(); p = d.add_paragraph(); picture(p)
                defaults=d.styles._element.xpath('./w:docDefaults/w:pPrDefault/w:pPr/w:spacing')[0]
                defaults.set(qn('w:line'),'240'); defaults.set(qn('w:lineRule'),'exact')
                if case.endswith('_direct'):
                    p.paragraph_format.line_spacing=Pt(12)
                    p.paragraph_format.line_spacing_rule=WD_LINE_SPACING.EXACTLY
                if case in ('empty_pstyle','duplicate_pstyle','missing_style','missing_style_direct'):
                    for _ in range(2 if case=='duplicate_pstyle' else 1):
                        n=OxmlElement('w:pStyle')
                        if case!='empty_pstyle': n.set(qn('w:val'),'Absent' if case.startswith('missing_style') else 'Normal')
                        p._p.get_or_add_pPr().append(n)
                elif case in ('empty_basedon','duplicate_basedon','cyclic_style','cyclic_style_direct'):
                    style=d.styles.add_style('DrawingStyle',WD_STYLE_TYPE.PARAGRAPH);p.style=style
                    for _ in range(2 if case=='duplicate_basedon' else 1):
                        n=OxmlElement('w:basedOn')
                        if case!='empty_basedon': n.set(qn('w:val'),style.style_id if case.startswith('cyclic_style') else 'Normal')
                        style._element.append(n)
                elif case=='wrong_type':
                    style=d.styles.add_style('WrongType',WD_STYLE_TYPE.CHARACTER)
                    n=OxmlElement('w:pStyle');n.set(qn('w:val'),style.style_id);p._p.get_or_add_pPr().append(n)
                elif case=='missing_default_id': del d.styles['Normal']._element.attrib[qn('w:styleId')]
                else:
                    for rule in (('exact','auto') if case=='duplicate_spacing' else ('mystery' if case=='bad_rule' else 'exact',)):
                        n=OxmlElement('w:spacing');n.set(qn('w:line'),'bad' if case=='bad_line' else '240');n.set(qn('w:lineRule'),rule);p._p.get_or_add_pPr().append(n)
                before=p._p.xml
                row=apply_drawing_line_box_safety(d)[0]
                self.assertFalse(row['changed']); self.assertEqual(p._p.xml,before)
                self.assertEqual(row['assessment_before'],'unverified')
                self.assertTrue(all(f.get('status')=='unverified' for f in audit_drawing_line_boxes(d)))


class DrawingPdfTests(unittest.TestCase):
    def run_audit(self, root, *, source_crop=None, output_crop=None, clip=False, unknown=False, minimum=False):
        paths=[root / x for x in ('source.docx','output.docx','synthetic.pdf','spec.json','styles.json')]
        source,output,pdf,spec,styles=paths
        for path,crop in ((source,source_crop),(output,output_crop)):
            d=Document();p=d.add_paragraph();node=picture(p)
            if minimum: p.paragraph_format.line_spacing=Pt(12);p.paragraph_format.line_spacing_rule=WD_LINE_SPACING.AT_LEAST
            if unknown: node.remove(node.find(qn('wp:extent')))
            if crop is not None:
                n=OxmlElement('a:srcRect');n.set('t',crop);p._p.xpath('.//pic:blipFill')[0].insert(1,n)
            d.save(path)
        out=fitz.open(); page=out.new_page(width=595,height=842)
        page.insert_image(fitz.Rect(100,100,172,172),stream=png())
        if clip:
            xref=page.get_contents()[-1]
            out.update_stream(xref,b'q 100 670 36 72 re W n\n'+out.xref_stream(xref)+b'\nQ')
        out.save(pdf);out.close();spec.write_text('{}');styles.write_text('{}')
        return audit(source,output,pdf,spec,style_map_path=styles)

    def test_unbound_pdf_clip_cannot_pass_even_for_on_page_bbox(self):
        for clip in (False,True):
            with self.subTest(clip=clip),tempfile.TemporaryDirectory() as td:
                report=self.run_audit(Path(td),clip=clip)
                row=report['drawing_visibility'][0]
                self.assertEqual(row['status'],'unverified')
                self.assertIsNone(row['rendered']['visible_area_ratio'])
                self.assertEqual(row['rendered']['raster_bbox_area_inside_page_ratio'],1)
                self.assertFalse(row['rendered']['visible_clip_bound'])
                self.assertNotEqual(report['status'],'passed')

    def test_source_crop_removal_and_change_are_independently_reported(self):
        for output in (None,'25000'):
            with self.subTest(output=output),tempfile.TemporaryDirectory() as td:
                report=self.run_audit(Path(td),source_crop='50000',output_crop=output)
                self.assertIn('drawing_crop_changed',{f['code'] for f in report['findings']})

    def test_crop_and_unknown_geometry_do_not_become_false_clipping_claims(self):
        for mode in ('crop','unknown','minimum'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as td:
                kw={'source_crop':'50000','output_crop':'50000'} if mode=='crop' else {mode:True}
                report=self.run_audit(Path(td),**kw)
                codes={f['code'] for f in report['findings']}
                self.assertNotIn('drawing_line_box_not_object_safe',codes)
                self.assertNotIn('rendered_drawing_clipped_by_page',codes)
                self.assertNotIn('rendered_drawing_extent_mismatch',codes)
                self.assertIn('rendered_drawing_visibility_unverified',codes)
                if mode=='unknown': self.assertIn('drawing_line_box_geometry_unverified',codes)


class DrawingBundleTests(unittest.TestCase):
    def test_helper_is_shipped_and_relocated_imports_work(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); archive=root/'runtime.zip'; target=root/'relocated skill'
            build_bundle(ROOT,archive)
            with ZipFile(archive) as bundle:
                helper='scripts/drawing_line_box.py'
                self.assertIn(helper,bundle.namelist())
                self.assertIn('tests/test_reconstructed_drawing_safety.py',bundle.namelist())
                self.assertEqual(bundle.read(helper),(ROOT/helper).read_bytes())
                bundle.extractall(target)
            self.assertEqual(verify_bundle(target)['status'],'pass')
            env=os.environ.copy();env.pop('PYTHONPATH',None)
            run=subprocess.run([sys.executable,'-I','-B','-c',
                                'import sys; sys.path.insert(0,sys.argv[1]); import apply_format_spec, rendered_format_audit, drawing_line_box',
                                str(target/'scripts')],cwd=root,env=env,capture_output=True,text=True)
            self.assertEqual(run.returncode,0,run.stderr)


if __name__=='__main__': unittest.main()
