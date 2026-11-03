#!/usr/bin/env python3
"""Exact-artifact internal review; host observation is not offline attestation.

Reviewer JSON is untrusted and can only reach pending/hold. The invoking host
may separately record an ACTUALLY OBSERVED collaboration.list_agents result.
That explicit host bridge is a trust boundary, not a signature verifier. It
must never be fed reviewer-supplied or fabricated tool output.
"""
from __future__ import annotations
import argparse
from pathlib import Path
import shutil
import uuid
from semantic_contract import strict_json_read, strict_json_loads, strict_json_dumps, sha256_file, sha256_json
from validation_budget import atomic_json,read_budget
from format_spec_validation import load_and_validate
ROOT=Path(__file__).resolve().parents[1]
ROLES={'source_format','visual'}


def _safe(root: Path,name: str) -> Path:
    path=(root/name).resolve()
    if root.resolve() not in path.parents:raise ValueError('artifact path outside review root')
    return path


def _current(work: Path):
    from current_session import inspect_session,session_context
    status=inspect_session(work,include_checks=False)
    if status['blocked_reasons'] or not (status['binding'] or {}).get('candidate_sha256'):
        raise ValueError('candidate binding is missing or changed: '+str(status['blocked_reasons']))
    return status['binding'],session_context(work)


def _result(status: str,reason: str | None=None) -> dict:
    return {'status':status,'reason':reason,'scope':'current_conversation_internal_review',
            'cryptographically_verified':False,'independent_review_verified':False,
            'provider_model_verified':False,'submission_ready':False}



def render_inventory_sha256(work_dir: Path, render: dict) -> str:
    """Canonical immutable renderer-output identity, frozen at operation completion."""
    work=Path(work_dir).resolve()
    if not isinstance(render,dict) or set(render)!={'candidate_sha256','operation_id','pdf','pages'}:
        raise ValueError('invalid render inventory fields')
    records=[]
    for record in [render['pdf'],*render['pages']]:
        expected={'path','sha256'} if record is render['pdf'] else {'page','path','sha256'}
        if not isinstance(record,dict) or set(record)!=expected:raise ValueError('invalid render page record')
        path=_safe(work,record['path'])
        if not path.is_file() or sha256_file(path)!=record['sha256']:raise ValueError('render inventory bytes changed')
        records.append({**record,'path':path.relative_to(work).as_posix()})
    pages=records[1:]
    if len({record['path'] for record in pages})!=len(pages):raise ValueError('render page paths must resolve to distinct files')
    canonical={'candidate_sha256':render['candidate_sha256'],'operation_id':render['operation_id'],'pdf':records[0],'pages':pages}
    return sha256_json(canonical)


def issue_review(work_dir: Path,role: str,*,renew: bool=False) -> dict:
    if role not in ROLES:raise ValueError('unknown review role')
    work=Path(work_dir).resolve();binding,ctx=_current(work)
    root=work/'session-reviews';index_path=root/'active.json'
    index=strict_json_read(index_path) if index_path.is_file() else {}
    if role in index and not renew:
        prior=strict_json_read(_safe(root,index[role])/'task.json')
        if prior['binding']==binding:return prior
    sources=[('sources/requirements'+Path(ctx['config']['requirements']).suffix,Path(ctx['config']['requirements'])),('sources/input'+Path(ctx['config']['input']).suffix,Path(ctx['config']['input'])),('candidate/review.docx',Path(ctx['manifest']['output']))]
    if role=='visual':
        render_path=work/'render/render-binding.json'
        if not render_path.is_file():raise ValueError('visual review requires a bound render inventory')
        render=strict_json_read(render_path)
        if render.get('candidate_sha256')!=binding['candidate_sha256'] or not render.get('pages'):raise ValueError('render does not match candidate')
        budget=read_budget(Path(ctx['config']['campaign']))
        operation=budget['operations'].get(render.get('operation_id'),{})
        if operation.get('kind')!='full_render' or operation.get('candidate_id')!=binding['run_id'] or operation.get('outcome')!='completed':raise ValueError('render has no completed budget operation')
        import fitz
        from PIL import Image
        pdf_record=render.get('pdf',{})
        pdf_path=_safe(work,pdf_record.get('path',''))
        if not pdf_path.is_file() or sha256_file(pdf_path)!=pdf_record.get('sha256'):
            raise ValueError('render PDF is missing or changed')
        if operation.get('evidence',{}).get('candidate_sha256')!=binding['candidate_sha256'] or operation.get('evidence',{}).get('pdf_sha256')!=pdf_record['sha256']:
            raise ValueError('render operation is not bound to the candidate and PDF bytes')
        with fitz.open(pdf_path) as pdf:
            page_count=pdf.page_count
        pages=render['pages']
        if not page_count or len(pages)!=page_count or [page.get('page') for page in pages]!=list(range(1,page_count+1)):
            raise ValueError('render inventory must cover every PDF page once in order')
        inventory_sha=render_inventory_sha256(work,render)
        if operation.get('evidence',{}).get('page_inventory_sha256')!=inventory_sha:
            raise ValueError('render page inventory differs from completed renderer output')
        sources.append(('render/document.pdf',pdf_path))
        for index,page in enumerate(pages,1):
            path=_safe(work,page['path'])
            if path.suffix.lower()!='.png' or sha256_file(path)!=page['sha256']:raise ValueError('render page bytes changed')
            with Image.open(path) as image:
                if image.format!='PNG' or min(image.size)<1:raise ValueError('render page is not a readable PNG')
                image.verify()
            sources.append((f'pages/page-{index:04}.png',path))
        index=strict_json_read(index_path) if index_path.is_file() else {}
    task_id='review-'+uuid.uuid4().hex;directory=root/task_id
    artifacts=[]
    for relative,source in sources:
        target=directory/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
        artifacts.append({'path':relative,'sha256':sha256_file(target),'kind':'image' if relative.startswith('pages/') else 'file'})
    task={'schema_version':'thesis-review-task/v1','task_id':task_id,'role':role,'binding':binding,
          'author_task_id':ctx['config']['author_task_id'],'artifacts':artifacts,'artifact_manifest_sha256':sha256_json(artifacts)}
    atomic_json(directory/'task.json',task);index[role]=task_id;atomic_json(index_path,index)
    return task


