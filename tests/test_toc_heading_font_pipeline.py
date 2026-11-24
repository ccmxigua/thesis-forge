"""Synthetic CLI test of bound heading face compaction, never native acceptance."""
import json
from pathlib import Path
import tempfile
import unittest
from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from tests import toc_pipeline_fixture as fixture
from font_declarations import run_cjk_face

def field(p, instruction, cached, locked=False):
    begin=OxmlElement('w:fldChar');begin.set(qn('w:fldCharType'),'begin')
    if locked:begin.set(qn('w:fldLock'),'true')
    p.add_run()._r.append(begin)
    node=OxmlElement('w:instrText');node.text=instruction;p.add_run()._r.append(node)
    sep=OxmlElement('w:fldChar');sep.set(qn('w:fldCharType'),'separate');p.add_run()._r.append(sep)
    p.add_run(cached)
    end=OxmlElement('w:fldChar');end.set(qn('w:fldCharType'),'end');p.add_run()._r.append(end)



class TocHeadingFontPipelineTests(unittest.TestCase):
 def execute(self, root, *, mixed=False, locked=False, charstyle=False, broken_bookmark=False, theme=False, duplicate_bookmark=False, reversed_bookmark=False, cross_paragraph=False, tamper_scorecard=False):
  d=Document();hs=d.styles['Heading 1'];hs.element.get_or_add_rPr().get_or_add_rFonts().set(qn('w:eastAsia'),'SimHei')
  hs.element.get_or_add_rPr().get_or_add_rFonts().attrib.pop(qn('w:eastAsiaTheme'),None)
  h=d.add_paragraph('第一章 合成',style=hs)
  h.runs[0]._r.get_or_add_rPr().get_or_add_rFonts().set(qn('w:eastAsia'),'SimHei')
  if mixed:h.add_run('异体')._r.get_or_add_rPr().get_or_add_rFonts().set(qn('w:eastAsia'),'KaiTi')
  if charstyle:
   cs=d.styles.add_style('Explicit Chinese Character',WD_STYLE_TYPE.CHARACTER)
   cs.element.get_or_add_rPr().get_or_add_rFonts().set(qn('w:eastAsia'),'SimHei');h.runs[0].style=cs
  b=OxmlElement('w:bookmarkStart');b.set(qn('w:id'),'42');b.set(qn('w:name'),'_TocSynthetic');h._p.insert(1,b)
  if not broken_bookmark:
   e=OxmlElement('w:bookmarkEnd');e.set(qn('w:id'),'42');h._p.append(e)
  if reversed_bookmark:h._p.insert(0,e)
  if cross_paragraph:d.add_paragraph()._p.append(e)
  if theme:h.runs[0]._r.get_or_add_rPr().get_or_add_rFonts().set(qn('w:eastAsiaTheme'),'majorEastAsia')
  if duplicate_bookmark:
   b2=OxmlElement('w:bookmarkStart');b2.set(qn('w:id'),'42');b2.set(qn('w:name'),'OtherBookmark');h._p.append(b2)
  st=d.styles.add_style('toc 1',WD_STYLE_TYPE.PARAGRAPH);st.base_style=d.styles['Normal']
  toc=d.add_paragraph(style=st)
  for kind,value in [('begin',None),(None,' TOC \\o "1-1" '),('separate',None)]:
   n=OxmlElement('w:fldChar' if kind else 'w:instrText')
   if kind:n.set(qn('w:fldCharType'),kind)
   else:n.text=value
   if kind=='begin'and locked:n.set(qn('w:fldLock'),'true')
   toc.add_run()._r.append(n)
  toc.add_run(h.text+'\t');field(toc,' PAGEREF _TocSynthetic \\h ','7')
  n=OxmlElement('w:fldChar');n.set(qn('w:fldCharType'),'end');toc.add_run()._r.append(n)
  template=Document();p=template.add_paragraph('目录');template.add_comment(p.runs,text='一级目录项：中文字体为宋体，西文字体为Times New Roman，四号，1.5倍行距。',author='Fixture')
  source=root/'template.docx';template.save(source)
  d,report,a=fixture.PropertyAuthorityTests().execute(root,d,{'body_text':{'font':{'size_pt':12}}},{'body_text':'Normal'},source,tamper_scorecard=tamper_scorecard)
  return d,a
 def test_redundant_bound_heading_face_is_compacted_without_changing_effective_font(self):
  with tempfile.TemporaryDirectory()as tmp:
   d,a=self.execute(Path(tmp));h=next(p for p in d.paragraphs if p.text=='第一章 合成')
   self.assertIsNone(h.runs[0]._r.get_or_add_rPr().get_or_add_rFonts().get(qn('w:eastAsia')))
   self.assertEqual(run_cjk_face(h.runs[0],h,d),'SimHei')
   self.assertTrue(any('7' in p.text and p.style.name=='toc 1'for p in d.paragraphs))
   r=json.loads((a/'toc-heading-fonts.json').read_text());self.assertEqual(len(r['changes']),1)
   self.assertTrue(r['serialized_audit']['valid']);self.assertFalse(r['native_font_retention_verified'])
 def test_unsafe_or_intentional_source_format_is_preserved(self):
  for option in ('mixed','locked','charstyle','broken_bookmark','theme','duplicate_bookmark','reversed_bookmark','cross_paragraph'):
   with self.subTest(option=option),tempfile.TemporaryDirectory()as tmp:
    d,a=self.execute(Path(tmp),**{option:True});h=next(p for p in d.paragraphs if p.text.startswith('第一章'))
    self.assertEqual(h.runs[0]._r.get_or_add_rPr().get_or_add_rFonts().get(qn('w:eastAsia')),'SimHei')
    r=json.loads((a/'toc-heading-fonts.json').read_text());self.assertEqual(r['changes'],[])

 def test_ownership_snapshot_rejects_ambiguous_and_unknown_sources(self):
  from types import SimpleNamespace
  from toc_heading_fonts import bound_heading_plan
  d=Document();a=d.add_paragraph('one');b=d.add_paragraph('two');c=d.add_paragraph('three')
  bindings=SimpleNamespace(roles_by_element={a._p:frozenset({'heading_1'}),b._p:frozenset({'heading_1','body_text'}),c._p:frozenset({'unknown'})})
  plan=bound_heading_plan(d,bindings)
  self.assertEqual(plan.owners,{a._p:'heading_1'})

 def test_receipts_bind_final_review_draft_bytes(self):
  import hashlib
  from toc_heading_fonts import audit_toc_heading_fonts
  from toc_style_contract import audit_toc_style_rules
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);d,a=self.execute(root)
   final_sha=hashlib.sha256((root/'synthetic-output.docx').read_bytes()).hexdigest()
   from source_role_binding import build_source_role_bindings
   initial=build_source_role_bindings(Document(root/'synthetic-source.docx'), explicit_mappings={'body_text':'Normal'})
   early=json.loads((a/'toc-source-role-bindings.json').read_text())
   self.assertEqual(early,{'schema_version':'1.0','records':initial.records,'findings':initial.findings})
   for name,audit in [('toc-heading-fonts.json',audit_toc_heading_fonts),('toc-style-application.json',audit_toc_style_rules)]:
    receipt=json.loads((a/name).read_text())
    self.assertEqual(receipt['serialized_docx_sha256'],final_sha)
    self.assertEqual(receipt['serialized_audit'],audit(d,receipt))

 def test_late_scorecard_heading_mutation_is_rejected(self):
  with tempfile.TemporaryDirectory() as tmp:
   with self.assertRaisesRegex(AssertionError,'final review draft TOC formatting differs; refusing commit'):
    self.execute(Path(tmp),tamper_scorecard=True)


 def test_early_toc_snapshot_excludes_later_scope_preparation(self):
  import subprocess
  import sys
  from source_role_binding import build_source_role_bindings
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp); doc=Document()
   doc.add_paragraph('第一章 合成','Heading 1')
   doc.add_paragraph('Synthetic original body.')
   args,source,_,_,audits=fixture.review_args(root,doc,{'body_text':{'font':{'size_pt':12}}},{'body_text':'Normal'})
   expected=build_source_role_bindings(Document(source),explicit_mappings={'body_text':'Normal'})
   # Inject one synthetic paragraph at the existing header preparation boundary
   # while still exercising the real formatter, serialization, and receipts.
   harness="""
import sys
sys.path.insert(0,sys.argv.pop(1))
import apply_format_spec as app
original=app.prepare_header_scope_boundaries
def prepare(doc,spec):
    doc.add_paragraph('Synthetic extra preparation heading','Heading 2')
    return original(doc,spec)
app.prepare_header_scope_boundaries=prepare
raise SystemExit(app.main(sys.argv[1:]))
"""
   run=subprocess.run([sys.executable,'-B','-c',harness,str(fixture.SCRIPT_ROOT/'scripts'),*args[3:]],
                      capture_output=True,text=True,timeout=90)
   self.assertEqual(run.returncode,0,run.stderr+run.stdout[-6000:])
   early=json.loads((audits/'toc-source-role-bindings.json').read_text())
   later=json.loads((audits/'source-role-bindings.json').read_text())
   self.assertEqual(early,{'schema_version':'1.0','records':expected.records,'findings':expected.findings})
   self.assertEqual(len(later['records']),len(early['records'])+1)
   self.assertFalse(any('heading_2' in row['roles'] for row in early['records']))
   self.assertTrue(any('heading_2' in row['roles'] for row in later['records']))
