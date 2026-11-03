from __future__ import annotations
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import pytest
from docx import Document
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))


def api():
    path=ROOT/'scripts/current_session.py'
    assert path.is_file(), 'Current-conversation orchestration is not implemented'
    spec=importlib.util.spec_from_file_location('session_target',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


@pytest.fixture
def prepared(tmp_path):
    m=api()
    from validation_budget import initialize_campaign
    campaign=tmp_path/'campaign';initialize_campaign(campaign)
    req=tmp_path/'requirements.docx';inp=tmp_path/'input.docx'
    d=Document();d.add_paragraph('正文使用宋体，小四号，1.5倍行距。');d.save(req)
    d=Document();d.add_paragraph('第1章 绪论',style='Heading 1');d.add_paragraph('用于软件单元测试的合成正文。');d.save(inp)
    work=tmp_path/'work with spaces'
    report=m.prepare_session(req,inp,work,campaign,author_task_id='test-author')
    assert report['next_action']=='semantic_response'
    return m,work,req,inp


def snapshot(path):
    return {str(p.relative_to(path)):hashlib.sha256(p.read_bytes()).hexdigest() for p in path.rglob('*') if p.is_file()}


def test_real_prepare_and_resume_never_run_provider_or_generate(prepared,monkeypatch):
    m,work,req,inp=prepared
    before=snapshot(work)
    def fail(*a,**k):raise AssertionError('Read-only resume must not launch a process')
    monkeypatch.setattr(subprocess,'run',fail)
    first=m.resume_session(work);second=m.resume_session(work)
    assert first==second
    assert first['binding']['candidate_sha256'] is None
    assert first['word_release']['status']=='not_verified'
    assert first['budgets']['used']['candidate']==0
    assert snapshot(work)==before
    assert not (work/'candidate/review.docx').exists()


def test_resume_rejects_source_bytes_without_mutating_run(prepared):
    m,work,req,inp=prepared;inp.write_bytes(inp.read_bytes()+b'changed')
    before=snapshot(work)
    report=m.resume_session(work)
    assert report['next_action']=='blocked'
    assert 'input_bytes_changed' in report['blocked_reasons']
    assert snapshot(work)==before


def test_resume_rejects_code_drift(prepared,monkeypatch):
    m,work,req,inp=prepared
    monkeypatch.setattr(m,'current_code_sha256',lambda:'0'*64)
    assert 'code_changed' in m.resume_session(work)['blocked_reasons']


def test_resume_rejects_missing_chunk_inventory(prepared):
    m,work,req,inp=prepared
    (work/'review/requirements/llm-request-chunks.json').unlink()
    report=m.resume_session(work)
    assert report['next_action']=='blocked'
    assert any('packet' in r for r in report['blocked_reasons'])


def test_prepare_refuses_existing_work_and_protects_input(prepared):
    m,work,req,inp=prepared
    with pytest.raises(ValueError,match='existing'):m.prepare_session(req,inp,work,work.parent/'campaign')


def test_status_schema_is_strict_and_matches_real_projection(prepared):
    m,work,req,inp=prepared
    from format_spec_validation import load_and_validate
    schema=ROOT/'schema/current-session-v1.schema.json'
    assert schema.is_file(), 'Current session status schema is missing'
    report=m.inspect_session(work)
    assert load_and_validate(report,schema)==[]
    report['invented_release']=True
    assert load_and_validate(report,schema)


def test_prepare_lets_existing_stage_runner_own_process_lifecycle(tmp_path,monkeypatch):
    m=api()
    from validation_budget import initialize_campaign
    campaign=tmp_path/'campaign';initialize_campaign(campaign)
    req=tmp_path/'requirements.docx';inp=tmp_path/'input.docx'
    for path in (req,inp):
        d=Document();d.add_paragraph('正文使用宋体小四号。');d.save(path)
    def forbidden(*args,**kwargs):raise AssertionError('Do not wrap the stage-owned pipeline in another subprocess.run timeout')
    monkeypatch.setattr(subprocess,'run',forbidden)
    assert m.prepare_session(req,inp,tmp_path/'work',campaign)['next_action']=='semantic_response'


def test_prepare_has_no_whole_pipeline_timeout_layer(tmp_path,monkeypatch):
    m=api()
    import thesis_format
    from validation_budget import initialize_campaign
    campaign=tmp_path/'campaign';initialize_campaign(campaign)
    req=tmp_path/'requirements.docx';inp=tmp_path/'input.docx'
    for path in (req,inp):
        d=Document();d.add_paragraph('正文使用宋体小四号。');d.save(path)
    def forbidden(*args,**kwargs):raise AssertionError('Current session must let leaf stages own deadlines, without wrapping the whole pipeline')
    monkeypatch.setattr(thesis_format,'run_stage',forbidden)
    assert m.prepare_session(req,inp,tmp_path/'work',campaign)['next_action']=='semantic_response'


def test_leaf_stage_timeout_cannot_write_after_return(tmp_path,monkeypatch):
    import time
    m=api()
    assert hasattr(m,'_current_pipeline_stage')
    import thesis_format_pipeline
    from process_runner import run_process
    marker=tmp_path/'late-marker'
    def controlled_pipeline(argv):
        child=[sys.executable,'-c',f'import time,pathlib;time.sleep(.35);pathlib.Path({str(marker)!r}).write_text("late")']
        return run_process(child,cwd=tmp_path,timeout=.05).returncode
    monkeypatch.setattr(thesis_format_pipeline,'main',controlled_pipeline)
    code=m._current_pipeline_stage([sys.executable,str(ROOT/'scripts/thesis_format_pipeline.py')],'test',timeout=1)
    assert code==124
    time.sleep(.5)
    assert not marker.exists()
