"""Captured incident replay and generic source/transaction/score adversaries."""
from __future__ import annotations

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
import apply_format_spec
from capability_planner import plan_capabilities
from draft_scorecard import build_scorecard
from pipeline_finding import integrity_findings
from repair_transaction import repair_receipt, validate_repair_receipt
from responsibility_ledger import (canonical_atom_identity, build_responsibility_ledger,
                                   requirement_evaluation_units, validate_requirement_evaluation_units)
from responsibility_projection import project_redundant_render_entities
from property_receipts import evaluation_unit_receipt_errors
from requirements_engine import build_llm_request
from semantic_contract import sha256_json
from thesis_format_pipeline import capability_gate_blocked


def captured_cases():
    data = json.loads((ROOT / "tests/fixtures/responsibility-entity-cases.json").read_text())
    for case in data["cases"]:
        chunk = case["chunk"]
        request = build_llm_request([], chunk["clauses"], {"evidence": list(chunk["evidence_context"].values())},
                                    {}, "full", contract_version="3.0")
        for field in ("declaration_anchor_preference", "declaration_anchor_candidates", "runtime_context"):
            request[field] = copy.deepcopy(chunk[field])
        chunk.update(request)
    return data["cases"]


class ResponsibilityRepairTests(unittest.TestCase):
    def test_captured_two_incidents_preserve_source_and_pending_duties(self):
        for case, expected_count in zip(captured_cases(), (13, 1)):
            raw, chunk = case["raw"], case["chunk"]
            before = copy.deepcopy(raw)
            candidate, audit = bridge.prepare_native_response_candidate(raw, chunk)
            self.assertEqual(raw, before)
            self.assertEqual(len(candidate["requirements"]), expected_count)
            # Compare the same normalization stage: native optional nulls and
            # unreferenced informational zero inventories have canonical forms.
            # Every real obligation and all other review fields remain exact.
            expected = bridge.normalize_native_response(raw, chunk["response_schema"])
            self.assertEqual(candidate["clause_reviews"], expected["clause_reviews"])
            self.assertEqual(bridge.validate_host_agent_response(candidate, chunk), [])
            transaction = audit["repair_transaction"]
            self.assertIsNotNone(transaction)
            self.assertFalse(transaction["submission_ready"])
            self.assertTrue(transaction["independent_review_required"])
            self.assertEqual(transaction["result_sha256"], sha256_json(candidate))
            for field in ("provenance", "clauses", "evidence_context"):
                self.assertNotIn("$." + field, transaction["allowed_changed_paths"])

    def test_admin_copies_do_not_authorize_losing_unique_effects(self):
        for change in ("unique_value", "applicability", "foreign_evidence", "new_prerequisite",
                       "empty_inventory", "second_entity", "wrong_source_hash", "unknown_property"):
            case = captured_cases()[1]
            raw, chunk = case["raw"], case["chunk"]
            req = raw["requirements"][1]
            if change == "unique_value": req["properties"]["institution"] = "Another institution"
            elif change == "applicability": req["applicability"] = {"status": "unresolved"}
            elif change == "foreign_evidence": req["evidence_ids"] = ["foreign"]
            elif change == "new_prerequisite": req["input_prerequisites"] = [{"required_fields": ["approval"]}]
            elif change == "empty_inventory":
                next(r for r in raw["clause_reviews"] if r["clause_id"] in req["clause_ids"])["obligations"] = []
            elif change == "second_entity": raw["requirements"].append(copy.deepcopy(raw["requirements"][0]))
            elif change == "wrong_source_hash": chunk["clauses"][0]["source_span"]["source_sha256"] = "f" * 64
            else: req["properties"]["future_operation"] = None
            original = copy.deepcopy(raw)
            with self.subTest(change=change):
                candidate, _ = project_redundant_render_entities(raw, chunk, validate=bridge.validate_host_agent_response)
                self.assertIsNone(candidate)
                self.assertEqual(raw, original)

    def test_no_lawful_projection_has_explicit_terminal_plan_and_stage_hashes(self):
        case = captured_cases()[1]
        case["raw"]["requirements"][1]["properties"]["institution"] = "Unproved unique operation"
        with self.assertRaises(ValueError) as caught:
            bridge.prepare_native_response_candidate(case["raw"], case["chunk"])
        error = caught.exception
        self.assertEqual(error.repair_plan["status"], "repair_plan_unavailable")
        self.assertEqual(error.repair_plan["candidate_sha256"], sha256_json(error.repair_base_candidate))
        self.assertEqual(error.stage_candidates[-1]["stage"], "compiled_candidate")
        self.assertEqual(error.repair_plan["error_bundle_sha256"], sha256_json(error.initial_error_records))

    def test_transaction_tamper_and_old_chunk_are_rejected(self):
        before, after = {"requirements": [1, 2], "clause_reviews": []}, {"requirements": [1], "clause_reviews": []}
        chunk = {"provenance": {"run_id": "current", "source_sha256": "a" * 64}}
        errors, proofs = [{"code": "bounded_duplicate"}], [{"unique_entity": True}]
        receipt = repair_receipt(before, after, errors, proofs, chunk)
        self.assertTrue(validate_repair_receipt(receipt, before, after, errors, chunk))
        for field in ("result_sha256", "error_bundle_sha256", "source_chunk_sha256", "allowed_changed_paths"):
            forged = copy.deepcopy(receipt); forged[field] = "forged"
            self.assertFalse(validate_repair_receipt(forged, before, after, errors, chunk))
        self.assertFalse(validate_repair_receipt(receipt, before, after, errors, {"provenance": {"run_id": "old"}}))

    def test_explicit_atom_identity_ignores_proposal_id_but_not_semantic_target(self):
        atom = {"id": "proposal-one", "actor": "author", "action": "format", "target": "Chinese keywords",
                "source_quote": "Keywords must use semicolons", "force": "required", "condition": "always"}
        source = {"source_sha256": "a" * 64, "span": {"start": 0, "end": 33}}
        first = canonical_atom_identity(source, atom)
        atom["id"] = "proposal-two"
        self.assertEqual(first, canonical_atom_identity(source, atom))
        atom["target"] = "English keywords"
        self.assertNotEqual(first, canonical_atom_identity(source, atom))

    def test_unknown_legacy_dimensions_are_not_normative_weights_or_execution(self):
        case = captured_cases()[1]
        candidate, _ = bridge.prepare_native_response_candidate(case["raw"], case["chunk"])
        ledger = build_responsibility_ledger(candidate, case["chunk"]["clauses"])
        self.assertFalse(ledger["execution_proof"])
        self.assertFalse(ledger["submission_ready"])
        self.assertTrue(any(a["route"] == "human" for a in ledger["atoms"]))
        self.assertTrue(all(a["force"] == "unknown" for a in ledger["atoms"]))
        self.assertEqual(ledger["authorization_status"], "not_an_authorization")
        self.assertTrue(ledger["review_coverage_complete"])

    def test_responsibility_ledger_rejects_partial_duplicate_and_dangling_routes(self):
        case = captured_cases()[1]
        response = copy.deepcopy(case["raw"])
        clauses = case["chunk"]["clauses"]
        response["clause_reviews"] = response["clause_reviews"][:-1]
        partial = build_responsibility_ledger(response, clauses)
        self.assertEqual(partial["status"], "invalid")
        self.assertFalse(partial["review_coverage_complete"])
        self.assertIn("missing_clause_review", {item["code"] for item in partial["errors"]})

        duplicate = copy.deepcopy(case["raw"])
        duplicate["clause_reviews"].append(copy.deepcopy(duplicate["clause_reviews"][0]))
        repeated = build_responsibility_ledger(duplicate, clauses)
        self.assertEqual(repeated["status"], "invalid")
        self.assertIn("duplicate_clause_review", {item["code"] for item in repeated["errors"]})

        dangling = copy.deepcopy(case["raw"])
        dangling["requirements"][0]["clause_ids"] = ["foreign-clause"]
        invalid_edge = build_responsibility_ledger(dangling, clauses)
        self.assertEqual(invalid_edge["status"], "invalid")
        self.assertIn("unknown_requirement_source", {item["code"] for item in invalid_edge["errors"]})

    def test_score_units_are_recomputed_from_current_bound_semantic_ledger(self):
        source_sha = "a" * 64
        source_text = "关键词之间用分号隔开"
        clause = {
            "id": "C-random", "text": source_text, "evidence_ids": ["E-random"],
            "source_span": {
                "evidence_id": "E-random", "start_offset": 0, "end_offset": len(source_text),
                "text": source_text, "source_sha256": hashlib.sha256(source_text.encode()).hexdigest(),
            },
        }
        atom = {
            "id": "obligation-random", "status": "covered", "actor": "author",
            "action": "separate", "target": "keywords", "source_quote": "用分号隔开",
            "force": "required", "applicability": "applicable",
        }
        provenance = {
            "run_id": "fresh-run", "source_sha256": source_sha,
            "clause_sha256": sha256_json([clause]),
        }
        review = {"classification": "executable", "obligations": [atom]}
        expected_units = requirement_evaluation_units(
            {clause["id"]: review}, {clause["id"]: clause}, [clause["id"]], source_sha,
        )
        spec = {
            "run_id": "fresh-run", "semantic_review_provenance": provenance,
            "requirements": [{"id": "R-current", "clause_ids": [clause["id"]],
                              "evaluation_units": expected_units}],
        }
        ledger = {
            "provenance": copy.deepcopy(provenance), "response_sha256": "b" * 64,
            "clauses": [{
                "clause_id": clause["id"],
                "source_text_sha256": hashlib.sha256(source_text.encode()).hexdigest(),
                "classification": "executable", "obligations": [copy.deepcopy(atom)],
            }],
        }
        errors, units_by_requirement = validate_requirement_evaluation_units(
            spec, ledger, [clause], expected_run_id="fresh-run",
            expected_response_sha256="b" * 64,
        )
        self.assertEqual(errors, [])
        self.assertEqual(units_by_requirement, {"R-current": expected_units})

        for mutation in ("invented_weight", "dropped_unit", "stale_source", "old_run", "old_response"):
            changed_spec, changed_ledger, changed_clause = (
                copy.deepcopy(spec), copy.deepcopy(ledger), copy.deepcopy(clause)
            )
            expected_response = "b" * 64
            if mutation == "invented_weight":
                changed_spec["requirements"][0]["evaluation_units"][0]["force"] = "optional"
            elif mutation == "dropped_unit":
                changed_spec["requirements"][0].pop("evaluation_units")
            elif mutation == "stale_source":
                changed_clause["text"] += "。"
            elif mutation == "old_run":
                changed_ledger["provenance"]["run_id"] = "old-run"
            else:
                expected_response = "c" * 64
            with self.subTest(mutation=mutation):
                errors, _ = validate_requirement_evaluation_units(
                    changed_spec, changed_ledger, [changed_clause],
                    expected_run_id="fresh-run", expected_response_sha256=expected_response,
                )
                self.assertTrue(errors)

    def test_capability_apply_gate_binds_spec_mode_run_and_blocks_before_execution(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            spec_path, report_path = root / "format-spec.json", root / "capability.json"
            spec = {"run_id": "fresh-run", "requirements": []}
            spec_path.write_text(json.dumps(spec), encoding="utf-8")

            def artifact(path):
                payload = path.read_bytes()
                return {"path": str(path.resolve()), "bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest()}

            report = {
                "stage": "capability_preflight", "compliance_mode": "full",
                "execution_ready": False,
                "findings": [{"code": "capability.backend_gap", "blocking": True}],
                "provenance": {"stage": "capability_preflight", "run_id": "fresh-run",
                               "format_spec": artifact(spec_path)},
            }
            report_path.write_text(json.dumps(report), encoding="utf-8")
            manifest = {
                "run_id": "fresh-run", "capability_preflight": str(report_path.resolve()),
                "format_spec_record": artifact(spec_path),
                "capability_preflight_record": artifact(report_path),
            }
            errors = apply_format_spec.validate_capability_report_for_apply(
                report, report_path=report_path, format_spec_path=spec_path, spec=spec,
                compliance_mode="full", pipeline_manifest=manifest,
            )
            self.assertIn("capability_report_full_mode_blocked", errors)

            report["compliance_mode"] = "supported_subset"
            report["execution_ready"] = True
            report["findings"][0]["blocking"] = False
            report_path.write_text(json.dumps(report), encoding="utf-8")
            manifest["capability_preflight_record"] = artifact(report_path)
            self.assertEqual(apply_format_spec.validate_capability_report_for_apply(
                report, report_path=report_path, format_spec_path=spec_path, spec=spec,
                compliance_mode="supported_subset", pipeline_manifest=manifest,
            ), [])

            spec_path.write_text(json.dumps({**spec, "requirements": [{"id": "tampered"}]}),
                                 encoding="utf-8")
            errors = apply_format_spec.validate_capability_report_for_apply(
                report, report_path=report_path, format_spec_path=spec_path, spec=spec,
                compliance_mode="supported_subset", pipeline_manifest=manifest,
            )
            self.assertTrue(any("sha256_mismatch" in error for error in errors))

    def test_apply_cli_blocks_full_capability_gap_before_opening_docx(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            spec_path, report_path = root / "format-spec.json", root / "capability.json"
            missing_input = root / "must-not-be-opened.docx"
            output = root / "must-not-be-created.docx"
            output_dir = root / "must-not-be-created"
            spec = {
                "schema_version": "1.0", "source_document": "source.docx",
                "roles": {}, "requirements": [], "status": "rule_resolved",
                "run_id": "fresh-run", "compliance_mode": "full",
                "clause_compliance": [{
                    "clause_id": "C1", "evidence_ids": [], "scope": "informational",
                    "status": "informational", "requirement_ids": [], "reason": "test fixture",
                }],
            }
            spec_path.write_text(json.dumps(spec), encoding="utf-8")

            def artifact(path):
                payload = path.read_bytes()
                return {"path": str(path.resolve()), "bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest()}

            report = {
                "stage": "capability_preflight", "compliance_mode": "full",
                "execution_ready": False,
                "findings": [{"code": "capability.backend_gap", "blocking": True}],
                "provenance": {"stage": "capability_preflight", "run_id": "fresh-run",
                               "format_spec": artifact(spec_path)},
            }
            report_path.write_text(json.dumps(report), encoding="utf-8")
            with patch.object(apply_format_spec, "Document") as document:
                with self.assertRaisesRegex(SystemExit, "capability_report_full_mode_blocked"):
                    apply_format_spec.main([
                        str(missing_input), str(spec_path), str(output),
                        "--out-dir", str(output_dir), "--compliance-mode", "full",
                        "--capability-report", str(report_path),
                    ])
                document.assert_not_called()
            self.assertFalse(output.exists())
            self.assertFalse(output_dir.exists())

    def test_unregistered_checker_integrity_blocks_both_modes_even_forged_nonblocking(self):
        registry = json.loads((ROOT / "resources/backend-capabilities.default.json").read_text())
        spec = {"requirements": [{"id": "dynamic", "role": "body_text", "properties": {"font": {"size_pt": 12}},
                "verification": {"mode": "static_docx", "checks": ["read"], "checker_ids": ["not_registered"]}}]}
        for mode in ("full", "supported_subset"):
            report = plan_capabilities(spec, registry, mode)
            self.assertTrue(capability_gate_blocked(report, mode))
            errors = integrity_findings(report["findings"])
            self.assertTrue(errors)
            for item in errors: item.update(blocking=False, gate_class="quality")
            self.assertTrue(capability_gate_blocked(report, mode))


class UnitScoreTests(unittest.TestCase):
    def setUp(self):
        self.binding = {"run_id": "fresh", "case_id": "case", "source_sha256": "a" * 64, "format_spec_sha256": "b" * 64}

    def unit(self, key="c", force="required", applicability="applicable"):
        return {"evaluation_unit_id": "EU-" + key * 32, "canonical_obligation_key": key * 64,
                "force": force, "applicability": applicability, "route": "automatic",
                "source_sha256": "a" * 64, "source_span_sha256": "d" * 64, "semantic_basis": "typed_source_atom"}

    def card(self, receipts, findings=None, authoritative_units=None):
        audit = {"missing_count": 0, "unexpected_count": 0, "duplicate_count": 0,
                 "expected_receipt_ids": [r["receipt_id"] for r in receipts], "receipts": receipts}
        return build_scorecard(
            self.binding, audit, [], findings or [],
            evaluation_units_by_requirement=authoritative_units,
        )

    def test_split_properties_and_duplicate_findings_do_not_inflate_denominator(self):
        automatic, recommended = self.unit(), self.unit("e", "recommended")
        receipts = [{"receipt_id": "p1", "requirement_id": "one", "status": "verified", "evaluation_units": [automatic]},
                    {"receipt_id": "p2", "requirement_id": "two", "status": "failed", "evaluation_units": [automatic]},
                    {"receipt_id": "p3", "requirement_id": "three", "status": "verified", "evaluation_units": [recommended]}]
        finding = {"role": "body_text", "property": "size", "actual": 11, "expected": 12}
        card = self.card(receipts, [finding, copy.deepcopy(finding)], {
            "one": [automatic], "two": [automatic], "three": [recommended],
        })
        self.assertEqual(card["total_weight"], 7)
        self.assertEqual(card["earned_weight"], 2)
        self.assertEqual(card["score"], 28.57)
        self.assertEqual(len(card["evaluation_units"]), 2)
        self.assertEqual(len([e for e in card["entries"] if e["kind"] == "finding"]), 1)
        self.assertFalse(card["submission_ready"])

    def test_unknown_force_and_applicability_stay_unweighted(self):
        legacy, unknown_applicability = self.unit(force="unknown"), self.unit("e", applicability="unknown")
        receipts = [{"receipt_id": "p1", "requirement_id": "one", "status": "verified", "evaluation_units": [legacy]},
                    {"receipt_id": "p2", "requirement_id": "two", "status": "verified", "evaluation_units": [unknown_applicability]}]
        card = self.card(receipts, authoritative_units={
            "one": [legacy], "two": [unknown_applicability],
        })
        self.assertIsNone(card["score"])
        self.assertEqual(len(card["ledgers"]["U"]), 2)

    def test_source_verification_is_human_pending_not_automatic_score(self):
        human = self.unit("f", force="required")
        human["route"] = "human"
        # A verified DOCX property is not evidence that a human source review
        # occurred. The same evaluation unit must stay out of the F denominator.
        card = self.card(
            [{"receipt_id": "p1", "requirement_id": "one", "status": "verified",
              "evaluation_units": [human]}],
            authoritative_units={"one": [human]},
        )
        unit = card["evaluation_units"][0]
        self.assertEqual(unit["ledger"], "H")
        self.assertEqual(unit["status"], "pending")
        self.assertIsNone(unit["weight"])
        self.assertEqual(card["total_weight"], 0)
        self.assertIsNone(card["score"])
        self.assertTrue(any(entry["kind"] == "human_review"
                            and entry["status"] == "pending"
                            and entry["detail"].get("evaluation_unit_id") == human["evaluation_unit_id"]
                            for entry in card["entries"]))

    def test_source_verification_route_is_human_owned(self):
        from responsibility_ledger import route_for_obligation
        self.assertEqual(route_for_obligation("requires_source_verification", "covered"), "human")

    def test_stale_hash_conflicting_unit_unknown_force_and_integrity_are_rejected(self):
        for mutation in ("stale", "force", "conflicting", "identity"):
            receipts = [{"receipt_id": "p1", "requirement_id": "one", "status": "verified", "evaluation_units": [self.unit()]}]
            authority = {"one": [self.unit()]}
            unit = receipts[0]["evaluation_units"][0]
            if mutation == "stale": unit["source_sha256"] = "f" * 64
            elif mutation == "force": unit["force"] = "high"
            elif mutation == "identity": unit["evaluation_unit_id"] = "EU-" + "f" * 32
            else:
                receipts.append({"receipt_id": "p2", "requirement_id": "two", "status": "verified",
                                 "evaluation_units": [self.unit(force="optional")]})
                authority["two"] = [self.unit()]
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.card(receipts, authoritative_units=authority)
        with self.assertRaisesRegex(ValueError, "verified current-run source ledger"):
            self.card([{"receipt_id": "p", "requirement_id": "one", "status": "verified",
                        "evaluation_units": [self.unit()]}])
        with self.assertRaises(ValueError):
            self.card([], [{"code": "capability.contract_binding_error", "blocking": False}])

    def test_sidecar_units_recomputed_from_current_spec(self):
        reqs = [{"id": "current", "evaluation_units": [self.unit()]}]
        receipts = [{"receipt_id": "p", "requirement_id": "current", "evaluation_units": [self.unit()]}]
        self.assertEqual(evaluation_unit_receipt_errors(receipts, reqs), [])
        receipts[0]["evaluation_units"][0]["force"] = "optional"
        self.assertTrue(evaluation_unit_receipt_errors(receipts, reqs))
