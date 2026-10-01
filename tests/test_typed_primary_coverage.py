from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from native_semantic_review import (build_obligation_coverage_request, OBLIGATION_COVERAGE_SCHEMA,
                                    validate_obligation_coverage_response, NativeSemanticReviewError)
from semantic_source_references import build_source_reference_packet, source_reference_schema, compile_source_reference_response
from format_spec_validation import validate_instance
from host_review_schema import native_output_schema, native_schema_support_errors


class TypedPrimaryCoverageTests(unittest.TestCase):
    def incident(self):
        return json.loads((ROOT / "tests/fixtures/typed-primary-coverage-incident.json").read_text())

    def request(self):
        case = self.incident()
        request = build_obligation_coverage_request(
            {"requirements": case["requirements"], "clause_reviews": [case["review"]]},
            {"clauses": [case["clause"]], "evidence_context": case["evidence_context"]},
            run_id="fresh-test-run", chunk_index=1)
        return case, request

    def valid_response(self, case, request):
        result = copy.deepcopy(case["independent_result"])
        result["identified_obligations"][0]["primary_obligation_id"] = case["review"]["obligations"][0]["id"]
        result["identified_obligations"][0]["requirement_refs"] = [request["checks"][0]["review_context"]["linked_requirements"][0]["requirement_ref"]]
        return {"results": [result]}

    def test_real_incident_was_missing_mapping_not_changed_modality(self):
        case, request = self.request()
        primary = case["review"]["obligations"][0]
        reviewer = case["independent_result"]["identified_obligations"][0]
        for key in ("actor", "action", "target", "source_quote", "force", "applicability"):
            self.assertEqual(primary[key], reviewer[key])
        self.assertNotIn("primary_obligation_id", reviewer)
        original = copy.deepcopy(case["independent_result"])
        with self.assertRaisesRegex(NativeSemanticReviewError, "typed-primary mapping missing"):
            validate_obligation_coverage_response({"results": [original]}, request["checks"])
        corrected_protocol = self.valid_response(case, request)  # Test simulation, not provider evidence.
        self.assertEqual(validate_obligation_coverage_response(corrected_protocol, request["checks"])[0]["verdict"], "consistent")

    def test_generation_compilation_and_validator_have_same_typed_mapping_contract(self):
        case, request = self.request()
        packet = build_source_reference_packet(request)
        wire = source_reference_schema(OBLIGATION_COVERAGE_SCHEMA, packet, coverage=True)
        self.assertEqual(native_schema_support_errors(native_output_schema(wire)), [])
        result = copy.deepcopy(self.valid_response(case, request)["results"][0])
        ref = packet["checks"][0]["source_spans"][0]["ref_id"]
        result.pop("machine_obligation_ids")
        result.pop("evidence_quotes"); result["evidence_refs"] = [ref]
        result["identified_obligations"][0].pop("source_quote")
        result["identified_obligations"][0]["source_ref"] = ref
        raw = {"results": [result]}
        self.assertEqual(validate_instance(raw, wire), [])
        compiled, _ = compile_source_reference_response(raw, request, OBLIGATION_COVERAGE_SCHEMA, coverage=True)
        self.assertEqual(validate_obligation_coverage_response(compiled, request["checks"])[0]["verdict"], "consistent")
        raw["results"][0]["identified_obligations"][0]["primary_obligation_id"] = "old-or-foreign-id"
        self.assertTrue(validate_instance(raw, wire))

    def test_mapping_does_not_authorize_modality_condition_target_drift_or_duplicates(self):
        case, request = self.request()
        for key, value in (("primary_obligation_id", "foreign"), ("force", "optional"),
                           ("applicability", "not_applicable"), ("target", "different document"),
                           ("condition", "after external approval")):
            response = self.valid_response(case, request)
            response["results"][0]["identified_obligations"][0][key] = value
            with self.subTest(key=key), self.assertRaises(NativeSemanticReviewError):
                validate_obligation_coverage_response(response, request["checks"])
        response = self.valid_response(case, request)
        response["results"][0]["identified_obligations"].append(copy.deepcopy(response["results"][0]["identified_obligations"][0]))
        with self.assertRaises(NativeSemanticReviewError):
            validate_obligation_coverage_response(response, request["checks"])

    def test_contextual_primary_quote_keeps_original_proof_but_scopes_reviewer_quote(self):
        case = self.incident()
        c = case["clause"]; text = c["source_span"]["text"]
        source = text + "。下一段是上下文。"
        eid = c["source_span"]["evidence_id"]
        c["source_span"]["source_sha256"] = hashlib.sha256(source.encode()).hexdigest()
        c.pop("source_evidence_text", None)
        evidence = case["evidence_context"][eid]; evidence["text"] = source
        primary = case["review"]["obligations"][0]; primary["source_quote"] = source
        original = copy.deepcopy(primary)
        request = build_obligation_coverage_request(
            {"requirements": [], "clause_reviews": [case["review"]]},
            {"clauses": [c], "evidence_context": case["evidence_context"]}, run_id="test-run", chunk_index=1)
        context = request["checks"][0]["review_context"]
        self.assertEqual(primary, original)
        self.assertEqual(context["primary_obligations"][0]["source_quote"], text)
        self.assertLess(len(text), len(source))
        self.assertEqual(context["primary_obligation_quote_bindings"][0]["original_source_quote"], source)
        c["source_span"]["source_sha256"] = "0" * 64
        with self.assertRaises(NativeSemanticReviewError):
            build_obligation_coverage_request({"clause_reviews": [case["review"]]},
                {"clauses": [c], "evidence_context": case["evidence_context"]}, run_id="old", chunk_index=1)

    def test_exact_primary_subquote_is_selectable_without_model_copying(self):
        case, request = self.request()
        primary = request["checks"][0]["review_context"]["primary_obligations"][0]
        primary["source_quote"] = "原创性声明"
        packet = build_source_reference_packet(request)
        self.assertIn("原创性声明", [s["text"] for s in packet["checks"][0]["source_spans"]])

    def test_external_and_mixed_can_report_unmapped_new_duty_but_not_claim_coverage(self):
        for classification in ("external_compliance", "executable_with_external_check"):
            case, request = self.request()
            request["checks"][0]["review_context"]["classification"] = classification
            packet = build_source_reference_packet(request)
            wire = source_reference_schema(OBLIGATION_COVERAGE_SCHEMA, packet, coverage=True)
            ref = packet["checks"][0]["source_spans"][0]["ref_id"]
            raw = {"results": [{"check_id": case["clause"]["id"], "verdict": "incomplete",
                   "rationale": "An additional source duty is unrepresented.", "evidence_refs": [ref],
                   "identified_obligations": [{"source_ref": ref, "disposition": "unrepresented", "requirement_refs": []}]}]}
            with self.subTest(classification=classification):
                self.assertEqual(validate_instance(raw, wire), [])
                raw["results"][0]["identified_obligations"][0]["disposition"] = "represented"
                self.assertTrue(validate_instance(raw, wire))
                raw["results"][0]["identified_obligations"][0]["disposition"] = "external_action_pending"
                self.assertTrue(validate_instance(raw, wire))

    def test_scope_unresolved_typed_atom_has_optional_current_mapping(self):
        case, request = self.request()
        ctx = request["checks"][0]["review_context"]
        ctx.update(classification="unresolved", linked_requirements=[], manual_review_codes=["abstract_translation_target_conflict"])
        from native_semantic_review import _SCOPE_DEPENDENCY_CODES
        ctx["manual_review_codes"] = [_SCOPE_DEPENDENCY_CODES[0]]
        packet = build_source_reference_packet(request)
        wire = source_reference_schema(OBLIGATION_COVERAGE_SCHEMA, packet, coverage=True)
        branches = wire["properties"]["results"]["items"]["anyOf"][0]["properties"]["identified_obligations"]["items"]["anyOf"]
        scope = next(b for b in branches if b["properties"]["disposition"].get("enum") == ["scope_unresolved"])
        self.assertEqual(scope["properties"]["primary_obligation_id"]["enum"], [case["review"]["obligations"][0]["id"]])
        self.assertNotIn("primary_obligation_id", scope["required"])


if __name__ == "__main__":
    unittest.main()
