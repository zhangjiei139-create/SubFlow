import copy
import json
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QWidget
from manual_review_dialog import ManualSubtitleReviewDialog
from tests.test_manual_review_dialog import FakePlayer


def evidence_for(wav):
    clips=[]
    for region in (1,2,3):
        start=region*1000.
        clips.append(dict(region=region,movie_start=start,subtitle_time=start+30,
            audio=str(wav),slow_audio=str(wav),vad=[[1,5],[10,15],[31,36]],initial_offset=0,
            selection_warning='',rows=[dict(start=start+s,end=start+s+3,text=f'Hello {s}',zh='')
                                      for s in (1,11,20,31,41,50)]))
    return dict(source='candidate.srt',clips=clips,region_options={str(i):[{},{}] for i in (1,2,3)})


class ManualWindowSwitchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.wav=Path(self.temp.name)/'clip.wav';self.wav.write_bytes(b'audio')
        self.player_patch=patch('manual_review_dialog.WindowsWavPlayer',FakePlayer)
        self.player_patch.start();self.addCleanup(self.player_patch.stop)
        self.parent=QWidget();self.parent.resize(1024,700)
        self.evidence=evidence_for(self.wav)
        self.dialog=ManualSubtitleReviewDialog(self.parent,self.evidence)
        self.addCleanup(self.dialog.reject)

    def wait(self):
        until=time.monotonic()+3
        while self.dialog._window_worker is not None and time.monotonic()<until:
            self.app.processEvents();time.sleep(.01)
        self.assertIsNone(self.dialog._window_worker)

    def test_switch_runs_off_gui_thread_and_resets_only_selected_region(self):
        for page in self.dialog.pages:
            page.spin.setValue(1.5);page.toggle_confirmation()
        old_players=self.dialog.pages[0].players
        worker_ids=[];release=threading.Event();started=threading.Event()
        def replace(evidence,index,log):
            worker_ids.append(threading.get_ident());started.set();release.wait(2)
            clip=copy.deepcopy(evidence['clips'][index])
            clip['movie_start']+=120;clip['subtitle_time']+=120
            for row in clip['rows']:row['start']+=120;row['end']+=120
            evidence['clips'][index]=clip
            return clip
        with patch('manual_review_dialog.manual_review_prepare.replace_review_window',side_effect=replace):
            self.dialog.replace_region(0)
            self.assertFalse(self.dialog.confirm_button.isEnabled())
            self.assertFalse(self.dialog.tabs.isEnabled())
            self.assertTrue(started.wait(1))
            self.assertNotEqual(worker_ids[0],threading.get_ident())
            release.set();self.wait()
        first=self.dialog.pages[0]
        self.assertEqual(first.clip['movie_start'],1120)
        self.assertIs(first.clip,self.evidence['clips'][0])
        self.assertFalse(first.locked);self.assertEqual(first.offset,0)
        self.assertFalse(old_players)
        self.assertTrue(all(p.locked and p.offset==1.5 for p in self.dialog.pages[1:]))
        self.assertFalse(self.dialog.confirm_button.isEnabled())

    def test_unavailable_replacement_keeps_audio_offset_and_confirmation(self):
        page=self.dialog.pages[0];page.spin.setValue(2);page.toggle_confirmation()
        old_clip=page.clip;old_players=page.players
        with patch('manual_review_dialog.manual_review_prepare.replace_review_window',side_effect=ValueError('本区没有其他片段')), \
             patch('manual_review_dialog.QMessageBox.information') as warning:
            self.dialog.replace_region(0);self.wait()
        self.assertIs(page.clip,old_clip);self.assertIs(page.players,old_players)
        self.assertTrue(page.locked);self.assertEqual(page.offset,2)
        warning.assert_called_once()

    def test_new_audio_open_failure_restores_visible_point_and_saved_evidence(self):
        self.evidence['review_dir']=self.temp.name
        old=self.evidence['clips'][0]
        replacement=copy.deepcopy(old)
        replacement['movie_start']+=120;replacement['subtitle_time']+=120
        replacement['audio']=str(Path(self.temp.name)/'missing.wav')
        self.evidence['clips'][0]=replacement
        with patch('manual_review_dialog.QMessageBox.information'):
            self.dialog._replacement_ready(0,old,replacement)
        self.assertIs(self.evidence['clips'][0],old)
        self.assertIs(self.dialog.pages[0].clip,old)
        saved=json.loads((Path(self.temp.name)/'manual-review.json').read_text(encoding='utf8'))
        self.assertEqual(saved['clips'][0]['subtitle_time'],old['subtitle_time'])

    def test_closing_while_preparing_does_not_open_new_audio_after_close(self):
        release=threading.Event();started=threading.Event()
        def replace(evidence,index,log):
            started.set();release.wait(2)
            return evidence['clips'][index]
        with patch('manual_review_dialog.manual_review_prepare.replace_review_window',side_effect=replace):
            self.dialog.replace_region(0);self.assertTrue(started.wait(1))
            self.dialog.reject();release.set();self.wait()
        self.assertTrue(self.dialog._audio_released)
        self.assertFalse(self.dialog.pages[0].players)


if __name__=='__main__':unittest.main()
