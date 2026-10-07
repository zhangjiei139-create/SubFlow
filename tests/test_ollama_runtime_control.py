import json
import subprocess
import threading
import unittest
from unittest.mock import Mock, patch

import ai_runtime
import subtitle_tool_core as core


class FakeResponse:
    def __init__(self, lines):
        self.lines = list(lines)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def readline(self):
        return self.lines.pop(0) if self.lines else b""


class OllamaRuntimeControlTests(unittest.TestCase):
    def setUp(self):
        with core._OLLAMA_STATE_CHANGED:
            core._OLLAMA_LEASES = 0
            core._OLLAMA_ACTIVE_REQUESTS = 0
            core._OLLAMA_STOPPING = False
            core._OLLAMA_SERVER_PROCESS = None
        core._OLLAMA_INFERENCE_GATE = threading.Semaphore(1)

    def test_environment_limits_ollama_to_one_inference_lane(self):
        with patch.object(ai_runtime, "resolve_model_root", return_value="X:/models"):
            environment = ai_runtime.ollama_environment()
        self.assertEqual(environment["OLLAMA_NUM_PARALLEL"], "1")
        self.assertEqual(environment["OLLAMA_MAX_LOADED_MODELS"], "1")

    def test_request_uses_bounded_context_and_streaming(self):
        response = FakeResponse([
            b'{"response":"OK","done":false}\n',
            b'{"done":true}\n',
        ])
        with patch.object(core.urllib.request, "urlopen", return_value=response) as urlopen:
            result = core.call_ollama("hello", "qwen3:8b", "http://127.0.0.1:11434", 600)
        payload = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(result, "OK")
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["keep_alive"], "10m")
        self.assertEqual(payload["options"]["num_ctx"], 4096)
        self.assertEqual(core._OLLAMA_ACTIVE_REQUESTS, 0)

    def test_request_has_hard_wall_clock_deadline(self):
        response = FakeResponse([b'{"response":"late","done":false}\n'])
        with patch.object(core, "OLLAMA_REQUEST_MAX_SECONDS", 1), patch.object(
            core.time, "monotonic", side_effect=[0.0, 2.0]
        ), patch.object(core.urllib.request, "urlopen", return_value=response):
            with self.assertRaises(TimeoutError):
                core.call_ollama("hello", "qwen3:8b", "http://127.0.0.1:11434", 600)
        self.assertEqual(core._OLLAMA_ACTIVE_REQUESTS, 0)

    def test_unload_never_runs_while_request_is_active(self):
        core._OLLAMA_ACTIVE_REQUESTS = 1
        with patch.object(core.subprocess, "run") as run:
            core.unload_ollama_model()
        run.assert_not_called()
        self.assertFalse(core._OLLAMA_STOPPING)

    def test_stuck_unload_terminates_only_owned_server(self):
        process = Mock()
        process.poll.return_value = None
        core._OLLAMA_SERVER_PROCESS = process
        messages = []
        with patch.object(core.Path, "exists", return_value=True), patch.object(
            core.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd="ollama stop", timeout=8),
        ), patch.object(core, "terminate_process_tree") as terminate:
            core.unload_ollama_model(messages.append)
        terminate.assert_called_once_with(process)
        self.assertIsNone(core._OLLAMA_SERVER_PROCESS)
        self.assertTrue(any("安全结束" in message for message in messages))
        self.assertFalse(core._OLLAMA_STOPPING)


if __name__ == "__main__":
    unittest.main()
