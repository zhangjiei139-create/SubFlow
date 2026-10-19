"""Real Qt/QML controller and dialog; media decoding/playback alone are mocked.

These are simulated confirmations, not evidence that a user listened to a film.
All source files, settings and review output live under TemporaryDirectory.
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication, QEvent, QMetaObject, QObject, QTimer, QUrl
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

import manual_review_state as state
import manual_timeline
import qt_app
import subtitle_tool_core as core
from manual_review_dialog import ManualSubtitleReviewDialog
from profile_model import BatchPlan, PreferenceProfile
from qt_workers import FunctionWorker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "qml_frontend"))
from bridge import BackendBridge
from main import LicenseBridge


class Player:
    def __init__(self, *args): self.ms = 0
    def position(self): return self.ms
    def duration(self): return 60000
    def is_playing(self): return False
    def play(self): pass
    def pause(self): pass
    def set_position(self, value): self.ms = value
    def close(self): pass


class ManualAlignmentEntryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.movie = self.work / "movie.mkv"
        self.movie.write_bytes(b"fixture movie, never decoded")
        self.path = str(self.movie)
        self.source = self.work / "original.srt"
        events = [core.SubtitleEvent(core.srt_time_from_milliseconds(int((r * 1000 + s) * 1000)),
                  core.srt_time_from_milliseconds(int((r * 1000 + s + 3) * 1000)), f"Dialogue {r}-{s}")
                  for r in range(3) for s in (1, 11, 20)]
        core.write_srt(self.source, events, {i: e.text for i, e in enumerate(events, 1)})
        for target, kwargs in (
            ("qt_app.ProMaxQt._detect_hardware", {}),
            ("qt_app.load_profiles", {"return_value": [PreferenceProfile(i, "test") for i in range(1, 5)]}),
            ("qt_app.save_profiles", {}),
            ("qt_app.pro_core.append_subtitle_diagnostic_log", {}),
            ("qt_app.QMessageBox.warning", {}),
            ("qt_app.QMessageBox.information", {}),
            ("manual_review_dialog.WindowsWavPlayer", {"new": Player}),
            ("bridge.BackendBridge._detect_hardware", {}),
            ("bridge.online_subtitles.load_settings", {"return_value": {}}),
        ):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.controller = qt_app.ProMaxQt()
        self.addCleanup(self.controller.close)
        self.controller._batch_add_paths([self.path])
        self.plan = self.controller.batch_plans[self.path]
        self.plan.status = "ready"
        self.plan.external_language = "en"
        self.plan.external_subtitle = str(self.source)
        state.capture_origin(self.plan, self.source, "en")
        shifted = self.work / "auto-shifted.srt"
        manual_timeline.apply_to_srt(self.source, shifted, [30, 1030, 2030], [4.71] * 3)
        self.plan.external_subtitle = str(shifted)
        self.plan.external_subtitle_verified = True
        self.plan.external_subtitle_content_hash = state.sha256(shifted)
        self.controller._analyze_paths = Mock(side_effect=self.mark_ready)
        self.controller._run_worker = self.run_worker
        self.prepared = []
        self.opened = []
        self.failures = []
        prepare = patch("qt_app.manual_review_prepare.prepare_review", side_effect=self.prepare)
        self.prepare_mock = prepare.start()
        self.addCleanup(prepare.stop)

    def mark_ready(self, paths):
        for path in paths: self.controller.batch_plans[path].status = "ready"

    @staticmethod
    def run_worker(function, **callbacks):
        worker = FunctionWorker(function)
        for name, callback in callbacks.items():
            if callback is not None: getattr(worker.signals, name).connect(callback)
        worker.run()

    def prepare(self, movie, source, work, audio_id, log, **kwargs):
        self.prepared.append(Path(source))
        work = Path(work)
        work.mkdir(parents=True)
        normalized = work / "manual-source.srt"
        normalized.write_bytes(Path(source).read_bytes())
        wav = work / "short.wav"
        wav.write_bytes(b"player is mocked; no media process")
        clips = []
        for region in (1, 2, 3):
            start = (region - 1) * 1000
            clips.append(dict(region=region, movie_start=start, subtitle_time=start + 30,
                audio=str(wav), slow_audio=str(wav), vad=[[1, 4], [11, 14], [20, 23]], initial_offset=0,
                rows=[dict(start=start + s, end=start + s + 3, text=f"Dialogue {region}-{s}", zh="")
                      for s in (1, 11, 20)]))
        return dict(source=str(source), source_sha256=state.sha256(source),
                    normalized_sha256=state.sha256(normalized), review_dir=str(work), cue_count=9, clips=clips)

    def schedule_dialog(self, offsets=None, confirmed_regions=3, change_source=False):
        deadline=time.monotonic()+2
        def act():
            dialog = next((w for w in QApplication.topLevelWidgets()
                           if isinstance(w, ManualSubtitleReviewDialog) and w.isVisible()), None)
            if dialog is None:
                # QML modal actions now open on the next event-loop turn.
                # Wait for the real window instead of assuming a zero timer
                # registered before that action will run after its creation.
                if time.monotonic()<deadline:
                    QTimer.singleShot(10,act)
                    return
                self.failures.append("actual manual dialog did not open")
                return
            self.opened.append(dialog.evidence if hasattr(dialog, "evidence") else True)
            try:
                self.assertFalse(self.plan.external_subtitle_verified)
                self.assertTrue(self.plan.manual_review_required)
                self.assertEqual([page.spin.value() for page in dialog.pages], [0.0] * 3)
                if offsets is None:
                    dialog.reject()
                    return
                for page, offset in zip(dialog.pages[:confirmed_regions], offsets):
                    page.spin.setValue(offset)
                    page.toggle_confirmation()
                self.assertEqual(dialog.confirm_button.isEnabled(), confirmed_regions == 3)
                if change_source: self.prepared[-1].write_text("changed", encoding="utf-8")
                if confirmed_regions == 3: dialog.confirm()
                else: dialog.reject()
            except BaseException as exc:
                self.failures.append(str(exc))
                dialog.reject()
        QTimer.singleShot(0, act)

    def force(self, offsets=None, confirmed_regions=3, change_source=False):
        self.schedule_dialog(offsets, confirmed_regions, change_source)
        self.controller._force_batch_manual_review(self.path)
        self.assertFalse(self.failures, self.failures)

    def test_real_main_qml_action_opens_verified_candidate_and_cancel_revokes(self):
        backend = BackendBridge()
        backend._tool_controller = self.controller
        license_bridge = LicenseBridge()
        license_bridge._valid = True
        engine = QQmlApplicationEngine()
        window = None
        try:
            for key, value in dict(backend=backend, licenseBridge=license_bridge,
                                   uiFontFamily="Arial", appVersion="test").items():
                engine.rootContext().setContextProperty(key, value)
            engine.load(QUrl.fromLocalFile(str(ROOT / "qml_frontend" / "Main.qml")))
            self.assertTrue(engine.rootObjects())
            window = engine.rootObjects()[0]
            backend.toolStateChanged.emit()
            self.app.processEvents()
            # File availability is deliberately probed off the GUI thread.
            # One event-loop turn does not guarantee that its queued result
            # has arrived. Wait for that condition before reading new delegates.
            deadline = time.monotonic() + 3
            while backend._batch_probe_running and time.monotonic() < deadline:
                QTest.qWait(10)
            self.assertFalse(backend._batch_probe_running)
            self.app.processEvents()
            batch_list = window.findChild(QQuickItem, "batchList")
            rows = [item for item in batch_list.childItems()[0].childItems() if item.objectName() == "movieRow"]
            self.assertEqual(len(rows), 1)
            action = rows[0].findChild(QObject, "contextAlignSubtitle")
            self.assertIsNotNone(action)
            self.assertTrue(action.property("enabled"))
            self.schedule_dialog()
            self.assertTrue(QMetaObject.invokeMethod(action, "triggered"))
            self.assertEqual(len(self.opened),0)
            self.app.processEvents()
            self.assertEqual(len(self.opened), 1)
            self.assertFalse(self.failures, self.failures)
            self.assertFalse(self.plan.external_subtitle_verified)
            self.assertTrue(self.plan.manual_review_required)
            self.assertEqual(self.plan.manual_confirmation_hash, "")
        finally:
            backend.shutdown()
            if window is not None:
                window.close()
            engine.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.app.processEvents()
            backend.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.app.processEvents()

    def test_three_confirmations_use_raw_baseline_and_reopen_does_not_stack(self):
        self.force([0, 0, 0])
        self.assertEqual(core.parse_subtitle(Path(self.plan.external_subtitle)), core.parse_subtitle(self.source))
        self.assertTrue(self.plan.external_subtitle_verified)
        self.assertFalse(self.plan.manual_review_required)
        self.force([1, 1, 1])
        expected = self.work / "expected.srt"
        manual_timeline.apply_to_srt(self.source, expected, [30, 1030, 2030], [1] * 3)
        self.assertEqual(Path(self.plan.external_subtitle).read_bytes(), expected.read_bytes())
        self.assertEqual(self.prepared[0], self.prepared[1])

    def test_partial_and_prepare_failure_never_verify(self):
        self.force([0, 0, 0], confirmed_regions=2)
        self.assertFalse(self.plan.external_subtitle_verified)
        self.assertTrue(self.plan.manual_review_required)
        self.prepare_mock.side_effect = RuntimeError("decode failed")
        self.controller._force_batch_manual_review(self.path)
        self.assertFalse(self.plan.external_subtitle_verified)
        self.assertTrue(self.plan.manual_review_required)

    def test_modified_confirmed_output_and_source_cannot_be_reused(self):
        self.force([0, 0, 0])
        Path(self.plan.external_subtitle).write_text("changed", encoding="utf-8")
        self.controller._force_batch_manual_review(self.path)
        self.assertEqual(len(self.prepared), 1)
        self.assertFalse(self.plan.external_subtitle_verified)
        self.assertTrue(self.plan.manual_review_required)

    def test_source_changed_during_dialog_rejects_confirmation(self):
        self.force([0, 0, 0], change_source=True)
        self.assertFalse(self.plan.external_subtitle_verified)
        self.assertTrue(self.plan.manual_review_required)

    def test_late_automatic_callback_cannot_restore_cancelled_review(self):
        request = qt_app._begin_subtitle_request(self.path, self.plan)
        self.force()
        callback = Mock()
        qt_app._deliver_subtitle_result(self.controller, self.path, request, callback)
        callback.assert_not_called()
        self.assertFalse(self.plan.external_subtitle_verified)

    def test_busy_blocks_subtitle_edits_and_manual_review_blocks_processing(self):
        self.assertEqual(self.plan.task_state, "waiting")
        self.assertTrue(state.subtitle_editable(self.controller, self.plan))
        for running, task_state in ((True, "waiting"), (True, "idle"), (False, "processing")):
            self.controller.batch_running = running
            self.plan.task_state = task_state
            self.controller._force_batch_manual_review(self.path)
            self.controller._import_batch_subtitle(self.path, str(self.source))
            self.assertFalse(state.subtitle_editable(self.controller, self.plan))
        self.prepare_mock.assert_not_called()
        self.controller.batch_running = False
        self.plan.task_state = "idle"
        self.plan.manual_review_required = True
        self.controller._run_worker = Mock()
        self.controller._batch_start_paths([self.path])
        self.controller._run_worker.assert_not_called()

    def test_reanalysis_preserves_baseline_and_required_state(self):
        self.controller._batch_manual_review_cancelled(self.path)
        old_origin = dict(self.plan.manual_review_origin)
        with patch("qt_app.batch_core.analyze_video", return_value=BatchPlan(self.path, 1, status="ready")):
            qt_app.ProMaxQt._analyze_paths(self.controller, [self.path])
        rebuilt = self.controller.batch_plans[self.path]
        self.assertEqual(rebuilt.manual_review_origin, old_origin)
        self.assertTrue(rebuilt.manual_review_required)
        self.assertFalse(rebuilt.external_subtitle_verified)
        self.assertEqual(rebuilt.status, "review")

    def test_automatic_success_keeps_no_dialog_and_new_selection_clears_required(self):
        self.plan.manual_review_required = True
        candidate = SimpleNamespace(language="en", feature_title="movie", release="fixture",
                                    recommendation="selected", match_reason="fixture")
        with patch("qt_app.pro_core.prepare_shared_subtitle_content_audio", return_value=None), patch(
            "qt_app.pro_core.preflight_online_subtitle", return_value=(self.source, "verified")):
            self.controller._batch_subtitle_selected(self.path, str(self.source), candidate, "本地导入字幕")
        self.assertTrue(self.plan.external_subtitle_verified)
        self.assertFalse(self.plan.manual_review_required)
        self.prepare_mock.assert_not_called()

    def test_preflight_failure_button_opens_existing_three_region_review(self):
        def failure_choice(box):
            next(button for button in box.buttons() if button.text() == "打开人工时间轴").click()
            return 0
        self.schedule_dialog([0, 0, 0], confirmed_regions=2)
        with patch.object(QMessageBox, "exec", failure_choice):
            self.controller._batch_online_verify_failed(self.path, str(self.source),
                SimpleNamespace(language="en"), "evidence insufficient")
        self.assertEqual(len(self.opened), 1)
        self.assertFalse(self.plan.external_subtitle_verified)
        self.assertTrue(self.plan.manual_review_required)

    def test_saved_confirmation_recovers_baseline_after_plan_rebuild(self):
        self.force([0, 0, 0])
        self.plan.manual_review_origin = {}
        baseline = state.resolve_origin(self.plan)
        self.assertEqual(baseline.read_bytes(), self.source.read_bytes())
        self.assertNotIn("api", json.dumps(self.plan.manual_review_origin))

    def test_rejected_smart_result_releases_only_its_own_busy_state(self):
        request = qt_app._begin_subtitle_request(self.path, self.plan)
        self.controller._smart_subtitle_request = request
        self.controller.batch_running = True
        self.movie.write_bytes(b"changed movie")
        callback = Mock()
        qt_app._deliver_smart_subtitle_result(self.controller, self.path, request, callback)
        callback.assert_not_called()
        self.assertFalse(self.controller.batch_running)
        # An old response must not clear a newer worker's ownership.
        self.controller._smart_subtitle_request = object()
        self.controller.batch_running = True
        qt_app._deliver_smart_subtitle_result(self.controller, self.path, request, callback)
        self.assertTrue(self.controller.batch_running)
        self.controller.batch_running = False

    def test_baseline_cannot_be_used_for_another_movie_with_same_stat(self):
        another = self.work / "another.mkv"
        another.write_bytes(self.movie.read_bytes())
        os.utime(another, ns=(self.movie.stat().st_atime_ns, self.movie.stat().st_mtime_ns))
        self.plan.path = str(another)
        with self.assertRaisesRegex(ValueError, "影片已变化"):
            state.resolve_origin(self.plan)

    def test_old_automatic_output_recovers_only_hash_verified_unshifted_source(self):
        self.plan.manual_review_origin = {}
        output = Path(self.plan.external_subtitle)
        verdict = dict(signature=dict(video=self.path, subtitle_sha256=state.sha256(self.source),
                                      **qt_app.media_signature(self.path)),
                       output_sha256=state.sha256(output), applied_offset=4.71)
        (output.parent / "vad-verdict.json").write_text(json.dumps(verdict), encoding="utf-8")
        baseline = state.resolve_origin(self.plan)
        self.assertEqual(baseline.read_bytes(), self.source.read_bytes())
        self.assertEqual(self.plan.manual_review_origin["raw_sha256"], state.sha256(self.source))
        self.plan.manual_review_origin = {}
        self.source.write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "找不到摘要一致"):
            state.resolve_origin(self.plan)

    def test_manual_preparation_blocks_analyze_and_other_movie_subtitle_tasks(self):
        self.plan.status = "processing"
        self.plan.status_label = "准备人工时间轴"
        self.controller._batch_analyze()
        self.controller._analyze_paths.assert_not_called()
        other = BatchPlan(str(self.work / "other.mkv"), 1, status="ready")
        self.assertFalse(state.subtitle_editable(self.controller, other))

    def test_failed_smart_replacement_preserves_manual_guard_and_origin(self):
        self.plan.manual_review_required = True
        self.plan.source_mode = "embedded"
        origin = dict(self.plan.manual_review_origin)
        with patch("qt_app.smart_subtitles.find_verified_english", side_effect=RuntimeError("no candidate")):
            self.controller._start_batch_smart_subtitle(self.path)
        self.assertTrue(self.plan.manual_review_required)
        self.assertEqual(self.plan.manual_review_origin, origin)
        self.assertFalse(self.plan.external_subtitle_verified)
        self.assertFalse(self.controller.batch_running)
        self.controller._run_worker = Mock()
        self.controller._batch_start_paths([self.path])
        self.controller._run_worker.assert_not_called()

    def test_successful_smart_replacement_clears_old_manual_origin_only_on_success(self):
        self.plan.manual_review_required = True
        result = SimpleNamespace(subtitle_path=str(self.source), language="en", report="verified",
                                 provider="test", release="fixture", identity_key="fixture", verification_seal="fixture")
        def search(*args):
            self.assertTrue(self.plan.manual_review_required)
            self.assertTrue(self.plan.manual_review_origin)
            return result
        with patch("qt_app.smart_subtitles.find_verified_english", side_effect=search):
            self.controller._start_batch_smart_subtitle(self.path)
        self.assertFalse(self.plan.manual_review_required)
        self.assertEqual(self.plan.manual_review_origin, {})
        self.assertTrue(self.plan.external_subtitle_verified)


if __name__ == "__main__":
    unittest.main()
