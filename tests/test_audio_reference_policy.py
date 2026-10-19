import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch

import audio_roles
import batch_core
import continuous_vad as vad
import profile_model
import pro_core as p
import subtitle_tool_core as core

def track(i=1,codec='DTS',name='',**kw):
    return core.Track(i,'audio',codec,'en',name,False,False,False,False,False,**kw)

def shared(i):
    return p.SharedSubtitleContentAudio(1800,i,i-1,'en',None,vad_reference=Path(f'audio-{i}.npz'),
        vad_preparation_state=dict(spent=10,limit=180,references={}))

class PurposeTests(unittest.TestCase):
    def test_container_flags_preserved_and_excluded(self):
        ts=core.tracks_from_media(dict(tracks=[dict(id=1,type='audio',codec='AAC',properties=dict(
            language='eng',flag_commentary=True,audio_channels=2)),dict(id=2,type='audio',codec='AAC',
            properties=dict(flag_visual_impaired=True))]))
        self.assertTrue(ts[0].commentary); self.assertEqual(ts[0].channels,2)
        self.assertTrue(ts[1].visual_impaired)
        self.assertEqual(audio_roles.dialogue_tracks(ts),[])

    def test_false_and_absent_flags_do_not_prove_commentary(self):
        self.assertFalse(audio_roles.excluded_reason(track(channels=2)))
        self.assertFalse(audio_roles.excluded_reason(track(channels=1)))

    def test_cheap_same_language_reference_precedes_main_and_channel_count(self):
        ts=[track(1,'TrueHD',channels=6),track(2,'AAC',channels=2)]
        self.assertEqual(vad.audio_track(ts,1).id,2)
        self.assertEqual(vad.audio_track(ts,2).id,2)
        self.assertEqual(vad.main_audio_track(ts,1).id,1)

    def test_cheaper_foreign_dub_does_not_override_reference_language(self):
        ts=[track(1,'TrueHD'),core.Track(2,'audio','AAC','de','',True,False,False,False,False),track(3,'DTS')]
        self.assertEqual(vad.audio_track(ts,1).id,3)
        self.assertEqual(vad.main_audio_track(ts,1).id,1)

    def test_missing_main_uses_default_language_before_cost(self):
        ts=[track(1,'TrueHD'),core.Track(2,'audio','AAC','de','',True,False,False,False,False)]
        self.assertEqual(vad.audio_track(ts,99).id,2)
        self.assertEqual(vad.main_audio_track(ts,99).id,2)

    def test_stereo_mono_remain_eligible(self):
        for channels in (1,2):
            self.assertEqual(vad.audio_track([track(channels=channels)],1).id,1)

    def test_directors_cut_is_not_commentary(self):
        for name in ("Director's Cut", 'Directors Cut', 'Director approved restoration'):
            self.assertFalse(profile_model.is_commentary(track(name=name)))

    def test_labels_and_flags_unified_for_main_and_vad(self):
        for bad in (track(1,name='Audio Description'),track(1,name='Music only'),track(1,commentary=True)):
            ts=[bad,track(2)]
            self.assertEqual(batch_core._main_audio(ts).id,2)
            self.assertEqual(vad.audio_track(ts,1).id,2)

    def test_all_commentary_never_falls_back_to_first(self):
        ts=[track(1,commentary=True),track(2,name='Commentary by director')]
        self.assertIsNone(batch_core._main_audio(ts))
        with self.assertRaisesRegex(RuntimeError,'正常对白'):vad.audio_track(ts)

    def test_native_retention_does_not_silently_delete(self):
        ts=[track(),track(2,commentary=True)]
        self.assertEqual(batch_core._audio_policy_selection(ts,ts[0],'native')[0],[1,2])

    def test_analysis_reports_only_special_audio_without_crashing_or_selecting_it(self):
        ts=[track(1,commentary=True),track(2,visual_impaired=True)]
        with patch.object(batch_core,'_inspect_media_cached',return_value=({},False)),patch.object(core,'tracks_from_media',return_value=ts):
            plan=batch_core.analyze_video('Film.2020.mkv',profile_model.PreferenceProfile(1,'test'))
        self.assertEqual(plan.status,'blocked')
        self.assertIn('非正片音轨',plan.summary)

class FallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.work=Path(self.tmp.name)
        self.a=self.work/'a.srt';self.b=self.work/'b.srt'
        self.a.write_text('1\n00:01:00,000 --> 00:01:02,000\nHello there\n')
        self.b.write_text('1\n00:02:00,000 --> 00:02:02,000\nOther dialogue\n')
        self.ts=[track(1,'DTS-HD MA'),track(2,'DTS')]
        self.session=vad.ReferenceAudioSession(shared(2),1,self.ts)
        self.logs=[]

    def invoke(self,source):
        return vad.preflight('movie.mkv',source,self.work/source.stem,1,self.logs.append,shared=self.session)

    @staticmethod
    def fail(*args,**kwargs):raise p.SubtitleContentMismatchError('连续VAD：范围外匹配明显更强')

    def test_two_different_clocks_switch_once_and_return_actual_audio(self):
        def run(*args,shared,**kw):
            if shared.validation_audio_id==2:self.fail()
            return Path('ok.srt'),'audio1 pass'
        with patch.object(vad,'_preflight_reference',side_effect=run),patch.object(vad,'shared_audio',return_value=shared(1)) as prepare:
            with self.assertRaises(p.SubtitleContentMismatchError):self.invoke(self.a)
            result=self.invoke(self.b)
            self.assertEqual(result[1],'audio1 pass')
            self.assertEqual(self.session.validation_audio_id,1)
            self.assertEqual(self.session.revision,1)
            self.invoke(self.a)
            prepare.assert_called_once()
            self.assertEqual(self.session.vad_preparation_state['limit'],180)
            self.assertGreaterEqual(self.session.vad_preparation_state['spent'],20)
            self.assertEqual(prepare.call_args.kwargs['reference_audio_id'],1)

    def test_fallback_targets_main_even_when_a_third_track_is_cheaper(self):
        self.session.tracks=(*self.ts,track(3,'AAC'))
        def run(*args,shared,**kw):
            if shared.validation_audio_id==2:self.fail()
            return Path('ok'),'pass'
        with patch.object(vad,'_preflight_reference',side_effect=run),patch.object(vad,'shared_audio',return_value=shared(1)) as prepare:
            with self.assertRaises(p.SubtitleContentMismatchError):self.invoke(self.a)
            self.invoke(self.b)
        self.assertEqual(prepare.call_args.kwargs['reference_audio_id'],1)
        self.assertEqual(self.session.validation_audio_id,1)

    def test_already_using_main_does_not_cycle_through_other_tracks(self):
        self.session.current=shared(1)
        with patch.object(vad,'_preflight_reference',side_effect=self.fail),patch.object(vad,'shared_audio') as prepare:
            for source in (self.a,self.b,self.a):
                with self.assertRaises(p.SubtitleContentMismatchError):self.invoke(source)
            prepare.assert_not_called()
        self.assertFalse(self.session.fallback_used)

    def test_successful_cheap_reference_never_prepares_main(self):
        with patch.object(vad,'_preflight_reference',return_value=(Path('ok'),'pass')),patch.object(vad,'shared_audio') as prepare:
            self.invoke(self.a)
            self.invoke(self.b)
            prepare.assert_not_called()
        self.assertEqual(self.session.validation_audio_id,2)
        self.assertTrue(self.session.confirmed)

    def test_marked_commentary_is_not_used_as_main_fallback(self):
        self.session.tracks=(track(1,'TrueHD',commentary=True),track(2,'AAC'),track(3,'DTS'))
        def run(*args,shared,**kw):
            if shared.validation_audio_id==2:self.fail()
            return Path('ok'),'pass'
        with patch.object(vad,'_preflight_reference',side_effect=run),patch.object(vad,'shared_audio') as prepare:
            for source in (self.a,self.b):
                with self.assertRaises(p.SubtitleContentMismatchError):self.invoke(source)
            prepare.assert_not_called()

    def test_duplicate_timeline_does_not_trigger_switch(self):
        self.b.write_bytes(self.a.read_bytes())
        with patch.object(vad,'_preflight_reference',side_effect=self.fail),patch.object(vad,'shared_audio') as prepare:
            for source in (self.a,self.b):
                with self.assertRaises(p.SubtitleContentMismatchError):self.invoke(source)
            prepare.assert_not_called()

    def test_wrong_language_or_partial_source_does_not_trigger_switch(self):
        for reason in ('字幕不完整','实际正文不是英文'):
            self.session.failures.clear()
            with patch.object(vad,'_preflight_reference',side_effect=p.SubtitleContentMismatchError(reason)),patch.object(vad,'shared_audio') as prepare:
                for source in (self.a,self.b):
                    with self.assertRaises(p.SubtitleContentMismatchError):self.invoke(source)
                prepare.assert_not_called()

    def test_previous_candidate_recovered_even_when_current_is_bad(self):
        def run(movie,source,*args,shared,**kw):
            if shared.validation_audio_id==1 and Path(source)==self.a:return Path('ok'),'pass'
            self.fail()
        with patch.object(vad,'_preflight_reference',side_effect=run),patch.object(vad,'shared_audio',return_value=shared(1)):
            for source in (self.a,self.b):
                with self.assertRaises(p.SubtitleContentMismatchError):self.invoke(source)
        self.assertEqual(self.session.validation_audio_id,1)
        self.assertTrue(self.session.confirmed)
        self.assertTrue(any('回到该原候选' in line for line in self.logs))

    def test_failed_alternate_does_not_become_confirmed_or_loop(self):
        with patch.object(vad,'_preflight_reference',side_effect=self.fail),patch.object(vad,'shared_audio',return_value=shared(1)) as prepare:
            for source in (self.a,self.b,self.a):
                with self.assertRaises(p.SubtitleContentMismatchError):self.invoke(source)
            prepare.assert_called_once()
        self.assertEqual(self.session.validation_audio_id,2)
        self.assertFalse(self.session.confirmed)
        self.assertTrue(self.session.unconfirmed)

    def test_cancellation_and_tool_error_do_not_switch(self):
        for error in (core.CancelledError('stop'),p.SubtitleVerificationToolError('tool')):
            with patch.object(vad,'_preflight_reference',side_effect=error),patch.object(vad,'shared_audio') as prepare:
                with self.assertRaises(type(error)):self.invoke(self.a)
                prepare.assert_not_called()

    def test_budget_exhaustion_never_resets_for_alternate(self):
        self.session.current.vad_preparation_state['spent']=180
        with patch.object(vad,'_preflight_reference',side_effect=self.fail),patch.object(vad,'shared_audio') as prepare:
            with self.assertRaises(p.SubtitleContentMismatchError):self.invoke(self.a)
            with self.assertRaises(p.SubtitlePreflightTimeoutError):self.invoke(self.b)
            prepare.assert_not_called()

    def test_movie_change_revokes_reference_session_before_matching(self):
        movie=self.work/'movie.mkv';movie.write_bytes(b'initial')
        self.session=vad.ReferenceAudioSession(shared(2),1,self.ts,movie=movie)
        movie.write_bytes(b'changed film')
        with patch.object(vad,'_preflight_reference') as match:
            with self.assertRaises(p.SubtitleVerificationToolError):
                vad.preflight(str(movie),self.a,self.work/'verify',2,self.logs.append,shared=self.session)
            match.assert_not_called()

    def test_cancelled_alternate_preparation_does_not_grant_manual_pending_or_reset_budget(self):
        with patch.object(vad,'_preflight_reference',side_effect=self.fail),patch.object(vad,'shared_audio',side_effect=core.CancelledError('stop')):
            with self.assertRaises(p.SubtitleContentMismatchError):self.invoke(self.a)
            with self.assertRaises(core.CancelledError):self.invoke(self.b)
        self.assertFalse(self.session.unconfirmed)
        self.assertFalse(self.session.confirmed)
        self.assertGreaterEqual(self.session.vad_preparation_state['spent'],10)
