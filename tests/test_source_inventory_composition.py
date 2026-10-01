from __future__ import annotations

import copy
import hashlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import host_agent_bridge as bridge
from host_review_contract import contract_error_records
from requirements_engine import build_llm_request
from semantic_contract import attach_request_provenance, sha256_json
from source_obligation_compiler import materialize_registered_abstract_quality_guidance


def fixture(count=4):
    clauses, evidence, requirements, reviews = [], [], [], []
    source = "中文摘要是论文内容的简要陈述，使用规范的学术用语"
    for i in range(count):
        cid, eid = f"dynamic-clause-{i}", f"dynamic-evidence-{i}"
        location = {"part": "document", "child_index": i, "order": i}
        evidence.append({"id": eid, "text": source, "kind": "paragraph", "location": location})
        clauses.append({"id": cid, "text": source, "evidence_ids": [eid], "location": location,
            "source_span": {"evidence_id": eid, "text": source, "start_offset": 0,
                "end_offset": len(source), "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                "location": location}})
        requirements.append({"role": "content_constraints", "clause_ids": [cid], "evidence_ids": [eid],
            "properties": {"abstract_zh": {"quality_guidance": ["academic_language"]}},
            "confidence": .9, "reason": "Current abstract quality guidance"})
        reviews.append({"clause_id": cid, "classification": "executable",
            "normative_basis": "explicit_normative_text", "reason": "explicit source rule", "obligations": []})
    chunk = build_llm_request([], clauses, {"evidence": evidence}, {}, "full", contract_version="3.0")
    chunk["batch"] = {"index": 1, "count": 1}
    chunk["case_id"] = "generic-inventory-composition"
    chunk["runtime_context"] = {"code_fingerprint_sha256": sha256_json({"test": "inventory"})}
    chunk = attach_request_provenance(chunk, source_sha256=sha256_json(evidence),
        evidence_doc={"evidence": evidence}, clauses=clauses, run_id="current-inventory-run")
    response = {"contract_version": "3.0", "provenance": chunk["provenance"], "requirements": requirements,
        "clause_reviews": reviews, "reported_conflicts": [], "unsupported_items": []}
    return response, chunk


def completion(response, chunk):
    completed = copy.deepcopy(response)
    for index, review in enumerate(completed["clause_reviews"]):
        review["obligations"] = [{"id": f"quality-{index}", "status": "covered",
            "reason": "Source abstract quality is represented", "action": "write", "target": "Chinese abstract",
            "source_quote": chunk["clauses"][index]["source_span"]["text"],
            "force": "required", "applicability": "applicable", "route": "human"}]
        # A model may insert set-valued flags in a different order.
        completed["requirements"][index]["properties"]["abstract_zh"]["quality_guidance"] = [
            "academic_language", "brief_statement_of_thesis_content"]
    return completed


class SourceInventoryCompositionTests(unittest.TestCase):
    def prepared_pair(self):
        raw, chunk = fixture()
        with self.assertRaises(ValueError) as raised:
            bridge.prepare_native_response_candidate(raw, chunk, source_projection_validation_sha256=sha256_json(chunk))
        error = raised.exception
        parent = error.stage_candidates[-1]["response"]
        records = error.retry_authorizing_error_records
        current, audit = bridge.prepare_native_response_candidate(completion(raw, chunk), chunk,
            source_projection_validation_sha256=sha256_json(chunk))
        return raw, chunk, parent, records, current, audit, error

    def test_compound_errors_have_one_source_bound_inventory_transition(self):
        raw, chunk, parent, records, current, audit, error = self.prepared_pair()
        self.assertEqual(len(error.abstract_quality_projections), 4)
        self.assertEqual(parent["clause_reviews"], raw["clause_reviews"])
        self.assertEqual(len(records), 8)  # four missing inventories + four derived schema reports
        changed = bridge._retry_change_paths(parent, current)
        self.assertEqual(changed, [f"$.clause_reviews[{i}].obligations" for i in range(4)])
        ledger = []
        failure, _ = bridge._retry_semantic_change_error(parent, current, records,
            contract_version="3.0", chunk=chunk, authorization_out=ledger)
        self.assertIsNone(failure)
        self.assertEqual(len(ledger), 4)
        self.assertTrue(all(r["rule_id"] == "v3_source_inventory_completion" for r in ledger))
        self.assertEqual(bridge.validate_host_agent_response(current, chunk), [])
        projected, receipt = bridge._project_validator_targeted_obligation_fields(parent, current, records, chunk=chunk)
        self.assertEqual(projected, current)
        self.assertEqual(receipt["status"], "projected")
        self.assertTrue(all(a["independent_review_required"] and not a["submission_ready"]
                            for a in audit["abstract_quality_projections"]))

    def test_complete_feedback_and_no_other_semantic_changes_are_required(self):
        _, chunk, parent, records, current, _, _ = self.prepared_pair()
        variants = []
        variants.append((current, records[1:], chunk))
        stale = copy.deepcopy(records); stale[0]["response_sha256"] = "0" * 64
        variants.append((current, stale, chunk))
        duplicate = [*records, records[0]]; variants.append((current, duplicate, chunk))
        for field, value in (("classification", "informational"), ("reason", "unrequested rewrite")):
            changed = copy.deepcopy(current); changed["clause_reviews"][0][field] = value
            variants.append((changed, records, chunk))
        for field, value in (("unknown_atom_field", True), ("force", "illegal_force"), ("status", "unverifiable")):
            changed = copy.deepcopy(current); changed["clause_reviews"][0]["obligations"][0][field] = value
            variants.append((changed, records, chunk))
        changed = copy.deepcopy(current); changed["requirements"][0]["properties"]["abstract_zh"]["min_chars"] = 500
        variants.append((changed, records, chunk))
        changed = copy.deepcopy(chunk); changed["evidence_context"]["dynamic-evidence-0"]["text"] += " changed"
        variants.append((current, records, changed))
        for candidate, feedback, source in variants:
            with self.subTest(candidate=candidate, feedback=feedback):
                self.assertFalse(bridge._v3_source_inventory_completion_allowed(parent, candidate, feedback,
                    bridge._retry_change_paths(parent, candidate), chunk=source))

    def test_nonempty_inventory_cannot_be_replaced_as_missing(self):
        _, chunk, _, _, current, _, _ = self.prepared_pair()
        parent = copy.deepcopy(current)
        parent["clause_reviews"][0]["obligations"][0]["reason"] = "original obligation"
        records = [{"code": "executable_review_obligations_missing", "json_pointer": "$.clause_reviews[0].obligations",
            "clause_id": "dynamic-clause-0", "response_sha256": bridge._response_sha256(parent)}]
        self.assertFalse(bridge._v3_source_inventory_completion_allowed(parent, current, records,
            ["$.clause_reviews[0].obligations"], chunk=chunk))
        self.assertIsNone(bridge._project_validator_targeted_obligation_fields(parent, current, records, chunk=chunk)[0])

    def test_quality_projection_preserves_review_payload_and_is_idempotent(self):
        raw, chunk = fixture(1)
        original = copy.deepcopy(raw)
        projected, audit = materialize_registered_abstract_quality_guidance(raw, chunk["clauses"],
            evidence_context=chunk["evidence_context"])
        self.assertEqual(raw, original)
        self.assertEqual(projected["clause_reviews"], raw["clause_reviews"])
        self.assertEqual(audit[0]["original_requirement"], original["requirements"][0])
        self.assertEqual(audit[0]["added_quality_guidance"], ["brief_statement_of_thesis_content"])
        self.assertEqual(audit[0]["source_sha256"], chunk["clauses"][0]["source_span"]["source_sha256"])
        again, second = materialize_registered_abstract_quality_guidance(projected, chunk["clauses"],
            evidence_context=chunk["evidence_context"])
        self.assertEqual((again, second), (projected, []))

    def test_registered_long_source_is_projected_without_complete_bundle(self):
        raw, chunk = fixture(1)
        text = ("中文摘要是论文内容的简要陈述，一般以第三人称语气撰写，300～1000字"
                "（如遇特殊需要字数可以略多），不加评论和解释,是一篇具有独立性和完整性的短文，"
                "能准确反映论文的中心思想，规范的学术用语，逻辑性强、结构严谨，"
                "体现出论文的新理论、新方法、新技术等")
        clause = chunk["clauses"][0]
        eid = clause["evidence_ids"][0]
        chunk["evidence_context"][eid]["text"] = text
        clause["text"] = text
        clause["source_span"].update(text=text, end_offset=len(text),
            source_sha256=hashlib.sha256(text.encode()).hexdigest())
        candidate, audit = materialize_registered_abstract_quality_guidance(raw, chunk["clauses"],
            evidence_context=chunk["evidence_context"])
        self.assertEqual(len(audit), 1)
        self.assertEqual(len(candidate["requirements"][0]["properties"]["abstract_zh"]["quality_guidance"]), 7)
        self.assertEqual(candidate["clause_reviews"], raw["clause_reviews"])
        self.assertNotIn("min_chars", candidate["requirements"][0]["properties"]["abstract_zh"])

    def test_unknown_ambiguous_or_unbound_sources_are_not_compiled(self):
        raw, chunk = fixture(1)
        variants = []
        for text in ("英文摘要是论文内容的简要陈述", "中文摘要不是论文内容的简要陈述",
                     "中文摘要是示例：论文内容的简要陈述", "中文摘要是论文内容的简要陈述，如果导师同意",
                     "中文摘要是“论文内容的简要陈述”", "中文摘要是论文内容的简要陈述，英文摘要同上",
                     "中文摘要是论文内容的简要陈述，不使用规范的学术用语",
                     "中文摘要是论文内容的简要陈述，未要求使用规范的学术用语",
                     "中文摘要是论文内容的简要陈述，如导师认可则使用规范的学术用语",
                     "中文摘要是论文内容的简要陈述，在必要时体现准确反映论文的中心思想",
                     "中文摘要是「论文内容的简要陈述」", "中文摘要是『论文内容的简要陈述』",
                     "中文摘要是论文内容的简要陈述，并要求论文采用逻辑性强的论证",
                     "中文摘要是论文内容的简要陈述，正文需满足以下要求，逻辑性强"):
            source = copy.deepcopy(chunk)
            clause = source["clauses"][0]; eid = clause["evidence_ids"][0]
            source["evidence_context"][eid]["text"] = text
            clause["text"] = text
            clause["source_span"].update(text=text, end_offset=len(text), source_sha256=hashlib.sha256(text.encode()).hexdigest())
            variants.append((raw, source))
        stale = copy.deepcopy(chunk); stale["clauses"][0]["source_span"]["source_sha256"] = "0" * 64
        variants.append((raw, stale))
        wrong = copy.deepcopy(chunk); wrong["evidence_context"]["dynamic-evidence-0"]["id"] = "other"
        variants.append((raw, wrong))
        duplicate = copy.deepcopy(chunk); duplicate["clauses"].append(copy.deepcopy(duplicate["clauses"][0]))
        variants.append((raw, duplicate))
        for kind in ("no_parent", "two_targets", "unresolved", "conflict", "unknown_flag", "selector"):
            candidate = copy.deepcopy(raw)
            if kind == "no_parent": candidate["requirements"] = []
            elif kind == "two_targets": candidate["requirements"].append(copy.deepcopy(candidate["requirements"][0]))
            elif kind == "unresolved": candidate["clause_reviews"][0]["classification"] = "unresolved"
            elif kind == "conflict": candidate["reported_conflicts"] = [{"reason": "competing rule"}]
            elif kind == "unknown_flag": candidate["requirements"][0]["properties"]["abstract_zh"]["quality_guidance"] = ["unknown"]
            else: candidate["requirements"][0]["selector"] = {"text": "different target"}
            variants.append((candidate, chunk))
        for candidate, source in variants:
            with self.subTest(source=source, candidate=candidate):
                projected, audit = materialize_registered_abstract_quality_guidance(candidate, source["clauses"],
                    evidence_context=source["evidence_context"])
                self.assertEqual((projected, audit), (candidate, []))


if __name__ == "__main__":
    unittest.main()
