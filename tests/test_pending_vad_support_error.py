"""Structured manual-pending rejection; no decoder, film, search or acceptance."""
import hashlib
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import continuous_vad as continuous
import pro_core as product
import subtitle_tool_core as core
import vad_evidence as evidence
from tests import test_independent_vad_support as existing


REASON = 'insufficient_independent_regional_support'


class PendingVadSupportErrorTest(unittest.TestCase):
    def setUp(self):
        self.fixture = existing.IndependentSupportTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.shared.vad_preparation_state = dict(
            limit=180., spent=12.5, references=dict(self.fixture.references))

    def route(self, cancel=None):
        f = self.fixture
        return continuous.preflight(str(f.movie), f.subtitle, f.root / 'work', 1,
                                    lambda _: None, cancel=cancel, shared=f.shared,
                                    source_language='en')

    def extension(self, _movie, _shared, _reference, fraction, *_args):
        return self.fixture.references[fraction]

    @staticmethod
    def mock_evidence(reason_list):
        def inspect(_reference, _events, raw_offset=None):
            if raw_offset is None:
                return dict(sufficient=True, reasons=[], fft_evaluated=False)
            return dict(sufficient=False, reasons=list(reason_list), fft_evaluated=True,
                        expandable=True, range_limited=False, outside_range_peak=False,
                        tool_coordinate_invalid=False, coordinate_error=False,
                        regional_offset_conflict=False)
        return inspect

    def test_real_fft_failure_retains_context_without_publishing_success(self):
        f = self.fixture
        original_subtitle = f.subtitle.read_bytes()
        originals = {fraction: path.read_bytes() for fraction, path in f.references.items()}
        with patch.object(product, '_alignment_candidate', side_effect=f.match) as match, \
             patch.object(continuous, '_extend_reference', side_effect=self.extension) as extend:
            with self.assertRaises(product.SubtitleInsufficientRegionalSupportError) as raised:
                self.route()
        error = raised.exception
        self.assertIsInstance(error, product.SubtitleContentMismatchError)
        self.assertEqual(error.reason_code, REASON)
        self.assertEqual(error.evidence['reasons'], [REASON])
        self.assertEqual(error.evidence['supporting_region_count'], 1)
        self.assertEqual(error.reference, f.references[.75])
        self.assertEqual(error.preparation_state['limit'], 180.)
        self.assertEqual(error.preparation_state['spent'], 12.5)
        self.assertEqual(match.call_count, 3)
        self.assertEqual([call.args[3] for call in extend.call_args_list], [.6, .75])
        self.assertEqual(original_subtitle, f.subtitle.read_bytes())
        self.assertEqual(originals, {fraction: path.read_bytes() for fraction, path in f.references.items()})
        self.assertFalse((f.root / 'work' / 'vad-verdict.json').exists())
        self.assertFalse((f.root / 'work' / 'online-verified.srt').exists())

    def test_error_evidence_and_budget_are_independent_snapshots(self):
        source_evidence = dict(reasons=[REASON], nested={'values': [1]})
        state = dict(limit=180., spent=12., references={.5: Path('speech-50.npz')})
        error = product.SubtitleInsufficientRegionalSupportError(source_evidence, 'speech-75.npz', state)
        source_evidence['reasons'].append('broad_peak')
        source_evidence['nested']['values'].append(2)
        state['spent'] = 170.
        state['references'][.5] = Path('different.npz')
        self.assertEqual(error.evidence['reasons'], [REASON])
        self.assertEqual(error.evidence['nested']['values'], [1])
        self.assertEqual(error.preparation_state['spent'], 12.)
        self.assertEqual(error.preparation_state['references'][.5], Path('speech-50.npz'))

    def test_class_rejects_nonexclusive_reasons_and_contradictory_flags(self):
        for reasons in ([], [REASON, 'broad_peak'], ['too_little_speech'], [REASON, REASON]):
            with self.subTest(reasons=reasons), self.assertRaises(ValueError):
                product.SubtitleInsufficientRegionalSupportError(dict(reasons=reasons), 'speech.npz', {})
        for flag in ('range_limited', 'outside_range_peak', 'coordinate_error',
                     'tool_coordinate_invalid', 'regional_offset_conflict'):
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                product.SubtitleInsufficientRegionalSupportError(dict(reasons=[REASON], **{flag: True}), 'speech.npz', {})

    def test_other_evidence_shortages_do_not_become_pending(self):
        for reason in ('broad_peak', 'alternative_peak', 'too_few_dialogue_cues',
                       'dialogue_concentrated', 'too_little_speech', 'too_little_quiet'):
            with self.subTest(reason=reason), \
                 patch.object(product, '_alignment_candidate', side_effect=self.fixture.match), \
                 patch.object(continuous, '_extend_reference', side_effect=self.extension), \
                 patch.object(evidence, 'evaluate_reference', side_effect=self.mock_evidence([REASON, reason])):
                with self.assertRaises(product.SubtitleContentMismatchError) as raised:
                    self.route()
                self.assertNotIsInstance(raised.exception, product.SubtitleInsufficientRegionalSupportError)

    def test_range_edge_remains_content_rejection(self):
        def edge_match(_movie, local, output, *_args, **kwargs):
            kwargs['diagnostics'].update(raw_offset_seconds=9.9, offset_seconds=9.9, score=1.)
            return product._shifted_srt(local, output, 9900)
        with patch.object(product, '_alignment_candidate', side_effect=edge_match):
            with self.assertRaises(product.SubtitleContentMismatchError) as raised:
                self.route()
        self.assertNotIsInstance(raised.exception, product.SubtitleInsufficientRegionalSupportError)

    def test_invalid_reference_identity_stops_before_matching(self):
        path = self.fixture.references[.5].with_suffix('.json')
        stored = json.loads(path.read_text('utf-8'))
        stored['signature']['audio_id'] = 999
        path.write_text(json.dumps(stored), encoding='utf-8')
        with patch.object(product, '_alignment_candidate') as match:
            with self.assertRaises(product.SubtitleVerificationToolError):
                self.route()
            match.assert_not_called()

    def test_completeness_and_language_checks_are_prior_to_pending_classification(self):
        with patch.object(product, 'subtitle_completeness', return_value=SimpleNamespace(accepted=False, report='not complete')), \
             patch.object(product, '_alignment_candidate') as match:
            with self.assertRaises(product.SubtitleContentMismatchError) as raised:
                self.route()
            self.assertNotIsInstance(raised.exception, product.SubtitleInsufficientRegionalSupportError)
            match.assert_not_called()
        with patch.object(product, 'detect_subtitle_language', return_value='zh'), \
             patch.object(product, '_alignment_candidate') as match:
            with self.assertRaises(product.SubtitleContentMismatchError) as raised:
                self.route()
            self.assertNotIsInstance(raised.exception, product.SubtitleInsufficientRegionalSupportError)
            match.assert_not_called()

    def test_final_audit_user_cancellation_wins_over_pending(self):
        cancel = threading.Event()
        inspect = self.mock_evidence([REASON])
        def cancel_after_final(reference, events, raw_offset=None):
            result = inspect(reference, events, raw_offset)
            if raw_offset is not None and Path(reference) == self.fixture.references[.75]:
                cancel.set()
            return result
        with patch.object(product, '_alignment_candidate', side_effect=self.fixture.match), \
             patch.object(continuous, '_extend_reference', side_effect=self.extension), \
             patch.object(evidence, 'evaluate_reference', side_effect=cancel_after_final):
            with self.assertRaises(core.CancelledError):
                self.route(cancel)

    def test_final_audit_budget_exhaustion_wins_over_pending(self):
        clock = product._DeadlineCancel(None, time.monotonic() + 20.)
        inspect = self.mock_evidence([REASON])
        def time_out_after_final(reference, events, raw_offset=None):
            result = inspect(reference, events, raw_offset)
            if raw_offset is not None and Path(reference) == self.fixture.references[.75]:
                clock.deadline = time.monotonic() - 1.
            return result
        with patch.object(product, '_DeadlineCancel', return_value=clock), \
             patch.object(product, '_alignment_candidate', side_effect=self.fixture.match), \
             patch.object(continuous, '_extend_reference', side_effect=self.extension), \
             patch.object(evidence, 'evaluate_reference', side_effect=time_out_after_final):
            with self.assertRaises(product.SubtitlePreflightTimeoutError):
                self.route()


if __name__ == '__main__':
    unittest.main()
