from __future__ import annotations

import copy
import argparse
import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from current_session_semantic_review import (  # noqa: E402
    CurrentSessionSemanticReviewError,
    current_session_binding,
    validate_current_session_semantic_review_response,
)
from semantic_contract import sha256_json  # noqa: E402
from thesis_format import pipeline_command  # noqa: E402
from thesis_format_pipeline import validate_semantic_review_configuration  # noqa: E402


def make_request() -> dict:
    checks = [
        {
            "check_id": f"abstract_zh.check_{index}",
            "document_text": f"摘要证据片段{index}。",
            "source_requirements": [{"requirement_id": f"R{index}"}],
        }
        for index in range(1, 7)
    ]
    projection = [[item["check_id"], item["document_text"]] for item in checks]
    return {
        "schema_version": "1.0",
        "protocol": "native_semantic_content_review_v1",
        "case_id": "bsu",
        "run_id": "v10-run-001",
        "source_sha256": "1" * 64,
        "format_spec_sha256": "2" * 64,
        "document_text_sha256": sha256_json(projection),
        "checks": checks,
    }


def make_response(request: dict, code_sha: str = "3" * 64) -> dict:
    return {
        "schema_version": "1.0",
        "protocol": "current_session_semantic_content_review_v1",
        "review_mode": "current_session",
        "provider_model_verified": False,
        "native_invocation": False,
        "binding": current_session_binding(request, code_fingerprint_sha256=code_sha),
        "results": [
            {
                "check_id": item["check_id"],
                "verdict": "uncertain" if item["check_id"].endswith("6") else "satisfied",
                "rationale": f"逐项对照要求 {item['check_id']}。",
                "evidence_quotes": [item["document_text"]],
            }
            for item in request["checks"]
        ],
    }


class CurrentSessionSemanticReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "response.json"
        self.request = make_request()
        self.code_sha = "3" * 64
        self.response = make_response(self.request, self.code_sha)
        self.write_response()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_response(self) -> None:
        self.path.write_text(
            json.dumps(self.response, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def validate(self, request: dict | None = None, **kwargs):
        return validate_current_session_semantic_review_response(
            self.path,
            self.request if request is None else request,
            code_fingerprint_sha256=kwargs.pop("code_sha", self.code_sha),
            output_policy=kwargs.pop("output_policy", "review_draft"),
            native_runtime=kwargs.pop("native_runtime", None),
            native_model=kwargs.pop("native_model", None),
        )

    def test_accepts_exact_six_check_response_and_mints_non_native_receipt(self) -> None:
        review, receipt = self.validate()
        self.assertEqual(review["status"], "completed")
        self.assertEqual(review["summary"], {"check_count": 6, "satisfied": 5, "noncompliant": 0, "uncertain": 1})
        self.assertEqual(review["results"][-1]["verdict"], "uncertain")
        for artifact in (review, receipt):
            self.assertFalse(artifact["provider_model_verified"])
            self.assertFalse(artifact["native_invocation"])
            self.assertFalse(artifact["submission_ready"])
        self.assertEqual(receipt["response_file"]["sha256"], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(receipt["binding"]["code_fingerprint_sha256"], self.code_sha)

    def test_rejects_stale_source_format_document_request_run_and_code_bindings(self) -> None:
        for field, value in (
            ("source_sha256", "a" * 64),
            ("format_spec_sha256", "b" * 64),
            ("document_text_sha256", "c" * 64),
            ("run_id", "different-run"),
        ):
            with self.subTest(field=field):
                changed = copy.deepcopy(self.request)
                changed[field] = value
                message = "document_text_sha256" if field == "document_text_sha256" else "binding differs"
                with self.assertRaisesRegex(CurrentSessionSemanticReviewError, message):
                    self.validate(changed)
        changed = copy.deepcopy(self.request)
        changed["checks"][0]["source_requirements"][0]["requirement_id"] = "different"
        with self.assertRaisesRegex(CurrentSessionSemanticReviewError, "binding differs"):
            self.validate(changed)
        with self.assertRaisesRegex(CurrentSessionSemanticReviewError, "binding differs"):
            self.validate(code_sha="4" * 64)

    def test_rejects_internally_inconsistent_document_text_digest(self) -> None:
        changed = copy.deepcopy(self.request)
        changed["checks"][0]["document_text"] = "changed current document text"
        with self.assertRaisesRegex(CurrentSessionSemanticReviewError, "document_text_sha256"):
            current_session_binding(changed, code_fingerprint_sha256=self.code_sha)

    def test_rejects_missing_extra_duplicate_and_unknown_check_results(self) -> None:
        mutations = {
            "missing": lambda value: value["results"].pop(),
            "extra": lambda value: value["results"].append(copy.deepcopy(value["results"][0]) | {"check_id": "not-current"}),
            "duplicate": lambda value: value["results"].append(copy.deepcopy(value["results"][0])),
            "unknown": lambda value: value["results"][0].update(check_id="unknown"),
        }
        for name, mutation in mutations.items():
            with self.subTest(name=name):
                self.response = copy.deepcopy(make_response(self.request, self.code_sha))
                mutation(self.response)
                self.write_response()
                with self.assertRaises(CurrentSessionSemanticReviewError):
                    self.validate()

    def test_rejects_non_exact_quotes_invalid_verdict_and_extra_identity_claims(self) -> None:
        mutations = {
            "quote": lambda value: value["results"][0].update(evidence_quotes=["近似但非原文"]),
            "verdict": lambda value: value["results"][0].update(verdict="passed"),
            "native_claim": lambda value: value.update(native_invocation=True),
            "provider_claim": lambda value: value.update(provider_model="native-gpt"),
        }
        for name, mutation in mutations.items():
            with self.subTest(name=name):
                self.response = copy.deepcopy(make_response(self.request, self.code_sha))
                mutation(self.response)
                self.write_response()
                with self.assertRaises(CurrentSessionSemanticReviewError):
                    self.validate()

    def test_rejects_submission_and_native_reviewer_mixing(self) -> None:
        with self.assertRaisesRegex(CurrentSessionSemanticReviewError, "review_draft"):
            self.validate(output_policy="submission")
        with self.assertRaisesRegex(CurrentSessionSemanticReviewError, "cannot be mixed"):
            self.validate(native_runtime="codex")
        with self.assertRaisesRegex(CurrentSessionSemanticReviewError, "cannot be mixed"):
            self.validate(native_model="gpt-6-luna")

    def test_wrapper_passes_response_only_on_an_offline_draft_continuation(self) -> None:
        args = SimpleNamespace(
            requirements=Path("req.docx"), input=Path("source.docx"), output=Path("draft.docx"),
            work_dir=Path("run"), host_review_chunk_size=8, output_policy="review_draft",
            style_template=None, thesis_profile=None, template_profile=None, render_report=None,
            strict_release=False, require_submission_ready=False, auto_host_agent=False,
        )
        command = pipeline_command(
            args, llm_response=Path("merged-response.json"),
            current_session_semantic_review_response=Path("abstract-response.json"),
        )
        option = "--current-session-semantic-review-response"
        self.assertIn(option, command)
        self.assertEqual(command[command.index(option) + 1], "abstract-response.json")
        with self.assertRaisesRegex(ValueError, "offline review_draft continuation"):
            pipeline_command(args, current_session_semantic_review_response=Path("abstract-response.json"))
        with self.assertRaisesRegex(ValueError, "auto-host-agent"):
            args.auto_host_agent = True
            pipeline_command(
                args, llm_response=Path("merged-response.json"),
                current_session_semantic_review_response=Path("abstract-response.json"),
            )

    def test_pipeline_rejects_missing_offline_continuation_native_mix_and_submission(self) -> None:
        base = {
            "prepare_host_review": False,
            "llm_response": None,
            "host_agent_audit": None,
            "merge_receipt": None,
            "allow_offline_review": True,
            "output_policy": "review_draft",
            "require_submission_ready": False,
            "strict_release": False,
            "offline_merge_receipt": None,
            "offline_parent_merge_receipt": None,
            "run_id": None,
            "semantic_review_runtime": None,
            "semantic_review_model": None,
            "semantic_review_reasoning_effort": None,
            "profile_confirmation_migration": None,
            "compliance_mode": "full",
            "analysis_mode": "llm_primary",
            "thesis_profile": None,
            "input": "source.docx",
            "template_profile": None,
            "render_report": None,
            "allow_unresolved": False,
            "preview_placeholders": False,
        }
        cases = [
            ({"current_session_semantic_review_response": Path("abstract.json")}, "non-release offline"),
            ({
                "current_session_semantic_review_response": Path("abstract.json"),
                "llm_response": Path("merged.json"),
                "offline_merge_receipt": Path("receipt.json"),
                "semantic_review_runtime": "codex",
                "semantic_review_model": "gpt-6-luna",
            }, "cannot be mixed with native review"),
            ({
                "current_session_semantic_review_response": Path("abstract.json"),
                "llm_response": Path("merged.json"),
                "output_policy": "submission",
                "allow_offline_review": False,
            }, "current-session"),
        ]
        for overrides, message in cases:
            with self.subTest(message=message):
                args = SimpleNamespace(**(base | overrides))
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    with self.assertRaises(SystemExit):
                        validate_semantic_review_configuration(args, argparse.ArgumentParser())
                self.assertIn(message, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
