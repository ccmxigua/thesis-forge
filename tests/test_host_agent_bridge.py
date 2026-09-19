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
