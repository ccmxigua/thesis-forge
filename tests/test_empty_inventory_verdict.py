"""Captured multi-check conflict, bounded correction and immutable sibling replay."""
from __future__ import annotations
import copy
import json
from contextlib import ExitStack
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace
from unittest.mock import patch
import tempfile
import unittest
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import native_semantic_review as native
import host_agent_bridge as bridge
import thesis_format_pipeline as pipeline
from semantic_contract import sha256_json, request_body_sha256
from semantic_source_references import build_source_reference_packet, compile_source_reference_response, source_reference_schema
from independent_retry_scope import prepare_retry_scope, constrain_retry_schema, validate_retry_scope, validate_persisted_empty_inventory_scope
from host_review_schema import native_output_schema, native_schema_support_errors
from tests import test_independent_retry_scope as scope_fixtures
from tests import test_host_agent_bridge as bridge_fixtures


def enveloped(raw):
    result = copy.deepcopy(raw)
    for item in result["results"]:
        atoms = item["identified_obligations"]
        item["identified_obligations"] = {"first": atoms[0] if atoms else None, "remaining": atoms[1:]}
    return result


class EmptyInventoryVerdictTests(unittest.TestCase):
    def setUp(self):
        self.case = json.loads((ROOT / "tests/fixtures/empty-inventory-verdict-incident.json").read_text())
        self.request = self.case["request"]
        self.first = self.case["first_compiled"]
        self.corrected = copy.deepcopy(self.first)
        for result in self.corrected["results"]:
            result["verdict"] = "consistent"
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name) / "independent-review-chunk-0001-attempt-01"
        self.output = self.base.with_name(self.base.name + "-provider-attempt-02")
        self.helper = scope_fixtures.RetryScopeTests()
        self.helper.case = self.case; self.helper.request = self.request; self.helper.base = self.base
        self.helper.write_parent()
        self.retry = self.feedback_request(self.request, self.first, "a" * 64)

    def feedback_request(self, request, first, candidate_sha):
        with self.assertRaises(native.EmptyInventoryVerdictError) as caught:
            native.validate_obligation_coverage_response(copy.deepcopy(first), request["checks"])
        exc = caught.exception
        return {**copy.deepcopy(request), "provider_attempt": 2, "retry_feedback": {
            "code": exc.code, "clause_ids": list(exc.clause_ids),
            "rejected_results": exc.rejected_results, "rejected_results_sha256": sha256_json(exc.rejected_results),
            "rejected_request_sha256": sha256_json(request), "checks_sha256": sha256_json(request["checks"]),
            "candidate_response_sha256": candidate_sha, "run_id": request["run_id"],
            "provenance": copy.deepcopy(request["provenance"])}}

    def scope(self):
        locks, proof = prepare_retry_scope(self.retry, self.output, native.OBLIGATION_COVERAGE_SCHEMA,
                                         provider_nullable_optionals=True)
        schema = constrain_retry_schema(source_reference_schema(native.OBLIGATION_COVERAGE_SCHEMA,
            build_source_reference_packet(self.retry), coverage=True, constrain_requirement_links=True), locks)
        return locks, proof, schema

    def test_all_five_captured_conflicts_collected_and_three_valid_siblings_retained(self):
        before = copy.deepcopy(self.first)
        self.assertEqual(self.retry["retry_feedback"]["clause_ids"], ["C00001", "C00002", "C00004", "C00005", "C00006"])
        locks, proof, schema = self.scope()
        self.assertEqual(sorted(locks), ["C00003", "C00007", "C00008"])
        self.assertEqual(native_schema_support_errors(native_output_schema(schema)), [])
        raw = self.helper.wire(self.corrected, self.retry)
        validate_retry_scope(raw, schema, locks, native=True)
        compiled, _ = compile_source_reference_response(raw, self.retry, native.OBLIGATION_COVERAGE_SCHEMA, coverage=True)
        self.assertEqual(len(native.validate_obligation_coverage_response(compiled, self.retry["checks"])), 8)
        self.assertEqual(self.first, before)
        self.assertIn("Never invent a duty", native._prompt(self.retry, retained_results=locks))
        self.assertTrue(proof["retention_is_not_submission_approval"])

    def test_verdict_inventory_protocol_is_not_label_or_id_specific(self):
        for verdict in ("uncertain", "manual_review_required", "backend_unsupported", "source_content_pending",
                        "source_content_verification_pending", "external_compliance_pending", "mixed_execution_external_pending"):
            result = copy.deepcopy(self.first["results"][0]); result["verdict"] = verdict
            check = copy.deepcopy(self.request["checks"][0])
            check["check_id"] = result["check_id"] = "other-template-item"
            with self.subTest(verdict=verdict), self.assertRaises(native.EmptyInventoryVerdictError):
                native.validate_obligation_coverage_response({"results": [result]}, [check])
        # A no-duty result is independently assessed, not inferred from a score.
        native.validate_obligation_coverage_response(copy.deepcopy(self.corrected), self.request["checks"])

    def test_stale_unknown_resealed_valid_results_and_extra_authority_rejected(self):
        mutations = [lambda r: r.update(run_id="old"), lambda r: r.update(provider_attempt=2.0),
            lambda r: r["retry_feedback"].update(clause_ids=["foreign"]),
            lambda r: r["retry_feedback"].update(checks_sha256="0"*64),
            lambda r: r["retry_feedback"].update(allow_reclassification=True),
            lambda r: r["retry_feedback"]["rejected_results"][0].update(evidence_quotes=["wrong source"])]
        for mutate in mutations:
            request = copy.deepcopy(self.retry); mutate(request)
            with self.subTest(mutate=mutate):
                self.assertFalse(native.empty_inventory_retry_feedback_is_bound(request))
        request = copy.deepcopy(self.retry)
        request["retry_feedback"]["rejected_results"][0]["verdict"] = "consistent"
        request["retry_feedback"]["rejected_results_sha256"] = sha256_json(request["retry_feedback"]["rejected_results"])
        self.assertFalse(native.empty_inventory_retry_feedback_is_bound(request))

    def test_no_captured_parent_and_hidden_sibling_error_cannot_authorize_retry(self):
        with self.assertRaisesRegex(native.NativeSemanticReviewError, "lacks captured"):
            prepare_retry_scope(self.retry, Path(self.tmp.name)/"missing-provider-attempt-02", native.OBLIGATION_COVERAGE_SCHEMA)
        changed = copy.deepcopy(self.first)
        changed["results"][2]["evidence_quotes"] = ["not current source"]
        # Wire cannot select invented quotes; use a separate legitimate failure:
        changed["results"][2]["evidence_quotes"] = self.first["results"][2]["evidence_quotes"]
        changed["results"][2]["machine_obligation_ids"] = ["invented-code"]
        self.helper.write_parent(changed)  # compiler owns machine IDs, so tamper compilation instead
        data = json.loads((self.base/"compiled-response.json").read_text())
        data["results"][2]["machine_obligation_ids"] = ["invented-code"]
        self.helper.write("compiled-response.json", data)
        with self.assertRaisesRegex(native.NativeSemanticReviewError, "does not reproduce"):
            self.scope()

    def test_uncorrected_target_or_changed_sibling_remains_rejected(self):
        locks, _, schema = self.scope()
        raw = self.helper.wire(self.first, self.retry)
        validate_retry_scope(raw, schema, locks, native=True)
        compiled, _ = compile_source_reference_response(raw, self.retry, native.OBLIGATION_COVERAGE_SCHEMA, coverage=True)
        with self.assertRaises(native.EmptyInventoryVerdictError):
            native.validate_obligation_coverage_response(compiled, self.retry["checks"])
        raw = self.helper.wire(self.corrected, self.retry)
        raw["results"][2]["rationale"] = "Changed unrelated assessment"
        with self.assertRaises(native.NativeSemanticReviewError): validate_retry_scope(raw, schema, locks, native=True)

    def mock_transport(self, stack, raws):
        context = SimpleNamespace(runtime="codex", as_audit=lambda: {"host_runtime": "codex"})
        remaining, by_request = iter(raws), {}
        def process(command, **kwargs):
            last = Path(command[command.index("--output-last-message") + 1])
            child = last.parent
            partitioned = child.name.startswith("native-batch-")
            owner = child.parent if partitioned else child
            if owner not in by_request:
                by_request[owner] = next(remaining)
            response = copy.deepcopy(by_request[owner])
            if partitioned:
                packet = json.loads((child / "source-reference-packet.json").read_text())
                ids = packet["native_review_partition"]["check_ids"]
                all_ids = {c["check_id"] for c in packet["orientation_only_checks"]}
                response["results"] = [r for r in response["results"]
                    if r.get("check_id") in ids or r.get("check_id") not in all_ids]
                from tests.test_independent_review_partition import keyed_wire
                response = keyed_wire(packet, response)
            text = json.dumps(response, ensure_ascii=False)
            last.write_text(text, encoding="utf-8")
            events = [{"type": "item.completed", "item": {"type": "agent_message", "text": text}},
                      {"type": "turn.completed"}]
            return CompletedProcess(command, 0, "\n".join(json.dumps(e) for e in events), "")
        for obj, name, value in ((native, "require_host_runtime", context), (native, "automatic_adapter_id", "codex"),
            (native.codex_adapter, "resolve_binary", "codex"), (native.codex_adapter, "probe_capabilities", {"output_schema_supported":True})):
            stack.enter_context(patch.object(obj,name,return_value=value))
        stack.enter_context(patch.object(native,"run_process",side_effect=process))

    def test_native_runner_and_resealed_consumer_proof(self):
        raw = enveloped(self.helper.wire(self.corrected, self.retry))
        with ExitStack() as stack:
            self.mock_transport(stack, [raw])
            audit = native.run_native_semantic_review(self.retry,output_dir=self.output,host_runtime="codex",model="gpt-6-luna",timeout=5)
        validate_persisted_empty_inventory_scope(self.retry,self.output,raw,audit)
        self.assertEqual(json.loads((self.output/"raw-response.json").read_text()),raw)
        proof_path = self.output/"validated-retry-scope.json"
        proof = json.loads(proof_path.read_text()); proof["retained_check_ids"] = []
        bridge._write_json(proof_path,proof)
        audit["corrective_review_scope"]["proof_sha256"] = bridge.sha256_file(proof_path)
        with self.assertRaisesRegex(ValueError,"does not reproduce"):
            validate_persisted_empty_inventory_scope(self.retry,self.output,raw,audit)

    def test_production_bridge_success_and_two_call_exhaustion_no_candidate_edit(self):
        for success in (True,False):
            with self.subTest(success=success), tempfile.TemporaryDirectory() as tmp:
                _,chunk = bridge_fixtures.HostAgentBridgeTests()._packet(Path(tmp)/"packet",source="Department name",contract_version="3.0")
                candidate={"contract_version":"3.0","provenance":chunk["provenance"],"requirements":[],
                    "clause_reviews":[{"clause_id":"C1","classification":"informational","reason":"Label."}],
                    "unsupported_items":[],"reported_conflicts":[]}
                before=copy.deepcopy(candidate)
                request=native.build_obligation_coverage_request(candidate,chunk,run_id=chunk["provenance"]["run_id"],chunk_index=1)
                request.update(attempt=1,provider_attempt=1)
                first={"results":[{"check_id":"C1","verdict":"uncertain","rationale":"Label without a specified operation.",
                    "evidence_quotes":["Department name"],"machine_obligation_ids":[],"identified_obligations":[]}]}
                retry=self.feedback_request(request,first,sha256_json(candidate))
                second=copy.deepcopy(first)
                if success: second["results"][0]["verdict"]="consistent"
                raws=[enveloped(self.helper.wire(first,request)), enveloped(self.helper.wire(second,retry))]
                directory=Path(tmp)/"packet"
                with ExitStack() as stack:
                    self.mock_transport(stack,raws); stack.enter_context(patch.object(bridge.time,"sleep"))
                    args=dict(review_dir=directory,run_id=request["run_id"],chunk_index=1,attempt=1,host_runtime="codex",model="gpt-6-luna",
                        timeout=5,agent_id="main",runner="exec",binary="codex",config_path=None,controller=bridge.RunController())
                    if success:
                        pointer=bridge._run_independent_obligation_coverage_review(candidate,chunk,**args)
                        envelope=json.loads((directory/pointer["audit_path"]).read_text())
                        bridge._validate_completed_obligation_ledger_chain(directory,envelope,pointer,candidate,chunk,chunk_index=1,attempt=1)
                        self.assertEqual(pointer["provider_attempt"],2)
                        full=json.loads((directory/"llm-request.json").read_text())
                        candidate_path=directory/"accepted-candidate.json"
                        bridge._write_json(candidate_path,candidate)
                        audit={"chunk_count":1,"adapter_id":"codex","host_runtime":"codex",
                            "chunk_lifecycle":[{"chunk_index":1,"status":"completed","remote_operation_state":"completed"}],
                            "chunk_runs":[{"chunk_index":1,"response_path":str(candidate_path),
                                "accepted_response_sha256":sha256_json(candidate),"independent_obligation_review":pointer}]}
                        def consume():
                            return pipeline._validate_independent_obligation_receipts(audit=audit,review_root=directory,
                                expected_run_id=request["run_id"],expected_request_body_sha=request_body_sha256(full),
                                expected_request_envelope_sha=None,expected_request_file_sha=None)
                        self.assertEqual(len(consume()),1)
                        # Rehash every success-facing reference to a forged proof;
                        # both consumers must still reconstruct the parent scope.
                        scope_path=Path(envelope["review_audit"]["corrective_review_scope"]["proof_path"])
                        scope=json.loads(scope_path.read_text()); scope["fresh_review_check_ids"]=[]
                        bridge._write_json(scope_path,scope)
                        envelope["review_audit"]["corrective_review_scope"]["proof_sha256"]=bridge.sha256_file(scope_path)
                        bridge._write_json(directory/pointer["audit_path"],envelope)
                        pointer["audit_sha256"]=bridge.sha256_file(directory/pointer["audit_path"])
                        with self.assertRaisesRegex(ValueError,"does not reproduce"): consume()
                        with self.assertRaisesRegex(ValueError,"does not reproduce"):
                            bridge._validate_completed_obligation_ledger_chain(directory,envelope,pointer,candidate,chunk,chunk_index=1,attempt=1)
                    else:
                        with self.assertRaises(bridge.IndependentObligationReviewError) as caught:
                            bridge._run_independent_obligation_coverage_review(candidate,chunk,**args)
                        self.assertFalse(caught.exception.retryable)
                        self.assertEqual(caught.exception.error_records[0]["provider_attempts"],2)
                self.assertEqual(before,candidate)

if __name__ == "__main__": unittest.main()
