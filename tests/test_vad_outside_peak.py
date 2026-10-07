"""The range diagnostic must reject a false in-range peak without more audio."""
from dataclasses import dataclass
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import vad_evidence as evidence


@dataclass(frozen=True)
class Event:
    start: float
    end: float
    text: str = "A complete dialogue sentence."


class OutsidePeakTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.reference = Path(self.directory.name) / "speech.npz"
        rng = np.random.default_rng(20261003)
        starts = np.cumsum(rng.uniform(3.0, 9.0, 135)) + 75
        self.events = [Event(float(start), float(start + rng.uniform(.7, 2.4))) for start in starts]
        self.speech = np.zeros(100000)
        signal = evidence._subtitle_signal(evidence._cues(self.events))
        self.speech[:len(signal)] = signal
        np.savez(self.reference, speech=self.speech)

    def moved(self, delta):
        return [Event(event.start + delta, event.end + delta, event.text) for event in self.events]

    def test_correct_in_range_offsets_are_not_rejected(self):
        for delta in (0, -.5, .5, -7.3, 7.3, -9.8, 9.8):
            with self.subTest(delta=delta):
                result = evidence.evaluate_reference(self.reference, self.moved(delta), raw_offset=-delta)
                self.assertTrue(result["sufficient"], result)
                self.assertFalse(result["outside_range_peak"])
                self.assertAlmostEqual(result["diagnostic_offset_seconds"], -delta, places=2)

    def test_outside_offsets_cannot_become_acceptable_local_peaks(self):
        for delta in (-12, 12, -24, 24, -35, 35, -55, 55):
            with self.subTest(delta=delta):
                events = self.moved(delta)
                metrics = evidence._correlate(self.speech, evidence._subtitle_signal(evidence._cues(events)))
                self.assertAlmostEqual(metrics["diagnostic_offset_seconds"], -delta, places=2)
                result = evidence.evaluate_reference(self.reference, events, raw_offset=metrics["offset_seconds"])
                self.assertFalse(result["sufficient"], result)
                self.assertTrue(result["range_limited"], result)
                self.assertFalse(result["expandable"], result)
                if abs(metrics["offset_seconds"]) < 9.9:
                    self.assertTrue(result["outside_range_peak"], result)
                    self.assertIn("outside_range_peak", result["reasons"])

    def test_wide_diagnostic_uses_one_fft_only(self):
        with patch.object(np.fft, "irfft", wraps=np.fft.irfft) as inverse:
            metrics = evidence._correlate(self.speech,
                evidence._subtitle_signal(evidence._cues(self.moved(-24))))
        self.assertEqual(inverse.call_count, 1)
        self.assertTrue(metrics["outside_range_peak"])
        self.assertEqual(metrics["diagnostic_offset_seconds"], 24)

    def test_short_references_keep_existing_matching_and_guard(self):
        self.assertEqual(evidence.subtitle_guard_seconds(199.99), 10)
        self.assertEqual(evidence.subtitle_guard_seconds(200), 70)
        metrics = evidence._correlate(np.tile([0., 0., 1., 1.], 1000),
                                      np.tile([0., 0., 1., 1.], 1000))
        self.assertFalse(metrics["diagnostic_evaluated"])
        self.assertFalse(metrics["outside_range_peak"])

    def test_repeated_speech_does_not_override_supported_local_peak(self):
        # A repeated voice burst elsewhere must not by itself invalidate the
        # supported local maximum.
        short = np.zeros(40000)
        short[7000:7100] = 1
        short[9500:9600] = 1
        events = [Event(70, 71)]
        metrics = evidence._correlate(short, evidence._subtitle_signal(evidence._cues(events)))
        self.assertEqual(metrics["offset_seconds"], 0)
        self.assertFalse(metrics["outside_range_peak"])


if __name__ == "__main__":
    unittest.main()
