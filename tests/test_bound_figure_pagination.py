"""Explicit, hashed group bindings only. Never discover groups from adjacency."""
import copy,hashlib,io,sys,unittest
from pathlib import Path
from docx import Document
from docx.shared import Inches
from PIL import Image
from lxml import etree
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))

def fixture():
 d=Document();im=io.BytesIO();Image.new('RGB',(10,10),'blue').save(im,format='PNG')
 for letter in ('a','b'):
  d.add_paragraph().add_run().add_picture(io.BytesIO(im.getvalue()),width=Inches(1))
  d.add_paragraph(f'({letter}) subfigure')
 d.add_paragraph('Figure 1-2 Example');d.add_paragraph('Unrelated prose')
 return d

def plan(d):
 import bound_figure_pagination as m
 return {'protocol':'explicit_figure_group_pagination_v1','document_xml_sha256':m.xml_sha(d._element),
         'grouping_evidence':{'kind':'host_reviewed_source_relation','source_quote':'Figure 1-2 Example','caption_body_child':4},
         'members':[{'body_child':i,'kind':k,'xml_sha256':m.xml_sha(d._element.body[i]),**({'media_sha256':m.media_hashes(d,d._element.body[i])} if k=='image' else {})} for i,k in enumerate(['image','subcaption','image','subcaption','caption'])]}
