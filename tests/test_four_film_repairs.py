from pathlib import Path
import sys,tempfile,unittest,threading,time
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import pro_core as p, subtitle_tool_core as core, continuous_vad as vad, smart_subtitles as smart

class FourFilmRepairs(unittest.TestCase):
    def test_locked_cleanup_is_bounded_and_does_not_raise(self):
        error=PermissionError('locked');error.winerror=32;messages=[]
        with patch.object(Path,'unlink',side_effect=error) as unlink,patch.object(p.time,'sleep') as sleep:
            p._cleanup_alignment_temporary(Path('owned-temp.mkv'),messages.append)
        self.assertEqual(unlink.call_count,5);self.assertEqual(sleep.call_count,4)
        self.assertIn('仍被占用',messages[-1])

    def test_transient_lock_is_retried(self):
        error=PermissionError('locked');error.winerror=32
        with patch.object(Path,'unlink',side_effect=[error,None]) as unlink,patch.object(p.time,'sleep'):
            p._cleanup_alignment_temporary(Path('owned-temp.mkv'),lambda _:None)
        self.assertEqual(unlink.call_count,2)

    def test_preparation_error_survives_cleanup_and_each_attempt_has_unique_input(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);movie=root/'movie.mkv';movie.write_bytes(b'video');exe=root/'tool';exe.write_bytes(b'exe');paths=[]
            def run(args,*a):paths.append(args[1]);raise core.CancelledError('deadline reached')
            with patch.object(core,'inspect_tracks',return_value=[core.Track(1,'audio','AAC','en','',True,False,False,False,False)]),patch.object(p,'_tool',return_value=str(exe)),patch.object(p,'_audio_stream_index',return_value=0),patch.object(p,'_run',side_effect=run),patch.object(p,'_cleanup_alignment_temporary'):
                for _ in range(2):
                    with self.assertRaisesRegex(core.CancelledError,'deadline reached'):
                        p._build_continuous_alignment_audio(str(movie),1,root/'cache',lambda _:None)
            self.assertNotEqual(paths[0],paths[1]);self.assertFalse((root/'cache/shared-alignment-speech.json').exists())

    def test_preparation_deadline_is_not_user_cancel(self):
        tr=core.Track(1,'audio','AAC','en','',True,False,False,False,False)
        fake=SimpleNamespace(timed_out=True,is_set=lambda:False)
        with tempfile.TemporaryDirectory() as td,patch.object(core,'inspect_tracks',return_value=[tr]),patch.object(core,'inspect_media',return_value={}),patch.object(p,'_DeadlineCancel',return_value=fake),patch.object(p,'_build_continuous_alignment_audio',side_effect=core.CancelledError('stopped')):
            with self.assertRaisesRegex(p.SubtitlePreflightTimeoutError,'候选尚未核验'):
                vad.shared_audio(str(Path(td)/'movie'),1,lambda _:None)

    def cue_fixture(self,td):
        root=Path(td);video=root/'movie.mkv';video.write_bytes(b'v');src=root/'sub.srt'
        src.write_text('1\n00:00:01,000 --> 00:00:02,000\nHello there\n\n2\n00:00:03,000 --> 00:00:04,000\n\n\n3\n00:00:05,000 --> 00:00:06,000\nAnother sentence\n',encoding='utf8')
        ref=root/'ref.npz';ref.write_bytes(b'cached')
        return video,src,SimpleNamespace(duration_seconds=60,validation_audio_id=1,vad_reference=ref,audio_language='en')

    def test_empty_cue_can_be_removed_but_valid_text_is_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            movie,src,shared=self.cue_fixture(td);messages=[]
            def align(m,n,o,*a,diagnostics,**kw):
                p._shifted_srt(n,o,1180);diagnostics.update(score=5,offset_seconds=1.18,raw_offset_seconds=1.18)
            with patch.object(p,'_alignment_candidate',side_effect=align):
                output,_=vad.preflight(movie,src,Path(td)/'work',1,messages.append,shared=shared)
            self.assertEqual(len(core.parse_subtitle(output)),2)
            self.assertTrue(any('原始3条，有效2条，纠偏后2条' in s for s in messages))

    def test_lost_or_changed_valid_text_is_rejected(self):
        for drop in (True,False):
            with self.subTest(drop=drop),tempfile.TemporaryDirectory() as td:
                movie,src,shared=self.cue_fixture(td)
                def align(m,n,o,*a,diagnostics,**kw):
                    text=n.read_text(encoding='utf8')
                    o.write_text(text.split('\n\n')[0]+'\n' if drop else text.replace('Hello there','Wrong dialogue'),encoding='utf8')
                    diagnostics.update(score=5,offset_seconds=1.18,raw_offset_seconds=1.18)
                with patch.object(p,'_alignment_candidate',side_effect=align):
                    with self.assertRaises(p.SubtitleVerificationToolError):vad.preflight(movie,src,Path(td)/'work',1,lambda _:None,shared=shared)
                self.assertFalse((Path(td)/'work/vad-verdict.json').exists())

    def test_pgs_conflict_and_pass_use_existing_thresholds(self):
        tracks=[core.Track(0,'audio','AAC','en','',True,False,False,False,False),core.Track(1,'subtitles','HDMV PGS','zh','',False,False,False,True,False)]
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);movie=root/'movie.mkv';movie.write_bytes(b'v');srt=root/'ref.srt'
            srt.write_text(''.join(f'{i}\n00:00:{i:02d},000 --> 00:00:{i:02d},800\nText {i}\n\n' for i in range(1,25)),encoding='utf8')
            with patch.object(core,'inspect_tracks',return_value=tracks),patch.object(core,'prepare_work_input',return_value=str(movie)),patch.object(core,'inspect_media',return_value={}),patch.object(p,'_pgs_sampled_noop',return_value=False),patch.object(p,'_embedded_image_intervals',return_value=[(i,i+.8) for i in range(1,25)]),patch.object(p,'_interval_alignment',return_value=(.45,-117.2)) as align:
                with self.assertRaisesRegex(p.SubtitleContentMismatchError,'45.0%'):
                    p.require_image_timeline_anchor(str(movie),[1],0,root,srt,lambda _:None)
                align.return_value=(.98,.20)
                result=p.require_image_timeline_anchor(str(movie),[1],0,root,srt,lambda _:None)
                self.assertTrue(result[1]['accepted']);self.assertEqual(result[1]['offset_ms'],0)
                align.return_value=(.98,-2.)
                self.assertEqual(p.require_image_timeline_anchor(str(movie),[1],0,root,srt,lambda _:None)[1]['offset_ms'],-2000)

    def test_downloaded_next_candidate_is_checked_with_same_deadline_no_new_search(self):
        candidates=[SimpleNamespace(release=f'Movie candidate {i}',file_name=f'c{i}.srt',language='en',file_id=str(i),identity_key=str(i)) for i in (1,2)]
        calls=[];deadlines=[]
        class Service:
            @staticmethod
            def load_settings():return {'key':'test'}
            @staticmethod
            def search(*args):calls.append('search');return None,candidates,None
            @staticmethod
            def download(key,candidate,destination):
                calls.append('download'+candidate.file_id);dest=Path(destination)/candidate.file_name;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_text('distinct text '+candidate.file_id);return dest
        def verify(video,src,work,audio,shared,log,cancel,*args,**kw):
            deadlines.append(cancel);calls.append('verify'+Path(src).stem[-1]);return Path(src),'VAD pass'
        def accept(path,cancel):
            self.assertIs(cancel,deadlines[-1]);calls.append('pgs'+path.stem[-1])
            if path.stem=='c1':raise p.SubtitleContentMismatchError('PGS mismatch')
        with tempfile.TemporaryDirectory() as td:
            movie=Path(td)/'Movie.mkv';movie.write_bytes(b'v')
            with patch.object(smart,'PROVIDERS',(('Test',Service,'key'),)),patch.object(p,'prepare_shared_subtitle_content_audio',return_value=object()) as prepare,patch.object(p,'preflight_online_subtitle',side_effect=verify):
                result=smart.find_verified_english(str(movie),0,lambda _:None,candidate_acceptance=accept)
            self.assertEqual(result.candidate.file_id,'2');prepare.assert_called_once()
            self.assertEqual(calls.count('search'),1);self.assertEqual(calls.count('download1'),1);self.assertEqual(calls.count('download2'),1)
            self.assertLess(calls.index('download2'),calls.index('pgs1'))

    def test_translation_log_only_skips_the_source_language(self):
        import batch_core as batch
        from profile_model import PreferenceProfile
        tracks=[core.Track(0,'audio','AAC','eng','',True,False,False,False,False)]
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);movie=root/'movie.mkv';movie.write_bytes(b'v');sub=root/'verified.srt';sub.write_text('verified')
            profile=PreferenceProfile(1,'test',subtitle_languages=['en','zh-CN'])
            with patch.object(batch,'_inspect_media_cached',return_value=({},False)),patch.object(core,'tracks_from_media',return_value=tracks):
                plan=batch.analyze_video(str(movie),profile)
            result=smart.SmartSubtitleResult(str(sub),'en','pass','Test',object());messages=[]
            with patch.object(core,'inspect_tracks',return_value=tracks),patch.object(smart,'find_verified_english',return_value=result),patch.object(core,'begin_ollama_lease'),patch.object(core,'end_ollama_lease',return_value=False),patch.object(p,'process_pro') as process:
                batch.process_plan(plan,profile,messages.append,None)
            self.assertEqual(process.call_args.kwargs['target_codes'],['zh-CN'])
            skip=next(line for line in messages if '仅跳过这些语言' in line)
            self.assertIn('仍需翻译：简体中文',skip)
            self.assertNotIn('不启动本地翻译模型',skip)

    def test_preparation_timeout_is_not_retried_as_a_bad_embedded_track(self):
        import batch_core as batch
        from profile_model import PreferenceProfile
        tracks=[core.Track(0,'audio','AAC','eng','',True,False,False,False,False),core.Track(1,'subtitles','S_TEXT/UTF8','eng','',True,False,True,False,False)]
        with tempfile.TemporaryDirectory() as td:
            movie=Path(td)/'movie.mkv';movie.write_bytes(b'v');profile=PreferenceProfile(1,'test',subtitle_languages=['en'])
            with patch.object(batch,'_inspect_media_cached',return_value=({},False)),patch.object(core,'tracks_from_media',return_value=tracks):
                plan=batch.analyze_video(str(movie),profile)
            with patch.object(core,'inspect_tracks',return_value=tracks),patch.object(p,'prepare_shared_subtitle_content_audio',side_effect=p.SubtitlePreflightTimeoutError('audio deadline')) as prepare,patch.object(smart,'find_verified_english') as search:
                with self.assertRaisesRegex(p.SubtitlePreflightTimeoutError,'audio deadline'):
                    batch.process_plan(plan,profile,lambda _:None,None)
            prepare.assert_called_once();search.assert_not_called()

if __name__=='__main__':unittest.main()
