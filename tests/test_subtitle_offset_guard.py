# -*- coding: utf-8 -*-
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import audio_offset_verifier
import pro_core
import subtitle_offset_guard


class SubtitleOffsetGuardTest(unittest.TestCase):
    def _case(self, residuals: list[float], proposed: float):
        with tempfile.TemporaryDirectory() as folder:
            subtitle = Path(folder) / "candidate.srt"
            blocks = []
            words = []
            for index, residual in enumerate(residuals):
                start = 100.0 + index * 100.0
                end = start + 2.0
                blocks.append(
                    f"{index + 1}\n"
                    f"00:{int(start // 60):02d}:{start % 60:06.3f} --> "
                    f"00:{int(end // 60):02d}:{end % 60:06.3f}\n"
                    "alpha beta gamma delta\n"
                )
                for word_index, text in enumerate(("alpha", "beta", "gamma", "delta")):
                    word_start = start + residual + word_index * 0.5
                    words.append(audio_offset_verifier.TimedWord(
                        word_start,
                        word_start + 0.5,
                        text,
                        index,
                        0.95,
                    ))
            subtitle.write_text("\n".join(blocks).replace(".", ","), encoding="utf-8")
            fingerprint = audio_offset_verifier.MovieAudioFingerprint(
                tuple(range(len(residuals))),
                (),
                (),
                tuple(words),
            )
            return subtitle_offset_guard.evaluate_proposed_offset(
                subtitle,
                fingerprint,
                proposed,
            )

    def test_pearl_like_wrong_shift_keeps_original(self):
        decision = self._case([-0.455, -0.036, -0.618, 0.341], -1.338)
        self.assertTrue(decision.eligible)
        self.assertEqual(decision.status, "KEEP_ORIGINAL")
        self.assertEqual(decision.selected_offset, 0.0)
        self.assertLess(decision.baseline_cost, decision.shifted_cost)

    def test_long_subtitle_display_does_not_invent_das_boot_like_shift(self):
        with tempfile.TemporaryDirectory() as folder:
            subtitle = Path(folder) / "candidate.srt"
            blocks = []
            words = []
            for index in range(3):
                cue_start = 100.0 + index * 100.0
                blocks.append(
                    f"{index + 1}\n"
                    f"{pro_core.legacy.srt_time_from_milliseconds(round(cue_start * 1000))} --> "
                    f"{pro_core.legacy.srt_time_from_milliseconds(round((cue_start + 3) * 1000))}\n"
                    "alpha beta gamma delta\n"
                )
                for word_index, text in enumerate(("alpha", "beta", "gamma", "delta")):
                    word_start = cue_start + word_index * 0.3
                    words.append(audio_offset_verifier.TimedWord(
                        word_start, word_start + 0.3, text, index, 0.95,
                    ))
            subtitle.write_text("\n".join(blocks), encoding="utf-8")
            fingerprint = audio_offset_verifier.MovieAudioFingerprint(
                (0, 1, 2), (), (), tuple(words),
            )
            decision = subtitle_offset_guard.evaluate_proposed_offset(
                subtitle, fingerprint, -0.9,
            )

        self.assertEqual(decision.region_offsets, (0.0, 0.0, 0.0))
        self.assertEqual(decision.status, "KEEP_ORIGINAL")
        self.assertEqual(decision.selected_offset, 0.0)

    def test_long_display_still_allows_real_fixed_offsets_in_both_directions(self):
        for actual_offset in (-3.0, 3.0):
            with self.subTest(actual_offset=actual_offset), tempfile.TemporaryDirectory() as folder:
                subtitle = Path(folder) / "candidate.srt"
                blocks = []
                words = []
                for index in range(3):
                    cue_start = 100.0 + index * 100.0
                    blocks.append(
                        f"{index + 1}\n"
                        f"{pro_core.legacy.srt_time_from_milliseconds(round(cue_start * 1000))} --> "
                        f"{pro_core.legacy.srt_time_from_milliseconds(round((cue_start + 3) * 1000))}\n"
                        "alpha beta gamma delta\n"
                    )
                    for word_index, text in enumerate(("alpha", "beta", "gamma", "delta")):
                        word_start = cue_start + actual_offset + word_index * 0.3
                        words.append(audio_offset_verifier.TimedWord(
                            word_start, word_start + 0.3, text, index, 0.95,
                        ))
                subtitle.write_text("\n".join(blocks), encoding="utf-8")
                fingerprint = audio_offset_verifier.MovieAudioFingerprint(
                    (0, 1, 2), (), (), tuple(words),
                )
                decision = subtitle_offset_guard.evaluate_proposed_offset(
                    subtitle, fingerprint, actual_offset,
                )
                self.assertEqual(decision.status, "APPLY_OFFSET")
                self.assertEqual(decision.region_offsets, (actual_offset,) * 3)
                self.assertEqual(decision.selected_offset, actual_offset)

    def test_missing_first_word_cannot_claim_cue_start_offset(self):
        with tempfile.TemporaryDirectory() as folder:
            subtitle = Path(folder) / "candidate.srt"
            subtitle.write_text(
                "1\n00:01:40,000 --> 00:01:43,000\nalpha beta gamma delta\n",
                encoding="utf-8",
            )
            words = tuple(
                audio_offset_verifier.TimedWord(100.0 + index * 0.3, 100.3 + index * 0.3,
                                                text, 0, 0.95)
                for index, text in enumerate(("beta", "gamma", "delta"))
            )
            fingerprint = audio_offset_verifier.MovieAudioFingerprint((0,), (), (), words)
            self.assertEqual(
                subtitle_offset_guard.measured_clip_offsets(subtitle, fingerprint, 0.0),
                {},
            )

    def test_conflicting_cues_in_one_window_cannot_be_averaged_or_hidden_by_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            subtitle = Path(folder) / "candidate.srt"
            cases = (
                (100.0, "alpha beta gamma delta", -0.375, 0),
                (105.0, "epsilon zeta eta theta", 1.079, 0),
                (200.0, "iota kappa lambda mu", -0.863, 1),
                (300.0, "nu omicron sigma tau", -1.190, 2),
                (310.0, "red green blue yellow", -0.187, 3),
            )
            blocks = []
            words = []
            for sequence, (start, body, offset, clip_index) in enumerate(cases, 1):
                blocks.append(
                    f"{sequence}\n"
                    f"{pro_core.legacy.srt_time_from_milliseconds(round(start * 1000))} --> "
                    f"{pro_core.legacy.srt_time_from_milliseconds(round((start + 3) * 1000))}\n"
                    f"{body}\n"
                )
                for word_index, token in enumerate(body.split()):
                    word_start = start + offset + word_index * 0.3
                    words.append(audio_offset_verifier.TimedWord(
                        word_start, word_start + 0.3, token, clip_index, 0.95,
                    ))
            subtitle.write_text("\n".join(blocks), encoding="utf-8")
            fingerprint = audio_offset_verifier.MovieAudioFingerprint(
                (0, 1, 2, 3), (), (), tuple(words),
            )
            conflicts = subtitle_offset_guard.conflicted_clip_offsets(
                subtitle, fingerprint, 0.0,
            )
            measurements = subtitle_offset_guard.measured_clip_offsets(
                subtitle, fingerprint, 0.0,
            )
            decision = subtitle_offset_guard.evaluate_proposed_offset(
                subtitle, fingerprint, -0.863,
                region_by_clip={0: 0, 1: 1, 2: 2, 3: 0},
            )

        self.assertEqual(len(conflicts[0]), 2)
        self.assertAlmostEqual(max(conflicts[0]) - min(conflicts[0]), 1.454)
        self.assertNotIn(0, measurements)
        self.assertEqual(decision.status, "TIMING_UNRESOLVED")
        self.assertIn("同一音频窗口0", decision.reason)

    def test_source_code_like_large_shift_is_applied(self):
        decision = self._case([8.229, 7.314, 8.117], 7.398)
        self.assertTrue(decision.eligible)
        self.assertEqual(decision.status, "APPLY_OFFSET")
        self.assertAlmostEqual(decision.selected_offset, 7.398)
        self.assertLess(decision.shifted_cost, decision.baseline_cost)

    def test_two_regions_cannot_authorize_a_shift(self):
        decision = self._case([2.1, 2.2], 2.0)
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.status, "NEED_MORE")

    def test_three_regions_with_one_outlier_remain_unresolved(self):
        decision = self._case([0.85, 1.00, -0.52], 0.85)
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.status, "TIMING_UNRESOLVED")

    def test_all_original_residuals_safe_keeps_even_without_shift_consensus(self):
        decision = self._case([0.048, -0.572, 0.458], 0.0)
        self.assertTrue(decision.eligible)
        self.assertEqual(decision.status, "KEEP_ORIGINAL")
        self.assertEqual(decision.selected_offset, 0.0)

    def test_fourth_region_cannot_hide_a_conflicting_measurement(self):
        decision = self._case([0.55, 1.01, 1.35, -0.23], 0.85)
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.status, "TIMING_UNRESOLVED")

    def test_shared_public_audio_uses_exactly_three_positions_and_can_fall_back(self):
        empty = audio_offset_verifier.MovieAudioFingerprint((), (), (), ())
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            pro_core.legacy, "inspect_media", return_value={}
        ), mock.patch.object(
            pro_core.legacy, "video_track_duration_ns", return_value=7_200_000_000_000
        ), mock.patch.object(
            pro_core, "_preferred_validation_audio_id", return_value=0
        ), mock.patch.object(
            pro_core, "_audio_stream_index", return_value=0
        ), mock.patch.object(
            pro_core, "_selected_audio_language", return_value="en"
        ), mock.patch.object(
            pro_core, "_tool", return_value="tool.exe"
        ), mock.patch.object(
            pro_core.audio_offset_verifier,
            "persistent_cache_dir",
            return_value=Path(folder) / "cache",
        ), mock.patch.object(
            pro_core.audio_offset_verifier,
            "build_movie_audio_fingerprint",
            return_value=empty,
        ) as build:
            shared = pro_core._legacy_whisper_prepare_shared_subtitle_content_audio(
                "movie.mkv",
                0,
                folder,
                lambda _message: None,
            )

        self.assertEqual(shared.fingerprint.usable_clip_count, 0)
        self.assertEqual(
            build.call_args.kwargs["probe_fractions"],
            pro_core.SHARED_SUBTITLE_PROBE_FRACTIONS,
        )
        self.assertEqual(build.call_args.kwargs["fingerprint_clips"], 3)


if __name__ == "__main__":
    unittest.main()
