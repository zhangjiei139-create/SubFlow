"""Decoder policy contracts. No real media, ASR, network, or external tools."""
import concurrent.futures
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import wave
from contextlib import ExitStack
from unittest.mock import patch
import continuous_vad as vad
import pro_core as p
import subtitle_tool_core as core


def track(i, codec='AAC', lang='en', name='', default=False):
    return core.Track(i, 'audio', codec, lang, name, default, False, False, False, False)


def pcm(path):
    with wave.open(str(path), 'wb') as f:
        f.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
        f.writeframes(b'\0\0' * 480)


class SelectionTests(unittest.TestCase):
    def test_english_priority_survives_cheaper_other_language(self):
        self.assertEqual(vad.audio_track([track(1,'AC-3','de'),track(2,'TrueHD')],1).id,2)

    def test_non_english_cost_order_stays_same_language(self):
        ts=[track(1,'TrueHD','de'),track(2,'AC-3','ja'),track(3,'DTS-HD MA','de'),track(4,'DTS','de'),track(5,'AC-3','de')]
        self.assertEqual(vad.audio_track(ts,1).id,5)
        self.assertEqual(vad.audio_track(ts[:-1],1).id,4)
        self.assertEqual(vad.audio_track(ts[:3],1).id,3)

    def test_commentary_description_partial_and_score_excluded(self):
        for name in ('Director commentary','Audio Description','partial dialogue','isolated score'):
            self.assertEqual(vad.audio_track([track(1,'TrueHD'),track(2,'AC-3',name=name)],1).id,1)

    def test_unknown_language_does_not_justify_switch(self):
        self.assertEqual(vad.audio_track([track(1,'TrueHD','und'),track(2,'AC-3','und')],1).id,1)

    def test_aliases_and_dca_are_recognized(self):
        self.assertTrue(vad.is_dts_codec('dca'))
        self.assertEqual(vad.audio_track([track(1,'TrueHD','deu'),track(2,'AC-3','ger')],1).id,2)

    def test_equal_cost_keeps_selected_track(self):
        self.assertEqual(vad.audio_track([track(1,default=True),track(2)],2).id,2)

    def test_audio_budget_not_pgs_or_candidate_budget(self):
        self.assertEqual(vad.preparation_budget_seconds(10800),180)
        self.assertEqual(vad.preparation_budget_seconds(10800.1),300)
        self.assertEqual(vad.image_preparation_budget_seconds(6727.68),143)
        import smart_subtitles
        self.assertEqual(smart_subtitles.SMART_TIME_BUDGET_SECONDS,20)


class DecodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.movie=self.root/'movie.mkv';self.movie.write_bytes(b'video')
        self.engine=self.root/'ffsubsync.exe';self.engine.write_bytes(b'engine')
        self.ffmpeg=self.root/'ffmpeg.exe';self.ffmpeg.write_bytes(b'decoder')
        self.cache=self.root/'cache';self.calls=[];self.logs=[];self.fail=None
        self.tracks=[track(1,'AC-3','fr'),track(2,'DTS-HD MA')]
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(core,'inspect_tracks',side_effect=lambda _:self.tracks))
        self.stack.enter_context(patch.object(core,'FFMPEG',str(self.ffmpeg)))
        self.stack.enter_context(patch.object(p,'_tool',return_value=str(self.engine)))
        self.stack.enter_context(patch.object(p,'_run',side_effect=self.run_tool))

    def run_tool(self,args,log,cancel):
        self.calls.append((args,cancel))
        if args[0]==str(self.ffmpeg):
            if self.fail:raise self.fail
            pcm(Path(args[-1]))
        else:
            Path(args[1]).with_suffix('.npz').write_bytes(b'fingerprint'*30)

    def build(self,cancel=None):
        return p._build_continuous_alignment_audio(str(self.movie),2,self.cache,self.logs.append,cancel)

    def test_core_applies_to_selected_input_and_keeps_continuous_full_duration(self):
        deadline=p._DeadlineCancel(None,time.monotonic()+180)
        self.build(deadline)
        dec,gen=[x[0] for x in self.calls]
        self.assertLess(dec.index('-core_only'),dec.index('-i'))
        self.assertEqual(dec[dec.index('-map')+1],'0:a:1')
        self.assertEqual(gen[gen.index('--reference-stream')+1],'a:0')
        self.assertEqual(dec[dec.index('-af')+1],'aresample=async=1')
        for args,cancel in self.calls:
            self.assertIs(cancel,deadline)
            self.assertNotIn('-ss',args);self.assertNotIn('-t',args)
        self.assertFalse(list(self.cache.glob('*.wav')))
        self.assertFalse(list(self.cache.glob('*.mkv')))
        saved=json.loads((self.cache/'shared-alignment-speech.json').read_text())
        self.assertEqual(saved['actual_decode_mode'],'dts-core')

    def test_non_dts_uses_original_path_without_extra_decode(self):
        for codec in ('FLAC','AC-3','AAC'):
            with self.subTest(codec=codec):
                self.tracks[1]=track(2,codec);self.calls.clear();self.build()
                self.assertEqual(len(self.calls),1)
                args=self.calls[0][0]
                self.assertEqual(args[0],str(self.engine))
                self.assertEqual(args[args.index('--reference-stream')+1],'a:1')

    def test_truehd_uses_input_stereo_without_changing_other_codecs(self):
        self.tracks[1]=track(2,'TrueHD Atmos')
        self.build()
        self.assertEqual(len(self.calls),2)
        decode,generate=[item[0] for item in self.calls]
        self.assertLess(decode.index('-downmix'),decode.index('-i'))
        self.assertEqual(decode[decode.index('-downmix')+1],'stereo')
        self.assertNotIn('-core_only',decode)
        self.assertEqual(decode[decode.index('-map')+1],'0:a:1')
        self.assertEqual(generate[generate.index('--reference-stream')+1],'a:0')
        self.assertFalse(list(self.cache.glob('*.wav')))

    def test_no_core_falls_back_once_with_same_deadline_and_cached_actual_mode(self):
        self.fail=RuntimeError('No valid DCA sub-stream found')
        deadline=p._DeadlineCancel(None,time.monotonic()+180)
        self.build(deadline)
        self.assertEqual(len(self.calls),2)
        self.assertTrue(all(x[1] is deadline for x in self.calls))
        self.assertTrue(any('剩余' in s for s in self.logs))
        saved=json.loads((self.cache/'shared-alignment-speech.json').read_text())
        self.assertEqual(saved['actual_decode_mode'],'full-fallback')
        self.build(deadline);self.assertEqual(len(self.calls),2)

    def test_cancel_does_not_fallback_or_publish(self):
        self.fail=core.CancelledError('stopped')
        with self.assertRaises(core.CancelledError):self.build()
        self.assertEqual(len(self.calls),1)
        self.assertFalse((self.cache/'shared-alignment-speech.json').exists())

    def test_deadline_expired_during_core_failure_prevents_retry(self):
        stop=threading.Event()
        original=self.run_tool
        def fail(args,log,cancel):
            stop.set();raise RuntimeError('No valid DCA sub-stream found')
        with patch.object(p,'_run',side_effect=fail) as run:
            with self.assertRaises(core.CancelledError):self.build(stop)
        self.assertEqual(run.call_count,1)

    def test_disk_error_does_not_retry_full_film(self):
        self.fail=RuntimeError('目标磁盘空间不足')
        with self.assertRaisesRegex(RuntimeError,'空间不足'):self.build()
        self.assertEqual(len(self.calls),1)

    def test_empty_core_audio_falls_back_instead_of_no_dialogue_verdict(self):
        def fake(args,log,cancel):
            self.calls.append((args,cancel))
            if args[0]==str(self.ffmpeg):Path(args[-1]).write_bytes(b'')
            else:Path(args[1]).with_suffix('.npz').write_bytes(b'full'*100)
        with patch.object(p,'_run',side_effect=fake):self.build()
        self.assertEqual(len(self.calls),2)
        self.assertTrue(any('core无法解码' in s for s in self.logs))

    def test_corrupt_fingerprint_and_decoder_change_invalidate_cache(self):
        result=self.build();self.build();self.assertEqual(len(self.calls),2)
        result.write_bytes(b'tamper'*100);self.build();self.assertEqual(len(self.calls),4)
        self.ffmpeg.write_bytes(b'newdecoder');self.build();self.assertEqual(len(self.calls),6)
        self.movie.write_bytes(b'changed movie');self.build();self.assertEqual(len(self.calls),8)

    def test_old_full_decode_cache_is_not_accepted_as_core(self):
        self.cache.mkdir();(self.cache/'shared-alignment-speech.npz').write_bytes(b'old'*100)
        (self.cache/'shared-alignment-speech.json').write_text(json.dumps({'mode':'continuous-v1'}))
        self.build();self.assertEqual(len(self.calls),2)

    def test_missing_track_fails_instead_of_decoding_first_stream(self):
        self.tracks=self.tracks[:1]
        with self.assertRaisesRegex(p.SubtitleVerificationToolError,'音轨不存在'):self.build()
        self.assertEqual(self.calls,[])

    def test_source_change_during_build_is_not_published(self):
        original=self.run_tool
        def run(args,log,cancel):
            original(args,log,cancel)
            if args[0]==str(self.engine):self.movie.write_bytes(b'changed during decode')
        with patch.object(p,'_run',side_effect=run):
            with self.assertRaisesRegex(p.SubtitleVerificationToolError,'文件在取证期间发生变化'):self.build()
        self.assertFalse((self.cache/'shared-alignment-speech.json').exists())

    def test_concurrent_requests_only_decode_once(self):
        with patch.object(core,'inspect_media',return_value={}),patch.object(vad,'cache_directory',return_value=self.cache):
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                results=list(pool.map(lambda _:vad.shared_audio(str(self.movie),2,self.logs.append),range(2)))
        self.assertEqual(len(self.calls),2)
        self.assertEqual(results[0].vad_reference,results[1].vad_reference)


if __name__=='__main__':unittest.main()
