#!/usr/bin/env python3
"""Atomic, explicit campaign reservations. Uncertain execution is never refunded."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import tempfile
import uuid
from semantic_contract import strict_json_read, strict_json_dumps

LIMITS={'candidate':2,'full_render':2,'wps_acceptance':1}
KINDS=set(LIMITS)
OUTCOMES={'reserved','completed','failed','unknown','not_executed'}


def atomic_json(path: Path, value: dict) -> None:
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.state-',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            stream.write(strict_json_dumps(value,indent=2,sort_keys=True)+'\n');stream.flush();os.fsync(stream.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


@contextmanager
def _locked(campaign: Path):
    campaign=Path(campaign).resolve()
    if not campaign.is_dir():raise ValueError('initialize campaign explicitly before use')
    with (campaign/'.budget.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        yield campaign/'validation-budget.json'


def _validate(state: dict) -> dict:
    if not isinstance(state,dict) or set(state)!={'schema_version','campaign_id','limits','operations'} or state['schema_version']!='thesis-validation-budget/v1' or state['limits']!=LIMITS or not isinstance(state['operations'],dict):
        raise ValueError('invalid validation campaign')
    for op in state['operations'].values():
        if not isinstance(op,dict) or set(op)!={'candidate_id','kind','outcome','evidence'} or op['kind'] not in KINDS or op['outcome'] not in OUTCOMES:
            raise ValueError('invalid validation operation')
    return state


def initialize_campaign(campaign: Path) -> dict:
    campaign=Path(campaign);campaign.mkdir(parents=True,exist_ok=True)
    with _locked(campaign) as path:
        if path.exists():return _validate(strict_json_read(path))
        state={'schema_version':'thesis-validation-budget/v1','campaign_id':str(uuid.uuid4()),'limits':dict(LIMITS),'operations':{}}
        atomic_json(path,state);return state


def _summary(state: dict) -> dict:
    charged=[op for op in state['operations'].values() if op['outcome']!='not_executed']
    candidates={op['candidate_id'] for op in charged if op['kind']=='candidate'}
    return {'campaign_id':state['campaign_id'],'limits':state['limits'],
            'used':{'candidate':len(candidates),'by_candidate':{c:{k:sum(op['candidate_id']==c and op['kind']==k for op in charged) for k in ('full_render','wps_acceptance')} for c in sorted(candidates)}},
            'operations':state['operations']}


def read_budget(campaign: Path) -> dict:
    path=Path(campaign)/'validation-budget.json'
    if not path.is_file():raise ValueError('initialize campaign explicitly before use')
    return _summary(_validate(strict_json_read(path)))


def reserve_operation(campaign: Path,candidate_id: str,kind: str,operation_id: str) -> dict:
    if kind not in KINDS or not all(isinstance(v,str) and v.strip() and len(v)<256 for v in (candidate_id,operation_id)):
        raise ValueError('invalid operation identity or kind')
    with _locked(campaign) as path:
        if not path.is_file():raise ValueError('initialize campaign explicitly before use')
        state=_validate(strict_json_read(path));ops=state['operations']
        if operation_id in ops:
            old=ops[operation_id]
            if old['candidate_id']!=candidate_id or old['kind']!=kind:raise ValueError('operation identity conflict')
            return {'status':old['outcome'],'can_execute':False,'operation_id':operation_id,'replayed':True}
        charged=[op for op in ops.values() if op['outcome']!='not_executed']
        candidates={op['candidate_id'] for op in charged if op['kind']=='candidate'}
        reason=None
        if kind=='candidate':
            if candidate_id in candidates:reason='candidate_already_reserved'
            elif len(candidates)>=LIMITS['candidate']:reason='candidate_limit_reached'
        elif candidate_id not in candidates:reason='candidate_not_reserved'
        elif sum(op['candidate_id']==candidate_id and op['kind']==kind for op in charged)>=LIMITS[kind]:reason=f'{kind}_limit_reached'
        if reason:return {'status':'blocked','can_execute':False,'reason':reason}
        ops[operation_id]={'candidate_id':candidate_id,'kind':kind,'outcome':'reserved','evidence':{}}
        atomic_json(path,state)
        return {'status':'reserved','can_execute':True,'operation_id':operation_id,'replayed':False}


def record_operation(campaign: Path,operation_id: str,outcome: str,evidence: dict) -> dict:
    if outcome not in OUTCOMES-{'reserved'} or not isinstance(evidence,dict):raise ValueError('invalid operation outcome')
    if not isinstance(evidence.get('evidence_refs'),list) or not evidence['evidence_refs'] or not all(isinstance(x,str) and x for x in evidence['evidence_refs']):
        raise ValueError('outcome requires evidence references')
    if outcome=='not_executed' and evidence.get('execution_confirmed') is not False:
        raise ValueError('refund requires affirmative evidence of non-execution')
    with _locked(campaign) as path:
        state=_validate(strict_json_read(path));op=state['operations'].get(operation_id)
        if op is None:raise ValueError('unknown operation')
        if op['outcome'] in {'completed','failed','not_executed'}:
            if op['outcome']==outcome and op['evidence']==evidence:return dict(op)
            raise ValueError('terminal outcome is immutable')
        op.update(outcome=outcome,evidence=evidence);atomic_json(path,state);return dict(op)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=['init','status','reserve','record']);p.add_argument('--campaign',type=Path,required=True)
    p.add_argument('--candidate-id');p.add_argument('--kind',choices=sorted(KINDS));p.add_argument('--operation-id');p.add_argument('--outcome',choices=sorted(OUTCOMES-{'reserved'}));p.add_argument('--evidence',type=Path)
    a=p.parse_args()
    if a.command=='init':r=initialize_campaign(a.campaign)
    elif a.command=='status':r=read_budget(a.campaign)
    elif a.command=='reserve':r=reserve_operation(a.campaign,a.candidate_id,a.kind,a.operation_id)
    else:r=record_operation(a.campaign,a.operation_id,a.outcome,strict_json_read(a.evidence))
    print(strict_json_dumps(r,indent=2));return 1 if r.get('status')=='blocked' else 0

if __name__=='__main__':raise SystemExit(main())
