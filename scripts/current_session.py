#!/usr/bin/env python3
"""Thin current-conversation adapter over the existing authoritative pipeline."""
from __future__ import annotations
import argparse
from contextlib import redirect_stdout, redirect_stderr
import io
from pathlib import Path
import subprocess
import sys
from semantic_contract import strict_json_read, strict_json_dumps, sha256_file
from validation_budget import atomic_json, read_budget, reserve_operation, record_operation
ROOT=Path(__file__).resolve().parents[1]
CONFIG='current-session.json'
PACKETS=('extraction-manifest.json','host-agent-review-manifest.json','llm-request.json','llm-request-chunks.json')
CONFIG_KEYS={'schema_version','campaign','campaign_id','author_task_id','requirements','input','run_id','code_sha256','packet_hashes'}


def current_code_sha256() -> str:
    from thesis_format_pipeline import runtime_code_fingerprint
    return runtime_code_fingerprint()['sha256']


def session_context(work_dir: Path) -> dict:
    work=Path(work_dir).resolve();config=strict_json_read(work/CONFIG)
    if not isinstance(config,dict) or set(config)!=CONFIG_KEYS or config['schema_version']!='thesis-current-session/v1':raise ValueError('invalid session configuration')
    return {'work':work,'config':config,'manifest':strict_json_read(work/'pipeline-manifest.json')}




def _current_pipeline_stage(command: list[str], label: str, *, timeout: int) -> int:
    """Execute only the deterministic pipeline inline; its leaf stages own timeouts.

    The legacy wrapper protocol supplies a whole-pipeline timeout. Do not impose
    that extra deadline here: leaf subprocesses already own their process groups
    and deadlines, so an outer kill would orphan independently grouped children.
    """
    if len(command)<2 or command[0]!=sys.executable or Path(command[1]).resolve()!=ROOT/'scripts/thesis_format_pipeline.py':
        raise ValueError('current-session executor accepts the deterministic pipeline only')
    from thesis_format_pipeline import main as deterministic_main
    return deterministic_main(command[2:])


def _invoke_pipeline(command: list[str]) -> subprocess.CompletedProcess:
    """Let the existing stage runner own deadlines and process-group cleanup.

    Do not place a shorter subprocess timeout around a wrapper whose children
    own separate process groups. Run that thin wrapper in this process instead.
    """
    from thesis_format import main as pipeline_main
    stdout,stderr=io.StringIO(),io.StringIO()
    with redirect_stdout(stdout),redirect_stderr(stderr):
        try:
            returncode=pipeline_main(command[2:],stage_executor=_current_pipeline_stage)
        except SystemExit as exc:
            returncode=exc.code if isinstance(exc.code,int) else 2
            if not isinstance(exc.code,int):print(str(exc.code),file=stderr)
    return subprocess.CompletedProcess(command,returncode,stdout.getvalue(),stderr.getvalue())


def prepare_session(requirements: Path,input: Path,work_dir: Path,campaign: Path,*,author_task_id: str | None=None) -> dict:
    requirements,input,work,campaign=(Path(p).resolve() for p in (requirements,input,work_dir,campaign))
    if work.exists():raise ValueError('existing work directory must use resume; never overwrite it')
    budget=read_budget(campaign)
    for source in (requirements,input):
        if not source.is_file():raise ValueError(f'missing source: {source}')
        if work==source or work in source.parents:raise ValueError('sources must remain outside work directory')
    command=[sys.executable,str(ROOT/'scripts/thesis_format.py'),str(requirements),str(input),'--work-dir',str(work)]
    result=_invoke_pipeline(command)
    if result.returncode:raise ValueError(f'preparation failed ({result.returncode}); retain {work} for diagnosis: {result.stderr[-1500:]} {result.stdout[-1500:]}')
    manifest=strict_json_read(work/'pipeline-manifest.json');review=work/'review/requirements'
    config={'schema_version':'thesis-current-session/v1','campaign':str(campaign),'campaign_id':budget['campaign_id'],
            'author_task_id':author_task_id,'requirements':str(requirements),'input':str(input),
            'run_id':manifest['run_id'],'code_sha256':current_code_sha256(),
            'packet_hashes':{name:sha256_file(review/name) for name in PACKETS}}
    atomic_json(work/CONFIG,config)
    return inspect_session(work)


