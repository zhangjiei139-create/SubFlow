import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication
APP = QApplication.instance() or QApplication([])
import subtitle_fonts as fonts
import subtitle_style_engine as engine
import subtitle_tool_core as core
import translation_recovery as recovery
import translation_review as review
from subtitle_audit import FilmAudit

CUSTOM = fonts.assets_root() / 'ma/MaShanZheng-Regular.ttf'


class FontFallbackTests(unittest.TestCase):
    def setUp(self):
        self.info = fonts.font_info(CUSTOM)
        self.style = dict(family=self.info['family'], size=48, color='#FFFFFF')

    def apply(self, body, text):
        plan = fonts.glyph_plan(self.info, text)
        events = [(1001, 3033, '0,0,Default,,0,0,0,,' + body)]
        return events, fonts.apply_glyph_fallbacks(events, self.info, self.style, plan)

    def test_only_missing_accent_gets_a_font_override(self):
        before, after = self.apply('the fiancée of my beloved son.', 'the fiancée of my beloved son.')
        body = after[0][2].split(',', 8)[8]
        self.assertEqual(after[0][:2], before[0][:2])
        self.assertEqual(engine.plain(body), 'the fiancée of my beloved son.')
        self.assertIn('Rubik', body)
        self.assertIn('}é{', body)
        self.assertNotIn('{\\fnRubik Light}the', body)
        self.assertIn('\\fn'+self.info['family']+'\\fs48}', body)

    def test_no_missing_glyph_does_not_touch_packets(self):
        before, after = self.apply('Plain English.', 'Plain English.')
        self.assertEqual(before, after)

    def test_markup_escapes_drawing_and_position_are_preserved(self):
        body = r'{\an8\pos(100,40)\i1}fiancée{\i0}\N\{literal\}{\p1}m 0 0 l 1 1{\p0} café'
        before, after = self.apply(body, engine.plain(body))
        self.assertEqual(engine.plain(after[0][2].split(',',8)[8]), engine.plain(body))
        self.assertIn(r'\pos(100,40)', after[0][2])
        self.assertIn(r'{\p1}m 0 0 l 1 1{\p0}', after[0][2])
        self.assertEqual(after[0][:2], before[0][:2])

    def test_uncoverable_character_fails_instead_of_removing_it(self):
        with self.assertRaisesRegex(ValueError, '均缺字'):
            fonts.glyph_plan(self.info, 'bad: ' + chr(0x10ffff))

    def test_preflight_detects_uncoverable_source_before_translation(self):
        style = dict(font_id='import:' + self.info['sha256'], **self.info, size_mode='auto', size=0, color='keep')
        with tempfile.TemporaryDirectory() as folder, fonts.processing({'en':style}, folder, 'result.mkv'):
            with self.assertRaisesRegex(ValueError, '均缺字'):
                fonts.preflight_text('en', chr(0x10ffff), lambda _:None)

    def test_edit_reuses_attached_fallback_and_keeps_primary_size(self):
        plan=fonts.glyph_plan(self.info,'fiancée')
        style=dict(self.info, font_id='import:'+self.info['sha256'], size=48,
                   glyph_fallbacks={c:dict(sha256=f['sha256'],family=f['family']) for c,f in plan.items()})
        original=[(1,2033,'0,0,Default,,0,0,0,,fiancée')]
        changed=fonts.reapply_styled_fallbacks(original,style)
        self.assertEqual(changed[0][:2],original[0][:2])
        self.assertIn('Rubik',changed[0][2])
        self.assertIn('\\fn'+self.info['family']+'\\fs48}',changed[0][2])

    def test_edit_needing_an_unattached_font_fails_before_replacing_output(self):
        style=dict(self.info,font_id='import:'+self.info['sha256'],size=48)
        with self.assertRaisesRegex(ValueError,'新的补字字体附件'):
            fonts.reapply_styled_fallbacks([(1,2033,'0,0,Default,,0,0,0,,fiancée')],style)


class RecoveryConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.lines = ['Each time I arrived,', 'somehow they had moved...', 'all their people.']
        self.good = dict(whole='每次我到达时，他们已经把所有人迁走了。', s1='每次我到达时，', s2='他们已经把所有人', s3='迁走了。')
        self.bad = dict(self.good, s2='他们已经')

    def test_inconsistent_parts_remain_rejected_with_original_response(self):
        diagnostics=[];request=unittest.mock.Mock(return_value=json.dumps(self.bad))
        with self.assertRaises(ValueError): recovery.recover(self.lines,'中文',request,diagnostics,lambda _:None)
        self.assertEqual(request.call_count,1)
        self.assertEqual(diagnostics[0]['response'],json.dumps(self.bad))
        self.assertIn('error',diagnostics[0])

    def test_english_or_malformed_response_does_not_seed_a_second_request(self):
        for response in ('not JSON',json.dumps(dict(self.bad,whole='English'))):
            request=unittest.mock.Mock(return_value=response)
            with self.assertRaises(ValueError): recovery.recover(self.lines,'中文',request,[],lambda _:None)
            self.assertEqual(request.call_count,1)


