"""Retained PGS verification has a cumulative film budget after audio passes."""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import pgs_local_alignment as alignment
import pro_core as p
import subtitle_tool_core as core


class RetainedImageStageBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.movie = self.root / 'Movie.2011.mkv'
        self.movie.write_bytes(b'unchanged-container-fixture')
        self.reference = self.root / 'audio-passed.srt'
        self.events = [core.SubtitleEvent(core.srt_time_from_milliseconds((i * 3 + 5) * 1000),
            core.srt_time_from_milliseconds((i * 3 + 7) * 1000), f'Dialogue {i}.')
            for i in range(590)]
        core.write_srt(self.reference, self.events,
                       {i: event.text for i, event in enumerate(self.events, 1)})
        self.media = {'tracks': [
            {'id': 1, 'type': 'audio', 'codec': 'AAC',
             'properties': {'language': 'eng', 'default_track': True}},
            {'id': 4, 'type': 'subtitles', 'codec': 'HDMV PGS',
             'properties': {'language': 'chi', 'codec_id': 'S_HDMV/PGS'}},
        ]}
        self.now = 100.0
        self.logs = []
        self.stop = threading.Event()

    def advance(self, seconds):
        self.now += seconds

    def decision(self, accepted=True):
        return alignment.LocalAlignmentResult(accepted, .09 if accepted else None,
                .09, 'supported_fixed_shift' if accepted else 'start_residual_inconsistent')

    def context(self, metadata_cost=0):
        stack = ExitStack()
        stack.enter_context(patch.object(p.time, 'monotonic', side_effect=lambda: self.now))
        def metadata(*args, **kwargs):
            self.advance(metadata_cost)
            core.check_cancel(kwargs.get('cancel_event'))
            return self.media
        self.metadata = stack.enter_context(patch.object(core, 'inspect_media', side_effect=metadata))
        self.tracks = stack.enter_context(patch.object(core, 'inspect_tracks',
            side_effect=AssertionError('metadata must come from the same bounded response')))
        stack.enter_context(patch.object(core, 'video_track_duration_ns', return_value=1800 * 10**9))
        return stack

    def stage(self):
        return p.SharedRetainedImageStage(str(self.movie), [4], 1800, cancel=self.stop)

    def require(self, stage, *, reference=None, status=None, candidate_cancel=None):
        return p.require_image_timeline_anchor(str(self.movie), [4], 1,
            self.root / 'work', reference or self.reference, self.logs.append,
            candidate_cancel, prepared_image_status=status, retained_stage=stage)

    def test_metadata_is_read_once_across_preparation_acceptance_and_signature_issuance(self):
        with self.context(metadata_cost=2), patch.object(p, '_pgs_local_corrections',
                return_value={0: self.decision()}) as local:
            stage = self.stage()
            media, tracks = stage.prepare_metadata(self.logs.append)
            # A caller cannot modify the shared ordinal/codec mapping.
            tracks[1].id = 999
            media['tracks'].clear()
            status = {}
            self.assertTrue(self.require(stage, status=status)[4]['accepted'])
            self.assertIsInstance(status['prepared_image_timing'], p.PreparedImageTiming)
            self.require(stage)
        self.metadata.assert_called_once()
        self.tracks.assert_not_called()
        self.assertEqual(local.call_count, 2)
        self.assertEqual(stage.spent_seconds, 2)

    def test_expired_audio_candidate_clock_cannot_expire_retained_stage(self):
        expired_audio = p._DeadlineCancel(self.stop, 99)
        expired_audio.candidate_match_clock = True
        def local(*args):
            cancel = args[5]
            self.assertFalse(getattr(cancel, 'candidate_match_clock', False))
            self.advance(24)
            core.check_cancel(cancel)
            return {0: self.decision()}
        with self.context(metadata_cost=3), patch.object(p, '_pgs_local_corrections', side_effect=local):
            stage = self.stage()
            self.assertTrue(self.require(stage, candidate_cancel=expired_audio)[4]['accepted'])
        self.assertEqual(stage.spent_seconds, 27)

    def test_local_twelve_second_limit_still_reaches_shared_full_fallback(self):
        def packets(*args):
            self.advance(13)
            core.check_cancel(args[3])
            raise AssertionError('local timer must fire')
        def full(*args):
            self.advance(10)
            core.check_cancel(args[3])
            return [(i * 3 + 5., i * 3 + 7.) for i in range(590)]
        with self.context(metadata_cost=1), patch.object(p, '_pgs_local_packets', side_effect=packets), \
                patch.object(p, '_embedded_image_intervals', side_effect=full) as scan, \
                patch.object(p, '_interval_alignment', return_value=(.94, .09)), \
                patch.object(p, '_full_image_region_consistency',
                             return_value=alignment.RegionConsistencyResult(False)):
            stage = self.stage()
            self.assertTrue(self.require(stage)[4]['accepted'])
        scan.assert_called_once()
        self.assertEqual(stage.spent_seconds, 24)
        self.assertTrue(any('12秒上限' in line for line in self.logs))

    def deferred_full_round_trip(self, full_seconds):
        """Exercise actual require -> deferred completion -> require entrypoints.

        Only the deferred request and ffprobe tool response are fixtures. The
        completion function, stage scopes, retained comparison, and handoff
        issuance remain the production implementations.
        """
        original_prepare = p.prepare_embedded_text_corrections
        prepared_once = False
        full_ready = False
        def prepare(*args, **kwargs):
            nonlocal prepared_once
            if not prepared_once:
                prepared_once = True
                self.advance(13)
                raise p.SharedImageTimingPreparationRequired(str(self.movie), 0,
                    self.root / 'work/embedded-subtitle-correction/image-timing-cache', 1800)
            return original_prepare(*args, **kwargs)
        def full(*args):
            nonlocal full_ready
            if not full_ready:
                self.advance(full_seconds)
                full_ready = True
            core.check_cancel(args[3])
            return [(i * 3 + 5., i * 3 + 7.) for i in range(590)]
        def compare(*args):
            self.advance(5)
            return .94, .09
        with self.context(), patch.object(p, 'prepare_embedded_text_corrections', side_effect=prepare), \
                patch.object(p, '_pgs_local_corrections', return_value={}), \
                patch.object(p, '_embedded_image_intervals', side_effect=full) as scan, \
                patch.object(p, '_interval_alignment', side_effect=compare), \
                patch.object(p, '_full_image_region_consistency',
                             return_value=alignment.RegionConsistencyResult(False)):
            stage, status = self.stage(), {}
            try:
                self.require(stage, status=status)
            except p.SharedImageTimingPreparationRequired as request:
                self.assertIs(request.retained_stage, stage)
                self.assertEqual(stage.spent_seconds, 13)
                self.assertIsNone(stage._active_cancel)
                p.complete_shared_image_timing(request, self.logs.append, self.stop)
            else:
                self.fail('deferred request did not reach its controller')
            self.assertEqual(stage.spent_seconds, 13 + full_seconds)
            result = self.require(stage, status=status)
            self.assertEqual(scan.call_count, 2)  # second response is cached
            return stage, status, result

    def test_deferred_completion_full_io_counts_towards_shared_sixty_second_budget(self):
        # 13 local +45 full +5 compare =63, not a renewed 60-second allowance.
        with self.assertRaises(p.RetainedSubtitleStageTimeoutError) as caught:
            self.deferred_full_round_trip(45)
        self.assertEqual(caught.exception.budget_seconds, 60)
        self.assertEqual(caught.exception.spent_seconds, 63)

    def test_deferred_completion_under_shared_budget_can_issue_verified_handoff(self):
        # 13 local +39 full +5 comparison =57 and all quality checks still pass.
        stage, status, result = self.deferred_full_round_trip(39)
        self.assertEqual(stage.spent_seconds, 57)
        self.assertTrue(result[4]['accepted'])
        self.assertIsInstance(status['prepared_image_timing'], p.PreparedImageTiming)

    def test_reentry_never_renews_cumulative_budget(self):
        def local(*args):
            self.advance(31)
            core.check_cancel(args[5])
            return {0: self.decision()}
        with self.context(), patch.object(p, '_pgs_local_corrections', side_effect=local):
            stage = self.stage()
            self.require(stage)
            with self.assertRaises(p.RetainedSubtitleStageTimeoutError) as caught:
                self.require(stage)
            self.assertEqual(caught.exception.budget_seconds, 60)
            self.assertEqual(caught.exception.remaining_seconds, 0)
            self.assertEqual(caught.exception.reason_code, 'retained_image_stage_timeout')
            with self.assertRaises(p.RetainedSubtitleStageTimeoutError):
                stage.prepare_metadata(self.logs.append)
        self.assertEqual(stage.spent_seconds, 62)
        self.metadata.assert_called_once()

    def test_final_signature_time_is_charged_and_timeout_clears_handoff(self):
        real_make = p._make_prepared_image_timing
        def make(*args, **kwargs):
            result = real_make(*args, **kwargs)
            self.advance(61)
            return result
        with self.context(), patch.object(p, '_pgs_local_corrections', return_value={0: self.decision()}), \
                patch.object(p, '_make_prepared_image_timing', side_effect=make):
            stage, status = self.stage(), {}
            with self.assertRaises(p.RetainedSubtitleStageTimeoutError):
                self.require(stage, status=status)
            self.assertEqual(status, {})

    def test_metadata_tool_work_is_bounded_and_remains_a_retained_stage_timeout(self):
        with self.context(metadata_cost=61):
            stage = self.stage()
            with self.assertRaises(p.RetainedSubtitleStageTimeoutError):
                stage.prepare_metadata(self.logs.append)
        self.assertIsNone(stage._media)
        self.assertEqual(stage.remaining_seconds, 0)

    def test_expiry_during_low_score_comparison_reports_stage_timeout_not_bad_subtitle(self):
        def compare(*args):
            self.advance(61)
            return .2, 0.0
        with self.context(), patch.object(p, '_pgs_local_corrections', return_value={}), \
                patch.object(p, '_embedded_image_intervals', return_value=[(i * 3., i * 3. + 2.)
                    for i in range(30)]), patch.object(p, '_interval_alignment', side_effect=compare):
            with self.assertRaises(p.RetainedSubtitleStageTimeoutError):
                self.require(self.stage())

    def test_expiry_after_full_read_with_too_few_packets_is_still_stage_timeout(self):
        def full(*args):
            self.advance(61)
            return []
        with self.context(), patch.object(p, '_pgs_local_corrections', return_value={}), \
                patch.object(p, '_embedded_image_intervals', side_effect=full):
            with self.assertRaises(p.RetainedSubtitleStageTimeoutError):
                self.require(self.stage())

    def test_user_cancel_takes_precedence_over_simultaneous_deadline(self):
        def local(*args):
            self.advance(61)
            self.stop.set()
            core.check_cancel(args[5])
        with self.context(), patch.object(p, '_pgs_local_corrections', side_effect=local):
            with self.assertRaises(core.CancelledError):
                self.require(self.stage())

    def test_precancelled_user_does_not_read_metadata(self):
        self.stop.set()
        with self.context():
            with self.assertRaises(core.CancelledError):
                self.stage().prepare_metadata(self.logs.append)
        self.metadata.assert_not_called()

    def test_metadata_failure_preserves_tool_reason_and_does_not_become_audio_timeout(self):
        with self.context(), patch.object(core, 'inspect_media', side_effect=RuntimeError('disk read error')):
            with self.assertRaisesRegex(p.SharedImageTimingPreparationError, 'disk read error') as caught:
                self.stage().prepare_metadata(self.logs.append)
        self.assertNotIsInstance(caught.exception, p.RetainedSubtitleStageTimeoutError)
        self.assertNotIsInstance(caught.exception, core.CancelledError)

    def test_changed_movie_cannot_reuse_metadata_or_ordinal_mapping(self):
        with self.context():
            stage = self.stage()
            stage.prepare_metadata(self.logs.append)
            self.movie.write_bytes(b'movie changed during run')
            with self.assertRaisesRegex(p.SharedImageTimingPreparationError, '影片或图片字幕规则'):
                self.require(stage)
        self.metadata.assert_called_once()

    def test_changed_retention_ids_cannot_reuse_stage(self):
        with self.context():
            stage = self.stage()
            stage.prepare_metadata(self.logs.append)
            with self.assertRaisesRegex(p.SharedImageTimingPreparationError, '字幕轨'):
                p.require_image_timeline_anchor(str(self.movie), [8], 1, self.root / 'work',
                    self.reference, self.logs.append, retained_stage=stage)

    def test_another_movie_cannot_reuse_shared_metadata(self):
        other = self.root / 'different.mkv'
        other.write_bytes(self.movie.read_bytes())
        with self.context():
            stage = self.stage()
            with self.assertRaises(p.SharedImageTimingPreparationError):
                stage.validate(other, [4])
        self.metadata.assert_not_called()

    def test_missing_image_track_is_tool_mapping_failure(self):
        self.media['tracks'] = self.media['tracks'][:1]
        with self.context():
            with self.assertRaisesRegex(p.SharedImageTimingPreparationError, '找不到'):
                self.stage().prepare_metadata(self.logs.append)

    def test_passed_handoff_stays_bound_to_exact_subtitle_content(self):
        with self.context(), patch.object(p, '_pgs_local_corrections', return_value={0: self.decision()}):
            status = {}
            self.require(self.stage(), status=status)
            handoff = status['prepared_image_timing']
            self.assertTrue(p.prepared_image_timing_matches_subtitle(handoff, self.reference))
            self.reference.write_text(self.reference.read_text(encoding='utf-8') + '\nnew text', encoding='utf-8')
            self.assertFalse(p.prepared_image_timing_matches_subtitle(handoff, self.reference))

    def test_inadequate_evidence_is_still_rejected_without_handoff(self):
        with self.context(), patch.object(p, '_pgs_local_corrections', return_value={}), \
                patch.object(p, '_embedded_image_intervals', return_value=[]):
            status = {}
            with self.assertRaises(p.RetainedSubtitleVerificationError):
                self.require(self.stage(), status=status)
            self.assertEqual(status, {})

    def test_regional_conflict_is_not_overridden_by_high_full_score(self):
        conflict = alignment.LocalAlignmentResult(False, None, .09,
                    'regional_offset_conflict', timing_conflict=True)
        with self.context(), patch.object(p, '_pgs_local_corrections', return_value={0: conflict}), \
                patch.object(p, '_embedded_image_intervals') as full:
            with self.assertRaises(p.RetainedSubtitleVerificationError) as caught:
                self.require(self.stage())
        self.assertEqual(caught.exception.results[4]['failure_kind'], 'non_fixed_offset')
        full.assert_not_called()


if __name__ == '__main__':
    unittest.main()
