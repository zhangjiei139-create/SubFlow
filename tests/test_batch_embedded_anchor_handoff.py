"""Keep a verified embedded source when crossing into the formal batch stage."""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import batch_core
from profile_model import BatchPlan, PreferenceProfile
import smart_subtitles
import subtitle_tool_core as core


class BatchEmbeddedAnchorHandoffTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.movie = self.root / "Film.1983.mkv"
        self.movie.write_bytes(b"movie")
        self.tracks = [
            core.Track(1, "audio", "TrueHD Atmos", "eng", "", True, False, False, False, False),
            core.Track(2, "audio", "AC-3", "eng", "", False, False, False, False, False),
            core.Track(3, "subtitles", "SubRip/SRT", "eng", "English-SRT", True, False, True, False, False),
            core.Track(4, "subtitles", "SubRip/SRT", "eng", "English-SDH-SRT", False, False, True, False, False),
            core.Track(6, "subtitles", "HDMV PGS", "ger", "German-PGS", False, False, False, True, False),
        ]
        self.shared = SimpleNamespace(validation_audio_id=2, vad_reference=self.root / "speech.npz")
        self.profile = PreferenceProfile(1, "test", audio_policy="native",
                                         subtitle_languages=["en", "de", "zh-CN"])
        self.messages = []

    def plan(self, *, translate=True):
        return BatchPlan(path=str(self.movie), profile_slot=1, status="ready",
                         output_path=str(self.root / "Film.SF.mkv"), audio_ids=[1, 2],
                         subtitle_ids=[3, 6] if translate else [6],
                         missing_subtitle_languages=["zh-CN"] if translate else [],
                         source_subtitle_id=3, source_mode="embedded" if translate else "none",
                         has_complete_text=True, has_complete_english_text=True,
                         has_complete_subtitle=True, has_image_subtitles=True,
                         has_retained_image_subtitles=True)

    def run_plan(self, plan, prepare):
        with ExitStack() as stack:
            stack.enter_context(patch.object(batch_core.core, "inspect_tracks", return_value=self.tracks))
            shared = stack.enter_context(patch.object(batch_core.pro_core, "prepare_shared_subtitle_content_audio",
                                                       return_value=self.shared))
            prepared = stack.enter_context(patch.object(batch_core.pro_core, "prepare_embedded_text_corrections",
                                                         side_effect=prepare))
            process = stack.enter_context(patch.object(batch_core.pro_core, "process_pro"))
            search = stack.enter_context(patch.object(smart_subtitles, "find_verified_english"))
            batch_core.process_plan(plan, self.profile, self.messages.append, None)
        search.assert_not_called()
        return shared, prepared, process

    def test_confirmed_source_is_forwarded_without_selecting_unused_sdh(self):
        handoff = object()
        def prepare(*args, **kwargs):
            kwargs["verification_status"].update(verified_anchor=True, track_id=3,
                                                  prepared_anchor=handoff)
            return {3: 60}, {3: self.root / "corrected.srt"}, 3
        shared, prepare_call, process = self.run_plan(self.plan(), prepare)
        shared.assert_called_once()
        prepare_call.assert_called_once()
        self.assertEqual(prepare_call.call_args.args[1], [3])
        kwargs = process.call_args.kwargs
        self.assertIs(kwargs["prepared_embedded_anchor"], handoff)
        self.assertIs(kwargs["shared_content_audio"], self.shared)
        self.assertEqual(kwargs["audio_source_id"], 1)
        self.assertEqual(kwargs["embedded_source_id"], 3)
        self.assertEqual(kwargs["keep_subtitle_ids"], [3, 6])
        self.assertEqual(kwargs["target_codes"], ["zh-CN"])

    def test_timing_only_anchor_is_forwarded_for_selected_image_subtitles(self):
        handoff = object()
        def prepare(*args, **kwargs):
            kwargs["verification_status"].update(verified_anchor=True, prepared_anchor=handoff)
            return {3: 60}, {3: self.root / "corrected.srt"}, 3
        self.profile.subtitle_languages = ["de"]
        _, _, process = self.run_plan(self.plan(translate=False), prepare)
        kwargs = process.call_args.kwargs
        self.assertIs(kwargs["prepared_embedded_anchor"], handoff)
        self.assertEqual(kwargs["source_mode"], "none")
        self.assertEqual(kwargs["keep_subtitle_ids"], [6])
        self.assertEqual(kwargs["embedded_source_id"], 3)

    def test_failed_first_source_cannot_leak_its_handoff_into_next_candidate(self):
        rejected, accepted = object(), object()
        def prepare(*args, **kwargs):
            track_id = args[2]
            kwargs["verification_status"].update(verified_anchor=track_id == 4,
                track_id=track_id, prepared_anchor=accepted if track_id == 4 else rejected)
            return {track_id: 60}, {track_id: self.root / "corrected.srt"}, track_id
        _, prepared, process = self.run_plan(self.plan(), prepare)
        self.assertEqual(prepared.call_count, 2)
        self.assertEqual(process.call_args.kwargs["embedded_source_id"], 4)
        self.assertIs(process.call_args.kwargs["prepared_embedded_anchor"], accepted)

    def test_legacy_preparer_without_handoff_remains_compatible(self):
        def prepare(*args, **kwargs):
            kwargs["verification_status"].update(verified_anchor=True)
            return {}, {}, 3
        _, _, process = self.run_plan(self.plan(), prepare)
        self.assertIsNone(process.call_args.kwargs["prepared_embedded_anchor"])

    def test_verified_external_reference_keeps_priority_over_embedded_sources(self):
        plan = self.plan()
        external = self.root / "confirmed.srt"
        external.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello.\n", encoding="utf-8")
        plan.source_mode = "external"
        plan.external_subtitle = str(external)
        plan.external_subtitle_verified = True
        plan.external_language = "en"
        shared, prepared, process = self.run_plan(plan, None)
        shared.assert_not_called()
        prepared.assert_not_called()
        self.assertIsNone(process.call_args.kwargs["prepared_embedded_anchor"])
        self.assertEqual(process.call_args.kwargs["verified_timeline_reference"], str(external))
        self.assertEqual(process.call_args.kwargs["external_subtitle"], str(external))
        self.assertEqual(process.call_args.kwargs["source_mode"], "online")


if __name__ == "__main__":
    unittest.main()
