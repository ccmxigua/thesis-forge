from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import host_agent_bridge as bridge
import native_semantic_review as native
from host_review_contract import contract_error_records
from source_atom_metadata import project_atom_metadata, bind_atom_quote
from source_literal_binding import compose_source_fragments
from semantic_contract import sha256_json


class SourceAtomMetadataTests(unittest.TestCase):
    def numbered_incident(self):
        data = json.loads((ROOT / "tests/fixtures/numbered-context-quote-incident.json").read_text())
        clauses = data["clauses"]
        ids = {c["id"]: f"current-clause-{i}" for i, c in enumerate(clauses)}
        old_eid = clauses[0]["evidence_ids"][0]
        for c in clauses:
            c["id"] = ids[c["id"]]
            c["evidence_ids"] = ["current-evidence"]
            c["source_span"]["evidence_id"] = "current-evidence"
        evidence = data["evidence_context"][old_eid]
        evidence["id"] = "current-evidence"
        review = data["review"]
        review["clause_id"] = clauses[0]["id"]
        review["obligations"][0]["id"] = "current-atom"
        response = {"contract_version": "3.0", "requirements": [], "clause_reviews": [review]}
        chunk = {"clauses": clauses, "evidence_context": {"current-evidence": evidence},
                 "provenance": {"run_id": "fresh-numbered-quote-test"}}
        return response, chunk

    def example(self):
        source = "Keywords  must use semicolons。"
        span_text = source[:-1]
        clause = {"id": "clause-random", "text": span_text, "evidence_ids": ["evidence-random"],
                  "source_span": {"evidence_id": "evidence-random", "start_offset": 0,
                                  "end_offset": len(span_text), "text": span_text,
                                  "source_sha256": hashlib.sha256(source.encode()).hexdigest()}}
        chunk = {"clauses": [clause], "evidence_context": {"evidence-random": {"text": source}}}
        response = {"requirements": [], "clause_reviews": [{"clause_id": clause["id"],
                    "classification": "executable", "reason": "source",
                    "obligations": [{"id": "atom-random", "status": "covered", "source_quote": source.replace("  ", " "),
                                     "route": "human", "actor": "author", "action": "separate",
                                     "target": "keywords", "force": "required"}]}]}
        return response, chunk

    def records(self, response, chunk, fields=("source_quote", "route")):
        suffixes = {"source_quote": "must_equal_current_source_subspan", "route": "responsibility_route_conflict"}
        return contract_error_records([
            f"$.clause_reviews[0].obligations[0].{field}: {suffixes[field]}" for field in fields
        ], response=response, chunk=chunk)

    def test_two_representation_errors_compose_without_semantic_change(self):
        response, chunk = self.example()
        original = copy.deepcopy(response)
        projected, audit = project_atom_metadata(response, self.records(response, chunk), chunk)
        expected = copy.deepcopy(response)
        expected["clause_reviews"][0]["obligations"][0].update(source_quote=chunk["clauses"][0]["text"], route="automatic")
        self.assertEqual(projected, expected)
        self.assertEqual(response, original)
        self.assertEqual(len(audit), 2)
        self.assertTrue(all(p["independent_review_required"] for p in audit))
        self.assertTrue(all(p["source_chunk_sha256"] == sha256_json(chunk) for p in audit))
        self.assertFalse(any(p["submission_ready"] for p in audit))

    def test_stale_source_feedback_foreign_edges_and_changed_words_are_not_repairable(self):
        for change in ("stale_hash", "wrong_evidence", "wrong_quote", "duplicate_clause", "stale_feedback"):
            response, chunk = self.example()
            response["clause_reviews"][0]["obligations"][0]["route"] = "automatic"
            records = self.records(response, chunk, ("source_quote",))
            if change == "stale_hash": chunk["clauses"][0]["source_span"]["source_sha256"] = "f" * 64
            elif change == "wrong_evidence": chunk["clauses"][0]["evidence_ids"] = ["foreign"]
            elif change == "wrong_quote":
                response["clause_reviews"][0]["obligations"][0]["source_quote"] = "Keywords may use commas."
                records = self.records(response, chunk, ("source_quote",))
            elif change == "duplicate_clause": chunk["clauses"].append(copy.deepcopy(chunk["clauses"][0]))
            else: records[0]["response_sha256"] = "0" * 64
            with self.subTest(change=change):
                self.assertEqual(project_atom_metadata(response, records, chunk), (None, []))

    def test_retry_correction_cannot_authorize_changes_of_meaning_or_atom_position(self):
        response, chunk = self.example()
        records = self.records(response, chunk)
        projected, _ = project_atom_metadata(response, records, chunk)
        quote_path = "$.clause_reviews[0].obligations[0].source_quote"
        self.assertTrue(bridge._atom_metadata_retry_path_allowed(response, projected, records, quote_path, chunk))
        for field, value in (("action", "omit"), ("target", "abstract"), ("status", "unverifiable"),
                             ("id", "new-atom"), ("force", "optional")):
            changed = copy.deepcopy(projected)
            changed["clause_reviews"][0]["obligations"][0][field] = value
            with self.subTest(field=field):
                self.assertFalse(bridge._atom_metadata_retry_path_allowed(response, changed, records, quote_path, chunk))
        changed = copy.deepcopy(projected)
        changed["clause_reviews"][0]["classification"] = "informational"
        self.assertFalse(bridge._atom_metadata_retry_path_allowed(response, changed, records, quote_path, chunk))

    def test_source_punctuation_and_normalized_whitespace_are_reconstructed_not_guessed(self):
        response, chunk = self.example()
        source = "密  级："
        c = chunk["clauses"][0]
        c.update(text="密 级")
        c["source_span"].update(text=source[:-1], end_offset=len(source)-1,
                                source_sha256=hashlib.sha256(source.encode()).hexdigest())
        chunk["evidence_context"]["evidence-random"]["text"] = source
        atom = response["clause_reviews"][0]["obligations"][0]
        atom.update(source_quote="密 级：", route="automatic")
        projected, _ = project_atom_metadata(response, self.records(response, chunk, ("source_quote",)), chunk)
        self.assertEqual(projected["clause_reviews"][0]["obligations"][0]["source_quote"], "密  级")
        atom["source_quote"] = "密级"  # Whitespace deletion is not equivalence.
        self.assertEqual(project_atom_metadata(response, self.records(response, chunk, ("source_quote",)), chunk), (None, []))

    def test_captured_first_wave_atoms_preserve_every_nonrepresentation_field(self):
        cases = json.loads((ROOT / "tests/fixtures/source-atom-metadata-cases.json").read_text())["cases"]
        counts = []
        for case in cases:
            response, chunk = case["response"], case["chunk"]
            # Use the real shared validator to recreate the error facts. Other
            # requirement-level findings remain untouched in this atom fixture.
            records = contract_error_records(bridge.validate_host_agent_response(response, chunk), response=response, chunk=chunk)
            projected, audit = project_atom_metadata(response, records, chunk)
            projected = projected or response
            counts.append(len(audit))
            for before, after in zip(response["clause_reviews"], projected["clause_reviews"]):
                self.assertEqual(before["classification"], after["classification"])
                for a, b in zip(before.get("obligations", []), after.get("obligations", [])):
                    self.assertEqual({k: v for k, v in a.items() if k not in {"source_quote", "route"}},
                                     {k: v for k, v in b.items() if k not in {"source_quote", "route"}})
            remaining = bridge.validate_host_agent_response(projected, chunk)
            self.assertFalse(any("must_equal_current_source_subspan" in e or "responsibility_route_conflict" in e for e in remaining), remaining)
        self.assertEqual(counts, [0, 0, 3, 7])

    def test_context_quote_is_source_bound_not_a_grant_of_execution_scope(self):
        response, chunk = self.example()
        c = chunk["clauses"][0]
        clause_map = {c["id"]: c}
        quote = chunk["evidence_context"]["evidence-random"]["text"]
        proof = bind_atom_quote(quote, c["id"], clause_map, chunk["evidence_context"])
        self.assertTrue(proof["context_is_not_execution_scope"])
        self.assertEqual(proof["clause_binding"]["text"], c["source_span"]["text"])
        for invalid in ("Foreign source.", "semicolons changed", "。", ""):
            with self.subTest(quote=invalid), self.assertRaises(ValueError):
                bind_atom_quote(invalid, c["id"], clause_map, chunk["evidence_context"])
        c["source_span"]["source_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            bind_atom_quote(quote, c["id"], clause_map, chunk["evidence_context"])

    def test_old_quote_diagnostic_allows_only_equivalent_bound_retry(self):
        response, chunk = self.example()
        atom = response["clause_reviews"][0]["obligations"][0]
        atom["source_quote"] = chunk["evidence_context"]["evidence-random"]["text"]
        atom["route"] = "automatic"
        records = self.records(response, chunk, ("source_quote",))
        after = copy.deepcopy(response)
        after["clause_reviews"][0]["obligations"][0]["source_quote"] = chunk["clauses"][0]["source_span"]["text"]
        path = "$.clause_reviews[0].obligations[0].source_quote"
        self.assertTrue(bridge._atom_metadata_retry_path_allowed(response, after, records, path, chunk))
        self.assertTrue(bridge._retry_changes_allowed(records, [path], contract_version="3.0",
                                                    previous_response=response, current_response=after, chunk=chunk))
        records[0]["code"] = "semantic_retry_change"
        self.assertFalse(bridge._atom_metadata_retry_path_allowed(response, after, records, path, chunk))
        records[0]["code"] = "contract_validation_error"
        records[0]["response_sha256"] = "0" * 64
        self.assertFalse(bridge._atom_metadata_retry_path_allowed(response, after, records, path, chunk))

    def test_internal_atom_whitespace_is_restored_without_widening_to_full_clause(self):
        response, chunk = self.example()
        source = "学位论文作者签名：                       年    月    日"
        clause = chunk["clauses"][0]
        clause.update(text=source)
        clause["source_span"].update(text=source, end_offset=len(source),
                                    source_sha256=hashlib.sha256(source.encode()).hexdigest())
        chunk["evidence_context"]["evidence-random"]["text"] = source
        atom = response["clause_reviews"][0]["obligations"][0]
        atom.update(source_quote="年 月 日", route="automatic")
        records = self.records(response, chunk, ("source_quote",))
        projected, audit = project_atom_metadata(response, records, chunk)
        self.assertEqual(projected["clause_reviews"][0]["obligations"][0]["source_quote"], "年    月    日")
        self.assertEqual({k: v for k, v in atom.items() if k != "source_quote"},
                         {k: v for k, v in projected["clause_reviews"][0]["obligations"][0].items() if k != "source_quote"})
        self.assertTrue(audit[0]["independent_review_required"])
        path = "$.clause_reviews[0].obligations[0].source_quote"
        self.assertTrue(bridge._atom_metadata_retry_path_allowed(response, projected, records, path, chunk))
        self.assertTrue(bridge._retry_changes_allowed(records, [path], contract_version="3.0",
                                                    previous_response=response, current_response=projected, chunk=chunk))
        for bad_quote in ("年月日", "年 天 日", "月 日 年"):
            atom["source_quote"] = bad_quote
            self.assertEqual(project_atom_metadata(response, self.records(response, chunk, ("source_quote",)), chunk), (None, []))

    def test_internal_atom_duplicate_occurrence_and_stale_source_are_rejected(self):
        for source, stale in (("日期：年    月    日；又一日期：年  月  日", False),
                              ("日期：年    月    日", True)):
            response, chunk = self.example()
            clause = chunk["clauses"][0]
            clause.update(text=source)
            clause["source_span"].update(text=source, end_offset=len(source),
                                        source_sha256="0" * 64 if stale else hashlib.sha256(source.encode()).hexdigest())
            chunk["evidence_context"]["evidence-random"]["text"] = source
            atom = response["clause_reviews"][0]["obligations"][0]
            atom.update(source_quote="年 月 日", route="automatic")
            self.assertEqual(project_atom_metadata(response, self.records(response, chunk, ("source_quote",)), chunk), (None, []))

    def test_numbered_context_recovers_only_missing_prefix_and_preserves_neighboring_text(self):
        response, chunk = self.numbered_incident()
        frozen = copy.deepcopy(response)
        old = response["clause_reviews"][0]["obligations"][0]["source_quote"]
        clause_map = {c["id"]: c for c in chunk["clauses"]}
        with self.assertRaises(ValueError):
            bind_atom_quote(old, chunk["clauses"][0]["id"], clause_map, chunk["evidence_context"])
        projected, audit = project_atom_metadata(response, self.records(response, chunk, ("source_quote",)), chunk)
        self.assertEqual(response, frozen)
        expected = copy.deepcopy(response)
        expected["clause_reviews"][0]["obligations"][0]["source_quote"] = "2. " + old
        self.assertEqual(projected, expected)
        self.assertEqual(len(audit), 1)
        recovery = audit[0]["quote_context_recovery"]
        self.assertEqual(recovery["restored_prefix"], "2. ")
        self.assertEqual(recovery["selected_quote_binding"]["clause_binding"]["text"], chunk["clauses"][0]["source_span"]["text"])
        self.assertTrue(recovery["context_is_not_execution_scope"])
        self.assertTrue(audit[0]["independent_review_required"])
        self.assertFalse(audit[0]["submission_ready"])
        self.assertEqual(project_atom_metadata(projected, self.records(projected, chunk, ("source_quote",)), chunk), (None, []))

    def test_numbered_context_retry_authorizes_no_other_semantics_or_source_edge(self):
        response, chunk = self.numbered_incident()
        records = self.records(response, chunk, ("source_quote",))
        projected, _ = project_atom_metadata(response, records, chunk)
        path = "$.clause_reviews[0].obligations[0].source_quote"
        self.assertTrue(bridge._retry_changes_allowed(records, [path], contract_version="3.0", previous_response=response, current_response=projected, chunk=chunk))
        for field, value in (("action", "submit only"), ("target", "neighbor duty"),
                             ("condition", None), ("status", "unverifiable"),
                             ("force", "optional"), ("id", "replacement")):
            changed = copy.deepcopy(projected)
            changed["clause_reviews"][0]["obligations"][0][field] = value
            with self.subTest(field=field):
                self.assertFalse(bridge._atom_metadata_retry_path_allowed(response, changed, records, path, chunk))
        changed = copy.deepcopy(projected)
        changed["clause_reviews"][0]["clause_id"] = chunk["clauses"][1]["id"]
        self.assertFalse(bridge._atom_metadata_retry_path_allowed(response, changed, records, path, chunk))

    def test_numbered_context_does_not_expand_independent_review_execution_scope(self):
        response, chunk = self.numbered_incident()
        projected, audit = project_atom_metadata(response, self.records(response, chunk, ("source_quote",)), chunk)
        request = native.build_obligation_coverage_request(projected, chunk, run_id="fresh-numbered-quote-test", chunk_index=1)
        check = next(c for c in request["checks"] if c["check_id"] == chunk["clauses"][0]["id"])
        selected = chunk["clauses"][0]["source_span"]["text"]
        self.assertEqual(check["document_text"], selected)
        atom = check["review_context"]["primary_obligations"][0]
        self.assertEqual(atom["source_quote"], selected)
        original = response["clause_reviews"][0]["obligations"][0]
        for k in ("actor", "action", "target", "condition", "force", "applicability", "status", "route"):
            self.assertEqual(atom[k], original[k])
        self.assertNotIn(chunk["clauses"][1]["source_span"]["text"], check["document_text"])
        self.assertTrue(audit[0]["independent_review_required"])

    def test_numbered_context_wrong_occurrence_stale_source_and_invented_words_are_rejected(self):
        for change in ("stale_hash", "wrong_evidence", "duplicate_clause", "stale_feedback",
                       "neighbor_only", "wrong_words", "duplicate_quote"):
            response, chunk = self.numbered_incident()
            atom = response["clause_reviews"][0]["obligations"][0]
            if change == "stale_hash": chunk["clauses"][0]["source_span"]["source_sha256"] = "0" * 64
            elif change == "wrong_evidence": chunk["clauses"][0]["evidence_ids"] = ["other"]
            elif change == "duplicate_clause": chunk["clauses"].append(copy.deepcopy(chunk["clauses"][0]))
            elif change == "neighbor_only": atom["source_quote"] = chunk["clauses"][1]["source_span"]["text"]
            elif change == "wrong_words": atom["source_quote"] = atom["source_quote"].replace("可以", "必须")
            elif change == "duplicate_quote":
                evidence = chunk["evidence_context"]["current-evidence"]
                evidence["text"] += atom["source_quote"]
                for clause in chunk["clauses"]:
                    clause["source_span"]["source_sha256"] = hashlib.sha256(evidence["text"].encode()).hexdigest()
            records = self.records(response, chunk, ("source_quote",))
            if change == "stale_feedback": records[0]["response_sha256"] = "0" * 64
            with self.subTest(change=change):
                self.assertEqual(project_atom_metadata(response, records, chunk), (None, []))

    def test_numbered_fringe_is_not_a_decimal_quantity_negation_or_semantic_prefix(self):
        for source, omitted in (("3.5 cm是高度；部门须审批。", "3."),
                                ("不得发布论文；部门须审批。", "不得"),
                                ("第二章说明内容；部门须审批。", "第二章"),
                                ("可能可以发布论文；部门须审批。", "可能")):
            response, chunk = self.example()
            selected = source.split("；")[0]
            clause = chunk["clauses"][0]
            clause.update(text=selected)
            clause["source_span"].update(text=selected, start_offset=0, end_offset=len(selected), source_sha256=hashlib.sha256(source.encode()).hexdigest())
            chunk["evidence_context"]["evidence-random"]["text"] = source
            response["clause_reviews"][0]["obligations"][0].update(source_quote=source[len(omitted):], route="automatic")
            with self.subTest(source=source):
                self.assertEqual(project_atom_metadata(response, self.records(response, chunk, ("source_quote",)), chunk), (None, []))


if __name__ == "__main__":
    unittest.main()
