"""Manual short-window decoder/cache contracts; no real media or tools."""
import concurrent.futures
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import wave
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import continuous_vad
import manual_review_prepare as review
import pro_core as p
import subtitle_tool_core as core

REAL_DECODER_OPTIONS=p.vad_decoder_options
REAL_DECODE_MODE=p.vad_decode_mode
REAL_WINDOW_INPUT_OPTIONS=p.vad_window_input_options


def policy(codec):
    value=codec.lower()
    if 'dts' in value or value=='dca': return ['-core_only','1']
    if 'truehd' in value or 'mlp' in value: return ['-downmix','stereo']
    return []


def mode(codec):
    opts=policy(codec)
    return 'dts-core' if '-core_only' in opts else 'truehd-stereo' if '-downmix' in opts else 'full'


def pcm(path,duration):
    with wave.open(str(path),'wb') as audio:
        audio.setparams((1,2,16000,0,'NONE','not compressed'))
        audio.writeframes(b'\0\0'*round(duration*16000))


class ManualShortAudioPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.movie=self.root/'movie.mkv';self.movie.write_bytes(b'movie-signature')
        self.ffmpeg=self.root/'ffmpeg.exe';self.ffmpeg.write_bytes(b'decoder-v1')
        self.engine=self.root/'ffsubsync.exe';self.engine.write_bytes(b'vad-v1')
        self.events=[core.SubtitleEvent('00:01:42,000','00:01:44,000','Original subtitle')]
        self.calls=[];self.logs=[];self.video_start=0.0
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(core,'FFMPEG',str(self.ffmpeg)))
        # Root owns these shared helpers. Test this module's delegation and
        # option ordering independently of the helpers' implementation tests.
        self.decoder=self.stack.enter_context(patch.object(p,'vad_decoder_options',side_effect=policy,create=True))
        self.decode_mode=self.stack.enter_context(patch.object(p,'vad_decode_mode',side_effect=mode,create=True))
        self.seek=self.stack.enter_context(patch.object(p,'vad_window_input_options',side_effect=self.input_options,create=True))
        self.run=self.stack.enter_context(patch.object(review,'run_limited',side_effect=self.tool))

    def input_options(self,movie,start):
        return ['-seek_timestamp','1','-ss',f'{self.video_start+start:.6f}']

    def tool(self,args,deadline,limit,description):
        self.calls.append(list(args))
        if args[0]==str(self.engine):
            np.savez_compressed(Path(args[1]).with_suffix('.npz'),speech=np.ones(6000,dtype=np.int8))
        elif 'atempo=0.5' in args:
            pcm(Path(args[-1]),120)
        else:
            pcm(Path(args[-1]),60)

    def prepare(self,name='review-1',codec='TrueHD Atmos',audio_id=7,index=1,start=100,deadline=None):
        work=self.root/name;work.mkdir(exist_ok=True)
        return review.prepare_window(str(self.movie),self.events,work,1,
            dict(start=float(start),cue_count=10,gap_count=3),index,str(self.engine),
            self.logs.append,deadline,audio_id=audio_id,audio_codec=codec)

    def test_decoder_options_are_input_only_on_a_single_60_second_window(self):
        for number,codec in enumerate(('DTS-HD Master Audio','dca','TrueHD Atmos','MLP','AC-3','FLAC')):
            with self.subTest(codec=codec):
                self.calls.clear()
                clip=self.prepare(f'codec-{number}',codec=codec)
                decode,vad,slow=self.calls
                self.decoder.assert_called_with(codec)
                for token in policy(codec):
                    self.assertLess(decode.index(token),decode.index('-i'))
                self.assertEqual(decode[decode.index('-map')+1],'0:a:1')
                self.assertEqual(decode[decode.index('-t')+1],'60')
                self.assertLess(decode.index('-ss'),decode.index('-i'))
                self.assertEqual(decode[decode.index('-af')+1],'aresample=16000:async=1:first_pts=0')
                self.assertNotIn('pan=mono|c0=FC',decode)
                self.assertEqual(vad[1],clip['audio'])
                self.assertNotEqual(vad[1],str(self.movie))
                self.assertEqual(vad[vad.index('--ffmpeg-path')+1],str(self.ffmpeg.parent))
                self.assertNotIn('--multi-segment-sync',vad)
                self.assertFalse(any(t in slow for t in ('-downmix','-core_only')))
                self.assertEqual(clip['duration'],60)
                self.assertEqual(clip['decode_mode'],mode(codec))

    def test_cache_reuses_all_short_artifacts_but_refreshes_subtitle_rows(self):
        first=self.prepare()
        self.events=[core.SubtitleEvent('00:01:42,000','00:01:44,000','Different candidate subtitle')]
        second=self.prepare('review-2')
        self.assertEqual(len(self.calls),3)
        self.assertFalse(first['short_audio_cache_hit'])
        self.assertTrue(second['short_audio_cache_hit'])
        self.assertEqual(second['rows'][0]['text'],'Different candidate subtitle')
        self.assertNotEqual(first['audio'],second['audio'])
        self.assertEqual(Path(first['audio']).read_bytes(),Path(second['audio']).read_bytes())
        original=Path(first['audio']).read_bytes()
        Path(second['audio']).write_bytes(b'changed-only-this-review')
        self.assertEqual(Path(first['audio']).read_bytes(),original)
        self.assertEqual(len(list(self.root.glob('movie_pro_work/manual-window-cache/*/manifest.json'))),1)

    def test_movie_track_codec_window_tools_rule_and_origin_invalidate_cache(self):
        self.prepare()
        changed=[
            dict(audio_id=8),dict(index=0),dict(codec='DTS-HD MA'),dict(start=130),
        ]
        for number,values in enumerate(changed):
            count=len(self.calls)
            clip=self.prepare(f'changed-{number}',**values)
            self.assertFalse(clip['short_audio_cache_hit'])
            self.assertEqual(len(self.calls),count+3)
        for label,path in (('movie',self.movie),('engine',self.engine),('ffmpeg',self.ffmpeg)):
            path.write_bytes(path.read_bytes()+b'new')
            count=len(self.calls)
            self.assertFalse(self.prepare('change-'+label)['short_audio_cache_hit'])
            self.assertEqual(len(self.calls),count+3)
        self.video_start=1.25
        self.assertFalse(self.prepare('origin-changed')['short_audio_cache_hit'])
        with patch.object(review,'SHORT_AUDIO_RULE','next-coordinate-rule'):
            self.assertFalse(self.prepare('rule-changed')['short_audio_cache_hit'])

    def test_nonzero_container_origin_is_seeked_once_without_manual_offset(self):
        self.video_start=2.5
        clip=self.prepare()
        decode=self.calls[0]
        self.assertEqual(decode[decode.index('-seek_timestamp')+1],'1')
        self.assertEqual(decode[decode.index('-ss')+1],'102.500000')
        self.assertEqual((clip['movie_start'],clip['subtitle_time'],clip['initial_offset']),(100,130,0))
        self.assertEqual(clip['timeline'],'video-relative')
        self.assertEqual(clip['rows'][0]['start'],102)

    def test_real_shared_helpers_decode_policy_and_video_origin_are_used(self):
        media={'tracks':[
            {'id':0,'type':'video','properties':{'minimum_timestamp':2500000000}},
            {'id':7,'type':'audio','properties':{'minimum_timestamp':9000000000}},
        ]}
        with patch.object(p,'vad_decoder_options',side_effect=REAL_DECODER_OPTIONS), \
             patch.object(p,'vad_decode_mode',side_effect=REAL_DECODE_MODE), \
             patch.object(p,'vad_window_input_options',side_effect=REAL_WINDOW_INPUT_OPTIONS), \
             patch.object(core,'inspect_media',return_value=media):
            for number,codec in enumerate(('TrueHD Atmos','MLP','DTS-HD Master Audio','dca','FLAC')):
                self.calls.clear()
                clip=self.prepare('real-helper-'+str(number),codec=codec)
                decode=self.calls[0]
                self.assertEqual(decode[decode.index('-ss')+1],'102.500000')
                for token in policy(codec):
                    self.assertLess(decode.index(token),decode.index('-i'))
                self.assertEqual(clip['decode_mode'],mode(codec))
                self.assertEqual(clip['movie_start'],100)
                self.assertEqual(clip['initial_offset'],0)

    def test_mtime_change_invalidates_same_size_movie(self):
        self.prepare()
        stat=self.movie.stat()
        os.utime(self.movie,ns=(stat.st_atime_ns,stat.st_mtime_ns+1000000))
        self.assertFalse(self.prepare('mtime-changed')['short_audio_cache_hit'])
        self.assertEqual(len(self.calls),6)

    def test_corrupt_cache_is_regenerated_not_shown(self):
        self.prepare()
        cache=next(self.root.glob('movie_pro_work/manual-window-cache/*/audio.wav'))
        cache.write_bytes(b'broken')
        result=self.prepare('review-2')
        self.assertFalse(result['short_audio_cache_hit'])
        self.assertEqual(len(self.calls),6)
        with wave.open(result['audio'],'rb') as wav:
            self.assertEqual(wav.getnframes(),960000)

    def test_failed_or_cancelled_preparation_does_not_publish_cache(self):
        def fail(args,*others):
            if args[0]==str(self.engine): raise core.CancelledError('cancelled')
            pcm(Path(args[-1]),60)
        with patch.object(review,'run_limited',side_effect=fail):
            with self.assertRaises(core.CancelledError): self.prepare()
        self.assertFalse(list(self.root.glob('movie_pro_work/manual-window-cache/*/manifest.json')))
        stop=threading.Event();stop.set()
        self.calls.clear()
        with self.assertRaises(core.CancelledError): self.prepare('already-stopped',deadline=stop)
        self.assertFalse(self.calls)

    def test_cache_write_failure_does_not_discard_ready_review(self):
        with patch.object(review,'_write_short_cache',side_effect=OSError('not writable')):
            clip=self.prepare()
        self.assertTrue(Path(clip['audio']).is_file())
        self.assertTrue(any('缓存写入未完成' in message for message in self.logs))

    def test_concurrent_reviews_share_one_window_generation(self):
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            clips=list(pool.map(self.prepare,('review-a','review-b')))
        self.assertEqual(len(self.calls),3)
        self.assertEqual(sum(clip['short_audio_cache_hit'] for clip in clips),1)
        self.assertNotEqual(clips[0]['audio'],clips[1]['audio'])

    def test_prepare_review_uses_existing_audio_choice_and_only_three_short_windows(self):
        tracks=[core.Track(1,'audio','AC-3','de','',True,False,False,False,False),
                core.Track(9,'audio','TrueHD','en','',False,False,False,False,False)]
        subtitle=self.root/'sub.srt'
        subtitle.write_text('1\n00:01:42,000 --> 00:01:44,000\nFull subtitle candidate\n',encoding='utf8')
        def shortlist(events,duration,region):
            return [dict(start=float(region*100),cue_count=10,gap_count=3)]
        with patch.object(core,'inspect_media',return_value={}), \
             patch.object(core,'video_track_duration_ns',return_value=1200*10**9), \
             patch.object(core,'inspect_tracks',return_value=tracks), \
             patch.object(p,'subtitle_completeness',return_value=SimpleNamespace(accepted=True)), \
             patch.object(p,'_tool',return_value=str(self.engine)), \
             patch.object(review,'shortlist_windows',side_effect=shortlist), \
             patch.object(continuous_vad,'shared_audio',side_effect=AssertionError('whole-film fingerprint invoked')):
            evidence=review.prepare_review(str(self.movie),str(subtitle),str(self.root/'prepare'),1,self.logs.append,source_language='de')
        self.assertEqual(evidence['validation_audio_id'],9)
        self.assertEqual(evidence['audio_stream_index'],1)
        self.assertEqual(evidence['audio_codec'],'TrueHD')
        self.assertEqual([clip['region'] for clip in evidence['clips']],[1,2,3])
        decodes=[args for args in self.calls if str(self.movie) in args]
        self.assertEqual(len(decodes),3)
        self.assertTrue(all(args[args.index('-t')+1]=='60' for args in decodes))
        self.assertTrue(all(args[args.index('-map')+1]=='0:a:1' for args in decodes))


if __name__=='__main__': unittest.main()
