"""Storage stalls must not stall the GUI or continuously replace its rows."""
import threading
import time
import unittest
from unittest.mock import patch

import test_qml_paired_context_menu as menu_scene
from PySide6.QtCore import QTimer
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest
import shiboken6


class QmlRefreshPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.scene = menu_scene.QmlPairedContextMenuTests()
        self.scene.setUpClass()
        self.scene.setUp()
        self.addCleanup(self.scene.tearDown)
        self.backend = self.scene.backend
        self.controller = self.scene.controller

    def test_idle_ticks_preserve_actual_delegates_and_open_menu(self):
        before = [shiboken6.getCppPointer(row)[0] for row in self.scene.rows()]
        menu = self.scene.open_menu()
        changes = []
        self.backend.batchItemsChanged.connect(lambda: changes.append(True))
        with patch("translation_remux.available") as probe:
            for _ in range(12):
                self.backend._sync_tool_state()
                self.scene.app.processEvents()
            probe.assert_not_called()
        self.assertFalse(changes)
        self.assertEqual(before, [shiboken6.getCppPointer(row)[0] for row in self.scene.rows()])
        self.assertTrue(menu.property("opened"))

    def test_blocked_storage_leaves_progress_and_gui_heartbeat_responsive(self):
        started, release = threading.Event(), threading.Event()
        worker_threads = []
        def probe(_output):
            worker_threads.append(threading.get_ident())
            started.set()
            release.wait(3)
            return False
        pulses = []
        timer = QTimer()
        timer.timeout.connect(lambda: pulses.append(time.monotonic()))
        timer.start(10)
        try:
            with patch("translation_remux.available", side_effect=probe):
                self.backend._batch_probe_next_at = 0
                self.backend._sync_tool_state()
                self.assertTrue(started.wait(0.5))
                self.controller.batch_progress.setValue(57)
                plan = self.controller.batch_plans[str(self.scene.movies[0])]
                plan.task_state = "processing"
                self.backend._sync_tool_state()
                QTest.qWait(120)
                self.assertGreaterEqual(len(pulses), 5)
                self.assertEqual(self.backend.batchProgress, 57)
                self.assertEqual(self.scene.rows()[0].property("modelData")["taskState"], "processing")
                self.assertTrue(all(thread != threading.get_ident() for thread in worker_threads))
                release.set()
                self.scene.wait_for_availability()
        finally:
            release.set()
            timer.stop()

    def test_disappearing_subtitle_disables_listening_after_background_refresh(self):
        movie = str(self.scene.movies[0])
        self.assertTrue(self.backend.batchItems[0]["canListenSubtitle"])
        self.scene.subtitles[0].unlink()
        self.scene.refresh()
        self.assertFalse(self.backend.batchItems[0]["canListenSubtitle"])
        self.assertEqual(self.controller.batch_plans[movie].external_subtitle, str(self.scene.subtitles[0]))

    def test_translation_receipt_availability_updates_without_changing_movie_plan(self):
        output = self.controller.batch_plans[str(self.scene.movies[0])].output_path
        self.assertFalse(self.backend.batchItems[0]["canReviewTranslation"])
        with patch("translation_remux.available", side_effect=lambda path: path == output):
            self.scene.refresh()
            self.assertTrue(self.backend.batchItems[0]["canReviewTranslation"])
            self.assertFalse(self.backend.batchItems[1]["canReviewTranslation"])
        self.assertEqual(self.controller.batch_plans[str(self.scene.movies[0])].output_path, output)

    def test_many_ticks_do_not_queue_more_workers_behind_slow_storage(self):
        started, release = threading.Event(), threading.Event()
        calls = []
        def probe(_output):
            calls.append(True)
            started.set()
            release.wait(3)
            return False
        try:
            with patch("translation_remux.available", side_effect=probe):
                self.backend._batch_probe_next_at = 0
                self.backend._sync_tool_state()
                self.assertTrue(started.wait(0.5))
                for _ in range(40):
                    self.backend._sync_tool_state()
                    self.scene.app.processEvents()
                self.assertEqual(len(calls), 1)
                release.set()
                self.scene.wait_for_availability()
        finally:
            release.set()


if __name__ == "__main__":
    unittest.main()
