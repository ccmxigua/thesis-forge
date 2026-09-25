from __future__ import annotations

import copy
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from native_semantic_review import OBLIGATION_COVERAGE_SCHEMA, RESPONSE_SCHEMA
from semantic_source_references import (
    build_source_reference_packet,
    compile_source_reference_response,
    source_reference_schema,
)


class SemanticSourceReferenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request = {
            "protocol": "test",
            "run_id": "run-1",
            "case_id": "case-1",
            "checks": [
                {"check_id": "C1", "document_text": "甲 年 月。第二句！",
                 "review_context": {"cited_evidence": {"E1": {"text": "甲 年  月。"}}}},
                {"check_id": "C2", "document_text": "乙文有独立职责。"},
            ],
        }

    def _simple_response(self, first_ref: str, second_ref: str) -> dict:
        return {"results": [
            {"check_id": "C1", "verdict": "uncertain", "rationale": "依据原文判断。",
             "evidence_refs": [first_ref]},
            {"check_id": "C2", "verdict": "satisfied", "rationale": "依据原文判断。",
             "evidence_refs": [second_ref]},
        ]}

    def test_code_owned_spans_preserve_whitespace_and_compile_without_retyping(self) -> None:
        packet = build_source_reference_packet(self.request)
        first_spans = packet["checks"][0]["source_spans"]
        sentence = next(span for span in first_spans if "年 月" in span["text"])
        second = packet["checks"][1]["source_spans"][0]
        raw = self._simple_response(sentence["ref_id"], second["ref_id"])
        raw_before = copy.deepcopy(raw)
        request_before = copy.deepcopy(self.request)

        compiled, audit = compile_source_reference_response(
            raw, self.request, RESPONSE_SCHEMA, coverage=False,
        )

        self.assertEqual(compiled["results"][0]["evidence_quotes"], [sentence["text"]])
        self.assertIn("年 月", compiled["results"][0]["evidence_quotes"][0])
        self.assertNotIn("年  月", compiled["results"][0]["evidence_quotes"][0])
        self.assertEqual(raw, raw_before)
        self.assertEqual(self.request, request_before)
        self.assertTrue(audit["semantic_verdicts_unchanged"])
        self.assertEqual(audit["run_id"], "run-1")
        self.assertEqual(audit["selections"][0]["spans"][0]["source_sha256"], sentence["source_sha256"])

    def test_references_cannot_cross_checks_or_survive_a_changed_request(self) -> None:
        packet = build_source_reference_packet(self.request)
        first = packet["checks"][0]["source_spans"][0]["ref_id"]
        second = packet["checks"][1]["source_spans"][0]["ref_id"]
        cross_check = self._simple_response(second, first)
        with self.assertRaisesRegex(ValueError, "source reference response rejected"):
            compile_source_reference_response(
                cross_check, self.request, RESPONSE_SCHEMA, coverage=False,
            )

        changed = copy.deepcopy(self.request)
        changed["checks"][0]["document_text"] += "新增"
        with self.assertRaisesRegex(ValueError, "source reference response rejected"):
            compile_source_reference_response(
                self._simple_response(first, second), changed, RESPONSE_SCHEMA, coverage=False,
            )

    def test_unknown_reference_duplicate_check_and_missing_check_are_rejected(self) -> None:
        packet = build_source_reference_packet(self.request)
        first = packet["checks"][0]["source_spans"][0]["ref_id"]
        second = packet["checks"][1]["source_spans"][0]["ref_id"]
        valid = self._simple_response(first, second)
        invalid_responses = [
            self._simple_response("Q" + "0" * 16, second),
            {"results": [valid["results"][0], valid["results"][0]]},
            {"results": [valid["results"][0]]},
        ]
        for response in invalid_responses:
            with self.subTest(response=response), self.assertRaises(ValueError):
                compile_source_reference_response(
                    response, self.request, RESPONSE_SCHEMA, coverage=False,
                )

    def test_coverage_machine_inventory_is_injected_by_code(self) -> None:
        request = {"protocol": "coverage", "run_id": "run-2", "checks": [{
            "check_id": "C1",
            "document_text": "签字后提交。",
            "review_context": {"machine_obligation_ids": ["source_fact_1"]},
        }]}
        packet = build_source_reference_packet(request)
        ref = packet["checks"][0]["source_spans"][0]["ref_id"]
        raw = {"results": [{
            "check_id": "C1", "verdict": "uncertain", "rationale": "职责范围需确认。",
            "evidence_refs": [ref], "identified_obligations": [{
                "source_ref": ref, "disposition": "ambiguous", "requirement_refs": [],
            }, {
                "source_ref": ref, "disposition": "ambiguous", "requirement_refs": [],
            }],
        }]}

        wire_schema = source_reference_schema(
            OBLIGATION_COVERAGE_SCHEMA, packet, coverage=True,
        )
        self.assertEqual(raw["results"][0].get("machine_obligation_ids"), None)
        compiled, audit = compile_source_reference_response(
            raw, request, OBLIGATION_COVERAGE_SCHEMA, coverage=True,
        )
        result = compiled["results"][0]
        self.assertEqual(result["machine_obligation_ids"], ["source_fact_1"])
        self.assertEqual(result["evidence_quotes"], ["签字后提交。"])
        self.assertEqual(result["identified_obligations"][0]["source_quote"], "签字后提交。")
        self.assertEqual(result["identified_obligations"][0]["disposition"], "ambiguous")
        self.assertEqual(len(audit["selections"][0]["obligations"]), 2)
        self.assertEqual(
            [item["obligation_index"] for item in audit["selections"][0]["obligations"]],
            [0, 1],
        )
        self.assertEqual(
            audit["selections"][0]["obligations"][0]["source_ref"],
            audit["selections"][0]["obligations"][1]["source_ref"],
        )
        self.assertNotIn("machine_obligation_ids", wire_schema["properties"]["results"]["items"]["anyOf"][0]["properties"])

    def test_english_source_spans_preserve_offsets_and_do_not_split_decimals_or_abbreviations(self) -> None:
        text = (
            "The Chinese abstract is 300 to 1,000 words. It cites e.g. prior work, "
            "including value 1.5, and usually ends here!\nA new paragraph follows."
        )
        request = {"run_id": "r-en", "checks": [{"check_id": "C-en", "document_text": text}]}
        spans = build_source_reference_packet(request)["checks"][0]["source_spans"]
        sentence_spans = [span for span in spans if (span["start"], span["end"]) != (0, len(text))]
        self.assertTrue(sentence_spans)
        self.assertTrue(all(text[span["start"]:span["end"]] == span["text"] for span in spans))
        rendered = [span["text"] for span in sentence_spans]
        self.assertTrue(any("300 to 1,000 words." in span for span in rendered))
        self.assertTrue(any("e.g. prior work" in span and "1.5" in span for span in rendered))
        self.assertTrue(any(span.endswith("usually ends here!") for span in rendered))
        self.assertTrue(any("A new paragraph follows." in span for span in rendered))
        self.assertFalse(any(span.rstrip().endswith("e.g.") for span in rendered))
        self.assertFalse(any(span.rstrip().endswith("1.") for span in rendered))

    def test_multipart_english_abbreviations_remain_inside_their_source_sentence(self) -> None:
        text = "Use U.S. standards. Use Ph.D. data. Use M.Sc. results. Check No. 3. Next sentence."
        request = {"run_id": "r-abbr", "checks": [{"check_id": "C-abbr", "document_text": text}]}
        spans = build_source_reference_packet(request)["checks"][0]["source_spans"]
        sentence_spans = [span for span in spans if (span["start"], span["end"]) != (0, len(text))]
        self.assertTrue(all(text[span["start"]:span["end"]] == span["text"] for span in spans))
        rendered = [span["text"] for span in sentence_spans]
        self.assertTrue(any(span.lstrip().startswith("Use U.S. standards.") for span in rendered))
        self.assertTrue(any(span.lstrip().startswith("Use Ph.D. data.") for span in rendered))
        self.assertTrue(any(span.lstrip().startswith("Use M.Sc. results.") for span in rendered))
        self.assertTrue(any(span.lstrip().startswith("Check No. 3.") for span in rendered))
        self.assertTrue(any(span.lstrip().startswith("Next sentence.") for span in rendered))

    def test_empty_or_malformed_source_check_fails_before_model_invocation(self) -> None:
        for checks in ([], [{"check_id": "C1", "document_text": "  "}], [None]):
            with self.subTest(checks=checks), self.assertRaises(ValueError):
                build_source_reference_packet({"checks": checks})


if __name__ == "__main__":
    unittest.main()
