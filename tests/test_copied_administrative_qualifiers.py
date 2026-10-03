"""Copied qualifiers lose neither original duties nor requirement identities."""
import copy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))
import host_agent_bridge as bridge
import test_administrative_relation_projection as generated
from administrative_relation_projection import project_copied_administrative_qualifiers
from requirements_engine import build_llm_request


def project(candidate, chunk):
    return project_copied_administrative_qualifiers(candidate, chunk,
        validate=bridge.validate_host_agent_response)


def incident():
    case = json.loads((ROOT / 'tests/fixtures/copied-administrative-qualifiers.json').read_text())
    chunk = case['chunk']
    request = build_llm_request([], chunk['clauses'],
        {'evidence': list(chunk['evidence_context'].values())}, {}, 'full', contract_version='3.0')
    request.update(chunk)
    request.update(case_id='current-case', batch={'index': 3},
        runtime_context={'code_fingerprint_sha256': 'f' * 64})
    return case['candidate'], request


def dynamic(variant=0):
    raw, chunk, _ = generated.fixture(variant)
    baseline, _ = bridge.prepare_native_response_candidate(raw, chunk)
    source = next(c for c in chunk['clauses'] if c['text'] == '申请编号')
    duplicate = copy.deepcopy(baseline['requirements'][0])
    duplicate['clause_ids'] = [source['id']]
    duplicate['evidence_ids'] = source['evidence_ids'][:]
    duplicate['field_key'] = 'separate-current-field-instance'
    duplicate['input_prerequisites'] = [{'kind': 'metadata',
        'key': 'thesis_profile.security_level', 'required': True, 'reason': 'Current input scope'}]
    admin = duplicate['properties']['non_public_administration']
    duplicate['properties']['fields'] = []
    admin['fields'] = [{**f, 'order': 1} for f in admin['fields'] if f['id'] == 'approval_number']
    admin['source_region'] = source['text']
    duplicate['verification']['checks'].append('Keep this unique field check')
    baseline['requirements'].append(duplicate)
    return baseline, chunk


