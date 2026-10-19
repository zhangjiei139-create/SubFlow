"""Selection, reserve-window and preparation-budget integration contracts."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import manual_review_prepare as r
import subtitle_tool_core as core


def option(start, tier=0):
    return dict(start=float(start),cue_count=10,dialogue_count=8,gap_count=3,
                half_counts=[4,4],occupied_seconds=20,tier=tier,score=20)


def clip(region, chosen, preferred=True, usable=True):
    start=chosen['start']
    return dict(region=region,movie_start=start,subtitle_time=start+30,
                audio='unused.wav',slow_audio='unused.half.wav',rows=[],
                cue_count=10,dialogue_count=8,large_gap_count=3,
                selection_tier=chosen['tier'],selection_score=chosen['score'],
                selection_warning='' if preferred else '后备片段',spoken_seconds=18 if preferred else 60,
                audio_quality=dict(preferred=preferred,usable=usable,pause_count=3 if preferred else 0))


class ManualWindowPreparationTest(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.movie=self.root/'movie.mkv'; self.movie.write_bytes(b'movie')
        self.source=self.root/'source.srt'
        self.source.write_text('1\n00:00:01,000 --> 00:00:03,000\nLeave the door open.\n',encoding='utf8')

    def prepare(self, chooser, options=None):
        with ExitStack() as stack:
            stack.enter_context(patch.object(core,'inspect_media',return_value={}))
            stack.enter_context(patch.object(core,'video_track_duration_ns',return_value=900*10**9))
            stack.enter_context(patch.object(core,'inspect_tracks',return_value=[SimpleNamespace(id=1,type='audio',codec='AC-3')]))
            stack.enter_context(patch.object(r.pro_core,'subtitle_completeness',return_value=SimpleNamespace(accepted=True)))
            stack.enter_context(patch.object(r.pro_core,'_preferred_validation_audio_id',return_value=1))
            stack.enter_context(patch.object(r.pro_core,'_audio_stream_index',return_value=0))
            stack.enter_context(patch.object(r.pro_core,'_tool',return_value='unused-engine'))
            stack.enter_context(patch.object(r,'shortlist_windows',side_effect=options or
                (lambda events,duration,region:[option((region-1)*300+x) for x in (0,120,240)])))
            probe=stack.enter_context(patch.object(r,'prepare_window',side_effect=chooser))
            half=stack.enter_context(patch.object(r,'ensure_half_audio'))
            result=r.prepare_review(str(self.movie),str(self.source),str(self.root/'review'),1,lambda _:None)
        return result,probe,half

    def test_balanced_primary_stops_after_one_probe_and_half_speed_only_for_selected(self):
        result,probe,half=self.prepare(lambda movie,events,work,region,chosen,*a,**k:clip(region,chosen))
        self.assertEqual(probe.call_count,3)
        self.assertEqual(half.call_count,3)
        self.assertTrue(all(call.kwargs['prepare_slow'] is False for call in probe.call_args_list))
        self.assertEqual([len(v) for v in result['region_probes'].values()],[1,1,1])
        self.assertEqual([c['movie_start'] for c in result['clips']],[0,300,600])

    def test_audio_with_pauses_wins_over_longest_continuous_activity(self):
        def choose(movie,events,work,region,chosen,*a,**k):
            return clip(region,chosen,preferred=int(chosen['start'])%300==120)
        result,probe,half=self.prepare(choose)
        self.assertEqual(probe.call_count,6)
        self.assertEqual([c['movie_start'] for c in result['clips']],[120,420,720])
        self.assertEqual(half.call_count,3)

    def test_each_region_probes_no_more_than_three_and_keeps_dense_backup(self):
        result,probe,half=self.prepare(lambda movie,events,work,region,chosen,*a,**k:clip(region,chosen,False))
        self.assertEqual(probe.call_count,9)
        self.assertEqual(half.call_count,3)
        self.assertTrue(all(c['selection_warning'] for c in result['clips']))

    def test_no_dialogue_candidates_does_not_silently_fall_back_to_three_cues(self):
        with self.assertRaisesRegex(RuntimeError,'至少 6 条有效对白'):
            self.prepare(Mock(),lambda *a:[])

    def test_no_usable_audio_reports_problem_instead_of_selecting_silent_window(self):
        with self.assertRaisesRegex(RuntimeError,'声音活动过少'):
            self.prepare(lambda movie,events,work,region,chosen,*a,**k:clip(region,chosen,False,False))

    def evidence(self):
        root=self.root/'review';root.mkdir()
        originals=[clip(i+1,option(i*300)) for i in range(3)]
        evidence=dict(movie=str(self.movie),source=str(self.source),review_dir=str(root),
                      source_sha256=hashlib.sha256(self.source.read_bytes()).hexdigest(),
                      movie_signature=r._file_signature(self.movie),clips=originals,
                      region_options={str(i+1):[option(i*300),option(i*300+120)] for i in range(3)},
                      region_probes={str(i+1):[c] for i,c in enumerate(originals)},
                      preparation_elapsed_seconds=10,elapsed_seconds=9000,
                      audio_stream_index=0,validation_audio_id=1,audio_codec='AC-3')
        return evidence

    def test_replacement_updates_real_control_point_only_in_one_region(self):
        evidence=self.evidence();originals=list(evidence['clips'])
        with patch.object(r.pro_core,'_tool',return_value='engine'), \
             patch.object(r,'prepare_window',side_effect=lambda movie,events,work,region,chosen,*a,**k:clip(region,chosen)), \
             patch.object(r,'ensure_half_audio'):
            result=r.replace_review_window(evidence,1)
        self.assertIs(evidence['clips'][0],originals[0])
        self.assertIs(evidence['clips'][2],originals[2])
        self.assertEqual(result['movie_start'],420)
        self.assertEqual(r.review_points(evidence),[30,450,630])
        saved=json.loads((Path(evidence['review_dir'])/'manual-review.json').read_text(encoding='utf8'))
        self.assertEqual(saved['clips'][1]['subtitle_time'],450)

    def test_user_listening_time_does_not_consume_preparation_budget(self):
        evidence=self.evidence()
        with patch.object(r.time,'monotonic',return_value=10000), \
             patch.object(r.pro_core,'_DeadlineCancel') as deadline, \
             patch.object(r.pro_core,'_tool',return_value='engine'), \
             patch.object(r,'prepare_window',side_effect=lambda movie,events,work,region,chosen,*a,**k:clip(region,chosen)), \
             patch.object(r,'ensure_half_audio'):
            r.replace_review_window(evidence,0)
        deadline.assert_called_once_with(None,10170)

    def test_source_change_is_rejected_without_replacing_selected_region(self):
        evidence=self.evidence();before=list(evidence['clips'])
        self.source.write_text('different subtitle',encoding='utf8')
        with self.assertRaisesRegex(RuntimeError,'字幕文件已改变'):
            r.replace_review_window(evidence,0)
        self.assertEqual(before,evidence['clips'])

    def test_exhausted_budget_can_cycle_ready_windows_without_decoding(self):
        evidence=self.evidence();evidence['preparation_elapsed_seconds']=180
        extra=clip(1,option(120));evidence['region_probes']['1'].append(extra)
        with patch.object(r,'prepare_window') as decode,patch.object(r,'ensure_half_audio'):
            self.assertIs(r.replace_review_window(evidence,0),extra)
        decode.assert_not_called()

    def test_failed_reserve_keeps_current_clip(self):
        evidence=self.evidence();current=evidence['clips'][0]
        with patch.object(r.pro_core,'_tool',return_value='engine'), \
             patch.object(r,'prepare_window',side_effect=RuntimeError('decoder failed')):
            with self.assertRaisesRegex(RuntimeError,'decoder failed'):
                r.replace_review_window(evidence,0)
        self.assertIs(evidence['clips'][0],current)

    def test_receipt_write_failure_rolls_back_control_point_used_by_confirmation(self):
        evidence=self.evidence();current=evidence['clips'][0]
        with patch.object(r.pro_core,'_tool',return_value='engine'), \
             patch.object(r,'prepare_window',side_effect=lambda movie,events,work,region,chosen,*a,**k:clip(region,chosen)), \
             patch.object(r,'ensure_half_audio'), \
             patch.object(r,'_save_evidence',side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError,'disk full'):
                r.replace_review_window(evidence,0)
        self.assertIs(evidence['clips'][0],current)
        self.assertEqual(r.review_points(evidence),[30,330,630])


if __name__=='__main__': unittest.main()
