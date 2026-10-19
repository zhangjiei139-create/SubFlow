"""Verify defaults at the actual processing entry, without opening a dialog."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import batch_core
import subtitle_fonts
from profile_model import BatchPlan, PreferenceProfile


class FontDefaultProcessingTests(unittest.TestCase):
    def dispatch(self, languages, styles, output=True):
        observed = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = BatchPlan(str(root / "input.mkv"), 1,
                             output_path=str(root / "custom-output.mkv") if output else "")
            profile = PreferenceProfile(1, "测试", subtitle_languages=languages,
                                        subtitle_styles=styles)

            def implementation(*args):
                context = subtitle_fonts.CONTEXT.get()
                observed.append(None if context is None else dict(context))
                self.assertIs(args[0], plan)
                self.assertIs(args[1], profile)
                return "处理完成"

            with patch.object(batch_core, "_process_plan_impl", implementation):
                self.assertEqual(batch_core.process_plan(plan, profile, lambda value: None, None),
                                 "处理完成")
        self.assertEqual(len(observed), 1)
        return observed[0]

    def test_unopened_font_preferences_enable_automatic_sizing(self):
        context = self.dispatch(["zh-CN", "en", "de"], {})
        self.assertEqual(set(context["styles"]), {"zh-CN", "en", "de"})
        for settings in context["styles"].values():
            self.assertEqual(settings, dict(font_id="", size_mode="auto", size=0, color="keep"))

    def test_explicit_manual_38_survives_processing(self):
        context = self.dispatch(["en"], {"en": dict(font_id="builtin:bree", size_mode="manual",
                                                  size=38, size_origin="user", color="#ABCDEF")})
        settings = context["styles"]["en"]
        self.assertEqual(settings["font_id"], "builtin:bree")
        self.assertEqual((settings["size_mode"], settings["size"]), ("manual", 38))
        self.assertEqual(settings["color"], "#ABCDEF")

    def test_previous_inserted_38_does_not_disable_the_default(self):
        context = self.dispatch(["en"], {"en": dict(font_id="builtin:rubik", size=38,
                                                  color="#FFFFFF")})
        self.assertEqual((context["styles"]["en"]["size_mode"],
                          context["styles"]["en"]["size"]), ("auto", 0))
        self.assertEqual(context["styles"]["en"]["font_id"], "builtin:rubik")

    def test_language_aliases_find_the_saved_settings(self):
        context = self.dispatch(["eng", "chi", "ger"], {"english": dict(size=45),
                                                         "zh_cn": dict(size=32)})
        self.assertEqual(set(context["styles"]), {"en", "zh-CN", "de"})
        self.assertEqual(context["styles"]["en"]["size"], 45)
        self.assertEqual(context["styles"]["zh-CN"]["size"], 32)
        self.assertEqual(context["styles"]["de"]["size_mode"], "auto")

    def test_hidden_unselected_languages_are_not_applied(self):
        context = self.dispatch(["en"], {"de": dict(size=48, color="#FFAABB")})
        self.assertEqual(set(context["styles"]), {"en"})
        self.assertEqual(context["styles"]["en"]["size_mode"], "auto")

    def test_no_selected_languages_does_not_enable_font_processing(self):
        self.assertIsNone(self.dispatch([], {"en": dict(size=48)}))

    def test_absent_output_preserves_the_existing_dispatch(self):
        self.assertIsNone(self.dispatch(["en"], {}, output=False))


if __name__ == "__main__":
    unittest.main()
