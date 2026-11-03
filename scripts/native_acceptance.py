#!/usr/bin/env python3
"""Record host-observed scoped WPS checks without making Word release claims.

No browser, upload, editing, download, renderer or model is invoked here.
The current conversation must supply actual observed evidence, not guesses.
"""
from __future__ import annotations
import argparse
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from semantic_contract import strict_json_read,strict_json_dumps,sha256_file
from validation_budget import atomic_json,read_budget,record_operation
from format_spec_validation import load_and_validate
ROOT=Path(__file__).resolve().parents[1]
CHECKS={'toc_links','page_number_sequence','heading_keep_next','embedded_objects','font_sample'}


def _result(observation: dict | None,status: str,next_action: str,reason: str | None=None) -> dict:
    return {'native_checks':{'wps':{'status':status,'reason':reason,'scope':'Observed checks and save-close-reopen only; not full fonts, OLE editability, mathematical correctness, or Word.'}},
            'word_release':{'status':'not_verified'},'next_action':next_action,
            'exported_bytes_binding':observation.get('exported_bytes_binding') if observation else None,
            'submission_ready':False,'provider_model_verified':False,'independent_review_verified':False}


def _validate(observation: dict) -> None:
    errors=load_and_validate(observation,ROOT/'schema/native-acceptance-v1.schema.json')
    if errors:raise ValueError('invalid native observation: '+'; '.join(errors))
    for value in (observation['native_object_url'],observation['save_close_reopen']['reopened_url']):
        if value is not None:
            url=urlparse(value)
            if url.scheme!='https' or url.username or url.password or url.hostname not in {'www.kdocs.cn','kdocs.cn','drive.wps.cn','docs.wps.com'}:
                raise ValueError('native URL is not a supported WPS object URL')
    timestamp=datetime.fromisoformat(observation['observed_at'].replace('Z','+00:00'))
    if timestamp.tzinfo is None:raise ValueError('native observation timestamp needs a timezone')
    names=[item['name'] for item in observation['checks']]
    if len(names)!=len(set(names)):raise ValueError('duplicate native check')


def evaluate_observation(observation: dict) -> dict:
    """Pure structural evaluation. It does not authenticate browser observations."""
    _validate(observation)
    if observation['upload_status']=='unknown':return _result(observation,'pending_unknown_upload','locate_existing_object','Do not submit another upload while the previous outcome is unknown.')
    if observation['upload_status']!='confirmed' or observation['native_object_url'] is None:return _result(observation,'not_verified','confirm_native_object')
    reopened=observation['save_close_reopen']
    if reopened['reopened'] and (reopened['reopened_url']!=observation['native_object_url'] or reopened['reopened_version']!=observation['native_version']):
        return _result(observation,'identity_mismatch','relocate_existing_object','URL/version differs after reopen; prior checks cannot prove this object.')
    if not all(reopened[key] for key in ('saved','closed','reopened')):return _result(observation,'pending_save_reopen','finish_save_close_reopen')
    checks={item['name']:item for item in observation['checks']}
    if any(item['outcome']=='fail' for item in checks.values()):return _result(observation,'failed','resolve_native_findings')
    if set(checks)!=CHECKS or any(item['outcome'] not in {'pass','not_applicable'} or not item['evidence_refs'] or not item['scope'].strip() for item in checks.values()) or not observation['evidence_refs']:
        return _result(observation,'pending_checks','complete_missing_checks')
    return _result(observation,'pass_scoped','complete_scoped')


