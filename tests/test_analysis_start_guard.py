"""Use real QML press/release events across asynchronous analysis completion."""
import unittest
from unittest.mock import patch
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest
from profile_model import BatchPlan
from tests import test_qml_paired_context_menu as menu_scene


class AnalysisStartGuardTests(unittest.TestCase):
    def setUp(self):
        self.scene = menu_scene.QmlPairedContextMenuTests()
        self.scene.setUpClass()
        self.scene.setUp()
        self.addCleanup(self.scene.tearDown)
        for name in ('controller', 'backend', 'window', 'app', 'start', 'movies', 'click', 'refresh'):
            setattr(self, name, getattr(self.scene, name))

    def test_busy_press_cannot_become_a_start_when_analysis_finishes(self):
        controller = self.controller
        button = self.window.findChild(QQuickItem, 'batchStartButton')
        analyze = self.window.findChild(QQuickItem, 'batchAnalyzeButton')
        pending = {}
        def defer(_task, **callbacks): pending.update(callbacks)
        with patch.object(controller, '_run_worker', side_effect=defer):
            self.click(analyze)
        self.assertTrue(controller.batch_running)
        self.assertFalse(button.isEnabled())
        self.assertFalse(analyze.isEnabled())
        position = button.mapToScene(QPointF(button.width()/2, button.height()/2))
        point = QPoint(round(position.x()), round(position.y()))
        QTest.mousePress(self.window, Qt.LeftButton, Qt.NoModifier, point)
        pending['result']([BatchPlan(str(p), 1, status='ready') for p in self.movies])
        pending['finished']()
        self.refresh()
        self.assertTrue(button.isEnabled())
        QTest.mouseRelease(self.window, Qt.LeftButton, Qt.NoModifier, point)
        self.app.processEvents()
        self.start.assert_not_called()
        self.click(button)
        self.start.assert_called_once_with()
        self.assertIn('处理启动请求：开始处理按钮', controller.batch_log.toPlainText())

    def test_backend_busy_request_is_dropped_without_later_continuation(self):
        self.controller.batch_running = True
        self.backend.batchStart()
        self.start.assert_not_called()
        self.controller._batch_idle()
        self.refresh()
        self.app.processEvents()
        self.start.assert_not_called()
        self.assertIn('本次请求不排队', self.controller.batch_log.toPlainText())
