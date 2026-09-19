from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "scripts"))

from host_adapters import codex  # noqa: E402


class CodexAdapterTests(unittest.TestCase):
    def test_build_command_uses_native_read_only_exec(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            prompt = root / "prompt.txt"
            output = root / "last-message.txt"
            prompt.write_text("Return one JSON object.", encoding="utf-8")
            command = codex.build_command(
                binary="/opt/homebrew/bin/codex",
                prompt_path=prompt,
                last_message_path=output,
                cwd=root,
                model="gpt-5.6-luna",
            )
        self.assertEqual(command[:2], ["/opt/homebrew/bin/codex", "exec"])
        self.assertIn("--ephemeral", command)
        self.assertIn("--ignore-user-config", command)
        self.assertEqual(command[command.index("--model") + 1], "gpt-5.6-luna")
        self.assertIn("--sandbox", command)
        self.assertIn("read-only", command)
        self.assertIn("--json", command)
        self.assertIn("--output-last-message", command)
        self.assertNotIn("openclaw", " ".join(command).lower())

    def test_build_command_without_model_preserves_native_cli_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            prompt = root / "prompt.txt"
            output = root / "last-message.txt"
            prompt.write_text("Return one JSON object.", encoding="utf-8")
            command = codex.build_command(
                binary="/opt/homebrew/bin/codex",
                prompt_path=prompt,
                last_message_path=output,
                cwd=root,
            )
        self.assertNotIn("--model", command)

    def test_build_command_binds_native_output_schema_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            prompt = root / "prompt.txt"
            output = root / "last-message.txt"
            schema = root / "response-schema.json"
            prompt.write_text("Return one JSON object.", encoding="utf-8")
            schema.write_text('{"type":"object"}', encoding="utf-8")
            command = codex.build_command(
                binary="/opt/homebrew/bin/codex",
                prompt_path=prompt,
                last_message_path=output,
                cwd=root,
                output_schema_path=schema,
            )
        self.assertEqual(command[command.index("--output-schema") + 1], str(schema.resolve()))
        self.assertLess(command.index("--output-schema"), command.index("-C"))

    def test_probe_capabilities_records_native_schema_support(self) -> None:
        version = subprocess.CompletedProcess(["codex", "--version"], 0, "codex 0.149.0\n", "")
        help_text = subprocess.CompletedProcess(
            ["codex", "exec", "--help"], 0,
            "Usage: codex exec [OPTIONS]\n--output-schema <FILE>\n", "",
        )
        with patch.object(codex.subprocess, "run", side_effect=[version, help_text]) as run:
            capabilities = codex.probe_capabilities(sys.executable)
        self.assertEqual(capabilities["version"], "codex 0.149.0")
        self.assertTrue(capabilities["output_schema_supported"])
        self.assertEqual(capabilities["structured_output_mode"], "native_schema")
        self.assertEqual(run.call_count, 2)

    def test_parse_result_requires_completed_turn_and_uses_final_message(self) -> None:
        response = {"contract_version": "2.1", "provenance": {}}
        stdout = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            json.dumps({
                "type": "item.completed",
                "item": {"type": "error", "message": "non-fatal config warning"},
            }),
            json.dumps({
                "type": "item.completed",
                "item": {"type": "agent_message", "text": json.dumps(response)},
            }),
            json.dumps({"type": "turn.completed", "usage": {"output_tokens": 1}}),
        ])
        parsed, audit = codex.parse_result(
            stdout, last_message=json.dumps(response),
        )
        self.assertEqual(parsed, response)
        self.assertTrue(audit["turn_completed"])
        self.assertEqual(audit["final_message_source"], "output-last-message")
        self.assertEqual(audit["stream_warnings"], ["non-fatal config warning"])

    def test_parse_result_rejects_truncated_or_unfinished_stream(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid JSONL"):
            codex.parse_result('{"type":"thread.started"}\n{broken')
        with self.assertRaisesRegex(ValueError, "did not complete"):
            codex.parse_result(json.dumps({"type": "thread.started"}))

    def test_parse_result_rejects_completed_turn_without_message(self) -> None:
        stdout = json.dumps({"type": "turn.completed"})
        with self.assertRaisesRegex(ValueError, "without a final assistant message"):
            codex.parse_result(stdout)

    def test_parse_result_rejects_terminal_message_mismatch(self) -> None:
        stdout = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            json.dumps({
                "type": "item.completed",
                "item": {"type": "agent_message", "text": '{"value": 1}'},
            }),
            json.dumps({"type": "turn.completed"}),
        ])
        with self.assertRaisesRegex(ValueError, "does not match"):
            codex.parse_result(stdout, last_message='{"value": 2}')

    def test_parse_result_rejects_duplicate_keys_in_final_json(self) -> None:
        stdout = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            json.dumps({
                "type": "item.completed",
                "item": {"type": "agent_message", "text": '{"value": 1, "value": 2}'},
            }),
            json.dumps({"type": "turn.completed"}),
        ])
        with self.assertRaisesRegex(ValueError, "not valid JSON"):
            codex.parse_result(stdout)

    def test_parse_result_rejects_failed_turn_even_after_completed_event(self) -> None:
        stdout = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            json.dumps({"type": "turn.completed"}),
            json.dumps({"type": "turn.failed", "message": "late failure"}),
        ])
        with self.assertRaisesRegex(ValueError, "turn failed"):
            codex.parse_result(stdout, last_message='{"value": 1}')


if __name__ == "__main__":
    unittest.main()
