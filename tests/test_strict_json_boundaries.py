from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.validate_artifact import _load_expected_manifest
from scripts.semantic_contract import strict_json_dumps


class StrictJsonBoundaryTests(unittest.TestCase):
    def test_expected_manifest_rejects_duplicate_keys_and_overflow(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "expected.json"
            for payload in (
                '{"schema_version":"1.0","schema_version":"0.9"}',
                '{"schema_version":"1.0","value":1e999}',
            ):
                with self.subTest(payload=payload):
                    path.write_text(payload, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        _load_expected_manifest(path)

    def test_runtime_json_writers_reject_nonfinite_values_before_publishing(self) -> None:
        from scripts.host_agent_bridge import _write_json as write_bridge_json
        from scripts.capability_planner import write_json as write_capability_json
        from scripts.manual_review import write_manual_review_ledger
        from scripts.post_render_acceptance import _write as write_post_render_json
        from scripts.profile_copilot import _write as write_profile_json
        from scripts.requirements_engine import write_json as write_requirements_json
        from scripts.review_unresolved_llm import dump as write_unresolved_json
        from scripts.run_golden_e2e import write_json as write_golden_json
        from scripts.thesis_format_pipeline import write_json as write_pipeline_json

        with self.assertRaisesRegex(ValueError, "Out of range float"):
            strict_json_dumps({"value": float("nan")})
        writers = (
            write_bridge_json, write_manual_review_ledger,
            write_requirements_json, write_pipeline_json, write_capability_json,
            write_post_render_json, write_profile_json, write_unresolved_json,
            write_golden_json,
        )
        with tempfile.TemporaryDirectory() as td:
            for index, writer in enumerate(writers):
                with self.subTest(writer=writer.__module__):
                    path = Path(td) / f"runtime-{index}.json"
                    with self.assertRaisesRegex(ValueError, "Out of range float"):
                        writer(path, {"value": float("inf")})
                    self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
