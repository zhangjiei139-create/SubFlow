"""Same-run image timing handoff; media tools and network are never invoked."""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import batch_core
import continuous_vad
import pgs_local_alignment as alignment
import pro_core as p
import smart_subtitles
import subtitle_tool_core as core
from profile_model import BatchPlan, PreferenceProfile


class PreparedImageHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.movie = self.root / "Movie.2014.mkv"
        self.movie.write_bytes(b"stub movie")
        self.reference = self.root / "aligned.srt"
        self.write_reference(self.reference)
        self.tracks = [
            core.Track(1, "audio", "AAC", "eng", "", True, False, False, False, False),
            core.Track(3, "subtitles", "SubRip", "eng", "", False, False, True, False, False),
            core.Track(4, "subtitles", "HDMV PGS", "deu", "", False, False, False, True, False),
            core.Track(5, "subtitles", "HDMV PGS", "eng", "", False, False, False, True, False),
        ]
        self.decisions = {1: self.decision(-.6), 2: self.decision(1.25)}
        self.logs = []

    def write_reference(self, destination, suffix=""):
        events = [core.SubtitleEvent(core.srt_time_from_milliseconds(1000 * (10 + i * 10)),
                                    core.srt_time_from_milliseconds(1000 * (12 + i * 10)),
                                    f"Dialogue {i}{suffix}.") for i in range(30)]
        core.write_srt(destination, events, {i: event.text for i, event in enumerate(events, 1)})

    @staticmethod
    def decision(offset=0, accepted=True):
        return alignment.LocalAlignmentResult(accepted, offset if accepted else None, offset,
                    "supported_fixed_shift" if accepted else "start_residual_inconsistent")

    def context(self):
        stack = ExitStack()
        stack.enter_context(patch.object(core, "inspect_tracks", side_effect=lambda *_: self.tracks))
        stack.enter_context(patch.object(core, "prepare_work_input", return_value=str(self.movie)))
        stack.enter_context(patch.object(core, "inspect_media", return_value={}))
        stack.enter_context(patch.object(core, "video_track_duration_ns", return_value=1000 * 10**9))
        return stack

    def issue(self, reference=None, cancel=None):
        status = {}
        results = p.require_image_timeline_anchor(str(self.movie), [4, 5], 1,
                self.root / "work", reference or self.reference, self.logs.append, cancel,
                prepared_image_status=status)
        return status.get("prepared_image_timing"), results

    def prepare(self, prepared, *, ids=None, audio_id=1, cancel=None):
        return p.prepare_embedded_text_corrections(str(self.movie), ids or [4, 5], None,
                audio_id, str(self.root / "work"), self.logs.append, cancel,
                verified_external_subtitle=str(self.reference), prepared_image_timing=prepared)

    def test_completed_candidate_reuses_each_track_offset_without_second_check(self):
        with self.context(), patch.object(p, "_pgs_local_corrections", return_value=self.decisions) as local, \
                patch.object(p, "_embedded_image_intervals") as full, \
                patch.object(p, "_shifted_srt") as shift:
            prepared, results = self.issue()
            offsets, sources, source_id = self.prepare(prepared)
        self.assertIsInstance(prepared, p.PreparedImageTiming)
        self.assertEqual(offsets, {4: -600, 5: 1250})
        self.assertEqual(results[4]["offset_ms"], -600)
        self.assertEqual(sources, {})
        self.assertIsNone(source_id)
        local.assert_called_once()
        full.assert_not_called()
        shift.assert_not_called()

    def test_formal_stage_applies_image_offsets_once_and_does_not_shift_anchor(self):
        with self.context(), patch.object(p, "_pgs_local_corrections", return_value=self.decisions) as local, \
                patch.object(p, "process_tracks_only", return_value="completed") as remux, \
                patch.object(p, "_shifted_srt") as shift:
            prepared, _ = self.issue()
            result = p.process_pro(str(self.movie), str(self.root / "output.mkv"), [1], [4, 5],
                "none", None, None, 1, "en", "en", [], str(self.root / "work"),
                self.logs.append, 1, None, verified_timeline_reference=str(self.reference),
                prepared_image_timing=prepared)
        self.assertEqual(result, "completed")
        self.assertEqual(remux.call_args.args[8], {4: -600, 5: 1250})
        local.assert_called_once()
        shift.assert_not_called()

    def test_full_fallback_success_is_reused_without_second_local_or_full_check(self):
        with self.context(), patch.object(p, "_pgs_local_corrections", return_value={}) as local, \
                patch.object(p, "_embedded_image_intervals", return_value=[(i * 10., i * 10. + 2.)
                    for i in range(30)]) as full, \
                patch.object(p, "_interval_alignment", side_effect=[(.91, -.6), (.95, 1.25)]) as compare:
            prepared, _ = self.issue()
            offsets, _, _ = self.prepare(prepared)
        self.assertEqual(offsets, {4: -600, 5: 1250})
        local.assert_called_once()
        self.assertEqual(full.call_count, 2)
        self.assertEqual(compare.call_count, 2)

    def test_movie_or_text_change_during_acceptance_cannot_issue_handoff(self):
        for change in ("movie", "reference"):
            def mutate(*args, **kwargs):
                if change == "movie":
                    self.movie.write_bytes(self.movie.read_bytes() + b" changed during acceptance")
                else:
                    self.write_reference(self.reference, suffix=" changed during acceptance")
                return self.decisions
            with self.subTest(change=change), self.context(), \
                    patch.object(p, "_pgs_local_corrections", side_effect=mutate):
                prepared, results = self.issue()
                self.assertIsNone(prepared)
                self.assertTrue(all(result["accepted"] for result in results.values()))

    def test_changed_movie_or_reference_or_rules_falls_back(self):
        for change in ("movie", "reference", "rules"):
            with self.subTest(change=change), self.context(), \
                    patch.object(p, "_pgs_local_corrections", return_value=self.decisions) as local:
                prepared, _ = self.issue()
                with ExitStack() as changes:
                    if change == "movie":
                        self.movie.write_bytes(self.movie.read_bytes() + b" changed")
                    elif change == "reference":
                        self.write_reference(self.reference, suffix="changed")
                    else:
                        changes.enter_context(patch.object(continuous_vad, "RULE_VERSION", "new-rule"))
                    self.prepare(prepared)
                self.assertEqual(local.call_count, 2)
        self.assertTrue(any("按原有流程重新核验" in line for line in self.logs))

    def test_changed_track_order_codec_retention_or_audio_falls_back(self):
        original = list(self.tracks)
        for change in ("order", "codec", "retention", "audio"):
            self.tracks = list(original)
            with self.subTest(change=change), self.context(), \
                    patch.object(p, "_pgs_local_corrections", return_value=self.decisions) as local:
                prepared, _ = self.issue()
                ids, audio_id = [4, 5], 1
                if change == "order":
                    self.tracks = [original[0], original[1], original[3], original[2]]
                elif change == "codec":
                    self.tracks = [*original[:2], replace(original[2], codec="different PGS"), original[3]]
                elif change == "retention":
                    ids = [4]
                else:
                    self.tracks = [replace(original[0], id=2), *original[1:]]
                    audio_id = 2
                self.prepare(prepared, ids=ids, audio_id=audio_id)
                self.assertEqual(local.call_count, 2)

    def test_unissued_dict_and_modified_copy_cannot_bypass_verification(self):
        with self.context(), patch.object(p, "_pgs_local_corrections", return_value=self.decisions) as local:
            prepared, _ = self.issue()
            forged = replace(prepared, offsets=((4, 0, "forged"), (5, 0, "forged")))
            for unissued in ({"accepted": True, "offset_ms": 0}, forged):
                offsets, _, _ = self.prepare(unissued)
                self.assertEqual(offsets, {4: -600, 5: 1250})
                self.assertFalse(p.prepared_image_timing_matches_subtitle(unissued, self.reference))
            self.assertEqual(local.call_count, 3)

    def test_failed_candidate_clears_status_and_is_never_issued(self):
        status = {"prepared_image_timing": object()}
        with self.context(), patch.object(p, "_pgs_local_corrections", return_value={}), \
                patch.object(p, "_embedded_image_intervals", return_value=[]):
            with self.assertRaises(p.SubtitleContentMismatchError):
                p.require_image_timeline_anchor(str(self.movie), [4, 5], 1, self.root / "work",
                    self.reference, self.logs.append, prepared_image_status=status)
        self.assertEqual(status, {})

    def test_precancelled_formal_reuse_does_not_apply_offsets(self):
        with self.context(), patch.object(p, "_pgs_local_corrections", return_value=self.decisions) as local:
            prepared, _ = self.issue()
            cancel = threading.Event()
            cancel.set()
            with self.assertRaises(core.CancelledError):
                self.prepare(prepared, cancel=cancel)
            local.assert_called_once()

    def run_batch(self, search):
        plan = BatchPlan(path=str(self.movie), profile_slot=1, status="ready",
            output_path=str(self.root / "Movie.SF.mkv"), audio_ids=[1], subtitle_ids=[4, 5],
            source_mode="none", has_image_subtitles=True, has_retained_image_subtitles=True)
        profile = PreferenceProfile(1, "fixture", subtitle_languages=["en", "de"])
        with patch.object(batch_core, "ordered_text_anchors", return_value=[]), \
                patch.object(smart_subtitles, "find_verified_english", side_effect=search), \
                patch.object(p, "process_pro") as process:
            batch_core.process_plan(plan, profile, self.logs.append, None)
        return process.call_args.kwargs["prepared_image_timing"]

    def test_batch_only_hands_off_result_for_final_subtitle_hash(self):
        final = self.root / "final.srt"
        final.write_bytes(self.reference.read_bytes())
        def search(*args, **kwargs):
            kwargs["candidate_acceptance"](self.reference, None)
            return SimpleNamespace(subtitle_path=str(final), language="en", report="verified",
                provider="fixture", release="fixture", identity_key="fixture", verification_seal="fixture")
        with self.context(), patch.object(p, "_pgs_local_corrections", return_value=self.decisions):
            prepared = self.run_batch(search)
            self.assertTrue(p.prepared_image_timing_matches_subtitle(prepared, final))
            self.write_reference(final, suffix="different")
            self.assertIsNone(self.run_batch(search))

    def test_batch_failed_later_candidate_cannot_reuse_earlier_result(self):
        final = self.reference
        def search(*args, **kwargs):
            accept = kwargs["candidate_acceptance"]
            accept(final, None)
            with patch.object(p, "_pgs_local_corrections", return_value={}), \
                    patch.object(p, "_embedded_image_intervals", return_value=[]):
                with self.assertRaises(p.SubtitleContentMismatchError):
                    accept(final, None)
            return SimpleNamespace(subtitle_path=str(final), language="en", report="verified",
                provider="fixture", release="fixture", identity_key="fixture", verification_seal="fixture")
        with self.context(), patch.object(p, "_pgs_local_corrections", return_value=self.decisions):
            self.assertIsNone(self.run_batch(search))

    def test_local_reason_does_not_claim_verified_drift(self):
        self.assertEqual(p._pgs_local_reason("start_residual_inconsistent"), "局部估计未能支持统一固定偏移")


if __name__ == "__main__":
    unittest.main()
