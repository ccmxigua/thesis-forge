"""Read-only diagnosis of one possible reference-only leaf in frozen review evidence."""
from __future__ import annotations
import argparse
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import re
import sys
from zipfile import BadZipFile, ZipFile

import fitz
from lxml import etree

from reference_leaf_advisory import W, inspect, q, regions
from semantic_contract import sha256_file, sha256_json, strict_json_dumps, strict_json_read
from session_review import _current, render_inventory_sha256
from validation_budget import read_budget

SCHEMA = 'thesis-reference-leaf-diagnostic-scope/v1'
DOCX_LIMIT = 10_000_000
PDF_LIMIT = 20_000_000


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(raw):
    return sha256(raw).hexdigest()


def path_under(root, relative):
    parts = Path(relative)
    require(not parts.is_absolute() and '..' not in parts.parts and parts.parts, 'unsafe review path')
    node = root
    require(not node.is_symlink(), 'redirected review path')
    for part in parts.parts:
        node = node / part
        require(not node.is_symlink(), 'redirected review path')
    return node


def bounded_bytes(path, expected, limit):
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    require(len(raw) <= limit, 'diagnostic file-size limit exceeded')
    require(digest(raw) == expected, 'frozen artifact bytes changed')
    return raw


def visual_inputs(work):
    """Verify the already issued visual task without creating, renewing or importing it."""
    binding, context = _current(work)
    config = context['config']
    active_path = path_under(work, 'session-reviews/active.json')
    index = strict_json_read(active_path)
    task_id = index.get('visual','')
    require(isinstance(task_id,str) and re.fullmatch(r'review-[0-9a-f]{32}',task_id),
            'already issued active visual review required')
    task_dir = path_under(work, 'session-reviews/'+task_id)
    task = strict_json_read(path_under(task_dir,'task.json'))
    require(task.get('task_id')==task_id and task.get('role')=='visual'
            and task.get('schema_version')=='thesis-review-task/v1'
            and task.get('author_task_id') == config['author_task_id'] and task.get('binding')==binding,
            'active visual task binding changed')
    render = strict_json_read(path_under(work,'render/render-binding.json'))
    pages = render['pages']
    require(3 <= len(pages) <= 30
            and [p.get('page') for p in pages] == list(range(1,len(pages)+1))
            and render['candidate_sha256']==binding['candidate_sha256'], 'unbound or out-of-scope render')
    for record in [render['pdf'], *pages]:
        path_under(work,record['path'])
    inventory = render_inventory_sha256(work,render)
    campaign = Path(config['campaign'])
    operation = read_budget(campaign)['operations'].get(render['operation_id'],{})
    evidence=operation.get('evidence',{})
    pdf_sha=render['pdf']['sha256']
    orig_sha=binding['candidate_sha256']
    require(operation.get('kind')=='full_render' and operation.get('outcome')=='completed'
            and operation.get('candidate_id')==binding['run_id']
            and evidence.get('candidate_sha256')==orig_sha
            and evidence.get('pdf_sha256')==pdf_sha
            and evidence.get('page_inventory_sha256')==inventory,
            'completed render operation does not bind this visual inventory')
    expected={'candidate/review.docx':(orig_sha,'file'),'render/document.pdf':(pdf_sha,'file')}
    expected.update(('sources/'+name+Path(config[name]).suffix,(binding[name+'_sha256'],'file'))
                    for name in ('requirements','input'))
    expected.update((f'pages/page-{item["page"]:04}.png',(item['sha256'],'image')) for item in pages)
    artifacts=task['artifacts']
    require(isinstance(artifacts,list) and len(artifacts)==len(expected)
            and all(isinstance(a,dict) and set(a)=={'path','sha256','kind'} for a in artifacts)
            and {a['path']:(a['sha256'],a['kind']) for a in artifacts}==expected
            and task['artifact_manifest_sha256']==sha256_json(artifacts),
            'frozen visual artifact manifest changed')
    for name, (content_sha, _) in expected.items():
        require(sha256_file(path_under(task_dir,name))==content_sha, 'frozen visual artifact bytes changed')
    docx=bounded_bytes(path_under(task_dir,'candidate/review.docx'),orig_sha,DOCX_LIMIT)
    pdf=bounded_bytes(path_under(task_dir,'render/document.pdf'),pdf_sha,PDF_LIMIT)
    anchors={'schema':SCHEMA,'run_id':binding['run_id'],'candidate_sha256':orig_sha,'pdf_sha256':pdf_sha,
             'page_inventory_sha256':inventory,'visual_review_task_id':task_id,
             'visual_artifact_manifest_sha256':task['artifact_manifest_sha256']}
    return anchors,docx,pdf,len(pages)


