"""Generation guidance cannot turn a rejected source review into acceptance."""
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
    build_source_reference_packet, source_reference_schema, compile_source_reference_response,
)


def incident():
    return json.loads((ROOT / "tests/fixtures/empty-incomplete-heading-incident.json").read_text())


def wire_result(request, verdict="incomplete", atoms=None):
    check = build_source_reference_packet(request)["checks"][0]
    return {"results": [{"check_id": check["check_id"], "verdict": verdict,
        "rationale": "Independent reading must decide the source duty, not the primary label.",
        "evidence_refs": [check["source_spans"][0]["ref_id"]],
        "identified_obligations": [] if atoms is None else atoms}]}


class CoverageVerdictGenerationTests(unittest.TestCase):
    def test_captured_contradiction_and_renamed_ids_remain_rejected(self):
        for cid in ("C00039", "unseen-school-heading"):
            data = incident()
            check = data["check"]; check["check_id"] = cid
            bad = copy.deepcopy(data["rejected_result"]); bad["check_id"] = cid
            before = copy.deepcopy(bad)
            with self.assertRaises(native.InconsistentObligationVerdictError):
                native.validate_obligation_coverage_response({"results": [bad]}, [check])
            self.assertEqual(bad, before)
            # This is an explicit offline candidate, never a parser rewrite or
            # a live model result; no-duty interpretation remains model-owned.
            proposal = copy.deepcopy(bad); proposal["verdict"] = "consistent"
            self.assertEqual(native.validate_obligation_coverage_response(
                {"results": [proposal]}, [check])[0]["identified_obligations"], [])

    def test_generation_couples_empty_inventory_without_rewriting_raw(self):
        request = {"protocol": native.OBLIGATION_COVERAGE_PROTOCOL,
                   "run_id": "new-source", "checks": [incident()["check"]]}
        packet = build_source_reference_packet(request)
        generation = source_reference_schema(native.OBLIGATION_COVERAGE_SCHEMA, packet,
            coverage=True, constrain_requirement_links=True)
        bad = wire_result(request); saved = copy.deepcopy(bad)
        self.assertNotEqual(validate_instance(bad, generation), [])
        self.assertEqual(validate_instance(wire_result(request, "consistent"), generation), [])
        compiled, receipt = compile_source_reference_response(bad, request,
            native.OBLIGATION_COVERAGE_SCHEMA, coverage=True)
        self.assertEqual(compiled["results"][0]["verdict"], "incomplete")
        self.assertTrue(receipt["semantic_verdicts_unchanged"])
        self.assertEqual(bad, saved)
        with self.assertRaises(native.InconsistentObligationVerdictError):
            native.validate_obligation_coverage_response(compiled, request["checks"])
        provider = native_output_schema(generation)
        self.assertEqual(native_schema_support_errors(provider), [])
        diagnostic = generation["properties"]["results"]["items"]["anyOf"][0]["anyOf"][-1]
        self.assertEqual(diagnostic["properties"]["verdict"]["enum"], ["incomplete"])
        self.assertEqual(diagnostic["properties"]["identified_obligations"]["minItems"], 1)
        # Native decoding drops minItems: never describe it as completeness proof.
        projected = provider["properties"]["results"]["items"]["anyOf"][0]["anyOf"][-1]
        self.assertNotIn("minItems", projected["properties"]["identified_obligations"])
        self.assertIn("minItems", projected["properties"]["identified_obligations"]["description"])

    def test_informational_real_omission_is_still_reportable(self):
        check = copy.deepcopy(incident()["check"])
        check.update(check_id="not-a-heading", document_text="正文应明确解释样本来源。")
        request = {"checks": [check]}; packet = build_source_reference_packet(request)
        ref = packet["checks"][0]["source_spans"][0]["ref_id"]
        raw = wire_result(request, atoms=[{"source_ref": ref,
            "disposition": "unrepresented", "requirement_refs": []}])
        generation = source_reference_schema(native.OBLIGATION_COVERAGE_SCHEMA, packet,
            coverage=True, constrain_requirement_links=True)
        self.assertEqual(validate_instance(raw, generation), [])
        compiled, _ = compile_source_reference_response(raw, request,
            native.OBLIGATION_COVERAGE_SCHEMA, coverage=True)
        self.assertEqual(native.validate_obligation_coverage_response(compiled, [check])[0]["verdict"],
                         "incomplete")

    def test_prompt_does_not_require_all_informational_classifications_to_change(self):
        request = {"protocol": native.OBLIGATION_COVERAGE_PROTOCOL, "checks": [incident()["check"]]}
        prompt = native._prompt(request)
        self.assertIn("Informational is a valid primary classification", prompt)
        self.assertIn("Never return incomplete with an empty inventory", prompt)
        self.assertIn("ONLY when THIS check's exact source", prompt)
        self.assertNotIn("classification must receive the existing bounded classification correction", prompt)
        self.assertIn("Treat an omitted obligation", prompt)
        self.assertIn("never infer absence of materials or delete a chapter", prompt)

    def test_same_candidate_reread_explicitly_allows_source_first_no_duty_conclusion(self):
        prompt = native._prompt({"protocol": native.OBLIGATION_COVERAGE_PROTOCOL,
            "checks": [incident()["check"]], "retry_feedback": {
                "code": native.InconsistentObligationVerdictError.code, "clause_ids": ["C00039"]}})
        self.assertIn("use consistent with an empty", prompt)
        self.assertIn("never authorizes inventing or relabeling", prompt)
        self.assertIn("unchanged candidate", prompt)

    def test_typed_feedback_is_check_scoped_immutable_and_stale_feedback_rejected(self):
        heading = incident()["check"]
        primary = {"id": "fresh-approval", "status": "unverifiable", "actor": "导师",
            "action": "同意", "target": "非公开标注", "source_quote": "导师同意",
            "force": "required", "applicability": "applicable", "condition": None}
        approval = {"check_id": "different-clause", "document_text": "非公开标注须导师同意。",
            "review_context": {"classification": "external_compliance", "requires_requirement": False,
                "linked_requirements": [], "primary_obligations": [primary]}}
        request = {"protocol": native.OBLIGATION_COVERAGE_PROTOCOL, "run_id": "fresh",
            "provenance": {"run_id": "fresh"}, "checks": [heading, approval]}
        feedback = {"code": native.TypedSourceAtomAlignmentError.code,
            "clause_ids": [approval["check_id"]], "checks_sha256": sha256_json(request["checks"]),
            "run_id": request["run_id"], "provenance": copy.deepcopy(request["provenance"]),
            "candidate_response_sha256": sha256_json("unchanged-candidate"),
            "disagreements": [{"check_id": approval["check_id"],
                "primary_obligation_id": primary["id"], "primary_sha256": sha256_json(primary),
                "fields": ["target"]}]}
        request["retry_feedback"] = feedback
        before = copy.deepcopy(request)
        prompt = native._prompt(request)
        view = json.loads(prompt.split("Current run-bound audit request:\n", 1)[1])
        self.assertNotIn("retry_feedback", view)
        self.assertNotIn("review_retry_feedback", view["checks"][0])
        scoped = view["checks"][1]["review_retry_feedback"]
        self.assertEqual(scoped["clause_ids"], [approval["check_id"]])
        self.assertEqual(scoped["disagreements"], feedback["disagreements"])
        self.assertIn("applies ONLY", prompt)
        self.assertEqual(request, before)
        request["retry_feedback"]["checks_sha256"] = "old-checks"
        with self.assertRaisesRegex(native.NativeSemanticReviewError, "not current-source bound"):
            native._prompt(request)


if __name__ == "__main__":
    unittest.main()
