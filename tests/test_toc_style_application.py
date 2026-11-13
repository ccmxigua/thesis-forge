"""Real CLI TOC style persistence; cached direct formatting is not authority."""
import json,tempfile,unittest
from pathlib import Path
from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.shared import Pt
from docx.oxml.ns import qn
from tests.test_toc_refresh_pipeline import field
from tests import toc_pipeline_fixture as fixture

class TocStyleApplicationTests(unittest.TestCase):
 def execute(self,root,shared=False,template=True,locked=False,ambiguous=False,mixed=False,toc_size=None,themed=False,cjk_only=False):
  d=Document();d.add_heading('第一章 合成',1);d.add_paragraph('正文 synthetic')
  st=d.styles.add_style('toc 1',WD_STYLE_TYPE.PARAGRAPH);st.base_style=d.styles['Normal']
  if themed:
   fonts=st.element.get_or_add_rPr().get_or_add_rFonts()
   fonts.set(qn('w:eastAsiaTheme'),'minorEastAsia')
   fonts.set(qn('w:asciiTheme'),'minorHAnsi')
   fonts.set(qn('w:hAnsiTheme'),'minorHAnsi')
   fonts.set(qn('w:cstheme'),'minorBidi')
  p=d.add_paragraph(style=st);field(p,' TOC \\o "1-1" ','第一章 合成\t1',locked)
  if mixed:p.add_run("unrelated outside field")
  for r in p.runs:r.font.size=Pt(14)
  if ambiguous:d.styles.add_style('TOC 1',WD_STYLE_TYPE.PARAGRAPH)
  if shared:d.add_paragraph('Unrelated text',style=st)
  t=Document();run=t.add_paragraph('目录').add_run()
  # Anchor must include visible title text.
  p=t.paragraphs[0];t.add_comment(p.runs,text='一级目录项：中文字体为宋体，西文字体为Times New Roman，四号，两端对齐，首行无缩进，左缩进0字符，右缩进0字符，段前和段后0行，1.5倍行距。',author='Fixture')
  if cjk_only:
   for node in t.part._comments_part.element.iter(qn('w:t')):
    node.text=(node.text or '').replace('西文字体为Times New Roman，','')
  path=root/'template.docx';t.save(path)
  roles={'body_text':{'font':{'size_pt':12}}}; mappings={'body_text':'Normal'}
  if toc_size is not None:roles['toc']={'font':{'size_pt':toc_size}};mappings['toc']='toc 1'
  return fixture.PropertyAuthorityTests().execute(root,d,roles,mappings,path if template else None)
 def test_bound_template_level_survives_cache_rebuild(self):
  with tempfile.TemporaryDirectory() as temp:
   d,_,a=self.execute(Path(temp));self.assertEqual(d.styles['toc 1'].font.size,Pt(14))
   self.assertEqual(d.styles['toc 1'].paragraph_format.line_spacing,1.5)
   r=json.loads((a/'toc-style-application.json').read_text());self.assertTrue(r['serialized_audit']['valid']);self.assertFalse(r['native_verified'])
 def test_shared_style_not_mutated(self):
  with tempfile.TemporaryDirectory() as temp:
   d,_,a=self.execute(Path(temp),shared=True);self.assertIsNone(d.styles['toc 1'].font.size)
   r=json.loads((a/'toc-style-application.json').read_text());self.assertEqual(r['status'],'unhandled_preserved')
 def test_no_explicit_template_no_cache_inference(self):
  with tempfile.TemporaryDirectory() as temp:
   d,_,a=self.execute(Path(temp),template=False);self.assertIsNone(d.styles['toc 1'].font.size)
   r=json.loads((a/'toc-style-application.json').read_text());self.assertEqual(r['status'],'not_required')

 def test_locked_field_preserved(self):
  with tempfile.TemporaryDirectory() as temp:
   d,_,a=self.execute(Path(temp),locked=True);self.assertIsNone(d.styles['toc 1'].font.size)
   r=json.loads((a/'toc-style-application.json').read_text());self.assertEqual(r['status'],'unhandled_preserved')
 def test_ambiguous_styles_preserved(self):
  with tempfile.TemporaryDirectory() as temp:
   d,_,a=self.execute(Path(temp),ambiguous=True);self.assertIsNone(d.styles['toc 1'].font.size)
   r=json.loads((a/'toc-style-application.json').read_text());self.assertEqual(r['status'],'unhandled_preserved')

 def test_mixed_cache_paragraph_preserved(self):
  with tempfile.TemporaryDirectory() as temp:
   d,_,a=self.execute(Path(temp),mixed=True);self.assertIsNone(d.styles['toc 1'].font.size)
   r=json.loads((a/'toc-style-application.json').read_text());self.assertEqual(r['status'],'unhandled_preserved')

 def test_written_toc_font_cannot_be_overridden_by_template(self):
  with tempfile.TemporaryDirectory() as temp:
   d,_,a=self.execute(Path(temp),toc_size=12)
   self.assertEqual(d.styles['toc 1'].font.size,Pt(12))
   r=json.loads((a/'toc-style-application.json').read_text());self.assertEqual(r['status'],'unhandled_preserved')
   self.assertTrue(any(x['code']=='toc_style_written_authority_present' for x in r['findings']))
 def test_matching_written_rule_not_silently_extended(self):
  with tempfile.TemporaryDirectory() as temp:
   d,_,a=self.execute(Path(temp),toc_size=14)
   r=json.loads((a/'toc-style-application.json').read_text());self.assertEqual(r['status'],'unhandled_preserved')
   self.assertEqual(r['changes'],[])

 def test_explicit_toc_faces_replace_competing_theme_tokens(self):
  from font_declarations import style_slot
  with tempfile.TemporaryDirectory() as temp:
   d,_,a=self.execute(Path(temp),themed=True)
   st=d.styles['toc 1']
   self.assertEqual(style_slot(st,'eastAsia'),(True,'SimSun'))
   for slot in ('ascii','hAnsi','cs'):
    self.assertEqual(style_slot(st,slot),(True,'Times New Roman'))
   r=json.loads((a/'toc-style-application.json').read_text())
   self.assertTrue(r['serialized_audit']['valid'])
   self.assertFalse(r['native_verified'])

 def test_unrequested_latin_theme_slots_remain(self):
  from font_declarations import style_slot
  with tempfile.TemporaryDirectory() as temp:
   d,_,a=self.execute(Path(temp),themed=True,cjk_only=True)
   st=d.styles['toc 1'];fonts=st.element.get_or_add_rPr().get_or_add_rFonts()
   self.assertEqual(style_slot(st,'eastAsia'),(True,'SimSun'))
   self.assertEqual(fonts.get(qn('w:asciiTheme')),'minorHAnsi')
   self.assertEqual(fonts.get(qn('w:hAnsiTheme')),'minorHAnsi')
   self.assertEqual(fonts.get(qn('w:cstheme')),'minorBidi')