def _validated(work: Path,task_id: str,observation: dict):
    binding,ctx=_current(work);root=work/'session-reviews';directory=_safe(root,task_id)
    task=strict_json_read(directory/'task.json')
    active=strict_json_read(root/'active.json')
    errors=load_and_validate(observation,ROOT/'schema/internal-review-v1.schema.json')
    if errors:raise ValueError('invalid review observation: '+'; '.join(errors))
    if task['task_id']!=task_id or active.get(task['role'])!=task_id:raise ValueError('stale task identity')
    for key in ('task_id','role','binding','artifact_manifest_sha256'):
        if observation[key]!=task[key]:raise ValueError('review binding changed: '+key)
    if task['binding']!=binding:raise ValueError('candidate binding changed')
    if task['artifact_manifest_sha256']!=sha256_json(task['artifacts']):raise ValueError('artifact manifest changed')
    for artifact in task['artifacts']:
        if sha256_file(_safe(directory,artifact['path']))!=artifact['sha256']:raise ValueError('artifact bytes changed')
    if observation['reviewer_task_id']==task['author_task_id']:raise ValueError('author cannot self-review')
    return task,directory


def _review_coverage(task,observation):
    if observation['verdict']=='HOLD' or observation['findings']:return _result('hold','Reviewer findings remain open; require correction and a fresh review.')
    reads={(item['path'],item['sha256'],item['method']) for item in observation['read_artifacts']}
    for artifact in task['artifacts']:
        method='image_view' if artifact['kind']=='image' else 'file_read'
        if (artifact['path'],artifact['sha256'],method) not in reads:return _result('pending_artifact_review','Required source/page was not observed.')
    if not task['author_task_id']:return _result('pending_host_verification','Author task identity is not observable.')
    return _result('pending_host_verification','Reviewer report is not host observation.')


def import_review(work_dir: Path,task_id: str,observation: dict) -> dict:
    work=Path(work_dir).resolve();task,directory=_validated(work,task_id,observation)
    existing=directory/'reviewer-report.json'
    if existing.is_file():
        old=strict_json_read(existing)
        if old!=observation and (old['verdict']=='HOLD' or old['findings']):return _result('hold','A held task cannot close itself; issue a fresh review.')
    atomic_json(existing,observation)
    return _review_coverage(task,observation)


