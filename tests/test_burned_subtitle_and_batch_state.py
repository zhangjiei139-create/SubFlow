# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import batch_core
import burned_subtitle_detector
import subtitle_tool_core as core
from profile_model import BatchPlan, PreferenceProfile
from qt_app import ProMaxQt


class BurnedSubtitleDetectorTest(unittest.TestCase):
    def test_single_window_text_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "movie.mkv"
            video.write_bytes(b"video")
            work = root / "work"
            work.mkdir()
            interval = {
                "start": 100.5,
                "end": 101.0,
                "peak": 100.75,
                "peak_score": -20.0,
                "duration": 0.5,
            }
            with patch.object(burned_subtitle_detector, "_read_cache", return_value={}), patch.object(
                burned_subtitle_detector, "_write_cache"
            ), patch.object(
                burned_subtitle_detector, "_duration_seconds", return_value=3600.0
            ), patch.object(
                burned_subtitle_detector, "_available_ocr_languages", return_value="eng"
            ), patch.object(
                burned_subtitle_detector, "_runtime_dir", return_value=work
            ), patch.object(
                burned_subtitle_detector, "_speech_intervals", return_value=([interval], 90.0, True)
            ), patch.object(
                burned_subtitle_detector, "_keyframes_near_window", return_value=([100.0], True)
            ), patch.object(
                burned_subtitle_detector, "_extract_speech_frames", return_value=[work / "frame.png"]
            ), patch.object(
                burned_subtitle_detector,
                "_visual_screen",
                return_value=burned_subtitle_detector.VisualScreen("suspicious", 0.8),
            ), patch.object(
                burned_subtitle_detector, "_ocr_lines", return_value=["Let us in, Dad."]
            ):
                result = burned_subtitle_detector.detect(str(video), {}, ["eng"])

        self.assertTrue(result.detected)
        self.assertEqual(result.evidence_count, 1)
        self.assertEqual(result.status, "detected")

    def test_incomplete_window_is_uncertain_and_not_cached(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "movie.mkv"
            video.write_bytes(b"video")
            work = root / "work"
            work.mkdir()
            with patch.object(burned_subtitle_detector, "_read_cache", return_value={}), patch.object(
                burned_subtitle_detector, "_write_cache"
            ) as write_cache, patch.object(
                burned_subtitle_detector, "_duration_seconds", return_value=3600.0
            ), patch.object(
                burned_subtitle_detector, "_available_ocr_languages", return_value="eng"
            ), patch.object(
                burned_subtitle_detector, "_runtime_dir", return_value=work
            ), patch.object(
                burned_subtitle_detector, "_speech_intervals", return_value=([], 90.0, False)
            ):
                result = burned_subtitle_detector.detect(str(video), {}, ["eng"])

        self.assertFalse(result.detected)
        self.assertEqual(result.status, "uncertain")
        write_cache.assert_not_called()

    def test_cache_identity_survives_a_file_rename(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            first = Path(folder) / "first.mkv"
            first.write_bytes(b"same video bytes")
            first_key = burned_subtitle_detector._cache_key(first)
            second = first.with_name("renamed.mkv")
            first.rename(second)
            self.assertEqual(first_key, burned_subtitle_detector._cache_key(second))

    def test_batch_analysis_blocks_detected_burned_subtitles(self) -> None:
        profile = PreferenceProfile(1, "test", ["AAC"], True, False, ["zh-CN"], "online")
        tracks = [core.Track(0, "audio", "AAC", "eng", "", True, False, False, False, False)]
        detected = burned_subtitle_detector.DetectionResult(True, 3, 8, "检测到烧录字幕")
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "movie.mkv"
            video.write_bytes(b"video")
            with patch.object(batch_core, "_inspect_media_cached", return_value=({}, False)), patch.object(
                batch_core.core, "tracks_from_media", return_value=tracks
            ), patch.object(
                batch_core.burned_subtitle_detector, "detect", return_value=detected
            ):
                plan = batch_core.analyze_video(str(video), profile)

        self.assertEqual(plan.status, "burned")
        self.assertTrue(plan.burned_subtitle)
        self.assertEqual(plan.task_status_label, "不可操作")

    def test_batch_analysis_skips_burned_scan_with_complete_subtitle_track(self) -> None:
        profile = PreferenceProfile(1, "test", subtitle_languages=[])
        subtitle_variants = (
            core.Track(1, "subtitles", "S_TEXT/UTF8", "eng", "", False, False, True, False, False),
            core.Track(1, "subtitles", "HDMV PGS", "chi", "", False, False, False, True, False),
        )
        for subtitle_track in subtitle_variants:
            with self.subTest(codec=subtitle_track.codec):
                tracks = [
                    core.Track(0, "audio", "AAC", "eng", "", True, False, False, False, False),
                    subtitle_track,
                ]
                with tempfile.TemporaryDirectory() as folder:
                    video = Path(folder) / "movie.mkv"
                    video.write_bytes(b"x" * (1024 * 1024 + 1))
                    with patch.object(batch_core, "_inspect_media_cached", return_value=({}, False)), patch.object(
                        batch_core.core, "tracks_from_media", return_value=tracks
                    ), patch.object(batch_core.burned_subtitle_detector, "detect") as detect:
                        plan = batch_core.analyze_video(str(video), profile)

                detect.assert_not_called()
                self.assertEqual(plan.status, "ready")
                self.assertFalse(plan.burned_subtitle)

    def test_batch_analysis_skips_burned_scan_with_forced_subtitle(self) -> None:
        profile = PreferenceProfile(1, "test", subtitle_languages=[])
        tracks = [
            core.Track(0, "audio", "AAC", "eng", "", True, False, False, False, False),
            core.Track(1, "subtitles", "HDMV PGS", "eng", "Forced", False, True, False, True, False),
        ]
        result = burned_subtitle_detector.DetectionResult(
            False, 0, 6, "烧录字幕检测证据不足", status="uncertain"
        )
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "movie.mkv"
            video.write_bytes(b"x" * (1024 * 1024 + 1))
            with patch.object(batch_core, "_inspect_media_cached", return_value=({}, False)), patch.object(
                batch_core.core, "tracks_from_media", return_value=tracks
            ), patch.object(
                batch_core.burned_subtitle_detector, "detect", return_value=result
            ) as detect:
                plan = batch_core.analyze_video(str(video), profile)

        detect.assert_not_called()
        self.assertEqual(plan.status, "ready")
        self.assertFalse(plan.burned_subtitle)

    def test_uncertain_burned_result_is_yellow_but_processable(self) -> None:
        profile = PreferenceProfile(1, "test", subtitle_languages=["en"])
        tracks = [core.Track(0, "audio", "AAC", "eng", "", True, False, False, False, False)]
        uncertain = burned_subtitle_detector.DetectionResult(
            False,
            0,
            6,
            "在 6 个抽样画面中发现 0 个烧录字幕证据",
            status="uncertain",
        )
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "movie.mkv"
            video.write_bytes(b"video")
            with patch.object(batch_core, "_inspect_media_cached", return_value=({}, False)), patch.object(
                batch_core.core, "tracks_from_media", return_value=tracks
            ), patch.object(
                batch_core.burned_subtitle_detector, "detect", return_value=uncertain
            ):
                plan = batch_core.analyze_video(str(video), profile)

        self.assertEqual(plan.status, "review")
        self.assertEqual(plan.task_state, "waiting")
        self.assertFalse(plan.burned_subtitle)
        self.assertIn("证据不足，不按烧录字幕阻断", plan.detail)


class PgsOcrFallbackTest(unittest.TestCase):
    def test_uses_compatibility_ocr_only_after_primary_and_rapidocr_fail(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            extracted = root / "track-7-en.sup"
            extracted.write_bytes(b"sup")
            tesseract = root / "tesseract.exe"
            tesseract.write_bytes(b"")
            tessdata = root / "tessdata"
            tessdata.mkdir()
            (tessdata / "eng.traineddata").write_bytes(b"")
            seconv = root / "seconv.exe"
            seconv.write_bytes(b"")
            calls = []

            def fake_run(args, **_kwargs):
                calls.append(args)
                if args[0] == str(seconv):
                    (root / "track-7-en.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
                else:
                    Path(args[args.index("--output") + 1]).write_bytes(b"")
                return None

            track = core.Track(7, "subtitles", "HDMV PGS", "en", "", False, False, False, True, False)
            with patch.object(core, "TESSERACT", str(tesseract)), patch.object(
                core, "SECONV", str(seconv)
            ), patch.object(core, "run_command", side_effect=fake_run):
                output = core.ocr_pgs_subtitle(extracted, track, root, lambda _message: None)
            self.assertEqual(output.name, "ocr-track-7-en.srt")
            self.assertGreater(output.stat().st_size, 0)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[-1][0], str(seconv))

    def test_large_composite_estimate_avoids_standard_ripper(self) -> None:
        class Item:
            width = 3000
            height = 120
            shape = (900, 0, 1020, 3000)

            def intersect(self, _other):
                return False

        estimated = core.estimate_pgs_composite_bytes([Item() for _ in range(600)], (130, 100), 31 * 1024)
        self.assertGreater(estimated, 384 * 1024 * 1024)


class BatchRuntimeStateTest(unittest.TestCase):
    def test_track_parser_keeps_existing_matroska_subtitle_statistics(self) -> None:
        tracks = core.tracks_from_media(
            {
                "tracks": [
                    {
                        "id": 2,
                        "type": "subtitles",
                        "codec": "SubRip/SRT",
                        "properties": {
                            "codec_id": "S_TEXT/UTF8",
                            "language_ietf": "en",
                            "tag_number_of_frames": "16",
                            "tag_duration": "00:01:20.998000000",
                            "tag_number_of_bytes": "414",
                        },
                    }
                ]
            }
        )

        self.assertEqual(tracks[0].statistics_frame_count, 16)
        self.assertAlmostEqual(tracks[0].statistics_duration_seconds or 0.0, 80.998)
        self.assertEqual(tracks[0].statistics_byte_count, 414)

    def test_analysis_skips_obviously_partial_text_track_without_extracting_it(self) -> None:
        profile = PreferenceProfile(1, "test", subtitle_languages=["zh-CN", "en"])
        media = {
            "container": {"properties": {"duration": 7_509_419_000_000}},
            "tracks": [
                {
                    "id": 0,
                    "type": "video",
                    "codec": "HEVC/H.265/MPEG-H",
                    "properties": {"tag_duration": "02:05:09.419000000"},
                },
                {
                    "id": 1,
                    "type": "audio",
                    "codec": "E-AC-3",
                    "properties": {"language_ietf": "en", "default_track": True},
                },
                {
                    "id": 2,
                    "type": "subtitles",
                    "codec": "SubRip/SRT",
                    "properties": {
                        "codec_id": "S_TEXT/UTF8",
                        "language_ietf": "en",
                        "track_name": "English",
                        "tag_number_of_frames": "16",
                        "tag_duration": "00:01:20.998000000",
                    },
                },
                {
                    "id": 3,
                    "type": "subtitles",
                    "codec": "SubRip/SRT",
                    "properties": {
                        "codec_id": "S_TEXT/UTF8",
                        "language_ietf": "en-GB",
                        "track_name": "English UK",
                        "tag_number_of_frames": "1156",
                        "tag_duration": "01:59:09.149000000",
                    },
                },
                {
                    "id": 4,
                    "type": "subtitles",
                    "codec": "SubRip/SRT",
                    "properties": {
                        "codec_id": "S_TEXT/UTF8",
                        "language_ietf": "cmn-Hans",
                        "track_name": "Simplified",
                        "tag_number_of_frames": "1164",
                        "tag_duration": "01:59:09.858000000",
                    },
                },
            ],
        }
        with patch.object(batch_core, "_inspect_media_cached", return_value=(media, False)):
            plan = batch_core.analyze_video("Big.Fish.2003.mkv", profile)

        self.assertEqual(plan.status, "ready")
        self.assertNotIn(2, plan.subtitle_ids)
        self.assertCountEqual(plan.subtitle_ids, [3, 4])
        self.assertIn("容器统计仅 16 条", plan.detail)

    def test_analysis_prefers_complete_sdh_pgs_over_four_frame_english_pgs(self) -> None:
        profile = PreferenceProfile(1, "test", subtitle_languages=["en", "zh-CN"])
        media = {
            "container": {"properties": {"duration": 7_910_000_000_000}},
            "tracks": [
                {"id": 0, "type": "video", "codec": "HEVC/H.265/MPEG-H",
                 "properties": {"tag_duration": "02:11:50.000000000"}},
                {"id": 1, "type": "audio", "codec": "AC-3",
                 "properties": {"language_ietf": "en", "default_track": True}},
                {"id": 3, "type": "subtitles", "codec": "SubRip/SRT",
                 "properties": {"codec_id": "S_TEXT/UTF8", "language_ietf": "en",
                                "track_name": "English Forced", "forced_track": True,
                                "tag_number_of_frames": "1"}},
                {"id": 4, "type": "subtitles", "codec": "HDMV PGS",
                 "properties": {"codec_id": "S_HDMV/PGS", "language_ietf": "en",
                                "track_name": "English SDH", "tag_number_of_frames": "3790"}},
                {"id": 11, "type": "subtitles", "codec": "HDMV PGS",
                 "properties": {"codec_id": "S_HDMV/PGS", "language_ietf": "en",
                                "track_name": "English", "tag_number_of_frames": "4"}},
            ],
        }
        with patch.object(batch_core, "_inspect_media_cached", return_value=(media, False)):
            plan = batch_core.analyze_video("Star.Wars.2019.mkv", profile)

        self.assertIn(4, plan.subtitle_ids)
        self.assertNotIn(3, plan.subtitle_ids)
        self.assertNotIn(11, plan.subtitle_ids)
        self.assertFalse(plan.has_complete_english_text)
        self.assertIn("容器统计仅 4 条", plan.detail)

    def test_processing_recheck_replaces_partial_selection_from_an_old_plan(self) -> None:
        profile = PreferenceProfile(1, "test", subtitle_languages=["en"])
        partial = core.Track(
            2, "subtitles", "S_TEXT/UTF8", "en", "English", True, False,
            True, False, False, 16, 80.998, 414,
        )
        complete = core.Track(
            3, "subtitles", "S_TEXT/UTF8", "en", "English UK", False, False,
            True, False, False, 1156, 7149.149, 48282,
        )
        plan = BatchPlan(
            "movie.mkv",
            1,
            subtitle_ids=[partial.id],
            source_subtitle_id=partial.id,
            source_mode="embedded",
        )
        messages: list[str] = []

        batch_core._refresh_incomplete_subtitle_selections(
            plan,
            profile,
            [partial, complete],
            7509.419,
            messages.append,
        )

        self.assertEqual(plan.subtitle_ids, [complete.id])
        self.assertEqual(plan.source_subtitle_id, complete.id)
        self.assertTrue(plan.has_complete_english_text)
        self.assertTrue(any("自动改用同语种完整字幕轨 3" in item for item in messages))

    def test_status_column_uses_runtime_state(self) -> None:
        plan = BatchPlan("movie.mkv", 1, status="ready", status_label="可以处理")
        self.assertEqual(ProMaxQt._batch_task_label(plan), "等待处理")
        plan.external_subtitle_verified = True
        self.assertEqual(ProMaxQt._batch_task_label(plan), "字幕匹配成功")
        plan.task_state = "processing"
        self.assertEqual(ProMaxQt._batch_task_label(plan), "正在处理")
        plan.task_state = "completed"
        self.assertEqual(ProMaxQt._batch_task_label(plan), "已经处理")

    def test_burned_subtitle_overrides_runtime_state(self) -> None:
        plan = BatchPlan("movie.mkv", 1, status="burned", burned_subtitle=True)
        self.assertEqual(ProMaxQt._batch_task_label(plan), "不可操作")
        self.assertEqual(ProMaxQt._batch_row_color(plan).name(), "#173f7a")
        self.assertEqual(ProMaxQt._batch_row_foreground(plan).name(), "#ffffff")

    def test_review_row_uses_yellow_background(self) -> None:
        plan = BatchPlan("movie.mkv", 1, status="review")
        self.assertEqual(ProMaxQt._batch_row_color(plan).name(), "#fff3da")

    def test_audio_passthrough_warning_is_yellow_but_processable(self) -> None:
        plan = BatchPlan(
            "movie.mkv",
            1,
            status="ready",
            audio_passthrough_warning=True,
        )
        self.assertEqual(ProMaxQt._batch_row_color(plan).name(), "#fff3da")
        self.assertEqual(ProMaxQt._batch_task_label(plan), "音频原样")

    def test_complete_non_english_text_is_ready_while_english_is_resolved_later(self) -> None:
        profile = PreferenceProfile(1, "test", ["AAC"], True, False, ["zh-CN", "en"], "online")
        tracks = [
            core.Track(0, "audio", "AAC", "eng", "", True, False, False, False, False),
            core.Track(1, "subtitles", "S_TEXT/UTF8", "spa", "", False, False, True, False, False),
        ]
        with patch.object(batch_core, "_inspect_media_cached", return_value=({}, False)), patch.object(
            batch_core.core, "tracks_from_media", return_value=tracks
        ):
            plan = batch_core.analyze_video("movie.mkv", profile)
        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.source_subtitle_id, 1)
        self.assertEqual(plan.source_mode, "embedded")
        self.assertTrue(plan.has_complete_text)

    def test_bilingual_full_track_is_complete_but_not_an_english_text_source(self) -> None:
        profile = PreferenceProfile(1, "test", ["AAC"], True, False, ["zh-CN", "en"], "online")
        tracks = [
            core.Track(0, "audio", "AAC", "eng", "", True, False, False, False, False),
            core.Track(
                1,
                "subtitles",
                "HDMV PGS",
                "chi",
                "简英双语特效字幕",
                True,
                True,
                False,
                True,
                False,
            ),
        ]
        with patch.object(batch_core, "_inspect_media_cached", return_value=({}, False)), patch.object(
            batch_core.core, "tracks_from_media", return_value=tracks
        ):
            plan = batch_core.analyze_video("movie.mkv", profile)

        self.assertFalse(batch_core._is_foreign_only_subtitle(tracks[1]))
        self.assertIn(1, plan.subtitle_ids)
        self.assertEqual(plan.status, "ready")
        self.assertIsNone(plan.source_subtitle_id)
        self.assertIn("内嵌文本字幕来源", plan.detail)

    def test_unnamed_forced_track_remains_incomplete(self) -> None:
        track = core.Track(
            1, "subtitles", "HDMV PGS", "eng", "", False, True, False, True, False
        )

        self.assertTrue(batch_core._is_foreign_only_subtitle(track))

    def test_existing_english_source_keeps_ready_translation_path(self) -> None:
        profile = PreferenceProfile(1, "test", ["AAC"], True, False, ["zh-CN", "en"], "online")
        tracks = [
            core.Track(0, "audio", "AAC", "eng", "", True, False, False, False, False),
            core.Track(1, "subtitles", "S_TEXT/UTF8", "eng", "", False, False, True, False, False),
        ]
        with patch.object(batch_core, "_inspect_media_cached", return_value=({}, False)), patch.object(
            batch_core.core, "tracks_from_media", return_value=tracks
        ):
            plan = batch_core.analyze_video("movie.mkv", profile)
        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.source_subtitle_id, 1)


if __name__ == "__main__":
    unittest.main()