def inspect_session(work_dir: Path, *, include_checks: bool=True) -> dict:
    status={'schema_version':'thesis-session-status/v1','binding':None,'next_action':'blocked','blocked_reasons':[],
            'internal_review':{'status':'not_verified'},'native_checks':{'wps':{'status':'not_verified'}},
            'word_release':{'status':'not_verified'},'budgets':None,
            'submission_ready':False,'provider_model_verified':False,'independent_review_verified':False}
    try:
        ctx=session_context(work_dir);work,config,manifest=ctx['work'],ctx['config'],ctx['manifest'];review=work/'review/requirements'
        source_records=manifest['inputs'];reasons=status['blocked_reasons']
        for field,record_name in (('requirements','requirements'),('input','source')):
            record=source_records[record_name];path=Path(config[field])
            if str(path)!=record['path'] or not path.is_file() or sha256_file(path)!=record['sha256']:reasons.append(f'{field}_bytes_changed')
        if config['run_id']!=manifest.get('run_id'):reasons.append('run_identity_changed')
        if current_code_sha256()!=config['code_sha256'] or manifest.get('code_fingerprint',{}).get('sha256')!=config['code_sha256']:reasons.append('code_changed')
        status['budgets']=read_budget(Path(config['campaign']))
        if status['budgets']['campaign_id']!=config['campaign_id']:reasons.append('campaign_changed')
        for name in PACKETS:
            if not (review/name).is_file() or sha256_file(review/name)!=config['packet_hashes'].get(name):reasons.append(f'packet_changed:{name}')
        output=Path(manifest['output']);candidate_sha=None
        if output.is_file():
            candidate_sha=sha256_file(output)
            if manifest.get('output_artifact',{}).get('sha256')!=candidate_sha:reasons.append('candidate_bytes_unbound_or_changed')
        status['binding']={'run_id':config['run_id'],'requirements_sha256':source_records['requirements']['sha256'],
                           'input_sha256':source_records['source']['sha256'],'code_sha256':config['code_sha256'],'candidate_sha256':candidate_sha}
        if reasons:return status
        if candidate_sha:
            status['next_action']='independent_review'
            if include_checks:
                status['toc']=toc_routing(output)
                from session_review import evaluate_reviews
                status['internal_review']=evaluate_reviews(work)
                from native_acceptance import evaluate_acceptance
                native=evaluate_acceptance(work)
                status['native_checks']=native['native_checks']
                if status['internal_review']['status']=='host_observed_scoped':
                    status['next_action']='complete_scoped' if native['native_checks']['wps']['status']=='pass_scoped' else 'native_acceptance'
            return status
        if manifest.get('status')!='host_review_required':
            reasons.append('pipeline_not_ready:'+str(manifest.get('status')));return status
        packet_manifest=strict_json_read(review/'host-agent-review-manifest.json')
        from requirements_engine import validate_host_review_chunk_source_projection
        validate_host_review_chunk_source_projection(strict_json_read(review/'llm-request.json'),strict_json_read(review/'llm-request-chunks.json'),packet_manifest)
        response_names=packet_manifest['response_files']
        for name in response_names:
            path=(review/name).resolve()
            if review.resolve() not in path.parents:raise ValueError('response path escapes packet directory')
        missing=[name for name in response_names if not (review/name).is_file()]
        status['semantic_task']={'review_dir':str(review),'missing_responses':missing}
        if missing:status['next_action']='semantic_response'
        elif not (review/'merge-receipt.json').is_file():status['next_action']='merge_response'
        else:
            receipt=strict_json_read(review/'merge-receipt.json')
            from offline_review_receipt import validate_offline_merge_receipt
            validate_offline_merge_receipt(response_path=Path(receipt['merged_response_path']),receipt_path=review/'merge-receipt.json',extraction_manifest=strict_json_read(review/'extraction-manifest.json'),work=work)
            status['next_action']='generate_candidate'
    except (ValueError,KeyError,TypeError,OSError) as exc:
        status['blocked_reasons'].append(str(exc));status['next_action']='blocked'
    return status



