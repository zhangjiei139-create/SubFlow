# -*- coding: utf-8 -*-
from __future__ import annotations

import tempfile
import hashlib
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import batch_core
import pro_core
import smart_subtitles
import subtitle_tool_core as core
from profile_model import PreferenceProfile


class ImageSubtitlePolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tracks = [
            core.Track(0, "audio", "AAC", "eng", "", True, False, False, False, False),
            core.Track(1, "subtitles", "HDMV PGS", "eng", "", True, False, False, True, False),
        ]

    def test_verified_text_reference_still_guides_preserved_pgs(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "movie.mkv"
            video.write_bytes(b"video")
            reference = root / "verified.srt"
            reference.write_text(
                "".join(
                    f"{index}\n00:00:{index:02d},000 --> 00:00:{index:02d},800\n"
                    f"Dialogue {index}\n\n"
                    for index in range(1, 21)
                ),
                encoding="utf-8",
            )
            messages: list[str] = []
            with patch.object(core, "inspect_tracks", return_value=self.tracks), patch.object(
                core, "prepare_work_input", return_value=str(video)
            ), patch.object(core, "inspect_media", return_value={}), patch.object(
                pro_core, "_embedded_image_intervals", return_value=[(float(i), float(i) + 1) for i in range(20)]
            ) as scan_image_timing, patch.object(
                pro_core, "_interval_alignment", return_value=(0.99, -2.0)
            ) as compare_timelines:
                offsets, corrected, source_id = pro_core.prepare_embedded_text_corrections(
                    str(video), [1], None, 0, str(root / "work"), messages.append,
                    verified_external_subtitle=str(reference),
                )
                compare_timelines.return_value = (0.70, 0.2)
                near_zero_offsets, _, _ = pro_core.prepare_embedded_text_corrections(
                    str(video), [1], None, 0, str(root / "work"), messages.append,
                    verified_external_subtitle=str(reference),
                )

        self.assertEqual(offsets, {1: -2000})
        self.assertEqual(corrected, {})
        self.assertIsNone(source_id)
        self.assertEqual(near_zero_offsets, {1: 0})
        self.assertEqual(scan_image_timing.call_count, 2)
        self.assertEqual(compare_timelines.call_count, 2)
        self.assertTrue(any("套用偏移 -2.00 秒" in message for message in messages))
        self.assertTrue(any("保持原时间轴" in message for message in messages))

    def test_confirmed_download_remains_anchor_for_embedded_text(self) -> None:
        tracks = [
            self.tracks[0],
            core.Track(2, "subtitles", "S_TEXT/UTF8", "deu", "", True, False, True, False, False),
        ]
        subtitles = "".join(
            f"{index}\n00:00:{index:02d},000 --> 00:00:{index:02d},800\n"
            f"Dialogue {index}\n\n"
            for index in range(1, 21)
        )
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "movie.mkv"
            video.write_bytes(b"video")
            reference = root / "confirmed-download.srt"
            reference.write_text(subtitles, encoding="utf-8")

            def fake_extract(args, **_kwargs):
                for spec in args[3:]:
                    Path(spec.split(":", 1)[1]).write_text(subtitles, encoding="utf-8")

            with patch.object(core, "inspect_tracks", return_value=tracks), patch.object(
                core, "prepare_work_input", return_value=str(video)
            ), patch.object(core, "run_command", side_effect=fake_extract), patch.object(
                core, "inspect_media", return_value={}
            ), patch.object(pro_core, "_interval_alignment", return_value=(0.95, 1.5)
            ), patch.object(pro_core, "align_embedded_text_track") as independently_align:
                offsets, corrected, _ = pro_core.prepare_embedded_text_corrections(
                    str(video), [2], None, 0, str(root / "work"),
                    lambda _message: None,
                    verified_external_subtitle=str(reference),
                )

        self.assertEqual(offsets, {2: 1500})
        self.assertIn(2, corrected)
        independently_align.assert_not_called()

    def test_text_source_is_preferred_while_default_pgs_is_kept(self) -> None:
        profile = PreferenceProfile(1, "test", subtitle_languages=["en", "de"])
        tracks = [
            core.Track(0, "audio", "AAC", "eng", "", True, False, False, False, False),
            core.Track(1, "subtitles", "HDMV PGS", "eng", "", True, False, False, True, False),
            core.Track(2, "subtitles", "S_TEXT/UTF8", "eng", "", False, False, True, False, False),
        ]
        with patch.object(batch_core, "_inspect_media_cached", return_value=({}, False)), patch.object(
            batch_core.core, "tracks_from_media", return_value=tracks
        ):
            plan = batch_core.analyze_video("movie.mkv", profile)

        self.assertIn(1, plan.subtitle_ids)
        self.assertEqual(plan.source_subtitle_id, 2)
        self.assertEqual(plan.source_mode, "embedded")

    def test_image_subtitle_is_kept_by_default(self) -> None:
        profile = PreferenceProfile(
            1,
            "test",
            ["AAC"],
            True,
            False,
            ["zh-CN", "en"],
            "online",
        )
        with patch.object(batch_core, "_inspect_media_cached", return_value=({}, False)), patch.object(
            batch_core.core, "tracks_from_media", return_value=self.tracks
        ):
            plan = batch_core.analyze_video("movie.mkv", profile)

        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.subtitle_ids, [1])
        self.assertEqual(plan.missing_subtitle_languages, ["zh-CN"])
        self.assertTrue(plan.has_image_subtitles)
        self.assertFalse(plan.has_complete_english_text)

    def test_replace_preference_does_not_discard_image_without_download(self) -> None:
        profile = PreferenceProfile(
            1, "test", subtitle_languages=["zh-CN", "en"],
            replace_downloaded_subtitle=True,
        )
        with patch.object(batch_core, "_inspect_media_cached", return_value=({}, False)), patch.object(
            batch_core.core, "tracks_from_media", return_value=self.tracks
        ):
            plan = batch_core.analyze_video("movie.mkv", profile)

        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.subtitle_ids, [1])
        self.assertEqual(plan.missing_subtitle_languages, ["zh-CN"])

    def test_complete_english_text_is_recorded_for_smart_noop(self) -> None:
        tracks = [
            self.tracks[0],
            core.Track(2, "subtitles", "S_TEXT/UTF8", "eng", "", True, False, True, False, False),
        ]
        profile = PreferenceProfile(1, "test")
        with patch.object(batch_core, "_inspect_media_cached", return_value=({}, False)), patch.object(
            batch_core.core, "tracks_from_media", return_value=tracks
        ):
            plan = batch_core.analyze_video("movie.mkv", profile)

        self.assertTrue(plan.has_complete_english_text)

    def test_non_english_image_subtitle_is_ready_while_english_is_resolved_later(self) -> None:
        tracks = [
            self.tracks[0],
            core.Track(2, "subtitles", "HDMV PGS", "ger", "", True, False, False, True, False),
        ]
        profile = PreferenceProfile(
            1,
            "test",
            subtitle_languages=["zh-CN"],
        )
        with patch.object(batch_core, "_inspect_media_cached", return_value=({}, False)), patch.object(
            batch_core.core, "tracks_from_media", return_value=tracks
        ):
            plan = batch_core.analyze_video("movie.mkv", profile)

        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.source_mode, "none")
        self.assertIsNone(plan.source_subtitle_id)
        self.assertIn("完整内嵌文本字幕来源", plan.detail)

    def test_batch_processing_prefers_verified_download_and_keeps_english_pgs(self) -> None:
        profile = PreferenceProfile(
            1,
            "test",
            subtitle_languages=["zh-CN", "en"],
        )
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "movie.mkv"
            video.write_bytes(b"video")
            tracks = list(self.tracks)
            with patch.object(batch_core, "_inspect_media_cached", return_value=({}, False)), patch.object(
                batch_core.core, "tracks_from_media", return_value=tracks
            ):
                plan = batch_core.analyze_video(str(video), profile)
            verified = root / "external-verified.srt"
            verified.write_text("verified", encoding="utf-8")
            result = smart_subtitles.SmartSubtitleResult(
                str(verified),
                "en",
                "字幕预检通过",
                "OpenSubtitles",
                object(),
            )
            with patch.object(batch_core.core, "inspect_tracks", return_value=tracks), patch.object(
                smart_subtitles, "find_verified_english", return_value=result
            ), patch.object(batch_core.pro_core, "process_pro") as process:
                batch_core.process_plan(plan, profile, lambda _message: None, None)

        kwargs = process.call_args.kwargs
        self.assertEqual(kwargs["source_mode"], "online")
        self.assertEqual(kwargs["external_subtitle"], str(verified))
        self.assertIn(1, kwargs["keep_subtitle_ids"])
        self.assertFalse(kwargs["include_external_source_in_output"])

    def test_download_replaces_same_language_pgs_only_when_selected(self) -> None:
        profile = PreferenceProfile(
            1, "test", subtitle_languages=["zh-CN", "en"],
            replace_downloaded_subtitle=True,
        )
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "movie.mkv"
            video.write_bytes(b"video")
            tracks = list(self.tracks)
            with patch.object(batch_core, "_inspect_media_cached", return_value=({}, False)), patch.object(
                batch_core.core, "tracks_from_media", return_value=tracks
            ):
                plan = batch_core.analyze_video(str(video), profile)
            verified = root / "online-verified.srt"
            verified.write_text("verified", encoding="utf-8")
            result = smart_subtitles.SmartSubtitleResult(
                str(verified), "en", "字幕预检通过",
                "OpenSubtitles", object(),
            )
            with patch.object(batch_core.core, "inspect_tracks", return_value=tracks), patch.object(
                smart_subtitles, "find_verified_english", return_value=result
            ), patch.object(batch_core.pro_core, "process_pro") as process:
                batch_core.process_plan(plan, profile, lambda _message: None, None)

        kwargs = process.call_args.kwargs
        self.assertEqual(kwargs["source_mode"], "online")
        self.assertEqual(kwargs["keep_subtitle_ids"], [])
        self.assertTrue(kwargs["include_external_source_in_output"])
        self.assertEqual(kwargs["external_subtitle"], str(verified))

    def test_download_replacement_works_without_translation_targets(self) -> None:
        profile = PreferenceProfile(
            1, "test", subtitle_languages=["en"],
            replace_downloaded_subtitle=True,
        )
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "movie.mkv"
            video.write_bytes(b"video")
            with patch.object(batch_core, "_inspect_media_cached", return_value=({}, False)), patch.object(
                batch_core.core, "tracks_from_media", return_value=self.tracks
            ):
                plan = batch_core.analyze_video(str(video), profile)
            verified = root / "online-verified.srt"
            verified.write_text("verified", encoding="utf-8")
            result = smart_subtitles.SmartSubtitleResult(
                str(verified), "en", "字幕预检通过",
                "OpenSubtitles", object(),
            )
            messages = []
            with patch.object(batch_core.core, "inspect_tracks", return_value=self.tracks), patch.object(
                smart_subtitles, "find_verified_english", return_value=result
            ), patch.object(batch_core.pro_core, "process_pro") as process:
                batch_core.process_plan(plan, profile, messages.append, None)

        self.assertTrue(any("按偏好写入成品并替换同语言内嵌字幕" in msg for msg in messages))
        self.assertFalse(any("核验后不自动写入成品" in msg for msg in messages))
        kwargs = process.call_args.kwargs
        self.assertEqual(kwargs["source_mode"], "online")
        self.assertEqual(kwargs["target_codes"], [])
        self.assertEqual(kwargs["keep_subtitle_ids"], [])
        self.assertTrue(kwargs["include_external_source_in_output"])

    def test_chinese_download_replacement_does_not_reuse_old_conversion_track(self) -> None:
        profile = PreferenceProfile(
            1, "test", subtitle_languages=["zh-CN", "zh-TW"],
            replace_downloaded_subtitle=True,
        )
        tracks = [
            self.tracks[0],
            core.Track(4, "subtitles", "S_TEXT/UTF8", "zh-CN", "Mandarin Simplified", True, False, True, False, False),
        ]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "movie.mkv"
            video.write_bytes(b"video")
            downloaded = root / "manual-confirmed.srt"
            downloaded.write_text("confirmed", encoding="utf-8")
            with patch.object(batch_core, "_inspect_media_cached", return_value=({}, False)), patch.object(
                batch_core.core, "tracks_from_media", return_value=tracks
            ):
                plan = batch_core.analyze_video(
                    str(video), profile, str(downloaded), "zh-CN", True,
                    external_subtitle_origin="download",
                )
            self.assertEqual(plan.local_chinese_conversion_sources, {"zh-TW": 4})
            with patch.object(batch_core.core, "inspect_tracks", return_value=tracks), patch.object(
                batch_core.pro_core, "process_pro"
            ) as process:
                batch_core.process_plan(plan, profile, lambda _message: None, None)

        kwargs = process.call_args.kwargs
        self.assertEqual(kwargs["keep_subtitle_ids"], [])
        self.assertEqual(kwargs["local_chinese_conversion_sources"], {})
        self.assertEqual(kwargs["target_codes"], ["zh-TW"])
        self.assertTrue(kwargs["include_external_source_in_output"])

    def test_manual_confirmed_timeline_is_not_resynchronized(self) -> None:
        profile = PreferenceProfile(1, "test", subtitle_languages=["en"])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "movie.mkv"
            video.write_bytes(b"video")
            subtitle = root / "manual-confirmed.srt"
            subtitle.write_text("confirmed", encoding="utf-8")
            file_hash = hashlib.sha256(subtitle.read_bytes()).hexdigest()
            with patch.object(batch_core, "_inspect_media_cached", return_value=({}, False)), patch.object(
                batch_core.core, "tracks_from_media", return_value=self.tracks
            ):
                plan = batch_core.analyze_video(
                    str(video), profile, str(subtitle), "en", True,
                    external_subtitle_origin="download",
                    manual_confirmation_hash=file_hash,
                )
            with patch.object(batch_core.core, "inspect_tracks", return_value=self.tracks), patch.object(
                batch_core.pro_core, "process_pro"
            ) as process:
                batch_core.process_plan(plan, profile, lambda _message: None, None)

        kwargs = process.call_args.kwargs
        self.assertTrue(kwargs["trust_external_original_timeline"])
        self.assertEqual(kwargs["verified_timeline_reference"], str(subtitle))

    def test_manual_confirmation_file_change_blocks_processing(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "movie.mkv"
            video.write_bytes(b"video")
            subtitle = root / "manual-confirmed.srt"
            subtitle.write_text("changed", encoding="utf-8")
            plan = batch_core.BatchPlan(
                path=str(video), profile_slot=1, status="ready",
                external_subtitle=str(subtitle),
                external_subtitle_verified=True,
                manual_confirmation_hash="prior-hash",
                source_mode="online",
            )
            with self.assertRaisesRegex(RuntimeError, "人工确认的字幕文件已变化"):
                batch_core.process_plan(
                    plan, PreferenceProfile(1, "test"),
                    lambda _message: None, None,
                )

    def test_batch_processing_stops_instead_of_using_pgs_as_english_text(self) -> None:
        profile = PreferenceProfile(
            1,
            "test",
            subtitle_languages=["zh-CN", "en"],
        )
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "movie.mkv"
            video.write_bytes(b"video")
            tracks = list(self.tracks)
            with patch.object(batch_core, "_inspect_media_cached", return_value=({}, False)), patch.object(
                batch_core.core, "tracks_from_media", return_value=tracks
            ):
                plan = batch_core.analyze_video(str(video), profile)
            with patch.object(batch_core.core, "inspect_tracks", return_value=tracks), patch.object(
                smart_subtitles, "find_verified_english", side_effect=RuntimeError("没有可靠候选")
            ), patch.object(batch_core.pro_core, "process_pro") as process:
                with self.assertRaisesRegex(RuntimeError, "请手动导入或选择字幕后核听"):
                    batch_core.process_plan(plan, profile, lambda _message: None, None)

        process.assert_not_called()

    def test_yellow_missing_subtitle_automatically_searches_and_processes(self) -> None:
        profile = PreferenceProfile(1, "test", subtitle_languages=["zh-CN", "en"])
        tracks = [core.Track(0, "audio", "AAC", "eng", "", True, False, False, False, False)]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "movie.mkv"
            video.write_bytes(b"video")
            with patch.object(batch_core, "_inspect_media_cached", return_value=({}, False)), patch.object(
                batch_core.core, "tracks_from_media", return_value=tracks
            ):
                plan = batch_core.analyze_video(str(video), profile)
            verified = root / "verified.srt"
            verified.write_text("verified", encoding="utf-8")
            result = smart_subtitles.SmartSubtitleResult(
                str(verified),
                "en",
                "字幕预检通过",
                "OpenSubtitles",
                object(),
                identity_key="imdb:1",
                release="Movie 2026",
                verification_seal="seal",
            )
            with patch.object(batch_core.core, "inspect_tracks", return_value=tracks), patch.object(
                smart_subtitles, "find_verified_english", return_value=result
            ) as search, patch.object(batch_core.pro_core, "process_pro") as process:
                batch_core.process_plan(plan, profile, lambda _message: None, None)

        search.assert_called_once()
        kwargs = process.call_args.kwargs
        self.assertEqual(kwargs["source_mode"], "online")
        self.assertEqual(kwargs["external_subtitle"], str(verified))
        self.assertTrue(kwargs["include_external_source_in_output"])

    def test_verified_english_for_english_target_does_not_start_translation_model(self) -> None:
        profile = PreferenceProfile(1, "test", subtitle_languages=["en"])
        tracks = [core.Track(0, "audio", "AAC", "eng", "", True, False, False, False, False)]
        ai_gate = MagicMock()
        ai_gate.acquire.return_value = True
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "movie.mkv"
            video.write_bytes(b"video")
            with patch.object(batch_core, "_inspect_media_cached", return_value=({}, False)), patch.object(
                batch_core.core, "tracks_from_media", return_value=tracks
            ):
                plan = batch_core.analyze_video(str(video), profile)
            verified = root / "verified.srt"
            verified.write_text("verified", encoding="utf-8")
            result = smart_subtitles.SmartSubtitleResult(
                str(verified),
                "en",
                "字幕预检通过",
                "OpenSubtitles",
                object(),
            )
            with patch.object(batch_core.core, "inspect_tracks", return_value=tracks), patch.object(
                smart_subtitles, "find_verified_english", return_value=result
            ), patch.object(batch_core.core, "begin_ollama_lease") as begin_lease, patch.object(
                batch_core.core, "end_ollama_lease"
            ) as end_lease, patch.object(batch_core.pro_core, "process_pro") as process:
                batch_core.process_plan(plan, profile, lambda _message: None, None, ai_gate=ai_gate)

        self.assertEqual(process.call_args.kwargs["target_codes"], [])
        ai_gate.acquire.assert_not_called()
        ai_gate.release.assert_not_called()
        begin_lease.assert_not_called()
        end_lease.assert_not_called()

    def test_non_english_source_is_not_used_to_generate_english(self) -> None:
        profile = PreferenceProfile(1, "test", subtitle_languages=["en"])
        tracks = [
            core.Track(0, "audio", "AAC", "chi", "", True, False, False, False, False),
            core.Track(1, "subtitles", "S_TEXT/UTF8", "chi", "", True, False, True, False, False),
        ]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "movie.mkv"
            video.write_bytes(b"video")
            with patch.object(batch_core, "_inspect_media_cached", return_value=({}, False)), patch.object(
                batch_core.core, "tracks_from_media", return_value=tracks
            ):
                plan = batch_core.analyze_video(str(video), profile)
            verified = root / "online-verified.srt"
            verified.write_text("verified", encoding="utf-8")
            result = smart_subtitles.SmartSubtitleResult(
                str(verified), "en", "字幕预检通过", "OpenSubtitles", object()
            )
            with patch.object(batch_core.core, "inspect_tracks", return_value=tracks), patch.object(
                smart_subtitles, "find_verified_english", return_value=result
            ) as search, patch.object(batch_core.pro_core, "process_pro") as process, patch.object(
                batch_core.core, "begin_ollama_lease"
            ) as begin_lease:
                batch_core.process_plan(plan, profile, lambda _message: None, None)

        search.assert_called_once()
        self.assertEqual(process.call_args.kwargs["source_mode"], "online")
        self.assertEqual(process.call_args.kwargs["target_codes"], [])
        begin_lease.assert_not_called()


    def test_embedded_verification_failure_tries_online_search(self) -> None:
        profile = PreferenceProfile(1, "test", subtitle_languages=["zh-CN", "en"])
        tracks = [
            core.Track(0, "audio", "AAC", "eng", "", True, False, False, False, False),
            core.Track(2, "subtitles", "S_TEXT/UTF8", "eng", "", True, False, True, False, False),
        ]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "movie.mkv"
            video.write_bytes(b"video")
            plan = batch_core.BatchPlan(
                path=str(video),
                profile_slot=1,
                audio_ids=[0],
                subtitle_ids=[2],
                missing_subtitle_languages=["zh-CN"],
                source_subtitle_id=2,
                source_mode="embedded",
                status="ready",
                status_label="可以处理",
                output_path=str(root / "movie.SF.mkv"),
                has_complete_english_text=True,
            )
            with patch.object(batch_core.core, "inspect_tracks", return_value=tracks), patch.object(
                batch_core.pro_core, "prepare_shared_subtitle_content_audio", return_value=MagicMock()
            ), patch.object(
                batch_core.pro_core, "prepare_embedded_text_corrections", side_effect=RuntimeError("核验失败")
            ), patch.object(smart_subtitles, "find_verified_english") as search, patch.object(
                batch_core.pro_core, "process_pro"
            ) as process, patch.object(batch_core.core, "begin_ollama_lease"), patch.object(
                batch_core.core, "end_ollama_lease", return_value=False
            ):
                batch_core.process_plan(plan, profile, lambda _message: None, None)

        search.assert_called_once()
        process.assert_called_once()
        self.assertFalse(process.call_args.kwargs["trust_embedded_original_timeline"])


    def test_yellow_missing_subtitle_stops_safely_when_search_fails(self) -> None:
        profile = PreferenceProfile(1, "test", subtitle_languages=["zh-CN", "en"])
        tracks = [core.Track(0, "audio", "AAC", "eng", "", True, False, False, False, False)]
        with patch.object(batch_core, "_inspect_media_cached", return_value=({}, False)), patch.object(
            batch_core.core, "tracks_from_media", return_value=tracks
        ):
            plan = batch_core.analyze_video("movie.mkv", profile)
        with patch.object(batch_core.core, "inspect_tracks", return_value=tracks), patch.object(
            smart_subtitles, "find_verified_english", side_effect=RuntimeError("没有可靠候选")
        ), patch.object(batch_core.pro_core, "process_pro") as process:
            with self.assertRaisesRegex(RuntimeError, "请手动导入或选择字幕后核听"):
                batch_core.process_plan(plan, profile, lambda _message: None, None)

        process.assert_not_called()


    def test_verified_external_file_cannot_silently_fall_back_when_missing(self) -> None:
        profile = PreferenceProfile(1, "test", subtitle_languages=["zh-CN", "en"])
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "movie.mkv"
            video.write_bytes(b"video")
            missing = Path(folder) / "missing-verified.srt"
            with patch.object(batch_core, "_inspect_media_cached", return_value=({}, False)), patch.object(
                batch_core.core, "tracks_from_media", return_value=self.tracks
            ):
                plan = batch_core.analyze_video(
                    str(video), profile, str(missing), "en", True, "verified"
                )

        self.assertEqual(plan.status, "review")
        self.assertIn("文件已丢失", plan.summary)

    def test_non_english_pgs_stops_when_english_download_fails(self) -> None:
        profile = PreferenceProfile(1, "test", subtitle_languages=["zh-CN", "en"])
        tracks = [
            core.Track(0, "audio", "AAC", "eng", "", True, False, False, False, False),
            core.Track(2, "subtitles", "HDMV PGS", "chi", "", True, False, False, True, False),
        ]
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "movie.mkv"
            video.write_bytes(b"video")
            with patch.object(batch_core, "_inspect_media_cached", return_value=({}, False)), patch.object(
                batch_core.core, "tracks_from_media", return_value=tracks
            ):
                plan = batch_core.analyze_video(str(video), profile)
            with patch.object(batch_core.core, "inspect_tracks", return_value=tracks), patch.object(
                smart_subtitles, "find_verified_english", side_effect=RuntimeError("没有可靠候选")
            ), patch.object(batch_core.pro_core, "process_pro") as process:
                with self.assertRaisesRegex(RuntimeError, "请手动导入或选择字幕后核听"):
                    batch_core.process_plan(plan, profile, lambda _message: None, None)

        process.assert_not_called()


    def test_online_verified_filename_requires_current_alignment(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            verified = Path(folder) / "online-verified.srt"
            verified.write_text("verified timeline", encoding="utf-8")
            messages: list[str] = []
            corrected = Path(folder) / "current-rule-verified.srt"
            with patch.object(pro_core, "align_external_subtitle", return_value=corrected) as align:
                selected = pro_core._external_timeline_for_processing(
                    "movie.mkv",
                    str(verified),
                    folder,
                    0,
                    messages.append,
                )

        self.assertEqual(selected, corrected)
        align.assert_called_once()
        self.assertFalse(any("不再重复自动对时" in message for message in messages))


    def test_retained_pgs_downloads_verified_text_as_timing_anchor_only(self) -> None:
        profile = PreferenceProfile(1, "test", subtitle_languages=["en"])
        tracks = list(self.tracks)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "movie.mkv"
            video.write_bytes(b"video")
            with patch.object(batch_core, "_inspect_media_cached", return_value=({}, False)), patch.object(
                batch_core.core, "tracks_from_media", return_value=tracks
            ):
                plan = batch_core.analyze_video(str(video), profile)
            verified = root / "online-verified.srt"
            verified.write_text("verified", encoding="utf-8")
            result = smart_subtitles.SmartSubtitleResult(
                str(verified), "en", "字幕预检通过", "OpenSubtitles", object()
            )
            with patch.object(batch_core.core, "inspect_tracks", return_value=tracks), patch.object(
                smart_subtitles, "find_verified_english", return_value=result
            ) as search, patch.object(batch_core.pro_core, "process_pro") as process:
                batch_core.process_plan(plan, profile, lambda _message: None, None)

        search.assert_called_once()
        kwargs = process.call_args.kwargs
        self.assertEqual(kwargs["source_mode"], "none")
        self.assertEqual(kwargs["target_codes"], [])
        self.assertEqual(kwargs["keep_subtitle_ids"], [1])
        self.assertEqual(kwargs["verified_timeline_reference"], str(verified))
        self.assertFalse(kwargs["include_external_source_in_output"])

    def test_unselected_embedded_english_text_can_anchor_retained_pgs(self) -> None:
        profile = PreferenceProfile(1, "test", subtitle_languages=["zh-CN"])
        tracks = [
            core.Track(0, "audio", "AAC", "eng", "", True, False, False, False, False),
            core.Track(1, "subtitles", "HDMV PGS", "chi", "", True, False, False, True, False),
            core.Track(2, "subtitles", "S_TEXT/UTF8", "eng", "", False, False, True, False, False),
        ]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "movie.mkv"
            video.write_bytes(b"video")
            with patch.object(batch_core, "_inspect_media_cached", return_value=({}, False)), patch.object(
                batch_core.core, "tracks_from_media", return_value=tracks
            ):
                plan = batch_core.analyze_video(str(video), profile)
            with patch.object(batch_core.core, "inspect_tracks", return_value=tracks), patch.object(
                smart_subtitles, "find_verified_english"
            ) as search, patch.object(batch_core.pro_core, "prepare_shared_subtitle_content_audio"), patch.object(
                batch_core.pro_core, "prepare_embedded_text_corrections",
                side_effect=lambda *a, **kw: kw["verification_status"].update(verified_anchor=True)
            ), patch.object(batch_core.pro_core, "process_pro") as process:
                batch_core.process_plan(plan, profile, lambda _message: None, None)

        search.assert_not_called()
        kwargs = process.call_args.kwargs
        self.assertEqual(kwargs["source_mode"], "none")
        self.assertEqual(kwargs["embedded_source_id"], 2)
        self.assertEqual(kwargs["keep_subtitle_ids"], [1])
        self.assertIsNone(kwargs["verified_timeline_reference"])

class AudioPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tracks = [
            core.Track(1, "audio", "TrueHD Atmos", "eng", "", True, False, False, False, False),
            core.Track(2, "audio", "AC-3", "eng", "", False, False, False, False, False),
            core.Track(3, "audio", "AAC", "chi", "", False, False, False, False, False),
        ]

    def test_native_keeps_every_original_audio_track(self) -> None:
        kept, generated, _action, source_id = batch_core._audio_policy_selection(self.tracks, self.tracks[0], "native")

        self.assertEqual(kept, [1, 2, 3])
        self.assertEqual(generated, [])
        self.assertIsNone(source_id)

    def test_universal_keeps_best_quality_and_one_universal_track(self) -> None:
        kept, generated, _action, source_id = batch_core._audio_policy_selection(self.tracks, self.tracks[0], "universal")

        self.assertEqual(kept, [1, 2, 3])
        self.assertEqual(generated, [])
        self.assertIsNone(source_id)

    def test_compact_keeps_only_one_universal_track(self) -> None:
        kept, generated, _action, source_id = batch_core._audio_policy_selection(self.tracks, self.tracks[0], "compact")

        self.assertEqual(kept, [2])
        self.assertEqual(generated, [])
        self.assertIsNone(source_id)

    def test_universal_policy_prefers_existing_main_audio_language(self) -> None:
        tracks = [
            core.Track(1, "audio", "AAC", "eng", "", True, False, False, False, False),
            core.Track(2, "audio", "AC-3", "chi", "", False, False, False, False, False),
        ]

        kept, generated, _action, source_id = batch_core._audio_policy_selection(tracks, tracks[0], "universal")

        self.assertEqual(kept, [1, 2])
        self.assertEqual(generated, [])
        self.assertIsNone(source_id)

    def test_missing_universal_track_is_generated_from_quality_source(self) -> None:
        quality_only = [self.tracks[0]]

        universal_ids, universal_generated, _action, universal_source_id = batch_core._audio_policy_selection(
            quality_only, quality_only[0], "universal"
        )
        compact_ids, compact_generated, _action, compact_source_id = batch_core._audio_policy_selection(
            quality_only, quality_only[0], "compact"
        )

        self.assertEqual(universal_ids, [1])
        self.assertEqual(universal_generated, ["AC-3"])
        self.assertEqual(universal_source_id, 1)
        self.assertEqual(compact_ids, [])
        self.assertEqual(compact_generated, ["AC-3"])
        self.assertEqual(compact_source_id, 1)

    def test_universal_groups_languages_and_makes_english_first(self) -> None:
        tracks = [
            core.Track(1, "audio", "E-AC-3", "chi", "", True, False, False, False, False),
            core.Track(2, "audio", "E-AC-3", "chi", "", False, False, False, False, False),
            core.Track(3, "audio", "E-AC-3", "eng", "", False, False, False, False, False),
            core.Track(4, "audio", "AAC", "spa", "", False, False, False, False, False),
        ]

        kept, generated, _action, source_id = batch_core._audio_policy_selection(
            tracks, tracks[0], "universal"
        )

        self.assertEqual(kept, [3, 1, 4])
        self.assertEqual(generated, [])
        self.assertIsNone(source_id)

    def test_und_audio_bypasses_policy_without_blocking_subtitles(self) -> None:
        tracks = [
            core.Track(1, "audio", "TrueHD Atmos", "und", "", True, False, False, False, False),
            core.Track(2, "audio", "AC-3", "eng", "", False, False, False, False, False),
            core.Track(3, "subtitles", "S_TEXT/UTF8", "eng", "", True, False, True, False, False),
        ]
        profile = PreferenceProfile(1, "test", subtitle_languages=["en"], audio_policy="compact")
        with patch.object(batch_core, "_inspect_media_cached", return_value=({}, False)), patch.object(
            batch_core.core, "tracks_from_media", return_value=tracks
        ):
            plan = batch_core.analyze_video("movie.mkv", profile)

        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.audio_ids, [1, 2])
        self.assertEqual(plan.missing_audio_formats, [])
        self.assertTrue(plan.audio_passthrough_warning)
        self.assertIn("存在未标记语言音轨，音频保持原样", plan.summary)

    def test_mux_can_preserve_original_audio_defaults(self) -> None:
        with patch.object(core, "run_command") as run_command:
            core.mux_video(
                "movie.mkv",
                "output.mkv",
                [1, 2],
                [],
                [],
                lambda _message: None,
                preserve_audio_defaults=True,
            )

        command = run_command.call_args.args[0]
        self.assertNotIn("--default-track", command)

    def test_mux_applies_verified_subtitle_track_offset(self) -> None:
        with patch.object(core, "run_command") as run_command:
            core.mux_video(
                "movie.mkv",
                "output.mkv",
                [1],
                [2, 3],
                [],
                lambda _message: None,
                subtitle_sync_offsets={2: 30310, 3: 0},
            )

        command = run_command.call_args.args[0]
        sync_index = command.index("--sync")
        self.assertEqual(command[sync_index + 1], "2:30310")

    def test_track_descriptions_include_audio_role_and_subtitle_format(self) -> None:
        subtitles = [
            core.Track(4, "subtitles", "VobSub", "eng", "", True, False, False, False, True),
            core.Track(5, "subtitles", "SubRip/SRT", "chi", "", False, False, True, False, False),
        ]

        self.assertEqual(
            batch_core._describe_audio_tracks(self.tracks[:2], self.tracks[0]),
            "en / TrueHD Atmos（主音轨），en / AC-3",
        )
        self.assertEqual(batch_core._describe_tracks(subtitles), "英文 / VobSub、中文（简繁未标记） / SRT")


class SameLanguageOcrOutputTest(unittest.TestCase):
    def test_image_source_can_become_same_language_text_without_translation(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "movie.mkv"
            video.write_bytes(b"video")
            output = root / "output.mkv"
            extracted = root / "track.sup"
            extracted.write_bytes(b"sup")
            ocr = root / "track.srt"
            ocr.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
            tracks = [
                core.Track(0, "audio", "AAC", "eng", "", True, False, False, False, False),
                core.Track(1, "subtitles", "HDMV PGS", "eng", "", True, False, False, True, False),
            ]
            generated_capture = []

            def mux(
                _input,
                output_path,
                _audio,
                _subs,
                generated,
                _log,
                cancel_event=None,
                preserve_audio_defaults=False,
                subtitle_sync_offsets=None,
                subtitle_language_preferences=None,
                chinese_script_equivalent=False,
                input_media=None,
            ):
                _ = cancel_event, preserve_audio_defaults, subtitle_sync_offsets
                self.assertIsNone(subtitle_language_preferences)
                generated_capture.extend(generated)
                Path(output_path).write_bytes(b"output")

            with patch.object(core, "inspect_media", return_value={"tracks": [], "container": {}}), patch.object(
                core, "inspect_tracks", return_value=tracks
            ), patch.object(core, "prepare_work_input", return_value=str(video)), patch.object(
                core, "extract_subtitle", return_value=extracted
            ), patch.object(core, "ocr_pgs_subtitle", return_value=ocr), patch.object(
                core, "ensure_output_disk_space"
            ), patch.object(core, "ensure_mux_disk_space"), patch.object(
                core, "ensure_ollama_running"
            ) as ollama, patch.object(core, "translate_events") as translate, patch.object(
                core, "mux_video", side_effect=mux
            ), patch.object(core, "validate_media_output"):
                core.process_video(
                    str(video),
                    str(output),
                    [0],
                    [],
                    1,
                    ["en"],
                    str(root / "work"),
                )

        ollama.assert_not_called()
        translate.assert_not_called()
        self.assertEqual(len(generated_capture), 1)
        self.assertEqual(generated_capture[0][1], "en")
        self.assertEqual(generated_capture[0][2], "英文 OCR")


class SmartSubtitleSearchTest(unittest.TestCase):
    def setUp(self) -> None:
        self._shared_content_patch = patch.object(
            smart_subtitles.pro_core,
            "prepare_shared_subtitle_content_audio",
            return_value=SimpleNamespace(fingerprint=object()),
        )
        self._shared_content_patch.start()
        self.addCleanup(self._shared_content_patch.stop)

    def test_shared_content_failure_never_falls_back_to_legacy_preflight(self) -> None:
        candidate = SimpleNamespace(
            release="Movie.2025.BluRay",
            file_name="Movie.2025.BluRay.srt",
            feature_title="Movie",
            language="en",
            identity_key="imdb:1",
            file_id="1",
        )

        class Service:
            @staticmethod
            def load_settings():
                return {"key": "token"}

            @staticmethod
            def search(_key, _video, _language):
                return None, [candidate], None

            @staticmethod
            def download(_key, _candidate, destination):
                path = Path(destination) / candidate.file_name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("subtitle", encoding="utf-8")
                return path

        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "Movie.2025.mkv"
            video.write_bytes(b"video")
            with patch.object(
                smart_subtitles, "PROVIDERS", (("OpenSubtitles", Service, "key"),)
            ), patch.object(
                smart_subtitles.pro_core,
                "prepare_shared_subtitle_content_audio",
                side_effect=RuntimeError("whisper unavailable"),
            ), patch.object(
                smart_subtitles.pro_core, "preflight_online_subtitle"
            ) as online_preflight, patch.object(
                smart_subtitles.pro_core, "preflight_external_subtitle"
            ) as legacy_preflight:
                with self.assertRaisesRegex(RuntimeError, "无法安全核验在线字幕候选"):
                    smart_subtitles.find_verified_english(
                        str(video), 0, lambda _message: None
                    )

        online_preflight.assert_not_called()
        legacy_preflight.assert_not_called()

    def test_shared_audio_starts_before_provider_search_finishes(self) -> None:
        audio_started = threading.Event()
        candidate = SimpleNamespace(
            release="Movie.2025.1080p.BluRay-GROUP",
            file_name="Movie.2025.1080p.BluRay-GROUP.en.srt",
            feature_title="Movie",
            language="en",
            identity_verified=True,
            identity_key="imdb:1",
            file_id="1",
        )

        class Service:
            @staticmethod
            def load_settings():
                return {"key": "token"}

            @staticmethod
            def search(_key, _video, _language):
                if not audio_started.wait(1.0):
                    raise AssertionError("公共音频没有与搜索同时启动")
                return None, [candidate], None

            @staticmethod
            def download(_key, _candidate, destination):
                path = Path(destination) / "movie.srt"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("subtitle", encoding="utf-8")
                return path

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "Movie.2025.mkv"
            video.write_bytes(b"video")
            verified = root / "verified.srt"
            verified.write_text("verified", encoding="utf-8")
            shared_content = SimpleNamespace(fingerprint=object())

            def prepare_audio(*_args, **_kwargs):
                audio_started.set()
                return shared_content

            with patch.object(smart_subtitles, "PROVIDERS", (("Test", Service, "key"),)), patch.object(
                smart_subtitles.pro_core, "prepare_shared_subtitle_content_audio", side_effect=prepare_audio
            ), patch.object(
                smart_subtitles.pro_core, "preflight_online_subtitle", return_value=(verified, "验证通过")
            ), patch.object(
                smart_subtitles.pro_core, "preflight_external_subtitle"
            ) as legacy_preflight:
                result = smart_subtitles.find_verified_english(str(video), 0, lambda _message: None)

        self.assertIs(result.candidate, candidate)
        legacy_preflight.assert_not_called()

    def test_candidate_downloads_are_limited_and_concurrent(self) -> None:
        candidates = [
            SimpleNamespace(
                release=f"Movie.2025.1080p.BluRay-GROUP-{index}",
                file_name=f"Movie.2025.1080p.BluRay-GROUP-{index}.en.srt",
                feature_title="Movie",
                language="en",
                identity_verified=True,
                identity_key="imdb:1",
                file_id=str(index),
            )
            for index in range(5)
        ]
        lock = threading.Lock()
        active = 0
        peak_active = 0

        class Service:
            @staticmethod
            def load_settings():
                return {"key": "token"}

            @staticmethod
            def search(_key, _video, _language):
                return None, candidates, None

            @staticmethod
            def download(_key, candidate, destination):
                nonlocal active, peak_active
                with lock:
                    active += 1
                    peak_active = max(peak_active, active)
                time.sleep(0.08)
                path = Path(destination) / f"{candidate.file_id}.srt"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"subtitle {candidate.file_id}", encoding="utf-8")
                with lock:
                    active -= 1
                return path

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "Movie.2025.mkv"
            video.write_bytes(b"video")
            verified = root / "verified.srt"
            verified.write_text("verified", encoding="utf-8")
            with patch.object(smart_subtitles, "PROVIDERS", (("Test", Service, "key"),)), patch.object(
                smart_subtitles.pro_core, "preflight_online_subtitle", return_value=(verified, "验证通过")
            ):
                smart_subtitles.find_verified_english(str(video), 0, lambda _message: None)

        self.assertGreaterEqual(peak_active, 2)
        self.assertLessEqual(peak_active, smart_subtitles.SMART_DOWNLOAD_WORKERS)

    def test_cross_provider_metadata_duplicates_are_removed_before_download(self) -> None:
        primary = SimpleNamespace(
            release="Movie.2025.1080p.BluRay-GROUP",
            file_name="Movie.2025.1080p.BluRay-GROUP.en.srt",
            feature_title="Movie",
            language="en",
            identity_verified=True,
            identity_key="imdb:1",
            file_id="primary",
        )
        duplicate = SimpleNamespace(
            release="Movie 2025 1080p BluRay GROUP",
            file_name="Movie.2025.1080p.BluRay-GROUP-English.srt",
            feature_title="Movie",
            language="en",
            identity_verified=True,
            identity_key="tmdb:2",
            file_id="duplicate",
        )
        downloads: list[str] = []

        class Primary:
            @staticmethod
            def load_settings():
                return {"primary": "token"}

            @staticmethod
            def search(_key, _video, _language):
                return None, [primary], None

            @staticmethod
            def download(_key, candidate, destination):
                downloads.append(candidate.file_id)
                path = Path(destination) / "primary.srt"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("subtitle", encoding="utf-8")
                return path

        class Secondary:
            @staticmethod
            def load_settings():
                return {"secondary": "token"}

            @staticmethod
            def search(_key, _video, _language):
                return None, [duplicate], None

            @staticmethod
            def download(_key, candidate, destination):
                downloads.append(candidate.file_id)
                raise AssertionError("重复元数据候选不应下载")

        providers = (
            ("OpenSubtitles", Primary, "primary"),
            ("SubDL", Secondary, "secondary"),
        )
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "Movie.2025.mkv"
            video.write_bytes(b"video")
            verified = root / "verified.srt"
            verified.write_text("verified", encoding="utf-8")
            with patch.object(smart_subtitles, "PROVIDERS", providers), patch.object(
                smart_subtitles.pro_core, "preflight_online_subtitle", return_value=(verified, "验证通过")
            ):
                smart_subtitles.find_verified_english(str(video), 0, lambda _message: None)

        self.assertEqual(downloads, ["primary"])

    def test_opensubtitles_precedes_higher_scored_subdl_and_subdl_fills_failure(self) -> None:
        primary = SimpleNamespace(
            release="Movie OpenSubtitles", file_name="open.srt", feature_title="Movie",
            feature_year="2025", language="en", identity_verified=True, identity_key="imdb:1", file_id="open",
            trusted=False, downloads=1,
        )
        secondary = SimpleNamespace(
            release="Movie SubDL", file_name="subdl.srt", feature_title="Movie",
            feature_year="2025", language="en", identity_verified=True, identity_key="imdb:1", file_id="subdl",
            trusted=True, downloads=999,
        )
        downloads: list[str] = []

        class Primary:
            @staticmethod
            def load_settings():
                return {"primary": "token"}

            @staticmethod
            def search(_key, _video, _language):
                return None, [primary], None

            @staticmethod
            def download(_key, candidate, destination):
                downloads.append(candidate.file_id)
                raise RuntimeError("primary download failed")

        class Secondary:
            @staticmethod
            def load_settings():
                return {"secondary": "token"}

            @staticmethod
            def search(_key, _video, _language):
                return None, [secondary], None

            @staticmethod
            def download(_key, candidate, destination):
                downloads.append(candidate.file_id)
                path = Path(destination) / "subdl.srt"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("SubDL content", encoding="utf-8")
                return path

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "Movie.2025.mkv"
            video.write_bytes(b"video")
            verified = root / "verified.srt"
            verified.write_text("verified", encoding="utf-8")
            providers = (
                ("OpenSubtitles", Primary, "primary"),
                ("SubDL", Secondary, "secondary"),
            )
            with patch.object(smart_subtitles, "PROVIDERS", providers), patch.object(
                smart_subtitles.pro_core,
                "preflight_online_subtitle",
                return_value=(verified, "验证通过"),
            ):
                result = smart_subtitles.find_verified_english(
                    str(video), 0, lambda _message: None, max_candidates=1
                )

        self.assertEqual(downloads, ["open", "subdl"])
        self.assertEqual(result.provider, "SubDL")

    def test_commentary_subtitle_is_rejected_from_opening_content(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            subtitle = Path(folder) / "commentary.srt"
            blocks = []
            for index in range(1, 25):
                text = (
                    "Hello everybody at home. My name is Dean, writer, director "
                    "and executive producer."
                    if index == 1
                    else f"Behind the scenes discussion {index}."
                )
                blocks.append(
                    f"{index}\n00:00:{index:02d},000 --> 00:00:{index:02d},900\n{text}\n"
                )
            subtitle.write_text("\n".join(blocks), encoding="utf-8")

            conflict = smart_subtitles.subtitle_content_conflict(subtitle, "en")

        self.assertIn("评论轨", conflict)

    def test_identical_downloaded_content_is_not_verified_twice(self) -> None:
        first = SimpleNamespace(
            release="first", file_name="first.srt", feature_title="", language="en",
            identity_verified=True, identity_key="imdb:1", file_id="1",
        )
        second = SimpleNamespace(
            release="second", file_name="second.srt", feature_title="", language="en",
            identity_verified=True, identity_key="imdb:1", file_id="2",
        )

        class Service:
            @staticmethod
            def load_settings():
                return {"key": "token"}

            @staticmethod
            def search(_key, _video, _language):
                return None, [first, second], None

            @staticmethod
            def download(_key, candidate, destination):
                path = Path(destination) / f"{candidate.release}.srt"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("same subtitle", encoding="utf-8")
                return path

        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "movie.mkv"
            video.write_bytes(b"video")
            with patch.object(smart_subtitles, "PROVIDERS", (("Test", Service, "key"),)), patch.object(
                smart_subtitles.pro_core,
                "preflight_online_subtitle",
                side_effect=RuntimeError("bad timing"),
            ) as verify:
                with self.assertRaisesRegex(RuntimeError, "已检验 1 条"):
                    smart_subtitles.find_verified_english(str(video), 0, lambda _message: None)

        self.assertEqual(verify.call_count, 1)

    def test_deadline_cancel_combines_user_cancel_and_time_budget(self) -> None:
        cancel = SimpleNamespace(is_set=lambda: False)
        with patch.object(smart_subtitles.time, "monotonic", return_value=20.0):
            deadline = smart_subtitles._DeadlineCancel(cancel, 19.0)
            self.assertTrue(deadline.is_set())
            self.assertTrue(deadline.timed_out)

    def test_excluded_manual_candidate_is_skipped(self) -> None:
        first = SimpleNamespace(
            release="first", file_name="first.srt", feature_title="", language="en",
            identity_verified=True, identity_key="imdb:1", file_id="1",
        )
        second = SimpleNamespace(
            release="second", file_name="second.srt", feature_title="", language="en",
            identity_verified=True, identity_key="imdb:1", file_id="2",
        )
        downloads: list[str] = []

        class Service:
            @staticmethod
            def load_settings():
                return {"key": "token"}

            @staticmethod
            def search(_key, _video, _language):
                return None, [first, second], None

            @staticmethod
            def download(_key, candidate, destination):
                downloads.append(candidate.release)
                path = Path(destination) / f"{candidate.release}.srt"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"subtitle {candidate.release}", encoding="utf-8")
                return path

        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "movie.mkv"
            video.write_bytes(b"video")
            aligned = Path(folder) / "verified.srt"
            aligned.write_text("verified", encoding="utf-8")
            with patch.object(smart_subtitles, "PROVIDERS", (("Test", Service, "key"),)), patch.object(
                smart_subtitles.pro_core,
                "preflight_online_subtitle",
                return_value=(aligned, "验证通过"),
            ):
                result = smart_subtitles.find_verified_english(
                    str(video),
                    0,
                    lambda _message: None,
                    max_candidates=3,
                    excluded_candidate_keys={smart_subtitles.candidate_key("Test", first)},
                )

        self.assertEqual(downloads, ["second"])
        self.assertIs(result.candidate, second)

    def test_candidates_are_verified_in_order_until_one_passes(self) -> None:
        first = SimpleNamespace(
            release="first", file_name="first.srt", feature_title="", language="en",
            identity_verified=True, identity_key="imdb:1",
        )
        second = SimpleNamespace(
            release="second", file_name="second.srt", feature_title="", language="en",
            identity_verified=True, identity_key="imdb:1",
        )

        class Service:
            @staticmethod
            def load_settings():
                return {"key": "token"}

            @staticmethod
            def search(_key, _video, _language):
                return None, [first, second], None

            @staticmethod
            def download(_key, candidate, destination):
                path = Path(destination) / f"{candidate.release}.srt"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"subtitle {candidate.release}", encoding="utf-8")
                return path

        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "movie.mkv"
            video.write_bytes(b"video")
            aligned = Path(folder) / "verified.srt"
            aligned.write_text("verified", encoding="utf-8")
            with patch.object(smart_subtitles, "PROVIDERS", (("Test", Service, "key"),)), patch.object(
                smart_subtitles.pro_core,
                "preflight_online_subtitle",
                side_effect=[RuntimeError("bad timing"), (aligned, "验证通过")],
            ) as verify:
                result = smart_subtitles.find_verified_english(str(video), 0, lambda _message: None)

        self.assertEqual(verify.call_count, 2)
        self.assertEqual(result.subtitle_path, str(aligned))
        self.assertIs(result.candidate, second)

    def test_online_search_does_not_run_ffsubsync_candidate_scoring(self) -> None:
        first = SimpleNamespace(
            release="wrong sequel", file_name="wrong.srt", feature_title="", language="en",
            identity_verified=True, identity_key="imdb:1",
        )
        second = SimpleNamespace(
            release="correct movie", file_name="correct.srt", feature_title="", language="en",
            identity_verified=True, identity_key="imdb:1",
        )

        class Service:
            @staticmethod
            def load_settings():
                return {"key": "token"}

            @staticmethod
            def search(_key, _video, _language):
                return None, [first, second], None

            @staticmethod
            def download(_key, candidate, destination):
                path = Path(destination) / f"{candidate.release}.srt"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"subtitle {candidate.release}", encoding="utf-8")
                return path

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "movie.mkv"
            video.write_bytes(b"video")
            verified = root / "verified.srt"
            verified.write_text("verified", encoding="utf-8")
            shared_content = SimpleNamespace(fingerprint=object())
            verified_candidates: list[str] = []

            def verify(_video, subtitle, *_args, **_kwargs):
                verified_candidates.append(Path(subtitle).read_text(encoding="utf-8"))
                return verified, "验证通过"

            with patch.object(smart_subtitles, "PROVIDERS", (("Test", Service, "key"),)), patch.object(
                smart_subtitles.pro_core,
                "prepare_shared_subtitle_content_audio",
                return_value=shared_content,
            ), patch.object(
                smart_subtitles.pro_core,
                "preflight_online_subtitle",
                side_effect=verify,
            ) as preflight:
                result = smart_subtitles.find_verified_english(
                    str(video),
                    0,
                    lambda _message: None,
                )

        self.assertEqual(preflight.call_count, 1)
        self.assertIs(result.candidate, first)
        self.assertEqual(verified_candidates, ["subtitle wrong sequel"])

    def test_preflight_timeout_moves_to_next_candidate(self) -> None:
        first = SimpleNamespace(
            release="first", file_name="first.srt", feature_title="Movie", feature_year="2025",
            language="en", identity_verified=True, identity_key="imdb:1", file_id="1",
        )
        second = SimpleNamespace(
            release="second", file_name="second.srt", feature_title="Movie", feature_year="2025",
            language="en", identity_verified=True, identity_key="imdb:1", file_id="2",
        )

        class Service:
            @staticmethod
            def load_settings():
                return {"key": "token"}

            @staticmethod
            def search(_key, _video, _language):
                return None, [first, second], None

            @staticmethod
            def download(_key, candidate, destination):
                path = Path(destination) / f"{candidate.release}.srt"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"subtitle {candidate.release}", encoding="utf-8")
                return path

        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "Movie.2025.mkv"
            video.write_bytes(b"video")
            aligned = Path(folder) / "verified.srt"
            aligned.write_text("verified", encoding="utf-8")
            budgets: list[float] = []
            outcomes = iter(
                [
                    smart_subtitles.pro_core.SubtitlePreflightTimeoutError("candidate timeout"),
                    (aligned, "验证通过"),
                ]
            )

            def verify(*_args, **kwargs):
                budgets.append(float(kwargs["time_budget_seconds"]))
                outcome = next(outcomes)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

            with patch.object(smart_subtitles, "PROVIDERS", (("Test", Service, "key"),)), patch.object(
                smart_subtitles.pro_core,
                "preflight_online_subtitle",
                side_effect=verify,
            ) as verify:
                result = smart_subtitles.find_verified_english(str(video), 0, lambda _message: None)

            self.assertEqual(verify.call_count, 2)
            self.assertIs(result.candidate, second)
            self.assertEqual(budgets, [20.0, 20.0])

    def test_missing_api_keys_requires_manual_setup(self) -> None:
        class Service:
            @staticmethod
            def load_settings():
                return {}

        with patch.object(smart_subtitles, "PROVIDERS", (("Test", Service, "key"),)):
            with self.assertRaisesRegex(RuntimeError, "手动字幕"):
                smart_subtitles.find_verified_english("movie.mkv", None, lambda _message: None)

    def test_candidate_scores_do_not_skip_primary_provider_result(self) -> None:
        rejected = SimpleNamespace(
            release="Misty Creed",
            file_name="Misty.Creed.srt",
            feature_title="Misty Creed",
            language="en",
            identity_verified=False,
            identity_key="",
        )
        accepted = SimpleNamespace(
            release="Creed III BluRay",
            file_name="Creed.III.BluRay.srt",
            feature_title="Creed III",
            language="en",
            identity_verified=True,
            identity_key="imdb:11145118",
        )
        downloads: list[str] = []

        class Primary:
            @staticmethod
            def load_settings():
                return {"primary": "token"}

            @staticmethod
            def search(_key, _video, _language):
                return None, [rejected], None

            @staticmethod
            def download(_key, candidate, destination):
                downloads.append(candidate.release)
                path = Path(destination) / "misty.srt"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("subtitle", encoding="utf-8")
                return path

        class Secondary:
            @staticmethod
            def load_settings():
                return {"secondary": "token"}

            @staticmethod
            def search(_key, _video, _language):
                return None, [accepted], None

            @staticmethod
            def download(_key, candidate, destination):
                downloads.append(candidate.release)
                path = Path(destination) / "creed.srt"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("subtitle", encoding="utf-8")
                return path

        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "Creed.III.2023.mkv"
            video.write_bytes(b"video")
            aligned = Path(folder) / "verified.srt"
            aligned.write_text("verified", encoding="utf-8")
            providers = (
                ("OpenSubtitles", Primary, "primary"),
                ("SubDL", Secondary, "secondary"),
            )
            with patch.object(smart_subtitles, "PROVIDERS", providers), patch.object(
                smart_subtitles.pro_core,
                "preflight_online_subtitle",
                return_value=(aligned, "验证通过"),
            ):
                result = smart_subtitles.find_verified_english(
                    str(video),
                    0,
                    lambda _message: None,
                )

        self.assertEqual(downloads, ["Misty Creed"])
        self.assertEqual(result.provider, "OpenSubtitles")
        self.assertIs(result.candidate, rejected)

    def test_commentary_candidate_is_excluded_as_non_main_subtitle(self) -> None:
        commentary = SimpleNamespace(
            release="Movie.2025.commentary",
            file_name="Movie.2025.commentary.srt",
            feature_title="Movie",
            language="en",
            identity_key="imdb:1",
            file_id="commentary",
        )
        normal = SimpleNamespace(
            release="Movie.2025.BluRay",
            file_name="Movie.2025.BluRay.srt",
            feature_title="Movie",
            language="en",
            identity_key="imdb:1",
            file_id="normal",
        )
        downloads: list[str] = []

        class Service:
            @staticmethod
            def load_settings():
                return {"key": "token"}

            @staticmethod
            def search(_key, _video, _language):
                return None, [commentary, normal], None

            @staticmethod
            def download(_key, candidate, destination):
                downloads.append(candidate.release)
                path = Path(destination) / candidate.file_name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("subtitle", encoding="utf-8")
                return path

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "Movie.2025.mkv"
            video.write_bytes(b"video")
            verified = root / "verified.srt"
            verified.write_text("verified", encoding="utf-8")
            with patch.object(
                smart_subtitles, "PROVIDERS", (("OpenSubtitles", Service, "key"),)
            ), patch.object(
                smart_subtitles.pro_core,
                "preflight_online_subtitle",
                return_value=(verified, "验证通过"),
            ):
                result = smart_subtitles.find_verified_english(
                    str(video), 0, lambda _message: None
                )

        self.assertEqual(downloads, ["Movie.2025.BluRay"])
        self.assertIs(result.candidate, normal)

    def test_candidate_batch_size_caps_formal_candidates_per_provider(self) -> None:
        candidates = [
            SimpleNamespace(
                release=f"candidate-{index}",
                file_name=f"candidate-{index}.srt",
                feature_title="Movie",
                language="en",
                identity_verified=False,
                identity_key="",
                file_id=str(index),
            )
            for index in range(6)
        ]

        class Service:
            @staticmethod
            def load_settings():
                return {"key": "token"}

            @staticmethod
            def search(_key, _video, _language):
                return None, candidates, None

            @staticmethod
            def download(_key, candidate, destination):
                path = Path(destination) / candidate.file_name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"subtitle {candidate.release}", encoding="utf-8")
                return path

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "Movie.2025.mkv"
            video.write_bytes(b"video")
            verified = root / "verified.srt"
            verified.write_text("verified", encoding="utf-8")
            failures = [RuntimeError(f"candidate {index} rejected") for index in range(5)]
            outcomes = [*failures, (verified, "验证通过")]
            with patch.object(
                smart_subtitles, "PROVIDERS", (("OpenSubtitles", Service, "key"),)
            ), patch.object(
                smart_subtitles.pro_core,
                "preflight_online_subtitle",
                side_effect=outcomes,
            ) as preflight:
                with self.assertRaises(RuntimeError):
                    smart_subtitles.find_verified_english(
                        str(video),
                        0,
                        lambda _message: None,
                        max_candidates=5,
                    )

        self.assertEqual(preflight.call_count, 5)


    def test_candidate_pool_downloads_before_local_verification(self) -> None:
        first = SimpleNamespace(
            release="first", file_name="first.srt", feature_title="", language="en",
            identity_verified=True, identity_key="imdb:1", file_id="1",
        )
        second = SimpleNamespace(
            release="second", file_name="second.srt", feature_title="", language="en",
            identity_verified=True, identity_key="imdb:1", file_id="2",
        )
        events: list[str] = []

        class Service:
            @staticmethod
            def load_settings():
                return {"key": "token"}

            @staticmethod
            def search(_key, _video, _language):
                return None, [first, second], None

            @staticmethod
            def download(_key, candidate, destination):
                events.append(f"download-{candidate.release}")
                path = Path(destination) / f"{candidate.release}.srt"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    f"1\n00:00:01,000 --> 00:00:02,000\n{candidate.release}\n",
                    encoding="utf-8",
                )
                return path

        def verify(*_args, **_kwargs):
            events.append("verify")
            raise RuntimeError("bad timing")

        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "movie.mkv"
            video.write_bytes(b"video")
            with patch.object(smart_subtitles, "PROVIDERS", (("Test", Service, "key"),)), patch.object(
                smart_subtitles.pro_core, "preflight_online_subtitle", side_effect=verify
            ):
                with self.assertRaisesRegex(RuntimeError, "已检验 2 条"):
                    smart_subtitles.find_verified_english(str(video), 0, lambda _message: None)

        self.assertIn("download-first", events)
        self.assertIn("download-second", events)
        self.assertGreater(events.index("verify"), events.index("download-first"))
        self.assertGreater(events.index("verify"), events.index("download-second"))

    def test_embedded_pgs_never_reorders_download_candidates(self) -> None:
        first = SimpleNamespace(
            release="metadata-first", file_name="first.srt", feature_title="", language="en",
            identity_verified=True, identity_key="imdb:1", file_id="1",
            trusted=False, downloads=1,
        )
        second = SimpleNamespace(
            release="metadata-second", file_name="second.srt", feature_title="", language="en",
            identity_verified=True, identity_key="imdb:1", file_id="2",
            trusted=False, downloads=1,
        )

        class Service:
            @staticmethod
            def load_settings():
                return {"key": "token"}

            @staticmethod
            def search(_key, _video, _language):
                return None, [first, second], None

            @staticmethod
            def download(_key, candidate, destination):
                path = Path(destination) / f"{candidate.release}.srt"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    f"1\n00:00:01,000 --> 00:00:02,000\n{candidate.release}\n",
                    encoding="utf-8",
                )
                return path

        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "movie.mkv"
            video.write_bytes(b"video")
            with patch.object(smart_subtitles, "PROVIDERS", (("Test", Service, "key"),)), patch.object(
                smart_subtitles.pro_core, "preflight_online_subtitle"
            ) as verify:
                verified = Path(folder) / "verified.srt"
                verified.write_text("verified", encoding="utf-8")
                verify.return_value = (verified, "独立音频核验通过")
                result = smart_subtitles.find_verified_english(str(video), 0, lambda _message: None)

        self.assertIs(result.candidate, first)
        self.assertEqual(result.report, "独立音频核验通过")
        verify.assert_called_once()
        self.assertEqual(verify.call_args.kwargs["candidate_label"], "Test候选")
    def test_similar_dialogue_with_different_timeline_keeps_provider_order(self) -> None:
        candidates = [SimpleNamespace(
            release=f"Movie.2025.BluRay.VERSION-{index}",
            file_name=f"Movie.2025.BluRay.VERSION-{index}.srt",
            feature_title="Movie", language="en", identity_verified=True,
            identity_key="imdb:1", file_id=str(index),
        ) for index in range(5)]
        checked: list[int] = []
        downloaded: list[int] = []

        class Service:
            @staticmethod
            def load_settings():
                return {"key": "token"}

            @staticmethod
            def search(_key, _video, _language):
                return None, candidates, None

            @staticmethod
            def download(_key, candidate, destination):
                index = int(candidate.file_id)
                downloaded.append(index)
                path = Path(destination) / f"{index}.srt"
                path.parent.mkdir(parents=True, exist_ok=True)
                variant = "bright" if index < 3 else "strange"
                shift = index if index < 3 else 0
                path.write_text("\n\n".join(
                    f"{number + 1}\n00:01:{number + shift:02d},000 --> "
                    f"00:01:{number + shift + 1:02d},000\n"
                    f"the {variant} {chr(97 + number // 26)}{chr(97 + number % 26)} planet is here"
                    for number in range(30)
                ), encoding="utf-8")
                return path

        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "Movie.2025.mkv"
            video.write_bytes(b"video")
            verified = Path(folder) / "verified.srt"
            verified.write_text("verified", encoding="utf-8")

            def verify(_video, subtitle, *_args, **_kwargs):
                index = int(Path(subtitle).stem)
                checked.append(index)
                if index != 1:
                    raise RuntimeError("candidate did not verify")
                return verified, "verified independently"

            with patch.object(smart_subtitles, "PROVIDERS", (("Test", Service, "key"),)), \
                 patch.object(smart_subtitles.pro_core, "prepare_shared_subtitle_content_audio",
                              return_value=SimpleNamespace(fingerprint=object())), \
                 patch.object(smart_subtitles.pro_core, "preflight_online_subtitle",
                              side_effect=verify):
                result = smart_subtitles.find_verified_english(
                    str(video), 0, lambda _message: None, max_candidates=3,
                )
        self.assertEqual(checked, [0, 1])
        self.assertEqual(set(downloaded), {0, 1})
        self.assertIs(result.candidate, candidates[1])


if __name__ == "__main__":
    unittest.main()
