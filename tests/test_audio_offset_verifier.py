import unittest

import audio_offset_verifier as verifier


class AudioOffsetVerifierTests(unittest.TestCase):
    def test_accepts_two_consistent_audio_anchors(self):
        candidate = [
            verifier.TimedText(100.0, 103.0, "we are going to find the dragon", -1),
            verifier.TimedText(500.0, 503.0, "this is where we belong together", -1),
        ]
        observed = [
            verifier.TimedText(123.9, 126.9, "we are going to find the dragon", 0),
            verifier.TimedText(523.9, 526.9, "this is where we belong together", 1),
        ]
        result = verifier.locate_offset(candidate, observed)
        self.assertTrue(result.accepted)
        self.assertAlmostEqual(result.offset_seconds, 23.9, places=1)

    def test_rejects_inconsistent_offsets(self):
        candidate = [
            verifier.TimedText(100.0, 103.0, "we are going to find the dragon", -1),
            verifier.TimedText(500.0, 503.0, "this is where we belong together", -1),
        ]
        observed = [
            verifier.TimedText(123.9, 126.9, "we are going to find the dragon", 0),
            verifier.TimedText(550.0, 553.0, "this is where we belong together", 1),
        ]
        self.assertFalse(verifier.locate_offset(candidate, observed).accepted)

    def test_rejects_wrong_movie_dialogue(self):
        candidate = [
            verifier.TimedText(100.0, 103.0, "the dragon is ready to fly tonight", -1),
            verifier.TimedText(500.0, 503.0, "we will protect the village together", -1),
        ]
        observed = [
            verifier.TimedText(123.9, 126.9, "open the courtroom and call the witness", 0),
            verifier.TimedText(523.9, 526.9, "the evidence has been submitted", 1),
        ]
        self.assertFalse(verifier.locate_offset(candidate, observed).accepted)

    def test_rejects_single_strong_anchor_when_offsets_conflict(self):
        candidate = [
            verifier.TimedText(100.0, 103.0, "we are going to find the dragon", -1),
            verifier.TimedText(500.0, 503.0, "this is where we belong together", -1),
        ]
        observed = [
            verifier.TimedText(102.9, 105.9, "we are going to find a dragon", 0),
            verifier.TimedText(504.3, 507.3, "this is where we belong together", 1),
        ]
        result = verifier.locate_offset(candidate, observed)
        self.assertFalse(result.accepted)

    def test_cross_language_temporal_corroboration_accepts_distant_weak_wording(self):
        observed = (
            verifier.TimedText(100.0, 102.0, "I thank you for the dignity of the ponzu.", 1),
            verifier.TimedText(300.0, 302.0, "Albert J.R.S.K.O.V. Ingerling.", 2),
            verifier.TimedText(
                600.0,
                602.0,
                "Every grain of grain ensures safety in our country and happiness.",
                5,
            ),
            verifier.TimedText(
                900.0,
                902.0,
                "The original was another autopede. It did not write anything else.",
                7,
            ),
        )
        candidate = [
            verifier.TimedText(101.0, 103.0, "all thanks to the grace of the bigwigs.", -1),
            verifier.TimedText(301.0, 303.0, 'Albert Jerska, Operation "Engerling."', -1),
            verifier.TimedText(
                601.0,
                603.0,
                "those gray men who ensure safety in our land and happiness.",
                -1,
            ),
            verifier.TimedText(901.0, 903.0, "He has never used anything else.", -1),
        ]
        fingerprint = verifier.MovieAudioFingerprint(
            (1, 2, 5, 7),
            observed,
            ((1, 1.0), (2, 1.0), (5, 1.0), (7, 1.0)),
        )

        result = verifier._locate_with_temporal_corroboration(
            candidate,
            fingerprint,
            duration_seconds=1000.0,
        )

        self.assertIsNotNone(result)
        self.assertTrue(result.accepted)
        self.assertAlmostEqual(result.offset_seconds, -1.0, places=1)

    def test_cross_language_temporal_corroboration_rejects_unrelated_dialogue(self):
        observed = (
            verifier.TimedText(100.0, 102.0, "the gray men protect our country", 1),
            verifier.TimedText(400.0, 402.0, "Albert writes with a red typewriter", 4),
            verifier.TimedText(800.0, 802.0, "search the books for hidden notes", 8),
        )
        candidate = [
            verifier.TimedText(101.0, 103.0, "the dragon flies above the village", -1),
            verifier.TimedText(401.0, 403.0, "open the courtroom for the witness", -1),
            verifier.TimedText(801.0, 803.0, "we will sail across the ocean", -1),
        ]
        fingerprint = verifier.MovieAudioFingerprint(
            (1, 4, 8),
            observed,
            ((1, 1.0), (4, 1.0), (8, 1.0)),
        )

        result = verifier._locate_with_temporal_corroboration(
            candidate,
            fingerprint,
            duration_seconds=1000.0,
        )

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
