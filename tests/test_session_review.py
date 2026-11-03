from __future__ import annotations
import importlib.util
import json
from pathlib import Path
import sys
import pytest
from docx import Document
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))


def api():
    p=ROOT/'scripts/session_review.py';assert p.is_file(),'Bound internal review is not implemented'
    spec=importlib.util.spec_from_file_location('review_target',p);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


@pytest.fixture
def review_fixture(tmp_path):
    m=api()
    from current_session import prepare_session
    from validation_budget import initialize_campaign
    from semantic_contract import sha256_file
    campaign=tmp_path/'campaign';initialize_campaign(campaign)
    req=tmp_path/'requirements.docx';src=tmp_path/'source.docx'
    for path,text in ((req,'正文使用宋体小四号。'),(src,'合成协议测试正文。')):
        doc=Document();doc.add_paragraph(text);doc.save(path)
    work=tmp_path/'work';prepare_session(req,src,work,campaign,author_task_id='author')
    # Protocol fixture only. Not a production candidate-generation claim.
    output=work/'fixture.docx';output.write_bytes(src.read_bytes())
    manifest=json.loads((work/'pipeline-manifest.json').read_text());manifest['output']=str(output);manifest['output_artifact']={'sha256':sha256_file(output)}
    (work/'pipeline-manifest.json').write_text(json.dumps(manifest))
    task=m.issue_review(work,'source_format')
    result='PASS: independently inspected the supplied synthetic artifacts.'
    observation={'schema_version':'thesis-internal-review/v1','task_id':task['task_id'],'role':task['role'],
                 'binding':task['binding'],'artifact_manifest_sha256':task['artifact_manifest_sha256'],
                 'reviewer_task_id':'independent-reviewer','verdict':'PASS','findings':[],
                 'read_artifacts':[{'path':x['path'],'sha256':x['sha256'],'method':'file_read'} for x in task['artifacts']],
                 'result_text':result,'source_kind':'reviewer_report'}
    observation['result_text']=json.dumps({key:value for key,value in observation.items() if key!='result_text'},sort_keys=True)
    return m,work,task,observation


def test_untrusted_import_never_self_certifies(review_fixture):
    m,work,task,obs=review_fixture
    out=m.import_review(work,task['task_id'],obs)
    assert out['status']=='pending_host_verification'
    assert out['provider_model_verified'] is False
    assert out['submission_ready'] is False
    assert out['cryptographically_verified'] is False


def test_distinct_real_tool_snapshot_is_scoped_and_fixture_cannot_be_promoted(review_fixture):
    m,work,task,obs=review_fixture
    snapshot={'agents':[{'agent_name':'independent-reviewer','agent_status':{'completed':obs['result_text']}}]}
    # Pure fixture evaluation exercises the actual matching algorithm without writing production receipts.
    assert m.evaluate_host_snapshot(task,obs,snapshot)['status']=='host_observed_scoped'
    m.import_review(work,task['task_id'],obs)
    out=m.record_host_observation(work,task['task_id'],snapshot,source_ref='fixture:tool-output',source_kind='fixture')
    assert out['status']=='pending_host_verification'
    assert not (work/'session-reviews'/task['task_id']/'host-observed.json').exists()


def test_author_identity_and_changed_candidate_are_rejected(review_fixture):
    m,work,task,obs=review_fixture
    obs['reviewer_task_id']='author'
    with pytest.raises(ValueError,match='author'):m.import_review(work,task['task_id'],obs)
    obs['reviewer_task_id']='independent-reviewer'
    (work/'fixture.docx').write_bytes(b'changed')
    with pytest.raises(ValueError,match='candidate|binding'):m.import_review(work,task['task_id'],obs)


def test_stale_task_snapshot_and_unknown_fields_are_rejected(review_fixture):
    m,work,task,obs=review_fixture
    obs['unexpected_release']=True
    with pytest.raises(ValueError):m.import_review(work,task['task_id'],obs)
    del obs['unexpected_release']
    (work/'session-reviews'/task['task_id']/task['artifacts'][0]['path']).write_bytes(b'changed')
    with pytest.raises(ValueError,match='artifact'):m.import_review(work,task['task_id'],obs)


def test_hold_is_not_pass_and_missing_read_is_unverifiable(review_fixture):
    m,work,task,obs=review_fixture
    obs['verdict']='HOLD';obs['findings']=[{'id':'heading-1','summary':'Requires author decision.'}]
    assert m.import_review(work,task['task_id'],obs)['status']=='hold'
    obs['verdict']='PASS';obs['findings']=[];obs['read_artifacts']=[]
    assert m.import_review(work,task['task_id'],obs)['status']=='hold'


def test_host_snapshot_requires_exact_actual_completed_result(review_fixture):
    m,work,task,obs=review_fixture
    forged={'agents':[{'agent_name':'independent-reviewer','agent_status':'running'}]}
    assert m.evaluate_host_snapshot(task,obs,forged)['status']=='pending_host_verification'
    forged['agents'][0]['agent_status']={'completed':'Different result'}
    assert m.evaluate_host_snapshot(task,obs,forged)['status']=='pending_host_verification'