def _context(work: Path,observation: dict):
    from current_session import inspect_session,session_context
    status=inspect_session(work,include_checks=False)
    if status['blocked_reasons'] or observation['input_binding']!=status['binding'] or not observation['input_binding']['candidate_sha256']:
        raise ValueError('native input binding is stale or unbound')
    ctx=session_context(work);campaign=Path(ctx['config']['campaign']);budget=read_budget(campaign)
    operation=budget['operations'].get(observation['operation_id'],{})
    if operation.get('candidate_id')!=status['binding']['run_id'] or operation.get('kind')!='wps_acceptance' or operation.get('outcome')=='not_executed':
        raise ValueError('native observation has no matching reserved acceptance')
    exported=observation['exported_bytes_binding']
    if exported is not None:
        path=(work/exported['path']).resolve()
        if work not in path.parents or not path.is_file() or sha256_file(path)!=exported['sha256']:
            raise ValueError('export bytes are missing, changed, or outside the run')
        if exported['native_object_url']!=observation['native_object_url'] or exported['native_version']!=observation['native_version']:
            raise ValueError('export is not bound to the observed native object')
    return campaign,operation


def record_native_observation(work_dir: Path,observation: dict) -> dict:
    work=Path(work_dir).resolve();_validate(observation);campaign,operation=_context(work,observation)
    path=work/'native-observations.json'
    records=strict_json_read(path) if path.is_file() else {'schema_version':'thesis-native-observations/v1','observations':[]}
    matches=[item for item in records['observations'] if item['observation_id']==observation['observation_id']]
    if matches:
        if matches!=[observation]:raise ValueError('observation identity reused with different content')
        return evaluate_acceptance(work)
    if operation['outcome'] in {'completed','failed'}:
        raise ValueError('terminal WPS acceptance accepts exact replay only; a new observation needs a fresh eligible operation')
    records['observations'].append(observation);atomic_json(path,records)
    result=evaluate_acceptance(work)
    state=result['native_checks']['wps']['status']
    if operation['outcome'] in {'reserved','unknown'}:
        outcome={'pending_unknown_upload':'unknown','pass_scoped':'completed','failed':'failed'}.get(state)
        if outcome:record_operation(campaign,observation['operation_id'],outcome,{'evidence_refs':[str(path),*observation['evidence_refs']],'observation_id':observation['observation_id']})
    return result


def evaluate_acceptance(work_dir: Path) -> dict:
    work=Path(work_dir).resolve();path=work/'native-observations.json'
    if not path.is_file():return _result(None,'not_verified','native_acceptance')
    try:
        records=strict_json_read(path)
        if set(records)!={'schema_version','observations'} or records['schema_version']!='thesis-native-observations/v1' or not records['observations']:raise ValueError('invalid native observation log')
        observation=records['observations'][-1];_validate(observation);_context(work,observation)
        previous=[item for item in records['observations'][:-1] if item['upload_status']=='confirmed' and item['native_object_url']]
        if previous:
            first,last=previous[0],previous[-1]
            if first['native_object_url']!=observation['native_object_url']:
                return _result(observation,'identity_mismatch','relocate_existing_object','Native object URL changed; old acceptance cannot be moved to a different object.')
            if last['native_version']!=observation['native_version'] and last['evidence_refs']==observation['evidence_refs']:
                return _result(observation,'identity_mismatch','recheck_current_version','Native version changed without fresh observations.')
        result=evaluate_observation(observation)
        references=observation['evidence_refs']+[ref for check in observation['checks'] for ref in check['evidence_refs']]
        if result['native_checks']['wps']['status']=='pass_scoped' and any(ref.startswith(('fixture:','test:')) for ref in references):
            return _result(observation,'pending_host_verification','supply_real_host_observations','Fixture evidence is not production acceptance.')
        return result
    except (ValueError,KeyError,OSError,TypeError) as exc:return _result(None,'blocked','resolve_binding',str(exc))


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=['record','status']);p.add_argument('--work-dir',type=Path,required=True);p.add_argument('--observation',type=Path);a=p.parse_args()
    result=record_native_observation(a.work_dir,strict_json_read(a.observation)) if a.command=='record' else evaluate_acceptance(a.work_dir)
    print(strict_json_dumps(result,indent=2));return 0

if __name__=='__main__':raise SystemExit(main())
