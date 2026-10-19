import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import tempfile
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QMessageBox, QWidget
import subtitle_tool_core as core
import translation_review as review
from translation_review_dialog import TranslationReviewDialog


class TranslationReviewDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        events=[core.SubtitleEvent('00:00:01,000','00:00:03,000','Think differently.')]
        self.report=review.report_path(self.root/'translation-cache-de.jsonl')
        issue=dict(index=1,start=events[0].start,end=events[0].end,source=events[0].text,
                   translation='Denke anders框.',reason='含中文字符',status='needs_review',repair_attempts=2)
        review.save_progress(events,'de','en',self.root/'translation-cache-de.jsonl',
            self.root/'translated-de.srt',{1:issue},complete=True,translations={1:issue['translation']})
        self.patch=patch('translation_review_dialog.translation_remux.load_bound_reports',
            side_effect=lambda _output:({},[(self.report,review.load_report(self.report))]))
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.parent=QWidget(); self.parent.resize(1280,800)
        self.dialog=TranslationReviewDialog(self.parent,self.root/'movie.SF.mkv')
        self.addCleanup(self.dialog.deleteLater)

    def _wait(self):
        end=time.monotonic()+3
        while self.dialog._worker is not None and time.monotonic()<end:
            self.app.processEvents(); time.sleep(.01)
        self.assertIsNone(self.dialog._worker)

    def test_edit_saves_original_timing_and_remuxes_without_full_processing(self):
        self.dialog.editor.setPlainText('Denke anders.')
        with patch('translation_review_dialog.translation_remux.remux_corrected_output',return_value='movie.SF.mkv') as remux:
            self.dialog.save_and_remux()
            self._wait()
        report=review.load_report(self.report)
        self.assertEqual(report['issues'][0]['translation'],'Denke anders.')
        self.assertEqual(report['issues'][0]['start'],'00:00:01,000')
        self.assertEqual(report['issues'][0]['source'],'Think differently.')
        self.assertTrue(self.dialog.remuxed)
        remux.assert_called_once()

    def test_remaining_warning_requires_explicit_user_confirmation(self):
        self.dialog.editor.setPlainText('Anders denken框.')
        with patch('translation_review_dialog.QMessageBox.question',return_value=QMessageBox.StandardButton.No), \
             patch('translation_review_dialog.translation_remux.remux_corrected_output') as remux:
            self.dialog.save_and_remux()
        remux.assert_not_called()
        self.assertEqual(review.load_report(self.report)['issues'][0]['translation'],'Denke anders框.')

    def test_late_stop_does_not_claim_published_output_was_unchanged(self):
        self.dialog._cancel.set()
        self.dialog._remux_done('movie.SF.mkv')
        self.dialog._remux_finished()
        self.assertIn('已写入成品',self.dialog.status.text())

    def test_remux_progress_verification_failure_and_retry(self):
        self.dialog.message.emit('mkvmerge 封装开始：主输入文件20GiB。')
        self.dialog.message.emit('mkvmerge 封装进度 47%；已用1秒。')
        self.assertEqual(self.dialog.mux_bar.value(),47)
        self.dialog.message.emit('mkvmerge 封装完成：耗时1秒。')
        self.assertEqual(self.dialog.mux_bar.maximum(),0)
        self.assertIn('正在验证',self.dialog.mux_label.text())
        messages=[]
        self.dialog.message.connect(messages.append)
        with patch('translation_review_dialog.QMessageBox.warning'):
            self.dialog._remux_error('标签发生变化')
        self.assertIn('标签发生变化',messages[-1])
        self.assertIn('原成品保留',self.dialog.mux_label.text())
        self.assertEqual(self.dialog.mux_bar.maximum(),100)
        self.dialog.message.emit('正在写入修改后的译文；原成品保留。')
        self.assertEqual(self.dialog.mux_bar.maximum(),0)
        self.dialog.message.emit('mkvmerge 封装进度 25%；已用1秒。')
        self.assertEqual(self.dialog.mux_bar.value(),25)
        self.dialog._remux_done('movie.SF.mkv')
        self.assertEqual(self.dialog.mux_bar.value(),100)
        self.assertIn('已完成',self.dialog.mux_label.text())


if __name__=='__main__': unittest.main()
