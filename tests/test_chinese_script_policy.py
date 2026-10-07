# -*- coding: utf-8 -*-
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import batch_core
import chinese_script_converter
import subtitle_tool_core as core
from profile_model import BatchPlan, PreferenceProfile


class ChineseSubtitlePolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.audio = core.Track(0, "audio", "AAC", "eng", "", True, False, False, False, False)
        self.traditional = core.Track(1, "subtitles", "S_TEXT/UTF8", "zh-TW", "繁体中文", False, False, True, False, False)
        self.simplified = core.Track(2, "subtitles", "S_TEXT/UTF8", "zh-CN", "简体中文", False, False, True, False, False)
        self.english = core.Track(3, "subtitles", "S_TEXT/UTF8", "eng", "English", True, False, True, False, False)

    def analyze(self, tracks, profile):
        with patch.object(batch_core, "_inspect_media_cached", return_value=({}, False)), patch.object(
            batch_core.core, "tracks_from_media", return_value=tracks
        ):
            return batch_core.analyze_video("movie.mkv", profile)

    def test_equivalent_accepts_only_available_traditional_for_simplified(self) -> None:
        profile = PreferenceProfile(
            1,
            "test",
            subtitle_languages=["zh-CN"],
            chinese_script_equivalent=True,
        )
        plan = self.analyze([self.audio, self.traditional], profile)
        self.assertEqual(plan.subtitle_ids, [self.traditional.id])
        self.assertEqual(plan.missing_subtitle_languages, [])
        self.assertEqual(plan.source_mode, "none")
        self.assertEqual(plan.local_chinese_conversion_sources, {})

    def test_equivalent_keeps_exact_selection_when_both_exist(self) -> None:
        profile = PreferenceProfile(
            1,
            "test",
            subtitle_languages=["zh-CN"],
            chinese_script_equivalent=True,
        )
        plan = self.analyze([self.audio, self.traditional, self.simplified], profile)
        self.assertEqual(plan.subtitle_ids, [self.simplified.id])
        self.assertEqual(plan.missing_subtitle_languages, [])

    def test_strict_mode_records_separate_local_conversion_source(self) -> None:
        profile = PreferenceProfile(1, "test", subtitle_languages=["zh-CN"])
        plan = self.analyze([self.audio, self.traditional, self.english], profile)
        self.assertEqual(plan.subtitle_ids, [])
        self.assertEqual(plan.missing_subtitle_languages, ["zh-CN"])
        self.assertEqual(plan.source_mode, "none")
        self.assertIsNone(plan.source_subtitle_id)
        self.assertEqual(plan.local_chinese_conversion_sources, {"zh-CN": self.traditional.id})

    def test_local_conversion_does_not_replace_english_model_source(self) -> None:
        profile = PreferenceProfile(1, "test", subtitle_languages=["zh-CN", "de"])
        plan = self.analyze([self.audio, self.traditional, self.english], profile)
        self.assertEqual(plan.missing_subtitle_languages, ["zh-CN", "de"])
        self.assertEqual(plan.local_chinese_conversion_sources, {"zh-CN": self.traditional.id})
        self.assertEqual(plan.source_mode, "embedded")
        self.assertEqual(plan.source_subtitle_id, self.english.id)

    def test_process_plan_passes_local_conversion_without_ai_target(self) -> None:
        profile = PreferenceProfile(1, "test", subtitle_languages=["zh-CN"], audio_policy="native")
        with tempfile.TemporaryDirectory() as folder:
            movie = Path(folder) / "movie.mkv"
            movie.write_bytes(b"test")
            output = Path(folder) / "movie.SF.mkv"
            plan = BatchPlan(
                str(movie),
                1,
                audio_ids=[self.audio.id],
                missing_subtitle_languages=["zh-CN"],
                local_chinese_conversion_sources={"zh-CN": self.traditional.id},
                source_mode="none",
                status="ready",
                status_label="可以处理",
                output_path=str(output),
            )
            def verified(*args, verification_status, **kwargs):
                verification_status['verified_anchor'] = True
            with patch.object(batch_core.pro_core, 'prepare_shared_subtitle_content_audio',
                return_value=SimpleNamespace(vad_reference=Path(folder)/'speech.npz', validation_audio_id=0)), patch.object(
                batch_core.pro_core, 'prepare_embedded_text_corrections', side_effect=verified), patch.object(
                batch_core.core,
                "inspect_tracks",
                return_value=[self.audio, self.traditional],
            ), patch.object(
                batch_core.pro_core,
                "process_pro",
                return_value=str(output),
            ) as process_pro, patch.object(
                batch_core.core,
                "begin_ollama_lease",
            ) as begin_lease:
                result = batch_core.process_plan(
                    plan,
                    profile,
                    lambda _message: None,
                    threading.Event(),
                )
            self.assertEqual(result, str(output))
            begin_lease.assert_not_called()
            kwargs = process_pro.call_args.kwargs
            self.assertEqual(kwargs["target_codes"], [])
            self.assertEqual(
                kwargs["local_chinese_conversion_sources"],
                {"zh-CN": self.traditional.id},
            )

    def test_local_conversion_helper_preserves_timeline_and_converts_text(self) -> None:
        import pro_core

        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "traditional.srt"
            source.write_text(
                "1\n00:00:01,000 --> 00:00:03,000\n後臺軟體發展\n",
                encoding="utf-8",
            )
            with patch.object(pro_core.legacy, "inspect_tracks", return_value=[self.traditional]):
                generated = pro_core._prepare_local_chinese_conversions(
                    "movie.mkv",
                    {"zh-CN": self.traditional.id},
                    folder,
                    {self.traditional.id: source},
                    {},
                    lambda _message: None,
                    threading.Event(),
                )
            self.assertEqual(len(generated), 1)
            output, code, _name, _default = generated[0]
            self.assertEqual(code, "zh-CN")
            converted = core.parse_subtitle(output)
            self.assertEqual(converted[0].start, "00:00:01,000")
            self.assertEqual(converted[0].end, "00:00:03,000")
            self.assertEqual(converted[0].text, "后台软件发展")

    def test_preference_defaults_to_strict_script_matching(self) -> None:
        self.assertFalse(PreferenceProfile(1, "test").chinese_script_equivalent)


class _FakeSubtitle:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeSubtitleCollection(list):
    def save(self, path: str, encoding: str = "utf-8") -> None:
        Path(path).write_text("\n".join(item.text for item in self), encoding=encoding)


class ChineseScriptConverterTest(unittest.TestCase):
    def test_phrase_conversion_preserves_source_events(self) -> None:
        source = _FakeSubtitleCollection([_FakeSubtitle("後臺軟體發展")])
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "converted.srt"
            chinese_script_converter.convert_subtitle_events(source, output, "zh-TW", "zh-CN")
            self.assertEqual(output.read_text(encoding="utf-8"), "后台软件发展")
        self.assertEqual(source[0].text, "後臺軟體發展")

    def test_simplified_to_traditional_uses_phrase_dictionary(self) -> None:
        source = _FakeSubtitleCollection([_FakeSubtitle("后台软件发展")])
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "converted.srt"
            chinese_script_converter.convert_subtitle_events(source, output, "zh-CN", "zh-TW")
            self.assertEqual(output.read_text(encoding="utf-8"), "後臺軟體發展")


if __name__ == "__main__":
    unittest.main()
