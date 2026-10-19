"""Only healthy partial clean EOF narrows a batch; valid progress is durable."""
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import subtitle_tool_core as core


class Stream:
    status = 200

    def __init__(self, *items):
        self.items = list(items)

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def readline(self):
        item = self.items.pop(0) if self.items else b""
        if isinstance(item, BaseException):
            raise item
        return json.dumps(item).encode("utf-8") + b"\n" if isinstance(item, dict) else item


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TimedStream(Stream):
    def __init__(self, clock, seconds, *items):
        super().__init__(*items)
        self.clock, self.seconds = clock, seconds
        self.socket = Mock()
        self.fp = Mock(raw=Mock(_sock=self.socket))

    def readline(self):
        self.clock.advance(self.seconds)
        return super().readline()


def incomplete():
    return Stream(dict(response="1. PRIVATE UNFINISHED TRANSLATION", done=False))


def complete(indices):
    text = "\n".join(f"{i}. Übersetzung {idx}" for i, idx in enumerate(indices, 1))
    return Stream(dict(response=text, done=True))


def prompt_indices(request):
    body = json.loads(request.data)
    return tuple(int(number) for number in re.findall(r"^\d+\. Source cue (\d+)\s*$",
                                                      body["prompt"], re.MULTILINE))


class StreamPolicyTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.object(core, "_OLLAMA_SERVER_PROCESS", None).start()
        patch.object(core, "_write_ollama_diagnostic").start()
        patch.object(core.time, "sleep").start()

    def batch(self, count=12, **kwargs):
        return core.translate_batch([f"Source cue {i}" for i in range(1, count + 1)],
                                    "de", "en", "fixture-model", "http://fixture.invalid", 90,
                                    **kwargs)

    def test_normal_batch_has_one_request_no_health_probe(self):
        with patch.object(core.urllib.request, "urlopen", return_value=complete(range(1, 13))) as request, \
                patch.object(core, "ollama_available") as health:
            self.assertEqual(self.batch(), [f"Übersetzung {i}" for i in range(1, 13)])
        self.assertEqual(request.call_count, 1)
        health.assert_not_called()

    def test_multilevel_clean_eof_discards_partial_and_maps_callback_offsets(self):
        requests, callbacks = [], []

        def generate(request, **_kwargs):
            indices = prompt_indices(request)
            requests.append(indices)
            return incomplete() if indices in (tuple(range(1, 13)), tuple(range(1, 7))) else complete(indices)

        metrics = core.TranslationMetrics()
        original_indices = tuple(i * 3 for i in range(1, 13))
        metrics.diagnostic_indices = original_indices
        with patch.object(core.urllib.request, "urlopen", side_effect=generate), \
                patch.object(core, "ollama_available", return_value=True) as health:
            result = self.batch(metrics=metrics,
                                on_fallback_translation=lambda offset, text: callbacks.append((offset, text)))
        self.assertEqual(result, [f"Übersetzung {i}" for i in range(1, 13)])
        self.assertEqual(requests, [tuple(range(1, 13)), tuple(range(1, 7)), (1, 2, 3),
                                    (4, 5, 6), tuple(range(7, 13))])
        self.assertEqual(callbacks, list(enumerate(result)))
        self.assertEqual(metrics.diagnostic_indices, original_indices)
        self.assertEqual(metrics.requests, 5)
        self.assertEqual(metrics.completed_requests, 3)
        self.assertEqual(metrics.failed_requests, 2)
        self.assertEqual(metrics.retries, 0)
        self.assertEqual(health.call_count, 2)

    def test_batch_split_also_works_without_metrics_or_callback(self):
        with patch.object(core.urllib.request, "urlopen", side_effect=[incomplete(), complete([1]), complete([2])]), \
                patch.object(core, "ollama_available", return_value=True):
            self.assertEqual(self.batch(2), ["Übersetzung 1", "Übersetzung 2"])

    def test_finished_children_persist_before_later_single_eof_exhausts(self):
        events = [core.SubtitleEvent(f"00:00:0{i},000", f"00:00:0{i},900", f"Source cue {i}")
                  for i in range(1, 5)]
        sequence = [incomplete(), complete([1, 2]), incomplete(), complete([3]), incomplete(), incomplete()]
        messages = []
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            output, cache = folder / "out.srt", folder / "cache.jsonl"
            with patch.object(core.urllib.request, "urlopen", side_effect=sequence) as request, \
                    patch.object(core, "ollama_available", return_value=True), \
                    patch.object(core, "ensure_ollama_running") as restart:
                with self.assertRaises(RuntimeError):
                    core.translate_events(events, output, cache, "de", "en", messages.append, batch_size=4)
            saved = core.load_cache(cache, "de", {i: event.text for i, event in enumerate(events, 1)})[0]
            self.assertEqual(saved, {1: "Übersetzung 1", 2: "Übersetzung 2", 3: "Übersetzung 3"})
            self.assertFalse(output.exists())
            self.assertEqual(request.call_count, 6)
            restart.assert_not_called()
            with patch.object(core.urllib.request, "urlopen", return_value=complete([4])) as request:
                core.translate_events(events, output, cache, "de", "en", messages.append)
            self.assertEqual(prompt_indices(request.call_args.args[0]), (4,))
            self.assertEqual([(item.start, item.end) for item in core.parse_subtitle(output)],
                             [(item.start, item.end) for item in events])

    def test_unhealthy_service_keeps_old_transport_retry_without_split(self):
        with patch.object(core.urllib.request, "urlopen", side_effect=[incomplete(), complete([1, 2])]) as request, \
                patch.object(core, "ollama_available", return_value=False) as health:
            self.assertEqual(self.batch(2), ["Übersetzung 1", "Übersetzung 2"])
        self.assertEqual(request.call_count, 2)
        self.assertEqual([prompt_indices(call.args[0]) for call in request.call_args_list], [(1, 2), (1, 2)])
        self.assertEqual(health.call_count, 1)

    def test_no_response_content_or_partial_json_keeps_old_retry(self):
        for first in (Stream(dict(response="", done=False)), Stream(b'{"response":"cut'),
                      Stream(dict(response="private partial", done=False), b'{"response":"cut')):
            with self.subTest(first=first), \
                    patch.object(core.urllib.request, "urlopen", side_effect=[first, complete([1, 2])]) as request, \
                    patch.object(core, "ollama_available") as health:
                self.assertEqual(self.batch(2), ["Übersetzung 1", "Übersetzung 2"])
            self.assertEqual(request.call_count, 2)
            self.assertEqual([prompt_indices(call.args[0]) for call in request.call_args_list], [(1, 2), (1, 2)])
            health.assert_not_called()

    def test_public_retry_remains_transport_compatible(self):
        with patch.object(core.urllib.request, "urlopen", side_effect=[incomplete(), complete([1, 2])]) as request, \
                patch.object(core, "ollama_available") as health:
            self.assertEqual(core.call_ollama_retry("private prompt", "fixture-model", "http://fixture.invalid", 90),
                             "1. Übersetzung 1\n2. Übersetzung 2")
        self.assertEqual(request.call_count, 2)
        health.assert_not_called()

    def test_single_cue_eof_after_format_fallback_retries_without_false_success(self):
        malformed = Stream(dict(response="unusable batch numbering", done=True))
        responses = [malformed, incomplete(), complete([1]), complete([2])]
        with patch.object(core.urllib.request, "urlopen", side_effect=responses) as request, \
                patch.object(core, "ollama_available") as health:
            self.assertEqual(self.batch(2), ["Übersetzung 1", "Übersetzung 2"])
        self.assertEqual(request.call_count, 4)
        health.assert_not_called()

    def test_cancellation_after_health_probe_stops_before_child_generation(self):
        cancel = threading.Event()

        def cancelled_probe(*_args, **_kwargs):
            cancel.set()
            return True

        with patch.object(core.urllib.request, "urlopen", return_value=incomplete()) as request, \
                patch.object(core, "ollama_available", side_effect=cancelled_probe), \
                patch.object(core, "ensure_ollama_running") as restart:
            with self.assertRaises(core.CancelledError):
                self.batch(2, cancel_event=cancel)
        self.assertEqual(request.call_count, 1)
        restart.assert_not_called()
        self.assertIsNone(core._OLLAMA_SPLIT_BUDGET.get())
        self.assertFalse(core._OLLAMA_BATCH_STREAM_RECOVERY.get())

    def test_explicit_output_format_error_without_response_text_can_split(self):
        message = "The model produced output that does not match the expected JSON format"
        for error in (message, {"message": message}):
            with self.subTest(error=error), \
                    patch.object(core.urllib.request, "urlopen", side_effect=[
                        Stream(dict(error=error)), complete([1]), complete([2])]) as request, \
                    patch.object(core, "ollama_available", return_value=True) as health:
                self.assertEqual(self.batch(2), ["Übersetzung 1", "Übersetzung 2"])
            self.assertEqual(request.call_count, 3)
            health.assert_called_once()
            self.assertIsNone(core._OLLAMA_SPLIT_BUDGET.get())
            self.assertFalse(core._OLLAMA_BATCH_STREAM_RECOVERY.get())

    def test_model_missing_oom_and_unknown_engine_errors_never_split_or_retry(self):
        errors = ["CUDA error: out of memory", "model 'missing-model' not found",
                  {"message": "failed to load model: out of memory"},
                  {"message": "The model produced output that does not match expected shape"},
                  "CUDA failure. The model produced output that does not match the expected JSON format"]
        for error in errors:
            with self.subTest(error=error), \
                    patch.object(core.urllib.request, "urlopen", return_value=Stream(dict(error=error))) as request, \
                    patch.object(core, "ollama_available") as health:
                with self.assertRaisesRegex(RuntimeError, "CUDA|model|shape"):
                    self.batch(2)
            self.assertEqual(request.call_count, 1)
            health.assert_not_called()
            self.assertIsNone(core._OLLAMA_SPLIT_BUDGET.get())
            self.assertFalse(core._OLLAMA_BATCH_STREAM_RECOVERY.get())

    def test_http_status_is_required_before_content_error_can_split(self):
        first = Stream(dict(error="The model produced output that does not match the expected JSON format"))
        first.status = 500
        with patch.object(core.urllib.request, "urlopen", side_effect=[first, complete([1, 2])]) as request, \
                patch.object(core, "ollama_available") as health:
            self.assertEqual(self.batch(2), ["Übersetzung 1", "Übersetzung 2"])
        self.assertEqual(request.call_count, 2)
        health.assert_not_called()

    def test_shared_deadline_caps_initial_socket_and_each_blocking_read(self):
        clock = Clock()
        response = TimedStream(clock, 1,
                               dict(response="1. Über", done=False), dict(response="setzung 1", done=True))
        budget = core._TranslationSplitBudget(deadline=105.0, remaining_requests=1)
        token = core._OLLAMA_SPLIT_BUDGET.set(budget)
        try:
            with patch.object(core.time, "monotonic", clock), \
                    patch.object(core.urllib.request, "urlopen", return_value=response) as request:
                result = core.call_ollama("fixture prompt", "fixture-model", "http://fixture.invalid", 600)
            self.assertEqual(result, "1. Übersetzung 1")
            self.assertEqual(request.call_args.kwargs["timeout"], 5.0)
            self.assertEqual([call.args[0] for call in response.socket.settimeout.call_args_list], [5.0, 4.0])
            self.assertEqual(budget.remaining_requests, 0)
        finally:
            core._OLLAMA_SPLIT_BUDGET.reset(token)
        self.assertIsNone(core._OLLAMA_SPLIT_BUDGET.get())

    def test_time_budget_expiry_keeps_earlier_child_cache_and_resets_context(self):
        clock = Clock()
        events = [core.SubtitleEvent(f"00:00:0{i},000", f"00:00:0{i},900", f"Source cue {i}")
                  for i in (1, 2)]
        responses = [incomplete(), TimedStream(clock, 10, dict(response="1. Übersetzung 1", done=True)),
                     TimedStream(clock, 81, dict(response="PRIVATE INCOMPLETE", done=False))]
        messages = []
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            output, cache = folder / "out.srt", folder / "cache.jsonl"
            with patch.object(core.time, "monotonic", clock), \
                    patch.object(core.urllib.request, "urlopen", side_effect=responses) as request, \
                    patch.object(core, "ollama_available", return_value=True):
                with self.assertRaisesRegex(core.TranslationRecoveryLimitError, "时间预算"):
                    core.translate_events(events, output, cache, "de", "en", messages.append, timeout=90)
            self.assertEqual(request.call_count, 3)
            saved = core.load_cache(cache, "de", {i: event.text for i, event in enumerate(events, 1)})[0]
            self.assertEqual(saved, {1: "Übersetzung 1"})
            self.assertFalse(output.exists())
        self.assertIsNone(core._OLLAMA_SPLIT_BUDGET.get())
        self.assertFalse(core._OLLAMA_BATCH_STREAM_RECOVERY.get())
        with patch.object(core.urllib.request, "urlopen", return_value=complete([1, 2])):
            self.assertEqual(self.batch(2), ["Übersetzung 1", "Übersetzung 2"])

    def test_actual_request_bound_includes_root_transport_retry_and_keeps_cache(self):
        events = [core.SubtitleEvent(f"00:00:0{i},000", f"00:00:0{i},900", f"Source cue {i}")
                  for i in (1, 2)]
        messages = []
        responses = [incomplete(), incomplete(), complete([1])]
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            output, cache = folder / "out.srt", folder / "cache.jsonl"
            with patch.object(core.urllib.request, "urlopen", side_effect=responses) as request, \
                    patch.object(core, "ollama_available", side_effect=[False, True]) as health:
                with self.assertRaisesRegex(core.TranslationRecoveryLimitError, "请求上限"):
                    core.translate_events(events, output, cache, "de", "en", messages.append)
            self.assertEqual(request.call_count, 2 * len(events) - 1)
            self.assertEqual([prompt_indices(call.args[0]) for call in request.call_args_list],
                             [(1, 2), (1, 2), (1,)])
            self.assertEqual(health.call_count, 2)
            saved = core.load_cache(cache, "de", {i: event.text for i, event in enumerate(events, 1)})[0]
            self.assertEqual(saved, {1: "Übersetzung 1"})
            self.assertFalse(output.exists())
            self.assertTrue(any("请求3次，完成1次，重试1次" in message for message in messages))
        self.assertIsNone(core._OLLAMA_SPLIT_BUDGET.get())
        self.assertFalse(core._OLLAMA_BATCH_STREAM_RECOVERY.get())

    def test_retry_backoff_is_capped_by_shared_remaining_time(self):
        clock = Clock()
        budget = core._TranslationSplitBudget(deadline=100.5, remaining_requests=2)
        metrics = core.TranslationMetrics()
        token = core._OLLAMA_SPLIT_BUDGET.set(budget)
        try:
            with patch.object(core.time, "monotonic", clock), \
                    patch.object(core.time, "sleep", side_effect=clock.advance) as sleep, \
                    patch.object(core.urllib.request, "urlopen", return_value=Stream(ConnectionResetError("offline"))) as request:
                with self.assertRaisesRegex(core.TranslationRecoveryLimitError, "时间预算"):
                    core.call_ollama_retry("fixture prompt", "fixture-model", "http://fixture.invalid", 90,
                                           metrics=metrics)
            self.assertEqual(request.call_count, 1)
            sleep.assert_called_once_with(0.5)
            self.assertEqual(metrics.retries, 0)
            self.assertEqual(metrics.retry_wait_seconds, 0.5)
        finally:
            core._OLLAMA_SPLIT_BUDGET.reset(token)

    def test_caller_context_is_restored_including_an_existing_budget(self):
        clock = Clock()
        budget = core._TranslationSplitBudget(deadline=110.0, remaining_requests=1)
        budget_token = core._OLLAMA_SPLIT_BUDGET.set(budget)
        mode_token = core._OLLAMA_BATCH_STREAM_RECOVERY.set(True)
        try:
            with patch.object(core.time, "monotonic", clock), \
                    patch.object(core.urllib.request, "urlopen", return_value=complete([1])), \
                    patch.object(core, "ollama_available") as health:
                self.assertEqual(self.batch(1), ["Übersetzung 1"])
            self.assertIs(core._OLLAMA_SPLIT_BUDGET.get(), budget)
            self.assertTrue(core._OLLAMA_BATCH_STREAM_RECOVERY.get())
            self.assertEqual(budget.remaining_requests, 0)
            health.assert_not_called()
        finally:
            core._OLLAMA_BATCH_STREAM_RECOVERY.reset(mode_token)
            core._OLLAMA_SPLIT_BUDGET.reset(budget_token)

    def test_cancellation_inside_active_split_keeps_earlier_child_and_resets_context(self):
        events = [core.SubtitleEvent(f"00:00:0{i},000", f"00:00:0{i},900", f"Source cue {i}")
                  for i in (1, 2)]
        responses = [incomplete(), complete([1]), Stream(core.CancelledError("stopped in child"))]
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            output, cache = folder / "out.srt", folder / "cache.jsonl"
            with patch.object(core.urllib.request, "urlopen", side_effect=responses) as request, \
                    patch.object(core, "ollama_available", return_value=True), \
                    patch.object(core, "ensure_ollama_running") as restart:
                with self.assertRaisesRegex(core.CancelledError, "stopped in child"):
                    core.translate_events(events, output, cache, "de", "en", lambda _: None)
            self.assertEqual(request.call_count, 3)
            restart.assert_not_called()
            self.assertFalse(output.exists())
            self.assertEqual(core.load_cache(cache, "de", {i: event.text for i, event in enumerate(events, 1)})[0],
                             {1: "Übersetzung 1"})
        self.assertIsNone(core._OLLAMA_SPLIT_BUDGET.get())
        self.assertFalse(core._OLLAMA_BATCH_STREAM_RECOVERY.get())

    def test_budget_expiry_before_scope_registration_does_not_steal_active_count(self):
        gate = threading.Semaphore(1)
        budget = core._TranslationSplitBudget(deadline=0, remaining_requests=1)
        token = core._OLLAMA_SPLIT_BUDGET.set(budget)
        try:
            with patch.object(core, "_OLLAMA_STOPPING", True), \
                    patch.object(core, "_OLLAMA_ACTIVE_REQUESTS", 2), \
                    patch.object(core, "_OLLAMA_INFERENCE_GATE", gate):
                with self.assertRaisesRegex(core.TranslationRecoveryLimitError, "时间预算"):
                    with core.ollama_request_scope():
                        self.fail("an expired waiting request cannot register")
                self.assertEqual(core._OLLAMA_ACTIVE_REQUESTS, 2)
                self.assertTrue(gate.acquire(blocking=False))
                gate.release()
                self.assertIs(core._OLLAMA_SPLIT_BUDGET.get(), budget)
        finally:
            core._OLLAMA_SPLIT_BUDGET.reset(token)
        self.assertIsNone(core._OLLAMA_SPLIT_BUDGET.get())

    def test_cancel_before_scope_registration_does_not_steal_active_count(self):
        gate = threading.Semaphore(1)
        cancel = threading.Event()
        cancel.set()
        budget = core._TranslationSplitBudget(deadline=core.time.monotonic() + 30, remaining_requests=1)
        token = core._OLLAMA_SPLIT_BUDGET.set(budget)
        try:
            with patch.object(core, "_OLLAMA_STOPPING", True), \
                    patch.object(core, "_OLLAMA_ACTIVE_REQUESTS", 2), \
                    patch.object(core, "_OLLAMA_INFERENCE_GATE", gate):
                with self.assertRaises(core.CancelledError):
                    with core.ollama_request_scope(cancel):
                        self.fail("a cancelled waiting request cannot register")
                self.assertEqual(core._OLLAMA_ACTIVE_REQUESTS, 2)
                self.assertTrue(gate.acquire(blocking=False))
                gate.release()
                self.assertIs(core._OLLAMA_SPLIT_BUDGET.get(), budget)
        finally:
            core._OLLAMA_SPLIT_BUDGET.reset(token)
        self.assertIsNone(core._OLLAMA_SPLIT_BUDGET.get())


if __name__ == "__main__":
    unittest.main()