def exclusive_xml(node):
    return etree.tostring(node,method='c14n',exclusive=True,with_comments=True)


def exact_empty(node):
    def exactly(parent,tag):
        return not (parent.attrib or parent.text or parent.tail) and [x.tag for x in parent]==['{'+W+'}'+tag]
    if node.tag!='{'+W+'}p' or not exactly(node,'pPr') or not exactly(node[0],'pageBreakBefore'):
        return False
    flag=node[0][0]
    return len(flag)==0 and not(flag.text or flag.tail) and dict(flag.attrib)=={'{'+W+'}val':'0'}


def parse_docx(archive):
    return etree.fromstring(archive.read('word/document.xml'),
        parser=etree.XMLParser(resolve_entities=False,no_network=True))


def scope(docx,pdf,page_count,anchors):
    parity, terms=inspect(docx)
    if parity or len(terms)!=1:
        return None
    terminal=terms[0]
    with fitz.open(stream=pdf,filetype='pdf') as d:
        require(len(d)==page_count,'actual PDF count does not match bound inventory')
        zz=[regions(p) for p in d]
        hit=[]
        for i in range(1,len(d)-1):
            head,body,foot=zz[i]
            p=d[i]
            refs=re.findall(r'\[(\d+)\]',' '.join(zz[i-1][1]))
            if (head==['参考文献'] and not body and len(foot)==1 and foot[0].isdigit()
                and not (p.get_image_info() or p.get_drawings() or p.get_links()
                         or p.first_annot is not None or p.first_widget is not None)
                and refs and int(refs[-1])==terminal and zz[i+1][1] and zz[i+1][1][0]=='附录'):
                hit.append(i+1)
    if len(hit)!=1:
        return None
    with ZipFile(BytesIO(docx)) as archive:
        nodes=list(parse_docx(archive).find('{'+W+'}body'))
        def text(node):
            return ''.join(q(node,'.//w:t/text()')).strip()
        indices=[]
        for i in range(1,len(nodes)-2):
            last,empty,section,title=nodes[i-1:i+3]
            if (exact_empty(empty) and last.tag=='{'+W+'}p'
                and bool(re.match(r'^\['+str(terminal)+r'\]\s*\S',text(last)))
                and section.tag=='{'+W+'}p' and not text(section)
                and len(q(section,'./w:pPr/w:sectPr'))==1
                and title.tag=='{'+W+'}p' and text(title)=='附录'):
                indices.append(i)
        if len(indices)!=1:
            return None
        i=indices[0]
        return {**anchors,'physical_page':hit[0],'terminal_reference':terminal,
                'body_child_index':i,'paragraph_c14n_sha256':digest(exclusive_xml(nodes[i]))}


def diagnose(work):
    anchors,docx,pdf,count=visual_inputs(work)
    proposal=scope(docx,pdf,count,anchors)
    checked,_,_,_=visual_inputs(work)
    require(checked==anchors,'frozen visual inputs changed during diagnosis')
    result={'scope':'caller_bound_frozen_pdf_diagnostic_only',
            'status':'human_review' if proposal else 'no_specific_finding',
            'submission_ready':False,'repair_authorized':False,'renderer_provenance_verified':False,
            'frozen_evidence':anchors}
    if proposal:
        result['diagnostic_scope']=proposal
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    subs=parser.add_subparsers(dest='verb',required=True)
    diag=subs.add_parser('diagnose'); diag.add_argument('--work',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        result=diagnose(args.work.resolve(strict=True))
    except (ValueError,OSError,KeyError,TypeError,IndexError,BadZipFile,etree.XMLSyntaxError,RuntimeError) as exc:
        print('HOLD: '+str(exc),file=sys.stderr)
        return 2
    print(strict_json_dumps(result,indent=2))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