class CopiedAdministrativeQualifierTests(unittest.TestCase):
    def assert_preserved(self, old, new, audit):
        self.assertEqual(len(old['requirements']), len(new['requirements']))
        self.assertEqual(old['clause_reviews'], new['clause_reviews'])
        expected = copy.deepcopy(old)
        for repair in audit:
            props = expected['requirements'][repair['requirement_index']]['properties']['non_public_administration']
            for copy_record in repair['removed_copies']:
                if copy_record['path'] == 'publication_default_policy':
                    self.assertEqual(props.pop('publication_default_policy'), copy_record['value'])
                else:
                    index = int(copy_record['path'].split('[')[1].split(']')[0])
                    self.assertEqual(props['security_marking_options'][index].pop('shorter_duration_allowed'), copy_record['value'])
            self.assertFalse(repair['submission_ready'])
            self.assertTrue(repair['independent_review_required'])
        self.assertEqual(expected, new)

    def test_captured_retry_is_a_bounded_patch_not_a_replacement_graph(self):
        raw, chunk = incident(); original = copy.deepcopy(raw)
        errors = bridge.validate_host_agent_response(raw, chunk)
        self.assertEqual(len(errors), 4)
        # The two free-text exception lists differ: code must not assert equivalence.
        self.assertEqual(project(raw, chunk), (None, []))
        proposed = json.loads((ROOT / 'tests/fixtures/copied-administrative-qualifiers.json').read_text())['model_retry']
        records = bridge.contract_error_records(errors, response=raw, chunk=chunk)
        repaired, receipt = bridge._project_validator_targeted_obligation_fields(raw, proposed, records, chunk=chunk)
        audit = receipt['source_bound_repairs']
        self.assertIsNotNone(repaired)
        self.assertEqual(bridge.validate_host_agent_response(repaired, chunk), [])
        self.assert_preserved(raw, repaired, audit)
        self.assertEqual(raw, original)
        self.assertTrue(receipt['discarded_unrequested_paths'])
        self.assertEqual(receipt['policy'], 'validator_targeted_administrative_qualifiers_v1')
        authorizations = []
        error, changed = bridge._retry_semantic_change_error(raw, repaired, records,
            contract_version='3.0', chunk=chunk, authorization_out=authorizations)
        self.assertIsNone(error)
        self.assertEqual(len(authorizations), 4)
        prepared, preparation_audit = bridge.prepare_native_response_candidate(repaired, chunk)
        # Candidate preparation may canonicalize an unreferenced informational
        # zero-inventory representation; it must make no other payload change.
        self.assertEqual(prepared, bridge.normalize_native_response(repaired, chunk['response_schema']))
        self.assertEqual(raw, original)
        self.assertEqual(project(repaired, chunk), (None, []))

    def test_distinct_extracted_sources_preserve_different_terms_and_identity(self):
        for variant in (0, 1):
            with self.subTest(variant=variant):
                raw, chunk = dynamic(variant)
                repaired, audit = project(raw, chunk)
                self.assertIsNotNone(repaired)
                self.assert_preserved(raw, repaired, audit)
                self.assertEqual(repaired['requirements'][0], raw['requirements'][0])
                self.assertEqual(repaired['requirements'][-1]['field_key'], 'separate-current-field-instance')
                self.assertEqual(bridge.validate_host_agent_response(repaired, chunk), [])

    def test_changed_source_scope_values_or_pending_atoms_are_not_copies(self):
        for change in ('source_hash', 'source_offset', 'foreign_evidence', 'different_scope',
                       'different_value', 'pending', 'different_table', 'ambiguous_anchor',
                       'reported_conflict', 'stale_response_provenance', 'unrelated_error'):
            with self.subTest(change=change):
                raw, chunk = dynamic()
                target = raw['requirements'][-1]
                source = next(c for c in chunk['clauses'] if c['id'] == target['clause_ids'][0])
                if change == 'source_hash': source['source_span']['source_sha256'] = '0' * 64
                elif change == 'source_offset': source['source_span']['start_offset'] += 1
                elif change == 'foreign_evidence': target['evidence_ids'] = ['foreign']
                elif change == 'different_scope': target['properties']['non_public_administration']['applicability']['conditions'][0]['value'] = ['classified']
                elif change == 'different_value': target['properties']['non_public_administration']['security_marking_options'][0]['maximum_duration']['value'] += 1
                elif change == 'pending': next(r for r in raw['clause_reviews'] if r['clause_id'] == source['id'])['obligations'][0]['status'] = 'unverifiable'
                elif change == 'different_table':
                    source['source_span']['location']['table_child_index'] += 1
                elif change == 'ambiguous_anchor': raw['requirements'].insert(0, copy.deepcopy(raw['requirements'][0]))
                elif change == 'reported_conflict': raw['reported_conflicts'] = [{'reason': 'Different current source rule'}]
                elif change == 'stale_response_provenance': raw['provenance'] = {**chunk['provenance'], 'run_id': 'old'}
                else: target['properties']['unknown_property'] = True
                original = copy.deepcopy(raw)
                self.assertEqual(project(raw, chunk), (None, []))
                self.assertEqual(raw, original)

    def test_complete_feedback_is_required_at_production_boundary(self):
        raw, chunk = incident()
        proposed = json.loads((ROOT / 'tests/fixtures/copied-administrative-qualifiers.json').read_text())['model_retry']
        records = bridge.contract_error_records(bridge.validate_host_agent_response(raw, chunk), response=raw, chunk=chunk)
        for incorrect in (records[:-1], [{**r, 'response_sha256': '0' * 64} for r in records]):
            repaired, _ = bridge._project_validator_targeted_obligation_fields(raw, proposed, incorrect, chunk=chunk)
            self.assertIsNone(repaired)

    def test_unauthorized_source_edge_removal_still_fails_retry_gate(self):
        raw, chunk = dynamic(); corrected, _ = project(raw, chunk)
        changed = copy.deepcopy(corrected)
        changed['requirements'][0]['clause_ids'].remove(changed['requirements'][-1]['clause_ids'][-1])
        records = bridge.contract_error_records(bridge.validate_host_agent_response(raw, chunk), response=raw, chunk=chunk)
        error, _ = bridge._retry_semantic_change_error(corrected, changed, records,
            contract_version='3.0', chunk=chunk)
        self.assertIsNotNone(error)

    def test_exception_differences_require_a_primary_proposal_not_automatic_equivalence(self):
        raw, chunk = dynamic()
        raw['requirements'][0]['properties']['non_public_administration']['applicability']['exceptions'] = ['anchor-only exception']
        raw['requirements'][-1]['properties']['non_public_administration']['applicability']['exceptions'] = ['target-only exception']
        self.assertEqual(project(raw, chunk), (None, []))
        raw, chunk = incident()
        proposed = json.loads((ROOT / 'tests/fixtures/copied-administrative-qualifiers.json').read_text())['model_retry']
        records = bridge.contract_error_records(bridge.validate_host_agent_response(raw, chunk), response=raw, chunk=chunk)
        for change in ('not_removed', 'deny_instead_of_remove', 'other_value', 'other_identity', 'other_count'):
            candidate = copy.deepcopy(proposed)
            props = candidate['requirements'][-1]['properties']['non_public_administration']
            if change == 'not_removed': props['publication_default_policy'] = 'unapproved_is_public'
            elif change == 'deny_instead_of_remove': props['security_marking_options'][0]['shorter_duration_allowed'] = False
            elif change == 'other_value': props['security_marking_options'][0]['maximum_duration']['value'] += 1
            elif change == 'other_identity': candidate['requirements'][-1]['field_key'] = 'foreign'
            else: candidate['requirements'].pop(0)
            self.assertIsNone(bridge._project_validator_targeted_obligation_fields(raw, candidate, records, chunk=chunk)[0], change)


if __name__ == '__main__':
    unittest.main()
