"""Bound wire corrections and mixed declaration text; no semantic pass rewriting."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))
import native_semantic_review as native
import host_agent_bridge as bridge
from semantic_contract import sha256_json
from semantic_source_references import (
    build_source_reference_packet, compile_source_reference_response,
    SourceReferenceResponseError,
)
import test_source_atom_review_alignment as alignment_tests
import test_native_semantic_review as native_tests
from test_host_agent_bridge import bind_mock_review_to_source_spans


class IndependentWireCorrectionTests(unittest.TestCase):
    def incident(self):
        return json.loads((ROOT / 'tests/fixtures/mixed-declaration-wire-incident.json').read_text())

    def wire(self):
        case = self.incident()
        request = {'protocol': native.OBLIGATION_COVERAGE_PROTOCOL, 'run_id': 'wire-fresh',
                   'provider_attempt': 1, 'provenance': {'run_id': 'wire-fresh'}, 'checks': [case['check']]}
        raw = {'results': [case['invalid_result']]}
        check = build_source_reference_packet(request)['checks'][0]
        for atom, primary in zip(raw['results'][0]['identified_obligations'],
                                 check['review_context']['primary_obligations']):
            atom['source_ref'] = next(s['ref_id'] for s in check['source_spans'] if s['text'] == primary['source_quote'])
        raw['results'][0]['evidence_refs'] = [a['source_ref'] for a in raw['results'][0]['identified_obligations']]
        from host_review_schema import normalize_native_response
        from semantic_source_references import source_reference_schema
        raw = normalize_native_response(raw, source_reference_schema(native.OBLIGATION_COVERAGE_SCHEMA,
            build_source_reference_packet(request), coverage=True))
        return request, raw

    def feedback(self, request, error):
        return {'code': native.SourceReferenceContractError.code,
                'issues': error.issues, 'schema_sha256': error.schema_sha256,
                'checks_sha256': sha256_json(request['checks']), 'run_id': request['run_id'],
                'provenance': request['provenance'], 'clause_ids': list(error.clause_ids),
                'rejected_request_sha256': sha256_json(request)}

    def test_real_wire_contradiction_retained_and_diagnosed(self):
        request, raw = self.wire(); original = copy.deepcopy(raw)
        with self.assertRaises(SourceReferenceResponseError) as caught:
            compile_source_reference_response(raw, request, native.OBLIGATION_COVERAGE_SCHEMA, coverage=True)
        error = native.SourceReferenceContractError(caught.exception)
        details = str(error.issues)
        self.assertIn('primary_obligation_id', details)
        self.assertIn('requires at most 0', details)
        self.assertEqual(raw, original)
        corrected = copy.deepcopy(raw)
        primaries = request['checks'][0]['review_context']['primary_obligations']
        for atom, primary in zip(corrected['results'][0]['identified_obligations'], primaries):
            atom['primary_obligation_id'] = primary['id']
            atom['disposition'] = 'represented' if primary['status'] == 'covered' else 'external_action_pending'
        response, _ = compile_source_reference_response(corrected, request, native.OBLIGATION_COVERAGE_SCHEMA, coverage=True)
        result = native.validate_obligation_coverage_response(response, request['checks'])[0]
        self.assertEqual(result['verdict'], 'mixed_execution_external_pending')
        self.assertEqual(result['identified_obligations'][1]['requirement_refs'], [])

    def test_feedback_replays_exact_rejected_input_and_refuses_tampering(self):
        request, raw = self.wire()
        with self.assertRaises(SourceReferenceResponseError) as caught:
            compile_source_reference_response(raw, request, native.OBLIGATION_COVERAGE_SCHEMA, coverage=True)
        error = native.SourceReferenceContractError(caught.exception)
        retry = {**request, 'provider_attempt': 2, 'retry_feedback': self.feedback(request, error)}
        self.assertTrue(native.source_reference_retry_feedback_is_bound(retry))
        self.assertIn('persistent errors still block', native._prompt(retry))
        for field, value in [('run_id', 'old'), ('checks_sha256', '0' * 64),
                             ('schema_sha256', '0' * 64), ('issues', []), ('clause_ids', ['foreign'])]:
            bad = copy.deepcopy(retry); bad['retry_feedback'][field] = value
            self.assertFalse(native.source_reference_retry_feedback_is_bound(bad), field)
            with self.assertRaises(native.NativeSemanticReviewError):
                native._prompt(bad)
        for field, value in [('errors', ['invented']), ('rejected_result', {}), ('result_index', -1)]:
            bad = copy.deepcopy(retry); bad['retry_feedback']['issues'][0][field] = value
            self.assertFalse(native.source_reference_retry_feedback_is_bound(bad), field)

    def test_unknown_duplicate_and_missing_checks_are_not_correction_authority(self):
        request, raw = self.wire()
        for results in [[], raw['results'] * 2, [{**raw['results'][0], 'check_id': 'foreign'}]]:
            with self.assertRaises(ValueError) as caught:
                compile_source_reference_response({'results': results}, request, native.OBLIGATION_COVERAGE_SCHEMA, coverage=True)
            self.assertNotIsInstance(caught.exception, SourceReferenceResponseError)

    def test_native_runner_keeps_failed_raw_without_success_receipt(self):
        from subprocess import CompletedProcess
        request, raw = self.wire()
        def build_command(**kwargs):
            kwargs['last_message_path'].write_text('{}', encoding='utf-8')
            return ['codex']
        helper = native_tests.NativeSemanticReviewTests()
        patches = helper._stub_codex_host(CompletedProcess(['codex'], 0, '{}', ''))
        patches[4] = patch.object(native.codex_adapter, 'build_command', side_effect=build_command)
        patches.append(patch.object(native.codex_adapter, 'parse_result', return_value=(raw, {})))
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / 'native'
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
                with self.assertRaises(native.SourceReferenceContractError):
                    native.run_native_semantic_review(request, output_dir=output,
                        host_runtime='codex', model='gpt-5.6-luna', timeout=5)
            self.assertEqual(json.loads((output / 'raw-response.json').read_text()), raw)
            self.assertFalse((output / 'response.json').exists())
            self.assertFalse((output / 'source-reference-compilation.json').exists())

    def test_mixed_declaration_materializes_text_not_attestation(self):
        case = self.incident(); original = copy.deepcopy(case['candidate'])
        projected, audits = bridge._materialize_fixed_declaration_source_text(original, case['chunk'])
        item = projected['requirements'][0]['properties']['items'][0]
        self.assertEqual(item['body_parts'], [case['chunk']['evidence_context']['E00034']['text']])
        self.assertEqual(len(audits), 1)
        self.assertEqual(projected['clause_reviews'], original['clause_reviews'])
        self.assertEqual(original, case['candidate'])
        self.assertEqual(projected['clause_reviews'][1]['obligations'][1]['status'], 'unverifiable')
        for field, value in [('source_quote', 'foreign source'), ('status', 'covered'), ('route', 'automatic')]:
            bad = copy.deepcopy(original); bad['clause_reviews'][1]['obligations'][1][field] = value
            unchanged, audit = bridge._materialize_fixed_declaration_source_text(bad, case['chunk'])
            self.assertEqual(unchanged, bad); self.assertEqual(audit, [])
        bad_chunk = copy.deepcopy(case['chunk'])
        bad_chunk['evidence_context']['E00034']['text'] += ' changed'
        unchanged, audit = bridge._materialize_fixed_declaration_source_text(original, bad_chunk)
        self.assertEqual(unchanged, original); self.assertEqual(audit, [])

    def run_correction(self, output, correct):
        helper = alignment_tests.SourceAtomReviewAlignmentTests()
        case, chunk, candidate = helper.fixture()
        calls = []
        def reviewer(request, **kwargs):
            calls.append(copy.deepcopy(request))
            result = copy.deepcopy(case['independent_result'])
            context = request['checks'][0]['review_context']
            for atom, primary in zip(result['identified_obligations'], context['primary_obligations']):
                atom.update({k: primary.get(k) for k in ('condition', 'source_quote')})
                atom['requirement_refs'] = [context['linked_requirements'][0]['requirement_ref']]
            if len(calls) == 1 or not correct:
                result['identified_obligations'][0]['primary_obligation_id'] = 'foreign'
            try:
                packet_check = build_source_reference_packet(request)['checks'][0]
                spans = packet_check['source_spans']
                wire_result = copy.deepcopy(result)
                wire_result.pop('machine_obligation_ids', None)
                wire_result['evidence_refs'] = [next(s['ref_id'] for s in spans if s['text'] == q)
                    for q in wire_result.pop('evidence_quotes')]
                for atom in wire_result['identified_obligations']:
                    quote = atom.pop('source_quote')
                    atom['source_ref'] = next(s['ref_id'] for s in spans if s['text'] == quote)
                compile_source_reference_response({'results': [wire_result]}, request,
                    native.OBLIGATION_COVERAGE_SCHEMA, coverage=True)
                bound = bind_mock_review_to_source_spans({'protocol': native.OBLIGATION_COVERAGE_PROTOCOL,
                    'status': 'completed', 'results': [result], 'summary': {}}, request, kwargs['output_dir'])
            except SourceReferenceResponseError as error:
                raise native.SourceReferenceContractError(error) from error
            native.validate_obligation_coverage_response({'results': bound['results']}, request['checks'])
            return bound
        with patch.object(bridge, 'run_native_semantic_review', side_effect=reviewer), patch.object(bridge.time, 'sleep'):
            pointer = bridge._run_independent_obligation_coverage_review(candidate, chunk,
                review_dir=Path(output), run_id='fresh-alignment-test', chunk_index=1, attempt=1,
                host_runtime='codex', model='gpt-5.6-luna', timeout=5, agent_id='main', runner='exec',
                binary='codex', config_path=None, controller=bridge.RunController())
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]['checks'], calls[1]['checks'])
        self.assertTrue(native.source_reference_retry_feedback_is_bound(calls[1]))
        self.assertEqual(candidate, helper.fixture()[2])
        return pointer

    def test_one_new_independent_read_never_modifies_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            pointer = self.run_correction(td, True)
            self.assertEqual(pointer['provider_attempt'], 2)
            ledger = json.loads((Path(td) / pointer['obligation_analysis_ledger_path']).read_text())
            self.assertFalse(ledger['submission_ready'])

    def test_repeated_wire_error_exhausts_without_success_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(bridge.IndependentObligationReviewError) as caught:
                self.run_correction(td, False)
            self.assertFalse(caught.exception.retryable)
            self.assertEqual(caught.exception.error_records[0]['provider_attempts'], 2)
            self.assertEqual(list(Path(td).rglob('obligation-analysis-ledger.json')), [])


if __name__ == '__main__':
    unittest.main()
