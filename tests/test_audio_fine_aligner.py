from __future__ import annotations

import tempfile
import unittest
from unittest import mock

from audio_fine_aligner import (
    FineAnchor,
    FineWord,
    _Cue,
    _match_words,
    _near_clip_boundary,
    _token_words,
    aggregate_deltas,
    run_shadow,
    should_run,
)
from audio_offset_verifier import AudioOffsetResult, OffsetAnchor


def stage32(*, residuals=(0.12, 0.10, 0.08), accepted=True):
    anchors = tuple(
        OffsetAnchor(index, 100.0 + index * 600, 90.0 + index * 600, 10.0, 0.9, 0.2)
        for index in range(len(residuals))
    )
    return AudioOffsetResult(
        accepted,
        10.0 if accepted else None,
        anchors,
        tuple(0.9 for _ in anchors),
        "test",
        residuals=tuple(residuals),
    )


def fine(delta: float, index: int) -> FineAnchor:
    return FineAnchor(index, 100.0 + index, 110.0 + index, "a useful dialogue line", 0.9, 0.9, delta, 0.2)


class AudioFineAlignerTests(unittest.TestCase):
    def test_clean_stage32_skips_fine_alignment(self):
        enabled, reason = should_run(stage32())
        self.assertFalse(enabled)
        self.assertIn("足够稳定", reason)

    def test_uncertain_stage32_triggers_fine_alignment(self):
        enabled, reason = should_run(stage32(residuals=(0.41, 0.18, 0.25)))
        self.assertTrue(enabled)
        self.assertIn("进入轻量精修", reason)

    def test_consistent_micro_offsets_are_suggested_only(self):
        result = aggregate_deltas((fine(0.27, 0), fine(0.31, 1), fine(0.34, 2)))
        self.assertTrue(result.accepted)
        self.assertEqual(result.suggested_micro_offset, 0.31)
        self.assertEqual(result.confidence, "HIGH")
        self.assertLess(result.spread, 0.10)

    def test_spread_rejects_and_returns_zero_micro_offset(self):
        result = aggregate_deltas((fine(-0.30, 0), fine(0.10, 1), fine(0.45, 2)))
        self.assertFalse(result.accepted)
        self.assertEqual(result.suggested_micro_offset, 0.0)

    def test_whisper_subword_tokens_are_reconstructed_as_words(self):
        payload = {
            "transcription": [{
                "tokens": [
                    {"text": " I", "offsets": {"from": 100, "to": 200}, "p": 0.9},
                    {"text": " can", "offsets": {"from": 200, "to": 350}, "p": 0.8},
                    {"text": " find", "offsets": {"from": 350, "to": 500}, "p": 0.9},
                    {"text": " some", "offsets": {"from": 500, "to": 650}, "p": 0.9},
                    {"text": "thing", "offsets": {"from": 650, "to": 800}, "p": 0.8},
                    {"text": ".", "offsets": {"from": 800, "to": 850}, "p": 1.0},
                ]
            }]
        }
        words = _token_words(payload, 20.0)
        self.assertEqual([word.text for word in words], ["i", "can", "find", "something"])
        self.assertEqual(words[-1].start, 20.5)
        self.assertEqual(words[-1].end, 20.8)

    def test_match_diagnostic_explains_missing_word_timestamps(self):
        result = _match_words(_Cue(10.0, 12.0, "I can find something for you"), (), 10.0, 12.0)
        self.assertFalse(result.accepted)
        self.assertFalse(result.has_word_timestamps)
        self.assertIn("ASR 缺词", result.reason)

    def test_shadow_logs_each_candidate_decision_without_changing_result(self):
        model = stage32(residuals=(0.41, 0.27, 0.22))
        cue = _Cue(100.0, 102.0, "I can find something for you")
        selected = tuple((anchor, residual, cue) for anchor, residual in zip(model.anchors, model.residuals))
        words = (
            FineWord(110.0, 110.2, "i", 0.9),
            FineWord(110.2, 110.4, "can", 0.9),
            FineWord(110.4, 110.7, "find", 0.9),
            FineWord(110.7, 111.1, "something", 0.9),
            FineWord(111.1, 111.3, "for", 0.9),
            FineWord(111.3, 112.0, "you", 0.9),
        )
        logs: list[str] = []
        with tempfile.TemporaryDirectory() as temporary, mock.patch(
            "audio_fine_aligner._cue_candidates", return_value=selected,
        ), mock.patch(
            "audio_fine_aligner._extract_clips",
            return_value=(
                {0: None, 1: None, 2: None},
                {0: 107.25, 1: 107.25, 2: 107.25},
                {0: 114.75, 1: 114.75, 2: 114.75},
            ),
        ), mock.patch(
            "audio_fine_aligner._transcribe", return_value={0: words, 1: words, 2: words},
        ):
            result = run_shadow(
                "movie.mkv", "subtitle.srt", temporary, model,
                duration_seconds=3600.0, audio_stream_index=0,
                ffmpeg="ffmpeg.exe", whisper="whisper.exe", whisper_model="model.bin",
                log=logs.append,
            )
        joined = "\n".join(logs)
        self.assertTrue(result.accepted)
        self.assertIn("Fine anchor candidate 1", joined)
        self.assertIn("Stage 3.2 probe 0", joined)
        self.assertIn("字幕文本", joined)
        self.assertIn("短音频范围", joined)
        self.assertIn("Whisper 识别文本", joined)
        self.assertIn("word timestamps：有", joined)
        self.assertIn("匹配词范围", joined)
        self.assertIn("匹配窗口：去重前", joined)
        self.assertIn("边界重采样：否", joined)
        self.assertIn("结论：接受", joined)

    def test_overlapping_windows_are_one_canonical_match(self):
        words = (
            FineWord(20.0, 20.2, "dramatic", 0.9),
            FineWord(20.2, 20.4, "music", 0.9),
            FineWord(20.4, 20.6, "i", 0.9),
            FineWord(20.6, 20.8, "hope", 0.9),
            FineWord(20.8, 21.0, "i", 0.9),
            FineWord(21.0, 21.2, "get", 0.9),
            FineWord(21.2, 21.4, "some", 0.9),
            FineWord(21.4, 21.6, "serious", 0.9),
            FineWord(21.6, 21.8, "burns", 0.9),
        )
        result = _match_words(_Cue(10.0, 12.0, "i hope i get some serious burns"), words, 20.4, 21.8)
        self.assertTrue(result.accepted)
        self.assertGreater(result.raw_match_count, result.canonical_match_count)
        self.assertEqual(result.matched_text, "i hope i get some serious burns")

    def test_repeated_phrase_at_distinct_times_remains_ambiguous(self):
        phrase = ("i", "can", "find", "something", "for", "you")
        words = []
        for index, word in enumerate((*phrase, "later", "dialogue", *phrase)):
            start = 30.0 + index * 0.25
            words.append(FineWord(start, start + 0.2, word, 0.9))
        result = _match_words(
            _Cue(10.0, 12.0, "i can find something for you"), tuple(words), 30.0, 31.5,
        )
        self.assertFalse(result.accepted)
        self.assertGreaterEqual(result.canonical_match_count, 2)
        self.assertIn("多义匹配", result.reason)

    def test_match_near_clip_edge_requires_resampling(self):
        words = (
            FineWord(40.1, 40.3, "i", 0.9),
            FineWord(40.3, 40.5, "can", 0.9),
            FineWord(40.5, 40.7, "find", 0.9),
            FineWord(40.7, 40.9, "something", 0.9),
            FineWord(40.9, 41.1, "for", 0.9),
            FineWord(41.1, 41.3, "you", 0.9),
        )
        result = _match_words(_Cue(10.0, 12.0, "i can find something for you"), words, 40.0, 42.0)
        self.assertTrue(result.accepted)
        self.assertTrue(_near_clip_boundary(result, 40.0, 46.0))
    def test_shadow_resamples_an_accepted_edge_match_once(self):
        model = stage32(residuals=(0.41, 0.27, 0.22))
        cue = _Cue(100.0, 102.0, "I can find something for you")
        selected = tuple((anchor, residual, cue) for anchor, residual in zip(model.anchors, model.residuals))
        words = (
            FineWord(110.1, 110.3, "i", 0.9),
            FineWord(110.3, 110.5, "can", 0.9),
            FineWord(110.5, 110.7, "find", 0.9),
            FineWord(110.7, 110.9, "something", 0.9),
            FineWord(110.9, 111.1, "for", 0.9),
            FineWord(111.1, 111.3, "you", 0.9),
        )
        initial = (
            {0: None, 1: None, 2: None},
            {0: 110.0, 1: 110.0, 2: 110.0},
            {0: 116.0, 1: 116.0, 2: 116.0},
        )
        expanded = (
            {0: None, 1: None, 2: None},
            {0: 108.25, 1: 108.25, 2: 108.25},
            {0: 119.75, 1: 119.75, 2: 119.75},
        )
        logs: list[str] = []
        with tempfile.TemporaryDirectory() as temporary, mock.patch(
            "audio_fine_aligner._cue_candidates", return_value=selected,
        ), mock.patch(
            "audio_fine_aligner._extract_clips", side_effect=(initial, expanded),
        ) as extract_mock, mock.patch(
            "audio_fine_aligner._transcribe",
            side_effect=({0: words, 1: words, 2: words}, {0: words, 1: words, 2: words}),
        ):
            result = run_shadow(
                "movie.mkv", "subtitle.srt", temporary, model,
                duration_seconds=3600.0, audio_stream_index=0,
                ffmpeg="ffmpeg.exe", whisper="whisper.exe", whisper_model="model.bin",
                log=logs.append,
            )
        self.assertTrue(result.accepted)
        self.assertEqual(extract_mock.call_count, 2)
        joined = "\n".join(logs)
        self.assertIn("需要边界重采样 3", joined)
        self.assertIn("边界重采样：是", joined)
if __name__ == "__main__":
    unittest.main()
