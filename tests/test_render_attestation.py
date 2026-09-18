from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import render_attestation


class RenderAttestationTest(unittest.TestCase):
    def test_caller_chosen_secret_environment_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as td, \
                patch.object(render_attestation, "default_key_path", return_value=Path(td) / "missing.key"), \
                patch.dict(os.environ, {"THESIS_WORD_ATTESTATION_SECRET_HEX": "11" * 32}):
            self.assertIsNone(render_attestation.load_key(create=False))

    def test_created_key_is_private_and_long_enough(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "key"
            with patch.object(render_attestation, "default_key_path", return_value=path):
                key = render_attestation.load_key(create=True)
            self.assertGreaterEqual(len(key), 32)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_rejects_group_readable_key(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "key"; path.write_bytes(b"x" * 32); path.chmod(0o640)
            with patch.object(render_attestation, "default_key_path", return_value=path), \
                    self.assertRaises(PermissionError):
                render_attestation.load_key(create=False)


if __name__ == "__main__":
    unittest.main()
