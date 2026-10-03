"""Portable nonempty shape, captured failures and lossless canonical replay."""
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
from semantic_contract import sha256_json
from semantic_source_references import (
    INVENTORY_ENVELOPE_PROTOCOL, build_source_reference_packet,
    compile_source_reference_response, decode_source_inventory_envelopes,
    source_inventory_generation_schema, source_reference_schema,
)
from tests.test_empty_inventory_verdict import enveloped

class SourceInventoryGenerationTests(unittest.TestCase):
    def schemas(self, request):
        packet = build_source_reference_packet(request)
        wire = source_reference_schema(native.OBLIGATION_COVERAGE_SCHEMA, packet,
            coverage=True, constrain_requirement_links=True)
        generated = source_inventory_generation_schema(wire)
        return packet, wire, generated, native_output_schema(generated)

    def test_confirmed_linked_structural_inventory_is_portably_nonempty(self):
        for source in ("某大学学位论文原创性声明", "Declaration of originality"):
            request = {"run_id": "new-template-run", "checks": [{
                "check_id": "dynamic-declaration-heading", "document_text": source,
                "review_context": {"classification": "covered", "primary_normative_basis": "template_structure",
                    "requires_requirement": True, "linked_requirements": [{
                        "requirement_ref": "RR-current-heading", "role": "declarations",
                        "properties": {"items": [{"heading": source}]},
                    }]},
            }]}
            original = copy.deepcopy(request)
            packet, wire, generation, provider = self.schemas(request)
            self.assertEqual(native_schema_support_errors(provider), [])
            ref = packet["checks"][0]["source_spans"][0]["ref_id"]
            raw = {"results": [{"check_id": "dynamic-declaration-heading", "verdict": "consistent",
                "rationale": "The source heading is preserved by the linked declaration.",
                "evidence_refs": [ref], "identified_obligations": {"first": None, "remaining": []}}]}
            with self.subTest(source=source):
                self.assertTrue(validate_instance(raw, generation))
                self.assertTrue(validate_instance(raw, provider))
                # Historical/raw evidence remains readable, not rewritten.
                compiled, _ = compile_source_reference_response(raw, request,
                    native.OBLIGATION_COVERAGE_SCHEMA, coverage=True)
                with self.assertRaises(native.MissingSourceObligationInventoryError):
                    native.validate_obligation_coverage_response(compiled, request["checks"])
                atom = {"source_ref": ref, "disposition": "represented", "requirement_refs": ["RR-current-heading"]}
                raw["results"][0]["identified_obligations"]["first"] = atom
                self.assertEqual(validate_instance(raw, generation), [])
                atom_schema = wire["properties"]["results"]["items"]["anyOf"][0]["properties"]["identified_obligations"]["items"]
                # Strict-provider null sentinels do not add semantic fields.
                props = atom_schema.get("properties") or atom_schema["anyOf"][0]["properties"]
                for field in props:
                    atom.setdefault(field, None)
                raw["results"][0]["identified_obligations"]["first"] = atom
                self.assertEqual(validate_instance(raw, provider), [])
                compiled, _ = compile_source_reference_response(raw, request,
                    native.OBLIGATION_COVERAGE_SCHEMA, coverage=True, provider_nullable_optionals=True)
                self.assertEqual(native.validate_obligation_coverage_response(compiled, request["checks"])[0]["verdict"], "consistent")
                self.assertEqual(compiled["results"][0]["identified_obligations"][0]["source_quote"], source)
                for field, value in (("source_ref", "foreign-source"), ("requirement_refs", ["RR-old"]),
                                     ("disposition", "unrepresented")):
                    bad = copy.deepcopy(raw)
                    bad["results"][0]["identified_obligations"]["first"][field] = value
                    if field == "disposition":
                        bad["results"][0]["identified_obligations"]["first"]["requirement_refs"] = []
                        candidate, _ = compile_source_reference_response(bad, request,
                            native.OBLIGATION_COVERAGE_SCHEMA, coverage=True, provider_nullable_optionals=True)
                        with self.assertRaises(native.NativeSemanticReviewError):
                            native.validate_obligation_coverage_response(candidate, request["checks"])
                    else:
                        self.assertTrue(validate_instance(bad, provider))
                self.assertEqual(request, original)

    def test_informational_heading_is_not_forced_by_presence_or_primary_links(self):
        for linked in ([], [{"requirement_ref": "RR-primary-not-proof"}]):
            request = {"checks": [{"check_id": "description-only", "document_text": "研究概况",
                "review_context": {"classification": "informational", "requires_requirement": True,
                    "linked_requirements": linked}}]}
            packet, _, generation, provider = self.schemas(request)
            raw = {"results": [{"check_id": "description-only", "verdict": "consistent", "rationale": "Only descriptive context.",
                "evidence_refs": [packet["checks"][0]["source_spans"][0]["ref_id"]],
                "identified_obligations": {"first": None, "remaining": []}}]}
            self.assertEqual(validate_instance(raw, generation), [])
            self.assertEqual(validate_instance(raw, provider), [])
            compiled, _ = compile_source_reference_response(raw, request, native.OBLIGATION_COVERAGE_SCHEMA, coverage=True)
            self.assertEqual(native.validate_obligation_coverage_response(compiled, request["checks"])[0]["identified_obligations"], [])

    def test_structural_inventory_prompt_preserves_independent_interpretation(self):
        request = {"protocol": native.OBLIGATION_COVERAGE_PROTOCOL, "checks": [],
            "retry_feedback": {"code": native.MissingSourceObligationInventoryError.code, "clause_ids": ["new-heading-id"]}}
        prompt = native._prompt(request)
        self.assertIn("structural or literal-preservation requirement", prompt)
        self.assertIn("rationale saying the title is already preserved cannot replace the inventory entry", prompt)
        self.assertIn("reject unsupported structural interpretations", prompt)
        self.assertIn("Never invent an obligation", prompt)
        self.assertIn("with no source duty may still have an empty inventory", prompt)

    def test_both_captured_eight_check_attempts_cannot_generate_empty_uncertain(self):
        case = json.loads((ROOT / "tests/fixtures/empty-inventory-generation-incident.json").read_text())
        for directory in ("independent-review-chunk-0004-attempt-01",
                          "independent-review-chunk-0004-attempt-01-provider-attempt-02"):
            request = case["artifacts"][directory + "/request.json"]["content"]
            raw = case["artifacts"][directory + "/raw-response.json"]["content"]
            compiled = case["artifacts"][directory + "/compiled-response.json"]["content"]
            original = copy.deepcopy(raw)
            packet, _, generation, provider = self.schemas(request)
            self.assertEqual(native_schema_support_errors(provider), [])
            self.assertEqual(len(request["checks"]), 8)
            candidate = enveloped(raw)
            for index in range(8):
                with self.subTest(directory=directory, index=index):
                    single = {"results": [candidate["results"][index]]}
                    self.assertTrue(validate_instance(single, provider))
            replay, proof = compile_source_reference_response(candidate, request,
                native.OBLIGATION_COVERAGE_SCHEMA, coverage=True, provider_nullable_optionals=True)
            self.assertEqual(replay, compiled)  # No semantic edit to make it pass.
            with self.assertRaises(native.EmptyInventoryVerdictError) as caught:
                native.validate_obligation_coverage_response(replay, request["checks"])
            self.assertEqual(len(caught.exception.clause_ids), 8)
            self.assertEqual(proof["inventory_shape_projection"]["policy"], INVENTORY_ENVELOPE_PROTOCOL)
            for item in candidate["results"]:
                item["verdict"] = "consistent"  # Synthetic independent choice, not a code repair.
            self.assertEqual(validate_instance(candidate, generation), [])
            self.assertEqual(validate_instance(candidate, provider), [])
            self.assertEqual(raw, original)
            self.assertEqual(packet["checks"][0]["document_text"], request["checks"][0]["document_text"])

    def test_all_diagnostic_verdicts_have_portable_required_first(self):
        request = {"run_id": "different-run", "checks": [{"check_id": "different-template-id",
            "document_text": "物理对象不明确，约3cm。", "review_context": {
                "classification": "unresolved", "linked_requirements": []}}]}
        packet, _, _, provider = self.schemas(request)
        result = {"check_id": "different-template-id", "rationale": "No source target determined.",
                  "evidence_refs": [packet["checks"][0]["source_spans"][0]["ref_id"]],
                  "identified_obligations": {"first": None, "remaining": []}}
        for verdict in native.OBLIGATION_COVERAGE_SCHEMA["properties"]["results"]["items"]["properties"]["verdict"]["enum"]:
            result["verdict"] = verdict
            with self.subTest(verdict=verdict):
                errors = validate_instance({"results": [result]}, provider)
                self.assertEqual(bool(errors), verdict != "consistent")

    def test_nonempty_ambiguity_compiles_in_order_without_source_or_verdict_edits(self):
        request = {"run_id": "another-run", "checks": [{"check_id": "free-id",
            "document_text": "Unknown target, 3 cm. Another unknown target.",
            "review_context": {"classification": "unresolved", "linked_requirements": []}}]}
        packet, wire, generation, provider = self.schemas(request)
        refs = packet["checks"][0]["source_spans"]
        atoms = [{"source_ref": span["ref_id"], "disposition": "ambiguous", "requirement_refs": []}
                 for span in refs[1:]]
        raw = {"results": [{"check_id": "free-id", "verdict": "uncertain", "rationale": "Targets remain ambiguous.",
            "evidence_refs": [refs[0]["ref_id"]], "identified_obligations": atoms}]}
        item_schema = wire["properties"]["results"]["items"]["anyOf"][0]["properties"]["identified_obligations"]["items"]
        # Populate only the strict provider's omission sentinels, not semantic fields.
        for atom in atoms:
            for field in item_schema["properties"]:
                atom.setdefault(field, None)
        raw = enveloped(raw)
        original = copy.deepcopy(raw)
        self.assertEqual(validate_instance(raw, provider), [])
        compiled, proof = compile_source_reference_response(raw, request,
            native.OBLIGATION_COVERAGE_SCHEMA, coverage=True, provider_nullable_optionals=True)
        self.assertEqual(compiled["results"][0]["verdict"], "uncertain")
        self.assertEqual([a["source_quote"] for a in compiled["results"][0]["identified_obligations"]],
                         [s["text"] for s in refs[1:]])
        self.assertEqual(proof["raw_response_sha256"], sha256_json(raw))
        self.assertEqual(raw, original)
        # Review validity is not execution readiness: preserve real ambiguity.
        validated = native.validate_obligation_coverage_response(compiled, request["checks"])
        self.assertEqual(validated[0]["verdict"], "uncertain")
        self.assertTrue(all(a["disposition"] == "ambiguous" for a in validated[0]["identified_obligations"]))

    def test_closed_shape_null_head_and_foreign_source_do_not_drop_payload(self):
        for envelope in ({"first": None, "remaining": [{}]}, {"first": None, "remaining": [], "hidden": 1},
                         {"first": [], "remaining": []}, {"first": {}, "remaining": [None]},
                         {"first": None}):
            raw = {"results": [{"check_id": "x", "identified_obligations": envelope}]}
            original = copy.deepcopy(raw)
            with self.subTest(envelope=envelope), self.assertRaises(ValueError):
                decode_source_inventory_envelopes(raw)
            self.assertEqual(raw, original)
        request = {"checks": [{"check_id": "x", "document_text": "Unknown target.",
            "review_context": {"classification": "unresolved", "linked_requirements": []}}]}
        packet, _, _, _ = self.schemas(request)
        raw = {"results": [{"check_id": "x", "verdict": "uncertain", "rationale": "Unresolved.",
            "evidence_refs": [packet["checks"][0]["source_spans"][0]["ref_id"]],
            "identified_obligations": {"first": {"source_ref": "old-or-foreign", "disposition": "ambiguous", "requirement_refs": []},
                                       "remaining": []}}]}
        with self.assertRaises(ValueError):
            compile_source_reference_response(raw, request, native.OBLIGATION_COVERAGE_SCHEMA, coverage=True)

    def test_historical_arrays_and_noncoverage_schema_are_unchanged(self):
        raw = {"results": [{"check_id": "any-id", "identified_obligations": []}]}
        self.assertEqual(decode_source_inventory_envelopes(raw), (raw, []))
        request = {"checks": [{"check_id": "x", "document_text": "Readable passage."}]}
        wire = source_reference_schema(native.RESPONSE_SCHEMA, build_source_reference_packet(request), coverage=False)
        self.assertNotIn("identified_obligations", wire["properties"]["results"]["items"]["anyOf"][0]["properties"])

    def test_retained_inventory_minimum_and_head_are_not_reopened(self):
        from independent_retry_scope import constrain_retry_schema, validate_retry_scope
        request = {"checks": [{"check_id": "locked", "document_text": "Unknown target. Next unknown target.",
            "review_context": {"classification": "unresolved", "linked_requirements": []}}]}
        packet, wire, _, _ = self.schemas(request)
        spans = packet["checks"][0]["source_spans"]
        atoms = [{"source_ref": s["ref_id"], "disposition": "ambiguous", "requirement_refs": []} for s in spans[1:]]
        result = {"check_id": "locked", "verdict": "uncertain", "rationale": "Actual source ambiguity.",
                  "evidence_refs": [spans[0]["ref_id"]], "identified_obligations": atoms}
        locks = {"locked": result}
        scoped = constrain_retry_schema(wire, locks)
        generation = source_inventory_generation_schema(scoped, retained_results=locks)
        provider = native_output_schema(generation)
        raw = enveloped({"results": [result]})
        self.assertEqual(validate_instance(raw, generation), [])
        self.assertEqual(validate_instance(raw, provider), [])
        for kind in ("null-head", "swapped", "extra-tail"):
            bad = copy.deepcopy(raw)
            envelope = bad["results"][0]["identified_obligations"]
            if kind == "null-head":
                envelope.update(first=None, remaining=[])
            elif kind == "swapped":
                envelope["first"], envelope["remaining"][0] = envelope["remaining"][0], envelope["first"]
            else:
                envelope["remaining"].append(copy.deepcopy(envelope["remaining"][0]))
            with self.subTest(kind=kind):
                self.assertTrue(validate_instance(bad, generation))
                if kind != "extra-tail":
                    self.assertTrue(validate_instance(bad, provider))
                # Native arrays still cannot lock every tail count/order.
                # The unchanged equality consumer remains authoritative.
                with self.assertRaises(native.NativeSemanticReviewError):
                    validate_retry_scope(bad, scoped, locks, native=True)


if __name__ == "__main__":
    unittest.main()
