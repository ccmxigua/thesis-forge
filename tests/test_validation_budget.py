from __future__ import annotations
import importlib.util
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]


def api():
    path=ROOT/'scripts/validation_budget.py'
    assert path.is_file(), 'Atomic validation campaign is not implemented'
    spec=importlib.util.spec_from_file_location('budget_target',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_campaign_caps_cross_candidates_and_replays(tmp_path):
    m=api();m.initialize_campaign(tmp_path)
    first=m.reserve_operation(tmp_path,'c1','candidate','create1')
    assert first['can_execute'] is True
    assert m.reserve_operation(tmp_path,'c1','candidate','create1')['can_execute'] is False
    m.record_operation(tmp_path,'create1','completed',{'evidence_refs':['test-output-1']})
    m.reserve_operation(tmp_path,'c2','candidate','create2')
    assert m.reserve_operation(tmp_path,'c3','candidate','create3')['status']=='blocked'
    for i in range(2): assert m.reserve_operation(tmp_path,'c1','full_render',f'render{i}')['can_execute']
    assert m.reserve_operation(tmp_path,'c1','full_render','render3')['status']=='blocked'
    assert m.reserve_operation(tmp_path,'c2','full_render','render4')['can_execute']
    assert m.reserve_operation(tmp_path,'c1','wps_acceptance','wps1')['can_execute']
    assert m.reserve_operation(tmp_path,'c1','wps_acceptance','wps2')['status']=='blocked'


def test_unknown_remains_charged_and_not_executed_requires_evidence(tmp_path):
    m=api();m.initialize_campaign(tmp_path)
    m.reserve_operation(tmp_path,'c1','candidate','create1')
    m.record_operation(tmp_path,'create1','unknown',{'evidence_refs':['transport-timeout']})
    assert not m.reserve_operation(tmp_path,'c1','candidate','create1')['can_execute']
    with pytest.raises(ValueError):m.record_operation(tmp_path,'create1','not_executed',{})
    m.record_operation(tmp_path,'create1','not_executed',{'execution_confirmed':False,'evidence_refs':['verified-no-execution']})
    assert m.reserve_operation(tmp_path,'c1','candidate','create1-retry')['can_execute']


def test_concurrent_last_slot_is_reserved_once(tmp_path):
    m=api();m.initialize_campaign(tmp_path)
    with ThreadPoolExecutor(max_workers=5) as pool:
        results=list(pool.map(lambda i:m.reserve_operation(tmp_path,f'c{i}','candidate',f'op{i}'),range(5)))
    assert sum(r['can_execute'] for r in results)==2
    assert m.read_budget(tmp_path)['used']['candidate']==2


def test_unregistered_campaign_and_identity_conflict_fail_closed(tmp_path):
    m=api()
    with pytest.raises(ValueError,match='initialize'):m.reserve_operation(tmp_path,'c1','candidate','one')
    m.initialize_campaign(tmp_path);m.reserve_operation(tmp_path,'c1','candidate','one')
    with pytest.raises(ValueError,match='identity'):m.reserve_operation(tmp_path,'c2','candidate','one')
    assert m.reserve_operation(tmp_path,'missing','full_render','bad')['status']=='blocked'
