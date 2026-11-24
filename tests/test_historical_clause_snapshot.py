"""Source clause history survives current-output reconciliation without promotion."""
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import compliance

class HistoricalSnapshotTests(unittest.TestCase):
    def snapshot(self, records):
        return compliance.historical_clause_snapshot(records,'supported_subset',source_sha256='a'*64,format_spec_sha256='b'*64)
    def test_original_status_and_nested_evidence_are_detached(self):
        records=[{'clause_id':'C1','status':'pending_execution','scope':'docx','detail':{'refs':['x']}}]
        snap=self.snapshot(records)
        records[0]['status']='satisfied';records[0]['detail']['refs'].append('changed')
        self.assertEqual(snap['records'][0]['status'],'pending_execution')
        self.assertEqual(snap['records'][0]['detail']['refs'],['x'])
        self.assertFalse(snap['submission_ready'])
        self.assertEqual(snap['binding']['source_sha256'],'a'*64)
    def test_reconciled_records_rejected_not_sanitized(self):
        with self.assertRaises(ValueError): self.snapshot([{'clause_id':'C1','status':'satisfied','current_output_instance':{}}])
    def test_invalid_binding_rejected(self):
        with self.assertRaises(ValueError): compliance.historical_clause_snapshot([], 'supported_subset',source_sha256='wrong',format_spec_sha256='b'*64)

class ReconcileBindingTests(unittest.TestCase):
    def exercise(self, mutate=None, accepted=False):
        import json, tempfile, hashlib
        from unittest.mock import patch
        import reconcile_current_output as module
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw)
            objects={'spec':{'requirements':[]},'styles':{},'receipts':{'receipts':[]},
                     'render':{'source_docx_sha256':'a'*64},
                     'card':{'binding':{'input_source_sha256':'a'*64}}}
            for name,obj in objects.items(): (root/name).write_text(json.dumps(obj))
            snap=compliance.historical_clause_snapshot([], 'supported_subset',source_sha256='a'*64,
                format_spec_sha256=hashlib.sha256((root/'spec').read_bytes()).hexdigest())
            if mutate:mutate(snap,root)
            (root/'history').write_text(json.dumps(snap));(root/'paper').write_bytes(b'candidate')
            args=[root/name for name in ('history','spec','styles','receipts','render','paper','card','out')]
            with patch.object(module,'finalize_records',side_effect=RuntimeError('validated boundary')) as final:
                if accepted:
                    with self.assertRaisesRegex(RuntimeError,'validated boundary'):module.reconcile(*args)
                    final.assert_called_once()
                else:
                    with self.assertRaises(ValueError):module.reconcile(*args)
                    final.assert_not_called()
                    self.assertFalse((root/'out').exists())
    def test_correct_snapshot_reaches_finalization(self):self.exercise(accepted=True)
    def test_wrong_spec_rejected_before_finalization(self):
        self.exercise(lambda snap,root:snap['binding'].update(format_spec_sha256='f'*64))
    def test_wrong_source_rejected_before_finalization(self):
        self.exercise(lambda snap,root:snap['binding'].update(source_sha256='f'*64))
    def test_missing_binding_rejected_before_finalization(self):
        self.exercise(lambda snap,root:snap.pop('binding'))
    def test_wrong_protocol_rejected_before_finalization(self):
        self.exercise(lambda snap,root:snap.update(protocol='unbound'))
    def test_renderer_source_disagreement_rejected(self):
        self.exercise(lambda snap,root:(root/'render').write_text('{"source_docx_sha256":"'+'f'*64+'"}'))
    def test_missing_scorecard_source_rejected(self):
        self.exercise(lambda snap,root:(root/'card').write_text('{"binding":{}}'))

if __name__=='__main__':unittest.main()
