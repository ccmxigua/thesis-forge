"""Bind case acceptance to the fresh launcher directory, not artifact claims."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import batch_rerun_ten_schools as batch


class BatchRunRootTests(unittest.TestCase):
    def _prepared(self, run, case='bsu'):
        work = run / case / 'work'
        work.mkdir(parents=True)
        manifest = work / 'pipeline-manifest.json'
        manifest.write_text(json.dumps({'status': 'host_review_required', 'output_policy': 'review_draft', 'case_id': case, 'submission_ready': False}))
        return {'returncode': 0, 'analysis_mode': 'llm_primary', 'case_id': case,
                'fresh_run': {'run_id': 'test-only', 'cache_reused': False, 'pipeline_manifest': str(manifest)}}

    def test_external_absolute_run_keeps_all_missing_receipt_gates(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / 'fresh'
            result = self._prepared(run)
            outcome = batch.case_acceptance(result, run_root=run)
            self.assertNotIn('case_root_outside_run_root', outcome['blockers'])
            self.assertFalse(outcome['accepted'])
            self.assertIn('host_agent_audit_not_merged', outcome['blockers'])
            self.assertIn('merge_receipt_not_merged', outcome['blockers'])
            self.assertIn('generated_docx_missing', outcome['blockers'])

    def test_relative_artifact_paths_still_use_resolution_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run = root / 'build' / 'fresh'
            result = self._prepared(run)
            result['fresh_run']['pipeline_manifest'] = str(Path(result['fresh_run']['pipeline_manifest']).relative_to(root))
            outcome = batch.case_acceptance(result, root=root, run_root=run)
            # Acceptance canonicalizes paths for its symlink-escape boundary;
            # macOS /var is an alias of /private/var, not a different run.
            self.assertEqual(outcome['checks']['pipeline_manifest'],
                             str((run / 'bsu/work/pipeline-manifest.json').resolve()))
            self.assertNotIn('case_root_outside_run_root', outcome['blockers'])

    def test_sibling_previous_run_and_symlink_escape_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            current = root / 'current'
            current.mkdir()
            previous = root / 'previous'
            result = self._prepared(previous)
            for alias in (False, True):
                with self.subTest(alias=alias):
                    candidate = json.loads(json.dumps(result))
                    if alias:
                        (current / 'escaped').symlink_to(previous / 'bsu', target_is_directory=True)
                        candidate['fresh_run']['pipeline_manifest'] = str(current / 'escaped/work/pipeline-manifest.json')
                    outcome = batch.case_acceptance(candidate, root=root, run_root=current)
                    self.assertFalse(outcome['accepted'])
                    self.assertIn('case_root_outside_run_root', outcome['blockers'])

    def test_launcher_supplies_fresh_boundary_separately_from_result(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run = root / 'fresh'
            with patch.object(batch, 'load_manifest', return_value=(root / 'source.tex', [{'id': 'bsu', 'analysis_mode': 'llm_primary'}], root / 'manifest.json')), \
                 patch.object(batch, 'run_case', return_value={'returncode': 2, 'run_root': str(root / 'forged')}), \
                 patch.object(batch, 'case_acceptance', return_value={'accepted': False, 'blockers': ['prepare_only']}) as gate:
                batch.main(['--prepare-host-review', '--schools', 'bsu', '--build-dir', str(run)])
            self.assertEqual(gate.call_args.kwargs['run_root'], run)
