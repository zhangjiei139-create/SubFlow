from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import audio_offset_verifier
import pro_core
import subtitle_offset_guard
import subtitle_tool_core as legacy


class EmbeddedTargetedAlignmentTest(unittest.TestCase):
    def _run_alignment(self, targeted_decision, initial_decision=None):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "Big.Fish.2003.mkv"
            subtitle = root / "embedded.srt"
            corrected = root / "corrected.srt"
            video.write_bytes(b"movie")
            subtitle.write_text("original", encoding="utf-8")
            corrected.write_text("shifted", encoding="utf-8")
            events = [
                legacy.SubtitleEvent(
                    f"00:{index // 2:02d}:00,000",
                    f"00:{index // 2:02d}:02,000",
                    "A complete and distinctive line of spoken English dialogue.",
                )
                for index in range(30)
            ]
            # The final cue must cover enough of the stated runtime.
            events[-1] = legacy.SubtitleEvent(
                "00:15:00,000", "00:15:02,000", events[-1].text
            )
            shared = pro_core.SharedSubtitleContentAudio(
                duration_seconds=1000.0,
                validation_audio_id=1,
                audio_stream_index=1,
                audio_language="en",
                fingerprint=audio_offset_verifier.MovieAudioFingerprint((), (), ()),
            )
            rough = audio_offset_verifier.AudioOffsetResult(
                True, -3.424, (), (), "rough proposal"
            )
            need_more = initial_decision or subtitle_offset_guard.OffsetGuardDecision(
                False, "NEED_MORE", None, -3.424, (-3.424,), None, None,
                "only one region",
            )
            messages = []
            with patch.object(legacy, "parse_subtitle", return_value=events), \
                 patch.object(legacy, "inspect_media", return_value={}), \
                 patch.object(legacy, "video_track_duration_ns", return_value=1_000_000_000_000), \
                 patch.object(pro_core, "_preferred_validation_audio_id", return_value=1), \
                 patch.object(audio_offset_verifier, "match_subtitle_to_fingerprint", return_value=rough), \
                 patch.object(subtitle_offset_guard, "evaluate_proposed_offset", return_value=need_more), \
                 patch.object(pro_core, "_targeted_candidate_offset_guard", return_value=(targeted_decision, 2)) as targeted, \
                 patch.object(pro_core, "_shifted_srt", return_value=corrected) as shift:
                output, report = pro_core.align_embedded_text_track(
                    str(video), subtitle, root / "work", 1, "en", 3,
                    messages.append, shared_content_audio=shared,
                )
            return subtitle, corrected, output, report, targeted, shift, messages

    def test_need_more_uses_bounded_region_backups_before_applying_offset(self) -> None:
        applied = subtitle_offset_guard.OffsetGuardDecision(
            True, "APPLY_OFFSET", -3.424, -3.424,
            (-3.424, -3.300, -3.500), 3.424, 0.124,
            "three distant regions agree",
        )
        _source, corrected, output, report, targeted, shift, messages = self._run_alignment(applied)
        targeted.assert_called_once()
        self.assertEqual(output, corrected)
        self.assertIn("-3.42 秒", report)
        self.assertEqual(shift.call_args.args[2], -3424)
        self.assertTrue(any("分区补查实际取证 2 处" in message for message in messages))

    def test_need_more_still_preserves_original_when_backups_fail(self) -> None:
        unresolved = subtitle_offset_guard.OffsetGuardDecision(
            False, "NEED_MORE", None, -3.424, (-3.424,), None, None,
            "still only one region",
        )
        source, _corrected, output, report, targeted, shift, _messages = self._run_alignment(unresolved)
        targeted.assert_called_once()
        shift.assert_not_called()
        self.assertEqual(output, source)
        self.assertIn("保留原时间轴", report)

    def test_trusted_conflict_is_not_hidden_by_replacing_sample_points(self) -> None:
        conflict = subtitle_offset_guard.OffsetGuardDecision(
            False, "TIMING_UNRESOLVED", None, -3.424,
            (-3.424, 2.100, -3.500), None, None,
            "credible regional conflict",
        )
        source, _corrected, output, _report, targeted, shift, _messages = \
            self._run_alignment(conflict, initial_decision=conflict)
        targeted.assert_not_called()
        shift.assert_not_called()
        self.assertEqual(output, source)


if __name__ == "__main__":
    unittest.main()


# These cases assert the retained Whisper algorithm, not the active VAD route.
# Active entry-point equivalence is covered by test_continuous_vad_route.
def setUpModule():
    global _historical_scope
    from tests.legacy_whisper_context import enter
    _historical_scope = enter()

def tearDownModule():
    _historical_scope.close()
