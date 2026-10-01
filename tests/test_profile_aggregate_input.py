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
from capability_planner import plan_capabilities
from format_contract_guards import input_prerequisite_errors, registered_input_key
from format_spec_validation import validate_instance
from host_review_contract import validate_response
from host_review_schema import native_output_schema, native_schema_support_errors
from input_resolver import input_conflicts, input_value_type_valid, resolve_input
from requirements_engine import build_llm_request


def prerequisite(key="thesis_profile", kind="metadata"):
    return {"kind": kind, "key": key, "required": True,
            "reason": "The operation consumes the supplied input at this scope."}


class ProfileAggregateInputTests(unittest.TestCase):
    def setUp(self):
        self.profile = {"schema_version": "1.0", "degree_level": "master",
                        "writing_language": "zh", "security_level": "public",
                        "has_appendices": False, "co_supervisor_count": 0,
                        "cover_metadata": {"title_zh": "Current source title"}}
        self.spec = {"requirements": [{
            "id": "R-current", "role": "body_text", "properties": {"font": {"size_pt": 12}},
            "clause_ids": ["C-current"], "input_prerequisites": [prerequisite()],
        }], "clause_compliance": [{
            "clause_id": "C-current", "scope": "docx", "status": "pending_execution",
            "requirement_ids": ["R-current"],
        }]}
        self.registry = json.loads((ROOT / "resources/backend-capabilities.default.json").read_text())

    def packet(self):
        text = "正文使用宋体"
        clause = {"id": "C-current", "text": text, "evidence_ids": ["E-current"],
                  "source_kind": "paragraph", "location": {}, "part_index": 0,
                  "source_span": {"evidence_id": "E-current", "text": text,
                                  "start_offset": 0, "end_offset": len(text),
                                  "source_sha256": hashlib.sha256(text.encode()).hexdigest()}}
        chunk = build_llm_request([], [clause], {"evidence": [{
            "id": "E-current", "text": text, "kind": "paragraph"}]}, {}, "full",
            contract_version="3.0")
        response = {"contract_version": "3.0", "requirements": [{
            "role": "body_text", "properties": {"font": {"cjk": "SimSun", "size_pt": 12}},
            "clause_ids": ["C-current"], "evidence_ids": ["E-current"],
            "confidence": 0.9, "reason": "Exact source font.",
            "input_prerequisites": [prerequisite()],
        }], "clause_reviews": [{"clause_id": "C-current", "classification": "executable",
            "reason": "Current source operation.", "obligations": [{"id": "source_clause",
                "status": "covered", "reason": "Represented in the font requirement."}]}],
            "unsupported_items": [], "reported_conflicts": []}
        return chunk, response

    def test_only_explicit_aggregate_and_metadata_kind_are_registered(self):
        self.assertTrue(registered_input_key("thesis_profile"))
        self.assertEqual(input_prerequisite_errors(self.spec), [])
        for key in ("runtime", "source_inventory", "template_profile", "thesis_profile.",
                    "thesis_profile.*", "thesis_profile.any_field", "thesis_profile.cover_metadata.any_field"):
            with self.subTest(key=key):
                self.assertFalse(registered_input_key(key))
        for kind in ("runtime", "source_content", "template_resource"):
            self.spec["requirements"][0]["input_prerequisites"] = [prerequisite(kind=kind)]
            self.assertTrue(input_prerequisite_errors(self.spec))

    def test_resolves_exact_whole_profile_without_mutation(self):
        original = copy.deepcopy(self.profile)
        present, namespace, value = resolve_input("thesis_profile", metadata=self.profile)
        self.assertTrue(present)
        self.assertEqual(namespace, "thesis_profile")
        self.assertIs(value, self.profile)
        self.assertEqual(self.profile, original)
        self.assertTrue(input_value_type_valid("thesis_profile", value))

    def test_missing_aggregate_never_searches_unrelated_inventory_or_cover(self):
        for metadata in (None, {}):
            self.assertFalse(resolve_input("thesis_profile", metadata=metadata,
                source_inventory={"security_level": "public", "cover_metadata": self.profile["cover_metadata"]})[0])
        present, _, value = resolve_input("thesis_profile", source_inventory={"thesis_profile": self.profile})
        self.assertTrue(present)
        self.assertIs(value, self.profile)
        # A cover-only profile object remains only a partial profile, not security metadata.
        partial = {"cover_metadata": self.profile["cover_metadata"]}
        self.assertFalse(resolve_input("thesis_profile.security_level", metadata=partial)[0])

    def test_malformed_supplied_aggregate_remains_visible_to_type_guard(self):
        for value in ("", [], (), "master", [self.profile], True, 1, {"has_appendices": "false"},
                      {"co_supervisor_count": True}, {"degree_level": []},
                      {"cover_metadata": []}, {"cover_metadata": {"title_zh": []}}):
            with self.subTest(value=value):
                present, _, resolved = resolve_input("thesis_profile", metadata=value,
                    source_inventory={"thesis_profile": self.profile})
                self.assertTrue(present)
                self.assertEqual(resolved, value)
                self.assertFalse(input_value_type_valid("thesis_profile", resolved))

    def test_canonical_pending_nulls_and_false_zero_are_not_invented_or_lost(self):
        pending = {"schema_version": "1.0", "degree_level": None, "writing_language": None,
                   "metadata_status": "pending", "has_appendices": False, "co_supervisor_count": 0}
        self.assertTrue(input_value_type_valid("thesis_profile", pending))
        self.assertFalse(resolve_input("thesis_profile.degree_level", metadata=pending)[0])
        self.assertTrue(resolve_input("thesis_profile.has_appendices", metadata=pending)[0])
        self.assertTrue(resolve_input("thesis_profile.co_supervisor_count", metadata=pending)[0])

    def test_explicit_profile_conflict_is_not_resolved_by_selection(self):
        other = {**self.profile, "security_level": "classified"}
        self.assertEqual(input_conflicts("thesis_profile", metadata=self.profile,
            source_inventory={"thesis_profile": copy.deepcopy(self.profile)}), [])
        self.assertEqual(len(input_conflicts("thesis_profile", metadata=self.profile,
            source_inventory={"thesis_profile": other})), 1)
        report = plan_capabilities(self.spec, self.registry, "full", metadata=self.profile,
            source_inventory={"thesis_profile": other})
        self.assertFalse(report["execution_ready"])
        self.assertTrue(report["requirements"][0]["input_conflicts"])

    def test_present_aggregate_resolves_requirement_in_both_modes(self):
        for mode in ("full", "supported_subset"):
            report = plan_capabilities(self.spec, self.registry, mode, metadata=self.profile)
            self.assertEqual(report["requirements"][0]["missing_declared_inputs"], [])
            self.assertEqual(report["requirements"][0]["input_type_errors"], [])
            self.assertTrue(report["execution_ready"])

    def test_aggregate_cannot_satisfy_missing_required_field(self):
        self.spec["requirements"][0]["input_prerequisites"].append(
            prerequisite("thesis_profile.cover_metadata.approval_number"))
        report = plan_capabilities(self.spec, self.registry, "full", metadata=self.profile)
        self.assertEqual(report["requirements"][0]["missing_declared_inputs"],
                         ["thesis_profile.cover_metadata.approval_number"])
        self.assertFalse(report["execution_ready"])

    def test_missing_or_wrong_type_root_is_not_ready(self):
        for value in (None, {}, [], "master", {"cover_metadata": {"title_zh": []}}):
            with self.subTest(value=value):
                report = plan_capabilities(self.spec, self.registry, "full", metadata=value)
                item = report["requirements"][0]
                self.assertTrue(item["missing_declared_inputs"] or item["input_type_errors"])
                self.assertFalse(report["execution_ready"])

    def test_wrong_kind_root_is_integrity_blocker_even_in_subset(self):
        self.spec["requirements"][0]["input_prerequisites"] = [prerequisite(kind="runtime")]
        for mode in ("full", "supported_subset"):
            report = plan_capabilities(self.spec, self.registry, mode, metadata=self.profile)
            self.assertFalse(report["execution_ready"])
            self.assertTrue(any(f["code"] == "capability.contract_binding_error" and f["blocking"]
                                for f in report["findings"]))

    def test_chunk_schema_and_shared_validator_accept_root_without_projection(self):
        chunk, response = self.packet()
        original = copy.deepcopy(response)
        self.assertEqual(validate_response(response, chunk), [])
        self.assertEqual(response, original)
        self.assertEqual(native_schema_support_errors(native_output_schema(chunk["response_schema"])), [])
        schema = json.loads((ROOT / "schema/format-spec.schema.json").read_text())
        self.assertEqual(validate_instance(prerequisite(), schema["$defs"]["inputPrerequisiteSpec"], schema), [])

    def test_shared_validator_rejects_wrong_kind_unknown_path_and_malformed_array(self):
        chunk, response = self.packet()
        for item in (prerequisite(kind="runtime"), prerequisite("thesis_profile.unregistered"), None):
            response["requirements"][0]["input_prerequisites"] = [item] if item else "malformed"
            self.assertTrue(validate_response(response, chunk))

    def test_valid_root_to_cover_or_scalar_retry_remains_unauthorized(self):
        chunk, response = self.packet()
        for key in ("thesis_profile.cover_metadata", "thesis_profile.security_level"):
            changed = copy.deepcopy(response)
            changed["requirements"][0]["input_prerequisites"][0]["key"] = key
            errors = [{"code": "input_prerequisite_namespace",
                       "json_pointer": "$.requirements[0].input_prerequisites[0].key",
                       "response_sha256": bridge._response_sha256(response)}]
            allowed = bridge._retry_changes_allowed(errors, bridge._retry_change_paths(response, changed),
                contract_version="3.0", previous_response=response, current_response=changed, chunk=chunk)
            self.assertFalse(allowed)


if __name__ == "__main__":
    unittest.main()
