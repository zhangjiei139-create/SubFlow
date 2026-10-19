import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PySide6.QtWidgets import QApplication,QWidget
from manual_review_dialog import ManualSubtitleReviewDialog
from tests.test_manual_review_dialog import FakePlayer

class ManualAudioSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        wav=Path(self.tmp.name)/'clip.wav';wav.write_bytes(b'fixture')
        clips=[dict(region=i,movie_start=i*1000,subtitle_time=i*1000+30,
            audio=str(wav),slow_audio=str(wav),vad=[[1,5],[10,15]],initial_offset=0,
            rows=[dict(start=i*1000+j,end=i*1000+j+3,text='hello',zh='') for j in (1,11,21,31)]) for i in (1,2,3)]
        self.parent=QWidget();self.parent.resize(1100,750);self.addCleanup(self.parent.close)
        self.evidence=dict(source='source.srt',clips=clips,validation_audio_id=2,
            audio_choices=[dict(id=1,label='轨1 英文 5.1',enabled=True),
                           dict(id=2,label='轨2 英文 双声道',enabled=True),
                           dict(id=3,label='轨3 评论',enabled=False)])
        self.patch=patch('manual_review_dialog.WindowsWavPlayer',FakePlayer);self.patch.start();self.addCleanup(self.patch.stop)
        self.dialog=ManualSubtitleReviewDialog(self.parent,self.evidence);self.addCleanup(self.dialog.close)

    def test_current_reference_shown_and_marked_tracks_disabled(self):
        self.assertEqual(self.dialog.audio_combo.currentData(),2)
        self.assertFalse(self.dialog.audio_combo.model().item(2).isEnabled())
        self.assertFalse(self.dialog.audio_switch.isEnabled())

    def test_switch_discards_confirmations_and_requests_new_preparation(self):
        for page in self.dialog.pages: page.toggle_confirmation()
        self.dialog.audio_combo.setCurrentIndex(0)
        self.assertTrue(self.dialog.audio_switch.isEnabled())
        self.dialog.request_audio_switch()
        self.assertEqual(self.dialog.result(),2)
        self.assertEqual(self.dialog.requested_audio_id,1)
        self.assertIsNone(self.dialog.confirmed_offsets)
        self.assertTrue(self.dialog._audio_released)
        self.assertEqual(self.evidence['validation_audio_id'],2)

    def test_window_worker_blocks_audio_switch(self):
        self.dialog.audio_combo.setCurrentIndex(0)
        self.dialog._window_worker=object()
        self.dialog._audio_selection_changed()
        self.dialog.request_audio_switch()
        self.assertIsNone(self.dialog.requested_audio_id)

    def test_mono_and_stereo_choices_remain_available(self):
        self.assertTrue(self.dialog.audio_combo.model().item(0).isEnabled())
        self.assertTrue(self.dialog.audio_combo.model().item(1).isEnabled())

    def test_changed_choice_cannot_confirm_old_audio(self):
        for page in self.dialog.pages: page.toggle_confirmation()
        self.dialog.audio_combo.setCurrentIndex(0)
        self.assertFalse(self.dialog.confirm_button.isEnabled())
        with patch('manual_review_dialog.QMessageBox.information') as notice:
            self.dialog.confirm()
            notice.assert_called_once()
        self.assertIsNone(self.dialog.confirmed_offsets)
