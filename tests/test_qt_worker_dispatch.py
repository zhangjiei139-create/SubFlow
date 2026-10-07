# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class QtWorkerDispatchTest(unittest.TestCase):
    def test_ocr_worker_does_not_start_qt_gui(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "missing.sub"
            output_path = root / "result.srt"
            environment = os.environ.copy()
            environment["QT_QPA_PLATFORM"] = "offscreen"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "qt_app.py"),
                    "--pgs-ocr-worker",
                    "--input",
                    str(input_path),
                    "--output",
                    str(output_path),
                    "--language",
                    "eng",
                    "--tesseract",
                    str(root / "tesseract.exe"),
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                timeout=20,
                check=False,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertTrue(Path(str(output_path) + ".error.log").exists())

    def test_qml_entry_dispatches_ocr_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "missing.sub"
            output_path = root / "result.srt"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "qml_frontend" / "main.py"),
                    "--pgs-ocr-worker",
                    "--input",
                    str(input_path),
                    "--output",
                    str(output_path),
                    "--language",
                    "eng",
                    "--tesseract",
                    str(root / "tesseract.exe"),
                ],
                cwd=ROOT,
                capture_output=True,
                timeout=20,
                check=False,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertTrue(Path(str(output_path) + ".error.log").exists())


if __name__ == "__main__":
    unittest.main()
