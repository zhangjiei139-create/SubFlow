from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pro_core
import subtitle_tool_core as core


def subtitle(track_id, language, name="", default=False, forced=False):
    return {
        "id": track_id, "type": "subtitles",
        "properties": {"language": language, "track_name": name,
                       "default_track": default, "forced_track": forced},
    }


class SubtitleDefaultPreferencesTest(unittest.TestCase):
    def setUp(self):
        self.media = {"tracks": [
            subtitle(4, "eng", "English", True),
            subtitle(9, "chi", "Mandarin Traditional"),
        ]}
        self.german = [(Path("german.srt"), "de", "German Auto", True)]

    def select(self, preferences, media=None, generated=None, equivalent=True):
        return core.select_default_subtitle(
            self.media if media is None else media,
            [4, 9], self.german if generated is None else generated,
            preferences, equivalent,
        )

    def test_retained_chinese_stays_default_when_only_german_is_added(self):
        self.assertEqual(self.select(["zh-CN", "en", "de"]), ("retained", 9))

    def test_actual_profile_order_can_prefer_new_german(self):
        self.assertEqual(self.select(["de", "en", "zh-CN"]), ("generated", 0))

    def test_missing_chinese_falls_back_to_retained_english_before_new_german(self):
        media = {"tracks": [subtitle(4, "eng", "English")]}
        self.assertEqual(self.select(["zh-CN", "en", "de"], media), ("retained", 4))

    def test_no_generated_subtitles_also_obeys_profile(self):
        self.assertEqual(self.select(["zh-CN", "en"], generated=[]), ("retained", 9))

    def test_native_empty_preferences_preserves_existing_default(self):
        self.assertEqual(self.select([], generated=[]), ("retained", 4))

    def test_same_script_default_prefers_named_mandarin_to_generic_chinese(self):
        media = {"tracks": [subtitle(4, "zh-TW", "Chinese", True),
                            subtitle(9, "chi", "Mandarin Traditional")]}
        self.assertEqual(self.select(["zh-TW"], media, generated=[]), ("retained", 9))

    def test_empty_preferences_preserves_generic_chinese_default(self):
        media = {"tracks": [subtitle(4, "zh-TW", "Chinese", True),
                            subtitle(9, "chi", "Mandarin Traditional")]}
        self.assertEqual(self.select([], media, generated=[]), ("retained", 4))

    def test_cantonese_does_not_satisfy_mandarin_preference(self):
        media = {"tracks": [subtitle(4, "eng"), subtitle(9, "chi", "Cantonese")]}
        self.assertEqual(self.select(["zh-CN", "en", "de"], media), ("retained", 4))

    def test_unknown_chinese_is_usable_without_claiming_simplified_script(self):
        media = {"tracks": [subtitle(4, "eng"), subtitle(9, "chi", "Chinese")]}
        self.assertEqual(self.select(["zh-CN", "en"], media, equivalent=False), ("retained", 9))

    def test_strict_script_prefers_generated_simplified_over_retained_traditional(self):
        generated = [(Path("simplified.srt"), "zh-CN", "简体中文 Converted", False)]
        self.assertEqual(self.select(["zh-CN", "en"], generated=generated, equivalent=False), ("generated", 0))

    def test_forced_chinese_is_not_default_instead_of_complete_english(self):
        media = {"tracks": [subtitle(4, "eng"), subtitle(9, "zh-CN", forced=True)]}
        self.assertEqual(self.select(["zh-CN", "en"], media), ("retained", 4))

    def test_mux_flags_default_on_retained_track_and_not_new_german(self):
        with patch.object(core, "run_command") as run, patch.object(core, "inspect_media") as inspect:
            core.mux_video(
                "movie.mkv", "out.mkv", [1, 2], [4, 9], self.german, lambda _: None,
                subtitle_language_preferences=["zh-CN", "en", "de"],
                chinese_script_equivalent=True, input_media=self.media,
                subtitle_sync_offsets={9: -150},
            )
        args = run.call_args.args[0]
        defaults = [args[i + 1] for i, value in enumerate(args) if value == "--default-track"]
        self.assertEqual(defaults, ["1:yes", "2:no", "4:no", "9:yes", "0:no"])
        self.assertIn("9:-150", args)
        inspect.assert_not_called()

    def test_legacy_call_without_preferences_preserves_explicit_flags(self):
        with patch.object(core, "run_command") as run, patch.object(core, "inspect_media") as inspect:
            core.mux_video("movie.mkv", "out.mkv", [1], [4], self.german, lambda _: None)
        args = run.call_args.args[0]
        defaults = [args[i + 1] for i, value in enumerate(args) if value == "--default-track"]
        self.assertEqual(defaults, ["1:yes", "4:no", "0:yes"])
        inspect.assert_not_called()


class DefaultPreferenceRouteTest(unittest.TestCase):
    def test_process_pro_forwards_preferences_to_embedded_and_tracks_only_routes(self):
        for mode, callee in (("embedded", "process_video"), ("none", "process_tracks_only")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                with patch.object(pro_core, "_prepare_local_chinese_conversions", return_value=[]), \
                     patch.object(core if mode == "embedded" else pro_core, callee, return_value="out.mkv") as process:
                    pro_core.process_pro(
                        "movie.mkv", "out.mkv", [1], [4, 9], mode, 4, None, 1, "en", "en",
                        [], folder, lambda _: None, 1, None, trust_embedded_original_timeline=True,
                        subtitle_language_preferences=["zh-CN", "en", "de"],
                        chinese_script_equivalent=True,
                    )
                self.assertEqual(process.call_args.kwargs["subtitle_language_preferences"], ["zh-CN", "en", "de"])
                self.assertTrue(process.call_args.kwargs["chinese_script_equivalent"])

    def test_online_route_forwards_full_preferences_to_mux(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.srt"
            source.write_text("1\n00:00:01,000 --> 00:00:03,000\nHello there\n", encoding="utf-8")
            media = {"tracks": [subtitle(4, "eng"), subtitle(9, "chi", "Mandarin Traditional")]}

            def mux(*args, **kwargs):
                Path(args[1]).write_bytes(b"output")

            with patch.object(pro_core, "_prepare_local_chinese_conversions", return_value=[]), \
                 patch.object(core, "ensure_output_disk_space"), \
                 patch.object(core, "ensure_mux_disk_space"), \
                 patch.object(core, "inspect_media", return_value=media), \
                 patch.object(core, "validate_media_output"), \
                 patch.object(core, "mux_video", side_effect=mux) as process:
                pro_core.process_pro(
                    "movie.mkv", str(root / "out.mkv"), [1], [4, 9], "online", None, str(source),
                    1, "en", "en", [], folder, lambda _: None, 1, None,
                    trust_embedded_original_timeline=True, trust_external_original_timeline=True,
                    include_external_source_in_output=False,
                    subtitle_language_preferences=["zh-CN", "en", "de"], chinese_script_equivalent=True,
                )
            self.assertEqual(process.call_args.kwargs["subtitle_language_preferences"], ["zh-CN", "en", "de"])
            self.assertIs(process.call_args.kwargs["input_media"], media)


if __name__ == "__main__":
    unittest.main()