def evaluate_host_snapshot(task: dict,observation: dict,tool_result: dict) -> dict:
    """Pure structural evaluation, not authentication of who supplied a snapshot."""
    result=_review_coverage(task,observation)
    if observation['source_kind']=='fixture':return _result('pending_host_verification','Fixture evidence cannot be accepted in production.')
    if result['status']!='pending_host_verification' or not task['author_task_id']:return result
    # The host must observe the complete bound payload, not a reusable free-form PASS.
    for key in ('task_id','role','binding','artifact_manifest_sha256'):
        if observation.get(key)!=task.get(key):return _result('pending_host_verification','Observed review is for a different task or artifact.')
    try:
        completed_payload=strict_json_loads(observation['result_text'])
    except (ValueError,TypeError):
        return _result('pending_host_verification','Completed result must contain the exact structured review payload.')
    expected_payload={key:value for key,value in observation.items() if key!='result_text'}
    if completed_payload!=expected_payload:
        return _result('pending_host_verification','Completed result does not commit this exact structured review.')
    matches=[agent for agent in tool_result.get('agents',[]) if agent.get('agent_name')==observation['reviewer_task_id']]
    if len(matches)!=1 or matches[0].get('agent_status')!={'completed':observation['result_text']}:
        return _result('pending_host_verification','No exact completed task result was observed.')
    return _result('host_observed_scoped','Exact distinct task result observed by this conversation; no offline authentication.')


def record_host_observation(work_dir: Path,task_id: str,tool_result: dict,*,source_ref: str,source_kind: str,image_evidence_refs: list[str] | None=None) -> dict:
    """Trusted host bridge ONLY after actual tool observation, never reviewer input.

    No CLI option silently promotes a reviewer report. The host must independently
    supply the exact list_agents result and a source reference from its conversation.
    Fixture results and unverifiable provenance remain pending and are not stored as
    accepted. This cannot certify the supplied source reference outside that host.
    """
    work=Path(work_dir).resolve();directory=_safe(work/'session-reviews',task_id)
    observation=strict_json_read(directory/'reviewer-report.json');task,directory=_validated(work,task_id,observation)
    if source_kind!='conversation_tool_observation' or not source_ref or source_ref.startswith(('fixture:','test:')):
        return _result('pending_host_verification','A real host tool observation is required.')
    result=evaluate_host_snapshot(task,observation,tool_result)
    if task['role']=='visual' and not image_evidence_refs:
        return _result('pending_host_verification','Actual image-tool observations are not available.')
    if result['status']=='host_observed_scoped':
        record={'task_id':task_id,'binding':task['binding'],'artifact_manifest_sha256':task['artifact_manifest_sha256'],
                'reviewer_report_sha256':sha256_json(observation),'tool':'collaboration.list_agents',
                'tool_result':tool_result,'tool_result_sha256':sha256_json(tool_result),
                'source_ref':source_ref,'source_kind':source_kind,'image_evidence_refs':image_evidence_refs or [],'result':result}
        atomic_json(directory/'host-observed.json',record)
    return result


def evaluate_reviews(work_dir: Path) -> dict:
    work=Path(work_dir).resolve();root=work/'session-reviews';out={'status':'not_verified','roles':{}}
    if not (root/'active.json').is_file():return out
    try:
        active=strict_json_read(root/'active.json')
        for role,task_id in active.items():
            directory=_safe(root,task_id);report_path=directory/'reviewer-report.json'
            if not report_path.is_file():out['roles'][role]=_result('pending');continue
            observation=strict_json_read(report_path);task,directory=_validated(work,task_id,observation)
            result=_review_coverage(task,observation);host_path=directory/'host-observed.json'
            if host_path.is_file():
                host=strict_json_read(host_path)
                if host['binding']!=task['binding'] or host['artifact_manifest_sha256']!=task['artifact_manifest_sha256'] or host['reviewer_report_sha256']!=sha256_json(observation) or host['tool_result_sha256']!=sha256_json(host['tool_result']):raise ValueError('host observation binding changed')
                if host['source_kind']=='conversation_tool_observation' and (role!='visual' or host['image_evidence_refs']):result=evaluate_host_snapshot(task,observation,host['tool_result'])
            out['roles'][role]=result
        out['status']='host_observed_scoped' if all(out['roles'].get(r,{}).get('status')=='host_observed_scoped' for r in ROLES) else 'pending'
    except (ValueError,KeyError,OSError,TypeError) as exc:out.update(status='blocked',reason=str(exc))
    return out


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('command',choices=['issue','import','status']);p.add_argument('--work-dir',type=Path,required=True);p.add_argument('--role',choices=sorted(ROLES));p.add_argument('--task-id');p.add_argument('--observation',type=Path);p.add_argument('--renew',action='store_true');a=p.parse_args()
    if a.command=='issue':result=issue_review(a.work_dir,a.role,renew=a.renew)
    elif a.command=='import':result=import_review(a.work_dir,a.task_id,strict_json_read(a.observation))
    else:result=evaluate_reviews(a.work_dir)
    print(strict_json_dumps(result,indent=2));return 0

if __name__=='__main__':raise SystemExit(main())
