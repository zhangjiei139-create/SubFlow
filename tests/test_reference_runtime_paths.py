from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
import batch_core as batch
import continuous_vad as vad
import online_subtitles as online
import pro_core as p
import smart_subtitles as smart
from tests.test_audio_reference_policy import track,shared

class RuntimePathTests(unittest.TestCase):
    def test_actual_embedded_entry_string_paths_reach_main_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);sources=[]
            for i in (1,2):
                source=root/f'{i}.srt';source.write_text(f'1\n00:0{i}:00,000 --> 00:0{i}:02,000\nHello\n');sources.append(source)
            session=vad.ReferenceAudioSession(shared(2),1,[track(1,'DTS-HD MA'),track(2,'DTS')])
            def match(*args,shared,**kw):
                if shared.validation_audio_id==2:raise p.SubtitleContentMismatchError('连续VAD：范围外匹配明显更强')
                return Path('passed.srt'),'passed'
            with patch.object(vad,'_preflight_reference',side_effect=match),patch.object(vad,'shared_audio',return_value=shared(1)) as prepare:
                with self.assertRaises(p.SubtitleContentMismatchError):
                    p.align_embedded_text_track('movie.mkv',sources[0],str(root),1,'en',4,lambda _:None,shared_content_audio=session)
                result=p.align_embedded_text_track('movie.mkv',sources[1],str(root),1,'en',7,lambda _:None,shared_content_audio=session)
            self.assertEqual(result[1],'passed')
            self.assertEqual(session.validation_audio_id,1)
            self.assertEqual(prepare.call_count,1)

    def test_time_axis_recording_error_is_infrastructure_not_bad_subtitle(self):
        session=vad.ReferenceAudioSession(shared(2),1,[track(1),track(2)])
        with patch.object(vad,'_preflight_reference',side_effect=p.SubtitleContentMismatchError('连续VAD：拒绝')), \
             patch.object(vad,'_subtitle_clock',side_effect=AttributeError('broken path')):
            with self.assertRaises(p.SubtitleVerificationToolError) as raised:
                vad.preflight('movie.mkv','candidate.srt','work',1,lambda _:None,shared=session)
        self.assertIn('不能据此判定字幕不匹配',str(raised.exception))
        advice=batch.failure_advice(raised.exception)
        self.assertIn('程序错误',advice)
        self.assertNotIn('字幕选择',advice)
        self.assertFalse(session.failures)

    def test_smart_tool_error_stops_candidate_checks_without_missing_source_claim(self):
        with tempfile.TemporaryDirectory() as folder,ExitStack() as stack:
            root=Path(folder);movie=root/'Film.2020.mkv';movie.write_bytes(b'film')
            candidates=[SimpleNamespace(file_id=str(i),release=f'Film.2020.BluRay.{i}',file_name=f'{i}.srt',
                language='en',feature_title='Film',feature_year='2020') for i in (1,2)]
            downloads=[]
            def download(key,candidate,destination):
                downloads.append(candidate.file_id);target=Path(destination)/candidate.file_name
                target.parent.mkdir(parents=True,exist_ok=True)
                target.write_text('1\n00:00:01,000 --> 00:00:03,000\nHello\n');return target
            service=SimpleNamespace(load_settings=lambda:{'key':'fixture'},download=download,
                search=lambda *a,**kw:(online.MediaIdentity('Film',year='2020'),candidates,online.SearchMeta()))
            stack.enter_context(patch.object(smart,'PROVIDERS',(('Test',service,'key'),)))
            stack.enter_context(patch.object(smart,'filename_container_identity_conflict',return_value=''))
            stack.enter_context(patch.object(p,'prepare_shared_subtitle_content_audio',return_value=shared(2)))
            check=stack.enter_context(patch.object(p,'preflight_online_subtitle',side_effect=p.SubtitleVerificationToolError('程序错误')))
            with self.assertRaises(p.SubtitleVerificationToolError):
                smart.find_verified_english(str(movie),1,lambda _:None,max_candidates=2)
            # The existing batch downloader may prefetch both sources before
            # matching starts. No second candidate is checked after a tool error.
            self.assertEqual(downloads,['1','2'])
            self.assertEqual(check.call_count,1)
