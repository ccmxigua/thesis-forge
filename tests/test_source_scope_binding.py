from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from source_scope_binding import (  # noqa: E402
    diagnose_source_scope_reference,
    validate_source_scope_binding,
)
import requirements_engine  # noqa: E402


def clause(clause_id: str, text: str, source: str, *, evidence_id: str,
           location: dict, start: int = 0) -> dict:
    return {
        "id": clause_id,
        "text": text,
        "source_evidence_text": source,
        "source_kind": "table_cell" if "table_child_index" in location else "paragraph",
        "location": dict(location),
        "evidence_ids": [evidence_id],
        "source_span": {
            "evidence_id": evidence_id,
            "start_offset": start,
            "end_offset": start + len(text),
            "text": text,
            "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            "location": dict(location),
        },
    }


class SourceScopeBindingTests(unittest.TestCase):
    def test_scope_requires_explicit_anchor_array_even_for_an_explicit_clause_set(self) -> None:
        source = "字段"
        location = {"part": "document", "child_index": 0, "paragraph": 0}
        clauses = [clause("C1", source, source, evidence_id="E1", location=location)]
        errors, projection = validate_source_scope_binding({
            "kind": "explicit_clause_set", "target_clause_ids": ["C1"],
        }, clauses, cited_clause_ids={"C1"})
        self.assertIsNone(projection)
        self.assertEqual(errors, ["source_scope_anchor_clause_ids_required"])

    def test_duplicate_ordinal_reference_is_reported_as_ambiguity(self) -> None:
        first = {"id": "C1", "text": "第一条 甲项", "evidence_ids": ["E1"]}
        second = {"id": "C2", "text": "第一条 乙项", "evidence_ids": ["E2"]}
        instruction = {"id": "C3", "text": "第一条中导师姓名使用五号宋体", "evidence_ids": ["E3"]}
        result = diagnose_source_scope_reference(instruction, [first, second, instruction])
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "pending_input")
        self.assertEqual(result["classification"], "semantic_ambiguity")
        self.assertEqual(result["candidate_clause_ids"], ["C1", "C2"])
        self.assertEqual(result["resolution_policy"], "human_selects_exact_clause_ids_before_execution")

    def test_rule_compiler_keeps_scoped_style_pending_instead_of_role_wide(self) -> None:
        clauses = []
        for index, source_text in enumerate(("第一条 甲项", "第一条 乙项"), 1):
            clauses.append({
                "id": f"C{index}", "text": source_text, "evidence_ids": [f"E{index}"],
                "context_before": [], "context_after": [],
            })
        instruction = {
            "id": "C3", "text": "第一条中导师姓名使用五号宋体。", "evidence_ids": ["E3"],
            "context_before": ["第一条 甲项", "第一条 乙项"], "context_after": [],
        }
        clauses.append(instruction)
        spec, questions, conflicts = requirements_engine.build_rule_result(
            Path("source.docx"), clauses,
        )
        self.assertEqual(conflicts, [])
        self.assertFalse(spec["requirements"])
        self.assertNotIn("cover_field_label", spec["roles"])
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0]["source_scope_diagnosis"]["classification"], "semantic_ambiguity")
        self.assertEqual(questions[0]["source_scope_diagnosis"]["candidate_clause_ids"], ["C1", "C2"])

    def test_anaphora_lists_candidates_but_never_resolves_by_proximity(self) -> None:
        prior_a = {"id": "C1", "text": "目录项目格式", "evidence_ids": ["E1"]}
        prior_b = {"id": "C2", "text": "第一条标题格式", "evidence_ids": ["E2"]}
        instruction = {"id": "C3", "text": "上述规定适用五号宋体", "evidence_ids": ["E3"]}
        result = diagnose_source_scope_reference(instruction, [prior_a, prior_b, instruction])
        self.assertEqual(result["classification"], "semantic_ambiguity")
        self.assertEqual(result["candidate_clause_ids"], ["C1", "C2"])

    def test_forward_table_reference_without_structural_candidate_is_capability_gap(self) -> None:
        instruction = {"id": "C1", "text": "字段按下表规定填写", "evidence_ids": ["E1"]}
        result = diagnose_source_scope_reference(instruction, [instruction])
        self.assertEqual(result["status"], "pending_input")
        self.assertEqual(result["classification"], "parser_capability_gap")
        self.assertEqual(result["reason"], "forward_target_not_structurally_observable")

    def test_same_paragraph_range_binds_exact_contiguous_clause_set(self) -> None:
        source = "甲标签乙标签丙"
        location = {"part": "document", "child_index": 5, "paragraph": 0}
        clauses = [
            clause("C1", "甲标签", source, evidence_id="E1", location=location, start=0),
            clause("C2", "乙标签", source, evidence_id="E1", location=location, start=3),
            clause("C3", "丙", source, evidence_id="E1", location=location, start=6),
        ]
        errors, projection = validate_source_scope_binding({
            "kind": "same_paragraph_range",
            "anchor_clause_ids": ["C1", "C3"],
            "target_clause_ids": ["C1", "C2", "C3"],
        }, clauses, cited_clause_ids={"C1", "C2", "C3"})
        self.assertEqual(errors, [])
        self.assertEqual(projection["target_clause_ids"], ["C1", "C2", "C3"])
        self.assertEqual([item["start_offset"] for item in projection["target_spans"]], [0, 3, 6])

    def test_same_paragraph_range_rejects_omitted_middle_target_and_bad_source_hash(self) -> None:
        source = "甲标签乙标签丙"
        location = {"part": "document", "child_index": 5, "paragraph": 0}
        clauses = [
            clause("C1", "甲标签", source, evidence_id="E1", location=location, start=0),
            clause("C2", "乙标签", source, evidence_id="E1", location=location, start=3),
            clause("C3", "丙", source, evidence_id="E1", location=location, start=6),
        ]
        errors, projection = validate_source_scope_binding({
            "kind": "same_paragraph_range", "anchor_clause_ids": ["C1", "C3"],
            "target_clause_ids": ["C1", "C3"],
        }, clauses, cited_clause_ids={"C1", "C3"})
        self.assertIsNone(projection)
        self.assertIn("source_scope_paragraph_range_target_set_mismatch", errors)
        clauses[1]["source_span"]["source_sha256"] = "0" * 64
        errors, projection = validate_source_scope_binding({
            "kind": "same_paragraph_range", "anchor_clause_ids": ["C1", "C3"],
            "target_clause_ids": ["C1", "C2", "C3"],
        }, clauses, cited_clause_ids={"C1", "C2", "C3"})
        self.assertIsNone(projection)
        self.assertTrue(any("source_scope_clause_span_unverifiable:C2" in error for error in errors))

    def test_table_row_range_uses_exact_cell_coordinates_and_order(self) -> None:
        clauses = [
            clause(f"C{column + 1}", f"字段{column + 1}", f"字段{column + 1}",
                   evidence_id=f"E{column + 1}",
                   location={"part": "document", "table_child_index": 3, "row": 2,
                             "column": column, "paragraph": 0})
            for column in range(3)
        ]
        errors, projection = validate_source_scope_binding({
            "kind": "same_table_row_range",
            "anchor_clause_ids": ["C1", "C3"],
            "target_clause_ids": ["C1", "C2", "C3"],
        }, clauses, cited_clause_ids={"C1", "C2", "C3"})
        self.assertEqual(errors, [])
        self.assertEqual(projection["target_clause_ids"], ["C1", "C2", "C3"])
        clauses[1]["location"]["row"] = 4
        clauses[1]["source_span"]["location"]["row"] = 4
        errors, projection = validate_source_scope_binding({
            "kind": "same_table_row_range",
            "anchor_clause_ids": ["C1", "C3"],
            "target_clause_ids": ["C1", "C2", "C3"],
        }, clauses, cited_clause_ids={"C1", "C2", "C3"})
        self.assertIsNone(projection)
        self.assertIn("source_scope_table_row_range_target_set_mismatch", errors)

    def test_table_row_range_rejects_clause_crossing_the_end_anchor(self) -> None:
        source = "甲乙丙丁"
        location = {
            "part": "document", "table_child_index": 3, "row": 2,
            "column": 0, "paragraph": 0,
        }
        clauses = [
            clause("C1", "甲", source, evidence_id="E1", location=location, start=0),
            clause("C2", "乙", source, evidence_id="E1", location=location, start=1),
            clause("C3", "乙丙丁", source, evidence_id="E1", location=location, start=1),
        ]
        errors, projection = validate_source_scope_binding({
            "kind": "same_table_row_range",
            "anchor_clause_ids": ["C1", "C2"],
            "target_clause_ids": ["C1", "C2", "C3"],
        }, clauses, cited_clause_ids={"C1", "C2", "C3"})
        self.assertIsNone(projection)
        self.assertIn("source_scope_table_row_range_target_set_mismatch", errors)

    def test_scope_cannot_reference_uncited_or_unavailable_clause(self) -> None:
        source = "字段"
        loc = {"part": "document", "child_index": 0, "paragraph": 0}
        clauses = [clause("C1", source, source, evidence_id="E1", location=loc)]
        errors, projection = validate_source_scope_binding({
            "kind": "explicit_clause_set", "anchor_clause_ids": [],
            "target_clause_ids": ["C1", "C2"],
        }, clauses, cited_clause_ids={"C1"})
        self.assertIsNone(projection)
        self.assertEqual(errors, [
            "source_scope_references_unknown_clause",
            "source_scope_references_uncited_clause",
        ])

    def _scoped_merge_fixture(self, include_second_instance: bool = True):
        source = "甲标签乙标签"
        location = {"part": "document", "child_index": 5, "paragraph": 0}
        clauses = [
            clause("C1", "甲标签", source, evidence_id="E1", location=location, start=0),
            clause("C2", "乙标签", source, evidence_id="E1", location=location, start=3),
        ]
        instances = []
        for target in clauses[:2 if include_second_instance else 1]:
            instance_id, error = requirements_engine._register_content_instance(
                instances, {"field_key": None}, "cover_field_label",
                {"text": target["text"]}, {target["id"]}, {"E1"},
                {item["id"]: item for item in clauses}, "exact current source label",
            )
            self.assertIsNotNone(instance_id)
            self.assertIsNone(error)
        response = {
            "contract_version": "2.1",
            "requirements": [{
                "role": "cover_field_label",
                "properties": {"font": {"size_pt": 16}},
                "source_scope": {
                    "kind": "same_paragraph_range",
                    "anchor_clause_ids": ["C1", "C2"],
                    "target_clause_ids": ["C1", "C2"],
                },
                "clause_ids": ["C1", "C2"], "evidence_ids": ["E1"],
                "confidence": .99, "reason": "将已核实的同段来源范围绑定到具体字段。",
            }],
            "clause_reviews": [
                {"clause_id": clause_id, "classification": "executable",
                 "requirement_indexes": [0], "reason": "来源范围明确。"}
                for clause_id in ("C1", "C2")
            ],
            "unsupported_items": [], "reported_conflicts": [],
        }
        rule_spec = {
            "schema_version": "1.0", "source_document": "synthetic-source",
            "roles": {}, "requirements": [], "content_instances": instances,
        }
        return source, location, clauses, response, rule_spec

    def test_merge_binds_scoped_style_only_to_exact_field_instances(self) -> None:
        _source, _location, clauses, response, rule_spec = self._scoped_merge_fixture()
        spec, conflicts, audit = requirements_engine.merge_llm_primary(
            Path("synthetic-source"), rule_spec, clauses, response, {"E1"},
        )
        self.assertFalse(any(item.get("type") in {"llm_contract", "source_scope_binding"}
                             for item in conflicts), conflicts)
        requirement = spec["requirements"][0]
        self.assertEqual(
            set(requirement["field_instance_ids"]),
            {item["id"] for item in spec["content_instances"]},
        )
        self.assertEqual(requirement["source_scope"]["target_clause_ids"], ["C1", "C2"])
        self.assertEqual(spec["roles"], {})
        self.assertTrue(all(item["style_properties"]["font"]["size_pt"] == 16
                            for item in spec["content_instances"]))
        self.assertTrue(any(item.get("accepted") is True for item in audit))

    def test_merge_rejects_scoped_style_when_any_target_instance_is_missing(self) -> None:
        _source, _location, clauses, response, rule_spec = self._scoped_merge_fixture(
            include_second_instance=False,
        )
        spec, conflicts, audit = requirements_engine.merge_llm_primary(
            Path("synthetic-source"), rule_spec, clauses, response, {"E1"},
        )
        self.assertEqual(spec["requirements"], [])
        self.assertEqual(spec["roles"], {})
        self.assertTrue(any(item.get("type") == "source_scope_binding"
                            and "source_scope_target_instance_count:C2:0" in str(item.get("reason"))
                            for item in conflicts), conflicts)
        self.assertTrue(any(item.get("accepted") is False
                            and item.get("reason") == "source_scope_target_instance_unresolved"
                            for item in audit))

    def test_merge_rejects_target_instance_that_also_covers_unselected_source_clause(self) -> None:
        _source, _location, clauses, response, rule_spec = self._scoped_merge_fixture()
        rule_spec["content_instances"][0]["clause_ids"].append("C99")
        spec, conflicts, audit = requirements_engine.merge_llm_primary(
            Path("synthetic-source"), rule_spec, clauses, response, {"E1"},
        )
        self.assertEqual(spec["requirements"], [])
        self.assertFalse(spec["roles"])
        self.assertTrue(any(
            item.get("type") == "source_scope_binding"
            and "source_scope_target_instance_contains_unselected_clause:C1" in str(item.get("reason"))
            for item in conflicts
        ), conflicts)
        self.assertTrue(any(item.get("accepted") is False for item in audit))

    def test_scoped_request_text_cannot_create_or_style_an_out_of_scope_instance(self) -> None:
        _source, _location, clauses, response, rule_spec = self._scoped_merge_fixture()
        requirement = response["requirements"][0]
        requirement["properties"] = {"text": "甲标签", "font": {"size_pt": 16}}
        requirement["source_scope"] = {
            "kind": "explicit_clause_set", "anchor_clause_ids": [],
            "target_clause_ids": ["C2"],
        }
        requirement["clause_ids"] = ["C1", "C2"]
        requirement["evidence_ids"] = ["E1"]
        for review in response["clause_reviews"]:
            review["requirement_indexes"] = [0]
        spec, conflicts, _audit = requirements_engine.merge_llm_primary(
            Path("synthetic-source"), rule_spec, clauses, response, {"E1"},
        )
        self.assertFalse(any(item.get("type") in {"llm_contract", "source_scope_binding"}
                             for item in conflicts), conflicts)
        by_clause = {
            clause_id: instance for instance in spec["content_instances"]
            for clause_id in instance["clause_ids"]
        }
        self.assertNotIn("font", by_clause["C1"].get("style_properties", {}))
        self.assertEqual(by_clause["C2"]["style_properties"]["font"]["size_pt"], 16)
        self.assertEqual(spec["requirements"][0]["field_instance_ids"], [by_clause["C2"]["id"]])

    def test_referenced_content_text_without_explicit_scope_is_not_accepted(self) -> None:
        _source, _location, clauses, _response, rule_spec = self._scoped_merge_fixture()
        instruction_text = "第一条中导师姓名使用五号宋体"
        clauses.append(clause(
            "C3", instruction_text, instruction_text, evidence_id="E2",
            location={"part": "document", "child_index": 6, "paragraph": 0},
        ))
        response = {
            "contract_version": "2.1",
            "requirements": [{
                "role": "cover_field_label",
                "properties": {"text": "导师姓名", "font": {"size_pt": 10}},
                "clause_ids": ["C3"], "evidence_ids": ["E2"],
                "confidence": .99, "reason": "原文要求调整第一条中的导师姓名。",
            }],
            "clause_reviews": [{
                "clause_id": "C3", "classification": "covered",
                "requirement_indexes": [0], "reason": "引用特定条目。",
            }],
            "unsupported_items": [], "reported_conflicts": [],
        }
        spec, conflicts, _audit = requirements_engine.merge_llm_primary(
            Path("synthetic-source"), rule_spec, clauses, response, {"E1", "E2"},
        )
        self.assertTrue(any(
            item.get("type") == "llm_contract"
            and "source_scope_binding_required_for_referenced_content" in str(item.get("reason"))
            for item in conflicts
        ), conflicts)
        self.assertFalse(any(
            item.get("role") == "cover_field_label" and item.get("properties", {}).get("font")
            for item in spec["requirements"]
        ))

    def test_build_request_exposes_only_current_clause_ids_for_scope_anchors(self) -> None:
        _source, _location, clauses, _response, rule_spec = self._scoped_merge_fixture()
        request = requirements_engine.build_llm_request(
            [], clauses,
            evidence_doc={"evidence": [{
                "id": "E1", "text": "甲标签乙标签", "kind": "paragraph",
                "location": {"part": "document", "child_index": 5, "paragraph": 0},
            }]},
            rule_spec=rule_spec,
            mode="full",
            contract_version="2.1",
        )
        item = next(
            branch for branch in request["response_schema"]["properties"]["requirements"]["items"]["anyOf"]
            if branch["properties"]["role"]["enum"] == ["cover_field_label"]
        )
        scope = item["properties"]["source_scope"]["$ref"]
        scope_schema = request["response_schema"]["$defs"][scope.removeprefix("#/$defs/")]
        ids = scope_schema["properties"]["target_clause_ids"]["items"]["enum"]
        self.assertEqual(ids, ["C1", "C2"])


if __name__ == "__main__":
    unittest.main()
