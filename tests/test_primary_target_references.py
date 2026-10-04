"""Explicit target agreement is a reviewer choice, never fuzzy equivalence."""
import copy
import json
from contextlib import ExitStack
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

import pytest
import native_semantic_review as native
import host_agent_bridge as bridge
import thesis_format_pipeline as pipeline
from semantic_contract import sha256_json, request_body_sha256
from format_spec_validation import validate_instance
from host_review_schema import native_output_schema, native_schema_support_errors
from semantic_source_references import (
    build_source_reference_packet, source_reference_schema,
    source_inventory_generation_schema, compile_source_reference_response,
)
from independent_retry_scope import prepare_retry_scope, constrain_retry_schema, validate_retry_scope
from tests import test_independent_retry_scope as scope_helpers
from tests import test_empty_inventory_verdict as transport_helpers
from tests.test_empty_inventory_verdict import enveloped
from tests import test_host_agent_bridge as bridge_helpers


def incident():
    return json.loads((Path(__file__).parent / 'fixtures/target-punctuation-incident.json').read_text())


def proposal(request, result, *, agree):
    raw = scope_helpers.RetryScopeTests().wire({'results': [copy.deepcopy(result)]}, request)
    atom = raw['results'][0]['identified_obligations'][0]
    if agree:
        atom['target'] = {'primary_target_ref': atom['primary_obligation_id']}
    return enveloped(raw)


def compile_response(raw, request):
    return compile_source_reference_response(raw, request, native.OBLIGATION_COVERAGE_SCHEMA,
        coverage=True, provider_nullable_optionals=True)


def test_historical_literal_disagreement_is_not_rewritten_and_explicit_selection_is_audited():
    data = incident()
    request = {'run_id': 'new-offline-identity', 'checks': [data['check']]}
    for agree in (False, True):
        raw = proposal(request, data['compiled_result'], agree=agree)
        saved = copy.deepcopy((raw, request))
        compiled, receipt = compile_response(raw, request)
        atom = compiled['results'][0]['identified_obligations'][0]
        if not agree:
            assert compiled['results'][0] == data['compiled_result']
            with pytest.raises(native.TypedSourceAtomAlignmentError):
                native.validate_obligation_coverage_response(compiled, request['checks'])
            assert 'target_selection' not in receipt['selections'][0]['obligations'][0]
        else:
            assert atom['target'] == '一级学科：'
            native.validate_obligation_coverage_response(compiled, request['checks'])
            proof = receipt['selections'][0]['obligations'][0]['target_selection']
            assert proof['agreement_selected_by_reviewer'] is True
            assert proof['mechanical_equivalence_claimed'] is False
            assert proof['primary_sha256'] == sha256_json(request['checks'][0]['review_context']['primary_obligations'][0])
        assert (raw, request) == saved


def test_generated_native_choices_remain_source_and_primary_scoped():
    data = incident()
    check = data['check']; check['check_id'] = 'unseen-other-label'
    primary = check['review_context']['primary_obligations'][0]
    primary['id'] = 'current-atom-elsewhere'
    result = data['compiled_result']; result['check_id'] = check['check_id']
    result['identified_obligations'][0]['primary_obligation_id'] = primary['id']
    request = {'run_id': 'independent-other-run', 'checks': [check]}
    schema = source_inventory_generation_schema(source_reference_schema(native.OBLIGATION_COVERAGE_SCHEMA,
        build_source_reference_packet(request), coverage=True, constrain_requirement_links=True))
    provider = native_output_schema(schema)
    assert native_schema_support_errors(provider) == []
    # Native adds nullable optional fields; this fixture uses canonical omission.
    for agree in (False, True):
        raw = proposal(request, result, agree=agree)
        assert validate_instance(raw, schema) == []
    for mutation in ('foreign_ref', 'missing_mapping', 'extra_target', 'foreign_source', 'cross_mapping'):
        raw = proposal(request, result, agree=True)
        atom = raw['results'][0]['identified_obligations']['first']
        if mutation == 'foreign_ref': atom['target']['primary_target_ref'] = 'stale-atom'
        elif mutation == 'missing_mapping': atom.pop('primary_obligation_id')
        elif mutation == 'extra_target': atom['target']['text'] = 'silently changed'
        elif mutation == 'foreign_source': atom['source_ref'] = 'wrong-source'
        else: atom['primary_obligation_id'] = 'another-atom'
        assert validate_instance(raw, schema)
        with pytest.raises(ValueError): compile_response(raw, request)