class RetryAndAuditTests(unittest.TestCase):
    def seed(self, folder, status='needs_review'):
        events=[core.SubtitleEvent(f'00:00:0{i},001',f'00:00:0{i+1},033',text) for i,text in enumerate(['Each time I arrived,','somehow they had moved...','all their people.'],1)]
        cache=Path(folder)/'cache.jsonl';output=Path(folder)/'out.srt'
        core.append_cache_records(cache,'zh-CN',[(1,events[0].text,'每次抵达时，'),(3,events[2].text,'所有人。')])
        issue=dict(index=2,start=events[1].start,end=events[1].end,source=events[1].text,translation=events[1].text,reason='漏译',status=status)
        review.save_progress(events,'zh-CN','en',cache,output,{2:issue},complete=True,translations={1:'每次抵达时，',2:events[1].text,3:'所有人。'})
        return events,cache,output

    def test_explicit_retry_requests_only_issue_and_recovers_necessary_neighbors(self):
        good=dict(whole='每次我到达时，他们已经把所有人迁走了。',s1='每次我到达时，',s2='他们已经把所有人',s3='迁走了。')
        with tempfile.TemporaryDirectory() as folder:
            events,cache,output=self.seed(folder)
            self.assertTrue(core.needs_model_translation(events,'zh-CN',cache,'en',retry_issues=True))
            self.assertFalse(core.needs_model_translation(events,'zh-CN',cache,'en'))
            with patch.object(core,'translate_batch',side_effect=[[events[1].text],core.TranslationGenerationError('repeat')]) as request, patch.object(core,'call_ollama_retry',return_value=json.dumps(good)):
                core.translate_events(events,output,cache,'zh-CN','en',lambda _:None,retry_issues=True)
            self.assertEqual(request.call_args_list[0].args[0],[events[1].text])
            self.assertEqual([e.text for e in core.parse_subtitle(output)],[good[k] for k in ('s1','s2','s3')])
            self.assertEqual(review.load_report(review.report_path(cache))['issues'],[])
            self.assertFalse(core.needs_model_translation(events,'zh-CN',cache,'en',retry_issues=True))

    def test_user_approved_issue_is_not_retried(self):
        with tempfile.TemporaryDirectory() as folder:
            events,cache,output=self.seed(folder,'user_approved')
            self.assertFalse(core.needs_model_translation(events,'zh-CN',cache,'en',retry_issues=True))
            with patch.object(core,'translate_batch',side_effect=AssertionError('manual approval must be preserved')):
                core.translate_events(events,output,cache,'zh-CN','en',lambda _:None,retry_issues=True)
            self.assertEqual(review.load_report(review.report_path(cache))['issues'][0]['status'],'user_approved')

    def test_failed_retry_does_not_overwrite_good_neighbors_and_saves_rejection(self):
        bad=dict(whole='每次我到达时，他们已经把所有人迁走了。',s1='每次我到达时，',s2='他们已经',s3='迁走了。')
        with tempfile.TemporaryDirectory() as folder:
            events,cache,output=self.seed(folder)
            with patch.object(core,'translate_batch',side_effect=[[events[1].text],core.TranslationGenerationError('repeat')]),patch.object(core,'call_ollama_retry',return_value=json.dumps(bad)):
                core.translate_events(events,output,cache,'zh-CN','en',lambda _:None,retry_issues=True)
            rows,_=core.load_cache(cache,'zh-CN',{i:e.text for i,e in enumerate(events,1)})
            self.assertEqual(rows,{1:'每次抵达时，',3:'所有人。'})
            issue=review.load_report(review.report_path(cache))['issues'][0]
            self.assertEqual(issue['status'],'needs_review')
            self.assertEqual(len(issue['recovery_diagnostics']),1)

    def test_failure_advice_matches_font_or_translation(self):
        for reason,word in [('英文字幕字体无法应用：缺字','字体及缺字'),('翻译中断','具体翻译问题')]:
            with tempfile.TemporaryDirectory() as folder:
                _,path=FilmAudit(Path(folder)/'movie.mkv',folder).write('failed',reason,lambda _:None)
                text=Path(path).read_text(encoding='utf-8')
                self.assertIn(word,text)
                self.assertNotIn('再选择完整正片字幕',text)


if __name__=='__main__':unittest.main()
