"""The real clear action resets both progress models without clearing active work."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QCoreApplication, QEvent, QUrl
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'qml_frontend'))
from bridge import BackendBridge
from main import LicenseBridge
from qt_workers import MuxProgressTracker


class BatchClearMuxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        for target, kwargs in (
            ('bridge.BackendBridge._detect_hardware', {}),
            ('qt_app.ProMaxQt._detect_hardware', {}),
            ('profile_model.profile_file', {'return_value':Path(self.folder.name)/'profiles.json'}),
            ('bridge.online_subtitles.load_settings', {'return_value':{}}),
        ):
            mock=patch(target, **kwargs);mock.start();self.addCleanup(mock.stop)
        self.backend=BackendBridge()
        self.backend._tool_sync_timer.stop()
        self.controller=self.backend._ensure_tool_controller()
        movie=Path(self.folder.name)/'film.mkv';movie.write_bytes(b'fixture')
        self.controller._batch_add_paths([str(movie)])
        self.plan=self.controller.batch_plans[str(movie)]
        self.controller.mux_progress=MuxProgressTracker()
        self.state=self.controller.mux_progress
        self.state.update('film.mkv：mkvmerge 封装开始：主输入文件1GiB。')
        self.state.update('film.mkv：mkvmerge 封装进度 100%；已用1秒。')
        self.state.update('film.mkv：正式输出已就绪：C:/ExampleMovies/film.mkv')
        self.controller.batch_progress.setValue(100)
        self.controller.batch_progress.setFormat('%p% · 已完成')

    def tearDown(self):
        self.controller.batch_running=False
        self.controller.deleteLater()
        self.backend.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
        self.app.processEvents()

    def test_clear_notifies_real_qml_and_hides_completed_bar_and_filename(self):
        engine=QQmlApplicationEngine();license_bridge=LicenseBridge();license_bridge._valid=True
        ctx=engine.rootContext()
        for name,value in [('backend',self.backend),('licenseBridge',license_bridge),
                           ('uiFontFamily','Arial'),('appVersion','test')]:ctx.setContextProperty(name,value)
        engine.load(QUrl.fromLocalFile(str(ROOT/'qml_frontend/Main.qml')))
        self.assertTrue(engine.rootObjects());window=engine.rootObjects()[0]
        area=window.findChild(QQuickItem,'muxProgressArea')
        bar=window.findChild(QQuickItem,'muxProgressBar')
        self.app.processEvents()
        self.assertEqual(area.property('opacity'),1)
        self.assertEqual(bar.property('value'),100)
        self.assertIn('film.mkv',self.backend.muxStatus)
        notifications=QSignalSpy(self.backend.toolStateChanged)
        self.backend.batchClear();self.app.processEvents()
        self.assertGreater(notifications.count(),0)
        self.assertEqual(area.property('opacity'),0)
        self.assertEqual(bar.property('value'),0)
        self.assertFalse(self.backend.muxVisible)
        self.assertEqual(self.backend.muxStatus,'')
        self.assertEqual(self.controller.batch_paths,[])
        self.assertEqual(self.controller.batch_plans,{})
        self.assertEqual(self.controller.batch_log.toPlainText(),'')
        self.assertEqual(self.backend.batchProgress,0)
        self.assertIn('等待分析',self.backend.batchStatus)
        window.close();engine.deleteLater();license_bridge.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)

    def test_clear_removes_failed_states_and_late_percent_cannot_recreate_bar(self):
        self.state.update('other.mkv：mkvmerge 封装开始：主输入文件1GiB。')
        self.state.update('other.mkv：处理失败：磁盘不足')
        self.backend.batchClear()
        self.state.update('film.mkv：mkvmerge 封装进度 100%；已用1秒。')
        self.state.update('film.mkv：mkvmerge 封装完成：耗时1秒。')
        self.state.update('film.mkv：正式输出已就绪：C:/ExampleMovies/film.mkv')
        self.assertFalse(self.backend.muxVisible)
        self.assertEqual(self.state.states,{})
        self.backend.batchClear()  # idempotent
        self.assertEqual(self.backend.muxStatus,'')

    def test_running_batch_or_single_movie_refuses_clear_and_keeps_progress(self):
        for field in ('batch_running','status','task_state'):
            with self.subTest(field=field):
                obj=self.controller if field=='batch_running' else self.plan
                old=getattr(obj,field);setattr(obj,field,True if field=='batch_running' else 'processing')
                self.backend.batchClear()
                self.assertEqual(len(self.controller.batch_paths),1)
                self.assertEqual(self.controller.batch_progress.value(),100)
                self.assertTrue(self.backend.muxVisible)
                self.assertIn('film.mkv',self.backend.muxStatus)
                setattr(obj,field,old)

    def test_clear_before_any_mux_does_not_require_tracker(self):
        del self.controller.mux_progress
        self.backend.batchClear()
        self.assertFalse(self.backend.muxVisible)
        self.assertEqual(self.controller.batch_paths,[])


if __name__=='__main__':unittest.main()
