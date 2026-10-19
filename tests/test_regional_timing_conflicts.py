"""Known regional timing contradictions; synthetic intervals, no media/network IO."""
from contextlib import ExitStack
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

import continuous_vad as continuous
import pgs_local_alignment as pgs
import pro_core as product
import sampled_vad as sampled
import subtitle_tool_core as core
import vad_evidence as vad


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str = "This is a complete dialogue sentence."


def write_events(path, intervals):
    events = [core.SubtitleEvent(core.srt_time_from_milliseconds(round(a * 1000)),
                                core.srt_time_from_milliseconds(round(b * 1000)),
                                f"This is complete dialogue number {index}.")
              for index, (a, b) in enumerate(sorted(intervals), 1)]
    core.write_srt(path, events, {index: event.text for index, event in enumerate(events, 1)})
    return events


class VadRegionalConflictTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        rng = np.random.default_rng(0)
        starts = np.sort(rng.uniform(70, 530, 100))
        self.audio = [(float(a), float(a + rng.uniform(.6, 1.6))) for a in starts]
        self.speech = np.zeros(60000)
        for a, b in self.audio:
            self.speech[round(a * 100):round(b * 100)] = 1
        self.reference = self.root / 'speech-50.npz'
        np.savez_compressed(self.reference, speech=self.speech)

    def cues(self, *, conflict=False, offset=3.):
        return [Cue(a - (offset if not conflict or index < 67 else -offset),
                    b - (offset if not conflict or index < 67 else -offset))
                for index, (a, b) in enumerate(self.audio)]

    def test_majority_peak_cannot_erase_supported_opposite_region(self):
        cues = self.cues(conflict=True)
        result = vad.evaluate_reference(self.reference, cues, raw_offset=3.)
        self.assertFalse(result['sufficient'], result)
        self.assertTrue(result['regional_offset_conflict'], result)
        self.assertFalse(result['expandable'])
        supported = [region['offset_seconds'] for region in result['regional_offsets']]
        self.assertIn(3., supported)
        self.assertIn(-3., supported)

    def test_consistent_positive_negative_and_zero_offsets_still_pass(self):
        for offset in (-7.3, 0., 3., 7.3):
            with self.subTest(offset=offset):
                result = vad.evaluate_reference(self.reference, self.cues(offset=offset), offset)
                self.assertTrue(result['sufficient'], result)
                self.assertFalse(result['regional_offset_conflict'])

    def test_sparse_regions_are_unknown_and_keep_expansion_available(self):
        cues = self.cues()[:12]
        result = vad.evaluate_reference(self.reference, cues)
        self.assertFalse(result['sufficient'])
        self.assertTrue(result['expandable'])
        self.assertFalse(result['regional_offset_conflict'])
        self.assertEqual(vad._regional_offset_evidence(self.speech, cues[:3]), [])

    def preflight_fixture(self):
        movie = self.root / 'identity-only.mkv'
        movie.write_bytes(b'Not media: identity for an offline timing test')
        span = sampled.centered_range(1200, .5)
        stat = movie.stat()
        info = dict(signature=dict(source=str(movie.resolve()), size=stat.st_size,
                    mtime_ns=stat.st_mtime_ns, audio_id=1, duration_seconds=1200),
                    timeline='video-relative', frame_rate_hz=100,
                    range=dict(start_frame=span.start_frame, end_frame=span.end_frame, fraction=.5),
                    fingerprint_sha256=hashlib.sha256(self.reference.read_bytes()).hexdigest())
        self.reference.with_suffix('.json').write_text(json.dumps(info), encoding='utf-8')
        subtitle = self.root / 'candidate.srt'
        write_events(subtitle, [(50., 51.), (150., 151.), (950., 951.), (1100., 1101.)]
                     + [(cue.start + 300, cue.end + 300) for cue in self.cues(conflict=True)])
        shared = SimpleNamespace(duration_seconds=1200, validation_audio_id=1,
                                 audio_language='en', vad_reference=self.reference,
                                 vad_preparation_state={})
        return movie, subtitle, shared

    @staticmethod
    def match(_movie, local, destination, _audio_id, _mode, _log, _cancel, **kwargs):
        kwargs['diagnostics'].update(raw_offset_seconds=3., offset_seconds=3., score=43502.)
        return product._shifted_srt(local, destination, 3000)

    def test_actual_preflight_rejects_without_expansion_or_verdict(self):
        movie, subtitle, shared = self.preflight_fixture()
        with patch.object(product, '_alignment_candidate', side_effect=self.match), \
             patch.object(continuous, '_extend_reference') as extend:
            with self.assertRaisesRegex(product.SubtitleContentMismatchError, '可信偏移冲突'):
                continuous.preflight(str(movie), subtitle, self.root / 'work', 1,
                                     lambda _: None, shared=shared)
        extend.assert_not_called()
        self.assertFalse((self.root / 'work' / 'vad-verdict.json').exists())
        self.assertFalse((self.root / 'work' / 'online-verified.srt').exists())

    def test_previous_bad_verdict_is_rechecked_without_acoustic_rebuild(self):
        movie, subtitle, shared = self.preflight_fixture()
        fingerprint = self.reference.read_bytes()
        old_rule = 'continuous-vad-v6-outside-range-peak-20261003'
        self.assertNotEqual(continuous.RULE_VERSION, old_rule)
        with patch.object(product, '_alignment_candidate', side_effect=self.match) as match, \
             patch.object(product, '_build_continuous_alignment_audio') as decode:
            with patch.object(continuous, 'RULE_VERSION', old_rule), \
                 patch.object(vad, '_regional_offset_evidence', return_value=[]):
                continuous.preflight(str(movie), subtitle, self.root / 'work', 1,
                                     lambda _: None, shared=shared)
            with self.assertRaisesRegex(product.SubtitleContentMismatchError, '可信偏移冲突'):
                continuous.preflight(str(movie), subtitle, self.root / 'work', 1,
                                     lambda _: None, shared=shared)
        self.assertEqual(match.call_count, 2)
        decode.assert_not_called()
        self.assertEqual(self.reference.read_bytes(), fingerprint)


class PgsRegionalConflictTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.movie = self.root / 'identity-only.mkv'
        self.movie.write_bytes(b'Not media: identity for an offline timing test')
        self.windows = [(100., 220.), (700., 820.), (1300., 1420.)]
        rng = np.random.default_rng(731)
        self.reference_regions = []
        for low, _ in self.windows:
            starts = low + 14 + np.arange(30) * 3 + rng.uniform(0, .7, 30)
            self.reference_regions.append([(float(a), float(a + rng.uniform(.5, 1.5))) for a in starts])
        self.reference_intervals = sum(self.reference_regions, [])
        self.reference = self.root / 'aligned.srt'
        write_events(self.reference, self.reference_intervals)
        self.tracks = [core.Track(1, 'audio', 'AAC', 'en', '', True, False, False, False, False),
                       core.Track(2, 'subtitles', 'HDMV PGS', 'en', '', False, False, False, True, False)]
        self.work = self.root / 'work'
        self.cache = self.work / 'embedded-subtitle-correction' / 'image-timing-cache'
        self.logs = []

    def regions(self, conflict=True):
        return [[(a - (3 if not conflict or index < 2 else -3),
                  b - (3 if not conflict or index < 2 else -3)) for a, b in region]
                for index, region in enumerate(self.reference_regions)]

    def context(self):
        stack = ExitStack()
        stack.enter_context(patch.object(core, 'inspect_tracks', return_value=self.tracks))
        stack.enter_context(patch.object(core, 'prepare_work_input', return_value=str(self.movie)))
        stack.enter_context(patch.object(core, 'inspect_media', return_value={}))
        stack.enter_context(patch.object(core, 'video_track_duration_ns', return_value=1800 * 10**9))
        stack.enter_context(patch.object(core, 'run_command', side_effect=AssertionError('Unexpected media IO')))
        return stack

    def prepare(self):
        status = {}
        offsets, _, _ = product.prepare_embedded_text_corrections(
            str(self.movie), [2], None, 1, str(self.work), self.logs.append,
            verified_external_subtitle=str(self.reference), verification_status=status)
        return offsets, status['image_results'][2]

    def cache_full_intervals(self, intervals):
        self.cache.mkdir(parents=True, exist_ok=True)
        product._write_image_cache(self.cache / 'all-image-subtitle-intervals.json', {
            'signature': product._image_cache_signature(str(self.movie), 'all-image-subtitle-intervals-v3'),
            'metadata': [{'index': 1, 'codec_name': 'hdmv_pgs_subtitle'}],
            'streams': {'0': intervals}})

    def test_local_conflict_survives_and_never_starts_full_scan(self):
        intervals = sum(self.regions(), [])
        packets = [dict(stream_index=1, pts_time=str(a), duration_time=str(b-a), size='1000')
                   for a, b in intervals]
        with self.context(), patch.object(product, '_pgs_local_packets', return_value=(
                [{'index': 1, 'codec_name': 'hdmv_pgs_subtitle'}], packets)) as read, \
                patch.object(product, '_embedded_image_intervals') as full:
            offsets, result = self.prepare()
        self.assertFalse(result['accepted'], result)
        self.assertTrue(result['local_evidence']['timing_conflict'])
        self.assertEqual(offsets, {2: 0})
        self.assertEqual(read.call_count, 1)
        full.assert_not_called()

    def test_existing_full_cache_cannot_skip_regional_conflict_check(self):
        intervals = sum(self.regions(), [])
        self.assertGreater(product._interval_alignment(self.reference_intervals, intervals)[0], .85)
        self.cache_full_intervals(intervals)
        with self.context(), patch.object(product, '_pgs_local_packets') as local:
            offsets, result = self.prepare()
            with self.assertRaisesRegex(product.SubtitleContentMismatchError, '可信区域偏移冲突'):
                product.require_image_timeline_anchor(str(self.movie), [2], 1, self.work,
                                                      self.reference, self.logs.append)
        self.assertFalse(result['accepted'], result)
        self.assertTrue(result['full_region_evidence']['conflict'])
        self.assertEqual(offsets, {2: 0})
        local.assert_not_called()

    def test_conflicting_track_does_not_discard_another_tracks_supported_result(self):
        metadata = [{'index': index, 'codec_name': 'hdmv_pgs_subtitle'} for index in (1, 2)]
        packets = [dict(stream_index=index, pts_time=str(a), duration_time=str(b-a), size='1000')
                   for index, regions in ((1, self.regions()), (2, self.regions(conflict=False)))
                   for a, b in sum(regions, [])]
        with patch.object(product, '_pgs_local_packets', return_value=(metadata, packets)) as read:
            results = product._pgs_local_corrections(str(self.movie), [0, 1],
                self.reference_intervals, 1800., self.logs.append, None, self.cache)
        self.assertFalse(results[0].accepted)
        self.assertTrue(results[0].timing_conflict)
        self.assertTrue(results[1].accepted)
        self.assertAlmostEqual(results[1].offset_seconds, 3.)
        self.assertEqual(read.call_count, 1)

    def test_consistent_full_fallback_still_applies_supported_shift(self):
        self.cache_full_intervals(sum(self.regions(conflict=False), []))
        with self.context():
            offsets, result = self.prepare()
        self.assertTrue(result['accepted'], result)
        self.assertFalse(result['full_region_evidence']['conflict'])
        self.assertEqual(offsets, {2: 3000})

    def test_sparse_other_regions_do_not_veto_supported_full_fallback(self):
        self.cache_full_intervals(self.regions(conflict=False)[0][:25])
        with self.context():
            offsets, result = self.prepare()
        self.assertTrue(result['accepted'], result)
        self.assertFalse(result['full_region_evidence']['conflict'])
        self.assertEqual(offsets, {2: 3000})

    def test_two_trusted_regions_can_conflict_even_when_third_is_empty(self):
        regions = self.regions()
        result = pgs.evaluate_region_consistency(self.reference_intervals, self.windows,
                                                 [regions[0], [], regions[2]])
        self.assertTrue(result.conflict, result)
        self.assertEqual(result.offsets, (3., None, -3.))


if __name__ == '__main__':
    unittest.main()