class BoundFigureTests(unittest.TestCase):
 def test_only_bound_chain_gets_keep_properties(self):
  import bound_figure_pagination as m
  d=fixture();before=m.xml_sha(d._element.body[5]);result=m.apply_bound_group(d,plan(d))
  self.assertEqual(result['status'],'configured_not_render_verified')
  self.assertFalse(result['submission_ready'])
  self.assertTrue(all(p.paragraph_format.keep_with_next is True for p in d.paragraphs[:4]))
  self.assertIs(d.paragraphs[4].paragraph_format.keep_with_next,False)
  self.assertEqual(before,m.xml_sha(d._element.body[5]))
 def test_unbound_wrong_hash_and_wrong_relation_are_atomic(self):
  import bound_figure_pagination as m
  for change in ['document','member','relation','gap','kind']:
   d=fixture();p=plan(d);before=m.xml_sha(d._element)
   if change=='document':p['document_xml_sha256']='f'*64
   if change=='member':p['members'][0]['xml_sha256']='f'*64
   if change=='relation':p['grouping_evidence']['source_quote']='Not this group'
   if change=='gap':p['members'][2]['body_child']=5
   if change=='kind':p['members'][1]['kind']='image'
   with self.assertRaises(ValueError):m.apply_bound_group(d,p)
   self.assertEqual(before,m.xml_sha(d._element))
 def test_no_plan_never_guesses_group(self):
  import bound_figure_pagination as m
  d=fixture();before=m.xml_sha(d._element)
  with self.assertRaises(ValueError):m.apply_bound_group(d,{})
  self.assertEqual(before,m.xml_sha(d._element))
 def test_rejects_section_break_fields_and_mixed_prose_atomically(self):
  import bound_figure_pagination as m
  from docx.oxml import OxmlElement
  from docx.oxml.ns import qn
  for defect in ('section','page','column','field','mixed_prose','caption_image'):
   d=fixture();node=d._element.body[2]
   if defect=='section':node.get_or_add_pPr().append(OxmlElement('w:sectPr'))
   elif defect in ('page','column'):
    br=OxmlElement('w:br');br.set(qn('w:type'),defect);node.append(br)
   elif defect=='field':node.append(OxmlElement('w:fldChar'))
   elif defect=='mixed_prose':d.paragraphs[2].add_run('intervening prose')
   else:d._element.body[1].append(copy.deepcopy(node.find('.//'+qn('w:drawing'))))
   p=plan(d);before=m.xml_sha(d._element)
   with self.subTest(defect=defect),self.assertRaises(ValueError):m.apply_bound_group(d,p)
   self.assertEqual(before,m.xml_sha(d._element))
 def test_rejects_stale_media_even_when_xml_matches(self):
  import bound_figure_pagination as m
  d=fixture();p=plan(d);rid=next(iter(p['members'][0]['media_sha256']))
  p['members'][0]['media_sha256'][rid]='0'*64
  before=m.xml_sha(d._element)
  with self.assertRaises(ValueError):m.apply_bound_group(d,p)
  self.assertEqual(before,m.xml_sha(d._element))
 def test_preserves_content_and_relationships_after_save(self):
  import bound_figure_pagination as m
  d=fixture();texts=[p.text for p in d.paragraphs];media=m.media_hashes(d,d._element.body[0]);m.apply_bound_group(d,plan(d))
  buf=io.BytesIO();d.save(buf);buf.seek(0);new=Document(buf)
  self.assertEqual(texts,[p.text for p in new.paragraphs])
  self.assertEqual(media,m.media_hashes(new,new._element.body[0]))
  self.assertTrue(new.paragraphs[0].paragraph_format.keep_with_next)
 def test_inherited_default_and_cyclic_style_boundaries(self):
  import bound_figure_pagination as m
  from docx.enum.style import WD_STYLE_TYPE
  from docx.oxml import OxmlElement
  from docx.oxml.ns import qn
  for case in ('base','default','cycle','missing'):
   d=fixture()
   if case=='default':d.styles['Normal'].paragraph_format.page_break_before=True
   else:
    a=d.styles.add_style('BoundA',WD_STYLE_TYPE.PARAGRAPH);b=d.styles.add_style('BoundB',WD_STYLE_TYPE.PARAGRAPH)
    a.base_style=b;d.paragraphs[2].style=a
    if case=='base':b.paragraph_format.page_break_before=True;d.paragraphs[2].paragraph_format.page_break_before=False
    elif case=='cycle':b.base_style=a
    else:a.element.find(qn('w:basedOn')).set(qn('w:val'),'MissingStyle')
   p=plan(d);before=m.xml_sha(d._element)
   with self.subTest(case=case),self.assertRaises(ValueError):m.apply_bound_group(d,p)
   self.assertEqual(before,m.xml_sha(d._element))
 def test_cli_publication_preserves_unrelated_zip_members(self):
  import bound_figure_pagination as m
  import tempfile,json
  from zipfile import ZipFile
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);src=root/'source.docx';fixture().save(src);d=Document(src);p=plan(d);p['input_docx_sha256']=hashlib.sha256(src.read_bytes()).hexdigest()
   spec=root/'plan.json';spec.write_text(json.dumps(p));out=root/'out.docx';report=root/'report.json'
   result=m.write_bound_copy(src,spec,out,report)
   self.assertFalse(result['visual_verified']);self.assertEqual(result,json.loads(report.read_text()))
   with ZipFile(src) as a,ZipFile(out) as b:
    self.assertEqual(a.namelist(),b.namelist())
    self.assertEqual([n for n in a.namelist() if a.read(n)!=b.read(n)],['word/document.xml'])
   before=out.read_bytes()
   with self.assertRaises(ValueError):m.write_bound_copy(src,spec,out,report)
   self.assertEqual(before,out.read_bytes())
 def test_second_publication_failure_rolls_back_without_overwrite(self):
  import bound_figure_pagination as m
  import tempfile,json,os
  from unittest.mock import patch
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);src=root/'source.docx';fixture().save(src);p=plan(Document(src));p['input_docx_sha256']=hashlib.sha256(src.read_bytes()).hexdigest()
   spec=root/'plan.json';spec.write_text(json.dumps(p));out=root/'out.docx';report=root/'report.json';real_link=os.link
   def raced_link(a,b):
    if Path(b)==report:
     report.write_text('concurrent owner');raise FileExistsError('report claimed')
    return real_link(a,b)
   with patch.object(m.os,'link',side_effect=raced_link),self.assertRaises(FileExistsError):m.write_bound_copy(src,spec,out,report)
   self.assertFalse(out.exists());self.assertEqual(report.read_text(),'concurrent owner')
   self.assertFalse([x for x in root.iterdir() if x.name.startswith('.')])
 def test_simple_fields_and_all_default_style_lexemes_are_rejected(self):
  import bound_figure_pagination as m
  from docx.oxml import OxmlElement
  from docx.oxml.ns import qn
  from docx.enum.style import WD_STYLE_TYPE
  for case in ('simple_field','true','on','multiple','missing'):
   d=fixture()
   if case=='simple_field':
    field=OxmlElement('w:fldSimple');field.set(qn('w:instr'),'REF hidden_target');d._element.body[4].append(field)
   elif case in ('true','on'):
    d.styles['Normal'].element.set(qn('w:default'),case);d.styles['Normal'].paragraph_format.page_break_before=True
   elif case=='multiple':
    st=d.styles.add_style('OtherDefault',WD_STYLE_TYPE.PARAGRAPH);st.element.set(qn('w:default'),'1');st.paragraph_format.page_break_before=True
   else:del d.styles['Normal'].element.attrib[qn('w:default')]
   p=plan(d);before=m.xml_sha(d._element)
   with self.subTest(case=case),self.assertRaises(ValueError):m.apply_bound_group(d,p)
   self.assertEqual(before,m.xml_sha(d._element))
if __name__=='__main__':unittest.main()
