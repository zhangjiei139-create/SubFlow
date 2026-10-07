# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

import batch_core
import subtitle_tool_core as core
from profile_model import BatchPlan, PreferenceProfile
from subtitle_languages import subtitle_language


class ChineseSubtitleMetadataTest(unittest.TestCase):
    def setUp(self):
        self.audio = core.Track(1, "audio", "DTS-HD Master Audio", "eng", "English", True, False, False, False, False)
        self.cantonese = core.Track(8, "subtitles", "HDMV PGS", "chi", "Cantonese", False, False, False, True, False)
        self.mandarin = core.Track(9, "subtitles", "HDMV PGS", "chi", "Mandarin Traditional", False, False, False, True, False)

    def analyze(self, subtitles, *, equivalent=False, target="zh-CN"):
        profile = PreferenceProfile(1, "test", subtitle_languages=[target], chinese_script_equivalent=equivalent)
        with patch.object(batch_core, "_inspect_media_cached", return_value=({}, False)), patch.object(
            batch_core.core, "tracks_from_media", return_value=[self.audio, *subtitles]
        ):
            return batch_core.analyze_video("movie.mkv", profile)

    def test_broad_chinese_tag_is_not_a_script_or_mandarin_proof(self):
        self.assertEqual(subtitle_language("chi"), "zh")
        self.assertEqual(subtitle_language("zho", "Chinese"), "zh")
        self.assertEqual(subtitle_language("chi", "Mandarin Traditional"), "zh-TW")
        self.assertEqual(subtitle_language("chi", "Mandarin Simplified"), "zh-CN")
        self.assertEqual(subtitle_language("chi", "Cantonese"), "yue")
        self.assertEqual(subtitle_language("zh-Hant", "Cantonese"), "yue")

    def test_explicit_script_metadata_wins_over_conflicting_title(self):
        self.assertEqual(subtitle_language("zh-Hans", "Traditional Chinese"), "zh-CN")
        self.assertEqual(subtitle_language("zh-Hant", "Simplified Chinese"), "zh-TW")
        self.assertEqual(subtitle_language("cmn-Hant-TW", "Mandarin"), "zh-TW")

    def test_new_hope_prefers_mandarin_traditional_over_cantonese(self):
        plan = self.analyze([self.cantonese, self.mandarin], equivalent=True)
        self.assertEqual(plan.subtitle_ids, [9])
        self.assertEqual(plan.missing_subtitle_languages, [])
        self.assertIn("繁体中文", plan.summary)
        self.assertNotIn("粤语字幕", plan.summary)

    def test_script_equivalence_does_not_let_cantonese_satisfy_chinese(self):
        plan = self.analyze([self.cantonese], equivalent=True)
        self.assertEqual(plan.subtitle_ids, [])
        self.assertEqual(plan.missing_subtitle_languages, ["zh-CN"])

    def test_strict_script_does_not_keep_traditional_as_simplified(self):
        plan = self.analyze([self.cantonese, self.mandarin])
        self.assertEqual(plan.subtitle_ids, [])
        self.assertEqual(plan.missing_subtitle_languages, ["zh-CN"])
        self.assertEqual(plan.local_chinese_conversion_sources, {})

    def test_unmarked_script_is_a_labelled_fallback_without_forced_translation(self):
        unknown = core.Track(5, "subtitles", "HDMV PGS", "chi", "Chinese", False, False, False, True, False)
        strict = self.analyze([unknown])
        self.assertEqual(strict.subtitle_ids, [5])
        self.assertEqual(strict.missing_subtitle_languages, [])
        self.assertIn("中文（简繁未标记）", strict.summary)
        compatible = self.analyze([unknown], equivalent=True)
        self.assertEqual(compatible.subtitle_ids, [5])
        self.assertEqual(compatible.missing_subtitle_languages, [])
        self.assertIn("中文（简繁未标记）", compatible.summary)

    def test_unmarked_chinese_cannot_displace_known_mandarin_with_equivalence(self):
        unknown = core.Track(5, "subtitles", "HDMV PGS", "chi", "Chinese", True, False, False, True, False)
        plan = self.analyze([unknown, self.cantonese, self.mandarin], equivalent=True)
        self.assertEqual(plan.subtitle_ids, [9])

    def test_named_mandarin_wins_equal_script_default_generic_chinese(self):
        generic = core.Track(7, "subtitles", "HDMV PGS", "zh-TW", "Chinese", True, False, False, True, False)
        plan = self.analyze([generic, self.mandarin], target="zh-TW")
        self.assertEqual(plan.subtitle_ids, [9])

    def test_exact_script_is_kept_when_both_mandarin_scripts_exist(self):
        simplified = core.Track(10, "subtitles", "HDMV PGS", "chi", "Mandarin Simplified", False, False, False, True, False)
        plan = self.analyze([self.cantonese, self.mandarin, simplified], equivalent=True)
        self.assertEqual(plan.subtitle_ids, [10])

    def test_local_text_conversion_uses_named_mandarin_script(self):
        traditional = core.Track(9, "subtitles", "S_TEXT/UTF8", "chi", "Mandarin Traditional", False, False, True, False, False)
        cantonese = core.Track(8, "subtitles", "S_TEXT/UTF8", "chi", "Cantonese", False, False, True, False, False)
        plan = self.analyze([cantonese, traditional])
        self.assertEqual(plan.subtitle_ids, [])
        self.assertEqual(plan.local_chinese_conversion_sources, {"zh-CN": 9})
        self.assertEqual(plan.source_mode, "none")

    def test_formal_recheck_preserves_explicit_valid_cantonese_track(self):
        # The processing recheck must not replace a valid saved/selected ID.
        profile = PreferenceProfile(1, "test", subtitle_languages=["zh-CN"], chinese_script_equivalent=True)
        plan = BatchPlan("movie.mkv", 1, subtitle_ids=[8])
        batch_core._refresh_incomplete_subtitle_selections(
            plan, profile, [self.audio, self.cantonese, self.mandarin], 7482.2, lambda _message: None
        )
        self.assertEqual(plan.subtitle_ids, [8])

    def test_explicit_chinese_download_replaces_unmarked_track_and_restores_missing_target(self):
        unknown = core.Track(5, "subtitles", "S_TEXT/UTF8", "chi", "Chinese", True, False, True, False, False)
        profile = PreferenceProfile(
            1, "test", subtitle_languages=["zh-CN", "zh-TW"],
            replace_downloaded_subtitle=True, audio_policy="native",
        )
        with tempfile.TemporaryDirectory() as folder:
            movie = Path(folder) / "movie.mkv"
            movie.write_bytes(b"movie")
            downloaded = Path(folder) / "downloaded.srt"
            downloaded.write_text("confirmed", encoding="utf-8")
            with patch.object(batch_core, "_inspect_media_cached", return_value=({}, False)), patch.object(
                batch_core.core, "tracks_from_media", return_value=[self.audio, unknown]
            ):
                plan = batch_core.analyze_video(
                    str(movie), profile, str(downloaded), "zh-CN", True,
                    external_subtitle_origin="download",
                )
            self.assertEqual(plan.subtitle_ids, [])
            self.assertEqual(plan.missing_subtitle_languages, ["zh-TW"])
            self.assertEqual(plan.local_chinese_conversion_sources, {})
            with patch.object(batch_core.core, "inspect_tracks", return_value=[self.audio, unknown]), patch.object(
                batch_core.pro_core, "process_pro"
            ) as process, patch.object(batch_core.core, "begin_ollama_lease"), patch.object(
                batch_core.core, "end_ollama_lease", return_value=False
            ):
                batch_core.process_plan(plan, profile, lambda _message: None, None)
            kwargs = process.call_args.kwargs
            self.assertEqual(kwargs["keep_subtitle_ids"], [])
            self.assertEqual(kwargs["target_codes"], ["zh-TW"])
            self.assertEqual(kwargs["local_chinese_conversion_sources"], {})
            self.assertTrue(kwargs["include_external_source_in_output"])

    def test_chinese_download_does_not_replace_known_other_script_or_cantonese(self):
        self.assertFalse(batch_core._subtitle_matches_download(self.mandarin, "zh-CN"))
        self.assertFalse(batch_core._subtitle_matches_download(self.cantonese, "zh-CN"))

    def test_downloaded_chinese_respects_script_equivalence_after_replacement(self):
        unknown = core.Track(5, "subtitles", "S_TEXT/UTF8", "chi", "Chinese", True, False, True, False, False)
        profile = PreferenceProfile(
            1, "test", subtitle_languages=["zh-CN", "zh-TW"],
            replace_downloaded_subtitle=True, chinese_script_equivalent=True,
            audio_policy="native",
        )
        with tempfile.TemporaryDirectory() as folder:
            movie = Path(folder) / "movie.mkv"
            movie.write_bytes(b"movie")
            downloaded = Path(folder) / "downloaded.srt"
            downloaded.write_text("confirmed", encoding="utf-8")
            with patch.object(batch_core, "_inspect_media_cached", return_value=({}, False)), patch.object(
                batch_core.core, "tracks_from_media", return_value=[self.audio, unknown]
            ):
                plan = batch_core.analyze_video(
                    str(movie), profile, str(downloaded), "zh-CN", True,
                    external_subtitle_origin="download",
                )
            self.assertEqual(plan.subtitle_ids, [])
            self.assertEqual(plan.missing_subtitle_languages, [])
            with patch.object(batch_core.core, "inspect_tracks", return_value=[self.audio, unknown]), patch.object(
                batch_core.pro_core, "process_pro"
            ) as process, patch.object(batch_core.core, "begin_ollama_lease") as ai_lease:
                batch_core.process_plan(plan, profile, lambda _message: None, None)
            kwargs = process.call_args.kwargs
            self.assertEqual(kwargs["keep_subtitle_ids"], [])
            self.assertEqual(kwargs["target_codes"], [])
            self.assertTrue(kwargs["include_external_source_in_output"])
            ai_lease.assert_not_called()


if __name__ == "__main__":
    unittest.main()
