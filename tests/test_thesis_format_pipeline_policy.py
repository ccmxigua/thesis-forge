from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from thesis_format_pipeline import (  # noqa: E402
    _source_content_pending_release_gates,
    enforce_obligation_review_output_policy,
)


class ThesisFormatPipelinePolicyTests(unittest.TestCase):
    def test_irreducible_ambiguity_is_allowed_only_in_review_draft(self) -> None:
        results = [{"check_id": "C00076", "verdict": "manual_review_required"}]

        self.assertEqual(
            enforce_obligation_review_output_policy(results, output_policy="review_draft"),
            ["C00076"],
        )
        with self.assertRaisesRegex(ValueError, "C00076.*submission output is blocked"):
            enforce_obligation_review_output_policy(results, output_policy="submission")

    def test_no_manual_review_result_preserves_both_output_policies(self) -> None:
        self.assertEqual(
            enforce_obligation_review_output_policy(
                [{"check_id": "C00066", "verdict": "consistent"}],
                output_policy="submission",
            ),
            [],
        )

    def test_genuine_author_content_pending_is_draft_only(self) -> None:
        results = [{"check_id": "C00102", "verdict": "source_content_pending"}]
        self.assertEqual(
            enforce_obligation_review_output_policy(results, output_policy="review_draft"),
            [],
        )
        with self.assertRaisesRegex(ValueError, "C00102.*submission output is blocked"):
            enforce_obligation_review_output_policy(results, output_policy="submission")

    def test_source_content_pending_generates_an_evidence_bound_author_input_gate(self) -> None:
        gates = _source_content_pending_release_gates([{
            "source_content_pending_items": [{
                "clause_id": "C00102",
                "source_quotes": ["这些内容是示例，请作者自行撰写真实内容。"],
                "evidence_ids": ["E00009"],
                "reason": "原文明确要求作者补写真实研究内容。",
            }],
        }])
        self.assertEqual(len(gates), 1)
        self.assertEqual(gates[0]["category"], "input_prerequisite")
        self.assertEqual(gates[0]["clause_ids"], ["C00102"])
        self.assertEqual(gates[0]["evidence_ids"], ["E00009"])
        self.assertIn("本人真实研究内容", gates[0]["action"])
        self.assertIn("【待补写真实论文内容：C00102】", gates[0]["placeholder_text"])
        self.assertNotIn("generated content", gates[0]["action"])

    def test_source_content_pending_rejects_missing_evidence_ids(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires non-empty evidence IDs"):
            _source_content_pending_release_gates([{
                "source_content_pending_items": [{
                    "clause_id": "C00102",
                    "source_quotes": ["这些示例内容请作者自行撰写真实内容。"],
                    "evidence_ids": [],
                    "reason": "待作者提供真实内容。",
                }],
            }])

    def test_source_content_pending_rejects_malformed_review_container(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be an array"):
            _source_content_pending_release_gates({})  # type: ignore[arg-type]

    def test_unknown_output_policy_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported output policy"):
            enforce_obligation_review_output_policy([], output_policy="preview")


if __name__ == "__main__":
    unittest.main()
