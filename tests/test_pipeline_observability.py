import contextlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import subtitle_tool_core as core


class FakeResponse:
    def __init__(self, text, **metrics):
        self.lines = [
            json.dumps({"response": text, "done": False}).encode() + b"\n",
            json.dumps({"done": True, **metrics}).encode() + b"\n",
        ]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def readline(self):
        return self.lines.pop(0) if self.lines else b""


class TranslationObservationTests(unittest.TestCase):
    def setUp(self):
        self.events = [core.SubtitleEvent("00:00:00,000", "00:00:01,000", "Hello")]

    def test_cache_probe_matches_actual_translation_and_checks_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "cache.jsonl"
            core.append_cache(cache, "de", 1, "Hello", "Hallo")
            self.assertFalse(core.needs_model_translation(self.events, "de", cache, "en"))
            with patch.object(core, "translate_batch") as translate:
                core.translate_events(self.events, root / "out.srt", cache, "de", "en", lambda _: None)
            translate.assert_not_called()
            self.assertIn("Hallo", (root / "out.srt").read_text(encoding="utf-8"))
            changed = [core.SubtitleEvent("00:00:00,000", "00:00:01,000", "Changed")]
            self.assertTrue(core.needs_model_translation(changed, "de", cache, "en"))
            self.assertTrue(core.needs_model_translation(self.events, "fr", cache, "en"))
            self.assertFalse(core.needs_model_translation(self.events, "en", cache, "eng"))
            self.assertFalse(core.needs_model_translation(self.events, "zh-CN", cache, "zh-TW"))
            self.assertFalse(core.needs_model_translation([], "de", cache, "en"))

    def test_done_metrics_preserve_text_and_validate_numeric_fields(self):
        metrics = core.TranslationMetrics()
        response = FakeResponse(
            "private translation", total_duration=3_800_000_000, load_duration=2_000_000_000,
            prompt_eval_duration=300_000_000, eval_duration=1_200_000_000,
            prompt_eval_count=100, eval_count=30,
        )
        with patch.object(core.urllib.request, "urlopen", return_value=response):
            result = core.call_ollama("secret prompt", "model", "http://localhost", 600, metrics=metrics)
        self.assertEqual(result, "private translation")
        self.assertEqual(metrics.requests, 1)
        self.assertEqual(metrics.completed_requests, 1)
        self.assertEqual(metrics.total_duration_ns, 3_800_000_000)
        self.assertEqual(metrics.load_duration_ns, 2_000_000_000)
        self.assertEqual(metrics.eval_count, 30)
        metrics.record_done({
            "load_duration": float("nan"), "prompt_eval_duration": -10,
            "eval_duration": True, "eval_count": 1.5, "prompt_eval_count": "200",
        })
        self.assertEqual(metrics.load_samples, 1)
        self.assertEqual(metrics.eval_duration_samples, 1)
        self.assertEqual(metrics.eval_count, 30)
        summary = metrics.summary()
        self.assertIn("模型加载2.00秒", summary)
        self.assertNotIn("secret", summary)
        self.assertNotIn("private", summary)

    def test_missing_metrics_are_reported_as_unavailable(self):
        metrics = core.TranslationMetrics()
        with patch.object(core.urllib.request, "urlopen", return_value=FakeResponse("OK")):
            core.call_ollama("input", "model", "http://localhost", 600, metrics=metrics)
        self.assertIn("模型加载未提供", metrics.summary())
        self.assertIn("文本生成未提供", metrics.summary())

    def test_retry_and_format_fallback_are_counted(self):
        metrics = core.TranslationMetrics()
        responses = [
            TimeoutError("timeout"), FakeResponse("unusable response"),
            FakeResponse("1. 一"), FakeResponse("1. 二"),
        ]
        with patch.object(core.urllib.request, "urlopen", side_effect=responses), patch.object(core.time, "sleep"):
            result = core.translate_batch(["secret one", "secret two"], "zh-CN", "en", "model", "http://localhost", 600, metrics=metrics)
        self.assertEqual(result, ["一", "二"])
        self.assertEqual(metrics.requests, 4)
        self.assertEqual(metrics.completed_requests, 3)
        self.assertEqual(metrics.retries, 1)
        self.assertEqual(metrics.batch_parse_failures, 1)
        self.assertEqual(metrics.fallback_lines, 2)

    def test_failure_still_reports_summary_without_private_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            messages = []
            with patch.object(core.urllib.request, "urlopen", side_effect=RuntimeError("server failed")):
                with self.assertRaisesRegex(RuntimeError, "server failed"):
                    core.translate_events(self.events, root / "out.srt", root / "cache.jsonl", "de", "en", messages.append)
            summary = next(message for message in messages if "翻译模型统计" in message)
            self.assertIn("请求1次，完成0次", summary)
            self.assertNotIn("Hello", summary)


class EmbeddedTranslationScopeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.video = self.root / "movie.mkv"
        self.video.write_bytes(b"video")
        self.source = self.root / "source.srt"
        self.source.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
        self.output = self.root / "out.mkv"
        self.track = core.Track(1, "subtitles", "SubRip", "eng", "", True, False, True, False, False)
        self.steps = []

    def process(self, targets, translate=None, ensure=None):
        def mux(_input, output, *args, **kwargs):
            self.steps.append("mux")
            Path(output).write_bytes(b"result")

        with contextlib.ExitStack() as patches:
            for name, value in (
                ("inspect_media", {"tracks": [], "container": {}}),
                ("inspect_tracks", [self.track]),
                ("prepare_work_input", str(self.video)),
            ):
                patches.enter_context(patch.object(core, name, return_value=value))
            for name in ("ensure_output_disk_space", "ensure_mux_disk_space", "validate_media_output"):
                patches.enter_context(patch.object(core, name))
            patches.enter_context(patch.object(core, "mux_video", side_effect=mux))
            ensure_mock = patches.enter_context(patch.object(core, "ensure_ollama_running", side_effect=ensure))
            if translate is not None:
                patches.enter_context(patch.object(core, "translate_events", side_effect=translate))
            result = core.process_video(
                str(self.video), str(self.output), [], [1], 1, targets, str(self.root / "work"),
                source_subtitle_override=self.source,
                translation_start=lambda: self.steps.append("start"),
                translation_end=lambda: self.steps.append("end"),
            )
        return result, ensure_mock

    def test_cache_complete_skips_model_and_scope(self):
        cache = self.root / "work" / "translation-cache-track-1-de.jsonl"
        cache.parent.mkdir()
        core.append_cache(cache, "de", 1, "Hello", "Hallo")
        _result, ensure = self.process(["de"])
        self.assertEqual(self.steps, ["mux"])
        ensure.assert_not_called()

    def test_all_languages_finish_before_scope_ends_and_mux(self):
        barrier = threading.Barrier(2)

        def translate(_events, output, _cache, target, *args, **kwargs):
            barrier.wait(timeout=2)
            if target == "de":
                time.sleep(0.02)
            self.steps.append(f"done-{target}")
            Path(output).write_text("translated", encoding="utf-8")

        self.process(["de", "fr"], translate=translate)
        self.assertEqual(self.steps[0], "start")
        self.assertEqual(set(self.steps[1:3]), {"done-de", "done-fr"})
        self.assertEqual(self.steps[3:], ["end", "mux"])

    def test_model_start_failure_releases_scope_before_propagating(self):
        with self.assertRaisesRegex(RuntimeError, "startup failed"):
            self.process(["de"], ensure=RuntimeError("startup failed"))
        self.assertEqual(self.steps, ["start", "end"])

    def test_parallel_translation_failure_waits_for_other_target_then_releases(self):
        barrier = threading.Barrier(2)

        def translate(_events, output, _cache, target, *args, **kwargs):
            barrier.wait(timeout=2)
            if target == "de":
                raise core.CancelledError("stop")
            time.sleep(0.02)
            self.steps.append("done-fr")
            Path(output).write_text("translated", encoding="utf-8")

        with self.assertRaises(core.CancelledError):
            self.process(["de", "fr"], translate=translate)
        self.assertEqual(self.steps, ["start", "done-fr", "end"])

    def test_same_language_and_no_targets_skip_scope(self):
        _result, ensure = self.process(["en"])
        self.assertEqual(self.steps, ["mux"])
        ensure.assert_not_called()
        self.steps.clear()
        _result, ensure = self.process([])
        self.assertEqual(self.steps, ["mux"])
        ensure.assert_not_called()


class MuxEstimateTests(unittest.TestCase):
    def test_rate_is_explicitly_estimated_processing_rate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mkv"
            source.write_bytes(b"x" * 1024 * 1024)
            process = Mock(returncode=0)
            process.communicate.return_value = (b"", b"")
            messages = []
            with patch.object(core.subprocess, "Popen", return_value=process), patch.object(core.time, "monotonic", side_effect=[0.0, 2.0]):
                core.run_command(["mkvmerge.exe", "-o", str(root / "out.mkv"), str(source)], messages.append)
            summary = messages[-1]
            self.assertIn("处理速率 0.5 MiB/s", summary)
            self.assertIn("不是实测磁盘读取速度", summary)
            self.assertNotIn("平均读取吞吐", summary)


if __name__ == "__main__":
    unittest.main()
