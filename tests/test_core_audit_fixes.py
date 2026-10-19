import tempfile
import json
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import subtitle_tool_core as core


class CoreAuditFixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.video = self.root / "movie.mkv"
        self.video.write_bytes(b"video")
        self.track = core.Track(2, "subtitles", "SubRip/SRT", "eng", "", False, False, True, False, False)
        self.full = "1\n00:00:01,000 --> 00:00:02,000\nFirst.\n\n2\n00:00:03,000 --> 00:00:04,000\nSecond.\n"

    def writer(self, args, **kwargs):
        for spec in args[3:]:
            path = Path(spec.split(":", 1)[1])
            path.write_text(self.full, encoding="utf-8")

    def test_cancelled_partial_is_not_reused_then_completed_cache_is_reused(self):
        def cancelled(args, **kwargs):
            Path(args[-1].split(":", 1)[1]).write_text(self.full.split("\n\n")[0], encoding="utf-8")
            raise core.CancelledError("cancelled")
        with patch.object(core, "run_command", side_effect=cancelled):
            with self.assertRaises(core.CancelledError):
                core.extract_subtitle(str(self.video), self.track, self.root, lambda _: None)
        with patch.object(core, "run_command", side_effect=self.writer) as extract:
            result = core.extract_subtitle(str(self.video), self.track, self.root, lambda _: None)
            self.assertEqual(len(core.parse_srt(result)), 2)
            core.extract_subtitle(str(self.video), self.track, self.root, lambda _: None)
            self.assertEqual(extract.call_count, 1)

    def test_unsealed_legacy_file_and_modified_completed_file_are_reextracted(self):
        target = self.root / "track-2-eng.srt"
        target.write_text("partial", encoding="utf-8")
        with patch.object(core, "run_command", side_effect=self.writer) as extract:
            core.extract_subtitle(str(self.video), self.track, self.root, lambda _: None)
            target.write_text("tampered", encoding="utf-8")
            core.extract_subtitle(str(self.video), self.track, self.root, lambda _: None)
            self.video.write_bytes(b"replacement film")
            core.extract_subtitle(str(self.video), self.track, self.root, lambda _: None)
            self.assertEqual(extract.call_count, 3)

    def test_multiple_tracks_still_use_one_tool_call(self):
        second = core.Track(3, "subtitles", "SubRip/SRT", "ger", "", False, False, True, False, False)
        requests = [(self.track, self.root / "a.srt"), (second, self.root / "b.srt")]
        with patch.object(core, "run_command", side_effect=self.writer) as extract:
            core.extract_subtitles(str(self.video), requests, lambda _: None)
            core.extract_subtitles(str(self.video), requests, lambda _: None)
        self.assertEqual(extract.call_count, 1)
        self.assertEqual(len(extract.call_args.args[0]), 5)

    def test_source_change_during_extraction_cannot_publish_cache(self):
        def changed(args, **kwargs):
            self.writer(args)
            self.video.write_bytes(b"different")
        with patch.object(core, "run_command", side_effect=changed):
            with self.assertRaisesRegex(RuntimeError, "影片发生变化"):
                core.extract_subtitle(str(self.video), self.track, self.root, lambda _: None)
        self.assertFalse(list(self.root.glob("*.complete.json")))

    def test_vobsub_requires_both_files_and_receipt(self):
        self.track = core.Track(2, "subtitles", "VobSub", "eng", "", False, False, False, False, True)
        def pair(args, **kwargs):
            self.writer(args)
            Path(args[-1].split(":", 1)[1]).with_suffix(".idx").write_bytes(b"index")
        with patch.object(core, "run_command", side_effect=pair) as extract:
            result = core.extract_subtitle(str(self.video), self.track, self.root, lambda _: None)
            result.with_suffix(".idx").unlink()
            core.extract_subtitle(str(self.video), self.track, self.root, lambda _: None)
            self.assertEqual(extract.call_count, 2)

    def test_numeric_dialogue_is_preserved_through_translation(self):
        with patch.object(core, "call_ollama_retry", return_value="2001年。\n42个人。"):
            result = core.translate_batch(["Year 2001.", "42 people."], "zh-CN", "English", "model", "host", 10)
        self.assertEqual(result, ["2001年。", "42个人。"])
        self.assertEqual(core.parse_numbered_response("1. 2001年。\n2) 42个人。", 2), result)
        self.assertIsNone(core.parse_numbered_response("1.5元。", 1))
        self.assertIsNone(core.parse_numbered_response("1:30到了。", 1))
        self.assertIsNone(core.parse_numbered_response("1-5个人。", 1))
        self.assertIsNone(core.parse_numbered_response("1. 第一行\n1. 第二行", 2))

    def test_short_vtt_timestamps_and_settings_match_long_form(self):
        path = self.root / "short.vtt"
        path.write_text("WEBVTT\n\ncue-id\n01:02.000 --> 01:05.250 align:start\n<b>2001年。</b>\n", encoding="utf-8")
        events = core.parse_vtt(path)
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0].start, events[0].end, events[0].text),
                         ("00:01:02,000", "00:01:05,250", "2001年。"))
        path.write_text("WEBVTT\n\n00:01:02.000 --> 00:01:05.250\n2001年。\n", encoding="utf-8")
        self.assertEqual(core.parse_vtt(path), events)

    def test_only_old_cache_for_numeric_sources_is_invalidated_once(self):
        path = self.root / "cache.jsonl"
        sources = {1: "2001.", 2: "Hello."}
        path.write_text("\n".join(json.dumps({"target": "zh-CN", "index": i,
                        "source": sources[i], "translation": translated})
                        for i, translated in [(1, "年。"), (2, "你好。")]) + "\n", encoding="utf-8")
        self.assertEqual(core.load_cache(path, "zh-CN", sources), ({2: "你好。"}, 1))
        core.append_cache(path, "zh-CN", 1, sources[1], "2001年。")
        translations = {1: "2001年。", 2: "你好。"}
        self.assertEqual(core.load_cache(path, "zh-CN", sources), (translations, 0))
        core.rewrite_cache(path, "zh-CN", sources, translations)
        self.assertEqual(core.load_cache(path, "zh-CN", sources), (translations, 0))


if __name__ == "__main__":
    unittest.main()
