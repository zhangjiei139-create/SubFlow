"""Validated rows survive later failures within the very same batch."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import subtitle_tool_core as core


class PartialProgressTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cache = self.root / "cache.jsonl"
        self.output = self.root / "out.srt"
        self.events = [core.SubtitleEvent(f"00:00:0{i},000", f"00:00:0{i},900", text)
                       for i, text in enumerate(("Hello.", "Goodbye.", "Somehow,", "Thank you."), 1)]

    def saved(self):
        return core.load_cache(self.cache, "zh-CN", {i: e.text for i, e in enumerate(self.events, 1)})[0]

    def run_translation(self, **kwargs):
        core.translate_events(self.events, self.output, self.cache, "zh-CN", "en", lambda _: None, **kwargs)

    def test_good_rows_saved_before_later_quality_repair_fails(self):
        with patch.object(core, "translate_batch", side_effect=[
                ["你好。", "再见。", "Somehow,", "谢谢。"], ConnectionError("offline")]):
            with self.assertRaisesRegex(ConnectionError, "offline"):
                self.run_translation()
        self.assertEqual(self.saved(), {1: "你好。", 2: "再见。"})
        self.assertFalse(self.output.exists())
        with patch.object(core, "translate_batch", return_value=["不知怎的，", "谢谢。"]) as translate:
            self.run_translation()
        self.assertEqual(translate.call_args.args[0], ["Somehow,", "Thank you."])
        self.assertEqual([(e.start, e.end) for e in core.parse_subtitle(self.output)],
                         [(e.start, e.end) for e in self.events])

    def test_single_fallback_progress_is_saved_before_batch_can_return(self):
        with patch.object(core, "call_ollama_retry", side_effect=[
                "bad batch numbering", "1. 你好。", "1. 再见。", ConnectionError("offline")]):
            with self.assertRaisesRegex(ConnectionError, "offline"):
                self.run_translation()
        self.assertEqual(self.saved(), {1: "你好。", 2: "再见。"})
        self.assertFalse(self.output.exists())

    def test_bad_single_fallback_is_not_cached(self):
        with patch.object(core, "call_ollama_retry", side_effect=[
                "bad batch numbering", "1. Hello.", "1. 再见。", ConnectionError("offline")]):
            with self.assertRaisesRegex(ConnectionError, "offline"):
                self.run_translation()
        self.assertEqual(self.saved(), {2: "再见。"})

    def test_stop_during_later_repair_preserves_prior_validated_rows(self):
        stop = threading.Event()

        def translate(_lines, *_args, **kwargs):
            if kwargs.get("require_target_script"):
                stop.set()
                raise core.CancelledError("stop")
            return ["你好。", "再见。", "Somehow,", "谢谢。"]

        with patch.object(core, "translate_batch", side_effect=translate):
            with self.assertRaises(core.CancelledError):
                self.run_translation(cancel_event=stop)
        self.assertEqual(self.saved(), {1: "你好。", 2: "再见。"})

    def test_recovered_group_is_saved_before_a_subsequent_row_fails(self):
        self.events = [core.SubtitleEvent(f"00:00:0{i},000", f"00:00:0{i},900", text)
                       for i, text in enumerate(("Each time I arrived,", "somehow they had moved...",
                                                 "all their people.", "Hello."), 1)]
        response = {"whole": "每次我到达时，不知怎的他们已经把所有人迁走了。",
                    "s1": "每次我到达时，", "s2": "不知怎的他们已经把所有人", "s3": "迁走了。"}
        with patch.object(core, "translate_batch", side_effect=[
                ["我到达时，", "somehow他们搬走了", "所有人。", "Hello."],
                core.TranslationGenerationError("loop"), ConnectionError("offline")]), \
                patch.object(core, "call_ollama_retry", return_value=json.dumps(response)):
            with self.assertRaisesRegex(ConnectionError, "offline"):
                self.run_translation()
        self.assertEqual(self.saved(), {i: response[f"s{i}"] for i in (1, 2, 3)})
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
