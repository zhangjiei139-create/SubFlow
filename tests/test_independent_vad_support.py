"""Real FFT/evidence and routing over synthetic NPZ; no decoder or external process."""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

import continuous_vad as continuous
import pro_core as product
import sampled_vad as sample
import subtitle_tool_core as core
import vad_evidence as evidence


class IndependentSupportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.movie = self.root / 'identity-only.mkv'
        self.movie.write_bytes(b'synthetic timing fixture; not media')
        self.speech = np.zeros(120000)
        self.events = []
        rng = np.random.default_rng(431)
        # Only the middle region matches. Other regions contain aperiodic
        # unrelated speech, so a strong global peak is not a valid fixed shift.
        for region in range(10):
            for start in region * 120 + 15 + np.sort(rng.uniform(0, 85, 15)):
                self.add_event(start, start + rng.uniform(.4, 1.2))
                spoken = region * 120 + rng.uniform(12, 107)
                self.speech[round(spoken * 100):round((spoken + .9) * 100)] = 1
        for start in 565 + np.sort(rng.uniform(0, 70, 55)):
            end = start + rng.uniform(.4, 1.2)
            self.add_event(start, end)
            self.speech[round(start * 100):round(end * 100)] = 1
        self.events.sort(key=lambda event: event.start)
        self.subtitle = self.root / 'source.srt'
        core.write_srt(self.subtitle, self.events, {i: e.text for i, e in enumerate(self.events, 1)})
        self.references = {}
        stat = self.movie.stat()
        for fraction in (.5, .6, .75):
            span = sample.centered_range(1200, fraction)
            path = self.root / f'speech-{round(fraction * 100)}.npz'
            np.savez_compressed(path, speech=self.speech[span.start_frame:span.end_frame])
            info = dict(signature=dict(source=str(self.movie.resolve()), size=stat.st_size,
                                       mtime_ns=stat.st_mtime_ns, audio_id=1, duration_seconds=1200),
                        timeline='video-relative', frame_rate_hz=100,
                        range=dict(start_frame=span.start_frame, end_frame=span.end_frame, fraction=fraction),
                        fingerprint_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            path.with_suffix('.json').write_text(json.dumps(info), encoding='utf-8')
            self.references[fraction] = path
        self.shared = SimpleNamespace(duration_seconds=1200, validation_audio_id=1,
                                      audio_language='de', vad_reference=self.references[.5],
                                      vad_preparation_state={})

    def add_event(self, start, end):
        self.events.append(core.SubtitleEvent(core.srt_time_from_milliseconds(round(start * 1000)),
                                             core.srt_time_from_milliseconds(round(end * 1000)),
                                             'This is a complete spoken dialogue sentence.'))

    def test_strong_unique_global_peak_with_only_one_region_is_insufficient(self):
        for fraction, reference in self.references.items():
            with self.subTest(fraction=fraction):
                span = sample.centered_range(1200, fraction)
                local = sample.crop_subtitles(self.events, span,
                                              guard=evidence.subtitle_guard_seconds(span.duration_seconds))
                result = evidence.evaluate_reference(reference, local, 0.)
                self.assertEqual(result['computed_offset_seconds'], 0.)
                self.assertLess(result['alternative_peak_ratio'], evidence.ALTERNATIVE_PEAK_RATIO)
                self.assertLess(result['peak_width_seconds'], evidence.MAX_PEAK_WIDTH_SECONDS)
                self.assertEqual(result['supporting_region_count'], 1, result)
                self.assertEqual(result['reasons'], ['insufficient_independent_regional_support'])
                self.assertFalse(result['sufficient'])
                self.assertTrue(result['expandable'])

    @staticmethod
    def match(_movie, local, output, _audio, _mode, _log, _cancel, **kwargs):
        kwargs['diagnostics'].update(raw_offset_seconds=0., offset_seconds=0., score=12345.)
        return product._shifted_srt(local, output, 0)

    def route(self):
        return continuous.preflight(str(self.movie), self.subtitle, self.root / 'work', 1,
                                    lambda _: None, shared=self.shared, source_language='en')

    def test_real_gate_extends_50_60_75_and_never_publishes_success(self):
        with patch.object(product, '_alignment_candidate', side_effect=self.match) as match, \
             patch.object(continuous, '_extend_reference',
                          side_effect=lambda _m, _s, _r, fraction, *_a: self.references[fraction]) as extend:
            with self.assertRaisesRegex(product.SubtitleContentMismatchError, '75%.*手动.*独立取样区域'):
                self.route()
        self.assertEqual(match.call_count, 3)
        self.assertEqual([call.args[3] for call in extend.call_args_list], [.6, .75])
        self.assertFalse((self.root / 'work' / 'vad-verdict.json').exists())
        self.assertFalse((self.root / 'work' / 'online-verified.srt').exists())

    def test_old_bad_verdict_rechecked_without_rebuilding_fingerprints(self):
        old_rule = 'continuous-vad-v7-regional-offset-conflict-20261008'
        self.assertNotEqual(continuous.RULE_VERSION, old_rule)
        original = {fraction: path.read_bytes() for fraction, path in self.references.items()}
        with patch.object(product, '_alignment_candidate', side_effect=self.match) as match, \
             patch.object(continuous, '_extend_reference',
                          side_effect=lambda _m, _s, _r, fraction, *_a: self.references[fraction]), \
             patch.object(product, '_build_continuous_alignment_audio') as decode:
            # Reproduce v7's acceptance by disabling only the new gate.
            with patch.object(continuous, 'RULE_VERSION', old_rule), \
                 patch.object(evidence, 'MIN_SUPPORTING_REGIONS', 0):
                self.route()
            with self.assertRaisesRegex(product.SubtitleContentMismatchError, '75%.*手动'):
                self.route()
        self.assertEqual(match.call_count, 4)
        decode.assert_not_called()
        self.assertEqual(original, {fraction: path.read_bytes() for fraction, path in self.references.items()})
        saved = json.loads((self.root / 'work' / 'vad-verdict.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['signature']['rule'], old_rule)  # no fresh successful verdict


if __name__ == '__main__':
    unittest.main()
