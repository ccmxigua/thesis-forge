"""Exact source aliases cannot become competing declaration payloads."""
import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import host_agent_bridge as bridge
import requirements_engine as engine
import resource_registry as registry
from test_host_agent_bridge import bind_declaration_fixture_spans


def source_fixture():
    texts = ["学位论文使用授权书", " 第一段精确正文。 ", "第二段精确正文。", "摘要"]
    source = {
        "clauses": [{"id": f"source-{i}", "text": text, "evidence_ids": [f"evidence-{i}"]}
                    for i, text in enumerate(texts)],
        "evidence_context": {f"evidence-{i}": {"id": f"evidence-{i}", "text": text}
                             for i, text in enumerate(texts)},
        "declaration_anchor_preference": "abstract_title_zh",
    }
    bind_declaration_fixture_spans(source)
    response = {"requirements": [{
        "role": "declarations", "clause_ids": ["source-0", "source-1", "source-2"],
        "evidence_ids": ["evidence-0", "evidence-1", "evidence-2"],
        "properties": {"before_role": "abstract_title_zh", "items": [{
            "id": "current-declaration", "heading": texts[0], "body": texts[1],
            "body_parts": texts[1:3], "source_evidence_ids": ["evidence-0", "evidence-1", "evidence-2"],
            "signature_placeholders": [],
        }]}}], "clause_reviews": [{"clause_id": f"source-{i}", "classification": "executable"}
                                  for i in range(3)]}
    return response, source


class DeclarationBodyRepresentationTests(unittest.TestCase):
    def test_source_proved_alias_removed_with_original_bytes_audited(self):
        response, source = source_fixture()
        original = copy.deepcopy(response)
        result, audit = bridge._materialize_fixed_declaration_source_text(response, source)
        item = result["requirements"][0]["properties"]["items"][0]
        self.assertNotIn("body", item)
        self.assertEqual(item["body_parts"], original["requirements"][0]["properties"]["items"][0]["body_parts"])
        self.assertTrue(audit[0]["redundant_body_removed"])
        self.assertEqual(audit[0]["original_body"], " 第一段精确正文。 ")
        self.assertEqual(response, original)
        self.assertEqual(bridge._materialize_fixed_declaration_source_text(result, source), (result, []))
        self.assertEqual(engine._declaration_body_parts(item), registry._body_parts(item))

    def test_competing_or_unbound_alias_is_never_removed(self):
        for mutation in ("different_body", "trimmed_body", "partial_parts", "reordered", "stale", "wrong_edges"):
            response, source = source_fixture()
            item = response["requirements"][0]["properties"]["items"][0]
            if mutation == "different_body": item["body"] = "第二段精确正文。"
            elif mutation == "trimmed_body": item["body"] = item["body"].strip()
            elif mutation == "partial_parts": item["body_parts"] = item["body_parts"][:1]
            elif mutation == "reordered": item["body_parts"].reverse()
            elif mutation == "stale": source["clauses"][1]["source_span"]["source_sha256"] = "0" * 64
            else: response["requirements"][0]["evidence_ids"] = ["evidence-0"]
            with self.subTest(mutation=mutation):
                self.assertEqual(bridge._materialize_fixed_declaration_source_text(response, source), (response, []))

    def test_consumers_reject_both_forms_instead_of_ignoring_or_appending(self):
        for body in ("first", "different"):
            item = {"body": body, "body_parts": ["first", "second"]}
            for consumer in (engine._declaration_body_parts, registry._body_parts):
                with self.subTest(body=body, consumer=consumer.__name__):
                    with self.assertRaisesRegex(ValueError, "ambiguous declaration body"):
                        consumer(item)

    def test_legacy_and_array_forms_preserve_spaces_and_order(self):
        for item, expected in (({"body": " exact "}, [" exact "]),
                               ({"body_parts": [" first ", "second", "second"]}, [" first ", "second", "second"]),
                               ({"body": None, "body_parts": ["first"]}, ["first"]),
                               ({"body": "one", "body_parts": []}, ["one"]), ({}, [])):
            for consumer in (engine._declaration_body_parts, registry._body_parts):
                with self.subTest(item=item, consumer=consumer.__name__):
                    self.assertEqual(consumer(item), expected)

    def test_retry_alias_projects_to_same_candidate_but_reason_drift_still_rejected(self):
        retry, source = source_fixture()
        parent = copy.deepcopy(retry)
        parent["requirements"][0]["properties"]["items"][0].pop("body")
        before, _ = bridge._materialize_fixed_declaration_source_text(parent, source)
        after, _ = bridge._materialize_fixed_declaration_source_text(retry, source)
        error, paths = bridge._retry_semantic_change_error(parent, retry, [], contract_version="3.0",
            chunk=source, comparison_previous_response=before, comparison_current_response=after)
        self.assertIsNone(error)
        self.assertEqual(paths, [])
        after["clause_reviews"][1]["reason"] = "unapproved new explanation"
        error, paths = bridge._retry_semantic_change_error(parent, retry, [], contract_version="3.0",
            chunk=source, comparison_previous_response=before, comparison_current_response=after)
        self.assertIsNotNone(error)
        self.assertTrue(any("reason" in path for path in paths))
