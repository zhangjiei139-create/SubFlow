# -*- coding: utf-8 -*-
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import batch_core
import smart_subtitles
import subtitle_tool_core as core
from profile_model import BatchPlan, PreferenceProfile


def track(
    track_id: int,
    track_type: str,
    language: str,
    *,
    text: bool = False,
    pgs: bool = False,
    forced: bool = False,
) -> core.Track:
    return core.Track(
        track_id,
        track_type,
        "S_TEXT/UTF8" if text else ("S_HDMV/PGS" if pgs else "AAC"),
        language,
        "",
        False,
        forced,
        text,
        pgs,
        False,
        statistics_duration_seconds=3600.0,
    )


class SubtitleServiceAvailabilityTest(unittest.TestCase):
    def test_missing_all_api_keys_is_unavailable_without_network_probe(self) -> None:
        service = SimpleNamespace(load_settings=lambda: {"key": ""})
        with patch.object(smart_subtitles, "PROVIDERS", (("Test", service, "key"),)), patch(
            "smart_subtitles.socket.create_connection"
        ) as connect:
            self.assertFalse(smart_subtitles.subtitle_service_available())
        connect.assert_not_called()

    def test_one_configured_reachable_provider_is_available(self) -> None:
        service = SimpleNamespace(load_settings=lambda: {"key": "configured"})
        connection = Mock()
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)
        with patch.object(smart_subtitles, "PROVIDERS", (("OpenSubtitles", service, "key"),)), patch(
            "smart_subtitles.socket.create_connection", return_value=connection
        ):
            self.assertTrue(smart_subtitles.subtitle_service_available())


class OfflineSourcePolicyTest(unittest.TestCase):
    def test_source_priority_is_audio_language_then_english_then_any(self) -> None:
        audio = track(1, "audio", "de")
        chinese = track(2, "subtitles", "zh-CN", text=True)
        english = track(3, "subtitles", "en", text=True)
        german_image = track(4, "subtitles", "de", pgs=True)

        selected = batch_core._offline_source_track(
            [audio, chinese, english, german_image],
            audio,
            3600.0,
        )

        self.assertEqual(selected.id, 4)

    def test_no_search_process_uses_complete_non_english_source_and_never_searches(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "movie.mkv"
            video.write_bytes(b"video")
            output = root / "movie.SF.mkv"
            audio = track(1, "audio", "de")
            chinese = track(2, "subtitles", "zh-CN", text=True)
            plan = BatchPlan(
                path=str(video),
                profile_slot=1,
                audio_ids=[1],
                missing_subtitle_languages=["en"],
                status="ready",
                output_path=str(output),
            )
            profile = PreferenceProfile(1, "test", subtitle_languages=["en"])

            with patch("batch_core.core.inspect_tracks", return_value=[audio, chinese]), patch(
                "batch_core.pro_core.process_pro", return_value=str(output)
            ) as process, patch("batch_core.core.begin_ollama_lease"), patch(
                "batch_core.core.end_ollama_lease", return_value=False
            ), patch("smart_subtitles.find_verified_english") as search:
                result = batch_core.process_plan(
                    plan,
                    profile,
                    lambda _message: None,
                    None,
                    allow_online_search=False,
                )

            self.assertEqual(result, str(output))
            search.assert_not_called()
            kwargs = process.call_args.kwargs
            self.assertEqual(kwargs["source_mode"], "embedded")
            self.assertEqual(kwargs["embedded_source_id"], 2)
            self.assertEqual(kwargs["target_codes"], ["en"])
            self.assertTrue(kwargs["trust_embedded_original_timeline"])
            self.assertTrue(kwargs["allow_reverse_translation"])

    def test_no_search_process_skips_when_only_incomplete_subtitle_exists(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "movie.mkv"
            video.write_bytes(b"video")
            audio = track(1, "audio", "en")
            forced = track(2, "subtitles", "en", text=True, forced=True)
            plan = BatchPlan(
                path=str(video),
                profile_slot=1,
                audio_ids=[1],
                missing_subtitle_languages=["zh-CN"],
                status="ready",
                output_path=str(root / "movie.SF.mkv"),
            )
            profile = PreferenceProfile(1, "test", subtitle_languages=["zh-CN"])

            with patch("batch_core.core.inspect_tracks", return_value=[audio, forced]):
                with self.assertRaises(batch_core.OfflineSubtitleUnavailableError):
                    batch_core.process_plan(
                        plan,
                        profile,
                        lambda _message: None,
                        None,
                        allow_online_search=False,
                    )


if __name__ == "__main__":
    unittest.main()
