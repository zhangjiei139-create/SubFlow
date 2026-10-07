# -*- coding: utf-8 -*-
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import strict_verification
import subtitle_tool_core as core


def media(duration: int = 7_200_000_000_000) -> dict:
    return {
        "container": {"properties": {"duration": duration}},
        "tracks": [
            {"type": "video", "codec": "HEVC", "properties": {}},
            {"type": "audio", "codec": "TrueHD", "properties": {}},
        ],
    }


class StrictVerificationTest(unittest.TestCase):
    def test_verified_output_requires_accepted_inspection_and_matching_source(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.mkv"
            output = root / "output.mkv"
            source.write_bytes(b"s" * (1024 * 1024 + 1))
            output.write_bytes(b"o" * (1024 * 1024 + 1))
            with patch.object(
                strict_verification.strict_inspection,
                "inspect_video",
                return_value={"status": "accepted", "reason_codes": []},
            ), patch.object(core, "inspect_media", side_effect=[media(), media()]):
                report = strict_verification.verify_output(str(output), str(source))
        self.assertTrue(report["validation"]["passed"])

    def test_rejects_duration_and_audio_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.mkv"
            output = root / "output.mkv"
            source.write_bytes(b"s" * (1024 * 1024 + 1))
            output.write_bytes(b"o" * (1024 * 1024 + 1))
            output_media = media(6_000_000_000_000)
            output_media["tracks"] = output_media["tracks"][:1]
            with patch.object(
                strict_verification.strict_inspection,
                "inspect_video",
                return_value={"status": "accepted", "reason_codes": []},
            ), patch.object(core, "inspect_media", side_effect=[media(), output_media]):
                report = strict_verification.verify_output(str(output), str(source))
        self.assertFalse(report["validation"]["passed"])
        self.assertIn("duration_mismatch", report["reason_codes"])
        self.assertIn("audio_track_count_mismatch", report["reason_codes"])

    def test_accepts_nas_ready_name_for_verification(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / "movie.mkv.ready"
            output.write_bytes(b"o" * (1024 * 1024 + 1))
            with patch.object(
                strict_verification.strict_inspection,
                "inspect_video",
                return_value={"status": "accepted", "reason_codes": []},
            ):
                report = strict_verification.verify_output(str(output))
        self.assertTrue(report["validation"]["passed"])


if __name__ == "__main__":
    unittest.main()