@pytest.mark.parametrize('field,value', [('actor', 'other actor'), ('action', 'delete'),
    ('force', 'optional'), ('applicability', 'unknown'), ('condition', 'Only sometimes')])
def test_target_agreement_cannot_hide_other_semantic_disagreement(field, value):
    data = incident(); request = {'run_id': 'new', 'checks': [data['check']]}
    raw = proposal(request, data['compiled_result'], agree=True)
    raw['results'][0]['identified_obligations']['first'][field] = value
    compiled, _ = compile_response(raw, request)
    with pytest.raises(native.NativeSemanticReviewError):
        native.validate_obligation_coverage_response(compiled, request['checks'])


def test_retained_reference_is_locked_and_foreign_candidate_is_not_replayable(tmp_path):
    data = incident(); check = data['check']
    sibling = copy.deepcopy(check); sibling['check_id'] = 'other-check'
    request = {'run_id': 'offline-current', 'provider_attempt': 1, 'provenance': {'run_id': 'offline-current'},
        'checks': [check, sibling]}
    result = data['compiled_result']
    raw = proposal(request, result, agree=False)
    other = copy.deepcopy(result); other['check_id'] = 'other-check'
    raw['results'].extend(proposal(request, other, agree=True)['results'])
    compiled, receipt = compile_response(raw, request)
    base = tmp_path / 'review'; base.mkdir()
    for name, value in [('request', request), ('raw-response', raw), ('compiled-response', compiled),
                        ('source-reference-packet', build_source_reference_packet(request)),
                        ('source-reference-compilation', receipt)]:
        (base / (name + '.json')).write_text(json.dumps(value))
    with pytest.raises(native.TypedSourceAtomAlignmentError) as caught:
        native.validate_obligation_coverage_response(copy.deepcopy(compiled), request['checks'])
    exc = caught.value
    retry = {**copy.deepcopy(request), 'provider_attempt': 2, 'retry_feedback': {
        'code': exc.code, 'clause_ids': list(exc.clause_ids), 'disagreements': exc.disagreements,
        'checks_sha256': sha256_json(request['checks']), 'candidate_response_sha256': 'a' * 64,
        'run_id': request['run_id'], 'provenance': copy.deepcopy(request['provenance'])}}
    locks, proof = prepare_retry_scope(retry, tmp_path / 'review-provider-attempt-02',
        native.OBLIGATION_COVERAGE_SCHEMA, provider_nullable_optionals=True)
    assert proof['retained_check_ids'] == ['other-check']
    assert isinstance(locks['other-check']['identified_obligations'][0]['target'], dict)
    corrected = proposal(retry, result, agree=True)
    corrected['results'].extend(enveloped({'results': [locks['other-check']]})['results'])
    schema = constrain_retry_schema(source_reference_schema(native.OBLIGATION_COVERAGE_SCHEMA,
        build_source_reference_packet(retry), coverage=True, constrain_requirement_links=True), locks)
    validate_retry_scope(corrected, schema, locks, native=True)
    changed = copy.deepcopy(corrected)
    changed['results'][1]['identified_obligations']['first']['target'] = '一级学科：'
    with pytest.raises(native.NativeSemanticReviewError): validate_retry_scope(changed, schema, locks, native=True)
    # Even semantically equal string/reference substitutions are not allowed
    # to rewrite a retained producer payload during a scoped retry.
    stale = copy.deepcopy(request)
    stale['checks'][0]['review_context']['primary_obligations'][0]['target'] = 'foreign target'
    with pytest.raises(ValueError): compile_response(raw, stale)


