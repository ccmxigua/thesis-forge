from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

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
            )
        self.assertEqual(command[:2], ["/opt/homebrew/bin/codex", "exec"])
        self.assertIn("--ephemeral", command)
        self.assertIn("--ignore-user-config", command)
        self.assertEqual(command[command.index("--model") + 1], codex.DEFAULT_MODEL)
        self.assertIn("--sandbox", command)
        self.assertIn("read-only", command)
        self.assertIn("--json", command)
        self.assertIn("--output-last-message", command)
        self.assertNotIn("openclaw", " ".join(command).lower())

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
