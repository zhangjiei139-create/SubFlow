from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

import burned_subtitle_detector_v8 as detector


class BurnedSubtitleDetectorV8Test(unittest.TestCase):
    def _image(self, root: Path, name: str, text: str = "", color=(255, 255, 255)) -> Path:
        image = np.full((360, 1280, 3), 35, dtype=np.uint8)
        if text:
            cv2.putText(
                image,
                text,
                (260, 250),
                cv2.FONT_HERSHEY_SIMPLEX,
                1.0,
                color,
                1,
                cv2.LINE_AA,
            )
        path = root / name
        cv2.imwrite(str(path), image)
        return path

    def test_visual_screen_skips_blank_frame(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            result = detector._visual_screen(self._image(Path(folder), "blank.png"))
        self.assertEqual(result.state, "clear")

    def test_visual_screen_keeps_white_and_colored_text_for_ocr(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            white = detector._visual_screen(self._image(root, "white.png", "Subtitle line"))
            yellow = detector._visual_screen(
                self._image(root, "yellow.png", "Yellow subtitle", (0, 220, 255))
            )
        self.assertEqual(white.state, "suspicious")
        self.assertEqual(yellow.state, "suspicious")

    def test_local_changed_text_is_independent_but_same_text_is_not(self) -> None:
        same = [
            {"position": 1, "second": 100.0, "text": "same subtitle"},
            {"position": 1, "second": 104.0, "text": "same subtitle"},
        ]
        changed = [same[0], {"position": 1, "second": 104.0, "text": "next subtitle"}]
        self.assertFalse(detector._independent_evidence(same))
        self.assertTrue(detector._independent_evidence(changed))

    def test_distant_changed_text_is_independent(self) -> None:
        evidence = [
            {"position": 1, "second": 100.0, "text": "first subtitle"},
            {"position": 4, "second": 500.0, "text": "second subtitle"},
        ]
        self.assertTrue(detector._independent_evidence(evidence))

    def test_distant_text_does_not_require_local_stability(self) -> None:
        evidence = [
            {"position": 1, "second": 100.0, "text": "first subtitle", "stable": False},
            {"position": 4, "second": 500.0, "text": "second subtitle", "stable": True},
        ]
        self.assertTrue(detector._independent_evidence(evidence))

    def test_local_changed_text_requires_stability(self) -> None:
        evidence = [
            {"position": 1, "second": 100.0, "text": "first subtitle", "stable": False},
            {"position": 1, "second": 103.0, "text": "second subtitle", "stable": True},
        ]
        self.assertFalse(detector._independent_evidence(evidence))

    def test_ocr_uses_actual_remaining_deadline(self) -> None:
        completed = type("Completed", (), {"returncode": 0, "stdout": b""})()
        with patch.object(detector.subprocess, "run", return_value=completed) as run:
            result = detector._ocr_lines_one_mode(
                Path("frame.png"), "eng", "6", time.monotonic() + 1.2
            )
        self.assertTrue(result.completed)
        self.assertLess(run.call_args.kwargs["timeout"], 1.21)


if __name__ == "__main__":
    unittest.main()
