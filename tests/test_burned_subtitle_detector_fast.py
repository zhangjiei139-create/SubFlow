# -*- coding: utf-8 -*-
from __future__ import annotations

import concurrent.futures
import subprocess
import threading
import time
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import burned_subtitle_detector as detector


class BurnedSubtitleFastPathTest(unittest.TestCase):
    def _run_detect(
        self,
        video: Path,
        work: Path,
        *,
        intervals=None,
        keyframes=None,
        frames=None,
        screens=None,
        ocr_lines=None,
    ):
        intervals = intervals if intervals is not None else [
            {"start": 100.5, "end": 101.0, "peak": 100.75, "peak_score": -20.0, "duration": 0.5}
        ]
        keyframes = keyframes if keyframes is not None else [99.0, 100.0]
        frames = frames if frames is not None else [work / "frame-01.png", work / "frame-02.png"]
        screens = screens if screens is not None else [detector.VisualScreen("clear")] * len(frames) * len(detector.AUDIO_RATIOS)
        with patch.object(detector, "_read_cache", return_value={}), patch.object(
            detector, "_write_cache"
        ) as write_cache, patch.object(
            detector, "_duration_seconds", return_value=3600.0
        ), patch.object(
            detector, "_available_ocr_languages", return_value="eng"
        ), patch.object(
            detector, "_runtime_dir", return_value=work
        ), patch.object(
            detector, "_speech_intervals", return_value=(intervals, 90.0, True)
        ) as speech, patch.object(
            detector, "_keyframes_near_window", return_value=(keyframes, True)
        ), patch.object(
            detector, "_extract_speech_frames", return_value=frames
        ), patch.object(
            detector, "_visual_screen", side_effect=screens
        ), patch.object(
            detector, "_ocr_lines", return_value=ocr_lines or []
        ) as ocr:
            result = detector.detect(str(video), {}, ["eng"])
        return result, write_cache, speech, ocr

    def test_configuration_uses_only_thirty_percent(self) -> None:
        self.assertEqual(detector.AUDIO_RATIOS, (0.30, 0.65))
        self.assertFalse(hasattr(detector, "SAMPLE_RATIOS"))

    def test_selects_earliest_interval_and_nearest_preceding_keyframe(self) -> None:
        intervals = [
            {"start": 110.0, "end": 111.0},
            {"start": 105.0, "end": 106.0},
        ]
        selected = detector._select_speech_interval(intervals, [100.0, 104.5, 109.0])
        self.assertIsNotNone(selected)
        self.assertEqual(selected[0]["start"], 105.0)
        self.assertEqual(selected[1], 104.5)

    def test_prefers_keyframe_inside_speech_before_preceding_keyframe(self) -> None:
        intervals = [
            {"start": 105.0, "end": 112.0},
        ]
        selected = detector._select_speech_interval(intervals, [100.0, 110.0])
        self.assertIsNotNone(selected)
        self.assertEqual(selected[1], 110.0)

    def test_reuses_vad_result_for_up_to_three_speech_attempts(self) -> None:
        intervals = [
            {"start": 101.0, "end": 102.0},
            {"start": 104.0, "end": 105.0},
            {"start": 107.0, "end": 108.0},
            {"start": 110.0, "end": 111.0},
        ]
        attempts = detector._speech_decode_attempts(intervals, [100.0, 104.5, 109.0])
        self.assertEqual(len(attempts), 3)
        self.assertEqual([item[0]["start"] for item in attempts], [101.0, 104.0, 107.0])
        self.assertEqual([item[1] for item in attempts], [100.0, 104.5, 104.5])

    def test_continuous_decode_uses_one_ffmpeg_process(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / "frames"

            def fake_run(args, **_kwargs):
                output.mkdir(parents=True, exist_ok=True)
                for index in range(1, 5):
                    (output / f"frame-{index:02d}.png").write_bytes(b"png")
                return subprocess.CompletedProcess(args, 0)

            interval = {
                "start": 100.5,
                "end": 101.0,
                "duration": 0.5,
            }
            with patch.object(detector.subprocess, "run", side_effect=fake_run) as run:
                frames = detector._extract_speech_frames(
                    root / "movie.mkv",
                    interval,
                    100.0,
                    output,
                    time.monotonic() + 5.0,
                )

        self.assertEqual(len(frames), 4)
        self.assertEqual(run.call_count, 1)
        command = run.call_args.args[0]
        self.assertIn("100.000000", command)
        self.assertIn("-noaccurate_seek", command)
        self.assertIn("0.500000", command)
        self.assertIn("2.000000", command)
        self.assertIn("fps=2,crop=iw:ih*0.42:0:ih*0.58,scale=1280:-2:force_original_aspect_ratio=decrease", command)

    def test_clear_frames_are_absent_without_ocr(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "movie.mkv"
            video.write_bytes(b"video")
            work = root / "work"
            work.mkdir()
            result, write_cache, speech, ocr = self._run_detect(video, work)

        self.assertEqual(result.status, "absent")
        self.assertEqual(result.sample_count, 4)
        self.assertEqual(speech.call_count, 2)
        ocr.assert_not_called()
        write_cache.assert_called_once()

    def test_first_meaningful_ocr_text_detects_burned_subtitle(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "movie.mkv"
            video.write_bytes(b"video")
            work = root / "work"
            work.mkdir()
            result, write_cache, _speech, ocr = self._run_detect(
                video,
                work,
                screens=[detector.VisualScreen("suspicious", 0.8), detector.VisualScreen("clear")],
                ocr_lines=["Let us in, Dad."],
            )

        self.assertEqual(result.status, "detected")
        self.assertTrue(result.detected)
        self.assertEqual(result.evidence_count, 1)
        self.assertEqual(ocr.call_count, 1)
        write_cache.assert_called_once()

    def test_two_completed_windows_without_speech_are_absent(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "movie.mkv"
            video.write_bytes(b"video")
            work = root / "work"
            work.mkdir()
            result, write_cache, _speech, ocr = self._run_detect(
                video,
                work,
                intervals=[],
                frames=[],
                screens=[],
            )

        self.assertEqual(result.status, "absent")
        self.assertEqual(result.sample_count, 0)
        ocr.assert_not_called()
        write_cache.assert_called_once()

    def test_ocr_timeout_is_uncertain_and_not_cached(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "movie.mkv"
            video.write_bytes(b"video")
            work = root / "work"
            work.mkdir()
            with patch.object(detector, "_read_cache", return_value={}), patch.object(
                detector, "_write_cache"
            ) as write_cache, patch.object(
                detector, "_duration_seconds", return_value=3600.0
            ), patch.object(
                detector, "_available_ocr_languages", return_value="eng"
            ), patch.object(
                detector, "_runtime_dir", return_value=work
            ), patch.object(
                detector,
                "_speech_intervals",
                return_value=([
                    {"start": 100.5, "end": 101.0, "peak": 100.75, "peak_score": -20.0, "duration": 0.5}
                ], 90.0, True),
            ), patch.object(
                detector, "_keyframes_near_window", return_value=([100.0], True)
            ), patch.object(
                detector, "_extract_speech_frames", return_value=[work / "frame.png"]
            ), patch.object(
                detector, "_visual_screen", return_value=detector.VisualScreen("suspicious", 0.8)
            ), patch.object(
                detector, "_ocr_lines", side_effect=subprocess.TimeoutExpired("tesseract", 1)
            ):
                result = detector.detect(str(video), {}, ["eng"])

        self.assertEqual(result.status, "uncertain")
        write_cache.assert_not_called()

    def test_ocr_checks_sparse_text_page_mode(self) -> None:
        completed = subprocess.CompletedProcess(
            ["tesseract"],
            0,
            b"level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n",
            b"",
        )
        with patch.object(detector.subprocess, "run", return_value=completed) as run:
            detector._ocr_lines(Path("frame.png"), "eng")
        modes = [call.args[0][call.args[0].index("--psm") + 1] for call in run.call_args_list]
        self.assertEqual(modes, ["6", "11"])

    def test_cache_identity_survives_file_rename(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            first = Path(folder) / "first.mkv"
            first.write_bytes(b"same video bytes")
            first_key = detector._cache_key(first)
            second = first.with_name("renamed.mkv")
            first.rename(second)
            self.assertEqual(first_key, detector._cache_key(second))

    def test_concurrent_scans_are_limited_to_two(self) -> None:
        active = 0
        maximum_active = 0
        lock = threading.Lock()

        def fake_detect(*_args, **_kwargs):
            nonlocal active, maximum_active
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return detector.DetectionResult(False, 0, 2, "clean", "absent", 0.82)

        with patch.object(detector, "_detect_impl", side_effect=fake_detect):
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
                results = list(executor.map(lambda name: detector.detect(name), ("a.mkv", "b.mkv", "c.mkv")))

        self.assertEqual(maximum_active, 2)
        self.assertEqual([result.status for result in results], ["absent", "absent", "absent"])


if __name__ == "__main__":
    unittest.main()
