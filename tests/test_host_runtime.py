from __future__ import annotations

import unittest

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from host_runtime import (  # noqa: E402
    HostAdapterUnavailable,
    HostRuntimeMismatch,
    automatic_adapter_id,
    inspect_host_runtime,
)


class HostRuntimeTests(unittest.TestCase):
    def test_codex_selects_the_codex_native_adapter(self) -> None:
        context = inspect_host_runtime(
            expected="codex",
            environ={"THESIS_FORGE_HOST_RUNTIME": "codex"},
            require=True,
        )
        self.assertEqual(automatic_adapter_id(context), "codex")

    def test_a_new_declared_host_is_not_rejected_as_an_unknown_name(self) -> None:
        context = inspect_host_runtime(
            expected="vertex",
            environ={
                "THESIS_FORGE_HOST_RUNTIME": "vertex",
                "THESIS_FORGE_HOST_RUNTIME_VERSION": "1.2",
            },
            require=True,
        )
        self.assertEqual(context.runtime, "vertex")
        self.assertEqual(context.verification_status, "declared")
        with self.assertRaisesRegex(HostAdapterUnavailable, "prepare-agent-review"):
            automatic_adapter_id(context)

    def test_expected_runtime_mismatch_fails_before_adapter_selection(self) -> None:
        with self.assertRaises(HostRuntimeMismatch):
            inspect_host_runtime(
                expected="codex",
                environ={"THESIS_FORGE_HOST_RUNTIME": "claude"},
                require=True,
            )


if __name__ == "__main__":
    unittest.main()
