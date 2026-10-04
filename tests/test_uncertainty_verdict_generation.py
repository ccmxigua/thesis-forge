"""Keep uncertainty distinct from missing duties at native generation time."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import native_semantic_review as native
from format_spec_validation import validate_instance
from host_review_schema import native_output_schema, native_schema_support_errors
from semantic_source_references import (
    build_source_reference_packet, compile_source_reference_response,
    source_inventory_generation_schema, source_reference_schema,
)


def incident():
    return json.loads((ROOT / "tests/fixtures/uncertain-unrepresented-label-incident.json").read_text())


def prepared(check):
    request = {"run_id": "offline-new-source", "checks": [check]}
    packet = build_source_reference_packet(request)
    wire = source_reference_schema(native.OBLIGATION_COVERAGE_SCHEMA, packet,
        coverage=True, constrain_requirement_links=True)
    generated = source_inventory_generation_schema(wire)
    provider = native_output_schema(generated)
    raw = {"results": [copy.deepcopy(incident()["rejected_raw_result"])]}
    result = raw["results"][0]
    result["check_id"] = check["check_id"]
    ref = packet["checks"][0]["source_spans"][0]["ref_id"]
    result["evidence_refs"] = [ref]
    result["identified_obligations"]["first"]["source_ref"] = ref
    return request, generated, provider, raw


def without_native_nulls(raw):
    result = copy.deepcopy(raw)
    for check in result["results"]:
        inventory = check["identified_obligations"]
        for atom in [inventory["first"], *inventory["remaining"]]:
            if isinstance(atom, dict):
                for key in list(atom):
                    if atom[key] is None:
                        del atom[key]
    return result


class UncertaintyVerdictGenerationTests(unittest.TestCase):
    def test_captured_rejection_is_still_replayable_without_semantic_rewriting(self):
        data = incident()
        rejected = {"results": [data["rejected_compiled_result"]]}
        saved = copy.deepcopy(rejected)
        with self.assertRaisesRegex(native.NativeSemanticReviewError, "uncertainty is not preserved safely"):
            native.validate_obligation_coverage_response(rejected, [data["check"]])
        self.assertEqual(rejected, saved)
        request, _, _, raw = prepared(data["check"])
        before = copy.deepcopy(raw)
        compiled, proof = compile_source_reference_response(raw, request,
            native.OBLIGATION_COVERAGE_SCHEMA, coverage=True, provider_nullable_optionals=True)
        self.assertEqual(compiled, rejected)
        self.assertTrue(proof["semantic_verdicts_unchanged"])
        self.assertEqual(raw, before)

    def test_native_generation_rejects_captured_conflict_and_generic_equivalent(self):
        generic = {"check_id": "unseen-label", "document_text": "处理类别：",
            "review_context": {"classification": "unresolved", "requires_requirement": False,
                "linked_requirements": [], "manual_review_codes": []}}
        for check in (incident()["check"], generic):
            with self.subTest(check_id=check["check_id"]):
                _, generated, provider, raw = prepared(check)
                before = copy.deepcopy(raw)
                self.assertEqual(native_schema_support_errors(provider), [])
                self.assertTrue(validate_instance(without_native_nulls(raw), generated))
                self.assertTrue(validate_instance(raw, provider))
                self.assertEqual(raw, before)

    def test_independent_uncertainty_omission_and_no_duty_remain_distinct_options(self):
        request, generated, provider, raw = prepared(incident()["check"])
        # These are explicit offline alternatives, never repairs to a provider
        # response or evidence that either semantic interpretation is correct.
        for verdict, disposition in (("uncertain", "ambiguous"), ("incomplete", "unrepresented")):
            proposal = copy.deepcopy(raw)
            result = proposal["results"][0]
            result["verdict"] = verdict
            result["identified_obligations"]["first"]["disposition"] = disposition
            self.assertEqual(validate_instance(without_native_nulls(proposal), generated), [])
            self.assertEqual(validate_instance(proposal, provider), [])
            compiled, _ = compile_source_reference_response(proposal, request,
                native.OBLIGATION_COVERAGE_SCHEMA, coverage=True, provider_nullable_optionals=True)
            validated = native.validate_obligation_coverage_response(compiled, request["checks"])
            self.assertEqual(validated[0]["verdict"], verdict)
            self.assertEqual(validated[0]["identified_obligations"][0]["disposition"], disposition)
        empty = copy.deepcopy(raw)
        empty["results"][0].update(verdict="consistent", identified_obligations={"first": None, "remaining": []})
        self.assertEqual(validate_instance(empty, provider), [])
        empty["results"][0]["verdict"] = "uncertain"
        self.assertTrue(validate_instance(empty, provider))

    def test_uncertain_tail_cannot_hide_an_unrepresented_duty(self):
        _, _, provider, raw = prepared(incident()["check"])
        inventory = raw["results"][0]["identified_obligations"]
        inventory["remaining"] = [copy.deepcopy(inventory["first"])]
        inventory["first"]["disposition"] = "ambiguous"
        self.assertTrue(validate_instance(raw, provider))

    def test_generation_does_not_waive_source_or_primary_uncertainty_gates(self):
        check = copy.deepcopy(incident()["check"])
        check["review_context"].update(classification="informational", primary_obligations=[])
        request, _, provider, raw = prepared(check)
        raw["results"][0]["identified_obligations"]["first"]["disposition"] = "ambiguous"
        self.assertEqual(validate_instance(raw, provider), [])
        compiled, _ = compile_source_reference_response(raw, request,
            native.OBLIGATION_COVERAGE_SCHEMA, coverage=True, provider_nullable_optionals=True)
        with self.assertRaises(native.NativeSemanticReviewError):
            native.validate_obligation_coverage_response(compiled, request["checks"])
        raw["results"][0]["identified_obligations"]["first"]["source_ref"] = "foreign-source"
        self.assertTrue(validate_instance(raw, provider))
        with self.assertRaises(ValueError):
            compile_source_reference_response(raw, request, native.OBLIGATION_COVERAGE_SCHEMA,
                coverage=True, provider_nullable_optionals=True)


if __name__ == "__main__":
    unittest.main()
