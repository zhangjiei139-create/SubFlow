import copy
from dataclasses import dataclass
from pathlib import Path
import tempfile
import unittest

import numpy as np

import sampled_vad as vad


@dataclass
class SubtitleEvent:
    start: str
    end: str
    text: str


class SampledVadTests(unittest.TestCase):
    def test_centered_grid_and_nested_fallback_ranges(self):
        spans = [vad.centered_range('7219.2125', part) for part in (.5, .6, .75)]
        self.assertEqual(spans[0], vad.FrameRange(180480, 541441))
        self.assertEqual(spans[1], vad.FrameRange(144384, 577537))
        self.assertTrue(spans[1].contains(spans[0]))
        self.assertTrue(spans[2].contains(spans[1]))
        self.assertEqual(vad.movie_frame_count('7219.2125'), 721922)
        self.assertEqual(vad.centered_range('100.01', .5), vad.FrameRange(2500, 7501))
        for value in ('nan', 'inf', -1, 0, True):
            with self.subTest(duration=value), self.assertRaises(vad.ReferenceValidationError):
                vad.centered_range(value, .5)
        with self.assertRaises(vad.ReferenceValidationError):
            vad.centered_range(100, .7)

    def test_50_to_60_owns_only_new_sides_with_two_seconds_context(self):
        base = vad.centered_range(100, .5)
        target = vad.centered_range(100, .6)
        plan = vad.extension_plan(base, target, 100)
        left, right = plan.segments
        self.assertEqual(base, vad.FrameRange(2500, 7500))
        self.assertEqual(target, vad.FrameRange(2000, 8000))
        self.assertEqual(left.owned_range, vad.FrameRange(2000, 2500))
        self.assertEqual(left.decode_range, vad.FrameRange(1800, 2700))
        self.assertEqual(right.owned_range, vad.FrameRange(7500, 8000))
        self.assertEqual(right.decode_range, vad.FrameRange(7300, 8200))
        self.assertEqual(left.trim_start_frames, 200)
        self.assertEqual(right.trim_end_frames, 200)
        self.assertEqual(sum(part.owned_range.frame_count for part in plan.segments), 1000)
        self.assertEqual(vad.extension_plan(target, target, 100).segments, ())

    def test_context_clamps_movie_boundaries_and_invalid_ranges_fail(self):
        plan = vad.extension_plan(vad.FrameRange(500, 9500), vad.FrameRange(0, 10000), 100)
        self.assertEqual(plan.segments[0].decode_range, vad.FrameRange(0, 700))
        self.assertEqual(plan.segments[1].decode_range, vad.FrameRange(9300, 10000))
        with self.assertRaises(vad.ReferenceValidationError):
            vad.extension_plan(vad.FrameRange(100, 200), vad.FrameRange(150, 300), 100)
        with self.assertRaises(vad.ReferenceValidationError):
            vad.extension_plan(vad.FrameRange(100, 200), vad.FrameRange(0, 10001), 100)

    def test_stitch_keeps_base_bit_exact_and_trims_context_not_unknown_gaps(self):
        base, target = vad.FrameRange(300, 700), vad.FrameRange(200, 800)
        plan = vad.extension_plan(base, target, 10, context_seconds=2)
        base_frames = np.linspace(0, 1, base.frame_count, dtype=np.float64)
        middle = {'speech': base_frames, 'tag': np.array('same')}
        sides = {}
        for part in plan.segments:
            speech = np.full(part.decode_range.frame_count, .25, dtype=np.float64)
            begin = part.trim_start_frames
            speech[begin:begin + part.owned_range.frame_count] = .5 if part.name == 'left' else .75
            sides[part.name] = {'speech': speech, 'tag': np.array('same')}
        merged = vad.stitch_reference(middle, plan, sides)
        np.testing.assert_array_equal(merged['speech'][:100], np.full(100, .5))
        np.testing.assert_array_equal(merged['speech'][100:500], base_frames)
        np.testing.assert_array_equal(merged['speech'][500:], np.full(100, .75))
        self.assertEqual(merged['tag'].item(), 'same')
        np.testing.assert_array_equal(middle['speech'], base_frames)
        with self.assertRaises(vad.ReferenceValidationError):
            vad.stitch_reference(middle, plan, {'left': sides['left']})
        bad = copy.deepcopy(sides)
        bad['left']['speech'] = bad['left']['speech'][:-1]
        with self.assertRaises(vad.ReferenceValidationError):
            vad.stitch_reference(middle, plan, bad)
        bad = copy.deepcopy(sides)
        bad['right']['tag'] = np.array('different')
        with self.assertRaises(vad.ReferenceValidationError):
            vad.stitch_reference(middle, plan, bad)
        with self.assertRaises(vad.ReferenceValidationError):
            vad.ExtensionPlan(base, target, (vad.ExtensionSegment('left', vad.FrameRange(200, 299), vad.FrameRange(0, 500)),))

    def test_exact_pcm_count_rejects_even_one_missing_or_extra_sample(self):
        span = vad.FrameRange(100, 200)
        vad.validate_pcm(span, 48000)
        self.assertEqual(vad.expected_vad_frames(48001), 101)
        self.assertEqual(vad.expected_vad_frames(47999), 100)
        for sample_count in (47999, 48001, 0):
            with self.subTest(samples=sample_count), self.assertRaises(vad.ReferenceValidationError):
                vad.validate_pcm(span, sample_count)
        with self.assertRaises(vad.ReferenceValidationError):
            vad.validate_pcm(span, 48000, channels=2)

    def test_npz_roundtrip_preserves_schema_and_metadata_binds_origin_policy_content(self):
        span = vad.FrameRange(1000, 1010)
        arrays = {'speech': np.arange(10, dtype=np.float64) / 10, 'other': np.array([9, 8], dtype=np.int16)}
        identity = {'source': {'size': 123, 'mtime_ns': 456}, 'audio_id': 2,
                    'decoder': 'dts-core', 'vad': 'webrtc-mode3'}
        metadata = vad.make_metadata(span, identity, arrays)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'reference.npz'
            vad.write_npz(path, arrays, span)
            loaded = vad.read_npz(path, span, metadata=metadata, identity=identity)
            self.assertEqual(set(loaded), {'speech', 'other'})
            np.testing.assert_array_equal(loaded['other'], arrays['other'])
            self.assertEqual(loaded['speech'].dtype, np.float64)
            with self.assertRaises(vad.ReferenceValidationError):
                vad.read_npz(path, vad.FrameRange(1001, 1011), metadata=metadata, identity=identity)
            for key, value in (('audio_id', 3), ('decoder', 'full')):
                wrong = dict(identity, **{key: value})
                with self.assertRaises(vad.ReferenceValidationError):
                    vad.read_npz(path, span, metadata=metadata, identity=wrong)
            arrays['speech'][0] = 1
            with self.assertRaises(vad.ReferenceValidationError):
                vad.validate_metadata(metadata, span, identity, arrays)

    def test_reference_rejects_extra_missing_nonfinite_or_object_frames(self):
        span = vad.FrameRange(100, 200)
        for speech in (np.zeros(99), np.zeros(101), np.zeros((100, 1)),
                       np.full(100, np.nan), np.zeros(100, dtype=object)):
            with self.subTest(shape=speech.shape, dtype=speech.dtype), self.assertRaises(vad.ReferenceValidationError):
                vad.validate_arrays({'speech': speech}, span)
        with self.assertRaises(vad.ReferenceValidationError):
            vad.validate_arrays({'other': np.zeros(100)}, span)

    def test_complete_cues_use_absolute_origin_guard_and_keep_text(self):
        span = vad.FrameRange(2000, 8000)
        events = [SubtitleEvent('00:00:29,999', '00:00:32,000', 'guard fail'),
                  SubtitleEvent('00:00:30,000', '00:00:43,137', 'fiancé™\nlong cue'),
                  SubtitleEvent('00:01:05,000', '00:01:10,000', 'right edge'),
                  SubtitleEvent('00:01:09,000', '00:01:10,001', 'incomplete'),
                  SubtitleEvent('00:00:19,000', '00:00:33,000', 'crosses start')]
        selected = vad.crop_subtitles(events, span)
        self.assertIsInstance(selected, tuple)
        self.assertEqual(selected, (SubtitleEvent('00:00:10,000', '00:00:23,137', 'fiancé™\nlong cue'),
                                    SubtitleEvent('00:00:45,000', '00:00:50,000', 'right edge')))
        self.assertEqual(events[1].start, '00:00:30,000')
        self.assertIsInstance(selected[0], SubtitleEvent)

    def test_full_shift_preserves_long_cue_duration_and_unicode_not_local_subset(self):
        events = [SubtitleEvent('00:00:01,000', '00:00:14,137', 'fiancé™\nfull original'),
                  SubtitleEvent('01:02:03,456', '01:02:05,000', 'tail')]
        shifted = vad.shift_full_subtitles(events, -230)
        self.assertEqual(shifted[0], SubtitleEvent('00:00:00,770', '00:00:13,907', events[0].text))
        self.assertEqual(shifted[1], SubtitleEvent('01:02:03,226', '01:02:04,770', 'tail'))
        self.assertEqual(len(shifted), len(events))
        self.assertEqual(vad.offset_milliseconds('-.0005'), -1)
        with self.assertRaises(vad.ReferenceValidationError):
            vad.shift_full_subtitles(events, -1001)

    def test_actual_core_event_type_and_guard_keyword_are_preserved(self):
        import subtitle_tool_core as core
        event = core.SubtitleEvent('00:00:30,000', '00:00:43,137', 'fiancé™')
        selected = vad.crop_subtitles([event], vad.FrameRange(2000, 8000), guard=10)
        self.assertIsInstance(selected[0], core.SubtitleEvent)
        self.assertEqual(selected[0].start, '00:00:10,000')
        self.assertEqual(selected[0].end, '00:00:23,137')
        self.assertEqual(selected[0].text, event.text)
        shifted = vad.shift_full_subtitles([event], -230)
        self.assertIsInstance(shifted[0], core.SubtitleEvent)
        self.assertEqual(shifted[0].end, '00:00:42,907')


if __name__ == '__main__':
    unittest.main()
