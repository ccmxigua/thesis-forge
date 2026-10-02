"""Typed human checks retain their complete inventory, never become compliance."""
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
sys.path.insert(0, str(ROOT / "tests"))
import host_agent_bridge as bridge
import native_semantic_review as native
import requirements_engine as engine
from semantic_contract import sha256_json
from semantic_source_references import (
    build_source_reference_packet, compile_source_reference_response, source_reference_schema,
)
from host_review_schema import native_output_schema, native_schema_support_errors
from format_spec_validation import validate_instance
from source_obligation_compiler import (
    materialize_source_verification_classifications,
    typed_source_verification_inventory_is_bound,
)
from test_pending_work_wire import wire_response


def fixture():
    data = json.loads((ROOT / "tests/fixtures/typed-verification-incident.json").read_text())
    cid, eid = "current-other-clause", "current-other-evidence"
    clause, evidence, review, result = [copy.deepcopy(data[k]) for k in ("clause", "evidence", "review", "result")]
    clause["id"] = cid; clause["evidence_ids"] = [eid]
    clause["source_span"]["evidence_id"] = eid
    evidence["id"] = eid; review["clause_id"] = cid; result["check_id"] = cid
    for atom in review["obligations"]: atom["id"] = "current-atom"
    for atom in result["identified_obligations"]: atom["primary_obligation_id"] = "current-atom"
    evidence_context = {eid: evidence}
    response = {"contract_version": "3.0", "requirements": [], "clause_reviews": [review],
                "unsupported_items": [], "reported_conflicts": []}
    provenance = {"version": "1.0", "origin": "fresh_host_agent", "run_id": "current-offline-run", "source_sha256": sha256_json(evidence),
        "evidence_sha256": sha256_json(evidence_context), "clause_sha256": sha256_json(clause),
        "request_sha256": sha256_json("fresh-current-packet")}
    response["provenance"] = provenance
    request = engine.build_llm_request([], [clause], {"evidence": [evidence]}, {}, "full",
        contract_version="3.0", runtime_context={"code_fingerprint_sha256": "f" * 64})
    chunk = {**request, "clauses": [clause], "evidence_context": evidence_context,
             "provenance": provenance, "case_id": "another-case"}
    return response, chunk, result


