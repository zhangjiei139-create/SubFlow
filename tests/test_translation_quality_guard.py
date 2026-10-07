import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import subtitle_tool_core as core


class TranslationQualityTests(unittest.TestCase):
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

    def test_persistent_leak_is_bounded_and_never_written_or_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events = [core.SubtitleEvent("00:00:00,000", "00:00:01,000", "LabeL")]
            with patch.object(core, "translate_batch", return_value=["LabeL"]) as model:
                with self.assertRaisesRegex(RuntimeError, "翻译验收失败"):
                    core.translate_events(events, root / "out.srt", root / "cache.jsonl",
                                          "zh-CN", "en", lambda _: None)
            self.assertEqual(model.call_count, 3)
            self.assertFalse((root / "out.srt").exists())
            self.assertFalse((root / "cache.jsonl").exists())

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


if __name__ == "__main__":
    unittest.main()
