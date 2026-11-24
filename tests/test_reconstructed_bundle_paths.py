import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from skill_bundle import _relative

class ReconstructedBundlePaths(unittest.TestCase):
    def test_declared_ui_assets_and_regression_sources_are_valid(self):
        for path in ('assets/icon.svg', 'tests/test_reconstructed_bundle_paths.py'):
            self.assertEqual(_relative(path), path)

    def test_unsafe_and_unrelated_paths_still_rejected(self):
        for path in ('../secret', '/etc/passwd', 'assets/../secret', '.git/config', 'unrelated/file', 'tests\\bad.py'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                _relative(path)
