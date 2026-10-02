from __future__ import annotations

import copy
import hashlib
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from subprocess import CompletedProcess
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from independent_retry_scope import prepare_retry_scope, constrain_retry_schema, validate_retry_scope
from native_semantic_review import (
    OBLIGATION_COVERAGE_SCHEMA, NativeSemanticReviewError,
    validate_obligation_coverage_response, _prompt,
)
from semantic_source_references import (
    build_source_reference_packet, source_reference_schema, compile_source_reference_response,
    source_inventory_generation_schema,
)
from host_review_schema import native_output_schema, native_schema_support_errors
import native_semantic_review as native_review


class RetryScopeTests(unittest.TestCase):
    def setUp(self):
        self.case = json.loads((ROOT / "tests/fixtures/independent-retry-sibling-incident.json").read_text())
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / "independent-review-chunk-0005-attempt-01"
        self.output = self.base.with_name(self.base.name + "-provider-attempt-02")
        self.request = copy.deepcopy(self.case["request"])
        self.retry = {**copy.deepcopy(self.request), "provider_attempt": 2,
                      "retry_feedback": {"code": "missing_source_obligation_inventory", "clause_ids": ["C00033"]}}
        self.write_parent()

    def wire(self, compiled, request):
        packet = build_source_reference_packet(request)
        checks = {c["check_id"]: c for c in packet["checks"]}
        raw = copy.deepcopy(compiled)
        for result in raw["results"]:
            spans = checks[result["check_id"]]["source_spans"]
            def ref(quote):
                found = [s["ref_id"] for s in spans if s["text"] == quote]
                self.assertEqual(len(found), 1); return found[0]
            result["evidence_refs"] = [ref(q) for q in result.pop("evidence_quotes")]
            result.pop("machine_obligation_ids")
            for atom in result["identified_obligations"]:
                atom["source_ref"] = ref(atom.pop("source_quote"))
        return raw

    def write(self, name, data):
        self.base.mkdir(parents=True, exist_ok=True)
        (self.base / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def write_parent(self, compiled=None):
        raw = self.wire(compiled or self.case["first_compiled"], self.request)
        replay, proof = compile_source_reference_response(raw, self.request, OBLIGATION_COVERAGE_SCHEMA, coverage=True,
                                                         provider_nullable_optionals=True)
        for name, data in (("request.json", self.request), ("raw-response.json", raw),
                           ("compiled-response.json", replay), ("source-reference-compilation.json", proof),
                           ("source-reference-packet.json", build_source_reference_packet(self.request))):
            self.write(name, data)

    def scope(self):
        locks, proof = prepare_retry_scope(self.retry, self.output, OBLIGATION_COVERAGE_SCHEMA,
            provider_nullable_optionals=True)
        schema = constrain_retry_schema(source_reference_schema(OBLIGATION_COVERAGE_SCHEMA,
            build_source_reference_packet(self.retry), coverage=True, constrain_requirement_links=True), locks)
        return locks, proof, schema

    def test_captured_retry_changed_only_sibling_capitalization(self):
        first = self.case["first_compiled"]["results"][1]["identified_obligations"][0]
        second = self.case["second_compiled"]["results"][1]["identified_obligations"][0]
        self.assertEqual(first["actor"], "author"); self.assertEqual(second["actor"], "Author")
        with self.assertRaisesRegex(NativeSemanticReviewError, "actor, target"):
            validate_obligation_coverage_response(copy.deepcopy(self.case["second_compiled"]), self.retry["checks"])
        locks, proof, schema = self.scope()
        self.assertEqual(proof["fresh_review_check_ids"], ["C00033"])
        self.assertEqual(proof["retained_check_ids"], ["C00037"])
        self.assertEqual(native_schema_support_errors(native_output_schema(schema)), [])
        corrected = self.wire(self.case["second_compiled"], self.retry)
        corrected["results"][1] = copy.deepcopy(locks["C00037"])
        original = copy.deepcopy(corrected)
        validate_retry_scope(corrected, schema, locks, native=True)
        compiled, _ = compile_source_reference_response(corrected, self.retry, OBLIGATION_COVERAGE_SCHEMA, coverage=True)
        self.assertEqual(len(validate_obligation_coverage_response(compiled, self.retry["checks"])), 2)
        self.assertEqual(original, corrected)  # No raw/result normalization to hide the drift.
        self.assertIn("not a new full-batch", _prompt(self.retry, retained_results=locks))

    def test_raw_drift_duplicate_missing_or_changed_pending_cannot_pass(self):
        locks, _, schema = self.scope()
        good = self.wire(self.case["second_compiled"], self.retry); good["results"][1] = copy.deepcopy(locks["C00037"])
        changes = [lambda x: x["results"][1]["identified_obligations"][0].update(actor="Author"),
                   lambda x: x["results"][1]["identified_obligations"][0].update(target="different duty"),
                   lambda x: x["results"][1]["identified_obligations"][0].update(disposition="represented"),
                   lambda x: x["results"][1].update(identified_obligations=[]),
                   lambda x: x["results"].append(copy.deepcopy(x["results"][1])),
                   lambda x: x["results"].pop()]
        for mutate in changes:
            raw = copy.deepcopy(good); mutate(raw)
            with self.subTest(mutate=mutate), self.assertRaises(NativeSemanticReviewError):
                validate_retry_scope(raw, schema, locks, native=True)

    def test_invalid_new_target_keeps_all_original_gates(self):
        locks, _, schema = self.scope()
        good = self.wire(self.case["second_compiled"], self.retry); good["results"][1] = copy.deepcopy(locks["C00037"])
        good["results"][0]["identified_obligations"] = []
        validate_retry_scope(good, schema, locks, native=True)
        compiled, _ = compile_source_reference_response(good, self.retry, OBLIGATION_COVERAGE_SCHEMA, coverage=True)
        with self.assertRaisesRegex(NativeSemanticReviewError, "no source-obligation inventory"):
            validate_obligation_coverage_response(compiled, self.retry["checks"])

    def test_hidden_second_error_is_not_retained(self):
        bad = copy.deepcopy(self.case["first_compiled"])
        bad["results"][1]["identified_obligations"][0]["target"] = "different legal duty"
        self.write_parent(bad)
        locks, proof, _ = self.scope()
        self.assertEqual(locks, {})
        self.assertEqual(proof["fresh_review_check_ids"], ["C00033", "C00037"])
        self.assertEqual(proof["retained_check_ids"], [])
        self.assertEqual(proof["additional_reproduced_rejections"][0]["code"],
                         "typed_source_atom_alignment_disagreement")
        # Scope construction is not acceptance or a semantic rewrite.
        with self.assertRaisesRegex(NativeSemanticReviewError, "target"):
            validate_obligation_coverage_response({"results": [bad["results"][1]]}, [self.request["checks"][1]])

    def test_hidden_condition_stays_rejected_after_fresh_review(self):
        bad = copy.deepcopy(self.case["first_compiled"])
        bad["results"][1]["identified_obligations"][0]["condition"] = "Except for already cited material."
        self.write_parent(bad)
        locks, proof, schema = self.scope()
        self.assertEqual(locks, {})
        self.assertIn("condition", proof["additional_reproduced_rejections"][0]["message"])
        self.assertEqual(proof["additional_reproduced_rejections"][0]["rejected_result_sha256"],
                         native_review.sha256_json(bad["results"][1]))
        corrected = copy.deepcopy(self.case["second_compiled"])
        corrected["results"][1] = bad["results"][1]
        raw = self.wire(corrected, self.retry)
        validate_retry_scope(raw, schema, locks, native=True)
        compiled, _ = compile_source_reference_response(raw, self.retry, OBLIGATION_COVERAGE_SCHEMA,
            coverage=True, provider_nullable_optionals=True)
        with self.assertRaisesRegex(NativeSemanticReviewError, "condition"):
            validate_obligation_coverage_response(compiled, self.retry["checks"])
        self.assertIn("not an exhaustive error set", _prompt(self.retry, retained_results=locks))

    def test_hidden_condition_reaches_native_reread_not_preflight_abort(self):
        bad = copy.deepcopy(self.case["first_compiled"])
        bad["results"][1]["identified_obligations"][0]["condition"] = "Except for already cited material."
        self.write_parent(bad)
        corrected = copy.deepcopy(self.case["second_compiled"])
        corrected["results"][1] = bad["results"][1]
        raw = self.wire(corrected, self.retry)
        context = SimpleNamespace(runtime="codex", as_audit=lambda: {"host_runtime": "codex"})
        def command(**kwargs):
            kwargs["last_message_path"].write_text("{}", encoding="utf-8")
            return ["codex"]
        with ExitStack() as stack:
            for obj, name, value in (
                (native_review, "require_host_runtime", context),
                (native_review, "automatic_adapter_id", "codex"),
                (native_review.codex_adapter, "resolve_binary", "codex"),
                (native_review.codex_adapter, "probe_capabilities", {"output_schema_supported": True}),
                (native_review.codex_adapter, "parse_result", (raw, {"event_types": ["task_complete"]})),
            ):
                stack.enter_context(patch.object(obj, name, return_value=value))
            transport = stack.enter_context(patch.object(native_review, "run_process",
                return_value=CompletedProcess(["codex"], 0, "{}", "")))
            stack.enter_context(patch.object(native_review.codex_adapter, "build_command", side_effect=command))
            with self.assertRaisesRegex(native_review.TypedSourceAtomAlignmentError, "condition"):
                native_review.run_native_semantic_review(self.retry, output_dir=self.output,
                    host_runtime="codex", model="gpt-6-luna", timeout=5)
        transport.assert_called_once()
        self.assertEqual(json.loads((self.output / "compiled-response.json").read_text())["results"][1],
                         bad["results"][1])
        self.assertTrue((self.output / "request.json").is_file())
        self.assertFalse((self.output / "response.json").exists())
        proof = json.loads((self.output / "validated-retry-scope.json").read_text())
        self.assertEqual(proof["fresh_review_check_ids"], ["C00033", "C00037"])
        self.assertEqual(proof["retained_check_ids"], [])

    def test_unknown_sibling_error_still_aborts_scope(self):
        original_validator = native_review.validate_obligation_coverage_response
        def validator(response, checks, **kwargs):
            if len(checks) == 1 and checks[0]["check_id"] == "C00037":
                raise NativeSemanticReviewError("unknown contract error")
            return original_validator(response, checks, **kwargs)
        with patch.object(native_review, "validate_obligation_coverage_response", side_effect=validator), \
                self.assertRaisesRegex(NativeSemanticReviewError, "unknown contract error"):
            self.scope()

    def test_mixed_errors_keep_only_proven_valid_siblings(self):
        extra = copy.deepcopy(self.request["checks"][1])
        extra["check_id"] = "extra-valid-check"
        self.request["checks"].append(extra)
        bad = copy.deepcopy(self.case["first_compiled"])
        valid = copy.deepcopy(bad["results"][1]); valid["check_id"] = extra["check_id"]
        bad["results"].append(valid)
        bad["results"][1]["identified_obligations"][0]["condition"] = "Exception not recorded by primary"
        self.retry = {**copy.deepcopy(self.request), "provider_attempt": 2,
                      "retry_feedback": {"code": "missing_source_obligation_inventory", "clause_ids": ["C00033"]}}
        self.write_parent(bad)
        locks, proof, _ = self.scope()
        self.assertEqual(list(locks), ["extra-valid-check"])
        self.assertEqual(proof["fresh_review_check_ids"], ["C00033", "C00037"])
        self.assertTrue(proof["retention_is_not_submission_approval"])

    def test_stale_run_source_candidate_or_feedback_is_rejected(self):
        mutations = [lambda r: r.update(run_id="old-run"),
                     lambda r: r["provenance"].update(source_sha256="0" * 64),
                     lambda r: r["checks"][1]["review_context"]["primary_obligations"][0].update(target="new duty"),
                     lambda r: r["retry_feedback"].update(clause_ids=["C00037"]),
                     lambda r: r["retry_feedback"].update(clause_ids=["unknown"])]
        for mutate in mutations:
            changed = copy.deepcopy(self.retry); mutate(changed)
            with self.subTest(mutate=mutate), self.assertRaises(NativeSemanticReviewError):
                prepare_retry_scope(changed, self.output, OBLIGATION_COVERAGE_SCHEMA)

    def test_parent_artifacts_replay_not_file_existence_or_self_hash(self):
        for name in ("raw-response.json", "compiled-response.json", "source-reference-packet.json", "source-reference-compilation.json"):
            self.write_parent()
            data = json.loads((self.base / name).read_text()); data["fake"] = True; self.write(name, data)
            with self.subTest(name=name), self.assertRaises((NativeSemanticReviewError, ValueError)):
                self.scope()

    def test_no_parent_evidence_means_no_retention_not_pass(self):
        other = Path(self.temp.name) / "other-provider-attempt-02"
        self.assertEqual(prepare_retry_scope(self.retry, other, OBLIGATION_COVERAGE_SCHEMA), ({}, None))
        (self.base / "compiled-response.json").unlink()
        with self.assertRaisesRegex(NativeSemanticReviewError, "incomplete"):
            self.scope()

    def test_other_retries_and_first_attempt_remain_unrestricted(self):
        for request in (self.request, {**copy.deepcopy(self.retry), "retry_feedback": {"code": "other"}}):
            self.assertEqual(prepare_retry_scope(request, self.output, OBLIGATION_COVERAGE_SCHEMA), ({}, None))

    def typed_parent(self):
        compiled = copy.deepcopy(self.case["second_compiled"])
        compiled["results"][1] = copy.deepcopy(self.case["first_compiled"]["results"][1])
        compiled["results"][1]["identified_obligations"][0]["target"] = "disputed responsibility target"
        self.write_parent(compiled)
        with self.assertRaises(native_review.TypedSourceAtomAlignmentError) as caught:
            validate_obligation_coverage_response(compiled, self.request["checks"])
        self.retry = {**copy.deepcopy(self.request), "provider_attempt": 2, "retry_feedback": {
            "code": caught.exception.code, "clause_ids": list(caught.exception.clause_ids),
            "disagreements": caught.exception.disagreements,
            "checks_sha256": native_review.sha256_json(self.request["checks"]),
            "run_id": self.request["run_id"], "provenance": copy.deepcopy(self.request["provenance"])}}
        return compiled

    def test_typed_retry_locks_only_individually_validated_siblings(self):
        self.typed_parent()
        locks, proof, schema = self.scope()
        self.assertEqual(proof["policy"], "validated_typed_alignment_retry_scope_v1")
        self.assertEqual(proof["fresh_review_check_ids"], ["C00037"])
        self.assertEqual(proof["retained_check_ids"], ["C00033"])
        corrected = copy.deepcopy(self.case["second_compiled"])
        corrected["results"][1] = self.case["first_compiled"]["results"][1]
        raw = self.wire(corrected, self.retry)
        raw["results"][0] = copy.deepcopy(locks["C00033"])
        validate_retry_scope(raw, schema, locks, native=True)
        damaged = copy.deepcopy(raw); damaged["results"][0]["identified_obligations"] = []
        with self.assertRaises(NativeSemanticReviewError):
            validate_retry_scope(damaged, schema, locks, native=True)
        compiled, _ = compile_source_reference_response(raw, self.retry, OBLIGATION_COVERAGE_SCHEMA, coverage=True)
        self.assertEqual(len(validate_obligation_coverage_response(compiled, self.retry["checks"])), 2)

    def test_typed_scope_refuses_changed_dispute_or_missing_parent(self):
        self.typed_parent()
        original = copy.deepcopy(self.retry)
        for damage in ("field", "primary_hash", "run", "check_hash"):
            self.retry = copy.deepcopy(original)
            feedback = self.retry["retry_feedback"]
            if damage == "field": feedback["disagreements"][0]["fields"] = ["condition"]
            elif damage == "primary_hash": feedback["disagreements"][0]["primary_sha256"] = "0" * 64
            elif damage == "run": feedback["run_id"] = "old"
            else: feedback["checks_sha256"] = "0" * 64
            with self.subTest(damage=damage), self.assertRaises(NativeSemanticReviewError):
                self.scope()
        self.retry = original
        with self.assertRaisesRegex(NativeSemanticReviewError, "parent evidence"):
            prepare_retry_scope(self.retry, Path(self.temp.name) / "missing-provider-attempt-02", OBLIGATION_COVERAGE_SCHEMA)

    def test_typed_persisted_scope_is_replayed_not_a_self_reported_pass(self):
        from independent_retry_scope import validate_persisted_empty_inventory_scope
        self.typed_parent()
        locks, proof, _ = self.scope()
        corrected = copy.deepcopy(self.case["second_compiled"])
        corrected["results"][1] = self.case["first_compiled"]["results"][1]
        raw = self.wire(corrected, self.retry); raw["results"][0] = copy.deepcopy(locks["C00033"])
        self.output.mkdir()
        path = self.output / "validated-retry-scope.json"
        path.write_text(json.dumps(proof), encoding="utf-8")
        audit = {"adapter_id": "codex", "corrective_review_scope": {
            "policy": proof["policy"], "proof_path": str(path.resolve()),
            "proof_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "fresh_review_check_ids": proof["fresh_review_check_ids"],
            "retained_check_ids": proof["retained_check_ids"]}}
        validate_persisted_empty_inventory_scope(self.retry, self.output, raw, audit)
        damaged = copy.deepcopy(raw); damaged["results"][0]["identified_obligations"] = []
        with self.assertRaises(NativeSemanticReviewError):
            validate_persisted_empty_inventory_scope(self.retry, self.output, damaged, audit)
        changed = copy.deepcopy(proof); changed["retained_check_ids"] = []
        path.write_text(json.dumps(changed), encoding="utf-8")
        audit["corrective_review_scope"]["proof_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaises(ValueError):
            validate_persisted_empty_inventory_scope(self.retry, self.output, raw, audit)

    def test_linked_parent_directory_is_not_current_invocation_evidence(self):
        link = Path(self.temp.name) / "linked"
        link.symlink_to(self.base, target_is_directory=True)
        linked_output = link.with_name("linked-provider-attempt-02")
        with self.assertRaisesRegex(NativeSemanticReviewError, "symlink"):
            prepare_retry_scope(self.retry, linked_output, OBLIGATION_COVERAGE_SCHEMA)

    def test_adapter_optional_null_handling_is_not_globally_codex(self):
        # Other adapters must reproduce their own compilation receipt, not
        # claim the Codex strict-output omission projection was applied.
        raw = self.wire(self.case["first_compiled"], self.request)
        compiled, proof = compile_source_reference_response(raw, self.request, OBLIGATION_COVERAGE_SCHEMA,
            coverage=True, provider_nullable_optionals=False)
        self.write("raw-response.json", raw); self.write("compiled-response.json", compiled)
        self.write("source-reference-compilation.json", proof)
        locks, _ = prepare_retry_scope(self.retry, self.output, OBLIGATION_COVERAGE_SCHEMA,
            provider_nullable_optionals=False)
        self.assertEqual(locks["C00037"]["identified_obligations"][0]["actor"], "author")
        with self.assertRaisesRegex(NativeSemanticReviewError, "does not reproduce"):
            prepare_retry_scope(self.retry, self.output, OBLIGATION_COVERAGE_SCHEMA,
                provider_nullable_optionals=True)

    def test_strict_parent_json_rejects_duplicate_keys_and_nonfinite_values(self):
        for text in ('{"results": [], "results": []}', '{"results": NaN}'):
            self.write_parent()
            (self.base / "raw-response.json").write_text(text, encoding="utf-8")
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.scope()

    def test_production_runner_keeps_raw_and_full_compilation_replayable(self):
        locks, _, schema = self.scope()
        raw = self.wire(self.case["second_compiled"], self.retry)
        raw["results"][1] = copy.deepcopy(locks["C00037"])
        original = copy.deepcopy(raw)
        observed = {}
        def command(**kwargs):
            observed.update(kwargs)
            kwargs["last_message_path"].write_text("{}", encoding="utf-8")
            return ["codex"]
        context = SimpleNamespace(runtime="codex", as_audit=lambda: {"host_runtime": "codex"})
        with ExitStack() as stack:
            for obj, name, value in (
                (native_review, "require_host_runtime", context),
                (native_review, "automatic_adapter_id", "codex"),
                (native_review.codex_adapter, "resolve_binary", "codex"),
                (native_review.codex_adapter, "probe_capabilities", {"output_schema_supported": True}),
                (native_review, "run_process", CompletedProcess(["codex"], 0, "{}", "")),
                (native_review.codex_adapter, "parse_result", (raw, {"event_types": ["task_complete"]})),
            ):
                stack.enter_context(patch.object(obj, name, return_value=value))
            stack.enter_context(patch.object(native_review.codex_adapter, "build_command", side_effect=command))
            audit = native_review.run_native_semantic_review(self.retry, output_dir=self.output,
                host_runtime="codex", model="gpt-6-luna", timeout=5)
        self.assertEqual(audit["status"], "completed")
        self.assertEqual(audit["corrective_review_scope"]["retained_check_ids"], ["C00037"])
        self.assertEqual(json.loads((self.output / "raw-response.json").read_text()), original)
        replay, _ = compile_source_reference_response(original, self.retry, OBLIGATION_COVERAGE_SCHEMA,
            coverage=True, provider_nullable_optionals=True)
        self.assertEqual(json.loads((self.output / "compiled-response.json").read_text()), replay)
        self.assertEqual(json.loads((self.output / "response-schema.json").read_text()), source_inventory_generation_schema(schema, retained_results=locks))
        self.assertEqual(observed["output_schema_path"], self.output / "provider-response-schema.json")
        self.assertEqual(replay["results"][1]["verdict"], "external_compliance_pending")

    def test_renamed_ids_and_changed_source_are_dynamic(self):
        # Rename every occurrence; regenerate all source references and parent
        # proofs. No school, clause ID or physical quotation is a global rule.
        text = json.dumps(self.case, ensure_ascii=False).replace("C00033", "X-title").replace("C00037", "X-legal")
        self.case = json.loads(text)
        self.request = copy.deepcopy(self.case["request"])
        self.retry = {**copy.deepcopy(self.request), "provider_attempt": 2,
                      "retry_feedback": {"code": "missing_source_obligation_inventory", "clause_ids": ["X-title"]}}
        self.write_parent()
        locks, proof, _ = self.scope()
        self.assertEqual(sorted(locks), ["X-legal"])
        self.assertEqual(proof["fresh_review_check_ids"], ["X-title"])


if __name__ == "__main__":
    unittest.main()
