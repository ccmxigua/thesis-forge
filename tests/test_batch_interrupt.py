from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from batch_rerun_ten_schools import (  # noqa: E402
    _read_artifact_json,
    failed_case_result,
    interrupted_case_result,
)


class BatchInterruptTests(unittest.TestCase):
    def test_interrupted_case_is_not_reported_as_completed_or_failed_normally(self) -> None:
        result = interrupted_case_result({"id": "BSU", "analysis_mode": "llm_primary"})
        self.assertEqual(result["returncode"], 130)
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["current_stage_state"], "unknown")
        self.assertFalse(result["acceptance"]["accepted"])
        self.assertEqual(result["acceptance"]["status"], "interrupted")

    def test_unexpected_failure_does_not_invent_completed_stages(self) -> None:
        result = failed_case_result({"id": "BSU", "analysis_mode": "llm_primary"}, RuntimeError("boom"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["current_stage_state"], "unknown")
        self.assertEqual(result["stages"], {})

    def test_strict_json_artifact_failures_become_unreadable_not_uncaught(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "artifact.json"
            for payload in ('{"x":1,"x":2}', '{"x":1e999}'):
                with self.subTest(payload=payload):
                    path.write_text(payload, encoding="utf-8")
                    resolved, value = _read_artifact_json(str(path), root=root)
                    self.assertEqual(resolved, path.resolve())
                    self.assertIsNone(value)


if __name__ == "__main__":
    unittest.main()
