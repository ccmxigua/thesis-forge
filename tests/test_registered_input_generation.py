"""Generation uses registered scopes without guessing presence or aliases."""
import copy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import host_agent_bridge as bridge
from format_contract_guards import (
    registered_input_catalog, registered_input_key,
    input_prerequisite_generation_schema, input_prerequisite_errors,
)
from format_spec_validation import validate_instance
from host_review_contract import contract_error_records
from host_review_schema import native_output_schema, native_schema_support_errors, normalize_native_response
from input_resolver import resolve_input
import test_profile_aggregate_input as profile_tests
from test_profile_aggregate_input import prerequisite


class RegisteredInputGenerationTests(unittest.TestCase):
    def setUp(self):
        self.base = json.loads((ROOT / "schema/format-spec.schema.json").read_text())["$defs"]["inputPrerequisiteSpec"]

    def test_catalog_all_registered_kinds_and_scopes_are_distinct(self):
        catalog = registered_input_catalog()
        for kind, keys in catalog.items():
            for key in keys:
                self.assertTrue(registered_input_key(key), key)
                self.assertEqual(input_prerequisite_errors({"requirements": [{
                    "input_prerequisites": [prerequisite(key, kind)]}]}), [])
        self.assertIn("thesis_profile", catalog["metadata"])
        self.assertIn("thesis_profile.cover_metadata", catalog["metadata"])
        self.assertNotIn("thesis_profile", catalog["source_content"])
        self.assertIn("source_inventory.abstract", catalog["source_content"])
        self.assertIn("source_inventory.content", catalog["source_content"])
        self.assertIn("runtime.anchor_inventory.selected", catalog["runtime"])
        self.assertEqual(catalog, registered_input_catalog())

    def test_dynamic_nested_paths_keep_source_and_template_extensibility(self):
        context = {"source_inventory": {"abstract": {"language_variant": {"ready": False}},
                      "tables": {"count": 0}, "abstract_zh": "unregistered", "body": {"bad.key": 9}},
                   "template_profile": {"resources": {"another_document": {"status": None}}},
                   "runtime_inventory": {"anchor_inventory": {"selected": {"name": "x"}, "guess": 1}}}
        existing = [{"input_prerequisites": [prerequisite("source_inventory.content.future_section", "source_content"),
                                              prerequisite("source_inventory.thesis_content", "source_content")]}]
        original = copy.deepcopy((context, existing))
        catalog = registered_input_catalog(context, existing)
        self.assertEqual((context, existing), original)
        for key in ("source_inventory.abstract.language_variant.ready", "source_inventory.tables.count",
                    "source_inventory.content.future_section"):
            self.assertIn(key, catalog["source_content"])
        self.assertIn("template_profile.resources.another_document.status", catalog["template_resource"])
        self.assertNotIn("source_inventory.abstract_zh", catalog["source_content"])
        self.assertNotIn("source_inventory.thesis_content", catalog["source_content"])
        self.assertNotIn("source_inventory.body.bad.key", catalog["source_content"])
        self.assertNotIn("runtime.anchor_inventory.guess", catalog["runtime"])
        # Offline contracts still accept registered nested scopes; generation
        # can enumerate them from a new input or an existing declaration.
        self.assertTrue(registered_input_key("source_inventory.abstract.unseen_document_field"))

    def test_local_and_native_schema_couple_kind_and_exact_keys(self):
        schema = input_prerequisite_generation_schema(self.base, registered_input_catalog())
        native = native_output_schema(schema)
        self.assertEqual(native_schema_support_errors(native), [])
        for candidate_schema in (schema, native):
            for kind, keys in registered_input_catalog().items():
                for key in keys:
                    self.assertEqual(validate_instance(prerequisite(key, kind), candidate_schema), [])
            for key, kind in (("source_inventory.thesis_content", "source_content"),
                              ("source_inventory.abstract_zh", "source_content"),
                              ("source_inventory.abstract", "metadata"),
                              ("thesis_profile", "source_content"),
                              ("runtime.anchor_inventory.guess", "runtime")):
                self.assertTrue(validate_instance(prerequisite(key, kind), candidate_schema), (key, kind))
        self.assertEqual(self.base.get("anyOf"), None)

    def test_catalog_is_not_proof_of_presence_or_approval(self):
        catalog = registered_input_catalog()
        for key in ("source_inventory.abstract", "source_inventory.content"):
            self.assertIn(key, catalog["source_content"])
            self.assertFalse(resolve_input(key)[0])
        key = "thesis_profile.cover_metadata.approval_number"
        self.assertIn(key, catalog["metadata"])
        self.assertFalse(resolve_input(key, metadata={"cover_metadata": {"title_zh": "title"}})[0])

    def test_complete_request_contract_matches_response_schema_and_native_nullable(self):
        chunk, response = profile_tests.ProfileAggregateInputTests().packet()
        definition = chunk["response_schema"]["$defs"]["inputPrerequisiteSpec"]
        self.assertEqual(definition, chunk["requirement_contract"]["$defs"]["inputPrerequisiteSpec"])
        self.assertEqual(chunk["requirement_contract"]["input_catalog"], registered_input_catalog())
        native = native_output_schema(chunk["response_schema"])
        self.assertEqual(native_schema_support_errors(native), [])
        schema = native["$defs"]["inputPrerequisiteSpec"]
        good = prerequisite("source_inventory.abstract", "source_content")
        self.assertEqual(validate_instance(good, schema, native), [])
        original = copy.deepcopy(response)
        response["requirements"][0]["input_prerequisites"] = [prerequisite("source_inventory.abstract_zh", "source_content")]
        normalized = normalize_native_response(response, chunk["response_schema"])
        self.assertEqual(normalized["requirements"][0]["input_prerequisites"], response["requirements"][0]["input_prerequisites"])
        self.assertEqual(original["requirements"][0]["input_prerequisites"], [prerequisite()])

    def test_unregistered_path_feedback_is_typed_and_not_semantic_authorization(self):
        chunk, response = profile_tests.ProfileAggregateInputTests().packet()
        response["requirements"][0]["input_prerequisites"] = [prerequisite("source_inventory.abstract_zh", "source_content")]
        errors = input_prerequisite_errors(response)
        records = contract_error_records(errors, response=response, chunk=chunk)
        self.assertEqual(records[0]["code"], "input_prerequisite_namespace")
        self.assertEqual(records[0]["json_pointer"], "$.requirements[0].input_prerequisites[0].key")
        guidance = bridge._structured_contract_repair_guidance(records, contract_version="3.0")
        self.assertIn("requirement_contract.input_catalog", guidance)
        self.assertIn("fail closed", guidance)
        corrected = copy.deepcopy(response)
        corrected["requirements"][0]["input_prerequisites"][0]["key"] = "source_inventory.abstract"
        self.assertFalse(bridge._retry_changes_allowed(records, bridge._retry_change_paths(response, corrected),
            contract_version="3.0", previous_response=response, current_response=corrected, chunk=chunk))


if __name__ == "__main__":
    unittest.main()
