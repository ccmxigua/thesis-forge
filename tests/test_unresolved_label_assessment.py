from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from unresolved_label_assessment import (
    unresolved_label_assessment, build_unresolved_label_assessments,
    validate_unresolved_label_assessments,
)
from native_semantic_review import (
    validate_obligation_coverage_response, MissingSourceObligationInventoryError,
    OBLIGATION_COVERAGE_SCHEMA,
)
from semantic_source_references import build_source_reference_packet, compile_source_reference_response
from semantic_contract import sha256_json
from semantic_review_ledger import build_semantic_review_ledger
from table_source_context import text_sha256, build_table_structure_context
import host_agent_bridge as bridge
import thesis_format_pipeline as pipeline


class UnresolvedLabelTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads((ROOT / "tests/fixtures/unresolved-label-review.json").read_text())
        self.request = copy.deepcopy(self.fixture["request"])
        self.check = self.request["checks"][0]
        self.result = copy.deepcopy(self.fixture["compiled_response"]["results"][0])

    def test_captured_empty_review_preserves_primary_unknowns(self):
        original = copy.deepcopy((self.check, self.result))
        accepted = validate_obligation_coverage_response({"results": [self.result]}, [self.check])
        self.assertEqual(accepted, [self.result])
        proof = unresolved_label_assessment(self.check, self.result)
        self.assertEqual(proof["primary_obligations"], self.check["review_context"]["primary_obligations"])
        self.assertFalse(proof["uncertainty_resolved"])
        self.assertFalse(proof["submission_ready"])
        self.assertEqual((self.check, self.result), original)

    def test_known_typed_or_pending_atoms_cannot_take_empty_path(self):
        for key, value in (("force", "required"), ("actor", "author"), ("action", "print"),
                           ("status", "covered"), ("route", "human"), ("applicability", "applicable"),
                           ("condition", "classified theses")):
            check = copy.deepcopy(self.check)
            check["review_context"]["primary_obligations"][0][key] = value
            with self.subTest(key=key):
                self.assertIsNone(unresolved_label_assessment(check, self.result))
                with self.assertRaises(MissingSourceObligationInventoryError):
                    validate_obligation_coverage_response({"results": [self.result]}, [check])

    def test_missing_partial_foreign_or_stale_source_proofs_rejected(self):
        def mutate_table(c, fn): fn(c["review_context"]["table_structure_context"])
        def mutate_atom(c, fn): fn(c["review_context"]["primary_obligations"][0])
        changes = [
            lambda c: c["review_context"].pop("table_structure_context"),
            lambda c: c["review_context"].update(primary_obligation_quote_bindings=[]),
            lambda c: c["review_context"].update(linked_requirements=[{"requirement_ref": "R"}]),
            lambda c: c.update(check_id="other"),
            lambda c: c.update(document_text="密 级"),
            lambda c: mutate_table(c, lambda t: t.update(target_column=1)),
            lambda c: mutate_table(c, lambda t: t["source_row"].update(row_sha256="0" * 64)),
            lambda c: mutate_atom(c, lambda a: a.update(source_quote="级")),
            lambda c: c["review_context"]["cited_evidence"]["E00004"].update(text="密 级："),
        ]
        for change in changes:
            check = copy.deepcopy(self.check); change(check)
            with self.subTest(change=change):
                self.assertIsNone(unresolved_label_assessment(check, self.result))

    def set_label(self, text):
        """Change every source binding, not just the visible test string."""
        self.check["document_text"] = text
        context = self.check["review_context"]
        context["semantic_clause_text"] = text
        full = text + "："
        target = context["cited_evidence"]["E00004"]
        target["text"] = full
        row = target["table_row_context"]
        row["cells"][0]["paragraphs"][0].update(text=full, text_sha256=text_sha256(full))
        row["row_sha256"] = sha256_json({k: v for k, v in row.items() if k != "row_sha256"})
        context["table_structure_context"] = build_table_structure_context(
            {"source_span": {"evidence_id": "E00004", "source_sha256": text_sha256(full)}},
            context["cited_evidence"])
        context["primary_obligations"][0]["source_quote"] = text
        b = context["primary_obligation_quote_bindings"][0]
        b.update(original_source_quote=text, review_source_quote=text,
                 original_quote_sha256=sha256_json(text), review_quote_sha256=sha256_json(text))
        proof = b["source_binding"]
        proof.update(quote_end_offset=len(text), quote_sha256=sha256_json(text))
        proof["clause_binding"]["text"] = text
        proof["clause_binding"]["source_fragments"][0].update(
            text=text, end_offset=len(text), source_sha256=text_sha256(full))
        self.result["evidence_quotes"] = [text]

    def test_other_label_and_ids_are_not_special_cased(self):
        self.set_label("Department name")
        self.check["check_id"] = self.result["check_id"] = "renamed-clause"
        self.check["review_context"]["primary_obligation_quote_bindings"][0]["source_binding"]["clause_binding"]["source_fragments"][0]["clause_id"] = "renamed-clause"
        self.assertIsNotNone(unresolved_label_assessment(self.check, self.result))

    def test_normative_numeric_conditional_and_external_prose_stays_blocked(self):
        for text in ("摘要应为第三人称", "Keywords must be separated", "最多八组", "300至1000字",
                     "导师签字", "部门批准", "必须填写", "If public leave blank", "Name；学号"):
            self.setUp(); self.set_label(text)
            with self.subTest(text=text):
                # Chinese written quantities/prose not captured by digit checks
                # still must not become general label no-duty evidence.
                self.assertIsNone(unresolved_label_assessment(self.check, self.result))

    def test_nonempty_or_incomplete_or_partial_review_is_not_synthesized(self):
        for mutation in (lambda r: r.update(verdict="incomplete"),
                         lambda r: r.update(evidence_quotes=["级"]),
                         lambda r: r.update(rationale=""),
                         lambda r: r.update(identified_obligations=[{"disposition": "ambiguous"}])):
            result = copy.deepcopy(self.result); mutation(result)
            self.assertIsNone(unresolved_label_assessment(self.check, result))

    def test_analysis_record_rebuilt_not_resealed(self):
        ledger = {"unresolved_label_assessments": build_unresolved_label_assessments([self.check], [self.result])}
        validate_unresolved_label_assessments(ledger, [self.check], [self.result])
        for mutate in (lambda l: l.update(unresolved_label_assessments=[]),
                       lambda l: l["unresolved_label_assessments"][0].update(uncertainty_resolved=True),
                       lambda l: l["unresolved_label_assessments"][0].update(primary_obligations=[])):
            changed = copy.deepcopy(ledger); mutate(changed)
            with self.assertRaises(ValueError):
                validate_unresolved_label_assessments(changed, [self.check], [self.result])

    def test_source_reference_compilation_and_production_ledger_keep_empty_distinct(self):
        packet = build_source_reference_packet(self.request)
        ref = next(s["ref_id"] for s in packet["checks"][0]["source_spans"] if s["text"] == self.check["document_text"])
        raw = {"results": [{k: copy.deepcopy(v) for k, v in self.result.items()
                            if k not in {"evidence_quotes", "machine_obligation_ids"}}]}
        raw["results"][0]["evidence_refs"] = [ref]
        before = copy.deepcopy(raw)
        compiled, compilation = compile_source_reference_response(raw, self.request, OBLIGATION_COVERAGE_SCHEMA, coverage=True)
        results = validate_obligation_coverage_response(compiled, self.request["checks"])
        self.assertEqual(before, raw)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            path = directory / "source-reference-compilation.json"
            bridge._write_json(path, compilation)
            audit = {"source_reference_compilation_path": str(path),
                     "request_sha256": sha256_json(self.request),
                     "source_reference_compilation_sha256": bridge.sha256_file(path),
                     "canonical_response_sha256": sha256_json(compiled),
                     "response_sha256": sha256_json(compiled), "results": results}
            pointer = bridge._write_obligation_analysis_ledger(audit, {},
                {"case_id": self.request["case_id"], "provenance": self.request["provenance"]},
                coverage_request=self.request, output_dir=directory, review_dir=directory,
                run_id=self.request["run_id"], chunk_index=1, attempt=1)
            ledger = json.loads((directory / pointer["path"]).read_text())
            self.assertEqual(ledger["obligations"], [])
            self.assertEqual(ledger["unresolved_label_assessments"][0]["primary_obligations"],
                             self.check["review_context"]["primary_obligations"])
            validate_unresolved_label_assessments(ledger, self.request["checks"], results)

    def test_primary_semantic_ledger_still_unresolved_not_covered(self):
        context = self.check["review_context"]
        primary = {"contract_version": "3.0", "requirements": [], "clause_reviews": [{
            "clause_id": self.check["check_id"], "classification": "unresolved",
            "reason": context["reason"], "obligations": context["primary_obligations"]}]}
        ledger = build_semantic_review_ledger(primary, [{"id": self.check["check_id"],
            "text": self.check["document_text"], "evidence_ids": ["E00004"]}])
        record = ledger["clauses"][0]
        self.assertEqual(record["classification"], "unresolved")
        self.assertEqual(record["obligations"], context["primary_obligations"])
        self.assertEqual(record["requirement_indexes"], [])

    def test_completed_bridge_chain_rebuilds_label_assessment(self):
        from tests.test_host_agent_bridge import HostAgentBridgeTests
        context = self.check["review_context"]
        fragment = context["primary_obligation_quote_bindings"][0]["source_binding"]["clause_binding"]["source_fragments"][0]
        clause = {"id": self.check["check_id"], "text": context["semantic_clause_text"],
                  "evidence_ids": [fragment["evidence_id"]], "source_span": {
                      k: copy.deepcopy(fragment[k]) for k in (
                          "evidence_id", "start_offset", "end_offset", "text", "source_sha256", "location")}}
        chunk = {"case_id": self.request["case_id"], "clauses": [clause],
                 "provenance": self.request["provenance"], "evidence_context": context["cited_evidence"]}
        primary = {"contract_version": "3.0", "provenance": self.request["provenance"],
                   "requirements": [], "clause_reviews": [{"clause_id": clause["id"],
                       "classification": "unresolved", "reason": context["reason"],
                       "normative_basis": "insufficient", "obligations": context["primary_obligations"]}]}
        def result_builder(check, source_ref):
            return {"check_id": check["check_id"], "verdict": "consistent",
                    "rationale": self.result["rationale"], "evidence_refs": [source_ref], "identified_obligations": []}
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            independent = HostAgentBridgeTests._fake_independent_review(primary, chunk,
                review_dir=directory, run_id=self.request["run_id"], chunk_index=1, attempt=1,
                result_builder=result_builder)
            envelope = json.loads((directory / independent["audit_path"]).read_text())
            bridge._validate_completed_obligation_ledger_chain(directory, envelope, independent,
                primary, chunk, chunk_index=1, attempt=1, output_policy="submission")
            path = directory / independent["obligation_analysis_ledger_path"]
            ledger = json.loads(path.read_text()); ledger["unresolved_label_assessments"] = []
            bridge._write_json(path, ledger)
            digest = bridge.sha256_file(path)
            independent["obligation_analysis_ledger_sha256"] = digest
            envelope["obligation_analysis_ledger"]["sha256"] = digest
            with self.assertRaisesRegex(ValueError, "canonical current-run reconstruction"):
                bridge._validate_completed_obligation_ledger_chain(directory, envelope, independent,
                    primary, chunk, chunk_index=1, attempt=1, output_policy="submission")
            spec = {"status": "needs_clarification", "completeness": {"unresolved_clause_ids": [clause["id"]]}}
            self.assertIn("unresolved_clauses", pipeline.requirement_blockers(spec, [{"clause_id": clause["id"]}], "full"))


if __name__ == "__main__":
    unittest.main()
