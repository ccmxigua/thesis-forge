"""Conservative packing must not lose source scope or weaken receipt checks."""
from __future__ import annotations

import ast
import copy
import inspect
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import requirements_engine as engine
from review_limits import DEFAULT_HOST_REVIEW_CHUNK_SIZE
from semantic_contract import attach_request_provenance


class ReviewPackingTests(unittest.TestCase):
    def test_entrypoint_defaults_all_use_same_explicit_overrideable_target(self):
        self.assertEqual(DEFAULT_HOST_REVIEW_CHUNK_SIZE, 8)
        for name in ("requirements_engine", "thesis_format", "thesis_format_pipeline", "batch_rerun_ten_schools"):
            tree = ast.parse((ROOT / "scripts" / (name + ".py")).read_text())
            calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                     and any(isinstance(arg, ast.Constant) and arg.value == "--host-review-chunk-size"
                             for arg in node.args)]
            self.assertEqual(len(calls), 1, name)
            default = next(keyword.value for keyword in calls[0].keywords if keyword.arg == "default")
            self.assertIsInstance(default, ast.Name)
            self.assertEqual(default.id, "DEFAULT_HOST_REVIEW_CHUNK_SIZE", name)
            help_text = next(keyword.value.value for keyword in calls[0].keywords if keyword.arg == "help")
            self.assertIn("%(default)s", help_text)
            self.assertIn("source-atomic", help_text)
        self.assertEqual(inspect.signature(engine.prepare_host_agent_review_packets).parameters["chunk_size"].default, 8)

    def test_default_packing_preserves_every_source_and_atomic_overflow(self):
        clauses = [
            {"id": f"X{i}", "text": f"来源{i}", "evidence_ids": [f"EV{i}"],
             "source_kind": "paragraph", "location": {"order": i}}
            for i in range(21)
        ]
        # A single physical occurrence is indivisible even above the target.
        for clause in clauses[8:18]:
            clause["evidence_ids"] = ["EV8"]
            clause["location"] = {"order": 8}
        evidence = {"evidence": [
            {"id": f"EV{i}", "kind": "paragraph", "text": ("".join(c["text"] for c in clauses[8:18])
                if i == 8 else f"来源{i}")}
            for i in list(range(9)) + list(range(18, 21))
        ]}
        original = copy.deepcopy(clauses)
        request = attach_request_provenance(
            engine.build_llm_request([], clauses, evidence, {}, "full"),
            source_sha256="a" * 64, evidence_doc=evidence, clauses=clauses, run_id="fresh-packing",
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = engine.prepare_host_agent_review_packets(request, clauses, evidence, "a" * 64, root)
            chunks = json.loads((root / "llm-request-chunks.json").read_text())
            self.assertEqual(manifest["chunk_size"], 8)
            self.assertEqual([len(c["clauses"]) for c in chunks], [8, 10, 3])
            self.assertEqual([c["id"] for chunk in chunks for c in chunk["clauses"]], [c["id"] for c in clauses])
            engine.validate_host_review_chunk_source_projection(request, chunks, manifest)
            for mutation in ("omit", "duplicate", "stale-run", "size", "source"):
                altered, altered_manifest = copy.deepcopy(chunks), copy.deepcopy(manifest)
                if mutation == "omit":
                    altered.pop()
                elif mutation == "duplicate":
                    altered[1] = copy.deepcopy(altered[0])
                elif mutation == "stale-run":
                    altered[0]["provenance"]["run_id"] = "old"
                elif mutation == "size":
                    altered_manifest["chunk_size"] = 20
                else:
                    altered[0]["clauses"][0]["text"] = "篡改"
                with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                    engine.validate_host_review_chunk_source_projection(request, altered, altered_manifest)
        self.assertEqual(clauses, original)
