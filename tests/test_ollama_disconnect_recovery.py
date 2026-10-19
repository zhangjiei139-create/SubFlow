import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import subtitle_tool_core as core


class StreamResponse:
    def __init__(self, *lines):
        self.lines = list(lines)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def readline(self):
        value = self.lines.pop(0) if self.lines else b""
        if isinstance(value, Exception):
            raise value
        return value


def complete(text):
    return StreamResponse(json.dumps({"response": text, "done": True}).encode() + b"\n")


class DisconnectRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.saved = {
            name: getattr(core, name) for name in (
                "_OLLAMA_SERVER_PROCESS", "_OLLAMA_SERVER_HOST", "_OLLAMA_ACTIVE_REQUESTS",
                "_OLLAMA_STOPPING", "_OLLAMA_INFERENCE_GATE",
            )
        }
        core._OLLAMA_SERVER_PROCESS = None
        core._OLLAMA_SERVER_HOST = None
        core._OLLAMA_ACTIVE_REQUESTS = 0
        core._OLLAMA_STOPPING = False
        core._OLLAMA_INFERENCE_GATE = threading.Semaphore(1)
        self.addCleanup(lambda: [setattr(core, key, value) for key, value in self.saved.items()])
        self.diagnostic = patch.object(core, "_write_ollama_diagnostic").start()
        self.addCleanup(patch.stopall)

    def retry_stream(self, first):
        metrics = core.TranslationMetrics()
        with patch.object(core.urllib.request, "urlopen", side_effect=[first, complete("complete answer")]), \
                patch.object(core.time, "sleep"):
            value = core.call_ollama_retry("secret prompt", "model", core.OLLAMA_HOST, 90, metrics=metrics)
        self.assertEqual(value, "complete answer")
        self.assertEqual(metrics.requests, 2)
        self.assertEqual(metrics.completed_requests, 1)
        self.assertEqual(metrics.retries, 1)
        self.assertEqual(core._OLLAMA_ACTIVE_REQUESTS, 0)
        self.assertTrue(core._OLLAMA_INFERENCE_GATE.acquire(blocking=False))
        core._OLLAMA_INFERENCE_GATE.release()

    def test_connection_reset_in_readline_retries_without_partial_text(self):
        self.retry_stream(StreamResponse(
            b'{"response":"unfinished private text","done":false}\n',
            ConnectionResetError(10054, "reset"),
        ))
        self.assertNotIn("secret prompt", str(self.diagnostic.call_args_list))
        self.assertNotIn("unfinished private text", str(self.diagnostic.call_args_list))

    def test_eof_without_done_is_retried(self):
        self.retry_stream(StreamResponse(b'{"response":"unfinished","done":false}\n'))

    def test_truncated_json_line_is_retried(self):
        self.retry_stream(StreamResponse(b'{"response":"unfinished'))

    def test_http_incomplete_read_is_retried(self):
        self.retry_stream(StreamResponse(core.http.client.IncompleteRead(b"partial", 20)))

    def test_finished_batch_cache_survives_exhausted_next_batch(self):
        events = [core.SubtitleEvent("00:00:00,000", "00:00:01,000", text)
                  for text in ("Hello", "Goodbye")]
        metrics_messages = []
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "cache.jsonl"
            output = Path(directory) / "out.srt"
            responses = [complete("1. Hallo"), ConnectionResetError(10054, "reset"),
                         ConnectionResetError(10054, "reset")]
            with patch.object(core.urllib.request, "urlopen", side_effect=responses) as request, \
                    patch.object(core.time, "sleep"):
                with self.assertRaisesRegex(RuntimeError, "2 次尝试上限"):
                    core.translate_events(events, output, cache, "de", "en", metrics_messages.append, batch_size=1)
            records = [json.loads(line) for line in cache.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([(item["index"], item["translation"]) for item in records], [(1, "Hallo")])
            self.assertFalse(output.exists())
            self.assertEqual(request.call_count, 3)
            self.assertTrue(any("请求3次，完成1次，重试1次" in text for text in metrics_messages))

    def test_cancel_after_error_skips_retry_and_engine_restart(self):
        event = threading.Event()

        def cancelled_request(*_args, **_kwargs):
            event.set()
            raise ConnectionResetError(10054, "reset")

        with patch.object(core, "call_ollama", side_effect=cancelled_request) as call, \
                patch.object(core, "ensure_ollama_running") as ensure:
            with self.assertRaises(core.CancelledError):
                core.call_ollama_retry("hello", "model", core.OLLAMA_HOST, 90, cancel_event=event)
        self.assertEqual(call.call_count, 1)
        ensure.assert_not_called()

    def test_cancel_during_backoff_is_immediate(self):
        event = threading.Event()
        with patch.object(core, "call_ollama", side_effect=ConnectionResetError(10054, "reset")) as call, \
                patch.object(event, "wait", side_effect=lambda _seconds: event.set()) as wait:
            with self.assertRaises(core.CancelledError):
                core.call_ollama_retry("hello", "model", core.OLLAMA_HOST, 90, cancel_event=event)
        self.assertEqual(call.call_count, 1)
        wait.assert_called_once_with(2)

    def test_semantic_model_error_is_not_retried(self):
        with patch.object(core, "call_ollama", side_effect=RuntimeError("model missing")) as call:
            with self.assertRaisesRegex(RuntimeError, "model missing"):
                core.call_ollama_retry("hello", "model", core.OLLAMA_HOST, 90)
        self.assertEqual(call.call_count, 1)

    def _check_restart(self, *, process, process_host=None, active=0, expected=False):
        core._OLLAMA_SERVER_PROCESS = process
        core._OLLAMA_SERVER_HOST = process_host
        core._OLLAMA_ACTIVE_REQUESTS = active
        with patch.object(core, "call_ollama", side_effect=[ConnectionResetError(10054, "reset"), "OK"]), \
                patch.object(core.time, "sleep"), patch.object(core, "ensure_ollama_running") as ensure, \
                patch.object(core, "terminate_process_tree") as terminate:
            self.assertEqual(core.call_ollama_retry("hello", "model", core.OLLAMA_HOST, 90), "OK")
        self.assertEqual(ensure.call_count, int(expected))
        terminate.assert_not_called()

    def test_owned_exited_engine_is_restarted(self):
        process = Mock(pid=123)
        process.poll.return_value = 13
        self._check_restart(process=process, process_host=core.OLLAMA_HOST, expected=True)

    def test_external_engine_is_not_restarted(self):
        self._check_restart(process=None)

    def test_owned_live_engine_is_not_restarted(self):
        process = Mock(pid=123)
        process.poll.return_value = None
        self._check_restart(process=process, process_host=core.OLLAMA_HOST)

    def test_other_host_is_not_restarted(self):
        process = Mock(pid=123)
        process.poll.return_value = 13
        self._check_restart(process=process, process_host="http://other-host")

    def test_other_active_request_prevents_restart(self):
        process = Mock(pid=123)
        process.poll.return_value = 13
        self._check_restart(process=process, process_host=core.OLLAMA_HOST, active=1)


class EngineDiagnosticsTests(unittest.TestCase):
    def test_capture_keeps_engine_errors_and_exit_without_dialogue(self):
        stream = io.BytesIO(
            b'time=2026 level=ERROR source=runner.go msg="runner terminated: exit status 2"\n'
            b'CUDA error: out of memory\n'
            b'{"prompt":"PRIVATE PROMPT", "response":"PRIVATE RESPONSE"}\n'
            b'time=2026 level=DEBUG prompt="MORE PRIVATE TEXT"\n'
            b'arbitrary dialogue continuation\n'
            + b'x' * 20000 + b'\n'
            b'ggml_cuda_init: found 1 CUDA device\n'
        )
        process = Mock(pid=123, stdout=stream)
        process.wait.return_value = 2
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ollama-engine.log"
            core._capture_ollama_diagnostics(process, path)
            text = path.read_text(encoding="utf-8")
        self.assertIn("CUDA error: out of memory", text)
        self.assertIn("runner terminated: exit status 2", text)
        self.assertIn("exit_code=2", text)
        self.assertIn("found 1 CUDA device", text)
        self.assertNotIn("PRIVATE", text)
        self.assertNotIn("dialogue", text)
        self.assertTrue(stream.closed)

    def test_diagnostics_are_bounded_and_rotate_without_open_handles(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(core, "_OLLAMA_DIAGNOSTIC_MAX_BYTES", 200):
            path = Path(directory) / "ollama-engine.log"
            for index in range(20):
                core._write_ollama_diagnostic(f"event {index}: " + "x" * 60, path)
            files = list(Path(directory).glob("*.log"))
            self.assertEqual(len(files), 2)
            self.assertTrue(all(item.stat().st_size <= 200 for item in files))
            self.assertIn("event 19", path.read_text(encoding="utf-8"))
            # On Windows this also catches leaked file handles after writes.
            path.replace(path.with_suffix(".moved"))


class EngineStartupTests(unittest.TestCase):
    def setUp(self):
        saved_process, saved_host = core._OLLAMA_SERVER_PROCESS, core._OLLAMA_SERVER_HOST
        self.addCleanup(lambda: setattr(core, "_OLLAMA_SERVER_PROCESS", saved_process))
        self.addCleanup(lambda: setattr(core, "_OLLAMA_SERVER_HOST", saved_host))
        core._OLLAMA_SERVER_PROCESS = None
        core._OLLAMA_SERVER_HOST = None
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.executable = self.root / "ollama.exe"
        self.executable.touch()

    def test_launch_captures_output_without_debug_and_registers_owned_host(self):
        process = Mock(pid=123)
        process.poll.return_value = None
        with patch.object(core, "OLLAMA_EXE", str(self.executable)), \
                patch.object(core, "ollama_runtime_dir", return_value=self.root), \
                patch.object(core, "ollama_available", side_effect=[False, True]), \
                patch.object(core.ai_runtime, "ollama_environment", return_value={"OLLAMA_DEBUG": "true"}), \
                patch.object(core.subprocess, "Popen", return_value=process) as popen, \
                patch.object(core.threading, "Thread") as thread:
            core.ensure_ollama_running()
        self.assertEqual(popen.call_args.kwargs["stdout"], core.subprocess.PIPE)
        self.assertEqual(popen.call_args.kwargs["stderr"], core.subprocess.STDOUT)
        self.assertEqual(popen.call_args.kwargs["env"]["OLLAMA_DEBUG"], "false")
        self.assertIs(thread.call_args.kwargs["target"], core._capture_ollama_diagnostics)
        thread.return_value.start.assert_called_once_with()
        self.assertIs(core._OLLAMA_SERVER_PROCESS, process)
        self.assertEqual(core._OLLAMA_SERVER_HOST, core.OLLAMA_HOST)

    def test_cancellation_during_startup_cleans_up_only_newly_started_engine(self):
        event = threading.Event()
        process = Mock(pid=123)
        process.poll.return_value = None

        def start_reader():
            event.set()

        with patch.object(core, "OLLAMA_EXE", str(self.executable)), \
                patch.object(core, "ollama_runtime_dir", return_value=self.root), \
                patch.object(core, "ollama_available", return_value=False), \
                patch.object(core.ai_runtime, "ollama_environment", return_value={}), \
                patch.object(core.subprocess, "Popen", return_value=process), \
                patch.object(core.threading, "Thread") as thread, \
                patch.object(core, "terminate_process_tree") as terminate:
            thread.return_value.start.side_effect = start_reader
            with self.assertRaises(core.CancelledError):
                core.ensure_ollama_running(cancel_event=event)
        terminate.assert_called_once_with(process)
        self.assertIsNone(core._OLLAMA_SERVER_PROCESS)
        self.assertIsNone(core._OLLAMA_SERVER_HOST)

    def test_unresponsive_existing_engine_is_never_replaced_or_killed(self):
        process = Mock(pid=123)
        process.poll.return_value = None
        core._OLLAMA_SERVER_PROCESS = process
        core._OLLAMA_SERVER_HOST = core.OLLAMA_HOST
        with patch.object(core, "OLLAMA_EXE", str(self.executable)), \
                patch.object(core, "ollama_available", return_value=False), \
                patch.object(core.subprocess, "Popen") as popen, \
                patch.object(core, "terminate_process_tree") as terminate:
            with self.assertRaisesRegex(RuntimeError, "启动超时"):
                core.ensure_ollama_running(wait_seconds=0)
        popen.assert_not_called()
        terminate.assert_not_called()
        self.assertIs(core._OLLAMA_SERVER_PROCESS, process)

    def test_existing_external_service_needs_no_spawn(self):
        with patch.object(core, "ollama_available", return_value=True), \
                patch.object(core.subprocess, "Popen") as popen:
            core.ensure_ollama_running()
        popen.assert_not_called()
        self.assertIsNone(core._OLLAMA_SERVER_PROCESS)


class TranslationEstimateTests(unittest.TestCase):
    def test_cached_rows_and_blank_rows_do_not_inflate_current_run_speed(self):
        events = [core.SubtitleEvent("00:00:00,000", "00:00:01,000", text)
                  for text in ("Hello", "Old", "Next", "Last", "")]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "cache.jsonl"
            core.append_cache_records(cache, "de", [(1, "Hello", "Hallo"), (2, "Old", "Alt")])
            messages = []
            with patch.object(core, "translate_batch", side_effect=[["Nächste"], ["Letzte"]]) as translate, \
                    patch.object(core.time, "monotonic", side_effect=[100.0, 110.0, 120.0]):
                core.translate_events(events, root / "out.srt", cache, "de", "en", messages.append, batch_size=1)
            progress = [text for text in messages if "翻译进度" in text]
            self.assertEqual(progress, ["翻译进度：3/5，预计剩余 10 秒", "翻译进度：4/5，预计剩余 0 秒"])
            self.assertEqual([call.args[0] for call in translate.call_args_list], [["Next"], ["Last"]])

    def test_all_cached_rows_make_no_model_request_or_eta(self):
        events = [core.SubtitleEvent("00:00:00,000", "00:00:01,000", "Hello")]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "cache.jsonl"
            core.append_cache(cache, "de", 1, "Hello", "Hallo")
            messages = []
            with patch.object(core, "translate_batch") as translate:
                core.translate_events(events, root / "out.srt", cache, "de", "en", messages.append)
            translate.assert_not_called()
            self.assertFalse(any("预计剩余" in text for text in messages))

    def test_model_load_sample_count_is_not_described_as_reload_count(self):
        metrics = core.TranslationMetrics()
        metrics.record_done({"load_duration": 10})
        metrics.record_done({"load_duration": 10})
        self.assertIn("模型加载0.00秒（2份响应统计）", metrics.summary())


if __name__ == "__main__":
    unittest.main()
