# -*- coding: utf-8 -*-
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import burned_subtitle_detector
import strict_inspection
import subtitle_tool_core as core


class StrictInspectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.media = {
            "container": {"properties": {"duration": 7_200_000_000_000}},
            "tracks": [],
        }
        self.base_tracks = [
            core.Track(0, "video", "HEVC", "und", "", True, False, False, False, False),
            core.Track(1, "audio", "TrueHD", "eng", "", True, False, False, False, False),
        ]
        self.clear = burned_subtitle_detector.DetectionResult(False, 0, 12, "clear")

    @staticmethod
    def _track(track_id: int, language: str, name: str = "", default: bool = False) -> core.Track:
        return core.Track(track_id, "subtitles", "SubRip/SRT", language, name, default, False, True, False, False)

    def _run(self, subtitles: list[core.Track], complete_ids: set[int]) -> dict:
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "movie.mkv"
            video.write_bytes(b"video")

            def inspect_text(_video, track, _duration, _work):
                if track.id in complete_ids:
                    return 1000, 0.95, [], True
                return 10, 0.10, ["subtitle_timeline_incomplete"], False

            with patch.object(core, "inspect_media", return_value=self.media), patch.object(
                core, "inspect_tracks", return_value=self.base_tracks + subtitles
            ), patch.object(
                burned_subtitle_detector, "detect", return_value=self.clear
            ), patch.object(
                strict_inspection, "_inspect_text_track", side_effect=inspect_text
            ):
                return strict_inspection.inspect_video(str(video))

    def test_accepts_exact_complete_text_languages(self) -> None:
        subtitles = [self._track(2, "eng", default=True), self._track(3, "chi", "简中"), self._track(4, "ger")]
        report = self._run(subtitles, {2, 3, 4})
        self.assertEqual(report["status"], "accepted")
        self.assertTrue(report["validation"]["passed"])

    def test_prefers_normal_subtitle_over_sdh_and_requires_cleanup(self) -> None:
        subtitles = [
            self._track(2, "eng", "English SDH"),
            self._track(3, "eng", "English", default=True),
            self._track(4, "chi", "简中"),
            self._track(5, "ger"),
        ]
        report = self._run(subtitles, {2, 3, 4, 5})
        self.assertEqual(report["selected_subtitle_tracks"]["en"], 3)
        self.assertEqual(report["status"], "needs_processing")
        self.assertIn(2, report["forbidden_subtitle_track_ids"])

    def test_existing_subtitles_skip_burned_check(self) -> None:
        subtitles = [self._track(2, "eng", default=True), self._track(3, "chi", "简中"), self._track(4, "ger")]
        failed = burned_subtitle_detector.DetectionResult(False, 0, 0, "components unavailable")
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "movie.mkv"
            video.write_bytes(b"video")
            with patch.object(core, "inspect_media", return_value=self.media), patch.object(
                core, "inspect_tracks", return_value=self.base_tracks + subtitles
            ), patch.object(
                burned_subtitle_detector, "detect", return_value=failed
            ) as detect, patch.object(
                strict_inspection, "_inspect_text_track", return_value=(1000, 0.95, [], True)
            ):
                report = strict_inspection.inspect_video(str(video))
        self.assertEqual(report["status"], "accepted")
        self.assertEqual(report["burned_subtitle_check"]["status"], "burned_not_detected")
        self.assertIn("未启动烧录字幕检查", report["burned_subtitle_check"]["detail"])
        detect.assert_not_called()

    def test_no_subtitle_tracks_run_burned_check(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "movie.mkv"
            video.write_bytes(b"video")
            with patch.object(core, "inspect_media", return_value=self.media), patch.object(
                core, "inspect_tracks", return_value=self.base_tracks
            ), patch.object(
                burned_subtitle_detector, "detect", return_value=self.clear
            ) as detect:
                report = strict_inspection.inspect_video(str(video))
        detect.assert_called_once()
        self.assertEqual(report["status"], "needs_processing")

    def test_requires_english_as_the_only_default_subtitle(self) -> None:
        subtitles = [self._track(2, "eng"), self._track(3, "chi", "简中", default=True), self._track(4, "ger")]
        report = self._run(subtitles, {2, 3, 4})
        self.assertEqual(report["status"], "needs_processing")
        self.assertIn("default_subtitle_must_be_english", report["reason_codes"])


if __name__ == "__main__":
    unittest.main()
