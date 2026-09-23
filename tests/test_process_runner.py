from __future__ import annotations

import sys
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from process_runner import run_process  # noqa: E402


class ProcessRunnerTests(unittest.TestCase):
    def test_timeout_returns_terminal_record_and_reaps_child(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            result = run_process(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                cwd=Path(td), timeout=1,
            )
        self.assertEqual(result.returncode, 124)
        self.assertIn("[process-timeout]", result.stderr)

    @unittest.skipUnless(sys.platform.startswith("darwin") or sys.platform.startswith("linux"), "POSIX process groups required")
    def test_timeout_terminates_descendant_holding_captured_pipes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            command = [
                sys.executable, "-c",
                "import subprocess,sys,time; "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)']); "
                "print(child.pid, flush=True); time.sleep(20)",
            ]
            started = time.monotonic()
            result = run_process(command, cwd=Path(td), timeout=1)
            elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 124)
        self.assertIn("[process-timeout]", result.stderr)
        self.assertTrue(result.stdout.strip().isdigit())
        # The child inherited the captured pipes. If the process group were
        # not terminated, communicate() would remain blocked until its sleep
        # elapsed instead of returning near the configured deadline.
        self.assertLess(elapsed, 8)

    def test_success_preserves_stdout_and_returncode(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            result = run_process(
                [sys.executable, "-c", "print('ok')"], cwd=Path(td), timeout=5,
            )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "ok")

    def test_input_and_environment_are_forwarded_to_child(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            result = run_process(
                [sys.executable, "-c", "import os,sys; print(os.environ['TF_TEST_VALUE']); print(sys.stdin.read(), end='')"],
                cwd=Path(td), timeout=5, input_text="payload\n",
                env={**os.environ, "TF_TEST_VALUE": "bound"},
            )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "bound\npayload\n")

    def test_communicate_oserror_terminates_owned_process_and_returns_127(self) -> None:
        process = Mock(pid=12345)
        process.communicate.side_effect = OSError("broken pipe")
        with patch("process_runner.subprocess.Popen", return_value=process), \
             patch("process_runner._terminate_and_reap", return_value=("", "")) as terminate:
            result = run_process(["host-cli"], cwd=Path("."), timeout=5)
        terminate.assert_called_once_with(process)
        self.assertEqual(result.returncode, 127)
        self.assertIn("broken pipe", result.stderr)

    def test_keyboard_interrupt_terminates_and_reaps_owned_process(self) -> None:
        process = Mock()
        process.pid = 12345
        process.communicate.side_effect = KeyboardInterrupt
        with patch("process_runner.subprocess.Popen", return_value=process), \
             patch("process_runner._terminate_and_reap") as terminate:
            with self.assertRaises(KeyboardInterrupt):
                run_process(["host-cli"], cwd=Path("."), timeout=5)
        terminate.assert_called_once_with(process)


if __name__ == "__main__":
    unittest.main()
