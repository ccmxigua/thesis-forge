from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

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

    def test_success_preserves_stdout_and_returncode(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            result = run_process(
                [sys.executable, "-c", "print('ok')"], cwd=Path(td), timeout=5,
            )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
