from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import host_agent_bridge as bridge  # noqa: E402
import requirements_engine as engine  # noqa: E402
from semantic_contract import attach_request_provenance  # noqa: E402


class HostAgentBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._host_runtime_env = patch.dict(
            os.environ,
            {"THESIS_FORGE_HOST_RUNTIME": "openclaw"},
        )
        self._host_runtime_env.start()

    def tearDown(self) -> None:
        self._host_runtime_env.stop()

    def _packet(self, directory: Path) -> tuple[Path, dict]:
        directory.mkdir(parents=True, exist_ok=True)
        clauses = [{
            "id": "C1", "text": "正文使用宋体", "evidence_ids": ["E1"],
            "source_kind": "paragraph", "location": {}, "part_index": 0,
        }]
        evidence = {"evidence": [{"id": "E1", "text": "正文使用宋体", "kind": "paragraph"}]}
        request = engine.build_llm_request([], clauses, evidence, {}, "full")
        request = attach_request_provenance(
            request, source_sha256="a" * 64, evidence_doc=evidence, clauses=clauses,
            run_id="run-bridge-test",
        )
        engine.prepare_host_agent_review_packets(
            request, clauses, evidence, "a" * 64, directory, chunk_size=1,
        )
        chunk = json.loads((directory / "llm-request-chunks.json").read_text(encoding="utf-8"))[0]
        return directory, chunk

    def _response(self, chunk: dict) -> dict:
        return {
            "contract_version": "2.1",
            "provenance": chunk["provenance"],
            "requirements": [],
            "clause_reviews": [{
                "clause_id": "C1",
                "classification": "informational",
                "requirement_indexes": [],
                "reason": "The current evidence was reviewed.",
            }],
            "unsupported_items": [],
            "reported_conflicts": [],
        }

    def _executable_response(self, chunk: dict, *, invalid_verification: bool = False) -> dict:
        response = self._response(chunk)
        response["requirements"] = [{
            "role": "body_text",
            "properties": {"font": {"cjk": "SimSun", "size_pt": 12}},
            "clause_ids": ["C1"],
            "evidence_ids": ["E1"],
            "confidence": 0.9,
            "reason": "The clause specifies the body-text font.",
            "verification": "word_render" if invalid_verification else {
                "mode": "word_render",
                "checks": ["Check the body-text font."],
            },
        }]
        response["clause_reviews"] = [{
            "clause_id": "C1",
            "classification": "executable",
            "requirement_indexes": [0],
            "reason": "The clause is executable in DOCX.",
        }]
        return response

    def test_compact_model_packet_preserves_the_machine_readable_contract(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _review_dir, chunk = self._packet(Path(td) / "requirements")
            chunk["source_continuity_context"] = {
                "policy": "orientation_only_not_for_review",
                "preceding_clauses": [{"id": "C0", "text": "前一段"}],
                "following_clauses": [{"id": "C2", "text": "后一段"}],
            }
            packet = bridge.compact_model_packet(chunk)
            contract = packet["requirement_contract"]
            self.assertEqual(contract["allowed_roles"], chunk["requirement_contract"]["allowed_roles"])
            self.assertEqual(
                contract["role_properties_schema"],
                chunk["requirement_contract"]["role_properties_schema"],
            )
            self.assertIn("roleSpec", contract["$defs"])
            self.assertIn("verificationSpec", packet["response_schema"]["$defs"])
            self.assertIn("inputPrerequisiteSpec", packet["response_schema"]["$defs"])
            self.assertNotIn("provenance", packet)
            self.assertEqual(
                packet["source_continuity_context"]["following_clauses"][0]["id"],
                "C2",
            )

    def test_chunk_packets_expose_bounded_continuity_without_expanding_scope(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td) / "requirements"
            clauses = [
                {"id": f"C{i}", "text": f"段落{i}", "evidence_ids": [f"E{i}"],
                 "source_kind": "paragraph", "location": {"order": i}, "part_index": 0}
                for i in range(1, 5)
            ]
            evidence = {"evidence": [
                {"id": f"E{i}", "text": f"段落{i}", "kind": "paragraph"}
                for i in range(1, 5)
            ]}
            request = engine.build_llm_request([], clauses, evidence, {}, "full")
            request = attach_request_provenance(
                request, source_sha256="a" * 64, evidence_doc=evidence,
                clauses=clauses, run_id="run-continuity-test",
            )
            engine.prepare_host_agent_review_packets(
                request, clauses, evidence, "a" * 64, directory, chunk_size=2,
            )
            chunks = json.loads((directory / "llm-request-chunks.json").read_text())
            self.assertEqual(
                [item["id"] for item in chunks[0]["source_continuity_context"]["following_clauses"]],
                ["C3", "C4"],
            )
            self.assertEqual(
                [item["id"] for item in chunks[1]["source_continuity_context"]["preceding_clauses"]],
                ["C1", "C2"],
            )
            self.assertEqual(chunks[0]["batch"]["clause_ids"], ["C1", "C2"])
            self.assertEqual(chunks[1]["batch"]["clause_ids"], ["C3", "C4"])

    def test_declaration_anchor_preference_is_bound_to_current_structure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td) / "requirements"
            clauses = [{
                "id": "C1", "text": "固定声明正文", "evidence_ids": ["E1"],
                "source_kind": "paragraph", "location": {}, "part_index": 0,
            }]
            evidence = {
                "evidence": [{"id": "E1", "text": "固定声明正文", "kind": "paragraph"}],
                "structure_evidence": {"sections": [{
                    "first_paragraphs": [{"text": "摘要", "style_name": "Abstract Title CN"}],
                    "last_paragraphs": [],
                }]},
            }
            request = engine.build_llm_request([], clauses, evidence, {}, "full")
            self.assertEqual(request["declaration_anchor_candidates"], [
                "document_start", "abstract_title_zh",
            ])
            self.assertEqual(request["declaration_anchor_preference"], "abstract_title_zh")
            request = attach_request_provenance(
                request, source_sha256="a" * 64, evidence_doc=evidence,
                clauses=clauses, run_id="run-anchor-test",
            )
            engine.prepare_host_agent_review_packets(
                request, clauses, evidence, "a" * 64, directory, chunk_size=1,
            )
            chunk = json.loads((directory / "llm-request-chunks.json").read_text())[0]
            self.assertEqual(chunk["declaration_anchor_preference"], "abstract_title_zh")

    def test_preflight_rejects_invalid_declaration_anchor_and_signature_only_block(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _review_dir, chunk = self._packet(Path(td) / "requirements")
            chunk["clauses"][0]["text"] = "作者姓名"
            chunk["evidence_context"]["E1"]["text"] = "作者姓名"
            response = self._response(chunk)
            response["requirements"] = [{
                "role": "declarations",
                "properties": {
                    "before_role": "declarations",
                    "items": [{
                        "id": "author_signature",
                        "heading": "作者姓名",
                        "body_parts": ["年 月 日于北体大"],
                        "source_evidence_ids": ["E1"],
                        "signature_placeholders": [],
                    }],
                },
                "clause_ids": ["C1"], "evidence_ids": ["E1"],
                "confidence": 0.9, "reason": "固定文本",
            }]
            response["clause_reviews"] = [{
                "clause_id": "C1", "classification": "executable",
                "requirement_indexes": [0], "reason": "声明结构",
            }]
            errors = bridge.validate_host_agent_response(response, chunk)
            self.assertTrue(any("before_role" in error for error in errors), errors)
            self.assertTrue(any("generic author/date/signature" in error for error in errors), errors)

    def test_preflight_rejects_response_schema_drift(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _review_dir, chunk = self._packet(Path(td) / "requirements")
            response = self._executable_response(chunk, invalid_verification=True)
            errors = bridge.validate_host_agent_response(response, chunk)
            self.assertTrue(any("verification" in error for error in errors), errors)

    def test_preflight_rejects_sample_bibliography_as_executable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _review_dir, chunk = self._packet(Path(td) / "requirements")
            chunk["clauses"][0].update({
                "text": "[1] 示例作者. 示例文献[J]. 示例期刊, 2020.",
                "source_text_full": "[1] 示例作者. 示例文献[J]. 示例期刊, 2020.",
                "context_before": ["参考文献"],
                "context_after": [],
            })
            response = self._executable_response(chunk)
            errors = bridge.validate_host_agent_response(response, chunk)
            self.assertTrue(
                any("sample_content_cannot_be_executable" in error for error in errors),
                errors,
            )

    def test_preflight_rejects_informational_as_normative_basis(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _review_dir, chunk = self._packet(Path(td) / "requirements")
            response = self._response(chunk)
            response["clause_reviews"][0]["normative_basis"] = "informational"
            errors = bridge.validate_host_agent_response(response, chunk)
            self.assertTrue(any("normative_basis" in error for error in errors), errors)

    def test_preflight_rejects_requirement_index_on_nonexecutable_review(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _review_dir, chunk = self._packet(Path(td) / "requirements")
            response = self._executable_response(chunk)
            response["clause_reviews"][0]["classification"] = "informational"
            response["clause_reviews"][0]["requirement_indexes"] = [0]
            errors = bridge.validate_host_agent_response(response, chunk)
            self.assertTrue(
                any("nonexecutable_review_must_not_reference_requirement" in error for error in errors),
                errors,
            )

    def test_preflight_reports_exact_clause_and_matching_requirement_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _review_dir, chunk = self._packet(Path(td) / "requirements")
            chunk["clauses"].append({
                "id": "C2", "text": "标题居中", "evidence_ids": ["E2"],
                "source_kind": "paragraph", "location": {}, "part_index": 0,
            })
            chunk["evidence_context"]["E2"] = {
                "id": "E2", "text": "标题居中", "kind": "paragraph",
            }
            response = self._executable_response(chunk)
            response["requirements"].append({
                "role": "body_text",
                "properties": {"font": {"cjk": "SimSun", "size_pt": 12}},
                "clause_ids": ["C2"],
                "evidence_ids": ["E2"],
                "confidence": 0.9,
                "reason": "The clause specifies the heading alignment.",
                "verification": {
                    "mode": "word_render",
                    "checks": ["Check the heading alignment."],
                },
            })
            response["clause_reviews"][0]["requirement_indexes"] = [0, 1]
            response["clause_reviews"].append({
                "clause_id": "C2",
                "classification": "executable",
                "requirement_indexes": [1],
                "reason": "The clause is executable in DOCX.",
            })

            errors = bridge.validate_host_agent_response(response, chunk)
            backed_errors = [
                error for error in errors
                if "requirement_index_not_backed_by_clause" in error
            ]
            self.assertEqual(len(backed_errors), 1, errors)
            self.assertIn("clause_id=C1", backed_errors[0])
            self.assertIn("requirement_index=1", backed_errors[0])
            self.assertIn("matching_indexes=[0]", backed_errors[0])

            response["clause_reviews"][0]["requirement_indexes"] = [0]
            self.assertFalse(
                any(
                    "requirement_index_not_backed_by_clause" in error
                    for error in bridge.validate_host_agent_response(response, chunk)
                )
            )

    def test_host_prompt_exposes_mechanical_contract_rules(self) -> None:
        prompt = bridge._host_prompt(
            request_path=Path("request.json"),
            chunk_path=Path("chunk.json"),
            response_path=Path("response.json"),
            run_id="run-1",
            chunk_index=1,
            chunk_count=1,
        )
        self.assertIn("classification and normative_basis are different fields", prompt)
        self.assertIn("Only covered, executable, and verify_existing", prompt)
        self.assertIn("require_after_role", prompt)
        self.assertIn("A single clause may support multiple requirements", prompt)
        self.assertIn("Role boundary for equations", prompt)
        self.assertIn("partial_clause_coverage error never authorizes changing classification", prompt)
        retry = bridge._host_prompt(
            request_path=Path("request.json"),
            chunk_path=Path("chunk.json"),
            response_path=Path("response.json"),
            run_id="run-1",
            chunk_index=1,
            chunk_count=1,
            retry_hint="informational normative_basis nonexecutable_review_must_not_reference_requirement",
        )
        self.assertIn("targeted contract repair rules", retry)
        self.assertIn("Remove normative_basis", retry)
        self.assertIn("never replace it with another guessed value", retry)

        retry_with_baseline = bridge._host_prompt(
            request_path=Path("request.json"),
            chunk_path=Path("chunk.json"),
            response_path=Path("response.json"),
            run_id="run-1",
            chunk_index=1,
            chunk_count=1,
            attempt=2,
            retry_hint="cover_binding_violation",
            retry_parent_response_sha256="a" * 64,
            retry_parent_response_path=Path("/tmp/parent-response.raw.json"),
            retry_error_records=[{
                "code": "cover_binding_violation",
                "json_pointer": "$.requirements[0].properties.fields[2]",
            }],
        )
        self.assertIn("/tmp/parent-response.raw.json", retry_with_baseline)
        self.assertIn("Preserve every non-error semantic field", retry_with_baseline)
        self.assertIn("Do not split, merge, add", retry_with_baseline)
        self.assertIn("FINAL RETRY INVARIANT", retry_with_baseline)
        self.assertIn("do not turn an unresolved or informational review into executable", retry_with_baseline)

    def test_retry_guidance_targets_exact_invalid_property_without_contradiction(self) -> None:
        retry = bridge._contract_repair_guidance(
            "local response contract validation failed: "
            "$.clause_reviews[12].normative_basis: 'informational' is not in "
            "['explicit_normative_text']; "
            "$.requirements[3].properties: unknown property 'style_hint'",
            include_base=False,
        )
        self.assertIn("clause_reviews[12]", retry)
        self.assertIn("remove the entire normative_basis property", retry)
        self.assertIn("Delete only the unknown property 'style_hint'", retry)
        self.assertNotIn("Do not repair by deleting evidence, clearing indexes", retry)

    def test_retry_guidance_targets_exact_requirement_clause_mapping(self) -> None:
        retry = bridge._contract_repair_guidance(
            "local response contract validation failed: "
            "$.clause_reviews[2]: requirement_index_not_backed_by_clause:"
            "clause_id=C00243:requirement_index=2:matching_indexes=[1]",
            include_base=False,
        )
        self.assertIn("clause_reviews[2]", retry)
        self.assertIn("C00243", retry)
        self.assertIn("requirements[2].clause_ids", retry)
        self.assertIn("matching requirement indexes are [1]", retry)
        self.assertIn("Do not copy a neighboring clause's index", retry)

    def test_retry_guidance_requires_payload_instead_of_field_key_only(self) -> None:
        retry = bridge._contract_repair_guidance(
            "local response contract validation failed: "
            "$.requirements[0].properties: must_include_semantic_payload",
            include_base=False,
        )
        self.assertIn("non-empty role-specific properties object", retry)
        self.assertIn("field_key alone", retry)
        self.assertIn("properties.text", retry)

    def test_retry_guidance_binds_prerequisites_and_admin_condition(self) -> None:
        retry = bridge._contract_repair_guidance(
            "local response contract validation failed: "
            "$.requirements[0].input_prerequisites[0].key: does not match "
            "'^(thesis_profile|source_inventory|template_profile|runtime)\\.'; "
            "$.requirements[1].properties.non_public_administration.applicability: "
            "must be conditional on thesis_profile.security_level",
            include_base=False,
        )
        self.assertIn("runtime_context.*", retry)
        self.assertIn("operator equals or in", retry)

    def test_invalid_retry_cannot_change_classification_to_escape_a_contract_error(self) -> None:
        previous = self._executable_response({"provenance": {}})
        current = self._executable_response({"provenance": {}})
        current["clause_reviews"][0]["classification"] = "informational"
        current["clause_reviews"][0].pop("requirement_indexes", None)
        change_error, changed = bridge._retry_semantic_change_error(
            previous,
            current,
            [{"code": "input_prerequisite_namespace"}],
            contract_version="2.1",
        )
        self.assertTrue(changed)
        self.assertIsNotNone(change_error)
        self.assertIn("semantic re-review", str(change_error))

    def test_retry_allows_only_completion_of_an_empty_requirement_payload(self) -> None:
        previous = self._executable_response({"provenance": {}})
        previous["requirements"][0]["properties"] = {}
        current = self._executable_response({"provenance": {}})
        records = [{"code": "empty_requirement_properties"}]
        self.assertTrue(
            bridge._retry_changes_allowed(
                records,
                ["$.requirements[0].properties.font"],
                contract_version="2.1",
                previous_response=previous,
                current_response=current,
            )
        )
        current["requirements"][0]["properties"]["font"]["size_pt"] = 11
        self.assertFalse(
            bridge._retry_changes_allowed(
                records,
                ["$.requirements[0].properties.font.size_pt"],
                contract_version="2.1",
                previous_response=current,
                current_response=current,
            )
        )

    def test_empty_payload_fill_may_rewrite_only_the_requirement_reason(self) -> None:
        previous = self._executable_response({"provenance": {}})
        previous["requirements"][0]["properties"] = {}
        previous["requirements"][0]["reason"] = "首轮解释"
        current = self._executable_response({"provenance": {}})
        current["requirements"][0]["reason"] = "重试后的解释"
        records = [{
            "code": "empty_requirement_properties",
            "json_pointer": "$.requirements[0].properties",
        }]
        changed = bridge._retry_change_paths(previous, current)
        self.assertEqual(
            changed,
            ["$.requirements[0].properties.font", "$.requirements[0].reason"],
        )
        self.assertTrue(bridge._retry_changes_allowed(
            records,
            changed,
            contract_version="2.1",
            previous_response=previous,
            current_response=current,
        ))
        current["requirements"][0]["clause_ids"] = ["C999"]
        changed = bridge._retry_change_paths(previous, current)
        self.assertFalse(bridge._retry_changes_allowed(
            records,
            changed,
            contract_version="2.1",
            previous_response=previous,
            current_response=current,
        ))

    def test_retry_allows_empty_payload_fill_and_validator_named_property_removal(self) -> None:
        previous = {
            "contract_version": "3.0",
            "requirements": [
                {
                    "role": "equations", "properties": {},
                    "clause_ids": ["C1"], "evidence_ids": ["E1"],
                },
                {
                    "role": "equation", "properties": {"style": "three_line"},
                    "clause_ids": ["C2"], "evidence_ids": ["E2"],
                },
            ],
            "clause_reviews": [
                {"clause_id": "C1", "classification": "executable"},
                {"clause_id": "C2", "classification": "executable"},
            ],
            "unsupported_items": [],
        }
        current = json.loads(json.dumps(previous))
        current["requirements"][0]["properties"] = {"same_line": False}
        current["requirements"][1]["properties"] = {}
        records = [
            {"code": "empty_requirement_properties", "json_pointer": "$.requirements[0].properties"},
            {
                "code": "unknown_property",
                "json_pointer": "$.requirements[1].properties",
                "raw_error": "unknown property 'style'",
            },
        ]
        changed = bridge._retry_change_paths(previous, current)
        self.assertEqual(
            changed,
            ["$.requirements[0].properties.same_line", "$.requirements[1].properties.style"],
        )
        self.assertTrue(bridge._retry_changes_allowed(
            records, changed, contract_version="3.0",
            previous_response=previous, current_response=current,
        ))

    def test_retry_allows_only_exact_fixed_text_repair(self) -> None:
        records = [{"code": "fixed_text_evidence_mismatch"}]
        self.assertTrue(
            bridge._retry_changes_allowed(
                records,
                ["$.requirements[0].properties.items[0].body_parts[0]"],
                contract_version="2.1",
            )
        )
        self.assertFalse(
            bridge._retry_changes_allowed(
                records,
                ["$.clause_reviews[0].classification"],
                contract_version="2.1",
            )
        )

    def test_retry_allows_only_targeted_cover_binding_repair(self) -> None:
        previous = {
            "contract_version": "3.0",
            "requirements": [{
                "role": "cover",
                "properties": {
                    "institution": "——",
                    "fields": [{"id": "security_marking", "label": "密级"}],
                },
                "clause_ids": ["C1"],
                "evidence_ids": ["E1"],
            }],
            "clause_reviews": [{
                "clause_id": "C1", "classification": "executable",
                "normative_basis": "template_structure",
            }],
            "unsupported_items": [],
        }
        current = json.loads(json.dumps(previous))
        current["requirements"][0]["properties"]["fields"] = []
        current["requirements"][0]["properties"]["non_public_administration"] = {
            "fields": [{"id": "security_marking", "label": "密级"}],
        }
        records = [{
            "code": "cover_binding_violation",
            "json_pointer": "$.requirements[0].properties.fields[0]",
        }]
        changed = bridge._retry_change_paths(previous, current)
        self.assertTrue(changed)
        self.assertTrue(
            bridge._retry_changes_allowed(
                records, changed, contract_version="3.0",
                previous_response=previous, current_response=current,
            )
        )

        current["clause_reviews"][0]["classification"] = "not_applicable"
        changed = bridge._retry_change_paths(previous, current)
        self.assertFalse(
            bridge._retry_changes_allowed(
                records, changed, contract_version="3.0",
                previous_response=previous, current_response=current,
            )
        )

    def test_v3_retry_allows_only_new_requirement_for_missing_executable_clause(self) -> None:
        previous = {
            "contract_version": "3.0",
            "requirements": [{
                "role": "cover_field_label", "properties": {"text": "标题"},
                "clause_ids": ["C1"], "evidence_ids": ["E1"],
            }],
            "clause_reviews": [
                {"clause_id": "C1", "classification": "executable"},
                {"clause_id": "C2", "classification": "executable"},
            ],
            "unsupported_items": [],
        }
        current = json.loads(json.dumps(previous))
        current["requirements"].append({
            "role": "declarations", "properties": {
                "before_role": "abstract_title_zh", "items": [{
                    "id": "authorization", "heading": "授权书",
                    "body_parts": ["固定正文"], "source_evidence_ids": ["E2"],
                    "signature_placeholders": [],
                }],
            }, "clause_ids": ["C2"], "evidence_ids": ["E2"],
        })
        records = [{
            "code": "requirement_relation_mismatch",
            "json_pointer": "$.clause_reviews[1]",
        }]
        changed = bridge._retry_change_paths(previous, current)
        self.assertEqual(changed, ["$.requirements"])
        self.assertTrue(bridge._retry_changes_allowed(
            records, changed, contract_version="3.0",
            previous_response=previous, current_response=current,
        ))

        current["requirements"][0]["properties"]["text"] = "改过的旧要求"
        changed = bridge._retry_change_paths(previous, current)
        self.assertFalse(bridge._retry_changes_allowed(
            records, changed, contract_version="3.0",
            previous_response=previous, current_response=current,
        ))

    def test_v3_retry_allows_only_first_occurrence_evidence_deduplication(self) -> None:
        previous = {
            "contract_version": "3.0",
            "requirements": [{
                "role": "declarations", "properties": {"text": "授权书"},
                "clause_ids": ["C1"], "evidence_ids": ["E1", "E2", "E2"],
            }],
            "clause_reviews": [{"clause_id": "C1", "classification": "executable"}],
            "unsupported_items": [], "reported_conflicts": [],
        }
        current = json.loads(json.dumps(previous))
        current["requirements"][0]["evidence_ids"] = ["E1", "E2"]
        records = [{
            "code": "duplicate_evidence_ids",
            "json_pointer": "$.requirements[0].evidence_ids",
        }]
        changed = bridge._retry_change_paths(previous, current)
        self.assertEqual(changed, ["$.requirements"])
        self.assertTrue(bridge._retry_changes_allowed(
            records, changed, contract_version="3.0",
            previous_response=previous, current_response=current,
        ))

        current["requirements"][0]["evidence_ids"] = ["E2", "E1"]
        changed = bridge._retry_change_paths(previous, current)
        self.assertFalse(bridge._retry_changes_allowed(
            records, changed, contract_version="3.0",
            previous_response=previous, current_response=current,
        ))
        current = json.loads(json.dumps(previous))
        current["requirements"][0]["properties"]["text"] = "改动后的授权书"
        current["requirements"][0]["evidence_ids"] = ["E1", "E2"]
        changed = bridge._retry_change_paths(previous, current)
        self.assertFalse(bridge._retry_changes_allowed(
            records, changed, contract_version="3.0",
            previous_response=previous, current_response=current,
        ))

    def test_v3_relation_completion_restores_requirements_after_reason_rewrite(self) -> None:
        previous = {
            "contract_version": "3.0",
            "requirements": [
                {
                    "role": "heading_2",
                    "properties": {"text": "5.1 研究结论"},
                    "clause_ids": ["C1"],
                    "evidence_ids": ["E1"],
                    "confidence": 0.98,
                    "reason": "原始解释",
                },
            ],
            "clause_reviews": [
                {"clause_id": "C1", "classification": "executable"},
                {"clause_id": "C2", "classification": "executable"},
            ],
            "unsupported_items": [],
            "reported_conflicts": [],
        }
        current = json.loads(json.dumps(previous))
        current["requirements"][0]["reason"] = "重试后的同义解释"
        current["requirements"].append({
            "role": "heading_3",
            "properties": {"text": "5.1.1 研究结论"},
            "clause_ids": ["C2"],
            "evidence_ids": ["E2"],
            "confidence": 0.97,
            "reason": "新增 requirement 的解释",
        })
        records = [{
            "code": "missing_derived_requirement",
            "json_pointer": "$.clause_reviews[1]",
            "raw_error": "$.clause_reviews[1]: executable_review_requires_derived_requirement",
        }]
        with patch.object(bridge, "validate_host_agent_response", return_value=[]):
            repaired, audit = bridge._v3_relation_completion_response(
                previous, current, records, chunk={},
            )
        self.assertIsNotNone(repaired)
        self.assertEqual(repaired["requirements"][0]["reason"], "原始解释")
        self.assertEqual(
            [item["clause_ids"] for item in repaired["requirements"]],
            [["C1"], ["C2"]],
        )
        self.assertEqual(audit["preserved_requirement_count"], 1)
        self.assertEqual(audit["added_requirement_count"], 1)

        current["requirements"][0]["properties"]["text"] = "改过的旧 requirement"
        with patch.object(bridge, "validate_host_agent_response", return_value=[]):
            repaired, audit = bridge._v3_relation_completion_response(
                previous, current, records, chunk={},
            )
        self.assertIsNone(repaired)
        self.assertIsNone(audit)

    def test_v3_relation_completion_handles_multiple_missing_clauses_and_empty_placeholder(self) -> None:
        previous = {
            "contract_version": "3.0",
            "requirements": [
                {
                    "role": "body_text", "properties": {"font": {"cjk": "SimSun"}},
                    "clause_ids": ["C1"], "evidence_ids": ["E1"],
                    "confidence": 0.9, "reason": "保留的已有要求",
                },
                {
                    "role": "abstract_title_zh", "properties": {},
                    "clause_ids": [], "evidence_ids": [], "confidence": 0,
                    "reason": "",
                },
            ],
            "clause_reviews": [
                {"clause_id": "C1", "classification": "executable"},
                {"clause_id": "C2", "classification": "executable"},
                {"clause_id": "C3", "classification": "executable"},
            ],
            "unsupported_items": [],
            "reported_conflicts": [],
        }
        current = {
            "contract_version": "3.0",
            "requirements": [
                json.loads(json.dumps(previous["requirements"][0])),
                json.loads(json.dumps(previous["requirements"][1])),
                {
                    "role": "content_constraints", "properties": {"keywords_zh": {"max_chars": 10}},
                    "clause_ids": ["C2"], "evidence_ids": ["E2"],
                    "confidence": 0.9, "reason": "C2 的证据支持关键词限制",
                },
                {
                    "role": "content_constraints", "properties": {"keywords_en": {"max_chars": 10}},
                    "clause_ids": ["C3"], "evidence_ids": ["E3"],
                    "confidence": 0.9, "reason": "C3 的证据支持英文关键词限制",
                },
            ],
            "clause_reviews": previous["clause_reviews"],
            "unsupported_items": [],
            "reported_conflicts": [],
        }
        records = [
            {
                "code": "empty_requirement_properties",
                "json_pointer": "$.requirements[1].properties",
                "raw_error": "$.requirements[1].properties: must_include_semantic_payload",
            },
            {
                "code": "missing_derived_requirement",
                "json_pointer": "$.clause_reviews[1]",
                "raw_error": "$.clause_reviews[1]: executable_review_requires_derived_requirement",
            },
            {
                "code": "missing_derived_requirement",
                "json_pointer": "$.clause_reviews[2]",
                "raw_error": "$.clause_reviews[2]: executable_review_requires_derived_requirement",
            },
            {
                "code": "requirement_relation_mismatch",
                "json_pointer": "$.requirements[1]",
                "raw_error": "requirements_not_referenced_by_clause_review:1",
            },
        ]
        with patch.object(bridge, "validate_host_agent_response", return_value=[]):
            repaired, audit = bridge._v3_relation_completion_response(
                previous, current, records, chunk={},
            )
        self.assertIsNotNone(repaired)
        self.assertEqual(
            [item["clause_ids"] for item in repaired["requirements"]],
            [["C1"], ["C2"], ["C3"]],
        )
        self.assertEqual(audit["missing_clause_ids"], ["C2", "C3"])
        self.assertEqual(audit["removed_unbound_placeholder_count"], 1)
        self.assertEqual(audit["added_requirement_count"], 2)

        current["requirements"][2]["clause_ids"] = ["C9"]
        with patch.object(bridge, "validate_host_agent_response", return_value=[]):
            repaired, audit = bridge._v3_relation_completion_response(
                previous, current, records, chunk={},
            )
        self.assertIsNone(repaired)
        self.assertIsNone(audit)

    def test_v3_retry_allows_only_proven_informational_projection(self) -> None:
        previous = {
            "contract_version": "3.0",
            "requirements": [
                {"role": "body_text", "properties": {"text": "保留"}, "clause_ids": ["C1"]},
                {"role": "body_text", "properties": {"text": "删除一"}, "clause_ids": ["C2"]},
                {"role": "body_text", "properties": {"text": "删除二"}, "clause_ids": ["C3"]},
            ],
            "clause_reviews": [
                {"clause_id": "C1", "classification": "executable"},
                {"clause_id": "C2", "classification": "informational"},
                {"clause_id": "C3", "classification": "informational"},
            ],
            "unsupported_items": [],
            "reported_conflicts": [],
        }
        current = json.loads(json.dumps(previous))
        current["requirements"] = [json.loads(json.dumps(previous["requirements"][0]))]
        records = [
            {"code": "informational_requirement_forbidden", "requirement_index": 1},
            {"code": "informational_requirement_forbidden", "requirement_index": 2},
        ]
        changed = bridge._retry_change_paths(previous, current)
        self.assertEqual(changed, ["$.requirements"])
        self.assertTrue(bridge._retry_changes_allowed(
            records, changed, contract_version="3.0",
            previous_response=previous, current_response=current,
            chunk={"clauses": [{"id": f"C{i}"} for i in range(1, 4)]},
        ))
        current["requirements"][0]["properties"]["text"] = "改动了保留项"
        changed = bridge._retry_change_paths(previous, current)
        self.assertFalse(bridge._retry_changes_allowed(
            records, changed, contract_version="3.0",
            previous_response=previous, current_response=current,
            chunk={"clauses": [{"id": f"C{i}"} for i in range(1, 4)]},
        ))

    def test_v3_retry_allows_only_proven_non_requirement_projection(self) -> None:
        previous = {
            "contract_version": "3.0",
            "requirements": [
                {
                    "role": "body_text", "properties": {"text": "保留"},
                    "clause_ids": ["C1"], "evidence_ids": ["E1"],
                },
                {
                    "role": "content_constraints", "properties": {"text": "未解决"},
                    "clause_ids": ["C2"], "evidence_ids": ["E2"],
                },
            ],
            "clause_reviews": [
                {"clause_id": "C1", "classification": "executable"},
                {"clause_id": "C2", "classification": "unresolved"},
            ],
            "unsupported_items": [],
            "reported_conflicts": [],
        }
        current = json.loads(json.dumps(previous, ensure_ascii=False))
        current["requirements"].pop(1)
        records = [{
            "code": "non_requirement_classification_relation",
            "json_pointer": "$.requirements[1]",
            "requirement_index": 1,
            "relation_category": "non_requirement_classification",
            "clause_ids": ["C2"],
        }]
        chunk = {"clauses": [{"id": "C1"}, {"id": "C2"}]}
        changed = bridge._retry_change_paths(previous, current)
        self.assertEqual(changed, ["$.requirements"])
        self.assertTrue(bridge._v3_non_requirement_projection_allowed(
            previous, current, records, changed, chunk=chunk,
        ))
        self.assertTrue(bridge._retry_changes_allowed(
            records, changed, contract_version="3.0",
            previous_response=previous, current_response=current, chunk=chunk,
        ))

        unsafe = json.loads(json.dumps(previous, ensure_ascii=False))
        unsafe["requirements"].pop(0)
        self.assertFalse(bridge._retry_changes_allowed(
            records, bridge._retry_change_paths(previous, unsafe),
            contract_version="3.0", previous_response=previous,
            current_response=unsafe, chunk=chunk,
        ))

    def test_v3_retry_allows_non_requirement_projection_with_named_payload_cleanup(self) -> None:
        previous = {
            "contract_version": "3.0",
            "requirements": [
                {
                    "role": "body_text", "properties": {"style": "bad"},
                    "clause_ids": ["C1"], "evidence_ids": ["E1"],
                },
                {
                    "role": "body_text", "properties": {"text": "未解决"},
                    "clause_ids": ["C2"], "evidence_ids": ["E2"],
                },
            ],
            "clause_reviews": [
                {"clause_id": "C1", "classification": "executable"},
                {"clause_id": "C2", "classification": "unsupported_backend"},
            ],
            "unsupported_items": [],
        }
        current = json.loads(json.dumps(previous, ensure_ascii=False))
        current["requirements"] = [{
            "role": "body_text", "properties": {},
            "clause_ids": ["C1"], "evidence_ids": ["E1"],
        }]
        records = [
            {
                "code": "unknown_property",
                "json_pointer": "$.requirements[0].properties",
                "raw_error": "unknown property 'style'",
            },
            {
                "code": "non_requirement_classification_relation",
                "json_pointer": "$.requirements[1]",
                "requirement_index": 1,
                "relation_category": "non_requirement_classification",
                "clause_ids": ["C2"],
            },
        ]
        chunk = {
            "clauses": [
                {"id": "C1", "evidence_ids": ["E1"]},
                {"id": "C2", "evidence_ids": ["E2"]},
            ],
            "evidence_context": {
                "E1": {"text": "固定正文"},
                "E2": {"text": "未解决"},
            },
            "requirement_contract": {
                "role_properties_schema": {"body_text": {"$ref": "#/$defs/roleSpec"}},
            },
        }
        changed = bridge._retry_change_paths(previous, current)
        self.assertEqual(changed, ["$.requirements"])
        self.assertTrue(bridge._retry_changes_allowed(
            records, changed, contract_version="3.0",
            previous_response=previous, current_response=current, chunk=chunk,
        ))

        current["requirements"][0]["properties"] = {"text": "猜测正文"}
        self.assertFalse(bridge._retry_changes_allowed(
            records, bridge._retry_change_paths(previous, current),
            contract_version="3.0", previous_response=previous,
            current_response=current, chunk=chunk,
        ))

    def test_v3_retry_allows_only_exact_fixed_declaration_completion(self) -> None:
        previous = {
            "contract_version": "3.0",
            "requirements": [
                {
                    "role": "body_text",
                    "properties": {"text": "保留 requirement"},
                    "clause_ids": ["C1"],
                    "evidence_ids": ["E1"],
                },
                {
                    "role": "table_text",
                    "properties": {},
                    "clause_ids": [],
                    "evidence_ids": [],
                    "confidence": 0,
                    "reason": "",
                },
            ],
            "clause_reviews": [
                {"clause_id": "C1", "classification": "executable", "reason": "保留"},
                {"clause_id": "C2", "classification": "executable", "reason": "原始标题"},
                {"clause_id": "C3", "classification": "executable", "reason": "原始正文"},
            ],
            "unsupported_items": [],
            "reported_conflicts": [],
        }
        current = json.loads(json.dumps(previous, ensure_ascii=False))
        current["requirements"] = [
            json.loads(json.dumps(previous["requirements"][0], ensure_ascii=False)),
            {
                "role": "declarations",
                "properties": {
                    "before_role": "abstract_title_zh",
                    "items": [{
                        "heading": "学位论文使用授权书",
                        "body_parts": ["本人同意提交电子版"],
                        "source_evidence_ids": ["E2", "E3"],
                    }],
                },
                "clause_ids": ["C2", "C3"],
                "evidence_ids": ["E2", "E3"],
                "confidence": 0.99,
                "reason": "由当前证据固定生成",
            },
        ]
        current["clause_reviews"][2]["reason"] = "当前证据明确该固定声明正文"
        records = [
            {
                "code": "empty_requirement_properties",
                "json_pointer": "$.requirements[1].properties",
                "raw_error": "must_include_semantic_payload",
            },
            {
                "code": "requirement_relation_mismatch",
                "json_pointer": "$.requirements[1]",
                "raw_error": "requirements_not_referenced_by_clause_review:1",
            },
            {
                "code": "missing_derived_requirement",
                "json_pointer": "$.clause_reviews[1]",
                "raw_error": "executable_review_requires_derived_requirement",
            },
            {
                "code": "missing_derived_requirement",
                "json_pointer": "$.clause_reviews[2]",
                "raw_error": "executable_review_requires_derived_requirement",
            },
        ]
        chunk = {
            "declaration_anchor_preference": "abstract_title_zh",
            "clauses": [
                {"id": "C1", "text": "保留 requirement", "evidence_ids": ["E1"]},
                {"id": "C2", "text": "学位论文使用授权书", "evidence_ids": ["E2"]},
                {"id": "C3", "text": "本人同意提交电子版", "evidence_ids": ["E3"]},
                {"id": "C4", "text": "摘要", "evidence_ids": ["E4"]},
            ],
            "evidence_context": {
                "E1": {"text": "保留 requirement"},
                "E2": {"text": "学位论文使用授权书"},
                "E3": {"text": "本人同意提交电子版"},
                "E4": {"text": "摘要"},
            },
        }
        changed = bridge._retry_change_paths(previous, current)
        self.assertEqual(
            changed,
            ["$.clause_reviews[2].reason", "$.requirements"],
        )
        with patch.object(bridge, "validate_host_agent_response", return_value=[]):
            self.assertTrue(bridge._retry_changes_allowed(
                records, changed, contract_version="3.0",
                previous_response=previous, current_response=current, chunk=chunk,
            ))

        current["requirements"][1]["properties"]["items"][0]["body_parts"] = ["猜测正文"]
        changed = bridge._retry_change_paths(previous, current)
        with patch.object(bridge, "validate_host_agent_response", return_value=[]):
            self.assertFalse(bridge._retry_changes_allowed(
                records, changed, contract_version="3.0",
                previous_response=previous, current_response=current, chunk=chunk,
            ))

    def test_v3_retry_restores_omitted_baseline_requirements_before_accepting_additions(self) -> None:
        previous = {
            "contract_version": "3.0",
            "requirements": [
                {
                    "role": "cover_field_label", "properties": {"text": "标题"},
                    "clause_ids": ["C1"], "evidence_ids": ["E1"],
                },
                {
                    "role": "body_text", "properties": {"font": {"latin": "Times New Roman"}},
                    "clause_ids": ["C3"], "evidence_ids": ["E3"],
                },
            ],
            "clause_reviews": [
                {"clause_id": "C1", "classification": "executable"},
                {"clause_id": "C2", "classification": "executable"},
                {"clause_id": "C3", "classification": "informational"},
            ],
            "unsupported_items": [],
            "reported_conflicts": [],
        }
        current = {
            "contract_version": "3.0",
            "requirements": [
                json.loads(json.dumps(previous["requirements"][0])),
                {
                    "role": "heading_1", "properties": {"text": "第一章"},
                    "clause_ids": ["C2"], "evidence_ids": ["E2"],
                },
            ],
            "clause_reviews": previous["clause_reviews"],
            "unsupported_items": [],
            "reported_conflicts": [],
        }
        records = [{
            "code": "requirement_relation_mismatch",
            "json_pointer": "$.clause_reviews[1]",
            "raw_error": "$.clause_reviews[1]: executable_review_requires_derived_requirement",
        }]
        chunk = {}
        with patch.object(bridge, "validate_host_agent_response", return_value=[]):
            repaired, audit = bridge._v3_relation_completion_response(
                previous, current, records, chunk=chunk,
            )
        self.assertIsNotNone(repaired)
        self.assertEqual(
            [item["clause_ids"] for item in repaired["requirements"]],
            [["C1"], ["C3"], ["C2"]],
        )
        self.assertEqual(audit["preserved_requirement_count"], 2)
        self.assertEqual(audit["added_requirement_count"], 1)

        current["requirements"][0]["properties"]["text"] = "改过的旧要求"
        with patch.object(bridge, "validate_host_agent_response", return_value=[]):
            repaired, audit = bridge._v3_relation_completion_response(
                previous, current, records, chunk=chunk,
            )
        self.assertIsNone(repaired)
        self.assertIsNone(audit)

    def test_v3_retry_allows_bounded_completion_of_truncated_response(self) -> None:
        previous = {
            "contract_version": "3.0",
            "requirements": [{
                "role": "cover_field_label", "properties": {},
                "clause_ids": ["C1"], "evidence_ids": ["E1"],
            }],
            "clause_reviews": [], "unsupported_items": [],
        }
        current = {
            "contract_version": "3.0",
            "requirements": [{
                "role": "cover_field_label", "properties": {"text": "标题"},
                "clause_ids": ["C1"], "evidence_ids": ["E1"],
            }, {
                "role": "body_text", "properties": {"text": "正文"},
                "clause_ids": ["C2"], "evidence_ids": ["E2"],
            }],
            "clause_reviews": [
                {"clause_id": "C1", "classification": "executable"},
                {"clause_id": "C2", "classification": "unsupported_backend"},
            ],
            "unsupported_items": ["C2：后端无法验证该要求"],
        }
        records = [
            {"code": "empty_requirement_properties"},
            {
                "code": "contract_validation_error",
                "raw_error": "clause_reviews_must_cover_each_chunk_clause_exactly_once",
            },
            {
                "code": "requirement_relation_mismatch",
                "raw_error": "requirements_not_referenced_by_clause_review:0",
            },
        ]
        chunk = {
            "evidence_context": {
                "E1": {"text": "标题"}, "E2": {"text": "正文"},
            },
            "requirement_contract": {
                "role_properties_schema": {
                    "cover_field_label": {"$ref": "#/$defs/roleSpec"},
                    "body_text": {"$ref": "#/$defs/roleSpec"},
                },
            },
        }
        changed = bridge._retry_change_paths(previous, current)
        self.assertEqual(
            changed,
            ["$.clause_reviews", "$.requirements", "$.unsupported_items"],
        )
        with patch.object(bridge, "validate_host_agent_response", return_value=[]):
            self.assertTrue(bridge._retry_changes_allowed(
                records, changed, contract_version="3.0",
                previous_response=previous, current_response=current, chunk=chunk,
            ))

        current["unsupported_items"] = ["C9：不属于当前 chunk"]
        changed = bridge._retry_change_paths(previous, current)
        with patch.object(bridge, "validate_host_agent_response", return_value=[]):
            self.assertFalse(bridge._retry_changes_allowed(
                records, changed, contract_version="3.0",
                previous_response=previous, current_response=current, chunk=chunk,
            ))

    def test_v3_retry_allows_only_validator_named_missing_clause_review(self) -> None:
        previous = {
            "contract_version": "3.0",
            "requirements": [{
                "role": "table", "properties": {"style": "three_line"},
                "clause_ids": ["C1"], "evidence_ids": ["E1"],
            }, {
                "role": "table", "properties": {"border_widths_pt": {"top": 1.5}},
                "clause_ids": ["C2"], "evidence_ids": ["E2"],
            }],
            "clause_reviews": [{"clause_id": "C1", "classification": "executable"}],
            "unsupported_items": [], "reported_conflicts": [],
        }
        current = json.loads(json.dumps(previous))
        current["clause_reviews"].append({
            "clause_id": "C2", "classification": "informational",
        })
        records = [{
            "code": "missing_clause_review",
            "json_pointer": "$.requirements[0]",
            "raw_error": "$.requirements[0]:missing_clause_review:clause_ids=C2",
        }, {
            "code": "contract_validation_error",
            "raw_error": "clause_reviews_must_cover_each_chunk_clause_exactly_once",
        }]
        chunk = {"clauses": [{"id": "C1"}, {"id": "C2"}]}
        changed = bridge._retry_change_paths(previous, current)
        self.assertEqual(changed, ["$.clause_reviews"])
        with patch.object(bridge, "validate_host_agent_response", return_value=[]):
            self.assertTrue(bridge._retry_changes_allowed(
                records, changed, contract_version="3.0",
                previous_response=previous, current_response=current, chunk=chunk,
            ))

        current["clause_reviews"][0]["classification"] = "informational"
        changed = bridge._retry_change_paths(previous, current)
        with patch.object(bridge, "validate_host_agent_response", return_value=[]):
            self.assertFalse(bridge._retry_changes_allowed(
                records, changed, contract_version="3.0",
                previous_response=previous, current_response=current, chunk=chunk,
            ))

    def test_v3_retry_discards_only_unbound_truncated_placeholder(self) -> None:
        previous = {
            "contract_version": "3.0",
            "requirements": [{
                "role": "body_text", "properties": {"style": "three_line"},
                "clause_ids": [], "evidence_ids": [], "reason": "",
            }],
            "clause_reviews": [], "unsupported_items": [],
        }
        current = {
            "contract_version": "3.0",
            "requirements": [{
                "role": "cover_field_label", "properties": {"text": "标题"},
                "clause_ids": ["C1"], "evidence_ids": ["E1"],
            }],
            "clause_reviews": [{
                "clause_id": "C1", "classification": "executable",
            }],
            "unsupported_items": [],
        }
        records = [
            {"code": "contract_validation_error", "raw_error": "$.requirements[0].reason: is shorter than 1 characters"},
            {"code": "unknown_property", "raw_error": "$.requirements[0].properties: unknown property 'style'"},
            {"code": "schema_contract_violation", "raw_error": "$.requirements[0].clause_ids: must_be_non_empty"},
            {"code": "schema_contract_violation", "raw_error": "$.requirements[0].evidence_ids: must_be_non_empty"},
            {"code": "contract_validation_error", "raw_error": "clause_reviews_must_cover_each_chunk_clause_exactly_once"},
        ]
        chunk = {
            "evidence_context": {"E1": {"text": "标题"}},
            "requirement_contract": {
                "role_properties_schema": {
                    "cover_field_label": {"$ref": "#/$defs/roleSpec"},
                },
            },
        }
        changed = bridge._retry_change_paths(previous, current)
        with patch.object(bridge, "validate_host_agent_response", return_value=[]):
            self.assertTrue(bridge._retry_changes_allowed(
                records, changed, contract_version="3.0",
                previous_response=previous, current_response=current, chunk=chunk,
            ))
        previous["requirements"][0]["clause_ids"] = ["C0"]
        self.assertFalse(bridge._retry_changes_allowed(
            records, changed, contract_version="3.0",
            previous_response=previous, current_response=current, chunk=chunk,
        ))

    def test_v3_retry_allows_schema_directed_cover_completion_only(self) -> None:
        previous = {
            "contract_version": "3.0",
            "requirements": [{
                "role": "cover", "properties": {
                    "before_role": "document_start", "items": [],
                }, "clause_ids": ["C1"], "evidence_ids": ["E1"],
                "verification": {
                    "mode": "static_docx", "checks": ["保留检查", ""],
                },
            }],
            "clause_reviews": [{
                "clause_id": "C1", "classification": "executable",
            }],
            "unsupported_items": [],
        }
        current = json.loads(json.dumps(previous))
        current["requirements"][0]["properties"] = {
            "institution": "——",
            "fields": [{
                "id": "title_zh", "label": "论文题目：",
                "value_from": "thesis_profile.cover_metadata.title_zh",
                "display_policy": "required", "order": 1,
            }],
            "before_role": "document_start",
            "missing_value_policy": "placeholder",
            "missing_value_placeholder": "——",
            "layout_id": "linear",
        }
        records = [
            {"code": "contract_validation_error", "json_pointer": "$.requirements[0].properties"},
            {"code": "schema_contract_violation", "json_pointer": "$.requirements[0].properties"},
            {"code": "unknown_property", "json_pointer": "$.requirements[0].properties"},
            {"code": "cover_binding_violation", "json_pointer": "$.requirements[0].properties.fields"},
            {
                "code": "contract_validation_error",
                "json_pointer": "$.requirements[0].verification.checks",
                "raw_error": "$.requirements[0].verification.checks[1]: is shorter than 1 characters",
            },
        ]
        current["requirements"][0]["verification"]["checks"] = ["保留检查"]
        changed = bridge._retry_change_paths(previous, current)
        chunk = {"response_schema": {"type": "object"}}
        with patch.object(bridge, "validate_host_agent_response", return_value=[]):
            self.assertTrue(bridge._retry_changes_allowed(
                records, changed, contract_version="3.0",
                previous_response=previous, current_response=current, chunk=chunk,
            ))
        current["clause_reviews"][0]["classification"] = "not_applicable"
        changed = bridge._retry_change_paths(previous, current)
        self.assertFalse(bridge._retry_changes_allowed(
            records, changed, contract_version="3.0",
            previous_response=previous, current_response=current, chunk=chunk,
        ))

    def test_mechanical_repair_removes_informational_only_requirement(self) -> None:
        response = {
            "requirements": [{
                "role": "body_text", "properties": {"text": "签名"},
                "clause_ids": ["C1"], "evidence_ids": ["E1"],
            }],
            "clause_reviews": [{
                "clause_id": "C1", "classification": "informational",
            }],
        }
        repaired, repairs = bridge._apply_safe_mechanical_repairs(
            response,
            [{
                "code": "informational_requirement_forbidden",
                "requirement_index": 0,
                "raw_error": "requirements_not_referenced_by_clause_review:0",
            }],
        )
        self.assertEqual(repaired["requirements"], [])
        self.assertEqual(repairs[0]["removed_clause_ids"], ["C1"])
        self.assertEqual(repairs[0]["removed_requirement"]["clause_ids"], ["C1"])
        self.assertEqual(
            repairs[0]["removed_requirement_sha256"],
            bridge._response_sha256(response["requirements"][0]),
        )
        self.assertEqual(len(response["requirements"]), 1)

    def test_mechanical_repair_removes_batch_informational_requirements_by_mask(self) -> None:
        response = {
            "requirements": [
                {"role": "body_text", "properties": {"text": "一"}, "clause_ids": ["C1"]},
                {"role": "body_text", "properties": {"text": "二"}, "clause_ids": ["C2"]},
                {"role": "body_text", "properties": {"text": "三"}, "clause_ids": ["C3"]},
                {"role": "body_text", "properties": {"text": "四"}, "clause_ids": ["C4"]},
            ],
            "clause_reviews": [
                {"clause_id": "C1", "classification": "informational"},
                {"clause_id": "C2", "classification": "informational"},
                {"clause_id": "C3", "classification": "executable"},
                {"clause_id": "C4", "classification": "informational"},
            ],
        }
        repaired, repairs = bridge._apply_safe_mechanical_repairs(
            response,
            [
                {"code": "informational_requirement_forbidden", "requirement_index": 0},
                {"code": "informational_requirement_forbidden", "requirement_index": 1},
                {"code": "informational_requirement_forbidden", "requirement_index": 3},
            ],
        )
        self.assertEqual(
            [item["clause_ids"] for item in repaired["requirements"]], [["C3"]]
        )
        self.assertEqual(repairs[-1]["removed_requirement_indexes"], [0, 1, 3])
        self.assertEqual(repairs[-1]["projection"], "ordered_mask")
        self.assertEqual(
            repairs[-1]["original_index_to_repaired_index"],
            {"0": None, "1": None, "2": 0, "3": None},
        )
        self.assertEqual(
            repairs[-1]["removed_requirement_fingerprints"],
            [
                bridge._response_sha256(response["requirements"][0]),
                bridge._response_sha256(response["requirements"][1]),
                bridge._response_sha256(response["requirements"][3]),
            ],
        )
        self.assertEqual(
            repairs[-1]["source_response_sha256"],
            bridge._response_sha256(response),
        )
        self.assertEqual(
            repairs[-1]["repaired_response_sha256"],
            bridge._response_sha256(repaired),
        )
        self.assertEqual(len(response["requirements"]), 4)

    def test_mechanical_repair_removes_only_unbound_empty_requirement_placeholder(self) -> None:
        response = {
            "requirements": [
                {
                    "role": "declarations",
                    "properties": {"items": [{"heading": "固定声明"}]},
                    "clause_ids": ["C1"],
                    "evidence_ids": ["E1"],
                    "reason": "固定声明文本有来源依据。",
                    "confidence": 0.9,
                },
                {
                    "role": "body_text",
                    "properties": {
                        "style": None,
                        "top_border_pt": None,
                        "header_border_pt": None,
                        "bottom_border_pt": None,
                        "remove_vertical_borders": None,
                        "repeat_header_row": None,
                        "allow_row_split": None,
                        "keep_with_caption": None,
                        "border_widths_pt": None,
                        "continuation": None,
                    },
                    "clause_ids": [],
                    "evidence_ids": [],
                    "confidence": 0,
                    "reason": "",
                },
            ],
            "clause_reviews": [{"clause_id": "C1", "classification": "covered"}],
        }
        repaired, repairs = bridge._apply_safe_mechanical_repairs(
            response,
            [{
                "code": "contract_validation_error",
                "json_pointer": "$.requirements[1].reason",
                "raw_error": "$.requirements[1].reason: is shorter than 1 characters",
            }, {
                "code": "empty_requirement_properties",
                "json_pointer": "$.requirements[1].properties",
                "raw_error": "$.requirements[1].properties: must_include_semantic_payload",
            }, {
                "code": "schema_contract_violation",
                "json_pointer": "$.requirements[1].clause_ids",
                "raw_error": "$.requirements[1].clause_ids: must_be_non_empty",
            }, {
                "code": "schema_contract_violation",
                "json_pointer": "$.requirements[1].evidence_ids",
                "raw_error": "$.requirements[1].evidence_ids: must_be_non_empty",
            }, {
                "code": "requirement_relation_mismatch",
                "json_pointer": "$.requirements[1]",
                "raw_error": "requirements_not_referenced_by_clause_review:1",
                "relation_category": "missing_clause_relation",
            }],
        )
        self.assertIsNotNone(repaired)
        self.assertEqual(len(repaired["requirements"]), 1)
        self.assertEqual(repaired["requirements"][0]["role"], "declarations")
        self.assertEqual(
            repairs[-1]["rule_id"],
            "remove_unbound_empty_requirement_placeholder_v1",
        )
        self.assertEqual(repairs[-1]["removed_requirement_indexes"], [1])

        for field, value in (("clause_ids", ["C2"]), ("evidence_ids", ["E2"]), ("reason", "有语义")):
            response["requirements"][1][field] = value
            rejected, _ = bridge._apply_safe_mechanical_repairs(
                response,
                [{
                    "code": "empty_requirement_properties",
                    "json_pointer": "$.requirements[1].properties",
                    "raw_error": "must_include_semantic_payload",
                }],
            )
            self.assertIsNone(rejected)
            response["requirements"][1][field] = [] if field != "reason" else ""

    def test_mechanical_repair_never_removes_mixed_or_noninformational_relations(self) -> None:
        cases = [
            (
                [{"role": "body_text", "properties": {"text": "混合"}, "clause_ids": ["C1", "C2"]}],
                [
                    {"clause_id": "C1", "classification": "informational"},
                    {"clause_id": "C2", "classification": "executable"},
                ],
            ),
            (
                [{"role": "body_text", "properties": {"text": "外部"}, "clause_ids": ["C1"]}],
                [{"clause_id": "C1", "classification": "external_compliance"}],
            ),
            (
                [{"role": "body_text", "properties": {"text": "缺审查"}, "clause_ids": ["C1"]}],
                [],
            ),
        ]
        for requirements, reviews in cases:
            response = {"requirements": requirements, "clause_reviews": reviews}
            repaired, repairs = bridge._apply_safe_mechanical_repairs(
                response,
                [{
                    "code": "informational_requirement_forbidden",
                    "requirement_index": 0,
                }],
            )
            self.assertIsNone(repaired)
            self.assertEqual(repairs, [])

    def test_mechanical_repair_normalizes_explicitly_optional_continuation_caption(self) -> None:
        response = {
            "requirements": [{
                "role": "table",
                "properties": {"continuation": {
                    "caption_suffix": "(续)",
                    "repeat_header_row": True,
                    "caption_required_on_continuation": True,
                    "verification": "word_render",
                }},
                "clause_ids": ["C00243"],
                "evidence_ids": ["E00174"],
            }],
            "clause_reviews": [{"clause_id": "C00243", "classification": "executable"}],
        }
        chunk = {"clauses": [{
            "id": "C00243",
            "text": "表序后跟表题(可省略)和“(续)”，居中置于表上方，续表均应重复表头",
        }]}
        repaired, repairs = bridge._apply_safe_mechanical_repairs(
            response,
            [{
                "code": "partial_clause_coverage",
                "json_pointer": "$.clause_reviews[0]",
                "raw_error": "$.clause_reviews[0]: partial_clause_coverage:table.continuation.optional_caption_marked_required",
            }],
            chunk=chunk,
        )
        self.assertIsNotNone(repaired)
        self.assertFalse(
            repaired["requirements"][0]["properties"]["continuation"]["caption_required_on_continuation"]
        )
        self.assertEqual(repairs[0]["rule_id"], "optional_continuation_caption_is_not_required")
        self.assertTrue(response["requirements"][0]["properties"]["continuation"]["caption_required_on_continuation"])

    def test_fixed_declaration_candidate_is_derived_from_exact_chunk_evidence(self) -> None:
        packet = bridge.compact_model_packet({
            "contract_version": "3.0",
            "clauses": [
                {"id": "C1", "text": "学位论文使用授权书", "evidence_ids": ["E1"]},
                {"id": "C2", "text": "本人同意提交电子版", "evidence_ids": ["E2"]},
                {"id": "C3", "text": "学位论文作者暨授权人签字", "evidence_ids": ["E3"]},
                {"id": "C4", "text": "摘要", "evidence_ids": ["E4"]},
            ],
            "evidence_context": {
                "E1": {"id": "E1", "kind": "paragraph", "text": "学位论文使用授权书"},
                "E2": {"id": "E2", "kind": "paragraph", "text": "本人同意提交电子版"},
                "E3": {"id": "E3", "kind": "paragraph", "text": "学位论文作者暨授权人签字"},
                "E4": {"id": "E4", "kind": "paragraph", "text": "摘要"},
            },
            "declaration_anchor_preference": "abstract_title_zh",
            "requirement_contract": {},
        })
        self.assertEqual(packet["fixed_declaration_candidates"][0]["clause_ids"], ["C1", "C2"])
        self.assertEqual(packet["fixed_declaration_candidates"][0]["before_role"], "abstract_title_zh")

    def test_non_public_declaration_heading_is_derived_from_exact_chunk_evidence(self) -> None:
        packet = bridge.compact_model_packet({
            "contract_version": "3.0",
            "clauses": [
                {"id": "C1", "text": "非公开学位论文标注说明", "evidence_ids": ["E1"]},
                {"id": "C2", "text": "根据北京体育大学有关规定，非公开学位论文须经批准方能标注。", "evidence_ids": ["E2"]},
                {"id": "C3", "text": "摘要", "evidence_ids": ["E3"]},
            ],
            "evidence_context": {
                "E1": {"id": "E1", "kind": "paragraph", "text": "非公开学位论文标注说明"},
                "E2": {"id": "E2", "kind": "paragraph", "text": "根据北京体育大学有关规定，非公开学位论文须经批准方能标注。"},
                "E3": {"id": "E3", "kind": "paragraph", "text": "摘要"},
            },
            "declaration_anchor_preference": "abstract_title_zh",
            "requirement_contract": {},
        })
        candidate = packet["fixed_declaration_candidates"][0]
        self.assertEqual(candidate["clause_ids"], ["C1", "C2"])
        self.assertEqual(candidate["heading_evidence_ids"], ["E1"])

    def test_unknown_property_is_removed_deterministically_without_semantic_retry(self) -> None:
        response = {
            "requirements": [{"properties": {"style": {"name": "bad"}, "text": "标题"}}],
        }
        repaired, repairs = bridge._apply_safe_mechanical_repairs(
            response,
            [{
                "code": "unknown_property",
                "json_pointer": "$.requirements[0].properties",
                "raw_error": "$.requirements[0].properties: unknown property 'style'",
            }],
        )
        self.assertEqual(repairs[0]["removed_property"], "style")
        self.assertIsNotNone(repaired)
        self.assertNotIn("style", repaired["requirements"][0]["properties"])
        self.assertEqual(repaired["requirements"][0]["properties"]["text"], "标题")
        self.assertIn("style", response["requirements"][0]["properties"])

    def test_retry_allows_exact_text_fill_after_unknown_property_removal(self) -> None:
        previous = {
            "contract_version": "3.0",
            "requirements": [{
                "role": "abstract_title_zh",
                "properties": {"style": "three_line"},
                "clause_ids": ["C1"],
                "evidence_ids": ["E1"],
            }],
            "clause_reviews": [{"clause_id": "C1", "classification": "executable"}],
            "unsupported_items": [],
        }
        current = json.loads(json.dumps(previous))
        current["requirements"][0]["properties"] = {"text": "摘  要"}
        records = [{
            "code": "unknown_property",
            "json_pointer": "$.requirements[0].properties",
            "raw_error": "unknown property 'style'",
        }]
        chunk = {
            "evidence_context": {"E1": {"text": "摘  要"}},
            "requirement_contract": {
                "role_properties_schema": {
                    "abstract_title_zh": {"$ref": "#/$defs/roleSpec"},
                },
            },
        }
        changed = bridge._retry_change_paths(previous, current)
        self.assertEqual(
            changed,
            [
                "$.requirements[0].properties.style",
                "$.requirements[0].properties.text",
            ],
        )
        self.assertTrue(bridge._retry_changes_allowed(
            records,
            changed,
            contract_version="3.0",
            previous_response=previous,
            current_response=current,
            chunk=chunk,
        ))
        current["requirements"][0]["properties"]["text"] = "猜测标题"
        changed = bridge._retry_change_paths(previous, current)
        self.assertFalse(bridge._retry_changes_allowed(
            records,
            changed,
            contract_version="3.0",
            previous_response=previous,
            current_response=current,
            chunk=chunk,
        ))

    def test_mixed_contract_errors_are_not_mechanically_repaired(self) -> None:
        response = {"requirements": [{"properties": {"style": {}}}]}
        repaired, repairs = bridge._apply_safe_mechanical_repairs(
            response,
            [
                {
                    "code": "unknown_property",
                    "json_pointer": "$.requirements[0].properties",
                    "raw_error": "unknown property 'style'",
                },
                {
                    "code": "requirement_relation_mismatch",
                    "json_pointer": "$.requirements",
                    "raw_error": "requirements_not_referenced_by_clause_review:0",
                },
            ],
        )
        self.assertIsNone(repaired)
        self.assertEqual(repairs, [])

    def test_unbacked_evidence_id_is_removed_deterministically(self) -> None:
        response = {
            "requirements": [{
                "evidence_ids": ["E1", "E2"],
                "clause_ids": ["C1"],
            }],
        }
        repaired, repairs = bridge._apply_safe_mechanical_repairs(
            response,
            [{
                "code": "evidence_relation_mismatch",
                "json_pointer": "$.requirements[0].evidence_ids",
                "raw_error": "$.requirements[0].evidence_ids: not_backed_by_clause:E2",
            }],
        )
        self.assertEqual(repairs[0]["removed_evidence_id"], "E2")
        self.assertEqual(repaired["requirements"][0]["evidence_ids"], ["E1"])
        self.assertEqual(response["requirements"][0]["evidence_ids"], ["E1", "E2"])

    def test_empty_role_payload_is_filled_from_exact_evidence_text(self) -> None:
        response = {
            "requirements": [{
                "role": "abstract_title_zh", "evidence_ids": ["E1"],
                "properties": {},
            }],
        }
        repaired, repairs = bridge._apply_safe_mechanical_repairs(
            response,
            [{
                "code": "empty_requirement_properties",
                "json_pointer": "$.requirements[0].properties",
                "raw_error": "must_include_semantic_payload",
            }],
            chunk={
                "evidence_context": {
                    "E1": {"style_name": "heading 1", "text": "摘 要"},
                },
                "requirement_contract": {
                    "role_properties_schema": {
                        "abstract_title_zh": {"$ref": "#/$defs/roleSpec"},
                    },
                },
            },
        )
        self.assertEqual(repaired["requirements"][0]["properties"]["text"], "摘 要")
        self.assertEqual(repairs[0]["filled_property"], "text")
        self.assertNotIn("text", response["requirements"][0]["properties"])

    def test_exact_acknowledgments_limit_is_compiled_into_nested_payload(self) -> None:
        response = {
            "requirements": [{
                "role": "content_constraints",
                "properties": {},
                "clause_ids": ["C1"],
                "evidence_ids": ["E1"],
            }],
        }
        repaired, repairs = bridge._apply_safe_mechanical_repairs(
            response,
            [{
                "code": "empty_requirement_properties",
                "json_pointer": "$.requirements[0].properties",
                "raw_error": "must_include_semantic_payload",
            }],
            chunk={
                "clauses": [{
                    "id": "C1", "text": "字数一般不超过500字", "evidence_ids": ["E1"],
                }],
                "evidence_context": {
                    "E1": {"id": "E1", "text": "字数一般不超过500字"},
                },
                "requirement_contract": {},
            },
        )
        self.assertIsNotNone(repaired)
        self.assertEqual(
            repaired["requirements"][0]["properties"],
            {"acknowledgments": {"max_chars": 500}},
        )
        self.assertEqual(repairs[0]["rule_id"], "compile_exact_acknowledgments_max_chars_v1")

        response["requirements"][0]["clause_ids"] = ["C2"]
        rejected, _ = bridge._apply_safe_mechanical_repairs(
            response,
            [{
                "code": "empty_requirement_properties",
                "json_pointer": "$.requirements[0].properties",
                "raw_error": "must_include_semantic_payload",
            }],
            chunk={
                "clauses": [{"id": "C2", "text": "致谢内容应简短", "evidence_ids": ["E1"]}],
                "evidence_context": {"E1": {"id": "E1", "text": "致谢内容应简短"}},
                "requirement_contract": {},
            },
        )
        self.assertIsNone(rejected)

    def test_exact_appendix_page_break_is_compiled_into_declared_payload(self) -> None:
        response = {
            "requirements": [{
                "role": "appendices",
                "properties": {
                    "required_when_profile_has_appendices": None,
                    "label_style": None,
                    "label_prefix": None,
                    "page_break_each": None,
                    "per_appendix_title_required": None,
                },
                "clause_ids": ["C1"],
                "evidence_ids": ["E1"],
            }],
        }
        repaired, repairs = bridge._apply_safe_mechanical_repairs(
            response,
            [{
                "code": "empty_requirement_properties",
                "json_pointer": "$.requirements[0].properties",
                "raw_error": "must_include_semantic_payload",
            }],
            chunk={
                "clauses": [{
                    "id": "C1", "text": "附录放在正文之后另起页", "evidence_ids": ["E1"],
                }],
                "evidence_context": {
                    "E1": {"id": "E1", "text": "附录放在正文之后另起页"},
                },
                "requirement_contract": {},
            },
        )
        self.assertIsNotNone(repaired)
        self.assertEqual(
            repaired["requirements"][0]["properties"],
            {"page_break_each": True},
        )
        self.assertEqual(repairs[0]["rule_id"], "compile_exact_appendix_page_break_v1")

        response["requirements"][0]["clause_ids"] = ["C2"]
        rejected, _ = bridge._apply_safe_mechanical_repairs(
            response,
            [{
                "code": "empty_requirement_properties",
                "json_pointer": "$.requirements[0].properties",
                "raw_error": "must_include_semantic_payload",
            }],
            chunk={
                "clauses": [{"id": "C2", "text": "附录放在正文之后"}],
                "evidence_context": {"E1": {"id": "E1", "text": "附录放在正文之后"}},
                "requirement_contract": {},
            },
        )
        self.assertIsNone(rejected)

    def test_exact_appendix_label_title_is_compiled_into_declared_payload(self) -> None:
        response = {
            "requirements": [{
                "role": "appendices",
                "properties": {
                    "required_when_profile_has_appendices": None,
                    "label_style": None,
                    "label_prefix": None,
                    "page_break_each": None,
                    "per_appendix_title_required": None,
                },
                "clause_ids": ["C1"],
                "evidence_ids": ["E1"],
            }],
        }
        repaired, repairs = bridge._apply_safe_mechanical_repairs(
            response,
            [{
                "code": "empty_requirement_properties",
                "json_pointer": "$.requirements[0].properties",
                "raw_error": "must_include_semantic_payload",
            }],
            chunk={
                "clauses": [{
                    "id": "C1",
                    "text": "附录的序号用A，B，C，…系列，如附录A，附录B等，每个附录应有标题",
                    "evidence_ids": ["E1"],
                }],
                "evidence_context": {
                    "E1": {
                        "id": "E1",
                        "text": "附录的序号用A，B，C，…系列，如附录A，附录B等，每个附录应有标题",
                    },
                },
                "requirement_contract": {},
            },
        )
        self.assertIsNotNone(repaired)
        self.assertEqual(
            repaired["requirements"][0]["properties"],
            {"label_style": "alpha_upper", "per_appendix_title_required": True},
        )
        self.assertEqual(repairs[0]["rule_id"], "compile_exact_appendix_label_title_v1")

    def test_empty_administrative_cover_institution_uses_neutral_placeholder(self) -> None:
        response = {
            "requirements": [{
                "role": "cover",
                "properties": {
                    "institution": "",
                    "fields": [{"id": "title_zh"}],
                    "non_public_administration": None,
                    "missing_value_policy": "placeholder",
                    "missing_value_placeholder": "——",
                },
                "clause_ids": ["C1"],
                "evidence_ids": ["E1"],
            }],
        }
        repaired, repairs = bridge._apply_safe_mechanical_repairs(
            response,
            [{
                "code": "cover_institution_placeholder",
                "json_pointer": "$.requirements[0].properties",
            }, {
                "code": "cover_institution_placeholder",
                "json_pointer": "$.requirements[0].properties.institution",
            }],
        )
        self.assertIsNotNone(repaired)
        self.assertEqual(
            repaired["requirements"][0]["properties"]["institution"],
            "——",
        )
        self.assertEqual(
            repairs[0]["rule_id"],
            "compile_neutral_cover_institution_placeholder_v1",
        )

        response["requirements"][0]["properties"].pop("missing_value_placeholder")
        rejected, _ = bridge._apply_safe_mechanical_repairs(
            response,
            [{
                "code": "cover_institution_placeholder",
                "json_pointer": "$.requirements[0].properties.institution",
            }],
        )
        self.assertIsNone(rejected)

    def test_combined_mechanical_repairs_do_not_require_semantic_retry(self) -> None:
        response = {
            "contract_version": "3.0",
            "requirements": [
                {
                    "role": "cover_field_label",
                    "properties": {"style": "three_line", "continuation": None},
                    "clause_ids": ["C1"], "evidence_ids": ["E1"],
                },
                {
                    "role": "cover",
                    "properties": {
                        "institution": "",
                        "fields": [],
                        "missing_value_policy": "placeholder",
                        "missing_value_placeholder": "——",
                    },
                    "clause_ids": ["C2"], "evidence_ids": ["E2"],
                },
                {
                    "role": "declarations",
                    "properties": {"items": [{
                        "id": "authorization",
                        "source_evidence_ids": ["E3", "E3", "E4"],
                    }]},
                    "clause_ids": ["C3"], "evidence_ids": ["E3", "E4"],
                },
            ],
            "clause_reviews": [], "unsupported_items": [], "reported_conflicts": [],
        }
        records = [
            {
                "code": "unknown_property",
                "json_pointer": "$.requirements[0].properties",
                "raw_error": "unknown property 'style'",
            },
            {
                "code": "cover_institution_placeholder",
                "json_pointer": "$.requirements[1].properties",
            },
            {
                "code": "contract_validation_error",
                "json_pointer": "$.requirements[2].properties",
                "raw_error": "must match at least one schema in anyOf",
            },
            {
                "code": "contract_validation_error",
                "json_pointer": "$.requirements[2].properties.items[0].source_evidence_ids",
                "raw_error": "items must be unique",
            },
        ]
        repaired, repairs = bridge._apply_safe_mechanical_repairs(response, records)
        self.assertIsNotNone(repaired)
        assert repaired is not None
        self.assertNotIn("style", repaired["requirements"][0]["properties"])
        self.assertEqual(
            repaired["requirements"][1]["properties"]["institution"], "——",
        )
        self.assertEqual(
            repaired["requirements"][2]["properties"]["items"][0]["source_evidence_ids"],
            ["E3", "E4"],
        )
        self.assertTrue(any(item.get("rule_id") == "deduplicate_first_occurrence_source_evidence_ids_v1"
                            for item in repairs))

    def test_unknown_layout_properties_are_removed_then_exact_text_is_filled(self) -> None:
        response = {
            "requirements": [{
                "role": "cover_field_label", "evidence_ids": ["E1"],
                "properties": {
                    "style": "three_line",
                    "remove_vertical_borders": False,
                    "repeat_header_row": False,
                    "allow_row_split": False,
                    "keep_with_caption": False,
                },
            }],
        }
        records = [
            {
                "code": "unknown_property",
                "json_pointer": "$.requirements[0].properties",
                "raw_error": f"unknown property '{name}'",
            }
            for name in (
                "style", "remove_vertical_borders", "repeat_header_row",
                "allow_row_split", "keep_with_caption",
            )
        ]
        repaired, repairs = bridge._apply_safe_mechanical_repairs(
            response,
            records,
            chunk={
                "evidence_context": {"E1": {"text": "论文题目（中文）"}},
                "requirement_contract": {
                    "role_properties_schema": {
                        "cover_field_label": {"$ref": "#/$defs/roleSpec"},
                    },
                },
            },
        )
        self.assertEqual(
            repaired["requirements"][0]["properties"],
            {"text": "论文题目（中文）"},
        )
        self.assertEqual(
            [item["code"] for item in repairs],
            ["unknown_property"] * 5 + ["empty_requirement_properties"],
        )

    def test_english_presence_fact_is_compiled_only_from_explicit_clause(self) -> None:
        response = {
            "requirements": [{
                "role": "body_text",
                "properties": {"font": {"latin": "Times New Roman"}},
                "clause_ids": ["C1"],
                "applicability": {
                    "status": "conditional",
                    "conditions": [{
                        "fact": "English text appears in the thesis",
                        "operator": "present",
                        "value": None,
                    }],
                    "exceptions": None,
                },
            }],
        }
        repaired, repairs = bridge._apply_safe_mechanical_repairs(
            response,
            [{
                "code": "applicability_fact_namespace",
                "json_pointer": "$.requirements[0].applicability.conditions[0].fact",
                "raw_error": (
                    "$.requirements[0].applicability.conditions[0].fact: "
                    "does not match the registered fact namespace"
                ),
            }],
            chunk={
                "clauses": [{
                    "id": "C1",
                    "text": "论文中出现英文时需要使用Times New Roman字体",
                }],
            },
        )
        self.assertEqual(
            repaired["requirements"][0]["applicability"]["conditions"][0]["fact"],
            "source_inventory.english_text",
        )
        self.assertEqual(repairs[0]["rule_id"], "explicit_english_presence_times_new_roman")
        self.assertEqual(
            response["requirements"][0]["applicability"]["conditions"][0]["fact"],
            "English text appears in the thesis",
        )

    def test_english_presence_fact_is_not_guessed_for_other_conditions(self) -> None:
        response = {
            "requirements": [{
                "role": "body_text",
                "properties": {"font": {"latin": "Times New Roman"}},
                "clause_ids": ["C1"],
                "applicability": {
                    "status": "conditional",
                    "conditions": [{
                        "fact": "some prose condition",
                        "operator": "present",
                        "value": None,
                    }],
                },
            }],
        }
        repaired, repairs = bridge._apply_safe_mechanical_repairs(
            response,
            [{
                "code": "applicability_fact_namespace",
                "json_pointer": "$.requirements[0].applicability.conditions[0].fact",
                "raw_error": "does not match the registered fact namespace",
            }],
            chunk={"clauses": [{"id": "C1", "text": "正文采用小四号字体"}]},
        )
        self.assertIsNone(repaired)
        self.assertEqual(repairs, [])

    def test_bridge_retries_locally_rejected_contract_in_a_new_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, chunk = self._packet(Path(td) / "requirements")
            invalid = self._executable_response(chunk, invalid_verification=True)
            valid = self._executable_response(chunk)
            envelopes = [
                {"runId": "openclaw-run-contract-retry-1", "status": "ok",
                 "provider": "openai", "model": "gpt-5.6-luna",
                 "result": {"payloads": [{"text": json.dumps(invalid)}]}},
                {"runId": "openclaw-run-contract-retry-2", "status": "ok",
                 "provider": "openai", "model": "gpt-5.6-luna",
                 "result": {"payloads": [{"text": json.dumps(valid)}]}},
            ]
            response_out = Path(td) / "host-agent-response.json"
            fake_results = [
                subprocess.CompletedProcess(["openclaw"], 0, json.dumps(envelopes[0]), ""),
                subprocess.CompletedProcess(["openclaw"], 0, json.dumps(envelopes[1]), ""),
            ]
            with patch.object(bridge, "_run_command", side_effect=fake_results) as run:
                audit = bridge.run_bridge(
                    review_dir, response_out=response_out,
                    agent_id="main", timeout=1, max_attempts=2,
                    openclaw_bin="openclaw", model="openai/gpt-5.6-luna",
                )
            self.assertEqual(audit["status"], "merged")
            self.assertEqual(audit["chunk_runs"][0]["attempt"], 2)
            self.assertIn("local response contract validation failed",
                          audit["chunk_runs"][0]["attempt_failures"][0])
            second_prompt = Path(
                run.call_args_list[1].args[0][run.call_args_list[1].args[0].index("--message-file") + 1]
            ).read_text(encoding="utf-8")
            self.assertIn("local contract validation failed", second_prompt)

    def test_retry_cannot_hide_a_semantic_classification_change(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, chunk = self._packet(Path(td) / "requirements")
            invalid = self._executable_response(chunk, invalid_verification=True)
            semantic_change = self._response(chunk)
            envelopes = [
                {"runId": "openclaw-run-semantic-retry-1", "status": "ok",
                 "provider": "openai", "model": "gpt-5.6-luna",
                 "result": {"payloads": [{"text": json.dumps(invalid)}]}},
                {"runId": "openclaw-run-semantic-retry-2", "status": "ok",
                 "provider": "openai", "model": "gpt-5.6-luna",
                 "result": {"payloads": [{"text": json.dumps(semantic_change)}]}},
            ]
            response_out = Path(td) / "host-agent-response.json"
            fake_results = [
                subprocess.CompletedProcess(["openclaw"], 0, json.dumps(envelopes[0]), ""),
                subprocess.CompletedProcess(["openclaw"], 0, json.dumps(envelopes[1]), ""),
            ]
            with patch.object(bridge, "_run_command", side_effect=fake_results):
                with self.assertRaisesRegex(ValueError, "semantic re-review"):
                    bridge.run_bridge(
                        review_dir, response_out=response_out,
                        agent_id="main", timeout=1, max_attempts=2,
                        openclaw_bin="openclaw", model="openai/gpt-5.6-luna",
                    )
            failure = json.loads((review_dir / "host-agent-run.json").read_text(encoding="utf-8"))
            self.assertFalse(failure["merged_response_written"])
            self.assertTrue(failure["structured_error_records"])
            self.assertEqual(failure["structured_error_records"][-1]["code"], "semantic_retry_change")

    def test_bridge_binds_missing_native_provenance_and_preserves_raw_response(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, chunk = self._packet(Path(td) / "requirements")
            response = self._response(chunk)
            del response["provenance"]
            envelope = {
                "runId": "openclaw-run-provenance-bind",
                "status": "ok",
                "provider": "openai", "model": "gpt-5.6-luna",
                "result": {"payloads": [{"text": json.dumps(response)}]},
            }
            response_out = Path(td) / "host-agent-response.json"
            fake = subprocess.CompletedProcess(
                ["openclaw"], 0, json.dumps(envelope), "",
            )
            with patch.object(bridge, "_run_command", return_value=fake):
                audit = bridge.run_bridge(
                    review_dir, response_out=response_out,
                    agent_id="main", timeout=1, max_attempts=1,
                    openclaw_bin="openclaw", model="openai/gpt-5.6-luna",
                )
            self.assertTrue(response_out.exists())
            raw_responses = list(review_dir.glob("*.raw.json"))
            self.assertEqual(len(raw_responses), 1)
            raw = json.loads(raw_responses[0].read_text(encoding="utf-8"))
            self.assertNotIn("provenance", raw)
            merged = json.loads(response_out.read_text(encoding="utf-8"))
            full_request = json.loads(
                (review_dir / "llm-request.json").read_text(encoding="utf-8")
            )
            self.assertEqual(merged["provenance"], full_request["provenance"])
            self.assertEqual(
                audit["chunk_runs"][0]["provenance_binding"],
                "bridge_generated",
            )
            self.assertEqual(
                audit["chunk_runs"][0]["provenance_mismatch_fields"],
                [],
            )

    def test_bridge_rejects_conflicting_native_provenance_without_rebinding(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, chunk = self._packet(Path(td) / "requirements")
            response = self._response(chunk)
            response["provenance"] = dict(chunk["provenance"])
            response["provenance"]["request_sha256"] = "0" * 64
            envelope = {
                "runId": "openclaw-run-provenance-conflict",
                "status": "ok",
                "provider": "openai", "model": "gpt-5.6-luna",
                "result": {"payloads": [{"text": json.dumps(response)}]},
            }
            response_out = Path(td) / "host-agent-response.json"
            fake = subprocess.CompletedProcess(
                ["openclaw"], 0, json.dumps(envelope), "",
            )
            with patch.object(bridge, "_run_command", return_value=fake):
                with self.assertRaisesRegex(bridge.HostAgentProvenanceMismatch, "provenance conflict"):
                    bridge.run_bridge(
                        review_dir, response_out=response_out,
                        agent_id="main", timeout=1, max_attempts=2,
                        openclaw_bin="openclaw", model="openai/gpt-5.6-luna",
                    )
            self.assertFalse(response_out.exists())
            audit = json.loads((review_dir / "host-agent-run.json").read_text(encoding="utf-8"))
            self.assertEqual(audit["error_type"], "HostAgentProvenanceMismatch")
            self.assertFalse(audit["merged_response_written"])

    def test_semantic_retry_projection_rebinds_current_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td) / "requirements"
            clauses = [{
                "id": "C1", "text": "图题应简明并置于图序之后", "evidence_ids": ["E1"],
                "source_kind": "paragraph", "location": {}, "part_index": 0,
            }]
            evidence = {
                "evidence": [{"id": "E1", "text": clauses[0]["text"], "kind": "paragraph"}],
            }
            request = engine.build_llm_request(
                [], clauses, evidence, {}, "full", contract_version="3.0",
            )
            request = attach_request_provenance(
                request, source_sha256="a" * 64, evidence_doc=evidence,
                clauses=clauses, run_id="run-v3-provenance-retry",
            )
            engine.prepare_host_agent_review_packets(
                request, clauses, evidence, "a" * 64, directory, chunk_size=1,
            )
            chunk = json.loads(
                (directory / "llm-request-chunks.json").read_text(encoding="utf-8")
            )[0]

            def review(classification: str) -> dict:
                return {
                    "clause_id": "C1",
                    "classification": classification,
                    "obligations": [
                        {"id": "caption_after_number", "status": "covered", "reason": "位置已由 requirement 表示。"},
                        {"id": "concise_caption", "status": "unverifiable", "reason": "简明性需要人工判断。"},
                    ],
                    "reason": "当前证据已审查。",
                    "normative_basis": "explicit_normative_text",
                }

            first = {
                "contract_version": "3.0",
                "requirements": [{
                    "role": "figure_caption",
                    "properties": {"position": "below"},
                    "clause_ids": ["C1"],
                    "evidence_ids": ["E1"],
                    "confidence": 0.9,
                    "reason": "证据支持图题位置。",
                    "verification": {"mode": "static_docx", "checks": ["检查图题位置。"]},
                }],
                "clause_reviews": [review("executable")],
                "unsupported_items": [],
                "reported_conflicts": [],
            }
            second = {
                "contract_version": "3.0",
                "requirements": [],
                "clause_reviews": [review("unverifiable")],
                "unsupported_items": [],
                "reported_conflicts": [],
            }
            envelopes = [
                {"runId": "openclaw-v3-provenance-retry-1", "status": "ok",
                 "provider": "openai", "model": "gpt-5.6-luna",
                 "result": {"payloads": [{"text": json.dumps(first, ensure_ascii=False)}]}},
                {"runId": "openclaw-v3-provenance-retry-2", "status": "ok",
                 "provider": "openai", "model": "gpt-5.6-luna",
                 "result": {"payloads": [{"text": json.dumps(second, ensure_ascii=False)}]}},
            ]
            response_out = Path(td) / "host-agent-response.json"
            fake_results = [
                subprocess.CompletedProcess(["openclaw"], 0, json.dumps(envelopes[0]), ""),
                subprocess.CompletedProcess(["openclaw"], 0, json.dumps(envelopes[1]), ""),
            ]
            with patch.object(bridge, "_run_command", side_effect=fake_results):
                audit = bridge.run_bridge(
                    directory, response_out=response_out,
                    agent_id="main", timeout=1, max_attempts=2,
                    openclaw_bin="openclaw", model="openai/gpt-5.6-luna",
                )

            merged = json.loads(response_out.read_text(encoding="utf-8"))
            full_request = json.loads(
                (directory / "llm-request.json").read_text(encoding="utf-8")
            )
            self.assertEqual(merged["provenance"], full_request["provenance"])
            self.assertEqual(merged["clause_reviews"][0]["classification"], "unverifiable")
            self.assertEqual(merged["requirements"], [])
            self.assertEqual(audit["status"], "merged")
            self.assertEqual(audit["chunk_runs"][0]["provenance_binding"], "bridge_generated")

    def test_bridge_rejects_tampered_chunk_before_starting_native_agent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, _chunk = self._packet(Path(td) / "requirements")
            chunks_path = review_dir / "llm-request-chunks.json"
            chunks = json.loads(chunks_path.read_text(encoding="utf-8"))
            chunks[0]["clauses"][0]["text"] = "被篡改的正文"
            chunks_path.write_text(json.dumps(chunks, ensure_ascii=False), encoding="utf-8")
            with patch.object(bridge, "_run_command") as run:
                with self.assertRaisesRegex(ValueError, "source projection"):
                    bridge.run_bridge(
                        review_dir, response_out=Path(td) / "host-agent-response.json",
                        agent_id="main", timeout=1, max_attempts=1,
                        openclaw_bin="openclaw", model="openai/gpt-5.6-luna",
                    )
            run.assert_not_called()

    def test_bridge_requires_declared_host_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, _chunk = self._packet(Path(td) / "requirements")
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "THESIS_FORGE_HOST_RUNTIME"):
                    bridge.run_bridge(review_dir, openclaw_bin="openclaw")

    def test_bridge_refuses_host_runtime_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, _chunk = self._packet(Path(td) / "requirements")
            with self.assertRaisesRegex(RuntimeError, "host runtime mismatch"):
                bridge.run_bridge(
                    review_dir, host_runtime="codex", openclaw_bin="openclaw",
                )

    def test_bridge_refuses_recent_parent_session_guess(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, _chunk = self._packet(Path(td) / "requirements")
            with self.assertRaisesRegex(RuntimeError, "refusing to select a recent"):
                bridge.run_bridge(
                    review_dir, inherit_parent_model=True,
                    openclaw_bin="openclaw",
                )

    def test_bridge_refuses_unbound_gateway_default(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, _chunk = self._packet(Path(td) / "requirements")
            with self.assertRaisesRegex(RuntimeError, "explicit model route"):
                bridge.run_bridge(review_dir, openclaw_bin="openclaw")

    def test_failed_bridge_persists_failure_audit_without_merged_response(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, chunk = self._packet(Path(td) / "requirements")
            response = self._response(chunk)
            response["clause_reviews"][0]["requirement_indexes"] = [0]
            envelope = {
                "runId": "openclaw-run-final-contract-failure",
                "status": "ok",
                "provider": "openai", "model": "gpt-5.6-luna",
                "result": {"payloads": [{"text": json.dumps(response)}]},
            }
            response_out = Path(td) / "host-agent-response.json"
            fake = subprocess.CompletedProcess(
                ["openclaw"], 0, json.dumps(envelope), "",
            )
            with patch.object(bridge, "_run_command", return_value=fake):
                with self.assertRaises(ValueError):
                    bridge.run_bridge(
                        review_dir, response_out=response_out,
                        agent_id="main", timeout=1, max_attempts=1,
                        openclaw_bin="openclaw", model="openai/gpt-5.6-luna",
                    )
            failure = json.loads((review_dir / "host-agent-run.json").read_text(encoding="utf-8"))
            self.assertEqual(failure["status"], "failed")
            self.assertFalse(failure["merged_response_written"])
            self.assertFalse(response_out.exists())
            self.assertEqual(len(failure["chunk_lifecycle"]), 1)
            self.assertEqual(failure["chunk_lifecycle"][0]["status"], "failed")
            self.assertEqual(failure["chunk_lifecycle"][0]["attempts"][0]["status"], "failed")

    def test_bridge_calls_openclaw_without_delivery_and_merges_fresh_response(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, chunk = self._packet(Path(td) / "requirements")
            response = self._response(chunk)
            envelope = {
                "runId": "openclaw-run-test",
                "status": "ok",
                "provider": "openai", "model": "gpt-5.6-luna",
                "result": {"payloads": [{"text": json.dumps(response, ensure_ascii=False)}]},
            }
            response_out = Path(td) / "host-agent-response.json"
            fake = subprocess.CompletedProcess(
                ["openclaw"], 0, json.dumps(envelope), "",
            )
            with patch.object(bridge, "_run_command", return_value=fake) as run:
                audit = bridge.run_bridge(
                    review_dir, response_out=response_out,
                    agent_id="main", timeout=1, openclaw_bin="openclaw",
                    model="openai/gpt-5.6-luna", runner="gateway",
                )
            self.assertEqual(audit["status"], "merged")
            self.assertEqual(audit["chunk_count"], 1)
            self.assertEqual(json.loads(response_out.read_text(encoding="utf-8"))["contract_version"], "2.1")
            command = run.call_args.args[0]
            self.assertIn("--session-key", command)
            self.assertIn("--message-file", command)
            self.assertNotIn("--deliver", command)
            self.assertTrue((review_dir / "host-agent-run.json").is_file())

    def test_bridge_calls_native_codex_without_openclaw_route_or_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, chunk = self._packet(Path(td) / "requirements")
            response = self._response(chunk)
            events = "\n".join([
                json.dumps({"type": "thread.started", "thread_id": "codex-thread"}),
                json.dumps({
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": json.dumps(response)},
                }),
                json.dumps({"type": "turn.completed", "usage": {"output_tokens": 1}}),
            ])
            response_out = Path(td) / "host-agent-response.json"
            fake = subprocess.CompletedProcess(["codex", "exec"], 0, events, "")
            with patch.dict(os.environ, {"THESIS_FORGE_HOST_RUNTIME": "codex"}):
                with patch.object(bridge, "_run_command", return_value=fake) as run:
                    audit = bridge.run_bridge(
                        review_dir, response_out=response_out,
                        timeout=1, max_attempts=1,
                        codex_bin=sys.executable,
                        allow_prompt_only=True,
                    )
            command = run.call_args.args[0]
            self.assertEqual(command[1], "exec")
            self.assertIn("--json", command)
            self.assertIn("--sandbox", command)
            self.assertNotIn("--model", command)
            self.assertNotIn("openclaw", " ".join(command).lower())
            self.assertNotIn("--session-key", command)
            self.assertEqual(audit["adapter_id"], "codex")
            self.assertEqual(audit["route_visibility"], "unobservable")
            self.assertEqual(audit["observed_routes"], ["unobservable"])
            self.assertEqual(audit["chunk_runs"][0]["actual_route"], "unobservable")
            self.assertEqual(json.loads(response_out.read_text(encoding="utf-8"))["contract_version"], "2.1")

    def test_bridge_refuses_prompt_only_codex_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, _chunk = self._packet(Path(td) / "requirements")
            with patch.dict(os.environ, {"THESIS_FORGE_HOST_RUNTIME": "codex"}):
                with patch.object(bridge.codex_adapter, "probe_capabilities", return_value={
                    "output_schema_supported": False,
                    "structured_output_mode": "prompt_only",
                }):
                    with self.assertRaisesRegex(RuntimeError, "refusing prompt-only"):
                        bridge.run_bridge(
                            review_dir,
                            host_runtime="codex",
                            codex_bin=sys.executable,
                            max_attempts=1,
                        )

    def test_cli_does_not_inject_parent_inheritance_for_codex(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, {"THESIS_FORGE_HOST_RUNTIME": "codex"}):
                with patch.object(bridge, "run_bridge", return_value={"status": "mocked"}) as run:
                    code = bridge.main([
                        td,
                        "--host-runtime", "codex",
                    ])
            self.assertEqual(code, 0)
            self.assertFalse(run.call_args.kwargs["inherit_parent_model"])
            self.assertIsNone(run.call_args.kwargs["codex_model"])

    def test_resolve_parent_model_copies_provider_and_model_override(self) -> None:
        parent_key = "agent:main:telegram:direct:chat:thread:38479"
        sessions = {
            "sessions": [{
                "key": parent_key,
                "agentId": "main",
                "kind": "direct",
                "modelOverride": "gpt-5.6-luna",
                "providerOverride": "openai",
                "updatedAt": 123,
            }],
        }
        fake = subprocess.CompletedProcess(
            ["openclaw", "sessions"], 0, json.dumps(sessions), "",
        )
        with patch.object(bridge, "_run_command", return_value=fake) as run:
            resolved = bridge.resolve_parent_model(
                "openclaw", agent_id="main", parent_session_key=parent_key,
            )
        self.assertEqual(resolved["model"], "openai/gpt-5.6-luna")
        self.assertEqual(resolved["source"], "parent-session-override")
        self.assertEqual(resolved["parent_model_override"], "gpt-5.6-luna")
        self.assertEqual(resolved["parent_provider_override"], "openai")
        self.assertEqual(run.call_args.args[0][0:3], ["openclaw", "sessions", "--json"])
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--agent") + 1], "main")

    def test_bridge_passes_inherited_parent_route_to_each_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, chunk = self._packet(Path(td) / "requirements")
            response = self._response(chunk)
            parent_key = "agent:main:telegram:direct:chat:thread:38479"
            sessions = {
                "sessions": [{
                    "key": parent_key,
                    "agentId": "main",
                    "kind": "direct",
                    "modelOverride": "gpt-5.6-luna",
                    "providerOverride": "openai",
                    "updatedAt": 123,
                }],
            }
            envelope = {
                "runId": "openclaw-run-inherit-test",
                "status": "ok",
                "result": {
                    "payloads": [{"text": json.dumps(response)}],
                    "meta": {"agentMeta": {
                        "provider": "openai", "model": "gpt-5.6-luna",
                    }},
                },
            }
            session_result = subprocess.CompletedProcess(
                ["openclaw", "sessions"], 0, json.dumps(sessions), "",
            )
            agent_result = subprocess.CompletedProcess(
                ["openclaw", "agent"], 0, json.dumps(envelope), "",
            )
            response_out = Path(td) / "host-agent-response.json"

            def fake_run(command, **kwargs):
                if "sessions" in command:
                    return session_result
                return agent_result

            with patch.object(bridge, "_run_command", side_effect=fake_run) as run:
                audit = bridge.run_bridge(
                    review_dir, response_out=response_out,
                    agent_id="main", timeout=1, openclaw_bin="openclaw",
                    inherit_parent_model=True, parent_session_key=parent_key,
                    auth_env_only=True,
                )
            command = next(
                call.args[0] for call in run.call_args_list if "agent" in call.args[0]
            )
            self.assertEqual(command[command.index("--model") + 1], "openai/gpt-5.6-luna")
            self.assertIn("--auth-env-only", command)
            self.assertEqual(audit["model"], "openai/gpt-5.6-luna")
            self.assertEqual(audit["model_source"], "parent-session-override")
            self.assertEqual(audit["chunk_runs"][0]["actual_route"], "openai/gpt-5.6-luna")

    def test_bridge_can_use_gateway_runner_without_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, chunk = self._packet(Path(td) / "requirements")
            response = self._response(chunk)
            envelope = {
                "runId": "openclaw-run-gateway-test",
                "status": "ok",
                "result": {
                    "payloads": [{"text": json.dumps(response)}],
                    "meta": {"agentMeta": {
                        "provider": "openai", "model": "gpt-5.6-luna",
                    }},
                },
            }
            response_out = Path(td) / "host-agent-response.json"
            fake = subprocess.CompletedProcess(
                ["openclaw", "agent"], 0, json.dumps(envelope), "",
            )
            with patch.object(bridge, "_run_command", return_value=fake) as run:
                audit = bridge.run_bridge(
                    review_dir, response_out=response_out,
                    agent_id="main", timeout=1, openclaw_bin="openclaw",
                    model="openai/gpt-5.6-luna", runner="gateway",
                )
            command = run.call_args.args[0]
            self.assertIn("--agent", command)
            self.assertIn("--session-key", command)
            self.assertIn("--model", command)
            self.assertNotIn("--deliver", command)
            session_key = command[command.index("--session-key") + 1]
            self.assertTrue(session_key.startswith("agent:main:thesis-host-agent:"))
            self.assertEqual(audit["runner"], "gateway")
            self.assertEqual(audit["chunk_runs"][0]["runner"], "gateway-agent")

    def test_resolve_parent_model_uses_effective_route_without_override(self) -> None:
        parent_key = "agent:main:telegram:direct:chat:thread:38479"
        sessions = {
            "sessions": [{
                "key": parent_key,
                "agentId": "main",
                "kind": "direct",
                "modelProvider": "sub2api",
                "model": "gpt-5.6-sol",
                "updatedAt": 123,
            }],
        }
        fake = subprocess.CompletedProcess(
            ["openclaw", "sessions"], 0, json.dumps(sessions), "",
        )
        with patch.object(bridge, "_run_command", return_value=fake):
            resolved = bridge.resolve_parent_model(
                "openclaw", agent_id="main", parent_session_key=parent_key,
            )
        self.assertEqual(resolved["model"], "sub2api/gpt-5.6-sol")
        self.assertEqual(resolved["source"], "parent-session-effective")
        self.assertEqual(resolved["parent_effective_provider"], "sub2api")
        self.assertEqual(resolved["parent_effective_model"], "gpt-5.6-sol")

    def test_resolve_parent_model_refuses_unresolved_effective_route(self) -> None:
        parent_key = "agent:main:telegram:direct:chat:thread:38479"
        sessions = {
            "sessions": [{
                "key": parent_key,
                "agentId": "main",
                "kind": "direct",
                "updatedAt": 123,
            }],
        }
        fake = subprocess.CompletedProcess(
            ["openclaw", "sessions"], 0, json.dumps(sessions), "",
        )
        with patch.object(bridge, "_run_command", return_value=fake):
            with self.assertRaisesRegex(ValueError, "refusing to use the gateway default"):
                bridge.resolve_parent_model(
                    "openclaw", agent_id="main", parent_session_key=parent_key,
                )

    def test_bridge_fails_closed_on_child_route_mismatch_without_retry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, chunk = self._packet(Path(td) / "requirements")
            response = self._response(chunk)
            parent_key = "agent:main:telegram:direct:chat:thread:38479"
            sessions = {
                "sessions": [{
                    "key": parent_key,
                    "agentId": "main",
                    "kind": "direct",
                    "modelProvider": "openai",
                    "model": "gpt-5.6-luna",
                    "updatedAt": 123,
                }],
            }
            envelope = {
                "runId": "openclaw-run-route-mismatch",
                "status": "ok",
                "provider": "sub2api",
                "model": "gpt-5.6-sol",
                "payloads": [{"text": json.dumps(response)}],
            }
            session_result = subprocess.CompletedProcess(
                ["openclaw", "sessions"], 0, json.dumps(sessions), "",
            )
            agent_result = subprocess.CompletedProcess(
                ["openclaw", "agent", "exec"], 0, json.dumps(envelope), "",
            )
            calls = []

            def fake_run(command, **kwargs):
                calls.append(command)
                return session_result if "sessions" in command else agent_result

            with patch.object(bridge, "_run_command", side_effect=fake_run) as run:
                with self.assertRaises(bridge.HostAgentRouteMismatch):
                    bridge.run_bridge(
                        review_dir,
                        response_out=Path(td) / "host-agent-response.json",
                        agent_id="main", timeout=1, max_attempts=2,
                        openclaw_bin="openclaw",
                        inherit_parent_model=True,
                        parent_session_key=parent_key,
                    )
            # One owned session-discovery call plus one child call; a route
            # mismatch must still not trigger a second child attempt.
            self.assertEqual(len(calls), 2)
            self.assertIn("sessions", calls[0])
            self.assertIn("agent", calls[1])
            self.assertFalse((Path(td) / "host-agent-response.json").exists())

    def test_bridge_refuses_existing_chunk_response_instead_of_reusing_it(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, chunk = self._packet(Path(td) / "requirements")
            response_name = json.loads(
                (review_dir / "host-agent-review-manifest.json").read_text(encoding="utf-8")
            )["response_files"][0]
            (review_dir / response_name).write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "refusing to reuse an existing chunk response"):
                bridge.run_bridge(
                    review_dir, response_out=Path(td) / "response.json", timeout=1,
                    model="openai/gpt-5.6-luna",
                )

    def test_bridge_retries_rejected_json_in_a_new_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, chunk = self._packet(Path(td) / "requirements")
            response = self._response(chunk)
            envelope = {
                "runId": "openclaw-run-retry-test",
                "status": "ok",
                "provider": "openai", "model": "gpt-5.6-luna",
                "result": {"payloads": [{"text": json.dumps(response, ensure_ascii=False)}]},
            }
            response_out = Path(td) / "host-agent-response.json"
            fake_results = [
                subprocess.CompletedProcess(["openclaw"], 0, "{broken", ""),
                subprocess.CompletedProcess(["openclaw"], 0, json.dumps(envelope), ""),
            ]
            with patch.object(bridge, "_run_command", side_effect=fake_results) as run:
                audit = bridge.run_bridge(
                    review_dir, response_out=response_out,
                    agent_id="main", timeout=1, max_attempts=2,
                    openclaw_bin="openclaw", model="openai/gpt-5.6-luna",
                    runner="gateway",
                )
            self.assertEqual(audit["status"], "merged")
            self.assertEqual(audit["chunk_runs"][0]["attempt"], 2)
            self.assertEqual(len(audit["chunk_runs"][0]["attempt_failures"]), 1)
            self.assertEqual(run.call_count, 2)
            first_command = run.call_args_list[0].args[0]
            second_command = run.call_args_list[1].args[0]
            first_key = first_command[first_command.index("--session-key") + 1]
            second_key = second_command[second_command.index("--session-key") + 1]
            self.assertNotEqual(first_key, second_key)
            self.assertIn("attempt-01", first_key)
            self.assertIn("attempt-02", second_key)

    def test_run_command_kills_and_reaps_the_process_group_on_timeout(self) -> None:
        process = Mock(pid=1234)
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(["openclaw"], 1),
            ("partial stdout", "partial stderr"),
        ]
        with patch.object(bridge.subprocess, "Popen", return_value=process) as popen:
            with patch.object(bridge.os, "killpg") as killpg:
                with self.assertRaises(subprocess.TimeoutExpired):
                    bridge._run_command(["openclaw"], timeout=1)
        popen.assert_called_once()
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        killpg.assert_called_once_with(1234, bridge.signal.SIGTERM)
        self.assertEqual(process.communicate.call_count, 2)

    def test_cancellation_after_raw_response_never_publishes_accepted_response(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, chunk = self._packet(Path(td) / "requirements")
            response = self._response(chunk)
            envelope = {
                "runId": "openclaw-run-cancel-race",
                "status": "ok",
                "result": {"payloads": [{"text": json.dumps(response)}]},
            }

            class CancelAfterRaw(bridge.RunController):
                def __init__(self) -> None:
                    super().__init__()
                    self.check_count = 0

                def check(self) -> None:
                    self.check_count += 1
                    if self.check_count >= 3:
                        raise bridge.HostAgentCancelled("fatal sibling failure")

            controller = CancelAfterRaw()
            response_path = review_dir / "llm-response-chunk-0001.attempt-01.json"
            with patch.object(
                bridge, "_run_command",
                return_value=subprocess.CompletedProcess(
                    ["openclaw"], 0, json.dumps(envelope), ""
                ),
            ):
                with self.assertRaises(bridge.HostAgentCancelled):
                    bridge.run_host_agent_chunk(
                        request_path=review_dir / "llm-request.json",
                        chunk_path=review_dir / "llm-request-chunks.json",
                        chunk=chunk,
                        response_path=response_path,
                        run_id="run-bridge-test",
                        chunk_index=1,
                        chunk_count=1,
                        agent_id="main",
                        timeout=1,
                        openclaw_bin="openclaw",
                        prompt_path=review_dir / "prompt.txt",
                        controller=controller,
                    )
            self.assertFalse(response_path.exists())
            self.assertTrue(response_path.with_name(
                f"{response_path.stem}.raw{response_path.suffix}"
            ).exists())

    def test_parse_openclaw_result_accepts_one_json_fence(self) -> None:
        envelope = {
            "status": "ok",
            "result": {"payloads": [{"text": "```json\n{\"ok\": true}\n```"}]},
        }
        response, _ = bridge.parse_openclaw_result(json.dumps(envelope))
        self.assertEqual(response, {"ok": True})

    def test_parse_openclaw_exec_result_reads_root_payloads_and_route(self) -> None:
        envelope = {
            "status": "ok",
            "provider": "openai",
            "model": "gpt-5.6-luna",
            "payloads": [{"text": "{\"ok\": true}"}],
        }
        response, parsed = bridge.parse_openclaw_result(json.dumps(envelope))
        self.assertEqual(response, {"ok": True})
        route = bridge.verify_host_agent_route(parsed, "openai/gpt-5.6-luna")
        self.assertEqual(route["route"], "openai/gpt-5.6-luna")


if __name__ == "__main__":
    unittest.main()
