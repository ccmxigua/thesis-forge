"""Offline observed disagreement, production replay and non-release consumers."""
from __future__ import annotations

import copy
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import native_semantic_review as native
import host_agent_bridge as bridge
import thesis_format_pipeline as pipeline
from source_inventory_dispute import inventory_existence_dispute, build_inventory_existence_disputes
from semantic_contract import sha256_json, request_body_sha256
from tests import test_empty_inventory_verdict as transport
from tests import test_independent_retry_scope as wire
from tests import test_host_agent_bridge as packets
from tests import test_manual_review as manual_fixtures
from manual_review import build_manual_review_ledger
from manual_review_display import append_manual_review_markers, audit_manual_review_markers
from draft_scorecard import build_scorecard, append_scorecard, audit_scorecard
from format_spec_validation import load_and_validate
from docx import Document


class InventoryDisputeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads((ROOT / "tests/fixtures/source-inventory-existence-dispute.json").read_text())
        self.check = self.fixture["checks"][0]
        self.result = self.fixture["response"]["results"][0]

    def validate(self, check=None, result=None, *, draft=True):
        return native.validate_obligation_coverage_response(
            {"results": [copy.deepcopy(result or self.result)]}, [copy.deepcopy(check or self.check)],
            allow_draft_disputes=draft,
        )

    def test_observed_no_duty_assessment_retained_without_rewriting_either_party(self):
        before = copy.deepcopy((self.check, self.result))
        self.assertEqual(self.validate(), [self.result])
        dispute = inventory_existence_dispute(self.check, self.result)
        self.assertIsNotNone(dispute)
        self.assertEqual(dispute["primary_review_context"], self.check["review_context"])
        self.assertEqual(dispute["independent_result"], self.result)
        self.assertFalse(dispute["coverage_complete"])
        self.assertFalse(dispute["execution_authorized"])
        self.assertFalse(dispute["submission_ready"])
        self.assertEqual(before, (self.check, self.result))
        with self.assertRaises(native.NativeSemanticReviewError): self.validate(draft=False)

    def test_bad_bindings_inventories_and_structural_errors_are_not_disputes(self):
        def binding(c): return c["review_context"]["primary_obligation_quote_bindings"][0]
        mutations = {
            "old hash": lambda c,r: binding(c)["source_binding"]["clause_binding"]["source_fragments"][0].update(source_sha256="0"*64),
            "wrong evidence": lambda c,r: binding(c)["source_binding"]["clause_binding"]["source_fragments"][0].update(evidence_id="foreign"),
            "wrong occurrence": lambda c,r: binding(c)["source_binding"].update(quote_start_offset=1),
            "wrong literal": lambda c,r: c["review_context"]["primary_obligations"][0].update(source_quote="日期"),
            "duplicate primary": lambda c,r: c["review_context"]["primary_obligations"].append(copy.deepcopy(c["review_context"]["primary_obligations"][0])),
            "missing primary": lambda c,r: c["review_context"].update(primary_obligations=[]),
            "covered": lambda c,r: c["review_context"]["primary_obligations"][0].update(status="covered"),
            "automatic": lambda c,r: c["review_context"]["primary_obligations"][0].update(route="automatic"),
            "unknown force": lambda c,r: c["review_context"]["primary_obligations"][0].update(force="unknown"),
            "mixed requirement": lambda c,r: c["review_context"].update(linked_requirements=[{"requirement_ref":"rr"}]),
            "machine fact": lambda c,r: c["review_context"].update(machine_obligation_ids=["known"]),
            "empty rationale": lambda c,r: r.update(rationale=" "),
            "foreign quote": lambda c,r: r.update(evidence_quotes=["unrelated"]),
            "foreign clause": lambda c,r: r.update(check_id="foreign"),
            "uncertain empty": lambda c,r: r.update(verdict="uncertain"),
            "fake field": lambda c,r: r.update(submission_ready=True),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                check, result = copy.deepcopy(self.check), copy.deepcopy(self.result)
                mutate(check, result)
                with self.assertRaises(native.NativeSemanticReviewError): self.validate(check, result)
        with self.assertRaises(native.NativeSemanticReviewError):
            native.validate_obligation_coverage_response({"results":[self.result,self.result]}, [self.check], allow_draft_disputes=True)

    def test_invalid_sibling_not_hidden_by_dispute(self):
        check, result = copy.deepcopy(self.check), copy.deepcopy(self.result)
        check["check_id"] = result["check_id"] = "sibling"
        result["evidence_quotes"] = ["bad"]
        with self.assertRaises(native.NativeSemanticReviewError):
            native.validate_obligation_coverage_response({"results":[self.result,result]}, [self.check,check], allow_draft_disputes=True)

    def test_envelope_reconstructs_dispute_and_blocks_forged_complete_or_policy(self):
        disputes = build_inventory_existence_disputes([self.check], [self.result])
        request = {"checks":[self.check],"output_policy":"review_draft"}
        envelope = {"results":[self.result],"source_inventory_disputes":disputes,
                    "status":"completed_with_disputes","coverage_complete":False,"submission_ready":False}
        native.validate_draft_dispute_envelope(envelope,request,output_policy="review_draft")
        for key,value in (("status","completed"),("coverage_complete",True),("submission_ready",True),
                          ("source_inventory_disputes",[]),("results",[])):
            forged=copy.deepcopy(envelope);forged[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):
                native.validate_draft_dispute_envelope(forged,request,output_policy="review_draft")
        for policy in ("submission","supported_subset"):
            with self.subTest(policy=policy),self.assertRaises(ValueError):
                native.validate_draft_dispute_envelope(envelope,request,output_policy=policy)
        with self.assertRaises(ValueError):
            pipeline.enforce_obligation_review_output_policy([self.result],output_policy="submission",source_inventory_disputes=disputes)

    def make_packet(self,directory,source):
        _,chunk=packets.HostAgentBridgeTests()._packet(directory,source=source,contract_version="3.0")
        context=copy.deepcopy(self.check["review_context"])
        atom=context["primary_obligations"][0]
        atom.update(id="arbitrary-human-duty",source_quote=source,action="Complete this field",target="Form field")
        candidate={"contract_version":"3.0","provenance":chunk["provenance"],"requirements":[],
            "clause_reviews":[{"clause_id":"C1","classification":"external_compliance","normative_basis":"external_duty",
                               "reason":"Primary proposes a human duty.","obligations":[atom]}],
            "unsupported_items":[],"reported_conflicts":[]}
        request=native.build_obligation_coverage_request(candidate,chunk,run_id=chunk["provenance"]["run_id"],chunk_index=1)
        request.update(attempt=1,provider_attempt=1,output_policy="review_draft")
        result={**copy.deepcopy(self.result),"check_id":"C1","evidence_quotes":[source],
                "rationale":"The source is a label without an explicit action."}
        return chunk,candidate,request,result

    def test_general_nondate_sources_and_current_known_facts(self):
        for source,allowed in (
                ("联系电话：",True),("Reviewer code:",True),
                ("未经批准的均为公开学位论文（公开的学位论文本项为空白）",False),
                ("关键词须源自论文",False),
                ("The following English is not correct.",False)):
            with self.subTest(source=source),tempfile.TemporaryDirectory() as tmp:
                chunk,candidate,request,result=self.make_packet(Path(tmp),source)
                if allowed:
                    self.assertIsNotNone(inventory_existence_dispute(request["checks"][0],result))
                    native.validate_obligation_coverage_response({"results":[result]},request["checks"],allow_draft_disputes=True)
                else:
                    self.assertIsNone(inventory_existence_dispute(request["checks"][0],result))
                    with self.assertRaises(native.NativeSemanticReviewError):
                        native.validate_obligation_coverage_response({"results":[result]},request["checks"],allow_draft_disputes=True)

    def test_production_bridge_both_consumers_manual_docx_and_resealed_tamper(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp)/"packet"
            chunk,candidate,request,result=self.make_packet(directory,"年    月    日")
            before=copy.deepcopy(candidate)
            raw=transport.enveloped(wire.RetryScopeTests().wire({"results":[result]},request))
            with ExitStack() as stack:
                transport.EmptyInventoryVerdictTests().mock_transport(stack,[raw])
                pointer=bridge._run_independent_obligation_coverage_review(candidate,chunk,review_dir=directory,
                    run_id=request["run_id"],chunk_index=1,attempt=1,host_runtime="codex",model="gpt-6-luna",
                    timeout=5,agent_id="main",runner="exec",binary="codex",config_path=None,
                    controller=bridge.RunController(),output_policy="review_draft")
            self.assertEqual(candidate,before)
            self.assertEqual(pointer["provider_attempt"],1)
            self.assertEqual(pointer["status"],"completed_with_disputes")
            envelope=json.loads((directory/pointer["audit_path"]).read_text())
            def bridge_consume():
                bridge._validate_completed_obligation_ledger_chain(directory,envelope,pointer,candidate,chunk,
                    chunk_index=1,attempt=1,output_policy="review_draft")
            bridge_consume()
            full=json.loads((directory/"llm-request.json").read_text())
            candidate_path=directory/"accepted-candidate.json";bridge._write_json(candidate_path,candidate)
            audit={"chunk_count":1,"adapter_id":"codex","host_runtime":"codex",
                "chunk_lifecycle":[{"chunk_index":1,"status":"completed","remote_operation_state":"completed"}],
                "chunk_runs":[{"chunk_index":1,"response_path":str(candidate_path),"accepted_response_sha256":sha256_json(candidate),
                               "independent_obligation_review":pointer}]}
            def consume(policy="review_draft"):
                return pipeline._validate_independent_obligation_receipts(audit=audit,review_root=directory,
                    expected_run_id=request["run_id"],expected_request_body_sha=request_body_sha256(full),
                    expected_request_envelope_sha=None,expected_request_file_sha=None,output_policy=policy)
            receipts=consume()
            self.assertEqual(receipts[0]["manual_review_required_clause_ids"],["C1"])
            self.assertTrue(receipts[0]["submission_blocked_by_manual_review"])
            with self.assertRaises(ValueError):consume("submission")
            self.verify_manual_draft(receipts,chunk,Path(tmp)/"draft.docx")
            ledger_path=directory/pointer["obligation_analysis_ledger_path"]
            ledger=json.loads(ledger_path.read_text())
            self.assertEqual(ledger["obligations"],[])  # no invented AO to satisfy a schema
            saved=copy.deepcopy(ledger)
            for mutation in ("deleted","changed_primary","stale_run"):
                ledger=copy.deepcopy(saved)
                if mutation=="deleted":ledger["source_inventory_disputes"]=[]
                if mutation=="changed_primary":ledger["source_inventory_disputes"][0]["primary_review_context"]["primary_obligations"]=[]
                if mutation=="stale_run":ledger["run_id"]="old-run"
                bridge._write_json(ledger_path,ledger)
                pointer["obligation_analysis_ledger_sha256"]=bridge.sha256_file(ledger_path)
                envelope["obligation_analysis_ledger"]["sha256"]=bridge.sha256_file(ledger_path)
                bridge._write_json(directory/pointer["audit_path"],envelope)
                pointer["audit_sha256"]=bridge.sha256_file(directory/pointer["audit_path"])
                with self.subTest(mutation=mutation):
                    with self.assertRaises(ValueError):bridge_consume()
                    with self.assertRaises(ValueError):consume()

    def verify_manual_draft(self,receipts,chunk,path):
        gates=pipeline._inventory_dispute_release_gates(receipts,clauses=chunk["clauses"])
        self.assertEqual(len(gates),1)
        binding=manual_fixtures.ManualReviewTests._binding(run_id=receipts[0]["run_id"])
        ledger=build_manual_review_ledger({},[],binding=binding,release_gates=gates)
        self.assertEqual(load_and_validate(ledger,ROOT/"schema/manual-review-ledger.schema.json"),[])
        self.assertEqual(len(ledger["items"]),1)
        card=build_scorecard(binding,{"missing_count":0,"unexpected_count":0,"duplicate_count":0,
                                     "receipts":[],"expected_receipt_ids":[]},ledger["items"],[])
        self.assertFalse(card["submission_ready"])
        doc=Document();doc.add_paragraph("保留原文")
        append_manual_review_markers(doc,ledger);append_scorecard(doc,card);doc.save(path)
        self.assertTrue(audit_manual_review_markers(path,ledger)["valid"])
        self.assertTrue(audit_scorecard(path,card)["valid"])
        text="\n".join(p.text for p in Document(path).paragraphs)
        self.assertIn("义务是否存在尚未确认",text)
        self.assertIn("不要按主审推测",text)
        self.assertIn("保留原文",text)
        # Both opinions participate in identity, so changing one invalidates
        # existing DOCX markers rather than silently changing the human task.
        changed_receipts=copy.deepcopy(receipts)
        changed_receipts[0]["source_inventory_disputes"][0]["independent_result"]["rationale"]="changed"
        changed=pipeline._inventory_dispute_release_gates(changed_receipts,clauses=chunk["clauses"])
        stale=build_manual_review_ledger({},[],binding=binding,release_gates=changed)
        self.assertNotEqual(stale["items"][0]["manual_obligation_id"],ledger["items"][0]["manual_obligation_id"])
        self.assertFalse(audit_manual_review_markers(path,stale)["valid"])


if __name__=="__main__":unittest.main()
