#!/usr/bin/env python3
"""Safe thesis-formatting entry point.

This intentionally exposes no rule-only, known-template, or supported-subset
switch.  Preparation writes an evidence-bound host-Agent review packet; final
formatting consumes the response produced by the Agent currently running this
skill.  The default is the host-neutral packet workflow: without a response,
prepare for the current conversation; with a merged response, continue a
non-release draft. No model or native CLI is selected by this default.
``--prepare-agent-review`` also explicitly selects packet preparation.
``--auto-host-agent`` selects an explicit native adapter only after the host
runtime has been declared and validated.  The selected adapter is always the
native CLI for that declared host; it never falls back across hosts.

Written requirements may be supplied as legacy binary Word ``.doc`` or OOXML
``.docx``.  Legacy input is normalized automatically in an isolated run
artifact before fresh evidence extraction; the original is never overwritten.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from host_adapters import codex as codex_adapter  # noqa: E402
from host_runtime import (  # noqa: E402
    HostRuntimeError,
    automatic_adapter_id,
    require_host_runtime,
)
from process_runner import run_process  # noqa: E402
from semantic_contract import strict_json_read  # noqa: E402
from review_limits import DEFAULT_HOST_REVIEW_CHUNK_SIZE  # noqa: E402


def pipeline_command(args: argparse.Namespace, *, prepare_host_review: bool = False,
                     llm_response: Path | None = None, run_id: str | None = None,
                     requirements_dir: Path | None = None,
                     host_agent_audit: Path | None = None,
                     merge_receipt: Path | None = None,
                     offline_merge_receipt: Path | None = None,
                     semantic_review_runtime: str | None = None,
                     semantic_review_model: str | None = None,
                     semantic_review_reasoning_effort: str | None = None) -> list[str]:
    if bool(semantic_review_runtime) != bool(semantic_review_model):
        raise ValueError("semantic review runtime and model must be supplied together")
    if semantic_review_reasoning_effort is not None and semantic_review_runtime != "codex":
        raise ValueError("semantic review reasoning effort requires Codex")
    output_policy = getattr(
        args,
        "output_policy",
        "submission" if (getattr(args, "strict_release", False)
                          or getattr(args, "require_submission_ready", False))
        else "review_draft",
    )
    command = [
        sys.executable, str(ROOT / "scripts" / "thesis_format_pipeline.py"),
        str(args.requirements), str(args.input), str(args.output or (args.work_dir / "not-generated.docx")),
        "--work-dir", str(args.work_dir),
        "--analysis-mode", "llm_primary", "--compliance-mode", "full",
        "--output-policy", output_policy,
        "--host-review-chunk-size", str(args.host_review_chunk_size),
    ]
    if requirements_dir:
        command += ["--requirements-dir", str(requirements_dir)]
    if prepare_host_review:
        command.append("--prepare-host-review")
    elif llm_response:
        command += ["--llm-response", str(llm_response)]
        # The preparation stage already created this run directory.  The
        # pipeline verifies its manifest, source bytes and code fingerprint
        # before permitting this explicit continuation.
        command.append("--allow-existing-work")
    if run_id:
        command += ["--run-id", run_id]
    if host_agent_audit:
        command += ["--host-agent-audit", str(host_agent_audit)]
    if merge_receipt:
        command += ["--merge-receipt", str(merge_receipt)]
    if offline_merge_receipt:
        command += ["--allow-offline-review", "--offline-merge-receipt", str(offline_merge_receipt)]
    if semantic_review_runtime:
        command += ["--semantic-review-runtime", semantic_review_runtime,
                    "--semantic-review-model", str(semantic_review_model)]
    if semantic_review_reasoning_effort is not None:
        command += ["--semantic-review-reasoning-effort", semantic_review_reasoning_effort]
    for option, value in (("--style-template", args.style_template),
                          ("--thesis-profile", args.thesis_profile),
                          ("--template-profile", args.template_profile),
                          ("--render-report", args.render_report)):
        if value:
            command += [option, str(value)]
    if args.require_submission_ready:
        command.append("--require-submission-ready")
    if args.strict_release:
        command.append("--strict-release")
    return command


def run_stage(command: list[str], label: str, *, timeout: int) -> int:
    print(f"\n{'=' * 70}\n{label}\n{'=' * 70}", flush=True)
    result = run_process(command, cwd=ROOT, timeout=timeout)
    if result.stdout:
        print(result.stdout[-1200:], end="" if result.stdout.endswith("\n") else "\n")
    if result.stderr:
        print("STDERR:", result.stderr[-1200:])
    return result.returncode


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "requirements", type=Path,
        help="school requirements Word file (.doc is normalized automatically; .docx passes through)",
    )
    p.add_argument("input", type=Path, help="thesis source (.tex or .docx)")
    p.add_argument("output", type=Path, nargs="?", help="formatted output DOCX")
    p.add_argument("--work-dir", type=Path,
                   help="run directory; preparation defaults to a new build/thesis-forge-<uuid> under the caller's directory; continuation requires the same directory")
    p.add_argument("--style-template", type=Path)
    p.add_argument("--thesis-profile", type=Path)
    p.add_argument("--template-profile", type=Path,
                   help="official template profile required by strict release")
    p.add_argument("--prepare-agent-review", action="store_true",
                   help="prepare packets for the current host Agent and stop before DOCX generation")
    p.add_argument("--auto-host-agent", action="store_true",
                   help="one command through the explicitly declared native host adapter")
    p.add_argument("--host-runtime",
                   help="expected native host runtime; must match THESIS_FORGE_HOST_RUNTIME")
    p.add_argument("--llm-response", type=Path,
                   help="offline complete contract-3.0 response, or an explicitly bound legacy 2.1 response, produced by the current host Agent")
    p.add_argument("--offline-review-draft", action="store_true",
                   help="complete the current agent's merged packet response as a non-release draft without Codex/OpenClaw CLI; never submission-ready")
    p.add_argument("--host-review-chunk-size", type=int, default=DEFAULT_HOST_REVIEW_CHUNK_SIZE,
                   help="target clauses per fresh Host Agent packet (default: %(default)s; source-atomic groups may exceed target)")
    p.add_argument("--host-agent-timeout", type=int, default=900,
                   help="per-chunk native host adapter timeout in seconds (default: 900)")
    p.add_argument("--host-agent-max-concurrency", type=int, default=4,
                   help="maximum number of independent Host Agent chunks in flight (default: 4)")
    p.add_argument("--host-agent-max-attempts", type=int, default=2,
                   help="maximum attempts per Host Agent chunk before failing closed (default: 2)")
    p.add_argument("--stage-timeout", type=int, default=1800,
                   help="hard timeout for each preparation, bridge, or formatting subprocess")
    p.add_argument("--host-agent-model",
                   help="explicit OpenClaw route for the OpenClaw adapter; omitted means copy the bound parent route")
    p.add_argument("--host-agent-parent-session-key",
                   help="exact parent session key to use for effective-route inheritance; never auto-discovered")
    p.add_argument("--no-host-agent-model-inheritance", action="store_true",
                   help="disable parent inheritance only with an explicit --host-agent-model route")
    p.add_argument("--host-agent-id", default="main",
                   help="OpenClaw agent id used by --auto-host-agent (default: main)")
    p.add_argument("--openclaw-bin",
                   help="optional openclaw executable used by --auto-host-agent")
    p.add_argument("--codex-bin",
                   help="optional native codex executable used by --auto-host-agent")
    p.add_argument("--codex-model",
                   help=f"native Codex model override (project default: {codex_adapter.DEFAULT_MODEL})")
    p.add_argument("--codex-reasoning-effort", type=codex_adapter.resolve_reasoning_effort,
                   help="explicit Codex effort for primary, independent and post-format review")
    p.add_argument("--allow-prompt-only", action="store_true",
                   help="explicit non-release override when native Codex lacks --output-schema")
    p.add_argument("--render-report", type=Path)
    p.add_argument("--output-policy", choices=["review_draft", "submission"], default="review_draft",
                   help="default review_draft generates a red-marked editable draft; submission is strict")
    p.add_argument("--require-submission-ready", action="store_true")
    p.add_argument("--strict-release", action="store_true",
                   help="also require official template profile, render evidence and submission readiness")
    args = p.parse_args(argv)
    # An ordinary skill is executed by the current conversation's Agent, not
    # by discovering a CLI or selecting a different model. Native execution
    # remains explicit even when host-runtime environment variables exist.
    if not (args.prepare_agent_review or args.auto_host_agent or args.llm_response):
        args.prepare_agent_review = True
    if args.work_dir is None:
        if args.llm_response:
            p.error("--llm-response continuation requires the original --work-dir")
        args.work_dir = Path.cwd() / "build" / f"thesis-forge-{uuid.uuid4().hex}"
    if not args.auto_host_agent:
        native_options = {
            "--host-runtime": args.host_runtime,
            "--codex-bin": args.codex_bin,
            "--codex-model": args.codex_model,
            "--codex-reasoning-effort": args.codex_reasoning_effort,
            "--openclaw-bin": args.openclaw_bin,
            "--host-agent-model": args.host_agent_model,
            "--host-agent-parent-session-key": args.host_agent_parent_session_key,
            "--no-host-agent-model-inheritance": args.no_host_agent_model_inheritance,
            "--allow-prompt-only": args.allow_prompt_only,
        }
        if args.host_agent_id != "main":
            native_options["--host-agent-id"] = args.host_agent_id
        supplied = [key for key, value in native_options.items()
                    if value is not None and value is not False]
        if supplied:
            p.error("native model/host options require explicit --auto-host-agent: "
                    + ", ".join(supplied))
        # Never fall back after a bad native receipt. Only the normal packet
        # path with no native audit defaults to its existing non-release gate.
        audit = args.work_dir.resolve() / "review" / "requirements" / "host-agent-run.json"
        if (args.llm_response and args.output_policy == "review_draft"
                and not args.strict_release and not args.require_submission_ready
                and not (audit.exists() or audit.is_symlink())):
            args.offline_review_draft = True
    # run_stage executes from ROOT. Resolve user paths in their invocation
    # directory first, so a loaded skill also works outside its checkout.
    for field in ("requirements", "input", "output", "work_dir", "llm_response",
                  "style_template", "thesis_profile", "template_profile", "render_report"):
        value = getattr(args, field)
        if value is not None:
            setattr(args, field, value.resolve())
    # Explicit executable paths have the same caller-relative contract; bare
    # command names retain their adapter's ordinary PATH lookup semantics.
    for field in ("codex_bin", "openclaw_bin"):
        value = getattr(args, field)
        if value and "/" in value:
            setattr(args, field, str(Path(value).resolve()))
    if args.output_policy == "review_draft" and (args.require_submission_ready or args.strict_release):
        p.error("--require-submission-ready/--strict-release require --output-policy submission")
    if args.offline_review_draft and (
        not args.llm_response or args.output_policy != "review_draft"
        or args.require_submission_ready or args.strict_release
    ):
        p.error("--offline-review-draft requires --llm-response and review_draft, and cannot release a submission")
    if sum(bool(value) for value in (
        args.prepare_agent_review, args.auto_host_agent, bool(args.llm_response),
    )) > 1:
        p.error("choose exactly one of --prepare-agent-review, --auto-host-agent, or --llm-response")
    if not args.prepare_agent_review and not args.output:
        p.error("output is required for final formatting; use --prepare-agent-review for the preparation stage")
    if args.host_agent_timeout <= 0:
        p.error("--host-agent-timeout must be a positive integer")
    if args.host_agent_max_concurrency <= 0:
        p.error("--host-agent-max-concurrency must be a positive integer")
    if args.host_agent_max_attempts <= 0:
        p.error("--host-agent-max-attempts must be a positive integer")
    if args.stage_timeout <= 0:
        p.error("--stage-timeout must be a positive integer")
    adapter_id: str | None = None
    if args.auto_host_agent:
        if args.no_host_agent_model_inheritance and not args.host_agent_model:
            p.error("--no-host-agent-model-inheritance requires --host-agent-model")
        try:
            runtime = require_host_runtime(args.host_runtime)
            adapter_id = automatic_adapter_id(runtime)
        except HostRuntimeError as exc:
            p.error(str(exc))
        if adapter_id == "codex":
            try:
                args.codex_model = codex_adapter.resolve_model(args.codex_model)
                args.codex_reasoning_effort = codex_adapter.resolve_reasoning_effort(args.codex_reasoning_effort)
            except ValueError as exc:
                p.error(str(exc))
            forbidden = []
            if args.host_agent_model:
                forbidden.append("--host-agent-model")
            if args.host_agent_parent_session_key:
                forbidden.append("--host-agent-parent-session-key")
            if args.no_host_agent_model_inheritance:
                forbidden.append("--no-host-agent-model-inheritance")
            if args.host_agent_id != "main":
                forbidden.append("--host-agent-id")
            if args.openclaw_bin:
                forbidden.append("--openclaw-bin")
            if forbidden:
                p.error(
                    "Codex native adapter does not accept OpenClaw-only options: "
                    + ", ".join(forbidden)
                )
        elif args.codex_reasoning_effort is not None:
            p.error("--codex-reasoning-effort requires the Codex adapter")
        work = args.work_dir.resolve()
        output = args.output.resolve() if args.output else None
        if work.exists() and any(work.iterdir()):
            p.error("--auto-host-agent requires a new empty --work-dir; refusing to reuse prior run artifacts")
        if output and output.exists():
            p.error("--auto-host-agent refuses to overwrite an existing output DOCX")
    if args.strict_release and (
        not args.thesis_profile or not args.template_profile
        or not args.render_report or not args.require_submission_ready
    ):
        p.error(
            "--strict-release requires --thesis-profile, --template-profile, "
            "--render-report, and --require-submission-ready"
        )

    if args.auto_host_agent:
        review_requirements = args.work_dir.resolve() / "review" / "requirements"
        execution_requirements = args.work_dir.resolve() / "execution" / "requirements"
        prepare = pipeline_command(args, prepare_host_review=True,
                                   requirements_dir=review_requirements)
        code = run_stage(prepare, "fresh extraction + Host Agent packet", timeout=args.stage_timeout)
        if code:
            return code
        extraction_manifest = review_requirements / "extraction-manifest.json"
        try:
            payload = strict_json_read(extraction_manifest)
            run_id = payload["run_id"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"auto Host Agent failed: cannot read fresh run_id: {exc}", file=sys.stderr)
            return 2
        response_out = args.work_dir.resolve() / "review" / "host-agent-response.json"
        bridge = [
            sys.executable, str(ROOT / "scripts" / "host_agent_bridge.py"),
            str(review_requirements),
            "--response-out", str(response_out),
            "--run-id", str(run_id),
            "--timeout", str(args.host_agent_timeout),
            "--max-concurrency", str(args.host_agent_max_concurrency),
            "--max-attempts", str(args.host_agent_max_attempts),
        ]
        if args.host_runtime:
            bridge += ["--host-runtime", args.host_runtime]
        if adapter_id == "codex":
            if args.codex_bin:
                bridge += ["--codex-bin", args.codex_bin]
            if args.codex_model:
                bridge += ["--codex-model", args.codex_model]
            if args.codex_reasoning_effort is not None:
                bridge += ["--codex-reasoning-effort", args.codex_reasoning_effort]
            if args.allow_prompt_only:
                bridge.append("--allow-prompt-only")
            label = "explicit Codex native adapter review + provenance merge"
        else:
            bridge += ["--agent-id", args.host_agent_id]
            if args.host_agent_model:
                bridge += ["--model", args.host_agent_model]
            elif args.no_host_agent_model_inheritance:
                bridge.append("--no-inherit-parent-model")
            else:
                bridge.append("--inherit-parent-model")
            if args.host_agent_parent_session_key:
                bridge += ["--parent-session-key", args.host_agent_parent_session_key]
            if args.openclaw_bin:
                bridge += ["--openclaw-bin", args.openclaw_bin]
            label = "explicit OpenClaw adapter review + provenance merge"
        code = run_stage(bridge, label, timeout=args.stage_timeout)
        if code:
            return code
        final = pipeline_command(
            args, llm_response=response_out, run_id=str(run_id),
            requirements_dir=execution_requirements,
            host_agent_audit=review_requirements / "host-agent-run.json",
            merge_receipt=review_requirements / "merge-receipt.json",
            semantic_review_runtime=runtime.runtime if adapter_id == "codex" else None,
            semantic_review_model=args.codex_model if adapter_id == "codex" else None,
            semantic_review_reasoning_effort=args.codex_reasoning_effort if adapter_id == "codex" else None,
        )
        return run_stage(
            final,
            "full deterministic format + declaration resources + DOCX audits",
            timeout=args.stage_timeout,
        )

    if args.prepare_agent_review:
        print(json.dumps({
            "workflow": "current_conversation_packets",
            "work_dir": str(args.work_dir),
            "semantic_executor": "current_agent",
            "model_selection": "unchanged_by_skill",
            "provider_model_verified": False,
            "submission_ready": False,
        }), flush=True)
        command = pipeline_command(
            args, prepare_host_review=True,
            requirements_dir=args.work_dir.resolve() / "review" / "requirements",
        )
    elif args.llm_response:
        review_requirements = args.work_dir.resolve() / "review" / "requirements"
        extraction_manifest = review_requirements / "extraction-manifest.json"
        audit = review_requirements / "host-agent-run.json"
        receipt = review_requirements / "merge-receipt.json"
        if not extraction_manifest.is_file() or not receipt.is_file() or (
            not args.offline_review_draft and not audit.is_file()
        ):
            p.error(
                "manual --llm-response requires the fresh review extraction manifest and "
                "merge-receipt.json; submission also requires host-agent-run.json under "
                "--work-dir/review/requirements"
            )
        try:
            extraction = strict_json_read(extraction_manifest)
            current_run_id = extraction["run_id"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            p.error(f"cannot read fresh semantic-review run_id: {exc}")
        command = pipeline_command(
            args, llm_response=args.llm_response,
            requirements_dir=args.work_dir.resolve() / "execution" / "requirements",
            run_id=str(current_run_id),
            host_agent_audit=None if args.offline_review_draft else audit,
            merge_receipt=None if args.offline_review_draft else receipt,
            offline_merge_receipt=receipt if args.offline_review_draft else None,
        )
    else:
        p.error("final formatting requires --llm-response, or use --auto-host-agent for one-command execution")
    return run_stage(command, "thesis-format stage", timeout=args.stage_timeout)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
