# -*- coding: utf-8 -*-
from __future__ import annotations

import concurrent.futures
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import audio_offset_verifier as audio
import pro_core
import subtitle_offset_guard as guard
import subtitle_tool_core as legacy


class SharedEvidenceTest(unittest.TestCase):
    def test_concurrent_exact_window_runs_asr_once_but_nearby_window_is_distinct(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            movie = root / "movie.mkv"
            movie.write_bytes(b"movie")
            model = root / "model.bin"
            model.write_bytes(b"model")
            extracts: list[tuple[int, float, float]] = []
            asr_calls: list[tuple[int, ...]] = []

            def extract(_movie, work, specs, **_kwargs):
                extracts.extend(specs)
                return {index: work / f"{index}.wav" for index, _start, _length in specs}

            def transcribe(wavs, starts, **_kwargs):
                time.sleep(0.05)
                asr_calls.append(tuple(wavs))
                return (
                    {index: [audio.TimedText(start, start + 1, "alpha beta gamma", index)]
                     for index, start in starts.items()},
                    {index: [audio.TimedWord(start, start + 0.3, "alpha", index, 0.9)]
                     for index, start in starts.items()},
                )

            def run(index, start):
                return audio.transcribe_exact_windows(
                    str(movie), [(index, start, 8.0)], audio_stream_index=0,
                    ffmpeg="ffmpeg-test", whisper="whisper-test", whisper_model=str(model),
                    audio_language="en", translate_to_english=False, cancel=None,
                    log=lambda _message: None,
                )

            with mock.patch.object(audio, "persistent_cache_dir", return_value=root / "cache"), \
                 mock.patch.object(audio, "_extract_clips", side_effect=extract), \
                 mock.patch.object(audio, "_transcribe_clips", side_effect=transcribe):
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    a = pool.submit(run, 0, 100.0)
                    b = pool.submit(run, 7, 100.0)
                    self.assertEqual(a.result()[0][0][0].source_index, 0)
                    self.assertEqual(b.result()[0][7][0].source_index, 7)
                run(8, 100.001)
            self.assertEqual(len(asr_calls), 2)
            self.assertEqual(len(extracts), 2)

    def test_concurrent_empty_transcript_is_not_retried_immediately(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            movie = root / "movie.mkv"
            movie.write_bytes(b"movie")
            calls = []
            def extract(_movie, work, specs, **_kwargs):
                return {index: work / f"{index}.wav" for index, _start, _length in specs}
            def transcribe(wavs, _starts, **_kwargs):
                calls.append(1)
                time.sleep(0.05)
                return ({index: [] for index in wavs}, {index: [] for index in wavs})
            def run(index):
                return audio.transcribe_exact_windows(
                    str(movie), [(index, 100.0, 8.0)], audio_stream_index=0,
                    ffmpeg="ffmpeg-test", whisper="whisper-test", whisper_model="model-test",
                    audio_language="en", translate_to_english=False, cancel=None,
                    log=lambda _message: None, task_failures={},
                )
            with mock.patch.object(audio, "persistent_cache_dir", return_value=root / "cache"), \
                 mock.patch.object(audio, "_extract_clips", side_effect=extract), \
                 mock.patch.object(audio, "_transcribe_clips", side_effect=transcribe):
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    first = pool.submit(run, 0)
                    second = pool.submit(run, 9)
                    self.assertEqual(first.result()[2][0], "EMPTY_TRANSCRIPT")
                    self.assertEqual(second.result()[2][9], "EMPTY_TRANSCRIPT")
            self.assertEqual(len(calls), 1)

    def test_failed_candidate_match_does_not_poison_movie_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            subtitle = Path(folder) / "right.srt"
            subtitle.write_text(
                "1\n00:01:40,000 --> 00:01:42,000\nalpha beta gamma delta\n",
                encoding="utf-8",
            )
            wrong = Path(folder) / "wrong.srt"
            wrong.write_text(
                "1\n00:01:40,000 --> 00:01:42,000\ncompletely unrelated words here\n",
                encoding="utf-8",
            )
            words = tuple(audio.TimedWord(102 + i * 0.3, 102.2 + i * 0.3, word, 0, 0.95)
                          for i, word in enumerate(("alpha", "beta", "gamma", "delta")))
            fingerprint = audio.MovieAudioFingerprint((0,), (), (), words)
            self.assertEqual(guard.measured_clip_offsets(wrong, fingerprint, 2.0), {})
            self.assertTrue(guard.measured_clip_offsets(subtitle, fingerprint, 2.0))

    def test_backup_in_same_area_is_not_an_independent_vote(self):
        with tempfile.TemporaryDirectory() as folder:
            subtitle = Path(folder) / "candidate.srt"
            subtitle.write_text(
                "1\n00:01:40,000 --> 00:01:42,000\nalpha beta gamma delta\n\n"
                "2\n00:03:20,000 --> 00:03:22,000\nred blue green yellow\n",
                encoding="utf-8",
            )
            words = []
            for clip, start, text in ((0, 102, ("alpha", "beta", "gamma", "delta")),
                                      (3, 102, ("alpha", "beta", "gamma", "delta")),
                                      (1, 202, ("red", "blue", "green", "yellow"))):
                words.extend(audio.TimedWord(start + i * 0.4, start + i * 0.4 + 0.3,
                                             token, clip, 0.95)
                             for i, token in enumerate(text))
            fingerprint = audio.MovieAudioFingerprint((0, 3, 1), (), (), tuple(words))
            decision = guard.evaluate_proposed_offset(
                subtitle, fingerprint, 2.0, require_word_evidence=True,
                region_by_clip={0: 0, 3: 0, 1: 1},
            )
            self.assertEqual(len(decision.region_offsets), 2)
            self.assertNotEqual(decision.status, "APPLY_OFFSET")

    def test_conflicting_backup_in_same_area_is_not_averaged_away(self):
        with tempfile.TemporaryDirectory() as folder:
            subtitle = Path(folder) / "candidate.srt"
            subtitle.write_text(
                "1\n00:01:40,000 --> 00:01:42,000\nalpha beta gamma delta\n",
                encoding="utf-8",
            )
            words = tuple(
                audio.TimedWord(102 + shift + i * 0.4,
                                102.3 + shift + i * 0.4, token, clip, 0.95)
                for clip, shift in ((0, 0.0), (3, 1.5))
                for i, token in enumerate(("alpha", "beta", "gamma", "delta"))
            )
            fingerprint = audio.MovieAudioFingerprint((0, 3), (), (), words)
            decision = guard.evaluate_proposed_offset(
                subtitle, fingerprint, 2.0, region_by_clip={0: 0, 3: 0},
            )
            self.assertEqual(decision.status, "TIMING_UNRESOLVED")

    def test_only_missing_area_runs_preselected_backup_and_adds_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subtitle = root / "candidate.srt"
            phrases = (
                "alpha beta gamma delta", "red blue green yellow",
                "north south east west",
            )
            blocks = []
            cues = []
            for area, phrase in enumerate(phrases):
                start = 100 + area * 100
                blocks.append(
                    f"{area + 1}\n00:{start // 60:02d}:{start % 60:02d},000 --> "
                    f"00:{(start + 2) // 60:02d}:{(start + 2) % 60:02d},000\n{phrase}"
                )
                cues.append(legacy.SubtitleEvent(
                    f"00:{start // 60:02d}:{start % 60:02d},000",
                    f"00:{(start + 2) // 60:02d}:{(start + 2) % 60:02d},000", phrase,
                ))
            subtitle.write_text("\n\n".join(blocks), encoding="utf-8")
            def words(index, area):
                start = 102 + area * 100
                return tuple(audio.TimedWord(
                    start + i * 0.4, start + i * 0.4 + 0.3, token, index, 0.95,
                ) for i, token in enumerate(phrases[area].split()))
            primary = audio.MovieAudioFingerprint((3, 4, 5), (), (), words(3, 0) + words(4, 1))
            backup = audio.MovieAudioFingerprint((8,), (), (), words(8, 2))
            shared = pro_core.SharedSubtitleContentAudio(
                400.0, 0, 0, "en", audio.MovieAudioFingerprint((), (), (), ()),
            )
            reserve = legacy.SubtitleEvent(
                "00:05:20,000", "00:05:22,000", "north south east west"
            )
            with mock.patch.object(pro_core, "_select_targeted_dialogue_cues",
                                   return_value=tuple(cues + cues[:2] + [reserve])), \
                 mock.patch.object(pro_core, "_extract_targeted_dialogue_fingerprint",
                                   side_effect=[primary, backup]) as extract:
                decision, used = pro_core._targeted_candidate_offset_guard(
                    "movie.mkv", subtitle, cues, work_dir=root, shared_content_audio=shared,
                    provisional_offset=2.0, candidate_label="test", log=lambda _message: None,
                    cancel=None,
                )
            self.assertEqual(extract.call_count, 2)
            self.assertEqual(extract.call_args_list[1].kwargs["clip_indexes"], (8,))
            self.assertEqual(used, 4)
            self.assertEqual(len(decision.region_offsets), 3)
            self.assertEqual(decision.status, "APPLY_OFFSET")

    def test_public_word_evidence_is_kept_and_only_missing_regions_are_transcribed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subtitle = root / "candidate.srt"
            phrases = (
                "alpha beta gamma delta", "red blue green yellow",
                "north south east west",
            )
            cues = []
            for area, phrase in enumerate(phrases):
                start = 100 + area * 100
                cues.append(legacy.SubtitleEvent(
                    f"00:{start // 60:02d}:{start % 60:02d},000",
                    f"00:{(start + 2) // 60:02d}:{(start + 2) % 60:02d},000", phrase,
                ))
            subtitle.write_text("\n\n".join(
                f"{area + 1}\n{cue.start} --> {cue.end}\n{cue.text}"
                for area, cue in enumerate(cues)
            ), encoding="utf-8")
            def words(index, area):
                start = 102 + area * 100
                return tuple(audio.TimedWord(
                    start + i * 0.4, start + i * 0.4 + 0.3, token, index, 0.95,
                ) for i, token in enumerate(phrases[area].split()))
            public = audio.MovieAudioFingerprint((0,), (), (), words(0, 0))
            targeted = audio.MovieAudioFingerprint((4, 5), (), (), words(4, 1) + words(5, 2))
            shared = pro_core.SharedSubtitleContentAudio(400.0, 0, 0, "en", public)
            with mock.patch.object(pro_core, "_select_targeted_dialogue_cues",
                                   return_value=tuple(cues + cues)) as _select, \
                 mock.patch.object(pro_core, "_extract_targeted_dialogue_fingerprint",
                                   return_value=targeted) as extract:
                decision, used = pro_core._targeted_candidate_offset_guard(
                    "movie.mkv", subtitle, cues, work_dir=root,
                    shared_content_audio=shared, provisional_offset=2.0,
                    candidate_label="test", log=lambda _message: None, cancel=None,
                )
            self.assertEqual(extract.call_count, 1)
            self.assertEqual(extract.call_args.args[1], tuple(cues[1:]))
            self.assertEqual(extract.call_args.kwargs["clip_indexes"], (4, 5))
            self.assertEqual(used, 2)
            self.assertEqual(len(decision.region_offsets), 3)
            self.assertEqual(decision.status, "APPLY_OFFSET")

    def test_second_reserve_is_used_only_when_first_reserve_has_no_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subtitle = root / "candidate.srt"
            phrases = (
                "alpha beta gamma delta", "red blue green yellow",
                "north south east west",
            )
            cues = [legacy.SubtitleEvent(
                f"00:{(100 + area * 100) // 60:02d}:{(100 + area * 100) % 60:02d},000",
                f"00:{(102 + area * 100) // 60:02d}:{(102 + area * 100) % 60:02d},000",
                phrase,
            ) for area, phrase in enumerate(phrases)]
            subtitle.write_text("\n\n".join(
                f"{area + 1}\n{cue.start} --> {cue.end}\n{cue.text}"
                for area, cue in enumerate(cues)
            ), encoding="utf-8")
            def words(index, area):
                return tuple(audio.TimedWord(
                    102 + area * 100 + i * 0.4,
                    102.3 + area * 100 + i * 0.4,
                    token, index, 0.95,
                ) for i, token in enumerate(phrases[area].split()))
            primary = audio.MovieAudioFingerprint((3, 4, 5), (), (), words(3, 0) + words(4, 1))
            empty_backup = audio.MovieAudioFingerprint((8,), (), (), ())
            final_backup = audio.MovieAudioFingerprint((11,), (), (), words(11, 2))
            shared = pro_core.SharedSubtitleContentAudio(
                400.0, 0, 0, "en", audio.MovieAudioFingerprint((), (), (), ())
            )
            first = legacy.SubtitleEvent("00:05:20,000", "00:05:22,000", phrases[2])
            second = legacy.SubtitleEvent("00:05:40,000", "00:05:42,000", phrases[2])
            pool = tuple(cues + cues[:2] + [first] + cues[:2] + [second])
            with mock.patch.object(pro_core, "_select_targeted_dialogue_cues", return_value=pool), \
                 mock.patch.object(pro_core, "_extract_targeted_dialogue_fingerprint",
                                   side_effect=[primary, empty_backup, final_backup]) as extract:
                decision, used = pro_core._targeted_candidate_offset_guard(
                    "movie.mkv", subtitle, cues, work_dir=root,
                    shared_content_audio=shared, provisional_offset=2.0,
                    candidate_label="test", log=lambda _message: None, cancel=None,
                )
            self.assertEqual([call.kwargs["clip_indexes"] for call in extract.call_args_list],
                             [(3, 4, 5), (8,), (11,)])
            self.assertEqual(used, 5)
            self.assertEqual(decision.status, "APPLY_OFFSET")

    def test_trusted_outlier_is_not_replaced_by_more_convenient_reserve(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subtitle = root / "candidate.srt"
            phrases = (
                "alpha beta gamma delta", "red blue green yellow",
                "north south east west",
            )
            cues = [legacy.SubtitleEvent(
                f"00:{(100 + area * 100) // 60:02d}:{(100 + area * 100) % 60:02d},000",
                f"00:{(102 + area * 100) // 60:02d}:{(102 + area * 100) % 60:02d},000",
                phrase,
            ) for area, phrase in enumerate(phrases)]
            subtitle.write_text("\n\n".join(
                f"{area + 1}\n{cue.start} --> {cue.end}\n{cue.text}"
                for area, cue in enumerate(cues)
            ), encoding="utf-8")
            words = tuple(audio.TimedWord(
                102 + area * 100 + (6.8 if area == 2 else 2.0) + i * 0.4,
                102.3 + area * 100 + (6.8 if area == 2 else 2.0) + i * 0.4,
                token, 3 + area, 0.95,
            ) for area, phrase in enumerate(phrases)
              for i, token in enumerate(phrase.split()))
            primary = audio.MovieAudioFingerprint((3, 4, 5), (), (), words)
            shared = pro_core.SharedSubtitleContentAudio(
                400.0, 0, 0, "en", audio.MovieAudioFingerprint((), (), (), ())
            )
            with mock.patch.object(pro_core, "_select_targeted_dialogue_cues",
                                   return_value=tuple(cues * 3)), \
                 mock.patch.object(pro_core, "_extract_targeted_dialogue_fingerprint",
                                   return_value=primary) as extract:
                decision, _used = pro_core._targeted_candidate_offset_guard(
                    "movie.mkv", subtitle, cues, work_dir=root,
                    shared_content_audio=shared, provisional_offset=2.0,
                    candidate_label="test", log=lambda _message: None, cancel=None,
                )
            extract.assert_called_once()
            self.assertEqual(decision.status, "TIMING_UNRESOLVED")


class VerdictReuseTest(unittest.TestCase):
    def test_english_manual_core_entry_cannot_use_legacy_acceptance(self):
        shared = pro_core.SharedSubtitleContentAudio(
            400.0, 0, 0, "en", audio.MovieAudioFingerprint((), (), (), ())
        )
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            movie = root / "movie.mkv"
            movie.write_bytes(b"movie")
            subtitle = root / "candidate.srt"
            subtitle.write_text(
                "1\n00:00:10,000 --> 00:00:12,000\nalpha beta gamma delta\n",
                encoding="utf-8",
            )
            with mock.patch.object(pro_core, "prepare_shared_subtitle_content_audio",
                                   return_value=shared) as prepare, \
                 mock.patch.object(pro_core.legacy, "inspect_media", return_value={}), \
                 mock.patch.object(pro_core, "preflight_online_subtitle",
                                   return_value=(Path(folder) / "verified.srt", "checked")) as online, \
                 mock.patch.object(pro_core, "_preflight_external_subtitle_impl") as legacy_path:
                result = pro_core.preflight_external_subtitle(
                    str(movie), str(subtitle), folder, 0,
                    lambda _message: None, source_language="en",
                    manual_reference_first=True,
                )
            self.assertEqual(result[1], "checked")
            prepare.assert_called_once()
            online.assert_called_once()
            legacy_path.assert_not_called()

    def test_manual_and_smart_labels_share_only_same_completed_verdict(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            movie = root / "movie.mkv"
            movie.write_bytes(b"movie")
            subtitle = root / "source.srt"
            subtitle.write_text(
                "\n\n".join(
                    f"{i}\n00:00:{i:02d},000 --> 00:00:{i:02d},900\n"
                    "Alpha beta gamma delta"
                    for i in range(1, 30)
                ), encoding="utf-8",
            )
            shared = pro_core.SharedSubtitleContentAudio(
                30.0, 0, 0, "en", audio.MovieAudioFingerprint((), (), (), ()),
            )
            undecided = audio.AudioOffsetResult(False, None, (), (), "no public match")
            apply = guard.OffsetGuardDecision(
                True, "APPLY_OFFSET", 2.0, 2.0, (2.0, 2.1, 1.9), 2.0, 0.1, "three strong areas"
            )
            def preflight(label):
                return pro_core.preflight_online_subtitle(
                    str(movie), str(subtitle), str(root / label), 0, shared,
                    lambda _message: None, source_language="en", candidate_label=label,
                )
            with mock.patch.object(audio, "persistent_cache_dir", return_value=root / "cache"), \
                 mock.patch.object(pro_core, "_tool", return_value=str(root / "model.bin")), \
                 mock.patch.object(pro_core.legacy, "inspect_media", return_value={}), \
                 mock.patch.object(pro_core, "prepare_shared_subtitle_content_audio",
                                   return_value=shared), \
                 mock.patch.object(pro_core, "detect_subtitle_language", return_value="en"), \
                 mock.patch.object(audio, "match_subtitle_to_fingerprint", return_value=undecided), \
                 mock.patch.object(pro_core, "_targeted_candidate_offset_guard", return_value=(apply, 3)) as targeted:
                first, first_report = preflight("smart")
                second, second_report = pro_core.preflight_external_subtitle(
                    str(movie), str(subtitle), str(root / "manual"), 0,
                    lambda _message: None, source_language="en",
                    manual_reference_first=True,
                )
                self.assertEqual(first.read_bytes(), second.read_bytes())
                self.assertEqual(first_report, second_report)
                self.assertEqual(targeted.call_count, 1)
                again, _ = pro_core.preflight_online_subtitle(
                    str(movie), str(first), str(root / "reentry"), 0, shared,
                    lambda _message: None, source_language="en", candidate_label="manual-again",
                )
                self.assertEqual(first.read_bytes(), again.read_bytes())
                self.assertEqual(targeted.call_count, 1)
                subtitle.write_text(subtitle.read_text(encoding="utf-8") + "\n", encoding="utf-8")
                preflight("changed")
                self.assertEqual(targeted.call_count, 2)


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
