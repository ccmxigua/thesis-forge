#!/usr/bin/env python3
"""Safe thesis-formatting entry point.

This intentionally exposes no rule-only, known-template, or supported-subset
switch.  Preparation writes an evidence-bound host-Agent review packet; final
formatting consumes the response produced by the Agent currently running this
skill.  ``--prepare-agent-review`` is the host-neutral packet workflow.
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
import subprocess
import sys
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


def pipeline_command(args: argparse.Namespace, *, prepare_host_review: bool = False,
                     llm_response: Path | None = None, run_id: str | None = None,
                     requirements_dir: Path | None = None,
                     host_agent_audit: Path | None = None,
                     merge_receipt: Path | None = None) -> list[str]:
    command = [
        sys.executable, str(ROOT / "scripts" / "thesis_format_pipeline.py"),
        str(args.requirements), str(args.input), str(args.output or (args.work_dir / "not-generated.docx")),
        "--work-dir", str(args.work_dir),
        "--analysis-mode", "llm_primary", "--compliance-mode", "full",
        "--host-review-chunk-size", str(args.host_review_chunk_size),
    ]
    if requirements_dir:
        command += ["--requirements-dir", str(requirements_dir)]
    if prepare_host_review:
        command.append("--prepare-host-review")
    elif llm_response:
        command += ["--llm-response", str(llm_response)]
    if run_id:
        command += ["--run-id", run_id]
    if host_agent_audit:
        command += ["--host-agent-audit", str(host_agent_audit)]
    if merge_receipt:
        command += ["--merge-receipt", str(merge_receipt)]
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


def run_stage(command: list[str], label: str) -> int:
    print(f"\n{'=' * 70}\n{label}\n{'=' * 70}", flush=True)
    result = subprocess.run(command, cwd=ROOT)
    return result.returncode


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "requirements", type=Path,
        help="school requirements Word file (.doc is normalized automatically; .docx passes through)",
    )
    p.add_argument("input", type=Path, help="thesis source (.tex or .docx)")
    p.add_argument("output", type=Path, nargs="?", help="formatted output DOCX")
    p.add_argument("--work-dir", type=Path, required=True)
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
                   help="offline complete contract-2.1 response produced by the current host Agent")
    p.add_argument("--host-review-chunk-size", type=int, default=20,
                   help="clauses per fresh Host Agent packet (default: 20)")
    p.add_argument("--host-agent-timeout", type=int, default=900,
                   help="per-chunk native host adapter timeout in seconds (default: 900)")
    p.add_argument("--host-agent-max-concurrency", type=int, default=4,
                   help="maximum number of independent Host Agent chunks in flight (default: 4)")
    p.add_argument("--host-agent-max-attempts", type=int, default=2,
                   help="maximum attempts per Host Agent chunk before failing closed (default: 2)")
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
    p.add_argument("--codex-model", default=codex_adapter.DEFAULT_MODEL,
                   help=f"explicit native Codex model (default: {codex_adapter.DEFAULT_MODEL})")
    p.add_argument("--render-report", type=Path)
    p.add_argument("--require-submission-ready", action="store_true")
    p.add_argument("--strict-release", action="store_true",
                   help="also require official template profile, render evidence and submission readiness")
    args = p.parse_args(argv)
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
        code = run_stage(prepare, "fresh extraction + Host Agent packet")
        if code:
            return code
        extraction_manifest = review_requirements / "extraction-manifest.json"
        try:
            payload = json.loads(extraction_manifest.read_text(encoding="utf-8"))
            run_id = payload["run_id"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
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
            bridge += ["--codex-model", args.codex_model]
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
        code = run_stage(bridge, label)
        if code:
            return code
        final = pipeline_command(
            args, llm_response=response_out, run_id=str(run_id),
            requirements_dir=execution_requirements,
            host_agent_audit=review_requirements / "host-agent-run.json",
            merge_receipt=review_requirements / "merge-receipt.json",
        )
        return run_stage(final, "full deterministic format + declaration resources + DOCX audits")

    if args.prepare_agent_review:
        command = pipeline_command(
            args, prepare_host_review=True,
            requirements_dir=args.work_dir.resolve() / "review" / "requirements",
        )
    elif args.llm_response:
        review_requirements = args.work_dir.resolve() / "review" / "requirements"
        extraction_manifest = review_requirements / "extraction-manifest.json"
        audit = review_requirements / "host-agent-run.json"
        receipt = review_requirements / "merge-receipt.json"
        if not extraction_manifest.is_file() or not audit.is_file() or not receipt.is_file():
            p.error(
                "manual --llm-response requires the fresh review extraction manifest, "
                "host-agent-run.json, and merge-receipt.json under --work-dir/review/requirements"
            )
        try:
            extraction = json.loads(extraction_manifest.read_text(encoding="utf-8"))
            current_run_id = extraction["run_id"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            p.error(f"cannot read fresh semantic-review run_id: {exc}")
        command = pipeline_command(
            args, llm_response=args.llm_response,
            requirements_dir=args.work_dir.resolve() / "execution" / "requirements",
            run_id=str(current_run_id),
            host_agent_audit=audit,
            merge_receipt=receipt,
        )
    else:
        p.error("final formatting requires --llm-response, or use --auto-host-agent for one-command execution")
    return subprocess.run(command, cwd=ROOT).returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
