from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from native_semantic_review import (  # noqa: E402
    NativeSemanticReviewError,
    validate_response,
)


class NativeSemanticReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.checks = [{
            "check_id": "abstract_zh.require_third_person",
            "document_text": "本文提出一种模型。",
        }, {
            "check_id": "abstract_en.required_sections",
            "document_text": "This study proposes a model.",
        }]

    def test_valid_exact_coverage_and_quotes_are_accepted(self) -> None:
        response = {"results": [
            {"check_id": "abstract_en.required_sections", "verdict": "uncertain",
             "rationale": "The source rule is unclear.", "evidence_quotes": ["This study"]},
            {"check_id": "abstract_zh.require_third_person", "verdict": "satisfied",
             "rationale": "Third person wording is present.", "evidence_quotes": ["本文提出"]},
        ]}
        results = validate_response(response, self.checks)
        self.assertEqual([item["check_id"] for item in results], sorted(
            item["check_id"] for item in self.checks
        ))

    def test_unknown_duplicate_missing_and_unquoted_evidence_are_rejected(self) -> None:
        valid = {
            "check_id": "abstract_zh.require_third_person", "verdict": "satisfied",
            "rationale": "Evidence supports this.", "evidence_quotes": ["本文提出"],
        }
        for response in (
            {"results": [valid, {**valid, "check_id": "unknown"}]},
            {"results": [valid, valid]},
            {"results": [valid]},
            {"results": [valid, {
                "check_id": "abstract_en.required_sections", "verdict": "satisfied",
                "rationale": "Not supported.", "evidence_quotes": ["not a quote"],
            }]},
        ):
            with self.subTest(response=response), self.assertRaises(NativeSemanticReviewError):
                validate_response(response, self.checks)

    def test_extra_fields_and_invalid_verdict_are_rejected_by_schema(self) -> None:
        response = {"results": [{
            "check_id": "abstract_zh.require_third_person", "verdict": "maybe",
            "rationale": "unclear", "evidence_quotes": ["本文提出"], "provenance": "forged",
        }]}
        with self.assertRaises(NativeSemanticReviewError):
            validate_response(response, self.checks)


if __name__ == "__main__":
    unittest.main()
