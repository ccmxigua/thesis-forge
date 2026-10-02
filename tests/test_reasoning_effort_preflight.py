"""Effort mismatches must stop before output or native execution starts."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import apply_format_spec
import thesis_format_pipeline
import native_semantic_review
import batch_rerun_ten_schools


class ReasoningEffortPreflightTests(unittest.TestCase):
    def test_batch_keeps_environment_resolved_codex_runtime_for_post_format(self):
        batch = batch_rerun_ten_schools
        for explicit in ([], ['--host-runtime', 'codex']):
            with self.subTest(explicit=explicit), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                case = {'id': 'bsu', 'analysis_mode': 'llm_primary'}
                with patch.dict('os.environ', {'THESIS_FORGE_HOST_RUNTIME': 'codex'}), \
                     patch.object(batch, 'load_manifest', return_value=(root / 'source.tex', [case], root / 'manifest.json')), \
                     patch.object(batch, 'run_case', return_value={'returncode': 2, 'acceptance': {'accepted': False}}) as runner:
                    batch.main(['--auto-host-agent', '--schools', 'bsu', '--build-dir', str(root / 'run'), '--codex-reasoning-effort', 'max', *explicit])
                self.assertEqual(runner.call_args.kwargs['host_runtime'], 'codex')
                self.assertEqual(runner.call_args.kwargs['codex_reasoning_effort'], 'max')

    def test_batch_rejects_effort_for_packet_or_openclaw_before_creating_run(self):
        batch = batch_rerun_ten_schools
        for extra in (['--prepare-host-review'], ['--auto-host-agent']):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                with patch.dict('os.environ', {'THESIS_FORGE_HOST_RUNTIME': 'openclaw'}), \
                     patch.object(batch, 'load_manifest', return_value=(root / 'source.tex', [{'id': 'bsu', 'analysis_mode': 'llm_primary'}], root / 'manifest.json')), \
                     patch.object(batch, 'run_case') as runner, self.assertRaises(SystemExit):
                    batch.main(['--schools', 'bsu', '--build-dir', str(root / 'run'), '--codex-reasoning-effort', 'max', *extra])
                self.assertFalse((root / 'run').exists())
                runner.assert_not_called()

    def test_post_format_cli_rejects_non_codex_and_empty_effort_before_loading(self):
        for module in (apply_format_spec, thesis_format_pipeline):
            for runtime, effort in [('openclaw', 'max'), ('codex', '')]:
                argv = ['requirements.docx', 'input.docx', 'output.docx'] if module is thesis_format_pipeline else ['input.docx', 'spec.json', 'style-map.json', '--out-dir', 'output']
                argv += ['--semantic-review-runtime', runtime, '--semantic-review-model', 'gpt-6-luna', '--semantic-review-reasoning-effort', effort]
                with self.subTest(module=module.__name__, runtime=runtime, effort=effort), self.assertRaises(SystemExit) as caught:
                    module.main(argv)
                self.assertEqual(caught.exception.code, 2)

    def test_native_runner_rejects_cross_host_effort_even_without_checks(self):
        with patch.dict('os.environ', {'THESIS_FORGE_HOST_RUNTIME': 'openclaw'}), \
             patch.object(native_semantic_review, 'run_process') as runner, \
             self.assertRaises(native_semantic_review.NativeSemanticReviewError):
            native_semantic_review.run_native_semantic_review(
                {'checks': []}, output_dir=Path('not-created'), host_runtime='openclaw',
                model='openai/gpt-6-luna', reasoning_effort='max',
            )
        runner.assert_not_called()
