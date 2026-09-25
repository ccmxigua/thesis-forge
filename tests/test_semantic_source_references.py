from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from native_semantic_review import OBLIGATION_COVERAGE_SCHEMA, RESPONSE_SCHEMA
from host_review_schema import native_output_schema, native_schema_support_errors
from semantic_contract import sha256_json
from semantic_source_references import (
    build_source_reference_packet,
    compile_source_reference_response,
    source_reference_schema,
)


class SemanticSourceReferenceTests(unittest.TestCase):
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
            "authoring_content_pending", "backend_unsupported",
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
