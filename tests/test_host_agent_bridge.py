from __future__ import annotations

import json
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

    def test_bridge_retries_locally_rejected_contract_in_a_new_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, chunk = self._packet(Path(td) / "requirements")
            invalid = self._executable_response(chunk, invalid_verification=True)
            valid = self._executable_response(chunk)
            envelopes = [
                {"runId": "openclaw-run-contract-retry-1", "status": "ok",
                 "result": {"payloads": [{"text": json.dumps(invalid)}]}},
                {"runId": "openclaw-run-contract-retry-2", "status": "ok",
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
                    openclaw_bin="openclaw",
                )
            self.assertEqual(audit["status"], "merged")
            self.assertEqual(audit["chunk_runs"][0]["attempt"], 2)
            self.assertIn("local response contract validation failed",
                          audit["chunk_runs"][0]["attempt_failures"][0])
            second_prompt = Path(
                run.call_args_list[1].args[0][run.call_args_list[1].args[0].index("--message-file") + 1]
            ).read_text(encoding="utf-8")
            self.assertIn("local contract validation failed", second_prompt)

    def test_bridge_owns_transport_provenance_when_model_echo_is_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, chunk = self._packet(Path(td) / "requirements")
            response = self._response(chunk)
            response["provenance"] = dict(chunk["provenance"])
            response["provenance"]["request_sha256"] = response["provenance"]["request_sha256"][:48]
            envelope = {
                "runId": "openclaw-run-provenance-copy",
                "status": "ok",
                "result": {"payloads": [{"text": json.dumps(response)}]},
            }
            response_out = Path(td) / "host-agent-response.json"
            fake = subprocess.CompletedProcess(
                ["openclaw"], 0, json.dumps(envelope), "",
            )
            with patch.object(bridge, "_run_command", return_value=fake):
                audit = bridge.run_bridge(
                    review_dir, response_out=response_out,
                    agent_id="main", timeout=1, openclaw_bin="openclaw",
                )
            merged = json.loads(response_out.read_text(encoding="utf-8"))
            full_request = json.loads(
                (review_dir / "llm-request.json").read_text(encoding="utf-8")
            )
            self.assertEqual(merged["provenance"], full_request["provenance"])
            warning = audit["chunk_runs"][0]["provenance_copy_warning"]
            self.assertIn("request_sha256", warning["mismatch_fields"])

    def test_failed_bridge_persists_failure_audit_without_merged_response(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, chunk = self._packet(Path(td) / "requirements")
            response = self._response(chunk)
            response["clause_reviews"][0]["requirement_indexes"] = [0]
            envelope = {
                "runId": "openclaw-run-final-contract-failure",
                "status": "ok",
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
                        openclaw_bin="openclaw",
                    )
            failure = json.loads((review_dir / "host-agent-run.json").read_text(encoding="utf-8"))
            self.assertEqual(failure["status"], "failed")
            self.assertFalse(failure["merged_response_written"])
            self.assertFalse(response_out.exists())

    def test_bridge_calls_openclaw_without_delivery_and_merges_fresh_response(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, chunk = self._packet(Path(td) / "requirements")
            response = self._response(chunk)
            envelope = {
                "runId": "openclaw-run-test",
                "status": "ok",
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
                )
            self.assertEqual(audit["status"], "merged")
            self.assertEqual(audit["chunk_count"], 1)
            self.assertEqual(json.loads(response_out.read_text(encoding="utf-8"))["contract_version"], "2.1")
            command = run.call_args.args[0]
            self.assertIn("--session-key", command)
            self.assertIn("--message-file", command)
            self.assertNotIn("--deliver", command)
            self.assertTrue((review_dir / "host-agent-run.json").is_file())

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
        with patch.object(bridge.subprocess, "run", return_value=fake) as run:
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

            with patch.object(bridge.subprocess, "run", return_value=session_result):
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
        with patch.object(bridge.subprocess, "run", return_value=fake):
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
        with patch.object(bridge.subprocess, "run", return_value=fake):
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

            with patch.object(bridge.subprocess, "run", return_value=session_result):
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
            self.assertEqual(len(calls), 1)
            self.assertFalse((Path(td) / "host-agent-response.json").exists())

    def test_bridge_refuses_existing_chunk_response_instead_of_reusing_it(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, chunk = self._packet(Path(td) / "requirements")
            response_name = json.loads(
                (review_dir / "host-agent-review-manifest.json").read_text(encoding="utf-8")
            )["response_files"][0]
            (review_dir / response_name).write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "refusing to reuse an existing chunk response"):
                bridge.run_bridge(review_dir, response_out=Path(td) / "response.json", timeout=1)

    def test_bridge_retries_rejected_json_in_a_new_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            review_dir, chunk = self._packet(Path(td) / "requirements")
            response = self._response(chunk)
            envelope = {
                "runId": "openclaw-run-retry-test",
                "status": "ok",
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
                    openclaw_bin="openclaw",
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
