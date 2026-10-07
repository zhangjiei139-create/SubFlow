# -*- coding: utf-8 -*-
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import audio_offset_verifier
import subtitle_tool_core as core


class _FinishedProcess:
    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.pid = 123

    def communicate(self, timeout=None):
        return self._stdout, self._stderr

    def poll(self):
        return self.returncode


class ExternalToolResilienceTests(unittest.TestCase):
    def test_mkvtoolnix_warning_exit_is_not_a_failure(self):
        messages: list[str] = []
        process = _FinishedProcess(
            1,
            "警告: 字幕条目已按时间重新排列。\n进度: 100%".encode("utf-8"),
        )
        with mock.patch.object(subprocess, "Popen", return_value=process):
            completed = core.run_command([r"C:\Tools\mkvmerge.exe", "input.mkv"], log=messages.append)
        self.assertEqual(completed.returncode, 1)
        self.assertTrue(any("已完成" in message and "警告" in message for message in messages))

    def test_non_mkvtool_failure_still_raises_with_local_codepage_text(self):
        process = _FinishedProcess(2, "错误: 找不到输入文件".encode("gb18030"))
        with mock.patch.object(subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(RuntimeError, "找不到输入文件"):
                core.run_command([r"C:\Tools\worker.exe"])

    def test_whisper_receives_staged_short_probe_paths(self):
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name)
            long_root = root / ("中文路径" * 18)
            long_root.mkdir()
            wavs = {}
            starts = {}
            for index in range(2):
                wav = long_root / f"clip-{index}.wav"
                wav.write_bytes(b"RIFF-test")
                wavs[index] = wav
                starts[index] = float(index * 100)

            seen_paths: list[Path] = []

            def fake_run(args, *, cancel, cwd=None):
                del cancel, cwd
                for raw in args:
                    if str(raw).lower().endswith(".wav"):
                        path = Path(raw)
                        seen_paths.append(path)
                        Path(str(path) + ".srt").write_text(
                            "1\n00:00:00,000 --> 00:00:01,000\nhello world\n",
                            encoding="utf-8",
                        )

            with mock.patch.object(audio_offset_verifier, "_run_tool", side_effect=fake_run):
                result = audio_offset_verifier._transcribe_clips(
                    wavs,
                    starts,
                    whisper="whisper-cli.exe",
                    whisper_model="ggml-base.bin",
                    audio_language="en",
                    translate_to_english=False,
                    cancel=None,
                )

            self.assertEqual(set(result), {0, 1})
            self.assertTrue(all(path.parent != long_root for path in seen_paths))
            self.assertTrue(all(path.name.startswith("probe-") for path in seen_paths))


if __name__ == "__main__":
    unittest.main()
