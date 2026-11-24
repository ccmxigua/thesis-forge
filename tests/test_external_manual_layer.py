import sys,unittest,importlib
from pathlib import Path
from lxml import etree
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
NS='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
def tree(texts):
 r=etree.Element('{'+NS+'}document',nsmap={'w':NS});b=etree.SubElement(r,'{'+NS+'}body')
 for text in texts:
  p=etree.SubElement(b,'{'+NS+'}p');run=etree.SubElement(p,'{'+NS+'}r')
  if text is None:etree.SubElement(run,'{'+NS+'}br').set('{'+NS+'}type','page')
  else:etree.SubElement(run,'{'+NS+'}t').text=text
 return r
class ExternalLayerTests(unittest.TestCase):
 def setup_case(self):
  m=importlib.import_module('external_manual_layer')
  ids={'MR-0001':'【MR-0001｜人工待审】详见同编号独立审查台账。'}
  r=tree([m.TITLE,m.introduction(1),ids['MR-0001'],None,'Original thesis text'])
  return m,ids,r
 def test_exact_layer_removed_and_body_preserved(self):
  m,ids,r=self.setup_case();out=m.remove_manual_layer(r,ids)
  self.assertEqual(out['removed_marker_ids'],['MR-0001'])
  self.assertEqual(r.xpath('//w:t/text()',namespaces={'w':NS}),['Original thesis text'])
 def test_wrong_intro_rejected_without_mutation(self):
  m,ids,r=self.setup_case();r.xpath('//w:t',namespaces={'w':NS})[1].text='Different instructions'
  before=etree.tostring(r)
  with self.assertRaises(ValueError):m.remove_manual_layer(r,ids)
  self.assertEqual(before,etree.tostring(r))
 def test_missing_marker_rejected(self):
  m,ids,r=self.setup_case();ids['MR-0002']='missing'
  with self.assertRaises(ValueError):m.remove_manual_layer(r,ids)
 def test_marker_with_section_boundary_rejected(self):
  m,ids,r=self.setup_case();p=r[0][2];pr=etree.SubElement(p,'{'+NS+'}pPr');etree.SubElement(pr,'{'+NS+'}sectPr')
  with self.assertRaises(ValueError):m.remove_manual_layer(r,ids)
 def test_nested_marker_is_not_removed_from_container(self):
  m,ids,r=self.setup_case();node=r[0][2];r[0].remove(node)
  # No unlocated marker means the exact generated empty-front declaration.
  empty=tree([m.EMPTY])[0][0];r[0].insert(2,empty)
  cell=etree.SubElement(r[0],'{'+NS+'}tc');cell.append(node)
  with self.assertRaises(ValueError):m.remove_manual_layer(r,ids)
 def test_missing_generated_break_rejected(self):
  m,ids,r=self.setup_case();r[0].remove(r[0][3])
  with self.assertRaises(ValueError):m.remove_manual_layer(r,ids)

class ExportLayerTests(unittest.TestCase):
 def test_real_strip_and_receipt_reject_tampering(self):
  import tempfile,json,copy
  from docx import Document
  from draft_scorecard import build_scorecard,scorecard_lines
  from manual_review_display import append_manual_review_markers
  from review_package_export import strip_scorecard_display,expected_paper_marker_ids,digest
  from semantic_contract import sha256_json
  with tempfile.TemporaryDirectory() as raw:
   root=Path(raw);source=root/'source.docx';d=Document();d.add_paragraph('Thesis stays intact.');d.save(source)
   item={'marker_id':'MR-0001','manual_obligation_id':'M1','category':'input_prerequisite','reason':'Verify source','action':'Inspect source'}
   binding={'run_id':'synthetic','case_id':'case','input_source_sha256':digest(source),'source_sha256':digest(source),'format_spec_sha256':'b'*64}
   card=build_scorecard(binding,{'receipts':[],'expected_receipt_ids':[],'missing_count':0,'unexpected_count':0,'duplicate_count':0},[item],[])
   # Construct a real generated marker layer and exact scorecard display.
   from manual_review_display import _text
   from external_manual_layer import TITLE,introduction
   from docx.enum.text import WD_BREAK
   d=Document();d.add_paragraph(TITLE);d.add_paragraph(introduction(1));d.add_paragraph(_text(item))
   d.add_paragraph().add_run().add_break(WD_BREAK.PAGE);d.add_paragraph('Thesis stays intact.')
   for line in reversed(scorecard_lines(card)):
    p=d.paragraphs[0].insert_paragraph_before(line)
   baseline=root/'baseline.docx';d.save(baseline);c=root/'card.json';c.write_text(json.dumps(card))
   out=root/'paper.docx';report=root/'split.json'
   split=strip_scorecard_display(source,baseline,c,out,report,externalize_manual=True)
   self.assertEqual([p.text for p in Document(out).paragraphs],['Thesis stays intact.'])
   self.assertEqual(expected_paper_marker_ids(card,out,split),[])
   self.assertEqual(expected_paper_marker_ids(card,out,None),['MR-0001'])
   original_bytes=out.read_bytes()
   # Updating only envelope hashes must not launder changed paper content.
   for change in ('body','format','table'):
    edited=Document(out)
    if change=='body':edited.paragraphs[0].text='Tampered thesis text.'
    elif change=='format':edited.paragraphs[0].runs[0].bold=True
    else:edited.add_table(rows=1,cols=1).cell(0,0).text='Unexpected table'
    edited.save(out)
    bad=copy.deepcopy(split);bad['output_docx']['sha256']=digest(out)
    bad['audit_sha256']=sha256_json({k:v for k,v in bad.items() if k!='audit_sha256'})
    with self.assertRaises(ValueError):expected_paper_marker_ids(card,out,bad)
    out.write_bytes(original_bytes)
   for field,value in [('externalized_manual_marker_ids',[]),('manual_review_marker_projection_sha256','f'*64),('output_docx',{'sha256':'f'*64})]:
    bad=copy.deepcopy(split);bad[field]=value;bad['audit_sha256']=sha256_json({k:v for k,v in bad.items() if k!='audit_sha256'})
    with self.assertRaises(ValueError):expected_paper_marker_ids(card,out,bad)

if __name__=="__main__":unittest.main()
