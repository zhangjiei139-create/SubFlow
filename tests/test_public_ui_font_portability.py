"""Public source uses installed UI fonts without distributing Windows fonts."""
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'qml_frontend'))
import main as frontend


class PublicUIFontPortabilityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.ui_root = self.root / 'ui'
        self.windows = self.root / 'system'
        self.system_font = self.windows / 'Fonts' / 'msyh.ttc'
        self.system_font.parent.mkdir(parents=True)
        self.system_font.touch()
        self.operating_system = SimpleNamespace(name='nt', environ={'WINDIR': str(self.windows)})

    def test_installed_font_is_used_when_bundled_font_is_absent(self):
        with patch.object(frontend, 'os', self.operating_system), \
                patch.object(frontend.QFontDatabase, 'addApplicationFont', return_value=7) as add, \
                patch.object(frontend.QFontDatabase, 'applicationFontFamilies', return_value=['Fixture Sans']):
            self.assertEqual(frontend.load_ui_font(self.ui_root), 'Fixture Sans')
        add.assert_called_once_with(str(self.system_font))

    def test_bundled_font_keeps_priority_when_available(self):
        bundled = self.ui_root / 'fonts' / 'NotoSansSC-VF.ttf'
        bundled.parent.mkdir(parents=True)
        bundled.touch()
        with patch.object(frontend, 'os', self.operating_system), \
                patch.object(frontend.QFontDatabase, 'addApplicationFont', return_value=8) as add, \
                patch.object(frontend.QFontDatabase, 'applicationFontFamilies', return_value=['Bundled Fixture']):
            self.assertEqual(frontend.load_ui_font(self.ui_root), 'Bundled Fixture')
        add.assert_called_once_with(str(bundled))

    def test_missing_font_files_keep_named_system_fallback(self):
        self.system_font.unlink()
        with patch.object(frontend, 'os', self.operating_system), \
                patch.object(frontend.QFontDatabase, 'addApplicationFont') as add:
            self.assertEqual(frontend.load_ui_font(self.ui_root), 'Microsoft YaHei UI')
        add.assert_not_called()


if __name__ == '__main__':
    unittest.main()