def test_visual_issue_requires_bound_real_render_inventory(review_fixture):
    m,work,task,obs=review_fixture
    with pytest.raises(ValueError,match='render'):m.issue_review(work,'visual')


def test_missing_source_read_does_not_reach_host_acceptance(review_fixture):
    m,work,task,obs=review_fixture;obs['read_artifacts']=[]
    assert m.import_review(work,task['task_id'],obs)['status']=='pending_artifact_review'


def test_fixture_report_cannot_be_promoted_even_with_matching_names(review_fixture):
    m,work,task,obs=review_fixture;obs['source_kind']='fixture'
    snapshot={'agents':[{'agent_name':'independent-reviewer','agent_status':{'completed':obs['result_text']}}]}
    assert m.evaluate_host_snapshot(task,obs,snapshot)['status']=='pending_host_verification'


@pytest.mark.parametrize('field',['task_id','role','candidate'])
def test_old_completed_result_cannot_attest_new_bound_report(review_fixture,field):
    import copy
    m,work,task,obs=review_fixture
    old_result=obs['result_text']
    changed_task=copy.deepcopy(task);changed_obs=copy.deepcopy(obs)
    if field=='candidate':
        changed_task['binding']['candidate_sha256']='1'*64;changed_obs['binding']['candidate_sha256']='1'*64
    else:
        value='new-task' if field=='task_id' else 'visual'
        changed_task[field]=value;changed_obs[field]=value
    snapshot={'agents':[{'agent_name':obs['reviewer_task_id'],'agent_status':{'completed':old_result}}]}
    assert m.evaluate_host_snapshot(changed_task,changed_obs,snapshot)['status']!='host_observed_scoped'


def test_visual_inventory_cannot_omit_pdf_pages(review_fixture):
    m,work,task,obs=review_fixture
    import fitz
    from PIL import Image
    from semantic_contract import sha256_file
    from validation_budget import reserve_operation,record_operation
    from current_session import session_context
    render=work/'render';render.mkdir();pdf=render/'document.pdf'
    document=fitz.open();document.new_page();document.new_page();document.save(pdf);document.close()
    page=render/'page-0001.png';Image.new('RGB',(8,8),'white').save(page)
    campaign=Path(session_context(work)['config']['campaign']);candidate=task['binding']['run_id']
    reserve_operation(campaign,candidate,'candidate','fixture-candidate')
    reserve_operation(campaign,candidate,'full_render','fixture-render')
    second=render/'page-0002.png';Image.new('RGB',(8,8),'white').save(second)
    binding={'candidate_sha256':task['binding']['candidate_sha256'],'operation_id':'fixture-render',
             'pdf':{'path':'render/document.pdf','sha256':sha256_file(pdf)},
             'pages':[{'page':1,'path':'render/page-0001.png','sha256':sha256_file(page)},
                      {'page':2,'path':'render/page-0002.png','sha256':sha256_file(second)}]}
    record_operation(campaign,'fixture-render','completed',{'evidence_refs':['fixture:render'],'candidate_sha256':task['binding']['candidate_sha256'],'pdf_sha256':sha256_file(pdf),'page_inventory_sha256':m.render_inventory_sha256(work,binding)})
    import copy
    incomplete=copy.deepcopy(binding);incomplete['pages'].pop()
    (render/'render-binding.json').write_text(json.dumps(incomplete))
    with pytest.raises(ValueError,match='page'):m.issue_review(work,'visual')
    aliases=copy.deepcopy(binding);aliases['pages'][1]['path']='render/./page-0001.png'
    (render/'render-binding.json').write_text(json.dumps(aliases))
    with pytest.raises(ValueError,match='distinct'):m.issue_review(work,'visual')
    (render/'render-binding.json').write_text(json.dumps(binding))
    visual=m.issue_review(work,'visual')
    assert sum(item['kind']=='image' for item in visual['artifacts'])==2
    assert any(item['path']=='render/document.pdf' for item in visual['artifacts'])
    Image.new('RGB',(8,8),'black').save(second)
    binding['pages'][1]['sha256']=sha256_file(second)
    (render/'render-binding.json').write_text(json.dumps(binding))
    with pytest.raises(ValueError,match='inventory'):m.issue_review(work,'visual',renew=True)


def test_visual_inventory_has_canonical_frozen_page_identity(tmp_path):
    m=api()
    assert hasattr(m,'render_inventory_sha256'),'Completed render does not bind its complete page inventory'
    from PIL import Image
    from semantic_contract import sha256_file
    root=tmp_path/'run';(root/'render').mkdir(parents=True)
    page=root/'render/page.png';Image.new('RGB',(4,4),'white').save(page)
    pdf=root/'render/file.pdf';pdf.write_bytes(b'protocol fixture')
    binding={'candidate_sha256':'1'*64,'operation_id':'render-1','pdf':{'path':'render/file.pdf','sha256':sha256_file(pdf)},'pages':[{'page':1,'path':'render/page.png','sha256':sha256_file(page)},{'page':2,'path':'render/./page.png','sha256':sha256_file(page)}]}
    with pytest.raises(ValueError,match='distinct|unique'):m.render_inventory_sha256(root,binding)
