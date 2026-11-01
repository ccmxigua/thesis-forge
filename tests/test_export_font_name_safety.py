"""Existing pure font-name/identity regressions, without formatter WIP fixtures."""
import hashlib
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import derive_review_draft_repairs as derive
from rendered_format_audit import _font_key


class ExportFontNameSafetyTests(unittest.TestCase):
    def test_derived_repair_code_identity_includes_shared_font_logic(self):
        identity = derive._code_identity()
        self.assertEqual(identity['files']['scripts/font_declarations.py'], hashlib.sha256(
            (ROOT / 'scripts/font_declarations.py').read_bytes()).hexdigest())

    def test_export_font_comparison_never_collapses_chinese_families_to_empty_keys(self):
        self.assertTrue(_font_key('宋体'))
        self.assertTrue(_font_key('仿宋'))
        self.assertNotEqual(_font_key('宋体'), _font_key('仿宋'))
        self.assertEqual(_font_key('宋体'), _font_key('SimSun'))
        self.assertEqual(_font_key('仿宋'), _font_key('FangSong'))
        self.assertEqual(_font_key('ABCDEF+SimSun'), _font_key('宋体'))
        self.assertNotEqual(_font_key('华文宋体'), _font_key('宋体'))
        self.assertNotEqual(_font_key('未知字体甲'), _font_key('未知字体乙'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
