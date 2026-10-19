from contextlib import ExitStack
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import batch_core as batch
import continuous_vad as vad
import online_subtitles as online
import profile_model as profiles
import pro_core as p
import smart_subtitles as smart
import subtitle_tool_core as core
from tests.test_audio_reference_policy import shared,track

class ReferenceHandoffTests(unittest.TestCase):
    def test_embedded_earlier_candidate_is_revisited_after_reference_changes(self):
        with tempfile.TemporaryDirectory() as folder,ExitStack() as stack:
            root=Path(folder);movie=root/'Film.2020.mkv';movie.write_bytes(b'media')
            ts=[track(1),track(2),core.Track(3,'subtitles','SubRip/SRT','en','',False,False,True,False,False),
                core.Track(4,'subtitles','SubRip/SRT','en','SDH',False,False,True,False,False)]
            ts[1].default=True
            profile=profiles.PreferenceProfile(1,'test',subtitle_languages=['en'],audio_policy='native')
            with patch.object(batch,'_inspect_media_cached',return_value=({},False)),patch.object(core,'tracks_from_media',return_value=ts):
                plan=batch.analyze_video(str(movie),profile)
            session=vad.ReferenceAudioSession(shared(2),2,ts);calls=[]
            def prepare(*args,verification_status,**kwargs):
                ident=args[2];calls.append((ident,session.validation_audio_id))
                if session.validation_audio_id==2:
                    if ident==4:
                        session.current=shared(1);session.confirmed=True;session.revision=1
                    raise p.SubtitleContentMismatchError('连续VAD：无法确认本候选')
                verification_status['verified_anchor']=True
            for name,kwargs in (
                ('inspect_tracks',dict(return_value=ts)),('begin_ollama_lease',{}),
                ('end_ollama_lease',dict(return_value=False))):
                stack.enter_context(patch.object(core,name,**kwargs))
            stack.enter_context(patch.object(p,'prepare_shared_subtitle_content_audio',return_value=session))
            stack.enter_context(patch.object(p,'prepare_embedded_text_corrections',side_effect=prepare))
            search=stack.enter_context(patch.object(smart,'find_verified_english'))
            process=stack.enter_context(patch.object(p,'process_pro'))
            batch.process_plan(plan,profile,lambda _:None,None)
            search.assert_not_called()
            self.assertEqual(calls,[(3,2),(4,2),(3,1)])
            self.assertEqual(process.call_args.kwargs['embedded_source_id'],3)
            self.assertEqual(process.call_args.kwargs['shared_audio_reference'],Path('audio-1.npz'))

    def test_downloaded_earlier_candidate_reused_with_actual_audio_and_seal(self):
        with tempfile.TemporaryDirectory() as folder,ExitStack() as stack:
            movie=Path(folder)/'Film.2020.mkv';movie.write_bytes(b'media')
            session=vad.ReferenceAudioSession(shared(2),2,[track(1),track(2)])
            candidates=[SimpleNamespace(file_id=str(i),release=f'Film.2020.BluRay.{i}',
                file_name=f'Film-{i}.srt',download_url=f'https://fixture.invalid/{i}.srt',
                language='en',feature_title='Film',feature_year='2020') for i in (1,2)]
            downloads=[];checks=[];accepted=[]
            def download(key,candidate,destination):
                downloads.append(candidate.file_id)
                target=Path(destination)/candidate.file_name;target.parent.mkdir(parents=True,exist_ok=True)
                second=int(candidate.file_id)
                target.write_text(f'1\n00:00:0{second},000 --> 00:00:0{second+1},000\nHello {second}\n')
                return target
            service=SimpleNamespace(load_settings=lambda:{'key':'fixture'},download=download,
                search=lambda *a,**kw:(online.MediaIdentity('Film',year='2020'),candidates,online.SearchMeta()))
            def preflight(video,source,*args,**kw):
                index=Path(source).stem[-1];checks.append(index)
                if len(checks)==2:
                    session.current=shared(1);session.revision=1;session.confirmed=True
                if index=='1' and session.validation_audio_id==1:return Path(source),'passed'
                raise p.SubtitleContentMismatchError('连续VAD：范围外匹配更强')
            stack.enter_context(patch.object(smart,'PROVIDERS',(('Test',service,'key'),)))
            stack.enter_context(patch.object(smart,'filename_container_identity_conflict',return_value=''))
            stack.enter_context(patch.object(p,'prepare_shared_subtitle_content_audio',return_value=session))
            stack.enter_context(patch.object(p,'preflight_online_subtitle',side_effect=preflight))
            result=smart.find_verified_english(str(movie),2,lambda _:None,max_candidates=2,
                candidate_acceptance=lambda aligned,cancel:accepted.append(cancel.validation_audio_id))
            self.assertEqual(downloads,['1','2'])
            self.assertEqual(checks,['1','2','1'])
            self.assertEqual(accepted,[1])
            self.assertEqual(result.validation_audio_id,1)
            self.assertIs(result.shared_audio,session)
            smart.verify_before_processing(str(movie),result.subtitle_path,result.provider,result.release,result.identity_key,result.verification_seal)

    def test_unconfirmed_reference_advice_does_not_only_blame_subtitles(self):
        advice=batch.failure_advice(p.SubtitleContentMismatchError('参照音轨或字幕版本待确认：连续VAD未通过'))
        self.assertIn('选择参照音轨',advice)
        self.assertIn('无需重新搜索',advice)

    def test_unconfirmed_reference_can_keep_raw_candidate_without_fake_support(self):
        import hashlib,json
        from subtitle_identity_guard import media_signature
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);movie=root/'Film.mkv';movie.write_bytes(b'media')
            source=root/'source.srt';source.write_text('1\n00:00:01,000 --> 00:00:02,000\nHello\n')
            ref=root/'speech.npz';ref.write_bytes(b'fingerprint')
            session=vad.ReferenceAudioSession(shared(2),2,[track(1),track(2)])
            session.current=p.SharedSubtitleContentAudio(1800,2,1,'en',None,vad_reference=ref)
            session.checked_references=[ref];session.unconfirmed=True;session.fallback_used=True
            candidate=SimpleNamespace(language='en',identity_key='fixture')
            item=dict(subtitle=source,raw_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                      candidate=candidate,provider='Test',release='Film',directory=root)
            rejection=p.SubtitleContentMismatchError('连续VAD：范围外匹配更强')
            saved=smart._pending_candidate(str(movie),media_signature(movie),item,rejection,session,
                reference_audio_unconfirmed=True)
            evidence=json.loads(saved.evidence_json)
            self.assertEqual(evidence['reasons'],['reference_audio_unconfirmed'])
            self.assertFalse(evidence['sufficient'])
            context=smart._pending_review_context(session,[saved])
            self.assertEqual(context['reason_code'],'reference_audio_unconfirmed')
            plan=profiles.BatchPlan(str(movie),1)
            pending=smart.PendingSubtitleReviewError([saved],context)
            batch._retain_pending_subtitle_review(plan,pending)
            self.assertTrue(plan.manual_review_required)
            self.assertFalse(plan.external_subtitle_verified)
            self.assertEqual(Path(plan.external_subtitle).read_bytes(),source.read_bytes())
            session.confirmed=True
            with self.assertRaises(RuntimeError):
                smart._pending_candidate(str(movie),media_signature(movie),item,rejection,session,
                    reference_audio_unconfirmed=True)

    def test_manual_controller_switch_reprepares_raw_source_and_binds_confirmation(self):
        import json
        import qt_app
        from PySide6.QtWidgets import QApplication
        from tests.test_manual_alignment_entry import ManualAlignmentEntryTest
        app=QApplication.instance() or QApplication([])
        fixture=ManualAlignmentEntryTest('test_three_confirmations_use_raw_baseline_and_reopen_does_not_stack')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.plan.audio_ids=[2]
        calls=[];dialogs=[]
        original_prepare=fixture.prepare
        def prepare(movie,source,work,audio_id,log,**kwargs):
            calls.append((audio_id,kwargs.get('reference_audio_id'),Path(source).read_bytes()))
            evidence=original_prepare(movie,source,work,audio_id,log,**kwargs)
            evidence['validation_audio_id']=audio_id
            evidence['audio_codec']='AAC'
            return evidence
        class Dialog:
            def __init__(self,parent,evidence):
                self.evidence=evidence;self.index=len(dialogs);dialogs.append(self)
                self.requested_audio_id=1 if self.index==0 else None
                self.confirmed_offsets=None if self.index==0 else [.1,.2,.3]
            def exec(self):return 2 if self.index==0 else 1
        with patch('qt_app.manual_review_prepare.prepare_review',side_effect=prepare),patch('qt_app.ManualSubtitleReviewDialog',Dialog):
            fixture.controller._start_batch_manual_review(fixture.path,str(fixture.source),SimpleNamespace(language='en',release='fixture'))
        self.assertEqual([(a,b) for a,b,_ in calls],[(2,None),(1,1)])
        self.assertEqual(calls[0][2],calls[1][2])
        record=json.loads((Path(fixture.plan.external_subtitle).parent/'manual-confirmation.json').read_text(encoding='utf8'))
        self.assertEqual(record['validation_audio_id'],1)
        self.assertEqual(fixture.plan.pending_review_audio_id,1)

    def test_saved_actual_main_reference_is_explicit_even_without_cached_short_audio(self):
        import qt_app
        from PySide6.QtWidgets import QApplication
        from tests.test_manual_alignment_entry import ManualAlignmentEntryTest
        app=QApplication.instance() or QApplication([])
        for reuse in (False,True):
            with self.subTest(reuse_audio=reuse):
                fixture=ManualAlignmentEntryTest('test_three_confirmations_use_raw_baseline_and_reopen_does_not_stack')
                fixture.setUp();self.addCleanup(fixture.doCleanups)
                fixture.plan.audio_ids=[2]
                fixture.plan.pending_review_audio_id=1
                fixture.plan._manual_audio_cache=None
                fixture.controller._pending_review_continuations={fixture.path:None} if reuse else {}
                calls=[];opened=[]
                original_prepare=fixture.prepare
                def prepare(movie,source,work,audio_id,log,**kwargs):
                    calls.append((audio_id,kwargs.get('reference_audio_id')))
                    evidence=original_prepare(movie,source,work,audio_id,log,**kwargs)
                    # Same behavior as real prepare_review: only an explicit
                    # reference can prevent cheap-first selection here.
                    evidence['validation_audio_id']=kwargs.get('reference_audio_id') or 2
                    evidence['audio_codec']='DTS-HD MA'
                    return evidence
                class Dialog:
                    def __init__(self,parent,evidence):opened.append(evidence['validation_audio_id'])
                    def exec(self):return 0
                with patch('qt_app.manual_review_prepare.prepare_review',side_effect=prepare),patch('qt_app.ManualSubtitleReviewDialog',Dialog):
                    fixture.controller._start_batch_manual_review(fixture.path,str(fixture.source),
                        SimpleNamespace(language='en',release='fixture'),reuse_audio=reuse)
                self.assertEqual(calls,[(1,1)])
                self.assertEqual(opened,[1])
                self.assertEqual(fixture.plan.pending_review_audio_id,1)
