from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from thesis_format_pipeline import (  # noqa: E402
    _scope_unresolved_release_gates,
    _source_content_pending_release_gates,
    _source_content_verification_release_gates,
    enforce_obligation_review_output_policy,
)
from obligation_workflow import OBLIGATION_ANALYSIS_LEDGER_PROTOCOL  # noqa: E402
from format_spec_validation import load_and_validate  # noqa: E402
from manual_review import build_manual_review_ledger  # noqa: E402
from semantic_contract import sha256_json  # noqa: E402


class ThesisFormatPipelinePolicyTests(unittest.TestCase):
    @staticmethod
    def _pending_bundle(
        *, clause_id: str, text: str, start: int, end: int, obligation_index: int = 0,
        raw_prefix: str = "前文：", raw_suffix: str = "。后文", run_id: str = "run-1",
        evidence_id: str = "E00009",
    ) -> tuple[dict, dict, dict, dict]:
        raw_source = raw_prefix + text + raw_suffix
        span_start = len(raw_prefix)
        source_sha = hashlib.sha256(raw_source.encode("utf-8")).hexdigest()
        source_text_sha = sha256_json(text)
        review_request_sha = "b" * 64
        case_id = "bsu"
        source_ref = "Q" + sha256_json({
            "request_sha256": review_request_sha,
            "check_id": clause_id,
            "start": start,
            "end": end,
            "text": text[start:end],
        })[:16]
        identity = {
            "protocol": OBLIGATION_ANALYSIS_LEDGER_PROTOCOL,
            "run_id": run_id,
            "case_id": case_id,
            "chunk_index": 1,
            "attempt": 1,
            "candidate_response_sha256": "a" * 64,
            "review_request_sha256": review_request_sha,
            "review_response_sha256": "c" * 64,
            "check_id": clause_id,
            "obligation_index": obligation_index,
            "source_ref": source_ref,
            "source_sha256": source_text_sha,
            "start": start,
            "end": end,
        }
        obligation_id = "AO-" + sha256_json(identity)[:24]
        item = {
            "analysis_obligation_id": obligation_id,
            "analysis_obligation_identity": identity,
            "work_type": "authoring_content",
            "clause_id": clause_id,
            "source_ref": source_ref,
            "source_quote": text[start:end],
            "source_start": start,
            "source_end": end,
            "source_text_sha256": source_text_sha,
            "obligation_summary": f"作者需补充：{text[start:end]}",
            "evidence_ids": [evidence_id],
            "requirement_refs": [],
            "execution_authorized": False,
            "source_location": {
                "evidence_id": evidence_id,
                "start_offset": span_start + start,
                "end_offset": span_start + end,
                "source_sha256": source_sha,
            },
        }
        review = {
            "run_id": run_id,
            "case_id": case_id,
            "chunk_index": 1,
            "attempt": 1,
            "candidate_response_sha256": "a" * 64,
            "review_request_sha256": review_request_sha,
            "review_response_sha256": "c" * 64,
            "source_content_pending_items": [item],
        }
        clause = {
            "id": clause_id, "text": text, "evidence_ids": [evidence_id],
            "source_span": {
                "evidence_id": evidence_id,
                "start_offset": span_start,
                "end_offset": span_start + len(text),
                "text": text,
                "source_sha256": source_sha,
            },
        }
        evidence_doc = {"evidence": [{"id": evidence_id, "text": raw_source}]}
        return review, clause, evidence_doc, item

    @staticmethod
    def _canonical_scope_source() -> tuple[list[dict], dict, dict]:
        quote = "at least 3 groups"
        raw_source = f"prefix {quote} suffix"
        start = len("prefix ")
        source_sha256 = hashlib.sha256(raw_source.encode("utf-8")).hexdigest()
        clause = {
            "id": "C1", "text": quote, "evidence_ids": ["E1"],
            "source_span": {
                "evidence_id": "E1", "start_offset": start,
                "end_offset": start + len(quote), "text": quote,
                "source_sha256": source_sha256,
            },
        }
        location = {
            "evidence_id": "E1", "start_offset": start,
            "end_offset": start + len(quote), "source_sha256": source_sha256,
        }
        item_base = {
            "clause_id": "C1", "source_quote": quote,
            "source_start": 0, "source_end": len(quote),
            "source_text_sha256": sha256_json(quote),
            "source_location": location,
            "evidence_ids": ["E1"], "execution_authorized": False,
        }
        return [clause], {"evidence": [{"id": "E1", "text": raw_source}]}, item_base

    @staticmethod
    def _scope_identity(item_base: dict, *, source_ref: str, obligation_index: int = 0):
        identity = {
            "protocol": OBLIGATION_ANALYSIS_LEDGER_PROTOCOL,
            "run_id": "run-1", "case_id": "bsu", "chunk_index": 1, "attempt": 1,
            "candidate_response_sha256": "a" * 64,
            "review_request_sha256": "b" * 64,
            "review_response_sha256": "c" * 64,
            "check_id": "C1", "obligation_index": obligation_index,
            "source_ref": source_ref,
            "source_sha256": item_base["source_text_sha256"],
            "start": item_base["source_start"], "end": item_base["source_end"],
        }
        return "AO-" + sha256_json(identity)[:24], identity

    @staticmethod
    def _scope_review_context():
        return {
            "run_id": "run-1", "case_id": "bsu", "chunk_index": 1, "attempt": 1,
            "candidate_response_sha256": "a" * 64,
            "review_request_sha256": "b" * 64,
            "review_response_sha256": "c" * 64,
        }

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
        quote = "这些内容是示例，请作者自行撰写真实内容。"
        review, clause, evidence_doc, item = self._pending_bundle(
            clause_id="C00102", text=quote, start=0, end=len(quote),
        )
        gates = _source_content_pending_release_gates(
            [review], clauses=[clause], evidence_doc=evidence_doc, expected_run_id="run-1",
        )
        self.assertEqual(len(gates), 1)
        self.assertEqual(gates[0]["category"], "input_prerequisite")
        self.assertEqual(gates[0]["clause_ids"], ["C00102"])
        self.assertEqual(gates[0]["evidence_ids"], ["E00009"])
        self.assertEqual(gates[0]["source_location"], item["source_location"])
        self.assertEqual(gates[0]["analysis_obligation_id"], item["analysis_obligation_id"])
        self.assertIn("本人真实研究内容", gates[0]["action"])
        self.assertIn(item["analysis_obligation_id"], gates[0]["placeholder_text"])
        self.assertNotIn("generated content", gates[0]["action"])
        with self.assertRaisesRegex(ValueError, "current-source-bound"):
            _source_content_pending_release_gates(
                [{**review, "case_id": None}], clauses=[clause],
                evidence_doc=evidence_doc, expected_run_id="run-1",
            )

    def test_source_content_pending_keeps_multiple_atomic_obligations_for_one_clause(self) -> None:
        text = "请作者补写研究背景；请作者补写研究方法。"
        review, clause, evidence_doc, first = self._pending_bundle(
            clause_id="C00102", text=text, start=0, end=10, obligation_index=0,
        )
        _, _, _, second = self._pending_bundle(
            clause_id="C00102", text=text, start=11, end=len(text), obligation_index=1,
        )
        review["source_content_pending_items"].append(second)
        gates = _source_content_pending_release_gates(
            [review], clauses=[clause], evidence_doc=evidence_doc, expected_run_id="run-1",
        )
        self.assertEqual(len(gates), 2)
        self.assertEqual({gate["clause_ids"][0] for gate in gates}, {"C00102"})
        self.assertEqual(
            {gate["analysis_obligation_id"] for gate in gates},
            {first["analysis_obligation_id"], second["analysis_obligation_id"]},
        )

    def test_existing_content_verification_creates_human_marker_not_authoring_todo(self) -> None:
        quote = (
            "关键词是为了便于做文献索引和检索工作而从论文中选取出来用以表示全文主题内容信息的单词或术语，"
            "在论文中有明确出处。"
        )
        raw_source = f"前文：{quote}后文"
        start = raw_source.index(quote)
        source_sha = hashlib.sha256(raw_source.encode("utf-8")).hexdigest()
        clause = {
            "id": "C00068", "text": quote, "evidence_ids": ["E00068"],
            "source_span": {
                "evidence_id": "E00068", "start_offset": start,
                "end_offset": start + len(quote), "text": quote,
                "source_sha256": source_sha,
            },
        }
        evidence_doc = {"evidence": [{"id": "E00068", "text": raw_source}]}
        review_request_sha256 = "b" * 64
        review_case_id = "bsu"
        source_ref = "Q" + sha256_json({
            "request_sha256": review_request_sha256,
            "check_id": "C00068",
            "start": 0,
            "end": len(quote),
            "text": quote,
        })[:16]
        identity = {
            "protocol": OBLIGATION_ANALYSIS_LEDGER_PROTOCOL,
            "run_id": "run-1",
            "case_id": review_case_id,
            "chunk_index": 1,
            "attempt": 1,
            "candidate_response_sha256": "a" * 64,
            "review_request_sha256": review_request_sha256,
            "review_response_sha256": "c" * 64,
            "check_id": "C00068",
            "obligation_index": 0,
            "source_ref": source_ref,
            "source_sha256": sha256_json(quote),
            "start": 0,
            "end": len(quote),
        }
        obligation_id = "AO-" + sha256_json(identity)[:24]
        reviews = [{"source_content_verification_items": [{
            "analysis_obligation_id": obligation_id,
            "analysis_obligation_identity": identity,
            "work_type": "existing_content_verification",
            "clause_id": "C00068", "source_ref": source_ref,
            "source_quote": quote, "source_start": 0, "source_end": len(quote),
            "source_text_sha256": sha256_json(quote),
            "obligation_summary": "请人工核对该现有内容是否能在论文正文找到明确出处。",
            "evidence_ids": ["E00068"],
            "requirement_refs": [],
            "execution_authorized": False,
            "source_location": {
                "evidence_id": "E00068", "start_offset": start,
                "end_offset": start + len(quote), "source_sha256": source_sha,
            },
        }], "run_id": "run-1", "case_id": review_case_id, "chunk_index": 1,
            "attempt": 1, "candidate_response_sha256": "a" * 64,
            "review_request_sha256": review_request_sha256,
            "review_response_sha256": "c" * 64}]
        gates = _source_content_verification_release_gates(
            reviews, clauses=[clause], evidence_doc=evidence_doc,
            expected_run_id="run-1",
        )
        self.assertEqual(len(gates), 1)
        with self.assertRaisesRegex(ValueError, "current review run"):
            _source_content_verification_release_gates(
                [{**reviews[0], "case_id": None}], clauses=[clause],
                evidence_doc=evidence_doc, expected_run_id="run-1",
            )
        gate = gates[0]
        self.assertEqual(gate["category"], "semantic_content_review")
        self.assertEqual(gate["clause_ids"], ["C00068"])
        self.assertEqual(gate["evidence_ids"], ["E00068"])
        self.assertEqual(
            gate["placeholder_text"],
            f"【待人工核验：{obligation_id}｜请人工核对该现有内容是否能在论文正文找到明确出处。】",
        )
        self.assertIn("核验", gate["action"])
        self.assertIn("仍未通过", gate["action"])
        self.assertNotIn("补写", gate["action"])
        self.assertNotIn("替换", gate["action"])

        second_identity = {**identity, "obligation_index": 1}
        second_obligation_id = "AO-" + sha256_json(second_identity)[:24]
        first_item = reviews[0]["source_content_verification_items"][0]
        second_item = {
            **first_item,
            "analysis_obligation_id": second_obligation_id,
            "analysis_obligation_identity": second_identity,
            "obligation_summary": "另一项独立义务也需人工核对现有正文出处。",
        }
        multiple_gates = _source_content_verification_release_gates(
            [{**reviews[0], "source_content_verification_items": [first_item, second_item]}],
            clauses=[clause], evidence_doc=evidence_doc, expected_run_id="run-1",
        )
        self.assertEqual(len(multiple_gates), 2)
        self.assertEqual({tuple(item["clause_ids"]) for item in multiple_gates}, {("C00068",)})
        self.assertEqual(
            {item["analysis_obligation_id"] for item in multiple_gates},
            {obligation_id, second_obligation_id},
        )

        stale_run = [{**reviews[0], "source_content_verification_items": [
            reviews[0]["source_content_verification_items"][0],
        ]}]
        with self.assertRaisesRegex(ValueError, "identity is not bound to the current review run"):
            _source_content_verification_release_gates(
                stale_run, clauses=[clause], evidence_doc=evidence_doc,
                expected_run_id="different-run",
            )

        forged_identity = {**identity, "source_ref": "Q" + "0" * 16}
        forged_source_ref = "Q" + "0" * 16
        forged_source_item = {
            **reviews[0]["source_content_verification_items"][0],
            "analysis_obligation_identity": forged_identity,
            "analysis_obligation_id": "AO-" + sha256_json(forged_identity)[:24],
            "source_ref": forged_source_ref,
        }
        with self.assertRaisesRegex(ValueError, "source_ref is not canonical"):
            _source_content_verification_release_gates(
                [{**reviews[0], "source_content_verification_items": [forged_source_item]}],
                clauses=[clause], evidence_doc=evidence_doc, expected_run_id="run-1",
            )

        ledger = build_manual_review_ledger(
            {}, [], release_gates=gates,
            binding={
                "case_id": "bsu", "run_id": "run-1",
                "source_sha256": "a" * 64, "clause_sha256": "b" * 64,
                "evidence_sha256": "c" * 64, "request_sha256": "d" * 64,
                "requirements_sha256": "e" * 64, "input_source_sha256": "f" * 64,
                "format_spec_sha256": "0" * 64, "official_template_sha256": None,
                "official_template_source": "not_supplied",
            },
        )
        self.assertFalse(ledger["submission_ready"])
        self.assertEqual(ledger["items"][0]["category"], "semantic_content_review")
        self.assertEqual(load_and_validate(ledger, ROOT / "schema" / "manual-review-ledger.schema.json"), [])

        forged = [{"source_content_verification_items": [{
            "clause_id": "C00068", "source_quotes": ["表格应居中"],
            "evidence_ids": ["E00068"],
        }]}]
        with self.assertRaisesRegex(ValueError, "malformed"):
            _source_content_verification_release_gates(
                forged, clauses=[clause], evidence_doc=evidence_doc, expected_run_id="run-1",
            )
        duplicate = [{**reviews[0], "source_content_verification_items": [
            *reviews[0]["source_content_verification_items"],
            *reviews[0]["source_content_verification_items"],
        ]}]
        with self.assertRaisesRegex(ValueError, "duplicate source-content verification obligation"):
            _source_content_verification_release_gates(
                duplicate, clauses=[clause], evidence_doc=evidence_doc, expected_run_id="run-1",
            )

    def test_keyword_provenance_human_verification_is_review_draft_only(self) -> None:
        results = [{"check_id": "C00068", "verdict": "source_content_verification_pending"}]
        self.assertEqual(
            enforce_obligation_review_output_policy(results, output_policy="review_draft"), [],
        )
        with self.assertRaisesRegex(ValueError, "human verification.*C00068.*submission output is blocked"):
            enforce_obligation_review_output_policy(results, output_policy="submission")

    def test_source_content_pending_rejects_missing_evidence_ids(self) -> None:
        review, clause, evidence_doc, item = self._pending_bundle(
            clause_id="C00102", text="请作者撰写真实研究内容。", start=0, end=13,
        )
        item["evidence_ids"] = []
        review["source_content_pending_items"] = [item]
        with self.assertRaisesRegex(ValueError, "obligation is malformed"):
            _source_content_pending_release_gates(
                [review], clauses=[clause], evidence_doc=evidence_doc, expected_run_id="run-1",
            )

    def test_source_content_pending_rejects_tampering_stale_run_and_duplicate_ao(self) -> None:
        text = "前句。请作者撰写真实研究内容。后句。"
        quote = "请作者撰写真实研究内容。"
        start = text.index(quote)
        review, clause, evidence_doc, item = self._pending_bundle(
            clause_id="C00102", text=text, start=start, end=start + len(quote),
        )
        mutations = [
            ({**item, "source_quote": "伪造的作者指令"}, [clause], evidence_doc, "current-source-bound"),
            ({**item, "evidence_ids": ["E99999"]}, [clause], evidence_doc, "current-source-bound"),
            (item, [{**clause, "id": "C99999"}], evidence_doc, "current-source-bound"),
            (item, [{**clause, "source_span": {**clause["source_span"], "start_offset": 999}}], evidence_doc,
             "current-source-bound"),
            (item, [{**clause, "source_span": {**clause["source_span"], "source_sha256": "a" * 64}}],
             evidence_doc, "current-source-bound"),
        ]
        for changed_item, changed_clauses, changed_evidence, _ in mutations:
            with self.subTest(item=changed_item, clauses=changed_clauses):
                with self.assertRaisesRegex(ValueError, "current-source-bound"):
                    _source_content_pending_release_gates(
                        [{**review, "source_content_pending_items": [changed_item]}],
                        clauses=changed_clauses, evidence_doc=changed_evidence,
                        expected_run_id="run-1",
                    )
        with self.assertRaisesRegex(ValueError, "current-source-bound"):
            _source_content_pending_release_gates(
                [{**review, "run_id": "stale-run"}], clauses=[clause],
                evidence_doc=evidence_doc, expected_run_id="run-1",
            )
        with self.assertRaisesRegex(ValueError, "duplicate source-content pending obligation"):
            _source_content_pending_release_gates(
                [{**review, "source_content_pending_items": [item, item]}],
                clauses=[clause], evidence_doc=evidence_doc, expected_run_id="run-1",
            )

    def test_source_content_pending_rejects_malformed_review_container(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be an array"):
            _source_content_pending_release_gates(  # type: ignore[arg-type]
                {}, clauses=[], evidence_doc={"evidence": []}, expected_run_id="run-1",
            )

    def test_each_scope_unresolved_obligation_gets_a_distinct_non_executable_marker(self) -> None:
        clauses, evidence_doc, item_base = self._canonical_scope_source()
        first_id, first_identity = self._scope_identity(
            item_base, source_ref="Q-source-1", obligation_index=0,
        )
        second_id, second_identity = self._scope_identity(
            item_base, source_ref="Q-source-2", obligation_index=1,
        )
        review = {
            **self._scope_review_context(),
            "scope_unresolved_items": [
                {
                    "analysis_obligation_id": first_id,
                    "analysis_obligation_identity": first_identity,
                    "work_type": "scope_clarification",
                    "clause_id": "C1", "source_ref": "Q-source-1",
                    **item_base,
                    "obligation_summary": "The lower bound uses groups.",
                    "scope_dependency_codes": ["quantitative_scope_unit_ambiguity"],
                    "scope_dependency_dimensions": ["metric"],
                },
                {
                    "analysis_obligation_id": second_id,
                    "analysis_obligation_identity": second_identity,
                    "work_type": "scope_clarification",
                    "clause_id": "C1", "source_ref": "Q-source-2",
                    **item_base,
                    "obligation_summary": "A separate obligation has a different unresolved target.",
                    "scope_dependency_codes": ["quantitative_scope_unit_ambiguity"],
                    "scope_dependency_dimensions": ["target"],
                },
            ],
        }
        gates = _scope_unresolved_release_gates(
            [review], clauses=clauses, evidence_doc=evidence_doc,
        )
        self.assertEqual(len(gates), 2)
        self.assertNotEqual(gates[0]["analysis_obligation_id"], gates[1]["analysis_obligation_id"])
        self.assertTrue(all(not gate["execution_authorized"] for gate in gates))

        ledger = build_manual_review_ledger({}, [], release_gates=gates, binding={
            "case_id": "bsu", "run_id": "run-1",
            "source_sha256": "a" * 64, "clause_sha256": "b" * 64,
            "evidence_sha256": "c" * 64, "request_sha256": None,
            "requirements_sha256": "d" * 64, "input_source_sha256": "e" * 64,
            "format_spec_sha256": "f" * 64, "official_template_sha256": None,
            "official_template_source": "not_supplied",
        })
        self.assertEqual(len(ledger["items"]), 2)
        self.assertFalse(ledger["submission_ready"])
        self.assertEqual(
            {item["analysis_obligation_id"] for item in ledger["items"]},
            {first_id, second_id},
        )
        self.assertTrue(all(
            item["marker_required"] and "待确认适用范围" in item["placeholder_text"]
            for item in ledger["items"]
        ))

    def test_scope_unresolved_release_gate_rejects_authorized_or_duplicate_records(self) -> None:
        clauses, evidence_doc, item_base = self._canonical_scope_source()
        obligation_id, identity = self._scope_identity(
            item_base, source_ref="Q-source-1",
        )
        item = {
            "analysis_obligation_id": obligation_id,
            "analysis_obligation_identity": identity,
            "work_type": "scope_clarification",
            "clause_id": "C1", "source_ref": "Q-source-1",
            **item_base,
            "obligation_summary": "scope unclear",
            "scope_dependency_codes": ["quantitative_scope_unit_ambiguity"],
            "scope_dependency_dimensions": ["metric"],
            "execution_authorized": True,
        }
        with self.assertRaisesRegex(ValueError, "malformed.*executable"):
            _scope_unresolved_release_gates(
                [{**self._scope_review_context(), "scope_unresolved_items": [item]}], clauses=clauses,
                evidence_doc=evidence_doc,
            )
        item["execution_authorized"] = False
        with self.assertRaisesRegex(ValueError, "duplicate scope-unresolved obligation"):
            _scope_unresolved_release_gates(
                [{**self._scope_review_context(), "scope_unresolved_items": [item, item]}], clauses=clauses,
                evidence_doc=evidence_doc,
            )

    def test_scope_unresolved_gate_rejects_unbound_quote_range_hash_or_location(self) -> None:
        clauses, evidence_doc, item_base = self._canonical_scope_source()
        obligation_id, identity = self._scope_identity(
            item_base, source_ref="Q-source-1",
        )
        base = {
            "analysis_obligation_id": obligation_id,
            "analysis_obligation_identity": identity,
            "work_type": "scope_clarification",
            "clause_id": "C1", "source_ref": "Q-source-1", **item_base,
            "obligation_summary": "scope unclear",
            "scope_dependency_codes": ["quantitative_scope_unit_ambiguity"],
            "scope_dependency_dimensions": ["metric"],
        }
        mutations = [
            {"source_start": 999, "source_end": 1000},
            {"source_quote": "forged quote"},
            {"source_text_sha256": "a" * 64},
            {"source_location": None},
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                item = {**base, **mutation}
                with self.assertRaisesRegex(ValueError, "unbound to exact source"):
                    _scope_unresolved_release_gates(
                        [{**self._scope_review_context(), "scope_unresolved_items": [item]}], clauses=clauses,
                        evidence_doc=evidence_doc,
                    )

    def test_unknown_output_policy_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported output policy"):
            enforce_obligation_review_output_policy([], output_policy="preview")


if __name__ == "__main__":
    unittest.main()
