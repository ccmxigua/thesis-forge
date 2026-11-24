#!/usr/bin/env python3
"""Read-only separation gate. Delivery structure is never formatting acceptance."""
from __future__ import annotations
import argparse
import json
import re
from pathlib import Path
from zipfile import BadZipFile
from docx.opc.exceptions import PackageNotFoundError
from lxml.etree import XMLSyntaxError
from docx import Document
from docx_semantics import all_body_paragraphs
from review_package_export import (digest, MR_START, SC_START, _human_marker_ids,
                                   _manual_marker_projections, expected_paper_marker_ids)
from draft_scorecard import _scorecard_semantics_valid, audit_external_scorecard
from semantic_contract import strict_json_read, sha256_json


def inspect_paper(paper: Path) -> dict:
    texts=[p.text for p in all_body_paragraphs(Document(paper))]
    scorecards=[t for t in texts if SC_START.match(t)]
    markers=[(MR_START.match(t).group(1),t) for t in texts if MR_START.match(t)]
    compact=[key for key,text in markers if text==f'【{key}｜人工待审】详见同编号独立审查台账。']
    verbose=len(markers)-len(compact)
    duplicated=len(markers)!=len({key for key,_ in markers})
    inline=bool(scorecards or verbose or duplicated)
    return {'protocol':'separated_review_delivery_v1',
            'status':'separation_required' if inline else 'external_ledger_verification_required',
            'paper_sha256':digest(paper),'verbose_scorecard_count':len(scorecards),
            'verbose_manual_marker_count':verbose,'compact_marker_ids':compact,
            'duplicate_manual_markers':duplicated,'delivery_verified':False,
            'submission_ready':False,'formatting_verified':False,'native_acceptance_verified':False}


def verify_delivery(paper: Path, ledger_json: Path, ledger_docx: Path) -> dict:
    result={'protocol':'separated_review_delivery_v1','status':'blocked',
            'delivery_verified':False,'submission_ready':False,'formatting_verified':False,
            'native_acceptance_verified':False,'blocked_reasons':[]}
    try:
        if len({p.resolve() for p in (paper,ledger_json,ledger_docx)})!=3:
            raise ValueError('paper and both ledgers must be distinct files')
        inspection=inspect_paper(paper);result['paper_inspection']=inspection
        if inspection['status']=='separation_required':raise ValueError('paper still contains verbose or duplicate diagnostics')
        wrapper=strict_json_read(ledger_json)
        if not isinstance(wrapper,dict):raise ValueError('ledger wrapper must be an object')
        unsigned={k:v for k,v in wrapper.items() if k!='audit_sha256'}
        if wrapper.get('audit_sha256')!=sha256_json(unsigned):raise ValueError('ledger audit hash mismatch')
        if wrapper.get('protocol')!='source_bound_external_review_ledger_v1' or wrapper.get('status')!='complete':
            raise ValueError('not a completed external review ledger export')
        if any(wrapper.get(k) is not False for k in ('submission_ready','model_request_made','semantic_review_is_new')):
            raise ValueError('invalid inherited non-submission lineage')
        if wrapper.get('paper_docx',{}).get('sha256')!=digest(paper):raise ValueError('paper bytes differ from ledger')
        if wrapper.get('editable_ledger_docx',{}).get('sha256')!=digest(ledger_docx):raise ValueError('editable ledger bytes differ')
        card=wrapper.get('scorecard',{})
        if not _scorecard_semantics_valid(card):raise ValueError('invalid scorecard semantics')
        if wrapper.get('item_count')!=len(card['entries']) or wrapper.get('status_counts')!=card['status_counts']:
            raise ValueError('ledger counts differ from complete scorecard')
        binding=card.get('binding',{})
        lineage=wrapper.get('lineage',{})
        if not binding.get('run_id') or not binding.get('case_id') or lineage.get('parent_run_id')!=binding['run_id'] or lineage.get('case_id')!=binding['case_id']:
            raise ValueError('missing or mismatched source run/case identity')
        source_sha=lineage.get('source_docx_sha256')
        if not isinstance(source_sha,str) or not re.fullmatch(r'[0-9a-f]{64}',source_sha) or source_sha!=card.get('binding',{}).get('input_source_sha256'):
            raise ValueError('source lineage differs from scorecard binding')
        reconciliation=wrapper.get('current_output_reconciliation',{})
        if reconciliation.get('status')!='complete' or any(reconciliation.get(k) is not False for k in ('submission_ready','model_request_made','semantic_review_is_new')):
            raise ValueError('current-output reconciliation is not complete and inherited')
        current=reconciliation.get('current_output',{})
        assessment=card.get('current_output_assessment',{})
        if not isinstance(current.get('pdf_sha256'),str) or not re.fullmatch(r'[0-9a-f]{64}',current['pdf_sha256']):
            raise ValueError('current PDF binding is missing or invalid')
        if wrapper.get('current_pdf',{}).get('sha256')!=current['pdf_sha256']:
            raise ValueError('PDF index differs from reconciliation')
        if current.get('docx_sha256')!=digest(paper) or current.get('pdf_sha256')!=assessment.get('pdf_sha256'):
            raise ValueError('reconciliation differs from current paper assessment')
        report=card.get('rendered_output_audit',{}).get('report')
        if not isinstance(report,dict) or not audit_external_scorecard(card,paper,report)['valid']:
            raise ValueError('external scorecard render binding is invalid')
        projections=_manual_marker_projections(card)
        expected_ids=expected_paper_marker_ids(card,paper,wrapper.get('split_report'))
        if set(inspection['compact_marker_ids'])!=set(expected_ids):
            raise ValueError('paper marker IDs differ from complete ledger')
        result.update(status='separated_delivery_verified',delivery_verified=True,
                      paper_sha256=digest(paper),ledger_json_sha256=digest(ledger_json),
                      ledger_docx_sha256=digest(ledger_docx),item_count=len(card['entries']))
    except (OSError,ValueError,KeyError,TypeError,AttributeError,BadZipFile,PackageNotFoundError,XMLSyntaxError) as exc:
        result['blocked_reasons'].append(str(exc))
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('paper',type=Path)
    parser.add_argument('--ledger-json',type=Path,required=True)
    parser.add_argument('--ledger-docx',type=Path,required=True)
    args=parser.parse_args();result=verify_delivery(args.paper,args.ledger_json,args.ledger_docx)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['delivery_verified'] else 2

if __name__=='__main__':raise SystemExit(main())
