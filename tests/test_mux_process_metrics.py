import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import subtitle_tool_core as core


class NativeFunction:
    """A callable Win32 stand-in that also accepts ctypes signatures."""

    def __init__(self, callback):
        self.callback = callback
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.callback(*args)


def fake_kernel(*, times_ok=True, io_ok=True, fail=False):
    def get_times(handle, _creation, _exit, kernel, user):
        if fail:
            raise OSError("counter unavailable")
        if times_ok:
            # Include the high DWORD so truncation to 32 bits is observable.
            ticks = (1 << 32) + 20_000_000
            kernel._obj.low, kernel._obj.high = ticks & 0xFFFFFFFF, ticks >> 32
            user._obj.low, user._obj.high = 30_000_000, 0
        return times_ok

    def get_io(handle, counters):
        if io_ok:
            counters._obj.read_bytes = (1 << 33) + 123
            counters._obj.write_bytes = (1 << 34) + 456
        return io_ok

    return SimpleNamespace(
        GetProcessTimes=NativeFunction(get_times),
        GetProcessIoCounters=NativeFunction(get_io),
    )


class MuxNativeCountersTests(unittest.TestCase):
    def test_reads_64_bit_cumulative_counters_without_owning_handle(self):
        native = fake_kernel()
        process = SimpleNamespace(_handle=123)
        result = core._mkvmerge_process_metrics(process, _kernel32=native)
        self.assertAlmostEqual(result["cpu_seconds"], ((1 << 32) + 50_000_000) / 10_000_000)
        self.assertEqual(result["read_bytes"], (1 << 33) + 123)
        self.assertEqual(result["write_bytes"], (1 << 34) + 456)
        self.assertEqual(len(native.GetProcessTimes.calls), 1)
        self.assertEqual(len(native.GetProcessIoCounters.calls), 1)
        self.assertEqual(native.GetProcessTimes.calls[0][0].value, 123)
        self.assertEqual(process._handle, 123)

    def test_partial_counter_failure_keeps_missing_values_unknown(self):
        result = core._mkvmerge_process_metrics(SimpleNamespace(_handle=123), _kernel32=fake_kernel(io_ok=False))
        self.assertIsNotNone(result["cpu_seconds"])
        self.assertIsNone(result["read_bytes"])
        self.assertIsNone(result["write_bytes"])

    def test_both_counter_calls_unavailable_returns_none(self):
        self.assertIsNone(core._mkvmerge_process_metrics(
            SimpleNamespace(_handle=123), _kernel32=fake_kernel(times_ok=False, io_ok=False),
        ))

    def test_native_exception_is_optional_diagnostic_failure(self):
        self.assertIsNone(core._mkvmerge_process_metrics(SimpleNamespace(_handle=123), _kernel32=fake_kernel(fail=True)))

    def test_invalid_handles_and_other_platforms_do_not_query_native_api(self):
        native = fake_kernel()
        for handle in (None, True, 0, -1, object()):
            with self.subTest(handle=handle):
                self.assertIsNone(core._mkvmerge_process_metrics(SimpleNamespace(_handle=handle), _kernel32=native))
        self.assertEqual(native.GetProcessTimes.calls, [])
        self.assertEqual(native.GetProcessIoCounters.calls, [])
        with patch.object(core.os, "name", "posix"):
            self.assertIsNone(core._mkvmerge_process_metrics(SimpleNamespace(_handle=123)))


class MuxCountersIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "source.mkv"
        self.source.write_bytes(b"x" * 4096)
        self.process = Mock(returncode=0)
        self.process.communicate.return_value = (b"", b"")
        self.logs = []

    def test_query_occurs_after_exit_without_extra_clock_or_wait(self):
        def metrics(process):
            self.assertIs(process, self.process)
            process.communicate.assert_called_once_with(timeout=0.2)
            process.wait.assert_not_called()
            return {"cpu_seconds": 5.0, "read_bytes": 123, "write_bytes": 456}

        with patch.object(core.subprocess, "Popen", return_value=self.process), patch.object(
            core, "_mkvmerge_process_metrics", side_effect=metrics,
        ) as query, patch.object(core.time, "monotonic", side_effect=[0.0, 2.0]) as clock:
            result = core.run_command(["mkvmerge.exe", "-o", str(self.root / "out.mkv"), str(self.source)], self.logs.append)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(clock.call_count, 2)
        query.assert_called_once_with(self.process)
        message = self.logs[-1]
        for fragment in ("CPU累计 5.00 秒", "读取 123 字节", "写入 456 字节", "不是实测磁盘吞吐"):
            self.assertIn(fragment, message)

    def test_unavailable_counters_do_not_change_success_or_invent_zeroes(self):
        with patch.object(core.subprocess, "Popen", return_value=self.process), patch.object(
            core, "_mkvmerge_process_metrics", return_value=None,
        ), patch.object(core.time, "monotonic", side_effect=[0.0, 2.0]):
            result = core.run_command(["mkvmerge.exe", "-o", str(self.root / "out.mkv"), str(self.source)], self.logs.append)
        self.assertEqual(result.returncode, 0)
        self.assertFalse(any("CPU累计" in line for line in self.logs))
        self.assertTrue(any("估算的处理速率" in line for line in self.logs))

    def test_non_mux_tools_do_not_query_counters(self):
        with patch.object(core.subprocess, "Popen", return_value=self.process), patch.object(
            core, "_mkvmerge_process_metrics",
        ) as query, patch.object(core.time, "monotonic", return_value=0.0) as clock:
            result = core.run_command(["ffmpeg.exe", "-i", str(self.source)], self.logs.append)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(clock.call_count, 1)
        query.assert_not_called()


if __name__ == "__main__":
    unittest.main()
