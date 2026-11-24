"""Exercise the mutating application path, not only the read-only compiler."""
import sys,unittest
from pathlib import Path
from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.section import WD_SECTION
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from apply_format_spec import ensure_page_number_sections,resolve_body_start_section
class TocApplicationTests(unittest.TestCase):
 def fixture(self,style='toc 1',derived=False,boundary=True):
  d=Document();s=d.styles.add_style(style,WD_STYLE_TYPE.PARAGRAPH)
  if derived:
   child=d.styles.add_style('University contents',WD_STYLE_TYPE.PARAGRAPH);child.base_style=s;s=child
  d.add_paragraph('Abstract II',style=s);d.add_paragraph('第1章 绪论1',style=s)
  if boundary:d.add_section(WD_SECTION.NEW_PAGE)
  d.add_heading('第1章 绪论',1)
  return d
 def rule(self,strategy='heading_text'):
  return {'front_matter_format':'roman_upper','body_format':'decimal','body_start_selector':{'strategy':strategy,'heading_text_pattern':r'^第\s*1\s*章'}}
 def test_existing_boundary_after_toc_unchanged(self):
  for derived in (False,True):
   d=self.fixture(derived=derived);before=d._element.xml
   self.assertFalse(ensure_page_number_sections(d,self.rule(),{}));self.assertEqual(before,d._element.xml)
   self.assertEqual(resolve_body_start_section(d,self.rule(),{}),1)
 def test_create_boundary_at_body_not_cached_toc(self):
  d=self.fixture(boundary=False)
  self.assertTrue(ensure_page_number_sections(d,self.rule(),{}))
  self.assertIsNone(d.paragraphs[0]._p.pPr.sectPr)
  self.assertIsNotNone(d.paragraphs[1]._p.pPr.sectPr)
 def test_unknown_duplicate_rejected_before_mutation(self):
  d=self.fixture(style='toc chapter');before=d._element.xml
  with self.assertRaises(ValueError):ensure_page_number_sections(d,self.rule(),{})
  self.assertEqual(before,d._element.xml)
 def test_duplicate_real_headings_rejected(self):
  d=self.fixture();d.add_heading('第1章 duplicate',1);before=d._element.xml
  with self.assertRaises(ValueError):ensure_page_number_sections(d,self.rule(),{})
  self.assertEqual(before,d._element.xml)
 def test_first_heading_mapped_toc_is_not_body(self):
  d=self.fixture();before=d._element.xml
  with self.assertRaises(ValueError):ensure_page_number_sections(d,self.rule('first_heading_1'),{'heading_1':{'style_name':'toc 1'}})
  self.assertEqual(before,d._element.xml)
