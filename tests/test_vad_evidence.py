import copy
from dataclasses import dataclass
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

import vad_evidence as evidence


@dataclass
class Event:
    start: float
    end: float
    text: str = "Spoken dialogue."


class VadEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.reference = Path(self.temp.name) / "reference.npz"
        rng = np.random.default_rng(20261002)
        self.events = [Event(float(start), float(start + rng.uniform(.8, 2.5)))
                       for start in np.arange(2, 398, 4) + rng.uniform(0, .7, 99)]
        speech = np.zeros(40000)
        signal = evidence._subtitle_signal(evidence._cues(self.events))
        speech[:len(signal)] = signal
        np.savez(self.reference, speech=speech)

    def test_precheck_does_not_run_fft_and_preserves_events(self):
        original = copy.deepcopy(self.events)
        with patch.object(evidence, "_correlate", side_effect=AssertionError("FFT ran")):
            result = evidence.evaluate_reference(self.reference, self.events)
        self.assertTrue(result["sufficient"])
        self.assertFalse(result["fft_evaluated"])
        self.assertEqual(result["covered_regions"], 5)
        self.assertIsNone(result["offset_seconds"])
        self.assertEqual(self.events, original)

    def test_known_shift_sign_and_engine_agreement(self):
        shifted = [Event(cue.start + 3.25, cue.end + 3.25, cue.text) for cue in self.events]
        result = evidence.evaluate_reference(self.reference, shifted, -3.25)
        self.assertTrue(result["sufficient"], result)
        self.assertTrue(result["fft_evaluated"])
        self.assertEqual(result["computed_offset_seconds"], -3.25)
        self.assertFalse(result["expandable"])

    def test_negative_raw_score_can_have_a_clear_unique_peak(self):
        # Subtitle display often lasts longer/more frequently than detected
        # speech. A dense cue track with only some audible cues has a negative
        # uncentered +/-1 score even at its correct, distinctive offset.
        rng = np.random.default_rng(20261003)
        starts = np.cumsum(rng.uniform(3.6, 5.0, 99))
        events = [Event(float(start), float(start + rng.uniform(3.0, 3.5)))
                  for start in starts]
        speech = np.zeros(round((events[-1].end + 3) * 100))
        for index in rng.choice(len(events), 30, replace=False):
            cue = events[index]
            speech[round(cue.start * 100):round(cue.end * 100)] = 1
        signal = evidence._subtitle_signal(evidence._cues(events))
        raw_score = np.dot(2 * speech[:len(signal)] - 1, 2 * signal - 1)
        self.assertLess(raw_score, 0)
        np.savez(self.reference, speech=speech)
        result = evidence.evaluate_reference(self.reference, events, 0)
        self.assertTrue(result["sufficient"], result)
        self.assertEqual(result["computed_offset_seconds"], 0)
        self.assertLess(result["alternative_peak_ratio"], evidence.ALTERNATIVE_PEAK_RATIO)
        self.assertLess(result["peak_width_seconds"], evidence.MAX_PEAK_WIDTH_SECONDS)

    def test_coordinate_disagreement_never_expands(self):
        result = evidence.evaluate_reference(self.reference, self.events, 1.0)
        self.assertFalse(result["sufficient"])
        self.assertTrue(result["tool_coordinate_invalid"])
        self.assertFalse(result["expandable"])
        self.assertIn("tool_coordinate_invalid", result["reasons"])

    def test_five_hundredths_tolerance_is_inclusive(self):
        self.assertTrue(evidence.evaluate_reference(self.reference, self.events, .05)["sufficient"])
        result = evidence.evaluate_reference(self.reference, self.events, .06)
        self.assertTrue(result["tool_coordinate_invalid"])

    def test_range_boundary_never_expands_or_runs_fft(self):
        for offset in (-10, -9.99, -9.9, 9.9, 10, 13.52):
            with self.subTest(offset=offset), patch.object(evidence, "_correlate", side_effect=AssertionError):
                result = evidence.evaluate_reference(self.reference, self.events, offset)
            self.assertTrue(result["range_limited"])
            self.assertFalse(result["expandable"])
            self.assertFalse(result["fft_evaluated"])

    def test_sparse_or_concentrated_cues_request_more_evidence(self):
        sparse = evidence.evaluate_reference(self.reference, self.events[:20])
        self.assertIn("too_few_dialogue_cues", sparse["reasons"])
        self.assertIn("dialogue_concentrated", sparse["reasons"])
        self.assertTrue(sparse["expandable"])
        concentrated = [Event(1 + i, 1.5 + i) for i in range(50)]
        result = evidence.evaluate_reference(self.reference, concentrated)
        self.assertNotIn("too_few_dialogue_cues", result["reasons"])
        self.assertIn("dialogue_concentrated", result["reasons"])

    def test_non_dialogue_and_music_cues_do_not_supply_dialogue_evidence(self):
        for text in ("[Music]", "(Shouting)", "♪ Singing lyrics ♪", "<i>♪♫</i>", ""):
            with self.subTest(text=text):
                events = [Event(cue.start, cue.end, text) for cue in self.events]
                result = evidence.evaluate_reference(self.reference, events)
                self.assertEqual(result["cue_count"], 0)
        ordinary = [Event(cue.start, cue.end, "[Alice] Hello!") for cue in self.events]
        self.assertEqual(evidence.evaluate_reference(self.reference, ordinary)["cue_count"], len(ordinary))

    def test_too_little_speech_or_quiet_requests_expansion(self):
        for frames, reason in ((np.zeros(40000), "too_little_speech"),
                               (np.ones(40000), "too_little_quiet")):
            np.savez(self.reference, speech=frames)
            result = evidence.evaluate_reference(self.reference, self.events)
            self.assertIn(reason, result["reasons"])
            self.assertTrue(result["expandable"])

    def test_deserializer_threshold_matches_engine(self):
        np.savez(self.reference, speech=np.tile([.1, .99, 1., 1.], 10000))
        result = evidence.evaluate_reference(self.reference, self.events)
        self.assertEqual(result["spoken_seconds"], 200)
        self.assertEqual(result["quiet_seconds"], 200)

    def test_duration_cap_rounding_and_metadata_affect_signal_not_original_cues(self):
        events = [Event(.005, .030), Event(1., 20.), Event(30., 40., "[Sound]")]
        original = copy.deepcopy(events)
        cues = evidence._cues(events)
        signal = evidence._subtitle_signal(cues)
        self.assertEqual(len(signal), 4002)  # metadata still establishes signal length
        self.assertEqual(signal[:4].tolist(), [1., 1., 0., 0.])
        self.assertEqual(signal[100:1100].sum(), 1000)
        self.assertEqual(signal[1100:].sum(), 0)
        self.assertEqual(events, original)

    def test_invalid_data_and_remote_timestamps_fail_without_expansion(self):
        for frames in (np.array([np.nan]), np.ones((2, 2)), np.array([])):
            np.savez(self.reference, speech=frames)
            result = evidence.evaluate_reference(self.reference, self.events)
            self.assertEqual(result["reasons"], ["reference_invalid"])
            self.assertFalse(result["expandable"])
        np.savez(self.reference, speech=np.zeros(40000))
        result = evidence.evaluate_reference(self.reference, [Event(3000, 3002)])
        self.assertEqual(result["reasons"], ["cue_coordinate_invalid"])
        self.assertFalse(result["expandable"])

    def test_ambiguous_and_broad_peaks_are_evidence_failures(self):
        offsets = np.arange(2000) / 100 - 10
        values = np.zeros(2000)
        values[1000] = 100
        values[1200] = 96
        metrics = evidence._peak_metrics(values, offsets)
        self.assertEqual(metrics["alternative_peak_ratio"], .96)
        self.assertEqual(metrics["peak_width_seconds"], 0)
        values = np.zeros(2000)
        values[950:1051] = 100
        metrics = evidence._peak_metrics(values, offsets)
        self.assertEqual(metrics["peak_width_seconds"], 1)
        for key, value, reason in (("alternative_peak_ratio", .96, "alternative_peak"),
                                   ("peak_width_seconds", 1., "broad_peak")):
            metrics = {"offset_seconds": 0., "alternative_peak_ratio": .2,
                       "peak_width_seconds": .2, "peak_contrast": 100}
            metrics[key] = value
            with patch.object(evidence, "_correlate", return_value=metrics):
                result = evidence.evaluate_reference(self.reference, self.events, 0)
            self.assertIn(reason, result["reasons"])
            self.assertTrue(result["expandable"])

    def test_nonfinite_tool_offset_never_expands(self):
        for offset in (float("nan"), float("inf"), True):
            result = evidence.evaluate_reference(self.reference, self.events, offset)
            self.assertTrue(result["tool_coordinate_invalid"])
            self.assertFalse(result["expandable"])


if __name__ == "__main__":
    unittest.main()
