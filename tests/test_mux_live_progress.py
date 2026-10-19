from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import subtitle_tool_core as core


class MuxProgressParsingTests(unittest.TestCase):
    def progress(self, input_size=1024 * 1024):
        with patch.object(core, "_system_cpu_times", return_value=None):
            return core._MkvmergeProgress(0.0, input_size)

    def test_split_records_cr_newline_and_non_progress_percentages(self):
        progress = self.progress()
        for part in (b"Movie title 88%\r#GUI#pro", b"gress 5%\r", b"#GUI#progress 25%\n"):
            progress.feed(part)
        messages = []
        with patch.object(core.time, "monotonic", return_value=5), patch.object(core, "_system_cpu_times", return_value=None):
            progress.report(messages.append)
        self.assertIn("进度 25%", messages[0])
        self.assertIn("已用 5.0 秒", messages[0])
        self.assertIn("非磁盘实测速率", messages[0])

    def test_regressing_invalid_progress_and_noise_are_bounded(self):
        progress = self.progress()
        progress.feed(b"#GUI#progress 70%\n")
        progress.feed(b"x" * 1024 * 1024)
        progress.feed(b"#GUI#progress 10%\r#GUI#progress 999%\r")
        self.assertEqual(progress._latest, 70)
        self.assertLessEqual(len(progress._tail), 256)

    def test_logs_are_throttled_and_unknown_progress_is_not_invented(self):
        progress = self.progress()
        messages = []
        with patch.object(core.time, "monotonic", side_effect=[0.2, 4.9, 5.0, 6.0, 10.0]), patch.object(core, "_system_cpu_times", return_value=None):
            for _ in range(5):
                progress.report(messages.append)
        self.assertEqual(len(messages), 2)
        self.assertTrue(all("等待工具报告百分比" in line for line in messages))
        self.assertFalse(any("MiB/s" in line or "进度 0%" in line for line in messages))

    def test_cpu_diagnostic_uses_deltas_and_marks_busy_only_when_measured(self):
        with patch.object(core, "_system_cpu_times", side_effect=[(100, 1000), (105, 1100)]):
            progress = core._MkvmergeProgress(0, 0)
            with patch.object(core.time, "monotonic", return_value=5):
                messages = []
                progress.report(messages.append)
        self.assertIn("CPU 95%", messages[0])
        self.assertIn("繁忙", messages[0])

    def test_reader_preserves_all_bytes_and_close(self):
        output = b"header\n#GUI#progress 35%\r" + b"x" * 100000
        stream = io.BytesIO(output)
        progress = self.progress()
        reader = core._MkvmergeProgressReader(stream, progress)
        self.assertEqual(reader.read(), output)
        self.assertEqual(progress._latest, 35)
        reader.close()
        self.assertTrue(stream.closed)

    def test_native_cpu_counters_preserve_64_bit_values_and_fail_open(self):
        class NativeFunction:
            def __call__(self, idle, kernel, user):
                idle._obj.low, idle._obj.high = 20, 1
                kernel._obj.low, kernel._obj.high = 40, 2
                user._obj.low, user._obj.high = 60, 3
                return 1

        native = SimpleNamespace(GetSystemTimes=NativeFunction())
        self.assertEqual(core._system_cpu_times(_kernel32=native), ((1 << 32) + 20, (5 << 32) + 100))
        self.assertIsNone(core._system_cpu_times(_kernel32=SimpleNamespace()))


class MuxProgressChildProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.mkv"
        self.source.write_bytes(b"x" * 4096)
        self.messages = []
        self.started = []
        self.real_popen = subprocess.Popen

    def run_child(self, script, *, log=None, cancel=None, name="mkvmerge.exe", extra=None):
        def spawn(args, **kwargs):
            if Path(args[0]).name == name:
                self.started.append(list(args))
                child = self.real_popen([sys.executable, "-u", "-c", script], **kwargs)
                self.child = child
                return child
            return self.real_popen(args, **kwargs)

        args = [name] + (extra if extra is not None else ["-o", str(self.root / "out.mkv"), str(self.source)])
        with patch.object(core.subprocess, "Popen", side_effect=spawn), patch.object(
            core, "_MKVMERGE_PROGRESS_INTERVAL_SECONDS", 0.15,
        ), patch.object(core, "_system_cpu_times", return_value=None):
            result = core.run_command(args, log or self.messages.append, cancel)
        return result

    def test_reports_before_exit_and_returns_complete_both_pipes(self):
        marker = self.root / "finished"
        script = (
            "import sys,time,pathlib\n"
            "sys.stdout.buffer.write(b'header\\n#GUI#pro');sys.stdout.flush();time.sleep(.25)\n"
            "sys.stdout.buffer.write(b'gress 25%\\r');sys.stdout.flush()\n"
            "sys.stderr.buffer.write(b'e'*300000);sys.stderr.flush();time.sleep(.4)\n"
            "sys.stdout.buffer.write(b'#GUI#progress 75%\\n');sys.stdout.flush();time.sleep(.3)\n"
            f"pathlib.Path({str(marker)!r}).write_text('done')\n"
            "sys.stdout.buffer.write(b'#GUI#progress 100%\\ntrailer')"
        )
        before_exit = []

        def log(message):
            self.messages.append(message)
            if "封装进度" in message:
                before_exit.append(not marker.exists())

        result = self.run_child(script, log=log)
        self.assertTrue(any(before_exit), self.messages)
        self.assertEqual(result.stdout, b"header\n#GUI#progress 25%\r#GUI#progress 75%\n#GUI#progress 100%\ntrailer")
        self.assertEqual(result.stderr, b"e" * 300000)
        self.assertEqual(self.started[0].count("--gui-mode"), 1)
        self.assertNotIn("--gui-mode", result.args)
        self.assertTrue(self.child.stdout.closed)
        self.assertTrue(self.child.stderr.closed)

    def test_gui_warning_preserves_success_and_warning_text(self):
        result = self.run_child("import sys;print('#GUI#warning subtitle reordered');sys.exit(1)")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(any("subtitle reordered" in line and "警告" in line for line in self.messages))

    def test_error_preserves_stderr_and_disk_full_classification(self):
        with self.assertRaisesRegex(RuntimeError, "目标磁盘空间不足"):
            self.run_child("import sys;sys.stderr.write('#GUI#error disk full');sys.exit(2)")

    def test_non_mux_and_identification_do_not_add_gui_mode(self):
        self.run_child("print('plain output')", name="ffmpeg.exe", extra=["-i", str(self.source)])
        self.assertNotIn("--gui-mode", self.started[-1])
        self.run_child("print('{}')", extra=["-J", str(self.source)])
        self.assertNotIn("--gui-mode", self.started[-1])
        self.assertFalse(any("封装进度" in line for line in self.messages))

    @unittest.skipUnless(os.name == "nt", "Windows process tree cancellation")
    def test_cancel_terminates_descendant_and_drains_pipes(self):
        pid_file = self.root / "descendant.pid"
        script = (
            "import subprocess,sys,time,pathlib\n"
            "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
            f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))\n"
            "print('#GUI#progress 15%',flush=True)\n"
            "time.sleep(60)\n"
        )
        cancel = threading.Event()
        timer = threading.Timer(0.75, cancel.set)
        timer.start()
        self.addCleanup(timer.cancel)
        started = time.monotonic()
        with self.assertRaises(core.CancelledError):
            self.run_child(script, cancel=cancel)
        self.assertLess(time.monotonic() - started, 8)
        self.assertIsNotNone(self.child.poll())
        self.assertTrue(self.child.stdout.closed)
        self.assertTrue(self.child.stderr.closed)
        self.assertTrue(pid_file.exists())
        import ctypes
        from ctypes import wintypes
        native = ctypes.WinDLL("kernel32", use_last_error=True)
        native.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        native.OpenProcess.restype = wintypes.HANDLE
        native.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        native.WaitForSingleObject.restype = wintypes.DWORD
        native.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = native.OpenProcess(0x00100000, False, int(pid_file.read_text()))
        if handle:
            try:
                self.assertEqual(native.WaitForSingleObject(handle, 0), 0)
            finally:
                native.CloseHandle(handle)

    @unittest.skipUnless(Path(core.MKVMERGE).is_file(), "bundled MKVToolNix unavailable")
    def test_bundled_mkvmerge_accepts_gui_mode_and_emits_parseable_records(self):
        subtitle = self.root / "source.srt"
        subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8")
        output = self.root / "subtitles.mkv"
        result = core.run_command([core.MKVMERGE, "-o", str(output), str(subtitle)], self.messages.append)
        self.assertIn(result.returncode, (0, 1))
        self.assertTrue(output.is_file())
        self.assertRegex(result.stdout, rb"#GUI#progress\s+100%")


if __name__ == "__main__":
    unittest.main()
