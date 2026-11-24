"""Synthetic delivery checks, not a user's thesis or visual acceptance."""
import importlib
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from docx import Document
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))

class DeliveryTests(unittest.TestCase):
    def module(self):
        path=ROOT/'scripts/separated_delivery.py'
        self.assertTrue(path.exists(), 'No separation delivery gate exists')
        return importlib.import_module('separated_delivery')

    def test_inline_diagnostics_are_never_paper_delivery(self):
        module=self.module()
        with tempfile.TemporaryDirectory() as raw:
            p=Path(raw)/'paper.docx';d=Document()
            d.add_paragraph('自动核验评分');d.add_paragraph('【SC-0001｜待核】some diagnosis')
            d.add_paragraph('【MR-0001｜人工待审】full diagnostic explanation')
            d.save(p);before=p.read_bytes()
            r=module.inspect_paper(p)
            self.assertEqual(r['status'],'separation_required')
            self.assertEqual(r['verbose_scorecard_count'],1)
            self.assertEqual(r['verbose_manual_marker_count'],1)
            self.assertEqual(p.read_bytes(),before)
            self.assertFalse(r['submission_ready'])

    def test_compact_marker_alone_is_not_a_complete_delivery(self):
        module=self.module()
        with tempfile.TemporaryDirectory() as raw:
            p=Path(raw)/'paper.docx';d=Document()
            d.add_paragraph('正文');d.add_paragraph('【MR-0001｜人工待审】详见同编号独立审查台账。');d.save(p)
            r=module.inspect_paper(p)
            self.assertEqual(r['status'],'external_ledger_verification_required')
            self.assertFalse(r['delivery_verified'])
            self.assertEqual(r['compact_marker_ids'],['MR-0001'])

    def test_table_diagnostics_are_not_missed(self):
        module=self.module()
        with tempfile.TemporaryDirectory() as raw:
            p=Path(raw)/'paper.docx';d=Document();d.add_table(1,1).cell(0,0).text='【SC-0001｜未核】diagnostic';d.save(p)
            self.assertEqual(module.inspect_paper(p)['status'],'separation_required')

    def test_missing_or_fabricated_ledger_fails_closed(self):
        module=self.module()
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);p=root/'paper.docx';Document().save(p)
            r=module.verify_delivery(p,root/'missing.json',root/'missing.docx')
            self.assertFalse(r['delivery_verified'])
            fake=root/'fake.json';fake.write_text(json.dumps({'status':'complete','paper_docx':{'sha256':module.digest(p)}}))
            r=module.verify_delivery(p,fake,p)
            self.assertFalse(r['delivery_verified'])
            self.assertFalse(r['submission_ready'])

    def test_duplicate_compact_markers_block_delivery(self):
        module=self.module()
        with tempfile.TemporaryDirectory() as raw:
            p=Path(raw)/'paper.docx';d=Document()
            for _ in range(2): d.add_paragraph('【MR-0001｜人工待审】详见同编号独立审查台账。')
            d.save(p)
            self.assertEqual(module.inspect_paper(p)['status'],'separation_required')

    def test_malformed_docx_is_blocked_not_uncaught(self):
        module=self.module()
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);p=root/'bad.docx';p.write_bytes(b'not a docx')
            result=module.verify_delivery(p,root/'ledger.json',root/'ledger.docx')
            self.assertFalse(result['delivery_verified'])
            self.assertTrue(result['blocked_reasons'])

    def test_complete_synthetic_export_and_byte_tamper(self):
        module=self.module()
        from draft_scorecard import POLICY, STATUSES
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);p=root/'paper.docx';ledger=root/'issues.docx';j=root/'ledger.json'
            Document().save(p);Document().save(ledger)
            counts={k:0 for k in STATUSES}
            report={'pdf_sha256':'b'*64}
            report['audit_sha256']=module.sha256_json(report)
            attachment={'protocol':'rendered_output_audit_attachment_v1','docx_sha256':module.digest(p),
                'pdf_sha256':report['pdf_sha256'],'report_sha256':module.sha256_json(report),
                'report':report,'submission_ready':False}
            attachment['audit_sha256']=module.sha256_json(attachment)
            card={'schema_version':'3.0','policy':POLICY,'entries':[],'status_counts':counts,
                'item_count':0,'diagnostic_item_count':0,'verified_count':0,'failed_count':0,
                'unverified_count':0,'pending_count':0,'submission_ready':False,
                'current_output_assessment':{'docx_sha256':module.digest(p),
                  'pdf_sha256':report['pdf_sha256'],'rendered_report_sha256':module.sha256_json(report),
                  'submission_ready':False,'field_refresh_claimed':False},'rendered_output_audit':attachment}
            wrapper={'protocol':'source_bound_external_review_ledger_v1','status':'complete',
                'submission_ready':False,'model_request_made':False,'semantic_review_is_new':False,
                'paper_docx':{'sha256':module.digest(p)},'editable_ledger_docx':{'sha256':module.digest(ledger)},
                'scorecard':card,'item_count':0,'status_counts':counts,
                'current_pdf':{'sha256':report['pdf_sha256']},
                'lineage':{'source_docx_sha256':'a'*64,'parent_run_id':'synthetic-run','case_id':'synthetic-case'},
                'current_output_reconciliation':{'status':'complete','submission_ready':False,
                  'model_request_made':False,'semantic_review_is_new':False,
                  'current_output':{'docx_sha256':module.digest(p),'pdf_sha256':report['pdf_sha256']}}}
            card['binding']={'input_source_sha256':'a'*64,'run_id':'synthetic-run','case_id':'synthetic-case'}
            wrapper['audit_sha256']=module.sha256_json(wrapper);j.write_text(json.dumps(wrapper))
            result=module.verify_delivery(p,j,ledger)
            self.assertTrue(result['delivery_verified'],result)
            self.assertFalse(result['submission_ready'])
            self.assertFalse(result['formatting_verified'])
            original=dict(wrapper)
            for key in ('lineage','current_output_reconciliation','current_pdf'):
                broken=dict(original);broken.pop(key)
                broken['audit_sha256']=module.sha256_json({k:v for k,v in broken.items() if k!='audit_sha256'})
                j.write_text(json.dumps(broken))
                self.assertFalse(module.verify_delivery(p,j,ledger)['delivery_verified'])
            broken=copy.deepcopy(original);broken['scorecard'].pop('binding')
            broken['audit_sha256']=module.sha256_json({k:v for k,v in broken.items() if k!='audit_sha256'})
            j.write_text(json.dumps(broken))
            self.assertFalse(module.verify_delivery(p,j,ledger)['delivery_verified'])
            broken=copy.deepcopy(original);broken['lineage']=None
            broken['audit_sha256']=module.sha256_json({k:v for k,v in broken.items() if k!='audit_sha256'})
            j.write_text(json.dumps(broken))
            self.assertFalse(module.verify_delivery(p,j,ledger)['delivery_verified'])
            j.write_text(json.dumps(original))
            d=Document(ledger);d.add_paragraph('changed');d.save(ledger)
            self.assertFalse(module.verify_delivery(p,j,ledger)['delivery_verified'])

if __name__=='__main__':unittest.main()
