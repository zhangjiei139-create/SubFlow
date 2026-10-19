"""Wall-clock accounting includes failed, cancelled and retried requests."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import subtitle_tool_core as core


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Response:
    def __init__(self, clock, seconds, result):
        self.clock, self.seconds, self.result = clock, seconds, result

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def readline(self):
        self.clock.advance(self.seconds)
        if isinstance(self.result, BaseException):
            raise self.result
        return json.dumps(self.result).encode() + b"\n"


class TranslationWallTimeTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.metrics = core.TranslationMetrics()

    def call(self):
        return core.call_ollama_retry("private text", "test", "http://localhost", 90,
                                      metrics=self.metrics)

    def test_failed_attempt_successful_retry_and_backoff_are_separate(self):
        responses = [Response(self.clock, 8, ConnectionError("offline")),
                     Response(self.clock, 3, {"response": "你好", "done": True, "total_duration": 2_000_000_000})]
        with patch.object(core.time, "monotonic", self.clock), \
                patch.object(core.time, "sleep", self.clock.advance), \
                patch.object(core, "_write_ollama_diagnostic"), \
                patch.object(core.urllib.request, "urlopen", side_effect=responses):
            self.assertEqual(self.call(), "你好")
        self.assertEqual(self.metrics.requests, 2)
        self.assertEqual(self.metrics.completed_requests, 1)
        self.assertEqual(self.metrics.failed_requests, 1)
        self.assertEqual(self.metrics.request_wall_seconds, 11)
        self.assertEqual(self.metrics.failed_request_seconds, 8)
        self.assertEqual(self.metrics.successful_request_seconds, 3)
        self.assertEqual(self.metrics.retry_wait_seconds, 2)
        self.assertEqual(self.metrics.total_duration_ns, 2_000_000_000)
        self.assertIn("请求实际耗时11.00秒（成功3.00秒，失败8.00秒", self.metrics.summary())

    def test_both_timeouts_are_counted_without_successful_engine_stats(self):
        responses = [Response(self.clock, 61, TimeoutError("idle")) for _ in range(2)]
        with patch.object(core.time, "monotonic", self.clock), \
                patch.object(core.time, "sleep", self.clock.advance), \
                patch.object(core, "_write_ollama_diagnostic"), \
                patch.object(core.urllib.request, "urlopen", side_effect=responses):
            with self.assertRaisesRegex(RuntimeError, "2 次尝试上限"):
                self.call()
        self.assertEqual(self.metrics.failed_requests, 2)
        self.assertEqual(self.metrics.failed_request_seconds, 122)
        self.assertEqual(self.metrics.total_samples, 0)
        self.assertIn("引擎已完成响应耗时未提供", self.metrics.summary())

    def test_generation_length_failure_is_timed_without_retry(self):
        response = Response(self.clock, 4, {"response": "partial", "done": True, "done_reason": "length"})
        with patch.object(core.time, "monotonic", self.clock), \
                patch.object(core.urllib.request, "urlopen", return_value=response) as request:
            with self.assertRaises(core.TranslationGenerationError):
                self.call()
        self.assertEqual(request.call_count, 1)
        self.assertEqual(self.metrics.failed_request_seconds, 4)
        self.assertEqual(self.metrics.completed_requests, 0)

    def test_cancellation_is_timed_separately(self):
        response = Response(self.clock, 5, core.CancelledError("stopped"))
        with patch.object(core.time, "monotonic", self.clock), \
                patch.object(core.urllib.request, "urlopen", return_value=response):
            with self.assertRaises(core.CancelledError):
                self.call()
        self.assertEqual(self.metrics.cancelled_requests, 1)
        self.assertEqual(self.metrics.failed_requests, 0)
        self.assertEqual(self.metrics.cancelled_request_seconds, 5)

    def test_translation_summary_includes_failed_request_and_backoff_in_whole_run(self):
        responses = [Response(self.clock, 8, ConnectionError("offline")),
                     Response(self.clock, 3, {"response": "1. 你好", "done": True})]
        logs = []
        events = [core.SubtitleEvent("00:00:01,000", "00:00:02,000", "Hello.")]
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(core.time, "monotonic", self.clock), \
                patch.object(core.time, "sleep", self.clock.advance), \
                patch.object(core, "_write_ollama_diagnostic"), \
                patch.object(core.urllib.request, "urlopen", side_effect=responses):
            folder = Path(directory)
            core.translate_events(events, folder / "out.srt", folder / "cache.jsonl", "zh-CN", "en", logs.append)
        self.assertTrue(any("整轮实际耗时13.00秒" in line and "失败8.00秒" in line
                            and "重试等待2.00秒" in line for line in logs))

    def test_failed_translation_logs_whole_run_even_when_no_request_completes(self):
        responses = [Response(self.clock, 61, TimeoutError("idle")) for _ in range(2)]
        logs = []
        events = [core.SubtitleEvent("00:00:01,000", "00:00:02,000", "Hello.")]
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(core.time, "monotonic", self.clock), \
                patch.object(core.time, "sleep", self.clock.advance), \
                patch.object(core, "_write_ollama_diagnostic"), \
                patch.object(core.urllib.request, "urlopen", side_effect=responses):
            folder = Path(directory)
            with self.assertRaisesRegex(RuntimeError, "2 次尝试上限"):
                core.translate_events(events, folder / "out.srt", folder / "cache.jsonl", "zh-CN", "en", logs.append)
        self.assertTrue(any("整轮实际耗时124.00秒" in line and "失败122.00秒" in line
                            and "完成0次" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
