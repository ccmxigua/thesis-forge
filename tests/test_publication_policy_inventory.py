"""Source-bound policy proposals and real offline bridge orchestration."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import host_agent_bridge as bridge
import native_semantic_review as native
import requirements_engine as engine
from publication_policy_inventory import CODE, RULE_ID, policy_inventory_retry_ledger
from semantic_contract import attach_request_provenance, sha256_json
import test_host_agent_bridge as host_tests


POLICY = "未经批准的均为公开学位论文（公开的学位论文本项为空白）"
APPROVAL = "非公开学位论文须经指导教师同意、作者本人申请和相关部门批准方能标注"


def fixture(policy=POLICY):
    source = APPROVAL + "。" + policy
    evidence = {"evidence": [
        {"id": "current-prose", "text": source},
        {"id": "current-field", "text": "申请密级"},
    ]}
    clauses = []
    for cid, eid, text, start in (
        ("neighbor-approval", "current-prose", APPROVAL, 0),
        ("selected-policy", "current-prose", policy, len(APPROVAL) + 1),
        ("security-label", "current-field", "申请密级", 0),
    ):
        full = next(e["text"] for e in evidence["evidence"] if e["id"] == eid)
        clauses.append({"id": cid, "text": text, "evidence_ids": [eid],
            "source_span": {"evidence_id": eid, "text": text,
                "start_offset": start, "end_offset": start + len(text),
                "source_sha256": hashlib.sha256(full.encode()).hexdigest()}})
    chunk = engine.build_llm_request([], clauses, evidence, {}, "full", contract_version="3.0")
    chunk.update(case_id="dynamic-policy-test", batch={"index": 1, "count": 1},
                 runtime_context={"code_fingerprint_sha256": sha256_json("policy-test")})
    chunk = attach_request_provenance(chunk, source_sha256=sha256_json(evidence),
        evidence_doc=evidence, clauses=clauses, run_id="current-policy-test")
    applicability = {"status": "conditional", "conditions": [{
        "fact": "thesis_profile.security_level", "operator": "in",
        "value": ["restricted", "classified"]}]}
    requirement = {"role": "cover", "properties": {
        "institution": "——", "fields": [], "before_role": "document_start",
        "layout_id": "linear", "missing_value_policy": "placeholder",
        "missing_value_placeholder": "——", "non_public_administration": {
            "applicability": applicability, "public_policy": "blank",
            "publication_default_policy": "unapproved_is_public", "source_region": "current field",
            "fields": [{"id": "security_marking", "label": "申请密级",
                "value_from": "thesis_profile.cover_metadata.security_marking",
                "display_policy": "if_present", "order": 1}]}},
        "clause_ids": ["selected-policy", "security-label"],
        "evidence_ids": ["current-prose", "current-field"], "confidence": .9,
        "reason": "The current policy and printed field are represented."}

    def atom(identity, status, quote, action, target, **extra):
        return {"id": identity, "status": status, "reason": "Source-linked model proposal",
            "action": action, "target": target, "source_quote": quote,
            "force": "required", "applicability": "applicable",
            "route": "human" if status == "unverifiable" else "automatic", **extra}

    reviews = [{"clause_id": "neighbor-approval", "classification": "external_compliance",
        "normative_basis": "external_duty", "reason": "Actual consent, application and approval are pending.",
        "obligations": [
            atom("consent", "unverifiable", "指导教师同意", "consent", "non-public marking", actor="指导教师"),
            atom("application", "unverifiable", "作者本人申请", "apply", "non-public marking", actor="作者本人"),
            atom("approval", "unverifiable", "相关部门批准", "approve", "non-public marking", actor="相关部门")]},
        {"clause_id": "selected-policy", "classification": "executable_with_external_check",
         "normative_basis": "explicit_normative_text", "reason": "Incorrectly duplicates an approval action.",
         "obligations": [
             atom("public-policy", "covered", "未经批准的均为公开学位论文", "treat as public", "unapproved theses"),
             atom("blank-policy", "covered", "公开的学位论文本项为空白", "leave blank", "administrative item", condition="thesis is public"),
             atom("invented-approval", "unverifiable", "未经批准", "approve", "non-public marking", actor="相关部门")]},
        {"clause_id": "security-label", "classification": "executable",
         "normative_basis": "explicit_normative_text", "reason": "Printed administrative field.",
         "obligations": [atom("label", "covered", "申请密级", "preserve", "field label")]}]
    response = {"contract_version": "3.0", "provenance": chunk["provenance"],
        "requirements": [requirement], "clause_reviews": reviews,
        "reported_conflicts": [], "unsupported_items": []}
    return response, chunk, evidence


def proposed(response):
    candidate = copy.deepcopy(response)
    review = candidate["clause_reviews"][1]
    review["obligations"] = review["obligations"][:2]
    review["classification"] = "executable"
    review["reason"] = "The selected clause states two policies, not a new approval action."
    return candidate


class PublicationPolicyInventoryTests(unittest.TestCase):
    def records(self, old, chunk):
        return bridge.contract_error_records(bridge.validate_host_agent_response(old, chunk),
            response=old, chunk=chunk)

    def proof(self, old, new, chunk, records):
        return policy_inventory_retry_ledger(old, new, records,
            bridge._retry_change_paths(old, new), chunk,
            validate=bridge.validate_host_agent_response, make_records=bridge.contract_error_records)

    def test_source_condition_is_rejected_before_independent_acceptance(self):
        old, chunk, _ = fixture(); frozen = copy.deepcopy((old, chunk))
        records = self.records(old, chunk)
        self.assertEqual([r["code"] for r in records], [CODE])
        self.assertEqual(records[0]["clause_id"], "selected-policy")
        self.assertEqual(records[0]["source_chunk_sha256"], sha256_json(chunk))
        self.assertTrue(records[0]["semantic_review_required"])
        with self.assertRaises(ValueError): bridge.prepare_native_response_candidate(old, chunk)
        self.assertEqual((old, chunk), frozen)

    def test_bounded_primary_proposal_preserves_actual_neighbor_duties(self):
        old, chunk, _ = fixture(); new = proposed(old)
        self.assertEqual(bridge.validate_host_agent_response(new, chunk), [])
        proofs = self.proof(old, new, chunk, self.records(old, chunk))
        self.assertEqual(len(proofs), 3)
        self.assertTrue(all(p["rule_id"] == RULE_ID and p["independent_review_required"]
                            and not p["submission_ready"] and not p["mechanical_equivalence_claimed"] for p in proofs))
        self.assertEqual(proofs[0]["rejected_model_atoms"], old["clause_reviews"][1]["obligations"][2:])
        self.assertEqual(new["requirements"], old["requirements"])
        self.assertEqual(new["clause_reviews"][0], old["clause_reviews"][0])
        authorized = []
        error, _ = bridge._retry_semantic_change_error(old, new, self.records(old, chunk),
            contract_version="3.0", chunk=chunk, authorization_out=authorized)
        self.assertIsNone(error); self.assertEqual(authorized, proofs)

    def test_complete_current_feedback_and_run_are_required(self):
        old, chunk, _ = fixture(); new = proposed(old); records = self.records(old, chunk)
        for defect in ("empty", "duplicate", "code-only", "old-response", "old-source", "run", "span", "evidence", "case", "mixed-error"):
            source = copy.deepcopy(chunk); feedback = copy.deepcopy(records)
            if defect == "empty": feedback = []
            elif defect == "duplicate": feedback *= 2
            elif defect == "code-only": feedback = [{"code": CODE}]
            elif defect == "old-response": feedback[0]["response_sha256"] = "0" * 64
            elif defect == "old-source": feedback[0]["source_chunk_sha256"] = "0" * 64
            elif defect == "run": source["provenance"]["run_id"] = "old-run"
            elif defect == "span": source["clauses"][1]["source_span"]["source_sha256"] = "0" * 64
            elif defect == "evidence": source["evidence_context"]["current-prose"]["text"] += " changed"
            elif defect == "case": source["case_id"] = "different-case"
            else: feedback.append({"code": "unrelated_failure"})
            with self.subTest(defect=defect): self.assertIsNone(self.proof(old, new, source, feedback))

    def test_unauthorized_changes_cannot_hide_in_policy_proposal(self):
        old, chunk, _ = fixture(); records = self.records(old, chunk)
        for defect in ("covered-atom", "covered-order", "delete-policy", "neighbor", "requirement", "source-edge", "informational", "reason", "add-atom"):
            new = proposed(old)
            if defect == "covered-atom": new["clause_reviews"][1]["obligations"][0]["target"] = "other target"
            elif defect == "covered-order": new["clause_reviews"][1]["obligations"].reverse()
            elif defect == "delete-policy": new["clause_reviews"][1]["obligations"].pop()
            elif defect == "neighbor": new["clause_reviews"][0]["obligations"].pop()
            elif defect == "requirement": new["requirements"][0]["reason"] += " changed"
            elif defect == "source-edge": new["requirements"][0]["evidence_ids"] = ["current-field"]
            elif defect == "informational": new["clause_reviews"][1]["classification"] = "informational"
            elif defect == "reason": new["clause_reviews"][1]["reason"] = " "
            else: new["clause_reviews"][1]["obligations"].append(copy.deepcopy(old["clause_reviews"][1]["obligations"][2]))
            with self.subTest(defect=defect): self.assertIsNone(self.proof(old, new, chunk, records))

    def test_unrecognized_mixed_or_quoted_sources_do_not_authorize_removal(self):
        for source in ("示例：" + POLICY, "“" + POLICY + "”", POLICY + "但必须经部门批准",
                       "未经批准的均为公开学位论文", "公开的学位论文本项为空白"):
            old, chunk, _ = fixture(source)
            records = self.records(old, chunk)
            self.assertFalse(any(r["code"] == CODE for r in records))
            self.assertIsNone(self.proof(old, proposed(old), chunk, [{"code": CODE}]))

    def _orchestration(self, reject_review=False, bad_proposal=False, repair_base=False):
        old, source, evidence = fixture(); calls = []; reviews = []
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td) / "packet"; directory.mkdir()
            engine.prepare_host_agent_review_packets(source, source["clauses"], evidence,
                sha256_json(evidence), directory, chunk_size=100)
            chunk = json.loads((directory / "llm-request-chunks.json").read_text())[0]
            def primary(_command, **kwargs):
                calls.append(kwargs.get("input", ""))
                body = copy.deepcopy(old) if len(calls) == 1 else proposed(old)
                body["provenance"] = chunk["provenance"]
                if repair_base and len(calls) == 1:
                    # The model-facing parent must be the compiled repair base,
                    # not decoded raw missing a code-owned source fact.
                    body["requirements"][0]["properties"]["non_public_administration"].pop("publication_default_policy")
                    body["requirements"][0]["source_fragment_clause_ids"] = None
                if bad_proposal and len(calls) > 1: body["clause_reviews"][0]["obligations"].pop()
                return subprocess.CompletedProcess(["mock-host"], 0, json.dumps({"runId": "offline-policy",
                    "status": "ok", "provider": "offline-test", "model": "policy-test",
                    "result": {"payloads": [{"text": json.dumps(body)}]}}), "")
            def independent(candidate, current, **kwargs):
                reviews.append(kwargs["attempt"])
                self.assertEqual(candidate["clause_reviews"][0], old["clause_reviews"][0])
                self.assertEqual(candidate["clause_reviews"][1]["obligations"], old["clause_reviews"][1]["obligations"][:2])
                if reject_review:
                    error = bridge.IndependentObligationReviewError("fresh reviewer rejects the proposal")
                    error.retryable = False
                    raise error
                return host_tests.HostAgentBridgeTests._fake_independent_review(candidate, current, **kwargs)
            output = Path(td) / "merged.json"
            with patch.dict(os.environ, {"THESIS_FORGE_HOST_RUNTIME": "openclaw"}), \
                 patch.object(bridge, "_run_command", side_effect=primary), \
                 patch.object(bridge, "_run_independent_obligation_coverage_review", side_effect=independent):
                if reject_review or bad_proposal:
                    with self.assertRaises((ValueError, bridge.IndependentObligationReviewError)):
                        bridge.run_bridge(directory, response_out=output, agent_id="main", timeout=1,
                            max_attempts=2, openclaw_bin="mock-host", model="offline-test/policy-test")
                else:
                    bridge.run_bridge(directory, response_out=output, agent_id="main", timeout=1,
                        max_attempts=2, openclaw_bin="mock-host", model="offline-test/policy-test")
            audit = json.loads((directory / "host-agent-run.json").read_text())
            self.assertEqual(output.exists(), not (reject_review or bad_proposal))
            return audit, calls, reviews

    def test_production_retry_bridge_requires_fresh_review_and_records_proposal(self):
        audit, calls, reviews = self._orchestration()
        self.assertEqual(len(calls), 2); self.assertEqual(reviews, [2])
        self.assertEqual(audit["status"], "merged")
        self.assertEqual(audit["chunk_runs"][0]["semantic_retry_change_policy"], RULE_ID)

    def test_fresh_review_rejection_cannot_publish_or_merge(self):
        audit, calls, reviews = self._orchestration(reject_review=True)
        self.assertEqual(len(calls), 2); self.assertEqual(reviews, [2])
        self.assertEqual(audit["status"], "failed")
        self.assertFalse(audit["merged_response_written"])

    def test_repair_base_proposal_compares_the_receipt_verified_parent_stage(self):
        audit, calls, reviews = self._orchestration(repair_base=True)
        self.assertEqual(len(calls), 2); self.assertEqual(reviews, [2])
        self.assertEqual(audit["status"], "merged")
        comparison = audit["chunk_runs"][0]["retry_stage_comparison"]
        self.assertEqual(comparison["semantic_parent_stage"], "unaccepted_repair_base")
        self.assertEqual(audit["chunk_runs"][0]["semantic_retry_change_policy"], RULE_ID)
        self.assertIn("original_raw_observation", comparison)

    def test_multiple_rejected_atoms_are_preserved_in_authorization_audit(self):
        old, chunk, _ = fixture()
        rejected = copy.deepcopy(old["clause_reviews"][1]["obligations"][-1])
        rejected.update(id="another-invented-action", actor="author", action="apply")
        old["clause_reviews"][1]["obligations"].append(rejected)
        records = self.records(old, chunk)
        proof = self.proof(old, proposed(old), chunk, records)
        self.assertIsNotNone(proof)
        self.assertEqual(len(proof[0]["rejected_model_atoms"]), 2)

    def test_bridge_unauthorized_neighbor_change_does_not_reach_reviewer(self):
        audit, calls, reviews = self._orchestration(bad_proposal=True)
        self.assertEqual(len(calls), 2); self.assertEqual(reviews, [])
        self.assertEqual(audit["status"], "failed")
        self.assertFalse(audit["merged_response_written"])


if __name__ == "__main__":
    unittest.main()
