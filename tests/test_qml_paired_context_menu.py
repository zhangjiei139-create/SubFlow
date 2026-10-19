"""Actual compact-menu clicks, unified listening and runtime row colors.

All movies/subtitles and preferences are temporary. No media tool, service,
translation or real manual preparation runs in this menu-only suite.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPoint, QPointF, Property, Qt, QUrl
from PySide6.QtGui import QAccessible, QAccessibleActionInterface, QColor
from PySide6.QtQml import QQmlApplicationEngine, QQmlExpression
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "qml_frontend"))
from bridge import BackendBridge


class LicenseFixture(QObject):
    @Property(bool, constant=True)
    def valid(self): return True
    @Property(bool, constant=True)
    def busy(self): return False
    @Property(str, constant=True)
    def message(self): return ""
    @Property(str, constant=True)
    def deviceCode(self): return "fixture"


class QmlPairedContextMenuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.patches = [
            patch.object(BackendBridge, "_detect_hardware"),
            patch("qt_app.ProMaxQt._detect_hardware"),
            patch("bridge.online_subtitles.load_settings", return_value={}),
            patch("profile_model.profile_file", return_value=self.folder / "profiles.json"),
        ]
        for p in self.patches: p.start()
        self.backend = BackendBridge()
        self.backend._tool_sync_timer.stop()
        self.controller = self.backend._ensure_tool_controller()
        self.movies = [self.folder / f"movie-{i}.mkv" for i in range(3)]
        self.subtitles = [self.folder / f"source-{i}.srt" for i in range(3)]
        for movie, subtitle in zip(self.movies, self.subtitles):
            movie.write_bytes(b"menu fixture, never decoded")
            subtitle.write_text("1\n00:00:01,000 --> 00:00:03,000\nFixture dialogue.\n", encoding="utf-8")
        self.controller._batch_add_paths([str(p) for p in self.movies])
        for movie, subtitle in zip(self.movies, self.subtitles):
            plan = self.controller.batch_plans[str(movie)]
            plan.status = "ready"
            plan.external_subtitle = str(subtitle)
            plan.external_subtitle_verified = True
        self.active = Mock()
        self.pending = Mock()
        self.start = Mock()
        self.online = Mock()
        self.real_pending_handler = self.controller._open_batch_pending_review
        self.controller._force_batch_manual_review = self.active
        self.controller._open_batch_pending_review = self.pending
        self.controller._batch_start_paths = self.start
        self.controller._open_batch_online_search = self.online
        self.license = LicenseFixture()
        self.engine = QQmlApplicationEngine()
        for key, value in {"backend": self.backend, "licenseBridge": self.license,
                           "uiFontFamily": "Arial", "appVersion": "menu-test"}.items():
            self.engine.rootContext().setContextProperty(key, value)
        self.engine.load(QUrl.fromLocalFile(str(ROOT / "qml_frontend/Main.qml")))
        self.assertTrue(self.engine.rootObjects())
        self.window = self.engine.rootObjects()[0]
        self.app.processEvents()
        self.wait_for_availability()
        self.select(0)

    def tearDown(self):
        self.backend._tool_sync_timer.stop()
        self.window.close()
        self.engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.backend.shutdown()
        self.backend.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        for p in reversed(self.patches): p.stop()
        self.temp.cleanup()

    def rows(self):
        items = self.window.findChild(QQuickItem, "batchList").childItems()[0].childItems()
        return sorted([item for item in items if item.objectName() == "movieRow"],
                      key=lambda item: item.property("index"))

    def click(self, item, button=Qt.MouseButton.LeftButton,
              modifiers=Qt.KeyboardModifier.NoModifier):
        position = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
        self._click_item = item
        self._click_window = item.window()
        QTest.mouseClick(self._click_window, button, modifiers,
                         QPoint(round(position.x()), round(position.y())))
        self.app.processEvents()

    def select(self, index, modifiers=Qt.KeyboardModifier.NoModifier):
        row = self.rows()[index]
        position = row.mapToScene(QPointF(100, row.height() / 2))
        QTest.mouseClick(self.window, Qt.MouseButton.LeftButton, modifiers,
                         QPoint(round(position.x()), round(position.y())))
        self.app.processEvents()

    def open_menu(self, row=0):
        self._menu_row = self.rows()[row]
        self.click(self._menu_row, Qt.MouseButton.RightButton)
        # Qt can replace the Python QQuickWindow wrapper while opening a popup
        # after a model refresh. The engine still owns the same live root.
        self.window = self.engine.rootObjects()[0]
        menu = self._menu_row.findChild(QObject, "movieContextMenu")
        self.assertTrue(menu.property("opened"))
        return menu

    def refresh(self):
        self.backend._batch_probe_next_at = 0
        self.backend.toolStateChanged.emit()
        self.app.processEvents()
        self.wait_for_availability()

    def wait_for_availability(self):
        import time
        deadline = time.monotonic() + 3
        while self.backend._batch_probe_running and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertFalse(self.backend._batch_probe_running)
        self.app.processEvents()

    def open_subtitle_submenu(self, row=0):
        menu = self.open_menu(row)
        expression = QQmlExpression(self.engine.rootContext(), menu, "itemAt(4)")
        value = expression.evaluate()
        self.assertFalse(expression.hasError())
        trigger = value[0] if isinstance(value, tuple) else value
        self.assertIsInstance(trigger, QQuickItem)
        self.click(trigger)
        QTest.qWait(100)
        submenu = menu.findChild(QObject, "contextChooseSubtitle")
        self.assertTrue(submenu.property("opened"))
        return submenu

    def test_batch_output_format_switches_all_pending_paths_and_locks_after_analysis(self):
        combo = self.window.findChild(QQuickItem, "batchOutputFormat")
        self.assertEqual(self.backend.batchOutputFormat, "MKV")
        for plan in self.controller.batch_plans.values():
            plan.status = "pending"
        self.refresh()
        self.assertTrue(combo.property("enabled"))
        combo.forceActiveFocus()
        QTest.keyClick(self.window, Qt.Key.Key_Down)
        self.app.processEvents()
        self.assertEqual(self.backend.batchOutputFormat, "MP4")
        self.assertTrue(all(Path(plan.output_path).suffix == ".mp4"
                            for plan in self.controller.batch_plans.values()))
        for plan in self.controller.batch_plans.values():
            plan.status = "ready"
        self.refresh()
        combo = self.window.findChild(QQuickItem, "batchOutputFormat")
        self.assertFalse(combo.property("enabled"))
        self.backend.batchSetOutputFormat("MKV")
        self.assertEqual(self.backend.batchOutputFormat, "MP4")
        self.backend.batchClear()
        self.app.processEvents()
        self.window = self.engine.rootObjects()[0]
        self.assertFalse(self.backend.batchOutputFormatLocked)
        self.backend.batchSetOutputFormat("MKV")
        self.assertEqual(self.backend.batchOutputFormat, "MKV")

    def test_open_subtitle_submenu_labels_have_space_and_online_click_targets_movie(self):
        submenu = self.open_subtitle_submenu(2)
        for name in ("contextChooseOnlineSubtitle", "contextImportSubtitle"):
            item = submenu.findChild(QQuickItem, name)
            label = item.property("contentItem")
            self.assertTrue(item.property("visible"))
            self.assertTrue(item.property("enabled"))
            self.assertGreaterEqual(label.width(), label.implicitWidth())
            self.assertGreater(item.width(), 80)
        self.click(submenu.findChild(QQuickItem, "contextChooseOnlineSubtitle"))
        self.window = self.engine.rootObjects()[0]
        self.online.assert_called_once_with(str(self.movies[2]))

    def test_open_subtitle_submenu_local_import_opens_folder_for_right_clicked_movie(self):
        submenu = self.open_subtitle_submenu(2)
        dialog = self.window.findChild(QObject, "importSubtitleDialog")
        self.assertFalse(dialog.property("visible"))
        self.click(submenu.findChild(QQuickItem, "contextImportSubtitle"))
        self.window = self.engine.rootObjects()[0]
        dialog = self.window.findChild(QObject, "importSubtitleDialog")
        try:
            self.assertTrue(dialog.property("visible"))
            self.assertEqual(Path(dialog.property("currentFolder").toLocalFile()), self.folder)
            self.online.assert_not_called()
        finally:
            expression = QQmlExpression(self.engine.rootContext(), dialog, "close()")
            expression.evaluate()
            self.assertFalse(expression.hasError())
            self.app.processEvents()
            self.window = self.engine.rootObjects()[0]

    def test_menu_has_seven_rows_and_two_nested_source_choices(self):
        menu = self.open_menu()
        self.assertEqual(menu.property("count"), 7)
        choose = menu.findChild(QObject, "contextChooseSubtitle")
        self.assertEqual(choose.property("title"), "字幕选择")
        self.assertEqual(choose.property("count"), 2)
        self.assertIsNotNone(choose.findChild(QObject, "contextChooseOnlineSubtitle"))
        self.assertIsNotNone(choose.findChild(QObject, "contextImportSubtitle"))
        for row_name, first, second in (
            ("contextProcessStopRow", "contextProcessSelected", "contextStopSelected"),
        ):
            row = menu.findChild(QQuickItem, row_name)
            left = row.findChild(QQuickItem, first)
            right = row.findChild(QQuickItem, second)
            self.assertIsNotNone(left)
            self.assertIsNotNone(right)
            self.assertAlmostEqual(left.mapToScene(QPointF()).y(), right.mapToScene(QPointF()).y())
            self.assertLess(left.mapToScene(QPointF()).x(), right.mapToScene(QPointF()).x())
        listen = menu.findChild(QQuickItem, "contextAlignSubtitle")
        self.assertEqual(listen.property("text"), "核听当前字幕")
        self.assertIsNone(menu.findChild(QObject, "contextPendingReview"))
        self.assertEqual(menu.findChild(QObject, "contextRemoveSelected").property("text"), "移除")

    def test_processing_half_does_not_activate_disabled_stop_half(self):
        menu = self.open_menu()
        process = menu.findChild(QQuickItem, "contextProcessSelected")
        stop = menu.findChild(QQuickItem, "contextStopSelected")
        self.assertTrue(process.property("enabled"))
        self.assertFalse(stop.property("enabled"))
        self.click(stop)
        self.assertFalse(any(event.is_set() for event in self.controller.batch_item_cancel_events.values()))
        self.start.assert_not_called()
        menu = self.open_menu()
        self.click(menu.findChild(QQuickItem, "contextProcessSelected"))
        self.start.assert_called_once_with([str(self.movies[0])])

    def test_stop_half_cancels_only_selected_waiting_and_processing_movies(self):
        self.select(1, Qt.KeyboardModifier.ControlModifier)
        self.controller.batch_running = True
        for index, movie in enumerate(self.movies):
            self.controller.batch_item_cancel_events[str(movie)] = threading.Event()
            self.controller.batch_plans[str(movie)].task_state = "processing" if index == 1 else "waiting"
        self.refresh()
        menu = self.open_menu(2)
        self.assertFalse(menu.findChild(QQuickItem, "contextProcessSelected").property("enabled"))
        stop = menu.findChild(QQuickItem, "contextStopSelected")
        self.assertTrue(stop.property("enabled"))
        self.click(stop)
        self.assertEqual([self.controller.batch_item_cancel_events[str(movie)].is_set()
                          for movie in self.movies], [True, True, False])
        self.start.assert_not_called()

    def test_listen_current_targets_right_clicked_movie_and_routes_active_review(self):
        self.controller.batch_plans[str(self.movies[1])].task_state = "completed"
        self.refresh()
        menu = self.open_menu(1)
        active = menu.findChild(QQuickItem, "contextAlignSubtitle")
        self.assertTrue(active.property("enabled"))
        self.click(active)
        self.active.assert_called_once_with(str(self.movies[1]))
        self.pending.assert_not_called()

    def test_unconfirmed_selection_listens_without_failed_processing_resumption(self):
        plan = self.controller.batch_plans[str(self.movies[0])]
        plan.status = "review"
        plan.task_state = "waiting"
        plan.manual_review_required = True
        self.controller._open_batch_pending_review = self.real_pending_handler
        self.controller._pending_review_continuations = {str(self.movies[0]): object()}
        prepare = Mock()
        self.controller._start_batch_manual_review = prepare
        self.refresh()
        menu = self.open_menu()
        listen = menu.findChild(QQuickItem, "contextAlignSubtitle")
        self.assertTrue(listen.property("enabled"))
        self.click(listen)
        prepare.assert_called_once()
        self.assertNotIn(str(self.movies[0]), self.controller._pending_review_continuations)
        self.start.assert_not_called()

    def test_failed_current_listen_routes_pending_and_missing_subtitle_disables_it(self):
        plan = self.controller.batch_plans[str(self.movies[0])]
        plan.manual_review_required = True
        plan.status = "failed"
        plan.task_state = "failed"
        self.refresh()
        menu = self.open_menu()
        self.assertTrue(menu.findChild(QQuickItem, "contextAlignSubtitle").property("enabled"))
        self.assertFalse(menu.findChild(QQuickItem, "contextProcessSelected").property("enabled"))
        self.assertFalse(menu.findChild(QQuickItem, "contextStopSelected").property("enabled"))
        self.click(menu.findChild(QQuickItem, "contextAlignSubtitle"))
        self.pending.assert_called_once_with(str(self.movies[0]))
        self.active.assert_not_called()
        plan.external_subtitle = str(self.folder / "missing.srt")
        self.refresh()
        menu = self.open_menu()
        item = menu.findChild(QQuickItem, "contextAlignSubtitle")
        self.assertFalse(item.property("enabled"))
        self.click(item)
        self.pending.assert_called_once()
        self.active.assert_not_called()

    def test_listen_current_has_accessible_name_and_keyboard_activation(self):
        menu = self.open_menu()
        active = menu.findChild(QQuickItem, "contextAlignSubtitle")
        interface = QAccessible.queryAccessibleInterface(active)
        self.assertIsNotNone(interface)
        self.assertEqual(interface.text(QAccessible.Text.Name), "核听当前字幕")
        self.assertFalse(interface.state().disabled)
        active.forceActiveFocus()
        QTest.keyClick(active.window(), Qt.Key.Key_Return)
        self.app.processEvents()
        self.active.assert_called_once_with(str(self.movies[0]))
        self.pending.assert_not_called()

        menu = self.open_menu()
        listen = menu.findChild(QQuickItem, "contextAlignSubtitle")
        QAccessible.queryAccessibleInterface(listen).actionInterface().doAction(
            QAccessibleActionInterface.pressAction())
        self.app.processEvents()
        self.assertFalse(menu.property("opened"))
        self.assertEqual(self.active.call_count, 2)

    def test_busy_disables_current_listen_without_dispatch(self):
        self.controller.batch_running = True
        self.refresh()
        menu = self.open_menu()
        listen = menu.findChild(QQuickItem, "contextAlignSubtitle")
        self.assertFalse(listen.property("enabled"))
        self.assertTrue(QAccessible.queryAccessibleInterface(listen).state().disabled)
        self.click(listen)
        self.pending.assert_not_called()
        self.active.assert_not_called()

    def test_runtime_failed_rows_use_controller_orange_despite_analysis_state(self):
        plan = self.controller.batch_plans[str(self.movies[0])]
        for status in ("ready", "review", "failed", "blocked"):
            plan.status = status
            plan.task_state = "failed"
            self.refresh()
            self.assertEqual(self.backend.batchItems[0]["rowColor"], "#fff3da")
            self.assertEqual(self.rows()[0].property("color"), QColor("#fff3da"))

    def test_selected_failed_row_keeps_visible_orange_fill(self):
        plan = self.controller.batch_plans[str(self.movies[0])]
        plan.status = "failed"
        plan.task_state = "failed"
        self.refresh()
        self.select(0)
        grab = self.rows()[0].grabToImage()
        ready = []
        grab.ready.connect(lambda: ready.append(True))
        for _ in range(30):
            if ready:
                break
            QTest.qWait(50)
        self.assertTrue(ready, "The selected failure row did not render")
        # Outside the selection outline and label bounds, verify the actual
        # composited pixel, not just the underlying rectangle's QColor.
        self.assertEqual(grab.image().pixelColor(6, 6), QColor("#fff3da"))

    def test_completed_unconfirmed_blocked_and_burned_colors_match_controller(self):
        plan = self.controller.batch_plans[str(self.movies[0])]
        for status, task in (("ready", "completed"), ("review", "waiting"),
                             ("blocked", "waiting"), ("burned", "waiting")):
            plan.status = status
            plan.task_state = task
            self.refresh()
            expected = self.controller._batch_row_color(plan)
            self.assertEqual(self.rows()[0].property("color"), expected)
            self.assertEqual(self.backend.batchItems[0]["rowColor"], expected.name())

    def test_open_menu_survives_periodic_refresh_and_resumes_after_close(self):
        changes = []
        self.backend.toolStateChanged.connect(lambda: changes.append(True))
        self.backend._tool_sync_timer.start(50)
        menu = self.open_menu()
        QTest.qWait(450)
        self.app.processEvents()
        self.assertTrue(menu.property("opened"))
        active = menu.findChild(QQuickItem, "contextAlignSubtitle")
        self.click(active)
        self.active.assert_called_once_with(str(self.movies[0]))
        count = len(changes)
        QTest.qWait(120)
        self.assertGreater(len(changes), count)


if __name__ == "__main__":
    unittest.main()
