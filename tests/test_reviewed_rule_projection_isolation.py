"""Reviewed source rejection must not survive in the executable role cache."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import requirements_engine as engine


def clause(cid='C1', eid='E1'):
    return {'id': cid, 'text': '所列字段的格式须单独核验。', 'evidence_ids': [eid]}


def rule(rid='R1', cid='C1', eid='E1', role='abstract_title_zh', props=None):
    return {'id': rid, 'role': role, 'properties': props or {'font': {'size_pt': 14}},
            'clause_ids': [cid], 'evidence_ids': [eid], 'source_text': '所列字段的格式须单独核验。'}


def pending(cid='C1'):
    return {'clause_id': cid, 'classification': 'unsupported_backend',
            'reason': 'The source targets a field whose scope is unsupported.',
            'obligations': [{'id': cid+'-scope', 'status': 'unsupported_backend',
                             'reason': 'Do not apply this scoped rule as a shared title style.'}]}


def response(reviews=None, requirements=None):
    return {'contract_version': '3.0', 'requirements': requirements or [],
            'clause_reviews': reviews or [pending()], 'unsupported_items': [], 'reported_conflicts': []}


def execute(baseline, answer=None, clauses=None):
    return engine.merge_llm_primary(Path('source.docx'), baseline, clauses or [clause()],
                                    answer or response(), {'E1','E2'})


def baseline(rules=None, roles=None):
    return {'schema_version': '1.0', 'source_document': 'source.docx',
            'roles': roles if roles is not None else {'abstract_title_zh': {'font': {'size_pt': 14, 'italic': True}}},
            'requirements': rules if rules is not None else [rule()], 'status': 'resolved'}


class ReviewedRuleProjectionIsolationTests(unittest.TestCase):
    def test_removes_only_bound_rejected_leaf_not_unattributed_style(self):
        original = baseline(); saved = copy.deepcopy(original)
        spec, _, audit = execute(original)
        self.assertEqual(spec['roles']['abstract_title_zh'], {'font': {'italic': True}})
        self.assertEqual(original, saved)
        record = next(x for x in audit if x.get('type') == 'reviewed_rule_projection_isolation')
        self.assertEqual(record['changes'][0]['property'], 'font.size_pt')
        self.assertEqual(record['changes'][0]['source_requirement_ids'], ['R1'])
        self.assertEqual(spec['clause_compliance'][0]['status'], 'unsupported_backend')

    def test_unreviewed_same_property_contributor_survives(self):
        rules = [rule(), rule('R2', 'C2', 'E2')]
        spec, _, _ = execute(baseline(rules), clauses=[clause(), clause('C2','E2')])
        self.assertEqual(spec['roles']['abstract_title_zh']['font']['size_pt'],14)

    def test_distinct_surviving_value_is_restored_without_dropping_other_properties(self):
        rules = [rule(), rule('R2','C2','E2', props={'font': {'size_pt':16}})]
        spec, _, _ = execute(baseline(rules), clauses=[clause(),clause('C2','E2')])
        self.assertEqual(spec['roles']['abstract_title_zh']['font'], {'size_pt':16,'italic':True})

    def test_uncertain_evidence_binding_cannot_authorize_removal(self):
        spec, _, _ = execute(baseline([rule(eid='E2')]))
        self.assertEqual(spec['roles']['abstract_title_zh']['font']['size_pt'],14)

    def test_mixed_reviewed_and_unreviewed_source_requirement_is_preserved(self):
        mixed=rule();mixed['clause_ids']=['C1','C2'];mixed['evidence_ids']=['E1','E2']
        spec, _, _ = execute(baseline([mixed]), clauses=[clause(),clause('C2','E2')])
        self.assertEqual(spec['roles']['abstract_title_zh']['font']['size_pt'],14)

    def test_accepted_role_correction_does_not_retain_old_role_cache(self):
        answer=response([{'clause_id':'C1','classification':'executable','reason':'This source describes the footer.',
                          'obligations':[{'id':'footer-size','status':'covered','reason':'Footer font is represented.'}]}],
                        [{'role':'footer','properties':{'font':{'size_pt':14}},'clause_ids':['C1'],
                          'evidence_ids':['E1'],'reason':'Correct role for this source.','confidence':1.0}])
        spec, _, _ = execute(baseline(),answer)
        self.assertEqual(spec['roles']['abstract_title_zh'],{'font':{'italic':True}})
        self.assertEqual(spec['roles']['footer']['font']['size_pt'],14)

    def test_new_same_value_requirement_is_not_removed_with_old_rejected_source(self):
        answer=response([pending(), {'clause_id':'C2','classification':'executable','reason':'Independent exact title rule.',
                          'obligations':[{'id':'title-size','status':'covered','reason':'Title size is represented.'}]}],
                        [{'role':'abstract_title_zh','properties':{'font':{'size_pt':14}},'clause_ids':['C2'],
                          'evidence_ids':['E2'],'reason':'Independent title rule.','confidence':1.0}])
        spec,_,_=execute(baseline(),answer,[clause(),clause('C2','E2')])
        self.assertEqual(spec['roles']['abstract_title_zh']['font']['size_pt'],14)

    def test_rejected_replacement_cannot_remove_original_projection(self):
        answer=response([{'clause_id':'C1','classification':'executable','reason':'Proposed footer.',
                          'obligations':[{'id':'footer-size','status':'covered','reason':'Proposed font.'}]}],
                        [{'role':'footer','properties':{'font':{'size_pt':14}},'clause_ids':['C1'],
                          'evidence_ids':['UNKNOWN'],'reason':'Invalid source binding.','confidence':1.0}])
        spec,_,_=execute(baseline(),answer)
        self.assertEqual(spec['roles']['abstract_title_zh']['font']['size_pt'],14)

    def test_conflicting_survivors_are_not_arbitrarily_chosen(self):
        third=clause('C3','E3')
        rules=[rule(),rule('R2','C2','E2',props={'font':{'size_pt':16}}),
               rule('R3','C3','E3',props={'font':{'size_pt':18}})]
        spec,_,_=execute(baseline(rules),clauses=[clause(),clause('C2','E2'),third])
        self.assertEqual(spec['roles']['abstract_title_zh'],{'font':{'italic':True}})

    def test_explicit_shared_footer_overrides_only_its_baseline_properties(self):
        b=baseline(roles={'footer':{'font':{'size_pt':14,'italic':True},'paragraph':{'alignment':'justify'}}})
        answer=response([{'clause_id':'C1','classification':'executable','reason':'Footer shared style.',
                          'obligations':[{'id':'footer','status':'covered','reason':'Explicit footer.'}]}],
                        [{'role':'footer','properties':{'font':{'size_pt':7.5},'paragraph':{'alignment':'center'}},
                          'clause_ids':['C1'],'evidence_ids':['E1'],'reason':'Footer shared style.','confidence':1}])
        spec,_,_=execute(b,answer)
        self.assertEqual(spec['roles']['footer'],{'font':{'size_pt':7.5,'italic':True},'paragraph':{'alignment':'center'}})

    def test_conflicting_shared_footer_defaults_block_instead_of_last_wins(self):
        b=baseline(roles={'footer':{'font':{'size_pt':14}}})
        reviews=[{'clause_id':cid,'classification':'executable','reason':'Footer shared style.',
                  'obligations':[{'id':cid+'-font','status':'covered','reason':'Explicit footer.'}]}
                 for cid in ['C1','C2']]
        reqs=[{'role':'footer','properties':{'font':{'size_pt':size}},'clause_ids':[cid],
               'evidence_ids':[eid],'reason':'Footer shared style.','confidence':1}
              for cid,eid,size in [('C1','E1',7.5),('C2','E2',9)]]
        spec,conflicts,_=execute(b,response(reviews,reqs),[clause(),clause('C2','E2')])
        self.assertTrue(any(c.get('reason')=='conflicting_shared_content_defaults' for c in conflicts))
        self.assertTrue(spec.get('blocking_errors'))
        self.assertEqual(spec['roles']['footer']['font']['size_pt'],14)

    def test_literal_instance_does_not_protect_rejected_shared_role_cache(self):
        b=baseline()
        cs=[clause(),clause('C2','E2')];cs[1]['text']='Abstract'
        answer=response([pending(),{'clause_id':'C2','classification':'executable','reason':'Exact title instance.',
                          'obligations':[{'id':'title','status':'covered','reason':'Exact title.'}]}],
                        [{'role':'abstract_title_zh','properties':{'text':'Abstract','font':{'size_pt':16}},
                          'clause_ids':['C2'],'evidence_ids':['E2'],'reason':'Exact title instance.','confidence':1}])
        spec,_,_=execute(b,answer,cs)
        self.assertNotIn('size_pt',spec['roles']['abstract_title_zh'].get('font',{}))
        self.assertTrue(any(i.get('style_properties',{}).get('font',{}).get('size_pt')==16 for i in spec['content_instances']))

    def test_rejected_page_projection_is_not_kept_as_executable_default(self):
        b=baseline([rule(role='page',props={'page_number':{'front_matter_format':'roman'}})],{})
        b['page']={'page_number':{'front_matter_format':'roman'},'margins_pt':{'top':20}}
        spec,_,_=execute(b)
        self.assertEqual(spec['page'],{'margins_pt':{'top':20}})

if __name__=='__main__':unittest.main()
