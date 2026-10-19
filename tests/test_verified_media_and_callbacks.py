import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import batch_core
import qt_app
import smart_subtitles
from profile_model import BatchPlan, PreferenceProfile
from subtitle_identity_guard import media_signature


class VerifiedMediaAndCallbacksTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.video = self.root / "Movie.mkv"
        self.video.write_bytes(b"movie")
        self.subtitle = self.root / "confirmed.srt"
        self.subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello.\n", encoding="utf-8")
        self.path = str(self.video)
        self.plan = BatchPlan(self.path, 1, status="ready", external_subtitle=str(self.subtitle),
                              external_subtitle_verified=True,
                              external_subtitle_content_hash=batch_core.hashlib.sha256(self.subtitle.read_bytes()).hexdigest(),
                              external_subtitle_media_signature=media_signature(self.path))
        self.window = SimpleNamespace(batch_plans={self.path: self.plan},
                                      _batch_log_line=Mock(), _render_batch_table=Mock())

    def test_removed_or_readded_plan_does_not_receive_old_callback(self):
        request = qt_app._begin_subtitle_request(self.path, self.plan)
        delivered = Mock()
        self.window.batch_plans.clear()
        qt_app._deliver_subtitle_result(self.window, self.path, request, delivered, "old")
        self.window.batch_plans[self.path] = BatchPlan(self.path, 1)
        qt_app._deliver_subtitle_result(self.window, self.path, request, delivered, "old")
        delivered.assert_not_called()

    def test_new_request_supersedes_old_but_unchanged_current_request_succeeds(self):
        old = qt_app._begin_subtitle_request(self.path, self.plan)
        current = qt_app._begin_subtitle_request(self.path, self.plan)
        delivered = Mock()
        qt_app._deliver_subtitle_result(self.window, self.path, old, delivered, "old")
        qt_app._deliver_subtitle_result(self.window, self.path, current, delivered, "new")
        delivered.assert_called_once_with("new")

    def test_replaced_video_invalidates_pending_callback(self):
        request = qt_app._begin_subtitle_request(self.path, self.plan)
        self.video.write_bytes(b"replacement")
        delivered = Mock()
        qt_app._deliver_subtitle_result(self.window, self.path, request, delivered)
        delivered.assert_not_called()
        self.assertFalse(self.plan.external_subtitle_verified)
        self.assertEqual(self.plan.status, "review")

    def test_clear_is_blocked_during_manual_preflight(self):
        self.plan.status = "processing"
        self.window.batch_running = False
        qt_app.ProMaxQt._batch_clear(self.window)
        self.assertIn(self.path, self.window.batch_plans)

    def test_qt_adds_same_stem_containers_with_distinct_output_paths(self):
        other = self.video.with_suffix(".mp4")
        other.write_bytes(b"other movie")
        window = SimpleNamespace(batch_paths=[], batch_plans={},
                                 batch_default=SimpleNamespace(currentIndex=lambda: 0),
                                 _render_batch_table=Mock(), _batch_log_line=Mock())
        qt_app.ProMaxQt._batch_add_paths(window, [self.path])
        qt_app.ProMaxQt._batch_add_paths(window, [str(other)])
        outputs = {Path(plan.output_path).name for plan in window.batch_plans.values()}
        self.assertEqual(outputs, {"Movie.mkv.SF.mkv", "Movie.mp4.SF.mkv"})

    def test_replaced_video_cannot_use_manual_verification(self):
        self.video.write_bytes(b"replacement")
        with patch.object(batch_core.pro_core, "process_pro") as process:
            with self.assertRaisesRegex(RuntimeError, "影片文件已变化"):
                batch_core.process_plan(self.plan, PreferenceProfile(1, "test"), lambda _: None, None)
        process.assert_not_called()

    def test_changed_preflight_subtitle_cannot_reuse_verified_timeline(self):
        self.subtitle.write_text("changed dialogue and timeline", encoding="utf-8")
        with patch.object(batch_core.pro_core, "process_pro") as process:
            with self.assertRaisesRegex(RuntimeError, "字幕文件已变化"):
                batch_core.process_plan(self.plan, PreferenceProfile(1, "test"), lambda _: None, None)
        process.assert_not_called()

    def test_subtitle_changed_while_callback_queued_is_rejected(self):
        stamped = qt_app._stamp_subtitle_result((self.subtitle, "passed"))
        self.subtitle.write_text("changed while queued", encoding="utf-8")
        self.window._batch_manual_review_failed = Mock()
        self.window._analyze_paths = Mock()
        qt_app.ProMaxQt._batch_online_verified(self.window, self.path, SimpleNamespace(language="en"), stamped)
        self.window._batch_manual_review_failed.assert_called_once()
        self.window._analyze_paths.assert_not_called()

    def test_unchanged_preflight_subtitle_binds_content_and_media(self):
        stamped = qt_app._stamp_subtitle_result((self.subtitle, "passed"))
        self.window._analyze_paths = Mock()
        qt_app.ProMaxQt._batch_online_verified(self.window, self.path, SimpleNamespace(language="en"), stamped)
        self.assertEqual(self.plan.external_subtitle_content_hash, stamped[2])
        self.assertEqual(self.plan.external_subtitle_media_signature, media_signature(self.path))
        self.window._analyze_paths.assert_called_once_with([self.path])

    def test_timing_only_download_must_verify_seal(self):
        self.plan.source_mode = "none"
        self.plan.external_subtitle_provider = "OpenSubtitles"
        with patch.object(smart_subtitles, "verify_before_processing", side_effect=RuntimeError("invalid seal")) as verify:
            with self.assertRaisesRegex(RuntimeError, "invalid seal"):
                batch_core.process_plan(self.plan, PreferenceProfile(1, "test"), lambda _: None, None)
        verify.assert_called_once()

    def test_reanalysis_invalidates_manual_verification_for_replaced_file(self):
        signature = media_signature(self.path)
        self.video.write_bytes(b"replacement")
        with patch.object(batch_core, "_inspect_media_cached", return_value=({}, False)):
            plan = batch_core.analyze_video(self.path, PreferenceProfile(1, "test"),
                                           str(self.subtitle), "en", True,
                                           external_subtitle_media_signature=signature)
        self.assertFalse(plan.external_subtitle_verified)


if __name__ == "__main__":
    unittest.main()
