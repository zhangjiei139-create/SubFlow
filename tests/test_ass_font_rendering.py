"""Check visible glyph height with the bundled player renderer, not just Qt metrics."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from PIL import Image
from PySide6.QtWidgets import QApplication
import subtitle_fonts as fonts
import subtitle_style_engine as engine

APP = QApplication.instance() or QApplication([])
FFMPEG = Path('C:/Program Files/SubFlow/_internal/tools/ffmpeg/bin/ffmpeg.exe')


@unittest.skipUnless(FFMPEG.is_file(), 'bundled subtitle renderer is unavailable')
class AssFontRenderingTests(unittest.TestCase):
    def test_auto_target_survives_different_font_metrics_and_video_resolutions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'fonts').mkdir()
            for code, _, filename in fonts.BUILTINS:
                with self.subTest(font=code):
                    path = fonts.assets_root() / code / filename
                    shutil.copy2(path, root / 'fonts' / filename)
                    info = fonts.font_info(path)
                    size, _ = engine.automatic_size(path, ['Hgjpqy'], 1280, 720)
                    private = fonts.header(dict(family=info['family'], size=size,
                                                color='#FFFFFF', bold=info['bold']))
                    body = ('\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'
                            'Dialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,Hgjpqy\n')
                    (root / 'test.ass').write_text(private.decode('utf-8') + body, encoding='utf-8')
                    for height in (720, 1080):
                        subprocess.run([str(FFMPEG), '-v', 'error', '-y', '-f', 'lavfi', '-i',
                                        f'color=c=black:s={height*16//9}x{height}:d=0.1', '-vf',
                                        'subtitles=test.ass:fontsdir=fonts', '-frames:v', '1', 'test.png'],
                                       cwd=root, capture_output=True, check=True)
                        with Image.open(root / 'test.png') as picture:
                            box = picture.convert('L').point(lambda x: 255 if x > 220 else 0).getbbox()
                        self.assertIsNotNone(box)
                        actual = (box[3] - box[1]) / height
                        self.assertAlmostEqual(actual, .05, delta=.006,
                                               msg=f'{code}, ASS {size}, picture {height}, height {actual}')


if __name__ == '__main__':
    unittest.main()
