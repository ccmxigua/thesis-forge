"""Descriptions cannot manufacture missing-content facts or erase source duties."""
from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import host_agent_bridge as bridge
import native_semantic_review as native
import source_obligation_compiler as compiler
from section_description import compile_section_description, project_section_description_claims
import test_host_agent_bridge as fixtures


class SectionDescriptionTests(unittest.TestCase):
    def fixture(self, directory, source="这部分是论文的摘要"):
        _, chunk = fixtures.HostAgentBridgeTests()._packet(Path(directory), contract_version="3.0", source=source)
        cid = chunk["clauses"][0]["id"]
        candidate = {"contract_version": "3.0", "provenance": chunk["provenance"],
                     "requirements": [], "unsupported_items": [], "reported_conflicts": [],
                     "clause_reviews": [{"clause_id": cid, "classification": "requires_source_content",
                         "normative_basis": "insufficient", "reason": "The model infers missing content.",
                         "obligations": [{"id": "model-input-claim", "status": "requires_source_content",
                             "reason": "Supply content.", "actor": "Author", "action": "Supply content.",
                             "target": "Abstract", "source_quote": source, "force": "required",
                             "applicability": "applicable", "route": "input"}]}]}
        return candidate, chunk

    def test_dynamic_bound_projection_preserves_full_audit_and_graph(self):
        with tempfile.TemporaryDirectory() as td:
            raw, chunk = self.fixture(td)
            original = copy.deepcopy(raw)
            candidate, audit = bridge.prepare_native_response_candidate(raw, chunk)
            records = audit["section_description_projections"]
            self.assertEqual(raw, original)
            self.assertEqual(candidate["requirements"], raw["requirements"])
            self.assertEqual(candidate["clause_reviews"][0]["classification"], "informational")
            self.assertEqual(candidate["clause_reviews"][0]["obligations"], [])
            self.assertEqual(records[0]["before_review"], original["clause_reviews"][0])
            self.assertEqual(records[0]["provenance"], chunk["provenance"])
            self.assertFalse(records[0]["submission_ready"])
            self.assertTrue(records[0]["requires_independent_review"])
            self.assertEqual(records[0]["source_fact"]["manuscript_presence"], "not_assessed")
            self.assertEqual(bridge.validate_host_agent_response(candidate, chunk), [])
            self.assertEqual(project_section_description_claims(candidate, chunk), (candidate, []))
            request = native.build_obligation_coverage_request(candidate, chunk, run_id=chunk["provenance"]["run_id"], chunk_index=1)
            check = request["checks"][0]
            self.assertEqual(check["review_context"]["primary_obligations"], [])
            self.assertIsNotNone(check["review_context"]["section_description"])
            result = {"results": [{"check_id": check["check_id"], "verdict": "consistent",
                      "rationale": "The source describes the section without a duty.",
                      "evidence_quotes": [check["document_text"]], "machine_obligation_ids": [],
                      "identified_obligations": []}]}
            self.assertEqual(native.validate_obligation_coverage_response(result, request["checks"])[0]["verdict"], "consistent")
            forged = copy.deepcopy(result)
            forged["results"][0].update(verdict="source_content_pending", identified_obligations=[{
                "source_quote": check["document_text"], "disposition": "authoring_content_pending", "requirement_refs": []}])
            with self.assertRaisesRegex(native.NativeSemanticReviewError, "explicit source authoring instruction"):
                native.validate_obligation_coverage_response(forged, request["checks"])

    def test_closed_grammar_rejects_instructions_mixed_duties_and_unknown_text(self):
        for source in ("这部分是论文的摘要，作者须补写内容", "这部分是论文的摘要，应为300字",
                       "如果提供资料，本节是论文的引言", "示例：这部分是论文的摘要",
                       "“这部分是论文的摘要”", "这部分是论文的摘要\n使用宋体", "这部分需要提供论文摘要",
                       "本节是论文的研究现状", "本节是论文的摘要（请补充）"):
            with self.subTest(source=source):
                self.assertIsNone(compile_section_description(source))
        for source in ("本节是论文的引言。", "这一部分是本论文的英文摘要", "本章是论文的结论"):
            with self.subTest(source=source):
                self.assertIsNotNone(compile_section_description(source))

    def test_invalid_stale_mixed_or_executable_claim_is_not_projected(self):
        with tempfile.TemporaryDirectory() as td:
            raw, chunk = self.fixture(td)
            variants = []
            stale = copy.deepcopy(chunk)
            stale["clauses"][0]["source_span"]["source_sha256"] = "0" * 64
            variants.append((raw, stale))
            extra_source = copy.deepcopy(chunk)
            extra_source["evidence_context"]["E1"]["text"] += "，作者应撰写内容"
            variants.append((raw, extra_source))
            for field, value in (("condition", "If not supplied"), ("status", "unresolved"),
                                 ("source_quote", "wrong source"), ("route", "automatic"),
                                 ("bogus", True)):
                altered = copy.deepcopy(raw)
                altered["clause_reviews"][0]["obligations"][0][field] = value
                variants.append((altered, chunk))
            duplicates = copy.deepcopy(raw)
            duplicates["clause_reviews"] *= 2
            variants.append((duplicates, chunk))
            duplicate_atoms = copy.deepcopy(raw)
            duplicate_atoms["clause_reviews"][0]["obligations"] *= 2
            variants.append((duplicate_atoms, chunk))
            conflicted = copy.deepcopy(raw)
            conflicted["reported_conflicts"] = [{"clause_ids": [chunk["clauses"][0]["id"]]}]
            variants.append((conflicted, chunk))
            for key in ("requirements", "catalog"):
                altered, altered_chunk = copy.deepcopy(raw), copy.deepcopy(chunk)
                target = altered["requirements"] if key == "requirements" else altered_chunk.setdefault("rule_spec", {}).setdefault("requirements", [])
                target.append({"clause_ids": [chunk["clauses"][0]["id"]], "role": "abstract_zh"})
                variants.append((altered, altered_chunk))
            for index, (candidate, packet) in enumerate(variants):
                with self.subTest(index=index):
                    self.assertEqual(project_section_description_claims(candidate, packet), (candidate, []))

    def test_existing_keyword_selection_keeps_typed_semantics_as_human_work(self):
        source = "关键词须源自论文，并在论文中有明确出处"
        with tempfile.TemporaryDirectory() as td:
            raw, chunk = self.fixture(td, source)
            review = raw["clause_reviews"][0]
            review["normative_basis"] = "explicit_normative_text"
            atom = review["obligations"][0]
            atom.update(actor="Thesis author or source-content provider",
                        action="Select thesis-derived keywords with clear support in the thesis.",
                        target="Chinese keywords.")
            original = copy.deepcopy(raw)
            candidate, audit = bridge.prepare_native_response_candidate(raw, chunk)
            corrected = candidate["clause_reviews"][0]
            self.assertEqual(raw, original)
            self.assertEqual(audit["section_description_projections"], [])
            self.assertEqual(corrected["classification"], "requires_source_verification")
            for key in ("id", "actor", "action", "target", "source_quote", "force", "applicability"):
                self.assertEqual(corrected["obligations"][0][key], atom[key])
            self.assertEqual(corrected["obligations"][0]["route"], "human")
            self.assertEqual(corrected["obligations"][0]["status"], "unresolved")
            self.assertTrue(audit["source_verification_classification_projections"][0]["typed_inventory_preserved"])
            for field, value in (("action", "Write new thesis-derived keywords with clear support in the thesis."),
                                 ("target", "Chinese keywords and approval number."),
                                 ("condition", "If no thesis was supplied"),
                                 ("force", "optional"), ("actor", "University approver")):
                forged = copy.deepcopy(atom)
                forged[field] = value
                with self.subTest(field=field):
                    self.assertFalse(compiler.typed_source_verification_inventory_is_bound(
                        source, [forged], expected_status="requires_source_content"))
            self.assertFalse(compiler.typed_source_verification_inventory_is_bound(
                source + "，作者应补写摘要", [atom], expected_status="requires_source_content"))


if __name__ == "__main__":
    unittest.main()
