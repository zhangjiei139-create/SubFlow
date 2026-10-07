"""Integration with actual tiny PCM/NPZ files and mocked external processes.

No movie is decoded and no real ffsubsync/Whisper process is launched. Evidence
is injected to test orchestration, not to validate acoustic VAD or peak quality.
"""
import concurrent.futures
from contextlib import ExitStack
import json
from pathlib import Path
import re
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

import numpy as np

import continuous_vad as continuous
import pro_core as product
import sampled_vad as sample
import smart_subtitles
import subtitle_tool_core as core
import vad_evidence


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


class SampledIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.movie = self.root / 'movie.mkv'
        self.movie.write_bytes(b'mocked media identity only')
        self.ffmpeg = self.root / 'ffmpeg.exe'
        self.ffmpeg.write_bytes(b'fake decoder executable identity')
        self.engine = self.root / 'ffsubsync.exe'
        self.engine.write_bytes(b'fake engine executable identity')
        self.duration = 60.0
        self.video_origin = 2.0
        self.tracks = [SimpleNamespace(id=7, type='audio', codec='TrueHD Atmos',
                                       language='en', default=True, name='')]
        self.events = [core.SubtitleEvent('00:00:02,000', '00:00:15,137', 'fiancé™\nlong original cue'),
                       core.SubtitleEvent('00:00:26,000', '00:00:28,000', 'middle first'),
                       core.SubtitleEvent('00:00:31,000', '00:00:32,000', 'middle second'),
                       core.SubtitleEvent('00:00:45,000', '00:00:46,000', 'tail original cue')]
        self.subtitle = self.root / 'original.srt'
        core.write_srt(self.subtitle, self.events, {i: e.text for i, e in enumerate(self.events, 1)})
        self.parsed_events = core.parse_subtitle(self.subtitle)
        self.logs = []
        self.commands = []
        self.command_lock = threading.Lock()
        self.offset = .25
        self.score = 1234.0
        self.pcm_delta = 0
        self.npz_delta = 0
        self.match_error = None
        self.clock = None
        self.minimum_fraction = .5
        self.post_match_insufficient = set()
        self.first_decode_entered = None
        self.release_first_decode = None
        self.stack.enter_context(patch.object(core, 'inspect_tracks', side_effect=lambda _: self.tracks))
        self.stack.enter_context(patch.object(core, 'inspect_media', side_effect=lambda _: {
            'tracks': [{'type': 'video', 'properties': {'minimum_timestamp': int(self.video_origin * 1e9)}}]}))
        self.stack.enter_context(patch.object(core, 'video_track_duration_ns', side_effect=lambda _: int(self.duration * 1e9)))
        self.stack.enter_context(patch.object(core, 'media_duration_ns', side_effect=lambda _: int(self.duration * 1e9)))
        self.stack.enter_context(patch.object(core, 'FFMPEG', str(self.ffmpeg)))
        self.stack.enter_context(patch.object(product, '_tool', return_value=str(self.engine)))
        self.stack.enter_context(patch.object(product, '_run', side_effect=self.fake_run))
        self.stack.enter_context(patch.object(product, 'subtitle_completeness', return_value=SimpleNamespace(accepted=True, report='fixture')))
        self.stack.enter_context(patch.object(vad_evidence, 'evaluate_reference', side_effect=self.fake_evidence))

    def fake_run(self, args, log, cancel=None, **kwargs):
        command = [str(value) for value in args]
        with self.command_lock:
            self.commands.append(command)
            first_decode = sum(cmd[0] == str(self.ffmpeg) for cmd in self.commands) == 1
        if command[0] == str(self.ffmpeg):
            if first_decode and self.first_decode_entered is not None:
                self.first_decode_entered.set()
                if not self.release_first_decode.wait(timeout=2):
                    raise AssertionError('Concurrent test did not release the decoder')
            requested = int(re.search(r'atrim=end_sample=(\d+)', command[command.index('-af') + 1]).group(1))
            with wave.open(command[-1], 'wb') as pcm:
                pcm.setnchannels(1)
                pcm.setsampwidth(2)
                pcm.setframerate(48000)
                pcm.writeframes(bytes((requested + self.pcm_delta) * 2))
            if self.clock is not None:
                self.clock.now += 3
        elif '--serialize-speech' in command:
            wav = Path(command[1])
            with wave.open(str(wav), 'rb') as pcm:
                frames = sample.expected_vad_frames(pcm.getnframes())
            np.savez_compressed(wav.with_suffix('.npz'), speech=np.ones(frames + self.npz_delta, dtype=np.float64))
            if self.clock is not None:
                self.clock.now += 2
        else:
            if self.match_error is not None:
                raise self.match_error
            source = Path(command[command.index('-i') + 1])
            target = Path(command[command.index('-o') + 1])
            # Reproduce the engine's destructive ten-second cap. Product must
            # discard this rewrite and shift the intact original input instead.
            capped = []
            for event in core.parse_subtitle(source):
                start, end = sample._time_ms(event.start), sample._time_ms(event.end)
                capped.append(core.SubtitleEvent(event.start, sample._format_ms(min(end, start + 10000)), event.text))
            core.write_srt(target, capped, {i: e.text for i, e in enumerate(capped, 1)})
            if self.clock is not None:
                self.clock.now += 7
        return subprocess.CompletedProcess(command, 0,
            stdout=f'score: {self.score:.3f}\noffset seconds: {self.offset:.3f}\nframerate scale factor: 1.000\n'.encode(), stderr=b'')

    def fake_evidence(self, reference, events, *, raw_offset=None):
        fraction = int(Path(reference).stem.rsplit('-', 1)[1]) / 100
        sufficient = fraction >= self.minimum_fraction
        if raw_offset is not None and fraction in self.post_match_insufficient:
            sufficient = False
        return {'sufficient': sufficient, 'reasons': [] if sufficient else ['injected evidence shortage'],
                'coordinate_error': False, 'tool_coordinate_invalid': False,
                'expandable': not sufficient}

    def prepare(self, budget=180):
        return continuous.shared_audio(str(self.movie), 7, self.logs.append, budget=budget)

    def preflight(self, shared, *, work='candidate', budget=20):
        return continuous.preflight(str(self.movie), self.subtitle, self.root / work, 7,
                                    self.logs.append, shared=shared, budget=budget)

    def decode_commands(self):
        return [command for command in self.commands if command[0] == str(self.ffmpeg)]

    def match_commands(self):
        return [command for command in self.commands if '-i' in command and command[0] != str(self.ffmpeg)]

    def test_50_success_stops_expansion_and_shifts_full_intact_cues(self):
        shared = self.prepare()
        output, _ = self.preflight(shared)
        self.assertEqual(len(self.decode_commands()), 1)
        self.assertEqual(len(self.match_commands()), 1)
        actual = core.parse_subtitle(output)
        self.assertEqual(actual, list(sample.shift_full_subtitles(self.parsed_events, 250)))
        self.assertEqual(actual[0].end, '00:00:15,387')
        self.assertEqual(actual[0].text, self.parsed_events[0].text)
        marker = json.loads((output.parent / 'vad-verdict.json').read_text(encoding='utf-8'))
        self.assertEqual(marker['matched_fraction'], .5)

    def test_50_shortage_extends_two_sides_once_to_60(self):
        self.minimum_fraction = .6
        shared = self.prepare()
        self.preflight(shared)
        self.assertEqual(len(self.decode_commands()), 3)
        self.assertEqual(len(self.match_commands()), 1)
        self.assertEqual(set(shared.vad_preparation_state['references']), {.5, .6})
        first, left, right = self.decode_commands()
        self.assertEqual(first[first.index('-t') + 1], '30.00')
        self.assertEqual(left[left.index('-t') + 1], '7.00')
        self.assertEqual(right[right.index('-t') + 1], '7.00')
        old = sample.read_npz(shared.vad_reference, sample.centered_range(60, .5))['speech']
        reference = shared.vad_preparation_state['references'][.6]
        expanded = sample.read_npz(reference, sample.centered_range(60, .6))['speech']
        np.testing.assert_array_equal(expanded[300:3300], old)

    def test_finite_negative_or_zero_score_uses_peak_evidence_without_extra_decode(self):
        shared = self.prepare()
        for index, score in enumerate((-14293.0, 0.0)):
            self.score = score
            with self.subTest(score=score):
                output, _ = self.preflight(shared, work=f'valid-low-score-{index}')
                self.assertEqual(core.parse_subtitle(output),
                                 list(sample.shift_full_subtitles(self.parsed_events, 250)))
                marker = json.loads((output.parent / 'vad-verdict.json').read_text(encoding='utf-8'))
                self.assertEqual(marker['matched_fraction'], .5)
                self.assertEqual(marker['raw_score'], score)
        self.assertEqual(len(self.decode_commands()), 1)
        self.assertEqual(len(self.match_commands()), 2)

    def test_negative_score_does_not_override_insufficient_peak_or_boundary(self):
        self.score = -14293.0
        self.post_match_insufficient.add(.5)
        shared = self.prepare()
        output, _ = self.preflight(shared)
        marker = json.loads((output.parent / 'vad-verdict.json').read_text(encoding='utf-8'))
        self.assertEqual(marker['matched_fraction'], .6)
        self.assertEqual(len(self.decode_commands()), 3)
        self.offset = -9.99
        with self.assertRaises(product.SubtitleContentMismatchError):
            self.preflight(shared, work='negative-boundary')
        self.assertEqual(len(self.decode_commands()), 3)

    def test_score_without_sampled_peak_audit_keeps_existing_rejection(self):
        shared = self.prepare()
        self.score = -14293.0
        with patch.object(product, '_sampled_reference_info', return_value=None):
            with self.assertRaises(product.SubtitleVerificationToolError):
                self.preflight(shared)
        self.assertEqual(len(self.decode_commands()), 1)

    def test_acceptance_rule_change_rechecks_verdict_but_reuses_acoustic_cache(self):
        shared = self.prepare()
        self.preflight(shared)
        with patch.object(continuous, 'RULE_VERSION', continuous.RULE_VERSION + '-next'):
            reused = self.prepare()
            self.preflight(reused)
        self.assertEqual(len(self.decode_commands()), 1)
        self.assertEqual(len(self.match_commands()), 2)

    def test_acoustic_rule_change_rebuilds_the_fingerprint(self):
        self.prepare()
        with patch.object(continuous, 'FINGERPRINT_RULE_VERSION', continuous.FINGERPRINT_RULE_VERSION + '-next'):
            self.prepare()
        self.assertEqual(len(self.decode_commands()), 2)

    def test_50_and_60_shortage_extend_to_75_but_not_full(self):
        self.minimum_fraction = .75
        shared = self.prepare()
        output, _ = self.preflight(shared)
        self.assertEqual(len(self.decode_commands()), 5)
        self.assertEqual(len(self.match_commands()), 1)
        self.assertEqual(set(shared.vad_preparation_state['references']), {.5, .6, .75})
        marker = json.loads((output.parent / 'vad-verdict.json').read_text(encoding='utf-8'))
        self.assertEqual(marker['matched_fraction'], .75)

    def test_post_match_insufficient_peak_can_extend_without_reprocessing_middle(self):
        self.post_match_insufficient.add(.5)
        shared = self.prepare()
        self.preflight(shared)
        self.assertEqual(len(self.decode_commands()), 3)
        self.assertEqual(len(self.match_commands()), 2)

    def test_boundary_and_tool_failure_do_not_trigger_expansion(self):
        shared = self.prepare()
        for number, offset in enumerate((-9.99, 9.99)):
            self.offset = offset
            with self.subTest(offset=offset), self.assertRaises(product.SubtitleContentMismatchError):
                self.preflight(shared, work=f'boundary-{number}')
        self.match_error = RuntimeError('injected external tool failure')
        with self.assertRaisesRegex(RuntimeError, 'external tool failure'):
            self.preflight(shared, work='tool-error')
        self.assertEqual(len(self.decode_commands()), 1)
        self.assertEqual(set(shared.vad_preparation_state['references']), {.5})

    def test_stronger_outside_peak_rejects_without_expansion_or_verdict_cache(self):
        shared = self.prepare()

        def range_conflict(reference, events, *, raw_offset=None):
            if raw_offset is None:
                return {'sufficient': True, 'reasons': [], 'expandable': False}
            return {'sufficient': False, 'reasons': ['outside_range_peak'],
                    'outside_range_peak': True, 'diagnostic_offset_seconds': 24.14,
                    'range_limited': True, 'expandable': False}

        with patch.object(vad_evidence, 'evaluate_reference', side_effect=range_conflict):
            with self.assertRaisesRegex(product.SubtitleContentMismatchError, '24.14'):
                self.preflight(shared)
        self.assertEqual(len(self.decode_commands()), 1)
        self.assertEqual(len(self.match_commands()), 1)
        self.assertEqual(set(shared.vad_preparation_state['references']), {.5})
        self.assertFalse((self.root / 'candidate' / 'vad-verdict.json').exists())
        self.assertFalse(any('证据足够' in line for line in self.logs))
        self.assertTrue(any('未采用窗内提议' in line for line in self.logs))

    def test_selected_audio_ordinal_and_input_decoder_options_precede_input(self):
        for index, codec in enumerate(('TrueHD Atmos', 'DTS-HD Master Audio', 'AC-3')):
            self.tracks = [SimpleNamespace(id=1, type='audio', codec='AAC'),
                           SimpleNamespace(id=9, type='audio', codec=codec)]
            self.commands.clear()
            reference = product._build_sampled_alignment_audio(str(self.movie), 9,
                self.root / f'decode-policy-{index}', self.logs.append, None, self.duration)
            command = self.decode_commands()[0]
            self.assertEqual(command[command.index('-map') + 1], '0:a:1')
            self.assertEqual(command[command.index('-ss') + 1], '17.000000')
            self.assertLess(command.index('-ss'), command.index('-i'))
            for option in product.vad_decoder_options(codec)[::2]:
                self.assertLess(command.index(option), command.index('-i'))
            self.assertIsNotNone(product._sampled_reference_info(reference))

    def test_one_missing_pcm_sample_or_npz_frame_is_rejected_not_repaired(self):
        self.pcm_delta = -1
        with self.assertRaises(product.SubtitleVerificationToolError):
            self.prepare()
        self.assertFalse(any('--serialize-speech' in command for command in self.commands))
        self.pcm_delta = 0
        self.npz_delta = -1
        with self.assertRaises(product.SubtitleVerificationToolError):
            self.prepare()

    def test_corrupted_cache_rebuilt_before_reuse(self):
        shared = self.prepare()
        Path(shared.vad_reference).write_bytes(b'broken npz')
        shared2 = self.prepare()
        self.assertEqual(len(self.decode_commands()), 2)
        self.assertIsNotNone(product._sampled_reference_info(shared2.vad_reference))

    def test_missing_or_wrong_sample_origin_must_not_be_treated_as_whole_movie(self):
        shared = self.prepare()
        metadata = Path(shared.vad_reference).with_suffix('.json')
        original = metadata.read_text(encoding='utf-8')
        for index, condition in enumerate(('missing', 'wrong-timeline', 'wrong-rate')):
            if condition == 'missing':
                metadata.unlink()
            else:
                info = json.loads(original)
                info['timeline' if condition == 'wrong-timeline' else 'frame_rate_hz'] = 'wrong'
                metadata.write_text(json.dumps(info), encoding='utf-8')
            with self.subTest(condition=condition), self.assertRaises(product.SubtitleVerificationToolError):
                self.preflight(shared, work=f'bad-origin-{index}')
            metadata.write_text(original, encoding='utf-8')

    def test_cached_verdict_revalidates_missing_wrong_origin_and_wrong_source_sidecars(self):
        shared = self.prepare()
        output, _ = self.preflight(shared)
        metadata = Path(shared.vad_reference).with_suffix('.json')
        original = metadata.read_text(encoding='utf-8')
        verdict = json.loads((output.parent / 'vad-verdict.json').read_text(encoding='utf-8'))
        self.assertIsInstance(verdict.get('matched_reference_sidecar_sha256'), str)
        for condition in ('missing', 'wrong-origin', 'wrong-source'):
            if condition == 'missing':
                metadata.unlink()
            else:
                info = json.loads(original)
                if condition == 'wrong-origin':
                    # Identical length and NPZ hash: shape validation alone
                    # cannot detect this incorrect absolute movie origin.
                    info['range']['start_frame'] += 100
                    info['range']['end_frame'] += 100
                else:
                    info['signature']['source'] = str(self.root / 'another-movie.mkv')
                metadata.write_text(json.dumps(info), encoding='utf-8')
            with self.subTest(condition=condition), self.assertRaises(product.SubtitleVerificationToolError):
                self.preflight(shared)  # Same candidate directory has a cached verdict.
            metadata.write_text(original, encoding='utf-8')
        self.assertEqual(len(self.match_commands()), 1)

    def test_cancel_after_pending_fingerprint_write_publishes_no_cache(self):
        cancel = threading.Event()
        cache = self.root / 'cancelled-build'
        original_write = sample.write_npz

        def write_then_cancel(*args, **kwargs):
            result = original_write(*args, **kwargs)
            cancel.set()
            return result

        with patch.object(sample, 'write_npz', side_effect=write_then_cancel):
            with self.assertRaises(core.CancelledError):
                product._build_sampled_alignment_audio(str(self.movie), 7, cache,
                    self.logs.append, cancel, self.duration)
        self.assertFalse((cache / 'speech-50.npz').exists())
        self.assertFalse((cache / 'speech-50.json').exists())
        self.assertEqual(list(cache.iterdir()), [])

    def test_concurrent_preparation_decodes_and_serializes_only_once(self):
        self.first_decode_entered = threading.Event()
        self.release_first_decode = threading.Event()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(self.prepare)
            self.assertTrue(self.first_decode_entered.wait(timeout=2))
            second = executor.submit(self.prepare)
            self.release_first_decode.set()
            shared1, shared2 = first.result(timeout=3), second.result(timeout=3)
        self.assertEqual(shared1.vad_reference, shared2.vad_reference)
        self.assertEqual(len(self.decode_commands()), 1)
        self.assertEqual(sum('--serialize-speech' in command for command in self.commands), 1)

    def test_concurrent_candidates_share_one_extension_and_keep_separate_outputs(self):
        shared = self.prepare()
        self.minimum_fraction = .6
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(self.preflight, shared, work='first-candidate')
            second = executor.submit(self.preflight, shared, work='second-candidate')
            outputs = [first.result(timeout=3)[0], second.result(timeout=3)[0]]
        self.assertEqual(len(self.decode_commands()), 3)
        self.assertEqual(sum('--serialize-speech' in command for command in self.commands), 3)
        self.assertEqual(len(self.match_commands()), 2)
        self.assertNotEqual(outputs[0], outputs[1])
        for output in outputs:
            self.assertEqual(core.parse_subtitle(output), list(sample.shift_full_subtitles(self.parsed_events, 250)))

    def test_pause_only_candidate_clocks_keeps_global_deadline_and_user_cancel(self):
        clock = FakeClock()
        user_cancel = threading.Event()
        global_clock = product._DeadlineCancel(user_cancel, 100)
        online_clock = smart_subtitles._DeadlineCancel(global_clock, 20)
        online_clock.candidate_match_clock = True
        local_clock = product._DeadlineCancel(online_clock, 15)
        local_clock.candidate_match_clock = True
        with patch.object(continuous.time, 'monotonic', side_effect=clock.monotonic):
            with continuous._pause_candidate_clocks(local_clock):
                self.assertIsNone(local_clock.deadline)
                self.assertIsNone(online_clock.deadline)
                self.assertEqual(global_clock.deadline, 100)
                self.assertFalse(local_clock.is_set())
                user_cancel.set()
                self.assertTrue(local_clock.is_set())
                user_cancel.clear()
                clock.now = 3
            self.assertEqual(local_clock.deadline, 18)
            self.assertEqual(online_clock.deadline, 23)
            self.assertEqual(global_clock.deadline, 100)

    def test_75_evidence_shortage_stops_without_decoding_whole_movie(self):
        self.minimum_fraction = 1
        shared = self.prepare()
        with self.assertRaisesRegex(product.SubtitleContentMismatchError, '75%'):
            self.preflight(shared)
        self.assertEqual(len(self.decode_commands()), 5)
        self.assertEqual(len(self.match_commands()), 0)
        self.assertEqual(set(shared.vad_preparation_state['references']), {.5, .6, .75})

    def test_cumulative_prep_budget_excludes_candidate_matches(self):
        self.clock = FakeClock()
        self.stack.enter_context(patch.object(continuous.time, 'monotonic', side_effect=self.clock.monotonic))
        self.minimum_fraction = .6
        shared = self.prepare(budget=20)
        self.assertEqual(shared.vad_preparation_state['spent'], 5)
        self.preflight(shared)
        self.assertEqual(shared.vad_preparation_state['spent'], 15)
        self.assertEqual(self.clock.now, 22)
        self.assertEqual(len(self.match_commands()), 1)

    def test_cumulative_prep_timeout_stops_extension_without_a_new_budget(self):
        self.clock = FakeClock()
        self.stack.enter_context(patch.object(continuous.time, 'monotonic', side_effect=self.clock.monotonic))
        self.minimum_fraction = .6
        shared = self.prepare(budget=10)
        with self.assertRaisesRegex(product.SubtitlePreflightTimeoutError, '累计10'):
            self.preflight(shared)
        self.assertEqual(shared.vad_preparation_state['spent'], 10)
        self.assertEqual(len(self.decode_commands()), 2)
        self.assertEqual(set(shared.vad_preparation_state['references']), {.5})


if __name__ == '__main__':
    unittest.main()
