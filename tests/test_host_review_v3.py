from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import host_agent_bridge as bridge  # noqa: E402
from format_spec_validation import schema_support_errors, validate_instance  # noqa: E402
from host_review_contract import (  # noqa: E402
    HOST_REVIEW_CONTRACT_V3,
    contract_error_records,
    derived_requirement_indexes,
    provenance_error_records,
    validate_response,
)
from host_review_schema import (  # noqa: E402
    native_schema_support_errors,
    applicability_value_schema,
    native_output_schema,
    normalize_native_response,
)
from requirements_engine import build_llm_request  # noqa: E402
from semantic_contract import attach_request_provenance  # noqa: E402
from semantic_review_ledger import build_semantic_review_ledger  # noqa: E402


class HostReviewV3Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.clauses = [{
            "id": "C1",
            "text": "正文使用宋体",
            "evidence_ids": ["E1"],
            "source_kind": "paragraph",
            "location": {},
            "part_index": 0,
        }]
        self.evidence = {
            "evidence": [{"id": "E1", "text": "正文使用宋体", "kind": "paragraph"}]
        }
        self.request = build_llm_request(
            [], self.clauses, self.evidence, {}, "full",
            contract_version=HOST_REVIEW_CONTRACT_V3,
        )
        self.request = attach_request_provenance(
            self.request,
            source_sha256="a" * 64,
            evidence_doc=self.evidence,
            clauses=self.clauses,
            run_id="run-v3-test",
        )

    def _informational_response(self) -> dict:
        return {
            "contract_version": HOST_REVIEW_CONTRACT_V3,
            "provenance": self.request["provenance"],
            "requirements": [],
            "clause_reviews": [{
                "clause_id": "C1",
                "classification": "informational",
                "reason": "The current evidence was reviewed.",
            }],
            "unsupported_items": [],
            "reported_conflicts": [],
        }

    def _executable_response(self) -> dict:
        return {
            "contract_version": HOST_REVIEW_CONTRACT_V3,
            "provenance": self.request["provenance"],
            "requirements": [{
                "role": "body_text",
                "properties": {"font": {"cjk": "SimSun", "size_pt": 12}},
                "clause_ids": ["C1"],
                "evidence_ids": ["E1"],
                "confidence": 0.9,
                "reason": "The clause specifies the body-text font.",
                "verification": {"mode": "word_render", "checks": ["Check the body-text font."]},
            }],
            "clause_reviews": [{
                "clause_id": "C1",
                "classification": "executable",
                "reason": "The clause is executable in DOCX.",
            }],
            "unsupported_items": [],
            "reported_conflicts": [],
        }

    def test_request_explains_multi_role_clause_and_equation_role_boundary(self) -> None:
        instructions = "\n".join(self.request["instructions"])
        self.assertIn("A single clause may support multiple requirements", instructions)
        self.assertIn("Role boundary for equations", instructions)
        self.assertIn("partial_clause_coverage error does not authorize changing classification", instructions)

    def test_v3_schema_has_one_model_authoritative_relation(self) -> None:
        reviews_schema = self.request["response_schema"]["properties"]["clause_reviews"]["items"]
        self.assertNotIn("requirement_indexes", reviews_schema["properties"])
        self.assertNotIn("Maintain requirement_indexes exactly", str(self.request["instructions"]))
        self.assertNotIn("zero-based index", str(self.request["instructions"]))
        self.assertNotIn("review/index pair", str(self.request["instructions"]))
        # The schema itself, rather than prose, is the enforcement boundary.
        self.assertEqual(validate_response(self._informational_response(), self.request), [])

    def test_v3_derives_reverse_relation_and_rejects_model_duplicate(self) -> None:
        response = self._executable_response()
        self.assertEqual(derived_requirement_indexes(response, self.clauses), {"C1": [0]})
        self.assertEqual(validate_response(response, self.request), [])

        response["clause_reviews"][0]["requirement_indexes"] = [0]
        errors = validate_response(response, self.request)
        self.assertTrue(any("forbidden_in_contract_3.0" in error for error in errors))

    def test_v3_rejects_duplicate_requirement_edges_and_evidence_ids(self) -> None:
        response = self._executable_response()
        response["requirements"][0]["clause_ids"] = ["C1", "C1"]
        response["requirements"][0]["evidence_ids"] = ["E1", "E1"]
        errors = validate_response(response, self.request)
        self.assertIn("$.requirements[0].clause_ids: duplicate", errors)
        self.assertIn("$.requirements[0].evidence_ids: duplicate", errors)

    def test_retry_prompt_does_not_reintroduce_v2_relation_instructions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            chunk_path = Path(td) / "chunk.json"
            chunk_path.write_text(json.dumps(self.request), encoding="utf-8")
            prompt = bridge._host_prompt(
                request_path=Path(td) / "request.json",
                chunk_path=chunk_path,
                response_path=Path(td) / "response.json",
                run_id="run-v3-test",
                chunk_index=1,
                chunk_count=1,
                attempt=2,
                retry_hint="requirement_index_not_backed_by_clause",
                retry_parent_response_sha256="b" * 64,
            )
        self.assertIn("Do not emit clause_reviews.requirement_indexes", prompt)
        self.assertNotIn("Maintain requirement_indexes exactly", prompt)
        self.assertNotIn("requirement_indexes: []", prompt)
        self.assertNotIn("zero-based index", prompt)
        self.assertNotIn("review/index pair", prompt)
        self.assertIn("rejected parent response sha256", prompt)

    def test_ledger_is_deterministic_and_does_not_infer_obligations(self) -> None:
        response = self._executable_response()
        ledger = build_semantic_review_ledger(response, self.clauses)
        self.assertEqual(ledger["relationship_policy"]["authoritative_edge"], "requirements[].clause_ids")
        self.assertEqual(ledger["edges"][0]["requirement_index"], 0)
        self.assertRegex(ledger["edges"][0]["requirement_id"], r"^R[0-9a-f]{16}$")
        self.assertEqual(ledger["requirements"][0]["requirement_id"], ledger["edges"][0]["requirement_id"])
        self.assertEqual(ledger["clauses"][0]["obligations"], [])
        self.assertEqual(ledger["clauses"][0]["obligation_decomposition"], "not_supplied")
        self.assertEqual(
            ledger["response_sha256"],
            build_semantic_review_ledger(response, self.clauses)["response_sha256"],
        )

    def test_ledger_requirement_ids_survive_response_reordering(self) -> None:
        first = self._executable_response()
        second = json.loads(json.dumps(first))
        second["requirements"].append({
            "role": "body_text",
            "properties": {"font": {"cjk": "SimHei", "size_pt": 11}},
            "clause_ids": ["C1"],
            "evidence_ids": ["E1"],
            "confidence": 0.8,
            "reason": "A second independent supported property.",
            "verification": {"mode": "word_render", "checks": ["Check the second property."]},
        })
        reordered = json.loads(json.dumps(second))
        reordered["requirements"] = list(reversed(reordered["requirements"]))
        first_ledger = build_semantic_review_ledger(second, self.clauses)
        second_ledger = build_semantic_review_ledger(reordered, self.clauses)
        self.assertEqual(
            {item["requirement_id"] for item in first_ledger["requirements"]},
            {item["requirement_id"] for item in second_ledger["requirements"]},
        )
        self.assertEqual(
            {(item["clause_id"], item["requirement_id"]) for item in first_ledger["edges"]},
            {(item["clause_id"], item["requirement_id"]) for item in second_ledger["edges"]},
        )

    def test_compact_packet_exposes_only_bounded_runtime_context(self) -> None:
        chunk = json.loads(json.dumps(self.request))
        chunk["runtime_context"] = {
            "confirmed_thesis_profile": {
                "schema_version": "1.0",
                "profile_id": "profile-test",
                "degree_category": "master",
                "security_level": "public",
                "cover_metadata": {"degree_category": "master"},
                "source_sha256": "should-not-be-exposed",
            },
            "runtime_inventory": {
                "status": "complete",
                "declaration_anchor_status": "selected",
                "anchor_inventory": {"selected": "abstract_title_zh"},
                "source_sha256": "should-not-be-exposed",
            },
            "case_id": "BSU",
            "run_id": "trusted-run-id-must-not-be-exposed",
            "code_fingerprint_sha256": "trusted-hash-must-not-be-exposed",
        }
        packet = bridge.compact_model_packet(chunk)
        context = packet["runtime_context"]
        self.assertEqual(context["confirmed_thesis_profile"]["degree_category"], "master")
        self.assertEqual(context["runtime_inventory"]["status"], "complete")
        self.assertEqual(context["case_id"], "BSU")
        self.assertNotIn("source_sha256", json.dumps(context, ensure_ascii=False))
        self.assertNotIn("trusted-run-id", json.dumps(context, ensure_ascii=False))
        self.assertIn("trusted hashes and provenance omitted", context["policy"])

    def test_contract_errors_are_structured_without_repairing_response(self) -> None:
        response = self._executable_response()
        response["clause_reviews"][0]["normative_basis"] = "informational"
        errors = validate_response(response, self.request)
        records = contract_error_records(errors, response=response, chunk=self.request)
        self.assertTrue(any(record["code"] == "normative_basis_invalid" for record in records))
        self.assertEqual(response["clause_reviews"][0]["normative_basis"], "informational")
        self.assertEqual(len(next(record for record in records if record["code"] == "normative_basis_invalid")["response_sha256"]), 64)

    def test_fixed_declaration_text_errors_are_distinguished_from_semantic_errors(self) -> None:
        records = contract_error_records(
            [
                "$.requirements[0].properties.items[0].body_parts[0]: "
                "must equal a complete cited source-evidence text; do not paraphrase",
            ],
            response=self._executable_response(),
            chunk=self.request,
        )
        self.assertEqual(records[0]["code"], "fixed_text_evidence_mismatch")

    def test_missing_v3_requirement_relation_is_structured_as_relation_error(self) -> None:
        records = contract_error_records(
            [
                "$.clause_reviews[12]: executable_review_requires_derived_requirement",
                "requirements_not_referenced_by_clause_review:0",
            ],
            response=self._executable_response(),
            chunk=self.request,
        )
        self.assertEqual(
            [record["code"] for record in records],
            ["missing_derived_requirement", "unused_executable_requirement"],
        )

    def test_informational_requirement_relation_is_explicitly_forbidden(self) -> None:
        response = self._informational_response()
        response["requirements"] = [{
            "role": "body_text",
            "properties": {"text": "正文使用宋体"},
            "clause_ids": ["C1"],
            "evidence_ids": ["E1"],
            "reason": "incorrectly emitted for an informational clause",
        }]
        errors = validate_response(response, self.request)
        self.assertTrue(
            any("informational_requirement_forbidden" in error for error in errors),
            errors,
        )
        records = contract_error_records(errors, response=response, chunk=self.request)
        info_records = [
            record for record in records
            if record["code"] == "informational_requirement_forbidden"
        ]
        self.assertTrue(info_records, records)
        self.assertEqual(info_records[0]["requirement_index"], 0)
        self.assertTrue(info_records[0]["mechanically_removable"])

    def test_aggregate_unused_relation_is_split_into_nine_current_facts(self) -> None:
        clauses = [{"id": f"C{i}", "text": f"说明{i}"} for i in range(1, 10)]
        response = {
            "requirements": [
                {"clause_ids": [f"C{i}"], "properties": {"text": f"说明{i}"}}
                for i in range(1, 10)
            ],
            "clause_reviews": [
                {"clause_id": f"C{i}", "classification": "informational"}
                for i in range(1, 10)
            ],
        }
        raw_error = "requirements_not_referenced_by_clause_review:" + ",".join(
            str(i) for i in range(9)
        )
        records = contract_error_records([raw_error], response=response, chunk={"clauses": clauses})
        self.assertEqual(len(records), 9)
        self.assertEqual(
            [record["requirement_index"] for record in records], list(range(9))
        )
        self.assertTrue(all(
            record["code"] == "informational_requirement_forbidden"
            and record["mechanically_removable"]
            for record in records
        ))

    def test_v3_relation_error_codes_cover_mixed_missing_unknown_and_nonrequirement(self) -> None:
        def build_case(clause_ids: list[str], requirements: list[dict], reviews: list[dict]) -> tuple[list[str], list[dict]]:
            clauses = [
                {"id": clause_id, "text": f"说明 {clause_id}", "evidence_ids": [f"E{index}"]}
                for index, clause_id in enumerate(clause_ids, start=1)
            ]
            evidence = {
                "evidence": [
                    {"id": f"E{index}", "text": f"说明 {clause_id}", "kind": "paragraph"}
                    for index, clause_id in enumerate(clause_ids, start=1)
                ]
            }
            request = build_llm_request(
                [], clauses, evidence, {}, "full", contract_version=HOST_REVIEW_CONTRACT_V3,
            )
            response = {
                "contract_version": HOST_REVIEW_CONTRACT_V3,
                "requirements": requirements,
                "clause_reviews": reviews,
                "unsupported_items": [],
                "reported_conflicts": [],
            }
            errors = validate_response(response, request)
            return errors, contract_error_records(errors, response=response, chunk=request)

        _, mixed_records = build_case(
            ["C1", "C2"],
            [{"role": "body_text", "properties": {"text": "混合"}, "clause_ids": ["C1", "C2"], "evidence_ids": ["E1", "E2"], "confidence": 0.9, "reason": "混合"}],
            [
                {"clause_id": "C1", "classification": "executable", "reason": "执行"},
                {"clause_id": "C2", "classification": "informational", "reason": "说明"},
            ],
        )
        self.assertIn("mixed_execution_classification_relation", {item["code"] for item in mixed_records})

        _, missing_records = build_case(
            ["C1", "C2"],
            [{"role": "body_text", "properties": {"text": "缺审查"}, "clause_ids": ["C1", "C2"], "evidence_ids": ["E1", "E2"], "confidence": 0.9, "reason": "缺审查"}],
            [{"clause_id": "C1", "classification": "informational", "reason": "说明"}],
        )
        self.assertIn("missing_clause_review", {item["code"] for item in missing_records})

        _, unknown_records = build_case(
            ["C1"],
            [{"role": "body_text", "properties": {"text": "未知"}, "clause_ids": ["C404"], "evidence_ids": ["E1"], "confidence": 0.9, "reason": "未知"}],
            [{"clause_id": "C1", "classification": "informational", "reason": "说明"}],
        )
        self.assertIn("unknown_clause_relation", {item["code"] for item in unknown_records})

        _, nonrequirement_records = build_case(
            ["C1"],
            [{"role": "body_text", "properties": {"text": "未解决"}, "clause_ids": ["C1"], "evidence_ids": ["E1"], "confidence": 0.9, "reason": "未解决"}],
            [{"clause_id": "C1", "classification": "unresolved", "reason": "需要确认"}],
        )
        self.assertIn("non_requirement_classification_relation", {item["code"] for item in nonrequirement_records})

    def test_unbacked_evidence_is_structured_as_evidence_relation_error(self) -> None:
        records = contract_error_records(
            ["$.requirements[0].evidence_ids: not_backed_by_clause:E00035"],
            response=self._executable_response(),
            chunk=self.request,
        )
        self.assertEqual(records[0]["code"], "evidence_relation_mismatch")

    def test_invalid_applicability_fact_namespace_is_structured(self) -> None:
        records = contract_error_records(
            [
                "$.requirements[0].applicability.conditions[0].fact: "
                "does not match '^(thesis_profile|source_inventory|template_profile|runtime)\\\\.'",
            ],
            response=self._executable_response(),
            chunk=self.request,
        )
        self.assertEqual(records[0]["code"], "applicability_fact_namespace")

    def test_equation_role_rejects_unsupported_numbering_without_normalizing(self) -> None:
        response = self._executable_response()
        response["requirements"][0]["role"] = "equations"
        response["requirements"][0]["properties"] = {
            "numbering": {"format": "chapter.decimal", "style": "decimal", "depth": 2},
        }
        errors = validate_response(response, self.request)
        self.assertTrue(any("unknown property 'numbering'" in error for error in errors), errors)
        self.assertEqual(response["requirements"][0]["properties"]["numbering"]["depth"], 2)

    def test_unsupported_schema_keyword_is_not_silently_ignored(self) -> None:
        schema = {"type": "array", "contains": {"const": "required"}}
        self.assertTrue(any("unsupported_schema_keyword:contains" in error for error in schema_support_errors(schema)))
        self.assertTrue(any("unsupported_schema_keyword:contains" in error for error in validate_instance([], schema)))

    def test_native_response_schema_has_no_provider_empty_schema(self) -> None:
        native_schema = native_output_schema(self.request["response_schema"])
        self.assertEqual(native_schema_support_errors(native_schema), [])
        self.assertNotIn("provenance", native_schema["properties"])
        requirement_properties = native_schema["properties"]["requirements"]["items"]["properties"]["properties"]
        self.assertGreaterEqual(len(requirement_properties["anyOf"]), 2)
        self.assertTrue(all("$ref" in variant for variant in requirement_properties["anyOf"]))
        self.assertNotEqual(requirement_properties, {"type": "object", "properties": {}})
        font_schema = native_schema["$defs"]["fontSpec"]
        self.assertEqual(set(font_schema["required"]), set(font_schema["properties"]))
        serialized_native_schema = json.dumps(native_schema)
        self.assertNotIn('"uniqueItems"', serialized_native_schema)
        self.assertNotIn('"minLength"', serialized_native_schema)
        conditions_schema = native_schema["$defs"]["applicabilitySpec"]["properties"]["conditions"]
        conditions_array = next(
            variant for variant in conditions_schema["anyOf"]
            if variant.get("type") == "array"
        )
        value_schema = conditions_array["items"]["properties"]["value"]
        self.assertNotEqual(value_schema, {})
        self.assertEqual(native_schema_support_errors(value_schema), [])

    def test_empty_requirement_properties_fail_closed_with_targeted_error(self) -> None:
        response = self._executable_response()
        response["requirements"][0]["properties"] = {}
        errors = validate_response(response, self.request)
        self.assertTrue(
            any("must_include_semantic_payload" in error for error in errors),
            errors,
        )
        records = contract_error_records(errors, response=response, chunk=self.request)
        self.assertTrue(
            any(record["code"] == "empty_requirement_properties" for record in records),
            records,
        )

    def test_input_prerequisite_namespace_is_not_model_defined(self) -> None:
        response = self._executable_response()
        response["requirements"][0]["input_prerequisites"] = [{
            "kind": "runtime",
            "key": "runtime_context.runtime_inventory.anchor_inventory.selected",
            "required": True,
            "reason": "anchor",
        }]
        errors = validate_response(response, self.request)
        self.assertTrue(any("input_prerequisites" in error for error in errors), errors)
        records = contract_error_records(errors, response=response, chunk=self.request)
        self.assertTrue(
            any(record["code"] == "input_prerequisite_namespace" for record in records),
            records,
        )

    def test_non_public_administration_requires_positive_security_condition(self) -> None:
        response = self._executable_response()
        response["requirements"][0] = {
            "role": "cover",
            "properties": {
                "institution": "北京体育大学",
                "fields": [{
                    "id": "title_zh",
                    "label": "论文题目",
                    "value_from": "thesis_profile.cover_metadata.title_zh",
                    "display_policy": "if_present",
                    "order": 1,
                }],
                "non_public_administration": {
                    "applicability": {
                        "status": "conditional",
                        "conditions": [{
                            "fact": "thesis_profile.security_level",
                            "operator": "not_equals",
                            "value": "public",
                        }],
                    },
                    "fields": [],
                    "public_policy": "blank",
                    "source_region": "E1",
                },
            },
            "clause_ids": ["C1"],
            "evidence_ids": ["E1"],
            "confidence": 0.9,
            "reason": "cover",
        }
        errors = validate_response(response, self.request)
        self.assertTrue(any("non_public_administration" in error for error in errors), errors)

    def test_admin_only_cover_requirement_does_not_duplicate_admin_fields(self) -> None:
        response = self._executable_response()
        response["requirements"][0] = {
            "role": "cover",
            "properties": {
                "institution": "北京体育大学",
                "fields": [],
                "non_public_administration": {
                    "applicability": {
                        "status": "conditional",
                        "conditions": [{
                            "fact": "thesis_profile.security_level",
                            "operator": "in",
                            "value": ["restricted", "classified"],
                        }],
                    },
                    "fields": [{
                        "id": "security_marking",
                        "label": "申请密级",
                        "value_from": "thesis_profile.cover_metadata.security_marking",
                        "display_policy": "blank_when_public",
                        "order": 1,
                    }],
                    "public_policy": "blank",
                    "source_region": "E1",
                },
            },
            "clause_ids": ["C1"],
            "evidence_ids": ["E1"],
            "confidence": 0.9,
            "reason": "The current chunk contains only the conditional administrative region.",
        }
        self.assertEqual(validate_response(response, self.request), [])

    def test_native_nullable_role_properties_are_normalized_before_local_validation(self) -> None:
        response = self._executable_response()
        response["requirements"][0]["properties"]["paragraph"] = None
        normalized = normalize_native_response(response, self.request["response_schema"])
        self.assertNotIn("paragraph", normalized["requirements"][0]["properties"])
        self.assertEqual(validate_response(normalized, self.request), [])

    def test_applicability_value_domain_covers_scalars_and_in_lists(self) -> None:
        schema = applicability_value_schema()
        for value in ("master", 2, 1.5, True, None, ["restricted", "classified"], [0, 2]):
            self.assertEqual(validate_instance(value, schema), [], value)

    def test_provenance_failures_are_structured_integrity_records(self) -> None:
        records = provenance_error_records(["run_id mismatch"], response=self._informational_response())
        self.assertEqual(records[0]["code"], "provenance_validation_error")
        self.assertTrue(records[0]["invocation_integrity_required"])
        self.assertFalse(records[0]["semantic_review_required"])

    def test_native_nullable_optionals_are_omitted_before_local_validation(self) -> None:
        response = self._executable_response()
        response["requirements"][0].update({
            "existing_requirement_id": None,
            "field_key": None,
            "applicability": None,
            "input_prerequisites": None,
        })
        response["requirements"][0]["verification"]["checker_ids"] = None
        response["clause_reviews"][0]["obligations"] = None
        normalized = normalize_native_response(response, self.request["response_schema"])
        requirement = normalized["requirements"][0]
        self.assertNotIn("existing_requirement_id", requirement)
        self.assertNotIn("field_key", requirement)
        self.assertNotIn("applicability", requirement)
        self.assertNotIn("input_prerequisites", requirement)
        self.assertNotIn("checker_ids", requirement["verification"])
        self.assertNotIn("obligations", normalized["clause_reviews"][0])
        self.assertEqual(validate_response(normalized, self.request), [])

    def test_exact_duplicate_requirements_are_explicitly_recorded(self) -> None:
        response = self._executable_response()
        duplicate = json.loads(json.dumps(response["requirements"][0]))
        response["requirements"].append(duplicate)
        from semantic_review_ledger import deduplicate_exact_requirements
        deduped, audit = deduplicate_exact_requirements(response)
        self.assertEqual(len(deduped["requirements"]), 1)
        self.assertEqual(audit["removed_indexes"], [1])

    def test_regression_a1_rejects_equation_shape_and_partial_table_coverage(self) -> None:
        clauses = [
            {"id": "C00243", "text": "表序后跟表题（可省略）和（续），居中置于表上方，续表均应重复表头。", "evidence_ids": ["E1"]},
            {"id": "C00252", "text": "公式按章编号。", "evidence_ids": ["E2"]},
        ]
        evidence = {"evidence": [
            {"id": "E1", "text": clauses[0]["text"], "kind": "paragraph"},
            {"id": "E2", "text": clauses[1]["text"], "kind": "paragraph"},
        ]}
        request = build_llm_request([], clauses, evidence, {}, "full", contract_version="2.1")
        request = attach_request_provenance(request, source_sha256="b" * 64, evidence_doc=evidence, clauses=clauses, run_id="a1")
        response = {
            "contract_version": "2.1", "provenance": request["provenance"],
            "requirements": [
                {"role": "table", "properties": {"continuation": {
                    "caption_suffix": "(续)", "repeat_header_row": True,
                    "caption_required_on_continuation": True, "verification": "word_render",
                }}, "clause_ids": ["C00243"], "evidence_ids": ["E1"], "confidence": 0.9, "reason": "table"},
                {"role": "equations", "properties": {"numbering": {"format": "chapter.decimal", "style": "decimal", "depth": 2}},
                 "clause_ids": ["C00252"], "evidence_ids": ["E2"], "confidence": 0.9, "reason": "equation"},
            ],
            "clause_reviews": [
                {"clause_id": "C00243", "classification": "executable", "requirement_indexes": [0], "reason": "table", "obligations": [{"id": "all", "status": "covered", "reason": "all"}]},
                {"clause_id": "C00252", "classification": "executable", "requirement_indexes": [1], "reason": "equation"},
            ],
            "unsupported_items": [], "reported_conflicts": [],
        }
        errors = validate_response(response, request)
        self.assertTrue(any("unknown property 'numbering'" in error for error in errors), errors)
        self.assertTrue(any("partial_clause_coverage" in error for error in errors), errors)

    def test_regression_a2_rejects_relation_drift_and_classification_as_basis(self) -> None:
        clauses = [{"id": "C00243", "text": "续表居中置于表上方并重复表头。", "evidence_ids": ["E1"]}]
        evidence = {"evidence": [{"id": "E1", "text": clauses[0]["text"], "kind": "paragraph"}]}
        request = build_llm_request([], clauses, evidence, {}, "full", contract_version="2.1")
        request = attach_request_provenance(request, source_sha256="c" * 64, evidence_doc=evidence, clauses=clauses, run_id="a2")
        response = {
            "contract_version": "2.1", "provenance": request["provenance"],
            "requirements": [
                {"role": "table", "properties": {"continuation": {"caption_suffix": "(续)", "repeat_header_row": True, "verification": "word_render"}}, "clause_ids": ["C00243"], "evidence_ids": ["E1"], "confidence": 0.9, "reason": "table"},
                {"role": "table_caption", "properties": {"position": "above", "paragraph": {"alignment": "center"}}, "clause_ids": ["C00245"], "evidence_ids": ["E1"], "confidence": 0.9, "reason": "caption"},
            ],
            "clause_reviews": [{"clause_id": "C00243", "classification": "executable", "requirement_indexes": [0, 1], "reason": "table", "normative_basis": "informational"}],
            "unsupported_items": [], "reported_conflicts": [],
        }
        errors = validate_response(response, request)
        self.assertTrue(any("normative_basis" in error for error in errors), errors)
        self.assertTrue(any("requirement_index_not_backed_by_clause" in error for error in errors), errors)


if __name__ == "__main__":
    unittest.main()
