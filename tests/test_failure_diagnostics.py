from __future__ import annotations

import copy
import errno
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from failure_diagnostics import exception_diagnostics
import host_agent_bridge as bridge
import native_semantic_review as native
import tests.test_host_agent_bridge as fixtures


def test_diagnostics_keep_suppressed_context_without_frame_contents():
    secret_local = "diagnostic-local-must-not-be-captured"
    try:
        try:
            raise bridge.HostAgentCancelled("sibling failed")
        except bridge.HostAgentCancelled:
            raise PermissionError(errno.EPERM, "Operation not permitted", b"/synthetic/path") from None
    except PermissionError as error:
        diagnostics = exception_diagnostics(error)
    encoded = json.dumps(diagnostics)
    assert secret_local not in encoded
    outer, original = diagnostics["exceptions"]
    assert outer["type"] == "PermissionError"
    assert outer["context_suppressed"] is True
    assert outer["context"] == 1
    assert outer["os_error"] == {"errno": 1, "filename": "/synthetic/path", "filename2": None}
    assert original["type"] == "HostAgentCancelled"
    assert set(outer["frames"][0]) == {"file", "line", "function"}


def test_diagnostics_bound_cycles_and_long_chains():
    root = RuntimeError("root")
    child = ValueError("child")
    root.__cause__ = child
    root.__context__ = child
    child.__context__ = root
    diagnostics = exception_diagnostics(root)
    assert len(diagnostics["exceptions"]) == 2
    assert diagnostics["exceptions"][0]["cause"] == 1
    assert diagnostics["exceptions"][0]["context"] == 1
    assert diagnostics["exceptions"][1]["context"] == 0
    assert diagnostics["chain_truncated"] is False
    cursor = root
    for _ in range(70):
        cursor.__cause__ = RuntimeError("bounded")
        cursor = cursor.__cause__
    diagnostics = exception_diagnostics(root)
    assert len(diagnostics["exceptions"]) == 64
    assert diagnostics["chain_truncated"] is True


def test_diagnostics_do_not_format_exceptions_or_copy_embedded_argv():
    error = subprocess.CalledProcessError(1, ["cli", "secret-command-argument"])
    assert "secret-command-argument" not in json.dumps(exception_diagnostics(error))

    class UnformattableError(RuntimeError):
        def __str__(self):
            raise AssertionError("diagnostics must not call an exception formatter")

    assert exception_diagnostics(UnformattableError())["exceptions"][0]["type"] == "UnformattableError"


@pytest.mark.parametrize("operation", ["spawn", "cancel_cleanup", "write_prompt"])
def test_native_permission_failure_retains_location_and_never_retries(tmp_path, operation):
    """Real coverage/native/process path; OS denial injected, no CLI is run."""
    chunk, response, _, _ = fixtures.HostAgentBridgeTests._external_compliance_review_case()
    unchanged = copy.deepcopy((chunk, response))
    controller = bridge.RunController()
    process = Mock(pid=123456)
    denial = PermissionError(errno.EPERM, "Operation not permitted")
    write_fresh = native._write_fresh

    def checked_write(path, text):
        if operation == "write_prompt" and path.name == "prompt.txt":
            raise PermissionError(errno.EPERM, "Operation not permitted", str(path))
        return write_fresh(path, text)

    with (
        patch.dict(os.environ, {"THESIS_FORGE_HOST_RUNTIME": "codex"}),
        patch.object(native.codex_adapter, "resolve_binary", return_value="mock-codex"),
        patch.object(native.codex_adapter, "probe_capabilities", return_value={"output_schema_supported": True}),
        patch.object(native, "_write_fresh", side_effect=checked_write),
        patch("process_runner.subprocess.Popen", return_value=process,
              side_effect=denial if operation == "spawn" else None) as spawn,
        patch("process_runner.os.killpg", side_effect=denial) as killpg,
        patch.object(controller, "check", side_effect=[None, bridge.HostAgentCancelled("sibling failed")]),
        patch.object(bridge.time, "sleep") as sleep,
    ):
        with pytest.raises(bridge.IndependentObligationReviewError) as caught:
            bridge._run_independent_obligation_coverage_review(
                response, chunk, review_dir=tmp_path, run_id="run-external-correction",
                chunk_index=2, attempt=1, host_runtime="codex", model="gpt-6-luna",
                timeout=900, agent_id="main", runner="exec", binary="mock-codex",
                config_path=None, controller=controller, output_policy="review_draft",
            )
    assert caught.value.retryable is False
    assert isinstance(caught.value.__cause__, PermissionError)
    assert spawn.call_count == (0 if operation == "write_prompt" else 1)
    assert killpg.call_count == (1 if operation == "cancel_cleanup" else 0)
    sleep.assert_not_called()
    output = tmp_path / "independent-review-chunk-0002-attempt-01"
    audit = json.loads((output / "coverage-audit.json").read_text())
    assert audit["status"] == "failed"
    assert audit["retryable"] is False
    assert audit["submission_ready"] is False
    assert audit["provider_attempt"] == 1
    assert audit["candidate_response_sha256"] == bridge._response_sha256(response)
    exceptions = audit["failure_diagnostics"]["exceptions"]
    assert exceptions[0]["type"] == "PermissionError"
    assert exceptions[0]["os_error"]["errno"] == errno.EPERM
    coordinates = {(Path(f["file"]).name, f["function"]) for f in exceptions[0]["frames"]}
    if operation == "cancel_cleanup":
        assert ("process_runner.py", "_terminate_and_reap") in coordinates
        assert exceptions[exceptions[0]["context"]]["type"] == "HostAgentCancelled"
    elif operation == "spawn":
        assert ("process_runner.py", "run_process") in coordinates
    else:
        assert ("test_failure_diagnostics.py", "checked_write") in coordinates
        assert exceptions[0]["os_error"]["filename"] == str(output / "prompt.txt")
    assert not (output / "stdout.jsonl").exists()
    assert not (output / "response.json").exists()
    assert not list(tmp_path.glob("**/obligation-analysis-ledger.json"))
    assert len(list(tmp_path.glob("independent-review-*"))) == 1
    assert (chunk, response) == unchanged
