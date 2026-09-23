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
import time
from pathlib import Path
from typing import Any, Mapping


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
    input_text: str | None = None,
    env: Mapping[str, str] | None = None,
    controller: Any = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``command`` with a hard deadline and descendant cleanup.

    ``input_text`` is intentionally named differently from the built-in
    ``input`` argument used by :func:`subprocess.run`; callers that drive
    AppleScript through stdin therefore use the same process-group cleanup as
    ordinary pipeline stages.
    """
    if isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("process timeout must be a positive integer")
    process = subprocess.Popen(
        command,
        cwd=cwd,
        text=True,
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=(os.name == "posix"),
        env=dict(env) if env is not None else None,
    )
    registered = False
    try:
        if controller is not None:
            controller.register(process)
            registered = True
        deadline = time.monotonic() + timeout
        pending_input = input_text
        while True:
            if controller is not None:
                controller.check()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    stdout, stderr = _terminate_and_reap(process)
                    marker = f"[process-timeout] command exceeded {timeout}s and was terminated"
                    stderr = (stderr or "") + ("\n" if stderr else "") + marker
                    return subprocess.CompletedProcess(command, 124, stdout, stderr)
                wait_for = min(0.5, remaining)
            else:
                wait_for = max(0.001, deadline - time.monotonic())
            try:
                stdout, stderr = process.communicate(
                    input=pending_input, timeout=wait_for,
                )
                pending_input = None
                if controller is not None:
                    controller.check()
                return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                pending_input = None
                if time.monotonic() >= deadline:
                    stdout, stderr = _terminate_and_reap(process)
                    marker = f"[process-timeout] command exceeded {timeout}s and was terminated"
                    stderr = (stderr or "") + ("\n" if stderr else "") + marker
                    return subprocess.CompletedProcess(command, 124, stdout, stderr)
                continue
    except KeyboardInterrupt:
        # Ctrl-C must not leave the CLI or a descendant running with our pipes
        # open. Reap the owned process group, then preserve normal interrupt
        # semantics for the caller so it can write its terminal receipt.
        _terminate_and_reap(process)
        raise
    except OSError as exc:
        # A failed communicate still belongs to this stage; expose a stable
        # execution record after cleaning up the owned process group.
        _terminate_and_reap(process)
        return subprocess.CompletedProcess(
            command, 127, "", f"{type(exc).__name__}: {exc}",
        )
    except BaseException:
        # Controller cancellation and unexpected communicate failures must not
        # leave a child or descendant alive with captured pipes open.
        if process.poll() is None:
            _terminate_and_reap(process)
        raise
    finally:
        if registered:
            controller.unregister(process)
