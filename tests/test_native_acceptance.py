from __future__ import annotations
import copy
import importlib.util
from pathlib import Path
import sys
import pytest
from tests.test_session_review import review_fixture
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))


def api():
    path=ROOT/'scripts/native_acceptance.py';assert path.is_file(),'Scoped native evidence is not implemented'
    spec=importlib.util.spec_from_file_location('native_target',path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


@pytest.fixture
def native_fixture(review_fixture):
    m=api();_,work,task,_=review_fixture
    from validation_budget import reserve_operation,record_operation
    from current_session import session_context
    campaign=Path(session_context(work)['config']['campaign']);run=task['binding']['run_id']
    reserve_operation(campaign,run,'candidate','fixture-create');record_operation(campaign,'fixture-create','completed',{'evidence_refs':['fixture:synthetic-candidate']})
    reserve_operation(campaign,run,'wps_acceptance','native-check')
    url='https://www.kdocs.cn/l/synthetic-test-object'
    observation={'schema_version':'thesis-native-acceptance/v1','observation_id':'obs-1','operation_id':'native-check','app':'wps',
                 'input_binding':task['binding'],'native_object_url':url,'native_version':None,
                 'observed_at':'2026-11-03T00:00:00Z','upload_status':'confirmed',
                 'checks':[{'name':name,'outcome':'pass','evidence_refs':['fixture:screenshot-'+name],'scope':'Synthetic protocol test only.'} for name in ['toc_links','page_number_sequence','heading_keep_next','embedded_objects','font_sample']],
                 'save_close_reopen':{'saved':True,'closed':True,'reopened':True,'reopened_url':url,'reopened_version':None},
                 'evidence_refs':['fixture:save-reopen'],'edited_online':False,'exported_bytes_binding':None}
    return m,work,campaign,observation


def test_wps_scope_is_not_word_release_and_no_export_claim(native_fixture):
    m,work,campaign,obs=native_fixture
    # Fixture is admitted only to pure evaluation; persisted production evaluation is separately scoped.
    result=m.evaluate_observation(obs)
    assert result['native_checks']['wps']['status']=='pass_scoped'
    assert result['word_release']['status']=='not_verified'
    assert result['exported_bytes_binding'] is None
    assert result['submission_ready'] is False


def test_unknown_upload_is_not_a_retry_permission(native_fixture):
    m,work,campaign,obs=native_fixture
    obs['upload_status']='unknown';obs['native_object_url']=None
    obs['save_close_reopen']={'saved':False,'closed':False,'reopened':False,'reopened_url':None,'reopened_version':None}
    result=m.record_native_observation(work,obs)
    assert result['native_checks']['wps']['status']=='pending_unknown_upload'
    assert result['next_action']=='locate_existing_object'
    from validation_budget import reserve_operation,read_budget
    assert not reserve_operation(campaign,obs['input_binding']['run_id'],'wps_acceptance','native-check')['can_execute']
    assert read_budget(campaign)['operations']['native-check']['outcome']=='unknown'


def test_missing_reopen_cannot_pass_and_replay_does_not_charge_again(native_fixture):
    m,work,campaign,obs=native_fixture;obs['save_close_reopen']['reopened']=False
    result=m.record_native_observation(work,obs)
    assert result['native_checks']['wps']['status']=='pending_save_reopen'
    from validation_budget import read_budget
    before=read_budget(campaign)
    assert m.record_native_observation(work,copy.deepcopy(obs))==result
    assert read_budget(campaign)==before


def test_url_or_version_drift_requires_relocation(native_fixture):
    m,work,campaign,obs=native_fixture
    obs['save_close_reopen']['reopened_url']='https://www.kdocs.cn/l/different'
    assert m.evaluate_observation(obs)['native_checks']['wps']['status']=='identity_mismatch'
    obs['save_close_reopen']['reopened_url']=obs['native_object_url'];obs['native_version']='v2';obs['save_close_reopen']['reopened_version']='v1'
    assert m.evaluate_observation(obs)['native_checks']['wps']['status']=='identity_mismatch'


def test_export_hash_cannot_be_claimed_without_downloaded_bytes(native_fixture):
    m,work,campaign,obs=native_fixture
    obs['edited_online']=True
    obs['exported_bytes_binding']={'path':'does-not-exist.docx','sha256':obs['input_binding']['candidate_sha256'],
                                 'native_object_url':obs['native_object_url'],'native_version':None,'evidence_ref':'fixture:download'}
    with pytest.raises(ValueError,match='export'):m.record_native_observation(work,obs)


def test_observation_identity_and_unknown_fields_are_strict(native_fixture):
    m,work,campaign,obs=native_fixture
    obs['magic_word_release']=True
    with pytest.raises(ValueError):m.record_native_observation(work,obs)
    del obs['magic_word_release'];obs['input_binding']['candidate_sha256']='0'*64
    with pytest.raises(ValueError,match='binding'):m.record_native_observation(work,obs)


def test_fixture_native_evidence_cannot_create_production_acceptance(native_fixture):
    m,work,campaign,obs=native_fixture
    assert m.record_native_observation(work,obs)['native_checks']['wps']['status']=='pending_host_verification'
    from validation_budget import read_budget
    assert read_budget(campaign)['operations']['native-check']['outcome']=='reserved'


def test_terminal_acceptance_cannot_be_changed_by_a_new_observation(native_fixture):
    m,work,campaign,obs=native_fixture
    for check in obs['checks']:check['evidence_refs']=['conversation:observed-check']
    obs['evidence_refs']=['conversation:actual-save-reopen']
    obs['checks'][0]['outcome']='fail'
    assert m.record_native_observation(work,obs)['native_checks']['wps']['status']=='failed'
    changed=copy.deepcopy(obs);changed['observation_id']='obs-2';changed['checks'][0]['outcome']='pass'
    with pytest.raises(ValueError,match='terminal'):m.record_native_observation(work,changed)
    assert m.evaluate_acceptance(work)['native_checks']['wps']['status']=='failed'