def toc_routing(candidate: Path) -> dict:
    """Read-only native-refresh routing, not pagination proof or a field update."""
    import re
    from zipfile import ZipFile
    from lxml import etree
    from toc_materializer import _effective_page_numbering, NS
    with ZipFile(candidate) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    root = etree.fromstring(members['word/document.xml'], parser=etree.XMLParser(resolve_entities=False, no_network=True))
    from types import SimpleNamespace
    from toc_refresh import collect_field_ranges, flatten_fields
    body = root.find('w:body', NS)
    if body is None:
        return {'toc_action': 'native_refresh_required', 'reasons': ['Missing document body; field scope cannot be established.']}
    # Word may split an opcode over several instrText nodes. Parse each
    # complete field rather than matching a token inside an arbitrary argument.
    fields, malformed = collect_field_ranges(SimpleNamespace(_element=SimpleNamespace(body=body)))
    simple_instructions = body.xpath('.//w:fldSimple/@w:instr', namespaces=NS)
    parsed_fields = list(flatten_fields(fields))
    has_toc = any(field['opcode'] == 'TOC' for field in parsed_fields) or any(
        re.match(r'\s*TOC(?:\s|$)', text, re.I) for text in simple_instructions)
    # Simple TOCs are handled by the existing materializer eligibility gates.
    # Other malformed fields cannot establish that no dynamic TOC is present.
    structural_errors = set(malformed) - {'simple_toc_field_not_supported'}
    # A complete PAGE/REF/etc. field may legitimately omit its cached result
    # and separator. The mutation-oriented parser flags that as incomplete,
    # but its known opcode still proves this document has no TOC.
    if not has_toc and parsed_fields and all(field['opcode'] for field in parsed_fields):
        structural_errors.discard('incomplete_field')
    structural_errors = sorted(structural_errors)
    if structural_errors:
        return {'toc_action': 'native_refresh_required',
                'reasons': ['Field structure unresolved: ' + ', '.join(structural_errors)]}
    if not has_toc:
        return {'toc_action': 'not_present', 'reasons': []}
    reasons = []
    try:
        _effective_page_numbering(root, members)
    except ValueError as exc:
        reasons.append(str(exc))
    if root.xpath('.//w:object | .//w:drawing | .//w:pict', namespaces=NS):
        reasons.append('Complex embedded or drawing objects require native pagination observation.')
    return {'toc_action': 'native_refresh_required' if reasons else 'safe_subset_eligible',
            'reasons': reasons, 'scope': 'Eligibility only; existing materializer source and geometry gates still apply. No renderer called.'}


def next_action(work_dir: Path) -> dict:
    result=inspect_session(work_dir)
    return {'next_action':result['next_action'],'blocked_reasons':result['blocked_reasons'],'binding':result['binding']}


def resume_session(work_dir: Path) -> dict:
    # Deliberately read-only. Pending tasks and uncertain side effects are not reissued.
    return inspect_session(work_dir)


def generate_candidate(work_dir: Path,operation_id: str) -> dict:
    work=Path(work_dir).resolve();status=inspect_session(work)
    if status['next_action']!='generate_candidate':return status
    ctx=session_context(work);config=ctx['config'];campaign=Path(config['campaign'])
    reservation=reserve_operation(campaign,config['run_id'],'candidate',operation_id)
    if not reservation['can_execute']:
        status['next_action']='blocked';status['blocked_reasons'].append('generation_not_reissued:'+reservation['status']);return status
    output=work/'candidate/review.docx';output.parent.mkdir(exist_ok=True)
    receipt=strict_json_read(work/'review/requirements/merge-receipt.json')
    command=[sys.executable,str(ROOT/'scripts/thesis_format.py'),config['requirements'],config['input'],str(output),'--work-dir',str(work),'--llm-response',receipt['merged_response_path']]
    try:
        result=_invoke_pipeline(command)
    except (OSError,subprocess.TimeoutExpired,KeyboardInterrupt) as exc:
        record_operation(campaign,operation_id,'unknown',{'evidence_refs':[str(work/'pipeline-manifest.json')],'error':str(exc)})
        raise
    atomic_json(work/'generation-execution.json',{'command':command,'returncode':result.returncode,'stdout':result.stdout,'stderr':result.stderr})
    record_operation(campaign,operation_id,'completed' if result.returncode==0 else 'failed',{'evidence_refs':[str(work/'generation-execution.json'),str(work/'pipeline-manifest.json')]})
    return inspect_session(work)


def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    run=sub.add_parser('run');run.add_argument('requirements',type=Path);run.add_argument('input',type=Path);run.add_argument('--work-dir',type=Path,required=True);run.add_argument('--campaign',type=Path,required=True);run.add_argument('--author-task-id')
    for command in ('status','next','resume','generate'):
        child=sub.add_parser(command);child.add_argument('--work-dir',type=Path,required=True)
        if command=='generate':child.add_argument('--operation-id',required=True)
    a=p.parse_args()
    if a.command=='run':r=prepare_session(a.requirements,a.input,a.work_dir,a.campaign,author_task_id=a.author_task_id)
    elif a.command=='generate':r=generate_candidate(a.work_dir,a.operation_id)
    elif a.command=='next':r=next_action(a.work_dir)
    else:r=inspect_session(a.work_dir)
    print(strict_json_dumps(r,indent=2));return 1 if r['next_action']=='blocked' else 0

if __name__=='__main__':raise SystemExit(main())