class TypedVerificationRoutingTests(unittest.TestCase):
    def project(self, response, chunk):
        return materialize_source_verification_classifications(response, chunk["clauses"],
            provenance=chunk["provenance"], evidence_context=chunk["evidence_context"])

    def origin_predicate_fixture(self):
        response, chunk, _ = fixture()
        source = "关键词是为了便于做文献索引和检索工作而从论文中选取出来用以表示全文主题内容信息的单词或术语，在论文中有明确出处"
        clause = chunk["clauses"][0]
        clause["text"] = source
        for key in ("source_text_full", "source_evidence_text"):
            if key in clause:
                clause[key] = source
        clause["source_span"].update(text=source, start_offset=0, end_offset=len(source),
            source_sha256=hashlib.sha256(source.encode()).hexdigest())
        chunk["evidence_context"][clause["source_span"]["evidence_id"]]["text"] = source
        review = response["clause_reviews"][0]
        review["classification"] = "unresolved"
        review["obligations"][0].update(actor="author", action="select and substantiate",
            target="Chinese keywords from the thesis", status="unresolved", route="human",
            source_quote=source[source.index("从论文中"):])
        return response, chunk

    def test_compound_origin_predicate_routes_human_without_rewriting_quote(self):
        response, chunk = self.origin_predicate_fixture()
        original = copy.deepcopy(response)
        candidate, audit = bridge.prepare_native_response_candidate(response, chunk)
        self.assertEqual(bridge.validate_host_agent_response(candidate, chunk), [])
        atom = candidate["clause_reviews"][0]["obligations"][0]
        self.assertEqual(candidate["clause_reviews"][0]["classification"], "requires_source_verification")
        for key in ("id", "actor", "action", "target", "condition", "source_quote", "force", "applicability"):
            self.assertEqual(atom.get(key), original["clause_reviews"][0]["obligations"][0].get(key))
        self.assertEqual((atom["status"], atom["route"]), ("unresolved", "human"))
        self.assertFalse(audit["source_verification_classification_projections"][0]["submission_ready"])
        self.assertEqual(response, original)

    def test_compound_origin_fragment_cannot_authorize_partial_or_unrelated_duty(self):
        mutations = ("half_quote", "selection_only", "wrong_target", "extra_action", "stale", "condition", "linked", "mixed")
        for mutation in mutations:
            response, chunk = self.origin_predicate_fixture()
            atom = response["clause_reviews"][0]["obligations"][0]
            if mutation == "half_quote": atom["source_quote"] = "在论文中有明确出处"
            elif mutation == "selection_only": atom["source_quote"] = "从论文中选取出来"
            elif mutation == "wrong_target": atom["target"] = "keywords from the internet"
            elif mutation == "extra_action": atom["action"] += " and obtain approval"
            elif mutation == "stale": chunk["clauses"][0]["source_span"]["source_sha256"] = "0" * 64
            elif mutation == "condition": atom["condition"] = "if convenient"
            elif mutation == "linked": response["requirements"] = [{"clause_ids": [chunk["clauses"][0]["id"]]}]
            else:
                clause = chunk["clauses"][0]
                source = clause["text"] + "；关键词至少三个。"
                clause["text"] = source
                for key in ("source_text_full", "source_evidence_text"):
                    if key in clause:
                        clause[key] = source
                clause["source_span"].update(text=source, end_offset=len(source), source_sha256=hashlib.sha256(source.encode()).hexdigest())
                chunk["evidence_context"][clause["source_span"]["evidence_id"]]["text"] = source
            with self.subTest(mutation=mutation):
                self.assertEqual(self.project(response, chunk), (response, []))

    def test_compound_predicate_still_requires_fresh_source_first_pending_review(self):
        response, chunk = self.origin_predicate_fixture()
        candidate, _ = bridge.prepare_native_response_candidate(response, chunk)
        request = native.build_obligation_coverage_request(candidate, chunk, run_id="fresh-predicate", chunk_index=1)
        _, _, result = fixture()
        result["verdict"] = "source_content_verification_pending"
        atom = candidate["clause_reviews"][0]["obligations"][0]
        identified = result["identified_obligations"][0]
        for key in ("actor", "action", "target", "source_quote", "force", "applicability", "condition"):
            if key in atom:
                identified[key] = atom[key]
            else:
                identified.pop(key, None)
        identified["disposition"] = "source_content_verification_pending"
        result["evidence_quotes"] = [atom["source_quote"]]
        raw = wire_response(request, result)
        compiled, _ = compile_source_reference_response(raw, request, native.OBLIGATION_COVERAGE_SCHEMA, coverage=True)
        self.assertEqual(native.validate_obligation_coverage_response(compiled, request["checks"]), [result])
        bad = copy.deepcopy(compiled)
        bad["results"][0]["identified_obligations"] = []
        with self.assertRaises(native.NativeSemanticReviewError):
            native.validate_obligation_coverage_response(bad, request["checks"])

    def test_split_origin_action_target_preserves_pending_inventory_and_current_binding(self):
        targets = ("keywords with clear provenance in the thesis", "keywords from the paper",
                   "Chinese keywords with source in the manuscript", "论文中有明确出处的关键词")
        for target in targets:
            for route in ("human", "input", None):
                response, chunk, _ = fixture()
                review = response["clause_reviews"][0]
                review["classification"] = "unresolved"
                atom = review["obligations"][0]
                atom.update(actor="author", action="select", target=target, status="unresolved", route=route)
                frozen = copy.deepcopy(response)
                candidate, audit = self.project(response, chunk)
                with self.subTest(target=target, route=route):
                    self.assertEqual(response, frozen)
                    self.assertEqual(candidate["requirements"], [])
                    self.assertEqual(candidate["clause_reviews"][0]["classification"], "requires_source_verification")
                    projected = candidate["clause_reviews"][0]["obligations"][0]
                    for key in ("id", "actor", "action", "target", "source_quote", "force", "applicability", "condition"):
                        self.assertEqual(projected.get(key), atom.get(key))
                    self.assertEqual((projected["status"], projected["route"]), ("unresolved", "human"))
                    self.assertTrue(audit[0]["current_evidence_binding_verified"])
                    self.assertEqual(audit[0]["original_primary_obligations"], [atom])
                    self.assertFalse(audit[0]["submission_ready"])

    def test_bare_selection_without_proven_source_origin_never_authorizes_projection(self):
        for mutation in ("bare", "internet", "new_content", "extra_duty", "stale", "ellipsis", "condition", "linked", "automatic"):
            response, chunk, _ = fixture()
            review = response["clause_reviews"][0]
            review["classification"] = "unresolved"
            atom = review["obligations"][0]
            atom.update(actor="author", action="select", target="keywords with clear provenance in the thesis", status="unresolved", route="input")
            if mutation == "bare": atom["target"] = "keywords"
            elif mutation == "internet": atom["target"] = "keywords from the internet"
            elif mutation == "new_content": atom["action"] = "write"
            elif mutation == "extra_duty": atom["target"] += " and new abstract content"
            elif mutation == "stale": chunk["clauses"][0]["source_span"]["source_sha256"] = "0" * 64
            elif mutation == "ellipsis": atom["source_quote"] = "关键词……有明确出处"
            elif mutation == "condition": atom["condition"] = "if the author chooses"
            elif mutation == "linked": response["requirements"] = [{"clause_ids": [chunk["clauses"][0]["id"]]}]
            else: atom["route"] = "automatic"
            with self.subTest(mutation=mutation):
                self.assertEqual(self.project(response, chunk), (response, []))

    def test_origin_selection_variants_keep_typed_semantics_and_pending_human_responsibility(self):
        actions = (
            "select keywords from the thesis and ensure each has a clear source",
            "choose key terms from the paper",
            "extract keywords from the paper",
            "pick keywords from the manuscript and check every term has provenance",
            "从论文中选取关键词并确保每个关键词有明确出处",
        )
        for action in actions:
            response, chunk, _ = fixture()
            atom = response["clause_reviews"][0]["obligations"][0]
            atom["action"] = action
            before = copy.deepcopy(response)
            candidate, audit = self.project(response, chunk)
            with self.subTest(action=action):
                self.assertEqual(response, before)
                self.assertEqual(candidate["clause_reviews"][0]["classification"], "requires_source_verification")
                projected = candidate["clause_reviews"][0]["obligations"][0]
                for key in ("id", "actor", "action", "target", "source_quote", "force", "applicability", "condition"):
                    self.assertEqual(projected.get(key), atom.get(key))
                self.assertEqual((projected["status"], projected["route"]), ("unresolved", "human"))
                self.assertEqual(audit[0]["original_primary_obligations"], [atom])
                self.assertTrue(audit[0]["current_evidence_binding_verified"])
                self.assertFalse(audit[0]["submission_ready"])

    def test_origin_selection_never_authorizes_other_content_or_unbound_source(self):
        for action in (
            "write keywords from the thesis", "select keywords from the internet",
            "select keywords from the thesis and write an abstract",
            "select keywords from the thesis and obtain department approval",
            "select new keywords from the thesis", "select keywords without thesis provenance",
            "从论文中选取关键词并撰写摘要", "从互联网中选取关键词",
        ):
            response, chunk, _ = fixture()
            response["clause_reviews"][0]["obligations"][0]["action"] = action
            with self.subTest(action=action):
                self.assertEqual(self.project(response, chunk), (response, []))
        for change in ("missing_evidence", "old_hash", "mixed_source", "linked", "duplicate"):
            response, chunk, _ = fixture()
            atom = response["clause_reviews"][0]["obligations"][0]
            atom["action"] = "select keywords from the thesis and ensure each has a clear source"
            if change == "missing_evidence": chunk["evidence_context"] = {}
            elif change == "old_hash": chunk["clauses"][0]["source_span"]["source_sha256"] = "0" * 64
            elif change == "mixed_source":
                clause = chunk["clauses"][0]; source = clause["text"] + "；作者须撰写摘要。"
                clause["text"] = source
                clause["source_span"].update(text=source, end_offset=len(source), source_sha256=hashlib.sha256(source.encode()).hexdigest())
                chunk["evidence_context"][clause["source_span"]["evidence_id"]]["text"] = source
                atom["source_quote"] = source
            elif change == "linked": response["requirements"] = [{"clause_ids": [chunk["clauses"][0]["id"]]}]
            else: response["clause_reviews"][0]["obligations"].append(copy.deepcopy(atom))
            with self.subTest(change=change):
                self.assertEqual(self.project(response, chunk), (response, []))

    def test_pure_origin_generation_rejects_authoring_and_preserves_diagnostics(self):
        response, chunk, result = fixture()
        action = "select keywords from the thesis and ensure each has a clear source"
        response["clause_reviews"][0]["obligations"][0]["action"] = action
        candidate, audit = bridge.prepare_native_response_candidate(response, chunk)
        self.assertEqual(bridge.validate_host_agent_response(candidate, chunk), [])
        self.assertTrue(audit["source_verification_classification_projections"][0]["typed_inventory_preserved"])
        request = native.build_obligation_coverage_request(candidate, chunk, run_id="fresh-select", chunk_index=1)
        wire = source_reference_schema(native.OBLIGATION_COVERAGE_SCHEMA,
            build_source_reference_packet(request), coverage=True, constrain_requirement_links=True)
        self.assertEqual(native_schema_support_errors(native_output_schema(wire)), [])
        result["identified_obligations"][0]["action"] = action
        original = wire_response(request, result)
        frozen = copy.deepcopy(original)
        self.assertTrue(validate_instance(original, wire))
        # Parsing retains rejected observations rather than silently relabeling.
        bad_compiled, _ = compile_source_reference_response(original, request, native.OBLIGATION_COVERAGE_SCHEMA, coverage=True)
        with self.assertRaises(native.SourceVerificationMislabelledAsAuthoringError):
            native.validate_obligation_coverage_response(bad_compiled, request["checks"])
        self.assertEqual(original, frozen)
        result["verdict"] = "source_content_verification_pending"
        result["identified_obligations"][0]["disposition"] = "source_content_verification_pending"
        raw = wire_response(request, result)
        self.assertEqual(validate_instance(raw, wire), [])
        provider = copy.deepcopy(raw)
        pending = wire["properties"]["results"]["items"]["anyOf"][0]["anyOf"][1]
        atom_schema = pending["properties"]["identified_obligations"]["items"]["anyOf"][0]
        for atom in provider["results"][0]["identified_obligations"]:
            for key in atom_schema["properties"]:
                atom.setdefault(key, None)
        self.assertEqual(validate_instance(provider, native_output_schema(wire)), [])
        provider_compiled, _ = compile_source_reference_response(provider, request,
            native.OBLIGATION_COVERAGE_SCHEMA, coverage=True, provider_nullable_optionals=True)
        self.assertEqual(native.validate_obligation_coverage_response(provider_compiled, request["checks"]), [result])
        compiled, receipt = compile_source_reference_response(raw, request, native.OBLIGATION_COVERAGE_SCHEMA, coverage=True)
        self.assertEqual(native.validate_obligation_coverage_response(compiled, request["checks"]), [result])
        self.assertTrue(receipt["semantic_verdicts_unchanged"])
        for change in ("missing_id", "foreign_id", "double_id", "mixed_authoring", "wrong_verdict"):
            bad = copy.deepcopy(raw); atom = bad["results"][0]["identified_obligations"][0]
            if change == "missing_id": atom.pop("primary_obligation_id")
            elif change == "foreign_id": atom["primary_obligation_id"] = "old-id"
            elif change == "double_id": bad["results"][0]["identified_obligations"].append(copy.deepcopy(atom))
            elif change == "mixed_authoring":
                other = copy.deepcopy(atom); other["disposition"] = "authoring_content_pending"
                bad["results"][0]["identified_obligations"].append(other)
            else: bad["results"][0]["verdict"] = "source_content_pending"
            with self.subTest(change=change):
                if change != "double_id": self.assertTrue(validate_instance(bad, wire))
                try:
                    observed, _ = compile_source_reference_response(bad, request, native.OBLIGATION_COVERAGE_SCHEMA, coverage=True)
                except ValueError:
                    continue
                with self.assertRaises(native.NativeSemanticReviewError):
                    native.validate_obligation_coverage_response(observed, request["checks"])
        diagnostic = copy.deepcopy(raw)
        diagnostic["results"][0]["verdict"] = "incomplete"
        diagnostic["results"][0]["identified_obligations"].append({
            "source_ref": raw["results"][0]["evidence_refs"][0], "disposition": "unrepresented", "requirement_refs": []})
        self.assertEqual(validate_instance(diagnostic, wire), [])
        self.assertEqual(candidate["requirements"], [])

    def test_origin_generation_constraint_does_not_close_unrelated_authoring_channels(self):
        response, chunk, _ = fixture()
        candidate, _ = self.project(response, chunk)
        request = native.build_obligation_coverage_request(candidate, chunk, run_id="fresh-general", chunk_index=1)
        check = request["checks"][0]
        check["document_text"] = "作者应补充本人真实研究内容。"
        check["review_context"]["source_content_verification_codes"] = []
        wire = source_reference_schema(native.OBLIGATION_COVERAGE_SCHEMA,
            build_source_reference_packet(request), coverage=True, constrain_requirement_links=True)
        # Verdict-shape coupling may use anyOf without closing author work.
        # Assert the channel's actual behavior, not the old schema topology.
        packet = build_source_reference_packet(request)
        ref = packet["checks"][0]["source_spans"][0]["ref_id"]
        for verdict in ("source_content_pending", "incomplete"):
            raw = {"results": [{"check_id": check["check_id"], "verdict": verdict,
                "rationale": "The explicit authoring instruction remains reportable.",
                "evidence_refs": [ref], "identified_obligations": [{
                    "source_ref": ref, "disposition": "authoring_content_pending",
                    "requirement_refs": []}]}]}
            with self.subTest(verdict=verdict):
                self.assertEqual(validate_instance(raw, wire), [])
        self.assertEqual(native_schema_support_errors(native_output_schema(wire)), [])

    def test_captured_typed_inventory_only_changes_pending_responsibility(self):
        response, chunk, _ = fixture(); frozen = copy.deepcopy(response)
        candidate, audit = self.project(response, chunk)
        self.assertEqual(response, frozen)
        expected = copy.deepcopy(response)
        expected["clause_reviews"][0]["classification"] = "requires_source_verification"
        expected["clause_reviews"][0]["obligations"][0].update(status="unresolved", route="human")
        expected["clause_reviews"][0]["reason"] = candidate["clause_reviews"][0]["reason"]
        expected["clause_reviews"][0]["obligations"][0]["reason"] = candidate["clause_reviews"][0]["obligations"][0]["reason"]
        self.assertEqual(candidate, expected)
        self.assertEqual(len(audit), 1)
        self.assertTrue(audit[0]["current_evidence_binding_verified"])
        self.assertTrue(audit[0]["typed_inventory_preserved"])
        self.assertFalse(audit[0]["submission_ready"])
        self.assertEqual(audit[0]["original_primary_obligations"], response["clause_reviews"][0]["obligations"])
        self.assertEqual(audit[0]["projected_primary_obligations"], expected["clause_reviews"][0]["obligations"])
        self.assertEqual(audit[0]["before_response_sha256"], sha256_json(response))
        self.assertEqual(audit[0]["after_response_sha256"], sha256_json(candidate))
        self.assertEqual(self.project(candidate, chunk), (candidate, []))

    def test_complete_bridge_candidate_and_pending_review_roundtrip(self):
        response, chunk, result = fixture()
        candidate, audit = bridge.prepare_native_response_candidate(response, chunk)
        self.assertEqual(bridge.validate_host_agent_response(candidate, chunk), [])
        self.assertEqual(candidate["clause_reviews"][0]["classification"], "requires_source_verification")
        self.assertTrue(audit["source_verification_classification_projections"][0]["typed_inventory_preserved"])
        request = native.build_obligation_coverage_request(candidate, chunk, run_id=chunk["provenance"]["run_id"], chunk_index=1)
        with self.assertRaises(native.SourceVerificationMislabelledAsAuthoringError):
            native.validate_obligation_coverage_response({"results": [copy.deepcopy(result)]}, request["checks"])
        result["verdict"] = "source_content_verification_pending"
        result["identified_obligations"][0]["disposition"] = "source_content_verification_pending"
        raw = wire_response(request, result)
        compiled, _ = compile_source_reference_response(raw, request, native.OBLIGATION_COVERAGE_SCHEMA, coverage=True)
        self.assertEqual(native.validate_obligation_coverage_response(compiled, request["checks"]), [result])
        self.assertEqual(candidate["requirements"], [])

    def test_current_binding_source_scope_and_conflicts_are_required(self):
        for change in ("missing_evidence", "stale_hash", "wrong_quote", "linked", "conflict", "non_explicit", "mixed_status", "duplicate", "new_field", "condition", "bad_force", "bad_applicability"):
            response, chunk, _ = fixture()
            atom = response["clause_reviews"][0]["obligations"][0]
            if change == "missing_evidence": chunk["evidence_context"] = {}
            elif change == "stale_hash": chunk["clauses"][0]["source_span"]["source_sha256"] = "0" * 64
            elif change == "wrong_quote": atom["source_quote"] = "来自另一份文档"
            elif change == "linked": response["requirements"] = [{"clause_ids": [chunk["clauses"][0]["id"]]}]
            elif change == "conflict": response["reported_conflicts"] = [{"clause_ids": [chunk["clauses"][0]["id"]]}]
            elif change == "non_explicit": response["clause_reviews"][0]["normative_basis"] = "ambiguous"
            elif change == "mixed_status": atom["status"] = "unverifiable"
            elif change == "duplicate": response["clause_reviews"][0]["obligations"].append(copy.deepcopy(atom))
            elif change == "new_field": atom["unknown_semantics"] = "do not silently drop me"
            elif change == "condition": atom["condition"] = "if the thesis has keywords"
            elif change == "bad_force": atom["force"] = {"invalid": "required"}
            elif change == "bad_applicability": atom["applicability"] = ["applicable"]
            with self.subTest(change=change): self.assertEqual(self.project(response, chunk), (response, []))

    def test_authoring_external_unknown_conditional_and_quoted_sources_are_not_projected(self):
        pure = "关键词须源自论文并有明确出处。"
        for source in (pure + "作者须撰写摘要。", pure + "学院须审批。", pure + "未知规则须遵守。",
                       pure + "一般3～8个，之间用分号分开。", "如果适用，" + pure,
                       "示例：“" + pure + "”", "关键词不需要源自论文并有明确出处。"):
            response, chunk, _ = fixture(); clause = chunk["clauses"][0]
            clause["text"] = source; clause["source_span"].update(text=source,
                start_offset=0, end_offset=len(source), source_sha256=hashlib.sha256(source.encode()).hexdigest())
            chunk["evidence_context"][clause["source_span"]["evidence_id"]]["text"] = source
            response["clause_reviews"][0]["obligations"][0]["source_quote"] = source
            with self.subTest(source=source): self.assertEqual(self.project(response, chunk), (response, []))

    def test_distinct_typed_atoms_are_all_retained_and_disagreement_rejected(self):
        response, chunk, result = fixture()
        second = copy.deepcopy(response["clause_reviews"][0]["obligations"][0])
        second.update(id="other-atom", action="verify topical correspondence", target="keyword topic")
        response["clause_reviews"][0]["obligations"].append(second)
        candidate, audit = self.project(response, chunk)
        self.assertEqual(len(candidate["clause_reviews"][0]["obligations"]), 2)
        self.assertEqual(len(audit[0]["original_primary_obligations"]), 2)
        request = native.build_obligation_coverage_request(candidate, chunk, run_id="current-offline-run", chunk_index=1)
        result["verdict"] = "source_content_verification_pending"
        result["identified_obligations"][0]["disposition"] = "source_content_verification_pending"
        other = copy.deepcopy(result["identified_obligations"][0]); other.update(primary_obligation_id="other-atom", action=second["action"], target=second["target"])
        result["identified_obligations"].append(other)
        self.assertEqual(native.validate_obligation_coverage_response({"results": [copy.deepcopy(result)]}, request["checks"]), [result])
        for field, value in (("action", "invent content"), ("target", "abstract"), ("applicability", "unknown")):
            wrong = copy.deepcopy(result); wrong["identified_obligations"][0][field] = value
            with self.subTest(field=field), self.assertRaises(native.TypedSourceAtomAlignmentError):
                native.validate_obligation_coverage_response({"results": [wrong]}, request["checks"])

    def test_short_pure_source_and_unresolved_baseline_keep_typed_semantics(self):
        response, chunk, _ = fixture(); source = "论文中的关键词须源自论文，并可追溯至对应原文。"
        clause = chunk["clauses"][0]; clause["text"] = source
        clause["source_span"].update(text=source, start_offset=0, end_offset=len(source), source_sha256=hashlib.sha256(source.encode()).hexdigest())
        chunk["evidence_context"][clause["source_span"]["evidence_id"]]["text"] = source
        response["clause_reviews"][0]["classification"] = "unresolved"
        atom = response["clause_reviews"][0]["obligations"][0]
        atom.update(status="unresolved", source_quote=source, route="human")
        candidate, audit = self.project(response, chunk)
        expected = {**atom, "reason": candidate["clause_reviews"][0]["obligations"][0]["reason"]}
        self.assertEqual(candidate["clause_reviews"][0]["obligations"], [expected])
        self.assertTrue(audit[0]["typed_inventory_preserved"])

    def test_correct_quote_cannot_authorize_unrelated_or_authoring_typed_semantics(self):
        for field, value in (("action", "invent an abstract"), ("action", "write keywords"),
                             ("target", "abstract"), ("actor", "department"),
                             ("force", "optional"), ("action", "unknown operation on keywords")):
            response, chunk, _ = fixture()
            response["clause_reviews"][0]["obligations"][0][field] = value
            with self.subTest(field=field, value=value):
                self.assertEqual(self.project(response, chunk), (response, []))

    def test_redteam_echo_of_unrelated_primary_cannot_gain_pending_acceptance(self):
        response, chunk, result = fixture()
        atom = response["clause_reviews"][0]["obligations"][0]
        atom.update(action="invent an abstract", target="abstract", reason="must write an abstract")
        candidate, audit = self.project(response, chunk)
        self.assertEqual((candidate, audit), (response, []))
        request = native.build_obligation_coverage_request(candidate, chunk, run_id="current-offline-run", chunk_index=1)
        result["verdict"] = "source_content_verification_pending"
        result["identified_obligations"][0].update(action=atom["action"], target=atom["target"], disposition="source_content_verification_pending")
        with self.assertRaises(native.NativeSemanticReviewError):
            native.validate_obligation_coverage_response({"results": [result]}, request["checks"])

    def test_chinese_typed_verification_uses_same_source_bound_human_path(self):
        response, chunk, _ = fixture()
        atom = response["clause_reviews"][0]["obligations"][0]
        atom.update(actor="作者或审查人", action="核验关键词来源与主题对应关系", target="中文关键词列表")
        candidate, audit = self.project(response, chunk)
        self.assertEqual(candidate["clause_reviews"][0]["classification"], "requires_source_verification")
        expected = {**atom, "status": "unresolved", "route": "human",
                    "reason": candidate["clause_reviews"][0]["obligations"][0]["reason"]}
        self.assertEqual(candidate["clause_reviews"][0]["obligations"], [expected])
        self.assertTrue(audit[0]["typed_inventory_preserved"])

    def test_contradictory_model_reason_is_audited_not_reused_as_authoring_prompt(self):
        response, chunk, _ = fixture()
        response["clause_reviews"][0]["reason"] = "The author must write an abstract."
        response["clause_reviews"][0]["obligations"][0]["reason"] = "The author must write an abstract."
        candidate, audit = self.project(response, chunk)
        self.assertEqual(audit[0]["original_primary_review"], response["clause_reviews"][0])
        for record in (candidate["clause_reviews"][0], candidate["clause_reviews"][0]["obligations"][0]):
            self.assertNotIn("write an abstract", record["reason"])
        self.assertFalse(audit[0]["submission_ready"])

    def retry(self, *, persist=False):
        response, chunk, result = fixture(); candidate, _ = self.project(response, chunk)
        frozen = copy.deepcopy(candidate); calls = []
        def reviewer(request, **kwargs):
            calls.append(copy.deepcopy(request)); observed = copy.deepcopy(result)
            if len(calls) == 2 and not persist:
                observed["verdict"] = "source_content_verification_pending"
                observed["identified_obligations"][0]["disposition"] = "source_content_verification_pending"
            compiled, receipt = compile_source_reference_response(wire_response(request, observed), request, native.OBLIGATION_COVERAGE_SCHEMA, coverage=True)
            native.validate_obligation_coverage_response(compiled, request["checks"])
            directory = kwargs["output_dir"]; directory.mkdir(parents=True, exist_ok=True)
            path = directory / "source-reference-compilation.json"; bridge._write_json(path, receipt)
            return {"status": "completed", "results": compiled["results"], "summary": {},
                "request_sha256": sha256_json(request), "response_sha256": sha256_json(compiled),
                "canonical_response_sha256": sha256_json(compiled), "source_reference_compilation_path": str(path),
                "source_reference_compilation_sha256": bridge.sha256_file(path)}
        with tempfile.TemporaryDirectory() as td, patch.object(bridge, "run_native_semantic_review", side_effect=reviewer), patch.object(bridge.time, "sleep"):
            args = dict(review_dir=Path(td), run_id="current-offline-run", chunk_index=1, attempt=1,
                host_runtime="codex", model="gpt-5.6-luna", timeout=5, agent_id="main", runner="exec",
                binary="codex", config_path=None, controller=bridge.RunController(), output_policy="review_draft")
            if persist:
                with self.assertRaises(bridge.IndependentObligationReviewError): bridge._run_independent_obligation_coverage_review(candidate, chunk, **args)
                self.assertEqual(list(Path(td).rglob("obligation-analysis-ledger.json")), [])
            else:
                pointer = bridge._run_independent_obligation_coverage_review(candidate, chunk, **args)
                self.assertEqual(pointer["status"], "completed")
                ledger = json.loads(next(Path(td).rglob("obligation-analysis-ledger.json")).read_text())
                self.assertFalse(ledger["submission_ready"])
                self.assertEqual(ledger["obligations"][0]["disposition"], "source_content_verification_pending")
            self.assertEqual(len(calls), 2); self.assertEqual(candidate, frozen)
            self.assertEqual(calls[0]["checks"], calls[1]["checks"])
            self.assertEqual(calls[1]["retry_feedback"]["code"], native.SourceVerificationMislabelledAsAuthoringError.code)

    def test_typed_mislabel_receives_only_one_same_candidate_independent_read(self): self.retry()
    def test_persistent_typed_mislabel_exhausts_without_success_ledger(self): self.retry(persist=True)


if __name__ == "__main__": unittest.main()
