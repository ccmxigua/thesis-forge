"""Bounded subprocess execution for pipeline stages.

Every stage owns a process group so a timed-out CLI cannot leave a descendant
holding captured pipes open.  A timeout is represented as return code 124 and
an explicit marker in stderr; callers can therefore persist a truthful stage
record without having to catch a second exception while finalizing manifests.
"""
from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path


def _terminate_and_reap(process: subprocess.Popen[str]) -> tuple[str, str]:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    else:
        process.terminate()
    try:
        return process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        return process.communicate()


def run_process(
    command: list[str],
    *,
    cwd: Path,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    """Run ``command`` with a hard deadline and descendant cleanup."""
    if isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("process timeout must be a positive integer")
    process = subprocess.Popen(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=(os.name == "posix"),
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        stdout, stderr = _terminate_and_reap(process)
        marker = f"[process-timeout] command exceeded {timeout}s and was terminated"
        stderr = (stderr or "") + ("\n" if stderr else "") + marker
        return subprocess.CompletedProcess(command, 124, stdout, stderr)
    except OSError as exc:
        # A failed communicate still belongs to this stage; ensure no child
        # survives before exposing a stable execution record.
        _terminate_and_reap(process)
        return subprocess.CompletedProcess(
            command, 127, "", f"{type(exc).__name__}: {exc}",
        )
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
