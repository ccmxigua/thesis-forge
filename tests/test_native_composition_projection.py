"""Native provider projection is weaker; the local contract remains mandatory."""
import copy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from host_review_schema import native_output_schema, native_schema_support_errors
from format_spec_validation import validate_instance


class NativeCompositionProjectionTests(unittest.TestCase):
    def test_conditional_constraint_stays_local_not_on_provider_wire(self):
        spec = json.loads((ROOT / "schema/format-spec.schema.json").read_text())
        # Exercise composition projection without imposing a field-name-only
        # title-label restriction on every real source document.
        spec["$defs"]["coverField"]["allOf"] = [{
            "if": {"type": "object", "properties": {"id": {"enum": ["student_id"]}}},
            "then": {"type": "object", "properties": {"display_policy": {"enum": ["required"]}}}}]
        local = {"type": "object", "properties": {"field": {"$ref": "#/$defs/coverField"}},
                 "required": ["field"], "additionalProperties": False, "$defs": spec["$defs"]}
        original = copy.deepcopy(local)
        wire = native_output_schema(local)
        self.assertEqual(native_schema_support_errors(wire), [])
        self.assertNotIn("allOf", wire["$defs"]["coverField"])
        self.assertIn("Local validator also requires", wire["$defs"]["coverField"]["description"])
        value = {"field": {"id": "student_id", "label": "学号", "order": 1,
            "value_from": "thesis_profile.cover_metadata.student_id", "display_policy": "if_present",
            "label_display_policy": "always"}}
        self.assertTrue(validate_instance(value, local))
        value["field"]["display_policy"] = "required"
        self.assertEqual(validate_instance(value, local), [])
        self.assertEqual(local, original)

    def test_unsupported_composition_is_rejected_before_dispatch_if_unprojected(self):
        for keyword, constraint in (("allOf", [{"type": "string"}]),
                ("oneOf", [{"type": "string"}, {"type": "null"}]),
                ("not", {"type": "null"}), ("if", {"type": "string"}),
                ("then", {"type": "string"}), ("else", {"type": "null"})):
            local = {"type": "string", keyword: constraint}
            with self.subTest(keyword=keyword):
                self.assertTrue(any(f"unsupported_keyword:{keyword}" in error
                    for error in native_schema_support_errors(local)))
                projected = native_output_schema(local)
                self.assertNotIn(keyword, projected)
                self.assertEqual(native_schema_support_errors(projected), [])

    def test_constraint_only_schema_cannot_turn_into_an_empty_native_schema(self):
        projected = native_output_schema({"allOf": [{"type": "string"}]})
        self.assertTrue(any("missing_type" in error for error in native_schema_support_errors(projected)))


if __name__ == "__main__":
    unittest.main()
