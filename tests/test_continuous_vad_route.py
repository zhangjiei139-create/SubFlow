import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import batch_core
import continuous_vad as vad
import manual_review_prepare as review
import manual_timeline as curve
import pro_core as p
import subtitle_tool_core as core


def track(i,language,kind='audio',codec='AAC',name='',default=False,forced=False):
    return core.Track(i,kind,codec,language,name,default,forced,kind=='subtitles',False,False)


class ContinuousVADTests(unittest.TestCase):
    def test_audio_choice_english_easy_decoder_excludes_commentary(self):
        tracks=[track(1,'de',default=True),track(2,'en',codec='TrueHD'),track(3,'en',codec='AAC'),
                track(4,'en',name='Director commentary',default=True)]
        self.assertEqual(vad.audio_track(tracks,1).id,3)
        self.assertEqual(vad.audio_track(tracks[:1],1).id,1)

    def test_text_priority_same_language_then_english_then_other_text_never_image(self):
        audio=track(1,'ja',default=True)
        eng=track(2,'en','subtitles');jap=track(3,'ja','subtitles');partial=track(4,'ja','subtitles',forced=True)
        other=track(6,'zh-CN','subtitles',default=True)
        self.assertEqual([t.id for t in batch_core.ordered_text_anchors([audio,eng,jap,partial,other],audio)], [3,2,6])
        audio_en=track(5,'en')
        self.assertEqual(batch_core.ordered_text_anchors([audio,audio_en,eng,jap],audio)[0].id,2)
        audio_unknown=track(7,'und',default=True)
        self.assertEqual(batch_core.ordered_text_anchors([audio_unknown,other,eng],audio_unknown)[0].id,2)

    def test_other_language_embedded_anchor_is_used_before_download(self):
        from profile_model import PreferenceProfile
        import smart_subtitles
        tracks = [track(1,'en',default=True), track(2,'zh-CN','subtitles')]
        with tempfile.TemporaryDirectory() as td:
            movie=Path(td)/'movie.mkv';movie.write_bytes(b'video')
            profile=PreferenceProfile(1,'test',subtitle_languages=['zh-CN','en'])
            with patch.object(batch_core,'_inspect_media_cached',return_value=({},False)), patch.object(core,'tracks_from_media',return_value=tracks):
                plan=batch_core.analyze_video(str(movie),profile)
            def verified(*args,verification_status,**kwargs):
                verification_status['verified_anchor']=True
            shared=SimpleNamespace(vad_reference=Path(td)/'speech.npz',validation_audio_id=1)
            with patch.object(core,'inspect_tracks',return_value=tracks), patch.object(p,'prepare_shared_subtitle_content_audio',return_value=shared), patch.object(p,'prepare_embedded_text_corrections',side_effect=verified), patch.object(smart_subtitles,'find_verified_english') as search, patch.object(p,'process_pro') as process, patch.object(core,'begin_ollama_lease'), patch.object(core,'end_ollama_lease',return_value=False):
                batch_core.process_plan(plan,profile,lambda _:None,None)
            search.assert_not_called()
            self.assertEqual(process.call_args.kwargs['embedded_source_id'],2)
            self.assertEqual(process.call_args.kwargs['source_mode'],'embedded')
            self.assertTrue(process.call_args.kwargs['allow_reverse_translation'])

    def test_failed_embedded_anchor_downloads_even_without_translation(self):
        from profile_model import PreferenceProfile
        import smart_subtitles
        tracks=[track(1,'en',default=True),track(2,'en','subtitles')]
        with tempfile.TemporaryDirectory() as td:
            movie=Path(td)/'movie.mkv';movie.write_bytes(b'video')
            profile=PreferenceProfile(1,'test',subtitle_languages=['en'])
            with patch.object(batch_core,'_inspect_media_cached',return_value=({},False)),patch.object(core,'tracks_from_media',return_value=tracks):
                plan=batch_core.analyze_video(str(movie),profile)
            self.assertFalse(plan.missing_subtitle_languages)
            shared=SimpleNamespace(vad_reference=Path(td)/'speech.npz',validation_audio_id=1)
            with patch.object(core,'inspect_tracks',return_value=tracks),patch.object(p,'prepare_shared_subtitle_content_audio',return_value=shared),patch.object(p,'prepare_embedded_text_corrections'),patch.object(smart_subtitles,'find_verified_english',side_effect=RuntimeError('no candidate')) as search,patch.object(p,'process_pro') as process:
                with self.assertRaisesRegex(RuntimeError,'没有可核验'):
                    batch_core.process_plan(plan,profile,lambda _:None,None)
            search.assert_called_once()
            process.assert_not_called()

    def test_incomplete_subtitle_stops_before_vad_preparation(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'partial.srt';src.write_text('1\n00:00:01,000 --> 00:00:02,000\nOnly a fragment\n')
            with patch.object(core,'inspect_media',return_value={}),patch.object(core,'video_track_duration_ns',return_value=7200*10**9),patch.object(vad,'shared_audio') as prepare:
                with self.assertRaisesRegex(p.SubtitleContentMismatchError,'不完整'):
                    vad.preflight('movie.mkv',src,Path(td)/'work',1,lambda _:None)
            prepare.assert_not_called()

    def test_vad_is_shared_across_threads_and_old_cache_invalidated(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);movie=root/'movie.mkv';movie.write_bytes(b'movie')
            exe=root/'ffsubsync.exe';exe.write_bytes(b'engine')
            aud=track(1,'en');cache=root/'cache';d=cache/'audio-1';d.mkdir(parents=True)
            (d/'shared-alignment-speech.npz').write_bytes(b'old'*100)
            (d/'shared-alignment-speech.json').write_text(json.dumps({'segment_count':10}))
            calls=[]
            def run(args,*a,**kw):
                calls.append(args);time.sleep(.05)
                Path(args[1]).with_suffix('.npz').write_bytes(b'continuous'*100)
            with patch.object(core,'inspect_tracks',return_value=[aud]),patch.object(core,'inspect_media',return_value={}),patch.object(p,'_audio_stream_index',return_value=0),patch.object(p,'_tool',return_value=str(exe)),patch.object(p,'_run',side_effect=run),patch.object(vad,'cache_directory',return_value=cache):
                with concurrent.futures.ThreadPoolExecutor(2) as pool:
                    results=list(pool.map(lambda _:vad.shared_audio(str(movie),1,lambda x:None),range(2)))
            self.assertEqual(len(calls),1)
            self.assertNotIn('--multi-segment-sync',calls[0])
            self.assertIn('48000',calls[0])
            self.assertEqual(results[0].vad_reference,results[1].vad_reference)
            self.assertIsNone(results[0].fingerprint)

    def test_manual_online_and_embedded_use_same_vad_without_whisper(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);video=root/'movie.mkv';video.write_bytes(b'video')
            source=root/'sub.srt';source.write_text('1\n00:00:30,000 --> 00:00:32,000\n你好，这是一句中文\n',encoding='utf8')
            ref=root/'speech.npz';ref.write_bytes(b'fake-cache')
            shared=SimpleNamespace(duration_seconds=60,validation_audio_id=1,audio_stream_index=0,audio_language='en',vad_reference=ref)
            def align(movie,normalized,out,*a,diagnostics,**kw):
                p._shifted_srt(normalized,out,-230)
                diagnostics.update(score=100,offset_seconds=-.23,raw_offset_seconds=-.23)
            with patch.object(vad,'shared_audio',return_value=shared),patch.object(p,'_alignment_candidate',side_effect=align) as run,patch.object(p.audio_offset_verifier,'build_movie_audio_fingerprint',side_effect=AssertionError('Whisper called')):
                a=p.preflight_online_subtitle(str(video),str(source),str(root/'online'),1,shared,lambda _:None,source_language='zh-CN')
                b=p.preflight_external_subtitle(str(video),str(source),str(root/'manual'),1,lambda _:None,source_language='zh-CN',manual_reference_first=True)
                c=p.align_embedded_text_track(str(video),source,root/'embedded',1,'zh-CN',2,lambda _:None,shared_content_audio=shared)
                self.assertEqual([core.parse_subtitle(x[0])[0].start for x in (a,b,c)],['00:00:29,770']*3)
                self.assertTrue(all(p._timeline_report_verified(x[1]) for x in (a,b,c)))
                # Strict input hash cache: no second application or new execution.
                p.preflight_external_subtitle(str(video),str(source),str(root/'manual'),1,lambda _:None,source_language='zh-CN')
                self.assertEqual(run.call_count,3)
                source.write_text(source.read_text(encoding='utf8').replace('30,000','31,000'),encoding='utf8')
                p.preflight_external_subtitle(str(video),str(source),str(root/'manual'),1,lambda _:None,source_language='zh-CN')
                self.assertEqual(run.call_count,4)

    def test_boundary_goes_to_manual_without_old_guard(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);video=root/'movie';video.write_bytes(b'v');src=root/'sub.srt';src.write_text('1\n00:00:30,000 --> 00:00:32,000\nSome dialogue\n')
            ref=root/'speech.npz';ref.write_bytes(b'cache');shared=SimpleNamespace(duration_seconds=60,validation_audio_id=1,vad_reference=ref,audio_language='en')
            def align(m,n,o,*a,diagnostics,**kw):
                o.write_bytes(n.read_bytes());diagnostics.update(score=1,offset_seconds=9.99,raw_offset_seconds=9.99)
            with patch.object(p,'_alignment_candidate',side_effect=align):
                with self.assertRaises(p.SubtitleContentMismatchError):
                    vad.preflight(str(video),str(src),root/'work',1,lambda _:None,shared=shared)
                self.assertFalse((root/'work/vad-verdict.json').exists())


class ThreePointTests(unittest.TestCase):
    def test_constant_piecewise_and_endpoint_behavior(self):
        x=[100,1000,2000]
        for t in (0,100,500,1000,1500,2000,3000):
            self.assertAlmostEqual(curve.map_time(t,x,[-2]*3),t-2)
        d=[-1,-3,1]
        self.assertAlmostEqual(curve.map_time(550,x,d),548)
        self.assertAlmostEqual(curve.map_time(1500,x,d),1499)
        self.assertEqual(curve.map_time(3000,x,d),3001)
        with self.assertRaisesRegex(ValueError, '中点明显'):
            curve.validate(x,d)
        curve.validate(x,[-1, -.1, .9])

    def test_both_cue_boundaries_use_original_timeline(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'src.srt';out=Path(td)/'out.srt'
            src.write_text('1\n00:01:00,000 --> 00:01:10,000\nLine\n')
            curve.apply_to_srt(src,out,[0,100,200],[0,10,20])
            event=core.parse_subtitle(out)[0]
            self.assertEqual((event.start,event.end),('00:01:06,000','00:01:17,000'))
            original=src.read_bytes()
            curve.apply_to_srt(src,out,[0,100,200],[0,10,20])
            self.assertEqual(src.read_bytes(),original)
            self.assertEqual(core.parse_subtitle(out)[0],event)

    def test_human_confirmation_not_vetoed_by_whisper(self):
        ev=dict(clips=[dict(region=i,subtitle_time=i*1000+30) for i in (1,2,3)])
        self.assertEqual(review.manual_offsets_rejection(ev,[-13.5,-.75,12]),'')
        self.assertTrue(review.manual_offsets_rejection(ev,[0,0,21]))
        self.assertIn('中点明显', review.manual_offsets_rejection(ev,[-13.5,3,12]))

    def test_negative_start_clips_without_losing_text(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'a.srt';out=Path(td)/'b.srt';src.write_text('1\n00:00:01,000 --> 00:00:02,000\nEarly line\n')
            curve.apply_to_srt(src,out,[10,100,200],[-5]*3)
            e=core.parse_subtitle(out)[0]
            self.assertEqual(e.start,'00:00:00,000');self.assertEqual(e.end,'00:00:00,001');self.assertEqual(e.text,'Early line')


if __name__=='__main__':unittest.main()
