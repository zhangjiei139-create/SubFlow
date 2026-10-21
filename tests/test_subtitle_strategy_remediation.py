"""Purpose, stage ownership, stop-loss and human drift regressions."""
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import batch_core as batch
import manual_timeline
import pro_core
import smart_subtitles as smart
import subtitle_audit
import subtitle_roles
import subtitle_tool_core as core
from profile_model import BatchPlan, PreferenceProfile


def subtitle(number, name, language='en', image=False):
    return core.Track(number, 'subtitles', 'HDMV PGS' if image else 'SubRip/SRT',
                      language, name, False, False, not image, image, False)


class StrategyRemediationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.movie = self.root / 'Movie.2025.mkv'
        self.movie.write_bytes(b'movie')
        self.audio = core.Track(1, 'audio', 'AAC', 'en', '', True, False, False, False, False)

    def test_1987_sdh_beats_both_commentaries_for_retention_and_offline_source(self):
        tracks = [self.audio, subtitle(4, 'English (SDH)', image=True),
                  subtitle(5, 'English (Commentary)', image=True),
                  subtitle(6, 'English (Text Commentary by Film Historian Eric Lichtenfeld)', image=True)]
        with patch.object(batch, '_inspect_media_cached', return_value=({}, False)), \
                patch.object(core, 'tracks_from_media', return_value=tracks):
            plan = batch.analyze_video(str(self.movie), PreferenceProfile(1, 'test', subtitle_languages=['en']))
        self.assertEqual(plan.subtitle_ids, [4])
        self.assertEqual(batch._offline_source_track(tracks, self.audio).id, 4)
        self.assertTrue(subtitle_roles.track_role(tracks[1]).main_dialogue)
        self.assertFalse(subtitle_roles.track_role(tracks[2]).main_dialogue)

    def test_known_featurettes_are_filtered_before_download(self):
        for name in ('Building the Predator Homeworld.eng.srt',
                     'Preparing for War - Development and Production.eng.srt',
                     'Movie.2025.Behind.the.Scenes.srt', '影片.制作特辑.srt'):
            with self.subTest(name=name):
                candidate = SimpleNamespace(release=name, file_name=name)
                self.assertIn('花絮', smart._candidate_role_conflict(candidate))

    def test_role_words_in_exact_film_title_are_not_filtered(self):
        for name in ('Making of a Murderer', 'Forced', 'Trailer', 'Sample', 'The Commentary'):
            identity = SimpleNamespace(title=name, original_title=name)
            candidate = SimpleNamespace(release=f'{name}.2025.BluRay', file_name=f'{name}.srt')
            self.assertEqual(smart._candidate_role_conflict(candidate, identity), '')
        self.assertIn('评论', smart._candidate_role_conflict(
            SimpleNamespace(release='The Commentary.2025.commentary', file_name='sub.srt'),
            SimpleNamespace(title='The Commentary')))

    def test_foreign_clocks_do_not_trigger_26_full_preflights(self):
        tracks = [self.audio, subtitle(2, 'English SDH'), subtitle(3, 'English')]
        tracks += [subtitle(i+4, 'Normal', code) for i, code in enumerate(['fr','de','es','it','pt','ja','ko','ru']*3)]
        anchors = batch.ordered_text_anchors(tracks, self.audio)
        self.assertEqual([track.id for track in anchors], [3, 2])

    def test_commentary_does_not_cover_language_or_suppress_verified_english(self):
        original = self.root / 'confirmed.srt'
        original.write_text('1\n00:00:01,000 --> 00:00:02,000\nHello there.\n', encoding='utf-8')
        plan = BatchPlan(str(self.movie), 1, status='ready', subtitle_ids=[5], audio_ids=[1],
            source_mode='external', external_subtitle=str(original), external_language='en',
            external_subtitle_verified=True, output_path=str(self.root/'Movie.SF.mkv'),
            external_subtitle_media_signature=batch.media_signature(self.movie),
            external_subtitle_content_hash=hashlib.sha256(original.read_bytes()).hexdigest())
        tracks = [self.audio, subtitle(5, 'English (Commentary)', image=True)]
        self.assertEqual(batch._subtitle_coverage(tracks[1:], ['en'], False), set())
        with patch.object(core, 'inspect_tracks', return_value=tracks), patch.object(pro_core, 'process_pro') as process:
            batch.process_plan(plan, PreferenceProfile(1, 'test', subtitle_languages=['en']), lambda _: None, None)
        self.assertEqual(process.call_args.kwargs['keep_subtitle_ids'], [])
        self.assertTrue(process.call_args.kwargs['include_external_source_in_output'])

    def service(self, count, same_clock=False, extra=False):
        candidates = [SimpleNamespace(file_id=str(i), release=f'Movie.2025.BluRay.{i}',
            file_name=f'Movie-{i}.srt', feature_title='Movie', feature_year='2025', language='en')
            for i in range(1, count+1)]
        if extra:
            candidates[0].release = 'Movie.2025.Making.of'
        def download(_key, candidate, destination):
            path = Path(destination)/candidate.file_name
            path.parent.mkdir(parents=True, exist_ok=True)
            second = 1 if same_clock else int(candidate.file_id)
            path.write_text(f'1\n00:00:0{second},000 --> 00:00:0{second},800\n'
                            f'This is distinct dialogue {candidate.file_id}.\n', encoding='utf-8')
            return path
        return SimpleNamespace(load_settings=lambda: {'key':'test'}, search=lambda *_: (None,candidates,None),
                               download=download)

    def search_context(self, service):
        reference = self.root/'speech-75.npz'
        reference.write_bytes(b'preserved VAD')
        reference.with_suffix('.json').write_text('{}', encoding='utf-8')
        shared = SimpleNamespace(duration_seconds=600., validation_audio_id=1, audio_language='en',
            vad_reference=reference, vad_preparation_state={'references':{.75:reference}})
        stack = ExitStack()
        stack.enter_context(patch.object(smart, 'PROVIDERS', (('Test',service,'key'),)))
        stack.enter_context(patch.object(smart, 'filename_container_identity_conflict', return_value=''))
        stack.enter_context(patch.object(pro_core, 'prepare_shared_subtitle_content_audio', return_value=shared))
        return stack, reference

    def report(self):
        paths = list(self.root.rglob('subtitle-report-*.json'))
        self.assertEqual(len(paths), 1)
        return json.loads(paths[0].read_text('utf-8'))

    def test_same_failed_clock_is_matched_once_and_reported_unknown(self):
        stack, reference = self.search_context(self.service(3, same_clock=True))
        error = pro_core.SubtitleInsufficientRegionalSupportError(
            dict(sufficient=False, reasons=['insufficient_independent_regional_support']), reference, {})
        with stack, patch.object(pro_core, 'preflight_online_subtitle', side_effect=error) as preflight:
            with self.assertRaises(smart.PendingSubtitleReviewError) as caught:
                smart.find_verified_english(str(self.movie), 1, lambda _: None)
        self.assertEqual(preflight.call_count, 1)
        self.assertEqual(len(caught.exception.candidates), 1)
        report = self.report()
        self.assertEqual(report['attempted_candidates'], 1)
        self.assertEqual(report['state_counts']['not_examined'], 2)
        self.assertEqual(report['state_counts']['insufficient_evidence'], 1)

    def test_pgs_rejection_keeps_passed_audio_stage_and_separate_error(self):
        stack, _ = self.search_context(self.service(1))
        def verify(*args, **kwargs):
            return Path(args[1]), '连续VAD时间轴核验通过：提议固定偏移 -0.89 秒'
        def reject(path, cancel):
            raise pro_core.RetainedSubtitleVerificationError('轨4尚未核验', {4:{'accepted':False}}, path)
        with stack, patch.object(pro_core, 'preflight_online_subtitle', side_effect=verify):
            with self.assertRaises(pro_core.RetainedSubtitleVerificationError):
                smart.find_verified_english(str(self.movie), 1, lambda _: None, candidate_acceptance=reject)
        row = self.report()['records'][0]
        passed = row['stages']['audio_timing']
        self.assertEqual(passed['state'], 'audio_verified')
        self.assertTrue(Path(passed['aligned_path']).is_file())
        self.assertEqual(row['state'], 'retained_track_unverified')
        self.assertNotIn('字幕选择', batch.failure_advice(pro_core.RetainedSubtitleVerificationError('轨4未核验', {})).split('调整')[0])

    def test_budget_does_not_claim_all_returned_candidates_were_rejected(self):
        stack, _ = self.search_context(self.service(4))
        with stack, patch.object(pro_core, 'preflight_online_subtitle', side_effect=RuntimeError('字幕错片')):
            with self.assertRaises(RuntimeError):
                smart.find_verified_english(str(self.movie), 1, lambda _: None, max_candidates=1)
        report = self.report()
        self.assertEqual(report['attempted_candidates'], 1)
        self.assertEqual(report['state_counts']['not_examined'], 3)

    def test_human_1987_offsets_stay_three_points_and_irregular_cuts_still_reject(self):
        points, offsets = [1740.,3630.,4350.], [.43,1.42,1.39]
        data = manual_timeline.observation_report(points, offsets)
        self.assertAlmostEqual(data['variation_seconds'], .99)
        self.assertAlmostEqual(data['endpoint_difference_seconds'], .96)
        self.assertTrue(data['fixed_offset_warning'])
        manual_timeline.validate(points, offsets)
        self.assertEqual(manual_timeline.map_time(1740.,points,offsets), 1740.43)
        self.assertEqual(manual_timeline.map_time(4350.,points,offsets), 4351.39)
        self.assertFalse(manual_timeline.observation_report(points,[0.,0.,0.])['fixed_offset_warning'])
        with self.assertRaises(ValueError):
            manual_timeline.validate(points, [0.,7.,0.])

    def checkpoint(self):
        path=self.root/'audio-passed.srt'
        path.write_text('1\n00:00:01,000 --> 00:00:02,000\nHello there.\n', encoding='utf-8')
        return dict(rule='audio-stage-main-purpose-v1',path=str(path),sha256=subtitle_audit.file_hash(path),
                    movie_signature=batch.media_signature(self.movie),selected_audio_id=1,validation_audio_id=1,
                    provider='Test',release='Movie.2025.BluRay',identity_key='imdb:1',
                    report='连续VAD时间轴核验通过：提议固定偏移 +0.01 秒',
                    seal=smart.verification_seal(str(self.movie),str(path),'Test','Movie.2025.BluRay','imdb:1'))

    def test_saved_audio_source_resumes_only_after_retained_track_acceptance(self):
        checkpoint=self.checkpoint()
        accept=[]
        with patch.object(smart,'filename_container_identity_conflict',return_value=''), \
                patch.object(smart,'PROVIDERS',()), patch.object(pro_core,'preflight_online_subtitle') as match:
            result=smart.find_verified_english(str(self.movie),1,lambda _:None,audio_checkpoint=checkpoint,
                candidate_acceptance=lambda path,cancel:accept.append(str(path)))
        match.assert_not_called()
        self.assertEqual(accept,[checkpoint['path']])
        self.assertEqual(result.subtitle_path,checkpoint['path'])

    def test_saved_audio_source_cannot_bypass_failed_pgs_or_changed_bindings(self):
        checkpoint=self.checkpoint()
        def reject(path,cancel):
            raise pro_core.RetainedSubtitleVerificationError('轨4未核验',{4:{'accepted':False}},path)
        with patch.object(smart,'filename_container_identity_conflict',return_value=''),patch.object(smart,'PROVIDERS',()):
            with self.assertRaises(pro_core.RetainedSubtitleVerificationError):
                smart.find_verified_english(str(self.movie),1,lambda _:None,audio_checkpoint=checkpoint,candidate_acceptance=reject)
        with self.assertRaises(RuntimeError):
            smart._validated_audio_checkpoint(str(self.movie),2,checkpoint)
        Path(checkpoint['path']).write_text('changed',encoding='utf-8')
        with self.assertRaises(RuntimeError):
            smart._validated_audio_checkpoint(str(self.movie),1,checkpoint)

    def test_pgs_regional_conflict_is_marked_without_declaring_its_cause(self):
        audit=subtitle_audit.FilmAudit(self.movie,self.root/'report')
        audit.track_result(4,'retained_image',False,'可信区域偏移冲突',evidence={'local_evidence':{'timing_conflict':True}})
        data,_=audit.write('failed','保留轨尚未核验',lambda _:None)
        self.assertTrue(data['suspected_mixed_timeline'])
        self.assertIn('漂移、跳变或错版',data['mixed_timeline_note'])

    def test_final_header_check_rejects_commentary_even_with_matching_track_counts(self):
        output=self.root/'partial.mkv'
        with output.open('wb') as file:
            file.truncate(1024*1024+1)
        metadata=dict(container={'recognized':True},tracks=[
            dict(id=0,type='video'),dict(id=1,type='audio'),
            dict(id=2,type='subtitles',properties=dict(language='eng',track_name='English (Commentary)'))])
        audit=subtitle_audit.FilmAudit(self.movie,self.root)
        with subtitle_audit.scope(audit),patch.object(core,'inspect_media',return_value=metadata):
            with self.assertRaisesRegex(RuntimeError,'成品字幕用途检查失败'):
                core.validate_media_output(output,metadata,1,1)
        self.assertFalse(audit.output_subtitles[0]['purpose_accepted'])

    def test_final_sdh_header_is_allowed_without_claiming_content_was_sampled(self):
        audit=subtitle_audit.FilmAudit(self.movie,self.root)
        audit.check_output_purposes(dict(tracks=[dict(id=4,type='subtitles',properties=dict(
            language='eng',track_name='English (SDH)'))]))
        self.assertTrue(audit.output_subtitles[0]['purpose_accepted'])
        self.assertEqual(audit.output_subtitles[0]['content_state'],'not_sampled')

    def test_reports_are_isolated_between_concurrent_films(self):
        def run(number):
            audit = subtitle_audit.FilmAudit(self.movie, self.root/str(number))
            with subtitle_audit.scope(audit):
                subtitle_audit.current().add(str(number))
                return subtitle_audit.current().records[0]['label']
        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(list(executor.map(run,[1,2])), ['1','2'])
        self.assertIsNone(subtitle_audit.current())

    def test_title_tech_suffix_and_alias_do_not_bypass_wrong_year_or_id(self):
        from subtitle_identity_guard import clean_video_title
        self.assertEqual(clean_video_title('Predator 4K'), ('Predator',''))
        identity = SimpleNamespace(title='Alien vs Predator', original_title='Alien vs Predator', year='2004', imdb_id='tt0370263')
        candidate = SimpleNamespace(feature_title='AVP: Alien vs Predator', feature_year='2004',
            feature_imdb_id='tt0370263', release='AVP.Alien.vs.Predator.2004', file_name='sub.srt')
        self.assertEqual(smart._candidate_identity_conflict(candidate,identity), '')
        import online_subtitles
        import subdl_subtitles
        self.assertTrue(online_subtitles._candidate_matches_identity(candidate,identity))
        self.assertIn('AVP: Alien vs Predator', online_subtitles._feature_title_variants(identity,strict=True))
        self.assertEqual(subdl_subtitles._movie_title_kind(identity,dict(name='AVP: Alien vs Predator',year='2004')), 'exact')
        self.assertNotEqual(subdl_subtitles._movie_title_kind(identity,dict(name='AVP: Alien vs Predator',year='2007')), 'exact')
        candidate.feature_imdb_id = 'tt0123456'
        self.assertIn('冲突',smart._candidate_identity_conflict(candidate,identity))


if __name__ == '__main__':
    unittest.main()
