"""Omit an unalignable retained track only after a reliable source exists."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import batch_core as batch
import pgs_local_alignment as alignment
import pro_core as p
import smart_subtitles as smart
import subtitle_audit
import subtitle_tool_core as core
from profile_model import BatchPlan, PreferenceProfile


class RetainedReplacementTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.movie=self.root/'Movie.2018.mkv';self.movie.write_bytes(b'original movie')
        self.reference=self.root/'verified.srt'
        self.events=[core.SubtitleEvent(core.srt_time_from_milliseconds((10+i*10)*1000),
            core.srt_time_from_milliseconds((12+i*10)*1000),f'This is the original dialogue number {i}.') for i in range(30)]
        core.write_srt(self.reference,self.events,{i:e.text for i,e in enumerate(self.events,1)})
        self.tracks=[core.Track(1,'audio','AAC','eng','',True,False,False,False,False),
            core.Track(3,'subtitles','SubRip','eng','English',False,False,True,False,False),
            core.Track(4,'subtitles','HDMV PGS','chi','简体中文',True,False,False,True,False),
            core.Track(5,'subtitles','HDMV PGS','deu','German',False,False,False,True,False)]
        self.logs=[]
        self.bad=dict(accepted=False,failure_kind='outside_automatic_offset_range',
                      proposed_offset_seconds=65.6,score=.70,reason='相对标轴+65.60秒，超过自动纠偏范围')

    def context(self):
        stack=ExitStack()
        stack.enter_context(patch.object(core,'inspect_tracks',side_effect=lambda *_:self.tracks))
        stack.enter_context(patch.object(core,'tracks_from_media',side_effect=lambda *_:self.tracks))
        stack.enter_context(patch.object(core,'inspect_media',return_value={}))
        stack.enter_context(patch.object(core,'video_track_duration_ns',return_value=400*10**9))
        return stack

    def issue(self,decisions,full=None,replace=True):
        status={}
        with self.context(),patch.object(p,'_pgs_local_corrections',return_value=decisions), \
                patch.object(p,'_embedded_image_intervals',return_value=full or []) as read, \
                patch.object(p,'_interval_alignment',return_value=(.70,65.6)):
            result=p.require_image_timeline_anchor(str(self.movie),[4,5],1,str(self.root/'work'),
                self.reference,self.logs.append,prepared_image_status=status,replace_unalignable=replace)
        return status,result,read

    def good(self):
        return alignment.LocalAlignmentResult(True,0.,0.,'supported_fixed_shift')

    def conflict(self):
        return alignment.LocalAlignmentResult(False,None,None,'regional_offset_conflict',
                                              timing_conflict=True,regional_offsets=(0.,2.,0.))

    def test_confirmed_conflict_is_replaced_and_successful_track_handoff_survives(self):
        status,results,read=self.issue({1:self.conflict(),2:self.good()})
        read.assert_not_called()
        self.assertEqual(set(status['replacement_tracks']),{4})
        self.assertFalse(results[4]['accepted'])
        self.assertEqual(status['prepared_image_timing'].image_track_ids,(5,))
        self.assertTrue(results[5]['accepted'])

    def test_supported_offset_outside_range_uses_replacement_without_widening_range(self):
        status,results,read=self.issue({2:self.good()},[(i*10.,i*10.+2.) for i in range(30)])
        read.assert_called_once()
        self.assertEqual(results[4]['failure_kind'],'outside_automatic_offset_range')
        self.assertEqual(set(status['replacement_tracks']),{4})
        self.assertEqual(status['prepared_image_timing'].offsets,((5,0,'pgs-local-fixed-v1'),))

    def test_unknown_track_still_blocks_even_when_another_track_can_be_replaced(self):
        with self.assertRaises(p.RetainedSubtitleVerificationError) as caught:
            self.issue({1:self.conflict()})
        self.assertEqual(caught.exception.results[5]['failure_kind'],'insufficient_evidence')

    def test_strict_caller_does_not_implicitly_authorize_replacement(self):
        with self.assertRaises(p.RetainedSubtitleVerificationError):
            self.issue({1:self.conflict(),2:self.good()},replace=False)

    def test_movie_changed_during_check_cannot_authorize_dropping_a_track(self):
        def mutate(*args,**kwargs):
            self.movie.write_bytes(b'changed original movie')
            return {1:self.conflict(),2:self.good()}
        with self.context(),patch.object(p,'_pgs_local_corrections',side_effect=mutate):
            with self.assertRaises(p.RetainedSubtitleVerificationError):
                p.require_image_timeline_anchor(str(self.movie),[4,5],1,str(self.root/'work'),
                    self.reference,self.logs.append,prepared_image_status={},replace_unalignable=True)

    def batch_run(self,search,process):
        plan=BatchPlan(str(self.movie),1,status='ready',audio_ids=[1],subtitle_ids=[4,5],
            output_path=str(self.root/'Movie.SF.mkv'),has_image_subtitles=True,has_retained_image_subtitles=True)
        self.latest_plan=plan
        profile=PreferenceProfile(1,'test',subtitle_languages=['zh-CN','de','en'])
        with self.context(),patch.object(batch,'ordered_text_anchors',return_value=[]), \
                patch.object(smart,'find_verified_english',side_effect=search) as find, \
                patch.object(p,'process_pro',side_effect=process) as call:
            batch.process_plan(plan,profile,self.logs.append,None)
        return plan,find,call

    def result(self,path=None):
        return SimpleNamespace(subtitle_path=str(path or self.reference),language='en',report='verified',
            provider='test',release='Movie.2018',identity_key='imdb:test',verification_seal='seal')

    def test_batch_adopts_passed_text_once_removes_bad_pgs_and_adds_missing_language(self):
        def search(*args,**kwargs):
            kwargs['candidate_acceptance'](self.reference,None)
            return self.result()
        with patch.object(p,'_pgs_local_corrections',return_value={1:self.conflict(),2:self.good()}):
            plan,find,call=self.batch_run(search,lambda **kwargs:None)
        find.assert_called_once()
        kwargs=call.call_args.kwargs
        self.assertEqual(kwargs['keep_subtitle_ids'],[5])
        self.assertEqual(kwargs['target_codes'],['zh-CN'])
        self.assertTrue(kwargs['include_external_source_in_output'])
        self.assertEqual(kwargs['prepared_image_timing'].image_track_ids,(5,))
        self.assertEqual(plan.subtitle_ids,[5])
        self.assertIn('zh-CN',plan.missing_subtitle_languages)
        persisted=batch.process_log_path(plan).read_text('utf-8')
        self.assertIn('原字幕轨 4（简体中文 / PGS）',persisted)
        self.assertIn('非固定偏移，无法统一纠偏',persisted)
        self.assertIn('test · Movie.2018',persisted)
        self.assertIn(str(self.reference),persisted)
        self.assertIn('按所选语言生成简体中文字幕',persisted)
        self.assertIn('内嵌字幕替换完成',persisted)
        self.assertNotIn('没有找到可核验的英文',persisted)

    def test_changed_final_candidate_cannot_inherit_prior_replacement(self):
        def search(*args,**kwargs):
            kwargs['candidate_acceptance'](self.reference,None)
            changed=self.root/'different.srt'
            changed.write_text(self.reference.read_text('utf-8')+'changed','utf-8')
            return self.result(changed)
        with patch.object(p,'_pgs_local_corrections',return_value={1:self.conflict(),2:self.good()}):
            with self.assertRaises(p.SubtitleVerificationToolError):
                self.batch_run(search,lambda **kwargs:self.fail('must not process a changed source'))

    def test_all_image_tracks_replaced_log_does_not_blame_preferences_or_discard_source(self):
        def search(*args,**kwargs):
            kwargs['candidate_acceptance'](self.reference,None)
            return self.result()
        with patch.object(p,'_pgs_local_corrections',return_value={1:self.conflict(),2:self.conflict()}):
            plan,_,call=self.batch_run(search,lambda **kwargs:None)
        self.assertEqual(call.call_args.kwargs['keep_subtitle_ids'],[])
        self.assertTrue(call.call_args.kwargs['include_external_source_in_output'])
        persisted=batch.process_log_path(plan).read_text('utf-8')
        self.assertIn('原轨失败不触发重新搜索英文',persisted)
        self.assertIn('本次输出不包含原图片字幕',persisted)
        self.assertNotIn('当前偏好未保留这些轨道',persisted)
        self.assertNotIn('不会因而自动写入成品',persisted)

    def formal_run(self,*,same_language=False,translate_error=False,trust=True,conversions=None):
        output=self.root/'formal.mkv';output.write_bytes(b'previous successful output')
        state={};generated=[]
        if same_language:
            self.tracks.append(core.Track(6,'subtitles','SubRip','chi','简体中文',False,False,True,False,False))
        kept=[4,5]+([6] if same_language else [])
        def prepare(*args,**kwargs):
            kwargs['verification_status'].update(verified_anchor=trust,image_results={4:self.bad,5:dict(accepted=True)})
            return {4:0,5:0},{},None
        def translate(events,destination,cache,target,*args,**kwargs):
            if translate_error:
                raise RuntimeError('translation fixture failed')
            core.write_srt(destination,events,{i:f'译文测试 {i}' for i,_ in enumerate(events,1)})
        def mux(input_path,partial,audio_ids,subtitle_ids,subtitles,*args,**kwargs):
            generated.extend(subtitles)
            self.assertNotIn(4,subtitle_ids) if trust else self.assertIn(4,subtitle_ids)
            Path(partial).write_bytes(b'new valid output fixture')
        stack=self.context()
        stack.enter_context(patch.object(p,'prepare_embedded_text_corrections',side_effect=prepare))
        stack.enter_context(patch.object(core,'ensure_output_disk_space'))
        stack.enter_context(patch.object(core,'ensure_mux_disk_space'))
        stack.enter_context(patch.object(core,'needs_model_translation',return_value=False))
        translations=stack.enter_context(patch.object(core,'translate_events',side_effect=translate))
        mux_call=stack.enter_context(patch.object(core,'mux_video',side_effect=mux))
        stack.enter_context(patch.object(core,'validate_media_output'))
        stack.enter_context(patch.object(p,'_external_timeline_for_processing',return_value=self.reference))
        with stack:
            kwargs=dict(input_path=str(self.movie),output_path=str(output),keep_audio_ids=[1],keep_subtitle_ids=kept,
                source_mode='external',embedded_source_id=None,external_subtitle=str(self.reference),audio_source_id=1,
                speech_language='en',source_language='en',target_codes=[],work_dir=str(self.root/'formal-work'),
                log=self.logs.append,parallel_targets=1,cancel_event=None,trust_external_original_timeline=trust,
                verified_timeline_reference=str(self.reference) if trust else None,
                include_external_source_in_output=False,subtitle_language_preferences=['zh-CN','de','en'],
                local_chinese_conversion_sources=conversions,retained_replacement_status=state)
            if translate_error:
                with self.assertRaisesRegex(RuntimeError,'翻译中断'):
                    p.process_pro(**kwargs)
            else:
                p.process_pro(**kwargs)
        return output,state,generated,translations,mux_call

    def test_formal_stage_adds_chinese_translation_on_verified_source_timeline(self):
        output,state,generated,translations,_=self.formal_run()
        self.assertEqual(state['keep_subtitle_ids'],[5])
        self.assertEqual(state['target_codes'],['zh-CN'])
        self.assertEqual([g[1] for g in generated],['en','zh-CN'])
        translated=core.parse_subtitle(next(g[0] for g in generated if g[1]=='zh-CN'))
        self.assertEqual([(e.start,e.end) for e in translated],[(e.start,e.end) for e in self.events])
        translations.assert_called_once()
        self.assertEqual(output.read_bytes(),b'new valid output fixture')
        self.assertEqual(self.movie.read_bytes(),b'original movie')

    def test_other_good_chinese_track_prevents_duplicate_translation(self):
        _,state,generated,translations,_=self.formal_run(same_language=True)
        self.assertEqual(state['keep_subtitle_ids'],[5,6])
        self.assertEqual(state['target_codes'],[])
        self.assertEqual([g[1] for g in generated],['en'])
        translations.assert_not_called()

    def test_dropped_track_is_not_used_for_local_chinese_conversion(self):
        _,state,_,_,_=self.formal_run(conversions={'zh-CN':4})
        self.assertEqual(state['local_chinese_conversion_sources'],{})
        self.assertEqual(state['target_codes'],['zh-CN'])

    def test_without_reliable_anchor_no_track_is_automatically_dropped(self):
        _,state,_,translations,_=self.formal_run(trust=False)
        self.assertEqual(state,{})
        translations.assert_not_called()

    def test_translation_failure_does_not_publish_or_overwrite_prior_output(self):
        output,_,_,_,mux=self.formal_run(translate_error=True)
        mux.assert_not_called()
        self.assertEqual(output.read_bytes(),b'previous successful output')

    def test_report_only_claims_replacement_after_successful_completion(self):
        for outcome,expected in [('failed','replacement_planned'),('completed','replaced')]:
            audit=subtitle_audit.FilmAudit(self.movie,self.root/outcome)
            first=len(self.logs)
            with subtitle_audit.scope(audit):
                p._record_retained_replacement([4],{4:self.bad},self.logs.append,
                    tracks=self.tracks,reference_path=self.reference,
                    reference_label='Test · Movie.2018',target_codes=['zh-CN'])
                data,report=audit.write(outcome,'fixture outcome',self.logs.append)
            row=next(r for r in data['records'] if r.get('track_id')==4)
            self.assertEqual(row['state'],expected)
            self.assertFalse(row['retained'])
            self.assertEqual(row['replacement']['source_path'],str(self.reference))
            self.assertEqual(row['replacement']['target_codes'],['zh-CN'])
            text=Path(report).read_text('utf-8')
            self.assertIn('替换来源：Test · Movie.2018',text)
            self.assertIn('补齐语言：简体中文',text)
            self.assertIn(str(self.reference),text)
            current='\n'.join(self.logs[first:])
            if outcome=='completed':
                self.assertIn('内嵌字幕替换完成',current)
                self.assertNotIn('内嵌字幕替换未完成',current)
            else:
                self.assertIn('内嵌字幕替换未完成',current)
                self.assertNotIn('内嵌字幕替换完成',current)

    def test_failure_and_stop_write_unfinished_replacement_to_persistent_log(self):
        def search(*args,**kwargs):
            kwargs['candidate_acceptance'](self.reference,None)
            return self.result()
        for error in (RuntimeError('翻译中断 fixture'),core.CancelledError('用户停止')):
            def fail(**kwargs):
                raise error
            with patch.object(p,'_pgs_local_corrections',return_value={1:self.conflict(),2:self.good()}):
                with self.assertRaises(type(error)):
                    self.batch_run(search,fail)
            persisted=batch.process_log_path(self.latest_plan).read_text('utf-8')
            self.assertIn('内嵌字幕替换未完成',persisted)
            self.assertIn('test · Movie.2018',persisted)
            self.assertNotIn('内嵌字幕替换完成',persisted)

    def test_formal_replacement_logs_source_language_and_omission_reason(self):
        self.formal_run()
        text='\n'.join(self.logs)
        self.assertIn('原字幕轨 4（简体中文 / PGS）',text)
        self.assertIn('超出当前自动纠偏范围',text)
        self.assertIn('+65.60',text)
        self.assertIn('内嵌字幕替换来源：已验证外挂文本',text)
        self.assertIn(str(self.reference),text)
        self.assertIn('按所选语言生成简体中文字幕',text)
        self.assertNotIn('按所选语言生成英文',text)

    def test_embedded_verified_source_can_replace_bad_english_without_translating_it(self):
        self.tracks[2]=core.Track(4,'subtitles','HDMV PGS','eng','English',False,False,False,True,False)
        def prepare(*args,**kwargs):
            kwargs['verification_status'].update(verified_anchor=True,track_id=3,
                image_results={4:self.bad,5:dict(accepted=True)})
            return {3:0,4:0,5:0},{3:self.reference},3
        state={}
        with self.context(),patch.object(p,'prepare_embedded_text_corrections',side_effect=prepare), \
                patch.object(core,'process_video',return_value='completed') as process:
            p.process_pro(str(self.movie),str(self.root/'embedded.mkv'),[1],[4,5],'none',3,None,1,
                'en','en',[],str(self.root/'work'),self.logs.append,1,None,
                subtitle_language_preferences=['en','de'],retained_replacement_status=state)
        self.assertEqual(process.call_args.args[3],[5,3])
        self.assertEqual(process.call_args.args[4],3)
        self.assertEqual(process.call_args.args[5],[])
        self.assertEqual(state['target_codes'],[])

    def test_chinese_equivalence_reuses_surviving_requested_script(self):
        self.tracks.append(core.Track(6,'subtitles','SubRip','chi','繁体中文',False,False,True,False,False))
        self.assertEqual(p._replacement_targets(self.tracks,[5,6],['zh-CN','de'],'en',True),[])
        self.assertEqual(p._replacement_targets(self.tracks,[5,6],['zh-CN','de'],'en',False),['zh-CN'])

    def test_offline_tracks_only_route_does_not_discard_unchecked_tracks(self):
        with patch.object(p,'prepare_embedded_text_corrections') as prepare, \
                patch.object(p,'process_tracks_only',return_value='completed') as process:
            p.process_pro(str(self.movie),str(self.root/'offline.mkv'),[1],[4,5],'none',None,None,1,
                'en','en',[],str(self.root/'work'),self.logs.append,1,None,
                trust_embedded_original_timeline=True,subtitle_language_preferences=['zh-CN','de'])
        prepare.assert_not_called()
        self.assertEqual(process.call_args.args[3],[4,5])


if __name__=='__main__':
    unittest.main()
