"""Prepared embedded anchors avoid extra IO without bypassing provenance checks."""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import tempfile
import unittest

import continuous_vad
import pgs_local_alignment
import pro_core as core
import subtitle_tool_core as legacy


def track(number, kind="text", language="en"):
    if kind == "audio":
        codec = "TrueHD Atmos" if number == 1 else "AC-3"
        return legacy.Track(number, "audio", codec, language, "", number == 1, False, False, False, False)
    return legacy.Track(number, "subtitles", "SubRip" if kind == "text" else "HDMV PGS",
                        language, "", False, False, kind == "text", kind == "pgs", False)


class PreparedEmbeddedAnchorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.movie = self.root / "Film.1983.mkv"
        self.movie.write_bytes(b"fixture movie; external media tools are mocked")
        self.work = self.root / "work"
        self.tracks = [track(1, "audio"), track(2, "audio"), track(3), track(4), track(6, "pgs", "zh-CN"), track(8)]
        self.logs = []
        self.events = [legacy.SubtitleEvent(
            legacy.srt_time_from_milliseconds((5 + 10 * i) * 1000),
            legacy.srt_time_from_milliseconds((7 + 10 * i) * 1000),
            f"This is a complete line of the film dialogue number {i}.",
        ) for i in range(180)]
        self.shared = SimpleNamespace(validation_audio_id=2)
        self.extracted_ids = []
        self.audio_checks = []
        self.local_references = []

    def write_events(self, path, events=None):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        events = self.events if events is None else events
        legacy.write_srt(path, events, {i: e.text for i, e in enumerate(events, 1)})
        return path

    def extract(self, args, **_kwargs):
        self.assertEqual(args[1], "tracks")
        for specification in args[3:]:
            number, destination = specification.split(":", 1)
            self.extracted_ids.append(int(number))
            self.write_events(destination)
        return SimpleNamespace(returncode=0)

    def audio_check(self, _movie, source, work, _audio, _language, track_id, *_args, **_kwargs):
        self.audio_checks.append(track_id)
        Path(work).mkdir(parents=True, exist_ok=True)
        corrected = core._shifted_srt(Path(source), Path(work) / "verified-anchor.srt", 60)
        return corrected, "连续VAD时间轴核验通过：提议固定偏移 +0.06 秒；待候选其余验收"

    def local(self, _movie, indices, reference, *_args, **_kwargs):
        self.local_references.append(reference)
        return {index: pgs_local_alignment.LocalAlignmentResult(True, .3, .3, "supported_fixed_shift")
                for index in indices}

    def context(self):
        stack = ExitStack()
        stack.enter_context(patch.object(legacy, "inspect_tracks", side_effect=lambda _: self.tracks))
        stack.enter_context(patch.object(legacy, "inspect_media", return_value={"container": {"properties": {"duration": 1800 * 10**9}}}))
        stack.enter_context(patch.object(legacy, "prepare_work_input", return_value=str(self.movie)))
        stack.enter_context(patch.object(legacy, "run_command", side_effect=self.extract))
        stack.enter_context(patch.object(core, "align_embedded_text_track", side_effect=self.audio_check))
        stack.enter_context(patch.object(core, "_pgs_local_corrections", side_effect=self.local))
        stack.enter_context(patch.object(core, "_interval_alignment", return_value=(.98, .24)))
        stack.enter_context(patch.object(core, "_embedded_image_intervals", side_effect=AssertionError("local acceptance must avoid full scan")))
        return stack

    def prepare(self, keep=(3,), *, prepared=None, status=None, external=None, source_id=3):
        return core.prepare_embedded_text_corrections(
            str(self.movie), list(keep), source_id, 1, str(self.work), self.logs.append,
            include_source_alternatives=prepared is not None,
            shared_content_audio=self.shared, verification_status=status,
            verified_external_subtitle=str(external) if external is not None else None,
            prepared_embedded_anchor=prepared,
        )

    def handoff(self, source_id=3):
        status = {}
        with self.context():
            self.prepare(keep=(source_id,), source_id=source_id, status=status)
        prepared = status["prepared_anchor"]
        self.assertIsInstance(prepared, core.PreparedEmbeddedAnchor)
        self.assertEqual(prepared.validation_audio_id, 2)
        self.assertEqual(prepared.offset_ms, 60)
        self.assertTrue(status["report"].startswith("已采用连续VAD固定偏移"))
        self.assertNotIn("待候选其余验收", status["report"])
        self.extracted_ids.clear()
        self.audio_checks.clear()
        self.logs.clear()
        return prepared

    def test_current_handoff_skips_anchor_and_unretained_alternative(self):
        prepared = self.handoff()
        with self.context():
            offsets, corrected, source_id = self.prepare(prepared=prepared)
        self.assertEqual(source_id, 3)
        self.assertEqual(offsets, {3: 60})
        self.assertEqual(corrected[3], prepared.corrected_subtitle_path)
        self.assertEqual(self.extracted_ids, [])
        self.assertEqual(self.audio_checks, [])
        self.assertTrue(any("不重复提取或音频对时" in line for line in self.logs))

    def test_main_truehd_track_can_use_actual_ac3_validation_handoff(self):
        prepared = self.handoff()
        self.assertEqual(core._prepared_anchor_audio_id(self.tracks, 1, self.shared), 2)
        with self.context():
            self.prepare(prepared=prepared)
        self.assertEqual(self.audio_checks, [])

    def test_other_retained_text_and_pgs_still_use_corrected_anchor(self):
        prepared = self.handoff()
        with self.context():
            offsets, corrected, _ = self.prepare(keep=(3, 6, 8), prepared=prepared)
        self.assertEqual(self.extracted_ids, [8])
        self.assertNotIn(4, self.extracted_ids)
        self.assertEqual(self.audio_checks, [])
        self.assertEqual(offsets, {3: 60, 8: 240, 6: 300})
        self.assertEqual(set(corrected), {3, 8})
        first = legacy.parse_subtitle(prepared.corrected_subtitle_path)[0]
        self.assertEqual(self.local_references[-1][0],
                         (core._subtitle_time_seconds(first.start), core._subtitle_time_seconds(first.end)))

    def test_retained_same_language_alternative_is_still_extracted(self):
        prepared = self.handoff()
        with self.context():
            offsets, corrected, _ = self.prepare(keep=(3, 4), prepared=prepared)
        self.assertEqual(self.extracted_ids, [4])
        self.assertEqual(self.audio_checks, [])
        self.assertEqual(offsets[4], 240)
        self.assertIn(4, corrected)

    def test_non_english_handoff_keeps_its_anchor_priority(self):
        self.tracks = [entry if entry.id != 8 else replace(entry, language="ja")
                       for entry in self.tracks]
        prepared = self.handoff(source_id=8)
        with self.context():
            offsets, corrected, source_id = self.prepare(
                keep=(3, 6, 8), source_id=8, prepared=prepared,
            )
        self.assertEqual(source_id, 8)
        self.assertEqual(self.extracted_ids, [3])
        self.assertEqual(self.audio_checks, [])
        self.assertEqual(offsets, {8: 60, 3: 240, 6: 300})
        self.assertEqual(corrected[8], prepared.corrected_subtitle_path)
        first = legacy.parse_subtitle(prepared.corrected_subtitle_path)[0]
        self.assertEqual(self.local_references[-1][0],
                         (core._subtitle_time_seconds(first.start), core._subtitle_time_seconds(first.end)))

    def assert_rechecked(self, prepared):
        with self.context():
            self.prepare(prepared=prepared)
        self.assertIn(4, self.extracted_ids)
        self.assertEqual(self.audio_checks, [3])
        self.assertTrue(any("不能复用" in line for line in self.logs))

    def test_changed_movie_is_rechecked(self):
        prepared = self.handoff()
        self.movie.write_bytes(self.movie.read_bytes() + b" changed")
        self.assert_rechecked(prepared)

    def test_changed_rule_is_rechecked(self):
        prepared = self.handoff()
        with patch.object(continuous_vad, "RULE_VERSION", continuous_vad.RULE_VERSION + "-next"):
            self.assert_rechecked(prepared)

    def test_changed_validation_audio_is_rechecked(self):
        prepared = self.handoff()
        self.tracks = [track for track in self.tracks if track.id != 2]
        self.shared = SimpleNamespace(validation_audio_id=1)
        self.assert_rechecked(prepared)

    def test_changed_source_hash_is_rechecked(self):
        prepared = self.handoff()
        prepared.source_subtitle_path.write_bytes(prepared.source_subtitle_path.read_bytes() + b"\n")
        self.assert_rechecked(prepared)

    def test_changed_corrected_hash_is_rechecked(self):
        prepared = self.handoff()
        prepared.corrected_subtitle_path.write_bytes(prepared.corrected_subtitle_path.read_bytes() + b"\n")
        self.assert_rechecked(prepared)

    def test_missing_corrected_file_is_rechecked(self):
        prepared = self.handoff()
        prepared.corrected_subtitle_path.unlink()
        self.assert_rechecked(prepared)

    def test_changed_track_language_is_rechecked(self):
        prepared = self.handoff()
        self.tracks = [track if track.id != 3 else replace(track, language="de") for track in self.tracks]
        with self.context():
            self.prepare(prepared=prepared)
        self.assertEqual(self.audio_checks, [3])
        self.assertTrue(any("不能复用" in line for line in self.logs))

    def test_pending_report_cannot_authorize_a_handoff(self):
        prepared = self.handoff()
        self.assert_rechecked(replace(prepared, report="连续VAD时间轴核验通过：提议固定偏移 +0.06 秒"))

    def test_changed_offset_cannot_authorize_a_handoff(self):
        prepared = self.handoff()
        self.assert_rechecked(replace(prepared, offset_ms=120))

    def test_external_confirmed_anchor_has_priority(self):
        prepared = self.handoff()
        external = core._shifted_srt(prepared.source_subtitle_path, self.root / "external.srt", 500)
        with self.context():
            offsets, corrected, _ = self.prepare(keep=(3, 6), prepared=prepared, external=external)
        self.assertEqual(self.extracted_ids, [4, 8])
        self.assertEqual(self.audio_checks, [])
        self.assertEqual(offsets[3], 0)  # Existing external-axis 250ms no-op rule.
        self.assertEqual(corrected[3].name, "original-normalized.srt")
        first = legacy.parse_subtitle(external)[0]
        self.assertEqual(self.local_references[-1][0][0], core._subtitle_time_seconds(first.start))
        self.assertFalse(any("复用预检已采用" in line for line in self.logs))

    def test_incomplete_source_does_not_produce_an_optimization_handoff(self):
        self.events = self.events[:20]
        status = {}
        with self.context():
            self.prepare(status=status)
        self.assertTrue(status["verified_anchor"])
        self.assertNotIn("prepared_anchor", status)

    def test_process_passes_shifted_source_without_applying_offset_twice(self):
        prepared = self.handoff()
        with self.context(), patch.object(core, "_prepare_local_chinese_conversions", return_value=[]), \
             patch.object(legacy, "process_video", return_value="output") as process:
            core.process_pro(
                str(self.movie), str(self.root / "out.mkv"), [1, 2], [3, 6], "embedded", 3,
                None, 1, "en", "en", ["zh-CN"], str(self.work), self.logs.append, 1, None,
                shared_content_audio=self.shared, prepared_embedded_anchor=prepared,
            )
        args = process.call_args.args
        self.assertEqual(args[11][3], 60)
        self.assertEqual(args[12], prepared.corrected_subtitle_path)
        self.assertEqual(args[13], 0)
        self.assertEqual(self.audio_checks, [])

    def test_tracks_only_retains_pgs_and_uses_unretained_text_anchor(self):
        prepared = self.handoff()
        with self.context(), patch.object(core, "_prepare_local_chinese_conversions", return_value=[]), \
             patch.object(core, "process_tracks_only", return_value="output") as process:
            core.process_pro(
                str(self.movie), str(self.root / "out.mkv"), [1, 2], [6], "none", 3,
                None, 1, "en", "en", [], str(self.work), self.logs.append, 1, None,
                shared_content_audio=self.shared, prepared_embedded_anchor=prepared,
            )
        self.assertEqual(process.call_args.args[3], [6])
        self.assertEqual(process.call_args.args[8][6], 300)
        self.assertEqual(self.audio_checks, [])
        self.assertEqual(self.extracted_ids, [])


if __name__ == "__main__":
    unittest.main()