def test_native_correction_persistence_and_both_consumers_replay_selection(tmp_path):
    directory = tmp_path / 'packet'
    _, chunk = bridge_helpers.HostAgentBridgeTests()._packet(directory, source='一级学科', contract_version='3.0')
    data = incident(); ctx = data['check']['review_context']
    candidate = {'contract_version': '3.0', 'provenance': chunk['provenance'],
        'clause_reviews': [{'clause_id': 'C1', 'classification': 'executable', 'reason': 'Source label.',
            'normative_basis': 'template_structure', 'obligations': copy.deepcopy(ctx['primary_obligations'])}],
        'requirements': [{'role': 'cover_field_label', 'properties': {'text': '一级学科：'},
            'clause_ids': ['C1'], 'evidence_ids': ['E1']}], 'unsupported_items': [], 'reported_conflicts': []}
    saved = copy.deepcopy(candidate)
    request = native.build_obligation_coverage_request(candidate, chunk, run_id=chunk['provenance']['run_id'], chunk_index=1)
    request.update(attempt=1, provider_attempt=1)
    first = data['compiled_result']; first['check_id'] = 'C1'
    first['identified_obligations'][0]['requirement_refs'] = [request['checks'][0]['review_context']['linked_requirements'][0]['requirement_ref']]
    with pytest.raises(native.TypedSourceAtomAlignmentError) as caught:
        native.validate_obligation_coverage_response({'results': [copy.deepcopy(first)]}, request['checks'])
    exc = caught.value
    retry = {**copy.deepcopy(request), 'provider_attempt': 2, 'retry_feedback': {
        'code': exc.code, 'clause_ids': list(exc.clause_ids), 'disagreements': exc.disagreements,
        'checks_sha256': sha256_json(request['checks']), 'candidate_response_sha256': sha256_json(candidate),
        'run_id': request['run_id'], 'provenance': copy.deepcopy(request['provenance'])}}
    raws = [proposal(request, first, agree=False), proposal(retry, first, agree=True)]
    with ExitStack() as stack:
        transport_helpers.EmptyInventoryVerdictTests().mock_transport(stack, raws)
        stack.enter_context(patch.object(bridge.time, 'sleep'))
        pointer = bridge._run_independent_obligation_coverage_review(candidate, chunk,
            review_dir=directory, run_id=request['run_id'], chunk_index=1, attempt=1, host_runtime='codex',
            model='gpt-6-luna', timeout=5, agent_id='main', runner='exec', binary='codex',
            config_path=None, controller=bridge.RunController())
    assert pointer['provider_attempt'] == 2 and candidate == saved
    envelope = json.loads((directory / pointer['audit_path']).read_text())
    bridge._validate_completed_obligation_ledger_chain(directory, envelope, pointer, candidate, chunk, chunk_index=1, attempt=1)
    full = json.loads((directory / 'llm-request.json').read_text())
    candidate_path = directory / 'accepted-candidate.json'; bridge._write_json(candidate_path, candidate)
    audit = {'chunk_count': 1, 'adapter_id': 'codex', 'host_runtime': 'codex',
        'chunk_lifecycle': [{'chunk_index': 1, 'status': 'completed', 'remote_operation_state': 'completed'}],
        'chunk_runs': [{'chunk_index': 1, 'response_path': str(candidate_path),
            'accepted_response_sha256': sha256_json(candidate), 'independent_obligation_review': pointer}]}
    def consume():
        return pipeline._validate_independent_obligation_receipts(audit=audit, review_root=directory,
            expected_run_id=request['run_id'], expected_request_body_sha=request_body_sha256(full),
            expected_request_envelope_sha=None, expected_request_file_sha=None)
    assert len(consume()) == 1
    review = directory / 'independent-review-chunk-0001-attempt-01-provider-attempt-02'
    assert json.loads((review / 'raw-response.json').read_text()) == raws[1]
    compiled = json.loads((review / 'compiled-response.json').read_text())
    compiled['results'][0]['identified_obligations'][0]['target'] = 'different target'
    bridge._write_json(review / 'compiled-response.json', compiled)
    with pytest.raises(ValueError): consume()
    with pytest.raises(ValueError):
        bridge._validate_completed_obligation_ledger_chain(directory, envelope, pointer, candidate, chunk, chunk_index=1, attempt=1)
