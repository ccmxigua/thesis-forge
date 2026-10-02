from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from native_semantic_review import (
    OBLIGATION_COVERAGE_SCHEMA,
    RESPONSE_SCHEMA,
    validate_obligation_coverage_response,
)
from host_review_schema import native_output_schema, native_schema_support_errors
from semantic_contract import sha256_json
from format_spec_validation import validate_instance
from semantic_source_references import (
    bind_validated_source_reference_selections,
    build_source_reference_packet,
    compile_source_reference_response,
    source_reference_schema,
    SourceReferenceResponseError,
)


class SemanticSourceReferenceTests(unittest.TestCase):
    def test_all_dispositions_bind_requirement_selectors_to_the_current_check(self):
        request = {"checks": [
            {"check_id": "dynamic-a", "document_text": "年   月", "review_context": {
                "linked_requirements": [{"requirement_ref": "RR-current-a"}]}},
            {"check_id": "dynamic-b", "document_text": "作者签字", "review_context": {
                "linked_requirements": [{"requirement_ref": "RR-current-b"}]}},
            {"check_id": "no-document-link", "document_text": "批准后公开", "review_context": {}},
        ]}
        packet = build_source_reference_packet(request)
        dispositions = ["represented", "unrepresented", "ambiguous", "external_action_pending",
                        "authoring_content_pending", "backend_unsupported", "source_content_verification_pending"]
        for constrained in (False, True):
            schema = source_reference_schema(OBLIGATION_COVERAGE_SCHEMA, packet, coverage=True,
                                            constrain_requirement_links=constrained)
            self.assertEqual(native_schema_support_errors(native_output_schema(schema)), [])
            for check, branch in zip(packet["checks"], schema["properties"]["results"]["items"]["anyOf"]):
                ref = check["source_spans"][0]["ref_id"]
                for disposition in dispositions:
                    for foreign in ("RR-current-b" if check["check_id"] == "dynamic-a" else "RR-current-a",
                                    "RR-current-", "made-up"):
                        result = {"check_id": check["check_id"], "verdict": "incomplete", "rationale": "Rejected source observation.",
                            "evidence_refs": [ref], "identified_obligations": [{"source_ref": ref,
                                "disposition": disposition, "requirement_refs": [foreign]}]}
                        with self.subTest(constrained=constrained, check=check["check_id"], disposition=disposition, foreign=foreign):
                            self.assertTrue(validate_instance(result, branch))

    def test_foreign_diagnostic_ref_enters_wire_error_router_not_semantic_repair(self):
        request = {"checks": [{"check_id": "dynamic-label", "document_text": "年   月", "review_context": {
            "classification": "covered", "requires_requirement": True,
            "linked_requirements": [{"requirement_ref": "RR-live-label"}]}}]}
        packet = build_source_reference_packet(request)
        ref = packet["checks"][0]["source_spans"][0]["ref_id"]
        raw = {"results": [{"check_id": "dynamic-label", "verdict": "consistent", "rationale": "Source label is preserved.",
            "evidence_refs": [ref], "identified_obligations": [{"source_ref": ref,
                "disposition": "unrepresented", "requirement_refs": ["RR-live-labe"]}]}]}
        original = copy.deepcopy(raw)
        with self.assertRaises(SourceReferenceResponseError) as caught:
            compile_source_reference_response(raw, request, OBLIGATION_COVERAGE_SCHEMA, coverage=True)
        self.assertEqual(caught.exception.issues[0]["check_id"], "dynamic-label")
        self.assertEqual(raw, original)
        # A valid selector alone cannot turn an unrepresented duty into coverage.
        raw["results"][0]["identified_obligations"][0]["requirement_refs"] = ["RR-live-label"]
        compiled, _ = compile_source_reference_response(raw, request, OBLIGATION_COVERAGE_SCHEMA, coverage=True)
        from native_semantic_review import NativeSemanticReviewError
        with self.assertRaisesRegex(NativeSemanticReviewError, "verdict conflicts"):
            validate_obligation_coverage_response(compiled, request["checks"])

    def test_external_generation_couples_pending_verdict_and_current_atom_mapping(self) -> None:
        request = {"run_id": "fresh", "checks": [{"check_id": "external-any-id",
            "document_text": "导师同意后学院批准。", "review_context": {
                "classification": "external_compliance", "requires_requirement": False,
                "linked_requirements": [], "primary_obligations": [
                    {"id": "consent", "status": "unverifiable"},
                    {"id": "approval", "status": "unverifiable"}]}}]}
        packet = build_source_reference_packet(request)
        ref = packet["checks"][0]["source_spans"][0]["ref_id"]
        wire = source_reference_schema(OBLIGATION_COVERAGE_SCHEMA, packet, coverage=True,
                                       constrain_requirement_links=True)
        native = native_output_schema(wire)
        self.assertEqual(native_schema_support_errors(native), [])
        branch = wire["properties"]["results"]["items"]["anyOf"][0]
        self.assertEqual(branch["properties"]["check_id"]["enum"], ["external-any-id"])
        raw = {"results": [{"check_id": "external-any-id", "verdict": "external_compliance_pending",
            "rationale": "External actions still pending.", "evidence_refs": [ref],
            "identified_obligations": [{"source_ref": ref, "disposition": "external_action_pending",
                "primary_obligation_id": oid, "requirement_refs": []} for oid in ("consent", "approval")]}]}
        pending_atom = branch["anyOf"][1]["properties"]["identified_obligations"]["items"]["anyOf"][0]
        provider = copy.deepcopy(raw)
        for atom in provider["results"][0]["identified_obligations"]:
            for key in pending_atom["properties"]:
                if key not in atom:
                    atom[key] = None  # Strict native optional omission sentinel.
        self.assertEqual(validate_instance(raw, wire), [])
        self.assertEqual(validate_instance(provider, native), [])
        compiled, receipt = compile_source_reference_response(provider, request,
            OBLIGATION_COVERAGE_SCHEMA, coverage=True, provider_nullable_optionals=True)
        self.assertEqual(compiled["results"][0]["verdict"], "external_compliance_pending")
        self.assertTrue(receipt["semantic_verdicts_unchanged"])
        for change in ("unrepresented", "ambiguous", "represented", "foreign-id", "missing-id", "wrong-verdict"):
            bad = copy.deepcopy(provider)
            atom = bad["results"][0]["identified_obligations"][0]
            if change in {"unrepresented", "ambiguous", "represented"}:
                atom["disposition"] = change
            elif change == "foreign-id":
                atom["primary_obligation_id"] = "old-id"
            elif change == "missing-id":
                atom["primary_obligation_id"] = None
            else:
                bad["results"][0]["verdict"] = "mixed_execution_external_pending"
            with self.subTest(change=change):
                self.assertTrue(validate_instance(bad, native))
        missing = copy.deepcopy(raw)
        missing["results"][0]["identified_obligations"] = []
        self.assertTrue(validate_instance(missing, wire))
        # Unsupported native minItems cannot establish inventory completeness.
        duplicate = copy.deepcopy(compiled)
        duplicate["results"][0]["identified_obligations"][1]["primary_obligation_id"] = "consent"
        from native_semantic_review import NativeSemanticReviewError
        with self.assertRaises(NativeSemanticReviewError):
            validate_obligation_coverage_response(duplicate, request["checks"])

    def test_external_generation_keeps_truthful_unmapped_diagnostics_and_raw_rejection(self) -> None:
        request = {"checks": [{"check_id": "different-id", "document_text": "本人签名并承担责任。",
            "review_context": {"classification": "external_compliance", "linked_requirements": [],
                "primary_obligations": [{"id": "sign", "status": "unverifiable"}]}}]}
        packet = build_source_reference_packet(request)
        ref = packet["checks"][0]["source_spans"][0]["ref_id"]
        wire = source_reference_schema(OBLIGATION_COVERAGE_SCHEMA, packet, coverage=True,
                                       constrain_requirement_links=True)
        raw = {"results": [{"check_id": "different-id", "verdict": "incomplete", "rationale": "Missing duty.",
            "evidence_refs": [ref], "identified_obligations": [{"source_ref": ref,
                "disposition": "unrepresented", "requirement_refs": []}]}]}
        self.assertEqual(validate_instance(raw, wire), [])
        provider = copy.deepcopy(raw)
        diagnostic_atoms = wire["properties"]["results"]["items"]["anyOf"][0]["anyOf"][0]["properties"]["identified_obligations"]["items"]["anyOf"]
        unmatched = next(atom for atom in diagnostic_atoms if "primary_obligation_id" not in atom["properties"])
        for key in unmatched["properties"]:
            provider["results"][0]["identified_obligations"][0].setdefault(key, None)
        self.assertEqual(validate_instance(provider, native_output_schema(wire)), [])
        diagnostic, _ = compile_source_reference_response(provider, request,
            OBLIGATION_COVERAGE_SCHEMA, coverage=True, provider_nullable_optionals=True)
        self.assertNotIn("primary_obligation_id", diagnostic["results"][0]["identified_obligations"][0])
        combined = copy.deepcopy(raw)
        combined["results"][0]["identified_obligations"].insert(0, {
            "source_ref": ref, "disposition": "external_action_pending",
            "primary_obligation_id": "sign", "requirement_refs": []})
        self.assertEqual(validate_instance(combined, wire), [])
        combined_compiled, _ = compile_source_reference_response(combined, request,
            OBLIGATION_COVERAGE_SCHEMA, coverage=True)
        from native_semantic_review import NativeSemanticReviewError
        with self.assertRaises(NativeSemanticReviewError):
            validate_obligation_coverage_response(combined_compiled, request["checks"])
        # This is a preserved diagnostic, not permission to accept omissions.
        raw["results"][0]["verdict"] = "external_compliance_pending"
        before = copy.deepcopy(raw)
        self.assertTrue(validate_instance(raw, wire))
        # Parsing is intentionally not a repairer or a provider-generation gate.
        # Historical contradictions are preserved for canonical rejection.
        compiled, _ = compile_source_reference_response(raw, request, OBLIGATION_COVERAGE_SCHEMA, coverage=True)
        self.assertEqual(raw, before)
        self.assertEqual(compiled["results"][0]["identified_obligations"][0]["disposition"], "unrepresented")
        changed = copy.deepcopy(request)
        changed["checks"][0]["document_text"] += "新增义务。"
        with self.assertRaises(ValueError):
            compile_source_reference_response(raw, changed, OBLIGATION_COVERAGE_SCHEMA, coverage=True)

    def test_mixed_generation_routes_atoms_to_matching_current_primary_status(self) -> None:
        request = {"checks": [{"check_id": "mixed-any-id", "document_text": "批准后应标注密级。",
            "review_context": {"classification": "executable_with_external_check", "requires_requirement": True,
                "linked_requirements": [{"requirement_ref": "RR-current"}], "primary_obligations": [
                    {"id": "approval", "status": "unverifiable"}, {"id": "label", "status": "covered"}]}}]}
        packet = build_source_reference_packet(request)
        ref = packet["checks"][0]["source_spans"][0]["ref_id"]
        wire = source_reference_schema(OBLIGATION_COVERAGE_SCHEMA, packet, coverage=True,
                                       constrain_requirement_links=True)
        raw = {"results": [{"check_id": "mixed-any-id", "verdict": "mixed_execution_external_pending",
            "rationale": "Split local formatting and external action.", "evidence_refs": [ref],
            "identified_obligations": [
                {"source_ref": ref, "disposition": "external_action_pending", "primary_obligation_id": "approval", "requirement_refs": []},
                {"source_ref": ref, "disposition": "represented", "primary_obligation_id": "label", "requirement_refs": ["RR-current"]}]}]}
        self.assertEqual(validate_instance(raw, wire), [])
        self.assertEqual(native_schema_support_errors(native_output_schema(wire)), [])
        for key, value in (("primary_obligation_id", "label"), ("disposition", "unrepresented"),
                           ("requirement_refs", ["RR-current"])):
            bad = copy.deepcopy(raw)
            bad["results"][0]["identified_obligations"][0][key] = value
            with self.subTest(key=key):
                self.assertTrue(validate_instance(bad, wire))
        diagnostic = copy.deepcopy(raw)
        diagnostic["results"][0]["verdict"] = "incomplete"
        diagnostic["results"][0]["identified_obligations"].append({"source_ref": ref,
            "disposition": "unrepresented", "requirement_refs": []})
        self.assertEqual(validate_instance(diagnostic, wire), [])

    def test_generation_schema_prevents_unlinked_and_cross_check_coverage(self) -> None:
        request = {"checks": [
            {"check_id": "label", "document_text": "论文题目", "review_context": {
                "classification": "informational", "linked_requirements": []}},
            {"check_id": "duty", "document_text": "表格应居中", "review_context": {
                "classification": "covered", "linked_requirements": [{"requirement_ref": "RR-live"}]}},
        ]}
        before = copy.deepcopy(OBLIGATION_COVERAGE_SCHEMA)
        packet = build_source_reference_packet(request)
        wire = source_reference_schema(OBLIGATION_COVERAGE_SCHEMA, packet, coverage=True,
                                       constrain_requirement_links=True)
        self.assertEqual(native_schema_support_errors(native_output_schema(wire)), [])
        self.assertEqual(OBLIGATION_COVERAGE_SCHEMA, before)
        for check in packet["checks"]:
            for disposition, refs, valid in (
                ("represented", [], False),
                ("represented", ["RR-live"], check["check_id"] == "duty"),
                ("represented", ["RR-other-clause"], False),
                ("unrepresented", [], True),
            ):
                with self.subTest(check=check["check_id"], disposition=disposition, refs=refs):
                    source_ref = check["source_spans"][0]["ref_id"]
                    raw = {"results": [{"check_id": check["check_id"], "verdict": "consistent",
                        "rationale": "Source audit.", "evidence_refs": [source_ref],
                        "identified_obligations": [{"source_ref": source_ref,
                            "disposition": disposition, "requirement_refs": refs}]}]}
                    self.assertEqual(not bool(validate_instance(raw, wire)), valid)
            raw["results"][0]["identified_obligations"] = []
            self.assertEqual(validate_instance(raw, wire), [])

    def test_native_minitems_omission_still_requires_canonical_link_guard(self) -> None:
        request = {"checks": [{"check_id": "duty", "document_text": "表格应居中",
            "review_context": {"classification": "covered", "requires_requirement": True,
                "linked_requirements": [{"requirement_ref": "RR-live"}]}}]}
        packet = build_source_reference_packet(request)
        wire = source_reference_schema(OBLIGATION_COVERAGE_SCHEMA, packet, coverage=True,
                                       constrain_requirement_links=True)
        ref = packet["checks"][0]["source_spans"][0]["ref_id"]
        raw = {"results": [{"check_id": "duty", "verdict": "consistent", "rationale": "Claimed.",
            "evidence_refs": [ref], "identified_obligations": [{
                "source_ref": ref, "disposition": "represented", "requirement_refs": []}]}]}
        self.assertTrue(validate_instance(raw, wire))
        # The native projection intentionally omits unsupported minItems.
        # It must not replace the authoritative post-provider semantic guard.
        native = native_output_schema(wire)
        branches = native["properties"]["results"]["items"]["anyOf"]
        obligation = next(b for b in branches[0]["properties"]["identified_obligations"]["items"]["anyOf"]
                          if b["properties"]["disposition"].get("enum") == ["represented"])
        self.assertNotIn("minItems", obligation["properties"]["requirement_refs"])
        compiled, _ = compile_source_reference_response(raw, request, OBLIGATION_COVERAGE_SCHEMA, coverage=True)
        from native_semantic_review import UnlinkedRepresentedObligationError
        with self.assertRaises(UnlinkedRepresentedObligationError):
            validate_obligation_coverage_response(compiled, request["checks"])

    def setUp(self) -> None:
        self.request = {
            "protocol": "test",
            "run_id": "run-1",
            "case_id": "case-1",
            "checks": [
                {"check_id": "C1", "document_text": "甲 年 月。第二句！",
                 "review_context": {"cited_evidence": {"E1": {"text": "甲 年  月。"}}}},
                {"check_id": "C2", "document_text": "乙文有独立职责。"},
            ],
        }

    def _simple_response(self, first_ref: str, second_ref: str) -> dict:
        return {"results": [
            {"check_id": "C1", "verdict": "uncertain", "rationale": "依据原文判断。",
             "evidence_refs": [first_ref]},
            {"check_id": "C2", "verdict": "satisfied", "rationale": "依据原文判断。",
             "evidence_refs": [second_ref]},
        ]}

    def test_code_owned_spans_preserve_whitespace_and_compile_without_retyping(self) -> None:
        packet = build_source_reference_packet(self.request)
        first_spans = packet["checks"][0]["source_spans"]
        sentence = next(span for span in first_spans if "年 月" in span["text"])
        second = packet["checks"][1]["source_spans"][0]
        raw = self._simple_response(sentence["ref_id"], second["ref_id"])
        raw_before = copy.deepcopy(raw)
        request_before = copy.deepcopy(self.request)

        compiled, audit = compile_source_reference_response(
            raw, self.request, RESPONSE_SCHEMA, coverage=False,
        )

        self.assertEqual(compiled["results"][0]["evidence_quotes"], [sentence["text"]])
        self.assertIn("年 月", compiled["results"][0]["evidence_quotes"][0])
        self.assertNotIn("年  月", compiled["results"][0]["evidence_quotes"][0])
        self.assertEqual(raw, raw_before)
        self.assertEqual(self.request, request_before)
        self.assertTrue(audit["semantic_verdicts_unchanged"])
        self.assertEqual(audit["run_id"], "run-1")
        self.assertEqual(audit["selections"][0]["spans"][0]["source_sha256"], sentence["source_sha256"])

    def test_references_cannot_cross_checks_or_survive_a_changed_request(self) -> None:
        packet = build_source_reference_packet(self.request)
        first = packet["checks"][0]["source_spans"][0]["ref_id"]
        second = packet["checks"][1]["source_spans"][0]["ref_id"]
        cross_check = self._simple_response(second, first)
        with self.assertRaisesRegex(ValueError, "source reference response rejected"):
            compile_source_reference_response(
                cross_check, self.request, RESPONSE_SCHEMA, coverage=False,
            )

        changed = copy.deepcopy(self.request)
        changed["checks"][0]["document_text"] += "新增"
        with self.assertRaisesRegex(ValueError, "source reference response rejected"):
            compile_source_reference_response(
                self._simple_response(first, second), changed, RESPONSE_SCHEMA, coverage=False,
            )

    def test_unknown_reference_duplicate_check_and_missing_check_are_rejected(self) -> None:
        packet = build_source_reference_packet(self.request)
        first = packet["checks"][0]["source_spans"][0]["ref_id"]
        second = packet["checks"][1]["source_spans"][0]["ref_id"]
        valid = self._simple_response(first, second)
        invalid_responses = [
            self._simple_response("Q" + "0" * 16, second),
            {"results": [valid["results"][0], valid["results"][0]]},
            {"results": [valid["results"][0]]},
        ]
        for response in invalid_responses:
            with self.subTest(response=response), self.assertRaises(ValueError):
                compile_source_reference_response(
                    response, self.request, RESPONSE_SCHEMA, coverage=False,
                )

    def test_coverage_machine_inventory_is_injected_by_code(self) -> None:
        request = {"protocol": "coverage", "run_id": "run-2", "checks": [{
            "check_id": "C1",
            "document_text": "签字后提交。",
            "review_context": {"machine_obligation_ids": ["source_fact_1"]},
        }]}
        packet = build_source_reference_packet(request)
        ref = packet["checks"][0]["source_spans"][0]["ref_id"]
        raw = {"results": [{
            "check_id": "C1", "verdict": "uncertain", "rationale": "职责范围需确认。",
            "evidence_refs": [ref], "identified_obligations": [{
                "source_ref": ref, "disposition": "ambiguous", "requirement_refs": [],
            }, {
                "source_ref": ref, "disposition": "ambiguous", "requirement_refs": [],
            }],
        }]}

        wire_schema = source_reference_schema(
            OBLIGATION_COVERAGE_SCHEMA, packet, coverage=True,
        )
        self.assertEqual(raw["results"][0].get("machine_obligation_ids"), None)
        compiled, audit = compile_source_reference_response(
            raw, request, OBLIGATION_COVERAGE_SCHEMA, coverage=True,
        )
        result = compiled["results"][0]
        self.assertEqual(result["machine_obligation_ids"], ["source_fact_1"])
        self.assertEqual(result["evidence_quotes"], ["签字后提交。"])
        self.assertEqual(result["identified_obligations"][0]["source_quote"], "签字后提交。")
        self.assertEqual(result["identified_obligations"][0]["disposition"], "ambiguous")
        self.assertEqual(len(audit["selections"][0]["obligations"]), 2)
        self.assertEqual(
            [item["obligation_index"] for item in audit["selections"][0]["obligations"]],
            [0, 1],
        )
        self.assertEqual(
            audit["selections"][0]["obligations"][0]["source_ref"],
            audit["selections"][0]["obligations"][1]["source_ref"],
        )
        self.assertNotIn("machine_obligation_ids", wire_schema["properties"]["results"]["items"]["anyOf"][0]["properties"])

    def test_external_action_mapping_uses_only_current_primary_ids(self) -> None:
        request = {"protocol": "coverage", "run_id": "run-external-map", "checks": [{
            "check_id": "C1", "document_text": "须经导师同意并由学院批准。",
            "review_context": {
                "classification": "external_compliance", "requires_requirement": False,
                "primary_obligations": [
                    {"id": "advisor_consent", "status": "unverifiable", "reason": "pending"},
                    {"id": "college_approval", "status": "unverifiable", "reason": "pending"},
                ],
                "linked_requirements": [], "machine_obligation_ids": [],
            },
        }]}
        packet = build_source_reference_packet(request)
        wire = source_reference_schema(OBLIGATION_COVERAGE_SCHEMA, packet, coverage=True)
        branch = wire["properties"]["results"]["items"]["anyOf"][0]
        obligation = branch["properties"]["identified_obligations"]["items"]
        normal = obligation.get("anyOf", [obligation])[0]
        self.assertEqual(
            normal["properties"]["primary_obligation_id"]["enum"],
            ["advisor_consent", "college_approval"],
        )
        self.assertIn("primary_obligation_id", normal["required"])
        self.assertEqual(native_schema_support_errors(native_output_schema(wire)), [])

    def test_mixed_action_wire_schema_requires_current_primary_ids(self) -> None:
        request = {"protocol": "coverage", "run_id": "run-mixed-map", "checks": [{
            "check_id": "C00040",
            "document_text": "未经批准的均为公开学位论文（公开的学位论文本项为空白）",
            "review_context": {
                "classification": "executable_with_external_check",
                "requires_requirement": True,
                "primary_obligations": [
                    {"id": "approval_status", "status": "unverifiable", "reason": "external"},
                    {"id": "public_blank", "status": "covered", "reason": "DOCX"},
                ],
                "linked_requirements": [{"requirement_ref": "RR-public-blank"}],
                "machine_obligation_ids": [],
            },
        }]}
        packet = build_source_reference_packet(request)
        wire = source_reference_schema(OBLIGATION_COVERAGE_SCHEMA, packet, coverage=True)
        obligation = wire["properties"]["results"]["items"]["anyOf"][0]["properties"]["identified_obligations"]["items"]
        normal = obligation.get("anyOf", [obligation])[0]
        self.assertEqual(
            normal["properties"]["primary_obligation_id"]["enum"],
            ["approval_status", "public_blank"],
        )
        self.assertIn("primary_obligation_id", normal["required"])
        self.assertEqual(native_schema_support_errors(native_output_schema(wire)), [])

    def test_registered_projection_keeps_provider_and_canonical_source_selections(self) -> None:
        source = "The following English is not correct."
        request = {
            "protocol": "obligation_coverage_v1", "run_id": "run-c74",
            "case_id": "bsu", "checks": [{
                "check_id": "C00074", "document_text": source,
                "review_context": {
                    "classification": "informational", "requires_requirement": False,
                    "primary_obligations": [], "linked_requirements": [],
                    "machine_obligation_ids": [],
                    "manual_review_codes": ["source_correction_target_ambiguity"],
                    "source_content_verification_codes": [],
                },
            }],
        }
        packet = build_source_reference_packet(request)
        ref = packet["checks"][0]["source_spans"][0]["ref_id"]
        raw = {"results": [{
            "check_id": "C00074", "verdict": "manual_review_required",
            "rationale": "The source does not identify a correction target.",
            "evidence_refs": [ref], "identified_obligations": [
                {
                    "source_ref": ref, "disposition": "unrepresented",
                    "obligation_summary": "Identify the approved target or replacement.",
                    "requirement_refs": [],
                },
                {
                    "source_ref": ref, "disposition": "unrepresented",
                    "obligation_summary": "Resolve the unspecified correction target.",
                    "requirement_refs": [],
                },
            ],
        }]}
        raw_before = copy.deepcopy(raw)
        compiled, compilation = compile_source_reference_response(
            raw, request, OBLIGATION_COVERAGE_SCHEMA, coverage=True,
        )
        compiled_before = copy.deepcopy(compiled)
        canonical = copy.deepcopy(compiled)
        validate_obligation_coverage_response(canonical, request["checks"])
        bound = bind_validated_source_reference_selections(
            compilation, compiled_before, canonical, request,
        )

        self.assertEqual(raw, raw_before)
        self.assertEqual(compiled, compiled_before)
        self.assertEqual(len(bound["selections"][0]["obligations"]), 2)
        self.assertEqual(len(canonical["results"][0]["identified_obligations"]), 1)
        self.assertEqual(len(bound["canonical_selections"][0]["obligations"]), 1)
        self.assertEqual(
            bound["canonical_selections"][0]["obligations"][0]["span"]["text"], source,
        )
        self.assertEqual(
            bound["canonical_response_sha256"], sha256_json(canonical),
        )
        self.assertEqual(
            bound["validation_projection"]["pre_validation_obligation_counts"],
            {"C00074": 2},
        )
        self.assertEqual(
            bound["validation_projection"]["canonical_obligation_counts"],
            {"C00074": 1},
        )
        self.assertEqual(bound["validation_projection"]["changed_check_ids"], ["C00074"])

    def test_registered_projection_fails_closed_without_a_selected_exact_source_span(self) -> None:
        source = "The following English is not correct."
        request = {
            "protocol": "obligation_coverage_v1", "run_id": "run-c74-ambiguous",
            "checks": [{
                "check_id": "C00074", "document_text": source,
                "review_context": {
                    "classification": "informational", "requires_requirement": False,
                    "primary_obligations": [], "linked_requirements": [],
                    "machine_obligation_ids": [],
                    "manual_review_codes": ["source_correction_target_ambiguity"],
                    "source_content_verification_codes": [],
                },
            }],
        }
        packet = build_source_reference_packet(request)
        source_ref = packet["checks"][0]["source_spans"][0]["ref_id"]
        raw = {"results": [{
            "check_id": "C00074", "verdict": "manual_review_required",
            "rationale": "The correction target is unresolved.",
            "evidence_refs": [source_ref],
            "identified_obligations": [{
                "source_ref": source_ref, "disposition": "unrepresented",
                "obligation_summary": "Determine the correction target.",
                "requirement_refs": [],
            }],
        }]}
        compiled, compilation = compile_source_reference_response(
            raw, request, OBLIGATION_COVERAGE_SCHEMA, coverage=True,
        )
        canonical = copy.deepcopy(compiled)
        canonical["results"][0]["identified_obligations"][0]["source_quote"] = (
            "English is not correct."
        )
        with self.assertRaisesRegex(ValueError, "not uniquely bound"):
            bind_validated_source_reference_selections(
                compilation, compiled, canonical, request,
            )

    def test_same_quote_obligation_reordering_does_not_reuse_source_refs_by_index(self) -> None:
        quote = "The following English is not correct."
        source = quote + "\n" + quote
        request = {
            "protocol": "coverage", "run_id": "run-same-quote-reorder",
            "checks": [{"check_id": "C1", "document_text": source}],
        }
        packet = build_source_reference_packet(request)
        refs = [
            span["ref_id"] for span in packet["checks"][0]["source_spans"]
            if span["text"] == quote
        ]
        self.assertEqual(len(refs), 2)
        raw = {"results": [{
            "check_id": "C1", "verdict": "manual_review_required",
            "rationale": "Two source occurrences are present.",
            "evidence_refs": refs,
            "identified_obligations": [
                {"source_ref": refs[0], "disposition": "unrepresented",
                 "obligation_summary": "First occurrence.", "requirement_refs": []},
                {"source_ref": refs[1], "disposition": "unrepresented",
                 "obligation_summary": "Second occurrence.", "requirement_refs": []},
            ],
        }]}
        compiled, compilation = compile_source_reference_response(
            raw, request, OBLIGATION_COVERAGE_SCHEMA, coverage=True,
        )
        reordered = copy.deepcopy(compiled)
        reordered["results"][0]["identified_obligations"].reverse()
        with self.assertRaisesRegex(ValueError, "not uniquely bound"):
            bind_validated_source_reference_selections(
                compilation, compiled, reordered, request,
            )

    def test_scope_dependency_fields_are_discriminated_by_disposition(self) -> None:
        request = {"protocol": "coverage", "run_id": "run-scope-schema", "checks": [{
            "check_id": "C00004", "document_text": "密级",
            "review_context": {
                "classification": "covered", "requires_requirement": True,
                "linked_requirements": [{"requirement_ref": "RR-security"}],
                "machine_obligation_ids": [],
            },
        }]}
        packet = build_source_reference_packet(request)
        ref = packet["checks"][0]["source_spans"][0]["ref_id"]
        wire_schema = source_reference_schema(
            OBLIGATION_COVERAGE_SCHEMA, packet, coverage=True,
        )
        result_schema = wire_schema["properties"]["results"]["items"]["anyOf"][0]
        obligation_schema = result_schema["properties"]["identified_obligations"]["items"]
        obligation_branches = obligation_schema.get("anyOf", [obligation_schema])
        self.assertEqual(len(obligation_branches), 1)
        normal_branch = obligation_branches[0]
        normal_properties = normal_branch["properties"]
        self.assertNotIn("scope_dependency_codes", normal_properties)
        self.assertNotIn("scope_dependency_dimensions", normal_properties)
        self.assertEqual(normal_properties["disposition"]["enum"], [
            "represented", "unrepresented", "ambiguous", "external_action_pending",
            "authoring_content_pending", "backend_unsupported", "source_content_verification_pending",
        ])
        self.assertIn("source_ref", normal_properties)
        self.assertNotIn("source_quote", normal_properties)
        self.assertEqual(native_schema_support_errors(native_output_schema(wire_schema)), [])

        represented = {"results": [{
            "check_id": "C00004", "verdict": "consistent",
            "rationale": "The conditional field is represented.", "evidence_refs": [ref],
            "identified_obligations": [{
                "source_ref": ref, "disposition": "represented",
                "requirement_refs": ["RR-security"], "obligation_summary": None,
            }],
        }]}
        compiled, _ = compile_source_reference_response(
            represented, request, OBLIGATION_COVERAGE_SCHEMA, coverage=True,
            provider_nullable_optionals=True,
        )
        obligation = compiled["results"][0]["identified_obligations"][0]
        self.assertEqual(obligation["source_quote"], "密级")
        self.assertNotIn("obligation_summary", obligation)

        # A provider or caller that ignores the discriminated output schema
        # must still fail local validation; the compiler must not erase the
        # contradictory non-null metadata to make the response pass.
        invalid = json.loads(json.dumps(represented))
        invalid["results"][0]["identified_obligations"][0][
            "scope_dependency_dimensions"
        ] = ["condition"]
        with self.assertRaisesRegex(ValueError, "source reference response rejected"):
            compile_source_reference_response(
                invalid, request, OBLIGATION_COVERAGE_SCHEMA, coverage=True,
                provider_nullable_optionals=True,
            )

        scope_request = {"protocol": "coverage", "run_id": "run-scope-authorized", "checks": [{
            "check_id": "C00076",
            "document_text": "The Chinese abstract is 300 to 1,000 words; its target remains unclear.",
            "review_context": {
                "classification": "unresolved", "requires_requirement": False,
                "linked_requirements": [], "machine_obligation_ids": [],
                "manual_review_codes": ["abstract_target_metric_ambiguity"],
            },
        }]}
        scope_packet = build_source_reference_packet(scope_request)
        scope_ref = scope_packet["checks"][0]["source_spans"][0]["ref_id"]
        scope_wire_schema = source_reference_schema(
            OBLIGATION_COVERAGE_SCHEMA, scope_packet, coverage=True,
        )
        scope_result_schema = scope_wire_schema["properties"]["results"]["items"]["anyOf"][0]
        scope_item_schema = scope_result_schema["properties"]["identified_obligations"]["items"]
        scope_branches = scope_item_schema.get("anyOf", [scope_item_schema])
        self.assertEqual(len(scope_branches), 2)
        scope_branch = next(
            item for item in scope_branches
            if item["properties"]["disposition"]["enum"] == ["scope_unresolved"]
        )
        self.assertEqual(
            scope_branch["properties"]["scope_dependency_codes"]["items"]["enum"],
            ["abstract_target_metric_ambiguity"],
        )
        self.assertEqual(native_schema_support_errors(native_output_schema(scope_wire_schema)), [])
        scope_unresolved = {"results": [{
            "check_id": "C00076", "verdict": "manual_review_required",
            "rationale": "The target remains unresolved.", "evidence_refs": [scope_ref],
            "identified_obligations": [{
                "source_ref": scope_ref, "disposition": "scope_unresolved",
                "obligation_summary": "The scope depends on unresolved target ambiguity.",
                "scope_dependency_codes": ["abstract_target_metric_ambiguity"],
                "scope_dependency_dimensions": ["target"], "requirement_refs": [],
            }],
        }]}
        compiled_scope, _ = compile_source_reference_response(
            scope_unresolved, scope_request, OBLIGATION_COVERAGE_SCHEMA, coverage=True,
        )
        self.assertEqual(
            compiled_scope["results"][0]["identified_obligations"][0]["disposition"],
            "scope_unresolved",
        )

    def test_codex_nullable_optional_obligations_normalize_with_raw_hash_preserved(self) -> None:
        request = {"protocol": "coverage", "run_id": "run-null", "checks": [{
            "check_id": "C1", "document_text": "该表述的目标仍需核实。",
            "review_context": {"machine_obligation_ids": []},
        }, {
            "check_id": "C2",
            "document_text": "The Chinese abstract is 300 to 1,000 words; its target remains unclear.",
            "review_context": {
                "machine_obligation_ids": [], "classification": "unresolved",
                "requires_requirement": False, "linked_requirements": [],
                "manual_review_codes": ["abstract_target_metric_ambiguity"],
            },
        }]}
        packet = build_source_reference_packet(request)
        first_ref = packet["checks"][0]["source_spans"][0]["ref_id"]
        second_ref = packet["checks"][1]["source_spans"][0]["ref_id"]
        raw = {"results": [{
            "check_id": "C1", "verdict": "uncertain", "rationale": "范围需要核实。",
            "evidence_refs": [first_ref], "identified_obligations": [{
                "source_ref": first_ref, "disposition": "ambiguous", "requirement_refs": [],
                "obligation_summary": None,
            }],
        }, {
            "check_id": "C2", "verdict": "uncertain", "rationale": "适用口径需要核实。",
            "evidence_refs": [second_ref], "identified_obligations": [{
                "source_ref": second_ref, "disposition": "scope_unresolved", "requirement_refs": [],
                "scope_dependency_codes": ["abstract_target_metric_ambiguity"],
                "scope_dependency_dimensions": ["target"],
                "obligation_summary": "The count target remains unresolved.",
            }],
        }]}
        raw_before = copy.deepcopy(raw)
        with self.assertRaisesRegex(ValueError, "source reference response rejected"):
            compile_source_reference_response(
                raw, request, OBLIGATION_COVERAGE_SCHEMA, coverage=True,
            )
        compiled, audit = compile_source_reference_response(
            raw, request, OBLIGATION_COVERAGE_SCHEMA, coverage=True,
            provider_nullable_optionals=True,
        )

        obligation = compiled["results"][0]["identified_obligations"][0]
        self.assertNotIn("obligation_summary", obligation)
        preserved = compiled["results"][1]["identified_obligations"][0]
        self.assertEqual(
            preserved["scope_dependency_codes"], ["abstract_target_metric_ambiguity"],
        )
        self.assertEqual(preserved["scope_dependency_dimensions"], ["target"])
        self.assertEqual(preserved["obligation_summary"], "The count target remains unresolved.")
        self.assertEqual(raw, raw_before)
        self.assertEqual(audit["raw_response_sha256"], sha256_json(raw_before))
        normalized_input = copy.deepcopy(raw_before)
        normalized_obligation = normalized_input["results"][0]["identified_obligations"][0]
        normalized_obligation.pop("obligation_summary")
        self.assertEqual(
            audit["provider_nullable_normalization"],
            {
                "policy": "strict_native_optional_nulls_to_omitted_v1",
                "provider_response_sha256": sha256_json(raw_before),
                "canonical_input_response_sha256": sha256_json(normalized_input),
            },
        )

        required_null = copy.deepcopy(raw_before)
        required_null["results"][0]["rationale"] = None
        with self.assertRaisesRegex(ValueError, "source reference response rejected"):
            compile_source_reference_response(
                required_null, request, OBLIGATION_COVERAGE_SCHEMA, coverage=True,
                provider_nullable_optionals=True,
            )

        unknown_null = copy.deepcopy(raw_before)
        unknown_null["results"][0]["identified_obligations"][0]["provider_extra"] = None
        with self.assertRaisesRegex(ValueError, "source reference response rejected"):
            compile_source_reference_response(
                unknown_null, request, OBLIGATION_COVERAGE_SCHEMA, coverage=True,
                provider_nullable_optionals=True,
            )

    def test_english_source_spans_preserve_offsets_and_do_not_split_decimals_or_abbreviations(self) -> None:
        text = (
            "The Chinese abstract is 300 to 1,000 words. It cites e.g. prior work, "
            "including value 1.5, and usually ends here!\nA new paragraph follows."
        )
        request = {"run_id": "r-en", "checks": [{"check_id": "C-en", "document_text": text}]}
        spans = build_source_reference_packet(request)["checks"][0]["source_spans"]
        sentence_spans = [span for span in spans if (span["start"], span["end"]) != (0, len(text))]
        self.assertTrue(sentence_spans)
        self.assertTrue(all(text[span["start"]:span["end"]] == span["text"] for span in spans))
        rendered = [span["text"] for span in sentence_spans]
        self.assertTrue(any("300 to 1,000 words." in span for span in rendered))
        self.assertTrue(any("e.g. prior work" in span and "1.5" in span for span in rendered))
        self.assertTrue(any(span.endswith("usually ends here!") for span in rendered))
        self.assertTrue(any("A new paragraph follows." in span for span in rendered))
        self.assertFalse(any(span.rstrip().endswith("e.g.") for span in rendered))
        self.assertFalse(any(span.rstrip().endswith("1.") for span in rendered))

    def test_multipart_english_abbreviations_remain_inside_their_source_sentence(self) -> None:
        text = "Use U.S. standards. Use Ph.D. data. Use M.Sc. results. Check No. 3. Next sentence."
        request = {"run_id": "r-abbr", "checks": [{"check_id": "C-abbr", "document_text": text}]}
        spans = build_source_reference_packet(request)["checks"][0]["source_spans"]
        sentence_spans = [span for span in spans if (span["start"], span["end"]) != (0, len(text))]
        self.assertTrue(all(text[span["start"]:span["end"]] == span["text"] for span in spans))
        rendered = [span["text"] for span in sentence_spans]
        self.assertTrue(any(span.lstrip().startswith("Use U.S. standards.") for span in rendered))
        self.assertTrue(any(span.lstrip().startswith("Use Ph.D. data.") for span in rendered))
        self.assertTrue(any(span.lstrip().startswith("Use M.Sc. results.") for span in rendered))
        self.assertTrue(any(span.lstrip().startswith("Check No. 3.") for span in rendered))
        self.assertTrue(any(span.lstrip().startswith("Next sentence.") for span in rendered))

    def test_empty_or_malformed_source_check_fails_before_model_invocation(self) -> None:
        for checks in ([], [{"check_id": "C1", "document_text": "  "}], [None]):
            with self.subTest(checks=checks), self.assertRaises(ValueError):
                build_source_reference_packet({"checks": checks})


if __name__ == "__main__":
    unittest.main()
