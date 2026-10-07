from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import audio_offset_verifier as verifier
import audio_precision_gate as gate
import pro_core
import subtitle_tool_core as core


class MuseRegressionTests(unittest.TestCase):
    def test_vtt_and_long_hour_srt_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            vtt = root / "caption.vtt"
            vtt.write_text(
                "WEBVTT\n\n100:00:00.000 --> 100:00:02.000\n<i>Hello</i> there\n",
                encoding="utf-8",
            )
            events = core.parse_subtitle(vtt)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].text, "Hello there")
            output = root / "caption.srt"
            core.write_srt(output, events, {1: events[0].text})
            self.assertEqual(core.parse_subtitle(output), events)

    def test_bad_timestamp_is_not_silently_rewritten_as_zero(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "bad.srt"
            source.write_text(
                "1\n00:99:00,000 --> 00:99:02,000\nWrong\n\n"
                "2\n00:00:03,000 --> 00:00:04,000\nRight\n",
                encoding="utf-8",
            )
            events = core.parse_subtitle(source)
            self.assertEqual([event.text for event in events], ["Right"])

    def test_negative_fixed_shift_clamped_at_zero_keeps_track_offset(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            original = root / "original.srt"
            events = [
                core.SubtitleEvent(core.srt_time_from_milliseconds(start),
                                   core.srt_time_from_milliseconds(start + 1000),
                                   f"Line {index}")
                for index, start in enumerate((500, 1500, 5000, 10000, 15000), 1)
            ]
            core.write_srt(original, events, {index: event.text for index, event in enumerate(events, 1)})
            corrected = pro_core._shifted_srt(original, root / "corrected.srt", -2000)
            self.assertEqual(pro_core._subtitle_offset_milliseconds(original, corrected), -2000)

    def test_production_gate_rejects_two_anchor_model(self) -> None:
        anchors = tuple(
            verifier.OffsetAnchor(index, float(index * 1000), float(index * 1000) - 1,
                                  1.0, 0.9, 0.2)
            for index in (1, 2)
        )
        result = verifier.AudioOffsetResult(True, 1.0, anchors, (0.9, 0.9), "ok")
        self.assertFalse(gate.evaluate_production_safety(result).eligible)


if __name__ == "__main__":
    unittest.main()
