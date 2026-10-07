from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import subtitle_tool_core as core


class _Process:
    returncode = 0
    pid = 123

    def communicate(self, timeout=None):
        return b"", b""

    def poll(self):
        return 0


class MkvmergeDiagnosticsTests(unittest.TestCase):
    def test_remux_logs_size_elapsed_and_throughput(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "movie.mkv"
            source.write_bytes(b"x" * (2 * 1024 * 1024))
            output = root / "out.mkv"
            logs = []
            with mock.patch("subtitle_tool_core.subprocess.Popen", return_value=_Process()):
                completed = core.run_command(
                    [str(root / "mkvmerge.exe"), "-o", str(output), str(source)],
                    log=logs.append,
                )
            self.assertEqual(completed.returncode, 0)
            self.assertTrue(any("mkvmerge 封装开始" in message for message in logs))
            self.assertTrue(any("mkvmerge 封装完成" in message for message in logs))
            self.assertTrue(any("MiB/s" in message for message in logs))


if __name__ == "__main__":
    unittest.main()
