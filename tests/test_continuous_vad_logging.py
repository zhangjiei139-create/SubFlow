"""VAD proposals must not become adoption claims before candidate acceptance."""
import json
from contextlib import ExitStack
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import continuous_vad as vad
import pro_core as p
import smart_subtitles


class ContinuousVADLoggingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.movie = root / 'movie.mkv'
        self.movie.write_bytes(b'local-movie')
        self.source = root / 'source.srt'
        self.source.write_text(
            '1\n00:00:30,000 --> 00:00:32,000\nThis is a dialogue line.\n',
            encoding='utf-8',
        )
        reference = root / 'speech.npz'
        reference.write_bytes(b'local-speech-reference')
        self.shared = SimpleNamespace(
            duration_seconds=60,
            validation_audio_id=1,
            audio_language='en',
            vad_reference=reference,
        )
        self.work = root / 'preflight'
        self.logs = []

    def align(self, movie, normalized, output, *args, diagnostics, **kwargs):
        p._shifted_srt(normalized, output, -230)
        diagnostics.update(score=100, offset_seconds=-.23, raw_offset_seconds=-.23)

    def preflight(self):
        return vad.preflight(
            str(self.movie), str(self.source), self.work, 1,
            self.logs.append, shared=self.shared, candidate_label='候选',
        )

    def test_passed_preflight_remains_pending_until_caller_accepts(self):
        with patch.object(p, '_alignment_candidate', side_effect=self.align):
            output, report = self.preflight()
        self.assertTrue(output.is_file())
        self.assertTrue(any('连续VAD提议固定偏移 -0.23 秒；待候选核验，尚未采用' in line
                            for line in self.logs))
        self.assertTrue(report.startswith(vad.PROPOSAL_REPORT_PREFIX))
        self.assertIn('待候选其余验收', report)
        self.assertFalse(any('已采用' in message for message in self.logs))
        marker = json.loads((self.work / 'vad-verdict.json').read_text(encoding='utf-8'))
        self.assertEqual(marker['proposal_offset'], -.23)
        self.assertEqual(marker['applied_offset'], -.23)
        self.assertEqual(marker['alignment_status'], 'timeline-preflight-passed-candidate-pending')
        adopted = vad.adopted_alignment_report(report)
        self.assertTrue(adopted.startswith('已采用连续VAD固定偏移 -0.23 秒'))
        self.assertNotIn('待候选', adopted)

    def test_failed_body_check_never_produces_adoption_or_cached_verdict(self):
        def mutate_body(movie, normalized, output, *args, diagnostics, **kwargs):
            self.align(movie, normalized, output, *args, diagnostics=diagnostics, **kwargs)
            output.write_text(
                output.read_text(encoding='utf-8').replace('dialogue line', 'different content'),
                encoding='utf-8',
            )
        with patch.object(p, '_alignment_candidate', side_effect=mutate_body):
            with self.assertRaisesRegex(p.SubtitleVerificationToolError, '正文或顺序'):
                self.preflight()
        self.assertTrue(any('提议固定偏移' in message for message in self.logs))
        self.assertFalse(any('已采用' in message or vad.PROPOSAL_REPORT_PREFIX in message
                             for message in self.logs))
        self.assertFalse((self.work / 'vad-verdict.json').exists())

    def test_legacy_cache_is_reused_as_pending_without_realigning(self):
        with patch.object(p, '_alignment_candidate', side_effect=self.align):
            self.preflight()
        marker_path = self.work / 'vad-verdict.json'
        marker = json.loads(marker_path.read_text(encoding='utf-8'))
        marker.pop('proposal_offset')
        marker.pop('alignment_status')
        marker['report'] = ('连续VAD确认固定偏移 -0.23 秒；音轨 1（en）；'
                            '未运行Whisper正文核验')
        marker_path.write_text(json.dumps(marker), encoding='utf-8')
        self.logs.clear()
        with patch.object(p, '_alignment_candidate', side_effect=AssertionError('cache was not reused')):
            _, report = self.preflight()
        self.assertTrue(report.startswith(vad.PROPOSAL_REPORT_PREFIX))
        self.assertIn('待候选其余验收', report)
        self.assertFalse(any('连续VAD确认固定偏移' in message or '已采用' in message
                             for message in self.logs))
        # Keep the historical record readable without rewriting its audit data.
        self.assertEqual(json.loads(marker_path.read_text(encoding='utf-8')), marker)
        self.assertTrue(vad.adopted_alignment_report(marker['report']).startswith(
            '已采用连续VAD固定偏移 -0.23 秒'))

    def test_report_recognition_preserves_history_without_accepting_raw_proposals(self):
        self.assertTrue(p._timeline_report_verified(
            vad.PROPOSAL_REPORT_PREFIX + ' -0.23 秒；待候选其余验收'))
        self.assertTrue(p._timeline_report_verified('连续VAD确认固定偏移 -0.23 秒'))
        self.assertFalse(p._timeline_report_verified('连续VAD提议固定偏移 -0.23 秒；尚未采用'))
        self.assertEqual(vad.adopted_alignment_report('连续VAD提议固定偏移 -0.23 秒'), '')

    def find_candidate(self, acceptance):
        identity = SimpleNamespace(title='Movie', original_title='Movie', year='2025')
        candidate = SimpleNamespace(
            file_id=1, file_name='Movie.2025.srt', release='Movie.2025.BluRay',
            language='en', feature_title='Movie', feature_year='2025',
            identity_key='title-year:movie:2025',
        )
        provider = SimpleNamespace(
            load_settings=Mock(return_value={'api_key': 'local-test-key'}),
            search=Mock(return_value=(identity, [candidate], SimpleNamespace(query_mode='title-year'))),
            download=Mock(return_value=self.source),
        )
        proposal = vad._proposal_report(-.23, self.shared)
        with ExitStack() as stack:
            stack.enter_context(patch.object(smart_subtitles, 'PROVIDERS', [('OpenSubtitles', provider, 'api_key')]))
            stack.enter_context(patch.object(smart_subtitles, 'filename_container_identity_conflict', return_value=''))
            stack.enter_context(patch.object(smart_subtitles, '_downloaded_candidate_structure_conflict', return_value=''))
            stack.enter_context(patch.object(p, 'persistent_subtitle_logger', side_effect=lambda movie, log: log))
            stack.enter_context(patch.object(p, 'prepare_shared_subtitle_content_audio', return_value=self.shared))
            stack.enter_context(patch.object(p, 'preflight_online_subtitle', return_value=(self.source, proposal)))
            return smart_subtitles.find_verified_english(
                str(self.movie), 1, self.logs.append, max_candidates=1,
                candidate_acceptance=acceptance,
            )

    def test_final_adoption_is_logged_only_after_remaining_candidate_acceptance(self):
        def accept(subtitle, cancel):
            self.logs.append('remaining-acceptance-completed')
        result = self.find_candidate(accept)
        accepted_index = self.logs.index('remaining-acceptance-completed')
        adopted = [(index, message) for index, message in enumerate(self.logs)
                   if '已采用连续VAD固定偏移' in message]
        self.assertEqual(len(adopted), 1)
        self.assertGreater(adopted[0][0], accepted_index)
        self.assertTrue(result.verification_seal)
        self.assertTrue(result.report.startswith('已采用连续VAD固定偏移 -0.23 秒'))
        self.assertNotIn('待候选', result.report)

    def test_remaining_candidate_rejection_does_not_claim_adoption(self):
        def reject(subtitle, cancel):
            raise RuntimeError('图片字幕验收未通过')
        with self.assertRaisesRegex(RuntimeError, '图片字幕验收未通过'):
            self.find_candidate(reject)
        self.assertFalse(any('已采用连续VAD固定偏移' in message for message in self.logs))


if __name__ == '__main__':
    unittest.main()
