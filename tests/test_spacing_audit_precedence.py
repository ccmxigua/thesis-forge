"""Written line-unit spacing must not reintroduce a template-only point rule."""
import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from post_generation_format_audit import compare_role
class SpacingAuditTests(unittest.TestCase):
 def rows(self,rule,official,generated):
  return compare_role('figure_caption',{'paragraph':rule},{'paragraph':official},{'paragraph':generated})
 def test_explicit_lines_do_not_reintroduce_template_points(self):
  for side in ('before','after'):
   for value in (0,1.5):
    rows=self.rows({f'space_{side}_lines':value},{f'space_{side}_pt':12},{f'space_{side}_pt':0,f'space_{side}_lines':value})
    self.assertNotIn(f'paragraph.space_{side}_pt',[r['property'] for r in rows])
    self.assertFalse(any(r['blocking'] for r in rows))
 def test_missing_line_value_is_not_inferred_from_zero_points(self):
  rows=self.rows({'space_before_lines':0},{'space_before_pt':12},{'space_before_pt':0})
  self.assertEqual(rows[0]['status'],'not_observable');self.assertTrue(rows[0]['blocking'])
 def test_wrong_lines_still_fail(self):
  rows=self.rows({'space_before_lines':1},{'space_before_pt':12},{'space_before_lines':0,'space_before_pt':12})
  self.assertTrue(any(r['status']=='fail' and r['blocking'] for r in rows))
 def test_template_points_remain_without_written_line_rule(self):
  rows=self.rows({}, {'space_before_pt':12},{'space_before_pt':0})
  self.assertEqual(rows[0]['status'],'fail')
 def test_explicit_null_lines_does_not_suppress_points(self):
  rows=self.rows({'space_before_lines':None},{'space_before_pt':12},{'space_before_pt':0})
  self.assertTrue(any(r['property']=='paragraph.space_before_pt' and r['blocking'] for r in rows))
 def test_official_line_conflict_still_preserved(self):
  rows=self.rows({'space_before_lines':0},{'space_before_lines':1,'space_before_pt':12},{'space_before_lines':0,'space_before_pt':0})
  self.assertEqual(rows[0]['status'],'conflict');self.assertFalse(rows[0]['blocking'])
