"""Unresolved source rules cannot authorize stale singleton role formatting."""
import copy,tempfile,unittest
from pathlib import Path
from docx import Document
from docx.shared import Pt
import test_reconstructed_property_authority as fixture
from requirements_engine import _isolate_reviewed_rule_projections

class UnresolvedRoleProjectionTests(unittest.TestCase):
 def isolate(self, classification='unresolved', accepted_font=None, bad_binding=False, surviving=False):
  props={'font':{'size_pt':14,'cjk':'SimSun','bold':True},'paragraph':{'line_spacing':{'type':'single','value':1,'unit':'multiple'}}}
  rule={'roles':{'abstract_title_zh':copy.deepcopy(props)},'requirements':[{'id':'R-stale','role':'abstract_title_zh','properties':props,'clause_ids':['C1'],'evidence_ids':['E1']}]}
  if bad_binding:rule['requirements'][0]['evidence_ids']=['UNRELATED']
  if surviving:
   other=copy.deepcopy(rule['requirements'][0]);other['id']='R-survives';other['clause_ids']=['C3'];other['evidence_ids']=['E3'];rule['requirements'].append(other)
  accepted={'paragraph':{'page_break_before':True}}
  if accepted_font:accepted['font']={'size_pt':accepted_font}
  spec={'roles':{'abstract_title_zh':copy.deepcopy(props)},'requirements':[{'role':'abstract_title_zh','properties':accepted,'clause_ids':['C2'],'evidence_ids':['E2']}]}
  if accepted_font:spec['roles']['abstract_title_zh']['font']['size_pt']=accepted_font
  spec['roles']['abstract_title_zh']['paragraph']['page_break_before']=True
  reviews={} if classification is None else {'C1':{'classification':classification,'requirement_indexes':[]}}
  changes=_isolate_reviewed_rule_projections(spec,rule,{'C1':{'evidence_ids':['E1']},'C2':{'evidence_ids':['E2']},'C3':{'evidence_ids':['E3']}},reviews,{0},{})
  return spec,changes
 def test_unresolved_stale_values_removed_before_application(self):
  spec,changes=self.isolate();self.assertNotIn('font',spec['roles']['abstract_title_zh']);self.assertTrue(changes)
  d=Document();p=d.add_paragraph('摘 要');fixture.font(p.runs[0],size=16,cjk='黑体',italic=False);p.paragraph_format.line_spacing=Pt(20)
  with tempfile.TemporaryDirectory() as tmp:
   out,_,_=fixture.PropertyAuthorityTests().execute(Path(tmp),d,spec['roles'],{'abstract_title_zh':'Normal'})
   p=next(p for p in out.paragraphs if p.text=='摘 要');self.assertEqual(p.runs[0].font.size,Pt(16));self.assertEqual(p.paragraph_format.line_spacing,Pt(20));self.assertTrue(p.paragraph_format.page_break_before)
 def test_missing_review_not_reclassified(self):
  spec,changes=self.isolate(None);self.assertEqual(spec['roles']['abstract_title_zh']['font']['size_pt'],14);self.assertEqual(changes,[])
 def test_accepted_replacement_keeps_own_leaf(self):
  spec,changes=self.isolate(accepted_font=18);self.assertEqual(spec['roles']['abstract_title_zh']['font'],{'size_pt':18});self.assertTrue(changes)

 def test_unbound_stale_claim_cannot_authorize_removal(self):
  spec,changes=self.isolate(bad_binding=True);self.assertEqual(spec['roles']['abstract_title_zh']['font']['size_pt'],14);self.assertEqual(changes,[])
 def test_surviving_unreviewed_source_value_retained(self):
  spec,changes=self.isolate(surviving=True);self.assertEqual(spec['roles']['abstract_title_zh']['font']['size_pt'],14);self.assertEqual(changes,[])
