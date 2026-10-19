"""Recovered request failures have bounded diagnostics, without new requests."""
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


class Stream:
    def __init__(self, items, clock):
        self.items, self.clock = list(items), clock

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def readline(self):
        self.clock.advance(0.5)
        item = self.items.pop(0) if self.items else b""
        if isinstance(item, BaseException):
            raise item
        return json.dumps(item).encode() + b"\n" if isinstance(item, dict) else item


class GenerationDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.logs = []
        self.metrics = core.TranslationMetrics()
        self.metrics.diagnostic_log = self.logs.append
        self.metrics.diagnostic_indices = (41, 44)

    def completed(self, text):
        return Stream([dict(response=text, done=True)], self.clock)

    def faulty(self, case):
        if case == "repeat":
            return Stream([dict(response="private-source 白人 ", done=False)] * 60, self.clock)
        if case == "length":
            return Stream([dict(response="PRIVATE-PARTIAL-TEXT", done=True, done_reason="length")], self.clock)
        return self.completed("PRIVATE-MALFORMED-TEXT\nextra\nextra")

    def batch(self, **kwargs):
        return core.translate_batch(["Hello.", "World."], "de", "en", "fixture-model",
                                    "http://fixture.invalid", 60, metrics=self.metrics, **kwargs)

    def test_repetition_length_and_format_recover_with_original_metrics(self):
        for case, code in (("repeat", "repetition_detected"), ("length", "generation_length_limit"),
                           ("format", "batch_response_format")):
            with self.subTest(case=case):
                self.setUp()
                responses = [self.faulty(case), self.completed("1. Hallo."), self.completed("1. Welt.")]
                with patch.object(core.time, "monotonic", self.clock), \
                        patch.object(core.urllib.request, "urlopen", side_effect=responses) as request:
                    self.assertEqual(self.batch(), ["Hallo.", "Welt."])
                self.assertEqual(request.call_count, 3)
                self.assertEqual(self.metrics.requests, 3)
                self.assertEqual(self.metrics.completed_requests, 3 if case == "format" else 2)
                self.assertEqual(self.metrics.failed_requests, 0 if case == "format" else 1)
                self.assertEqual(self.metrics.retries, 0)
                # Keep the established aggregate counter, including generation fallback.
                self.assertEqual(self.metrics.batch_parse_failures, 1)
                self.assertEqual(self.metrics.fallback_lines, 2)
                self.assertEqual(len(self.logs), 1)
                message = self.logs[0]
                for required in (code, "德语（de）", "字幕第 41—44 行", "本批2条", "请求耗时", "逐条重译"):
                    self.assertIn(required, message)
                self.assertNotIn("private-source", message)
                self.assertNotIn("PRIVATE-", message)
                self.assertIn(f"请求耗时 {self.metrics.request_wall_seconds - 1:.2f} 秒", message)

    def test_transport_retry_does_not_become_a_format_or_generation_failure(self):
        responses = [Stream([ConnectionError("offline")], self.clock), self.completed("1. Hallo.\n2. Welt.")]
        with patch.object(core.time, "monotonic", self.clock), \
                patch.object(core.time, "sleep", self.clock.advance), \
                patch.object(core, "_write_ollama_diagnostic"), \
                patch.object(core.urllib.request, "urlopen", side_effect=responses) as request:
            self.assertEqual(self.batch(), ["Hallo.", "Welt."])
        self.assertEqual(request.call_count, 2)
        self.assertEqual(self.metrics.retries, 1)
        self.assertEqual(self.metrics.failed_requests, 1)
        self.assertEqual(self.metrics.retry_wait_seconds, 2)
        self.assertEqual(self.metrics.batch_parse_failures, 0)
        self.assertEqual(self.metrics.fallback_lines, 0)
        self.assertEqual(self.logs, [])

    def test_stream_cancellation_propagates_without_a_fallback(self):
        with patch.object(core.time, "monotonic", self.clock), \
                patch.object(core.urllib.request, "urlopen",
                             return_value=Stream([core.CancelledError("stopped")], self.clock)) as request:
            with self.assertRaises(core.CancelledError):
                self.batch()
        self.assertEqual(request.call_count, 1)
        self.assertEqual(self.metrics.cancelled_requests, 1)
        self.assertEqual(self.metrics.failed_requests, 0)
        self.assertEqual(self.metrics.batch_parse_failures, 0)
        self.assertEqual(self.logs, [])

    def test_diagnostic_logger_failure_cannot_interrupt_recovered_translation(self):
        def failed_logger(_message):
            raise OSError("log file is unavailable")
        self.metrics.diagnostic_log = failed_logger
        responses = [self.faulty("length"), self.completed("1. Hallo."), self.completed("1. Welt.")]
        with patch.object(core.time, "monotonic", self.clock), \
                patch.object(core.urllib.request, "urlopen", side_effect=responses) as request:
            self.assertEqual(self.batch(), ["Hallo.", "Welt."])
        self.assertEqual(request.call_count, 3)
        self.assertEqual(self.metrics.failed_requests, 1)
        self.assertEqual(self.metrics.fallback_lines, 2)

    def test_logger_error_does_not_hide_new_cancellation(self):
        stop = threading.Event()
        def cancelled_logger(_message):
            stop.set()
            raise OSError("log file is unavailable")
        self.metrics.diagnostic_log = cancelled_logger
        with patch.object(core.time, "monotonic", self.clock), \
                patch.object(core.urllib.request, "urlopen", return_value=self.faulty("length")) as request:
            with self.assertRaises(core.CancelledError):
                self.batch(cancel_event=stop)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(self.metrics.fallback_lines, 0)

    def test_logger_cancellation_is_not_swallowed(self):
        def cancelled_logger(_message):
            raise core.CancelledError("stopped by observer")
        self.metrics.diagnostic_log = cancelled_logger
        with patch.object(core.time, "monotonic", self.clock), \
                patch.object(core.urllib.request, "urlopen", return_value=self.faulty("length")) as request:
            with self.assertRaises(core.CancelledError):
                self.batch()
        self.assertEqual(request.call_count, 1)

    def test_diagnostics_are_bounded_without_changing_recovery_counts(self):
        responses = []
        for _ in range(25):
            responses.extend([self.faulty("format"), self.completed("1. Hallo."), self.completed("1. Welt.")])
        with patch.object(core.time, "monotonic", self.clock), \
                patch.object(core.urllib.request, "urlopen", side_effect=responses) as request:
            for _ in range(25):
                self.assertEqual(self.batch(), ["Hallo.", "Welt."])
        self.assertEqual(request.call_count, 75)
        self.assertEqual(self.metrics.requests, 75)
        self.assertEqual(self.metrics.batch_parse_failures, 25)
        self.assertEqual(self.metrics.fallback_lines, 50)
        self.assertEqual(len(self.logs), 20)
        self.assertIn("已达20条", self.logs[-1])
        self.assertTrue(all(len(message) < 600 for message in self.logs))

    def test_old_metrics_without_observer_remain_compatible(self):
        self.metrics = core.TranslationMetrics()
        responses = [self.faulty("length"), self.completed("1. Hallo."), self.completed("1. Welt.")]
        with patch.object(core.time, "monotonic", self.clock), \
                patch.object(core.urllib.request, "urlopen", side_effect=responses):
            self.assertEqual(self.batch(), ["Hallo.", "Welt."])
        self.assertEqual(self.metrics.batch_parse_failures, 1)
        self.assertEqual(self.logs, [])

    def test_single_cue_generation_error_keeps_existing_propagation(self):
        with patch.object(core.time, "monotonic", self.clock), \
                patch.object(core.urllib.request, "urlopen", return_value=self.faulty("length")) as request:
            with self.assertRaises(core.TranslationGenerationError) as raised:
                core.translate_batch(["Hello."], "de", "en", "fixture", "http://fixture.invalid", 60,
                                     metrics=self.metrics)
        self.assertEqual(raised.exception.reason_code, "generation_length_limit")
        self.assertEqual(request.call_count, 1)
        self.assertEqual(self.metrics.batch_parse_failures, 0)
        self.assertEqual(self.logs, [])

    def test_translate_events_reports_original_cue_indices_with_cached_rows(self):
        events = [core.SubtitleEvent(f"00:00:0{i},000", f"00:00:0{i},900", text)
                  for i, text in enumerate(("Existing.", "Hello.", "World."), 1)]
        responses = [self.faulty("length"), self.completed("1. Hallo."), self.completed("1. Welt.")]
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(core.time, "monotonic", self.clock), \
                patch.object(core.urllib.request, "urlopen", side_effect=responses) as request:
            folder = Path(directory)
            cache = folder / "cache.jsonl"
            core.append_cache_records(cache, "de", [(1, "Existing.", "Vorhanden.")])
            core.translate_events(events, folder / "out.srt", cache, "de", "en", self.logs.append)
            self.assertEqual([event.text for event in core.parse_subtitle(folder / "out.srt")],
                             ["Vorhanden.", "Hallo.", "Welt."])
        self.assertEqual(request.call_count, 3)
        diagnostic = [line for line in self.logs if line.startswith("翻译异常：")]
        self.assertEqual(len(diagnostic), 1)
        self.assertIn("字幕第 2—3 行", diagnostic[0])
        self.assertTrue(any("批次格式回退1批，逐条重译2条" in line for line in self.logs))


if __name__ == "__main__":
    unittest.main()
