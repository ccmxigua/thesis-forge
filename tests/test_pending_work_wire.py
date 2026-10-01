"""Registered codes are per-source selectors, not generic human-task tags."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import host_agent_bridge as bridge
import native_semantic_review as native
from format_spec_validation import validate_instance
from host_review_schema import native_output_schema, native_schema_support_errors
from pending_source_work import compile_pending_source_work, ANTI_EXCERPT, REPETITION_SCOPE, SECTION_CHOICE
from semantic_contract import sha256_json
from semantic_source_references import (
    build_source_reference_packet, source_reference_schema, compile_source_reference_response,
    SourceReferenceResponseError,
)


def incident():
    return json.loads((ROOT / "tests/fixtures/pending-work-code-incident.json").read_text())


def wire_response(request, canonical):
    packet = build_source_reference_packet(request)
    result = copy.deepcopy(canonical)
    check = next(c for c in packet["checks"] if c["check_id"] == result["check_id"])
    def ref(quote):
        return next(s["ref_id"] for s in check["source_spans"] if s["text"] == quote)
    result["evidence_refs"] = [ref(q) for q in result.pop("evidence_quotes")]
    result.pop("machine_obligation_ids")
    for obligation in result["identified_obligations"]:
        obligation["source_ref"] = ref(obligation.pop("source_quote"))
    return {"results": [result]}


class PendingWorkWireTests(unittest.TestCase):
    def test_captured_wrong_code_rejected_before_semantic_validation(self):
        data = incident(); request = {"run_id": "current", "checks": [data["check"]]}
        raw = wire_response(request, data["result"]); frozen = copy.deepcopy(raw)
        with self.assertRaises(SourceReferenceResponseError) as caught:
            compile_source_reference_response(raw, request, native.OBLIGATION_COVERAGE_SCHEMA, coverage=True)
        self.assertEqual(caught.exception.issues[0]["check_id"], data["check"]["check_id"])
        self.assertIn("pending_work_code", json.dumps(caught.exception.issues))
        self.assertEqual(raw, frozen)
        with self.assertRaisesRegex(native.NativeSemanticReviewError, "pending human-work inventory"):
            native.validate_obligation_coverage_response({"results": [copy.deepcopy(data["result"])]}, request["checks"])

    def test_generic_null_keeps_exact_human_duty_and_no_automatic_requirement(self):
        data = incident(); request = {"run_id": "current", "checks": [data["check"]]}
        raw = wire_response(request, data["result"])
        raw["results"][0]["identified_obligations"][0]["pending_work_code"] = None
        compiled, receipt = compile_source_reference_response(raw, request, native.OBLIGATION_COVERAGE_SCHEMA,
            coverage=True, provider_nullable_optionals=True)
        expected = copy.deepcopy(data["result"])
        expected["identified_obligations"][0].pop("pending_work_code")
        self.assertEqual(compiled, {"results": [expected]})
        self.assertEqual(native.validate_obligation_coverage_response(copy.deepcopy(compiled), request["checks"]), [expected])
        self.assertTrue(receipt["semantic_verdicts_unchanged"])
        self.assertNotEqual(receipt["raw_response_sha256"], receipt["compiled_response_sha256"])
        self.assertEqual(compiled["results"][0]["identified_obligations"][0]["requirement_refs"], [])

    def test_source_grammar_limits_codes_and_native_disposition(self):
        sources = (
            ("本部分主要撰写国内的研究现状，不能是文献资料的简单摘录，需要分类、总结、归纳", {ANTI_EXCERPT}),
            ("本部分主要撰写国内的研究现状，计算在重复率内，需要分类、总结、归纳", {REPETITION_SCOPE}),
            ("若论文研究确实无国外资料，本部分也可删除", {SECTION_CHOICE}),
            ("论文中的实验数据须可追溯至原始实验记录。", set()),
        )
        for source, allowed in sources:
            request = {"checks": [{"check_id": "dynamic", "document_text": source,
                "review_context": {"classification": "requires_source_verification"}}]}
            packet = build_source_reference_packet(request)
            schema = source_reference_schema(native.OBLIGATION_COVERAGE_SCHEMA, packet, coverage=True)
            projected = native_output_schema(schema)
            self.assertEqual(native_schema_support_errors(projected), [])
            obligation_schema = schema["properties"]["results"]["items"]["anyOf"][0]["properties"]["identified_obligations"]["items"]
            for code in (None, ANTI_EXCERPT, REPETITION_SCOPE, SECTION_CHOICE, "unknown"):
                for disposition in ("source_content_verification_pending", "external_action_pending", "authoring_content_pending"):
                    atom = {"source_ref": packet["checks"][0]["source_spans"][0]["ref_id"],
                            "disposition": disposition, "requirement_refs": []}
                    if code is not None: atom["pending_work_code"] = code
                    with self.subTest(source=source, code=code, disposition=disposition):
                        valid = code is None or (code in allowed and disposition == "source_content_verification_pending")
                        self.assertEqual(not bool(validate_instance(atom, obligation_schema)), valid)

    def test_foreign_context_cannot_authorize_unrelated_registered_code(self):
        data = incident(); check = data["check"]
        check["review_context"]["pending_source_work"] = compile_pending_source_work(
            "本部分主要撰写国内的研究现状，不能是文献资料的简单摘录，需要分类、总结、归纳")
        request = {"checks": [check]}; raw = wire_response(request, data["result"])
        with self.assertRaises(SourceReferenceResponseError):
            compile_source_reference_response(raw, request, native.OBLIGATION_COVERAGE_SCHEMA, coverage=True)

    def test_registered_code_cannot_select_an_unrelated_span_in_same_check(self):
        source = "本部分主要撰写国内的研究现状，不能是文献资料的简单摘录，计算在重复率内，需要分类、总结、归纳"
        facts = compile_pending_source_work(source)
        quotes = [fact["source_quote"] for fact in facts] + ["需要分类、总结、归纳"]
        check = {"check_id": "new", "document_text": source, "review_context": {
            "primary_obligations": [{"source_quote": quote} for quote in quotes]}}
        packet = build_source_reference_packet({"checks": [check]})
        schema = source_reference_schema(native.OBLIGATION_COVERAGE_SCHEMA, packet, coverage=True)
        atoms = schema["properties"]["results"]["items"]["anyOf"][0]["properties"]["identified_obligations"]["items"]
        for fact in facts:
            for span in packet["checks"][0]["source_spans"]:
                atom = {"source_ref": span["ref_id"], "pending_work_code": fact["code"],
                    "disposition": "source_content_verification_pending", "requirement_refs": []}
                expected = span["start"] <= fact["start"] and span["end"] >= fact["end"]
                with self.subTest(code=fact["code"], quote=span["text"]):
                    self.assertEqual(not bool(validate_instance(atom, atoms)), expected)

    def test_mixed_authoring_and_two_registered_checks_survive_wire_roundtrip(self):
        source = "本部分主要撰写国内的研究现状，不能是文献资料的简单摘录，计算在重复率内，需要分类、总结、归纳"
        facts = compile_pending_source_work(source)
        check = {"check_id": "dynamic-mixed", "document_text": source, "review_context": {
            "classification": "requires_source_content", "requires_requirement": False,
            "linked_requirements": [], "machine_obligation_ids": [], "pending_source_work": facts}}
        request = {"checks": [check]}
        result = {"check_id": check["check_id"], "verdict": "source_content_pending",
            "rationale": "Author work and human checks remain distinct and pending.",
            "evidence_quotes": [source], "machine_obligation_ids": [],
            "identified_obligations": [{"source_quote": source, "disposition": "authoring_content_pending",
                "obligation_summary": "撰写、分类、总结和归纳。", "requirement_refs": []}] + [
                {"source_quote": source, "disposition": "source_content_verification_pending",
                 "pending_work_code": fact["code"], "obligation_summary": fact["source_quote"],
                 "requirement_refs": []} for fact in facts]}
        compiled, _ = compile_source_reference_response(wire_response(request, result), request,
            native.OBLIGATION_COVERAGE_SCHEMA, coverage=True)
        self.assertEqual(compiled, {"results": [result]})
        self.assertEqual(native.validate_obligation_coverage_response(copy.deepcopy(compiled), [check]), [result])
        self.assertEqual(len(compiled["results"][0]["identified_obligations"]), 3)

    def test_external_unmatched_duty_stays_reportable_not_auto_promoted(self):
        source = "作者须申请批准，同时须核对数据来源。"
        check = {"check_id": "external", "document_text": source, "review_context": {
            "classification": "external_compliance", "primary_obligations": [{"id": "approval"}],
            "linked_requirements": []}}
        packet = build_source_reference_packet({"checks": [check]})
        schema = source_reference_schema(native.OBLIGATION_COVERAGE_SCHEMA, packet, coverage=True)
        atom_schema = schema["properties"]["results"]["items"]["anyOf"][0]["properties"]["identified_obligations"]["items"]
        atom = {"source_ref": packet["checks"][0]["source_spans"][0]["ref_id"],
                "disposition": "unrepresented", "obligation_summary": "Additional provenance duty.", "requirement_refs": []}
        self.assertEqual(validate_instance(atom, atom_schema), [])
        # An unmatched atom must expose a primary inventory gap; this patch
        # must not silently promote it to a mapped/accepted pending action.
        for disposition in ("external_action_pending", "source_content_verification_pending", "represented"):
            atom["disposition"] = disposition
            self.assertTrue(validate_instance(atom, atom_schema))

    def test_registered_code_cannot_borrow_an_external_primary_id(self):
        source = "本部分主要撰写国内的研究现状，不能是文献资料的简单摘录，需要分类、总结、归纳"
        self.assertTrue(compile_pending_source_work(source))
        for classification in ("external_compliance", "executable_with_external_check"):
            check = {"check_id": "external", "document_text": source, "review_context": {
                "classification": classification, "primary_obligations": [{"id": "external-primary"}],
                "linked_requirements": []}}
            packet = build_source_reference_packet({"checks": [check]})
            schema = source_reference_schema(native.OBLIGATION_COVERAGE_SCHEMA, packet, coverage=True)
            atom_schema = schema["properties"]["results"]["items"]["anyOf"][0]["properties"]["identified_obligations"]["items"]
            atom = {"source_ref": packet["checks"][0]["source_spans"][0]["ref_id"],
                "disposition": "source_content_verification_pending", "pending_work_code": ANTI_EXCERPT,
                "primary_obligation_id": "external-primary", "requirement_refs": []}
            self.assertTrue(validate_instance(atom, atom_schema))
            atom.pop("pending_work_code"); atom.pop("primary_obligation_id")
            atom["disposition"] = "unrepresented"
            self.assertEqual(validate_instance(atom, atom_schema), [])

    def test_unregistered_quoted_negated_and_conditional_sources_keep_generic_channel(self):
        for source in ("例如：“不能是文献资料的简单摘录”。", "若需要，计算在重复率内。", "本部分不是文献资料的简单摘录。"):
            self.assertEqual(compile_pending_source_work(source), [])
            request = {"checks": [{"check_id": "new", "document_text": source}]}
            packet = build_source_reference_packet(request)
            schema = source_reference_schema(native.OBLIGATION_COVERAGE_SCHEMA, packet, coverage=True)
            atom_schema = schema["properties"]["results"]["items"]["anyOf"][0]["properties"]["identified_obligations"]["items"]
            atom = {"source_ref": packet["checks"][0]["source_spans"][0]["ref_id"],
                    "disposition": "source_content_verification_pending", "requirement_refs": []}
            self.assertEqual(validate_instance(atom, atom_schema), [])
            atom["pending_work_code"] = ANTI_EXCERPT
            self.assertTrue(validate_instance(atom, atom_schema))

    def run_bound_retry(self, *, persist=False):
        data = incident(); source = data["check"]["document_text"]; cid = "different-current-clause"
        eid = "different-current-evidence"; provenance = {"run_id": "wire-offline", "source_sha256": sha256_json(source),
            "clause_sha256": sha256_json(cid), "evidence_sha256": sha256_json(eid), "request_sha256": sha256_json("new")}
        chunk = {"case_id": "other-case", "provenance": provenance,
            "clauses": [{"id": cid, "text": source, "evidence_ids": [eid], "source_span": {
                "evidence_id": eid, "start_offset": 0, "end_offset": len(source), "text": source,
                "source_sha256": hashlib.sha256(source.encode()).hexdigest()}}],
            "evidence_context": {eid: {"id": eid, "text": source}}}
        candidate = {"contract_version": "3.0", "requirements": [], "clause_reviews": [{
            "clause_id": cid, "classification": "requires_source_verification", "reason": "Human verification.",
            "obligations": copy.deepcopy(data["check"]["review_context"]["primary_obligations"])}],
            "unsupported_items": [], "reported_conflicts": [], "provenance": provenance}
        frozen = copy.deepcopy(candidate); calls = []
        def reviewer(request, **kwargs):
            calls.append(copy.deepcopy(request))
            canonical = copy.deepcopy(data["result"]); canonical["check_id"] = cid
            raw = wire_response(request, canonical)
            if len(calls) == 2 and not persist:
                raw["results"][0]["identified_obligations"][0].pop("pending_work_code")
            try:
                compiled, receipt = compile_source_reference_response(raw, request,
                    native.OBLIGATION_COVERAGE_SCHEMA, coverage=True)
            except SourceReferenceResponseError as error:
                raise native.SourceReferenceContractError(error) from error
            native.validate_obligation_coverage_response(compiled, request["checks"])
            directory = kwargs["output_dir"]; directory.mkdir(parents=True, exist_ok=True)
            path = directory / "source-reference-compilation.json"
            bridge._write_json(path, receipt)
            return {"status": "completed", "results": compiled["results"], "summary": {},
                "request_sha256": sha256_json(request), "response_sha256": sha256_json(compiled),
                "canonical_response_sha256": sha256_json(compiled),
                "source_reference_compilation_path": str(path),
                "source_reference_compilation_sha256": bridge.sha256_file(path)}
        with tempfile.TemporaryDirectory() as td, patch.object(bridge, "run_native_semantic_review", side_effect=reviewer), patch.object(bridge.time, "sleep"):
            args = dict(review_dir=Path(td), run_id="wire-offline", chunk_index=1, attempt=1,
                host_runtime="codex", model="gpt-5.6-luna", timeout=5, agent_id="main", runner="exec",
                binary="codex", config_path=None, controller=bridge.RunController(), output_policy="review_draft")
            if persist:
                with self.assertRaises(bridge.IndependentObligationReviewError):
                    bridge._run_independent_obligation_coverage_review(candidate, chunk, **args)
                self.assertEqual(list(Path(td).rglob("obligation-analysis-ledger.json")), [])
            else:
                pointer = bridge._run_independent_obligation_coverage_review(candidate, chunk, **args)
                self.assertEqual(pointer["status"], "completed")
                ledger = json.loads(next(Path(td).rglob("obligation-analysis-ledger.json")).read_text())
                self.assertFalse(ledger["submission_ready"])
                self.assertEqual(ledger["obligations"][0]["disposition"], "source_content_verification_pending")
            self.assertEqual(len(calls), 2)
            self.assertEqual(candidate, frozen)
            self.assertNotIn("retry_feedback", calls[0])
            self.assertTrue(native.source_reference_retry_feedback_is_bound(calls[1]))
            self.assertNotEqual(build_source_reference_packet(calls[0])["checks"][0]["source_spans"],
                build_source_reference_packet(calls[1])["checks"][0]["source_spans"])

    def test_wrong_code_receives_only_one_fresh_independent_read(self):
        self.run_bound_retry()

    def test_persistent_wrong_code_exhausts_without_success_ledger(self):
        self.run_bound_retry(persist=True)


if __name__ == "__main__": unittest.main()
