"""Synthetic non-raster drawings must not disappear from PDF visibility audits."""
import tempfile
import unittest
from pathlib import Path
from lxml import etree
from docx import Document
from docx.oxml.ns import qn
import fitz
from test_reconstructed_drawing_safety import picture
from rendered_format_audit import audit

class NonRasterVisibilityTests(unittest.TestCase):
    def test_unbound_nonraster_or_external_drawings_remain_unverified(self):
        for mode in ('chart','smartart','vector','linked','mixed'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as td:
                root=Path(td); d=Document(); p=d.add_paragraph(); picture(p)
                if mode=='mixed': picture(p)
                drawing=p._p.xpath('.//w:drawing')[-1]
                if mode=='linked':
                    blip=drawing.xpath('.//a:blip')[0];blip.attrib.pop(qn('r:embed'));blip.set(qn('r:link'),'rIdExternal')
                else:
                    graphic=drawing.xpath('.//a:graphicData')[0]
                    for child in list(graphic):graphic.remove(child)
                    ns,tag={'chart':('http://schemas.openxmlformats.org/drawingml/2006/chart','chart'),'smartart':('http://schemas.openxmlformats.org/drawingml/2006/diagram','relIds'),'vector':('http://schemas.microsoft.com/office/word/2010/wordprocessingShape','wsp'),'mixed':('http://schemas.openxmlformats.org/drawingml/2006/chart','chart')}[mode]
                    graphic.append(etree.Element('{'+ns+'}'+tag))
                source=root/'source.docx';d.save(source)
                pdf=root/'synthetic.pdf';f=fitz.open();f.new_page();f.save(pdf);f.close()
                spec=root/'spec.json';spec.write_text('{}'); styles=root/'styles.json';styles.write_text('{}')
                result=audit(source,source,pdf,spec,style_map_path=styles)
                rows=result['drawing_visibility']
                self.assertEqual(len(rows),2 if mode=='mixed' else 1)
                self.assertEqual(rows[-1]['status'],'unverified')
                self.assertIn('rendered_drawing_visibility_unverified',{x['code'] for x in result['findings']})
                self.assertNotEqual(result['status'],'passed')

    def test_single_group_with_embedded_and_unsupported_content_is_not_raster_only(self):
        from rendered_format_audit import _drawing_records
        for tag in ('linked', 'chart', 'vector', 'smartart'):
            with self.subTest(tag=tag), tempfile.TemporaryDirectory() as td:
                d=Document();p=d.add_paragraph();picture(p)
                data=p._p.xpath('.//a:graphicData')[0]
                if tag=='linked':
                    extra=etree.Element(qn('a:blip'));extra.set(qn('r:link'),'rIdExternal')
                else:
                    ns,name={'chart':('http://schemas.openxmlformats.org/drawingml/2006/chart','chart'),'vector':('http://schemas.microsoft.com/office/word/2010/wordprocessingShape','wsp'),'smartart':('http://schemas.openxmlformats.org/drawingml/2006/diagram','relIds')}[tag]
                    extra=etree.Element('{'+ns+'}'+name)
                group=etree.Element('{http://schemas.microsoft.com/office/word/2010/wordprocessingGroup}wgp')
                for child in list(data):
                    data.remove(child); group.append(child)
                group.append(extra);data.append(group)
                path=Path(td)/'group.docx';d.save(path)
                rows,_=_drawing_records(path)
                self.assertEqual(len(rows),2)
                self.assertEqual(sum(r.get('visibility_binding_supported') is False for r in rows),1)
