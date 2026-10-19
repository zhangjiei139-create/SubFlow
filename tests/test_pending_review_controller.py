"""Real manual dialog, checked cached inputs and deferred batch continuation.

Media preparation is a fixture; these are simulated confirmations, not
independent listening tests on a movie.
"""
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QMenu
from PySide6.QtGui import QAction
from PySide6.QtCore import QTimer
from manual_review_dialog import ManualSubtitleReviewDialog

import manual_review_state as state
import qt_app
import batch_core
from profile_model import BatchPlan
from tests import test_manual_alignment_entry as fixtures


class PendingReviewControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    setUp = fixtures.ManualAlignmentEntryTest.setUp
    prepare = fixtures.ManualAlignmentEntryTest.prepare
    mark_ready = fixtures.ManualAlignmentEntryTest.mark_ready
    run_worker = staticmethod(fixtures.ManualAlignmentEntryTest.run_worker)
    schedule_dialog = fixtures.ManualAlignmentEntryTest.schedule_dialog

    def pending(self):
        self.plan.manual_review_required = True
        self.plan.manual_review_resume_processing = True
        self.plan.task_state = "failed"
        self.plan.status = "review"
        self.plan.status_label = "处理失败"
        self.controller._batch_start_paths = Mock()

    def open_review(self, offsets=None):
        self.schedule_dialog(offsets)
        self.controller._listen_current_subtitle(self.path)
        self.assertFalse(self.failures, self.failures)

    def idle(self):
        self.controller._batch_idle()
        self.app.processEvents()

    def test_confirm_continues_only_this_film_after_analysis_is_idle(self):
        self.pending()
        self.open_review([.10, .32, .35])
        self.controller._batch_start_paths.assert_not_called()
        self.assertTrue(self.plan.external_subtitle_verified)
        self.assertFalse(self.plan.manual_review_required)
        self.idle()
        self.controller._batch_start_paths.assert_called_once_with([self.path])
        self.assertFalse(self.controller._pending_review_continuations)

    def test_cancel_keeps_pending_and_never_continues(self):
        self.pending()
        self.open_review()
        self.idle()
        self.controller._batch_start_paths.assert_not_called()
        self.assertEqual(state.review_action_states(self.controller, self.plan), {"pending": True, "active": False})
        self.assertEqual(qt_app.ProMaxQt._batch_task_label(self.plan), "处理失败")
        self.assertEqual(qt_app.ProMaxQt._batch_row_color(self.plan).name(), "#fff3da")

    def test_reopening_cancelled_pending_reuses_audio_with_fresh_output_directory(self):
        self.pending()
        self.open_review()
        first = self.plan._manual_audio_cache["evidence"]["review_dir"]
        self.open_review()
        second = self.plan._manual_audio_cache["evidence"]["review_dir"]
        self.assertEqual(self.prepare_mock.call_count, 1)
        self.assertNotEqual(first, second)
        self.assertEqual((Path(first) / "manual-source.srt").read_bytes(), (Path(second) / "manual-source.srt").read_bytes())
        self.assertTrue(Path(first).is_dir())

    def test_tampered_audio_is_not_reused(self):
        self.pending()
        self.open_review()
        cached_audio = Path(self.plan._manual_audio_cache["evidence"]["clips"][0]["audio"])
        cached_audio.write_bytes(b"changed")
        self.open_review()
        self.assertEqual(self.prepare_mock.call_count, 2)

    def test_cancel_retains_switched_window_and_preparation_history(self):
        self.pending()
        def change_window():
            dialog = next(w for w in QApplication.topLevelWidgets()
                          if isinstance(w, ManualSubtitleReviewDialog) and w.isVisible())
            dialog.evidence["clips"][0]["subtitle_time"] = 45
            dialog.evidence["region_attempts"] = {"1": [42]}
            dialog.evidence["preparation_elapsed_seconds"] = 9.5
        QTimer.singleShot(0, change_window)
        self.open_review()
        cached = self.plan._manual_audio_cache["evidence"]
        self.assertEqual(cached["clips"][0]["subtitle_time"], 45)
        self.assertEqual(cached["region_attempts"], {"1": [42]})
        self.assertEqual(cached["preparation_elapsed_seconds"], 9.5)
        self.open_review()
        self.assertEqual(self.prepare_mock.call_count, 1)
        self.assertEqual(self.plan._manual_audio_cache["evidence"]["clips"][0]["subtitle_time"], 45)

    def test_changed_audio_track_is_not_reused(self):
        self.pending()
        self.open_review()
        self.plan.audio_ids = [9]
        self.open_review()
        self.assertEqual(self.prepare_mock.call_count, 2)

    def test_pending_uses_actual_automatic_audio_not_first_output_audio(self):
        self.pending()
        self.plan.audio_ids = [1, 2]
        self.plan.pending_review_audio_id = 2
        self.open_review()
        self.assertEqual(self.prepare_mock.call_args.args[3], 2)
        self.assertEqual(self.plan._manual_audio_cache["audio_id"], 2)
        self.open_review()
        self.assertEqual(self.prepare_mock.call_count, 1)

    def test_batch_handoff_persists_actual_audio_for_controller(self):
        self.plan.audio_ids = [1, 2]
        candidate = SimpleNamespace(subtitle_path=str(self.source), language="en")
        pending = SimpleNamespace(candidates=(candidate,), context={"validation_audio_id": 2}, manifest_path="saved.json")
        with patch("smart_subtitles.validate_pending_candidate"):
            result = batch_core._retain_pending_subtitle_review(self.plan, pending)
        self.assertIs(result.plan, self.plan)
        self.assertTrue(self.plan.manual_review_required)
        self.assertEqual(self.plan.pending_review_audio_id, 2)
        self.open_review()
        self.assertEqual(self.prepare_mock.call_args.args[3], 2)

    def test_changed_confirmed_file_cannot_continue(self):
        self.pending()
        self.open_review([0, 0, 0])
        Path(self.plan.external_subtitle).write_bytes(b"changed output")
        self.idle()
        self.controller._batch_start_paths.assert_not_called()

    def test_changed_movie_cannot_continue(self):
        self.pending()
        self.open_review([0, 0, 0])
        self.movie.write_bytes(b"different movie")
        self.idle()
        self.controller._batch_start_paths.assert_not_called()

    def test_no_source_or_busy_disables_both_review_actions(self):
        self.pending()
        self.controller.batch_running = True
        self.assertEqual(state.review_action_states(self.controller, self.plan), {"pending": False, "active": False})
        self.controller.batch_running = False
        self.plan.external_subtitle = str(self.work / "missing.srt")
        self.assertEqual(state.review_action_states(self.controller, self.plan), {"pending": False, "active": False})

    def test_active_review_has_no_automatic_continuation(self):
        self.controller._batch_start_paths = Mock()
        self.assertEqual(state.review_action_states(self.controller, self.plan), {"pending": False, "active": True})
        self.schedule_dialog([0, 0, 0])
        self.controller._force_batch_manual_review(self.path)
        self.idle()
        self.controller._batch_start_paths.assert_not_called()

    def test_native_paired_controls_have_separate_enabled_states(self):
        menu = QMenu()
        self.addCleanup(menu.close)
        chosen = [None]
        left, right = qt_app._paired_menu_actions(menu, ("处理", "停止"), (True, False), chosen,
                                                 ("pending", "active"))
        self.assertEqual(len(menu.actions()), 1)
        self.assertTrue(left.isEnabled())
        self.assertFalse(right.isEnabled())
        left.trigger()
        self.assertIs(chosen[0], left)

    def test_native_pending_menu_disables_processing_until_confirmation(self):
        self.pending()
        table = self.controller.batch_table
        table.selectRow(0)
        position = table.visualItemRect(table.item(0, 0)).center()
        menu = QMenu(table)
        self.addCleanup(menu.close)
        def inspect_menu(_position):
            self.assertFalse(menu.findChild(QAction, "contextProcessSelected").isEnabled())
            self.assertFalse(menu.findChild(QAction, "contextStopSelected").isEnabled())
            self.assertIsNone(menu.findChild(QAction, "contextPendingReview"))
            listen = menu.findChild(QAction, "contextAlignSubtitle")
            self.assertTrue(listen.isEnabled())
            self.assertEqual(listen.text(), "核听当前字幕")
            return None
        with patch("qt_app.QMenu", return_value=menu), patch.object(menu, "exec", side_effect=inspect_menu) as execute:
            self.controller._batch_context_menu(position)
        execute.assert_called_once()

    def test_listenable_failure_counts_once_and_does_not_stop_remaining_batch(self):
        other = self.work / "other.mkv"
        other.write_bytes(b"another fixture movie")
        self.controller._batch_add_paths([str(other)])
        other_plan = self.controller.batch_plans[str(other)]
        other_plan.status = "ready"
        visited = []
        manifest = self.work / "pending-review.json"
        manifest.write_text("{}", encoding="utf-8")

        def process(plan, *args):
            visited.append(plan.path)
            if plan.path == self.path:
                state.clear_trust(plan)
                plan.manual_review_resume_processing = True
                plan.status, plan.status_label = "review", "需要确认"
                raise batch_core.PendingSubtitleReviewError(plan, (), {}, str(manifest))

        with patch("qt_app.batch_core.plan_requires_online_subtitle_service", return_value=False), patch(
                "qt_app.batch_core.enough_space", return_value=(True, "")), patch(
                "qt_app.system_resources.RuntimeGuard") as guard, patch(
                "qt_app.batch_core.process_plan", side_effect=process):
            guard.return_value.can_start_next.return_value = (True, "")
            self.controller._batch_start_paths([self.path, str(other)])
        self.assertEqual(visited, [self.path, str(other)])
        self.assertEqual(self.plan.task_state, "failed")
        self.assertEqual(other_plan.task_state, "completed")
        self.assertFalse(self.controller.batch_running)
        self.assertIn("失败 1", self.controller.batch_progress.format())
        self.assertIn("核听当前字幕", self.controller.batch_progress.format())
        self.assertNotIn("待核听", self.controller.batch_progress.format())
        self.assertEqual(self.controller.batch_progress.value(), 100)
        self.assertEqual(state.review_action_states(self.controller, self.plan), {"pending": True, "active": False})

    def test_reselected_subtitle_with_old_failure_does_not_auto_continue(self):
        self.pending()
        candidate = SimpleNamespace(language="en", feature_title="movie", release="new source",
                                    recommendation="selected", match_reason="local")
        self.controller._run_worker = Mock()
        self.controller._batch_subtitle_selected(self.path, str(self.source), candidate, "本地导入字幕")
        self.assertFalse(self.plan.manual_review_resume_processing)
        self.controller._batch_manual_review_cancelled(self.path)
        self.assertEqual(self.plan.task_state, "failed")
        self.controller._run_worker = self.run_worker
        self.open_review([0, 0, 0])
        self.idle()
        self.controller._batch_start_paths.assert_not_called()
        self.assertTrue(self.plan.external_subtitle_verified)

    def test_reanalysis_keeps_processing_failure_origin_and_orange(self):
        self.pending()
        rebuilt = BatchPlan(self.path, 1, status="ready")
        state.preserve_origin(self.plan, rebuilt)
        self.assertTrue(rebuilt.manual_review_resume_processing)
        self.assertTrue(rebuilt.manual_review_required)
        self.assertEqual(rebuilt.task_state, "failed")
        self.assertEqual(qt_app.ProMaxQt._batch_task_label(rebuilt), "处理失败")
        self.assertEqual(qt_app.ProMaxQt._batch_row_color(rebuilt).name(), "#fff3da")

    def test_analysis_blocked_stays_red_and_burned_stays_blue(self):
        self.plan.status, self.plan.task_state = "blocked", "blocked"
        self.assertEqual(qt_app.ProMaxQt._batch_row_color(self.plan).name(), "#fde9e7")
        self.plan.status, self.plan.task_state = "burned", "failed"
        self.plan.burned_subtitle = True
        self.assertEqual(qt_app.ProMaxQt._batch_row_color(self.plan).name(), "#173f7a")


if __name__ == "__main__":
    unittest.main()
