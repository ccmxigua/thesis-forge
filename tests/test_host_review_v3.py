from __future__ import annotations

import copy
import json
import hashlib
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
    _abstract_obligation_gaps,
    _security_marking_qualifier_binding_errors,
    _table_obligation_gaps,
    _keyword_obligation_gaps,
    _literal_text_source_binding_error,
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


def _with_test_source_spans(clauses: list[dict], evidence_doc: dict) -> list[dict]:
    evidence_by_id = {
        str(item.get("id")): item
        for item in evidence_doc.get("evidence", [])
        if isinstance(item, dict) and item.get("id")
    }
    for clause in clauses:
        if isinstance(clause.get("source_span"), dict):
            continue
        evidence_ids = clause.get("evidence_ids") or []
        if len(evidence_ids) != 1:
            raise AssertionError("test source span needs exactly one evidence id")
        evidence_id = str(evidence_ids[0])
        source_text = evidence_by_id[evidence_id]["text"]
        clause_text = clause["text"]
        start = source_text.find(clause_text)
        if start < 0 or source_text.find(clause_text, start + 1) >= 0:
            raise AssertionError("test clause must occur uniquely in cited evidence")
        end = start + len(clause_text)
        clause["source_span"] = {
            "evidence_id": evidence_id,
            "start_offset": start,
            "end_offset": end,
            "text": source_text[start:end],
            "source_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
            "location": {},
        }
    return clauses


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
        self.clauses = _with_test_source_spans(self.clauses, self.evidence)
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
                "obligations": [{
                    "id": "source_clause",
                    "status": "covered",
                    "reason": "The cited source obligation is represented by the requirement.",
                }],
            }],
            "unsupported_items": [],
            "reported_conflicts": [],
        }

    def test_shorter_security_marking_fact_requires_exact_allowance_property(self) -> None:
        clause = {"id": "C50", "text": "注：限制★2年(可少于2年)"}
        requirement = {
            "role": "cover",
            "properties": {"non_public_administration": {
                "security_marking_options": [
                    {"label": "限制", "maximum_duration": {"value": 2, "unit": "年"}},
                    {"label": "秘密", "maximum_duration": {"value": 10, "unit": "年"}},
                ],
            }},
            "verification": {"checker_ids": ["cover_non_public_administration"]},
        }
        self.assertEqual(
            _table_obligation_gaps(clause, [requirement], [0]),
            ["cover.security_marking_options.shorter_duration_allowed"],
        )
        requirement["properties"]["non_public_administration"]["security_marking_options"][0][
            "shorter_duration_allowed"
        ] = True
        self.assertEqual(_table_obligation_gaps(clause, [requirement], [0]), [])
        requirement["properties"]["non_public_administration"]["security_marking_options"][0][
            "shorter_duration_allowed"
        ] = False
        self.assertEqual(
            _table_obligation_gaps(clause, [requirement], [0]),
            ["cover.security_marking_options.shorter_duration_allowed"],
        )

    def test_shorter_security_marking_flag_requires_direct_source_binding(self) -> None:
        option = {
            "label": "限制", "maximum_duration": {"value": 2, "unit": "年"},
            "shorter_duration_allowed": True,
        }
        requirement = {
            "role": "cover", "clause_ids": ["C50"],
            "properties": {"non_public_administration": {
                "security_marking_options": [option],
            }},
        }
        response = {"requirements": [requirement]}
        source_clause = {"id": "C50", "text": "注：限制★2年(可少于2年)"}
        self.assertEqual(
            _security_marking_qualifier_binding_errors(response, [source_clause]), [],
        )
        unrelated_clause = {"id": "C43", "text": "□限制(≤2年) □秘密(≤10年)"}
        requirement["clause_ids"] = ["C43"]
        self.assertEqual(
            _security_marking_qualifier_binding_errors(response, [unrelated_clause]),
            ["$.requirements[0].properties.non_public_administration"
             ".security_marking_options[0].shorter_duration_allowed: "
             "must_be_bound_to_linked_source_clause"],
        )
        requirement["clause_ids"] = ["C50"]
        option["shorter_duration_allowed"] = False
        self.assertEqual(
            _security_marking_qualifier_binding_errors(response, [source_clause]),
            ["$.requirements[0].properties.non_public_administration"
             ".security_marking_options[0].shorter_duration_allowed: "
             "must_be_source_compiled_true"],
        )

    def test_request_explains_multi_role_clause_and_equation_role_boundary(self) -> None:
        instructions = "\n".join(self.request["instructions"])
        self.assertIn("A single clause may support multiple requirements", instructions)
        self.assertIn("Role boundary for equations", instructions)
        self.assertIn("partial_clause_coverage error does not authorize changing classification", instructions)
        self.assertIn("executable_review_requires_all_obligations_covered", instructions)
        self.assertIn("not IDs to copy into obligations[]", instructions)

    def test_v3_requires_source_span_bound_to_exact_current_evidence(self) -> None:
        response = self._informational_response()
        mutations = {
            "missing": lambda clause: clause.pop("source_span"),
            "cross_evidence": lambda clause: clause["source_span"].update(evidence_id="E2"),
            "bad_offset": lambda clause: clause["source_span"].update(end_offset=999),
            "bad_hash": lambda clause: clause["source_span"].update(source_sha256="0" * 64),
            "changed_span_text": lambda clause: clause["source_span"].update(text="伪造来源"),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                chunk = json.loads(json.dumps(self.request))
                mutate(chunk["clauses"][0])
                errors = validate_response(response, chunk)
                self.assertTrue(errors, errors)

    def test_v3_request_builder_refuses_clause_without_bound_source_span(self) -> None:
        clauses = [{"id": "C1", "text": "正文使用宋体", "evidence_ids": ["E1"]}]
        with self.assertRaisesRegex(ValueError, "required_for_contract_3.0"):
            build_llm_request(
                [], clauses, self.evidence, {}, "full",
                contract_version=HOST_REVIEW_CONTRACT_V3,
            )

    def test_compiled_source_facts_are_independent_of_model_obligation_ids(self) -> None:
        source = "表序后跟表题(可省略)和“(续)”，居中置于表上方，续表均应重复表头"
        clauses = [{
            "id": "C00243", "text": source, "evidence_ids": ["E1"],
            "source_kind": "paragraph", "location": {}, "part_index": 0,
        }]
        evidence = {"evidence": [{"id": "E1", "text": source, "kind": "paragraph"}]}
        clauses = _with_test_source_spans(clauses, evidence)
        request = build_llm_request(
            [], clauses, evidence, {}, "full", contract_version=HOST_REVIEW_CONTRACT_V3,
        )
        request = attach_request_provenance(
            request, source_sha256="d" * 64, evidence_doc=evidence,
            clauses=clauses, run_id="compiled-obligation-test",
        )
        response = {
            "contract_version": HOST_REVIEW_CONTRACT_V3,
            "provenance": request["provenance"],
            "requirements": [
                {
                    "role": "table",
                    "properties": {"continuation": {
                        "caption_suffix": "(续)", "repeat_header_row": True,
                        "caption_required_on_continuation": False,
                        "verification": "word_render",
                    }},
                    "clause_ids": ["C00243"], "evidence_ids": ["E1"],
                    "confidence": 0.98, "reason": "续表结构属性。",
                    "verification": {
                        "mode": "word_render", "checks": ["核对续表属性。"],
                        "checker_ids": ["docx.property_receipts", "docx.word_render"],
                    },
                },
                {
                    "role": "table_caption",
                    "properties": {"position": "above", "paragraph": {"alignment": "center"}},
                    "clause_ids": ["C00243"], "evidence_ids": ["E1"],
                    "confidence": 0.98, "reason": "表题位置和对齐。",
                    "verification": {
                        "mode": "static_docx", "checks": ["核对表题位置和对齐。"],
                        "checker_ids": ["docx.property_receipts"],
                    },
                },
            ],
            "clause_reviews": [{
                "clause_id": "C00243", "classification": "executable",
                "reason": "续表的明确属性由独立 requirement 表达。",
                "obligations": [
                    {"id": "suffix", "status": "covered", "reason": "续页标记已绑定。"},
                    {"id": "headers", "status": "covered", "reason": "重复表头已绑定。"},
                    {"id": "caption-placement", "status": "covered", "reason": "表题位置和对齐已绑定。"},
                ],
            }],
            "unsupported_items": [], "reported_conflicts": [],
        }
        self.assertEqual(validate_response(response, request), [])

        ledger = build_semantic_review_ledger(response, clauses)
        inventory = ledger["clauses"][0]["source_obligation_inventory"]
        self.assertEqual(
            {item["id"] for item in inventory},
            {
                "table.continuation.caption_optional",
                "table.continuation.caption_suffix",
                "table.continuation.repeat_header_row",
                "table_caption.alignment_center",
                "table_caption.position_above",
            },
        )
        self.assertTrue(all(item["model_echo_required"] is False for item in inventory))
        self.assertTrue(all(item["checker_binding_status"] == "bound" for item in inventory))
        self.assertTrue(all(item["execution_receipt_status"] == "pending_generation" for item in inventory))
        self.assertFalse(ledger["clauses"][0]["source_obligation_inventory_complete"])
        self.assertEqual(ledger["source_obligation_inventory_scope"], "partial_machine_recognized_supplement")
        optionality = next(
            item for item in inventory
            if item["id"] == "table.continuation.caption_optional"
        )
        self.assertEqual(
            optionality["candidate_requirement_bindings"][0]["observed_value"], False,
        )
        self.assertEqual(
            optionality["candidate_requirement_bindings"][0]["requirement_id"],
            ledger["requirements"][0]["requirement_id"],
        )
        self.assertEqual(ledger["schema_version"], "1.2")

        invalid = json.loads(json.dumps(response))
        invalid["requirements"][0]["properties"]["continuation"][
            "caption_required_on_continuation"
        ] = True
        errors = validate_response(invalid, request)
        self.assertTrue(any(
            "partial_clause_coverage:table.continuation.caption_optional" in error
            for error in errors
        ), errors)
        self.assertNotIn("executable_review_missing_source_inventory", str(errors))

        missing_checker = json.loads(json.dumps(response))
        missing_checker["requirements"][0]["verification"]["checker_ids"].remove(
            "docx.word_render"
        )
        errors = validate_response(missing_checker, request)
        self.assertFalse(any("missing_checker" in error for error in errors), errors)
        from source_obligation_compiler import materialize_known_source_verification
        projected_checker, checker_repairs = materialize_known_source_verification(
            missing_checker, clauses,
        )
        self.assertTrue(checker_repairs)
        self.assertIn(
            "docx.word_render",
            projected_checker["requirements"][0]["verification"]["checker_ids"],
        )
        incomplete_ledger = build_semantic_review_ledger(projected_checker, clauses)
        optionality_record = next(
            item for item in incomplete_ledger["clauses"][0]["source_obligation_inventory"]
            if item["id"] == "table.continuation.caption_optional"
        )
        self.assertEqual(optionality_record["checker_binding_status"], "bound")
        self.assertEqual(optionality_record["missing_checker_ids"], [])

    def test_uncovered_obligation_retry_preserves_status_and_projects_relation(self) -> None:
        clauses = [
            {
                "id": "C1", "text": "图题应置于图下方，同时应简明并置于图序之后",
                "evidence_ids": ["E1"], "source_kind": "paragraph",
                "location": {}, "part_index": 0,
            },
            {
                "id": "C2", "text": "图题应置于图下方",
                "evidence_ids": ["E2"], "source_kind": "paragraph",
                "location": {}, "part_index": 0,
            },
        ]
        evidence = {
            "evidence": [
                {"id": "E1", "text": clauses[0]["text"], "kind": "paragraph"},
                {"id": "E2", "text": clauses[1]["text"], "kind": "paragraph"},
            ]
        }
        clauses = _with_test_source_spans(clauses, evidence)
        chunk = build_llm_request(
            [], clauses, evidence, {}, "full", contract_version=HOST_REVIEW_CONTRACT_V3,
        )
        previous = {
            "contract_version": HOST_REVIEW_CONTRACT_V3,
            "requirements": [{
                "role": "body_text", "properties": {"text": "图题应置于图下方"},
                "clause_ids": ["C1", "C2"], "evidence_ids": ["E1", "E2"],
                "confidence": 0.9, "reason": "图题位置要求。",
            }],
            "clause_reviews": [
                {
                    "clause_id": "C1", "classification": "executable",
                    "reason": "图题标题位置可核验，简明性尚不能由当前执行器核验。",
                    "obligations": [
                        {"id": "title_after_number", "status": "covered", "reason": "位置可核验。"},
                        {"id": "title_concise", "status": "unverifiable", "reason": "简明性需要语义判断。"},
                    ],
                },
                {"clause_id": "C2", "classification": "executable", "reason": "位置可核验。",
                 "obligations": [{"id": "placement", "status": "covered", "reason": "位置已表达。"}]},
            ],
            "unsupported_items": [],
            "reported_conflicts": [],
        }
        current = json.loads(json.dumps(previous, ensure_ascii=False))
        current["clause_reviews"][0]["classification"] = "unverifiable"
        current["requirements"][0]["clause_ids"] = ["C2"]
        # Leave the now-unrelated E1 citation in the candidate: the trusted
        # projection must prune it together with C1's removed requirement edge.
        records = [{
            "code": "executable_review_obligations_uncovered",
            "json_pointer": "$.clause_reviews[0].obligations",
            "raw_error": "$.clause_reviews[0].obligations: executable_review_requires_all_obligations_covered",
        }]

        self.assertEqual(
            validate_response(current, chunk),
            ["$.requirements[0].evidence_ids: not_backed_by_clause:E1"],
        )
        repaired, audit = bridge._v3_uncovered_obligation_reclassification_response(
            previous, current, records, chunk=chunk,
        )
        self.assertIsNotNone(repaired)
        self.assertEqual(repaired["clause_reviews"][0]["classification"], "unverifiable")
        self.assertEqual(
            [item["status"] for item in repaired["clause_reviews"][0]["obligations"]],
            ["covered", "unverifiable"],
        )
        self.assertEqual(repaired["requirements"][0]["clause_ids"], ["C2"])
        self.assertEqual(repaired["requirements"][0]["evidence_ids"], ["E2"])
        self.assertEqual(validate_response(repaired, chunk), [])
        self.assertEqual(audit["rule_id"], "preserve_uncovered_obligation_and_project_relation_v1")
        self.assertTrue(bridge._v3_uncovered_obligation_reclassification_allowed(
            previous, current, records, chunk=chunk,
        ))

        unsafe = json.loads(json.dumps(previous, ensure_ascii=False))
        unsafe["clause_reviews"][0]["obligations"][1]["status"] = "covered"
        unsafe["clause_reviews"][0]["classification"] = "executable"
        self.assertEqual(validate_response(unsafe, chunk), [])
        self.assertIsNone(
            bridge._v3_uncovered_obligation_reclassification_response(
                previous, unsafe, records, chunk=chunk,
            )[0]
        )
        self.assertFalse(bridge._retry_changes_allowed(
            records,
            bridge._retry_change_paths(previous, unsafe),
            contract_version=HOST_REVIEW_CONTRACT_V3,
            previous_response=previous,
            current_response=unsafe,
            chunk=chunk,
        ))

    def test_uncovered_obligation_projection_retains_shared_evidence(self) -> None:
        clauses = [
            {
                "id": "C1", "text": "图题应置于图下方；简明",
                "evidence_ids": ["E1"], "source_kind": "paragraph",
                "location": {}, "part_index": 0,
            },
            {
                "id": "C2", "text": "图题应置于图下方",
                "evidence_ids": ["E1"], "source_kind": "paragraph",
                "location": {}, "part_index": 0,
            },
        ]
        evidence = {
            "evidence": [{
                "id": "E1", "text": "图题应置于图下方；简明", "kind": "paragraph",
            }]
        }
        clauses = _with_test_source_spans(clauses, evidence)
        chunk = build_llm_request(
            [], clauses, evidence, {}, "full", contract_version=HOST_REVIEW_CONTRACT_V3,
        )
        previous = {
            "contract_version": HOST_REVIEW_CONTRACT_V3,
            "requirements": [{
                "role": "body_text", "properties": {"text": "图题应置于图下方"},
                "clause_ids": ["C1", "C2"], "evidence_ids": ["E1"],
                "confidence": 0.9, "reason": "图题位置要求。",
            }],
            "clause_reviews": [
                {
                    "clause_id": "C1", "classification": "executable",
                    "reason": "简明性尚不能由当前执行器核验。",
                    "obligations": [
                        {"id": "placement", "status": "covered", "reason": "位置已表达。"},
                        {"id": "conciseness", "status": "unverifiable", "reason": "需人工判断。"},
                    ],
                },
                {
                    "clause_id": "C2", "classification": "executable",
                    "reason": "位置可核验。",
                    "obligations": [{
                        "id": "placement", "status": "covered", "reason": "位置已表达。",
                    }],
                },
            ],
            "unsupported_items": [],
            "reported_conflicts": [],
        }
        current = json.loads(json.dumps(previous, ensure_ascii=False))
        current["clause_reviews"][0]["classification"] = "unverifiable"
        current["requirements"][0]["clause_ids"] = ["C2"]
        records = [{
            "code": "executable_review_obligations_uncovered",
            "json_pointer": "$.clause_reviews[0].obligations",
            "raw_error": (
                "$.clause_reviews[0].obligations: "
                "executable_review_requires_all_obligations_covered"
            ),
        }]

        self.assertEqual(validate_response(current, chunk), [])
        repaired, audit = bridge._v3_uncovered_obligation_reclassification_response(
            previous, current, records, chunk=chunk,
        )
        self.assertIsNotNone(repaired)
        self.assertEqual(repaired["requirements"][0]["clause_ids"], ["C2"])
        self.assertEqual(repaired["requirements"][0]["evidence_ids"], ["E1"])
        self.assertEqual(validate_response(repaired, chunk), [])
        self.assertEqual(audit["removed_clause_edges"], ["C1"])

    def test_uncovered_obligation_error_is_structured(self) -> None:
        raw_error = "$.clause_reviews[0].obligations: executable_review_requires_all_obligations_covered"
        records = contract_error_records(
            [raw_error], response=self._executable_response(), chunk=self.request,
        )
        self.assertEqual(records[0]["code"], "executable_review_obligations_uncovered")
        self.assertTrue(records[0]["semantic_review_required"])

    def test_reported_conflicts_are_bound_to_current_chunk_facts(self) -> None:
        response = self._informational_response()
        response["reported_conflicts"] = [{
            "type": "source_conflict",
            "reason": "Two cited statements disagree about the same property.",
            "clause_ids": ["C1"],
            "evidence_ids": ["E1"],
            "target": {"role": "body_text", "property": "font"},
            "candidates": [{
                "evidence_id": "E1", "value_type": "object",
                "value_json": json.dumps({"cjk": "SimSun", "size_pt": 12}),
            }],
            "status": "unresolved",
        }]
        self.assertEqual(validate_response(response, self.request), [])

    def test_typed_conflict_values_survive_native_schema_and_match_target_property(self) -> None:
        response = self._informational_response()
        conflict = {
            "type": "source_conflict",
            "reason": "Two sources disagree about font payload.",
            "clause_ids": ["C1"],
            "evidence_ids": ["E1"],
            "target": {"role": "body_text", "property": "font"},
            "conditions": [{"evidence_id": "E1", "statement": "When the body is Chinese."}],
            "candidates": [{
                "evidence_id": "E1", "value_type": "object",
                "value_json": json.dumps({"cjk": "SimSun", "size_pt": 12}),
            }],
            "status": "requires_human_review",
        }
        response["reported_conflicts"] = [conflict]
        self.assertEqual(validate_response(response, self.request), [])

        native = native_output_schema(self.request["response_schema"])
        self.assertEqual(native_schema_support_errors(native), [])
        conflict_item = native["properties"]["reported_conflicts"]["items"]
        self.assertEqual(conflict_item["$ref"], "#/$defs/reportedConflict")
        candidates_schema = native["$defs"]["reportedConflict"]["properties"]["candidates"]
        candidate_array_schema = next(
            item for item in candidates_schema["anyOf"] if item.get("type") == "array"
        )
        candidate_branches = candidate_array_schema["items"]["anyOf"]
        self.assertEqual(len(candidate_branches), 2)
        encoded_branch = next(
            item for item in candidate_branches
            if "value_json" in item.get("properties", {})
        )
        self.assertIn("value_json", encoded_branch["required"])

        wrong_type = json.loads(json.dumps(response))
        wrong_type["reported_conflicts"][0]["candidates"][0]["value_json"] = json.dumps("SimSun")
        wrong_type["reported_conflicts"][0]["candidates"][0]["value_type"] = "string"
        self.assertIn("does_not_match_target_property_schema", str(
            validate_response(wrong_type, self.request)
        ))

    def test_conflict_target_supports_nested_and_identity_selected_properties(self) -> None:
        cases = [
            ({"role": "body_text", "property": "font.size_pt"}, 12),
            ({"role": "cover", "property": "fields[classification_number].label"}, "分类号"),
        ]
        for target, value in cases:
            with self.subTest(target=target):
                response = self._informational_response()
                response["reported_conflicts"] = [{
                    "type": "source_conflict",
                    "reason": "同一版式属性存在两个来源候选。",
                    "clause_ids": ["C1"],
                    "evidence_ids": ["E1"],
                    "target": target,
                    "candidates": [{"evidence_id": "E1", "value": value}],
                    "status": "unresolved",
                }]
                self.assertEqual(validate_response(response, self.request), [])

        unknown_selector = self._informational_response()
        unknown_selector["reported_conflicts"] = [{
            "type": "source_conflict",
            "reason": "候选不应被猜测。",
            "clause_ids": ["C1"], "evidence_ids": ["E1"],
            "target": {"role": "cover", "property": "fields[not_a_field].label"},
            "candidates": [{"evidence_id": "E1", "value": "未知字段"}],
            "status": "unresolved",
        }]
        self.assertIn("unknown_registered_role_property", str(
            validate_response(unknown_selector, self.request)
        ))

    def test_conflict_candidate_strict_json_and_condition_evidence_are_checked(self) -> None:
        response = self._informational_response()
        response["reported_conflicts"] = [{
            "type": "semantic_conflict",
            "reason": "The alternatives cannot be reconciled.",
            "clause_ids": ["C1"],
            "evidence_ids": ["E1"],
            "conditions": [{"evidence_id": "E999", "statement": "Only under case X."}],
            "candidates": [{
                "evidence_id": "E1", "value_type": "object",
                "value_json": '{"font":"A","font":"B"}',
            }],
            "status": "unresolved",
        }]
        errors = validate_response(response, self.request)
        self.assertIn("value_json_invalid:duplicate JSON object key", str(errors))
        self.assertIn("conditions[0].evidence_id: must_reference_conflict_evidence", str(errors))

    def test_reported_conflicts_reject_unknown_unbound_and_unregistered_refs(self) -> None:
        base_conflict = {
            "type": "semantic_conflict",
            "reason": "The evidence cannot be reconciled automatically.",
            "clause_ids": ["C1"],
            "evidence_ids": ["E1"],
            "status": "requires_human_review",
        }
        cases = [
            ({**base_conflict, "clause_ids": ["C999"]}, "clause_ids: unknown:C999"),
            ({**base_conflict, "evidence_ids": ["E999"]}, "evidence_ids: not_in_chunk:E999"),
            ({**base_conflict, "candidates": [{"evidence_id": "E2", "value": "x"}]},
             "must_reference_conflict_evidence"),
            ({**base_conflict, "target": {"role": "body_text", "property": "not_registered"}},
             "unknown_registered_role_property"),
        ]
        for conflict, expected in cases:
            with self.subTest(expected=expected):
                response = self._informational_response()
                response["reported_conflicts"] = [conflict]
                request = json.loads(json.dumps(self.request))
                request["evidence_context"]["E2"] = {"id": "E2", "text": "other"}
                errors = validate_response(response, request)
                self.assertIn(expected, str(errors))

    def test_v3_schema_has_one_model_authoritative_relation(self) -> None:
        reviews_schema = self.request["response_schema"]["properties"]["clause_reviews"]["items"]
        self.assertNotIn("requirement_indexes", str(reviews_schema))
        executable_branch = next(
            branch for branch in reviews_schema["anyOf"]
            if "executable" in branch["properties"]["classification"]["enum"]
        )
        informational_branch = next(
            branch for branch in reviews_schema["anyOf"]
            if "informational" in branch["properties"]["classification"]["enum"]
        )
        self.assertIn("obligations", executable_branch["required"])
        self.assertEqual(executable_branch["properties"]["obligations"]["minItems"], 1)
        self.assertNotIn("obligations", informational_branch["required"])
        self.assertNotIn("Maintain requirement_indexes exactly", str(self.request["instructions"]))
        self.assertNotIn("zero-based index", str(self.request["instructions"]))
        self.assertNotIn("review/index pair", str(self.request["instructions"]))
        # The schema itself, rather than prose, is the enforcement boundary.
        self.assertEqual(validate_response(self._informational_response(), self.request), [])

    def test_keyword_validator_uses_exact_full_source_and_blocks_unparsed_hard_signal(self) -> None:
        clause = {
            "id": "C72",
            "text": "at least 3 groups, with a maximum of 8 sets",
            "source_text_full": "Key Words: Terminology; at least 3 groups, with a maximum of 8 sets",
            "source_span": {"text": "at least 3 groups, with a maximum of 8 sets"},
            "evidence_ids": ["E72"],
        }
        requirements = [{
            "role": "content_constraints",
            "properties": {"keywords_en": {
                "max_item_chars": 7,
                "item_length_metric": "cjk_characters",
                "min_count": 3,
                "max_count": 8,
            }},
        }]
        gaps = _keyword_obligation_gaps(clause, requirements, [0])
        self.assertIn("keywords_en.hard_count_range:unresolved_source", gaps)

    def test_keyword_validator_inherits_subject_from_exact_source_span(self) -> None:
        clause = {
            "id": "C72",
            "text": "at least 3 groups, with a maximum of 8 sets",
            "source_span": {
                "text": "Key Words: at least 3 groups, with a maximum of 8 sets",
            },
            "evidence_ids": ["E72"],
        }
        requirements = [{
            "role": "content_constraints",
            "properties": {"keywords_en": {
                "max_item_chars": 7,
                "item_length_metric": "cjk_characters",
            }},
        }]
        gaps = _keyword_obligation_gaps(clause, requirements, [0])
        self.assertIn("keywords_en.hard_count_range:unresolved_source", gaps)

    def test_full_request_retains_bound_context_for_split_hard_keyword_range(self) -> None:
        source = "关键词 最少3组，最多8组。"
        start = source.index("最少")
        end = source.rindex("。")
        exact_clause = source[start:end]
        evidence = {"evidence": [{"id": "E72", "text": source, "kind": "paragraph"}]}
        clause = {
            "id": "C72", "text": exact_clause, "evidence_ids": ["E72"],
            "source_text_full": source,
            "source_span": {
                "evidence_id": "E72", "start_offset": start, "end_offset": end,
                "text": exact_clause,
                "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                "location": {},
            },
        }
        request = build_llm_request(
            [], [clause], evidence, {}, "full", contract_version=HOST_REVIEW_CONTRACT_V3,
        )
        packet = request["clauses"][0]
        self.assertEqual(packet["source_text_full"], source)
        gaps = _keyword_obligation_gaps(
            packet,
            [{"role": "content_constraints", "properties": {"keywords_zh": {}}}],
            [0],
        )
        self.assertIn("keywords_zh.min_count", gaps)
        self.assertIn("keywords_zh.max_count", gaps)

    def test_v3_literal_text_must_match_exact_bound_evidence(self) -> None:
        def response_errors(source: str, candidate: str) -> list[str]:
            clauses = [{"id": "C1", "text": source, "evidence_ids": ["E1"]}]
            evidence = {"evidence": [{"id": "E1", "text": source, "kind": "paragraph"}]}
            clauses = _with_test_source_spans(clauses, evidence)
            request = build_llm_request(
                [], clauses, evidence, {}, "full", contract_version=HOST_REVIEW_CONTRACT_V3,
            )
            request = attach_request_provenance(
                request, source_sha256="b" * 64, evidence_doc=evidence,
                clauses=clauses, run_id="literal-source-test",
            )
            response = self._executable_response()
            response["provenance"] = request["provenance"]
            response["requirements"][0]["role"] = "cover_field_label"
            response["requirements"][0]["properties"] = {"text": candidate}
            response["requirements"][0]["clause_ids"] = ["C1"]
            response["requirements"][0]["evidence_ids"] = ["E1"]
            return validate_response(response, request)

        self.assertEqual(response_errors("分类号", "分类号"), [])
        for source, mutated in (
            ("分类号", "分类号："),
            ("密  级：", "密 级："),
            ("关键词；", "关键词,"),
        ):
            with self.subTest(source=source, mutated=mutated):
                errors = response_errors(source, mutated)
                self.assertTrue(any(
                    "must_be_exact_substring_of_cited_source_span" in error
                    for error in errors
                ), errors)

    def test_v3_literal_text_cannot_cross_pair_a_clause_with_nonprimary_evidence(self) -> None:
        clauses = [
            {"id": "C1", "text": "封面字段标签", "evidence_ids": ["E1"]},
            {"id": "C2", "text": "分类号", "evidence_ids": ["E2"]},
        ]
        evidence = {
            "evidence": [
                {"id": "E1", "text": "封面字段标签", "kind": "paragraph"},
                {"id": "E2", "text": "分类号", "kind": "paragraph"},
            ]
        }
        clauses = _with_test_source_spans(clauses, evidence)
        # E2 is associated as secondary context for C1, but C1's exact primary
        # source span remains E1. The union must not let E2 stand in for E1.
        clauses[0]["evidence_ids"].append("E2")
        request = build_llm_request(
            [], clauses, evidence, {}, "full", contract_version=HOST_REVIEW_CONTRACT_V3,
        )
        request = attach_request_provenance(
            request, source_sha256="c" * 64, evidence_doc=evidence,
            clauses=clauses, run_id="cross-paired-literal-test",
        )
        response = self._executable_response()
        response["provenance"] = request["provenance"]
        response["requirements"][0].update({
            "role": "cover_field_label",
            "properties": {"text": "分类号"},
            "clause_ids": ["C1", "C2"],
            "evidence_ids": ["E2"],
        })
        response["clause_reviews"] = [
            {
                "clause_id": clause_id,
                "classification": "executable",
                "reason": "The source-bound clause was reviewed.",
                "obligations": [{
                    "id": "source_clause", "status": "covered",
                    "reason": "The source clause is represented.",
                }],
            }
            for clause_id in ("C1", "C2")
        ]
        errors = validate_response(response, request)
        self.assertTrue(any(
            "must_cite_primary_source_span_evidence:C1" in error
            for error in errors
        ), errors)

    def test_v3_literal_binding_checks_every_clause_before_accepting_any_match(self) -> None:
        evidence = {
            "E1": {"text": "标签"},
            "E2": {"text": "另一来源"},
        }
        clauses = {
            "C1": {"source_span": {
                "evidence_id": "E1", "start_offset": 0, "end_offset": 2,
            }},
            "C2": {"source_span": {
                "evidence_id": "E2", "start_offset": 0, "end_offset": 4,
            }},
        }
        for clause_ids in (["C1", "C2"], ["C2", "C1"]):
            with self.subTest(clause_ids=clause_ids):
                error = _literal_text_source_binding_error(
                    {
                        "properties": {"text": "标签"},
                        "clause_ids": clause_ids,
                        "evidence_ids": ["E1"],
                    },
                    0,
                    clauses,
                    evidence,
                )
                self.assertIn("must_cite_primary_source_span_evidence:C2", error or "")

        valid = _literal_text_source_binding_error(
            {
                "properties": {"text": "标签"},
                "clause_ids": ["C2", "C1"],
                "evidence_ids": ["E1", "E2"],
            },
            0,
            clauses,
            evidence,
        )
        self.assertIsNone(valid)

    def test_c66_abstract_bundle_requires_each_explicit_source_obligation(self) -> None:
        source = (
            "中文摘要是论文内容的简要陈述，一般以第三人称语气撰写，"
            "300～1000字（如遇特殊需要字数可以略多），不加评论和解释，"
            "是一篇具有独立性和完整性的短文，能准确反映论文的中心思想，"
            "规范的学术用语，逻辑性强、结构严谨，体现出论文的新理论、新方法、新技术等"
        )
        from source_obligation_compiler import compile_abstract_source_constraints

        compiled = compile_abstract_source_constraints(source)
        self.assertIsNotNone(compiled)
        properties = compiled["properties"]
        expected = {
            "brief_statement_of_thesis_content",
            "new_theory_method_technology",
        }
        self.assertTrue(expected.issubset(set(properties["quality_guidance"])))
        for key in expected:
            partial = dict(properties)
            partial["quality_guidance"] = [
                value for value in properties["quality_guidance"] if value != key
            ]
            gaps = _abstract_obligation_gaps(
                {"text": source, "source_span": {"text": source}},
                [{"role": "content_constraints", "properties": {"abstract_zh": partial}}],
                [0],
            )
            self.assertIn(f"abstract_zh.quality_guidance:{key}", gaps)

    def test_keyword_validator_keeps_unitless_mandatory_range_unresolved(self) -> None:
        source = "关键词最少3，最多8"
        clause = {
            "id": "C81", "text": source, "evidence_ids": ["E81"],
            "source_span": {"text": source},
        }
        requirements = [{
            "role": "content_constraints",
            "properties": {"keywords_zh": {"min_count": 3, "max_count": 8}},
        }]
        gaps = _keyword_obligation_gaps(clause, requirements, [0])
        self.assertIn("keywords_zh.hard_count_range:unresolved_source", gaps)

    def test_v3_provider_schema_requires_non_null_non_empty_executable_inventory(self) -> None:
        local = self.request["response_schema"]
        provider = native_output_schema(local)
        self.assertEqual(native_schema_support_errors(provider), [])
        branches = provider["properties"]["clause_reviews"]["items"]["anyOf"]
        executable = next(
            branch for branch in branches
            if "executable" in branch["properties"]["classification"]["enum"]
        )
        self.assertIn("obligations", executable["required"])
        self.assertEqual(executable["properties"]["obligations"]["type"], "array")
        self.assertNotIn("null", str(executable["properties"]["obligations"]))

        for obligations in (None, []):
            response = self._executable_response()
            response["clause_reviews"][0]["obligations"] = obligations
            errors = validate_response(response, self.request)
            self.assertTrue(errors, obligations)
            self.assertIn("review_requires_non_empty_source_inventory", str(errors))
            normalized = normalize_native_response(response, local)
            self.assertEqual(normalized["clause_reviews"][0]["obligations"], obligations)

        response = self._executable_response()
        response["clause_reviews"][0].pop("obligations")
        errors = validate_response(response, self.request)
        self.assertTrue(errors)
        self.assertIn("review_requires_non_empty_source_inventory", str(errors))

    def test_v3_external_compliance_does_not_require_docx_obligation_inventory(self) -> None:
        response = self._informational_response()
        review = response["clause_reviews"][0]
        review.update({
            "classification": "external_compliance",
            "normative_basis": "external_duty",
            "reason": "This duty is completed outside the DOCX workflow.",
        })

        for obligations in (
            None,
            [],
            [{
                "id": "external-signature",
                "status": "unverifiable",
                "reason": "The signature is verified by an external process.",
            }],
        ):
            with self.subTest(obligations=obligations):
                candidate = copy.deepcopy(response)
                candidate["clause_reviews"][0]["obligations"] = obligations
                normalized = normalize_native_response(
                    candidate, self.request["response_schema"],
                )
                if obligations is None:
                    self.assertNotIn("obligations", normalized["clause_reviews"][0])
                self.assertEqual(validate_response(normalized, self.request), [])

    def test_legacy_21_review_schema_keeps_legacy_contract_shape(self) -> None:
        legacy = build_llm_request(
            [], self.clauses, self.evidence, {}, "full", contract_version="2.1",
        )
        item = legacy["response_schema"]["properties"]["clause_reviews"]["items"]
        self.assertIn("properties", item)
        self.assertIn("requirement_indexes", item["properties"])
        self.assertNotIn("anyOf", item)

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

    def test_ledger_preserves_model_obligations_without_inference(self) -> None:
        response = self._executable_response()
        ledger = build_semantic_review_ledger(response, self.clauses)
        self.assertEqual(ledger["relationship_policy"]["authoritative_edge"], "requirements[].clause_ids")
        self.assertEqual(ledger["edges"][0]["requirement_index"], 0)
        self.assertRegex(ledger["edges"][0]["requirement_id"], r"^R[0-9a-f]{16}$")
        self.assertEqual(ledger["requirements"][0]["requirement_id"], ledger["edges"][0]["requirement_id"])
        self.assertEqual(ledger["clauses"][0]["obligations"], response["clause_reviews"][0]["obligations"])
        self.assertEqual(
            ledger["clauses"][0]["obligation_decomposition"],
            "model_semantic_items_plus_code_compiled_source_facts",
        )
        informational = self._informational_response()
        informational_ledger = build_semantic_review_ledger(informational, self.clauses)
        self.assertEqual(informational_ledger["clauses"][0]["obligations"], [])
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

    def test_duplicate_requirement_evidence_ids_are_a_bounded_mechanical_error(self) -> None:
        response = self._executable_response()
        response["requirements"][0]["evidence_ids"] = ["E1", "E1"]
        errors = validate_response(response, self.request)
        records = contract_error_records(errors, response=response, chunk=self.request)
        self.assertEqual(
            [
                record["code"] for record in records
                if "evidence_ids" in str(record["raw_error"])
            ],
            ["duplicate_evidence_ids"],
        )

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

    def test_fixed_declaration_text_preserves_source_whitespace_and_punctuation_exactly(self) -> None:
        source = "密  级："
        clauses = [{"id": "C1", "text": source, "evidence_ids": ["E1"]}]
        evidence = {"evidence": [{"id": "E1", "text": source, "kind": "paragraph"}]}
        clauses = _with_test_source_spans(clauses, evidence)
        request = build_llm_request(
            [], clauses, evidence, {}, "full", contract_version=HOST_REVIEW_CONTRACT_V3,
        )
        request = attach_request_provenance(
            request, source_sha256="a" * 64, evidence_doc=evidence,
            clauses=clauses, run_id="fixed-declaration-exactness",
        )
        response = self._executable_response()
        response["provenance"] = request["provenance"]
        response["requirements"][0].update({
            "role": "declarations",
            "properties": {
                "before_role": "document_start",
                "items": [{
                    "id": "security-label",
                    "heading": source,
                    "body_parts": [source],
                    "signature_placeholders": [],
                }],
            },
        })
        errors = validate_response(response, request)
        self.assertFalse(any("fixed_text" in error for error in errors), errors)

        response["requirements"][0]["properties"]["items"][0]["heading"] += "："
        response["requirements"][0]["properties"]["items"][0]["body_parts"] = ["密 级："]
        errors = validate_response(response, request)
        fixed_text_errors = [error for error in errors if "complete cited source-evidence text" in error]
        self.assertEqual(len(fixed_text_errors), 2, errors)

    def test_compatibility_declaration_body_is_source_bound_like_body_parts(self) -> None:
        source = "固定声明正文：本人已知悉相关规定。"
        clauses = [{"id": "C1", "text": source, "evidence_ids": ["E1"]}]
        evidence = {"evidence": [{"id": "E1", "text": source, "kind": "paragraph"}]}
        clauses = _with_test_source_spans(clauses, evidence)
        request = build_llm_request(
            [], clauses, evidence, {}, "full", contract_version=HOST_REVIEW_CONTRACT_V3,
        )
        request = attach_request_provenance(
            request, source_sha256="d" * 64, evidence_doc=evidence,
            clauses=clauses, run_id="declaration-body-exactness",
        )
        response = self._executable_response()
        response["provenance"] = request["provenance"]
        response["requirements"][0].update({
            "role": "declarations",
            "properties": {
                "before_role": "document_start",
                "items": [{
                    "id": "declaration",
                    "body": "伪造声明正文",
                    "signature_placeholders": [],
                }],
            },
        })
        errors = validate_response(response, request)
        self.assertTrue(any(
            ".body: must exactly equal a complete cited source-evidence text" in error
            for error in errors
        ), errors)

        response["requirements"][0]["properties"]["items"][0]["body"] = source
        self.assertFalse(validate_response(response, request))

    def test_missing_v3_requirement_relation_is_structured_as_relation_error(self) -> None:
        records = contract_error_records(
            [
                "$.clause_reviews[0]: executable_review_requires_derived_requirement",
                "requirements_not_referenced_by_clause_review:0",
            ],
            response=self._executable_response(),
            chunk=self.request,
        )
        self.assertEqual(
            [record["code"] for record in records],
            ["missing_derived_requirement", "unused_executable_requirement"],
        )
        self.assertEqual(records[0]["clause_id"], "C1")

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
            clauses = _with_test_source_spans(clauses, evidence)
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
        nonrequirement_record = next(
            item for item in nonrequirement_records
            if item["code"] == "non_requirement_classification_relation"
        )
        self.assertEqual(nonrequirement_record["requirement_index"], 0)
        self.assertEqual(nonrequirement_record["relation_category"], "non_requirement_classification")
        self.assertEqual(nonrequirement_record["clause_ids"], ["C1"])
        self.assertIs(nonrequirement_record["mechanically_removable"], False)

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
        requirement_branches = native_schema["properties"]["requirements"]["items"]["anyOf"]
        body_text_branch = next(
            branch for branch in requirement_branches
            if branch["properties"]["role"].get("enum") == ["body_text"]
        )
        requirement_properties = body_text_branch["properties"]["properties"]
        self.assertIn("$ref", requirement_properties)
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

    def test_requirement_native_schema_binds_role_to_payload_and_normalizes_by_role(self) -> None:
        native_schema = native_output_schema(self.request["response_schema"])
        branches = native_schema["properties"]["requirements"]["items"]["anyOf"]
        roles = {
            branch["properties"]["role"]["enum"][0]
            for branch in branches
            if isinstance(branch.get("properties", {}).get("role"), dict)
        }
        self.assertIn("table_caption", roles)
        self.assertIn("table", roles)

        wrong_role = self._executable_response()
        wrong_role["requirements"][0]["role"] = "table_caption"
        wrong_role["requirements"][0]["properties"] = {
            "continuation": {"caption_suffix": "(续)", "repeat_header_row": True}
        }
        errors = validate_response(wrong_role, self.request)
        self.assertTrue(any("must match at least one schema" in error for error in errors), errors)

        table_response = self._executable_response()
        table_response["requirements"][0]["role"] = "table"
        table_response["requirements"][0]["properties"] = {
            "continuation": {"caption_suffix": "(续)", "repeat_header_row": True}
        }
        table_response["requirements"][0]["properties"]["continuation"]["caption_required_on_continuation"] = None
        normalized = normalize_native_response(table_response, self.request["response_schema"])
        self.assertNotIn(
            "caption_required_on_continuation",
            normalized["requirements"][0]["properties"]["continuation"],
        )

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

    def test_source_marking_options_are_projected_then_exactly_validated(self) -> None:
        source = "密级：□限制(≤2年) □秘密(≤10年) □机密(≤20年)"
        clauses = [{
            "id": "C43", "text": source, "evidence_ids": ["E43"],
            "source_kind": "table_cell", "location": {}, "part_index": 0,
        }]
        evidence = {"evidence": [{"id": "E43", "text": source, "kind": "table_cell"}]}
        clauses = _with_test_source_spans(clauses, evidence)
        request = build_llm_request(
            [], clauses, evidence, {}, "full", contract_version=HOST_REVIEW_CONTRACT_V3,
        )
        request = attach_request_provenance(
            request, source_sha256="a" * 64, evidence_doc=evidence, clauses=clauses,
            run_id="security-marking-test",
        )
        response = {
            "contract_version": HOST_REVIEW_CONTRACT_V3,
            "provenance": request["provenance"],
            "requirements": [{
                "role": "cover",
                "properties": {
                    "institution": "——", "fields": [],
                    "non_public_administration": {
                        "applicability": {"status": "conditional", "conditions": [{
                            "fact": "thesis_profile.security_level", "operator": "in",
                            "value": ["restricted", "classified"],
                        }]},
                        "fields": [{
                            "id": "security_marking", "label": "密级",
                            "value_from": "thesis_profile.cover_metadata.security_marking",
                            "display_policy": "blank_when_public", "order": 1,
                        }],
                        "public_policy": "blank", "source_region": "E43",
                    },
                },
                "clause_ids": ["C43"], "evidence_ids": ["E43"],
                "confidence": 0.9, "reason": "Preserve the source administrative choices.",
            }],
            "clause_reviews": [{
                "clause_id": "C43", "classification": "executable",
                "reason": "The source gives explicit security choices and limits.",
                "obligations": [{
                    "id": "security_marking_options", "status": "covered",
                    "reason": "The exact choices are preserved by the source-bound projection.",
                }],
            }],
            "unsupported_items": [], "reported_conflicts": [],
        }
        self.assertEqual(validate_response(response, request), [])

        conflicting = json.loads(json.dumps(response, ensure_ascii=False))
        conflicting["requirements"][0]["properties"]["non_public_administration"][
            "security_marking_options"
        ] = [{"label": "限制", "maximum_duration": {"value": 9, "unit": "年"}}]
        errors = validate_response(conflicting, request)
        self.assertTrue(any("cover.security_marking_options" in error for error in errors), errors)

    def test_native_nullable_role_properties_are_normalized_before_local_validation(self) -> None:
        response = self._executable_response()
        response["requirements"][0]["properties"]["paragraph"] = None
        normalized = normalize_native_response(response, self.request["response_schema"])
        self.assertNotIn("paragraph", normalized["requirements"][0]["properties"])
        errors = validate_response(normalized, self.request)
        self.assertEqual(errors, [])

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
        self.assertIsNone(normalized["clause_reviews"][0]["obligations"])
        errors = validate_response(normalized, self.request)
        self.assertIn("review_requires_non_empty_source_inventory", str(errors))

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
