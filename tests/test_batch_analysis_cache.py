from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import batch_core
import burned_subtitle_detector
import subtitle_tool_core as core
from profile_model import PreferenceProfile


MEDIA = {
    "tracks": [
        {
            "id": 0,
            "type": "video",
            "codec": "HEVC/H.265",
            "properties": {},
        },
        {
            "id": 1,
            "type": "audio",
            "codec": "AC-3",
            "properties": {
                "language_ietf": "en",
                "default_track": True,
            },
        },
    ],
    "container": {"properties": {"duration": 7_200_000_000_000}},
}


class BatchAnalysisCacheTests(unittest.TestCase):
    def test_media_probe_cache_uses_size_and_mtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "sample.mkv"
            video.write_bytes(b"sample")
            with mock.patch.dict(os.environ, {"SUBFLOW_RUNTIME_DIR": str(root / "runtime")}):
                with mock.patch.object(core, "inspect_media", return_value=MEDIA) as inspect:
                    first, first_hit = batch_core._inspect_media_cached(str(video), lambda _message: None)
                    second, second_hit = batch_core._inspect_media_cached(str(video), lambda _message: None)
                    self.assertEqual(first, MEDIA)
                    self.assertEqual(second, MEDIA)
                    self.assertFalse(first_hit)
                    self.assertTrue(second_hit)
                    self.assertEqual(inspect.call_count, 1)

                    video.write_bytes(b"sample changed")
                    os.utime(video, None)
                    _third, third_hit = batch_core._inspect_media_cached(str(video), lambda _message: None)
                    self.assertFalse(third_hit)
                    self.assertEqual(inspect.call_count, 2)

    def test_analyze_reuses_media_for_burned_scan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "movie.mkv"
            video.write_bytes(b"x" * (1024 * 1024 + 1))
            logs = []
            profile = PreferenceProfile(
                1,
                "test",
                subtitle_languages=[],
                audio_policy="native",
            )
            result = burned_subtitle_detector.DetectionResult(
                False, 0, 6, "未发现烧录字幕", "absent", 1.0,
            )
            with mock.patch.dict(os.environ, {"SUBFLOW_RUNTIME_DIR": str(root / "runtime")}):
                with mock.patch.object(core, "inspect_media", return_value=MEDIA) as inspect:
                    with mock.patch.object(
                        burned_subtitle_detector,
                        "detect",
                        return_value=result,
                    ) as detect:
                        plan = batch_core.analyze_video(str(video), profile, log=logs.append)
            self.assertEqual(inspect.call_count, 1)
            self.assertIs(detect.call_args.args[1], MEDIA)
            self.assertEqual(plan.status, "ready")
            self.assertTrue(any("容器轨道分析耗时" in message for message in logs))
            self.assertTrue(any("分析完成，耗时" in message for message in logs))


if __name__ == "__main__":
    unittest.main()
