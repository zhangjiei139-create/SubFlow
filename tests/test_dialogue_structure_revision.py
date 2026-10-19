import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtWidgets import QApplication
import subtitle_fonts as fonts
import subtitle_style_engine as engine
import subtitle_tool_core as core

APP=QApplication.instance() or QApplication([])
FONT=fonts.assets_root()/'bree/BreeSerif-Regular.ttf'
CHINESE=fonts.assets_root()/'chill/ChillRoundF v3.0.ttf'


def settings(font=FONT):
    return dict(fonts.font_info(font),size=52,size_mode='auto',color='#FFFFFF',
                canvas_width=1280,canvas_height=720,dialogue_layout=engine.DIALOGUE_LAYOUT_VERSION)


def packet(text,start=1001,end=4057,order=0,layer='0',actor='',margin='0'):
    return (start,end,f'{order},{layer},Default,{actor},{margin},0,0,,'+fonts.ass_text(text))


def body(events):return ''.join(engine.plain(e[2].split(',',8)[8]) for e in events)


class DialogueStructureRevisionTests(unittest.TestCase):
    def test_english_fragment_joins_with_exact_approved_spaces_and_time(self):
        events=[packet('I just wish\nyou and me were\ngoing to a new home')]
        output,report=engine.layout_dialogue(events,settings(),FONT)
        self.assertEqual(len(output),1)
        self.assertEqual(body(output),'I just wish you and me were going to a new home')
        self.assertEqual(output[0][:2],(1001,4057))
        self.assertEqual([e['after'] for e in report['hardbreak_joins'][0]['edits']],[' ',' '])

    def test_chinese_joins_without_inventing_word_spaces(self):
        text='我问了马林斯先生\n是否可以把这个房间变成我们的新教室'
        output,report=engine.layout_dialogue([packet(text)],settings(CHINESE),CHINESE)
        self.assertEqual(body(output),text.replace('\n',''))
        self.assertEqual(report['hardbreak_joins'][0]['edits'][0]['after'],'')

    def test_existing_spaces_punctuation_and_combining_letters_are_not_normalized(self):
        text='Café  cafe\u0301\nwill stay exactly.'
        output,_=engine.layout_dialogue([packet(text)],settings(),FONT)
        self.assertEqual(body(output),'Café  cafe\u0301 will stay exactly.')

    def test_output_space_loss_is_rejected_despite_approved_join(self):
        original=engine._wrap_dialogue
        def corrupt(units,*args):
            return [[u for u in line if u[1]!=' '] for line in original(units,*args)]
        with patch.object(engine,'_wrap_dialogue',side_effect=corrupt):
            with self.assertRaisesRegex(ValueError,'改变了字幕正文'):
                engine.layout_dialogue([packet('Keep  these\nspaces.')],settings(),FONT)

    def test_italic_and_regular_speech_remain_two_groups(self):
        text='<i>...BY WITNESSES\nWHO CAME FORWARD</i>\nDO YOU THINK\nYOU COULD SHUT OFF THE TV?'
        output,report=engine.layout_dialogue([packet(text)],settings(),FONT)
        self.assertEqual(len(output),2)
        self.assertEqual(engine.plain(output[0][2].split(',',8)[8]),'...BY WITNESSES WHO CAME FORWARD')
        self.assertEqual(engine.plain(output[1][2].split(',',8)[8]),'DO YOU THINK YOU COULD SHUT OFF THE TV?')
        self.assertIn(r'\i1',output[0][2]);self.assertIn(r'\i0',output[1][2])

    def test_semantic_boundaries_including_non_latin_names_are_not_joined(self):
        for text in ('- Are you ready?\n- Yes.','unfinished\njohn: Yes.',
                     '正在交谈\n张三：你好','First sentence.\nAnother one.',
                     'music ♪\nanother lyric','♬ first lyric\nsecond lyric ♬',
                     '♩ first lyric\nsecond lyric ♩','unfinished\n[DOOR SLAMS]',
                     'word-\ncontinuation','First\n\nSecond'):
            with self.subTest(text=text):
                units=engine._dialogue_units(fonts.ass_text(text),settings())
                actual,edits,expected=engine._normalize_dialogue_hardbreaks(units)
                self.assertEqual(edits,[])
                self.assertEqual(expected,engine.plain(fonts.ass_text(text)))

    def test_japanese_korean_and_unmeasured_scripts_keep_their_breaks(self):
        for text in ('これは\n字幕です','한국어\n자막입니다','مرحبا\nبالعالم','Привет\nмир'):
            units=engine._dialogue_units(fonts.ass_text(text),settings())
            _,edits,expected=engine._normalize_dialogue_hardbreaks(units)
            self.assertEqual(edits,[])
            self.assertEqual(expected,text)

    def test_extended_han_does_not_gain_a_latin_word_separator(self):
        text='𠀀\n𠀁'
        units=engine._dialogue_units(fonts.ass_text(text),settings())
        _,edits,expected=engine._normalize_dialogue_hardbreaks(units)
        self.assertEqual(expected,'𠀀𠀁')
        self.assertEqual(edits,[dict(offset=1,before='\n',after='')])

    def test_explicit_actor_margin_layer_and_position_do_not_get_new_joining(self):
        text='Short\ncontinuation'
        for event in (packet(text,actor='Named'),packet(text,margin='12'),packet(text,layer='1'),
                      (1001,4057,r'0,0,Default,,0,0,0,,{\pos(100,50)}Short\Ncontinuation')):
            _,report=engine.layout_dialogue([event],settings(),FONT)
            self.assertEqual(report.get('hardbreak_joins',[]),[])

    def test_exact_duplicate_event_removed_but_independent_events_stay(self):
        first=packet('Redundant drawing')
        output,report=engine.layout_dialogue([first,first],settings(),FONT)
        self.assertEqual(len(output),1)
        self.assertEqual(len(report['duplicate_events_removed']),1)
        for second in (packet('Redundant drawing',order=1),packet('Redundant drawing',start=2000),
                       packet('Redundant drawing',actor='Other'),packet('Redundant drawing',layer='1'),
                       packet('Redundant drawing',margin='12')):
            _,report=engine.layout_dialogue([first,second],settings(),FONT)
            self.assertEqual(report['duplicate_events_removed'],[])

    def test_repeated_body_is_only_reported_never_deleted(self):
        text='Can you hear me?\nYes, I can.\nCan you hear me?\nYes, I can.'
        output,report=engine.layout_dialogue([packet(text)],settings(),FONT)
        self.assertEqual(body(output),text.replace('\n',''))
        self.assertEqual(len(report['repeated_text_warnings']),1)
        self.assertEqual(report['repeated_text_warnings'][0]['action'],'retained_for_confirmation')
        self.assertEqual(report['duplicate_events_removed'],[])

    def test_legacy_rule_keeps_its_hard_breaks_and_duplicates(self):
        event=packet('First\ncontinuation')
        old=dict(settings(),dialogue_layout=engine.LEGACY_DIALOGUE_LAYOUT_VERSION)
        output,report=engine.layout_dialogue([event,event],old,FONT)
        self.assertEqual(len(output),4)
        self.assertEqual(report['hardbreak_joins'],[])
        self.assertEqual(report['duplicate_events_removed'],[])

    def test_diagnostic_json_has_precise_locations_without_mutating_input(self):
        text='Can you hear me?\nYes, I can.\nCan you hear me?\nYes, I can.'
        events=[packet(text)];before=list(events);messages=[]
        with tempfile.TemporaryDirectory() as folder:
            fonts.layout_styled_events(events,settings(),fonts.header(settings()),raw=FONT,
                                       code='en',work=folder,log=messages.append)
            diagnostic=next(Path(folder).glob('subtitle-layout-*.json'))
            document=json.loads(diagnostic.read_text(encoding='utf8'))
            warning=document['repeated_text_warnings'][0]
            self.assertEqual((warning['cue'],warning['start_ms'],warning['end_ms']),(1,1001,4057))
            self.assertTrue(any('未作为处理失败' in s for s in messages))
            self.assertEqual(events,before)

    def test_diagnostic_io_or_logger_errors_do_not_fail_good_output(self):
        report=dict(layout_version=engine.DIALOGUE_LAYOUT_VERSION,
                    hardbreak_joins=[dict(edits=[{}])],repeated_text_warnings=[],
                    duplicate_events_removed=[],multiline_cues=0)
        with tempfile.TemporaryDirectory() as folder,patch.object(Path,'write_text',side_effect=OSError('disk')):
            fonts._record_layout_diagnostics(report,[packet('Good')],settings(),'en',folder,
                lambda _:(_ for _ in ()).throw(RuntimeError('logger')),None)

    def test_diagnostic_cancellation_is_not_swallowed(self):
        report=dict(layout_version=engine.DIALOGUE_LAYOUT_VERSION,
                    hardbreak_joins=[dict(edits=[{}])],repeated_text_warnings=[],
                    duplicate_events_removed=[],multiline_cues=0)
        with self.assertRaises(core.CancelledError):
            fonts._record_layout_diagnostics(report,[packet('Good')],settings(),'en',None,
                lambda _:(_ for _ in ()).throw(core.CancelledError()),None)


if __name__=='__main__':unittest.main()
