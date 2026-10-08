from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import thesis_format as wrapper  # noqa: E402


class ThesisFormatWrapperTests(unittest.TestCase):
    def _args(self) -> argparse.Namespace:
        return argparse.Namespace(
            requirements=Path("requirements.docx"),
            input=Path("thesis.tex"),
            output=Path("output.docx"),
            work_dir=Path("build/run"),
            style_template=None,
            thesis_profile=Path("profile.json"),
            template_profile=Path("template-profile.json"),
            render_report=Path("render-report.json"),
            host_review_chunk_size=20,
            require_submission_ready=True,
            strict_release=True,
        )

    def test_pipeline_command_forwards_template_profile_to_strict_pipeline(self) -> None:
        command = wrapper.pipeline_command(self._args())
        self.assertIn("--template-profile", command)
        self.assertEqual(
            command[command.index("--template-profile") + 1],
            "template-profile.json",
        )

    def test_post_format_review_refuses_partial_route(self) -> None:
        with self.assertRaises(ValueError):
            wrapper.pipeline_command(self._args(), semantic_review_runtime="codex")
        with self.assertRaises(ValueError):
            wrapper.pipeline_command(self._args(), semantic_review_model="gpt-6-luna")

    def test_portable_offline_draft_does_not_request_native_host(self) -> None:
        args = self._args()
        args.require_submission_ready = False
        args.strict_release = False
        command = wrapper.pipeline_command(
            args, llm_response=Path("review/response.json"),
            offline_merge_receipt=Path("review/merge-receipt.json"),
        )
        self.assertIn("--allow-offline-review", command)
        self.assertIn("--offline-merge-receipt", command)
        self.assertIn("--allow-existing-work", command)
        self.assertNotIn("--host-agent-audit", command)
        self.assertNotIn("--strict-release", command)

    def test_profile_confirmation_lineage_is_forwarded_for_prepare_and_offline_generate(self) -> None:
        with patch.object(wrapper, "run_stage", return_value=0) as run:
            self.assertEqual(wrapper.main([
                "requirements.docx", "fixed-source.docx", "output.docx",
                "--work-dir", "build/run", "--thesis-profile", "confirmed-profile.json",
                "--prepare-agent-review", "--offline-parent-merge-receipt", "parent-receipt.json",
                "--profile-confirmation-migration", "confirmation.json", "--run-id", "parent-run",
            ]), 0)
        prepare = run.call_args.args[0]
        self.assertIn("--prepare-host-review", prepare)
        self.assertIn("--offline-parent-merge-receipt", prepare)
        self.assertIn("--profile-confirmation-migration", prepare)
        self.assertEqual(prepare[prepare.index("--run-id") + 1], "parent-run")
        self.assertNotIn("--auto-host-agent", prepare)

        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            review = work / "review" / "requirements"
            review.mkdir(parents=True)
            (review / "extraction-manifest.json").write_text(json.dumps({"run_id": "parent-run"}))
            (review / "merge-receipt.json").write_text("{}")
            with patch.object(wrapper, "run_stage", return_value=0) as run:
                self.assertEqual(wrapper.main([
                    "requirements.docx", "fixed-source.docx", str(work / "output.docx"),
                    "--work-dir", str(work), "--thesis-profile", "confirmed-profile.json",
                    "--llm-response", "review/requirements/host-agent-response.json",
                    "--offline-parent-merge-receipt", "parent-receipt.json",
                    "--profile-confirmation-migration", "confirmation.json", "--run-id", "parent-run",
                ]), 0)
            generate = run.call_args.args[0]
            self.assertIn("--llm-response", generate)
            self.assertIn("--allow-offline-review", generate)
            self.assertIn("--offline-merge-receipt", generate)
            self.assertIn("--offline-parent-merge-receipt", generate)
            self.assertIn("--profile-confirmation-migration", generate)
            self.assertIn("--allow-existing-work", generate)

    def test_packet_preparation_does_not_select_native_adapter(self) -> None:
        with patch.object(wrapper, "run_stage", return_value=0) as run_stage:
            code = wrapper.main([
                "requirements.docx", "thesis.tex", "--work-dir", "build/run",
                "--prepare-agent-review",
            ])
        self.assertEqual(code, 0)
        command = run_stage.call_args.args[0]
        self.assertIn("--prepare-host-review", command)
        self.assertNotIn("--host-runtime", command)
        self.assertNotIn("--auto-host-agent", command)
        self.assertNotIn("--allow-existing-work", command)

    def test_auto_codex_binds_default_to_primary_and_post_format_review(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with patch.dict("os.environ", {"THESIS_FORGE_HOST_RUNTIME": "codex"}), \
                 patch.object(wrapper, "strict_json_read", return_value={"run_id": "fresh-run"}), \
                 patch.object(wrapper, "run_stage", return_value=0) as run:
                code = wrapper.main([
                    "requirements.docx", "thesis.tex", str(Path(td) / "out.docx"),
                    "--work-dir", str(Path(td) / "work"), "--auto-host-agent",
                    "--host-runtime", "codex",
                ])
            self.assertEqual(code, 0)
            primary = run.call_args_list[1].args[0]
            final = run.call_args_list[2].args[0]
            self.assertEqual(primary[primary.index("--codex-model") + 1], "gpt-6-luna")
            self.assertEqual(final[final.index("--semantic-review-model") + 1], "gpt-6-luna")
            self.assertEqual(final[final.index("--semantic-review-runtime") + 1], "codex")

    def test_default_uses_current_agent_even_with_declared_native_host(self) -> None:
        with patch.dict("os.environ", {"THESIS_FORGE_HOST_RUNTIME": "codex"}), \
             patch.object(wrapper, "run_stage", return_value=0) as run, \
             patch.object(wrapper, "require_host_runtime") as native, \
             patch.object(wrapper.codex_adapter, "resolve_model") as model:
            self.assertEqual(wrapper.main(["requirements.docx", "thesis.docx"]), 0)
        native.assert_not_called()
        model.assert_not_called()
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertIn("--prepare-host-review", command)
        self.assertNotIn("--semantic-review-runtime", command)
        self.assertNotIn("--codex-model", command)
        work = Path(command[command.index("--work-dir") + 1])
        self.assertTrue(work.is_absolute())
        self.assertTrue(work.name.startswith("thesis-forge-"))
        self.assertEqual(command[2], str(Path("requirements.docx").resolve()))
        self.assertEqual(command[3], str(Path("thesis.docx").resolve()))

    def test_explicit_codex_effort_survives_primary_and_post_format_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with patch.dict("os.environ", {"THESIS_FORGE_HOST_RUNTIME": "codex"}), \
                 patch.object(wrapper, "strict_json_read", return_value={"run_id": "fresh-run"}), \
                 patch.object(wrapper, "run_stage", return_value=0) as run:
                code = wrapper.main([
                    "requirements.docx", "thesis.tex", str(Path(td) / "out.docx"),
                    "--work-dir", str(Path(td) / "work"), "--auto-host-agent",
                    "--host-runtime", "codex", "--codex-reasoning-effort", "max",
                ])
            self.assertEqual(code, 0)
            primary = run.call_args_list[1].args[0]
            final = run.call_args_list[2].args[0]
            self.assertEqual(primary[primary.index("--codex-reasoning-effort") + 1], "max")
            self.assertEqual(final[final.index("--semantic-review-reasoning-effort") + 1], "max")
            self.assertEqual(primary[primary.index("--max-attempts") + 1], "2")
            self.assertEqual(primary[primary.index("--timeout") + 1], "900")

    def test_effort_requires_explicit_native_codex_execution(self) -> None:
        for extra in ([], ["--auto-host-agent", "--host-runtime", "openclaw"]):
            with self.subTest(extra=extra), patch.dict("os.environ", {"THESIS_FORGE_HOST_RUNTIME": "openclaw"}), \
                 patch.object(wrapper, "run_stage") as stage, self.assertRaises(SystemExit):
                wrapper.main(["requirements.docx", "thesis.tex", "--codex-reasoning-effort", "max", *extra])
            stage.assert_not_called()

    def test_explicit_native_executable_path_is_caller_relative(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with patch.dict("os.environ", {"THESIS_FORGE_HOST_RUNTIME": "codex"}), \
                 patch.object(wrapper, "strict_json_read", return_value={"run_id": "r"}), \
                 patch.object(wrapper, "run_stage", return_value=0) as run:
                self.assertEqual(wrapper.main([
                    "r.docx", "i.docx", str(Path(td) / "o.docx"),
                    "--work-dir", str(Path(td) / "work"), "--auto-host-agent",
                    "--codex-bin", "./tools/codex", "--codex-model", "chosen-model"]), 0)
            primary = run.call_args_list[1].args[0]
            self.assertEqual(primary[primary.index("--codex-bin") + 1],
                             str(Path("./tools/codex").resolve()))
            self.assertEqual(primary[primary.index("--codex-model") + 1], "chosen-model")

    def test_default_work_directories_are_distinct(self) -> None:
        with patch.object(wrapper, "run_stage", return_value=0) as run:
            for _ in range(2):
                wrapper.main(["requirements.docx", "thesis.docx"])
        commands = [call.args[0] for call in run.call_args_list]
        work = [c[c.index("--work-dir") + 1] for c in commands]
        self.assertNotEqual(*work)

    def test_native_options_are_not_silently_ignored_by_packet_default(self) -> None:
        for flag in ("--codex-model", "--host-runtime", "--codex-bin", "--host-agent-model"):
            for value in ("override", ""):
                with self.subTest(flag=flag, value=value), \
                     patch.object(wrapper, "run_stage") as run, \
                     self.assertRaises(SystemExit):
                    wrapper.main(["r.docx", "i.docx", flag, value])
                run.assert_not_called()

    def test_packet_continuation_requires_original_work_and_receipt(self) -> None:
        with patch.object(wrapper, "run_stage") as run:
            for options in (["--llm-response", "response.json"],
                            ["--llm-response", "response.json", "--work-dir", "missing-work"]):
                with self.subTest(options=options), self.assertRaises(SystemExit):
                    wrapper.main(["r.docx", "i.docx", "out.docx", *options])
        run.assert_not_called()

    def test_submission_does_not_default_to_offline_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            review = work / "review" / "requirements"
            review.mkdir(parents=True)
            (review / "extraction-manifest.json").write_text(json.dumps({"run_id": "r"}))
            (review / "merge-receipt.json").write_text("{}")
            with patch.object(wrapper, "run_stage") as run, self.assertRaises(SystemExit):
                wrapper.main(["r.docx", "i.docx", "out.docx", "--work-dir", str(work),
                              "--llm-response", "response.json", "--output-policy", "submission"])
            run.assert_not_called()

    def test_existing_native_audit_never_falls_back_to_offline(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            review = work / "review" / "requirements"
            review.mkdir(parents=True)
            for filename in ("merge-receipt.json", "host-agent-run.json"):
                (review / filename).write_text("{}")
            (review / "extraction-manifest.json").write_text(json.dumps({"run_id": "r"}))
            with patch.object(wrapper, "run_stage", return_value=0) as run:
                self.assertEqual(wrapper.main([
                    "r.docx", "i.docx", "out.docx", "--work-dir", str(work),
                    "--llm-response", "response.json"]), 0)
            command = run.call_args.args[0]
            self.assertIn("--host-agent-audit", command)
            self.assertNotIn("--allow-offline-review", command)

    def test_broken_native_audit_symlink_is_not_offline_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            review = work / "review" / "requirements"
            review.mkdir(parents=True)
            (review / "merge-receipt.json").write_text("{}")
            (review / "extraction-manifest.json").write_text(json.dumps({"run_id": "r"}))
            (review / "host-agent-run.json").symlink_to(work / "missing-audit.json")
            with patch.object(wrapper, "run_stage") as run, self.assertRaises(SystemExit):
                wrapper.main(["r.docx", "i.docx", "out.docx", "--work-dir", str(work),
                              "--llm-response", "response.json"])
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
