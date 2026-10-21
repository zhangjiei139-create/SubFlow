"""Independent audio/retained-stage controller regressions; no real IO workers."""
from contextlib import ExitStack
import copy
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import batch_core as batch
import pro_core as pro
import smart_subtitles as smart
import subtitle_audit
import subtitle_tool_core as core
from profile_model import BatchPlan, PreferenceProfile


class FakeClock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class AudioRetainedStageHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.movie = self.root / 'Movie.2025.mkv'
        self.movie.write_bytes(b'isolated controller fixture')
        self.clock = FakeClock()
        self.logs = []
        self.cancel = threading.Event()
        self.report_text = '连续VAD时间轴核验通过：提议固定偏移 +0.09 秒'
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('socket.socket.connect', side_effect=AssertionError('network forbidden')))
        self.stack.enter_context(patch('socket.create_connection', side_effect=AssertionError('network forbidden')))
        self.stack.enter_context(patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network forbidden')))
        self.stack.enter_context(patch.object(smart.time, 'monotonic', self.clock))
        self.stack.enter_context(patch.object(smart, 'filename_container_identity_conflict', return_value=''))
        self.reference = self.root / 'speech-75.npz'
        self.reference.write_bytes(b'VAD fixture')
        self.shared = SimpleNamespace(duration_seconds=600., validation_audio_id=1,
            audio_language='en', vad_reference=self.reference, revision=0,
            vad_preparation_state={'references': {.75: self.reference}})
        self.prepare = self.stack.enter_context(patch.object(pro,
            'prepare_shared_subtitle_content_audio', return_value=self.shared))

    def service(self, count=2):
        candidates = [SimpleNamespace(file_id=str(i), release=f'Movie.2025.BluRay.{i}',
            file_name=f'movie-{i}.srt', feature_title='Movie', feature_year='2025',
            language='en', identity_key='imdb:fixture', identity_verified=True)
            for i in range(1, count + 1)]

        def download(_key, item, destination):
            path = Path(destination) / item.file_name
            path.parent.mkdir(parents=True, exist_ok=True)
            self.write_subtitle(path, int(item.file_id))
            return path

        service = SimpleNamespace(load_settings=Mock(return_value={'key': 'fixture'}),
            search=Mock(return_value=(SimpleNamespace(title='Movie', original_title='Movie', year='2025'),
                                      candidates, None)), download=Mock(side_effect=download))
        self.stack.enter_context(patch.object(smart, 'PROVIDERS', (('Test', service, 'key'),)))
        return service

    def write_subtitle(self, path, second=1, text='This is the correct dialogue for this movie.'):
        Path(path).write_text(f'1\n00:00:{second:02d},000 --> 00:00:{second:02d},800\n{text}\n',
                              encoding='utf-8')

    def preflight(self, seconds=1.0, error=None):
        def verify(*args, **kwargs):
            self.clock.advance(seconds)
            if error is not None:
                raise error
            return Path(args[1]), self.report_text
        return self.stack.enter_context(patch.object(pro, 'preflight_online_subtitle', side_effect=verify))

    def run_search(self, accept=None, **kwargs):
        return smart.find_verified_english(str(self.movie), 1, self.logs.append,
            self.cancel, candidate_acceptance=accept, **kwargs)

    def checkpoint_path(self):
        return smart._work_directory(str(self.movie)) / 'audio-verified-source.json'

    def saved(self):
        return json.loads(self.checkpoint_path().read_text(encoding='utf-8'))

    def report(self):
        paths = list(self.root.rglob('subtitle-report-*.json'))
        self.assertTrue(paths)
        return json.loads(max(paths, key=lambda path: path.stat().st_mtime_ns).read_text(encoding='utf-8'))

    def checkpoint(self):
        source = self.root / 'audio-passed.srt'
        self.write_subtitle(source)
        return smart._checkpoint_after_audio(str(self.movie), 1, self.shared, source,
            'Test', 'Movie.2025.BluRay', 'imdb:fixture', self.report_text,
            smart.media_signature(str(self.movie)))

    def assert_not_finally_accepted(self, report):
        self.assertFalse(any(row.get('stages', {}).get('acceptance', {}).get('state') == 'verified'
                             for row in report['records']))

    def test_imports_business_modules_from_this_test_tree(self):
        expected = Path(__file__).resolve().parents[1]
        for module in (smart, batch, pro):
            self.assertEqual(Path(module.__file__).resolve().parent, expected)

    def test_audio_pass_then_retained_and_finalization_over_20_seconds_succeeds(self):
        self.service()
        verify = self.preflight(19.9)
        stage = pro.SharedRetainedImageStage(self.movie, [4], 600, self.cancel)

        def accept(path, cancel):
            self.assertEqual(cancel.validation_audio_id, 1)
            self.assertFalse(cancel.is_set())
            with stage.scope(self.logs.append) as bounded:
                self.clock.advance(35.0)
                core.check_cancel(bounded)
            self.clock.advance(1.0)  # Handoff/final bookkeeping after the image read.

        result = self.run_search(accept)
        self.assertTrue(Path(result.subtitle_path).is_file())
        self.assertEqual(verify.call_count, 1)
        self.assertEqual(stage.spent_seconds, 35.0)
        self.assertEqual(self.saved()['state'], 'completed')
        row = self.report()['records'][0]
        self.assertEqual(row['stages']['audio_timing']['state'], 'audio_verified')
        self.assertEqual(row['stages']['acceptance']['state'], 'verified')

    def test_shared_preparation_before_audio_does_not_spend_audio_budget(self):
        self.service(1)
        verify = self.preflight(19.9)
        def prepare(_cancel):
            self.clock.advance(45.)
        result = self.run_search(candidate_preparation=prepare)
        self.assertTrue(Path(result.subtitle_path).is_file())
        self.assertEqual(verify.call_count, 1)
        self.assertEqual(self.saved()['state'], 'completed')

    def test_audio_above_20_seconds_is_still_rejected_before_retained_stage(self):
        self.service(1)
        verify = self.preflight(20.01)
        accept = Mock()
        with self.assertRaises(RuntimeError):
            self.run_search(accept)
        self.assertEqual(verify.call_count, 1)
        accept.assert_not_called()
        self.assertFalse(self.checkpoint_path().exists())
        row = self.report()['records'][0]
        self.assertEqual(row['stages']['audio_timing']['state'], 'timeout')
        self.assert_not_finally_accepted(self.report())

    def test_audio_exact_deadline_is_not_silently_extended(self):
        self.service(1)
        self.preflight(20.0)
        accept = Mock()
        with self.assertRaises(RuntimeError):
            self.run_search(accept)
        accept.assert_not_called()
        self.assertEqual(self.report()['records'][0]['stages']['audio_timing']['state'], 'timeout')

    def test_retained_timeout_stops_without_matching_next_candidate(self):
        service = self.service(3)
        verify = self.preflight(1.)
        stage = pro.SharedRetainedImageStage(self.movie, [4], 600, self.cancel)
        def accept(_path, _cancel):
            with stage.scope(self.logs.append) as bounded:
                self.clock.advance(stage.limit_seconds + .01)
                core.check_cancel(bounded)
        with self.assertRaises(pro.RetainedSubtitleStageTimeoutError) as caught:
            self.run_search(accept)
        self.assertEqual(service.search.call_count, 1)
        self.assertEqual(verify.call_count, 1)
        self.assertTrue(caught.exception.audio_checkpoint)
        self.assertEqual(self.saved()['state'], 'retained_unverified')
        row = self.report()['records'][0]
        self.assertEqual(row['stages']['audio_timing']['state'], 'audio_verified')
        self.assertEqual(row['stages']['retained_tracks']['state'], 'timeout')
        self.assert_not_finally_accepted(self.report())

    def test_retained_failure_is_never_returned_as_final_verified(self):
        self.service(3)
        verify = self.preflight()
        def accept(path, _cancel):
            raise pro.RetainedSubtitleVerificationError('轨4仍不通过', {4: {'accepted': False}}, path)
        with self.assertRaises(pro.RetainedSubtitleVerificationError):
            self.run_search(accept)
        self.assertEqual(verify.call_count, 1)
        self.assertEqual(self.saved()['state'], 'retained_unverified')
        row = self.report()['records'][0]
        self.assertEqual(row['stages']['audio_timing']['state'], 'audio_verified')
        self.assertEqual(row['stages']['retained_tracks']['state'], 'retained_track_unverified')
        self.assert_not_finally_accepted(self.report())

    def test_local_to_shared_full_pgs_fallback_keeps_same_audio_passed_source(self):
        self.service(2)
        verify = self.preflight(19.9)
        stage = pro.SharedRetainedImageStage(self.movie, [4], 600, self.cancel)
        sources = []
        def accept(path, _cancel):
            sources.append(str(path))
            with stage.scope(self.logs.append):
                if len(sources) == 1:
                    self.clock.advance(15.)
                    request = pro.SharedImageTimingPreparationRequired(str(self.movie), 0,
                        self.root / 'image-cache', 600.)
                    request.retained_stage = stage
                    raise request
                self.clock.advance(4.)
        def scan(*args, **kwargs):
            self.clock.advance(40.)
            core.check_cancel(args[3])
            return [(1., 2.)]
        with patch.object(pro, '_embedded_image_intervals', side_effect=scan) as scan_image:
            result = self.run_search(accept)
        self.assertEqual(verify.call_count, 1)
        scan_image.assert_called_once()
        self.assertEqual(sources, [result.subtitle_path, result.subtitle_path])
        self.assertEqual(stage.spent_seconds, 59.)
        self.assertEqual(self.saved()['state'], 'completed')

    def test_full_pgs_fallback_cannot_renew_the_cumulative_retained_budget(self):
        self.service(2)
        verify = self.preflight()
        stage = pro.SharedRetainedImageStage(self.movie, [4], 600, self.cancel)
        accepts = []
        def accept(path, _cancel):
            accepts.append(str(path))
            with stage.scope(self.logs.append):
                self.clock.advance(15.)
                request = pro.SharedImageTimingPreparationRequired(str(self.movie), 0,
                    self.root / 'image-cache', 600.)
                request.retained_stage = stage
                raise request
        def scan(*args, **kwargs):
            self.clock.advance(46.)
            core.check_cancel(args[3])
        with patch.object(pro, '_embedded_image_intervals', side_effect=scan):
            with self.assertRaises(pro.RetainedSubtitleStageTimeoutError):
                self.run_search(accept)
        self.assertEqual(verify.call_count, 1)
        self.assertEqual(len(accepts), 1)
        self.assertEqual(stage.spent_seconds, 61.)
        self.assertEqual(self.saved()['state'], 'retained_unverified')
        row = self.report()['records'][0]
        self.assertEqual(row['stages']['audio_timing']['state'], 'audio_verified')
        self.assertEqual(row['stages']['retained_tracks']['state'], 'timeout')
        self.assert_not_finally_accepted(self.report())

    def test_retained_tool_failure_is_not_a_reason_to_try_another_candidate(self):
        self.service(3)
        verify = self.preflight()
        accept = Mock(side_effect=pro.SubtitleVerificationToolError('ffprobe failed'))
        with self.assertRaises(pro.SubtitleVerificationToolError):
            self.run_search(accept)
        self.assertEqual(verify.call_count, 1)
        self.assertEqual(self.saved()['state'], 'retained_unverified')
        row = self.report()['records'][0]
        self.assertEqual(row['stages']['audio_timing']['state'], 'audio_verified')
        self.assertEqual(row['stages']['retained_tracks']['state'], 'infrastructure')
        self.assert_not_finally_accepted(self.report())

    def test_audio_tool_failure_stops_before_retained_without_saved_pass(self):
        self.service(3)
        verify = self.preflight(error=pro.SubtitleVerificationToolError('local VAD tool failed'))
        accept = Mock()
        with self.assertRaises(pro.SubtitleVerificationToolError):
            self.run_search(accept)
        self.assertEqual(verify.call_count, 1)
        accept.assert_not_called()
        self.assertFalse(self.checkpoint_path().exists())
        self.assertEqual(self.report()['records'][0]['stages']['audio_timing']['state'], 'infrastructure')

    def test_saved_checkpoint_restarts_only_retained_stage_without_provider_or_vad(self):
        checkpoint = self.checkpoint()
        smart._save_audio_checkpoint(smart._work_directory(str(self.movie)), checkpoint, 'retained_unverified')
        forbidden_service = SimpleNamespace(load_settings=Mock(side_effect=AssertionError('settings not needed')))
        self.stack.enter_context(patch.object(smart, 'PROVIDERS', (('Forbidden', forbidden_service, 'key'),)))
        verify = self.stack.enter_context(patch.object(pro, 'preflight_online_subtitle',
            side_effect=AssertionError('VAD rematch forbidden')))
        accept = Mock()
        result = self.run_search(accept)
        self.assertEqual(result.subtitle_path, checkpoint['path'])
        self.prepare.assert_not_called()
        verify.assert_not_called()
        forbidden_service.load_settings.assert_not_called()
        self.assertEqual(accept.call_count, 1)
        self.assertEqual(str(accept.call_args.args[0]), checkpoint['path'])
        self.assertEqual(self.saved()['state'], 'completed')
        self.assertTrue(self.report()['records'][0]['stages']['audio_timing']['cache_reused'])

    def test_saved_checkpoint_still_requires_retained_acceptance(self):
        checkpoint = self.checkpoint()
        self.stack.enter_context(patch.object(smart, 'PROVIDERS', ()))
        accept = Mock(side_effect=pro.RetainedSubtitleVerificationError('轨4未确认', {4: {'accepted': False}}))
        with self.assertRaises(pro.RetainedSubtitleVerificationError):
            self.run_search(accept, audio_checkpoint=checkpoint)
        accept.assert_called_once()
        self.prepare.assert_not_called()
        self.assert_not_finally_accepted(self.report())

    def test_saved_checkpoint_retained_timeout_keeps_audio_stage(self):
        checkpoint = self.checkpoint()
        self.stack.enter_context(patch.object(smart, 'PROVIDERS', ()))
        accept = Mock(side_effect=pro.RetainedSubtitleStageTimeoutError(60, 60.1))
        with self.assertRaises(pro.RetainedSubtitleStageTimeoutError):
            self.run_search(accept, audio_checkpoint=checkpoint)
        row = self.report()['records'][0]
        self.assertEqual(row['stages']['audio_timing']['state'], 'audio_verified')
        self.assertEqual(row['stages']['retained_tracks']['state'], 'timeout')
        self.assertEqual(self.saved()['state'], 'retained_unverified')

    def test_changed_movie_rejects_checkpoint(self):
        checkpoint = self.checkpoint()
        self.movie.write_bytes(b'changed movie file')
        with self.assertRaises(RuntimeError):
            smart._validated_audio_checkpoint(str(self.movie), 1, checkpoint)

    def test_changed_subtitle_rejects_checkpoint(self):
        checkpoint = self.checkpoint()
        self.write_subtitle(checkpoint['path'], text='This subtitle has changed after verification.')
        with self.assertRaises(RuntimeError):
            smart._validated_audio_checkpoint(str(self.movie), 1, checkpoint)

    def test_changed_selected_audio_rejects_checkpoint(self):
        checkpoint = self.checkpoint()
        with self.assertRaises(RuntimeError):
            smart._validated_audio_checkpoint(str(self.movie), 2, checkpoint)

    def test_changed_seal_or_rule_or_pass_report_rejects_checkpoint(self):
        original = self.checkpoint()
        for field, value in (('seal', 'not-a-seal'), ('rule', 'unrecognized-rule'),
                             ('report', '没有通过音频核验')):
            with self.subTest(field=field):
                checkpoint = copy.deepcopy(original)
                checkpoint[field] = value
                with self.assertRaises(RuntimeError):
                    smart._validated_audio_checkpoint(str(self.movie), 1, checkpoint)

    def test_invalid_saved_checkpoint_cannot_call_acceptance(self):
        checkpoint = self.checkpoint()
        checkpoint['seal'] = 'changed'
        smart._save_audio_checkpoint(smart._work_directory(str(self.movie)), checkpoint, 'retained_unverified')
        self.stack.enter_context(patch.object(smart, 'PROVIDERS', ()))
        accept = Mock()
        with self.assertRaises(RuntimeError):
            self.run_search(accept)
        accept.assert_not_called()
        self.prepare.assert_not_called()

    def test_malformed_persisted_checkpoint_shapes_are_cache_misses(self):
        self.stack.enter_context(patch.object(smart, 'PROVIDERS', ()))
        path = self.checkpoint_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        invalid_path = self.checkpoint()
        invalid_path['path'] = ['not', 'a', 'path']
        for document in ([], None, 42, 'not a record',
                         {'schema': 1, 'state': 'retained_unverified', 'checkpoint': []},
                         {'schema': 1, 'state': 'audio_verified', 'checkpoint': None},
                         {'schema': 1, 'state': 'audio_verified', 'checkpoint': [1]},
                         {'schema': 1, 'state': 'audio_verified', 'checkpoint': 'bad record'},
                         {'schema': 1, 'state': 'audio_verified', 'checkpoint': invalid_path}):
            with self.subTest(document=document):
                path.write_text(json.dumps(document), encoding='utf-8')
                accept = Mock()
                with self.assertRaisesRegex(RuntimeError, '未配置'):
                    self.run_search(accept)
                accept.assert_not_called()
        self.prepare.assert_not_called()

    def test_completed_or_rejected_checkpoint_is_not_automatically_reused(self):
        self.stack.enter_context(patch.object(smart, 'PROVIDERS', ()))
        checkpoint = self.checkpoint()
        for state in ('completed', 'source_rejected'):
            with self.subTest(state=state):
                smart._save_audio_checkpoint(smart._work_directory(str(self.movie)), checkpoint, state)
                accept = Mock()
                with self.assertRaisesRegex(RuntimeError, '未配置'):
                    self.run_search(accept)
                accept.assert_not_called()
        self.prepare.assert_not_called()

    def test_user_cancel_during_retained_wins_over_stage_timeout(self):
        self.service(2)
        verify = self.preflight()
        stage = pro.SharedRetainedImageStage(self.movie, [4], 600, self.cancel)
        def accept(_path, _cancel):
            with stage.scope(self.logs.append) as bounded:
                self.clock.advance(stage.limit_seconds + 1)
                self.cancel.set()
                core.check_cancel(bounded)
        with self.assertRaises(core.CancelledError):
            self.run_search(accept)
        self.assertEqual(verify.call_count, 1)
        self.assertEqual(self.report()['outcome'], 'stopped')
        self.assertEqual(self.report()['records'][0]['stages']['audio_timing']['state'], 'audio_verified')
        self.assertNotIn('retained_tracks', self.report()['records'][0]['stages'])
        self.assert_not_finally_accepted(self.report())

    def test_user_cancel_at_audio_return_does_not_mint_pass_checkpoint(self):
        self.service(2)
        def verify(*args, **kwargs):
            self.cancel.set()
            return Path(args[1]), self.report_text
        match = self.stack.enter_context(patch.object(pro, 'preflight_online_subtitle', side_effect=verify))
        accept = Mock()
        with self.assertRaises(core.CancelledError):
            self.run_search(accept)
        self.assertEqual(match.call_count, 1)
        accept.assert_not_called()
        self.assertFalse(self.checkpoint_path().exists())
        self.assertEqual(self.report()['outcome'], 'stopped')

    def test_user_cancel_on_checkpoint_resume_cannot_return_verified(self):
        checkpoint = self.checkpoint()
        self.cancel.set()
        accept = Mock()
        with self.assertRaises(core.CancelledError):
            self.run_search(accept, audio_checkpoint=checkpoint)
        accept.assert_not_called()
        self.prepare.assert_not_called()
        self.assertEqual(self.report()['outcome'], 'stopped')

    def test_normal_wrong_candidate_still_allows_next_candidate(self):
        self.service(2)
        calls = []
        def verify(*args, **kwargs):
            calls.append(Path(args[1]))
            if len(calls) == 1:
                raise RuntimeError('字幕错片，时间轴不匹配')
            return Path(args[1]), self.report_text
        self.stack.enter_context(patch.object(pro, 'preflight_online_subtitle', side_effect=verify))
        accept = Mock()
        result = self.run_search(accept)
        self.assertEqual(len(calls), 2)
        self.assertEqual(Path(result.subtitle_path), calls[1])
        accept.assert_called_once()
        rows = self.report()['records']
        self.assertEqual(rows[0]['stages']['audio_timing']['state'], 'rejected')
        self.assertEqual(rows[1]['stages']['acceptance']['state'], 'verified')

    def batch_plan(self):
        return BatchPlan(str(self.movie), 1, status='ready', audio_ids=[1], subtitle_ids=[4],
            has_image_subtitles=True, has_retained_image_subtitles=True,
            output_path=str(self.root / 'Movie.SF.mkv'))

    def batch_context(self):
        audio = core.Track(1, 'audio', 'AAC', 'en', '', True, False, False, False, False)
        image = core.Track(4, 'subtitles', 'HDMV PGS', 'en', '', False, False, False, True, False)
        stack = ExitStack()
        stack.enter_context(patch.object(core, 'inspect_tracks', return_value=[audio, image]))
        stack.enter_context(patch.object(batch, 'ordered_text_anchors', return_value=[]))
        stack.enter_context(patch.object(batch, '_duration_from_track_statistics', return_value=600.))
        return stack

    def test_batch_keeps_timeout_checkpoint_and_blocks_translation_and_mux(self):
        plan = self.batch_plan()
        checkpoint = self.checkpoint()
        error = pro.RetainedSubtitleStageTimeoutError(60, 61)
        error.audio_checkpoint = checkpoint
        with self.batch_context(), patch.object(smart, 'find_verified_english', side_effect=error), \
                patch.object(pro, 'process_pro') as process:
            with self.assertRaises(pro.RetainedSubtitleStageTimeoutError):
                batch.process_plan(plan, PreferenceProfile(1, 'fixture', subtitle_languages=['en']),
                                   self.logs.append, None)
        self.assertEqual(plan.audio_verified_source, checkpoint)
        self.assertFalse(plan.external_subtitle_verified)
        process.assert_not_called()

    def test_retained_timeout_advice_distinguishes_saved_audio_from_preparation_timeout(self):
        for has_checkpoint in (True, False):
            with self.subTest(has_audio_checkpoint=has_checkpoint):
                error = pro.RetainedSubtitleStageTimeoutError(60, 61)
                if has_checkpoint:
                    error.audio_checkpoint = self.checkpoint()
                advice = batch.failure_advice(error)
                self.assertIn('保留', advice)
                self.assertIn('重新处理', advice)
                self.assertNotIn('右键影片→字幕选择', advice)
                self.assertNotIn('改用', advice)
                self.assertNotIn('换一份', advice)
                if has_checkpoint:
                    self.assertIn('复用', advice)
                    self.assertIn('音频', advice)
                else:
                    self.assertNotIn('已通过音频', advice)
                    self.assertNotIn('音频已通过', advice)
                    self.assertNotIn('音频核验已完成', advice)
                    self.assertNotIn('已保存来源', advice)

    def test_batch_passes_prior_checkpoint_and_clears_it_only_after_acceptance(self):
        plan = self.batch_plan()
        checkpoint = self.checkpoint()
        plan.audio_verified_source = copy.deepcopy(checkpoint)
        def search(*args, **kwargs):
            self.assertEqual(kwargs['audio_checkpoint'], checkpoint)
            self.assertTrue(callable(kwargs['candidate_acceptance']))
            self.assertTrue(callable(kwargs['candidate_preparation']))
            return SimpleNamespace(subtitle_path=checkpoint['path'], language='en', report=self.report_text,
                provider='Test', release=checkpoint['release'], identity_key=checkpoint['identity_key'],
                verification_seal=checkpoint['seal'])
        with self.batch_context(), patch.object(smart, 'find_verified_english', side_effect=search), \
                patch.object(pro, 'process_pro', return_value='finished') as process:
            batch.process_plan(plan, PreferenceProfile(1, 'fixture', subtitle_languages=['en']),
                               self.logs.append, None)
        self.assertEqual(plan.audio_verified_source, {})
        self.assertTrue(plan.external_subtitle_verified)
        process.assert_called_once()


if __name__ == '__main__':
    unittest.main()
