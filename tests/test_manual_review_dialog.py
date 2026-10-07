"""Focused regression for the integrated three-window manual timeline."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QScrollArea, QWidget
from manual_review_dialog import ManualSubtitleReviewDialog
from qt_app import STYLE


class FakePlayer:
    def __init__(self, path, alias):
        self.ms = 0
        self.playing = False
    def position(self):
        return self.ms
    def duration(self):
        return 120000
    def is_playing(self):
        return self.playing
    def play(self):
        self.playing = True
    def pause(self):
        self.playing = False
    def set_position(self, position):
        self.ms = int(position)
    def close(self):
        self.playing = False


class ManualTimelineDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        for name in ('msyh.ttc','msyhbd.ttc','arial.ttf'):
            font=Path('C:/Windows/Fonts')/name
            if font.is_file(): QFontDatabase.addApplicationFont(str(font))

    def setUp(self):
        self.previous_style=self.app.styleSheet()
        self.app.setStyleSheet(STYLE)

    def tearDown(self):
        self.app.setStyleSheet(self.previous_style)

    def test_playback_follows_cursor_and_manual_click_only_while_paused(self):
        with tempfile.TemporaryDirectory() as directory:
            wav = Path(directory) / 'short.wav'
            wav.write_bytes(b'local-review-audio')
            clips = []
            for region in (1, 2, 3):
                start = region * 1000.0
                clips.append(dict(region=region, movie_start=start,
                    subtitle_time=start+30, audio=str(wav), slow_audio=str(wav),
                    vad=[[1, 5], [10, 15], [31, 36]], initial_offset=0,
                    rows=[dict(start=start+second, end=start+second+3,
                               text=f'cue {second}', zh='')
                          for second in (1, 11, 20, 31, 41, 50)]))
            parent = QWidget()
            parent.resize(1100, 700)
            with patch('manual_review_dialog.WindowsWavPlayer', FakePlayer):
                dialog = ManualSubtitleReviewDialog(parent, dict(source='candidate.srt', clips=clips))
                dialog.show()
                self.app.processEvents()
                self.assertEqual(dialog.width(), min(935,dialog.screen().availableGeometry().width()))
                self.assertGreaterEqual(dialog.height(), 595)
                self.assertLessEqual(dialog.width(), parent.width())
                self.assertLessEqual(dialog.height(), parent.height())
                self.assertEqual(dialog.tabs.count(), 3)
                for index, region in enumerate(dialog.pages):
                    dialog.tabs.setCurrentIndex(index)
                    self.app.processEvents()
                    self.assertFalse(any(area.widget() is region for area in dialog.findChildren(QScrollArea)))
                    viewport = region.parentWidget()
                    timeline_rect = QRect(
                        region.timeline.mapTo(viewport, region.timeline.rect().topLeft()),
                        region.timeline.size(),
                    )
                    self.assertEqual(timeline_rect.intersected(viewport.rect()).height(),
                                     region.timeline.height())
                    self.assertEqual(region.timeline.visibleRegion().boundingRect(),region.timeline.rect())
                    self.assertTrue(viewport.rect().contains(QRect(
                        region.play_button.mapTo(viewport, region.play_button.rect().topLeft()),
                        region.play_button.size())))
                    self.assertTrue(viewport.rect().contains(QRect(
                        region.spin.mapTo(viewport, region.spin.rect().topLeft()),region.spin.size())))
                dialog.tabs.setCurrentIndex(0)
                page = dialog.pages[0]
                self.assertTrue(page.cue_card.isVisible())
                self.assertEqual((page.prev_page.text(), page.next_page.text()), ('上一页', '下一页'))
                page.seek(12)
                self.assertEqual(page.selected, 1)
                self.app.processEvents()
                cue_rect=next(rect for index,rect in page.timeline.cue_rects if index==2)
                QTest.mouseClick(page.timeline,Qt.MouseButton.LeftButton,pos=cue_rect.center().toPoint())
                self.assertEqual(page.selected, 2)
                self.assertTrue(page.manual_selection)
                page.toggle()
                self.assertEqual(page.selected, 1)
                self.assertFalse(page.manual_selection)
                page.clicked_cue(2)
                self.assertEqual(page.selected, 1)
                page.players[1.0].ms = 31500
                page.refresh()
                self.assertEqual(page.timeline.view_start, 30)
                self.assertEqual(page.selected, 3)
                page.toggle()
                page.change_half(0)
                self.assertEqual(page.timeline.view_start, 0)
                for item, offset in zip(dialog.pages, (-6.83, -6.76, -6.90)):
                    item.spin.setValue(offset)
                    item.toggle_confirmation()
                    self.assertTrue(item.locked)
                self.assertTrue(dialog.confirm_button.isEnabled())
                dialog.confirm()
                self.assertEqual(dialog.confirmed_offsets, [-6.83, -6.76, -6.90])
                self.assertFalse(Path(dialog._audio_stage.name).exists())


if __name__ == '__main__':
    unittest.main()
