import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import subtitle_tool_core as core
import translation_review as review


class TranslationQualityTests(unittest.TestCase):
    def test_supported_non_cjk_targets_reject_new_cjk_text(self):
        for target in ("en", "es", "fr", "de", "pt", "ru", "hi"):
            for translation in ("Die 橡胶haut.", "Die ゴムhaut.", "Die 고무haut.", "Die 𠮷haut."):
                with self.subTest(target=target, translation=translation):
                    self.assertIn("中日韩文字", core.translation_quality_issue(
                        "The rubber skin was easy to spot.", translation, target))

    def test_native_scripts_names_and_punctuation_remain_accepted(self):
        for target, translation in (
            ("de", "Müller, hör zu: Die Gummihaut war äußerst auffällig!"),
            ("de", "John Connor und Sarah Connor grüßen Großmutter."),
            ("de", "♪ — „Grüß dich, Chloe!“ 2029, G-9. ♪"),
            ("es", "La piel de goma era fácil de ver."),
            ("fr", "La peau en caoutchouc était visible."),
            ("pt", "A pele de borracha era visível."),
            ("ru", "Резиновая кожа была заметна."),
            ("hi", "रबर की त्वचा पहचानना आसान था।"),
        ):
            with self.subTest(target=target, translation=translation):
                self.assertFalse(core.translation_quality_issue("The rubber skin.", translation, target))

    def test_source_cjk_names_and_quotes_are_allowed_without_waiving_new_leaks(self):
        for source, translation in (
            ("Ask 王伟 about the rubber skin.", "Frag 王伟 nach der Gummihaut."),
            ("请问王伟在哪里？", "Wo ist 王伟?"),
            ("The sign says こんにちは.", "Auf dem Schild steht こんにちは."),
            ("Call 김민수.", "Ruf 김민수 an."),
        ):
            with self.subTest(source=source):
                self.assertFalse(core.translation_quality_issue(source, translation, "de"))
        self.assertTrue(core.translation_quality_issue(
            "Ask 王伟 about the rubber skin.", "Frag 王伟 nach der 橡胶haut.", "de"))
        self.assertTrue(core.translation_quality_issue(
            "橡树 and 胶水 are on the sign.", "Die 橡胶haut.", "de"))

    def test_cjk_targets_and_unsupported_targets_are_outside_new_check(self):
        for target, translation in (("zh-CN", "橡胶皮肤"), ("zh-TW", "橡膠皮膚"),
                                    ("ja", "ゴムの皮膚"), ("ko", "고무 피부")):
            with self.subTest(target=target):
                self.assertFalse(core.translation_quality_issue("The rubber skin.", translation, target))
        self.assertFalse(core.translation_quality_issue("The rubber skin.", "橡胶haut", "unknown"))

    def test_non_cjk_repair_prompt_uses_target_and_context(self):
        prompt = core.make_prompt(["The rubber skin."], "de", "en", require_target_script=True,
                                  context_lines=("The early models.", "They improved them."))
        self.assertIn("into German", prompt)
        self.assertIn("Do not introduce Chinese, Japanese, or Korean text", prompt)
        self.assertIn("appears verbatim in the source line", prompt)
        self.assertIn("Previous: The early models.", prompt)
        self.assertIn("Next: They improved them.", prompt)
        self.assertIn("Output exactly 1 lines", prompt)
        self.assertNotIn("previous translation", core.make_prompt(["Hello."], "de", "en"))

    def test_real_missed_lines_and_safe_names_codes_music(self):
        for source, result in (("Somehow,", "somehow,"), ("Somehow,", "总是 somehow,"), ("LabeL", "LabeL"),
                               ("Thank You", "Thank You"), ("こんにちは", "こんにちは"),
                               ("Hello", "")):
            self.assertTrue(core.translation_quality_issue(source, result, "zh-CN"))
        for text in ("Chloe...", "Darth Vader", "R2！", "G-9。", "D-7。", "♪"):
            self.assertFalse(core.translation_quality_issue(text, text, "zh-CN"))
        self.assertFalse(core.translation_quality_issue("Hello", "你好", "zh-TW"))
        self.assertFalse(core.translation_quality_issue("Hello", "Hallo", "de"))
        self.assertFalse(core.translation_quality_issue("For a Pontiac Trans Am.", "是为了辆庞蒂克Trans Am。", "zh-CN"))
        self.assertFalse(core.translation_quality_issue("for the dissolution of The Axe and Cross on my very first...", "The Axe and Cross解散的责任。", "zh-CN"))
        self.assertTrue(core.translation_quality_issue("and I somehow entered his mind.", "我 somehow 进入了他的脑海。", "zh-CN"))

    def test_invalid_cached_line_reloads_model_and_repairs_only_that_line(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "cache.jsonl"
            events = [core.SubtitleEvent("00:00:00,000", "00:00:01,000", "Somehow,"),
                      core.SubtitleEvent("00:00:02,000", "00:00:03,000", "Hello")]
            core.append_cache_records(cache, "zh-CN", [(1, "Somehow,", "somehow,"),
                                                       (2, "Hello", "你好")])
            self.assertTrue(core.needs_model_translation(events, "zh-CN", cache, "en"))
            with patch.object(core, "translate_batch", side_effect=[["somehow,"], ["不知怎的，"]]) as model:
                core.translate_events(events, root / "out.srt", cache, "zh-CN", "en", lambda _: None)
            self.assertEqual([call.args[0] for call in model.call_args_list], [["Somehow,"], ["Somehow,"]])
            self.assertEqual(model.call_args_list[1].kwargs["context_lines"], ("", "Hello"))
            result = (root / "out.srt").read_text(encoding="utf-8")
            self.assertIn("不知怎的", result)
            self.assertIn("你好", result)
            self.assertFalse(core.needs_model_translation(events, "zh-CN", cache, "en"))

    def test_legacy_german_cache_invalidates_and_repairs_only_new_cjk_leak(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "cache.jsonl"
            sources = ["The rubber skin was easy to spot.", "Hello, Müller.", "Ask 王伟."]
            translations = ["Die 橡胶haut war leicht zu erkennen.", "Hallo, Müller.", "Frag 王伟."]
            events = [core.SubtitleEvent("00:00:00,000", "00:00:01,000", source) for source in sources]
            # No parser/version field: old records must receive the current quality check too.
            cache.write_text("".join(json.dumps({"target": "de", "index": index,
                "source": source, "translation": translation}, ensure_ascii=False) + "\n"
                for index, (source, translation) in enumerate(zip(sources, translations), 1)), encoding="utf-8")
            before = cache.read_bytes()
            accepted, stale = core.load_cache(cache, "de", dict(enumerate(sources, 1)))
            self.assertEqual(stale, 1)
            self.assertEqual(accepted, {2: translations[1], 3: translations[2]})
            self.assertTrue(core.needs_model_translation(events, "de", cache, "en"))
            self.assertEqual(cache.read_bytes(), before)

            repaired = "Die Gummihaut war leicht zu erkennen."
            logs = []
            with patch.object(core, "translate_batch", side_effect=[[translations[0]], [repaired]]) as model:
                core.translate_events(events, root / "out.srt", cache, "de", "en", logs.append)
            self.assertEqual([call.args[0] for call in model.call_args_list], [[sources[0]], [sources[0]]])
            self.assertTrue(model.call_args_list[1].kwargs["require_target_script"])
            self.assertEqual(model.call_args_list[1].kwargs["context_lines"], ("", sources[1]))
            self.assertTrue(any("缓存匹配：2，缓存失效：1，待翻译：1" in message for message in logs))
            self.assertEqual(core.load_cache(cache, "de", dict(enumerate(sources, 1))),
                             ({1: repaired, 2: translations[1], 3: translations[2]}, 0))
            self.assertFalse(core.needs_model_translation(events, "de", cache, "en"))
            self.assertEqual([event.text for event in core.parse_subtitle(root / "out.srt")],
                             [repaired, translations[1], translations[2]])

    def test_repeated_german_leak_keeps_good_cache_and_marks_usable_srt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output, cache = root / "out.srt", root / "cache.jsonl"
            events = [core.SubtitleEvent("00:00:00,000", "00:00:01,000", "Hello."),
                      core.SubtitleEvent("00:00:02,000", "00:00:03,000", "The rubber skin.")]
            core.append_cache(cache, "de", 1, "Hello.", "Hallo.")
            before = cache.read_bytes()
            output.write_text("previous subtitle", encoding="utf-8")
            with patch.object(core, "translate_batch", return_value=["Die 橡胶haut."]) as model:
                core.translate_events(events, output, cache, "de", "en", lambda _: None)
            self.assertEqual(model.call_count, 3)
            self.assertTrue(all(call.args[0] == ["The rubber skin."] for call in model.call_args_list))
            self.assertEqual(cache.read_bytes(), before)
            self.assertEqual([event.text for event in core.parse_subtitle(output)], ["Hallo.", "Die 橡胶haut."])
            issue = review.load_report(review.report_path(cache))["issues"][0]
            self.assertEqual(issue["index"], 2)
            self.assertEqual(issue["status"], "needs_review")
            self.assertIn("中日韩文字", issue["reason"])

    def test_persistent_leak_is_bounded_and_separate_from_clean_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events = [core.SubtitleEvent("00:00:00,000", "00:00:01,000", "LabeL")]
            with patch.object(core, "translate_batch", return_value=["LabeL"]) as model:
                core.translate_events(events, root / "out.srt", root / "cache.jsonl",
                                      "zh-CN", "en", lambda _: None)
            self.assertEqual(model.call_count, 3)
            self.assertTrue((root / "out.srt").exists())
            self.assertEqual(core.load_cache(root / "cache.jsonl", "zh-CN", {1: "LabeL"}), ({}, 0))
            self.assertEqual(review.load_report(review.report_path(root / "cache.jsonl"))["issues"][0]["translation"], "LabeL")

    def test_cancel_during_repair_never_publishes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cancel = threading.Event()
            events = [core.SubtitleEvent("00:00:00,000", "00:00:01,000", "Somehow,")]
            def model(*args, **kwargs):
                cancel.set()
                return ["somehow,"]
            with patch.object(core, "translate_batch", side_effect=model):
                with self.assertRaises(Exception):
                    core.translate_events(events, root / "out.srt", root / "cache.jsonl",
                                          "zh-CN", "en", lambda _: None, cancel_event=cancel)
            self.assertFalse((root / "out.srt").exists())

    def test_cancel_after_successful_single_line_repair_never_publishes_or_caches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cancel = threading.Event()
            events = [core.SubtitleEvent("00:00:00,000", "00:00:01,000", "The rubber skin.")]
            def model(*args, **kwargs):
                if kwargs.get("require_target_script"):
                    cancel.set()
                    return ["Die Gummihaut."]
                return ["Die 橡胶haut."]
            with patch.object(core, "translate_batch", side_effect=model) as translate:
                with self.assertRaises(core.CancelledError):
                    core.translate_events(events, root / "out.srt", root / "cache.jsonl",
                                          "de", "en", lambda _: None, cancel_event=cancel)
            self.assertEqual(translate.call_count, 2)
            self.assertFalse((root / "out.srt").exists())
            self.assertFalse((root / "cache.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
